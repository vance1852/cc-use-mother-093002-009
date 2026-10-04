import unittest

from inclusive_support.acceptance import run


class SupportAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["conflict_blocked"])
        self.assertEqual(["ap-1", "ap-3"], result["ranking"]["allocated"])
        self.assertEqual(["ap-2"], result["ranking"]["deduplicated"])
        self.assertEqual("completed", result["ap1_status"])
        self.assertEqual("cosigned", result["special_cosigned"])
        self.assertEqual(250_000, result["withdrawn_released"])
        self.assertEqual(600_000, result["budget_available"])
        self.assertTrue(result["audit_valid"])


if __name__ == "__main__":
    unittest.main()
