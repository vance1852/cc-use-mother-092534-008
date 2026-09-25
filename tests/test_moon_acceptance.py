import unittest

from moon_governance.acceptance import run


class MoonAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["replayed"])
        self.assertTrue(result["conflict_detected"])
        self.assertTrue(result["lease_conflict"])
        self.assertTrue(result["duplicate_decision_blocked"])
        self.assertTrue(result["same_reviewer_blocked"])
        self.assertTrue(result["appeal_overturned"])
        self.assertTrue(result["no_drift"])
        self.assertTrue(result["withdrawn_placeholder"])
        self.assertTrue(result["queue_requeued"])
        self.assertTrue(result["late_pending"])
        self.assertEqual("blocked", result["explain_hometown"])
        self.assertEqual("visible", result["explain_poetry"])
        self.assertEqual("visible", result["explain_after_restart"])
        self.assertEqual(result["queue_before_restart"], result["queue_after_restart"])
        self.assertGreaterEqual(result["queue_after_restart"], 1)
        self.assertEqual(3, result["published_members"])
        self.assertEqual(0, result["appeal_queue_after_restart"])


if __name__ == "__main__":
    unittest.main()
