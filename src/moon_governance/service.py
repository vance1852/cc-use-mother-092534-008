"""云赏月互动内容治理服务：投稿版本、审核租约、复议、规则升级与专题冻结。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from festival_foundation.audit import append_event, canonical_json, digest
from festival_foundation.clock import Clock
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.models import Actor
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .policies import (
    BLOCKED_STATUSES,
    REASON_LIMIT,
    SCOPE_RANK,
    TITLE_LIMIT,
    scope_covers,
    validate_citations,
    validate_declaration,
    validate_kind,
    validate_media,
    validate_scope,
    validate_text,
)
from .storage import ensure_governance_schema

MIN_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 3600
DEFAULT_LEASE_SECONDS = 900


class GovernanceService(DomainService):
    """在基础层之上实现云赏月互动内容的治理规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        super().__init__(database, clock)
        ensure_governance_schema(self.database.connection, self._now())

    # ---- 基础工具 ----

    def _idempotent_call(self, connection, *, request_id: str, action: str,
                         payload: dict[str, Any],
                         create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        """相同请求安全重放返回原响应，不同载荷复用编号则明确冲突。"""
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "replayed": True,
                    "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "replayed": False,
                "resource_type": resource_type, "resource_id": resource_id, **response}

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _current_rule_version(self, connection) -> int:
        return connection.execute("SELECT MAX(rule_version) AS version FROM rule_registry").fetchone()["version"]

    def _site_for_actor(self, connection, actor: Actor, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.role != "admin" and actor.organization_id != row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")
        return row

    def _get_submission_row(self, connection, submission_id: str):
        row = connection.execute("SELECT * FROM submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if row is None:
            raise NotFoundError("投稿不存在")
        return row

    def _get_version_row(self, connection, version_id: str):
        row = connection.execute(
            "SELECT * FROM submission_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("投稿版本不存在")
        return row

    def _get_task_row(self, connection, task_id: str):
        row = connection.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("审核任务不存在")
        return row

    def _get_topic_row(self, connection, topic_id: str):
        row = connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        if row is None:
            raise NotFoundError("专题不存在")
        return row

    def _cancel_open_tasks(self, connection, submission_id: str) -> None:
        connection.execute(
            "UPDATE review_tasks SET status='cancelled' WHERE submission_id=? AND status IN ('pending','leased')",
            (submission_id,),
        )

    def _open_task(self, connection, submission_id: str):
        return connection.execute(
            "SELECT * FROM review_tasks WHERE submission_id=? AND status IN ('pending','leased') "
            "ORDER BY created_at LIMIT 1",
            (submission_id,),
        ).fetchone()

    def _create_task(self, connection, *, submission_id: str, version_id: str,
                     kind: str, rule_version: int) -> str:
        task_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO review_tasks(task_id,submission_id,version_id,kind,rule_version,status,created_at) "
            "VALUES(?,?,?,?,?,'pending',?)",
            (task_id, submission_id, version_id, kind, rule_version, self._now()),
        )
        return task_id

    def _content_fields(self, *, text_body: Any, media_summaries: Any, declaration: Any,
                        citations: Any, visibility_scope: Any) -> dict[str, Any]:
        return {
            "text_body": validate_text(text_body),
            "media_summaries": validate_media(media_summaries),
            "declaration": validate_declaration(declaration),
            "citations": validate_citations(citations),
            "visibility_scope": validate_scope(visibility_scope),
        }

    def _insert_version(self, connection, *, submission_id: str, version_no: int,
                        content: dict[str, Any], actor_id: str,
                        version_id: str | None = None) -> tuple[str, str]:
        version_id = version_id or uuid.uuid4().hex
        content_hash = digest({"submission_id": submission_id, "version_no": version_no, **content})
        connection.execute(
            "INSERT INTO submission_versions(version_id,submission_id,version_no,text_body,media_json,"
            "declaration_json,citations_json,visibility_scope,review_status,decision_json,content_hash,"
            "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'pending',NULL,?,?,?)",
            (version_id, submission_id, version_no, content["text_body"],
             canonical_json(content["media_summaries"]), canonical_json(content["declaration"]),
             canonical_json(content["citations"]), content["visibility_scope"],
             content_hash, actor_id, self._now()),
        )
        return version_id, content_hash

    # ---- 投稿生命周期 ----

    def submit_submission(self, *, request_id: str, actor_id: str, site_id: str, submission_id: str,
                          kind: str, author_id: str, text_body: Any, media_summaries: Any,
                          declaration: Any, citations: Any, visibility_scope: Any) -> dict[str, Any]:
        """登记一条新投稿，只保存文本与媒体摘要，并进入待审队列。"""
        payload = {"actor_id": actor_id, "site_id": site_id, "submission_id": submission_id,
                   "kind": kind, "author_id": author_id, "text_body": text_body,
                   "media_summaries": media_summaries, "declaration": declaration,
                   "citations": citations, "visibility_scope": visibility_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            submission_id = self._identifier(submission_id, "submission_id")
            author_id = self._identifier(author_id, "author_id")
            kind = validate_kind(kind)
            content = self._content_fields(text_body=text_body, media_summaries=media_summaries,
                                           declaration=declaration, citations=citations,
                                           visibility_scope=visibility_scope)
            rule_version = self._current_rule_version(connection)

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM submissions WHERE submission_id=?",
                                      (submission_id,)).fetchone():
                    raise ConflictError("投稿编号已经存在")
                now = self._now()
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO submissions(submission_id,site_id,kind,author_id,status,current_version_id,"
                    "created_by,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?,?)",
                    (submission_id, site_id, kind, author_id, version_id, actor_id, now, now),
                )
                version_id, content_hash = self._insert_version(
                    connection, submission_id=submission_id, version_no=1,
                    content=content, actor_id=actor_id, version_id=version_id)
                task_id = self._create_task(connection, submission_id=submission_id,
                                            version_id=version_id, kind="initial",
                                            rule_version=rule_version)
                append_event(connection, actor_id=actor_id, action="submission.submitted",
                             resource_type="submission", resource_id=submission_id,
                             detail={"site_id": site_id, "kind": kind, "author_id": author_id,
                                     "version_id": version_id, "content_hash": content_hash,
                                     "rule_version": rule_version},
                             occurred_at=now)
                return "submission", submission_id, {
                    "submission_id": submission_id, "version_id": version_id, "version_no": 1,
                    "task_id": task_id, "status": "pending", "rule_version": rule_version}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.submit", payload=payload, create=create)

    def revise_submission(self, *, request_id: str, actor_id: str, submission_id: str,
                          text_body: Any, media_summaries: Any, declaration: Any,
                          citations: Any, visibility_scope: Any) -> dict[str, Any]:
        """作者修改产生新版本，旧版本保留，已引用旧版本的专题不自动漂移。"""
        payload = {"actor_id": actor_id, "submission_id": submission_id, "text_body": text_body,
                   "media_summaries": media_summaries, "declaration": declaration,
                   "citations": citations, "visibility_scope": visibility_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            submission = self._get_submission_row(connection, submission_id)
            self._site_for_actor(connection, actor, submission["site_id"])
            content = self._content_fields(text_body=text_body, media_summaries=media_summaries,
                                           declaration=declaration, citations=citations,
                                           visibility_scope=visibility_scope)
            rule_version = self._current_rule_version(connection)

            def create() -> tuple[str, str, dict[str, Any]]:
                if submission["status"] in BLOCKED_STATUSES:
                    raise ConflictError("已撤回或已处置的投稿不能修改")
                row = connection.execute(
                    "SELECT MAX(version_no) AS version FROM submission_versions WHERE submission_id=?",
                    (submission_id,)).fetchone()
                version_no = row["version"] + 1
                now = self._now()
                self._cancel_open_tasks(connection, submission_id)
                version_id, content_hash = self._insert_version(
                    connection, submission_id=submission_id, version_no=version_no,
                    content=content, actor_id=actor_id)
                connection.execute(
                    "UPDATE submissions SET status='pending', current_version_id=?, updated_at=? "
                    "WHERE submission_id=?",
                    (version_id, now, submission_id),
                )
                task_id = self._create_task(connection, submission_id=submission_id,
                                            version_id=version_id, kind="initial",
                                            rule_version=rule_version)
                append_event(connection, actor_id=actor_id, action="submission.revised",
                             resource_type="submission", resource_id=submission_id,
                             detail={"version_id": version_id, "version_no": version_no,
                                     "content_hash": content_hash, "rule_version": rule_version},
                             occurred_at=now)
                return "version", version_id, {
                    "submission_id": submission_id, "version_id": version_id,
                    "version_no": version_no, "task_id": task_id, "status": "pending"}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.revise", payload=payload, create=create)

    def withdraw_submission(self, *, request_id: str, actor_id: str, submission_id: str,
                            reason: str) -> dict[str, Any]:
        """作者撤回投稿，阻止未来读取并保留审计占位。"""
        payload = {"actor_id": actor_id, "submission_id": submission_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            submission = self._get_submission_row(connection, submission_id)
            self._site_for_actor(connection, actor, submission["site_id"])
            reason = validate_text(reason, "reason", REASON_LIMIT)

            def create() -> tuple[str, str, dict[str, Any]]:
                if submission["status"] == "withdrawn":
                    raise ConflictError("投稿已撤回")
                if submission["status"] == "compliance_removed":
                    raise ConflictError("投稿已被合规处置")
                now = self._now()
                self._cancel_open_tasks(connection, submission_id)
                connection.execute(
                    "UPDATE submissions SET status='withdrawn', updated_at=? WHERE submission_id=?",
                    (now, submission_id),
                )
                append_event(connection, actor_id=actor_id, action="submission.withdrawn",
                             resource_type="submission", resource_id=submission_id,
                             detail={"reason": reason,
                                     "current_version_id": submission["current_version_id"]},
                             occurred_at=now)
                return "submission", submission_id, {"submission_id": submission_id,
                                                     "status": "withdrawn"}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.withdraw", payload=payload, create=create)

    def compliance_remove(self, *, request_id: str, actor_id: str, submission_id: str,
                          reason: str) -> dict[str, Any]:
        """合规处置投稿，阻止未来读取并保留审计占位。"""
        payload = {"actor_id": actor_id, "submission_id": submission_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            submission = self._get_submission_row(connection, submission_id)
            reason = validate_text(reason, "reason", REASON_LIMIT)

            def create() -> tuple[str, str, dict[str, Any]]:
                if submission["status"] == "compliance_removed":
                    raise ConflictError("投稿已被合规处置")
                now = self._now()
                self._cancel_open_tasks(connection, submission_id)
                connection.execute(
                    "UPDATE submissions SET status='compliance_removed', updated_at=? "
                    "WHERE submission_id=?",
                    (now, submission_id),
                )
                append_event(connection, actor_id=actor_id, action="submission.compliance_removed",
                             resource_type="submission", resource_id=submission_id,
                             detail={"reason": reason,
                                     "current_version_id": submission["current_version_id"]},
                             occurred_at=now)
                return "submission", submission_id, {"submission_id": submission_id,
                                                     "status": "compliance_removed"}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.compliance_remove", payload=payload,
                                         create=create)

    # ---- 审核租约与决定 ----

    def claim_review(self, *, request_id: str, actor_id: str, task_id: str,
                     lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
        """认领审核任务，租约保证多人审核时只有唯一有效决定。"""
        payload = {"actor_id": actor_id, "task_id": task_id, "lease_seconds": lease_seconds}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            task = self._get_task_row(connection, task_id)
            try:
                lease_seconds = int(lease_seconds)
            except (TypeError, ValueError) as exc:
                raise ValidationError("lease_seconds 必须是整数") from exc
            if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
                raise ValidationError(
                    f"lease_seconds 需在 {MIN_LEASE_SECONDS} 到 {MAX_LEASE_SECONDS} 之间")

            def create() -> tuple[str, str, dict[str, Any]]:
                now_dt = self.clock.now()
                if task["status"] in ("decided", "cancelled"):
                    raise ConflictError("审核任务已关闭")
                if task["kind"] == "appeal":
                    version = self._get_version_row(connection, task["version_id"])
                    prior = json.loads(version["decision_json"]) if version["decision_json"] else {}
                    if prior.get("decided_by") == actor_id:
                        raise PermissionDenied("复议须由另一名审核者处理")
                if task["status"] == "leased" and self._parse_time(task["lease_expires_at"]) > now_dt:
                    if task["lease_owner"] != actor_id:
                        raise ConflictError("审核任务已被其他审核者租用")
                    return "review_task", task_id, {
                        "task_id": task_id, "lease_token": task["lease_token"],
                        "lease_expires_at": task["lease_expires_at"], "lease_owner": actor_id}
                token = uuid.uuid4().hex
                expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
                connection.execute(
                    "UPDATE review_tasks SET status='leased', lease_owner=?, lease_token=?, "
                    "lease_expires_at=? WHERE task_id=?",
                    (actor_id, token, expires_at, task_id),
                )
                append_event(connection, actor_id=actor_id, action="review.lease_acquired",
                             resource_type="review_task", resource_id=task_id,
                             detail={"submission_id": task["submission_id"], "kind": task["kind"],
                                     "lease_owner": actor_id, "lease_expires_at": expires_at},
                             occurred_at=self._now())
                return "review_task", task_id, {
                    "task_id": task_id, "lease_token": token,
                    "lease_expires_at": expires_at, "lease_owner": actor_id}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.claim", payload=payload, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, task_id: str,
                      lease_token: str, decision: str, reason: str) -> dict[str, Any]:
        """租约持有人在租约有效期内作出唯一有效审核决定。"""
        payload = {"actor_id": actor_id, "task_id": task_id, "lease_token": lease_token,
                   "decision": decision, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer", "admin")
            task = self._get_task_row(connection, task_id)
            decision = str(decision).strip()
            reason = validate_text(reason, "reason", REASON_LIMIT)

            def create() -> tuple[str, str, dict[str, Any]]:
                if task["status"] == "decided":
                    raise ConflictError("审核任务已有有效决定")
                if task["status"] == "cancelled":
                    raise ConflictError("审核任务已取消")
                if task["status"] != "leased":
                    raise ConflictError("审核任务尚未认领")
                if task["lease_owner"] != actor_id:
                    raise PermissionDenied("只有租约持有人可以作出决定")
                if task["lease_token"] != lease_token:
                    raise PermissionDenied("租约令牌无效")
                if self._parse_time(task["lease_expires_at"]) <= self.clock.now():
                    raise ConflictError("租约已过期，请重新认领")
                submission = self._get_submission_row(connection, task["submission_id"])
                version = self._get_version_row(connection, task["version_id"])
                if submission["current_version_id"] != task["version_id"]:
                    raise ConflictError("审核任务已失效")
                kind = task["kind"]
                if kind == "appeal":
                    if decision not in ("overturn", "uphold"):
                        raise ValidationError("复议决定必须是 overturn 或 uphold")
                    prior = json.loads(version["decision_json"]) if version["decision_json"] else {}
                    if prior.get("decided_by") == actor_id:
                        raise PermissionDenied("复议须由另一名审核者处理")
                    review_status = "approved" if decision == "overturn" else "rejected"
                else:
                    if decision not in ("approve", "reject"):
                        raise ValidationError("审核决定必须是 approve 或 reject")
                    review_status = "approved" if decision == "approve" else "rejected"
                now = self._now()
                decision_record = {"decided_by": actor_id, "decision": decision, "reason": reason,
                                   "rule_version": task["rule_version"], "task_id": task_id,
                                   "task_kind": kind, "decided_at": now}
                connection.execute(
                    "UPDATE submission_versions SET review_status=?, decision_json=? WHERE version_id=?",
                    (review_status, canonical_json(decision_record), task["version_id"]),
                )
                connection.execute(
                    "UPDATE submissions SET status=?, updated_at=? WHERE submission_id=?",
                    (review_status, now, task["submission_id"]),
                )
                connection.execute(
                    "UPDATE review_tasks SET status='decided', decided_by=?, decided_at=?, decision=?, "
                    "decision_reason=? WHERE task_id=?",
                    (actor_id, now, decision, reason, task_id),
                )
                if kind == "appeal":
                    connection.execute(
                        "UPDATE appeals SET status='decided', decided_at=? WHERE task_id=?",
                        (now, task_id),
                    )
                append_event(connection, actor_id=actor_id, action="review.decided",
                             resource_type="submission", resource_id=task["submission_id"],
                             detail={"task_id": task_id, "version_id": task["version_id"],
                                     "kind": kind, "decision": decision,
                                     "rule_version": task["rule_version"]},
                             occurred_at=now)
                return "review_task", task_id, {
                    "task_id": task_id, "submission_id": task["submission_id"],
                    "version_id": task["version_id"], "decision": decision,
                    "review_status": review_status, "rule_version": task["rule_version"]}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.decide", payload=payload, create=create)

    # ---- 复议 ----

    def request_appeal(self, *, request_id: str, actor_id: str, submission_id: str,
                       reason: str) -> dict[str, Any]:
        """对被驳回的当前版本发起复议，由另一名审核者处理。"""
        payload = {"actor_id": actor_id, "submission_id": submission_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            submission = self._get_submission_row(connection, submission_id)
            self._site_for_actor(connection, actor, submission["site_id"])
            reason = validate_text(reason, "reason", REASON_LIMIT)
            rule_version = self._current_rule_version(connection)

            def create() -> tuple[str, str, dict[str, Any]]:
                if submission["status"] != "rejected":
                    raise ConflictError("仅被驳回的投稿可以申请复议")
                version_id = submission["current_version_id"]
                if connection.execute("SELECT 1 FROM appeals WHERE version_id=?",
                                      (version_id,)).fetchone():
                    raise ConflictError("该版本已申请过复议")
                now = self._now()
                appeal_id = uuid.uuid4().hex
                task_id = self._create_task(connection, submission_id=submission_id,
                                            version_id=version_id, kind="appeal",
                                            rule_version=rule_version)
                connection.execute(
                    "INSERT INTO appeals(appeal_id,submission_id,version_id,task_id,reason,status,"
                    "requested_by,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                    (appeal_id, submission_id, version_id, task_id, reason, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="appeal.requested",
                             resource_type="submission", resource_id=submission_id,
                             detail={"appeal_id": appeal_id, "version_id": version_id,
                                     "reason": reason},
                             occurred_at=now)
                return "appeal", appeal_id, {
                    "appeal_id": appeal_id, "task_id": task_id, "submission_id": submission_id,
                    "version_id": version_id, "status": "pending"}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.appeal", payload=payload, create=create)

    # ---- 规则升级 ----

    def upgrade_rules(self, *, request_id: str, actor_id: str, note: str) -> dict[str, Any]:
        """升级审核规则，只重新评估未终结投稿，终结投稿保持原状。"""
        payload = {"actor_id": actor_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            note = validate_text(note, "note", REASON_LIMIT)

            def create() -> tuple[str, str, dict[str, Any]]:
                new_version = self._current_rule_version(connection) + 1
                now = self._now()
                connection.execute(
                    "INSERT INTO rule_registry(rule_version,note,activated_by,activated_at) "
                    "VALUES(?,?,?,?)",
                    (new_version, note, actor_id, now),
                )
                requeued = 0
                rows = connection.execute(
                    "SELECT submission_id, status, current_version_id FROM submissions "
                    "WHERE status IN ('pending','approved') ORDER BY created_at"
                ).fetchall()
                for row in rows:
                    self._cancel_open_tasks(connection, row["submission_id"])
                    kind = "initial" if row["status"] == "pending" else "reevaluation"
                    self._create_task(connection, submission_id=row["submission_id"],
                                      version_id=row["current_version_id"], kind=kind,
                                      rule_version=new_version)
                    requeued += 1
                append_event(connection, actor_id=actor_id, action="rules.upgraded",
                             resource_type="rules", resource_id=str(new_version),
                             detail={"rule_version": new_version, "note": note,
                                     "requeued": requeued},
                             occurred_at=now)
                return "rules", str(new_version), {"rule_version": new_version,
                                                   "requeued": requeued}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.upgrade_rules", payload=payload,
                                         create=create)

    # ---- 专题 ----

    def _check_referenceable(self, connection, *, submission, version, audience_scope: str) -> None:
        """专题只能引用已通过且授权范围匹配的确定版本。"""
        if submission["status"] in BLOCKED_STATUSES:
            raise ValidationError("投稿已撤回或被处置，不能引用")
        if version["review_status"] != "approved":
            raise ValidationError("只能引用已审核通过的版本")
        decision = json.loads(version["decision_json"]) if version["decision_json"] else {}
        if decision.get("rule_version") != self._current_rule_version(connection):
            raise ValidationError("版本未在现行规则下通过审核")
        if not scope_covers(version["visibility_scope"], audience_scope):
            raise ValidationError("投稿授权范围不能覆盖专题受众范围")

    def create_topic(self, *, request_id: str, actor_id: str, topic_id: str, site_id: str,
                     title: str, audience_scope: str) -> dict[str, Any]:
        """创建专题草稿。"""
        payload = {"actor_id": actor_id, "topic_id": topic_id, "site_id": site_id,
                   "title": title, "audience_scope": audience_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_actor(connection, actor, site_id)
            topic_id = self._identifier(topic_id, "topic_id")
            title = validate_text(title, "title", TITLE_LIMIT)
            audience_scope = validate_scope(audience_scope)

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM topics WHERE topic_id=?",
                                      (topic_id,)).fetchone():
                    raise ConflictError("专题编号已经存在")
                now = self._now()
                connection.execute(
                    "INSERT INTO topics(topic_id,site_id,title,audience_scope,status,created_by,"
                    "created_at) VALUES(?,?,?,?,'draft',?,?)",
                    (topic_id, site_id, title, audience_scope, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="topic.created",
                             resource_type="topic", resource_id=topic_id,
                             detail={"site_id": site_id, "title": title,
                                     "audience_scope": audience_scope},
                             occurred_at=now)
                return "topic", topic_id, {"topic_id": topic_id, "status": "draft",
                                           "audience_scope": audience_scope}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.create_topic", payload=payload,
                                         create=create)

    def add_topic_member(self, *, request_id: str, actor_id: str, topic_id: str,
                         submission_id: str, version_id: str,
                         position: int | None = None) -> dict[str, Any]:
        """把确定版本加入专题草稿，引用前校验审核结论与授权范围。"""
        payload = {"actor_id": actor_id, "topic_id": topic_id, "submission_id": submission_id,
                   "version_id": version_id, "position": position}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            topic = self._get_topic_row(connection, topic_id)
            self._site_for_actor(connection, actor, topic["site_id"])
            submission = self._get_submission_row(connection, submission_id)
            version = self._get_version_row(connection, version_id)
            if version["submission_id"] != submission_id:
                raise ValidationError("版本不属于该投稿")
            if position is not None:
                if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                    raise ValidationError("position 必须是非负整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                if topic["status"] != "draft":
                    raise ConflictError("专题已发布，成员清单已冻结")
                self._check_referenceable(connection, submission=submission, version=version,
                                          audience_scope=topic["audience_scope"])
                if connection.execute(
                        "SELECT 1 FROM topic_members WHERE topic_id=? AND submission_id=?",
                        (topic_id, submission_id)).fetchone():
                    raise ConflictError("投稿已在专题成员中")
                if position is None:
                    row = connection.execute(
                        "SELECT MAX(position) AS position FROM topic_members WHERE topic_id=?",
                        (topic_id,)).fetchone()
                    member_position = 0 if row["position"] is None else row["position"] + 1
                else:
                    member_position = position
                connection.execute(
                    "INSERT INTO topic_members(topic_id,submission_id,version_id,position,added_by,"
                    "added_at) VALUES(?,?,?,?,?,?)",
                    (topic_id, submission_id, version_id, member_position, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="topic.member_added",
                             resource_type="topic", resource_id=topic_id,
                             detail={"submission_id": submission_id, "version_id": version_id,
                                     "position": member_position},
                             occurred_at=self._now())
                return "topic", topic_id, {
                    "topic_id": topic_id, "submission_id": submission_id,
                    "version_id": version_id, "position": member_position}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.add_member", payload=payload, create=create)

    def remove_topic_member(self, *, request_id: str, actor_id: str, topic_id: str,
                            submission_id: str) -> dict[str, Any]:
        """从专题草稿中移除成员。"""
        payload = {"actor_id": actor_id, "topic_id": topic_id, "submission_id": submission_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            topic = self._get_topic_row(connection, topic_id)
            self._site_for_actor(connection, actor, topic["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if topic["status"] != "draft":
                    raise ConflictError("专题已发布，成员清单已冻结")
                cursor = connection.execute(
                    "DELETE FROM topic_members WHERE topic_id=? AND submission_id=?",
                    (topic_id, submission_id),
                )
                if cursor.rowcount == 0:
                    raise NotFoundError("投稿不在专题成员中")
                append_event(connection, actor_id=actor_id, action="topic.member_removed",
                             resource_type="topic", resource_id=topic_id,
                             detail={"submission_id": submission_id},
                             occurred_at=self._now())
                return "topic", topic_id, {"topic_id": topic_id,
                                           "submission_id": submission_id, "removed": True}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.remove_member", payload=payload,
                                         create=create)

    def publish_topic(self, *, request_id: str, actor_id: str, topic_id: str) -> dict[str, Any]:
        """发布专题：冻结成员清单并校验所有引用版本，任一失效即整体拒绝。"""
        payload = {"actor_id": actor_id, "topic_id": topic_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            topic = self._get_topic_row(connection, topic_id)
            self._site_for_actor(connection, actor, topic["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if topic["status"] != "draft":
                    raise ConflictError("专题已发布，不能重复发布")
                members = connection.execute(
                    "SELECT * FROM topic_members WHERE topic_id=? ORDER BY position, added_at",
                    (topic_id,),
                ).fetchall()
                if not members:
                    raise ValidationError("专题成员不能为空")
                failures = []
                snapshot = []
                for member in members:
                    submission = self._get_submission_row(connection, member["submission_id"])
                    version = self._get_version_row(connection, member["version_id"])
                    try:
                        self._check_referenceable(connection, submission=submission,
                                                  version=version,
                                                  audience_scope=topic["audience_scope"])
                    except ValidationError as exc:
                        failures.append({"submission_id": member["submission_id"],
                                         "version_id": member["version_id"], "reason": str(exc)})
                    else:
                        snapshot.append({"submission_id": member["submission_id"],
                                         "version_id": member["version_id"],
                                         "position": member["position"],
                                         "content_hash": version["content_hash"]})
                if failures:
                    detail = "; ".join(
                        f"{item['submission_id']}: {item['reason']}" for item in failures)
                    raise ValidationError(f"专题引用校验失败，整体拒绝发布: {detail}")
                now = self._now()
                connection.execute(
                    "UPDATE topics SET status='published', published_at=?, snapshot_json=? "
                    "WHERE topic_id=?",
                    (now, canonical_json(snapshot), topic_id),
                )
                append_event(connection, actor_id=actor_id, action="topic.published",
                             resource_type="topic", resource_id=topic_id,
                             detail={"members": len(snapshot),
                                     "content_hashes": [item["content_hash"] for item in snapshot]},
                             occurred_at=now)
                return "topic", topic_id, {"topic_id": topic_id, "status": "published",
                                           "members": len(snapshot), "published_at": now}

            return self._idempotent_call(connection, request_id=request_id,
                                         action="gov.publish_topic", payload=payload,
                                         create=create)

    # ---- 队列与读取 ----

    @staticmethod
    def _queue_item(row, now: datetime) -> dict[str, Any]:
        expired = bool(row["lease_expires_at"]) and \
            datetime.fromisoformat(row["lease_expires_at"].replace("Z", "+00:00")) <= now
        return {"task_id": row["task_id"], "kind": row["kind"],
                "submission_id": row["submission_id"], "version_id": row["version_id"],
                "rule_version": row["rule_version"], "status": row["status"],
                "lease_owner": row["lease_owner"], "lease_expires_at": row["lease_expires_at"],
                "lease_expired": expired, "created_at": row["created_at"]}

    def _queue(self, *kinds: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in kinds)
        rows = self.database.connection.execute(
            f"SELECT * FROM review_tasks WHERE status IN ('pending','leased') "
            f"AND kind IN ({placeholders}) ORDER BY created_at",
            list(kinds),
        ).fetchall()
        now = self.clock.now()
        return [self._queue_item(row, now) for row in rows]

    def review_queue(self) -> list[dict[str, Any]]:
        """待审队列，来自 SQLite 持久化状态，服务重启后继续保留。"""
        return self._queue("initial", "reevaluation")

    def appeal_queue(self) -> list[dict[str, Any]]:
        """待复议队列，来自 SQLite 持久化状态，服务重启后继续保留。"""
        rows = self.database.connection.execute(
            "SELECT t.*, a.appeal_id, a.reason AS appeal_reason, a.requested_by "
            "FROM review_tasks t JOIN appeals a ON a.task_id=t.task_id "
            "WHERE t.status IN ('pending','leased') AND t.kind='appeal' ORDER BY t.created_at"
        ).fetchall()
        now = self.clock.now()
        return [{**self._queue_item(row, now), "appeal_id": row["appeal_id"],
                 "appeal_reason": row["appeal_reason"], "requested_by": row["requested_by"]}
                for row in rows]

    @staticmethod
    def _version_content(submission, version) -> dict[str, Any]:
        decision = json.loads(version["decision_json"]) if version["decision_json"] else None
        return {"submission_id": submission["submission_id"], "version_id": version["version_id"],
                "version_no": version["version_no"], "kind": submission["kind"],
                "author_id": submission["author_id"], "text_body": version["text_body"],
                "media_summaries": json.loads(version["media_json"]),
                "declaration": json.loads(version["declaration_json"]),
                "citations": json.loads(version["citations_json"]),
                "visibility_scope": version["visibility_scope"],
                "review_status": version["review_status"], "decision": decision,
                "content_hash": version["content_hash"]}

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        """后台治理视图：投稿全量版本、审核结论与未结任务。"""
        connection = self.database.connection
        submission = self._get_submission_row(connection, submission_id)
        versions = []
        for row in connection.execute(
                "SELECT * FROM submission_versions WHERE submission_id=? ORDER BY version_no",
                (submission_id,)):
            versions.append({**self._version_content(submission, row),
                             "created_at": row["created_at"]})
        now = self.clock.now()
        open_tasks = [self._queue_item(row, now) for row in connection.execute(
            "SELECT * FROM review_tasks WHERE submission_id=? AND status IN ('pending','leased') "
            "ORDER BY created_at", (submission_id,))]
        appeals = [{"appeal_id": row["appeal_id"], "version_id": row["version_id"],
                    "status": row["status"], "reason": row["reason"],
                    "requested_by": row["requested_by"], "created_at": row["created_at"]}
                   for row in connection.execute(
                       "SELECT * FROM appeals WHERE submission_id=? ORDER BY created_at",
                       (submission_id,))]
        return {"submission_id": submission["submission_id"], "site_id": submission["site_id"],
                "kind": submission["kind"], "author_id": submission["author_id"],
                "status": submission["status"],
                "current_version_id": submission["current_version_id"],
                "created_by": submission["created_by"], "created_at": submission["created_at"],
                "updated_at": submission["updated_at"], "versions": versions,
                "open_tasks": open_tasks, "appeals": appeals}

    def read_submission_content(self, submission_id: str) -> dict[str, Any]:
        """面向未来的读取路径：可见时返回内容，否则返回审计占位。"""
        connection = self.database.connection
        submission = self._get_submission_row(connection, submission_id)
        version = self._get_version_row(connection, submission["current_version_id"])
        explanation = self.explain_submission(submission_id)
        if explanation["state"] != "visible":
            return {"submission_id": submission_id, "state": explanation["state"],
                    "reason_code": explanation["reason_code"], "placeholder": True,
                    "message": explanation["message"]}
        return {"submission_id": submission_id, "state": "visible", "placeholder": False,
                "content": self._version_content(submission, version)}

    def explain_submission(self, submission_id: str) -> dict[str, Any]:
        """解释投稿当前为何可见、受限或被阻止。"""
        connection = self.database.connection
        submission = self._get_submission_row(connection, submission_id)
        version = self._get_version_row(connection, submission["current_version_id"])
        current_rules = self._current_rule_version(connection)
        decision = json.loads(version["decision_json"]) if version["decision_json"] else None
        result: dict[str, Any] = {
            "submission_id": submission_id, "kind": submission["kind"],
            "author_id": submission["author_id"], "status": submission["status"],
            "current_version_id": version["version_id"], "version_no": version["version_no"],
            "visibility_scope": version["visibility_scope"],
            "current_rule_version": current_rules,
            "approved_rule_version": decision.get("rule_version")
            if decision and version["review_status"] == "approved" else None,
        }
        open_task = self._open_task(connection, submission_id)
        if open_task:
            result["open_task"] = {"task_id": open_task["task_id"], "kind": open_task["kind"],
                                   "status": open_task["status"],
                                   "rule_version": open_task["rule_version"]}
        if submission["status"] == "withdrawn":
            result.update(state="blocked", reason_code="withdrawn",
                          message="作者已撤回投稿，未来读取被阻止，仅保留审计占位")
            return result
        if submission["status"] == "compliance_removed":
            result.update(state="blocked", reason_code="compliance_removed",
                          message="投稿已被合规处置，未来读取被阻止，仅保留审计占位")
            return result
        if version["review_status"] == "pending":
            result.update(state="restricted", reason_code="pending_review",
                          message="当前版本正在等待审核，内容暂不公开")
            return result
        if version["review_status"] == "rejected":
            result["decision"] = decision
            appeal = connection.execute("SELECT * FROM appeals WHERE version_id=?",
                                        (version["version_id"],)).fetchone()
            if appeal:
                result["appeal"] = {"appeal_id": appeal["appeal_id"], "status": appeal["status"]}
            result.update(state="restricted", reason_code="rejected",
                          message="当前版本未通过审核，内容受限")
            return result
        if decision and decision.get("rule_version") != current_rules:
            result.update(state="restricted", reason_code="rule_upgrade_pending",
                          message="审核规则已升级，复核完成前内容暂不公开")
            return result
        result.update(state="visible", reason_code="approved",
                      message="当前版本已在现行规则下通过审核，内容可见")
        return result

    def explain_version(self, version_id: str) -> dict[str, Any]:
        """解释单个版本当前为何可见、受限、被替代或被阻止。"""
        connection = self.database.connection
        version = self._get_version_row(connection, version_id)
        submission = self._get_submission_row(connection, version["submission_id"])
        result: dict[str, Any] = {
            "version_id": version_id, "submission_id": submission["submission_id"],
            "version_no": version["version_no"], "review_status": version["review_status"],
            "visibility_scope": version["visibility_scope"],
            "is_current": version_id == submission["current_version_id"],
        }
        if submission["status"] in BLOCKED_STATUSES:
            result.update(state="blocked", reason_code=submission["status"],
                          message="投稿已撤回或被处置，版本读取被阻止，仅保留审计占位")
            return result
        if version_id != submission["current_version_id"]:
            result.update(state="replaced", reason_code="superseded",
                          replaced_by=submission["current_version_id"],
                          message="作者已提交新版本，本版本被替代；已发布专题中的引用仍固定指向本版本")
            return result
        current = self.explain_submission(submission["submission_id"])
        result.update(state=current["state"], reason_code=current["reason_code"],
                      message=current["message"])
        return result

    @staticmethod
    def _member_state(submission, version) -> tuple[str, str | None]:
        if submission["status"] == "withdrawn":
            return "blocked", "withdrawn"
        if submission["status"] == "compliance_removed":
            return "blocked", "compliance_removed"
        if version["review_status"] != "approved":
            return "invalidated", "approval_revoked"
        return "ok", None

    def get_topic(self, topic_id: str) -> dict[str, Any]:
        """读取专题：成员固定指向引用时的版本，失效成员以审计占位替代。"""
        connection = self.database.connection
        topic = self._get_topic_row(connection, topic_id)
        members = connection.execute(
            "SELECT * FROM topic_members WHERE topic_id=? ORDER BY position, added_at",
            (topic_id,),
        ).fetchall()
        items = []
        for member in members:
            submission = self._get_submission_row(connection, member["submission_id"])
            version = self._get_version_row(connection, member["version_id"])
            state, reason = self._member_state(submission, version)
            entry: dict[str, Any] = {"submission_id": member["submission_id"],
                                     "version_id": member["version_id"],
                                     "position": member["position"], "state": state}
            if state == "ok":
                entry["content"] = self._version_content(submission, version)
            else:
                entry["placeholder"] = True
                entry["reason_code"] = reason
            if topic["status"] == "draft":
                try:
                    self._check_referenceable(connection, submission=submission, version=version,
                                              audience_scope=topic["audience_scope"])
                    entry["referenceable_now"] = True
                except ValidationError:
                    entry["referenceable_now"] = False
            items.append(entry)
        return {"topic_id": topic["topic_id"], "site_id": topic["site_id"],
                "title": topic["title"], "audience_scope": topic["audience_scope"],
                "status": topic["status"], "published_at": topic["published_at"],
                "items": items}
