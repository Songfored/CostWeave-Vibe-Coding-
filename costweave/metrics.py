"""Deterministic, auditable metrics for result-blind policy tournaments."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..intelligence.common import bounded_text, finite_number, probability, validate_exact_fields
from .contracts import RoutingRecordV1
from .policies import PolicyDecisionV1


def _utility(record: RoutingRecordV1, *, quality_weight: float, cost_weight: float, latency_weight: float) -> float:
    if record.latency_ms is None and latency_weight > 0:
        raise ValueError("latency is required when latency_weight is positive")
    latency_seconds = 0.0 if record.latency_ms is None else record.latency_ms / 1000.0
    return (
        quality_weight * record.quality_score
        - cost_weight * record.cost_usd
        - latency_weight * latency_seconds
    )


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class PolicyCaseOutcomeV1:
    case_id: str
    policy_id: str
    selected_model_id: str | None
    abstained: bool
    quality_score: float | None
    accepted: bool | None
    cost_usd: float | None
    latency_ms: int | None
    utility: float | None
    oracle_model_id: str | None
    oracle_utility: float | None
    decision_regret: float | None
    oracle_agreement: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))
        if self.selected_model_id is not None:
            object.__setattr__(self, "selected_model_id", bounded_text(self.selected_model_id, "selected_model_id"))
        if not isinstance(self.abstained, bool):
            raise TypeError("abstained must be bool")
        if self.abstained and self.selected_model_id is not None:
            raise ValueError("abstained outcomes must not select a model")
        if not self.abstained and self.selected_model_id is None:
            raise ValueError("decided outcomes must select a model")
        for name in ("quality_score", "cost_usd", "utility", "oracle_utility", "decision_regret"):
            value = getattr(self, name)
            if value is not None:
                minimum = 0.0 if name in {"quality_score", "cost_usd", "decision_regret"} else None
                normalized = finite_number(value, name, minimum=minimum)
                if name == "quality_score" and normalized > 1.0:
                    raise ValueError("quality_score must be <= 1")
                object.__setattr__(self, name, normalized)
        if self.quality_score is not None and self.accepted is None:
            raise ValueError("quality and accepted must be present together")
        if self.accepted is not None and not isinstance(self.accepted, bool):
            raise TypeError("accepted must be bool or None")
        if self.latency_ms is not None:
            if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, int) or self.latency_ms < 0:
                raise ValueError("latency_ms must be a non-negative int or None")
        if self.abstained:
            if any(value is not None for value in (self.quality_score, self.accepted, self.cost_usd, self.latency_ms, self.utility)):
                raise ValueError("abstained outcomes must not expose selected result values")
        if self.oracle_model_id is None:
            if self.oracle_utility is not None or self.decision_regret is not None or self.oracle_agreement is not None:
                raise ValueError("oracle fields must be None when oracle is unavailable")
        else:
            object.__setattr__(self, "oracle_model_id", bounded_text(self.oracle_model_id, "oracle_model_id"))
            if self.oracle_utility is None:
                raise ValueError("oracle_utility is required when oracle_model_id is present")
            if self.abstained:
                if self.decision_regret is not None or self.oracle_agreement is not None:
                    raise ValueError("abstained outcomes have no decision regret or agreement")
            elif self.decision_regret is None or self.oracle_agreement is None:
                raise ValueError("decided outcomes require regret and agreement")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "policy_id": self.policy_id,
            "selected_model_id": self.selected_model_id,
            "abstained": self.abstained,
            "quality_score": self.quality_score,
            "accepted": self.accepted,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "utility": self.utility,
            "oracle_model_id": self.oracle_model_id,
            "oracle_utility": self.oracle_utility,
            "decision_regret": self.decision_regret,
            "oracle_agreement": self.oracle_agreement,
        }


@dataclass(frozen=True, slots=True)
class PolicyMetricsV1:
    policy_id: str
    total_cases: int
    decided_cases: int
    coverage: float
    accepted_count: int
    accepted_rate: float | None
    mean_quality: float | None
    total_cost_usd: float
    cost_per_accepted_result_usd: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    mean_utility: float | None
    error_risk: float | None
    oracle_agreement: float | None
    mean_decision_regret: float | None
    model_selection_counts: Mapping[str, int]
    failure_case_ids: tuple[str, ...]
    failure_case_total_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))
        for name in ("total_cases", "decided_cases", "accepted_count", "failure_case_total_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if self.decided_cases > self.total_cases or self.accepted_count > self.decided_cases:
            raise ValueError("metric counts are inconsistent")
        if self.failure_case_total_count < len(self.failure_case_ids):
            raise ValueError("failure_case_total_count cannot be below visible IDs")
        object.__setattr__(self, "coverage", probability(self.coverage, "coverage"))
        object.__setattr__(self, "total_cost_usd", finite_number(self.total_cost_usd, "total_cost_usd", minimum=0.0))
        if self.accepted_rate is not None:
            object.__setattr__(self, "accepted_rate", probability(self.accepted_rate, "accepted_rate"))
        if self.error_risk is not None:
            object.__setattr__(self, "error_risk", probability(self.error_risk, "error_risk"))
        if self.oracle_agreement is not None:
            object.__setattr__(self, "oracle_agreement", probability(self.oracle_agreement, "oracle_agreement"))
        for name in (
            "mean_quality", "cost_per_accepted_result_usd", "p50_latency_ms", "p95_latency_ms",
            "mean_utility", "mean_decision_regret",
        ):
            value = getattr(self, name)
            if value is not None:
                minimum = 0.0 if name in {"mean_quality", "cost_per_accepted_result_usd", "p50_latency_ms", "p95_latency_ms", "mean_decision_regret"} else None
                object.__setattr__(self, name, finite_number(value, name, minimum=minimum))
        counts = dict(self.model_selection_counts)
        if any(not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0 for key, value in counts.items()):
            raise ValueError("model_selection_counts must contain non-negative integer counts")
        if sum(counts.values()) != self.decided_cases:
            raise ValueError("model selection counts must sum to decided_cases")
        object.__setattr__(self, "model_selection_counts", dict(sorted(counts.items())))
        object.__setattr__(self, "failure_case_ids", tuple(bounded_text(item, "failure_case_id", maximum=200) for item in self.failure_case_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "total_cases": self.total_cases,
            "decided_cases": self.decided_cases,
            "coverage": self.coverage,
            "accepted_count": self.accepted_count,
            "accepted_rate": self.accepted_rate,
            "mean_quality": self.mean_quality,
            "total_cost_usd": self.total_cost_usd,
            "cost_per_accepted_result_usd": self.cost_per_accepted_result_usd,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "mean_utility": self.mean_utility,
            "error_risk": self.error_risk,
            "oracle_agreement": self.oracle_agreement,
            "mean_decision_regret": self.mean_decision_regret,
            "model_selection_counts": dict(self.model_selection_counts),
            "failure_case_ids": list(self.failure_case_ids),
            "failure_case_total_count": self.failure_case_total_count,
        }


def evaluate_policy_decisions(
    records: Sequence[RoutingRecordV1],
    decisions: Sequence[PolicyDecisionV1],
    *,
    quality_weight: float,
    cost_weight: float,
    latency_weight: float,
) -> tuple[PolicyCaseOutcomeV1, ...]:
    """Join blind decisions to observed records only inside the evaluator."""

    if not records:
        raise ValueError("records cannot be empty")
    if not decisions:
        raise ValueError("decisions cannot be empty")
    quality_weight = finite_number(quality_weight, "quality_weight", minimum=0.0)
    cost_weight = finite_number(cost_weight, "cost_weight", minimum=0.0)
    latency_weight = finite_number(latency_weight, "latency_weight", minimum=0.0)
    if quality_weight == cost_weight == latency_weight == 0:
        raise ValueError("at least one utility weight must be positive")
    by_case: dict[str, dict[str, RoutingRecordV1]] = {}
    for record in records:
        if not isinstance(record, RoutingRecordV1):
            raise TypeError("records must contain RoutingRecordV1")
        if record.case_id in by_case and record.model_id in by_case[record.case_id]:
            raise ValueError("duplicate case/model record")
        by_case.setdefault(record.case_id, {})[record.model_id] = record
    policy_ids = {decision.policy_id for decision in decisions}
    if len(policy_ids) != 1:
        raise ValueError("decisions must belong to one policy")
    seen_cases: set[str] = set()
    outcomes: list[PolicyCaseOutcomeV1] = []
    for decision in decisions:
        if not isinstance(decision, PolicyDecisionV1):
            raise TypeError("decisions must contain PolicyDecisionV1")
        if decision.case_id in seen_cases:
            raise ValueError("decision case IDs must be unique")
        seen_cases.add(decision.case_id)
        case_records = by_case.get(decision.case_id)
        if not case_records:
            raise ValueError("decision case is not present in records")
        utility_by_model = {model_id: _utility(record, quality_weight=quality_weight, cost_weight=cost_weight, latency_weight=latency_weight) for model_id, record in case_records.items()}
        oracle_model_id = min(utility_by_model, key=lambda model_id: (-utility_by_model[model_id], model_id))
        oracle_utility = utility_by_model[oracle_model_id]
        if decision.abstained:
            outcomes.append(PolicyCaseOutcomeV1(
                decision.case_id, decision.policy_id, None, True, None, None, None, None, None,
                oracle_model_id, oracle_utility, None, None,
            ))
            continue
        if decision.selected_model_id not in case_records:
            raise ValueError("decision selected model is not present in records")
        record = case_records[decision.selected_model_id]
        utility = utility_by_model[decision.selected_model_id]
        outcomes.append(PolicyCaseOutcomeV1(
            decision.case_id, decision.policy_id, decision.selected_model_id, False,
            record.quality_score, record.accepted, record.cost_usd, record.latency_ms,
            utility, oracle_model_id, oracle_utility, oracle_utility - utility,
            decision.selected_model_id == oracle_model_id,
        ))
    return tuple(outcomes)


def summarize_policy(outcomes: Iterable[PolicyCaseOutcomeV1]) -> PolicyMetricsV1:
    items = tuple(outcomes)
    if not items:
        raise ValueError("outcomes cannot be empty")
    policy_ids = {item.policy_id for item in items}
    if len(policy_ids) != 1:
        raise ValueError("outcomes must belong to one policy")
    total = len(items)
    decided = tuple(item for item in items if not item.abstained)
    accepted = tuple(item for item in decided if item.accepted is True)
    quality_values = tuple(item.quality_score for item in decided if item.quality_score is not None)
    latencies = tuple(item.latency_ms for item in decided if item.latency_ms is not None)
    utilities = tuple(item.utility for item in decided if item.utility is not None)
    regrets = tuple(item.decision_regret for item in decided if item.decision_regret is not None)
    agreements = tuple(item.oracle_agreement for item in decided if item.oracle_agreement is not None)
    failures = tuple(item.case_id for item in decided if item.accepted is False)
    total_cost = sum(item.cost_usd or 0.0 for item in decided)
    return PolicyMetricsV1(
        policy_id=next(iter(policy_ids)),
        total_cases=total,
        decided_cases=len(decided),
        coverage=len(decided) / total,
        accepted_count=len(accepted),
        accepted_rate=(len(accepted) / len(decided)) if decided else None,
        mean_quality=(sum(quality_values) / len(quality_values)) if quality_values else None,
        total_cost_usd=total_cost,
        cost_per_accepted_result_usd=(total_cost / len(accepted)) if accepted else None,
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        mean_utility=(sum(utilities) / len(utilities)) if utilities else None,
        error_risk=(1.0 - len(accepted) / len(decided)) if decided else None,
        oracle_agreement=(sum(bool(item) for item in agreements) / len(agreements)) if agreements else None,
        mean_decision_regret=(sum(regrets) / len(regrets)) if regrets else None,
        model_selection_counts=Counter(item.selected_model_id for item in decided if item.selected_model_id is not None),
        failure_case_ids=failures[:100],
        failure_case_total_count=len(failures),
    )


__all__ = [
    "PolicyCaseOutcomeV1",
    "PolicyMetricsV1",
    "evaluate_policy_decisions",
    "summarize_policy",
]
