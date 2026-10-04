"""运行普惠支持资格、配额与成效跟踪服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import PermissionDenied
from .inclusive import InclusiveService
from .service import DomainService
from .storage import Database

START = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)

POLICY = {
    "weights": {"region_priority": 0.4, "digital_gap": 0.3,
                "beneficiary_need": 0.15, "review_score": 0.15},
    "tier_weights": {"remote": 1.0, "rural": 0.6, "urban": 0.2},
    "reserve_hours": 48,
    "categories": {
        "connectivity": {
            "quota": 2,
            "per_applicant": 100000,
            "required_materials": ["id_copy", "budget_plan"],
            "milestones": [
                {"code": "m1", "kind": "training", "title": "数字技能培训",
                 "prerequisite": True, "seq": 1},
                {"code": "m2", "kind": "deployment", "title": "站点部署验收",
                 "prerequisite": False, "seq": 2},
            ],
            "installments": [
                {"seq": 1, "pct": 30, "trigger": "__commit__"},
                {"seq": 2, "pct": 30, "trigger": "m1"},
                {"seq": 3, "pct": 40, "trigger": "m2"},
            ],
            "metrics": [{"code": "users", "target": 50}],
            "region_floor": {"remote": 1},
        }
    },
}


def _clock(hours: float = 0) -> FixedClock:
    return FixedClock(START + timedelta(hours=hours))


def _bootstrap(database: Database, hours: float = 0) -> tuple[DomainService, InclusiveService]:
    domain = DomainService(database, _clock(hours))
    inclusive = InclusiveService(database, _clock(hours))
    if database.connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]:
        # 数据库重开：基础目录已经持久化，只需重建服务对象（时钟按 hours 推进）。
        return domain, inclusive
    domain.register_organization(request_id="org-o1", actor_id="bootstrap",
                                 organization_id="o1", name="项目管理机构")
    domain.register_actor(request_id="admin-a1", actor_id="bootstrap", new_actor_id="a1",
                          display_name="管理员", role="admin", organization_id="o1")
    for org_id, name in (("o2", "偏远乡服务中心"), ("o3", "偏远乡二站"),
                         ("o4", "城区服务点"), ("o5", "乡镇服务站"),
                         ("o6", "云联服务商公司")):
        domain.register_organization(request_id=f"org-{org_id}", actor_id="a1",
                                     organization_id=org_id, name=name)
    domain.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                          display_name="操作员", role="operator", organization_id="o1")
    domain.register_actor(request_id="rv1", actor_id="a1", new_actor_id="rv1",
                          display_name="独立评审", role="reviewer", organization_id="o1")
    domain.register_actor(request_id="rv2", actor_id="a1", new_actor_id="rv2",
                          display_name="服务商机构评审", role="reviewer", organization_id="o6")
    domain.register_actor(request_id="au1", actor_id="a1", new_actor_id="au1",
                          display_name="审计员", role="auditor", organization_id="o1")
    return domain, inclusive


def run() -> dict[str, object]:
    """执行完整的资格、配额、候补、承诺、成效与特批链路并返回核对结果。"""

    checks: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "inclusive.sqlite3"
        database = Database(path)
        domain, svc = _bootstrap(database)

        svc.register_region(request_id="reg-remote", actor_id="a1", region_id="rg-remote",
                            name="偏远山区县", tier="remote", priority=0.95, population=8000)
        svc.register_region(request_id="reg-rural", actor_id="a1", region_id="rg-rural",
                            name="农业乡镇", tier="rural", priority=0.6, population=12000)
        svc.register_region(request_id="reg-urban", actor_id="a1", region_id="rg-urban",
                            name="中心城区", tier="urban", priority=0.1, population=30000)
        svc.register_provider(request_id="pv1", actor_id="a1", provider_id="pv1",
                              organization_id="o6", name="云联服务商")
        svc.freeze_policy(request_id="pol-v1", actor_id="a1", version="v1", policy=POLICY)
        svc.open_round(request_id="round-1", actor_id="a1", round_id="r1",
                       policy_version="v1", budget={"connectivity": 270000})

        # 申请主体：ap1 与 ap2 同属一个关联集团，ap2 应在关联去重中出局。
        svc.register_applicant(request_id="ap1", actor_id="op1", applicant_id="ap1",
                               organization_id="o2", region_id="rg-remote",
                               affiliation_group="group-alpha",
                               baseline={"digital_readiness": 20}, beneficiary_population=1000)
        svc.register_applicant(request_id="ap2", actor_id="op1", applicant_id="ap2",
                               organization_id="o3", region_id="rg-remote",
                               affiliation_group="group-alpha",
                               baseline={"digital_readiness": 30}, beneficiary_population=900)
        svc.register_applicant(request_id="ap3", actor_id="op1", applicant_id="ap3",
                               organization_id="o4", region_id="rg-urban",
                               affiliation_group="group-beta",
                               baseline={"digital_readiness": 80}, beneficiary_population=500)
        svc.register_applicant(request_id="ap4", actor_id="op1", applicant_id="ap4",
                               organization_id="o5", region_id="rg-rural",
                               affiliation_group="group-gamma",
                               baseline={"digital_readiness": 50}, beneficiary_population=300)
        svc.submit_application(request_id="app1", actor_id="op1", application_id="app1",
                               round_id="r1", applicant_id="ap1", category="connectivity",
                               provider_id="pv1", requested_amount=90000)
        dup = svc.submit_application(request_id="app2", actor_id="op1", application_id="app2",
                                     round_id="r1", applicant_id="ap2", category="connectivity",
                                     provider_id="pv1", requested_amount=90000)
        svc.submit_application(request_id="app3", actor_id="op1", application_id="app3",
                               round_id="r1", applicant_id="ap3", category="connectivity",
                               provider_id="pv1", requested_amount=80000)
        svc.submit_application(request_id="app4", actor_id="op1", application_id="app4",
                               round_id="r1", applicant_id="ap4", category="connectivity",
                               provider_id="pv1", requested_amount=70000)
        checks["dedup_screens_duplicate"] = dup["screening_result"] == "duplicate"

        # 评审回避：rv2 与服务商 pv1 同属机构 o6，必须被拦截。
        try:
            svc.submit_review(request_id="rev-blocked", actor_id="rv2", application_id="app1",
                              score=90)
            raise AssertionError("利益相关评审应当被回避")
        except PermissionDenied:
            checks["reviewer_conflict_blocked"] = True
        svc.submit_review(request_id="rev1-1", actor_id="rv1", application_id="app1", score=90)
        svc.submit_review(request_id="rev1-3", actor_id="rv1", application_id="app3", score=70)
        svc.submit_review(request_id="rev1-4", actor_id="rv1", application_id="app4", score=80)

        # 冻结政策版本下的可复核排序。
        ranking = svc.run_ranking(request_id="rr1", actor_id="a1", round_id="r1")
        run_id = ranking["resource_id"]
        ranking_list = svc.list_ranking(run_id)
        selected = [i["application_id"] for i in ranking_list["items"] if i["decision"] == "selected"]
        waitlisted = [i["application_id"] for i in ranking_list["items"]
                      if i["decision"] == "waitlisted"]
        checks["remote_floor_and_rank_select_app1_app4"] = selected == ["app1", "app4"]
        checks["app3_waitlisted"] = waitlisted == ["app3"]
        app1_view = svc.get_application("app1")
        checks["ranking_reason_codes_reviewable"] = \
            app1_view["policy_version"] == "v1" and bool(app1_view["reason_codes"]) \
            and bool(app1_view["snapshot_hash"])

        occupied_before = svc.fund_report("r1")["categories"]["connectivity"]["occupied"]
        svc.run_ranking(request_id="rr1", actor_id="a1", round_id="r1")  # 重放
        occupied_after = svc.fund_report("r1")["categories"]["connectivity"]["occupied"]
        checks["ranking_replay_does_not_double_occupy"] = occupied_before == occupied_after == 160000

        # 入选者完成必要材料和前置里程碑后，限时预留转正式承诺并生成分期。
        svc.submit_material(request_id="mat1-a", actor_id="op1", application_id="app1",
                            material_key="id_copy")
        svc.submit_material(request_id="mat1-b", actor_id="op1", application_id="app1",
                            material_key="budget_plan")
        svc.report_milestone(request_id="ms1-m1", actor_id="op1", application_id="app1",
                             code="m1", evidence={"trainees": 40})
        svc.verify_milestone(request_id="vf1-m1", actor_id="rv1", application_id="app1",
                             code="m1", passed=True)
        commit = svc.commit_reservation(request_id="commit1", actor_id="op1",
                                        application_id="app1")
        checks["commitment_created"] = commit["resource_type"] == "commitment"
        balance0 = svc.installment_balance("app1")
        checks["commit_pays_prerequisite_installments"] = balance0["paid_amount"] == 54000 \
            and balance0["remaining_amount"] == 36000

        # 服务商部署回调：重放同一 request_id 不能重复兑付分期。
        svc.report_milestone(request_id="ms1-m2", actor_id="op1", application_id="app1",
                             code="m2", evidence={"sites": 3})
        svc.report_milestone(request_id="ms1-m2", actor_id="op1", application_id="app1",
                             code="m2", evidence={"sites": 3})
        svc.verify_milestone(request_id="vf1-m2", actor_id="rv1", application_id="app1",
                             code="m2", passed=True)
        svc.report_outcome(request_id="out1", actor_id="op1", application_id="app1",
                           metric_code="users", value=60)
        outcome = svc.verify_outcome(request_id="vout1", actor_id="rv1", application_id="app1",
                                     metric_code="users", passed=True)
        checks["outcome_verified_receipt"] = outcome["resource_type"] == "outcome"
        checks["app1_completed"] = svc.get_application("app1")["status"] == "completed"
        balance1 = svc.installment_balance("app1")
        checks["installments_fully_paid"] = \
            balance1["paid_amount"] == 90000 and balance1["remaining_amount"] == 0

        # 已完成且核验通过的支持不参与重排。
        svc.run_ranking(request_id="rr2", actor_id="a1", round_id="r1")
        run2 = svc.list_ranking(_latest_run(database, "r1"))
        locked = [i for i in run2["items"] if i["application_id"] == "app1"][0]
        checks["completed_support_locked_from_rerank"] = locked["decision"] == "locked"

        # 时钟推进超过预留窗口：app4 到期释放，稳定候补 app3 晋升。
        database.close()
        database = Database(path)
        _, svc = _bootstrap(database, hours=49)
        sweep = svc.sweep_expirations(request_id="sweep1", actor_id="op1")
        checks["expired_released_and_waitlist_promoted"] = \
            sweep["expired"] == ["app4"] and sweep["promoted"] == ["app3"]
        checks["app3_reservation_deadline_kept"] = \
            svc.get_application("app3")["status"] == "reserved"
        waitlist = svc.list_waitlist("r1")
        checks["waitlist_position_stable"] = \
            waitlist == [] or all(i["status"] != "eligible" for i in waitlist)

        # 去重出局者通过特批通道：发起人之外的审计员独立会签，记录公平性变化。
        svc.initiate_special(request_id="sp1", actor_id="op1", special_id="special1",
                             round_id="r1", application_id="app2", amount=90000,
                             reason="偏远民族乡唯一公共服务点")
        svc.countersign_special(request_id="spsign1", actor_id="au1",
                                special_id="special1", approved=True)
        signed = svc.get_special("special1")
        checks["special_records_fairness_impact"] = \
            signed["status"] == "approved" and bool(signed["fairness_after"])
        checks["special_reserves_funds"] = \
            svc.get_application("app2")["status"] == "reserved"

        # 恢复运行后核对：分期余额、预留时钟和审计链仍准确。
        database.close()
        database = Database(path)
        _, svc_later = _bootstrap(database, hours=50)
        app3 = svc_later.get_application("app3")
        balance_restart = svc_later.installment_balance("app1")
        checks["restart_preserves_clock_and_balance"] = \
            bool(app3["expires_at"]) and balance_restart["paid_amount"] == 90000
        valid, event_count = svc_later.verify_audit()
        checks["audit_chain_valid"] = valid

        # 预算缩减：保护已兑付资金，先收回最晚的预留，释放名额沿候补继续。
        reduced = svc_later.reduce_budget(request_id="cut1", actor_id="a1", round_id="r1",
                                          category="connectivity", new_amount=120000)
        checks["budget_cut_releases_unfulfilled_only"] = \
            sorted(reduced["released_reservations"]) == ["app2", "app3"] \
            and svc_later.installment_balance("app1")["paid_amount"] == 90000

        # 管理者按版本比较资金覆盖、地区分布与有效成果。
        funds = svc_later.fund_report("r1")
        regions = svc_later.region_report("r1")
        outcomes = svc_later.outcome_report("r1")
        comparison = svc_later.compare_policies()
        checks["reports_cover_funds_regions_outcomes"] = (
            funds["categories"]["connectivity"]["paid"] == 90000
            and "rg-remote" in regions["regions"]
            and outcomes["completed"] == 1
            and outcomes["metrics"]["users"]["effective"] == 1
            and "v1" in comparison["versions"]
        )
        exits = svc_later.list_exits("r1")
        checks["exit_responsibility_recorded"] = len(exits) >= 2
        valid_after, _ = svc_later.verify_audit()
        checks["audit_chain_valid_after_all"] = valid_after
        database.close()

    return {"status": "ok" if all(checks.values()) else "failed",
            "checks": checks}


def _latest_run(database: Database, round_id: str) -> str:
    row = database.connection.execute(
        "SELECT run_id FROM incl_ranking_runs WHERE round_id=? ORDER BY rowid DESC LIMIT 1",
        (round_id,)).fetchone()
    return row["run_id"]


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
