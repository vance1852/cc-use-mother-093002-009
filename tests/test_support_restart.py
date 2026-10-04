import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from inclusive_support.service import SupportService
from digital_trade_foundation.storage import Database

from test_support_service import CONFIG, ManualClock


class RestartDurabilityTest(unittest.TestCase):
    """恢复运行后预留时钟、候补位置和分期余额仍须准确。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "support.sqlite3"
        self.clock = ManualClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = SupportService(Database(self.path), self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="管理单位")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理者", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                                    display_name="经办", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev", actor_id="admin1", new_actor_id="rev1",
                                    display_name="评估", role="reviewer", organization_id="o1")

    def tearDown(self):
        self.directory.cleanup()

    def _reopen(self):
        self.service = SupportService(Database(self.path), self.clock)

    def _prepare(self):
        config = dict(CONFIG, total_budget=350_000, max_amount_per_application=350_000)
        self.service.create_policy(request_id="policy", actor_id="admin1", policy_id="pol-1",
                                   name="普惠支持", config=config)
        self.service.freeze_policy(request_id="freeze", actor_id="admin1", policy_id="pol-1")
        self.service.register_provider(request_id="prov", actor_id="op1",
                                       provider_id="prov-1", name="服务商")
        self.service.register_applicant(request_id="apl-a", actor_id="op1", applicant_id="apl-A",
                                        name="主体A", region_id="r-remote",
                                        affiliations=[{"related_key": "owner-x",
                                                       "relation": "beneficial_owner"}])
        self.service.register_applicant(request_id="apl-b", actor_id="op1", applicant_id="apl-B",
                                        name="主体B", region_id="r-town",
                                        affiliations=[{"related_key": "owner-y",
                                                       "relation": "beneficial_owner"}])
        self.service.submit_application(request_id="ap-1", actor_id="op1", application_id="ap-1",
                                        policy_id="pol-1", applicant_id="apl-A", provider_id="prov-1",
                                        category="infrastructure",
                                        baseline={"connectivity": 10, "device_ratio": 20},
                                        requested_amount=300_000)
        self.service.submit_application(request_id="ap-2", actor_id="op1", application_id="ap-2",
                                        policy_id="pol-1", applicant_id="apl-B", provider_id="prov-1",
                                        category="training",
                                        baseline={"connectivity": 50, "device_ratio": 60},
                                        requested_amount=250_000)
        for index, application_id in enumerate(("ap-1", "ap-2")):
            self.service.review_application(request_id=f"review-{index}", actor_id="rev1",
                                            application_id=application_id, decision="approve",
                                            note="通过")
        self.service.run_ranking(request_id="rank", actor_id="admin1", policy_id="pol-1")

    def test_restart_keeps_reservation_clock_and_waitlist(self):
        self._prepare()
        self.assertEqual("reserved", self.service.get_application("ap-1")["status"])
        self.assertEqual("waitlisted", self.service.get_application("ap-2")["status"])
        self._reopen()
        # 重启后预留时钟仍然有效：到期后清扫释放并沿稳定候补继续
        self.clock.advance(hours=73)
        policy = self.service.get_policy("pol-1")
        self.assertEqual("expired", self.service.get_application("ap-1")["status"])
        promoted = self.service.get_application("ap-2")
        self.assertEqual("reserved", promoted["status"])
        self.assertEqual("promotion", promoted["reservation"]["source"])
        self.assertEqual(100_000, policy["budget"]["available"])

    def test_restart_keeps_installment_balances(self):
        self._prepare()
        self.service.submit_material(request_id="mat", actor_id="op1", application_id="ap-1",
                                     material_key="budget_plan", payload_data={"total": 1})
        self.service.report_milestone(request_id="survey", actor_id="op1", application_id="ap-1",
                                      milestone_key="site_survey", kind="survey",
                                      evidence={"ok": True})
        self.service.verify_milestone(request_id="vsurvey", actor_id="rev1", application_id="ap-1",
                                      milestone_key="site_survey", approve=True, note="合格")
        self.service.commit_reservation(request_id="commit", actor_id="op1", application_id="ap-1")
        self.service.report_milestone(request_id="train", actor_id="op1", application_id="ap-1",
                                      milestone_key="training_done", kind="training",
                                      evidence={"sessions": 4})
        self.service.verify_milestone(request_id="vtrain", actor_id="rev1", application_id="ap-1",
                                      milestone_key="training_done", approve=True, note="达标")
        self._reopen()
        detail = self.service.get_application("ap-1")
        self.assertEqual(["released", "pending"],
                         [item["status"] for item in detail["installments"]])
        self.assertEqual([120_000, 180_000],
                         [item["amount"] for item in detail["installments"]])
        policy = self.service.get_policy("pol-1")
        self.assertEqual(300_000, policy["budget"]["commitment_outstanding"])
        self.assertEqual(120_000, policy["budget"]["disbursed"])
        self.assertEqual(50_000, policy["budget"]["available"])
        # 撤回只释放未兑现的 18 万，可用 23 万不足以让候补的 25 万转正
        self.service.withdraw_application(request_id="wd", actor_id="op1", application_id="ap-1",
                                          exit_obligations=["return_devices"])
        self.assertEqual("waitlisted", self.service.get_application("ap-2")["status"])
        policy = self.service.get_policy("pol-1")
        self.assertEqual(230_000, policy["budget"]["available"])


if __name__ == "__main__":
    unittest.main()
