import unittest

from moon_governance.acceptance import run


class GovernanceAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(all(value for value in result["checks"].values()
                            if isinstance(value, bool)))


if __name__ == "__main__":
    unittest.main()
