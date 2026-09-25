import unittest

from festival_foundation.service import DomainService
from festival_foundation.storage import Database
from moon_governance.api import route
from moon_governance.service import MoonGovernanceService


DECLARATION = {"original": True, "license_scopes": ["event_display", "collection_reuse"]}


class MoonApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.foundation = DomainService(self.database)
        self.moon = MoonGovernanceService(self.database)
        self.call("POST", "/organizations",
                  {"request_id": "org", "organization_id": "o1", "name": "云赏月组委会"},
                  actor="bootstrap")
        self.call("POST", "/actors",
                  {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
                   "role": "admin", "organization_id": "o1"}, actor="bootstrap")
        self.call("POST", "/actors",
                  {"request_id": "operator", "new_actor_id": "op1", "display_name": "运营",
                   "role": "operator", "organization_id": "o1"}, actor="a1")
        self.call("POST", "/actors",
                  {"request_id": "reviewer", "new_actor_id": "r1", "display_name": "审核员",
                   "role": "reviewer", "organization_id": "o1"}, actor="a1")
        self.call("POST", "/sites",
                  {"request_id": "site", "site_id": "s1", "organization_id": "o1",
                   "name": "主会场", "timezone_name": "Asia/Shanghai"}, actor="op1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor=""):
        return route(self.moon, self.foundation, method, path, body,
                     {"X-Actor-Id": actor} if actor else {})

    def submit(self, request_id="sub-1"):
        status, payload = self.call("POST", "/submissions",
                                    {"request_id": request_id, "site_id": "s1",
                                     "kind": "blessing", "author_id": "user-1",
                                     "text_body": "但愿人长久", "declaration": DECLARATION,
                                     "visibility_scope": "public"}, actor="op1")
        self.assertEqual(201, status)
        return payload["resource_id"]

    def test_health_and_foundation_fallback(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = self.call("GET", "/missing")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_submission_review_flow_over_http(self):
        submission_id = self.submit()
        status, payload = self.call("GET", "/review-queue")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        task_id = payload["items"][0]["task_id"]

        status, payload = self.call("POST", f"/review-tasks/{task_id}/lease",
                                    {"lease_seconds": 120}, actor="r1")
        self.assertEqual(200, status)
        self.assertEqual("r1", payload["lease_owner"])

        status, payload = self.call("POST", f"/review-tasks/{task_id}/decision",
                                    {"request_id": "d-1", "decision": "approve"}, actor="r1")
        self.assertEqual(201, status)
        status, payload = self.call("POST", f"/review-tasks/{task_id}/decision",
                                    {"request_id": "d-1", "decision": "approve"}, actor="r1")
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

        status, payload = self.call("GET", f"/submissions/{submission_id}")
        self.assertEqual(200, status)
        self.assertTrue(payload["visible"])
        self.assertEqual("但愿人长久", payload["content"]["text_body"])

        status, payload = self.call("GET", f"/submissions/{submission_id}/explain")
        self.assertEqual(200, status)
        self.assertEqual("visible", payload["effective_state"])

    def test_collection_flow_over_http(self):
        submission_id = self.submit()
        status, payload = self.call("GET", "/review-queue")
        task_id = payload["items"][0]["task_id"]
        self.call("POST", f"/review-tasks/{task_id}/lease", {"lease_seconds": 120}, actor="r1")
        self.call("POST", f"/review-tasks/{task_id}/decision",
                  {"request_id": "d-1", "decision": "approve"}, actor="r1")

        status, payload = self.call("POST", "/collections",
                                    {"request_id": "c-1", "site_id": "s1", "title": "中秋专题",
                                     "usage_scope": "collection_reuse"}, actor="op1")
        self.assertEqual(201, status)
        collection_id = payload["resource_id"]
        status, _ = self.call("POST", f"/collections/{collection_id}/items",
                              {"request_id": "i-1", "submission_id": submission_id, "version": 1},
                              actor="op1")
        self.assertEqual(201, status)
        status, payload = self.call("POST", f"/collections/{collection_id}/publish",
                                    {"request_id": "p-1"}, actor="op1")
        self.assertEqual(201, status)
        status, payload = self.call("GET", f"/collections/{collection_id}/publication")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["publication_version"])
        self.assertTrue(payload["members"][0]["available"])

    def test_invalid_body_returns_400(self):
        status, payload = self.call("POST", "/submissions", {"request_id": "x"}, actor="op1")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_missing_actor_returns_404(self):
        status, payload = self.call("POST", "/submissions",
                                    {"request_id": "sub-1", "site_id": "s1", "kind": "blessing",
                                     "author_id": "user-1", "text_body": "但愿人长久",
                                     "declaration": DECLARATION, "visibility_scope": "public"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_withdraw_then_read_tombstone_over_http(self):
        submission_id = self.submit()
        status, payload = self.call("POST", f"/submissions/{submission_id}/withdraw",
                                    {"request_id": "w-1", "reason": "作者撤回"}, actor="op1")
        self.assertEqual(201, status)
        status, payload = self.call("GET", f"/submissions/{submission_id}")
        self.assertEqual(200, status)
        self.assertTrue(payload["placeholder"])
        self.assertIsNone(payload["content"])
        status, payload = self.call("GET", f"/submissions/{submission_id}/versions/1")
        self.assertEqual(200, status)
        self.assertTrue(payload["placeholder"])
        self.assertIsNone(payload["content"])


if __name__ == "__main__":
    unittest.main()
