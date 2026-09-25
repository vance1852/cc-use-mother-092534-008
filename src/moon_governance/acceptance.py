"""云赏月互动内容治理服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, DomainError, PermissionDenied
from festival_foundation.storage import Database

from .service import GovernanceService

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def _content(text: str, scope: str) -> dict[str, Any]:
    return {"text_body": text,
            "media_summaries": [{"media_id": "media-1", "media_type": "image",
                                 "checksum": "sha256:demo", "width": 640, "height": 480}],
            "declaration": {"original": True, "grant": "授权云赏月活动专题引用"},
            "citations": [{"reference": "苏轼《水调歌头》", "source_type": "classic"}],
            "visibility_scope": scope}


def _setup(service: GovernanceService) -> None:
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-moon", name="云赏月运营中心")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-1",
                           display_name="平台管理员", role="admin", organization_id="org-moon")
    service.register_actor(request_id="req-operator", actor_id="admin-1", new_actor_id="ops-1",
                           display_name="运营人员", role="operator", organization_id="org-moon")
    service.register_actor(request_id="req-rev1", actor_id="admin-1", new_actor_id="rev-1",
                           display_name="审核者甲", role="reviewer", organization_id="org-moon")
    service.register_actor(request_id="req-rev2", actor_id="admin-1", new_actor_id="rev-2",
                           display_name="审核者乙", role="reviewer", organization_id="org-moon")
    service.register_site(request_id="req-site", actor_id="ops-1", site_id="moon-live",
                          organization_id="org-moon", name="云赏月主会场",
                          timezone_name="Asia/Shanghai")


def _submit(service: GovernanceService, submission_id: str, kind: str, text: str,
            scope: str, request_id: str) -> dict[str, Any]:
    return service.submit_submission(request_id=request_id, actor_id="ops-1", site_id="moon-live",
                                     submission_id=submission_id, kind=kind, author_id="user-1001",
                                     **_content(text, scope))


def _decide_open_task(service: GovernanceService, submission_id: str, reviewer: str,
                      decision: str, request_tag: str) -> dict[str, Any]:
    task = next(item for item in service.review_queue() + service.appeal_queue()
                if item["submission_id"] == submission_id)
    claim = service.claim_review(request_id=f"req-claim-{request_tag}", actor_id=reviewer,
                                 task_id=task["task_id"])
    return service.decide_review(request_id=f"req-decide-{request_tag}", actor_id=reviewer,
                                 task_id=task["task_id"], lease_token=claim["lease_token"],
                                 decision=decision, reason="验收审核结论")


def run() -> dict[str, Any]:
    """执行完整治理链路并返回验收结果，中途模拟一次服务重启。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "governance.sqlite3"
        database = Database(path)
        service = GovernanceService(database, FixedClock(NOW))
        _setup(service)
        checks: dict[str, Any] = {}

        # 投稿进入待审队列
        _submit(service, "sub-poem", "poetry_chain", "明月几时有，把酒问青天。", "public", "req-sub-poem")
        _submit(service, "sub-home", "hometown_intro", "我的家乡在钱塘江畔。", "event", "req-sub-home")
        _submit(service, "sub-bless", "blessing", "但愿人长久，千里共婵娟。", "public", "req-sub-bless")
        checks["initial_queue_three"] = len(service.review_queue()) == 3

        # 相同投稿请求安全重放，不同载荷复用编号明确冲突
        replay = _submit(service, "sub-poem", "poetry_chain", "明月几时有，把酒问青天。",
                         "public", "req-sub-poem")
        checks["submit_replayed"] = replay["replayed"]
        try:
            _submit(service, "sub-poem", "poetry_chain", "被篡改的内容。", "public", "req-sub-poem")
            checks["payload_conflict"] = False
        except ConflictError:
            checks["payload_conflict"] = True

        # 审核：通过与驳回
        _decide_open_task(service, "sub-poem", "rev-1", "approve", "poem")
        _decide_open_task(service, "sub-home", "rev-1", "reject", "home")
        _decide_open_task(service, "sub-bless", "rev-2", "approve", "bless")
        checks["rejected_restricted"] = \
            service.explain_submission("sub-home")["reason_code"] == "rejected"

        # 复议必须由另一名审核者处理
        service.request_appeal(request_id="req-appeal-home", actor_id="ops-1",
                               submission_id="sub-home", reason="作者补充了家乡出处说明")
        appeal_task = service.appeal_queue()[0]
        try:
            service.claim_review(request_id="req-claim-appeal-self", actor_id="rev-1",
                                 task_id=appeal_task["task_id"])
            checks["appeal_self_blocked"] = False
        except PermissionDenied:
            checks["appeal_self_blocked"] = True
        _decide_open_task(service, "sub-home", "rev-2", "overturn", "appeal-home")
        checks["appeal_visible"] = service.explain_submission("sub-home")["state"] == "visible"

        # 专题引用与发布冻结
        service.create_topic(request_id="req-topic", actor_id="ops-1", topic_id="topic-moon",
                             site_id="moon-live", title="中秋云赏月精选", audience_scope="event")
        try:
            service.add_topic_member(request_id="req-add-bad", actor_id="ops-1",
                                     topic_id="topic-moon", submission_id="sub-poem",
                                     version_id="not-a-version")
            checks["bad_member_rejected"] = False
        except DomainError:
            checks["bad_member_rejected"] = True
        poem_v1 = service.get_submission("sub-poem")["versions"][0]["version_id"]
        home_v1 = service.get_submission("sub-home")["versions"][0]["version_id"]
        bless_v1 = service.get_submission("sub-bless")["versions"][0]["version_id"]
        service.add_topic_member(request_id="req-add-poem", actor_id="ops-1",
                                 topic_id="topic-moon", submission_id="sub-poem",
                                 version_id=poem_v1, position=0)
        service.add_topic_member(request_id="req-add-home", actor_id="ops-1",
                                 topic_id="topic-moon", submission_id="sub-home",
                                 version_id=home_v1, position=1)
        service.add_topic_member(request_id="req-add-bless", actor_id="ops-1",
                                 topic_id="topic-moon", submission_id="sub-bless",
                                 version_id=bless_v1, position=2)
        published = service.publish_topic(request_id="req-publish", actor_id="ops-1",
                                          topic_id="topic-moon")
        checks["published_members_three"] = published["members"] == 3
        checks["publish_replayed"] = service.publish_topic(
            request_id="req-publish", actor_id="ops-1", topic_id="topic-moon")["replayed"]

        # 作者修改产生新版本，已发布专题不自动漂移
        service.revise_submission(request_id="req-revise-poem", actor_id="ops-1",
                                  submission_id="sub-poem",
                                  **_content("明月几时有，把酒问青天。（修订版）", "public"))
        topic_view = service.get_topic("topic-moon")
        poem_item = next(item for item in topic_view["items"]
                         if item["submission_id"] == "sub-poem")
        checks["topic_keeps_pinned_version"] = (
            poem_item["version_id"] == poem_v1
            and poem_item["content"]["text_body"] == "明月几时有，把酒问青天。")
        checks["revision_restricted"] = \
            service.explain_submission("sub-poem")["reason_code"] == "pending_review"
        checks["old_version_replaced"] = \
            service.explain_version(poem_v1)["state"] == "replaced"

        # 撤回阻止未来读取，专题成员保留审计占位
        service.withdraw_submission(request_id="req-withdraw-bless", actor_id="ops-1",
                                    submission_id="sub-bless", reason="作者申请撤回")
        bless_read = service.read_submission_content("sub-bless")
        checks["withdraw_blocks_read"] = bless_read["state"] == "blocked" \
            and bless_read["placeholder"] and "content" not in bless_read
        bless_item = next(item for item in service.get_topic("topic-moon")["items"]
                          if item["submission_id"] == "sub-bless")
        checks["topic_placeholder"] = bless_item["state"] == "blocked" \
            and bless_item.get("placeholder") is True

        # 规则升级只重新评估未终结投稿
        upgrade = service.upgrade_rules(request_id="req-rules-v2", actor_id="admin-1",
                                        note="新增媒体摘要核验规则")
        checks["rule_requeued_only_open"] = upgrade["requeued"] == 2
        checks["upgrade_restricted"] = \
            service.explain_submission("sub-home")["reason_code"] == "rule_upgrade_pending"
        _decide_open_task(service, "sub-home", "rev-1", "approve", "home-reeval")
        checks["reeval_restored"] = service.explain_submission("sub-home")["state"] == "visible"

        # 模拟服务重启：待审与待复议队列继续保留
        pending_before = len(service.review_queue())
        database.close()
        database = Database(path)
        service = GovernanceService(database, FixedClock(NOW))
        checks["queue_survives_restart"] = len(service.review_queue()) == pending_before \
            and pending_before > 0
        checks["appeal_queue_empty"] = len(service.appeal_queue()) == 0

        valid, event_count = service.verify_audit()
        checks["audit_events"] = event_count
        result = {"status": "ok" if all(
            value for key, value in checks.items() if isinstance(value, bool)) else "failed",
            "audit_valid": valid, "checks": checks}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
