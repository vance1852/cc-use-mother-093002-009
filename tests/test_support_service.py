import unittest
from datetime import datetime, timedelta, timezone

from digital_trade_foundation.errors import ConflictError, PermissionDenied, ValidationError
from inclusive_support.service import SupportService
from digital_trade_foundation.storage import Database


class ManualClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, **kwargs):
        self.value = self.value + timedelta(**kwargs)


CONFIG = {
    "total_budget": 1_000_000,
    "reservation_ttl_hours": 72,
    "min_reviews": 1,
    "baseline_metrics": ["connectivity", "device_ratio"],
    "baseline_weight": 10,
    "region_priorities": {"r-remote": 1, "r-town": 2},
    "region_tier_scores": {"1": 1000, "2": 500},
    "category_weights": {"infrastructure": 300, "training": 100},
    "max_amount_per_application": 400_000,
    "required_materials": ["budget_plan"],
    "prerequisite_milestones": [{"key": "site_survey", "kind": "survey"}],
    "installment_plan": [
        {"milestone_key": "training_done", "kind": "training", "percent": 40},
        {"milestone_key": "deployment_done", "kind": "deployment", "percent": 60},
    ],
    "required_outcome_metrics": ["users_connected"],
}


class SupportServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = ManualClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = SupportService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="管理单位")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin1",
                                    display_name="管理者", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="admin1", new_actor_id="op1",
                                    display_name="经办", role="operator", organization_id="o1")
        self.service.register_actor(request_id="rev1", actor_id="admin1", new_actor_id="rev1",
                                    display_name="评估一", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="rev2", actor_id="admin1", new_actor_id="rev2",
                                    display_name="评估二", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="aud", actor_id="admin1", new_actor_id="aud1",
                                    display_name="会签", role="auditor", organization_id="o1")
        self._requests = 0

    def tearDown(self):
        self.database.close()

    def _req(self, label):
        self._requests += 1
        return f"{label}-{self._requests}"

    def _policy(self, config=None, policy_id="pol-1", freeze=True):
        _, created = self.service.create_policy(request_id=self._req("policy"), actor_id="admin1",
                                                policy_id=policy_id, name="普惠支持",
                                                config=config or CONFIG)
        if freeze:
            self.service.freeze_policy(request_id=self._req("freeze"), actor_id="admin1",
                                       policy_id=policy_id)
        return created

    def _applicant(self, applicant_id, region_id="r-remote", key="owner-a"):
        self.service.register_applicant(request_id=self._req("apl"), actor_id="op1",
                                        applicant_id=applicant_id, name=f"主体{applicant_id}",
                                        region_id=region_id,
                                        affiliations=[{"related_key": key, "relation": "beneficial_owner"}])

    def _provider(self, provider_id="prov-1"):
        self.service.register_provider(request_id=self._req("prov"), actor_id="op1",
                                       provider_id=provider_id, name=f"服务商{provider_id}")

    def _application(self, application_id, applicant_id, provider_id="prov-1",
                     category="infrastructure", baseline=None, amount=300_000, policy_id="pol-1"):
        self.service.submit_application(
            request_id=self._req("app"), actor_id="op1", application_id=application_id,
            policy_id=policy_id, applicant_id=applicant_id, provider_id=provider_id,
            category=category, baseline=baseline or {"connectivity": 10, "device_ratio": 20},
            requested_amount=amount)

    def _review(self, application_id, reviewer="rev1"):
        self.service.review_application(request_id=self._req("review"), actor_id=reviewer,
                                        application_id=application_id, decision="approve", note="通过")

    def _commit(self, application_id):
        self.service.submit_material(request_id=self._req("mat"), actor_id="op1",
                                     application_id=application_id, material_key="budget_plan",
                                     payload_data={"total": 1})
        self.service.report_milestone(request_id=self._req("ms"), actor_id="op1",
                                      application_id=application_id, milestone_key="site_survey",
                                      kind="survey", evidence={"ok": True})
        self.service.verify_milestone(request_id=self._req("vms"), actor_id="rev1",
                                      application_id=application_id, milestone_key="site_survey",
                                      approve=True, note="合格")
        return self.service.commit_reservation(request_id=self._req("commit"), actor_id="op1",
                                               application_id=application_id)

    def _standard_ranking(self):
        self._policy()
        self._provider("prov-1")
        self._provider("prov-2")
        self._applicant("apl-A", key="owner-x")
        self._applicant("apl-B", key="owner-x")
        self._applicant("apl-C", region_id="r-town", key="owner-y")
        self._application("ap-1", "apl-A", baseline={"connectivity": 10, "device_ratio": 20})
        self._application("ap-2", "apl-B", provider_id="prov-2",
                          baseline={"connectivity": 20, "device_ratio": 30})
        self._application("ap-3", "apl-C", provider_id="prov-2", category="training",
                          baseline={"connectivity": 50, "device_ratio": 60}, amount=250_000)
        for application_id in ("ap-1", "ap-2", "ap-3"):
            self._review(application_id)
        return self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1",
                                        policy_id="pol-1")

    # ---------- 关联去重与排序 ----------

    def test_affiliation_dedup_and_reviewable_ranking(self):
        _, ranking = self._standard_ranking()
        self.assertEqual(["ap-1", "ap-3"], ranking["allocated"])
        self.assertEqual(["ap-2"], ranking["deduplicated"])
        rows = {row["application_id"]: row for row in self.service.policy_ranking("pol-1")}
        self.assertEqual(2150, rows["ap-1"]["score"])
        self.assertEqual(2050, rows["ap-2"]["score"])
        self.assertEqual(1050, rows["ap-3"]["score"])
        self.assertEqual(1, rows["ap-1"]["reasons"]["region_tier"])
        explain = self.service.explain_application("ap-2")
        decisions = [row["decision"] for row in explain["decisions"]]
        self.assertIn("affiliation_deduplicated", decisions)
        dedup = [row for row in explain["decisions"] if row["decision"] == "affiliation_deduplicated"][0]
        self.assertEqual("ap-1", dedup["reason"]["winner_application_id"])

    def test_ranking_is_deterministic_across_reruns(self):
        config = dict(CONFIG, total_budget=250_000, max_amount_per_application=250_000)
        self._policy(config=config)
        self._provider()
        self._applicant("apl-A", key="owner-x")
        self._applicant("apl-B", key="owner-y")
        self._applicant("apl-C", key="owner-z")
        self._application("ap-1", "apl-A", amount=200_000)
        self._application("ap-2", "apl-B", amount=200_000,
                          baseline={"connectivity": 30, "device_ratio": 30})
        self._application("ap-3", "apl-C", amount=200_000,
                          baseline={"connectivity": 40, "device_ratio": 40})
        for application_id in ("ap-1", "ap-2", "ap-3"):
            self._review(application_id)
        _, first_run = self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1",
                                                policy_id="pol-1")
        self.assertEqual(["ap-1"], first_run["allocated"])
        self.assertEqual(["ap-2", "ap-3"], first_run["waitlisted"])
        first = {row["application_id"]: row["score"] for row in self.service.policy_ranking("pol-1")}
        _, second_run = self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1",
                                                 policy_id="pol-1")
        second = {row["application_id"]: row["score"] for row in self.service.policy_ranking("pol-1")}
        # 冻结政策版本下重跑排序：得分与候补相对顺序保持一致
        self.assertEqual(first, second)
        self.assertEqual(["ap-2", "ap-3"], second_run["waitlisted"])
        self.assertEqual("waitlisted", self.service.get_application("ap-2")["status"])

    def test_ranking_requires_frozen_policy(self):
        self._policy(freeze=False)
        with self.assertRaises(ConflictError):
            self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1", policy_id="pol-1")

    def test_duplicate_application_rejected(self):
        self._policy()
        self._provider()
        self._applicant("apl-A")
        self._application("ap-1", "apl-A")
        with self.assertRaises(ConflictError):
            self._application("ap-2", "apl-A")

    # ---------- 评审回避 ----------

    def test_reviewer_conflict_recusal(self):
        self._policy()
        self._provider("prov-1")
        self._applicant("apl-A")
        self._application("ap-1", "apl-A")
        self.service.declare_conflict(request_id=self._req("conflict"), actor_id="rev1",
                                      provider_id="prov-1", reason="持股")
        with self.assertRaises(PermissionDenied):
            self._review("ap-1", reviewer="rev1")
        self._review("ap-1", reviewer="rev2")
        self.assertEqual("accepted", self.service.get_application("ap-1")["status"])

    def test_min_reviews_requires_multiple_approvals(self):
        config = dict(CONFIG, min_reviews=2)
        self._policy(config=config)
        self._provider()
        self._applicant("apl-A")
        self._application("ap-1", "apl-A")
        self._review("ap-1", reviewer="rev1")
        self.assertEqual("submitted", self.service.get_application("ap-1")["status"])
        with self.assertRaises(ConflictError):
            self._review("ap-1", reviewer="rev1")
        self._review("ap-1", reviewer="rev2")
        self.assertEqual("accepted", self.service.get_application("ap-1")["status"])

    # ---------- 预留、承诺与分期 ----------

    def test_commit_requires_materials_and_prerequisite(self):
        self._standard_ranking()
        with self.assertRaises(ConflictError):
            self.service.commit_reservation(request_id=self._req("commit"), actor_id="op1",
                                            application_id="ap-1")
        self.service.submit_material(request_id=self._req("mat"), actor_id="op1",
                                     application_id="ap-1", material_key="budget_plan",
                                     payload_data={"total": 1})
        with self.assertRaises(ConflictError):
            self.service.commit_reservation(request_id=self._req("commit"), actor_id="op1",
                                            application_id="ap-1")
        self.service.report_milestone(request_id=self._req("ms"), actor_id="op1",
                                      application_id="ap-1", milestone_key="site_survey",
                                      kind="survey", evidence={"ok": True})
        self.service.verify_milestone(request_id=self._req("vms"), actor_id="rev1",
                                      application_id="ap-1", milestone_key="site_survey",
                                      approve=True, note="合格")
        _, result = self.service.commit_reservation(request_id=self._req("commit"), actor_id="op1",
                                                    application_id="ap-1")
        self.assertEqual([120_000, 180_000], [item["amount"] for item in result["installments"]])
        self.assertEqual("committed", self.service.get_application("ap-1")["status"])

    def test_reservation_expiry_promotes_waitlist(self):
        config = dict(CONFIG, total_budget=350_000, max_amount_per_application=350_000)
        self._policy(config=config)
        self._provider()
        self._applicant("apl-A", key="owner-x")
        self._applicant("apl-B", region_id="r-town", key="owner-y")
        self._application("ap-1", "apl-A", amount=300_000)
        self._application("ap-2", "apl-B", amount=250_000, category="training",
                          baseline={"connectivity": 50, "device_ratio": 60})
        self._review("ap-1")
        self._review("ap-2")
        _, ranking = self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1",
                                              policy_id="pol-1")
        self.assertEqual(["ap-1"], ranking["allocated"])
        self.assertEqual(["ap-2"], ranking["waitlisted"])
        self.clock.advance(hours=73)
        policy = self.service.get_policy("pol-1")
        self.assertEqual("expired", self.service.get_application("ap-1")["status"])
        promoted = self.service.get_application("ap-2")
        self.assertEqual("reserved", promoted["status"])
        self.assertEqual("promotion", promoted["reservation"]["source"])
        self.assertEqual(100_000, policy["budget"]["available"])

    def test_callback_replay_does_not_double_release(self):
        self._standard_ranking()
        self._commit("ap-1")
        self.service.report_milestone(request_id="cb-1", actor_id="op1", application_id="ap-1",
                                      milestone_key="training_done", kind="training",
                                      evidence={"sessions": 4})
        receipt, _ = self.service.report_milestone(request_id="cb-1", actor_id="op1",
                                                   application_id="ap-1", milestone_key="training_done",
                                                   kind="training", evidence={"sessions": 4})
        self.assertTrue(receipt.replayed)
        self.service.verify_milestone(request_id=self._req("v"), actor_id="rev1",
                                      application_id="ap-1", milestone_key="training_done",
                                      approve=True, note="达标")
        rows = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM support_budget_ledger WHERE kind='installment_released'"
        ).fetchone()
        self.assertEqual(1, rows["count"])
        detail = self.service.get_application("ap-1")
        self.assertEqual(["released", "pending"],
                         [item["status"] for item in detail["installments"]])
        with self.assertRaises(ConflictError):
            self.service.report_milestone(request_id=self._req("cb"), actor_id="op1",
                                          application_id="ap-1", milestone_key="training_done",
                                          kind="training", evidence={"sessions": 5})

    def test_application_replay_does_not_duplicate(self):
        self._policy()
        self._provider()
        self._applicant("apl-A")
        payload = dict(actor_id="op1", application_id="ap-1", policy_id="pol-1",
                       applicant_id="apl-A", provider_id="prov-1", category="infrastructure",
                       baseline={"connectivity": 10, "device_ratio": 20}, requested_amount=300_000)
        first, _ = self.service.submit_application(request_id="req-app", **payload)
        second, _ = self.service.submit_application(request_id="req-app", **payload)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        count = self.database.connection.execute("SELECT COUNT(*) AS count FROM support_applications").fetchone()
        self.assertEqual(1, count["count"])

    def test_full_lifecycle_completes(self):
        self._standard_ranking()
        self._commit("ap-1")
        for milestone_key, kind in (("training_done", "training"), ("deployment_done", "deployment")):
            self.service.report_milestone(request_id=self._req("ms"), actor_id="op1",
                                          application_id="ap-1", milestone_key=milestone_key,
                                          kind=kind, evidence={"ok": True})
            self.service.verify_milestone(request_id=self._req("v"), actor_id="rev1",
                                          application_id="ap-1", milestone_key=milestone_key,
                                          approve=True, note="达标")
        self.service.report_outcome(request_id=self._req("out"), actor_id="op1",
                                    application_id="ap-1", metric_key="users_connected", value=800)
        self.service.verify_outcome(request_id=self._req("vo"), actor_id="rev1",
                                    application_id="ap-1", metric_key="users_connected", approve=True)
        detail = self.service.get_application("ap-1")
        self.assertEqual("completed", detail["status"])
        self.assertEqual("completed", detail["commitment"]["status"])
        # 已完成且核验通过的支持不参与重排
        _, ranking = self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1",
                                              policy_id="pol-1")
        self.assertEqual(0, ranking["ranked"])
        self.assertEqual("completed", self.service.get_application("ap-1")["status"])

    # ---------- 退出与释放 ----------

    def test_withdraw_releases_and_promotes(self):
        config = dict(CONFIG, total_budget=350_000, max_amount_per_application=350_000)
        self._policy(config=config)
        self._provider()
        self._applicant("apl-A", key="owner-x")
        self._applicant("apl-B", region_id="r-town", key="owner-y")
        self._application("ap-1", "apl-A", amount=300_000)
        self._application("ap-2", "apl-B", amount=250_000, category="training",
                          baseline={"connectivity": 50, "device_ratio": 60})
        self._review("ap-1")
        self._review("ap-2")
        self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1", policy_id="pol-1")
        _, result = self.service.withdraw_application(
            request_id=self._req("wd"), actor_id="op1", application_id="ap-1",
            exit_obligations=["return_devices"])
        self.assertEqual(300_000, result["released"])
        self.assertEqual(["ap-2"], result["promoted"])
        detail = self.service.get_application("ap-1")
        self.assertEqual("withdrawn", detail["status"])
        self.assertEqual(["return_devices"], detail["exit_obligations"])
        self.assertEqual("reserved", self.service.get_application("ap-2")["status"])

    def test_partial_failure_releases_only_unfulfilled(self):
        self._standard_ranking()
        self._commit("ap-1")
        self.service.report_milestone(request_id=self._req("ms"), actor_id="op1",
                                      application_id="ap-1", milestone_key="training_done",
                                      kind="training", evidence={"ok": True})
        self.service.verify_milestone(request_id=self._req("v"), actor_id="rev1",
                                      application_id="ap-1", milestone_key="training_done",
                                      approve=True, note="达标")
        _, result = self.service.exit_application(
            request_id=self._req("exit"), actor_id="admin1", application_id="ap-1",
            reason="partial_failure", exit_obligations=["submit_final_report"])
        self.assertEqual(180_000, result["released"])
        detail = self.service.get_application("ap-1")
        self.assertEqual("exited", detail["status"])
        self.assertEqual("exited", detail["commitment"]["status"])
        self.assertEqual(180_000, detail["commitment"]["released_back"])
        self.assertEqual(["released", "cancelled"],
                         [item["status"] for item in detail["installments"]])
        policy = self.service.get_policy("pol-1")
        self.assertEqual(1_000_000 - 250_000 - 120_000, policy["budget"]["available"])

    def test_budget_cut_releases_lowest_priority_first(self):
        config = dict(CONFIG, total_budget=500_000)
        self._policy(config=config)
        self._provider()
        self._applicant("apl-A", key="owner-x")
        self._applicant("apl-B", region_id="r-town", key="owner-y")
        self._application("ap-1", "apl-A", amount=200_000)
        self._application("ap-2", "apl-B", amount=200_000, category="training",
                          baseline={"connectivity": 50, "device_ratio": 60})
        self._review("ap-1")
        self._review("ap-2")
        self.service.run_ranking(request_id=self._req("rank"), actor_id="admin1", policy_id="pol-1")
        _, result = self.service.cut_budget(request_id=self._req("cut"), actor_id="admin1",
                                            policy_id="pol-1", new_total_budget=250_000)
        self.assertEqual([{"application_id": "ap-2", "released": 200_000, "kind": "reservation"}],
                         result["released"])
        self.assertEqual("reserved", self.service.get_application("ap-1")["status"])
        self.assertEqual("exited", self.service.get_application("ap-2")["status"])
        policy = self.service.get_policy("pol-1")
        self.assertEqual(250_000, policy["budget"]["total_budget"])
        self.assertEqual(50_000, policy["budget"]["available"])

    def test_budget_cut_beyond_unfulfilled_rejected(self):
        self._standard_ranking()
        self._commit("ap-1")
        self.service.report_milestone(request_id=self._req("ms"), actor_id="op1",
                                      application_id="ap-1", milestone_key="training_done",
                                      kind="training", evidence={"ok": True})
        self.service.verify_milestone(request_id=self._req("v"), actor_id="rev1",
                                      application_id="ap-1", milestone_key="training_done",
                                      approve=True, note="达标")
        # 已拨付的 12 万不再属于未兑现部分，预算不能缩减到其之下
        with self.assertRaises(ValidationError):
            self.service.cut_budget(request_id=self._req("cut"), actor_id="admin1",
                                    policy_id="pol-1", new_total_budget=100_000)

    # ---------- 特批会签 ----------

    def test_special_approval_requires_independent_cosign(self):
        self._standard_ranking()
        _, special = self.service.propose_special_approval(
            request_id=self._req("sa"), actor_id="admin1", approval_id="sa-1", policy_id="pol-1",
            application_id="ap-2", amount=100_000, justification="偏远补充覆盖")
        self.assertIn("max_share_gap", special["fairness"]["after"])
        self.assertIn("share_gap_delta", special["fairness"])
        with self.assertRaises(PermissionDenied):
            self.service.cosign_special_approval(request_id=self._req("cs"), actor_id="admin1",
                                                 approval_id="sa-1", approve=True)
        _, result = self.service.cosign_special_approval(request_id=self._req("cs"), actor_id="aud1",
                                                         approval_id="sa-1", approve=True)
        self.assertEqual("cosigned", result["status"])
        detail = self.service.get_application("ap-2")
        self.assertEqual("reserved", detail["status"])
        self.assertEqual("special", detail["reservation"]["source"])
        decisions = [row["decision"] for row in detail["decisions"]]
        self.assertIn("allocated_special", decisions)

    def test_special_approval_over_budget_rejected(self):
        self._standard_ranking()
        with self.assertRaises(ValidationError):
            self.service.propose_special_approval(
                request_id=self._req("sa"), actor_id="admin1", approval_id="sa-1", policy_id="pol-1",
                application_id="ap-2", amount=900_000, justification="超额")

    # ---------- 角色分离 ----------

    def test_role_separation(self):
        self._policy()
        self._provider()
        self._applicant("apl-A")
        with self.assertRaises(PermissionDenied):
            self.service.submit_application(request_id=self._req("x"), actor_id="rev1",
                                            application_id="ap-x", policy_id="pol-1",
                                            applicant_id="apl-A", provider_id="prov-1",
                                            category="infrastructure",
                                            baseline={"connectivity": 1, "device_ratio": 1},
                                            requested_amount=1000)
        with self.assertRaises(PermissionDenied):
            self.service.run_ranking(request_id=self._req("x"), actor_id="op1", policy_id="pol-1")
        with self.assertRaises(PermissionDenied):
            self.service.create_policy(request_id=self._req("x"), actor_id="op1",
                                       policy_id="pol-x", name="越权", config=CONFIG)
        with self.assertRaises(PermissionDenied):
            self.service.freeze_policy(request_id=self._req("x"), actor_id="aud1", policy_id="pol-1")
        self._application("ap-1", "apl-A")
        with self.assertRaises(PermissionDenied):
            self.service.review_application(request_id=self._req("x"), actor_id="op1",
                                            application_id="ap-1", decision="approve", note="越权")
        with self.assertRaises(PermissionDenied):
            self.service.verify_outcome(request_id=self._req("x"), actor_id="op1",
                                        application_id="ap-1", metric_key="users_connected",
                                        approve=True)

    # ---------- 报告与比较 ----------

    def test_report_and_compare(self):
        self._standard_ranking()
        self._commit("ap-1")
        report = self.service.policy_report("pol-1")
        self.assertEqual(1_000_000, report["budget"]["total_budget"])
        self.assertEqual(300_000, report["budget"]["commitment_outstanding"])
        self.assertEqual(250_000, report["budget"]["reserved_active"])
        self.assertEqual(450_000, report["budget"]["available"])
        regions = {row["region_id"]: row for row in report["regions"]}
        self.assertEqual(2, regions["r-remote"]["applications"])
        self.assertEqual(1, regions["r-town"]["funded"])
        self.assertEqual(1, report["decisions"]["affiliation_deduplicated"])
        comparison = self.service.compare_policies(["pol-1"])
        self.assertEqual(1, len(comparison["policies"]))
        self.assertEqual(report["coverage"], comparison["policies"][0]["coverage"])


if __name__ == "__main__":
    unittest.main()
