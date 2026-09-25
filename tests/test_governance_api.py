import unittest

from festival_foundation.storage import Database
from moon_governance.api import route
from moon_governance.service import GovernanceService

ACTOR = {"X-Actor-Id": "bootstrap"}


class GovernanceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = GovernanceService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "云赏月运营中心"}, ACTOR)
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, ACTOR)
        route(self.service, "POST", "/actors",
              {"request_id": "operator", "new_actor_id": "op1", "display_name": "运营",
               "role": "operator", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors",
              {"request_id": "reviewer", "new_actor_id": "r1", "display_name": "审核者",
               "role": "reviewer", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "主会场", "timezone_name": "Asia/Shanghai"}, {"X-Actor-Id": "op1"})

    def tearDown(self):
        self.database.close()

    def _submit(self, request_id="req-sub-1"):
        return route(self.service, "POST", "/submissions", {
            "request_id": request_id, "site_id": "s1", "submission_id": "sub-1",
            "kind": "blessing", "author_id": "user-1001",
            "text_body": "但愿人长久，千里共婵娟。",
            "media_summaries": [{"media_id": "m1", "media_type": "image",
                                 "checksum": "sha256:abc"}],
            "declaration": {"original": True, "grant": "授权专题引用"},
            "citations": [], "visibility_scope": "public"}, {"X-Actor-Id": "op1"})

    def test_submit_then_replay_returns_same_receipt(self):
        status, first = self._submit()
        self.assertEqual(201, status)
        self.assertFalse(first["replayed"])
        status, second = self._submit()
        self.assertEqual(200, status)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["version_id"], second["version_id"])

    def test_explain_and_queue_endpoints(self):
        self._submit()
        status, explanation = route(self.service, "GET", "/submissions/sub-1/explain", None)
        self.assertEqual(200, status)
        self.assertEqual("restricted", explanation["state"])
        self.assertEqual("pending_review", explanation["reason_code"])
        status, queue = route(self.service, "GET", "/review-queue", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(queue["items"]))
        status, appeals = route(self.service, "GET", "/appeal-queue", None)
        self.assertEqual(200, status)
        self.assertEqual([], appeals["items"])

    def test_decide_without_lease_returns_409(self):
        _, submission = self._submit()
        status, payload = route(self.service, "POST",
                                f"/review-tasks/{submission['task_id']}/decisions",
                                {"request_id": "req-decide", "lease_token": "x",
                                 "decision": "approve", "reason": "未认领"},
                                {"X-Actor-Id": "r1"})
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_missing_field_returns_400(self):
        status, payload = route(self.service, "POST", "/submissions",
                                {"request_id": "req-incomplete"}, {"X-Actor-Id": "op1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_foundation_routes_still_work(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])
        status, payload = route(self.service, "GET", "/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
