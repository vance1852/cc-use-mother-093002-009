import unittest
from datetime import datetime, timezone

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.storage import Database
from inclusive_support.api import route
from inclusive_support.service import SupportService

from test_support_service import CONFIG


class SupportApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = SupportService(
            self.database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="管理单位")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理者", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                                    display_name="经办", role="operator", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def _admin(self):
        return {"X-Actor-Id": "admin1"}

    def _operator(self):
        return {"X-Actor-Id": "op1"}

    def test_policy_roundtrip_over_http(self):
        status, payload = route(self.service, "POST", "/support/policies",
                                {"request_id": "p1", "policy_id": "pol-1", "name": "普惠支持",
                                 "config": CONFIG}, self._admin())
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = route(self.service, "POST", "/support/policies",
                                {"request_id": "p1", "policy_id": "pol-1", "name": "普惠支持",
                                 "config": CONFIG}, self._admin())
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = route(self.service, "POST", "/support/policies/freeze",
                                {"request_id": "f1", "policy_id": "pol-1"}, self._admin())
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/support/policy?policy_id=pol-1", None)
        self.assertEqual(200, status)
        self.assertEqual("frozen", payload["status"])
        self.assertEqual(1_000_000, payload["budget"]["total_budget"])

    def test_foundation_routes_still_work(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_support_route_returns_404(self):
        status, payload = route(self.service, "GET", "/support/missing", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_body_returns_400(self):
        status, payload = route(self.service, "POST", "/support/policies",
                                {"request_id": "p1"}, self._admin())
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_permission_denied_over_http(self):
        status, payload = route(self.service, "POST", "/support/policies",
                                {"request_id": "p1", "policy_id": "pol-1", "name": "越权",
                                 "config": CONFIG}, self._operator())
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_missing_query_param_returns_400(self):
        status, payload = route(self.service, "GET", "/support/policy", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
