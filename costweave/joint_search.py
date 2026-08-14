"""Deterministic joint plan/route/schedule search for CostWeave v0.6.3.

The implementation is deliberately provider-free.  It searches complete
strategy candidates (management mode, task DAG, route, schedule, validation
and recovery) instead of selecting a model one node at a time.  Every
candidate carries its evidence, hard constraints and compiler audit so the
same object can be replayed by an offline tournament or an advisory service.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .common import (
    MANAGEMENT_MODES,
    NonNegativeEstimate,
    ProbabilityInterval,
    RECOVERY_POLICIES,
    RISK_LEVELS,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_digest,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
)
from .strategy import StrategyCandidateV1, StrategyNodeV1


JOINT_SEARCH_SCHEMA_VERSION = "v0.6.3"
JOINT_SEARCH_POLICY_REVISION = "joint-plan-route-schedule-search-v1"
JOINT_SEARCH_ACTIONS = ("SELECT", "ABSTAIN", "ESCALATE")
JOINT_SEARCHERS = ("greedy", "beam", "successive_halving", "pareto")
JOINT_CANDIDATE_SOURCES = (
    "safety_baseline",
    "historical",
    "planner",
    "local_transform",
)


def _non_negative_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0 or (maximum is not None and value > maximum):
        suffix = f" <= {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be between 0 and{suffix}")
    return value


def _positive_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    result = _non_negative_int(value, name, maximum=maximum)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _mapping_text(value: Any, name: str, *, maximum_items: int = 128) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if len(value) > maximum_items:
        raise ValueError(f"{name} has too many entries")
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = bounded_text(key, f"{name}.key", maximum=128)
        result[key_text] = bounded_text(item, f"{name}.{key_text}", maximum=512)
    return dict(sorted(result.items()))


def _route_pairs(value: Mapping[str, str], name: str = "node_routes") -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    pairs: list[tuple[str, str]] = []
    for node_id, route_id in sorted(value.items()):
        pairs.append((bounded_text(node_id, f"{name}.node_id"), bounded_text(route_id, f"{name}.route_id")))
    return tuple(pairs)


def _pairs_to_map(value: Iterable[tuple[str, str]], name: str = "pairs") -> dict[str, str]:
    result: dict[str, str] = {}
    for node_id, route_id in value:
        if node_id in result:
            raise ValueError(f"{name} contains duplicate node_id")
        result[node_id] = route_id
    return result


@dataclass(frozen=True, slots=True)
class JointSearchBudgetV1:
    """Money, token, wall-clock and search-evaluation limits."""

    money_usd: float = 1.0
    input_tokens: int = 32_000
    wall_clock_ms: int = 30_000
    max_evaluations: int = 128
    beam_width: int = 8
    max_candidates: int = 512
    amortization_count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "money_usd", finite_number(self.money_usd, "money_usd", minimum=0.0))
        object.__setattr__(self, "input_tokens", _positive_int(self.input_tokens, "input_tokens", maximum=10_000_000))
        object.__setattr__(self, "wall_clock_ms", _positive_int(self.wall_clock_ms, "wall_clock_ms", maximum=86_400_000))
        object.__setattr__(self, "max_evaluations", _positive_int(self.max_evaluations, "max_evaluations", maximum=10_000))
        object.__setattr__(self, "beam_width", _positive_int(self.beam_width, "beam_width", maximum=512))
        object.__setattr__(self, "max_candidates", _positive_int(self.max_candidates, "max_candidates", maximum=20_000))
        object.__setattr__(self, "amortization_count", _positive_int(self.amortization_count, "amortization_count", maximum=1_000_000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "money_usd": self.money_usd,
            "input_tokens": self.input_tokens,
            "wall_clock_ms": self.wall_clock_ms,
            "max_evaluations": self.max_evaluations,
            "beam_width": self.beam_width,
            "max_candidates": self.max_candidates,
            "amortization_count": self.amortization_count,
            "schema_version": JOINT_SEARCH_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointSearchBudgetV1":
        validate_exact_fields(
            value,
            required=frozenset({"money_usd", "input_tokens", "wall_clock_ms", "max_evaluations", "beam_width", "max_candidates", "amortization_count", "schema_version"}),
        )
        if value["schema_version"] != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported joint search budget schema_version")
        return cls(**{key: value[key] for key in ("money_usd", "input_tokens", "wall_clock_ms", "max_evaluations", "beam_width", "max_candidates", "amortization_count")})


@dataclass(frozen=True, slots=True)
class JointTaskNodeV1:
    node_id: str
    objective_hash: str
    dependencies: tuple[str, ...] = ()
    deliverables: tuple[str, ...] = ()
    required_authority_scopes: tuple[str, ...] = ()
    risk_level: str = "low"
    context_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", bounded_text(self.node_id, "node_id", maximum=128))
        object.__setattr__(self, "objective_hash", validate_sha256(self.objective_hash, "objective_hash"))
        object.__setattr__(self, "dependencies", unique_texts(self.dependencies, "dependencies", maximum_items=128))
        object.__setattr__(self, "deliverables", unique_texts(self.deliverables, "deliverables", maximum_items=128))
        object.__setattr__(self, "required_authority_scopes", unique_texts(self.required_authority_scopes, "required_authority_scopes", maximum_items=64))
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"unsupported risk_level: {self.risk_level!r}")
        object.__setattr__(self, "context_tokens", _non_negative_int(self.context_tokens, "context_tokens", maximum=10_000_000))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "objective_hash": self.objective_hash,
            "dependencies": list(self.dependencies),
            "deliverables": list(self.deliverables),
            "required_authority_scopes": list(self.required_authority_scopes),
            "risk_level": self.risk_level,
            "context_tokens": self.context_tokens,
            "schema_version": JOINT_SEARCH_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointTaskNodeV1":
        required = frozenset({"node_id", "objective_hash", "dependencies", "deliverables", "required_authority_scopes", "risk_level", "context_tokens", "schema_version"})
        validate_exact_fields(value, required=required)
        if value["schema_version"] != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported joint task node schema_version")
        return cls(
            node_id=value["node_id"], objective_hash=value["objective_hash"], dependencies=tuple(value["dependencies"]),
            deliverables=tuple(value["deliverables"]), required_authority_scopes=tuple(value["required_authority_scopes"]),
            risk_level=value["risk_level"], context_tokens=value["context_tokens"],
        )


@dataclass(frozen=True, slots=True)
class JointRouteOptionV1:
    route_id: str
    executor_id: str
    reasoning_tier: str
    validator_id: str
    tool_ids: tuple[str, ...] = ()
    authority_scopes: tuple[str, ...] = ()
    predicted_success: ProbabilityInterval = field(default_factory=lambda: ProbabilityInterval(0.5, 0.2, 0.8))
    predicted_cost_usd: NonNegativeEstimate = field(default_factory=lambda: NonNegativeEstimate(0.01, 0.02))
    validation_cost_usd: NonNegativeEstimate = field(default_factory=lambda: NonNegativeEstimate(0.0, 0.0))
    predicted_latency_ms: NonNegativeEstimate = field(default_factory=lambda: NonNegativeEstimate(100.0, 200.0))
    validation_latency_ms: NonNegativeEstimate = field(default_factory=lambda: NonNegativeEstimate(0.0, 0.0))
    failure_correlation_group: str = "unknown"
    handoff_fields: Mapping[str, str] = field(default_factory=dict)
    information_loss: float = 0.0
    capacity_units: int = 1
    cost_known: bool = True
    latency_known: bool = True

    def __post_init__(self) -> None:
        for name in ("route_id", "executor_id", "reasoning_tier", "validator_id", "failure_correlation_group"):
            object.__setattr__(self, name, bounded_text(getattr(self, name), name))
        object.__setattr__(self, "tool_ids", unique_texts(self.tool_ids, "tool_ids", maximum_items=64))
        object.__setattr__(self, "authority_scopes", unique_texts(self.authority_scopes, "authority_scopes", maximum_items=64))
        if not isinstance(self.predicted_success, ProbabilityInterval):
            raise TypeError("predicted_success must be ProbabilityInterval")
        for name in ("predicted_cost_usd", "validation_cost_usd", "predicted_latency_ms", "validation_latency_ms"):
            if not isinstance(getattr(self, name), NonNegativeEstimate):
                raise TypeError(f"{name} must be NonNegativeEstimate")
        object.__setattr__(self, "handoff_fields", _mapping_text(self.handoff_fields, "handoff_fields"))
        object.__setattr__(self, "information_loss", probability(self.information_loss, "information_loss"))
        object.__setattr__(self, "capacity_units", _positive_int(self.capacity_units, "capacity_units", maximum=1_000))
        object.__setattr__(self, "cost_known", _bool(self.cost_known, "cost_known"))
        object.__setattr__(self, "latency_known", _bool(self.latency_known, "latency_known"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id, "executor_id": self.executor_id, "reasoning_tier": self.reasoning_tier,
            "validator_id": self.validator_id, "tool_ids": list(self.tool_ids), "authority_scopes": list(self.authority_scopes),
            "predicted_success": self.predicted_success.to_dict(), "predicted_cost_usd": self.predicted_cost_usd.to_dict(),
            "validation_cost_usd": self.validation_cost_usd.to_dict(), "predicted_latency_ms": self.predicted_latency_ms.to_dict(),
            "validation_latency_ms": self.validation_latency_ms.to_dict(), "failure_correlation_group": self.failure_correlation_group,
            "handoff_fields": dict(self.handoff_fields), "information_loss": self.information_loss, "capacity_units": self.capacity_units,
            "cost_known": self.cost_known, "latency_known": self.latency_known, "schema_version": JOINT_SEARCH_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointRouteOptionV1":
        required = frozenset({"route_id", "executor_id", "reasoning_tier", "validator_id", "tool_ids", "authority_scopes", "predicted_success", "predicted_cost_usd", "validation_cost_usd", "predicted_latency_ms", "validation_latency_ms", "failure_correlation_group", "handoff_fields", "information_loss", "capacity_units", "cost_known", "latency_known", "schema_version"})
        validate_exact_fields(value, required=required)
        if value["schema_version"] != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported joint route option schema_version")
        return cls(
            route_id=value["route_id"], executor_id=value["executor_id"], reasoning_tier=value["reasoning_tier"], validator_id=value["validator_id"],
            tool_ids=tuple(value["tool_ids"]), authority_scopes=tuple(value["authority_scopes"]), predicted_success=ProbabilityInterval.from_dict(value["predicted_success"]),
            predicted_cost_usd=NonNegativeEstimate.from_dict(value["predicted_cost_usd"]), validation_cost_usd=NonNegativeEstimate.from_dict(value["validation_cost_usd"]),
            predicted_latency_ms=NonNegativeEstimate.from_dict(value["predicted_latency_ms"]), validation_latency_ms=NonNegativeEstimate.from_dict(value["validation_latency_ms"]),
            failure_correlation_group=value["failure_correlation_group"], handoff_fields=value["handoff_fields"], information_loss=value["information_loss"],
            capacity_units=value["capacity_units"], cost_known=value["cost_known"], latency_known=value["latency_known"],
        )


@dataclass(frozen=True, slots=True)
class JointRouteSetV1:
    node_id: str
    options: tuple[JointRouteOptionV1, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", bounded_text(self.node_id, "node_id", maximum=128))
        if not isinstance(self.options, (tuple, list)) or not self.options:
            raise ValueError("each node must have at least one route option")
        normalized = tuple(self.options)
        if any(not isinstance(item, JointRouteOptionV1) for item in normalized):
            raise TypeError("options must contain JointRouteOptionV1")
        if len({item.route_id for item in normalized}) != len(normalized):
            raise ValueError("route_id must be unique within a node")
        object.__setattr__(self, "options", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "options": [item.to_dict() for item in self.options], "schema_version": JOINT_SEARCH_SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointRouteSetV1":
        validate_exact_fields(value, required=frozenset({"node_id", "options", "schema_version"}))
        if value["schema_version"] != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported joint route set schema_version")
        return cls(node_id=value["node_id"], options=tuple(JointRouteOptionV1.from_dict(item) for item in value["options"]))


@dataclass(frozen=True, slots=True)
class JointSearchProblemV1:
    project_id: str
    case_id: str
    task_family: str
    nodes: tuple[JointTaskNodeV1, ...]
    route_sets: tuple[JointRouteSetV1, ...]
    budget: JointSearchBudgetV1 = field(default_factory=JointSearchBudgetV1)
    deadline_ms: int = 30_000
    min_success_lower: float = 0.5
    max_parallelism: int = 4
    max_information_loss: float = 0.2
    failure_loss_usd: float = 1.0
    latency_weight_usd_per_second: float = 0.001
    allowed_tools: tuple[str, ...] = ()
    allowed_authority_scopes: tuple[str, ...] = ()
    required_deliverables: tuple[str, ...] = ()
    management_modes: tuple[str, ...] = ("no_delegation", "planner", "multiplanner")
    recovery_policies: tuple[str, ...] = ("retry_node", "subgraph_first", "full_replan", "stop_partial")
    historical_route_ids: Mapping[str, str] = field(default_factory=dict)
    planner_route_ids: Mapping[str, str] = field(default_factory=dict)
    reuse_count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id", maximum=200))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        task_family = bounded_text(self.task_family, "task_family", maximum=64)
        if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for char in task_family):
            raise ValueError("task_family must use lowercase slug characters")
        object.__setattr__(self, "task_family", task_family)
        if not isinstance(self.nodes, (tuple, list)) or not self.nodes:
            raise ValueError("nodes must not be empty")
        nodes = tuple(self.nodes)
        if any(not isinstance(item, JointTaskNodeV1) for item in nodes):
            raise TypeError("nodes must contain JointTaskNodeV1")
        node_ids = {item.node_id for item in nodes}
        if len(node_ids) != len(nodes):
            raise ValueError("node_id must be unique")
        for item in nodes:
            missing = set(item.dependencies) - node_ids
            if missing:
                raise ValueError(f"node dependencies do not exist: {sorted(missing)!r}")
        object.__setattr__(self, "nodes", nodes)
        route_sets = tuple(self.route_sets)
        if {item.node_id for item in route_sets} != node_ids:
            raise ValueError("route_sets must cover each task node exactly once")
        if any(not isinstance(item, JointRouteSetV1) for item in route_sets):
            raise TypeError("route_sets must contain JointRouteSetV1")
        object.__setattr__(self, "route_sets", tuple(sorted(route_sets, key=lambda item: item.node_id)))
        object.__setattr__(self, "deadline_ms", _positive_int(self.deadline_ms, "deadline_ms", maximum=86_400_000))
        object.__setattr__(self, "min_success_lower", probability(self.min_success_lower, "min_success_lower"))
        object.__setattr__(self, "max_parallelism", _positive_int(self.max_parallelism, "max_parallelism", maximum=1_000))
        object.__setattr__(self, "max_information_loss", probability(self.max_information_loss, "max_information_loss"))
        object.__setattr__(self, "failure_loss_usd", finite_number(self.failure_loss_usd, "failure_loss_usd", minimum=0.0))
        object.__setattr__(self, "latency_weight_usd_per_second", finite_number(self.latency_weight_usd_per_second, "latency_weight_usd_per_second", minimum=0.0))
        object.__setattr__(self, "allowed_tools", unique_texts(self.allowed_tools, "allowed_tools", maximum_items=256))
        object.__setattr__(self, "allowed_authority_scopes", unique_texts(self.allowed_authority_scopes, "allowed_authority_scopes", maximum_items=128))
        object.__setattr__(self, "required_deliverables", unique_texts(self.required_deliverables, "required_deliverables", maximum_items=256))
        modes = unique_texts(self.management_modes, "management_modes", maximum_items=8)
        if any(mode not in MANAGEMENT_MODES for mode in modes):
            raise ValueError("unsupported management mode")
        object.__setattr__(self, "management_modes", modes)
        policies = unique_texts(self.recovery_policies, "recovery_policies", maximum_items=8)
        if any(policy not in RECOVERY_POLICIES for policy in policies):
            raise ValueError("unsupported recovery policy")
        object.__setattr__(self, "recovery_policies", policies)
        historical = _mapping_text(self.historical_route_ids, "historical_route_ids")
        planner = _mapping_text(self.planner_route_ids, "planner_route_ids")
        if set(historical) - node_ids or set(planner) - node_ids:
            raise ValueError("route preference contains unknown node")
        object.__setattr__(self, "historical_route_ids", historical)
        object.__setattr__(self, "planner_route_ids", planner)
        object.__setattr__(self, "reuse_count", _positive_int(self.reuse_count, "reuse_count", maximum=1_000_000))
        self.topological_order()

    def topological_order(self) -> tuple[str, ...]:
        dependencies = {item.node_id: set(item.dependencies) for item in self.nodes}
        order: list[str] = []
        remaining = set(dependencies)
        while remaining:
            ready = sorted(node_id for node_id in remaining if not (dependencies[node_id] & remaining))
            if not ready:
                raise ValueError("task graph contains a cycle")
            order.extend(ready)
            remaining.difference_update(ready)
        return tuple(order)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "case_id": self.case_id, "task_family": self.task_family,
            "nodes": [item.to_dict() for item in self.nodes], "route_sets": [item.to_dict() for item in self.route_sets],
            "budget": self.budget.to_dict(), "deadline_ms": self.deadline_ms, "min_success_lower": self.min_success_lower,
            "max_parallelism": self.max_parallelism, "max_information_loss": self.max_information_loss, "failure_loss_usd": self.failure_loss_usd,
            "latency_weight_usd_per_second": self.latency_weight_usd_per_second, "allowed_tools": list(self.allowed_tools),
            "allowed_authority_scopes": list(self.allowed_authority_scopes), "required_deliverables": list(self.required_deliverables),
            "management_modes": list(self.management_modes), "recovery_policies": list(self.recovery_policies),
            "historical_route_ids": dict(self.historical_route_ids), "planner_route_ids": dict(self.planner_route_ids),
            "reuse_count": self.reuse_count, "schema_version": JOINT_SEARCH_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointSearchProblemV1":
        required = frozenset({"project_id", "case_id", "task_family", "nodes", "route_sets", "budget", "deadline_ms", "min_success_lower", "max_parallelism", "max_information_loss", "failure_loss_usd", "latency_weight_usd_per_second", "allowed_tools", "allowed_authority_scopes", "required_deliverables", "management_modes", "recovery_policies", "historical_route_ids", "planner_route_ids", "reuse_count", "schema_version"})
        validate_exact_fields(value, required=required)
        if value["schema_version"] != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported joint search problem schema_version")
        return cls(
            project_id=value["project_id"], case_id=value["case_id"], task_family=value["task_family"],
            nodes=tuple(JointTaskNodeV1.from_dict(item) for item in value["nodes"]),
            route_sets=tuple(JointRouteSetV1.from_dict(item) for item in value["route_sets"]), budget=JointSearchBudgetV1.from_dict(value["budget"]),
            deadline_ms=value["deadline_ms"], min_success_lower=value["min_success_lower"], max_parallelism=value["max_parallelism"],
            max_information_loss=value["max_information_loss"], failure_loss_usd=value["failure_loss_usd"], latency_weight_usd_per_second=value["latency_weight_usd_per_second"],
            allowed_tools=tuple(value["allowed_tools"]), allowed_authority_scopes=tuple(value["allowed_authority_scopes"]), required_deliverables=tuple(value["required_deliverables"]),
            management_modes=tuple(value["management_modes"]), recovery_policies=tuple(value["recovery_policies"]), historical_route_ids=value["historical_route_ids"], planner_route_ids=value["planner_route_ids"], reuse_count=value["reuse_count"],
        )


@dataclass(frozen=True, slots=True)
class PromptHandoffAuditV1:
    coverage: float
    contradiction_count: int
    authority_ok: bool
    information_loss: float
    passed: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "coverage", probability(self.coverage, "coverage"))
        object.__setattr__(self, "contradiction_count", _non_negative_int(self.contradiction_count, "contradiction_count", maximum=10_000))
        object.__setattr__(self, "authority_ok", _bool(self.authority_ok, "authority_ok"))
        object.__setattr__(self, "information_loss", probability(self.information_loss, "information_loss"))
        object.__setattr__(self, "passed", _bool(self.passed, "passed"))
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=32))

    def to_dict(self) -> dict[str, Any]:
        return {"coverage": self.coverage, "contradiction_count": self.contradiction_count, "authority_ok": self.authority_ok, "information_loss": self.information_loss, "passed": self.passed, "reason_codes": list(self.reason_codes), "schema_version": JOINT_SEARCH_SCHEMA_VERSION}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PromptHandoffAuditV1":
        validate_exact_fields(value, required=frozenset({"coverage", "contradiction_count", "authority_ok", "information_loss", "passed", "reason_codes", "schema_version"}))
        if value["schema_version"] != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError("unsupported prompt handoff audit schema_version")
        return cls(coverage=value["coverage"], contradiction_count=value["contradiction_count"], authority_ok=value["authority_ok"], information_loss=value["information_loss"], passed=value["passed"], reason_codes=tuple(value["reason_codes"]))


@dataclass(frozen=True, slots=True)
class JointSearchCandidateV1:
    case_id: str
    source: str
    strategy: StrategyCandidateV1
    node_routes: tuple[tuple[str, str], ...]
    parallel_width: int
    expected_loss: float
    cvar_tail_loss: float
    compiler_audit: PromptHandoffAuditV1
    hard_constraint_checks: Mapping[str, bool]
    candidate_id: str | None = None
    schema_version: str = JOINT_SEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        if self.source not in JOINT_CANDIDATE_SOURCES:
            raise ValueError(f"unsupported candidate source: {self.source!r}")
        if not isinstance(self.strategy, StrategyCandidateV1):
            raise TypeError("strategy must be StrategyCandidateV1")
        pairs = _route_pairs(_pairs_to_map(self.node_routes))
        object.__setattr__(self, "node_routes", pairs)
        object.__setattr__(self, "parallel_width", _positive_int(self.parallel_width, "parallel_width", maximum=10_000))
        object.__setattr__(self, "expected_loss", finite_number(self.expected_loss, "expected_loss", minimum=0.0))
        object.__setattr__(self, "cvar_tail_loss", finite_number(self.cvar_tail_loss, "cvar_tail_loss", minimum=0.0))
        if not isinstance(self.compiler_audit, PromptHandoffAuditV1):
            raise TypeError("compiler_audit must be PromptHandoffAuditV1")
        if not isinstance(self.hard_constraint_checks, Mapping) or not self.hard_constraint_checks:
            raise ValueError("hard_constraint_checks must not be empty")
        checks: dict[str, bool] = {}
        for key, value in self.hard_constraint_checks.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, bool):
                raise TypeError("hard constraint checks must map strings to bool")
            checks[key] = value
        object.__setattr__(self, "hard_constraint_checks", dict(sorted(checks.items())))
        if self.schema_version != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {JOINT_SEARCH_SCHEMA_VERSION}")
        if self.candidate_id is not None and (not isinstance(self.candidate_id, str) or not self.candidate_id.strip()):
            raise ValueError("candidate_id must be non-empty when supplied")
        if self.candidate_id is not None and self.candidate_id != self.computed_id:
            raise ValueError("candidate_id does not match candidate content")

    @property
    def feasible(self) -> bool:
        return all(self.hard_constraint_checks.values()) and self.compiler_audit.passed

    @property
    def computed_id(self) -> str:
        return stable_id("joint_candidate", self._identity())

    def _identity(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "source": self.source, "strategy": self.strategy.to_dict(),
            "node_routes": [list(item) for item in self.node_routes], "parallel_width": self.parallel_width,
            "expected_loss": self.expected_loss, "cvar_tail_loss": self.cvar_tail_loss,
            "compiler_audit": self.compiler_audit.to_dict(), "hard_constraint_checks": dict(self.hard_constraint_checks), "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["candidate_id"] = self.computed_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointSearchCandidateV1":
        required = frozenset({"case_id", "source", "strategy", "node_routes", "parallel_width", "expected_loss", "cvar_tail_loss", "compiler_audit", "hard_constraint_checks", "schema_version"})
        validate_exact_fields(value, required=required, optional=frozenset({"candidate_id"}))
        return cls(case_id=value["case_id"], source=value["source"], strategy=StrategyCandidateV1.from_dict(value["strategy"]), node_routes=tuple((item[0], item[1]) for item in value["node_routes"]), parallel_width=value["parallel_width"], expected_loss=value["expected_loss"], cvar_tail_loss=value["cvar_tail_loss"], compiler_audit=PromptHandoffAuditV1.from_dict(value["compiler_audit"]), hard_constraint_checks=value["hard_constraint_checks"], candidate_id=value.get("candidate_id"), schema_version=value["schema_version"])


@dataclass(frozen=True, slots=True)
class JointSearchDecisionV1:
    decision_id: str
    project_id: str
    case_id: str
    action: str
    selected_candidate_id: str | None
    selected_candidate: JointSearchCandidateV1 | None
    candidate_ids: tuple[str, ...]
    pareto_frontier_ids: tuple[str, ...]
    evaluated_count: int
    feasible_count: int
    pruned_count: int
    searcher: str
    budget_used: Mapping[str, int | float]
    stop_reason: str
    reason_codes: tuple[str, ...]
    advisory_only: bool = True
    provider_calls: int = 0
    network_calls: int = 0
    commercial_claims_allowed: bool = False
    trace_hash: str = ""
    schema_version: str = JOINT_SEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", bounded_text(self.decision_id, "decision_id"))
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id", maximum=200))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        if self.action not in JOINT_SEARCH_ACTIONS:
            raise ValueError(f"unsupported joint search action: {self.action!r}")
        if self.selected_candidate is not None and not isinstance(self.selected_candidate, JointSearchCandidateV1):
            raise TypeError("selected_candidate must be JointSearchCandidateV1 or None")
        if self.selected_candidate_id is not None:
            object.__setattr__(self, "selected_candidate_id", bounded_text(self.selected_candidate_id, "selected_candidate_id"))
        if self.action == "SELECT" and (self.selected_candidate is None or self.selected_candidate_id != self.selected_candidate.computed_id):
            raise ValueError("SELECT requires a matching selected candidate")
        if self.action != "SELECT" and self.selected_candidate is not None:
            raise ValueError("non-SELECT decisions cannot contain a selected candidate")
        object.__setattr__(self, "candidate_ids", unique_texts(self.candidate_ids, "candidate_ids", maximum_items=20_000))
        object.__setattr__(self, "pareto_frontier_ids", unique_texts(self.pareto_frontier_ids, "pareto_frontier_ids", maximum_items=20_000))
        object.__setattr__(self, "evaluated_count", _non_negative_int(self.evaluated_count, "evaluated_count", maximum=20_000))
        object.__setattr__(self, "feasible_count", _non_negative_int(self.feasible_count, "feasible_count", maximum=20_000))
        object.__setattr__(self, "pruned_count", _non_negative_int(self.pruned_count, "pruned_count", maximum=20_000))
        if self.searcher not in JOINT_SEARCHERS:
            raise ValueError("unsupported joint searcher")
        object.__setattr__(self, "budget_used", dict(self.budget_used))
        object.__setattr__(self, "stop_reason", bounded_text(self.stop_reason, "stop_reason", maximum=512))
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=64))
        if not _bool(self.advisory_only, "advisory_only") or self.provider_calls != 0 or self.network_calls != 0 or self.commercial_claims_allowed:
            raise ValueError("v0.6.3 decisions are advisory-only and provider-free")
        object.__setattr__(self, "provider_calls", _non_negative_int(self.provider_calls, "provider_calls"))
        object.__setattr__(self, "network_calls", _non_negative_int(self.network_calls, "network_calls"))
        object.__setattr__(self, "trace_hash", self._trace_hash() if not self.trace_hash else validate_sha256(self.trace_hash, "trace_hash"))
        if self.schema_version != JOINT_SEARCH_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {JOINT_SEARCH_SCHEMA_VERSION}")

    def _trace_identity(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "project_id": self.project_id, "case_id": self.case_id, "action": self.action,
            "selected_candidate_id": self.selected_candidate_id, "candidate_ids": list(self.candidate_ids), "pareto_frontier_ids": list(self.pareto_frontier_ids),
            "evaluated_count": self.evaluated_count, "feasible_count": self.feasible_count, "pruned_count": self.pruned_count,
            "searcher": self.searcher, "budget_used": dict(self.budget_used), "stop_reason": self.stop_reason, "reason_codes": list(self.reason_codes),
            "schema_version": self.schema_version,
        }

    def _trace_hash(self) -> str:
        return stable_digest(self._trace_identity(), length=64)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._trace_identity(), "selected_candidate": self.selected_candidate.to_dict() if self.selected_candidate is not None else None,
            "advisory_only": self.advisory_only, "provider_calls": self.provider_calls, "network_calls": self.network_calls,
            "commercial_claims_allowed": self.commercial_claims_allowed, "trace_hash": self.trace_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JointSearchDecisionV1":
        required = frozenset({"decision_id", "project_id", "case_id", "action", "selected_candidate_id", "selected_candidate", "candidate_ids", "pareto_frontier_ids", "evaluated_count", "feasible_count", "pruned_count", "searcher", "budget_used", "stop_reason", "reason_codes", "advisory_only", "provider_calls", "network_calls", "commercial_claims_allowed", "trace_hash", "schema_version"})
        validate_exact_fields(value, required=required)
        selected = value["selected_candidate"]
        return cls(decision_id=value["decision_id"], project_id=value["project_id"], case_id=value["case_id"], action=value["action"], selected_candidate_id=value["selected_candidate_id"], selected_candidate=None if selected is None else JointSearchCandidateV1.from_dict(selected), candidate_ids=tuple(value["candidate_ids"]), pareto_frontier_ids=tuple(value["pareto_frontier_ids"]), evaluated_count=value["evaluated_count"], feasible_count=value["feasible_count"], pruned_count=value["pruned_count"], searcher=value["searcher"], budget_used=value["budget_used"], stop_reason=value["stop_reason"], reason_codes=tuple(value["reason_codes"]), advisory_only=value["advisory_only"], provider_calls=value["provider_calls"], network_calls=value["network_calls"], commercial_claims_allowed=value["commercial_claims_allowed"], trace_hash=value["trace_hash"], schema_version=value["schema_version"])


def compile_prompt_handoff(problem: JointSearchProblemV1, choices: Mapping[str, JointRouteOptionV1]) -> PromptHandoffAuditV1:
    """Check that a complete candidate preserves deliverables and authority."""

    required = set(problem.required_deliverables)
    delivered = {item for node in problem.nodes for item in node.deliverables}
    coverage = 1.0 if not required else len(required & delivered) / len(required)
    contradictions = 0
    fields: dict[str, str] = {}
    authority_ok = True
    information_loss = 0.0
    reasons: list[str] = []
    node_by_id = {item.node_id: item for item in problem.nodes}
    for node_id, route in choices.items():
        node = node_by_id[node_id]
        if not set(node.required_authority_scopes).issubset(set(route.authority_scopes)):
            authority_ok = False
        if problem.allowed_authority_scopes and not set(route.authority_scopes).issubset(set(problem.allowed_authority_scopes)):
            authority_ok = False
        for key, value in route.handoff_fields.items():
            # Ordinary fields are node-local.  Only explicitly shared fields
            # participate in contradiction checking; this prevents an
            # ``objective`` field on two different DAG nodes from being
            # mistaken for a conflicting handoff contract.
            if key.startswith("shared:"):
                if key in fields and fields[key] != value:
                    contradictions += 1
                fields[key] = value
        information_loss = max(information_loss, route.information_loss)
        if route.tool_ids and not set(route.tool_ids).issubset(set(problem.allowed_tools)):
            authority_ok = False
        if node.risk_level in {"high", "critical"} and route.validator_id == route.executor_id:
            authority_ok = False
    if coverage < 1.0:
        reasons.append("deliverable_coverage_gap")
    if contradictions:
        reasons.append("handoff_contradiction")
    if not authority_ok:
        reasons.append("authority_or_tool_violation")
    if information_loss > problem.max_information_loss:
        reasons.append("information_loss_exceeded")
    return PromptHandoffAuditV1(coverage=coverage, contradiction_count=contradictions, authority_ok=authority_ok, information_loss=information_loss, passed=not reasons, reason_codes=tuple(reasons))


class JointSearchPlugin(Protocol):
    name: str

    def search(self, problem: JointSearchProblemV1, candidates: Sequence[JointSearchCandidateV1]) -> Sequence[JointSearchCandidateV1]:
        ...


class JointCandidateFactory:
    """Build bounded candidates from safe, historical, planner and local seeds."""

    def generate(self, problem: JointSearchProblemV1) -> tuple[JointSearchCandidateV1, ...]:
        node_by_id = {item.node_id: item for item in problem.nodes}
        routes_by_node = {item.node_id: {route.route_id: route for route in item.options} for item in problem.route_sets}
        route_order = {node_id: tuple(sorted(options.values(), key=lambda item: (-(item.predicted_success.lower), item.predicted_cost_usd.mean, item.route_id))) for node_id, options in routes_by_node.items()}
        seeds: list[tuple[str, dict[str, str]]] = []
        strongest = {node_id: options[0].route_id for node_id, options in route_order.items()}
        seeds.append(("safety_baseline", strongest))
        if set(problem.historical_route_ids) == set(node_by_id) and all(problem.historical_route_ids[node_id] in routes_by_node[node_id] for node_id in node_by_id):
            seeds.append(("historical", dict(problem.historical_route_ids)))
        planner = {node_id: problem.planner_route_ids.get(node_id, route_order[node_id][min(1, len(route_order[node_id]) - 1)].route_id) for node_id in node_by_id}
        seeds.append(("planner", planner))
        for source, base in tuple(seeds):
            for node_id in sorted(node_by_id):
                for option in route_order[node_id][1:3]:
                    transformed = dict(base)
                    transformed[node_id] = option.route_id
                    seeds.append(("local_transform", transformed))
        seen: set[str] = set()
        results: list[JointSearchCandidateV1] = []
        for source, choices in seeds:
            for mode in problem.management_modes:
                if mode == "no_delegation" and len(problem.nodes) != 1:
                    continue
                # Each recovery policy is a real search dimension, but we keep
                # the Cartesian product bounded by the problem budget.
                for recovery_policy in problem.recovery_policies[:3]:
                    candidate = self._build(problem, source, mode, recovery_policy, choices, node_by_id, routes_by_node)
                    if candidate.computed_id in seen:
                        continue
                    seen.add(candidate.computed_id)
                    results.append(candidate)
                    if len(results) >= problem.budget.max_candidates:
                        return tuple(results)
        return tuple(results)

    def _build(self, problem: JointSearchProblemV1, source: str, mode: str, recovery_policy: str, choices: Mapping[str, str], node_by_id: Mapping[str, JointTaskNodeV1], routes_by_node: Mapping[str, Mapping[str, JointRouteOptionV1]]) -> JointSearchCandidateV1:
        selected: dict[str, JointRouteOptionV1] = {}
        missing = False
        for node_id in node_by_id:
            route = routes_by_node[node_id].get(choices.get(node_id, ""))
            if route is None:
                missing = True
            else:
                selected[node_id] = route
        compiler = compile_prompt_handoff(problem, selected) if not missing else PromptHandoffAuditV1(0.0, 0, False, 1.0, False, ("route_missing",))
        order = problem.topological_order()
        depths: dict[str, int] = {}
        for node_id in order:
            node = node_by_id[node_id]
            depths[node_id] = 0 if not node.dependencies else 1 + max(depths[dep] for dep in node.dependencies)
        width_by_level: dict[int, int] = {}
        for depth in depths.values():
            width_by_level[depth] = width_by_level.get(depth, 0) + 1
        parallel_width = max(width_by_level.values() or [0])
        planning_cost = {"no_delegation": 0.0, "planner": 0.002, "multiplanner": 0.006, "expert": 0.01}.get(mode, 0.004)
        planning_latency = {"no_delegation": 0.0, "planner": 20.0, "multiplanner": 60.0, "expert": 90.0}.get(mode, 40.0)
        total_cost = planning_cost
        total_p95_cost = planning_cost
        total_tokens = 0
        success = 1.0
        lower = 1.0
        correlation: dict[str, float] = {}
        for node_id, route in selected.items():
            node = node_by_id[node_id]
            total_cost += route.predicted_cost_usd.mean + route.validation_cost_usd.mean
            total_p95_cost += route.predicted_cost_usd.p95 + route.validation_cost_usd.p95
            total_tokens += node.context_tokens
            success *= route.predicted_success.mean
            lower *= route.predicted_success.lower
            correlation[route.failure_correlation_group] = max(correlation.get(route.failure_correlation_group, 0.0), 1.0 - route.predicted_success.lower)
            total_tokens += route.capacity_units * 16
        correlation_penalty = min(0.5, sum(value for value in correlation.values() if value > 0.5) * 0.08)
        lower = max(0.0, lower * (1.0 - correlation_penalty))
        success = max(0.0, min(1.0, success * (1.0 - correlation_penalty / 2.0)))
        finish: dict[str, float] = {}
        for node_id in order:
            route = selected.get(node_id)
            node_latency = 0.0 if route is None else route.predicted_latency_ms.p95 + route.validation_latency_ms.p95
            finish[node_id] = (max((finish[dep] for dep in node_by_id[node_id].dependencies), default=0.0) + node_latency)
        total_latency = planning_latency + max(finish.values() or [0.0])
        handoff_cost = total_tokens * 0.00000002
        exploration_cost = planning_cost / problem.reuse_count
        expected_loss = total_cost + handoff_cost + exploration_cost + problem.latency_weight_usd_per_second * total_latency / 1000.0 + problem.failure_loss_usd * (1.0 - lower)
        tail_loss = total_p95_cost + handoff_cost * 1.5 + problem.latency_weight_usd_per_second * total_latency / 1000.0 + problem.failure_loss_usd
        cvar_tail_loss = expected_loss + max(0.0, tail_loss - expected_loss) * (1.0 - lower) / 0.1
        checks = {
            "route_complete": not missing and len(selected) == len(node_by_id),
            "cost_known": all(route.cost_known for route in selected.values()),
            "latency_known": all(route.latency_known for route in selected.values()),
            "budget_money": total_cost <= problem.budget.money_usd + 1e-12,
            "budget_tokens": total_tokens <= problem.budget.input_tokens,
            "deadline": total_latency <= min(problem.deadline_ms, problem.budget.wall_clock_ms),
            "parallelism": parallel_width <= problem.max_parallelism,
            "chance_constraint": lower >= problem.min_success_lower,
            "management_depth": mode != "expert" or 2 <= 2,
            "compiler": compiler.passed,
        }
        if mode == "no_delegation":
            checks["no_delegation_shape"] = len(node_by_id) == 1
        strategy_nodes: list[StrategyNodeV1] = []
        for node_id in order:
            node = node_by_id[node_id]
            route = selected.get(node_id)
            if route is None:
                # The strategy contract still needs a well-formed node for a
                # pruned candidate; use a deterministic unavailable placeholder.
                route = JointRouteOptionV1(route_id="missing-route", executor_id="unavailable", reasoning_tier="unknown", validator_id="validator:none", predicted_success=ProbabilityInterval(0.0, 0.0, 0.0), predicted_cost_usd=NonNegativeEstimate(0.0, 0.0), predicted_latency_ms=NonNegativeEstimate(0.0, 0.0))
            strategy_nodes.append(StrategyNodeV1(node_id=node.node_id, objective_hash=node.objective_hash, dependencies=node.dependencies, executor_id=route.executor_id, reasoning_tier=route.reasoning_tier, tool_ids=route.tool_ids, parallel_group=f"level-{depths[node_id]}", validator_id=route.validator_id, predicted_success=route.predicted_success, predicted_cost_usd=NonNegativeEstimate(route.predicted_cost_usd.mean + route.validation_cost_usd.mean, route.predicted_cost_usd.p95 + route.validation_cost_usd.p95), predicted_latency_ms=NonNegativeEstimate(route.predicted_latency_ms.mean + route.validation_latency_ms.mean, route.predicted_latency_ms.p95 + route.validation_latency_ms.p95)))
        strategy = StrategyCandidateV1(case_id=problem.case_id, management_mode=mode, nodes=tuple(strategy_nodes), planning_cost_usd=NonNegativeEstimate(planning_cost, planning_cost), planning_latency_ms=NonNegativeEstimate(planning_latency, planning_latency), predicted_success=ProbabilityInterval(success, lower, min(1.0, success + 0.1)), predicted_total_cost_usd=NonNegativeEstimate(total_cost, max(total_cost, total_p95_cost)), predicted_total_latency_ms=NonNegativeEstimate(total_latency * 0.7, total_latency), hard_constraint_checks=checks, recovery_policy=recovery_policy, evidence_ids=("joint-search-offline-fixture",), policy_revision=JOINT_SEARCH_POLICY_REVISION)
        return JointSearchCandidateV1(case_id=problem.case_id, source=source, strategy=strategy, node_routes=_route_pairs(choices), parallel_width=parallel_width, expected_loss=expected_loss, cvar_tail_loss=cvar_tail_loss, compiler_audit=compiler, hard_constraint_checks=checks)


class JointStrategySearcher:
    """Bounded deterministic search with an explicit Anytime result."""

    def __init__(self, *, factory: JointCandidateFactory | None = None) -> None:
        self.factory = factory or JointCandidateFactory()

    @staticmethod
    def _key(candidate: JointSearchCandidateV1) -> tuple[float, float, float, str]:
        return (candidate.expected_loss, candidate.cvar_tail_loss, -candidate.strategy.predicted_success.lower, candidate.computed_id)

    @staticmethod
    def pareto_frontier(candidates: Sequence[JointSearchCandidateV1]) -> tuple[JointSearchCandidateV1, ...]:
        feasible = [item for item in candidates if item.feasible]
        frontier: list[JointSearchCandidateV1] = []
        for candidate in feasible:
            dominated = False
            for other in feasible:
                if other.computed_id == candidate.computed_id:
                    continue
                no_worse = other.expected_loss <= candidate.expected_loss and other.strategy.predicted_total_latency_ms.p95 <= candidate.strategy.predicted_total_latency_ms.p95 and other.strategy.predicted_success.lower >= candidate.strategy.predicted_success.lower
                strictly = other.expected_loss < candidate.expected_loss or other.strategy.predicted_total_latency_ms.p95 < candidate.strategy.predicted_total_latency_ms.p95 or other.strategy.predicted_success.lower > candidate.strategy.predicted_success.lower
                if no_worse and strictly:
                    dominated = True
                    break
            if not dominated:
                frontier.append(candidate)
        return tuple(sorted(frontier, key=JointStrategySearcher._key))

    def search(self, problem: JointSearchProblemV1, *, searcher: str = "beam") -> tuple[JointSearchDecisionV1, tuple[JointSearchCandidateV1, ...]]:
        if searcher not in JOINT_SEARCHERS:
            raise ValueError(f"unsupported searcher: {searcher!r}")
        candidates = list(self.factory.generate(problem))
        ordered = sorted(candidates, key=self._key)
        budget = problem.budget
        trace: list[str] = [f"generated:{len(candidates)}"]
        if searcher == "greedy":
            evaluated = ordered[: budget.max_evaluations]
            trace.append(f"greedy:evaluated:{len(evaluated)}")
        elif searcher == "successive_halving":
            pool = ordered[: budget.max_evaluations]
            stage = 0
            while len(pool) > budget.beam_width:
                keep = max(budget.beam_width, math.ceil(len(pool) / 2))
                pool = pool[:keep]
                stage += 1
                trace.append(f"halving:{stage}:kept:{len(pool)}")
            evaluated = pool
        elif searcher == "pareto":
            evaluated = ordered[: budget.max_evaluations]
            trace.append(f"pareto:evaluated:{len(evaluated)}")
        else:
            evaluated = ordered[: min(budget.max_evaluations, max(budget.beam_width, 1) * 4)]
            trace.append(f"beam:width:{budget.beam_width}:evaluated:{len(evaluated)}")
        feasible = [item for item in evaluated if item.feasible]
        frontier = self.pareto_frontier(evaluated)
        selected = min(frontier or feasible, key=self._key) if (frontier or feasible) else None
        if selected is not None:
            action = "SELECT"
            reason_codes = ("hard_constraints_satisfied", "pareto_frontier_retained", "anytime_best_returned")
            stop_reason = "best_feasible_within_search_budget"
        elif any(item.strategy.management_mode in {"expert", "multiplanner"} or not item.compiler_audit.authority_ok for item in evaluated):
            action = "ESCALATE"
            reason_codes = ("no_feasible_strategy", "requires_authority_or_search_review")
            stop_reason = "all_candidates_pruned_or_unsafe"
        else:
            action = "ABSTAIN"
            reason_codes = ("no_feasible_strategy", "insufficient_evidence_or_budget")
            stop_reason = "no_candidate_satisfied_chance_and_resource_constraints"
        decision_id = stable_id("joint_decision", {"project_id": problem.project_id, "case_id": problem.case_id, "searcher": searcher, "candidate_ids": [item.computed_id for item in evaluated]})
        budget_used = {"evaluations": len(evaluated), "candidate_pool": len(candidates), "money_usd": sum(item.strategy.planning_cost_usd.mean for item in evaluated), "wall_clock_ms": sum(item.strategy.planning_latency_ms.mean for item in evaluated)}
        decision = JointSearchDecisionV1(decision_id=decision_id, project_id=problem.project_id, case_id=problem.case_id, action=action, selected_candidate_id=None if selected is None else selected.computed_id, selected_candidate=selected, candidate_ids=tuple(item.computed_id for item in evaluated), pareto_frontier_ids=tuple(item.computed_id for item in frontier), evaluated_count=len(evaluated), feasible_count=len(feasible), pruned_count=max(0, len(candidates) - len(evaluated)), searcher=searcher, budget_used=budget_used, stop_reason=stop_reason, reason_codes=reason_codes)
        return decision, tuple(evaluated)


def build_demo_problem() -> JointSearchProblemV1:
    """A tiny deterministic fixture used by the CLI and UI."""

    digest = "a" * 64
    return JointSearchProblemV1(
        project_id="local-default", case_id="offline-joint-demo", task_family="code", deadline_ms=2_000, min_success_lower=0.55, max_parallelism=2, required_deliverables=("patch", "validation"), allowed_tools=("repo-read", "repo-write"), allowed_authority_scopes=("workspace",), budget=JointSearchBudgetV1(money_usd=0.05, input_tokens=10_000, wall_clock_ms=2_000, max_evaluations=32, beam_width=4, max_candidates=64),
        nodes=(
            JointTaskNodeV1("inspect", digest, deliverables=("patch",), required_authority_scopes=("workspace",), context_tokens=120),
            JointTaskNodeV1("verify", digest, dependencies=("inspect",), deliverables=("validation",), required_authority_scopes=("workspace",), context_tokens=80),
        ),
        route_sets=(
            JointRouteSetV1("inspect", (
                JointRouteOptionV1("local-fast", "local-model", "low", "validator:syntax", tool_ids=("repo-read",), authority_scopes=("workspace",), predicted_success=ProbabilityInterval(0.82, 0.70, 0.90), predicted_cost_usd=NonNegativeEstimate(0.001, 0.002), predicted_latency_ms=NonNegativeEstimate(80, 120), failure_correlation_group="local", handoff_fields={"objective": "patch"}),
                JointRouteOptionV1("strong-safe", "strong-model", "high", "validator:independent", tool_ids=("repo-read", "repo-write"), authority_scopes=("workspace",), predicted_success=ProbabilityInterval(0.96, 0.90, 0.99), predicted_cost_usd=NonNegativeEstimate(0.02, 0.03), predicted_latency_ms=NonNegativeEstimate(180, 260), failure_correlation_group="strong", handoff_fields={"objective": "patch"}),
            )),
            JointRouteSetV1("verify", (
                JointRouteOptionV1("local-check", "local-model", "medium", "validator:syntax", tool_ids=("repo-read",), authority_scopes=("workspace",), predicted_success=ProbabilityInterval(0.88, 0.76, 0.94), predicted_cost_usd=NonNegativeEstimate(0.001, 0.002), predicted_latency_ms=NonNegativeEstimate(70, 100), failure_correlation_group="local", handoff_fields={"objective": "validation"}),
                JointRouteOptionV1("strong-review", "strong-model", "high", "validator:independent", tool_ids=("repo-read",), authority_scopes=("workspace",), predicted_success=ProbabilityInterval(0.97, 0.92, 0.99), predicted_cost_usd=NonNegativeEstimate(0.015, 0.025), predicted_latency_ms=NonNegativeEstimate(150, 220), failure_correlation_group="strong", handoff_fields={"objective": "validation"}),
            )),
        ),
    )


__all__ = [
    "JOINT_CANDIDATE_SOURCES", "JOINT_SEARCH_ACTIONS", "JOINT_SEARCHERS", "JOINT_SEARCH_POLICY_REVISION", "JOINT_SEARCH_SCHEMA_VERSION",
    "JointCandidateFactory", "JointRouteOptionV1", "JointRouteSetV1", "JointSearchBudgetV1", "JointSearchCandidateV1", "JointSearchDecisionV1", "JointSearchPlugin", "JointSearchProblemV1", "JointStrategySearcher", "JointTaskNodeV1", "PromptHandoffAuditV1", "build_demo_problem", "compile_prompt_handoff",
]
