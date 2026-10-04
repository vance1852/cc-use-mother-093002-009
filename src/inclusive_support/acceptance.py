"""运行普惠支持服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from digital_trade_foundation.clock import FixedClock
from digital_trade_foundation.errors import PermissionDenied
from digital_trade_foundation.storage import Database

from .service import SupportService


POLICY_CONFIG = {
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


def run() -> dict[str, object]:
    """执行一条覆盖去重、回避、排序、预留、分期、特批与退出的完整链路。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        service = SupportService(database, FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="普惠支持管理单位")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="项目管理者", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="op-001",
                               display_name="申报经办", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-rev1", actor_id="admin-001", new_actor_id="rev-001",
                               display_name="评估人员一", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-rev2", actor_id="admin-001", new_actor_id="rev-002",
                               display_name="评估人员二", role="reviewer", organization_id="org-001")
        service.register_actor(request_id="req-auditor", actor_id="admin-001", new_actor_id="aud-001",
                               display_name="独立会签人", role="auditor", organization_id="org-001")

        service.create_policy(request_id="req-policy", actor_id="admin-001", policy_id="pol-2026",
                              name="2026 年度人工智能普惠支持", config=POLICY_CONFIG)
        service.freeze_policy(request_id="req-freeze", actor_id="admin-001", policy_id="pol-2026")

        service.register_provider(request_id="req-prov1", actor_id="op-001",
                                  provider_id="prov-1", name="大型服务商甲")
        service.register_provider(request_id="req-prov2", actor_id="op-001",
                                  provider_id="prov-2", name="服务商乙")
        service.declare_conflict(request_id="req-conflict", actor_id="rev-001",
                                 provider_id="prov-1", reason="持有服务商甲股份")

        service.register_applicant(request_id="req-apl-a", actor_id="op-001", applicant_id="apl-A",
                                   name="山区公共服务站A", region_id="r-remote",
                                   affiliations=[{"related_key": "owner-x", "relation": "beneficial_owner"}])
        service.register_applicant(request_id="req-apl-b", actor_id="op-001", applicant_id="apl-B",
                                   name="山区公共服务站B", region_id="r-remote",
                                   affiliations=[{"related_key": "owner-x", "relation": "beneficial_owner"}])
        service.register_applicant(request_id="req-apl-c", actor_id="op-001", applicant_id="apl-C",
                                   name="乡镇服务中心C", region_id="r-town",
                                   affiliations=[{"related_key": "owner-y", "relation": "beneficial_owner"}])

        service.submit_application(request_id="req-ap1", actor_id="op-001", application_id="ap-1",
                                   policy_id="pol-2026", applicant_id="apl-A", provider_id="prov-1",
                                   category="infrastructure",
                                   baseline={"connectivity": 10, "device_ratio": 20},
                                   requested_amount=300_000)
        service.submit_application(request_id="req-ap2", actor_id="op-001", application_id="ap-2",
                                   policy_id="pol-2026", applicant_id="apl-B", provider_id="prov-2",
                                   category="infrastructure",
                                   baseline={"connectivity": 20, "device_ratio": 30},
                                   requested_amount=300_000)
        service.submit_application(request_id="req-ap3", actor_id="op-001", application_id="ap-3",
                                   policy_id="pol-2026", applicant_id="apl-C", provider_id="prov-2",
                                   category="training",
                                   baseline={"connectivity": 50, "device_ratio": 60},
                                   requested_amount=250_000)

        conflict_blocked = False
        try:
            service.review_application(request_id="req-review-blocked", actor_id="rev-001",
                                       application_id="ap-1", decision="approve", note="存在利益关系")
        except PermissionDenied:
            conflict_blocked = True
        for index, application_id in enumerate(("ap-1", "ap-2", "ap-3")):
            service.review_application(request_id=f"req-review-{index}", actor_id="rev-002",
                                       application_id=application_id, decision="approve", note="材料齐全")

        _, ranking = service.run_ranking(request_id="req-rank", actor_id="admin-001", policy_id="pol-2026")

        service.submit_material(request_id="req-material", actor_id="op-001", application_id="ap-1",
                                material_key="budget_plan", payload_data={"total": 300_000})
        service.report_milestone(request_id="cb-survey", actor_id="op-001", application_id="ap-1",
                                 milestone_key="site_survey", kind="survey",
                                 evidence={"report": "survey-001"})
        service.verify_milestone(request_id="req-verify-survey", actor_id="rev-002",
                                 application_id="ap-1", milestone_key="site_survey",
                                 approve=True, note="勘察合格")
        service.commit_reservation(request_id="req-commit", actor_id="op-001",
                                   application_id="ap-1")

        service.report_milestone(request_id="cb-train", actor_id="op-001", application_id="ap-1",
                                 milestone_key="training_done", kind="training",
                                 evidence={"sessions": 4})
        _, callback_replay = service.report_milestone(request_id="cb-train", actor_id="op-001",
                                                      application_id="ap-1", milestone_key="training_done",
                                                      kind="training", evidence={"sessions": 4})
        service.verify_milestone(request_id="req-verify-train", actor_id="rev-002",
                                 application_id="ap-1", milestone_key="training_done",
                                 approve=True, note="培训达标")
        service.report_milestone(request_id="cb-deploy", actor_id="op-001", application_id="ap-1",
                                 milestone_key="deployment_done", kind="deployment",
                                 evidence={"nodes": 3})
        service.verify_milestone(request_id="req-verify-deploy", actor_id="rev-002",
                                 application_id="ap-1", milestone_key="deployment_done",
                                 approve=True, note="部署完成")
        service.report_outcome(request_id="req-outcome", actor_id="op-001", application_id="ap-1",
                               metric_key="users_connected", value=800)
        service.verify_outcome(request_id="req-verify-outcome", actor_id="rev-002",
                               application_id="ap-1", metric_key="users_connected", approve=True)

        _, special = service.propose_special_approval(
            request_id="req-special", actor_id="admin-001", approval_id="sa-1", policy_id="pol-2026",
            application_id="ap-2", amount=100_000,
            justification="偏远地区补充覆盖，关联组内另一申请已去重")
        _, cosigned = service.cosign_special_approval(request_id="req-cosign", actor_id="aud-001",
                                                      approval_id="sa-1", approve=True)

        _, withdrawn = service.withdraw_application(
            request_id="req-withdraw", actor_id="op-001", application_id="ap-3",
            exit_obligations=["return_devices", "submit_final_report"])

        ap1 = service.get_application("ap-1")
        ap2_explain = service.explain_application("ap-2")
        report = service.policy_report("pol-2026")
        comparison = service.compare_policies(["pol-2026"])
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "conflict_blocked": conflict_blocked,
            "ranking": {"allocated": ranking["allocated"], "deduplicated": ranking["deduplicated"]},
            "installments": ap1["installments"],
            "callback_replay_status": callback_replay["status"],
            "ap1_status": ap1["status"],
            "special_cosigned": cosigned["status"],
            "fairness_gap_delta": cosigned["fairness"]["share_gap_delta"],
            "withdrawn_released": withdrawn["released"],
            "ap2_decisions": [row["decision"] for row in ap2_explain["decisions"]],
            "budget_available": report["budget"]["available"],
            "regions": [region["region_id"] for region in report["regions"]],
            "compared_policies": len(comparison["policies"]),
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = (
        result["status"] == "ok"
        and result["conflict_blocked"]
        and result["ranking"]["allocated"] == ["ap-1", "ap-3"]
        and result["ranking"]["deduplicated"] == ["ap-2"]
        and result["ap1_status"] == "completed"
        and result["special_cosigned"] == "cosigned"
        and result["withdrawn_released"] == 250_000
        and result["budget_available"] == 600_000
        and result["audit_valid"]
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
