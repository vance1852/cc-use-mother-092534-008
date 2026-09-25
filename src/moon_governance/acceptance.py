"""运行云赏月内容治理服务的离线端到端验收。

覆盖投稿登记、审核租约、复议、版本漂移、专题发布、撤回占位、规则升级、
幂等重放与服务重启后的队列恢复，成功时输出一行 status 为 ok 的 JSON。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, PermissionDenied
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .service import MoonGovernanceService


CLOCK = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
DECLARATION = {"original": True, "license_scopes": ["event_display", "collection_reuse"]}


def _bootstrap(foundation: DomainService) -> None:
    foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                     organization_id="org-moon", name="云赏月活动组委会")
    foundation.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-1",
                              display_name="系统管理员", role="admin", organization_id="org-moon")
    foundation.register_actor(request_id="acc-operator", actor_id="admin-1", new_actor_id="operator-1",
                              display_name="运营编辑", role="operator", organization_id="org-moon")
    foundation.register_actor(request_id="acc-reviewer-1", actor_id="admin-1", new_actor_id="reviewer-1",
                              display_name="审核员甲", role="reviewer", organization_id="org-moon")
    foundation.register_actor(request_id="acc-reviewer-2", actor_id="admin-1", new_actor_id="reviewer-2",
                              display_name="审核员乙", role="reviewer", organization_id="org-moon")
    foundation.register_site(request_id="acc-site", actor_id="operator-1", site_id="moon-site",
                             organization_id="org-moon", name="云赏月主会场", timezone_name="Asia/Shanghai")


def _submit(moon: MoonGovernanceService, request_id: str, kind: str, author_id: str,
            text_body: str, **extra):
    payload = {"request_id": request_id, "actor_id": "operator-1", "site_id": "moon-site",
               "kind": kind, "author_id": author_id, "text_body": text_body,
               "declaration": DECLARATION, "visibility_scope": "public"}
    payload.update(extra)
    return moon.submit_submission(**payload)


def _open_task_for(moon: MoonGovernanceService, submission_id: str, kind: str = "initial") -> str:
    for item in moon.list_review_queue(kind):
        if item["submission_id"] == submission_id:
            return item["task_id"]
    raise AssertionError(f"没有找到 {submission_id} 的 {kind} 任务")


def _decide(moon: MoonGovernanceService, request_id: str, reviewer: str,
            task_id: str, decision: str, reason: str = ""):
    moon.acquire_lease(actor_id=reviewer, task_id=task_id, lease_seconds=300)
    return moon.submit_decision(request_id=request_id, actor_id=reviewer,
                                task_id=task_id, decision=decision, reason=reason)


def run() -> dict[str, object]:
    """执行完整治理流程，中途重启一次服务，返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "moon_acceptance.sqlite3"
        database = Database(path)
        foundation = DomainService(database, CLOCK)
        moon = MoonGovernanceService(database, CLOCK)
        _bootstrap(foundation)

        # 1. 登记三类投稿：诗词接龙、家乡介绍、祝福留言
        poetry = _submit(moon, "acc-poetry", "poetry_chain", "user-1001",
                         "海上生明月，天涯共此时。",
                         media_summaries=[{"media_id": "img-01", "kind": "image",
                                           "sha256": "a" * 64, "width": 1080, "height": 720,
                                           "description": "江面月色照片"}])
        hometown = _submit(moon, "acc-hometown", "hometown_intro", "user-1002",
                           "我的家乡在钱塘江畔，中秋夜潮声伴月。",
                           declaration={"original": False,
                                        "license_scopes": ["event_display", "collection_reuse"]},
                           citations=[{"source": "《钱塘县志》", "note": "风俗段落摘录"}],
                           media_summaries=[{"media_id": "vid-01", "kind": "video",
                                             "sha256": "b" * 64, "duration_ms": 32000}])
        blessing = _submit(moon, "acc-blessing", "blessing", "user-1003", "但愿人长久，千里共婵娟。")

        # 2. 幂等：相同投稿请求安全重放，不同载荷复用编号明确冲突
        replay = _submit(moon, "acc-poetry", "poetry_chain", "user-1001",
                         "海上生明月，天涯共此时。",
                         media_summaries=[{"media_id": "img-01", "kind": "image",
                                           "sha256": "a" * 64, "width": 1080, "height": 720,
                                           "description": "江面月色照片"}])
        conflict_detected = False
        try:
            _submit(moon, "acc-poetry", "blessing", "user-1001", "被篡改的载荷")
        except ConflictError:
            conflict_detected = True

        # 3. 审核：两人同时处理时由租约保证唯一有效决定
        poetry_task = _open_task_for(moon, poetry.resource_id)
        hometown_task = _open_task_for(moon, hometown.resource_id)
        blessing_task = _open_task_for(moon, blessing.resource_id)
        moon.acquire_lease(actor_id="reviewer-1", task_id=poetry_task, lease_seconds=300)
        lease_conflict = False
        try:
            moon.acquire_lease(actor_id="reviewer-2", task_id=poetry_task, lease_seconds=300)
        except ConflictError:
            lease_conflict = True
        moon.submit_decision(request_id="acc-d1", actor_id="reviewer-1",
                             task_id=poetry_task, decision="approve")
        _decide(moon, "acc-d2", "reviewer-1", hometown_task, "approve")
        _decide(moon, "acc-d3", "reviewer-1", blessing_task, "reject", "祝福语与活动主题不符")
        duplicate_decision = False
        try:
            _decide(moon, "acc-d4", "reviewer-2", blessing_task, "approve")
        except ConflictError:
            duplicate_decision = True

        # 4. 复议：必须由另一名审核者处理
        moon.request_appeal(request_id="acc-appeal", actor_id="operator-1",
                            submission_id=blessing.resource_id, version=1, reason="作者补充了创作背景")
        appeal_task = _open_task_for(moon, blessing.resource_id, "appeal")
        same_reviewer_blocked = False
        try:
            moon.acquire_lease(actor_id="reviewer-1", task_id=appeal_task, lease_seconds=300)
        except PermissionDenied:
            same_reviewer_blocked = True
        _decide(moon, "acc-d5", "reviewer-2", appeal_task, "approve", "复议确认可以展示")
        appeal_overturned = moon.get_version(blessing.resource_id, 1)["review_state"] == "approved"

        # 5. 作者修改产生新版本，旧专题引用不漂移
        moon.add_version(request_id="acc-poetry-v2", actor_id="operator-1",
                         submission_id=poetry.resource_id, author_id="user-1001",
                         text_body="海上生明月，天涯共此时。情人怨遥夜，竟夕起相思。",
                         declaration=DECLARATION, visibility_scope="public",
                         media_summaries=[{"media_id": "img-01", "kind": "image",
                                           "sha256": "a" * 64, "description": "江面月色照片"}])

        # 6. 专题：只引用已通过且授权匹配的确定版本，发布时冻结成员清单
        collection = moon.create_collection(request_id="acc-collection", actor_id="operator-1",
                                            site_id="moon-site", title="中秋诗词夜",
                                            usage_scope="collection_reuse")
        collection_id = collection.resource_id
        moon.add_collection_item(request_id="acc-item-1", actor_id="operator-1",
                                 collection_id=collection_id,
                                 submission_id=poetry.resource_id, version=1)
        moon.add_collection_item(request_id="acc-item-2", actor_id="operator-1",
                                 collection_id=collection_id,
                                 submission_id=hometown.resource_id, version=1)
        moon.add_collection_item(request_id="acc-item-3", actor_id="operator-1",
                                 collection_id=collection_id,
                                 submission_id=blessing.resource_id, version=1)
        publish = moon.publish_collection(request_id="acc-publish", actor_id="operator-1",
                                          collection_id=collection_id)
        publish_replay = moon.publish_collection(request_id="acc-publish", actor_id="operator-1",
                                                 collection_id=collection_id)

        # 7. 规则升级：只重新评估未终结投稿（诗词 v2 仍在队列中）
        upgrade = moon.upgrade_rules(request_id="acc-rules", actor_id="admin-1",
                                     note="上线新版内容规范")
        queue_after_upgrade = moon.list_review_queue()
        upgraded = all(item["rule_version"] == 2 for item in queue_after_upgrade)

        # 8. 审核诗词 v2（按新规则），随后撤回家乡介绍
        poetry_v2_task = _open_task_for(moon, poetry.resource_id)
        _decide(moon, "acc-d6", "reviewer-1", poetry_v2_task, "approve")
        moon.withdraw_submission(request_id="acc-withdraw", actor_id="operator-1",
                                 submission_id=hometown.resource_id, reason="作者要求撤回")

        published = moon.get_published_collection(collection_id)
        member_by_submission = {m["submission_id"]: m for m in published["members"]}
        no_drift = member_by_submission[poetry.resource_id]["version"] == 1
        withdrawn_placeholder = (
            not member_by_submission[hometown.resource_id]["available"]
            and member_by_submission[hometown.resource_id]["placeholder"]
            and member_by_submission[hometown.resource_id]["content"] is None)
        tombstoned = moon.get_version(hometown.resource_id, 1)["placeholder"]
        explain_hometown = moon.explain_submission(hometown.resource_id)
        explain_poetry = moon.explain_submission(poetry.resource_id)

        # 9. 留下一条待审投稿用于验证重启恢复
        late = _submit(moon, "acc-late", "blessing", "user-1004", "月圆人团圆。")
        queue_before_restart = len(moon.list_review_queue())
        valid_before, events_before = foundation.verify_audit()
        database.close()

        # 10. 模拟服务重启：同一数据库文件重新建服务，队列与审计链继续保留
        database2 = Database(path)
        foundation2 = DomainService(database2, CLOCK)
        moon2 = MoonGovernanceService(database2, CLOCK)
        queue_after_restart = moon2.list_review_queue()
        appeal_queue_after_restart = moon2.list_review_queue("appeal")
        valid_after, events_after = foundation2.verify_audit()
        explain_after_restart = moon2.explain_submission(poetry.resource_id)
        database2.close()

        return {
            "status": "ok",
            "submissions": 4,
            "replayed": replay.replayed and publish_replay.replayed,
            "conflict_detected": conflict_detected,
            "lease_conflict": lease_conflict,
            "duplicate_decision_blocked": duplicate_decision,
            "same_reviewer_blocked": same_reviewer_blocked,
            "appeal_overturned": appeal_overturned,
            "published_members": len(published["members"]),
            "no_drift": no_drift,
            "withdrawn_placeholder": withdrawn_placeholder and tombstoned,
            "explain_hometown": explain_hometown["effective_state"],
            "explain_poetry": explain_poetry["effective_state"],
            "rule_version": upgrade.resource_id,
            "queue_requeued": upgraded,
            "queue_before_restart": queue_before_restart,
            "queue_after_restart": len(queue_after_restart),
            "appeal_queue_after_restart": len(appeal_queue_after_restart),
            "late_pending": any(item["submission_id"] == late.resource_id for item in queue_after_restart),
            "explain_after_restart": explain_after_restart["effective_state"],
            "audit_valid": valid_before and valid_after,
            "audit_events": events_after,
            "audit_events_before_restart": events_before,
        }


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = [value for key, value in result.items()
              if key not in ("status", "submissions", "published_members", "queue_before_restart",
                             "queue_after_restart", "appeal_queue_after_restart",
                             "audit_events", "audit_events_before_restart",
                             "explain_hometown", "explain_poetry", "explain_after_restart",
                             "rule_version")]
    ok = (result["status"] == "ok" and all(checks)
          and result["explain_hometown"] == "blocked"
          and result["explain_poetry"] == "visible"
          and result["explain_after_restart"] == "visible"
          and result["queue_after_restart"] == result["queue_before_restart"] >= 1)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
