"""普惠支持资格、配额与成效跟踪的领域服务。

在数字贸易基础服务的稳定边界上扩展：申请主体关联去重、评审回避、
冻结政策版本下的可复核排序、限时预留转正式承诺、分期额度、里程碑与
效果指标核验、退出释放与稳定候补、独立会签特批以及按版本的成效比较。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from digital_trade_foundation.audit import append_event, canonical_json, digest
from digital_trade_foundation.errors import (ConflictError, NotFoundError,
                                             PermissionDenied, ValidationError)
from digital_trade_foundation.models import WriteReceipt
from digital_trade_foundation.service import DomainService
from digital_trade_foundation.storage import Database

from .policy import compute_affiliation_groups, score_application, validate_config
from .schema import SUPPORT_SCHEMA


TERMINAL_STATUSES = ("completed", "rejected", "deduplicated", "expired", "withdrawn", "exited")


class SupportService(DomainService):
    """在基础服务之上协调普惠支持的资格、配额与成效规则。"""

    def __init__(self, database: Database, clock=None) -> None:
        super().__init__(database, clock)
        self.database.connection.executescript(SUPPORT_SCHEMA)

    # ---------- 基础工具 ----------

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _stamp(self) -> str:
        return self._iso(self.clock.now())

    def _idempotent_result(self, connection, *, request_id: str, action: str,
                           payload: dict[str, Any],
                           create: Callable[[], tuple[str, str, dict[str, Any]]]) -> tuple[WriteReceipt, dict[str, Any]]:
        """与基础服务相同的幂等规则，但把业务响应一并回放。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True), json.loads(row["response_json"])
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._stamp()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False), response

    def _flag(self, value: Any, field: str) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"{field} 必须是布尔值")
        return value

    def _amount(self, value: Any, field: str, *, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if allow_zero:
            if value < 0:
                raise ValidationError(f"{field} 不能为负数")
        elif value <= 0:
            raise ValidationError(f"{field} 必须大于 0")
        return value

    def _obligations(self, value: Any) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValidationError("exit_obligations 必须是非空字符串列表")
        result = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValidationError("exit_obligations 必须是非空字符串列表")
            result.append(item.strip())
        return result

    # ---------- 行读取 ----------

    def _policy_row(self, connection, policy_id: str):
        row = connection.execute("SELECT * FROM support_policies WHERE policy_id=?", (policy_id,)).fetchone()
        if row is None:
            raise NotFoundError("政策版本不存在")
        return row

    def _application_row(self, connection, application_id: str):
        row = connection.execute("SELECT * FROM support_applications WHERE application_id=?", (application_id,)).fetchone()
        if row is None:
            raise NotFoundError("申请不存在")
        return row

    def _config(self, policy_row) -> dict[str, Any]:
        return json.loads(policy_row["config_json"])

    # ---------- 预算台账 ----------

    def _available(self, connection, policy_id: str) -> int:
        """可用额度 = 总预算 - 生效预留 - 承诺未释放部分（含已拨付）。"""

        total = connection.execute("SELECT total_budget FROM support_policies WHERE policy_id=?",
                                   (policy_id,)).fetchone()["total_budget"]
        reserved = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS value FROM support_reservations WHERE policy_id=? AND status='active'",
            (policy_id,)).fetchone()["value"]
        committed = connection.execute(
            "SELECT COALESCE(SUM(amount - released_back),0) AS value FROM support_commitments WHERE policy_id=?",
            (policy_id,)).fetchone()["value"]
        return total - reserved - committed

    def _ledger(self, connection, *, policy_id: str, application_id: str | None, kind: str,
                amount: int, note: str, actor_id: str, now: str) -> None:
        connection.execute(
            "INSERT INTO support_budget_ledger(entry_id,policy_id,application_id,kind,amount,available_after,note,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, policy_id, application_id, kind, amount,
             self._available(connection, policy_id), note, actor_id, now),
        )

    def _decision(self, connection, *, policy_id: str, application_id: str, decision: str,
                  reason: dict[str, Any], actor_id: str, now: str) -> None:
        connection.execute(
            "INSERT INTO support_decisions(policy_id,application_id,decision,reason_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (policy_id, application_id, decision, canonical_json(reason), actor_id, now),
        )

    def _set_status(self, connection, application_id: str, status: str, now: str,
                    obligations: list[str] | None = None) -> None:
        if obligations is None:
            connection.execute("UPDATE support_applications SET status=?, updated_at=? WHERE application_id=?",
                               (status, now, application_id))
        else:
            connection.execute(
                "UPDATE support_applications SET status=?, exit_obligations_json=?, updated_at=? WHERE application_id=?",
                (status, canonical_json(obligations), now, application_id))

    # ---------- 关联组 ----------

    def _policy_groups(self, connection, policy_id: str) -> dict[str, str]:
        applicant_ids = [row["applicant_id"] for row in connection.execute(
            "SELECT DISTINCT applicant_id FROM support_applications WHERE policy_id=?", (policy_id,))]
        if not applicant_ids:
            return {}
        placeholders = ",".join("?" for _ in applicant_ids)
        pairs = [(row["applicant_id"], row["related_key"]) for row in connection.execute(
            f"SELECT applicant_id, related_key FROM support_affiliations WHERE applicant_id IN ({placeholders})",
            applicant_ids)]
        return compute_affiliation_groups(applicant_ids, pairs)

    def _held_groups(self, connection, policy_id: str, groups: dict[str, str]) -> set[str]:
        rows = connection.execute(
            "SELECT applicant_id FROM support_applications a WHERE a.policy_id=? AND ("
            "EXISTS(SELECT 1 FROM support_reservations r WHERE r.application_id=a.application_id AND r.status='active') OR "
            "EXISTS(SELECT 1 FROM support_commitments c WHERE c.application_id=a.application_id AND c.status='active'))",
            (policy_id,)).fetchall()
        return {groups[row["applicant_id"]] for row in rows if row["applicant_id"] in groups}

    # ---------- 预留与释放 ----------

    def _create_reservation(self, connection, *, policy_id: str, application_id: str, amount: int,
                            source: str, ttl_hours: int, now_dt: datetime, actor_id: str) -> None:
        now = self._iso(now_dt)
        expires_at = self._iso(now_dt + timedelta(hours=ttl_hours))
        connection.execute(
            "INSERT INTO support_reservations(application_id,policy_id,amount,source,status,expires_at,created_at) "
            "VALUES(?,?,?,?,'active',?,?)",
            (application_id, policy_id, amount, source, expires_at, now))
        self._ledger(connection, policy_id=policy_id, application_id=application_id, kind="reserved",
                     amount=-amount, note=f"限时预留({source})", actor_id=actor_id, now=now)

    def _release_reservation(self, connection, reservation, *, new_status: str, reason: str,
                             obligations: list[str] | None, actor_id: str, now: str) -> None:
        application_id = reservation["application_id"]
        connection.execute("UPDATE support_reservations SET status='released', resolved_at=? WHERE application_id=?",
                           (now, application_id))
        self._set_status(connection, application_id, new_status, now, obligations)
        self._ledger(connection, policy_id=reservation["policy_id"], application_id=application_id,
                     kind="reservation_released", amount=reservation["amount"], note=reason,
                     actor_id=actor_id, now=now)
        self._decision(connection, policy_id=reservation["policy_id"], application_id=application_id,
                       decision=new_status, reason={"reason": reason, "released_amount": reservation["amount"]},
                       actor_id=actor_id, now=now)
        append_event(connection, actor_id=actor_id, action="support.reservation.released",
                     resource_type="support_application", resource_id=application_id,
                     detail={"reason": reason, "amount": reservation["amount"]}, occurred_at=now)

    def _release_commitment_unfulfilled(self, connection, commitment, *, new_status: str, reason: str,
                                        obligations: list[str], actor_id: str, now: str) -> int:
        application_id = commitment["application_id"]
        pending = connection.execute(
            "SELECT COALESCE(SUM(amount),0) AS value FROM support_installments WHERE application_id=? AND status='pending'",
            (application_id,)).fetchone()["value"]
        connection.execute("UPDATE support_installments SET status='cancelled' WHERE application_id=? AND status='pending'",
                           (application_id,))
        connection.execute(
            "UPDATE support_commitments SET released_back=released_back+?, status='exited', closed_at=? WHERE application_id=?",
            (pending, now, application_id))
        self._set_status(connection, application_id, new_status, now, obligations)
        self._ledger(connection, policy_id=commitment["policy_id"], application_id=application_id,
                     kind="commitment_released", amount=pending, note=reason, actor_id=actor_id, now=now)
        self._decision(connection, policy_id=commitment["policy_id"], application_id=application_id,
                       decision=new_status,
                       reason={"reason": reason, "released_unfulfilled": pending, "obligations": obligations},
                       actor_id=actor_id, now=now)
        append_event(connection, actor_id=actor_id, action="support.commitment.released",
                     resource_type="support_application", resource_id=application_id,
                     detail={"reason": reason, "released_unfulfilled": pending}, occurred_at=now)
        return pending

    def _sweep_expired(self, connection, now_dt: datetime) -> None:
        """把到期的限时预留释放，并沿稳定候补继续分配。"""

        now = self._iso(now_dt)
        rows = connection.execute(
            "SELECT * FROM support_reservations WHERE status='active' AND expires_at<=?", (now,)).fetchall()
        policies = set()
        for reservation in rows:
            self._release_reservation(connection, reservation, new_status="expired",
                                      reason="预留逾期未转化", obligations=None, actor_id="system", now=now)
            policies.add(reservation["policy_id"])
        for policy_id in sorted(policies):
            policy = self._policy_row(connection, policy_id)
            self._promote_waitlist(connection, policy, now_dt, actor_id="system")

    def _promote_waitlist(self, connection, policy, now_dt: datetime, actor_id: str) -> list[str]:
        """按冻结排序产生的稳定候补顺序，把释放出的额度继续分配。"""

        policy_id = policy["policy_id"]
        config = self._config(policy)
        available = self._available(connection, policy_id)
        if available <= 0:
            return []
        rows = connection.execute(
            "SELECT a.*, r.score AS score FROM support_applications a "
            "JOIN support_rankings r ON r.policy_id=a.policy_id AND r.application_id=a.application_id "
            "WHERE a.policy_id=? AND a.status='waitlisted' "
            "ORDER BY r.score DESC, a.created_at, a.application_id",
            (policy_id,)).fetchall()
        if not rows:
            return []
        groups = self._policy_groups(connection, policy_id)
        held = self._held_groups(connection, policy_id, groups)
        now = self._iso(now_dt)
        promoted = []
        for row in rows:
            group = groups[row["applicant_id"]]
            if group in held or row["requested_amount"] > available:
                continue
            self._create_reservation(connection, policy_id=policy_id, application_id=row["application_id"],
                                     amount=row["requested_amount"], source="promotion",
                                     ttl_hours=config["reservation_ttl_hours"], now_dt=now_dt, actor_id=actor_id)
            self._set_status(connection, row["application_id"], "reserved", now)
            self._decision(connection, policy_id=policy_id, application_id=row["application_id"],
                           decision="allocated",
                           reason={"source": "promotion", "score": row["score"], "amount": row["requested_amount"]},
                           actor_id=actor_id, now=now)
            append_event(connection, actor_id=actor_id, action="support.reservation.promoted",
                         resource_type="support_application", resource_id=row["application_id"],
                         detail={"amount": row["requested_amount"], "score": row["score"]}, occurred_at=now)
            held.add(group)
            available -= row["requested_amount"]
            promoted.append(row["application_id"])
        return promoted

    # ---------- 政策版本 ----------

    def create_policy(self, *, request_id: str, actor_id: str, policy_id: str, name: str,
                      config: dict[str, Any]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "policy_id": policy_id, "name": name, "config": config}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            policy_id = self._identifier(policy_id, "policy_id")
            name = self._text(name, "name")
            normalized = validate_config(config)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO support_policies(policy_id,name,status,config_json,total_budget,created_by,created_at) "
                        "VALUES(?,?,'draft',?,?,?,?)",
                        (policy_id, name, canonical_json(normalized), normalized["total_budget"],
                         actor_id, self._stamp()))
                except Exception as exc:
                    raise ConflictError("政策版本编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="support.policy.created",
                             resource_type="support_policy", resource_id=policy_id,
                             detail={"name": name, "total_budget": normalized["total_budget"]},
                             occurred_at=self._stamp())
                return "support_policy", policy_id, {"policy_id": policy_id, "status": "draft", "config": normalized}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.create_policy", payload=payload, create=create)

    def freeze_policy(self, *, request_id: str, actor_id: str, policy_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "policy_id": policy_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            policy = self._policy_row(connection, policy_id)
            if policy["status"] != "draft":
                raise ConflictError("只有草稿状态的政策版本可以冻结")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                connection.execute("UPDATE support_policies SET status='frozen', frozen_at=? WHERE policy_id=?",
                                   (now, policy_id))
                append_event(connection, actor_id=actor_id, action="support.policy.frozen",
                             resource_type="support_policy", resource_id=policy_id,
                             detail={"config_hash": digest(json.loads(policy["config_json"]))}, occurred_at=now)
                return "support_policy", policy_id, {"policy_id": policy_id, "status": "frozen", "frozen_at": now}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.freeze_policy", payload=payload, create=create)

    def cut_budget(self, *, request_id: str, actor_id: str, policy_id: str,
                   new_total_budget: int) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "policy_id": policy_id, "new_total_budget": new_total_budget}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            self._sweep_expired(connection, now_dt)
            policy = self._policy_row(connection, policy_id)
            if policy["status"] != "frozen":
                raise ConflictError("只有冻结的政策版本才能缩减预算")
            new_total_budget = self._amount(new_total_budget, "new_total_budget", allow_zero=True)
            total = policy["total_budget"]
            if new_total_budget >= total:
                raise ValidationError("新预算必须低于当前预算")
            reserved_active = connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS value FROM support_reservations WHERE policy_id=? AND status='active'",
                (policy_id,)).fetchone()["value"]
            committed_outstanding = connection.execute(
                "SELECT COALESCE(SUM(amount - released_back),0) AS value FROM support_commitments WHERE policy_id=?",
                (policy_id,)).fetchone()["value"]
            pending_installments = connection.execute(
                "SELECT COALESCE(SUM(i.amount),0) AS value FROM support_installments i "
                "JOIN support_commitments c ON c.application_id=i.application_id "
                "WHERE c.policy_id=? AND i.status='pending'", (policy_id,)).fetchone()["value"]
            need = reserved_active + committed_outstanding - new_total_budget
            if need > reserved_active + pending_installments:
                raise ValidationError("缩减幅度超过可释放的未兑现部分")

            def create() -> tuple[str, str, dict[str, Any]]:
                released: list[dict[str, Any]] = []
                remaining = need
                if remaining > 0:
                    order = ("ORDER BY COALESCE(k.score,-1) ASC, a.created_at DESC, a.application_id DESC")
                    reservations = connection.execute(
                        "SELECT r.* FROM support_reservations r "
                        "JOIN support_applications a ON a.application_id=r.application_id "
                        "LEFT JOIN support_rankings k ON k.policy_id=a.policy_id AND k.application_id=a.application_id "
                        f"WHERE r.policy_id=? AND r.status='active' {order}", (policy_id,)).fetchall()
                    for reservation in reservations:
                        if remaining <= 0:
                            break
                        self._release_reservation(connection, reservation, new_status="exited",
                                                  reason="预算缩减释放未兑现预留", obligations=["budget_cut"],
                                                  actor_id=actor_id, now=now)
                        released.append({"application_id": reservation["application_id"],
                                         "released": reservation["amount"], "kind": "reservation"})
                        remaining -= reservation["amount"]
                    if remaining > 0:
                        commitments = connection.execute(
                            "SELECT c.* FROM support_commitments c "
                            "JOIN support_applications a ON a.application_id=c.application_id "
                            "LEFT JOIN support_rankings k ON k.policy_id=a.policy_id AND k.application_id=a.application_id "
                            f"WHERE c.policy_id=? AND c.status='active' {order}", (policy_id,)).fetchall()
                        for commitment in commitments:
                            if remaining <= 0:
                                break
                            freed = self._release_commitment_unfulfilled(
                                connection, commitment, new_status="exited",
                                reason="预算缩减释放未兑现分期", obligations=["budget_cut"],
                                actor_id=actor_id, now=now)
                            released.append({"application_id": commitment["application_id"],
                                             "released": freed, "kind": "commitment"})
                            remaining -= freed
                connection.execute("UPDATE support_policies SET total_budget=? WHERE policy_id=?",
                                   (new_total_budget, policy_id))
                self._ledger(connection, policy_id=policy_id, application_id=None, kind="budget_cut",
                             amount=-(total - new_total_budget), note="预算缩减",
                             actor_id=actor_id, now=now)
                append_event(connection, actor_id=actor_id, action="support.policy.budget_cut",
                             resource_type="support_policy", resource_id=policy_id,
                             detail={"old_total": total, "new_total": new_total_budget, "released": released},
                             occurred_at=now)
                return "support_policy", policy_id, {"policy_id": policy_id, "new_total_budget": new_total_budget,
                                                     "released": released}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.cut_budget", payload=payload, create=create)

    # ---------- 主体、服务商与回避 ----------

    def register_applicant(self, *, request_id: str, actor_id: str, applicant_id: str, name: str,
                           region_id: str, affiliations: list[dict[str, str]] | None = None
                           ) -> tuple[WriteReceipt, dict[str, Any]]:
        affiliations = affiliations or []
        payload = {"actor_id": actor_id, "applicant_id": applicant_id, "name": name,
                   "region_id": region_id, "affiliations": affiliations}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            applicant_id = self._identifier(applicant_id, "applicant_id")
            name = self._text(name, "name")
            region_id = self._identifier(region_id, "region_id")
            normalized = []
            for item in affiliations:
                if not isinstance(item, dict):
                    raise ValidationError("affiliations 必须是对象列表")
                normalized.append({
                    "related_key": self._text(str(item.get("related_key", "")), "related_key"),
                    "relation": self._text(str(item.get("relation", "")), "relation", 80),
                })
            if len({item["related_key"] for item in normalized}) != len(normalized):
                raise ValidationError("affiliations 存在重复受益关系键")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                try:
                    connection.execute(
                        "INSERT INTO support_applicants(applicant_id,name,region_id,created_by,created_at) VALUES(?,?,?,?,?)",
                        (applicant_id, name, region_id, actor_id, now))
                except Exception as exc:
                    raise ConflictError("申请主体编号已经存在") from exc
                for item in normalized:
                    connection.execute(
                        "INSERT INTO support_affiliations(applicant_id,related_key,relation,created_at) VALUES(?,?,?,?)",
                        (applicant_id, item["related_key"], item["relation"], now))
                append_event(connection, actor_id=actor_id, action="support.applicant.registered",
                             resource_type="support_applicant", resource_id=applicant_id,
                             detail={"name": name, "region_id": region_id, "affiliations": normalized},
                             occurred_at=now)
                return "support_applicant", applicant_id, {"applicant_id": applicant_id}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.register_applicant", payload=payload, create=create)

    def register_provider(self, *, request_id: str, actor_id: str, provider_id: str,
                          name: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "provider_id": provider_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            provider_id = self._identifier(provider_id, "provider_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute("INSERT INTO support_providers(provider_id,name,created_by,created_at) VALUES(?,?,?,?)",
                                       (provider_id, name, actor_id, self._stamp()))
                except Exception as exc:
                    raise ConflictError("服务商编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="support.provider.registered",
                             resource_type="support_provider", resource_id=provider_id,
                             detail={"name": name}, occurred_at=self._stamp())
                return "support_provider", provider_id, {"provider_id": provider_id}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.register_provider", payload=payload, create=create)

    def declare_conflict(self, *, request_id: str, actor_id: str, provider_id: str, reason: str,
                         reviewer_id: str | None = None) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "provider_id": provider_id, "reason": reason, "reviewer_id": reviewer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            target = reviewer_id or actor_id
            if actor.role == "reviewer" and target != actor_id:
                raise PermissionDenied("评估人员只能申报本人的利益冲突")
            reviewer = self._actor(connection, target)
            if reviewer.role != "reviewer":
                raise ValidationError("被申报人必须是评估人员角色")
            if connection.execute("SELECT 1 FROM support_providers WHERE provider_id=?",
                                  (provider_id,)).fetchone() is None:
                raise NotFoundError("服务商不存在")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO support_reviewer_conflicts(reviewer_id,provider_id,reason,declared_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (target, provider_id, reason, actor_id, self._stamp()))
                except Exception as exc:
                    raise ConflictError("该评估人员与服务商的冲突已经登记") from exc
                append_event(connection, actor_id=actor_id, action="support.conflict.declared",
                             resource_type="support_provider", resource_id=provider_id,
                             detail={"reviewer_id": target, "reason": reason}, occurred_at=self._stamp())
                return "support_provider", provider_id, {"provider_id": provider_id, "reviewer_id": target}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.declare_conflict", payload=payload, create=create)

    def _check_recusal(self, connection, application, reviewer_id: str) -> None:
        """评审回避：与服务商存在利益关系或本人经办的评估人员不得评审。"""

        if application["created_by"] == reviewer_id:
            raise PermissionDenied("不能评审本人提交的申请")
        conflict = connection.execute(
            "SELECT 1 FROM support_reviewer_conflicts WHERE reviewer_id=? AND provider_id=?",
            (reviewer_id, application["provider_id"])).fetchone()
        if conflict:
            raise PermissionDenied("评估人员与该申请的服务商存在利益关系，应当回避")

    # ---------- 申请与评审 ----------

    def submit_application(self, *, request_id: str, actor_id: str, application_id: str, policy_id: str,
                           applicant_id: str, provider_id: str, category: str,
                           baseline: dict[str, Any], requested_amount: int) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "policy_id": policy_id,
                   "applicant_id": applicant_id, "provider_id": provider_id, "category": category,
                   "baseline": baseline, "requested_amount": requested_amount}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            application_id = self._identifier(application_id, "application_id")
            policy = self._policy_row(connection, policy_id)
            if policy["status"] not in ("draft", "frozen"):
                raise ConflictError("政策版本已关闭，不能申报")
            config = self._config(policy)
            applicant = connection.execute("SELECT * FROM support_applicants WHERE applicant_id=?",
                                           (applicant_id,)).fetchone()
            if applicant is None:
                raise NotFoundError("申请主体不存在")
            if connection.execute("SELECT 1 FROM support_providers WHERE provider_id=?",
                                  (provider_id,)).fetchone() is None:
                raise NotFoundError("服务商不存在")
            if category not in config["category_weights"]:
                raise ValidationError("支持类别不在政策范围内")
            region_id = applicant["region_id"]
            if region_id not in config["region_priorities"]:
                raise ValidationError("申请主体所在地区不在政策优先级范围内")
            requested_amount = self._amount(requested_amount, "requested_amount")
            if requested_amount > config["max_amount_per_application"]:
                raise ValidationError("申请额度超过单申请上限")
            if not isinstance(baseline, dict):
                raise ValidationError("baseline 必须是对象")
            for metric in config["baseline_metrics"]:
                value = baseline.get(metric)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                    raise ValidationError(f"baseline.{metric} 必须是 0 到 100 的数值")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                try:
                    connection.execute(
                        "INSERT INTO support_applications(application_id,policy_id,applicant_id,provider_id,category,"
                        "region_id,baseline_json,requested_amount,status,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?, 'submitted',?,?,?)",
                        (application_id, policy_id, applicant_id, provider_id, category, region_id,
                         canonical_json(baseline), requested_amount, actor_id, now, now))
                except Exception as exc:
                    raise ConflictError("申请编号已存在或该主体在本政策版本下已申报") from exc
                append_event(connection, actor_id=actor_id, action="support.application.submitted",
                             resource_type="support_application", resource_id=application_id,
                             detail={"policy_id": policy_id, "applicant_id": applicant_id, "provider_id": provider_id,
                                     "category": category, "region_id": region_id,
                                     "requested_amount": requested_amount}, occurred_at=now)
                return "support_application", application_id, {"application_id": application_id,
                                                               "status": "submitted"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.submit_application", payload=payload, create=create)

    def submit_material(self, *, request_id: str, actor_id: str, application_id: str, material_key: str,
                        payload_data: dict[str, Any]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "material_key": material_key,
                   "payload_data": payload_data}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            application = self._application_row(connection, application_id)
            if application["status"] not in ("submitted", "accepted", "waitlisted", "reserved"):
                raise ConflictError("当前状态不能补充材料")
            material_key = self._identifier(material_key, "material_key")
            if not isinstance(payload_data, dict) or not payload_data:
                raise ValidationError("payload_data 必须是非空对象")
            data_hash = digest(payload_data)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM support_materials WHERE application_id=? AND material_key=?",
                    (application_id, material_key)).fetchone()
                if existing:
                    if existing["payload_hash"] != data_hash:
                        raise ConflictError("同一材料键已经提交不同内容")
                    return "support_application", application_id, {"application_id": application_id,
                                                                   "material_key": material_key}
                connection.execute(
                    "INSERT INTO support_materials(application_id,material_key,payload_json,payload_hash,submitted_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (application_id, material_key, canonical_json(payload_data), data_hash, actor_id, self._stamp()))
                append_event(connection, actor_id=actor_id, action="support.material.submitted",
                             resource_type="support_application", resource_id=application_id,
                             detail={"material_key": material_key, "payload_hash": data_hash},
                             occurred_at=self._stamp())
                return "support_application", application_id, {"application_id": application_id,
                                                               "material_key": material_key}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.submit_material", payload=payload, create=create)

    def review_application(self, *, request_id: str, actor_id: str, application_id: str, decision: str,
                           note: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            application = self._application_row(connection, application_id)
            self._check_recusal(connection, application, actor_id)
            if application["status"] != "submitted":
                raise ConflictError("申请不在待评审状态")
            if decision not in ("approve", "reject"):
                raise ValidationError("decision 必须是 approve 或 reject")
            note = self._text(note, "note", 500)
            policy = self._policy_row(connection, application["policy_id"])
            config = self._config(policy)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                try:
                    connection.execute(
                        "INSERT INTO support_reviews(application_id,reviewer_id,decision,note,created_at) VALUES(?,?,?,?,?)",
                        (application_id, actor_id, decision, note, now))
                except Exception as exc:
                    raise ConflictError("该评估人员已经评审过此申请") from exc
                status = "submitted"
                if decision == "reject":
                    status = "rejected"
                    self._set_status(connection, application_id, "rejected", now)
                    self._decision(connection, policy_id=application["policy_id"], application_id=application_id,
                                   decision="rejected_review", reason={"note": note, "reviewer_id": actor_id},
                                   actor_id=actor_id, now=now)
                else:
                    approvals = connection.execute(
                        "SELECT COUNT(*) AS count FROM support_reviews WHERE application_id=? AND decision='approve'",
                        (application_id,)).fetchone()["count"]
                    if approvals >= config["min_reviews"]:
                        status = "accepted"
                        self._set_status(connection, application_id, "accepted", now)
                append_event(connection, actor_id=actor_id, action="support.application.reviewed",
                             resource_type="support_application", resource_id=application_id,
                             detail={"decision": decision, "note": note, "status": status}, occurred_at=now)
                return "support_application", application_id, {"application_id": application_id, "status": status}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.review_application", payload=payload, create=create)

    # ---------- 排序与预留 ----------

    def run_ranking(self, *, request_id: str, actor_id: str, policy_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "policy_id": policy_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            self._sweep_expired(connection, now_dt)
            policy = self._policy_row(connection, policy_id)
            if policy["status"] != "frozen":
                raise ConflictError("只有冻结的政策版本才能产生排序")
            config = self._config(policy)

            def create() -> tuple[str, str, dict[str, Any]]:
                run_id = uuid.uuid4().hex
                rows = connection.execute(
                    "SELECT * FROM support_applications WHERE policy_id=? AND status IN ('accepted','waitlisted')",
                    (policy_id,)).fetchall()
                groups = self._policy_groups(connection, policy_id)
                scored = []
                for row in rows:
                    score, reasons = score_application(config, region_id=row["region_id"],
                                                       category=row["category"],
                                                       baseline=json.loads(row["baseline_json"]))
                    scored.append({"row": row, "score": score, "reasons": reasons})
                scored.sort(key=lambda item: (-item["score"], item["row"]["created_at"], item["row"]["application_id"]))
                for position, item in enumerate(scored, start=1):
                    item["position"] = position
                    reasons = {**item["reasons"], "affiliation_group": groups[item["row"]["applicant_id"]]}
                    connection.execute(
                        "INSERT OR REPLACE INTO support_rankings(policy_id,application_id,run_id,score,position,reasons_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (policy_id, item["row"]["application_id"], run_id, item["score"], position,
                         canonical_json(reasons), now))
                deduplicated: list[str] = []
                survivors = []
                best_by_group: dict[str, dict[str, Any]] = {}
                for item in scored:
                    group = groups[item["row"]["applicant_id"]]
                    if group not in best_by_group:
                        best_by_group[group] = item
                        survivors.append(item)
                        continue
                    winner = best_by_group[group]
                    application_id = item["row"]["application_id"]
                    self._set_status(connection, application_id, "deduplicated", now)
                    self._decision(connection, policy_id=policy_id, application_id=application_id,
                                   decision="affiliation_deduplicated",
                                   reason={"affiliation_group": group,
                                           "winner_application_id": winner["row"]["application_id"],
                                           "score": item["score"], "winner_score": winner["score"]},
                                   actor_id=actor_id, now=now)
                    append_event(connection, actor_id=actor_id, action="support.application.deduplicated",
                                 resource_type="support_application", resource_id=application_id,
                                 detail={"affiliation_group": group,
                                         "winner_application_id": winner["row"]["application_id"]},
                                 occurred_at=now)
                    deduplicated.append(application_id)
                available = self._available(connection, policy_id)
                held = self._held_groups(connection, policy_id, groups)
                allocated: list[str] = []
                waitlisted: list[str] = []
                for item in survivors:
                    row = item["row"]
                    application_id = row["application_id"]
                    group = groups[row["applicant_id"]]
                    if group in held:
                        self._set_status(connection, application_id, "deduplicated", now)
                        self._decision(connection, policy_id=policy_id, application_id=application_id,
                                       decision="affiliation_deduplicated",
                                       reason={"affiliation_group": group, "held_by_existing": True,
                                               "score": item["score"]},
                                       actor_id=actor_id, now=now)
                        deduplicated.append(application_id)
                        continue
                    amount = row["requested_amount"]
                    if amount <= available:
                        self._create_reservation(connection, policy_id=policy_id, application_id=application_id,
                                                 amount=amount, source="ranking",
                                                 ttl_hours=config["reservation_ttl_hours"],
                                                 now_dt=now_dt, actor_id=actor_id)
                        self._set_status(connection, application_id, "reserved", now)
                        self._decision(connection, policy_id=policy_id, application_id=application_id,
                                       decision="allocated",
                                       reason={"source": "ranking", "score": item["score"],
                                               "position": item["position"], "amount": amount},
                                       actor_id=actor_id, now=now)
                        held.add(group)
                        available -= amount
                        allocated.append(application_id)
                    else:
                        self._set_status(connection, application_id, "waitlisted", now)
                        self._decision(connection, policy_id=policy_id, application_id=application_id,
                                       decision="waitlisted",
                                       reason={"score": item["score"], "position": item["position"],
                                               "requested_amount": amount, "available": available},
                                       actor_id=actor_id, now=now)
                        waitlisted.append(application_id)
                append_event(connection, actor_id=actor_id, action="support.policy.ranked",
                             resource_type="support_policy", resource_id=policy_id,
                             detail={"run_id": run_id, "ranked": len(scored), "allocated": allocated,
                                     "waitlisted": waitlisted, "deduplicated": deduplicated},
                             occurred_at=now)
                return "support_policy", policy_id, {"policy_id": policy_id, "run_id": run_id,
                                                     "ranked": len(scored), "allocated": allocated,
                                                     "waitlisted": waitlisted, "deduplicated": deduplicated}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.run_ranking", payload=payload, create=create)

    def commit_reservation(self, *, request_id: str, actor_id: str,
                           application_id: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            self._sweep_expired(connection, now_dt)
            application = self._application_row(connection, application_id)
            if application["status"] != "reserved":
                raise ConflictError("申请不在限时预留状态")
            reservation = connection.execute(
                "SELECT * FROM support_reservations WHERE application_id=? AND status='active'",
                (application_id,)).fetchone()
            if reservation is None:
                raise ConflictError("限时预留已失效")
            policy = self._policy_row(connection, application["policy_id"])
            config = self._config(policy)
            materials = {row["material_key"] for row in connection.execute(
                "SELECT material_key FROM support_materials WHERE application_id=?", (application_id,))}
            missing_materials = [key for key in config["required_materials"] if key not in materials]
            if missing_materials:
                raise ConflictError("必要材料未补齐: " + ",".join(missing_materials))
            verified = {row["milestone_key"] for row in connection.execute(
                "SELECT milestone_key FROM support_milestones WHERE application_id=? AND status='verified'",
                (application_id,))}
            missing_milestones = [entry["key"] for entry in config["prerequisite_milestones"]
                                  if entry["key"] not in verified]
            if missing_milestones:
                raise ConflictError("前置里程碑未核验通过: " + ",".join(missing_milestones))

            def create() -> tuple[str, str, dict[str, Any]]:
                amount = reservation["amount"]
                connection.execute(
                    "UPDATE support_reservations SET status='converted', resolved_at=? WHERE application_id=?",
                    (now, application_id))
                connection.execute(
                    "INSERT INTO support_commitments(application_id,policy_id,amount,released_back,status,created_at) "
                    "VALUES(?,?,?,0,'active',?)",
                    (application_id, application["policy_id"], amount, now))
                installments = []
                accumulated = 0
                plan = config["installment_plan"]
                for index, entry in enumerate(plan, start=1):
                    if index < len(plan):
                        value = amount * entry["percent"] // 100
                        accumulated += value
                    else:
                        value = amount - accumulated
                    connection.execute(
                        "INSERT INTO support_installments(application_id,installment_no,milestone_key,amount,status) "
                        "VALUES(?,?,?,?,'pending')",
                        (application_id, index, entry["milestone_key"], value))
                    installments.append({"installment_no": index, "milestone_key": entry["milestone_key"],
                                         "amount": value, "status": "pending"})
                self._set_status(connection, application_id, "committed", now)
                self._ledger(connection, policy_id=application["policy_id"], application_id=application_id,
                             kind="committed", amount=0, note="限时预留转为正式承诺", actor_id=actor_id, now=now)
                self._decision(connection, policy_id=application["policy_id"], application_id=application_id,
                               decision="committed", reason={"amount": amount}, actor_id=actor_id, now=now)
                append_event(connection, actor_id=actor_id, action="support.commitment.created",
                             resource_type="support_application", resource_id=application_id,
                             detail={"amount": amount, "installments": installments}, occurred_at=now)
                for installment in installments:
                    self._release_installment_if_verified(connection, application, installment["milestone_key"],
                                                          actor_id, now)
                self._maybe_complete(connection, application_id, actor_id, now)
                final_installments = [
                    {"installment_no": row["installment_no"], "milestone_key": row["milestone_key"],
                     "amount": row["amount"], "status": row["status"]}
                    for row in connection.execute(
                        "SELECT * FROM support_installments WHERE application_id=? ORDER BY installment_no",
                        (application_id,))]
                return "support_commitment", application_id, {"application_id": application_id,
                                                              "commitment_amount": amount,
                                                              "installments": final_installments}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.commit_reservation", payload=payload, create=create)

    # ---------- 里程碑与分期 ----------

    def _milestone_kinds(self, config: dict[str, Any]) -> dict[str, str]:
        kinds = {entry["key"]: entry["kind"] for entry in config["prerequisite_milestones"]}
        kinds.update({entry["milestone_key"]: entry["kind"] for entry in config["installment_plan"]})
        return kinds

    def report_milestone(self, *, request_id: str, actor_id: str, application_id: str, milestone_key: str,
                         kind: str, evidence: dict[str, Any]) -> tuple[WriteReceipt, dict[str, Any]]:
        """服务商回调登记里程碑；重放同一请求或同一内容不会重复生效。"""

        payload = {"actor_id": actor_id, "application_id": application_id, "milestone_key": milestone_key,
                   "kind": kind, "evidence": evidence}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            now_dt = self.clock.now()
            self._sweep_expired(connection, now_dt)
            application = self._application_row(connection, application_id)
            if application["status"] not in ("reserved", "committed"):
                raise ConflictError("当前状态不能登记里程碑")
            policy = self._policy_row(connection, application["policy_id"])
            kinds = self._milestone_kinds(self._config(policy))
            if milestone_key not in kinds:
                raise ValidationError("里程碑不在政策范围内")
            if kind != kinds[milestone_key]:
                raise ValidationError("里程碑类型与政策定义不一致")
            if not isinstance(evidence, dict) or not evidence:
                raise ValidationError("evidence 必须是非空对象")
            evidence_hash = digest(evidence)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                existing = connection.execute(
                    "SELECT * FROM support_milestones WHERE application_id=? AND milestone_key=?",
                    (application_id, milestone_key)).fetchone()
                if existing:
                    if digest(json.loads(existing["evidence_json"])) == evidence_hash:
                        return "support_milestone", application_id, {
                            "application_id": application_id, "milestone_key": milestone_key,
                            "status": existing["status"]}
                    if existing["status"] != "rejected":
                        raise ConflictError("同一里程碑已经登记不同内容")
                    connection.execute(
                        "UPDATE support_milestones SET evidence_json=?, status='reported', reported_by=?, "
                        "verified_by=NULL, updated_at=? WHERE application_id=? AND milestone_key=?",
                        (canonical_json(evidence), actor_id, now, application_id, milestone_key))
                    append_event(connection, actor_id=actor_id, action="support.milestone.reported",
                                 resource_type="support_application", resource_id=application_id,
                                 detail={"milestone_key": milestone_key, "kind": kind, "resubmitted": True},
                                 occurred_at=now)
                    return "support_milestone", application_id, {
                        "application_id": application_id, "milestone_key": milestone_key, "status": "reported"}
                connection.execute(
                    "INSERT INTO support_milestones(application_id,milestone_key,kind,evidence_json,status,reported_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,'reported',?,?,?)",
                    (application_id, milestone_key, kind, canonical_json(evidence), actor_id, now, now))
                append_event(connection, actor_id=actor_id, action="support.milestone.reported",
                             resource_type="support_application", resource_id=application_id,
                             detail={"milestone_key": milestone_key, "kind": kind}, occurred_at=now)
                return "support_milestone", application_id, {
                    "application_id": application_id, "milestone_key": milestone_key, "status": "reported"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.report_milestone", payload=payload, create=create)

    def _release_installment_if_verified(self, connection, application, milestone_key: str,
                                         actor_id: str, now: str) -> None:
        milestone = connection.execute(
            "SELECT status FROM support_milestones WHERE application_id=? AND milestone_key=?",
            (application["application_id"], milestone_key)).fetchone()
        if milestone is None or milestone["status"] != "verified":
            return
        installment = connection.execute(
            "SELECT * FROM support_installments WHERE application_id=? AND milestone_key=? AND status='pending'",
            (application["application_id"], milestone_key)).fetchone()
        if installment is None:
            return
        connection.execute(
            "UPDATE support_installments SET status='released', released_at=? "
            "WHERE application_id=? AND installment_no=?",
            (now, application["application_id"], installment["installment_no"]))
        self._ledger(connection, policy_id=application["policy_id"], application_id=application["application_id"],
                     kind="installment_released", amount=installment["amount"],
                     note=f"分期拨付({milestone_key})", actor_id=actor_id, now=now)
        append_event(connection, actor_id=actor_id, action="support.installment.released",
                     resource_type="support_application", resource_id=application["application_id"],
                     detail={"installment_no": installment["installment_no"], "amount": installment["amount"],
                             "milestone_key": milestone_key}, occurred_at=now)

    def verify_milestone(self, *, request_id: str, actor_id: str, application_id: str, milestone_key: str,
                         approve: bool, note: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "milestone_key": milestone_key,
                   "approve": approve, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            approve = self._flag(approve, "approve")
            note = self._text(note, "note", 500)
            application = self._application_row(connection, application_id)
            self._check_recusal(connection, application, actor_id)
            milestone = connection.execute(
                "SELECT * FROM support_milestones WHERE application_id=? AND milestone_key=?",
                (application_id, milestone_key)).fetchone()
            if milestone is None:
                raise NotFoundError("里程碑不存在")
            if milestone["status"] != "reported":
                raise ConflictError("里程碑不在待核验状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                status = "verified" if approve else "rejected"
                connection.execute(
                    "UPDATE support_milestones SET status=?, verified_by=?, updated_at=? "
                    "WHERE application_id=? AND milestone_key=?",
                    (status, actor_id, now, application_id, milestone_key))
                append_event(connection, actor_id=actor_id, action="support.milestone.verified",
                             resource_type="support_application", resource_id=application_id,
                             detail={"milestone_key": milestone_key, "approve": approve, "note": note},
                             occurred_at=now)
                if approve:
                    self._release_installment_if_verified(connection, application, milestone_key, actor_id, now)
                    self._maybe_complete(connection, application_id, actor_id, now)
                return "support_milestone", application_id, {
                    "application_id": application_id, "milestone_key": milestone_key, "status": status}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.verify_milestone", payload=payload, create=create)

    # ---------- 效果指标 ----------

    def report_outcome(self, *, request_id: str, actor_id: str, application_id: str, metric_key: str,
                       value: float) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "metric_key": metric_key, "value": value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "operator")
            application = self._application_row(connection, application_id)
            if application["status"] != "committed":
                raise ConflictError("只有正式承诺中的申请可以登记效果指标")
            policy = self._policy_row(connection, application["policy_id"])
            config = self._config(policy)
            if metric_key not in config["required_outcome_metrics"]:
                raise ValidationError("效果指标不在政策范围内")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValidationError("value 必须是非负数值")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                existing = connection.execute(
                    "SELECT * FROM support_outcomes WHERE application_id=? AND metric_key=?",
                    (application_id, metric_key)).fetchone()
                if existing:
                    if float(existing["value"]) == float(value):
                        return "support_outcome", application_id, {
                            "application_id": application_id, "metric_key": metric_key,
                            "status": existing["status"]}
                    if existing["status"] != "rejected":
                        raise ConflictError("同一效果指标已经登记不同数值")
                    connection.execute(
                        "UPDATE support_outcomes SET value=?, status='reported', reported_by=?, verified_by=NULL "
                        "WHERE application_id=? AND metric_key=?",
                        (float(value), actor_id, application_id, metric_key))
                    append_event(connection, actor_id=actor_id, action="support.outcome.reported",
                                 resource_type="support_application", resource_id=application_id,
                                 detail={"metric_key": metric_key, "value": value, "resubmitted": True},
                                 occurred_at=now)
                    return "support_outcome", application_id, {
                        "application_id": application_id, "metric_key": metric_key, "status": "reported"}
                connection.execute(
                    "INSERT INTO support_outcomes(application_id,metric_key,value,status,reported_by,created_at) "
                    "VALUES(?,?,?,'reported',?,?)",
                    (application_id, metric_key, float(value), actor_id, now))
                append_event(connection, actor_id=actor_id, action="support.outcome.reported",
                             resource_type="support_application", resource_id=application_id,
                             detail={"metric_key": metric_key, "value": value}, occurred_at=now)
                return "support_outcome", application_id, {
                    "application_id": application_id, "metric_key": metric_key, "status": "reported"}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.report_outcome", payload=payload, create=create)

    def verify_outcome(self, *, request_id: str, actor_id: str, application_id: str, metric_key: str,
                       approve: bool) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "metric_key": metric_key,
                   "approve": approve}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            approve = self._flag(approve, "approve")
            application = self._application_row(connection, application_id)
            self._check_recusal(connection, application, actor_id)
            outcome = connection.execute(
                "SELECT * FROM support_outcomes WHERE application_id=? AND metric_key=?",
                (application_id, metric_key)).fetchone()
            if outcome is None:
                raise NotFoundError("效果指标不存在")
            if outcome["status"] != "reported":
                raise ConflictError("效果指标不在待核验状态")

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                status = "verified" if approve else "rejected"
                connection.execute(
                    "UPDATE support_outcomes SET status=?, verified_by=? WHERE application_id=? AND metric_key=?",
                    (status, actor_id, application_id, metric_key))
                append_event(connection, actor_id=actor_id, action="support.outcome.verified",
                             resource_type="support_application", resource_id=application_id,
                             detail={"metric_key": metric_key, "approve": approve}, occurred_at=now)
                if approve:
                    self._maybe_complete(connection, application_id, actor_id, now)
                return "support_outcome", application_id, {
                    "application_id": application_id, "metric_key": metric_key, "status": status}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.verify_outcome", payload=payload, create=create)

    def _maybe_complete(self, connection, application_id: str, actor_id: str, now: str) -> None:
        """全部分期拨付且效果指标核验通过时，支持进入完成状态且不再参与重排。"""

        commitment = connection.execute(
            "SELECT * FROM support_commitments WHERE application_id=? AND status='active'",
            (application_id,)).fetchone()
        if commitment is None:
            return
        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM support_installments WHERE application_id=? AND status='pending'",
            (application_id,)).fetchone()["count"]
        if pending:
            return
        application = self._application_row(connection, application_id)
        policy = self._policy_row(connection, application["policy_id"])
        config = self._config(policy)
        verified = {row["metric_key"] for row in connection.execute(
            "SELECT metric_key FROM support_outcomes WHERE application_id=? AND status='verified'",
            (application_id,))}
        if any(metric not in verified for metric in config["required_outcome_metrics"]):
            return
        connection.execute("UPDATE support_commitments SET status='completed', closed_at=? WHERE application_id=?",
                           (now, application_id))
        self._set_status(connection, application_id, "completed", now)
        self._decision(connection, policy_id=application["policy_id"], application_id=application_id,
                       decision="completed", reason={"amount": commitment["amount"]}, actor_id=actor_id, now=now)
        append_event(connection, actor_id=actor_id, action="support.application.completed",
                     resource_type="support_application", resource_id=application_id,
                     detail={"amount": commitment["amount"]}, occurred_at=now)

    # ---------- 放弃、退出与预算释放 ----------

    def withdraw_application(self, *, request_id: str, actor_id: str, application_id: str,
                             exit_obligations: list[str]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "exit_obligations": exit_obligations}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            self._sweep_expired(connection, now_dt)
            application = self._application_row(connection, application_id)
            if actor.role != "admin" and application["created_by"] != actor_id:
                raise PermissionDenied("只能撤回本人提交的申请")
            obligations = self._obligations(exit_obligations)
            status = application["status"]
            if status in TERMINAL_STATUSES:
                raise ConflictError("申请已处于终态，不能撤回")

            def create() -> tuple[str, str, dict[str, Any]]:
                released = 0
                reservation = connection.execute(
                    "SELECT * FROM support_reservations WHERE application_id=? AND status='active'",
                    (application_id,)).fetchone()
                if reservation is not None:
                    released = reservation["amount"]
                    self._release_reservation(connection, reservation, new_status="withdrawn",
                                              reason="申请方放弃", obligations=obligations,
                                              actor_id=actor_id, now=now)
                else:
                    commitment = connection.execute(
                        "SELECT * FROM support_commitments WHERE application_id=? AND status='active'",
                        (application_id,)).fetchone()
                    if commitment is not None:
                        released = self._release_commitment_unfulfilled(
                            connection, commitment, new_status="withdrawn",
                            reason="申请方放弃", obligations=obligations, actor_id=actor_id, now=now)
                    else:
                        self._set_status(connection, application_id, "withdrawn", now, obligations)
                        self._decision(connection, policy_id=application["policy_id"],
                                       application_id=application_id, decision="withdrawn",
                                       reason={"reason": "申请方放弃", "obligations": obligations},
                                       actor_id=actor_id, now=now)
                append_event(connection, actor_id=actor_id, action="support.application.withdrawn",
                             resource_type="support_application", resource_id=application_id,
                             detail={"obligations": obligations, "released": released}, occurred_at=now)
                promoted: list[str] = []
                if released:
                    policy = self._policy_row(connection, application["policy_id"])
                    promoted = self._promote_waitlist(connection, policy, now_dt, actor_id=actor_id)
                return "support_application", application_id, {"application_id": application_id,
                                                               "status": "withdrawn",
                                                               "released": released, "promoted": promoted}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.withdraw_application", payload=payload, create=create)

    def exit_application(self, *, request_id: str, actor_id: str, application_id: str, reason: str,
                         exit_obligations: list[str]) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "application_id": application_id, "reason": reason,
                   "exit_obligations": exit_obligations}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            self._sweep_expired(connection, now_dt)
            application = self._application_row(connection, application_id)
            if reason not in ("partial_failure", "provider_default", "budget_adjustment", "other"):
                raise ValidationError("reason 不在允许范围内")
            obligations = self._obligations(exit_obligations)
            status = application["status"]
            if status in TERMINAL_STATUSES:
                raise ConflictError("申请已处于终态，不能退出")

            def create() -> tuple[str, str, dict[str, Any]]:
                released = 0
                reservation = connection.execute(
                    "SELECT * FROM support_reservations WHERE application_id=? AND status='active'",
                    (application_id,)).fetchone()
                if reservation is not None:
                    released = reservation["amount"]
                    self._release_reservation(connection, reservation, new_status="exited",
                                              reason=reason, obligations=obligations,
                                              actor_id=actor_id, now=now)
                else:
                    commitment = connection.execute(
                        "SELECT * FROM support_commitments WHERE application_id=? AND status='active'",
                        (application_id,)).fetchone()
                    if commitment is not None:
                        released = self._release_commitment_unfulfilled(
                            connection, commitment, new_status="exited",
                            reason=reason, obligations=obligations, actor_id=actor_id, now=now)
                    else:
                        self._set_status(connection, application_id, "exited", now, obligations)
                        self._decision(connection, policy_id=application["policy_id"],
                                       application_id=application_id, decision="exited",
                                       reason={"reason": reason, "obligations": obligations},
                                       actor_id=actor_id, now=now)
                append_event(connection, actor_id=actor_id, action="support.application.exited",
                             resource_type="support_application", resource_id=application_id,
                             detail={"reason": reason, "obligations": obligations, "released": released},
                             occurred_at=now)
                promoted: list[str] = []
                if released:
                    policy = self._policy_row(connection, application["policy_id"])
                    promoted = self._promote_waitlist(connection, policy, now_dt, actor_id=actor_id)
                return "support_application", application_id, {"application_id": application_id,
                                                               "status": "exited", "released": released,
                                                               "promoted": promoted}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.exit_application", payload=payload, create=create)

    # ---------- 特批会签 ----------

    def _region_amounts(self, connection, policy_id: str, config: dict[str, Any]) -> dict[str, int]:
        amounts = {region_id: 0 for region_id in config["region_priorities"]}
        for row in connection.execute(
                "SELECT a.region_id AS region_id, COALESCE(SUM(r.amount),0) AS value "
                "FROM support_reservations r JOIN support_applications a ON a.application_id=r.application_id "
                "WHERE r.policy_id=? AND r.status='active' GROUP BY a.region_id", (policy_id,)):
            amounts[row["region_id"]] = amounts.get(row["region_id"], 0) + row["value"]
        for row in connection.execute(
                "SELECT a.region_id AS region_id, COALESCE(SUM(c.amount - c.released_back),0) AS value "
                "FROM support_commitments c JOIN support_applications a ON a.application_id=c.application_id "
                "WHERE c.policy_id=? GROUP BY a.region_id", (policy_id,)):
            amounts[row["region_id"]] = amounts.get(row["region_id"], 0) + row["value"]
        return amounts

    def _fairness_impact(self, connection, policy, config: dict[str, Any],
                       region_id: str, amount: int) -> dict[str, Any]:
        """计算特批对地区公平性的影响：各地区份额与最大份额差的变化。"""

        before_amounts = self._region_amounts(connection, policy["policy_id"], config)
        after_amounts = dict(before_amounts)
        after_amounts[region_id] = after_amounts.get(region_id, 0) + amount

        def shares(amounts: dict[str, int]) -> dict[str, float]:
            total = sum(amounts.values())
            if not total:
                return {region: 0.0 for region in amounts}
            return {region: round(value / total, 6) for region, value in amounts.items()}

        def gap(share_map: dict[str, float]) -> float:
            if not share_map:
                return 0.0
            return round(max(share_map.values()) - min(share_map.values()), 6)

        before_shares = shares(before_amounts)
        after_shares = shares(after_amounts)
        return {
            "region_id": region_id,
            "amount": amount,
            "before": {"amounts": before_amounts, "shares": before_shares, "max_share_gap": gap(before_shares)},
            "after": {"amounts": after_amounts, "shares": after_shares, "max_share_gap": gap(after_shares)},
            "share_gap_delta": round(gap(after_shares) - gap(before_shares), 6),
        }

    def propose_special_approval(self, *, request_id: str, actor_id: str, approval_id: str, policy_id: str,
                                 application_id: str, amount: int,
                                 justification: str) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "approval_id": approval_id, "policy_id": policy_id,
                   "application_id": application_id, "amount": amount, "justification": justification}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            now_dt = self.clock.now()
            self._sweep_expired(connection, now_dt)
            approval_id = self._identifier(approval_id, "approval_id")
            policy = self._policy_row(connection, policy_id)
            if policy["status"] != "frozen":
                raise ConflictError("只有冻结的政策版本才能发起特批")
            config = self._config(policy)
            application = self._application_row(connection, application_id)
            if application["policy_id"] != policy_id:
                raise ValidationError("申请不属于该政策版本")
            if application["status"] not in ("submitted", "accepted", "waitlisted", "deduplicated"):
                raise ConflictError("当前状态不能发起特批")
            amount = self._amount(amount, "amount")
            if amount > self._available(connection, policy_id):
                raise ValidationError("特批额度超过可用预算")
            justification = self._text(justification, "justification", 500)
            fairness = self._fairness_impact(connection, policy, config, application["region_id"], amount)

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._stamp()
                try:
                    connection.execute(
                        "INSERT INTO support_special_approvals(approval_id,policy_id,application_id,amount,"
                        "justification,fairness_json,status,proposed_by,created_at) VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (approval_id, policy_id, application_id, amount, justification,
                         canonical_json(fairness), actor_id, now))
                except Exception as exc:
                    raise ConflictError("特批编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="support.special_approval.proposed",
                             resource_type="support_special_approval", resource_id=approval_id,
                             detail={"policy_id": policy_id, "application_id": application_id, "amount": amount,
                                     "fairness": fairness}, occurred_at=now)
                return "support_special_approval", approval_id, {
                    "approval_id": approval_id, "status": "pending", "fairness": fairness}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.propose_special_approval", payload=payload, create=create)

    def cosign_special_approval(self, *, request_id: str, actor_id: str, approval_id: str,
                                approve: bool) -> tuple[WriteReceipt, dict[str, Any]]:
        payload = {"actor_id": actor_id, "approval_id": approval_id, "approve": approve}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "auditor")
            approve = self._flag(approve, "approve")
            now_dt = self.clock.now()
            now = self._iso(now_dt)
            self._sweep_expired(connection, now_dt)
            approval = connection.execute("SELECT * FROM support_special_approvals WHERE approval_id=?",
                                          (approval_id,)).fetchone()
            if approval is None:
                raise NotFoundError("特批不存在")
            if approval["proposed_by"] == actor_id:
                raise PermissionDenied("会签人必须独立于提案人")
            if approval["status"] != "pending":
                raise ConflictError("特批已完成会签")

            def create() -> tuple[str, str, dict[str, Any]]:
                if not approve:
                    connection.execute(
                        "UPDATE support_special_approvals SET status='rejected', cosigned_by=?, cosigned_at=? "
                        "WHERE approval_id=?", (actor_id, now, approval_id))
                    append_event(connection, actor_id=actor_id, action="support.special_approval.rejected",
                                 resource_type="support_special_approval", resource_id=approval_id,
                                 detail={}, occurred_at=now)
                    return "support_special_approval", approval_id, {"approval_id": approval_id,
                                                                     "status": "rejected"}
                policy = self._policy_row(connection, approval["policy_id"])
                config = self._config(policy)
                if approval["amount"] > self._available(connection, approval["policy_id"]):
                    raise ConflictError("可用预算不足，特批无法生效")
                application = self._application_row(connection, approval["application_id"])
                if application["status"] not in ("submitted", "accepted", "waitlisted", "deduplicated"):
                    raise ConflictError("申请当前状态不能接受特批预留")
                self._create_reservation(connection, policy_id=approval["policy_id"],
                                         application_id=approval["application_id"],
                                         amount=approval["amount"], source="special",
                                         ttl_hours=config["reservation_ttl_hours"],
                                         now_dt=now_dt, actor_id=actor_id)
                self._set_status(connection, approval["application_id"], "reserved", now)
                connection.execute(
                    "UPDATE support_special_approvals SET status='cosigned', cosigned_by=?, cosigned_at=? "
                    "WHERE approval_id=?", (actor_id, now, approval_id))
                self._decision(connection, policy_id=approval["policy_id"],
                               application_id=approval["application_id"], decision="allocated_special",
                               reason={"approval_id": approval_id, "amount": approval["amount"],
                                       "fairness": json.loads(approval["fairness_json"])},
                               actor_id=actor_id, now=now)
                append_event(connection, actor_id=actor_id, action="support.special_approval.cosigned",
                             resource_type="support_special_approval", resource_id=approval_id,
                             detail={"application_id": approval["application_id"], "amount": approval["amount"]},
                             occurred_at=now)
                return "support_special_approval", approval_id, {
                    "approval_id": approval_id, "status": "cosigned",
                    "application_id": approval["application_id"],
                    "fairness": json.loads(approval["fairness_json"])}

            return self._idempotent_result(connection, request_id=request_id,
                                           action="support.cosign_special_approval", payload=payload, create=create)

    # ---------- 查询与说明 ----------

    def _budget_summary(self, connection, policy_id: str, total_budget: int) -> dict[str, int]:
        summary = {
            "total_budget": total_budget,
            "reserved_active": connection.execute(
                "SELECT COALESCE(SUM(amount),0) AS value FROM support_reservations "
                "WHERE policy_id=? AND status='active'", (policy_id,)).fetchone()["value"],
            "commitment_outstanding": connection.execute(
                "SELECT COALESCE(SUM(amount - released_back),0) AS value FROM support_commitments "
                "WHERE policy_id=?", (policy_id,)).fetchone()["value"],
            "disbursed": connection.execute(
                "SELECT COALESCE(SUM(i.amount),0) AS value FROM support_installments i "
                "JOIN support_commitments c ON c.application_id=i.application_id "
                "WHERE c.policy_id=? AND i.status='released'", (policy_id,)).fetchone()["value"],
        }
        summary["available"] = (summary["total_budget"] - summary["reserved_active"]
                                - summary["commitment_outstanding"])
        return summary

    def get_policy(self, policy_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            self._sweep_expired(connection, self.clock.now())
            policy = self._policy_row(connection, policy_id)
            config = self._config(policy)
            budget = self._budget_summary(connection, policy_id, policy["total_budget"])
            return {"policy_id": policy["policy_id"], "name": policy["name"], "status": policy["status"],
                    "config": config, "budget": budget, "created_by": policy["created_by"],
                    "created_at": policy["created_at"], "frozen_at": policy["frozen_at"]}

    def get_application(self, application_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            self._sweep_expired(connection, self.clock.now())
            application = self._application_row(connection, application_id)
            groups = self._policy_groups(connection, application["policy_id"])
            ranking = connection.execute(
                "SELECT * FROM support_rankings WHERE policy_id=? AND application_id=?",
                (application["policy_id"], application_id)).fetchone()
            reservation = connection.execute(
                "SELECT * FROM support_reservations WHERE application_id=?", (application_id,)).fetchone()
            commitment = connection.execute(
                "SELECT * FROM support_commitments WHERE application_id=?", (application_id,)).fetchone()
            result = {
                "application_id": application_id,
                "policy_id": application["policy_id"],
                "applicant_id": application["applicant_id"],
                "provider_id": application["provider_id"],
                "category": application["category"],
                "region_id": application["region_id"],
                "affiliation_group": groups.get(application["applicant_id"]),
                "baseline": json.loads(application["baseline_json"]),
                "requested_amount": application["requested_amount"],
                "status": application["status"],
                "exit_obligations": (json.loads(application["exit_obligations_json"])
                                     if application["exit_obligations_json"] else None),
                "created_by": application["created_by"],
                "created_at": application["created_at"],
                "updated_at": application["updated_at"],
                "materials": [row["material_key"] for row in connection.execute(
                    "SELECT material_key FROM support_materials WHERE application_id=? ORDER BY material_key",
                    (application_id,))],
                "reviews": [{"reviewer_id": row["reviewer_id"], "decision": row["decision"], "note": row["note"]}
                            for row in connection.execute(
                                "SELECT * FROM support_reviews WHERE application_id=? ORDER BY created_at",
                                (application_id,))],
                "ranking": ({"run_id": ranking["run_id"], "score": ranking["score"],
                             "position": ranking["position"], "reasons": json.loads(ranking["reasons_json"])}
                            if ranking else None),
                "reservation": ({"amount": reservation["amount"], "source": reservation["source"],
                                 "status": reservation["status"], "expires_at": reservation["expires_at"]}
                                if reservation else None),
                "commitment": ({"amount": commitment["amount"], "released_back": commitment["released_back"],
                                "status": commitment["status"]} if commitment else None),
                "installments": [{"installment_no": row["installment_no"], "milestone_key": row["milestone_key"],
                                  "amount": row["amount"], "status": row["status"]}
                                 for row in connection.execute(
                                     "SELECT * FROM support_installments WHERE application_id=? "
                                     "ORDER BY installment_no", (application_id,))],
                "milestones": [{"milestone_key": row["milestone_key"], "kind": row["kind"],
                                "status": row["status"], "reported_by": row["reported_by"],
                                "verified_by": row["verified_by"]}
                               for row in connection.execute(
                                   "SELECT * FROM support_milestones WHERE application_id=? ORDER BY milestone_key",
                                   (application_id,))],
                "outcomes": [{"metric_key": row["metric_key"], "value": row["value"], "status": row["status"]}
                             for row in connection.execute(
                                 "SELECT * FROM support_outcomes WHERE application_id=? ORDER BY metric_key",
                                 (application_id,))],
                "decisions": [{"decision": row["decision"], "reason": json.loads(row["reason_json"]),
                               "created_by": row["created_by"], "created_at": row["created_at"]}
                              for row in connection.execute(
                                  "SELECT * FROM support_decisions WHERE application_id=? ORDER BY seq",
                                  (application_id,))],
            }
            return result

    def explain_application(self, application_id: str) -> dict[str, Any]:
        """按政策版本说明一次获配或落选的原因。"""

        detail = self.get_application(application_id)
        return {"application_id": application_id, "policy_id": detail["policy_id"],
                "status": detail["status"], "ranking": detail["ranking"],
                "reviews": detail["reviews"], "decisions": detail["decisions"]}

    def policy_ranking(self, policy_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(immediate=True) as connection:
            self._sweep_expired(connection, self.clock.now())
            self._policy_row(connection, policy_id)
            return [{"application_id": row["application_id"], "run_id": row["run_id"], "score": row["score"],
                     "position": row["position"], "reasons": json.loads(row["reasons_json"])}
                    for row in connection.execute(
                        "SELECT * FROM support_rankings WHERE policy_id=? "
                        "ORDER BY score DESC, position, application_id", (policy_id,))]

    def policy_decisions(self, policy_id: str) -> list[dict[str, Any]]:
        with self.database.transaction(immediate=True) as connection:
            self._sweep_expired(connection, self.clock.now())
            self._policy_row(connection, policy_id)
            return [{"application_id": row["application_id"], "decision": row["decision"],
                     "reason": json.loads(row["reason_json"]), "created_by": row["created_by"],
                     "created_at": row["created_at"]}
                    for row in connection.execute(
                        "SELECT * FROM support_decisions WHERE policy_id=? ORDER BY seq", (policy_id,))]

    def policy_report(self, policy_id: str) -> dict[str, Any]:
        """汇总资金覆盖、地区分布与有效成果。"""

        with self.database.transaction(immediate=True) as connection:
            self._sweep_expired(connection, self.clock.now())
            policy = self._policy_row(connection, policy_id)
            config = self._config(policy)
            budget = self._budget_summary(connection, policy_id, policy["total_budget"])
            applications = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(requested_amount),0) AS requested "
                "FROM support_applications WHERE policy_id=?", (policy_id,)).fetchone()
            by_status = {row["status"]: row["count"] for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM support_applications WHERE policy_id=? GROUP BY status",
                (policy_id,))}
            regions = []
            region_amounts = self._region_amounts(connection, policy_id, config)
            for region_id, tier in sorted(config["region_priorities"].items(), key=lambda item: item[1]):
                stats = connection.execute(
                    "SELECT COUNT(*) AS count, "
                    "COALESCE(SUM(CASE WHEN status IN ('reserved','committed','completed') THEN 1 ELSE 0 END),0) AS funded, "
                    "COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0) AS completed "
                    "FROM support_applications WHERE policy_id=? AND region_id=?",
                    (policy_id, region_id)).fetchone()
                disbursed = connection.execute(
                    "SELECT COALESCE(SUM(i.amount),0) AS value FROM support_installments i "
                    "JOIN support_applications a ON a.application_id=i.application_id "
                    "WHERE a.policy_id=? AND a.region_id=? AND i.status='released'",
                    (policy_id, region_id)).fetchone()["value"]
                regions.append({"region_id": region_id, "tier": tier, "applications": stats["count"],
                                "funded": stats["funded"], "completed": stats["completed"],
                                "encumbered": region_amounts.get(region_id, 0), "disbursed": disbursed})
            outcomes = []
            for metric in config["required_outcome_metrics"]:
                row = connection.execute(
                    "SELECT COUNT(*) AS reported, "
                    "COALESCE(SUM(CASE WHEN o.status='verified' THEN 1 ELSE 0 END),0) AS verified, "
                    "AVG(CASE WHEN o.status='verified' THEN o.value END) AS avg_value "
                    "FROM support_outcomes o JOIN support_applications a ON a.application_id=o.application_id "
                    "WHERE a.policy_id=? AND o.metric_key=?", (policy_id, metric)).fetchone()
                outcomes.append({"metric_key": metric, "reported": row["reported"],
                                 "verified": row["verified"],
                                 "avg_verified_value": (round(row["avg_value"], 4)
                                                        if row["avg_value"] is not None else None)})
            decisions = {row["decision"]: row["count"] for row in connection.execute(
                "SELECT decision, COUNT(*) AS count FROM support_decisions WHERE policy_id=? GROUP BY decision",
                (policy_id,))}
            funded_total = budget["reserved_active"] + budget["commitment_outstanding"]
            return {"policy_id": policy_id, "name": policy["name"], "status": policy["status"],
                    "budget": budget,
                    "coverage": {"applications": applications["count"],
                                 "requested_total": applications["requested"],
                                 "funded_total": funded_total,
                                 "coverage_ratio": (round(funded_total / budget["total_budget"], 6)
                                                    if budget["total_budget"] else 0.0),
                                 "by_status": by_status},
                    "regions": regions, "outcomes": outcomes, "decisions": decisions}

    def compare_policies(self, policy_ids: list[str]) -> dict[str, Any]:
        """并排比较多个政策版本的资金覆盖、地区分布与有效成果。"""

        if not policy_ids:
            raise ValidationError("policy_ids 不能为空")
        return {"policies": [self.policy_report(policy_id) for policy_id in policy_ids]}
