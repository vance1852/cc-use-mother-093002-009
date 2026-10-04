import unittest

from digital_trade_foundation.acceptance_inclusive import POLICY, _bootstrap
from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from digital_trade_foundation.storage import Database


class InclusiveTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        _, self.svc = _bootstrap(self.database, 0)
        self.svc.register_region(request_id="regr", actor_id="a1", region_id="rg-remote",
                                 name="偏远县", tier="remote", priority=0.9, population=1000)
        self.svc.register_region(request_id="regr2", actor_id="a1", region_id="rg-rural",
                                 name="乡镇", tier="rural", priority=0.6, population=1000)
        self.svc.register_region(request_id="regu", actor_id="a1", region_id="rg-urban",
                                 name="城区", tier="urban", priority=0.1, population=1000)
        self.svc.register_provider(request_id="pvp", actor_id="a1", provider_id="pv1",
                                   organization_id="o6", name="云联服务商")
        self.svc.freeze_policy(request_id="pol", actor_id="a1", version="v1", policy=POLICY)
        self.svc.open_round(request_id="rnd", actor_id="a1", round_id="r1",
                            policy_version="v1", budget={"connectivity": 200000})

    def tearDown(self):
        self.database.close()

    def _applicant(self, req, applicant, org, region, group, readiness=20, pop=100):
        self.svc.register_applicant(request_id=req, actor_id="op1", applicant_id=applicant,
                                    organization_id=org, region_id=region,
                                    affiliation_group=group,
                                    baseline={"digital_readiness": readiness},
                                    beneficiary_population=pop)

    def _full_app(self, app_req, app_id, applicant, amount=90000):
        return self.svc.submit_application(request_id=app_req, actor_id="op1",
                                           application_id=app_id,
                                           round_id="r1", applicant_id=applicant,
                                           category="connectivity", provider_id="pv1",
                                           requested_amount=amount)

    def test_affiliation_dedup_keeps_only_first_application(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._applicant("a2x", "ap2", "o3", "rg-remote", "g1")
        first = self._full_app("f1", "app1", "ap1")
        second = self._full_app("f2", "app2", "ap2")
        self.assertEqual("eligible", first["screening_result"])
        self.assertEqual("duplicate", second["screening_result"])

    def test_declared_interest_blocks_reviewer(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._full_app("f1", "app1", "ap1")
        self.svc.declare_reviewer_interest(request_id="link", actor_id="a1",
                                           reviewer_id="rv1", provider_id="pv1")
        with self.assertRaises(PermissionDenied):
            self.svc.submit_review(request_id="rev", actor_id="rv1", application_id="app1", score=80)

    def test_provider_callback_replay_does_not_pay_installment_twice(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._full_app("f1", "app1", "ap1")
        self.svc.submit_review(request_id="rv", actor_id="rv1", application_id="app1", score=90)
        self.svc.run_ranking(request_id="run", actor_id="a1", round_id="r1")
        for key in ("id_copy", "budget_plan"):
            self.svc.submit_material(request_id=f"m-{key}", actor_id="op1",
                                     application_id="app1", material_key=key)
        self.svc.report_milestone(request_id="m1", actor_id="op1", application_id="app1", code="m1")
        self.svc.verify_milestone(request_id="v1", actor_id="rv1", application_id="app1",
                                  code="m1", passed=True)
        self.svc.commit_reservation(request_id="c", actor_id="op1", application_id="app1")
        before = self.svc.installment_balance("app1")["paid_amount"]
        first = self.svc.report_milestone(request_id="m2", actor_id="op1",
                                          application_id="app1", code="m2")
        replay = self.svc.report_milestone(request_id="m2", actor_id="op1",
                                           application_id="app1", code="m2")
        self.svc.verify_milestone(request_id="v2", actor_id="rv1", application_id="app1",
                                  code="m2", passed=True)
        after = self.svc.installment_balance("app1")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(90000, after["paid_amount"])
        self.assertEqual(0, after["remaining_amount"])
        self.assertEqual(before, 54000)

    def test_commit_requires_materials_and_prerequisite(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._full_app("f1", "app1", "ap1")
        self.svc.submit_review(request_id="rv", actor_id="rv1", application_id="app1", score=90)
        self.svc.run_ranking(request_id="run", actor_id="a1", round_id="r1")
        with self.assertRaises(ConflictError):
            self.svc.commit_reservation(request_id="c-early", actor_id="op1",
                                        application_id="app1")
        self.svc.submit_material(request_id="k1", actor_id="op1",
                                 application_id="app1", material_key="id_copy")
        self.svc.submit_material(request_id="k2", actor_id="op1",
                                 application_id="app1", material_key="budget_plan")
        with self.assertRaises(ConflictError):
            self.svc.commit_reservation(request_id="c-blocked", actor_id="op1",
                                        application_id="app1")

    def test_waiver_releases_reservation_and_promotes_waitlist(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1", readiness=10)
        self._applicant("a3x", "ap3", "o4", "rg-urban", "g3", readiness=90)
        self._applicant("a4x", "ap4", "o5", "rg-rural", "g4", readiness=50)
        for req, app, ap in (("f1", "app1", "ap1"), ("f3", "app3", "ap3"), ("f4", "app4", "ap4")):
            self._full_app(req, app, ap, amount=90000 if app != "app3" else 80000)
        for app, score in (("app1", 90), ("app3", 70), ("app4", 80)):
            self.svc.submit_review(request_id=f"rv-{app}", actor_id="rv1",
                                   application_id=app, score=score)
        self.svc.run_ranking(request_id="run", actor_id="a1", round_id="r1")
        self.assertEqual("reserved", self.svc.get_application("app1")["status"])
        self.svc.waive_application(request_id="w", actor_id="op1", application_id="app1")
        self.assertEqual("reserved", self.svc.get_application("app3")["status"])
        waitlist = self.svc.list_waitlist("r1")
        # 候补 app3 已晋升为预留；app4 本来就是选中预留，没有遗留的可晋升候补。
        self.assertEqual(["app3"], [i["application_id"] for i in waitlist])
        self.assertTrue(all(i["status"] == "reserved" for i in waitlist))

    def test_partial_failure_releases_remaining_and_locks_paid(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._applicant("a3x", "ap3", "o4", "rg-urban", "g3", readiness=90)
        self._full_app("f1", "app1", "ap1")
        self._full_app("f3", "app3", "ap3", amount=80000)
        self.svc.submit_review(request_id="r1", actor_id="rv1", application_id="app1", score=90)
        self.svc.submit_review(request_id="r3", actor_id="rv1", application_id="app3", score=70)
        self.svc.run_ranking(request_id="run", actor_id="a1", round_id="r1")
        for key in ("id_copy", "budget_plan"):
            self.svc.submit_material(request_id=f"k-{key}", actor_id="op1",
                                     application_id="app1", material_key=key)
        self.svc.report_milestone(request_id="m1", actor_id="op1", application_id="app1", code="m1")
        self.svc.verify_milestone(request_id="v1", actor_id="rv1", application_id="app1",
                                  code="m1", passed=True)
        self.svc.commit_reservation(request_id="c", actor_id="op1", application_id="app1")
        # 部署里程碑核验失败：剩余分期释放，已兑付保留，候补晋升。
        self.svc.report_milestone(request_id="m2", actor_id="op1", application_id="app1", code="m2")
        self.svc.verify_milestone(request_id="v2", actor_id="rv1", application_id="app1",
                                  code="m2", passed=False)
        balance = self.svc.installment_balance("app1")
        self.assertEqual(54000, balance["paid_amount"])
        self.assertEqual(36000, balance["released_amount"])
        self.assertEqual("partial_failed", self.svc.get_application("app1")["status"])
        self.assertEqual("reserved", self.svc.get_application("app3")["status"])
        # 重排时局部失败的支持锁定，不重新参与。
        run2 = self.svc.run_ranking(request_id="run2", actor_id="a1", round_id="r1")
        listing = self.svc.list_ranking(run2["resource_id"])
        item = next(i for i in listing["items"] if i["application_id"] == "app1")
        self.assertEqual("locked", item["decision"])

    def test_countersign_must_be_independent_auditor(self):
        self._applicant("a2x", "ap2", "o3", "rg-remote", "g1")
        self._full_app("f2", "app2", "ap2")
        self.svc.initiate_special(request_id="sp", actor_id="op1", special_id="s1",
                                  round_id="r1", application_id="app2", amount=90000,
                                  reason="偏远地区唯一站点")
        with self.assertRaises(PermissionDenied):
            self.svc.countersign_special(request_id="sign-self", actor_id="op1",
                                         special_id="s1", approved=True)
        # 评审人不能代替独立会签。
        with self.assertRaises(PermissionDenied):
            self.svc.countersign_special(request_id="sign-rv", actor_id="rv1",
                                         special_id="s1", approved=True)

    def test_special_over_remaining_budget_rejected(self):
        self._applicant("a2x", "ap2", "o3", "rg-remote", "g1")
        self._full_app("f2", "app2", "ap2")
        self.svc.initiate_special(request_id="sp", actor_id="op1", special_id="s1",
                                  round_id="r1", application_id="app2", amount=90000,
                                  reason="理由")
        # 缩减预算到 80000（当前无资金占用，允许缩减），特批 90000 超剩余预算，应被拒绝。
        self.svc.reduce_budget(request_id="cut", actor_id="a1", round_id="r1",
                               category="connectivity", new_amount=80000)
        with self.assertRaises(ConflictError):
            self.svc.countersign_special(request_id="sign2", actor_id="au1",
                                         special_id="s1", approved=True)

    def test_budget_cannot_cut_below_paid(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._full_app("f1", "app1", "ap1")
        self.svc.submit_review(request_id="rv", actor_id="rv1", application_id="app1", score=90)
        self.svc.run_ranking(request_id="run", actor_id="a1", round_id="r1")
        for key in ("id_copy", "budget_plan"):
            self.svc.submit_material(request_id=f"k-{key}", actor_id="op1",
                                     application_id="app1", material_key=key)
        self.svc.report_milestone(request_id="m1", actor_id="op1", application_id="app1", code="m1")
        self.svc.verify_milestone(request_id="v1", actor_id="rv1", application_id="app1",
                                  code="m1", passed=True)
        self.svc.commit_reservation(request_id="c", actor_id="op1", application_id="app1")
        with self.assertRaises(ConflictError):
            self.svc.reduce_budget(request_id="cut", actor_id="a1", round_id="r1",
                                   category="connectivity", new_amount=1000)

    def test_outcome_below_target_is_not_effective(self):
        self._applicant("a1x", "ap1", "o2", "rg-remote", "g1")
        self._full_app("f1", "app1", "ap1")
        self.svc.submit_review(request_id="rv", actor_id="rv1", application_id="app1", score=90)
        self.svc.run_ranking(request_id="run", actor_id="a1", round_id="r1")
        for key in ("id_copy", "budget_plan"):
            self.svc.submit_material(request_id=f"k-{key}", actor_id="op1",
                                     application_id="app1", material_key=key)
        self.svc.report_milestone(request_id="m1", actor_id="op1", application_id="app1", code="m1")
        self.svc.verify_milestone(request_id="v1", actor_id="rv1", application_id="app1",
                                  code="m1", passed=True)
        self.svc.commit_reservation(request_id="c", actor_id="op1", application_id="app1")
        self.svc.report_milestone(request_id="m2", actor_id="op1", application_id="app1", code="m2")
        self.svc.verify_milestone(request_id="v2", actor_id="rv1", application_id="app1",
                                  code="m2", passed=True)
        self.svc.report_outcome(request_id="o", actor_id="op1", application_id="app1",
                                metric_code="users", value=10)
        result = self.svc.verify_outcome(request_id="vo", actor_id="rv1", application_id="app1",
                                         metric_code="users", passed=True)
        self.assertFalse(result["effective"])
        self.assertEqual("committed", self.svc.get_application("app1")["status"])


class InclusivePolicyValidationTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        _, self.svc = _bootstrap(self.database, 0)

    def tearDown(self):
        self.database.close()

    def test_weights_must_sum_to_one(self):
        bad = {**POLICY, "weights": {"region_priority": 0.5, "digital_gap": 0.3,
                                     "beneficiary_need": 0.15, "review_score": 0.15}}
        with self.assertRaises(ValidationError):
            self.svc.freeze_policy(request_id="bad", actor_id="a1", version="bad", policy=bad)

    def test_installment_pcts_must_total_100(self):
        category = {**POLICY["categories"]["connectivity"],
                    "installments": [{"seq": 1, "pct": 50, "trigger": "__commit__"},
                                     {"seq": 2, "pct": 40, "trigger": "m2"}]}
        bad = {**POLICY, "categories": {"connectivity": category}}
        with self.assertRaises(ValidationError):
            self.svc.freeze_policy(request_id="bad2", actor_id="a1", version="bad2", policy=bad)


if __name__ == "__main__":
    unittest.main()
