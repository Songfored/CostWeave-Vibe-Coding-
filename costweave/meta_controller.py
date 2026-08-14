"""Deterministic rational metareasoning contracts for CostWeave v0.6.1.

This module is deliberately provider-free.  It decides whether more planning
is worth its cost; it does not execute a planner, probe, model or tool.  A
caller can therefore use the same decision contract in an offline tournament,
an advisory API, or a future runtime adapter without silently changing the
authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..contracts_v4 import utc_now
from .beliefs import SystemBeliefV1, TaskBeliefV1
from .common import (
    BUDGET_SCHEMA_VERSION,
    META_ACTIONS,
    META_CONTROLLER_POLICY_REVISION,
    META_CONTROLLER_SCHEMA_VERSION,
    NonNegativeEstimate,
    ProbabilityInterval,
    bounded_text,
    finite_number,
    probability,
    stable_digest,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_timestamp,
)
from .strategy import StrategyCandidateV1


META_REASON_CODES = (
    "hard_constraints_satisfied",
    "risk_low",
    "ood_below_threshold",
    "evidence_fresh",
    "utility_gap_safe",
    "single_feasible_candidate",
    "requires_expert_risk",
    "requires_expert_ood",
    "requires_expert_capacity",
    "requires_expert_stale_evidence",
    "requires_expert_side_effects",
    "requires_expert_no_feasible_strategy",
    "requires_expert_budget",
    "utility_intervals_overlap",
    "probe_positive_evi",
    "deliberation_positive_evc",
    "evc_non_positive",
    "committee_budget_exhausted",
    "committee_consensus_reached",
    "committee_member_unavailable",
)


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


@dataclass(frozen=True, slots=True)
class UtilityIntervalV1:
    """An interval in the common utility unit used by EVC and EVI."""

    mean: float
    lower: float
    upper: float

    def __post_init__(self) -> None:
        mean = finite_number(self.mean, "mean")
        lower = finite_number(self.lower, "lower")
        upper = finite_number(self.upper, "upper")
        if lower > mean or mean > upper:
            raise ValueError("utility interval must satisfy lower <= mean <= upper")
        object.__setattr__(self, "mean", mean)
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)

    def to_dict(self) -> dict[str, float]:
        return {"mean": self.mean, "lower": self.lower, "upper": self.upper}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UtilityIntervalV1":
        validate_exact_fields(value, required=frozenset({"mean", "lower", "upper"}))
        return cls(mean=value["mean"], lower=value["lower"], upper=value["upper"])


@dataclass(frozen=True, slots=True)
class BudgetAmountV1:
    """A non-negative, multi-dimensional planning budget amount."""

    money_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_clock_ms: int = 0
    planner_calls: int = 0
    judge_calls: int = 0
    probe_calls: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "money_usd", finite_number(self.money_usd, "money_usd", minimum=0.0))
        for name in (
            "input_tokens", "output_tokens", "wall_clock_ms",
            "planner_calls", "judge_calls", "probe_calls",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "money_usd": self.money_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "wall_clock_ms": self.wall_clock_ms,
            "planner_calls": self.planner_calls,
            "judge_calls": self.judge_calls,
            "probe_calls": self.probe_calls,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BudgetAmountV1":
        validate_exact_fields(
            value,
            required=frozenset({"money_usd", "input_tokens", "output_tokens", "wall_clock_ms", "planner_calls", "judge_calls", "probe_calls"}),
        )
        return cls(**dict(value))

    def add(self, other: "BudgetAmountV1") -> "BudgetAmountV1":
        if not isinstance(other, BudgetAmountV1):
            raise TypeError("other must be BudgetAmountV1")
        return BudgetAmountV1(
            money_usd=self.money_usd + other.money_usd,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            wall_clock_ms=self.wall_clock_ms + other.wall_clock_ms,
            planner_calls=self.planner_calls + other.planner_calls,
            judge_calls=self.judge_calls + other.judge_calls,
            probe_calls=self.probe_calls + other.probe_calls,
        )

    def subtract(self, other: "BudgetAmountV1") -> "BudgetAmountV1":
        if not isinstance(other, BudgetAmountV1):
            raise TypeError("other must be BudgetAmountV1")
        values = (
            self.money_usd - other.money_usd,
            self.input_tokens - other.input_tokens,
            self.output_tokens - other.output_tokens,
            self.wall_clock_ms - other.wall_clock_ms,
            self.planner_calls - other.planner_calls,
            self.judge_calls - other.judge_calls,
            self.probe_calls - other.probe_calls,
        )
        if any(value < 0 for value in values):
            raise ValueError("budget amount cannot become negative")
        return BudgetAmountV1(*values)

    def fits_within(self, limit: "BudgetAmountV1") -> bool:
        if not isinstance(limit, BudgetAmountV1):
            raise TypeError("limit must be BudgetAmountV1")
        return (
            self.money_usd <= limit.money_usd + 1e-12
            and self.input_tokens <= limit.input_tokens
            and self.output_tokens <= limit.output_tokens
            and self.wall_clock_ms <= limit.wall_clock_ms
            and self.planner_calls <= limit.planner_calls
            and self.judge_calls <= limit.judge_calls
            and self.probe_calls <= limit.probe_calls
        )


@dataclass(frozen=True, slots=True)
class PlanningBudgetV1:
    budget_id: str
    project_id: str
    limits: BudgetAmountV1
    created_at: str
    schema_version: str = BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "budget_id", bounded_text(self.budget_id, "budget_id"))
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id"))
        if not isinstance(self.limits, BudgetAmountV1):
            raise TypeError("limits must be BudgetAmountV1")
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != BUDGET_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {BUDGET_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget_id": self.budget_id,
            "project_id": self.project_id,
            "limits": self.limits.to_dict(),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "budget_hash": self.budget_hash,
        }

    @property
    def budget_hash(self) -> str:
        return stable_digest(
            {"budget_id": self.budget_id, "project_id": self.project_id, "limits": self.limits.to_dict(), "schema_version": self.schema_version},
            length=64,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanningBudgetV1":
        required = frozenset({"budget_id", "project_id", "limits", "created_at", "schema_version"})
        validate_exact_fields(value, required=required, optional=frozenset({"budget_hash"}))
        item = cls(
            budget_id=value["budget_id"],
            project_id=value["project_id"],
            limits=BudgetAmountV1.from_dict(value["limits"]),
            created_at=value["created_at"],
            schema_version=value["schema_version"],
        )
        if "budget_hash" in value and value["budget_hash"] != item.budget_hash:
            raise ValueError("budget_hash does not match content")
        return item


@dataclass(frozen=True, slots=True)
class PlannerMemberV1:
    """A planner proposal source used by the bounded committee selector."""

    member_id: str
    role: str
    information_value: UtilityIntervalV1
    planning_cost_usd: NonNegativeEstimate
    planning_latency_ms: NonNegativeEstimate
    failure_correlation_group: str
    evidence_level: str
    evidence_age_ms: int
    specialties: tuple[str, ...] = ()
    available: bool = True
    schema_version: str = META_CONTROLLER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "member_id", bounded_text(self.member_id, "member_id"))
        object.__setattr__(self, "role", bounded_text(self.role, "role"))
        if not isinstance(self.information_value, UtilityIntervalV1):
            raise TypeError("information_value must be UtilityIntervalV1")
        if not isinstance(self.planning_cost_usd, NonNegativeEstimate):
            raise TypeError("planning_cost_usd must be NonNegativeEstimate")
        if not isinstance(self.planning_latency_ms, NonNegativeEstimate):
            raise TypeError("planning_latency_ms must be NonNegativeEstimate")
        object.__setattr__(self, "failure_correlation_group", bounded_text(self.failure_correlation_group, "failure_correlation_group"))
        object.__setattr__(self, "evidence_level", bounded_text(self.evidence_level, "evidence_level"))
        object.__setattr__(self, "evidence_age_ms", _non_negative_int(self.evidence_age_ms, "evidence_age_ms"))
        object.__setattr__(self, "specialties", unique_texts(self.specialties, "specialties", maximum_items=32))
        object.__setattr__(self, "available", _bool(self.available, "available"))
        if self.schema_version != META_CONTROLLER_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {META_CONTROLLER_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "role": self.role,
            "information_value": self.information_value.to_dict(),
            "planning_cost_usd": self.planning_cost_usd.to_dict(),
            "planning_latency_ms": self.planning_latency_ms.to_dict(),
            "failure_correlation_group": self.failure_correlation_group,
            "evidence_level": self.evidence_level,
            "evidence_age_ms": self.evidence_age_ms,
            "specialties": list(self.specialties),
            "available": self.available,
            "schema_version": self.schema_version,
        }

    @property
    def member_hash(self) -> str:
        return stable_id("planner_member", self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlannerMemberV1":
        required = frozenset({"member_id", "role", "information_value", "planning_cost_usd", "planning_latency_ms", "failure_correlation_group", "evidence_level", "evidence_age_ms", "specialties", "available", "schema_version"})
        validate_exact_fields(value, required=required, optional=frozenset({"member_hash"}))
        item = cls(
            member_id=value["member_id"], role=value["role"],
            information_value=UtilityIntervalV1.from_dict(value["information_value"]),
            planning_cost_usd=NonNegativeEstimate.from_dict(value["planning_cost_usd"]),
            planning_latency_ms=NonNegativeEstimate.from_dict(value["planning_latency_ms"]),
            failure_correlation_group=value["failure_correlation_group"],
            evidence_level=value["evidence_level"], evidence_age_ms=value["evidence_age_ms"],
            specialties=tuple(value["specialties"]), available=value["available"],
            schema_version=value["schema_version"],
        )
        if "member_hash" in value and value["member_hash"] != item.member_hash:
            raise ValueError("member_hash does not match content")
        return item


@dataclass(frozen=True, slots=True)
class PlannerCommitteeV1:
    case_id: str
    member_ids: tuple[str, ...]
    marginal_information_values: Mapping[str, float]
    expected_value_of_computation: float
    stop_reason: str
    call_count: int
    budget: BudgetAmountV1
    created_at: str
    schema_version: str = META_CONTROLLER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        members = unique_texts(self.member_ids, "member_ids", maximum_items=16)
        object.__setattr__(self, "member_ids", members)
        if not isinstance(self.marginal_information_values, Mapping):
            raise TypeError("marginal_information_values must be a mapping")
        values = {str(key): finite_number(value, f"marginal_information_values[{key}]") for key, value in self.marginal_information_values.items()}
        if set(values) != set(members):
            raise ValueError("marginal information keys must exactly match member_ids")
        object.__setattr__(self, "marginal_information_values", dict(sorted(values.items())))
        object.__setattr__(self, "expected_value_of_computation", finite_number(self.expected_value_of_computation, "expected_value_of_computation"))
        object.__setattr__(self, "stop_reason", bounded_text(self.stop_reason, "stop_reason"))
        object.__setattr__(self, "call_count", _non_negative_int(self.call_count, "call_count"))
        if self.call_count != len(members):
            raise ValueError("call_count must equal selected member count")
        if not isinstance(self.budget, BudgetAmountV1):
            raise TypeError("budget must be BudgetAmountV1")
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != META_CONTROLLER_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {META_CONTROLLER_SCHEMA_VERSION}")

    @property
    def committee_id(self) -> str:
        return stable_id("planner_committee", {"case_id": self.case_id, "member_ids": list(self.member_ids), "marginal_information_values": dict(self.marginal_information_values), "expected_value_of_computation": self.expected_value_of_computation, "stop_reason": self.stop_reason, "call_count": self.call_count, "budget": self.budget.to_dict(), "schema_version": self.schema_version})

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "member_ids": list(self.member_ids),
            "marginal_information_values": dict(self.marginal_information_values),
            "expected_value_of_computation": self.expected_value_of_computation,
            "stop_reason": self.stop_reason,
            "call_count": self.call_count,
            "budget": self.budget.to_dict(),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "committee_id": self.committee_id,
        }


@dataclass(frozen=True, slots=True)
class MetaControllerConfigV1:
    ood_threshold: float = 0.25
    safety_margin: float = 0.05
    max_telemetry_age_ms: int = 86_400_000
    max_queue_depth: int = 32
    coordination_penalty_per_member: float = 0.01
    latency_penalty_per_second: float = 0.01
    cost_penalty_per_usd: float = 1.0
    max_committee_members: int = 4
    consensus_width: float = 0.05
    probe_cost_usd: float = 0.001
    minimum_probe_value: float = 0.01
    expert_budget: BudgetAmountV1 = BudgetAmountV1(money_usd=0.05, planner_calls=1)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ood_threshold", probability(self.ood_threshold, "ood_threshold"))
        object.__setattr__(self, "safety_margin", probability(self.safety_margin, "safety_margin"))
        object.__setattr__(self, "max_telemetry_age_ms", _non_negative_int(self.max_telemetry_age_ms, "max_telemetry_age_ms"))
        object.__setattr__(self, "max_queue_depth", _non_negative_int(self.max_queue_depth, "max_queue_depth"))
        object.__setattr__(self, "coordination_penalty_per_member", finite_number(self.coordination_penalty_per_member, "coordination_penalty_per_member", minimum=0.0))
        object.__setattr__(self, "latency_penalty_per_second", finite_number(self.latency_penalty_per_second, "latency_penalty_per_second", minimum=0.0))
        object.__setattr__(self, "cost_penalty_per_usd", finite_number(self.cost_penalty_per_usd, "cost_penalty_per_usd", minimum=0.0))
        members = _non_negative_int(self.max_committee_members, "max_committee_members")
        if not 1 <= members <= 16:
            raise ValueError("max_committee_members must be between 1 and 16")
        object.__setattr__(self, "max_committee_members", members)
        object.__setattr__(self, "consensus_width", finite_number(self.consensus_width, "consensus_width", minimum=0.0))
        object.__setattr__(self, "probe_cost_usd", finite_number(self.probe_cost_usd, "probe_cost_usd", minimum=0.0))
        object.__setattr__(self, "minimum_probe_value", finite_number(self.minimum_probe_value, "minimum_probe_value", minimum=0.0))
        if not isinstance(self.expert_budget, BudgetAmountV1):
            raise TypeError("expert_budget must be BudgetAmountV1")


@dataclass(frozen=True, slots=True)
class MetaDecisionV1:
    project_id: str
    case_id: str
    belief_hash: str
    task_belief_id: str
    system_belief_id: str
    candidate_strategy_ids: tuple[str, ...]
    selected_strategy_id: str | None
    action: str
    budget: BudgetAmountV1
    reason_codes: tuple[str, ...]
    utility_interval: UtilityIntervalV1
    expected_regret_reduction: UtilityIntervalV1
    information_value: UtilityIntervalV1
    planning_money_cost: float
    latency_penalty: float
    coordination_penalty: float
    expected_value_of_computation: float
    committee_member_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    created_at: str
    schema_version: str = META_CONTROLLER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id"))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        from .common import validate_sha256

        object.__setattr__(self, "belief_hash", validate_sha256(self.belief_hash, "belief_hash"))
        object.__setattr__(self, "task_belief_id", bounded_text(self.task_belief_id, "task_belief_id"))
        object.__setattr__(self, "system_belief_id", bounded_text(self.system_belief_id, "system_belief_id"))
        candidates = unique_texts(self.candidate_strategy_ids, "candidate_strategy_ids", maximum_items=256)
        object.__setattr__(self, "candidate_strategy_ids", candidates)
        if self.selected_strategy_id is not None:
            selected = bounded_text(self.selected_strategy_id, "selected_strategy_id")
            if selected not in candidates:
                raise ValueError("selected_strategy_id must be a candidate or None")
            object.__setattr__(self, "selected_strategy_id", selected)
        if self.action not in META_ACTIONS:
            raise ValueError(f"unsupported action: {self.action!r}")
        if not isinstance(self.budget, BudgetAmountV1):
            raise TypeError("budget must be BudgetAmountV1")
        reasons = unique_texts(self.reason_codes, "reason_codes", maximum_items=64)
        if not reasons:
            raise ValueError("reason_codes must contain at least one item")
        object.__setattr__(self, "reason_codes", reasons)
        for field_name in ("utility_interval", "expected_regret_reduction", "information_value"):
            if not isinstance(getattr(self, field_name), UtilityIntervalV1):
                raise TypeError(f"{field_name} must be UtilityIntervalV1")
        for field_name in ("planning_money_cost", "latency_penalty", "coordination_penalty"):
            object.__setattr__(self, field_name, finite_number(getattr(self, field_name), field_name, minimum=0.0))
        object.__setattr__(self, "expected_value_of_computation", finite_number(self.expected_value_of_computation, "expected_value_of_computation"))
        object.__setattr__(self, "committee_member_ids", unique_texts(self.committee_member_ids, "committee_member_ids", maximum_items=16))
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))
        object.__setattr__(self, "policy_version", bounded_text(self.policy_version, "policy_version"))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != META_CONTROLLER_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {META_CONTROLLER_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "case_id": self.case_id,
            "belief_hash": self.belief_hash,
            "task_belief_id": self.task_belief_id,
            "system_belief_id": self.system_belief_id,
            "candidate_strategy_ids": list(self.candidate_strategy_ids),
            "selected_strategy_id": self.selected_strategy_id,
            "action": self.action,
            "budget": self.budget.to_dict(),
            "reason_codes": list(self.reason_codes),
            "utility_interval": self.utility_interval.to_dict(),
            "expected_regret_reduction": self.expected_regret_reduction.to_dict(),
            "information_value": self.information_value.to_dict(),
            "planning_money_cost": self.planning_money_cost,
            "latency_penalty": self.latency_penalty,
            "coordination_penalty": self.coordination_penalty,
            "expected_value_of_computation": self.expected_value_of_computation,
            "committee_member_ids": list(self.committee_member_ids),
            "evidence_ids": list(self.evidence_ids),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
        }

    @property
    def decision_id(self) -> str:
        return stable_id("meta_decision", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["created_at"] = self.created_at
        result["decision_id"] = self.decision_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MetaDecisionV1":
        required = frozenset({
            "project_id", "case_id", "belief_hash", "task_belief_id", "system_belief_id",
            "candidate_strategy_ids", "selected_strategy_id", "action", "budget", "reason_codes",
            "utility_interval", "expected_regret_reduction", "information_value",
            "planning_money_cost", "latency_penalty", "coordination_penalty",
            "expected_value_of_computation", "committee_member_ids", "evidence_ids",
            "policy_id", "policy_version", "created_at", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"decision_id"}))
        item = cls(
            project_id=value["project_id"], case_id=value["case_id"], belief_hash=value["belief_hash"],
            task_belief_id=value["task_belief_id"], system_belief_id=value["system_belief_id"],
            candidate_strategy_ids=tuple(value["candidate_strategy_ids"]),
            selected_strategy_id=value["selected_strategy_id"], action=value["action"],
            budget=BudgetAmountV1.from_dict(value["budget"]), reason_codes=tuple(value["reason_codes"]),
            utility_interval=UtilityIntervalV1.from_dict(value["utility_interval"]),
            expected_regret_reduction=UtilityIntervalV1.from_dict(value["expected_regret_reduction"]),
            information_value=UtilityIntervalV1.from_dict(value["information_value"]),
            planning_money_cost=value["planning_money_cost"], latency_penalty=value["latency_penalty"],
            coordination_penalty=value["coordination_penalty"], expected_value_of_computation=value["expected_value_of_computation"],
            committee_member_ids=tuple(value["committee_member_ids"]), evidence_ids=tuple(value["evidence_ids"]),
            policy_id=value["policy_id"], policy_version=value["policy_version"], created_at=value["created_at"],
            schema_version=value["schema_version"],
        )
        if "decision_id" in value and value["decision_id"] != item.decision_id:
            raise ValueError("decision_id does not match content")
        return item


def belief_hash(task_belief: TaskBeliefV1, system_belief: SystemBeliefV1) -> str:
    """Return a full SHA-256 hash of the input beliefs, including no wall-clock time."""

    if not isinstance(task_belief, TaskBeliefV1) or not isinstance(system_belief, SystemBeliefV1):
        raise TypeError("belief_hash requires TaskBeliefV1 and SystemBeliefV1")
    return stable_digest({"task_belief_id": task_belief.belief_id, "system_belief_id": system_belief.belief_id}, length=64)


def _candidate_utility(candidate: StrategyCandidateV1) -> UtilityIntervalV1:
    """Convert a candidate's forecast to the shared utility unit conservatively."""

    cost_mean = candidate.predicted_total_cost_usd.mean
    cost_p95 = candidate.predicted_total_cost_usd.p95
    latency_mean = candidate.predicted_total_latency_ms.mean / 1000.0
    latency_p95 = candidate.predicted_total_latency_ms.p95 / 1000.0
    # The initial lab utility is deliberately simple and explicit.  Later
    # versions may learn weights, but this version must remain reproducible.
    lower = candidate.predicted_success.lower - cost_p95 - latency_p95 * 0.01
    mean = candidate.predicted_success.mean - cost_mean - latency_mean * 0.01
    upper = candidate.predicted_success.upper - cost_mean - latency_mean * 0.01
    return UtilityIntervalV1(lower=lower, mean=mean, upper=upper)


def select_adaptive_committee(
    members: Sequence[PlannerMemberV1],
    *,
    case_id: str,
    expected_regret_reduction: UtilityIntervalV1,
    budget: BudgetAmountV1,
    max_members: int = 4,
    consensus_width: float = 0.05,
    coordination_penalty: float = 0.01,
    now: str | None = None,
) -> PlannerCommitteeV1:
    """Select a bounded, non-fixed planner committee by marginal EVC.

    Members are sorted deterministically by information value per planning
    cost.  A member from an already selected correlation group receives a
    reduced marginal value.  Selection stops as soon as the marginal EVC is
    non-positive, the consensus interval is narrow enough, or any budget
    dimension would be exceeded.
    """

    case = bounded_text(case_id, "case_id", maximum=200)
    if isinstance(max_members, bool) or not isinstance(max_members, int) or not 1 <= max_members <= 16:
        raise ValueError("max_members must be between 1 and 16")
    if isinstance(consensus_width, bool) or consensus_width < 0:
        raise ValueError("consensus_width must be non-negative")
    if isinstance(coordination_penalty, bool) or coordination_penalty < 0:
        raise ValueError("coordination_penalty must be non-negative")
    if not isinstance(expected_regret_reduction, UtilityIntervalV1):
        raise TypeError("expected_regret_reduction must be UtilityIntervalV1")
    if not isinstance(budget, BudgetAmountV1):
        raise TypeError("budget must be BudgetAmountV1")
    normalized: dict[str, PlannerMemberV1] = {}
    for member in members:
        if not isinstance(member, PlannerMemberV1):
            raise TypeError("members must contain PlannerMemberV1")
        if member.member_id in normalized:
            raise ValueError("planner member IDs must be unique")
        normalized[member.member_id] = member
    available = [item for item in normalized.values() if item.available]
    unavailable = len(available) != len(normalized)
    available.sort(key=lambda item: (-(item.information_value.mean / max(item.planning_cost_usd.mean, 1e-9)), item.member_id))
    selected: list[PlannerMemberV1] = []
    marginal: dict[str, float] = {}
    used = BudgetAmountV1()
    selected_groups: set[str] = set()
    stop_reason = "no_members_available"
    for member in available:
        if len(selected) >= max_members:
            stop_reason = "committee_limit_reached"
            break
        correlation_discount = 0.5 if member.failure_correlation_group in selected_groups else 1.0
        gain = max(0.0, member.information_value.mean * correlation_discount)
        latency_penalty = member.planning_latency_ms.mean / 1000.0 * coordination_penalty
        incremental = gain - member.planning_cost_usd.mean - latency_penalty
        candidate_amount = BudgetAmountV1(
            money_usd=member.planning_cost_usd.mean,
            wall_clock_ms=int(round(member.planning_latency_ms.mean)),
            planner_calls=1,
        )
        if not candidate_amount.add(used).fits_within(budget):
            stop_reason = "budget_exhausted"
            break
        if incremental <= 0:
            stop_reason = "evc_non_positive"
            break
        selected.append(member)
        # Only selected members are part of the immutable committee contract.
        # Keeping rejected candidates in this mapping would make the record
        # impossible to validate and would overstate the paid information.
        marginal[member.member_id] = incremental
        used = used.add(candidate_amount)
        selected_groups.add(member.failure_correlation_group)
        if len(selected) >= 2 and expected_regret_reduction.upper - sum(item.information_value.lower for item in selected) <= consensus_width:
            stop_reason = "consensus_reached"
            break
    else:
        stop_reason = "all_positive_members_selected" if available else "no_members_available"
    if unavailable and not selected:
        stop_reason = "member_unavailable"
    if not selected and not normalized:
        marginal = {}
    timestamp = now or utc_now()
    return PlannerCommitteeV1(
        case_id=case,
        member_ids=tuple(item.member_id for item in selected),
        marginal_information_values=marginal,
        expected_value_of_computation=sum(marginal.get(item.member_id, 0.0) for item in selected),
        stop_reason=stop_reason,
        call_count=len(selected),
        budget=used,
        created_at=timestamp,
    )


class MetaControllerEngine:
    """Pure, deterministic Rational MetaController.

    The engine returns an auditable decision and optional committee snapshot;
    it performs no persistence and no external call.
    """

    def __init__(self, config: MetaControllerConfigV1 | None = None) -> None:
        self.config = config or MetaControllerConfigV1()

    @staticmethod
    def _validate_inputs(task: TaskBeliefV1, system: SystemBeliefV1, candidates: Sequence[StrategyCandidateV1]) -> tuple[StrategyCandidateV1, ...]:
        if not isinstance(task, TaskBeliefV1) or not isinstance(system, SystemBeliefV1):
            raise TypeError("task and system must be TaskBeliefV1/SystemBeliefV1")
        if task.case_id != system.case_id:
            raise ValueError("task and system beliefs must refer to the same case")
        normalized: list[StrategyCandidateV1] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, StrategyCandidateV1):
                raise TypeError("candidates must contain StrategyCandidateV1")
            if candidate.case_id != task.case_id:
                raise ValueError("candidate case_id does not match beliefs")
            if candidate.strategy_id in seen:
                raise ValueError("candidate strategy IDs must be unique")
            seen.add(candidate.strategy_id)
            normalized.append(candidate)
        return tuple(sorted(normalized, key=lambda item: item.strategy_id))

    def decide(
        self,
        *,
        project_id: str,
        task_belief: TaskBeliefV1,
        system_belief: SystemBeliefV1,
        candidates: Sequence[StrategyCandidateV1],
        planner_members: Sequence[PlannerMemberV1] = (),
        available_budget: BudgetAmountV1 | None = None,
        now: str | None = None,
    ) -> tuple[MetaDecisionV1, PlannerCommitteeV1 | None]:
        project = bounded_text(project_id, "project_id")
        candidates_tuple = self._validate_inputs(task_belief, system_belief, candidates)
        timestamp = now or utc_now()
        available = available_budget or BudgetAmountV1(money_usd=system_belief.budget_remaining_usd)
        feasible = tuple(item for item in candidates_tuple if item.feasible)
        reasons: list[str] = []
        expert_reasons: list[str] = []
        if not feasible:
            expert_reasons.append("requires_expert_no_feasible_strategy")
        if task_belief.risk_level in {"high", "critical"}:
            expert_reasons.append("requires_expert_risk")
        else:
            reasons.append("risk_low")
        if task_belief.ood_score > self.config.ood_threshold:
            expert_reasons.append("requires_expert_ood")
        else:
            reasons.append("ood_below_threshold")
        if system_belief.telemetry_age_ms > self.config.max_telemetry_age_ms:
            expert_reasons.append("requires_expert_stale_evidence")
        else:
            reasons.append("evidence_fresh")
        if system_belief.local_available_slots <= 0 or system_belief.local_queue_depth > self.config.max_queue_depth:
            expert_reasons.append("requires_expert_capacity")
        if task_belief.external_side_effects and not task_belief.reversible:
            expert_reasons.append("requires_expert_side_effects")
        if feasible:
            utilities = {item.strategy_id: _candidate_utility(item) for item in feasible}
            ranked = sorted(feasible, key=lambda item: (-utilities[item.strategy_id].mean, item.strategy_id))
            best = ranked[0]
            best_utility = utilities[best.strategy_id]
            second_utility = utilities[ranked[1].strategy_id] if len(ranked) > 1 else None
            if second_utility is None:
                gap_safe = True
                reasons.append("single_feasible_candidate")
            else:
                gap_safe = best_utility.lower >= second_utility.upper + self.config.safety_margin
                reasons.append("utility_gap_safe" if gap_safe else "utility_intervals_overlap")
            if not all(best.hard_constraint_checks.values()):
                expert_reasons.append("requires_expert_no_feasible_strategy")
            planning_cost = best.planning_cost_usd.mean
            if planning_cost > available.money_usd + 1e-12:
                expert_reasons.append("requires_expert_budget")
        else:
            ranked = []
            best = None
            best_utility = UtilityIntervalV1(mean=0.0, lower=0.0, upper=0.0)
            second_utility = None
            gap_safe = False
        expected_regret = UtilityIntervalV1(
            lower=0.0,
            mean=max(0.0, (best_utility.upper - best_utility.lower) * 0.5),
            upper=max(0.0, best_utility.upper - best_utility.lower),
        )
        members = tuple(planner_members)
        fast_eligible = not expert_reasons and bool(feasible) and gap_safe
        if expert_reasons or fast_eligible:
            # Mandatory expert escalation and a proven-safe fast path must not
            # spend an additional committee call merely to document a choice.
            committee = select_adaptive_committee(
                (), case_id=task_belief.case_id,
                expected_regret_reduction=expected_regret,
                budget=available,
                max_members=self.config.max_committee_members,
                consensus_width=self.config.consensus_width,
                coordination_penalty=self.config.coordination_penalty_per_member,
                now=timestamp,
            )
        else:
            committee = select_adaptive_committee(
                members,
                case_id=task_belief.case_id,
                expected_regret_reduction=expected_regret,
                budget=available,
                max_members=self.config.max_committee_members,
                consensus_width=self.config.consensus_width,
                coordination_penalty=self.config.coordination_penalty_per_member,
                now=timestamp,
            )
        committee_reason = {
            "budget_exhausted": "committee_budget_exhausted",
            "consensus_reached": "committee_consensus_reached",
            "member_unavailable": "committee_member_unavailable",
        }.get(committee.stop_reason)
        if committee_reason and not expert_reasons and not fast_eligible:
            reasons.append(committee_reason)
        selected_member_ids = set(committee.member_ids)
        selected_members = tuple(item for item in members if item.member_id in selected_member_ids)
        committee_evi = UtilityIntervalV1(
            lower=0.0,
            mean=max(0.0, sum(max(0.0, item.information_value.mean) for item in selected_members)),
            upper=max(0.0, sum(max(0.0, item.information_value.upper) for item in selected_members)),
        )
        planning_money_cost = committee.budget.money_usd
        latency_penalty = committee.budget.wall_clock_ms / 1000.0 * self.config.latency_penalty_per_second
        coordination_penalty = max(0, committee.call_count - 1) * self.config.coordination_penalty_per_member
        evc = expected_regret.mean - planning_money_cost * self.config.cost_penalty_per_usd - latency_penalty - coordination_penalty
        action = "expert"
        action_budget = self.config.expert_budget
        if fast_eligible:
            action = "fast"
            action_budget = BudgetAmountV1()
            reasons.append("hard_constraints_satisfied")
        elif expert_reasons:
            reasons.extend(expert_reasons)
        elif committee_evi.mean >= self.config.minimum_probe_value and self.config.probe_cost_usd <= available.money_usd:
            action = "probe"
            action_budget = BudgetAmountV1(money_usd=self.config.probe_cost_usd, probe_calls=1)
            reasons.append("probe_positive_evi")
        elif evc > 0.0 and committee.call_count > 0:
            action = "deliberate"
            action_budget = committee.budget
            reasons.append("deliberation_positive_evc")
        else:
            reasons.append("evc_non_positive")
        # Budget is a hard constraint for the selected metareasoning action.
        if not action_budget.fits_within(available):
            action = "expert"
            action_budget = self.config.expert_budget
            reasons.append("requires_expert_budget")
        reasons = list(dict.fromkeys(reasons))
        decision = MetaDecisionV1(
            project_id=project,
            case_id=task_belief.case_id,
            belief_hash=belief_hash(task_belief, system_belief),
            task_belief_id=task_belief.belief_id,
            system_belief_id=system_belief.belief_id,
            candidate_strategy_ids=tuple(item.strategy_id for item in candidates_tuple),
            selected_strategy_id=best.strategy_id if best is not None else None,
            action=action,
            budget=action_budget,
            reason_codes=tuple(reasons),
            utility_interval=best_utility,
            expected_regret_reduction=expected_regret,
            information_value=committee_evi,
            planning_money_cost=planning_money_cost,
            latency_penalty=latency_penalty,
            coordination_penalty=coordination_penalty,
            expected_value_of_computation=evc,
            committee_member_ids=committee.member_ids if action == "deliberate" else (),
            evidence_ids=unique_texts(tuple(task_belief.evidence_ids) + tuple(system_belief.evidence_ids), "evidence_ids", maximum_items=64),
            policy_id="rational-meta-controller",
            policy_version=META_CONTROLLER_POLICY_REVISION,
            created_at=timestamp,
        )
        return decision, committee

    choose = decide


__all__ = [
    "BudgetAmountV1",
    "META_REASON_CODES",
    "MetaControllerConfigV1",
    "MetaControllerEngine",
    "MetaDecisionV1",
    "PlannerCommitteeV1",
    "PlannerMemberV1",
    "PlanningBudgetV1",
    "UtilityIntervalV1",
    "belief_hash",
    "select_adaptive_committee",
]
