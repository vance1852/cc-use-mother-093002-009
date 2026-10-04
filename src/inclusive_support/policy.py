"""普惠支持排序、关联去重与政策配置的确定性规则。"""

from __future__ import annotations

from typing import Any

from digital_trade_foundation.errors import ValidationError


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数")
    if allow_zero:
        if value < 0:
            raise ValidationError(f"{field} 不能为负数")
    elif value <= 0:
        raise ValidationError(f"{field} 必须大于 0")
    return value


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValidationError(f"{field} 必须是非空字符串列表")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(f"{field} 必须是非空字符串列表")
        result.append(item.strip())
    if len(set(result)) != len(result):
        raise ValidationError(f"{field} 不能包含重复项")
    return result


def validate_config(config: Any) -> dict[str, Any]:
    """校验并规范化政策版本配置，冻结后排序只读取该配置。"""

    if not isinstance(config, dict):
        raise ValidationError("config 必须是对象")

    def need(key: str) -> Any:
        if key not in config:
            raise ValidationError(f"config 缺少字段 {key}")
        return config[key]

    total_budget = _positive_int(need("total_budget"), "total_budget")
    reservation_ttl_hours = _positive_int(need("reservation_ttl_hours"), "reservation_ttl_hours")
    min_reviews = _positive_int(need("min_reviews"), "min_reviews")
    baseline_metrics = _string_list(need("baseline_metrics"), "baseline_metrics")
    baseline_weight = _positive_int(need("baseline_weight"), "baseline_weight", allow_zero=True)
    max_amount = _positive_int(need("max_amount_per_application"), "max_amount_per_application")
    if max_amount > total_budget:
        raise ValidationError("max_amount_per_application 不能超过 total_budget")

    region_priorities_raw = need("region_priorities")
    if not isinstance(region_priorities_raw, dict) or not region_priorities_raw:
        raise ValidationError("region_priorities 必须是非空对象")
    region_priorities: dict[str, int] = {}
    for region_id, tier in region_priorities_raw.items():
        if not isinstance(region_id, str) or not region_id.strip():
            raise ValidationError("region_priorities 的地区编号不能为空")
        region_priorities[region_id.strip()] = _positive_int(tier, f"region_priorities.{region_id}")

    tier_scores_raw = need("region_tier_scores")
    if not isinstance(tier_scores_raw, dict):
        raise ValidationError("region_tier_scores 必须是对象")
    region_tier_scores: dict[str, int] = {}
    for tier, score in tier_scores_raw.items():
        region_tier_scores[str(tier)] = _positive_int(score, f"region_tier_scores.{tier}", allow_zero=True)
    missing_tiers = {str(tier) for tier in region_priorities.values()} - set(region_tier_scores)
    if missing_tiers:
        raise ValidationError("region_tier_scores 缺少地区优先级档位")

    category_weights_raw = need("category_weights")
    if not isinstance(category_weights_raw, dict) or not category_weights_raw:
        raise ValidationError("category_weights 必须是非空对象")
    category_weights: dict[str, int] = {}
    for category, weight in category_weights_raw.items():
        if not isinstance(category, str) or not category.strip():
            raise ValidationError("category_weights 的支持类别不能为空")
        category_weights[category.strip()] = _positive_int(weight, f"category_weights.{category}", allow_zero=True)

    required_materials = _string_list(need("required_materials"), "required_materials", allow_empty=True)

    def milestone_entries(value: Any, field: str, key_name: str) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValidationError(f"{field} 必须是列表")
        entries = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError(f"{field} 必须是对象列表")
            key = item.get(key_name)
            kind = item.get("kind")
            if not isinstance(key, str) or not key.strip() or not isinstance(kind, str) or not kind.strip():
                raise ValidationError(f"{field} 的里程碑需要 {key_name} 与 kind")
            entries.append({key_name: key.strip(), "kind": kind.strip(), "percent": item.get("percent")})
        return entries

    prerequisite_milestones = milestone_entries(need("prerequisite_milestones"), "prerequisite_milestones", "key")
    prereq_keys = [entry["key"] for entry in prerequisite_milestones]
    if len(set(prereq_keys)) != len(prereq_keys):
        raise ValidationError("prerequisite_milestones 存在重复里程碑")

    installment_plan = milestone_entries(need("installment_plan"), "installment_plan", "milestone_key")
    if not installment_plan:
        raise ValidationError("installment_plan 不能为空")
    plan_keys: list[str] = []
    percent_total = 0
    normalized_plan: list[dict[str, Any]] = []
    for entry in installment_plan:
        percent = _positive_int(entry["percent"], "installment_plan.percent")
        if percent > 100:
            raise ValidationError("installment_plan.percent 不能超过 100")
        percent_total += percent
        plan_keys.append(entry["milestone_key"])
        normalized_plan.append({"milestone_key": entry["milestone_key"], "kind": entry["kind"], "percent": percent})
    if len(set(plan_keys)) != len(plan_keys):
        raise ValidationError("installment_plan 存在重复里程碑")
    if percent_total != 100:
        raise ValidationError("installment_plan 的 percent 合计必须等于 100")
    if set(plan_keys) & set(prereq_keys):
        raise ValidationError("前置里程碑与分期里程碑不能重名")

    required_outcome_metrics = _string_list(need("required_outcome_metrics"), "required_outcome_metrics")

    return {
        "total_budget": total_budget,
        "reservation_ttl_hours": reservation_ttl_hours,
        "min_reviews": min_reviews,
        "baseline_metrics": baseline_metrics,
        "baseline_weight": baseline_weight,
        "region_priorities": region_priorities,
        "region_tier_scores": region_tier_scores,
        "category_weights": category_weights,
        "max_amount_per_application": max_amount,
        "required_materials": required_materials,
        "prerequisite_milestones": [{"key": entry["key"], "kind": entry["kind"]} for entry in prerequisite_milestones],
        "installment_plan": normalized_plan,
        "required_outcome_metrics": required_outcome_metrics,
    }


def compute_affiliation_groups(applicant_ids: list[str], pairs: list[tuple[str, str]]) -> dict[str, str]:
    """按受益关系键把申请主体划分成关联组，组号取组内最小主体编号。"""

    parent = {applicant_id: applicant_id for applicant_id in applicant_ids}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    by_key: dict[str, list[str]] = {}
    for applicant_id, related_key in pairs:
        if applicant_id not in parent:
            continue
        by_key.setdefault(related_key, []).append(applicant_id)
    for members in by_key.values():
        root = find(members[0])
        for other in members[1:]:
            other_root = find(other)
            if root != other_root:
                parent[max(root, other_root)] = min(root, other_root)
                root = find(root)
    smallest: dict[str, str] = {}
    for applicant_id in parent:
        root = find(applicant_id)
        smallest[root] = min(smallest.get(root, applicant_id), applicant_id)
    return {applicant_id: smallest[find(applicant_id)] for applicant_id in parent}


def baseline_index(baseline: dict[str, Any], metrics: list[str]) -> float:
    """计算数字基础条件指数，取值 0 到 100，越低表示基础越薄弱。"""

    values = [float(baseline[metric]) for metric in metrics]
    return sum(values) / len(values)


def score_application(config: dict[str, Any], *, region_id: str, category: str,
                      baseline: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """按冻结政策配置计算申请得分，并给出可复核的得分构成。"""

    tier = config["region_priorities"][region_id]
    tier_score = int(config["region_tier_scores"][str(tier)])
    index = baseline_index(baseline, config["baseline_metrics"])
    need_score = round(config["baseline_weight"] * (100 - index))
    category_score = int(config["category_weights"][category])
    total = tier_score + need_score + category_score
    reasons = {
        "region_id": region_id,
        "region_tier": tier,
        "tier_score": tier_score,
        "baseline_index": round(index, 4),
        "need_score": need_score,
        "category": category,
        "category_score": category_score,
        "total_score": total,
    }
    return total, reasons
