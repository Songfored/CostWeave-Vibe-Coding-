"""Provider-neutral planning-governance contracts for CostWeave v0.5.3."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping


PLANNING_SCHEMA_VERSION = "v0.5.3"
PLANNING_POLICY_VERSION = "planning-governor-0.5.3"


class PlanningContractError(ValueError):
    """Raised when a deliberation contract violates a hard invariant."""


class PlanningMode(StrEnum):
    NO_DELEGATION = "NO_DELEGATION"
    DIRECT_DELEGATION = "DIRECT_DELEGATION"
    SINGLE_PLANNER = "SINGLE_PLANNER"
    MULTI_PLANNER = "MULTI_PLANNER"
    EXPERT_OVERRIDE = "EXPERT_OVERRIDE"
    ASK_USER = "ASK_USER"


@dataclass(frozen=True, slots=True)
class ManagementLimits:
    max_management_depth: int = 2
    governor_can_spawn_governor: bool = False
    planner_can_spawn_planner: bool = False
    max_planners_per_round: int = 3
    max_adjudication_rounds: int = 1
    max_full_replans: int = 1
    max_node_escalations: int = 2

    def __post_init__(self) -> None:
        if self.max_management_depth != 2:
            raise PlanningContractError("management depth is frozen at two")
        if self.governor_can_spawn_governor or self.planner_can_spawn_planner:
            raise PlanningContractError("recursive management layers are forbidden")
        if not 1 <= self.max_planners_per_round <= 3:
            raise PlanningContractError("planner panel must contain one to three roles")
        if self.max_adjudication_rounds != 1 or self.max_full_replans != 1:
            raise PlanningContractError("v0.5.3 permits only one adjudication and one full replan")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PlanningBudget:
    max_cost: float
    max_latency_ms: int
    max_calls: int
    task_budget_ratio: float

    def __post_init__(self) -> None:
        if self.max_cost < 0 or self.max_latency_ms < 0 or self.max_calls < 0:
            raise PlanningContractError("planning budget values cannot be negative")
        if not 0 <= self.task_budget_ratio <= 0.25:
            raise PlanningContractError("planning cannot exceed 25% of the task budget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost": round(self.max_cost, 6),
            "max_latency_ms": self.max_latency_ms,
            "max_calls": self.max_calls,
            "task_budget_ratio": round(self.task_budget_ratio, 4),
        }


@dataclass(frozen=True, slots=True)
class PanelPolicy:
    size: int
    required_diversity: tuple[str, ...] = ()
    selected_roles: tuple[str, ...] = ()
    selected_assignments: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.size <= 3:
            raise PlanningContractError("panel size must be between zero and three")
        if len(self.selected_roles) != self.size:
            raise PlanningContractError("panel size must equal selected role count")
        if len(self.selected_assignments) not in {0, self.size}:
            raise PlanningContractError("assignments must be empty or match panel size")

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "required_diversity": list(self.required_diversity),
            "selected_roles": list(self.selected_roles),
            "selected_assignments": [
                dict(item) for item in self.selected_assignments
            ],
        }


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role_id: str
    title: str
    objective: str
    required_capabilities: tuple[str, ...]
    evaluation_focus: tuple[str, ...]
    authority: tuple[str, ...] = ("propose",)
    forbidden_actions: tuple[str, ...] = (
        "spawn_planner",
        "change_budget",
        "execute_irreversible_action",
    )
    management_level: int = 2
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.role_id.strip() or not self.objective.strip():
            raise PlanningContractError("role id and objective are required")
        if self.management_level != 2:
            raise PlanningContractError("planner roles must remain at management level two")
        if "spawn_planner" not in self.forbidden_actions:
            raise PlanningContractError("planner roles must not create another planner layer")
        if not self.required_capabilities or not self.evaluation_focus:
            raise PlanningContractError("role capabilities and evaluation focus are required")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "required_capabilities",
            "evaluation_focus",
            "authority",
            "forbidden_actions",
        ):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True, slots=True)
class PlanProposal:
    proposal_id: str
    planner_role: str
    objective_interpretation: str
    decomposition_outline: tuple[str, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    acceptance_strategy: tuple[str, ...]
    estimated_planning_cost: float
    estimated_planning_latency_ms: int
    confidence: float
    assignment: Mapping[str, Any] = field(default_factory=dict)
    hard_constraints: tuple[str, ...] = ()
    strategy: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.proposal_id.strip() or not self.planner_role.strip():
            raise PlanningContractError("proposal id and planner role are required")
        if not self.decomposition_outline or not self.acceptance_strategy:
            raise PlanningContractError("proposal requires a decomposition and acceptance strategy")
        if self.estimated_planning_cost < 0 or self.estimated_planning_latency_ms < 0:
            raise PlanningContractError("proposal estimates cannot be negative")
        if not 0 <= self.confidence <= 1:
            raise PlanningContractError("proposal confidence must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "decomposition_outline",
            "assumptions",
            "risks",
            "acceptance_strategy",
            "hard_constraints",
            "evidence",
        ):
            data[key] = list(data[key])
        data["assignment"] = dict(self.assignment)
        data["strategy"] = dict(self.strategy)
        data["estimated_planning_cost"] = round(
            self.estimated_planning_cost, 6
        )
        data["confidence"] = round(self.confidence, 4)
        return data


@dataclass(frozen=True, slots=True)
class AnonymousProposal:
    """Identity-blind view used by the scorer and adjudicator."""

    alias: str
    objective_interpretation: str
    decomposition_outline: tuple[str, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    acceptance_strategy: tuple[str, ...]
    hard_constraints: tuple[str, ...]
    strategy: Mapping[str, Any]
    evidence: tuple[str, ...]
    confidence: float
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.alias.strip() or not self.decomposition_outline:
            raise PlanningContractError("anonymous proposal requires an alias and decomposition")
        if not 0 <= self.confidence <= 1:
            raise PlanningContractError("anonymous proposal confidence must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "decomposition_outline",
            "assumptions",
            "risks",
            "acceptance_strategy",
            "hard_constraints",
            "evidence",
        ):
            data[key] = list(data[key])
        data["strategy"] = dict(self.strategy)
        data["confidence"] = round(self.confidence, 4)
        return data


@dataclass(frozen=True, slots=True)
class ProposalScorecard:
    alias: str
    scores: Mapping[str, float]
    total: float
    hard_constraints_passed: bool
    failed_constraints: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.alias.strip() or not self.scores:
            raise PlanningContractError("scorecard requires an alias and scores")
        if any(not 0 <= float(value) <= 1 for value in self.scores.values()):
            raise PlanningContractError("score dimensions must be between zero and one")
        if not 0 <= self.total <= 1:
            raise PlanningContractError("score total must be between zero and one")
        if self.hard_constraints_passed and self.failed_constraints:
            raise PlanningContractError("passing scorecard cannot contain failed constraints")

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "scores": {
                key: round(float(value), 4)
                for key, value in self.scores.items()
            },
            "total": round(self.total, 4),
            "hard_constraints_passed": self.hard_constraints_passed,
            "failed_constraints": list(self.failed_constraints),
            "reasons": list(self.reasons),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ProposalConflict:
    conflict_id: str
    conflict_type: str
    severity: str
    aliases: tuple[str, ...]
    evidence: tuple[str, ...]
    resolution_required: bool
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.severity not in {"low", "medium", "high"}:
            raise PlanningContractError("conflict severity must be low, medium, or high")
        if len(self.aliases) < 2 or not self.evidence:
            raise PlanningContractError("conflict requires at least two aliases and evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "conflict_type": self.conflict_type,
            "severity": self.severity,
            "aliases": list(self.aliases),
            "evidence": list(self.evidence),
            "resolution_required": self.resolution_required,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    outcome: str
    rounds_used: int
    selected_aliases: tuple[str, ...]
    rejected_aliases: tuple[str, ...]
    rejection_reasons: Mapping[str, str]
    compiler_directives: tuple[str, ...]
    reasons: tuple[str, ...]
    confidence: float
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.outcome not in {"not_required", "selected", "merged", "degraded"}:
            raise PlanningContractError("unsupported adjudication outcome")
        if self.rounds_used not in {0, 1}:
            raise PlanningContractError("adjudication is limited to zero or one round")
        if not self.selected_aliases and self.outcome != "not_required":
            raise PlanningContractError("adjudication must select at least one proposal")
        if not 0 <= self.confidence <= 1:
            raise PlanningContractError("adjudication confidence must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "rounds_used": self.rounds_used,
            "selected_aliases": list(self.selected_aliases),
            "rejected_aliases": list(self.rejected_aliases),
            "rejection_reasons": dict(self.rejection_reasons),
            "compiler_directives": list(self.compiler_directives),
            "reasons": list(self.reasons),
            "confidence": round(self.confidence, 4),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class DeliberationReceipt:
    decision_id: str
    mode: PlanningMode
    parallel: bool
    proposal_count: int
    anonymous_proposals: tuple[AnonymousProposal, ...]
    scorecards: tuple[ProposalScorecard, ...]
    conflicts: tuple[ProposalConflict, ...]
    adjudication: AdjudicationResult
    actual_cost: float
    actual_latency_ms: int
    calls_used: int
    budget_degraded: bool
    early_stopped: bool
    stop_reason: str
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.proposal_count != len(self.anonymous_proposals):
            raise PlanningContractError("proposal count and anonymous proposal count disagree")
        if self.proposal_count != len(self.scorecards):
            raise PlanningContractError("every proposal requires one scorecard")
        if self.actual_cost < 0 or self.actual_latency_ms < 0 or self.calls_used < 0:
            raise PlanningContractError("deliberation measurements cannot be negative")
        if self.adjudication.rounds_used > 1:
            raise PlanningContractError("recursive adjudication is forbidden")

    def to_dict(self) -> dict[str, Any]:
        data = {
            "decision_id": self.decision_id,
            "mode": self.mode.value,
            "parallel": self.parallel,
            "proposal_count": self.proposal_count,
            "anonymous_proposals": [
                proposal.to_dict() for proposal in self.anonymous_proposals
            ],
            "scorecards": [score.to_dict() for score in self.scorecards],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "adjudication": self.adjudication.to_dict(),
            "actual_cost": round(self.actual_cost, 6),
            "actual_latency_ms": self.actual_latency_ms,
            "calls_used": self.calls_used,
            "budget_degraded": self.budget_degraded,
            "early_stopped": self.early_stopped,
            "stop_reason": self.stop_reason,
            "schema_version": self.schema_version,
        }
        semantic = dict(data)
        data["fingerprint"] = hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return data


DIFFICULTY_DIMENSIONS = (
    "ambiguity",
    "decomposition_uncertainty",
    "novelty",
    "domain_specialization",
    "cross_domain_coupling",
    "context_multimodal_load",
    "tool_chain_depth",
    "verification_difficulty",
    "error_loss",
    "irreversibility",
    "concurrency_deadline_pressure",
)


@dataclass(frozen=True, slots=True)
class DifficultyInterval:
    lower: float
    expected: float
    upper: float

    def __post_init__(self) -> None:
        if not 0 <= self.lower <= self.expected <= self.upper <= 1:
            raise PlanningContractError(
                "difficulty interval must be ordered within zero and one"
            )

    def to_dict(self) -> dict[str, float]:
        return {
            "lower": round(self.lower, 4),
            "expected": round(self.expected, 4),
            "upper": round(self.upper, 4),
        }


@dataclass(frozen=True, slots=True)
class TaskDifficultyVector:
    dimensions: Mapping[str, float]
    overall: DifficultyInterval
    confidence: float
    trigger_features: tuple[str, ...]
    classifier_id: str
    classifier_version: str

    def __post_init__(self) -> None:
        if set(self.dimensions) != set(DIFFICULTY_DIMENSIONS):
            raise PlanningContractError(
                "difficulty vector must contain the frozen eleven dimensions"
            )
        if any(not 0 <= float(value) <= 1 for value in self.dimensions.values()):
            raise PlanningContractError(
                "difficulty dimensions must be between zero and one"
            )
        if not 0 <= self.confidence <= 1:
            raise PlanningContractError(
                "difficulty confidence must be between zero and one"
            )
        if not self.classifier_id or not self.classifier_version:
            raise PlanningContractError("difficulty classifier identity is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": {
                key: round(float(self.dimensions[key]), 4)
                for key in DIFFICULTY_DIMENSIONS
            },
            "overall": self.overall.to_dict(),
            "confidence": round(self.confidence, 4),
            "trigger_features": list(self.trigger_features),
            "classifier_id": self.classifier_id,
            "classifier_version": self.classifier_version,
        }


@dataclass(frozen=True, slots=True)
class ExpectedLoss:
    money: float
    time: float
    failure: float
    error_risk: float
    coordination: float

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.money,
                self.time,
                self.failure,
                self.error_risk,
                self.coordination,
            )
        ):
            raise PlanningContractError("expected loss components cannot be negative")

    @property
    def total(self) -> float:
        return (
            self.money
            + self.time
            + self.failure
            + self.error_risk
            + self.coordination
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "money": round(self.money, 6),
            "time": round(self.time, 6),
            "failure": round(self.failure, 6),
            "error_risk": round(self.error_risk, 6),
            "coordination": round(self.coordination, 6),
            "total": round(self.total, 6),
        }


@dataclass(frozen=True, slots=True)
class PlanningCandidate:
    mode: PlanningMode
    feasible: bool
    expected_success: float
    success_lower_bound: float
    expected_cost: float
    expected_latency_ms: int
    planning_cost: float
    planning_latency_ms: int
    expected_loss: ExpectedLoss
    hard_rejections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.success_lower_bound <= self.expected_success <= 1:
            raise PlanningContractError(
                "candidate success estimates must be ordered within zero and one"
            )
        if any(
            value < 0
            for value in (
                self.expected_cost,
                self.expected_latency_ms,
                self.planning_cost,
                self.planning_latency_ms,
            )
        ):
            raise PlanningContractError("candidate estimates cannot be negative")
        if self.feasible and self.hard_rejections:
            raise PlanningContractError(
                "feasible candidate cannot contain hard rejections"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "feasible": self.feasible,
            "expected_success": round(self.expected_success, 4),
            "success_lower_bound": round(self.success_lower_bound, 4),
            "expected_cost": round(self.expected_cost, 6),
            "expected_latency_ms": self.expected_latency_ms,
            "planning_cost": round(self.planning_cost, 6),
            "planning_latency_ms": self.planning_latency_ms,
            "expected_loss": self.expected_loss.to_dict(),
            "hard_rejections": list(self.hard_rejections),
        }


@dataclass(frozen=True, slots=True)
class PlanningDecision:
    decision_id: str
    mode: PlanningMode
    confidence: float
    reasons: tuple[str, ...]
    rejected_modes: Mapping[str, str]
    planning_budget: PlanningBudget
    panel_policy: PanelPolicy
    stop_conditions: tuple[str, ...]
    escalation_conditions: tuple[str, ...]
    limits: ManagementLimits = field(default_factory=ManagementLimits)
    difficulty_vector: TaskDifficultyVector | None = None
    candidates: tuple[PlanningCandidate, ...] = ()
    value_of_deliberation: float = 0.0
    vod_margin: float = 0.0
    pre_execution_escalation: Mapping[str, Any] = field(
        default_factory=dict
    )
    policy_version: str = PLANNING_POLICY_VERSION
    schema_version: str = PLANNING_SCHEMA_VERSION
    simulated: bool = False

    def __post_init__(self) -> None:
        if not self.decision_id.strip() or not self.reasons:
            raise PlanningContractError("decision id and reasons are required")
        if not 0 <= self.confidence <= 1:
            raise PlanningContractError("decision confidence must be between zero and one")
        expected_panel = {
            PlanningMode.NO_DELEGATION: 0,
            PlanningMode.ASK_USER: 0,
            PlanningMode.DIRECT_DELEGATION: 1,
            PlanningMode.SINGLE_PLANNER: 1,
            PlanningMode.EXPERT_OVERRIDE: 1,
        }
        if self.mode in expected_panel and self.panel_policy.size != expected_panel[self.mode]:
            raise PlanningContractError("planning mode and panel size disagree")
        if self.mode == PlanningMode.MULTI_PLANNER and not 2 <= self.panel_policy.size <= 3:
            raise PlanningContractError("MultiPlanner requires two or three roles")
        if self.panel_policy.size > self.limits.max_planners_per_round:
            raise PlanningContractError("panel exceeds the hard planner limit")
        if self.value_of_deliberation < 0 or self.vod_margin < 0:
            raise PlanningContractError("VoD values cannot be negative")
        if self.candidates:
            selected = [
                item
                for item in self.candidates
                if item.mode == self.mode and item.feasible
            ]
            if not selected:
                raise PlanningContractError(
                    "selected planning mode must be a feasible candidate"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "decision_id": self.decision_id,
            "mode": self.mode.value,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
            "rejected_modes": dict(self.rejected_modes),
            "planning_budget": self.planning_budget.to_dict(),
            "panel_policy": self.panel_policy.to_dict(),
            "stop_conditions": list(self.stop_conditions),
            "escalation_conditions": list(self.escalation_conditions),
            "limits": self.limits.to_dict(),
            "difficulty_vector": (
                self.difficulty_vector.to_dict()
                if self.difficulty_vector is not None
                else None
            ),
            "candidates": [
                candidate.to_dict() for candidate in self.candidates
            ],
            "value_of_deliberation": round(
                self.value_of_deliberation, 6
            ),
            "vod_margin": round(self.vod_margin, 6),
            "pre_execution_escalation": dict(
                self.pre_execution_escalation
            ),
            "simulated": self.simulated,
            "fingerprint": self.fingerprint(),
        }

    def fingerprint(self) -> str:
        semantic = {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "decision_id": self.decision_id,
            "mode": self.mode.value,
            "confidence": round(self.confidence, 6),
            "reasons": list(self.reasons),
            "rejected_modes": dict(self.rejected_modes),
            "planning_budget": self.planning_budget.to_dict(),
            "panel_policy": self.panel_policy.to_dict(),
            "stop_conditions": list(self.stop_conditions),
            "escalation_conditions": list(self.escalation_conditions),
            "limits": self.limits.to_dict(),
            "difficulty_vector": (
                self.difficulty_vector.to_dict()
                if self.difficulty_vector is not None
                else None
            ),
            "candidates": [
                candidate.to_dict() for candidate in self.candidates
            ],
            "value_of_deliberation": round(
                self.value_of_deliberation, 8
            ),
            "vod_margin": round(self.vod_margin, 8),
            "pre_execution_escalation": dict(
                self.pre_execution_escalation
            ),
            "simulated": self.simulated,
        }
        return hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkOrder:
    work_order_id: str
    task_id: str
    objective: str
    dependencies: tuple[str, ...]
    input_artifacts: tuple[str, ...]
    context_manifest: tuple[Mapping[str, Any], ...]
    assumptions: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    output_schema: Mapping[str, Any]
    acceptance_tests: tuple[str, ...]
    cost_limit: float
    time_limit_ms: int
    escalation_conditions: tuple[str, ...]
    idempotency_key: str
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.work_order_id or not self.task_id or not self.objective.strip():
            raise PlanningContractError("work order identity and objective are required")
        if not self.acceptance_tests or not self.output_schema:
            raise PlanningContractError("work order requires output schema and acceptance tests")
        if self.cost_limit < 0 or self.time_limit_ms <= 0:
            raise PlanningContractError("work order limits are invalid")
        if not self.idempotency_key.strip():
            raise PlanningContractError("work order idempotency key is required")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "dependencies",
            "input_artifacts",
            "context_manifest",
            "assumptions",
            "allowed_tools",
            "forbidden_actions",
            "acceptance_tests",
            "escalation_conditions",
        ):
            data[key] = list(data[key])
        data["output_schema"] = dict(self.output_schema)
        return data


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    work_order_id: str
    status: str
    output: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...]
    acceptance_results: tuple[Mapping[str, Any], ...]
    assumptions_used: tuple[str, ...]
    unresolved_risks: tuple[str, ...]
    actual_cost: float
    actual_latency_ms: int
    confidence: float
    escalation_requested: bool = False
    escalation_reason: str = ""
    schema_version: str = PLANNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed", "rate_limited", "escalated"}:
            raise PlanningContractError("unsupported result status")
        if self.actual_cost < 0 or self.actual_latency_ms < 0:
            raise PlanningContractError("result measurements cannot be negative")
        if not 0 <= self.confidence <= 1:
            raise PlanningContractError("result confidence must be between zero and one")
        if self.escalation_requested and not self.escalation_reason.strip():
            raise PlanningContractError("escalation requires a reason")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "evidence",
            "acceptance_results",
            "assumptions_used",
            "unresolved_risks",
        ):
            data[key] = list(data[key])
        data["output"] = dict(self.output)
        return data
