import unittest

from digital_trade_foundation.acceptance_inclusive import POLICY
from digital_trade_foundation.api import route
from digital_trade_foundation.inclusive import InclusiveService
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database


class InclusiveApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.domain = DomainService(self.database)
        self.inclusive = InclusiveService(self.database)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="管理机构")
        self.domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="a1"):
        return route(self.domain, method, path, body, {"X-Actor-Id": actor},
                     inclusive=self.inclusive)

    def test_inclusive_route_requires_known_path(self):
        status, payload = self._route("GET", "/inclusive/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_policy_freeze_and_round_lifecycle_over_http(self):
        status, receipt = self._route("POST", "/inclusive/policies",
                                      {"request_id": "p1", "version": "v1", "policy": POLICY})
        self.assertEqual(201, status)
        self.assertEqual("policy", receipt["resource_type"])
        # 重放同一请求返回 200 而不是再次创建。
        status, replay = self._route("POST", "/inclusive/policies",
                                     {"request_id": "p1", "version": "v1", "policy": POLICY})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        status, round_receipt = self._route("POST", "/inclusive/rounds",
                                            {"request_id": "r1", "round_id": "r1",
                                             "policy_version": "v1",
                                             "budget": {"connectivity": 100000}})
        self.assertEqual(201, status)
        status, report = self._route("GET", "/inclusive/reports/funds?round_id=r1")
        self.assertEqual(200, status)
        self.assertEqual(100000, report["categories"]["connectivity"]["budget"])

    def test_policy_comparison_available(self):
        status, payload = self._route("GET", "/inclusive/reports/policy-comparison")
        self.assertEqual(200, status)
        self.assertIn("versions", payload)


if __name__ == "__main__":
    unittest.main()
