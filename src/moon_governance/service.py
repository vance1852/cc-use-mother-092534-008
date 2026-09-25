"""云赏月互动内容治理服务的核心业务逻辑。

治理规则概述：
- 投稿只登记文本与媒体摘要（类型、摘要哈希、尺寸、时长、说明），不接收媒体本体；
- 作者每次修改产生新版本，旧版本与其审核结论保持不变，专题引用不发生漂移；
- 审核以任务形式进入队列，多人审核时由租约保证唯一有效决定；
- 复议针对未通过版本，每版本仅一次，且必须由另一名审核者处理；
- 规则升级只重新评估未终结投稿（打开中的任务），已终结结论不受影响；
- 撤回与合规处置使投稿进入终结状态，阻止未来读取并保留审计占位；
- 专题只能引用已通过且授权范围匹配的确定版本，发布时冻结成员清单并整体校验。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from festival_foundation.audit import append_event, canonical_json, digest
from festival_foundation.clock import Clock, SystemClock
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.models import Actor, WriteReceipt
from festival_foundation.storage import Database

from .schema import ensure_schema


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

SUBMISSION_KINDS = frozenset({"poetry_chain", "hometown_intro", "blessing"})
MEDIA_KINDS = frozenset({"image", "video", "audio"})
MEDIA_KEYS = frozenset({"media_id", "kind", "sha256", "duration_ms", "width", "height", "description"})
DECLARATION_KEYS = frozenset({"original", "license_scopes", "notes"})
CITATION_KEYS = frozenset({"source", "note"})
VISIBILITY_SCOPES = frozenset({"public", "event", "restricted"})
LICENSE_SCOPES = frozenset({"event_display", "collection_reuse", "external_promotion"})
COLLECTION_VISIBLE_SCOPES = frozenset({"public", "event"})
DECISIONS = frozenset({"approve", "reject"})

MIN_LEASE_SECONDS = 30
MAX_LEASE_SECONDS = 3600


class MoonGovernanceService:
    """治理云赏月活动的投稿、审核、复议、专题与规则版本。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        ensure_schema(database.connection)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        return datetime.fromisoformat(value)

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _current_rule_version(self, connection) -> int:
        row = connection.execute("SELECT MAX(rule_version) AS version FROM moon_rule_sets").fetchone()
        return row["version"] if row["version"] is not None else 1

    # ------------------------------------------------------------------
    # 投稿载荷校验
    # ------------------------------------------------------------------
    def _validate_media(self, media_summaries: Any) -> list[dict[str, Any]]:
        if not isinstance(media_summaries, list):
            raise ValidationError("media_summaries 必须是数组")
        items = []
        for entry in media_summaries:
            if not isinstance(entry, dict):
                raise ValidationError("媒体摘要必须是对象")
            unknown = set(entry) - MEDIA_KEYS
            if unknown:
                raise ValidationError("媒体摘要只允许登记元信息字段，不能包含媒体本体")
            media_id = self._identifier(entry.get("media_id", ""), "media_id")
            kind = entry.get("kind")
            if kind not in MEDIA_KINDS:
                raise ValidationError("媒体类型必须是 image、video 或 audio")
            sha256 = str(entry.get("sha256", ""))
            if not HEX64.fullmatch(sha256):
                raise ValidationError("sha256 必须是 64 位小写十六进制摘要")
            item: dict[str, Any] = {"media_id": media_id, "kind": kind, "sha256": sha256}
            for key in ("duration_ms", "width", "height"):
                if key in entry:
                    value = entry[key]
                    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                        raise ValidationError(f"{key} 必须是非负整数")
                    item[key] = value
            if "description" in entry:
                item["description"] = self._text(entry["description"], "description", 200)
            items.append(item)
        return items

    def _validate_declaration(self, declaration: Any) -> dict[str, Any]:
        if not isinstance(declaration, dict):
            raise ValidationError("declaration 必须是对象")
        unknown = set(declaration) - DECLARATION_KEYS
        if unknown:
            raise ValidationError("作者声明包含不支持的字段")
        original = declaration.get("original")
        if not isinstance(original, bool):
            raise ValidationError("original 必须是布尔值")
        scopes = declaration.get("license_scopes")
        if not isinstance(scopes, list) or not scopes or any(scope not in LICENSE_SCOPES for scope in scopes):
            raise ValidationError("license_scopes 必须是非空数组且在允许范围内")
        result: dict[str, Any] = {"original": original, "license_scopes": sorted(set(scopes))}
        if "notes" in declaration:
            result["notes"] = self._text(declaration["notes"], "notes", 500)
        return result

    def _validate_citations(self, citations: Any, original: bool) -> list[dict[str, Any]]:
        if not isinstance(citations, list):
            raise ValidationError("citations 必须是数组")
        items = []
        for entry in citations:
            if not isinstance(entry, dict):
                raise ValidationError("来源引用必须是对象")
            unknown = set(entry) - CITATION_KEYS
            if unknown:
                raise ValidationError("来源引用包含不支持的字段")
            item = {"source": self._text(entry.get("source", ""), "source", 200)}
            if "note" in entry:
                item["note"] = self._text(entry["note"], "note", 200)
            items.append(item)
        if not original and not items:
            raise ValidationError("非原创投稿必须登记来源引用")
        return items

    def _version_payload(self, text_body: str, media_summaries: Any, declaration: Any,
                         citations: Any, visibility_scope: str) -> tuple[str, list, dict, list, str]:
        text = self._text(text_body, "text_body", 2000)
        media = self._validate_media(media_summaries)
        decl = self._validate_declaration(declaration)
        cites = self._validate_citations(citations, decl["original"])
        if visibility_scope not in VISIBILITY_SCOPES:
            raise ValidationError("visibility_scope 不在允许范围内")
        return text, media, decl, cites, visibility_scope

    # ------------------------------------------------------------------
    # 行存取辅助
    # ------------------------------------------------------------------
    def _insert_version(self, connection, *, submission_id: str, version: int, text_body: str,
                        media: list, declaration: dict, citations: list, visibility_scope: str,
                        rule_version: int, created_by: str, created_at: str) -> tuple[str, str]:
        text_hash = digest(text_body)
        media_hash = digest(media)
        connection.execute(
            "INSERT INTO moon_submission_versions(submission_id,version,text_body,text_hash,media_json,media_hash,"
            "declaration_json,citations_json,visibility_scope,review_state,rule_version,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
            (submission_id, version, text_body, text_hash, canonical_json(media), media_hash,
             canonical_json(declaration), canonical_json(citations), visibility_scope,
             rule_version, created_by, created_at),
        )
        return text_hash, media_hash

    def _open_task(self, connection, *, submission_id: str, version: int, kind: str,
                   rule_version: int, created_at: str, original_reviewer: str | None = None) -> str:
        task_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO moon_review_tasks(task_id,submission_id,version,kind,state,rule_version,"
            "original_reviewer,created_at) VALUES(?,?,?,?,'open',?,?,?)",
            (task_id, submission_id, version, kind, rule_version, original_reviewer, created_at),
        )
        return task_id

    def _cancel_open_tasks(self, connection, submission_id: str) -> int:
        cursor = connection.execute(
            "UPDATE moon_review_tasks SET state='cancelled', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE submission_id=? AND state='open'",
            (submission_id,),
        )
        return cursor.rowcount

    def _submission_row(self, submission_id: str):
        row = self.database.connection.execute(
            "SELECT * FROM moon_submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("投稿不存在")
        return row

    def _version_row(self, submission_id: str, version: int):
        row = self.database.connection.execute(
            "SELECT * FROM moon_submission_versions WHERE submission_id=? AND version=?",
            (submission_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError("投稿版本不存在")
        return row

    def _version_rows(self, submission_id: str) -> list:
        return self.database.connection.execute(
            "SELECT * FROM moon_submission_versions WHERE submission_id=? ORDER BY version",
            (submission_id,),
        ).fetchall()

    def _task_row(self, connection, task_id: str):
        row = connection.execute("SELECT * FROM moon_review_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("审核任务不存在")
        return row

    def _collection_row(self, connection, collection_id: str):
        row = connection.execute("SELECT * FROM moon_collections WHERE collection_id=?", (collection_id,)).fetchone()
        if row is None:
            raise NotFoundError("专题不存在")
        return row

    @staticmethod
    def _task_view(row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"], "submission_id": row["submission_id"], "version": row["version"],
            "kind": row["kind"], "state": row["state"], "rule_version": row["rule_version"],
            "original_reviewer": row["original_reviewer"],
            "lease_owner": row["lease_owner"], "lease_expires_at": row["lease_expires_at"],
            "decided_by": row["decided_by"], "decided_at": row["decided_at"],
            "decision": row["decision"], "decision_reason": row["decision_reason"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _public_content(row) -> dict[str, Any]:
        return {
            "version": row["version"],
            "text_body": row["text_body"],
            "media_summaries": json.loads(row["media_json"]),
            "declaration": json.loads(row["declaration_json"]),
            "citations": json.loads(row["citations_json"]),
            "visibility_scope": row["visibility_scope"],
            "rule_version": row["rule_version"],
        }

    @staticmethod
    def _tombstone(submission, latest_version) -> dict[str, Any]:
        return {
            "status": submission["status"],
            "closed_reason": submission["closed_reason"],
            "closed_at": submission["closed_at"],
            "content_hash": latest_version["text_hash"],
            "media_hash": latest_version["media_hash"],
        }

    # ------------------------------------------------------------------
    # 投稿登记与版本
    # ------------------------------------------------------------------
    def submit_submission(self, *, request_id: str, actor_id: str, site_id: str, kind: str,
                          author_id: str, text_body: str, declaration: dict,
                          media_summaries: Any = None, citations: Any = None,
                          visibility_scope: str = "event") -> WriteReceipt:
        media_summaries = media_summaries if media_summaries is not None else []
        citations = citations if citations is not None else []
        payload = {"actor_id": actor_id, "site_id": site_id, "kind": kind, "author_id": author_id,
                   "text_body": text_body, "media_summaries": media_summaries, "declaration": declaration,
                   "citations": citations, "visibility_scope": visibility_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            if kind not in SUBMISSION_KINDS:
                raise ValidationError("投稿类型不受支持")
            author_id = self._identifier(author_id, "author_id")
            text, media, decl, cites, scope = self._version_payload(
                text_body, media_summaries, declaration, citations, visibility_scope)

            def create() -> tuple[str, str, dict[str, Any]]:
                submission_id = uuid.uuid4().hex
                now = self._now()
                rule_version = self._current_rule_version(connection)
                connection.execute(
                    "INSERT INTO moon_submissions(submission_id,site_id,kind,author_id,status,current_version,"
                    "created_by,created_at) VALUES(?,?,?,?,'active',1,?,?)",
                    (submission_id, site_id, kind, author_id, actor_id, now),
                )
                text_hash, media_hash = self._insert_version(
                    connection, submission_id=submission_id, version=1, text_body=text, media=media,
                    declaration=decl, citations=cites, visibility_scope=scope,
                    rule_version=rule_version, created_by=actor_id, created_at=now)
                task_id = self._open_task(connection, submission_id=submission_id, version=1,
                                          kind="initial", rule_version=rule_version, created_at=now)
                append_event(connection, actor_id=actor_id, action="submission.submitted",
                             resource_type="submission", resource_id=submission_id,
                             detail={"site_id": site_id, "kind": kind, "author_id": author_id, "version": 1,
                                     "text_hash": text_hash, "media_hash": media_hash,
                                     "rule_version": rule_version, "task_id": task_id},
                             occurred_at=self._now())
                return "submission", submission_id, {"submission_id": submission_id, "task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_submission", payload=payload, create=create)

    def add_version(self, *, request_id: str, actor_id: str, submission_id: str, author_id: str,
                    text_body: str, declaration: dict, media_summaries: Any = None,
                    citations: Any = None, visibility_scope: str = "event") -> WriteReceipt:
        media_summaries = media_summaries if media_summaries is not None else []
        citations = citations if citations is not None else []
        payload = {"actor_id": actor_id, "submission_id": submission_id, "author_id": author_id,
                   "text_body": text_body, "media_summaries": media_summaries, "declaration": declaration,
                   "citations": citations, "visibility_scope": visibility_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            author_id = self._identifier(author_id, "author_id")
            text, media, decl, cites, scope = self._version_payload(
                text_body, media_summaries, declaration, citations, visibility_scope)

            def create() -> tuple[str, str, dict[str, Any]]:
                submission = connection.execute(
                    "SELECT * FROM moon_submissions WHERE submission_id=?", (submission_id,)).fetchone()
                if submission is None:
                    raise NotFoundError("投稿不存在")
                if submission["status"] != "active":
                    raise ConflictError("投稿已撤回或已处置，不能再修改")
                if submission["author_id"] != author_id:
                    raise PermissionDenied("只能登记原作者本人的修改")
                new_version = submission["current_version"] + 1
                now = self._now()
                rule_version = self._current_rule_version(connection)
                cancelled = self._cancel_open_tasks(connection, submission_id)
                connection.execute(
                    "UPDATE moon_submissions SET current_version=? WHERE submission_id=?",
                    (new_version, submission_id),
                )
                text_hash, media_hash = self._insert_version(
                    connection, submission_id=submission_id, version=new_version, text_body=text, media=media,
                    declaration=decl, citations=cites, visibility_scope=scope,
                    rule_version=rule_version, created_by=actor_id, created_at=now)
                task_id = self._open_task(connection, submission_id=submission_id, version=new_version,
                                          kind="initial", rule_version=rule_version, created_at=now)
                append_event(connection, actor_id=actor_id, action="submission.version_added",
                             resource_type="submission", resource_id=submission_id,
                             detail={"version": new_version, "text_hash": text_hash, "media_hash": media_hash,
                                     "rule_version": rule_version, "task_id": task_id,
                                     "cancelled_tasks": cancelled},
                             occurred_at=self._now())
                return "submission", submission_id, {"submission_id": submission_id,
                                                     "version": new_version, "task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_version", payload=payload, create=create)

    def _close_submission(self, *, request_id: str, actor_id: str, submission_id: str, reason: str,
                          status: str, roles: tuple[str, ...], action: str, event: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "submission_id": submission_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *roles)
            reason = self._text(reason, "reason", 200)

            def create() -> tuple[str, str, dict[str, Any]]:
                submission = connection.execute(
                    "SELECT * FROM moon_submissions WHERE submission_id=?", (submission_id,)).fetchone()
                if submission is None:
                    raise NotFoundError("投稿不存在")
                if submission["status"] != "active":
                    raise ConflictError("投稿已处于终结状态")
                now = self._now()
                connection.execute(
                    "UPDATE moon_submissions SET status=?, closed_reason=?, closed_at=? WHERE submission_id=?",
                    (status, reason, now, submission_id),
                )
                cancelled = self._cancel_open_tasks(connection, submission_id)
                latest = connection.execute(
                    "SELECT * FROM moon_submission_versions WHERE submission_id=? ORDER BY version DESC LIMIT 1",
                    (submission_id,)).fetchone()
                append_event(connection, actor_id=actor_id, action=event,
                             resource_type="submission", resource_id=submission_id,
                             detail={"reason": reason, "cancelled_tasks": cancelled,
                                     "content_hash": latest["text_hash"], "media_hash": latest["media_hash"]},
                             occurred_at=self._now())
                return "submission", submission_id, {"submission_id": submission_id, "status": status}

            return self._idempotent(connection, request_id=request_id, action=action,
                                    payload=payload, create=create)

    def withdraw_submission(self, *, request_id: str, actor_id: str, submission_id: str,
                            reason: str) -> WriteReceipt:
        return self._close_submission(request_id=request_id, actor_id=actor_id, submission_id=submission_id,
                                      reason=reason, status="withdrawn", roles=("admin", "operator"),
                                      action="withdraw_submission", event="submission.withdrawn")

    def takedown_submission(self, *, request_id: str, actor_id: str, submission_id: str,
                            reason: str) -> WriteReceipt:
        return self._close_submission(request_id=request_id, actor_id=actor_id, submission_id=submission_id,
                                      reason=reason, status="taken_down", roles=("admin", "reviewer"),
                                      action="takedown_submission", event="submission.taken_down")

    # ------------------------------------------------------------------
    # 审核队列、租约与结论
    # ------------------------------------------------------------------
    def list_review_queue(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None and kind not in ("initial", "appeal"):
            raise ValidationError("kind 必须是 initial 或 appeal")
        query = ("SELECT t.*, s.kind AS submission_kind, s.author_id FROM moon_review_tasks t "
                 "JOIN moon_submissions s ON s.submission_id=t.submission_id WHERE t.state='open'")
        parameters: list[Any] = []
        if kind:
            query += " AND t.kind=?"
            parameters.append(kind)
        query += " ORDER BY t.created_at, t.task_id"
        items = []
        for row in self.database.connection.execute(query, parameters):
            view = self._task_view(row)
            view["submission_kind"] = row["submission_kind"]
            view["author_id"] = row["author_id"]
            items.append(view)
        return items

    def get_review_task(self, task_id: str) -> dict[str, Any]:
        connection = self.database.connection
        task = self._task_row(connection, task_id)
        submission = connection.execute(
            "SELECT * FROM moon_submissions WHERE submission_id=?", (task["submission_id"],)).fetchone()
        version = connection.execute(
            "SELECT * FROM moon_submission_versions WHERE submission_id=? AND version=?",
            (task["submission_id"], task["version"])).fetchone()
        view = self._task_view(task)
        view["submission_status"] = submission["status"]
        if submission["status"] == "active":
            view["content"] = self._public_content(version)
            view["placeholder"] = False
        else:
            view["content"] = None
            view["placeholder"] = True
            view["tombstone"] = self._tombstone(submission, version)
        return view

    def acquire_lease(self, *, actor_id: str, task_id: str, lease_seconds: int = 300) -> dict[str, Any]:
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) \
                or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValidationError(f"lease_seconds 必须在 {MIN_LEASE_SECONDS} 到 {MAX_LEASE_SECONDS} 秒之间")
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            task = self._task_row(connection, task_id)
            if task["state"] != "open":
                raise ConflictError("审核任务已关闭，不能租用")
            if task["kind"] == "appeal" and task["original_reviewer"] == actor_id:
                raise PermissionDenied("复议必须由另一名审核者处理")
            now = self.clock.now()
            if task["lease_owner"] and task["lease_owner"] != actor_id \
                    and self._parse_ts(task["lease_expires_at"]) > now:
                raise ConflictError("审核任务正由其他审核者租用")
            expires_at = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
            connection.execute(
                "UPDATE moon_review_tasks SET lease_owner=?, lease_expires_at=? WHERE task_id=?",
                (actor_id, expires_at, task_id),
            )
            append_event(connection, actor_id=actor_id, action="review.lease_acquired",
                         resource_type="review_task", resource_id=task_id,
                         detail={"submission_id": task["submission_id"], "version": task["version"],
                                 "kind": task["kind"], "lease_expires_at": expires_at},
                         occurred_at=self._now())
            return self._task_view(self._task_row(connection, task_id))

    def release_lease(self, *, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            task = self._task_row(connection, task_id)
            if task["state"] != "open":
                raise ConflictError("审核任务已关闭")
            if not task["lease_owner"]:
                raise ConflictError("审核任务未被租用")
            if task["lease_owner"] != actor_id and actor.role != "admin":
                raise PermissionDenied("只能释放本人持有的租约")
            connection.execute(
                "UPDATE moon_review_tasks SET lease_owner=NULL, lease_expires_at=NULL WHERE task_id=?",
                (task_id,),
            )
            append_event(connection, actor_id=actor_id, action="review.lease_released",
                         resource_type="review_task", resource_id=task_id,
                         detail={"previous_owner": task["lease_owner"]}, occurred_at=self._now())
            return self._task_view(self._task_row(connection, task_id))

    def submit_decision(self, *, request_id: str, actor_id: str, task_id: str,
                        decision: str, reason: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "decision": decision, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            if decision not in DECISIONS:
                raise ValidationError("decision 必须是 approve 或 reject")
            reason = str(reason).strip()
            if decision == "reject" and not reason:
                raise ValidationError("驳回必须填写理由")
            if len(reason) > 200:
                raise ValidationError("reason 不能超过 200 个字符")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                if task["state"] != "open":
                    raise ConflictError("审核任务已有结论，不能重复决定")
                if task["lease_owner"] != actor_id:
                    raise PermissionDenied("只有持有租约的审核者才能提交结论")
                now = self.clock.now()
                if not task["lease_expires_at"] or self._parse_ts(task["lease_expires_at"]) <= now:
                    raise ConflictError("租约已过期，请重新租用")
                if task["kind"] == "appeal" and task["original_reviewer"] == actor_id:
                    raise PermissionDenied("复议必须由另一名审核者处理")
                review_state = "approved" if decision == "approve" else "rejected"
                connection.execute(
                    "UPDATE moon_submission_versions SET review_state=? WHERE submission_id=? AND version=?",
                    (review_state, task["submission_id"], task["version"]),
                )
                connection.execute(
                    "UPDATE moon_review_tasks SET state='decided', decided_by=?, decided_at=?, decision=?,"
                    " decision_reason=?, lease_owner=NULL, lease_expires_at=NULL WHERE task_id=?",
                    (actor_id, self._now(), decision, reason or None, task_id),
                )
                append_event(connection, actor_id=actor_id, action="review.decided",
                             resource_type="review_task", resource_id=task_id,
                             detail={"submission_id": task["submission_id"], "version": task["version"],
                                     "kind": task["kind"], "decision": decision, "reason": reason,
                                     "rule_version": task["rule_version"]},
                             occurred_at=self._now())
                return "review_task", task_id, {"task_id": task_id, "decision": decision,
                                                "review_state": review_state}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_review_decision", payload=payload, create=create)

    def request_appeal(self, *, request_id: str, actor_id: str, submission_id: str,
                       version: int, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "submission_id": submission_id,
                   "version": version, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reason = self._text(reason, "reason", 200)
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValidationError("version 必须是正整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                submission = connection.execute(
                    "SELECT * FROM moon_submissions WHERE submission_id=?", (submission_id,)).fetchone()
                if submission is None:
                    raise NotFoundError("投稿不存在")
                if submission["status"] != "active":
                    raise ConflictError("投稿已处于终结状态，不能申请复议")
                version_row = connection.execute(
                    "SELECT * FROM moon_submission_versions WHERE submission_id=? AND version=?",
                    (submission_id, version)).fetchone()
                if version_row is None:
                    raise NotFoundError("投稿版本不存在")
                if version_row["review_state"] != "rejected":
                    raise ConflictError("只有未通过审核的版本可以申请复议")
                existing = connection.execute(
                    "SELECT 1 FROM moon_review_tasks WHERE submission_id=? AND version=? AND kind='appeal'",
                    (submission_id, version)).fetchone()
                if existing:
                    raise ConflictError("该版本已经提交过复议")
                initial = connection.execute(
                    "SELECT * FROM moon_review_tasks WHERE submission_id=? AND version=? AND kind='initial'",
                    (submission_id, version)).fetchone()
                original_reviewer = initial["decided_by"] if initial else None
                task_id = self._open_task(
                    connection, submission_id=submission_id, version=version, kind="appeal",
                    rule_version=self._current_rule_version(connection), created_at=self._now(),
                    original_reviewer=original_reviewer)
                append_event(connection, actor_id=actor_id, action="appeal.requested",
                             resource_type="review_task", resource_id=task_id,
                             detail={"submission_id": submission_id, "version": version,
                                     "reason": reason, "original_reviewer": original_reviewer},
                             occurred_at=self._now())
                return "review_task", task_id, {"task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="request_appeal", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 规则升级
    # ------------------------------------------------------------------
    def upgrade_rules(self, *, request_id: str, actor_id: str, note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            note = self._text(note, "note", 200)

            def create() -> tuple[str, str, dict[str, Any]]:
                new_version = self._current_rule_version(connection) + 1
                connection.execute(
                    "INSERT INTO moon_rule_sets(rule_version,note,created_by,created_at) VALUES(?,?,?,?)",
                    (new_version, note, actor_id, self._now()),
                )
                cursor = connection.execute(
                    "UPDATE moon_review_tasks SET rule_version=?, lease_owner=NULL, lease_expires_at=NULL "
                    "WHERE state='open'",
                    (new_version,),
                )
                requeued = cursor.rowcount
                append_event(connection, actor_id=actor_id, action="rules.upgraded",
                             resource_type="rule_set", resource_id=str(new_version),
                             detail={"note": note, "rule_version": new_version, "requeued": requeued},
                             occurred_at=self._now())
                return "rule_set", str(new_version), {"rule_version": new_version, "requeued": requeued}

            return self._idempotent(connection, request_id=request_id,
                                    action="upgrade_rules", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 专题编辑与发布
    # ------------------------------------------------------------------
    def create_collection(self, *, request_id: str, actor_id: str, site_id: str,
                          title: str, usage_scope: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "title": title, "usage_scope": usage_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            title = self._text(title, "title", 100)
            if usage_scope not in LICENSE_SCOPES:
                raise ValidationError("usage_scope 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                collection_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO moon_collections(collection_id,site_id,title,usage_scope,state,created_by,created_at) "
                    "VALUES(?,?,?,?,'draft',?,?)",
                    (collection_id, site_id, title, usage_scope, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="collection.created",
                             resource_type="collection", resource_id=collection_id,
                             detail={"site_id": site_id, "title": title, "usage_scope": usage_scope},
                             occurred_at=self._now())
                return "collection", collection_id, {"collection_id": collection_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_collection", payload=payload, create=create)

    def _reference_problems(self, connection, collection, submission_id: str, version: int) -> list[str]:
        problems = []
        submission = connection.execute(
            "SELECT * FROM moon_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if submission is None:
            return ["投稿不存在"]
        version_row = connection.execute(
            "SELECT * FROM moon_submission_versions WHERE submission_id=? AND version=?",
            (submission_id, version)).fetchone()
        if version_row is None:
            return ["投稿版本不存在"]
        if submission["status"] != "active":
            problems.append("投稿已撤回或已处置")
        if version_row["review_state"] != "approved":
            problems.append("版本未通过审核")
        declaration = json.loads(version_row["declaration_json"])
        if collection["usage_scope"] not in declaration["license_scopes"]:
            problems.append("作者授权范围不包含专题用途")
        if version_row["visibility_scope"] not in COLLECTION_VISIBLE_SCOPES:
            problems.append("作者设定的可见范围不允许进入专题")
        return problems

    def add_collection_item(self, *, request_id: str, actor_id: str, collection_id: str,
                            submission_id: str, version: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "collection_id": collection_id,
                   "submission_id": submission_id, "version": version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValidationError("version 必须是正整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                collection = self._collection_row(connection, collection_id)
                if collection["state"] == "archived":
                    raise ConflictError("专题已归档，不能再编辑")
                problems = self._reference_problems(connection, collection, submission_id, version)
                if problems:
                    raise ConflictError("引用不满足条件：" + "；".join(problems))
                existing = connection.execute(
                    "SELECT 1 FROM moon_collection_items WHERE collection_id=? AND submission_id=?",
                    (collection_id, submission_id)).fetchone()
                if existing:
                    raise ConflictError("专题已包含该投稿")
                row = connection.execute(
                    "SELECT MAX(position) AS position FROM moon_collection_items WHERE collection_id=?",
                    (collection_id,)).fetchone()
                position = (row["position"] or 0) + 1
                connection.execute(
                    "INSERT INTO moon_collection_items(collection_id,submission_id,version,position,added_by,added_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (collection_id, submission_id, version, position, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="collection.item_added",
                             resource_type="collection", resource_id=collection_id,
                             detail={"submission_id": submission_id, "version": version, "position": position},
                             occurred_at=self._now())
                return "collection", collection_id, {"collection_id": collection_id, "position": position}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_collection_item", payload=payload, create=create)

    def publish_collection(self, *, request_id: str, actor_id: str, collection_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "collection_id": collection_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                collection = self._collection_row(connection, collection_id)
                if collection["state"] == "archived":
                    raise ConflictError("专题已归档，不能发布")
                items = connection.execute(
                    "SELECT * FROM moon_collection_items WHERE collection_id=? ORDER BY position",
                    (collection_id,)).fetchall()
                if not items:
                    raise ValidationError("专题没有成员，不能发布")
                failures = []
                for item in items:
                    problems = self._reference_problems(
                        connection, collection, item["submission_id"], item["version"])
                    if problems:
                        failures.append(f"{item['submission_id']}#v{item['version']}：{'；'.join(problems)}")
                if failures:
                    raise ConflictError("专题存在失效引用，整体拒绝发布：" + "；".join(failures))
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM moon_collection_publications WHERE collection_id=?",
                    (collection_id,)).fetchone()
                publication_version = row["count"] + 1
                members = [{"position": item["position"], "submission_id": item["submission_id"],
                            "version": item["version"]} for item in items]
                connection.execute(
                    "INSERT INTO moon_collection_publications(collection_id,publication_version,members_json,"
                    "published_by,published_at) VALUES(?,?,?,?,?)",
                    (collection_id, publication_version, canonical_json(members), actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE moon_collections SET state='published' WHERE collection_id=?", (collection_id,))
                append_event(connection, actor_id=actor_id, action="collection.published",
                             resource_type="collection", resource_id=collection_id,
                             detail={"publication_version": publication_version,
                                     "members": [{"submission_id": m["submission_id"], "version": m["version"]}
                                                 for m in members]},
                             occurred_at=self._now())
                return "collection", collection_id, {"collection_id": collection_id,
                                                     "publication_version": publication_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_collection", payload=payload, create=create)

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        connection = self.database.connection
        collection = self._collection_row(connection, collection_id)
        items = []
        for item in connection.execute(
                "SELECT * FROM moon_collection_items WHERE collection_id=? ORDER BY position",
                (collection_id,)):
            problems = self._reference_problems(
                connection, collection, item["submission_id"], item["version"])
            items.append({"position": item["position"], "submission_id": item["submission_id"],
                          "version": item["version"], "valid": not problems, "problems": problems})
        row = connection.execute(
            "SELECT MAX(publication_version) AS version FROM moon_collection_publications WHERE collection_id=?",
            (collection_id,)).fetchone()
        return {"collection_id": collection_id, "site_id": collection["site_id"],
                "title": collection["title"], "usage_scope": collection["usage_scope"],
                "state": collection["state"], "items": items,
                "latest_publication_version": row["version"]}

    def get_published_collection(self, collection_id: str) -> dict[str, Any]:
        connection = self.database.connection
        collection = self._collection_row(connection, collection_id)
        publication = connection.execute(
            "SELECT * FROM moon_collection_publications WHERE collection_id=? "
            "ORDER BY publication_version DESC LIMIT 1",
            (collection_id,)).fetchone()
        if publication is None:
            raise NotFoundError("专题尚未发布")
        members = []
        for member in json.loads(publication["members_json"]):
            submission = connection.execute(
                "SELECT * FROM moon_submissions WHERE submission_id=?",
                (member["submission_id"],)).fetchone()
            version_row = connection.execute(
                "SELECT * FROM moon_submission_versions WHERE submission_id=? AND version=?",
                (member["submission_id"], member["version"])).fetchone()
            entry = {"position": member["position"], "submission_id": member["submission_id"],
                     "version": member["version"]}
            reason = "ok"
            if submission is None or version_row is None:
                reason = "missing"
            elif submission["status"] == "withdrawn":
                reason = "withdrawn"
            elif submission["status"] == "taken_down":
                reason = "taken_down"
            elif version_row["review_state"] != "approved":
                reason = "not_approved"
            if reason == "ok":
                entry.update({"available": True, "reason": "ok", "placeholder": False,
                              "content": self._public_content(version_row),
                              "content_hash": version_row["text_hash"]})
            else:
                entry.update({"available": False, "reason": reason, "placeholder": True, "content": None,
                              "content_hash": version_row["text_hash"] if version_row else None})
            members.append(entry)
        return {"collection_id": collection_id, "title": collection["title"],
                "usage_scope": collection["usage_scope"],
                "publication_version": publication["publication_version"],
                "published_by": publication["published_by"], "published_at": publication["published_at"],
                "members": members}

    # ------------------------------------------------------------------
    # 读取与解释
    # ------------------------------------------------------------------
    def get_submission(self, submission_id: str) -> dict[str, Any]:
        submission = self._submission_row(submission_id)
        versions = self._version_rows(submission_id)
        latest = versions[-1]
        approved = [row for row in versions if row["review_state"] == "approved"]
        served = approved[-1] if approved else None
        base = {"submission_id": submission_id, "site_id": submission["site_id"],
                "kind": submission["kind"], "author_id": submission["author_id"],
                "status": submission["status"], "current_version": submission["current_version"]}
        if submission["status"] != "active":
            note = "投稿已撤回，内容停止读取并保留审计占位" if submission["status"] == "withdrawn" \
                else "投稿已被合规处置，内容停止读取并保留审计占位"
            return {**base, "visible": False, "placeholder": True, "served_version": None,
                    "content": None, "tombstone": self._tombstone(submission, latest), "notes": [note]}
        notes = []
        if served is None:
            notes.append("投稿尚未通过审核，公开渠道不可见")
        elif any(row["review_state"] == "pending" for row in versions if row["version"] > served["version"]):
            notes.append(f"新版本 v{latest['version']} 正在审核，当前仍展示 v{served['version']}")
        return {**base, "visible": served is not None, "placeholder": False,
                "served_version": served["version"] if served else None,
                "content": self._public_content(served) if served else None,
                "tombstone": None, "notes": notes}

    def get_version(self, submission_id: str, version: int) -> dict[str, Any]:
        connection = self.database.connection
        submission = self._submission_row(submission_id)
        version_row = self._version_row(submission_id, version)
        blocked = submission["status"] != "active"
        tasks = [self._task_view(row) for row in connection.execute(
            "SELECT * FROM moon_review_tasks WHERE submission_id=? AND version=? ORDER BY created_at, task_id",
            (submission_id, version))]
        result = {"submission_id": submission_id, "version": version,
                  "review_state": version_row["review_state"], "rule_version": version_row["rule_version"],
                  "visibility_scope": version_row["visibility_scope"],
                  "content_hash": version_row["text_hash"], "media_hash": version_row["media_hash"],
                  "created_by": version_row["created_by"], "created_at": version_row["created_at"],
                  "placeholder": blocked,
                  "content": None if blocked else self._public_content(version_row),
                  "tasks": tasks, "referenced_by": self._references(submission_id, version)}
        if blocked:
            result["tombstone"] = self._tombstone(submission, version_row)
        return result

    def _references(self, submission_id: str, version: int) -> list[dict[str, Any]]:
        connection = self.database.connection
        references = []
        for row in connection.execute(
                "SELECT c.collection_id, c.title, c.state FROM moon_collection_items i "
                "JOIN moon_collections c ON c.collection_id=i.collection_id "
                "WHERE i.submission_id=? AND i.version=? ORDER BY c.collection_id",
                (submission_id, version)):
            references.append({"collection_id": row["collection_id"], "title": row["title"],
                               "state": row["state"], "pinned": True})
        for row in connection.execute(
                "SELECT collection_id, publication_version, members_json FROM moon_collection_publications "
                "ORDER BY collection_id, publication_version"):
            for member in json.loads(row["members_json"]):
                if member["submission_id"] == submission_id and member["version"] == version:
                    references.append({"collection_id": row["collection_id"],
                                       "publication_version": row["publication_version"],
                                       "pinned_in_publication": True})
        return references

    def explain_submission(self, submission_id: str) -> dict[str, Any]:
        connection = self.database.connection
        submission = self._submission_row(submission_id)
        versions = self._version_rows(submission_id)
        blocked = submission["status"] != "active"
        approved_versions = [row["version"] for row in versions if row["review_state"] == "approved"]
        max_approved = max(approved_versions) if approved_versions else None
        open_tasks = [self._task_view(row) for row in connection.execute(
            "SELECT * FROM moon_review_tasks WHERE submission_id=? AND state='open' ORDER BY created_at, task_id",
            (submission_id,))]
        open_appeal_versions = {row["version"] for row in open_tasks if row["kind"] == "appeal"}

        version_infos = []
        for row in versions:
            reasons = []
            if blocked:
                label = "blocked"
                reasons.append("投稿已撤回，所有版本停止读取" if submission["status"] == "withdrawn"
                               else "投稿已被合规处置，所有版本停止读取")
            elif row["review_state"] == "pending":
                label = "restricted"
                reasons.append(f"版本正在等待审核（规则 v{row['rule_version']}），公开渠道不可见")
            elif row["review_state"] == "rejected":
                label = "restricted"
                decided = connection.execute(
                    "SELECT decision_reason FROM moon_review_tasks WHERE submission_id=? AND version=? "
                    "AND state='decided' AND decision='reject' ORDER BY decided_at DESC LIMIT 1",
                    (submission_id, row["version"])).fetchone()
                detail = f"：{decided['decision_reason']}" if decided and decided["decision_reason"] else ""
                reasons.append(f"版本未通过审核{detail}")
                if row["version"] in open_appeal_versions:
                    reasons.append("复议处理中，等待另一名审核者结论")
            elif max_approved is not None and row["version"] < max_approved:
                label = "superseded"
                reasons.append(f"更新的版本 v{max_approved} 已通过审核，当前版本被替代")
            else:
                label = "visible"
                reasons.append("版本已通过审核，当前可见")
                if row["visibility_scope"] == "restricted":
                    reasons.append("作者设定可见范围为 restricted，仅后台可见")
            version_infos.append({
                "version": row["version"], "review_state": row["review_state"],
                "rule_version": row["rule_version"], "visibility_scope": row["visibility_scope"],
                "state_label": label, "reasons": reasons,
                "referenced_by": self._references(submission_id, row["version"])})

        if blocked:
            effective_state = "blocked"
            effective_reasons = ["投稿已撤回，内容停止读取并保留审计占位" if submission["status"] == "withdrawn"
                                 else "投稿已被合规处置，内容停止读取并保留审计占位"]
        elif max_approved is not None:
            effective_state = "visible"
            effective_reasons = [f"版本 v{max_approved} 已通过审核，当前对外可见"]
            pending_newer = [row["version"] for row in versions
                             if row["review_state"] == "pending" and row["version"] > max_approved]
            if pending_newer:
                effective_reasons.append(f"新版本 v{max(pending_newer)} 正在审核，通过前不影响当前展示")
        else:
            effective_state = "restricted"
            effective_reasons = ["投稿尚未有通过审核的版本，公开渠道不可见"]

        result = {"submission_id": submission_id, "site_id": submission["site_id"],
                  "kind": submission["kind"], "author_id": submission["author_id"],
                  "status": submission["status"], "current_version": submission["current_version"],
                  "effective_state": effective_state, "effective_reasons": effective_reasons,
                  "versions": version_infos, "open_tasks": open_tasks}
        if blocked:
            result["tombstone"] = self._tombstone(submission, versions[-1])
        return result
