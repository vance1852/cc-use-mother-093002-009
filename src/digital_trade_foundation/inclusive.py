"""普惠支持资格、配额与成效跟踪领域服务。

在基础服务的事务、幂等回执和哈希审计之上，实现：
冻结政策版本、关联去重与评审回避、可复核排序、限时预留转正式承诺、
分期额度、培训与部署里程碑、效果指标、稳定候补、预算缩减释放、
退出责任、特批独立会签与公平性快照、资金与地区分布报告。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Callable

from .audit import append_event, canonical_json, digest, verify_chain
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor
from .storage import Database

# 申请的完整状态机：
# eligible -> reserved -> committed -> completed
#                |            |-> partial_failed / budget_cut / waived
#                |-> expired / waived / budget_cut
# eligible -> duplicate（关联去重出局，可走特批）
# eligible -> reserved(waitlist 晋升) / waived
ACTIVE_OCCUPYING = ("reserved", "committed", "completed", "partial_failed", "budget_cut")
SCORABLE = ("eligible",)
COMMIT_TRIGGER = "__commit__"


class InclusiveService:
    """协调普惠支持的资格、配额、承诺分期与成效规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    def verify_audit(self) -> tuple[bool, int]:
        """校验与基础服务共享的哈希审计链。"""

        return verify_chain(self.database.connection)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not value or len(value) > 64:
            raise ValidationError(f"{field} 格式无效")
        return value

    def _amount(self, value: Any, field: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} 必须是正整数金额（最小货币单位）")
        return value

    def _actor(self, conn, actor_id: str) -> Actor:
        row = conn.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    **json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, **response}

    def _replay(self, conn, *, request_id: str, action: str,
                payload: dict[str, Any]) -> dict[str, Any] | None:
        """在任何业务校验或写入之前处理重放，保证回调重放不重复占用额度。"""

        request_id = self._id(request_id, "request_id")
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"request_id": request_id, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "replayed": True}

    def _load(self, conn, query: str, params: tuple = ()):
        row = conn.execute(query, params).fetchone()
        if row is None:
            raise NotFoundError("业务对象不存在")
        return row

    def _app(self, conn, application_id: str):
        return self._load(conn, "SELECT * FROM incl_applications WHERE application_id=?", (application_id,))

    def _round_policy(self, conn, round_id: str):
        row = self._load(conn, "SELECT * FROM incl_rounds WHERE round_id=?", (round_id,))
        policy = conn.execute("SELECT * FROM incl_policies WHERE version=?", (row["policy_version"],)).fetchone()
        return row, json.loads(policy["payload_json"]), policy["payload_hash"]

    def _policy_for_app(self, conn, app) -> dict[str, Any]:
        policy_row = conn.execute(
            "SELECT p.payload_json FROM incl_policies p JOIN incl_rounds r ON r.policy_version=p.version "
            "WHERE r.round_id=?", (app["round_id"],)).fetchone()
        return json.loads(policy_row["payload_json"])

    def _ledger_balance(self, conn, round_id: str, category: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS total FROM incl_fund_ledger WHERE round_id=? AND category=?",
            (round_id, category)).fetchone()
        return int(row["total"])

    def _ledger_add(self, conn, *, round_id: str, category: str, application_id: str,
                    amount: int, reason: str, request_id: str | None) -> None:
        conn.execute(
            "INSERT INTO incl_fund_ledger(ledger_id,round_id,category,application_id,amount,reason,"
            "request_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, round_id, category, application_id, amount, reason,
             request_id, self._now()),
        )

    def _reviewer_conflict(self, conn, actor: Actor, provider_id: str) -> dict[str, Any] | None:
        """评审/核验回避：显式利益关系或与服务商同属一个机构。"""
        provider = conn.execute("SELECT * FROM incl_providers WHERE provider_id=?", (provider_id,)).fetchone()
        if provider is not None and provider["organization_id"] == actor.organization_id:
            return {"type": "same_organization", "provider_id": provider_id}
        link = conn.execute(
            "SELECT 1 FROM incl_reviewer_links WHERE reviewer_id=? AND provider_id=?",
            (actor.actor_id, provider_id)).fetchone()
        if link:
            return {"type": "declared_interest", "provider_id": provider_id}
        return None

    # ------------------------------------------------------------------#
    # 基础目录：地区、服务商、利益关系
    # ------------------------------------------------------------------
    def register_region(self, *, request_id: str, actor_id: str, region_id: str, name: str,
                        tier: str, priority: float, population: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "region_id": region_id, "name": name, "tier": tier,
                   "priority": priority, "population": population}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            region_id = self._id(region_id, "region_id")
            name = str(name).strip()
            tier = str(tier).strip()
            if not name or not tier:
                raise ValidationError("地区名称与层级不能为空")
            if not isinstance(priority, (int, float)) or not 0 <= float(priority) <= 1:
                raise ValidationError("priority 必须在 0 到 1 之间")
            if not isinstance(population, int) or population < 0:
                raise ValidationError("population 必须是非负整数")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO incl_regions(region_id,name,tier,priority,population,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (region_id, name, tier, float(priority), population, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("地区编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="inclusive.region.registered",
                             resource_type="region", resource_id=region_id,
                             detail={"name": name, "tier": tier, "priority": float(priority)},
                             occurred_at=self._now())
                return "region", region_id, {"region_id": region_id}

            return self._idempotent(conn, request_id=request_id, action="register_region",
                                    payload=payload, create=create)

    def register_provider(self, *, request_id: str, actor_id: str, provider_id: str,
                          organization_id: str, name: str, active: bool = True) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "provider_id": provider_id,
                   "organization_id": organization_id, "name": name, "active": active}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            provider_id = self._id(provider_id, "provider_id")
            name = str(name).strip()
            if not name:
                raise ValidationError("服务商名称不能为空")
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (organization_id,)).fetchone() is None:
                raise NotFoundError("服务商挂靠机构不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO incl_providers(provider_id,organization_id,name,active,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (provider_id, organization_id, name, 1 if active else 0, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("服务商编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="inclusive.provider.registered",
                             resource_type="provider", resource_id=provider_id,
                             detail={"organization_id": organization_id, "name": name},
                             occurred_at=self._now())
                return "provider", provider_id, {"provider_id": provider_id}

            return self._idempotent(conn, request_id=request_id, action="register_provider",
                                    payload=payload, create=create)

    def declare_reviewer_interest(self, *, request_id: str, actor_id: str,
                                  reviewer_id: str, provider_id: str) -> dict[str, Any]:
        """登记评审人与服务商之间的利益关系，用于评分和核验回避。"""
        payload = {"actor_id": actor_id, "reviewer_id": reviewer_id, "provider_id": provider_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            self._load(conn, "SELECT 1 FROM actors WHERE actor_id=?", (reviewer_id,))
            self._load(conn, "SELECT 1 FROM incl_providers WHERE provider_id=?", (provider_id,))

            def create():
                conn.execute(
                    "INSERT OR IGNORE INTO incl_reviewer_links(reviewer_id,provider_id,created_at) "
                    "VALUES(?,?,?)", (reviewer_id, provider_id, self._now()))
                append_event(conn, actor_id=actor_id, action="inclusive.reviewer_interest.declared",
                             resource_type="reviewer_link", resource_id=f"{reviewer_id}:{provider_id}",
                             detail={"reviewer_id": reviewer_id, "provider_id": provider_id},
                             occurred_at=self._now())
                return "reviewer_link", f"{reviewer_id}:{provider_id}", \
                    {"reviewer_id": reviewer_id, "provider_id": provider_id}

            return self._idempotent(conn, request_id=request_id, action="declare_reviewer_interest",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 政策版本冻结
    # ------------------------------------------------------------------
    def freeze_policy(self, *, request_id: str, actor_id: str, version: str,
                      policy: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version": version, "policy": policy}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            version = self._id(version, "version")
            self._validate_policy(policy)
            policy_hash = digest({"version": version, "policy": policy})

            def create():
                try:
                    conn.execute(
                        "INSERT INTO incl_policies(version,payload_json,payload_hash,frozen_by,frozen_at) "
                        "VALUES(?,?,?,?,?)",
                        (version, canonical_json(policy), policy_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("政策版本已经冻结过") from exc
                append_event(conn, actor_id=actor_id, action="inclusive.policy.frozen",
                             resource_type="policy", resource_id=version,
                             detail={"version": version, "policy_hash": policy_hash,
                                     "categories": sorted(policy["categories"])},
                             occurred_at=self._now())
                return "policy", version, {"version": version, "policy_hash": policy_hash}

            return self._idempotent(conn, request_id=request_id, action="freeze_policy",
                                    payload=payload, create=create)

    def _validate_policy(self, policy: dict[str, Any]) -> None:
        if not isinstance(policy, dict):
            raise ValidationError("policy 必须是对象")
        weights = policy.get("weights")
        required_weights = {"region_priority", "digital_gap", "beneficiary_need", "review_score"}
        if not isinstance(weights, dict) or set(weights) != required_weights:
            raise ValidationError("weights 必须包含 region_priority/digital_gap/beneficiary_need/review_score")
        if abs(sum(float(v) for v in weights.values()) - 1.0) > 1e-9:
            raise ValidationError("weights 权重之和必须为 1")
        if not isinstance(policy.get("tier_weights"), dict) or not policy["tier_weights"]:
            raise ValidationError("tier_weights 必须是非空的地区层级权重表")
        for tier, value in policy["tier_weights"].items():
            if not 0 <= float(value) <= 1:
                raise ValidationError(f"tier_weights.{tier} 必须在 0 到 1 之间")
        reserve_hours = policy.get("reserve_hours")
        if not isinstance(reserve_hours, int) or reserve_hours <= 0:
            raise ValidationError("reserve_hours 必须是正整数（小时）")
        categories = policy.get("categories")
        if not isinstance(categories, dict) or not categories:
            raise ValidationError("categories 必须是非空的支持类别配置")
        for category, config in categories.items():
            if not isinstance(config.get("quota"), int) or config["quota"] <= 0:
                raise ValidationError(f"{category}.quota 必须是正整数")
            if not isinstance(config.get("per_applicant"), int) or config["per_applicant"] <= 0:
                raise ValidationError(f"{category}.per_applicant 必须是正整数金额上限")
            materials = config.get("required_materials", [])
            if not isinstance(materials, list) or not all(isinstance(m, str) and m for m in materials):
                raise ValidationError(f"{category}.required_materials 必须是字符串列表")
            milestones = config.get("milestones", [])
            if not isinstance(milestones, list) or not milestones:
                raise ValidationError(f"{category}.milestones 至少包含一个里程碑")
            codes = set()
            for milestone in milestones:
                code = milestone.get("code")
                if not code or code in codes:
                    raise ValidationError(f"{category} 里程碑编号缺失或重复")
                codes.add(code)
                if milestone.get("kind") not in {"training", "deployment"}:
                    raise ValidationError(f"{category}.{code}.kind 必须是 training 或 deployment")
            installments = config.get("installments", [])
            if not isinstance(installments, list) or not installments:
                raise ValidationError(f"{category}.installments 至少包含一期")
            total_pct = 0
            for installment in installments:
                seq = installment.get("seq")
                pct = installment.get("pct")
                trigger = installment.get("trigger")
                if not isinstance(seq, int) or seq < 1 or not isinstance(pct, int) or pct <= 0:
                    raise ValidationError(f"{category} 分期 seq/pct 无效")
                if trigger != COMMIT_TRIGGER and trigger not in codes:
                    raise ValidationError(f"{category} 第 {seq} 期 trigger 必须引用里程碑或 {COMMIT_TRIGGER}")
                total_pct += pct
            if total_pct != 100:
                raise ValidationError(f"{category} 分期比例之和必须为 100")
            metrics = config.get("metrics", [])
            if not isinstance(metrics, list):
                raise ValidationError(f"{category}.metrics 必须是列表")
            for metric in metrics:
                if not metric.get("code") or not isinstance(metric.get("target"), (int, float)):
                    raise ValidationError(f"{category} 指标必须包含 code 与 target")
            floor = config.get("region_floor", {})
            if not isinstance(floor, dict):
                raise ValidationError(f"{category}.region_floor 必须是层级到保底名额的映射")

    # ------------------------------------------------------------------
    # 轮次与预算
    # ------------------------------------------------------------------
    def open_round(self, *, request_id: str, actor_id: str, round_id: str,
                   policy_version: str, budget: dict[str, int]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id,
                   "policy_version": policy_version, "budget": budget}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            round_id = self._id(round_id, "round_id")
            policy_row = self._load(conn, "SELECT * FROM incl_policies WHERE version=?", (policy_version,))
            policy = json.loads(policy_row["payload_json"])
            if not isinstance(budget, dict) or not budget:
                raise ValidationError("budget 必须是非空的类别预算映射")
            for category, amount in budget.items():
                if category not in policy["categories"]:
                    raise ValidationError(f"预算类别 {category} 不在冻结政策中")
                if not isinstance(amount, int) or amount <= 0:
                    raise ValidationError(f"预算 {category} 必须是正整数")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO incl_rounds(round_id,policy_version,budget_json,status,opened_at) "
                        "VALUES(?,?,?,'open',?)",
                        (round_id, policy_version, canonical_json(budget), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("轮次编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="inclusive.round.opened",
                             resource_type="round", resource_id=round_id,
                             detail={"policy_version": policy_version, "budget": budget},
                             occurred_at=self._now())
                return "round", round_id, {"round_id": round_id}

            return self._idempotent(conn, request_id=request_id, action="open_round",
                                    payload=payload, create=create)

    def close_round(self, *, request_id: str, actor_id: str, round_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            round_row = self._load(conn, "SELECT * FROM incl_rounds WHERE round_id=?", (round_id,))
            if round_row["status"] != "open":
                raise ConflictError("轮次已经关闭")

            def create():
                conn.execute("UPDATE incl_rounds SET status='closed', closed_at=? WHERE round_id=?",
                             (self._now(), round_id))
                append_event(conn, actor_id=actor_id, action="inclusive.round.closed",
                             resource_type="round", resource_id=round_id,
                             detail={"policy_version": round_row["policy_version"]},
                             occurred_at=self._now())
                return "round", round_id, {"round_id": round_id, "status": "closed"}

            return self._idempotent(conn, request_id=request_id, action="close_round",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 申请主体登记与申报（关联去重）
    # ------------------------------------------------------------------
    def register_applicant(self, *, request_id: str, actor_id: str, applicant_id: str,
                           organization_id: str, region_id: str, affiliation_group: str,
                           baseline: dict[str, Any], beneficiary_population: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "applicant_id": applicant_id,
                   "organization_id": organization_id, "region_id": region_id,
                   "affiliation_group": affiliation_group, "baseline": baseline,
                   "beneficiary_population": beneficiary_population}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            applicant_id = self._id(applicant_id, "applicant_id")
            affiliation_group = self._id(affiliation_group, "affiliation_group")
            self._load(conn, "SELECT 1 FROM organizations WHERE organization_id=?", (organization_id,))
            self._load(conn, "SELECT 1 FROM incl_regions WHERE region_id=?", (region_id,))
            readiness = baseline.get("digital_readiness") if isinstance(baseline, dict) else None
            if not isinstance(readiness, (int, float)) or not 0 <= float(readiness) <= 100:
                raise ValidationError("baseline.digital_readiness 必须在 0 到 100 之间")
            if not isinstance(beneficiary_population, int) or beneficiary_population < 0:
                raise ValidationError("beneficiary_population 必须是非负整数")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO incl_applicants(applicant_id,organization_id,region_id,"
                        "affiliation_group,baseline_json,beneficiary_population,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (applicant_id, organization_id, region_id, affiliation_group,
                         canonical_json(baseline), beneficiary_population, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("申请主体编号或挂靠机构已经登记") from exc
                append_event(conn, actor_id=actor_id, action="inclusive.applicant.registered",
                             resource_type="applicant", resource_id=applicant_id,
                             detail={"organization_id": organization_id, "region_id": region_id,
                                     "affiliation_group": affiliation_group},
                             occurred_at=self._now())
                return "applicant", applicant_id, {"applicant_id": applicant_id}

            return self._idempotent(conn, request_id=request_id, action="register_applicant",
                                    payload=payload, create=create)

    def submit_application(self, *, request_id: str, actor_id: str, application_id: str,
                           round_id: str, applicant_id: str, category: str,
                           provider_id: str, requested_amount: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id, "round_id": round_id,
                   "applicant_id": applicant_id, "category": category, "provider_id": provider_id,
                   "requested_amount": requested_amount}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="submit_application",
                                  payload=payload)
            if replay:
                return replay
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._id(application_id, "application_id")
            round_row = self._load(conn, "SELECT * FROM incl_rounds WHERE round_id=?", (round_id,))
            if round_row["status"] != "open":
                raise ConflictError("轮次已关闭，不能提交申报")
            policy = json.loads(conn.execute(
                "SELECT payload_json FROM incl_policies WHERE version=?",
                (round_row["policy_version"],)).fetchone()["payload_json"])
            if category not in policy["categories"]:
                raise ValidationError("支持类别不在冻结政策中")
            cap = policy["categories"][category]["per_applicant"]
            requested_amount = self._amount(requested_amount, "requested_amount")
            if requested_amount > cap:
                raise ValidationError(f"申报金额超过该类别单主体上限 {cap}")
            applicant = self._load(conn, "SELECT * FROM incl_applicants WHERE applicant_id=?",
                                   (applicant_id,))
            provider = self._load(conn, "SELECT 1 AS x, active FROM incl_providers WHERE provider_id=?",
                                  (provider_id,))
            if not provider["active"]:
                raise ConflictError("服务商已停用")

            def create():
                # 关联去重：同一轮次、同一类别下，一个关联组只保留最早一条有效申报。
                keeper = conn.execute(
                    "SELECT application_id FROM incl_applications WHERE round_id=? AND category=? AND status!='duplicate' "
                    "AND applicant_id IN (SELECT applicant_id FROM incl_applicants "
                    "WHERE affiliation_group=?) ORDER BY submitted_at, application_id LIMIT 1",
                    (round_id, category, applicant["affiliation_group"])).fetchone()
                status = "duplicate" if keeper else "eligible"
                now = self._now()
                try:
                    conn.execute(
                        "INSERT INTO incl_applications(application_id,round_id,applicant_id,category,"
                        "provider_id,requested_amount,status,submitted_at,screened_at,screening_result,"
                        "screening_reason,screening_detail,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (application_id, round_id, applicant_id, category, provider_id,
                         requested_amount, status, now, now,
                         "duplicate" if keeper else "eligible",
                         "affiliation_group_dedup" if keeper else "screening_passed",
                         canonical_json({"affiliation_group": applicant["affiliation_group"],
                                         **({"keeper_application_id": keeper["application_id"]} if keeper else {})}),
                         now),
                    )
                except Exception as exc:
                    raise ConflictError("申请编号已经存在或同一主体重复申报该类别") from exc
                for milestone in policy["categories"][category]["milestones"]:
                    conn.execute(
                        "INSERT INTO incl_milestones(application_id,code,kind,title,prerequisite,seq,status) "
                        "VALUES(?,?,?,?,?,?,'pending')",
                        (application_id, milestone["code"], milestone["kind"],
                         milestone.get("title", milestone["code"]),
                         1 if milestone.get("prerequisite") else 0,
                         milestone.get("seq", 0)),
                    )
                append_event(conn, actor_id=actor_id, action="inclusive.application.submitted",
                             resource_type="application", resource_id=application_id,
                             detail={"round_id": round_id, "category": category,
                                     "provider_id": provider_id, "screening": status,
                                     "affiliation_group": applicant["affiliation_group"]},
                             occurred_at=now)
                return "application", application_id, \
                    {"application_id": application_id, "screening_result": status}

            return self._idempotent(conn, request_id=request_id, action="submit_application",
                                    payload=payload, create=create)
    # ------------------------------------------------------------------
    def submit_review(self, *, request_id: str, actor_id: str, application_id: str,
                      score: float, comment: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "score": score, "comment": comment}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="submit_review",
                                  payload=payload)
            if replay:
                return replay
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            app = self._app(conn, application_id)
            if app["status"] not in SCORABLE:
                raise ConflictError("当前申请状态不接受评分")
            conflict = self._reviewer_conflict(conn, actor, app["provider_id"])
            if conflict:
                raise PermissionDenied(f"评审回避：存在利益关系 {conflict['type']}")
            if not isinstance(score, (int, float)) or not 0 <= float(score) <= 100:
                raise ValidationError("score 必须在 0 到 100 之间")

            def create():
                conn.execute(
                    "INSERT INTO incl_reviewer_scores(application_id,reviewer_id,score,comment,created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(application_id,reviewer_id) DO UPDATE SET "
                    "score=excluded.score, comment=excluded.comment, created_at=excluded.created_at",
                    (application_id, actor_id, float(score), str(comment)[:500], self._now()),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.review.submitted",
                             resource_type="application", resource_id=application_id,
                             detail={"score": float(score)}, occurred_at=self._now())
                return "review", f"{application_id}:{actor_id}", \
                    {"application_id": application_id, "reviewer_id": actor_id}

            return self._idempotent(conn, request_id=request_id, action="submit_review",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 冻结排序与名额分配
    # ------------------------------------------------------------------
    def run_ranking(self, *, request_id: str, actor_id: str, round_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "round_id": round_id}
        with self.database.transaction(immediate=True) as conn:
            request_id = self._id(request_id, "request_id")
            payload_hash = digest(payload)
            existing = conn.execute(
                "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["action"] != "run_ranking" or existing["payload_hash"] != payload_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                return {"request_id": request_id, "resource_type": existing["resource_type"],
                        "resource_id": existing["resource_id"], "replayed": True}
            # 先处理已到期的预留，释放出来的预算参与本轮分配。
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            round_row, policy, policy_hash = self._round_policy(conn, round_id)
            budget = json.loads(round_row["budget_json"])
            run_id = uuid.uuid4().hex
            now = self._now()
            expires = (self.clock.now() + timedelta(hours=policy["reserve_hours"])) \
                .isoformat().replace("+00:00", "Z")

            apps = conn.execute(
                "SELECT a.*, ap.region_id, ap.affiliation_group, ap.baseline_json, "
                "ap.beneficiary_population, r.tier AS region_tier, r.priority AS region_priority "
                "FROM incl_applications a "
                "JOIN incl_applicants ap ON ap.applicant_id=a.applicant_id "
                "JOIN incl_regions r ON r.region_id=ap.region_id "
                "WHERE a.round_id=?", (round_id,)).fetchall()

            # 快照与评分只对候选申请做；重复申报、已放弃/到期的不进入排序。
            candidates_by_category: dict[str, list] = {}
            locked_items: list[dict[str, Any]] = []
            snapshot_rows = []
            for app in apps:
                if app["status"] == "duplicate":
                    locked_items.append({"application_id": app["application_id"], "category": app["category"],
                                         "decision": "duplicate", "total_score": 0.0,
                                         "factors": {}, "reason_codes": ["duplicate_affiliation"]})
                    continue
                if app["status"] in ("waived", "expired"):
                    continue
                if app["status"] in ("committed", "completed", "partial_failed", "budget_cut"):
                    reason = {"completed": "verified_completed_locked",
                              "committed": "commitment_locked",
                              "partial_failed": "partial_failure_locked",
                              "budget_cut": "budget_cut_locked"}[app["status"]]
                    locked_items.append({"application_id": app["application_id"], "category": app["category"],
                                         "decision": "locked", "total_score": 0.0, "factors": {},
                                         "reason_codes": [reason]})
                    continue
                if app["status"] == "reserved":
                    # 既有限时预留不受重排影响，时钟保持不变。
                    locked_items.append({"application_id": app["application_id"], "category": app["category"],
                                         "decision": "held", "total_score": 0.0, "factors": {},
                                         "reason_codes": ["reservation_held"]})
                    continue
                candidates_by_category.setdefault(app["category"], []).append(app)

            selected_ids: set[str] = set()
            all_items = list(locked_items)
            for category, cohort in candidates_by_category.items():
                config = policy["categories"][category]
                scored = []
                max_pop = max([int(a["beneficiary_population"]) for a in cohort] + [1])
                for app in cohort:
                    score_row = conn.execute(
                        "SELECT AVG(score) AS avg_score FROM incl_reviewer_scores WHERE application_id=?",
                        (app["application_id"],)).fetchone()
                    avg_review = float(score_row["avg_score"] or 0.0)
                    baseline = json.loads(app["baseline_json"])
                    factors = {
                        "region_priority": 0.5 * float(policy["tier_weights"].get(app["region_tier"], 0.0))
                                           + 0.5 * float(app["region_priority"]),
                        "digital_gap": (100.0 - float(baseline.get("digital_readiness", 100))) / 100.0,
                        "beneficiary_need": int(app["beneficiary_population"]) / max_pop,
                        "review_score": avg_review / 100.0,
                    }
                    total = sum(float(policy["weights"][key]) * factors[key] for key in factors)
                    scored.append((app, factors, total))
                    snapshot_rows.append({
                        "application_id": app["application_id"],
                        "region_id": app["region_id"], "tier": app["region_tier"],
                        "digital_readiness": baseline.get("digital_readiness"),
                        "beneficiary_population": app["beneficiary_population"],
                        "avg_review_score": avg_review,
                        "submitted_at": app["submitted_at"], "factors": factors, "total": total,
                    })
                scored.sort(key=lambda item: (
                    -item[2],
                    -item[1]["region_priority"], -item[1]["digital_gap"],
                    -item[1]["beneficiary_need"], -item[1]["review_score"],
                    item[0]["submitted_at"], item[0]["application_id"],
                ))

                occupying = conn.execute(
                    "SELECT COUNT(*) AS c FROM incl_applications WHERE round_id=? AND category=? "
                    "AND (status='reserved' OR commitment_id IS NOT NULL)",
                    (round_id, category)).fetchone()["c"]
                quota_left = max(0, int(config["quota"]) - int(occupying))
                budget_left = int(budget[category]) - self._ledger_balance(conn, round_id, category)

                chosen: list[tuple] = []
                chosen_ids: set[str] = set()
                # 地区保底：偏远层级先占保底名额，但仍受预算约束。
                for tier, floor in sorted(config.get("region_floor", {}).items(),
                                          key=lambda kv: -policy["tier_weights"].get(kv[0], 0.0)):
                    taken = sum(1 for c in chosen if c[0]["region_tier"] == tier)
                    for item in scored:
                        if taken >= int(floor) or len(chosen) >= quota_left:
                            break
                        app, _, _ = item
                        if app["region_tier"] == tier and app["application_id"] not in chosen_ids \
                                and app["requested_amount"] <= budget_left:
                            chosen.append(item + ("region_floor:" + tier,))
                            chosen_ids.add(app["application_id"])
                            budget_left -= int(app["requested_amount"])
                            taken += 1
                # 其余名额严格按冻结排序填充。
                for item in scored:
                    if len(chosen) >= quota_left:
                        break
                    app, _, _ = item
                    if app["application_id"] in chosen_ids:
                        continue
                    if app["requested_amount"] <= budget_left:
                        chosen.append(item + ("rank_selected",))
                        chosen_ids.add(app["application_id"])
                        budget_left -= int(app["requested_amount"])

                for rank, item in enumerate(scored, start=1):
                    app, factors, total = item
                    if app["application_id"] in chosen_ids:
                        continue
                    all_items.append({"application_id": app["application_id"], "category": category,
                                      "decision": "waitlisted", "total_score": total, "factors": factors,
                                      "rank_position": rank,
                                      "reason_codes": ["quota_or_budget_exhausted"]})
                for item in chosen:
                    app, factors, total = item[0], item[1], item[2]
                    reason = item[3]
                    selected_ids.add(app["application_id"])
                    rank = next(i for i, x in enumerate(scored, start=1)
                                if x[0]["application_id"] == app["application_id"])
                    all_items.append({"application_id": app["application_id"], "category": category,
                                      "decision": "selected", "total_score": total, "factors": factors,
                                      "rank_position": rank,
                                      "reason_codes": [reason, "policy:" + round_row["policy_version"]]})

            snapshot_hash = digest({"policy_hash": policy_hash,
                                    "rows": sorted(snapshot_rows, key=lambda r: r["application_id"])})
            conn.execute(
                "INSERT INTO incl_ranking_runs(run_id,round_id,policy_version,snapshot_hash,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, round_id, round_row["policy_version"], snapshot_hash, actor_id, now),
            )

            waitlist_counter: dict[str, int] = {}
            for item in sorted(all_items, key=lambda i: (i["category"], i.get("rank_position", 0))):
                conn.execute(
                    "INSERT INTO incl_ranking_items(run_id,application_id,category,rank_position,"
                    "total_score,factors_json,decision,reason_codes_json) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, item["application_id"], item["category"],
                     item.get("rank_position", 0) if item["decision"] != "locked"
                     and item["decision"] != "held" and item["decision"] != "duplicate" else 0,
                     item["total_score"], canonical_json(item["factors"]), item["decision"],
                     canonical_json(item["reason_codes"])),
                )
                app_id = item["application_id"]
                if item["decision"] == "selected":
                    self._make_reservation(conn, run_id=run_id, app_id=app_id,
                                           amount=conn.execute(
                                               "SELECT requested_amount FROM incl_applications "
                                               "WHERE application_id=?", (app_id,)).fetchone()["requested_amount"],
                                           expires_at=expires, now=now, reason="ranking_selected")
                elif item["decision"] == "waitlisted":
                    waitlist_counter[item["category"]] = waitlist_counter.get(item["category"], 0) + 1
                    conn.execute(
                        "UPDATE incl_applications SET status='eligible', waitlist_rank=?, current_run_id=?, "
                        "updated_at=? WHERE application_id=?",
                        (waitlist_counter[item["category"]], run_id, now, app_id),
                    )
                else:
                    conn.execute("UPDATE incl_applications SET current_run_id=?, updated_at=? "
                                 "WHERE application_id=?", (run_id, now, app_id))

            append_event(conn, actor_id=actor_id, action="inclusive.ranking.run",
                         resource_type="ranking_run", resource_id=run_id,
                         detail={"round_id": round_id, "policy_version": round_row["policy_version"],
                                 "snapshot_hash": snapshot_hash, "selected": len(selected_ids)},
                         occurred_at=now)

            response = {"run_id": run_id, "snapshot_hash": snapshot_hash,
                        "selected": len(selected_ids)}
            conn.execute(
                "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
                "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, "run_ranking", payload_hash, "ranking_run", run_id,
                 canonical_json(response), now),
            )
            return {"request_id": request_id, "resource_type": "ranking_run",
                    "resource_id": run_id, "replayed": False, **response}

    def _make_reservation(self, conn, *, run_id: str | None, app_id: str, amount: int,
                          expires_at: str, now: str, reason: str,
                          request_id: str | None = None, via_special: bool = False) -> None:
        app = self._app(conn, app_id)
        conn.execute(
            "UPDATE incl_applications SET status='reserved', reserved_amount=?, reserved_at=?, "
            "expires_at=?, waitlist_rank=NULL, current_run_id=COALESCE(?, current_run_id), "
            "via_special=?, updated_at=? WHERE application_id=?",
            (amount, now, expires_at, run_id, 1 if via_special else (app["via_special"] or 0), now, app_id),
        )
        self._ledger_add(conn, round_id=app["round_id"], category=app["category"],
                         application_id=app_id, amount=amount, reason=reason, request_id=request_id)
        append_event(conn, actor_id="system", action="inclusive.funds.reserved",
                     resource_type="application", resource_id=app_id,
                     detail={"amount": amount, "reason": reason, "expires_at": expires_at},
                     occurred_at=now)

    def _release_reservation(self, conn, app, *, reason: str, now: str,
                             new_status: str = "expired") -> int:
        """释放整笔预留，返回释放金额。"""
        amount = int(app["reserved_amount"] or 0)
        if amount:
            self._ledger_add(conn, round_id=app["round_id"], category=app["category"],
                             application_id=app["application_id"], amount=-amount,
                             reason=reason, request_id=None)
        conn.execute(
            "UPDATE incl_applications SET status=?, reserved_amount=NULL, reserved_at=NULL, "
            "expires_at=NULL, updated_at=? WHERE application_id=?",
            (new_status, now, app["application_id"]),
        )
        append_event(conn, actor_id="system", action="inclusive.funds.released",
                     resource_type="application", resource_id=app["application_id"],
                     detail={"amount": amount, "reason": reason, "new_status": new_status},
                     occurred_at=now)
        return amount

    def _sweep_expired(self, conn) -> tuple[list[str], list[str]]:
        """把到期未转正式承诺的预留释放并登记退出责任。

        返回 (到期申请, 沿候补新晋升的申请)。
        """
        now_text = self._now()
        rows = conn.execute(
            "SELECT * FROM incl_applications WHERE status='reserved' AND expires_at<=?",
            (now_text,)).fetchall()
        affected = []
        promoted: list[str] = []
        for app in rows:
            self._release_reservation(conn, app, reason="reservation_expired", now=now_text)
            conn.execute(
                "INSERT INTO incl_exits(exit_id,application_id,kind,responsibility_amount,note,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, app["application_id"], "expired", 0,
                 "预留到期未完成必要材料或前置里程碑", "system", now_text),
            )
            affected.append(app["application_id"])
            promoted.extend(self._promote_waitlist(conn, app["round_id"]))
        return affected, promoted

    def _promote_waitlist(self, conn, round_id: str) -> list[str]:
        """沿最新一次冻结排序的稳定候补继续，预算与名额同时满足才晋升。"""
        round_row = conn.execute("SELECT * FROM incl_rounds WHERE round_id=?", (round_id,)).fetchone()
        if round_row is None:
            return []
        policy = json.loads(conn.execute(
            "SELECT payload_json FROM incl_policies WHERE version=?",
            (round_row["policy_version"],)).fetchone()["payload_json"])
        run_row = conn.execute(
            "SELECT * FROM incl_ranking_runs WHERE round_id=? ORDER BY rowid DESC LIMIT 1",
            (round_id,)).fetchone()
        if run_row is None:
            return []
        budget = json.loads(round_row["budget_json"])
        now = self._now()
        expires = (self.clock.now() + timedelta(hours=policy["reserve_hours"])) \
            .isoformat().replace("+00:00", "Z")
        promoted = []
        items = conn.execute(
            "SELECT ri.* FROM incl_ranking_items ri WHERE ri.run_id=? AND ri.decision='waitlisted' "
            "ORDER BY ri.category, ri.rank_position", (run_row["run_id"],)).fetchall()
        for item in items:
            app = conn.execute("SELECT * FROM incl_applications WHERE application_id=?",
                               (item["application_id"],)).fetchone()
            if app is None or app["status"] != "eligible" or app["current_run_id"] != run_row["run_id"]:
                continue
            category = item["category"]
            config = policy["categories"][category]
            occupying = conn.execute(
                "SELECT COUNT(*) AS c FROM incl_applications WHERE round_id=? AND category=? "
                "AND (status='reserved' OR commitment_id IS NOT NULL)",
                (round_id, category)).fetchone()["c"]
            if occupying >= config["quota"]:
                continue
            balance = self._ledger_balance(conn, round_id, category)
            if balance + int(app["requested_amount"]) > int(budget[category]):
                continue
            self._make_reservation(conn, run_id=run_row["run_id"], app_id=app["application_id"],
                                   amount=int(app["requested_amount"]), expires_at=expires,
                                   now=now, reason="waitlist_promoted")
            promoted.append(app["application_id"])
        return promoted

    # ------------------------------------------------------------------
    # 材料、限时预留转正式承诺、分期
    # ------------------------------------------------------------------
    def submit_material(self, *, request_id: str, actor_id: str, application_id: str,
                        material_key: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id, "material_key": material_key}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="submit_material",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            app = self._app(conn, application_id)
            if app["status"] not in ("reserved", "eligible"):
                raise ConflictError("当前申请状态不能补交材料")
            policy = self._policy_for_app(conn, app)
            if material_key not in policy["categories"][app["category"]].get("required_materials", []):
                raise ValidationError("该材料不在政策要求清单内")

            def create():
                conn.execute(
                    "INSERT OR IGNORE INTO incl_materials(application_id,material_key,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?)", (application_id, material_key, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.material.submitted",
                             resource_type="application", resource_id=application_id,
                             detail={"material_key": material_key}, occurred_at=self._now())
                return "material", f"{application_id}:{material_key}", \
                    {"application_id": application_id, "material_key": material_key}

            return self._idempotent(conn, request_id=request_id, action="submit_material",
                                    payload=payload, create=create)

    def commit_reservation(self, *, request_id: str, actor_id: str,
                           application_id: str) -> dict[str, Any]:
        """必要材料与前置里程碑齐备后，限时预留才转为正式承诺并生成分期。"""
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="commit_reservation",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            app = self._app(conn, application_id)
            if app["status"] != "reserved":
                raise ConflictError("只有限时预留状态可以转正式承诺")
            if app["expires_at"] <= self._now():
                # 时钟跨越到期点：先释放再报错，保证不超额。
                self._release_reservation(conn, app, reason="reservation_expired", now=self._now())
                self._promote_waitlist(conn, app["round_id"])
                raise ConflictError("预留已到期，额度已释放给候补")
            policy = self._policy_for_app(conn, app)
            config = policy["categories"][app["category"]]
            missing_materials = [key for key in config.get("required_materials", [])
                                 if conn.execute("SELECT 1 FROM incl_materials WHERE application_id=? "
                                                 "AND material_key=?", (application_id, key)).fetchone() is None]
            if missing_materials:
                raise ConflictError(f"必要材料未齐备：{','.join(missing_materials)}")
            blocked = conn.execute(
                "SELECT code FROM incl_milestones WHERE application_id=? AND prerequisite=1 "
                "AND status!='verified'", (application_id,)).fetchall()
            if blocked:
                raise ConflictError(f"前置里程碑未核验通过：{','.join(r['code'] for r in blocked)}")

            def create():
                commitment_id = uuid.uuid4().hex
                amount = int(app["reserved_amount"])
                conn.execute(
                    "INSERT INTO incl_commitments(commitment_id,application_id,round_id,category,"
                    "total_amount,created_at) VALUES(?,?,?,?,?,?)",
                    (commitment_id, application_id, app["round_id"], app["category"], amount, self._now()),
                )
                remaining = amount
                installments = sorted(config["installments"], key=lambda x: x["seq"])
                now = self._now()
                for index, installment in enumerate(installments):
                    if index == len(installments) - 1:
                        part = remaining
                    else:
                        part = int(amount * installment["pct"] / 100)
                        remaining -= part
                    trigger = installment["trigger"]
                    if trigger == COMMIT_TRIGGER:
                        already_paid = True
                        paid_at = now
                    else:
                        milestone_row = conn.execute(
                            "SELECT status FROM incl_milestones WHERE application_id=? AND code=?",
                            (application_id, trigger)).fetchone()
                        already_paid = milestone_row is not None and milestone_row["status"] == "verified"
                        paid_at = now if already_paid else None
                    conn.execute(
                        "INSERT INTO incl_installments(commitment_id,seq,amount,trigger_code,status,paid_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (commitment_id, installment["seq"], part, trigger,
                         "paid" if already_paid else "scheduled", paid_at),
                    )
                conn.execute(
                    "UPDATE incl_applications SET status='committed', commitment_id=?, committed_at=?, "
                    "updated_at=? WHERE application_id=?",
                    (commitment_id, now, now, application_id),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.commitment.created",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"application_id": application_id, "amount": amount,
                                     "installments": len(installments)},
                             occurred_at=now)
                return "commitment", commitment_id, {"commitment_id": commitment_id, "amount": amount}

            return self._idempotent(conn, request_id=request_id, action="commit_reservation",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 里程碑（服务商回调）、核验、分期兑付
    # ------------------------------------------------------------------
    def report_milestone(self, *, request_id: str, actor_id: str, application_id: str,
                         code: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """服务商回调上报培训或部署里程碑。重放同一 request_id 不重复占用额度。"""
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "code": code, "evidence": evidence or {}}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="report_milestone",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            app = self._app(conn, application_id)
            if app["status"] not in ("reserved", "committed"):
                raise ConflictError("只有预留或正式承诺的申请可以上报里程碑")
            milestone = self._load(
                conn, "SELECT * FROM incl_milestones WHERE application_id=? AND code=?",
                (application_id, code))
            if app["status"] == "reserved" and not milestone["prerequisite"]:
                raise ConflictError("只有前置里程碑允许在转正式承诺前上报")
            if milestone["status"] in ("verified", "reported"):
                raise ConflictError("里程碑已经上报或核验")

            def create():
                conn.execute(
                    "UPDATE incl_milestones SET status='reported', evidence_json=?, reported_by=?, "
                    "reported_at=? WHERE application_id=? AND code=?",
                    (canonical_json(evidence or {}), actor_id, self._now(), application_id, code),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.milestone.reported",
                             resource_type="milestone", resource_id=f"{application_id}:{code}",
                             detail={"application_id": application_id, "code": code, "kind": milestone["kind"]},
                             occurred_at=self._now())
                return "milestone", f"{application_id}:{code}", \
                    {"application_id": application_id, "code": code, "status": "reported"}

            return self._idempotent(conn, request_id=request_id, action="report_milestone",
                                    payload=payload, create=create)

    def verify_milestone(self, *, request_id: str, actor_id: str, application_id: str,
                         code: str, passed: bool) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "code": code, "passed": passed}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="verify_milestone",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            app = self._app(conn, application_id)
            if app["status"] not in ("reserved", "committed"):
                raise ConflictError("只有预留或正式承诺的申请可以核验里程碑")
            conflict = self._reviewer_conflict(conn, actor, app["provider_id"])
            if conflict:
                raise PermissionDenied(f"评审回避：核验人存在利益关系 {conflict['type']}")
            milestone = self._load(
                conn, "SELECT * FROM incl_milestones WHERE application_id=? AND code=?",
                (application_id, code))
            if app["status"] == "reserved" and not milestone["prerequisite"]:
                raise ConflictError("只有前置里程碑允许在转正式承诺前核验")
            if milestone["status"] != "reported":
                raise ConflictError("只能核验已上报的里程碑")
            now = self._now()

            def create():
                if passed:
                    conn.execute(
                        "UPDATE incl_milestones SET status='verified', verified_by=?, verified_at=? "
                        "WHERE application_id=? AND code=?", (actor_id, now, application_id, code))
                    released_or_paid = self._pay_installment(conn, app, code, now, actor_id)
                    append_event(conn, actor_id=actor_id, action="inclusive.milestone.verified",
                                 resource_type="milestone", resource_id=f"{application_id}:{code}",
                                 detail={"application_id": application_id, "code": code,
                                         "installment": released_or_paid},
                                 occurred_at=now)
                    self._maybe_complete(conn, app["application_id"], actor_id, now)
                else:
                    conn.execute(
                        "UPDATE incl_milestones SET status='failed', verified_by=?, verified_at=? "
                        "WHERE application_id=? AND code=?", (actor_id, now, application_id, code))
                    if app["status"] == "reserved":
                        self._release_reservation(conn, app, reason="prerequisite_milestone_failed",
                                                  now=now, new_status="expired")
                        released = 0
                        conn.execute(
                            "INSERT INTO incl_exits(exit_id,application_id,kind,responsibility_amount,note,"
                            "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, application_id, "failure", 0,
                             f"前置里程碑 {code} 核验未通过，预留释放", actor_id, now))
                    else:
                        released = self._release_remaining_installments(
                            conn, app, reason="milestone_failed", now=now, exit_kind="failure")
                        conn.execute(
                            "UPDATE incl_applications SET status='partial_failed', updated_at=? "
                            "WHERE application_id=?", (now, application_id))
                    append_event(conn, actor_id=actor_id, action="inclusive.milestone.failed",
                                 resource_type="milestone", resource_id=f"{application_id}:{code}",
                                 detail={"application_id": application_id, "code": code,
                                         "released_amount": released},
                                 occurred_at=now)
                    self._promote_waitlist(conn, app["round_id"])
                return "milestone", f"{application_id}:{code}", \
                    {"application_id": application_id, "code": code,
                     "status": "verified" if passed else "failed"}

            return self._idempotent(conn, request_id=request_id, action="verify_milestone",
                                    payload=payload, create=create)

    def _pay_installment(self, conn, app, trigger_code: str, now: str, actor_id: str) -> dict[str, Any]:
        commitment = conn.execute(
            "SELECT * FROM incl_commitments WHERE application_id=?", (app["application_id"],)).fetchone()
        if commitment is None:
            return {"action": "none"}
        row = conn.execute(
            "SELECT * FROM incl_installments WHERE commitment_id=? AND trigger_code=? AND status='scheduled'",
            (commitment["commitment_id"], trigger_code)).fetchone()
        if row is None:
            return {"action": "none"}
        conn.execute(
            "UPDATE incl_installments SET status='paid', paid_at=? WHERE commitment_id=? AND seq=?",
            (now, commitment["commitment_id"], row["seq"]))
        # 兑付不改变台账占用：预留转承诺时整笔已计入预算，这里只记录兑现。
        append_event(conn, actor_id=actor_id, action="inclusive.installment.paid",
                     resource_type="commitment", resource_id=commitment["commitment_id"],
                     detail={"application_id": app["application_id"], "seq": row["seq"],
                             "amount": row["amount"]},
                     occurred_at=now)
        return {"action": "paid", "seq": row["seq"], "amount": row["amount"]}

    def _paid_total(self, conn, application_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(i.amount),0) AS total FROM incl_installments i "
            "JOIN incl_commitments c ON c.commitment_id=i.commitment_id "
            "WHERE c.application_id=? AND i.status='paid'", (application_id,)).fetchone()
        return int(row["total"])

    def _release_remaining_installments(self, conn, app, *, reason: str, now: str,
                                        exit_kind: str) -> int:
        """局部失败或预算缩减：释放所有尚未兑付的分期，返回释放金额。"""
        commitment = conn.execute(
            "SELECT * FROM incl_commitments WHERE application_id=?", (app["application_id"],)).fetchone()
        if commitment is None:
            return 0
        rows = conn.execute(
            "SELECT * FROM incl_installments WHERE commitment_id=? AND status='scheduled' ORDER BY seq",
            (commitment["commitment_id"],)).fetchall()
        released = 0
        for row in rows:
            conn.execute(
                "UPDATE incl_installments SET status='released' WHERE commitment_id=? AND seq=?",
                (commitment["commitment_id"], row["seq"]))
            released += int(row["amount"])
            self._ledger_add(conn, round_id=app["round_id"], category=app["category"],
                             application_id=app["application_id"], amount=-int(row["amount"]),
                             reason=reason, request_id=None)
        if released:
            paid = self._paid_total(conn, app["application_id"])
            conn.execute(
                "INSERT INTO incl_exits(exit_id,application_id,kind,responsibility_amount,note,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, app["application_id"], exit_kind,
                 paid if exit_kind in ("waiver", "failure") else 0,
                 f"{reason} 释放未兑现分期 {released}", "system", now),
            )
            append_event(conn, actor_id="system", action="inclusive.funds.released",
                         resource_type="commitment", resource_id=commitment["commitment_id"],
                         detail={"application_id": app["application_id"], "amount": released,
                                 "reason": reason},
                         occurred_at=now)
        return released

    # ------------------------------------------------------------------
    # 效果指标与完成
    # ------------------------------------------------------------------
    def report_outcome(self, *, request_id: str, actor_id: str, application_id: str,
                       metric_code: str, value: float) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "metric_code": metric_code, "value": value}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="report_outcome",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            app = self._app(conn, application_id)
            if app["status"] not in ("committed", "partial_failed"):
                raise ConflictError("当前申请状态不能上报效果指标")
            if not isinstance(value, (int, float)):
                raise ValidationError("指标值必须是数值")

            def create():
                existing = conn.execute(
                    "SELECT status FROM incl_outcomes WHERE application_id=? AND metric_code=?",
                    (application_id, metric_code)).fetchone()
                if existing and existing["status"] == "verified":
                    raise ConflictError("已核验的指标不能修改")
                conn.execute(
                    "INSERT INTO incl_outcomes(application_id,metric_code,value,effective,status,"
                    "reported_by,reported_at) VALUES(?,?,?,0,'reported',?,?) "
                    "ON CONFLICT(application_id,metric_code) DO UPDATE SET value=excluded.value,"
                    "status='reported', reported_by=excluded.reported_by, reported_at=excluded.reported_at",
                    (application_id, metric_code, float(value), actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.outcome.reported",
                             resource_type="outcome", resource_id=f"{application_id}:{metric_code}",
                             detail={"application_id": application_id, "value": float(value)},
                             occurred_at=self._now())
                return "outcome", f"{application_id}:{metric_code}", \
                    {"application_id": application_id, "metric_code": metric_code}

            return self._idempotent(conn, request_id=request_id, action="report_outcome",
                                    payload=payload, create=create)

    def verify_outcome(self, *, request_id: str, actor_id: str, application_id: str,
                       metric_code: str, passed: bool) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "metric_code": metric_code, "passed": passed}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="verify_outcome",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "reviewer")
            app = self._app(conn, application_id)
            conflict = self._reviewer_conflict(conn, actor, app["provider_id"])
            if conflict:
                raise PermissionDenied(f"评审回避：核验人存在利益关系 {conflict['type']}")
            row = self._load(conn, "SELECT * FROM incl_outcomes WHERE application_id=? AND metric_code=?",
                             (application_id, metric_code))
            if row["status"] != "reported":
                raise ConflictError("只能核验已上报的指标")
            policy = self._policy_for_app(conn, app)
            target = next((m["target"] for m in policy["categories"][app["category"]].get("metrics", [])
                           if m["code"] == metric_code), None)
            effective = bool(passed and target is not None and float(row["value"]) >= float(target))
            now = self._now()

            def create():
                conn.execute(
                    "UPDATE incl_outcomes SET status=?, effective=?, verified_by=?, verified_at=? "
                    "WHERE application_id=? AND metric_code=?",
                    ("verified" if passed else "rejected", 1 if effective else 0,
                     actor_id, now, application_id, metric_code),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.outcome.verified",
                             resource_type="outcome", resource_id=f"{application_id}:{metric_code}",
                             detail={"application_id": application_id, "passed": passed,
                                     "effective": effective, "target": target},
                             occurred_at=now)
                if passed:
                    self._maybe_complete(conn, application_id, actor_id, now)
                return "outcome", f"{application_id}:{metric_code}", \
                    {"application_id": application_id, "metric_code": metric_code,
                     "status": "verified" if passed else "rejected", "effective": effective}

            return self._idempotent(conn, request_id=request_id, action="verify_outcome",
                                    payload=payload, create=create)

    def _maybe_complete(self, conn, application_id: str, actor_id: str, now: str) -> None:
        app = conn.execute("SELECT * FROM incl_applications WHERE application_id=?",
                           (application_id,)).fetchone()
        if app is None or app["status"] != "committed":
            return
        policy = self._policy_for_app(conn, app)
        config = policy["categories"][app["category"]]
        pending_ms = conn.execute(
            "SELECT COUNT(*) AS c FROM incl_milestones WHERE application_id=? AND status!='verified'",
            (application_id,)).fetchone()["c"]
        if pending_ms:
            return
        required_codes = [m["code"] for m in config.get("metrics", [])]
        for code in required_codes:
            row = conn.execute(
                "SELECT status, effective FROM incl_outcomes WHERE application_id=? AND metric_code=?",
                (application_id, code)).fetchone()
            if row is None or row["status"] != "verified" or not row["effective"]:
                return
        conn.execute("UPDATE incl_applications SET status='completed', completed_at=?, updated_at=? "
                     "WHERE application_id=?", (now, now, application_id))
        append_event(conn, actor_id=actor_id, action="inclusive.application.completed",
                     resource_type="application", resource_id=application_id,
                     detail={"round_id": app["round_id"], "category": app["category"]},
                     occurred_at=now)

    # ------------------------------------------------------------------
    # 放弃、逾期扫描、预算缩减
    # ------------------------------------------------------------------
    def waive_application(self, *, request_id: str, actor_id: str,
                          application_id: str, note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id, "note": note}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="waive_application",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            app = self._app(conn, application_id)
            now = self._now()
            if app["status"] in ("waived", "expired", "completed", "budget_cut", "partial_failed",
                                 "duplicate"):
                raise ConflictError(f"申请处于 {app['status']} 状态，不能放弃")

            def create():
                if app["status"] == "reserved":
                    self._release_reservation(conn, app, reason="applicant_waived", now=now,
                                              new_status="waived")
                    responsibility = 0
                elif app["status"] == "committed":
                    released = self._release_remaining_installments(
                        conn, app, reason="applicant_waived", now=now, exit_kind="waiver")
                    responsibility = self._paid_total(conn, application_id)
                    conn.execute("UPDATE incl_applications SET status='waived', updated_at=? "
                                 "WHERE application_id=?", (now, application_id))
                else:  # eligible / waitlisted
                    conn.execute("UPDATE incl_applications SET status='waived', updated_at=? "
                                 "WHERE application_id=?", (now, application_id))
                    responsibility = 0
                conn.execute(
                    "INSERT INTO incl_exits(exit_id,application_id,kind,responsibility_amount,note,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, application_id, "waiver", responsibility, str(note)[:500],
                     actor_id, now),
                )
                append_event(conn, actor_id=actor_id, action="inclusive.application.waived",
                             resource_type="application", resource_id=application_id,
                             detail={"responsibility_amount": responsibility}, occurred_at=now)
                self._promote_waitlist(conn, app["round_id"])
                return "application", application_id, \
                    {"application_id": application_id, "status": "waived",
                     "responsibility_amount": responsibility}

            return self._idempotent(conn, request_id=request_id, action="waive_application",
                                    payload=payload, create=create)

    def sweep_expirations(self, *, request_id: str, actor_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id}
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")

            def create():
                expired_all, promoted_all = self._sweep_expired(conn)
                append_event(conn, actor_id=actor_id, action="inclusive.expirations.swept",
                             resource_type="sweep", resource_id=request_id,
                             detail={"expired": len(expired_all), "promoted": len(promoted_all)},
                             occurred_at=self._now())
                return "sweep", request_id, {"expired": expired_all, "promoted": promoted_all}

            return self._idempotent(conn, request_id=request_id, action="sweep_expirations",
                                    payload=payload, create=create)

    def reduce_budget(self, *, request_id: str, actor_id: str, round_id: str,
                      category: str, new_amount: int) -> dict[str, Any]:
        """预算缩减：保护已兑付资金，逆候补顺序收回预留，再砍未兑现分期。"""
        payload = {"actor_id": actor_id, "round_id": round_id, "category": category,
                   "new_amount": new_amount}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="reduce_budget",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin")
            round_row = self._load(conn, "SELECT * FROM incl_rounds WHERE round_id=?", (round_id,))
            budget = json.loads(round_row["budget_json"])
            if category not in budget:
                raise ValidationError("预算类别不存在")
            new_amount = self._amount(new_amount, "new_amount")

            def create():
                paid_row = conn.execute(
                    "SELECT COALESCE(SUM(i.amount),0) AS total FROM incl_installments i "
                    "JOIN incl_commitments c ON c.commitment_id=i.commitment_id "
                    "JOIN incl_applications a ON a.application_id=c.application_id "
                    "WHERE a.round_id=? AND c.category=? AND i.status='paid'",
                    (round_id, category)).fetchone()
                if new_amount < int(paid_row["total"]):
                    raise ConflictError("缩减后预算不能低于已兑付金额")
                now = self._now()
                released_reservations = []
                # 先逆候补晋升顺序收回最晚获得的预留。
                while True:
                    occupied = self._ledger_balance(conn, round_id, category)
                    if occupied <= new_amount:
                        break
                    candidate = conn.execute(
                        "SELECT * FROM incl_applications WHERE round_id=? AND category=? AND status='reserved' "
                        "ORDER BY reserved_at DESC, application_id DESC LIMIT 1",
                        (round_id, category)).fetchone()
                    if candidate is None:
                        break
                    self._release_reservation(conn, candidate, reason="budget_cut", now=now,
                                              new_status="budget_cut")
                    conn.execute(
                        "INSERT INTO incl_exits(exit_id,application_id,kind,responsibility_amount,note,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, candidate["application_id"], "budget_cut", 0,
                         "预算缩减收回限时预留", "system", now))
                    released_reservations.append(candidate["application_id"])
                released_installments = 0
                if self._ledger_balance(conn, round_id, category) > new_amount:
                    commitments = conn.execute(
                        "SELECT a.* FROM incl_applications a WHERE a.round_id=? AND a.category=? "
                        "AND a.status IN ('committed','partial_failed') "
                        "ORDER BY a.committed_at DESC, a.application_id DESC",
                        (round_id, category)).fetchall()
                    for app in commitments:
                        if self._ledger_balance(conn, round_id, category) <= new_amount:
                            break
                        released_installments += self._release_remaining_installments(
                            conn, app, reason="budget_cut", now=now, exit_kind="budget_cut")
                        conn.execute(
                            "UPDATE incl_applications SET status='budget_cut', updated_at=? "
                            "WHERE application_id=?", (now, app["application_id"]))
                budget[category] = new_amount
                conn.execute("UPDATE incl_rounds SET budget_json=? WHERE round_id=?",
                             (canonical_json(budget), round_id))
                append_event(conn, actor_id=actor_id, action="inclusive.budget.reduced",
                             resource_type="round", resource_id=round_id,
                             detail={"category": category, "new_amount": new_amount,
                                     "reservations_cut": len(released_reservations),
                                     "installments_released": released_installments},
                             occurred_at=now)
                promoted = self._promote_waitlist(conn, round_id)
                return "round", round_id, {"round_id": round_id, "category": category,
                                           "released_reservations": released_reservations,
                                           "released_installments": released_installments,
                                           "promoted": promoted}

            return self._idempotent(conn, request_id=request_id, action="reduce_budget",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 特批：独立会签 + 地区公平性影响
    # ------------------------------------------------------------------
    def _fairness_snapshot(self, conn, round_id: str) -> dict[str, Any]:
        rows = conn.execute(
            "SELECT a.category, r.tier, r.region_id, COUNT(*) AS count, "
            "COALESCE(SUM(COALESCE(a.reserved_amount, c.total_amount)),0) AS amount "
            "FROM incl_applications a "
            "JOIN incl_applicants ap ON ap.applicant_id=a.applicant_id "
            "JOIN incl_regions r ON r.region_id=ap.region_id "
            "LEFT JOIN incl_commitments c ON c.application_id=a.application_id "
            "WHERE a.round_id=? AND a.status IN ('reserved','committed','completed',"
            "'partial_failed','budget_cut') GROUP BY a.category, r.tier, r.region_id",
            (round_id,)).fetchall()
        by_category: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = by_category.setdefault(row["category"], {"total_count": 0, "total_amount": 0,
                                                              "by_tier": {}})
            entry["total_count"] += row["count"]
            entry["total_amount"] += int(row["amount"])
            tier = entry["by_tier"].setdefault(row["tier"], {"count": 0, "amount": 0})
            tier["count"] += row["count"]
            tier["amount"] += int(row["amount"])
        for entry in by_category.values():
            entry["remote_count_share"] = (
                sum(v["count"] for k, v in entry["by_tier"].items() if k != "urban")
                / entry["total_count"] if entry["total_count"] else 0.0)
        return {"round_id": round_id, "categories": by_category}

    def initiate_special(self, *, request_id: str, actor_id: str, special_id: str,
                         round_id: str, application_id: str, amount: int,
                         reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "special_id": special_id, "round_id": round_id,
                   "application_id": application_id, "amount": amount, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="initiate_special",
                                  payload=payload)
            if replay:
                return replay
            actor = self._actor(conn, actor_id)
            self._require(actor, "admin", "operator")
            special_id = self._id(special_id, "special_id")
            round_row = self._load(conn, "SELECT * FROM incl_rounds WHERE round_id=?", (round_id,))
            app = self._app(conn, application_id)
            if app["round_id"] != round_id:
                raise ValidationError("申请不属于该轮次")
            if app["status"] not in ("eligible", "duplicate", "waived", "expired", "budget_cut"):
                raise ConflictError("只有候补、去重出局或被释放的申请可以申请特批")
            policy = json.loads(conn.execute(
                "SELECT payload_json FROM incl_policies WHERE version=?",
                (round_row["policy_version"],)).fetchone()["payload_json"])
            amount = self._amount(amount, "amount")
            if amount > policy["categories"][app["category"]]["per_applicant"]:
                raise ValidationError("特批金额超过单主体上限")
            reason = str(reason).strip()
            if not reason:
                raise ValidationError("特批理由不能为空")

            def create():
                before = self._fairness_snapshot(conn, round_id)
                try:
                    conn.execute(
                        "INSERT INTO incl_specials(special_id,round_id,application_id,amount,reason,"
                        "initiator_id,status,fairness_before_json,created_at) "
                        "VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (special_id, round_id, application_id, amount, reason, actor_id,
                         canonical_json(before), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("特批编号已经存在") from exc
                append_event(conn, actor_id=actor_id, action="inclusive.special.initiated",
                             resource_type="special", resource_id=special_id,
                             detail={"application_id": application_id, "amount": amount},
                             occurred_at=self._now())
                return "special", special_id, {"special_id": special_id, "status": "pending"}

            return self._idempotent(conn, request_id=request_id, action="initiate_special",
                                    payload=payload, create=create)

    def countersign_special(self, *, request_id: str, actor_id: str, special_id: str,
                            approved: bool) -> dict[str, Any]:
        """审计员独立会签；批准时占用真实预算并记录对地区公平性的影响。"""
        payload = {"actor_id": actor_id, "special_id": special_id, "approved": approved}
        with self.database.transaction(immediate=True) as conn:
            replay = self._replay(conn, request_id=request_id, action="countersign_special",
                                  payload=payload)
            if replay:
                return replay
            self._sweep_expired(conn)
            actor = self._actor(conn, actor_id)
            self._require(actor, "auditor")
            special = self._load(conn, "SELECT * FROM incl_specials WHERE special_id=?", (special_id,))
            if special["status"] != "pending":
                raise ConflictError("特批已经会签")
            if actor.actor_id == special["initiator_id"]:
                raise PermissionDenied("会签必须由发起人之外的独立审计员完成")
            app = self._app(conn, special["application_id"])
            conflict = self._reviewer_conflict(conn, actor, app["provider_id"])
            if conflict:
                raise PermissionDenied(f"评审回避：会签人存在利益关系 {conflict['type']}")
            now = self._now()

            def create():
                if not approved:
                    conn.execute(
                        "UPDATE incl_specials SET status='denied', countersigner_id=?, "
                        "countersigned_at=? WHERE special_id=?", (actor_id, now, special_id))
                    append_event(conn, actor_id=actor_id, action="inclusive.special.countersigned",
                                 resource_type="special", resource_id=special_id,
                                 detail={"approved": False}, occurred_at=now)
                    return "special", special_id, {"special_id": special_id, "status": "denied"}
                round_row = conn.execute("SELECT * FROM incl_rounds WHERE round_id=?",
                                         (special["round_id"],)).fetchone()
                budget = json.loads(round_row["budget_json"])
                balance = self._ledger_balance(conn, special["round_id"], app["category"])
                if balance + int(special["amount"]) > int(budget[app["category"]]):
                    raise ConflictError("特批金额超过剩余预算，不能会签批准")
                policy = json.loads(conn.execute(
                    "SELECT payload_json FROM incl_policies WHERE version=?",
                    (round_row["policy_version"],)).fetchone()["payload_json"])
                expires = (self.clock.now() + timedelta(hours=policy["reserve_hours"])) \
                    .isoformat().replace("+00:00", "Z")
                # 去重出局申请特批获批后恢复为可兑现状态。
                if app["status"] == "duplicate":
                    conn.execute("UPDATE incl_applications SET screening_result='eligible',"
                                 "screening_reason='special_overrides_dedup', updated_at=? "
                                 "WHERE application_id=?", (now, app["application_id"]))
                self._make_reservation(conn, run_id=app["current_run_id"],
                                       app_id=app["application_id"], amount=int(special["amount"]),
                                       expires_at=expires, now=now, reason="special_approved",
                                       request_id=request_id, via_special=True)
                after = self._fairness_snapshot(conn, special["round_id"])
                conn.execute(
                    "UPDATE incl_specials SET status='approved', countersigner_id=?, "
                    "countersigned_at=?, fairness_after_json=? WHERE special_id=?",
                    (actor_id, now, canonical_json(after), special_id))
                append_event(conn, actor_id=actor_id, action="inclusive.special.countersigned",
                             resource_type="special", resource_id=special_id,
                             detail={"approved": True, "amount": special["amount"],
                                     "fairness_before": json.loads(special["fairness_before_json"]),
                                     "fairness_after": after},
                             occurred_at=now)
                return "special", special_id, {"special_id": special_id, "status": "approved",
                                               "fairness_before": json.loads(special["fairness_before_json"]),
                                               "fairness_after": after}

            return self._idempotent(conn, request_id=request_id, action="countersign_special",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询：可复核的获配/落选原因与对比报告
    # ------------------------------------------------------------------
    def get_special(self, special_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM incl_specials WHERE special_id=?", (special_id,)).fetchone()
        if row is None:
            raise NotFoundError("特批不存在")
        result = {key: row[key] for key in row.keys()}
        for key in ("fairness_before_json", "fairness_after_json"):
            new_key = key.replace("_json", "")
            result[new_key] = json.loads(row[key]) if row[key] else None
            result.pop(key, None)
        return result

    def get_application(self, application_id: str) -> dict[str, Any]:
        conn = self.database.connection
        app = conn.execute(
            "SELECT a.*, ap.region_id, ap.affiliation_group FROM incl_applications a "
            "JOIN incl_applicants ap ON ap.applicant_id=a.applicant_id "
            "WHERE a.application_id=?", (application_id,)).fetchone()
        if app is None:
            raise NotFoundError("申请不存在")
        run = None
        if app["current_run_id"]:
            run = conn.execute("SELECT * FROM incl_ranking_runs WHERE run_id=?",
                               (app["current_run_id"],)).fetchone()
        item = None
        if run:
            item = conn.execute("SELECT * FROM incl_ranking_items WHERE run_id=? AND application_id=?",
                                (run["run_id"], application_id)).fetchone()
        result = {key: app[key] for key in app.keys()}
        for key in ("baseline_json",):
            result.pop(key, None)
        result["via_special"] = bool(app["via_special"])
        if item:
            result["policy_version"] = run["policy_version"]
            result["snapshot_hash"] = run["snapshot_hash"]
            result["decision"] = item["decision"]
            result["rank_position"] = item["rank_position"]
            result["total_score"] = item["total_score"]
            result["factors"] = json.loads(item["factors_json"])
            result["reason_codes"] = json.loads(item["reason_codes_json"])
        return result

    def list_ranking(self, run_id: str) -> dict[str, Any]:
        conn = self.database.connection
        run = conn.execute("SELECT * FROM incl_ranking_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise NotFoundError("排序运行不存在")
        items = []
        for row in conn.execute("SELECT * FROM incl_ranking_items WHERE run_id=? "
                                "ORDER BY category, rank_position", (run_id,)).fetchall():
            items.append({"application_id": row["application_id"], "category": row["category"],
                          "rank_position": row["rank_position"], "total_score": row["total_score"],
                          "decision": row["decision"], "factors": json.loads(row["factors_json"]),
                          "reason_codes": json.loads(row["reason_codes_json"])})
        return {"run_id": run_id, "round_id": run["round_id"],
                "policy_version": run["policy_version"], "snapshot_hash": run["snapshot_hash"],
                "created_at": run["created_at"], "items": items}

    def list_waitlist(self, round_id: str) -> list[dict[str, Any]]:
        conn = self.database.connection
        run = conn.execute(
            "SELECT * FROM incl_ranking_runs WHERE round_id=? ORDER BY rowid DESC LIMIT 1",
            (round_id,)).fetchone()
        if run is None:
            return []
        rows = conn.execute(
            "SELECT ri.application_id, ri.category, ri.rank_position, ri.total_score, "
            "ri.reason_codes_json, a.status, a.waitlist_rank "
            "FROM incl_ranking_items ri JOIN incl_applications a ON a.application_id=ri.application_id "
            "WHERE ri.run_id=? AND ri.decision='waitlisted' ORDER BY ri.category, ri.rank_position",
            (run["run_id"],)).fetchall()
        return [{"application_id": r["application_id"], "category": r["category"],
                 "rank_position": r["rank_position"], "waitlist_rank": r["waitlist_rank"],
                 "status": r["status"], "total_score": r["total_score"],
                 "reason_codes": json.loads(r["reason_codes_json"])} for r in rows]

    def installment_balance(self, application_id: str) -> dict[str, Any]:
        """分期余额：恢复运行后仍可准确查询未兑现金额。"""
        conn = self.database.connection
        app = self._app(conn, application_id)
        rows = conn.execute(
            "SELECT i.seq, i.amount, i.trigger_code, i.status, i.paid_at "
            "FROM incl_installments i JOIN incl_commitments c ON c.commitment_id=i.commitment_id "
            "WHERE c.application_id=? ORDER BY i.seq", (application_id,)).fetchall()
        installments = [dict(r) for r in rows]
        scheduled = sum(r["amount"] for r in installments if r["status"] == "scheduled")
        paid = sum(r["amount"] for r in installments if r["status"] == "paid")
        released = sum(r["amount"] for r in installments if r["status"] == "released")
        return {"application_id": application_id, "status": app["status"],
                "installments": installments, "paid_amount": paid,
                "remaining_amount": scheduled, "released_amount": released}

    def list_exits(self, round_id: str | None = None) -> list[dict[str, Any]]:
        conn = self.database.connection
        if round_id:
            rows = conn.execute(
                "SELECT e.* FROM incl_exits e JOIN incl_applications a ON a.application_id=e.application_id "
                "WHERE a.round_id=? ORDER BY e.created_at", (round_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM incl_exits ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def fund_report(self, round_id: str) -> dict[str, Any]:
        conn = self.database.connection
        round_row = self._load(conn, "SELECT * FROM incl_rounds WHERE round_id=?", (round_id,))
        budget = json.loads(round_row["budget_json"])
        categories = {}
        for category, amount in budget.items():
            occupied = self._ledger_balance(conn, round_id, category)
            paid = conn.execute(
                "SELECT COALESCE(SUM(i.amount),0) AS total FROM incl_installments i "
                "JOIN incl_commitments c ON c.commitment_id=i.commitment_id "
                "WHERE c.round_id=? AND c.category=? AND i.status='paid'",
                (round_id, category)).fetchone()["total"]
            count_statuses = ("eligible", "reserved", "committed", "completed",
                              "partial_failed", "waived", "expired", "budget_cut", "duplicate")
            counts = {status: conn.execute(
                "SELECT COUNT(*) AS c FROM incl_applications WHERE round_id=? AND category=? AND status=?",
                (round_id, category, status)).fetchone()["c"]
                for status in count_statuses}
            categories[category] = {"budget": amount, "occupied": occupied,
                                    "remaining": amount - occupied, "paid": int(paid),
                                    "coverage": occupied / amount if amount else 0.0,
                                    "counts": counts}
        return {"round_id": round_id, "policy_version": round_row["policy_version"],
                "categories": categories}

    def region_report(self, round_id: str) -> dict[str, Any]:
        conn = self.database.connection
        rows = conn.execute(
            "SELECT r.region_id, r.tier, r.priority, a.category, a.status, COUNT(*) AS count, "
            "COALESCE(SUM(COALESCE(c.total_amount, a.reserved_amount, a.requested_amount)),0) AS amount "
            "FROM incl_applications a "
            "JOIN incl_applicants ap ON ap.applicant_id=a.applicant_id "
            "JOIN incl_regions r ON r.region_id=ap.region_id "
            "LEFT JOIN incl_commitments c ON c.application_id=a.application_id "
            "WHERE a.round_id=? GROUP BY r.region_id, a.category, a.status", (round_id,)).fetchall()
        regions: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = regions.setdefault(row["region_id"],
                                       {"tier": row["tier"], "priority": row["priority"],
                                        "categories": {}})
            entry["categories"][row["category"] + ":" + row["status"]] = \
                {"count": row["count"], "amount": int(row["amount"])}
        return {"round_id": round_id, "regions": regions,
                "fairness": self._fairness_snapshot(conn, round_id)}

    def outcome_report(self, round_id: str) -> dict[str, Any]:
        conn = self.database.connection
        rows = conn.execute(
            "SELECT o.metric_code, o.status, o.effective, COUNT(*) AS count "
            "FROM incl_outcomes o JOIN incl_applications a ON a.application_id=o.application_id "
            "WHERE a.round_id=? GROUP BY o.metric_code, o.status, o.effective", (round_id,)).fetchall()
        metrics: dict[str, dict[str, int]] = {}
        for row in rows:
            entry = metrics.setdefault(row["metric_code"], {"reported": 0, "verified": 0,
                                                            "effective": 0, "rejected": 0})
            entry[row["status"]] = entry.get(row["status"], 0) + row["count"]
            if row["effective"]:
                entry["effective"] += row["count"]
        completed = conn.execute(
            "SELECT COUNT(*) AS c FROM incl_applications WHERE round_id=? AND status='completed'",
            (round_id,)).fetchone()["c"]
        committed = conn.execute(
            "SELECT COUNT(*) AS c FROM incl_applications WHERE round_id=? AND status IN "
            "('committed','completed','partial_failed')", (round_id,)).fetchone()["c"]
        return {"round_id": round_id, "metrics": metrics, "completed": completed,
                "committed": committed,
                "completion_rate": completed / committed if committed else 0.0}

    def compare_policies(self) -> dict[str, Any]:
        """按政策版本比较资金覆盖、地区分布与有效成果。"""
        conn = self.database.connection
        result = {}
        for round_row in conn.execute("SELECT * FROM incl_rounds ORDER BY opened_at").fetchall():
            fund = self.fund_report(round_row["round_id"])
            outcome = self.outcome_report(round_row["round_id"])
            fairness = self._fairness_snapshot(conn, round_row["round_id"])
            version = result.setdefault(round_row["policy_version"], {"rounds": []})
            version["rounds"].append({"round_id": round_row["round_id"],
                                      "status": round_row["status"],
                                      "fund": fund["categories"],
                                      "completed": outcome["completed"],
                                      "completion_rate": outcome["completion_rate"],
                                      "fairness": fairness})
        return {"versions": result}
