import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database
from moon_governance.service import MoonGovernanceService


class ManualClock:
    def __init__(self):
        self._now = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now += timedelta(seconds=seconds)


DECLARATION = {"original": True, "license_scopes": ["event_display", "collection_reuse"]}


class MoonServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock()
        self.foundation = DomainService(self.database, self.clock)
        self.moon = MoonGovernanceService(self.database, self.clock)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="云赏月组委会")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                       display_name="运营", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="reviewer1", actor_id="a1", new_actor_id="r1",
                                       display_name="审核员一", role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="reviewer2", actor_id="a1", new_actor_id="r2",
                                       display_name="审核员二", role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                       display_name="审计员", role="auditor", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="主会场", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def submit(self, request_id="sub-1", **overrides):
        payload = {"request_id": request_id, "actor_id": "op1", "site_id": "s1",
                   "kind": "poetry_chain", "author_id": "user-1",
                   "text_body": "海上生明月", "declaration": DECLARATION,
                   "visibility_scope": "public"}
        payload.update(overrides)
        return self.moon.submit_submission(**payload)

    def open_task(self, submission_id, kind="initial"):
        for item in self.moon.list_review_queue(kind):
            if item["submission_id"] == submission_id:
                return item["task_id"]
        self.fail(f"没有找到 {submission_id} 的 {kind} 任务")

    def decide(self, request_id, reviewer, task_id, decision, reason=""):
        self.moon.acquire_lease(actor_id=reviewer, task_id=task_id, lease_seconds=300)
        return self.moon.submit_decision(request_id=request_id, actor_id=reviewer,
                                         task_id=task_id, decision=decision, reason=reason)

    def approved_submission(self, request_id="sub-ok", **overrides):
        receipt = self.submit(request_id, **overrides)
        task_id = self.open_task(receipt.resource_id)
        self.decide(f"{request_id}-d", "r1", task_id, "approve")
        return receipt.resource_id

    # ------------------------------------------------------------------
    # 投稿登记与校验
    # ------------------------------------------------------------------
    def test_submit_creates_pending_task(self):
        receipt = self.submit()
        self.assertFalse(receipt.replayed)
        queue = self.moon.list_review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual(receipt.resource_id, queue[0]["submission_id"])
        self.assertEqual("initial", queue[0]["kind"])
        self.assertEqual(1, queue[0]["rule_version"])
        view = self.moon.get_submission(receipt.resource_id)
        self.assertFalse(view["visible"])
        self.assertIn("尚未通过审核", view["notes"][0])

    def test_submit_stores_media_summary_without_body(self):
        receipt = self.submit(media_summaries=[{"media_id": "m1", "kind": "video", "sha256": "ab" * 32,
                                                "duration_ms": 1200, "description": "月色短片"}])
        content = self.moon.get_version(receipt.resource_id, 1)["content"]
        self.assertEqual("video", content["media_summaries"][0]["kind"])
        self.assertEqual("ab" * 32, content["media_summaries"][0]["sha256"])

    def test_submit_rejects_media_body_fields(self):
        with self.assertRaises(ValidationError):
            self.submit(media_summaries=[{"media_id": "m1", "kind": "image", "sha256": "ab" * 32,
                                          "data": "base64..."}])

    def test_submit_rejects_bad_media_hash(self):
        with self.assertRaises(ValidationError):
            self.submit(media_summaries=[{"media_id": "m1", "kind": "image", "sha256": "not-a-hash"}])

    def test_non_original_requires_citations(self):
        with self.assertRaises(ValidationError):
            self.submit(declaration={"original": False, "license_scopes": ["event_display"]})
        receipt = self.submit(declaration={"original": False, "license_scopes": ["event_display"]},
                              citations=[{"source": "《全唐诗》"}])
        self.assertFalse(receipt.replayed)

    def test_submit_rejects_unknown_scope_and_kind(self):
        with self.assertRaises(ValidationError):
            self.submit(declaration={"original": True, "license_scopes": ["everywhere"]})
        with self.assertRaises(ValidationError):
            self.submit(kind="live_stream")
        with self.assertRaises(ValidationError):
            self.submit(visibility_scope="friends")

    def test_submit_replay_and_conflict(self):
        first = self.submit("req-1")
        second = self.submit("req-1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        with self.assertRaises(ConflictError):
            self.submit("req-1", text_body="另一段文字")

    def test_auditor_and_reviewer_cannot_submit(self):
        with self.assertRaises(PermissionDenied):
            self.submit(actor_id="au1")
        with self.assertRaises(PermissionDenied):
            self.submit(actor_id="r1")

    # ------------------------------------------------------------------
    # 审核租约与结论
    # ------------------------------------------------------------------
    def test_lease_guarantees_single_decision(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=300)
        with self.assertRaises(ConflictError):
            self.moon.acquire_lease(actor_id="r2", task_id=task_id, lease_seconds=300)
        with self.assertRaises(PermissionDenied):
            self.moon.submit_decision(request_id="d-x", actor_id="r2",
                                      task_id=task_id, decision="approve")
        receipt = self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                            task_id=task_id, decision="approve")
        self.assertFalse(receipt.replayed)
        self.assertEqual("approved", self.moon.get_version(submission_id, 1)["review_state"])
        with self.assertRaises(ConflictError):
            self.decide("d-2", "r2", task_id, "reject", "重复决定")

    def test_decision_replay_is_safe(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=300)
        first = self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                          task_id=task_id, decision="approve")
        replay = self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                           task_id=task_id, decision="approve")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                      task_id=task_id, decision="reject", reason="改判")

    def test_expired_lease_can_be_taken_over(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=30)
        self.clock.advance(31)
        task = self.moon.acquire_lease(actor_id="r2", task_id=task_id, lease_seconds=300)
        self.assertEqual("r2", task["lease_owner"])
        with self.assertRaises(PermissionDenied):
            self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                      task_id=task_id, decision="approve")

    def test_expired_lease_blocks_decision(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=30)
        self.clock.advance(31)
        with self.assertRaises(ConflictError):
            self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                      task_id=task_id, decision="approve")

    def test_release_lease(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=300)
        with self.assertRaises(PermissionDenied):
            self.moon.release_lease(actor_id="r2", task_id=task_id)
        task = self.moon.release_lease(actor_id="r1", task_id=task_id)
        self.assertIsNone(task["lease_owner"])
        task = self.moon.acquire_lease(actor_id="r2", task_id=task_id, lease_seconds=300)
        self.assertEqual("r2", task["lease_owner"])

    def test_reject_requires_reason(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=300)
        with self.assertRaises(ValidationError):
            self.moon.submit_decision(request_id="d-1", actor_id="r1",
                                      task_id=task_id, decision="reject")

    def test_operator_cannot_decide(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        with self.assertRaises(PermissionDenied):
            self.moon.acquire_lease(actor_id="op1", task_id=task_id, lease_seconds=300)

    # ------------------------------------------------------------------
    # 复议
    # ------------------------------------------------------------------
    def test_appeal_requires_another_reviewer(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.decide("d-1", "r1", task_id, "reject", "与主题不符")
        self.moon.request_appeal(request_id="ap-1", actor_id="op1",
                                 submission_id=submission_id, version=1, reason="补充背景")
        appeal_task = self.open_task(submission_id, "appeal")
        with self.assertRaises(PermissionDenied):
            self.moon.acquire_lease(actor_id="r1", task_id=appeal_task, lease_seconds=300)
        self.decide("d-2", "r2", appeal_task, "approve", "复议通过")
        self.assertEqual("approved", self.moon.get_version(submission_id, 1)["review_state"])

    def test_appeal_only_for_rejected_and_only_once(self):
        submission_id = self.submit().resource_id
        with self.assertRaises(ConflictError):
            self.moon.request_appeal(request_id="ap-0", actor_id="op1",
                                     submission_id=submission_id, version=1, reason="尚未审核")
        task_id = self.open_task(submission_id)
        self.decide("d-1", "r1", task_id, "reject", "与主题不符")
        self.moon.request_appeal(request_id="ap-1", actor_id="op1",
                                 submission_id=submission_id, version=1, reason="补充背景")
        with self.assertRaises(ConflictError):
            self.moon.request_appeal(request_id="ap-2", actor_id="op1",
                                     submission_id=submission_id, version=1, reason="重复复议")

    def test_appeal_reject_is_terminal(self):
        submission_id = self.submit().resource_id
        task_id = self.open_task(submission_id)
        self.decide("d-1", "r1", task_id, "reject", "与主题不符")
        self.moon.request_appeal(request_id="ap-1", actor_id="op1",
                                 submission_id=submission_id, version=1, reason="补充背景")
        appeal_task = self.open_task(submission_id, "appeal")
        self.decide("d-2", "r2", appeal_task, "reject", "维持原结论")
        self.assertEqual("rejected", self.moon.get_version(submission_id, 1)["review_state"])
        self.assertEqual([], self.moon.list_review_queue())

    # ------------------------------------------------------------------
    # 版本与漂移
    # ------------------------------------------------------------------
    def test_new_version_cancels_pending_task(self):
        submission_id = self.submit("v1").resource_id
        self.moon.add_version(request_id="v2", actor_id="op1", submission_id=submission_id,
                              author_id="user-1", text_body="修改后的诗句",
                              declaration=DECLARATION, visibility_scope="public")
        queue = self.moon.list_review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual(2, queue[0]["version"])
        submission = self.moon.get_submission(submission_id)
        self.assertEqual(2, submission["current_version"])
        self.assertFalse(submission["visible"])

    def test_edit_keeps_previous_approved_version_served(self):
        submission_id = self.approved_submission("ok-1")
        self.moon.add_version(request_id="ok-1-v2", actor_id="op1", submission_id=submission_id,
                              author_id="user-1", text_body="修改后的诗句",
                              declaration=DECLARATION, visibility_scope="public")
        submission = self.moon.get_submission(submission_id)
        self.assertTrue(submission["visible"])
        self.assertEqual(1, submission["served_version"])
        self.assertIn("正在审核", submission["notes"][0])

    def test_edit_by_other_author_rejected(self):
        submission_id = self.submit("v1").resource_id
        with self.assertRaises(PermissionDenied):
            self.moon.add_version(request_id="v2", actor_id="op1", submission_id=submission_id,
                                  author_id="user-2", text_body="他人修改",
                                  declaration=DECLARATION, visibility_scope="public")

    # ------------------------------------------------------------------
    # 撤回与合规处置
    # ------------------------------------------------------------------
    def test_withdraw_blocks_reads_and_keeps_tombstone(self):
        submission_id = self.approved_submission("ok-1")
        self.moon.withdraw_submission(request_id="w-1", actor_id="op1",
                                      submission_id=submission_id, reason="作者撤回")
        view = self.moon.get_submission(submission_id)
        self.assertFalse(view["visible"])
        self.assertTrue(view["placeholder"])
        self.assertEqual("withdrawn", view["tombstone"]["status"])
        version = self.moon.get_version(submission_id, 1)
        self.assertTrue(version["placeholder"])
        self.assertIsNone(version["content"])
        self.assertTrue(version["content_hash"])
        explain = self.moon.explain_submission(submission_id)
        self.assertEqual("blocked", explain["effective_state"])
        with self.assertRaises(ConflictError):
            self.moon.add_version(request_id="w-2", actor_id="op1", submission_id=submission_id,
                                  author_id="user-1", text_body="再次修改",
                                  declaration=DECLARATION, visibility_scope="public")

    def test_takedown_cancels_open_tasks(self):
        submission_id = self.submit().resource_id
        self.assertEqual(1, len(self.moon.list_review_queue()))
        self.moon.takedown_submission(request_id="t-1", actor_id="r1",
                                      submission_id=submission_id, reason="合规处置")
        self.assertEqual([], self.moon.list_review_queue())
        view = self.moon.get_submission(submission_id)
        self.assertEqual("taken_down", view["tombstone"]["status"])
        with self.assertRaises(PermissionDenied):
            self.moon.takedown_submission(request_id="t-2", actor_id="op1",
                                          submission_id=submission_id, reason="越权")

    def test_close_is_terminal(self):
        submission_id = self.submit().resource_id
        self.moon.withdraw_submission(request_id="w-1", actor_id="op1",
                                      submission_id=submission_id, reason="作者撤回")
        with self.assertRaises(ConflictError):
            self.moon.takedown_submission(request_id="t-1", actor_id="r1",
                                          submission_id=submission_id, reason="重复处置")

    # ------------------------------------------------------------------
    # 规则升级
    # ------------------------------------------------------------------
    def test_rule_upgrade_requeues_only_open_tasks(self):
        decided_id = self.approved_submission("ok-1")
        pending_id = self.submit("p-1").resource_id
        pending_task = self.open_task(pending_id)
        self.moon.acquire_lease(actor_id="r1", task_id=pending_task, lease_seconds=300)
        receipt = self.moon.upgrade_rules(request_id="rule-1", actor_id="a1", note="新版规范")
        self.assertFalse(receipt.replayed)
        queue = self.moon.list_review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual(2, queue[0]["rule_version"])
        self.assertIsNone(queue[0]["lease_owner"])
        with self.assertRaises(PermissionDenied):
            self.moon.submit_decision(request_id="d-stale", actor_id="r1",
                                      task_id=pending_task, decision="approve")
        decided_tasks = self.moon.get_version(decided_id, 1)["tasks"]
        self.assertEqual(1, decided_tasks[0]["rule_version"])
        replay = self.moon.upgrade_rules(request_id="rule-1", actor_id="a1", note="新版规范")
        self.assertTrue(replay.replayed)

    def test_rule_upgrade_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.moon.upgrade_rules(request_id="rule-1", actor_id="r1", note="越权")

    # ------------------------------------------------------------------
    # 专题
    # ------------------------------------------------------------------
    def make_collection(self, request_id="c-1", usage_scope="collection_reuse"):
        receipt = self.moon.create_collection(request_id=request_id, actor_id="op1",
                                              site_id="s1", title="中秋专题",
                                              usage_scope=usage_scope)
        return receipt.resource_id

    def test_collection_item_requires_approved_and_scope(self):
        collection_id = self.make_collection()
        pending_id = self.submit("p-1").resource_id
        with self.assertRaises(ConflictError):
            self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                          collection_id=collection_id,
                                          submission_id=pending_id, version=1)
        narrow_id = self.approved_submission(
            "ok-2", declaration={"original": True, "license_scopes": ["event_display"]})
        with self.assertRaises(ConflictError):
            self.moon.add_collection_item(request_id="i-2", actor_id="op1",
                                          collection_id=collection_id,
                                          submission_id=narrow_id, version=1)
        approved_id = self.approved_submission("ok-3")
        receipt = self.moon.add_collection_item(request_id="i-3", actor_id="op1",
                                                collection_id=collection_id,
                                                submission_id=approved_id, version=1)
        self.assertFalse(receipt.replayed)
        with self.assertRaises(ConflictError):
            self.moon.add_collection_item(request_id="i-4", actor_id="op1",
                                          collection_id=collection_id,
                                          submission_id=approved_id, version=1)

    def test_collection_item_rejects_restricted_visibility(self):
        collection_id = self.make_collection()
        hidden_id = self.approved_submission("ok-1", visibility_scope="restricted")
        with self.assertRaises(ConflictError):
            self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                          collection_id=collection_id,
                                          submission_id=hidden_id, version=1)

    def test_publish_freezes_members_and_replays(self):
        collection_id = self.make_collection()
        approved_id = self.approved_submission("ok-1")
        self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                      collection_id=collection_id,
                                      submission_id=approved_id, version=1)
        first = self.moon.publish_collection(request_id="pub-1", actor_id="op1",
                                             collection_id=collection_id)
        replay = self.moon.publish_collection(request_id="pub-1", actor_id="op1",
                                              collection_id=collection_id)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        published = self.moon.get_published_collection(collection_id)
        self.assertEqual(1, published["publication_version"])
        self.assertEqual(1, len(published["members"]))
        self.assertTrue(published["members"][0]["available"])
        with self.assertRaises(ConflictError):
            self.moon.publish_collection(request_id="pub-1", actor_id="a1",
                                         collection_id=collection_id)

    def test_publish_rejects_whole_collection_on_any_invalid_member(self):
        collection_id = self.make_collection()
        keep_id = self.approved_submission("ok-1")
        drop_id = self.approved_submission("ok-2")
        self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                      collection_id=collection_id, submission_id=keep_id, version=1)
        self.moon.add_collection_item(request_id="i-2", actor_id="op1",
                                      collection_id=collection_id, submission_id=drop_id, version=1)
        self.moon.withdraw_submission(request_id="w-1", actor_id="op1",
                                      submission_id=drop_id, reason="作者撤回")
        with self.assertRaises(ConflictError):
            self.moon.publish_collection(request_id="pub-1", actor_id="op1",
                                         collection_id=collection_id)
        collection = self.moon.get_collection(collection_id)
        self.assertEqual("draft", collection["state"])
        self.assertIsNone(collection["latest_publication_version"])
        invalid = [item for item in collection["items"] if not item["valid"]]
        self.assertEqual(1, len(invalid))
        self.assertIn("投稿已撤回或已处置", invalid[0]["problems"])

    def test_published_collection_tombstones_invalidated_member(self):
        collection_id = self.make_collection()
        keep_id = self.approved_submission("ok-1")
        drop_id = self.approved_submission("ok-2")
        self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                      collection_id=collection_id, submission_id=keep_id, version=1)
        self.moon.add_collection_item(request_id="i-2", actor_id="op1",
                                      collection_id=collection_id, submission_id=drop_id, version=1)
        self.moon.publish_collection(request_id="pub-1", actor_id="op1",
                                     collection_id=collection_id)
        self.moon.takedown_submission(request_id="t-1", actor_id="r1",
                                      submission_id=drop_id, reason="合规处置")
        published = self.moon.get_published_collection(collection_id)
        members = {m["submission_id"]: m for m in published["members"]}
        self.assertTrue(members[keep_id]["available"])
        self.assertEqual("海上生明月", members[keep_id]["content"]["text_body"])
        self.assertFalse(members[drop_id]["available"])
        self.assertEqual("taken_down", members[drop_id]["reason"])
        self.assertTrue(members[drop_id]["placeholder"])
        self.assertIsNone(members[drop_id]["content"])
        self.assertTrue(members[drop_id]["content_hash"])

    def test_collection_does_not_drift_to_new_version(self):
        collection_id = self.make_collection()
        submission_id = self.approved_submission("ok-1")
        self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                      collection_id=collection_id,
                                      submission_id=submission_id, version=1)
        self.moon.publish_collection(request_id="pub-1", actor_id="op1",
                                     collection_id=collection_id)
        self.moon.add_version(request_id="ok-1-v2", actor_id="op1", submission_id=submission_id,
                              author_id="user-1", text_body="修改后的诗句",
                              declaration=DECLARATION, visibility_scope="public")
        task_id = self.open_task(submission_id)
        self.decide("d-v2", "r1", task_id, "approve")
        published = self.moon.get_published_collection(collection_id)
        self.assertEqual(1, published["members"][0]["version"])
        self.assertEqual("海上生明月", published["members"][0]["content"]["text_body"])
        submission = self.moon.get_submission(submission_id)
        self.assertEqual(2, submission["served_version"])

    def test_unpublished_collection_has_no_publication(self):
        collection_id = self.make_collection()
        with self.assertRaises(NotFoundError):
            self.moon.get_published_collection(collection_id)

    # ------------------------------------------------------------------
    # 解释
    # ------------------------------------------------------------------
    def test_explain_labels(self):
        submission_id = self.submit("v1").resource_id
        explain = self.moon.explain_submission(submission_id)
        self.assertEqual("restricted", explain["effective_state"])
        self.assertEqual("restricted", explain["versions"][0]["state_label"])
        task_id = self.open_task(submission_id)
        self.decide("d-1", "r1", task_id, "approve")
        explain = self.moon.explain_submission(submission_id)
        self.assertEqual("visible", explain["effective_state"])
        self.assertEqual("visible", explain["versions"][0]["state_label"])
        self.moon.add_version(request_id="v2", actor_id="op1", submission_id=submission_id,
                              author_id="user-1", text_body="修改后的诗句",
                              declaration=DECLARATION, visibility_scope="public")
        task_id = self.open_task(submission_id)
        self.decide("d-2", "r1", task_id, "approve")
        explain = self.moon.explain_submission(submission_id)
        labels = {item["version"]: item["state_label"] for item in explain["versions"]}
        self.assertEqual("superseded", labels[1])
        self.assertEqual("visible", labels[2])
        self.assertIn("被替代", explain["versions"][0]["reasons"][0])

    def test_explain_lists_collection_references(self):
        collection_id = self.make_collection()
        submission_id = self.approved_submission("ok-1")
        self.moon.add_collection_item(request_id="i-1", actor_id="op1",
                                      collection_id=collection_id,
                                      submission_id=submission_id, version=1)
        self.moon.publish_collection(request_id="pub-1", actor_id="op1",
                                     collection_id=collection_id)
        explain = self.moon.explain_submission(submission_id)
        references = explain["versions"][0]["referenced_by"]
        self.assertTrue(any(ref.get("pinned") for ref in references))
        self.assertTrue(any(ref.get("pinned_in_publication") for ref in references))

    # ------------------------------------------------------------------
    # 重启恢复
    # ------------------------------------------------------------------
    def test_queues_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moon.sqlite3"
            database = Database(path)
            foundation = DomainService(database, self.clock)
            moon = MoonGovernanceService(database, self.clock)
            foundation.register_organization(request_id="org", actor_id="bootstrap",
                                             organization_id="o1", name="云赏月组委会")
            foundation.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                      display_name="管理员", role="admin", organization_id="o1")
            foundation.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                      display_name="运营", role="operator", organization_id="o1")
            foundation.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="r1",
                                      display_name="审核员一", role="reviewer", organization_id="o1")
            foundation.register_actor(request_id="reviewer2", actor_id="a1", new_actor_id="r2",
                                      display_name="审核员二", role="reviewer", organization_id="o1")
            foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                     organization_id="o1", name="主会场", timezone_name="Asia/Shanghai")
            pending = moon.submit_submission(request_id="p-1", actor_id="op1", site_id="s1",
                                             kind="blessing", author_id="user-1",
                                             text_body="月圆人团圆", declaration=DECLARATION,
                                             visibility_scope="public")
            rejected = moon.submit_submission(request_id="p-2", actor_id="op1", site_id="s1",
                                              kind="blessing", author_id="user-2",
                                              text_body="另一段祝福", declaration=DECLARATION,
                                              visibility_scope="public")
            task_id = next(item["task_id"] for item in moon.list_review_queue()
                           if item["submission_id"] == rejected.resource_id)
            moon.acquire_lease(actor_id="r1", task_id=task_id, lease_seconds=300)
            moon.submit_decision(request_id="d-1", actor_id="r1",
                                 task_id=task_id, decision="reject", reason="与主题不符")
            moon.request_appeal(request_id="ap-1", actor_id="op1",
                                submission_id=rejected.resource_id, version=1, reason="补充背景")
            database.close()

            database2 = Database(path)
            foundation2 = DomainService(database2, self.clock)
            moon2 = MoonGovernanceService(database2, self.clock)
            initial_queue = moon2.list_review_queue("initial")
            appeal_queue = moon2.list_review_queue("appeal")
            self.assertEqual([pending.resource_id], [item["submission_id"] for item in initial_queue])
            self.assertEqual([rejected.resource_id], [item["submission_id"] for item in appeal_queue])
            valid, count = foundation2.verify_audit()
            self.assertTrue(valid)
            self.assertGreater(count, 0)
            explain = moon2.explain_submission(rejected.resource_id)
            self.assertEqual("restricted", explain["effective_state"])
            self.assertIn("复议处理中", explain["versions"][0]["reasons"][-1])
            database2.close()


if __name__ == "__main__":
    unittest.main()
