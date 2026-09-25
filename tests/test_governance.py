import unittest
from datetime import datetime, timedelta, timezone

from festival_foundation.errors import ConflictError, PermissionDenied, ValidationError
from festival_foundation.storage import Database
from moon_governance.service import GovernanceService


class ManualClock:
    def __init__(self, start):
        self._now = start

    def now(self):
        return self._now

    def advance(self, **kwargs):
        self._now = self._now + timedelta(**kwargs)


CONTENT = {
    "text_body": "明月几时有，把酒问青天。",
    "media_summaries": [{"media_id": "m1", "media_type": "image", "checksum": "sha256:abc"}],
    "declaration": {"original": True, "grant": "授权云赏月专题引用"},
    "citations": [{"reference": "苏轼《水调歌头》", "source_type": "classic"}],
    "visibility_scope": "public",
}


class GovernanceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = GovernanceService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="云赏月运营中心")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="运营", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev1", actor_id="a1", new_actor_id="r1",
                                    display_name="审核者甲", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="rev2", actor_id="a1", new_actor_id="r2",
                                    display_name="审核者乙", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="主会场", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ---- 辅助 ----

    def _submit(self, suffix="1", **overrides):
        payload = dict(request_id=f"req-sub-{suffix}", actor_id="op1", site_id="s1",
                       submission_id=f"sub-{suffix}", kind="poetry_chain",
                       author_id="user-1001", **CONTENT)
        payload.update(overrides)
        return self.service.submit_submission(**payload)

    def _open_task(self, submission_id):
        for item in self.service.review_queue() + self.service.appeal_queue():
            if item["submission_id"] == submission_id:
                return item
        return None

    def _claim(self, task_id, reviewer, tag):
        return self.service.claim_review(request_id=f"req-claim-{tag}", actor_id=reviewer,
                                         task_id=task_id)

    def _decide(self, submission_id, reviewer, decision, tag, reason="审核结论"):
        task = self._open_task(submission_id)
        self.assertIsNotNone(task, f"{submission_id} 没有待处理任务")
        claim = self._claim(task["task_id"], reviewer, tag)
        return self.service.decide_review(request_id=f"req-decide-{tag}", actor_id=reviewer,
                                          task_id=task["task_id"],
                                          lease_token=claim["lease_token"],
                                          decision=decision, reason=reason)

    def _approve(self, submission_id, reviewer="r1", tag=None):
        return self._decide(submission_id, reviewer, "approve", tag or f"approve-{submission_id}")

    # ---- 投稿与审核 ----

    def test_submit_enters_pending_queue(self):
        result = self._submit()
        self.assertEqual("pending", result["status"])
        queue = self.service.review_queue()
        self.assertEqual(1, len(queue))
        self.assertEqual("initial", queue[0]["kind"])
        self.assertEqual(1, queue[0]["rule_version"])

    def test_approve_makes_content_visible(self):
        self._submit()
        self._approve("sub-1")
        explanation = self.service.explain_submission("sub-1")
        self.assertEqual("visible", explanation["state"])
        self.assertEqual("approved", explanation["reason_code"])
        content = self.service.read_submission_content("sub-1")
        self.assertFalse(content["placeholder"])
        self.assertEqual("明月几时有，把酒问青天。", content["content"]["text_body"])
        self.assertEqual("sha256:abc", content["content"]["media_summaries"][0]["checksum"])

    def test_reject_restricts_content(self):
        self._submit()
        self._decide("sub-1", "r1", "reject", "reject-1", reason="引用出处不明")
        explanation = self.service.explain_submission("sub-1")
        self.assertEqual("restricted", explanation["state"])
        self.assertEqual("rejected", explanation["reason_code"])
        content = self.service.read_submission_content("sub-1")
        self.assertTrue(content["placeholder"])
        self.assertNotIn("content", content)

    def test_submission_replay_and_payload_conflict(self):
        first = self._submit()
        second = self._submit()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["version_id"], second["version_id"])
        with self.assertRaises(ConflictError):
            self._submit(text_body="被篡改的内容")

    def test_auditor_and_reviewer_cannot_submit(self):
        with self.assertRaises(PermissionDenied):
            self._submit(actor_id="au1", request_id="req-sub-au")
        with self.assertRaises(PermissionDenied):
            self._submit(actor_id="r1", request_id="req-sub-r")

    def test_media_and_declaration_validation(self):
        with self.assertRaises(ValidationError):
            self._submit(request_id="req-bad-media",
                         media_summaries=[{"media_id": "m1", "media_type": "image"}])
        with self.assertRaises(ValidationError):
            self._submit(request_id="req-bad-decl", declaration={"grant": "授权"})
        with self.assertRaises(ValidationError):
            self._submit(request_id="req-bad-scope", visibility_scope="everyone")
        with self.assertRaises(ValidationError):
            self._submit(request_id="req-bad-kind", kind="essay")

    # ---- 租约 ----

    def test_lease_is_exclusive_and_decide_needs_lease(self):
        self._submit()
        task = self._open_task("sub-1")
        self._claim(task["task_id"], "r1", "l1")
        with self.assertRaises(ConflictError):
            self._claim(task["task_id"], "r2", "l2")
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(request_id="req-decide-stranger", actor_id="r2",
                                       task_id=task["task_id"], lease_token="x",
                                       decision="approve", reason="越权")

    def test_decide_requires_valid_token(self):
        self._submit()
        task = self._open_task("sub-1")
        self._claim(task["task_id"], "r1", "token")
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(request_id="req-decide-badtoken", actor_id="r1",
                                       task_id=task["task_id"], lease_token="wrong",
                                       decision="approve", reason="令牌错误")

    def test_expired_lease_allows_reclaim_and_blocks_decide(self):
        self._submit()
        task = self._open_task("sub-1")
        claim = self._claim(task["task_id"], "r1", "expire")
        self.clock.advance(seconds=901)
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="req-decide-expired", actor_id="r1",
                                       task_id=task["task_id"],
                                       lease_token=claim["lease_token"],
                                       decision="approve", reason="租约过期")
        reclaim = self._claim(task["task_id"], "r2", "reclaim")
        result = self.service.decide_review(request_id="req-decide-reclaim", actor_id="r2",
                                            task_id=task["task_id"],
                                            lease_token=reclaim["lease_token"],
                                            decision="approve", reason="重新认领后审核")
        self.assertEqual("approved", result["review_status"])

    def test_single_valid_decision_and_replay(self):
        self._submit()
        task = self._open_task("sub-1")
        claim = self._claim(task["task_id"], "r1", "once")
        first = self.service.decide_review(request_id="req-decide-once", actor_id="r1",
                                           task_id=task["task_id"],
                                           lease_token=claim["lease_token"],
                                           decision="approve", reason="通过")
        replay = self.service.decide_review(request_id="req-decide-once", actor_id="r1",
                                            task_id=task["task_id"],
                                            lease_token=claim["lease_token"],
                                            decision="approve", reason="通过")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="req-decide-twice", actor_id="r1",
                                       task_id=task["task_id"],
                                       lease_token=claim["lease_token"],
                                       decision="reject", reason="重复决定")
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="req-decide-once", actor_id="r1",
                                       task_id=task["task_id"],
                                       lease_token=claim["lease_token"],
                                       decision="reject", reason="同号异载荷")

    # ---- 版本与替代 ----

    def _revise(self, submission_id, tag, text="修订后的内容", scope="public"):
        return self.service.revise_submission(
            request_id=f"req-revise-{tag}", actor_id="op1", submission_id=submission_id,
            **{**CONTENT, "text_body": text, "visibility_scope": scope})

    def test_revision_creates_new_version_and_replaces_old(self):
        self._submit()
        self._approve("sub-1")
        revised = self._revise("sub-1", "r1")
        self.assertEqual(2, revised["version_no"])
        detail = self.service.get_submission("sub-1")
        self.assertEqual(2, len(detail["versions"]))
        old_explain = self.service.explain_version(detail["versions"][0]["version_id"])
        self.assertEqual("replaced", old_explain["state"])
        self.assertEqual("superseded", old_explain["reason_code"])
        current = self.service.explain_submission("sub-1")
        self.assertEqual("restricted", current["state"])
        self.assertEqual("pending_review", current["reason_code"])

    def test_revision_cancels_open_task(self):
        self._submit()
        task = self._open_task("sub-1")
        claim = self._claim(task["task_id"], "r1", "stale")
        self._revise("sub-1", "cancel")
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="req-decide-stale", actor_id="r1",
                                       task_id=task["task_id"],
                                       lease_token=claim["lease_token"],
                                       decision="approve", reason="任务已取消")

    # ---- 撤回与合规处置 ----

    def test_withdraw_blocks_reads_and_keeps_audit_placeholder(self):
        self._submit()
        self._approve("sub-1")
        self.service.withdraw_submission(request_id="req-withdraw-1", actor_id="op1",
                                         submission_id="sub-1", reason="作者申请撤回")
        content = self.service.read_submission_content("sub-1")
        self.assertEqual("blocked", content["state"])
        self.assertEqual("withdrawn", content["reason_code"])
        self.assertTrue(content["placeholder"])
        explanation = self.service.explain_submission("sub-1")
        self.assertEqual("blocked", explanation["state"])
        actions = [event["action"] for event in self.service.audit_events()]
        self.assertIn("submission.withdrawn", actions)

    def test_withdraw_is_terminal(self):
        self._submit()
        self.service.withdraw_submission(request_id="req-withdraw-t", actor_id="op1",
                                         submission_id="sub-1", reason="撤回")
        with self.assertRaises(ConflictError):
            self._revise("sub-1", "after-withdraw")
        with self.assertRaises(ConflictError):
            self.service.withdraw_submission(request_id="req-withdraw-again", actor_id="op1",
                                             submission_id="sub-1", reason="重复撤回")

    def test_compliance_remove_blocks_and_requires_admin(self):
        self._submit()
        self._approve("sub-1")
        with self.assertRaises(PermissionDenied):
            self.service.compliance_remove(request_id="req-comp-deny", actor_id="op1",
                                           submission_id="sub-1", reason="越权")
        self.service.compliance_remove(request_id="req-comp-1", actor_id="a1",
                                       submission_id="sub-1", reason="违反社区规范")
        content = self.service.read_submission_content("sub-1")
        self.assertEqual("blocked", content["state"])
        self.assertEqual("compliance_removed", content["reason_code"])
        with self.assertRaises(ConflictError):
            self._revise("sub-1", "after-compliance")

    # ---- 复议 ----

    def test_appeal_requires_different_reviewer_and_can_overturn(self):
        self._submit()
        self._decide("sub-1", "r1", "reject", "rej-appeal")
        self.service.request_appeal(request_id="req-appeal-1", actor_id="op1",
                                    submission_id="sub-1", reason="作者补充出处")
        task = self._open_task("sub-1")
        self.assertEqual("appeal", task["kind"])
        with self.assertRaises(PermissionDenied):
            self._claim(task["task_id"], "r1", "appeal-self")
        claim = self._claim(task["task_id"], "r2", "appeal-other")
        result = self.service.decide_review(request_id="req-decide-appeal", actor_id="r2",
                                            task_id=task["task_id"],
                                            lease_token=claim["lease_token"],
                                            decision="overturn", reason="复议通过")
        self.assertEqual("approved", result["review_status"])
        self.assertEqual("visible", self.service.explain_submission("sub-1")["state"])

    def test_appeal_uphold_keeps_rejected(self):
        self._submit()
        self._decide("sub-1", "r1", "reject", "rej-uphold")
        self.service.request_appeal(request_id="req-appeal-up", actor_id="op1",
                                    submission_id="sub-1", reason="作者申诉")
        self._decide("sub-1", "r2", "uphold", "appeal-uphold")
        explanation = self.service.explain_submission("sub-1")
        self.assertEqual("rejected", explanation["reason_code"])
        self.assertEqual("decided", explanation["appeal"]["status"])
        with self.assertRaises(ConflictError):
            self.service.request_appeal(request_id="req-appeal-again", actor_id="op1",
                                        submission_id="sub-1", reason="重复复议")

    def test_appeal_only_for_rejected(self):
        self._submit()
        with self.assertRaises(ConflictError):
            self.service.request_appeal(request_id="req-appeal-pending", actor_id="op1",
                                        submission_id="sub-1", reason="未驳回")

    # ---- 规则升级 ----

    def test_rule_upgrade_requeues_only_open_submissions(self):
        self._submit(suffix="a")
        self._submit(suffix="b")
        self._submit(suffix="c")
        self._submit(suffix="d")
        self._approve("sub-a")
        self._decide("sub-b", "r1", "reject", "rej-b")
        self.service.withdraw_submission(request_id="req-withdraw-c", actor_id="op1",
                                         submission_id="sub-c", reason="撤回")
        upgrade = self.service.upgrade_rules(request_id="req-rules-2", actor_id="a1",
                                             note="规则升级")
        self.assertEqual(2, upgrade["rule_version"])
        self.assertEqual(2, upgrade["requeued"])
        queue = {item["submission_id"]: item for item in self.service.review_queue()}
        self.assertEqual({"sub-a", "sub-d"}, set(queue))
        self.assertEqual("reevaluation", queue["sub-a"]["kind"])
        self.assertEqual(2, queue["sub-a"]["rule_version"])
        restricted = self.service.explain_submission("sub-a")
        self.assertEqual("restricted", restricted["state"])
        self.assertEqual("rule_upgrade_pending", restricted["reason_code"])
        self.assertEqual("rejected", self.service.explain_submission("sub-b")["reason_code"])
        self.assertEqual("blocked", self.service.explain_submission("sub-c")["state"])

    def test_reevaluation_approve_restores_visibility(self):
        self._submit()
        self._approve("sub-1")
        self.service.upgrade_rules(request_id="req-rules-x", actor_id="a1", note="升级")
        self._approve("sub-1", reviewer="r2", tag="reeval")
        explanation = self.service.explain_submission("sub-1")
        self.assertEqual("visible", explanation["state"])
        self.assertEqual(2, explanation["approved_rule_version"])

    def test_rule_upgrade_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.upgrade_rules(request_id="req-rules-deny", actor_id="op1", note="越权")

    # ---- 专题 ----

    def _topic_with_member(self, submission_id="sub-1", scope="event", topic_id="topic-1"):
        self.service.create_topic(request_id=f"req-topic-{topic_id}", actor_id="op1",
                                  topic_id=topic_id, site_id="s1", title="云赏月精选",
                                  audience_scope=scope)
        version_id = self.service.get_submission(submission_id)["versions"][0]["version_id"]
        self.service.add_topic_member(request_id=f"req-add-{topic_id}-{submission_id}",
                                      actor_id="op1", topic_id=topic_id,
                                      submission_id=submission_id, version_id=version_id)
        return version_id

    def test_add_member_requires_approved_version(self):
        self._submit()
        self.service.create_topic(request_id="req-topic-p", actor_id="op1", topic_id="topic-p",
                                  site_id="s1", title="草稿专题", audience_scope="public")
        version_id = self.service.get_submission("sub-1")["versions"][0]["version_id"]
        with self.assertRaises(ValidationError):
            self.service.add_topic_member(request_id="req-add-pending", actor_id="op1",
                                          topic_id="topic-p", submission_id="sub-1",
                                          version_id=version_id)

    def test_add_member_requires_scope_match(self):
        self._submit(visibility_scope="event")
        self._approve("sub-1")
        self.service.create_topic(request_id="req-topic-pub", actor_id="op1",
                                  topic_id="topic-pub", site_id="s1", title="公开专题",
                                  audience_scope="public")
        version_id = self.service.get_submission("sub-1")["versions"][0]["version_id"]
        with self.assertRaises(ValidationError):
            self.service.add_topic_member(request_id="req-add-scope", actor_id="op1",
                                          topic_id="topic-pub", submission_id="sub-1",
                                          version_id=version_id)
        self._topic_with_member(scope="event", topic_id="topic-event")

    def test_publish_freezes_members_and_replays(self):
        self._submit()
        self._approve("sub-1")
        version_id = self._topic_with_member()
        published = self.service.publish_topic(request_id="req-publish-1", actor_id="op1",
                                               topic_id="topic-1")
        self.assertEqual("published", published["status"])
        replay = self.service.publish_topic(request_id="req-publish-1", actor_id="op1",
                                            topic_id="topic-1")
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ConflictError):
            self.service.publish_topic(request_id="req-publish-again", actor_id="op1",
                                       topic_id="topic-1")
        with self.assertRaises(ConflictError):
            self.service.add_topic_member(request_id="req-add-after-publish", actor_id="op1",
                                          topic_id="topic-1", submission_id="sub-1",
                                          version_id=version_id)

    def test_published_topic_keeps_pinned_version(self):
        self._submit()
        self._approve("sub-1")
        version_id = self._topic_with_member()
        self.service.publish_topic(request_id="req-publish-pin", actor_id="op1",
                                   topic_id="topic-1")
        self._revise("sub-1", "pin", text="修订版不漂移")
        topic = self.service.get_topic("topic-1")
        item = topic["items"][0]
        self.assertEqual(version_id, item["version_id"])
        self.assertEqual("ok", item["state"])
        self.assertEqual("明月几时有，把酒问青天。", item["content"]["text_body"])

    def test_publish_rejects_when_any_member_invalid(self):
        self._submit(suffix="a")
        self._submit(suffix="b")
        self._approve("sub-a")
        self._approve("sub-b")
        self._topic_with_member("sub-a")
        version_b = self.service.get_submission("sub-b")["versions"][0]["version_id"]
        self.service.add_topic_member(request_id="req-add-b", actor_id="op1", topic_id="topic-1",
                                      submission_id="sub-b", version_id=version_b)
        self.service.withdraw_submission(request_id="req-withdraw-b", actor_id="op1",
                                         submission_id="sub-b", reason="撤回")
        with self.assertRaises(ValidationError):
            self.service.publish_topic(request_id="req-publish-fail", actor_id="op1",
                                       topic_id="topic-1")
        self.assertEqual("draft", self.service.get_topic("topic-1")["status"])

    def test_topic_member_placeholder_after_withdraw(self):
        self._submit()
        self._approve("sub-1")
        self._topic_with_member()
        self.service.publish_topic(request_id="req-publish-w", actor_id="op1",
                                   topic_id="topic-1")
        self.service.withdraw_submission(request_id="req-withdraw-w", actor_id="op1",
                                         submission_id="sub-1", reason="撤回")
        item = self.service.get_topic("topic-1")["items"][0]
        self.assertEqual("blocked", item["state"])
        self.assertEqual("withdrawn", item["reason_code"])
        self.assertTrue(item["placeholder"])
        self.assertNotIn("content", item)

    def test_topic_requires_operator_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_topic(request_id="req-topic-deny", actor_id="r1",
                                      topic_id="topic-deny", site_id="s1", title="越权",
                                      audience_scope="event")

    # ---- 重启恢复 ----

    def test_queues_survive_restart(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            service = GovernanceService(database, self.clock)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="云赏月运营中心")
            service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                   display_name="运营", role="operator", organization_id="o1")
            service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="主会场",
                                  timezone_name="Asia/Shanghai")
            service.submit_submission(request_id="req-sub-1", actor_id="op1", site_id="s1",
                                      submission_id="sub-1", kind="blessing",
                                      author_id="user-1001", **CONTENT)
            database.close()

            database2 = Database(path)
            service2 = GovernanceService(database2, self.clock)
            queue = service2.review_queue()
            self.assertEqual(1, len(queue))
            self.assertEqual("sub-1", queue[0]["submission_id"])
            self.assertEqual([], service2.appeal_queue())
            database2.close()


if __name__ == "__main__":
    unittest.main()
