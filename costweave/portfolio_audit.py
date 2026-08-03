"""Advisory whole-DAG portfolio optimization audit for CostWeave v0.5.6.4.3."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any, Mapping, Sequence

from .evaluation_protocol import canonical_json


PORTFOLIO_AUDIT_SCHEMA_VERSION = "v0.5.6.4.3"
PORTFOLIO_AUDIT_POLICY_REVISION = "global-dag-portfolio-audit-v1"
TOPOLOGY_KINDS = ("current", "fine_grained", "consolidated")
RISK_LEVELS = ("low", "normal", "medium", "high", "critical")
NODE_KINDS = ("execution", "planning", "validation", "merge", "approval")


class PortfolioAuditError(ValueError):
    """Raised when an advisory portfolio problem violates its contract."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any, name: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioAuditError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < minimum:
        raise PortfolioAuditError(f"{name} is out of range")
    return number


def _probability(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number > 1.0:
        raise PortfolioAuditError(f"{name} must be in [0,1]")
    return number


def _bounded(value: str, name: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 160 or "\n" in text or "\r" in text:
        raise PortfolioAuditError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("bearer ", "api_key=", "password=", "secret=")
    ):
        raise PortfolioAuditError(f"{name} appears to contain a secret")
    return text


def _sha256(value: str, name: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PortfolioAuditError(f"{name} must be a sha256 digest")
    return text


def _evidence_rank(value: str) -> int:
    prefix = str(value).split("-", 1)[0].upper()
    return {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4, "E5": 5}.get(
        prefix, -1
    )


@dataclass(frozen=True, slots=True)
class PortfolioNode:
    node_id: str
    kind: str
    risk_level: str
    criticality: float
    required_capabilities: tuple[str, ...] = ()
    allowed_providers: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    management_depth: int = 0

    def __post_init__(self) -> None:
        _bounded(self.node_id, "node_id")
        if self.kind not in NODE_KINDS:
            raise PortfolioAuditError("node kind is invalid")
        if self.risk_level not in RISK_LEVELS:
            raise PortfolioAuditError("node risk level is invalid")
        _probability(self.criticality, "criticality")
        if self.management_depth < 0:
            raise PortfolioAuditError("management depth cannot be negative")
        for item in (
            *self.required_capabilities,
            *self.allowed_providers,
            *self.required_residency_tags,
        ):
            _bounded(item, "node constraint")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for name in (
            "required_capabilities",
            "allowed_providers",
            "required_residency_tags",
        ):
            result[name] = list(result[name])
        return result


@dataclass(frozen=True, slots=True)
class PortfolioEdge:
    from_node: str
    to_node: str
    kind: str = "hard"
    transfer_tokens: int = 0
    transfer_latency_ms: int = 0
    invalidation_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        _bounded(self.from_node, "from_node")
        _bounded(self.to_node, "to_node")
        if self.from_node == self.to_node:
            raise PortfolioAuditError("self dependency is not allowed")
        if self.kind not in {"hard", "soft", "validation"}:
            raise PortfolioAuditError("edge kind is invalid")
        if self.transfer_tokens < 0 or self.transfer_latency_ms < 0:
            raise PortfolioAuditError("edge transfer values cannot be negative")
        _finite(self.invalidation_cost_usd, "invalidation_cost_usd")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PortfolioOption:
    option_id: str
    node_id: str
    model_id: str
    deployment_id: str
    provider: str
    resource_pool: str
    failure_domain: str
    success_mean: float
    success_lower: float
    direct_model_cost_usd: float
    validation_cost_usd: float
    tool_cost_usd: float
    rework_cost_if_failed_usd: float
    escalation_cost_if_failed_usd: float
    latency_p50_ms: int
    latency_p95_ms: int
    capacity_units: float = 1.0
    quota_units: float = 1.0
    capabilities: tuple[str, ...] = ()
    residency_tags: tuple[str, ...] = ()
    evidence_level: str = "E0-rule-prior"

    def __post_init__(self) -> None:
        for name in (
            "option_id",
            "node_id",
            "model_id",
            "deployment_id",
            "provider",
            "resource_pool",
            "failure_domain",
            "evidence_level",
        ):
            _bounded(str(getattr(self, name)), name)
        mean = _probability(self.success_mean, "success_mean")
        lower = _probability(self.success_lower, "success_lower")
        if lower > mean:
            raise PortfolioAuditError("success lower bound exceeds mean")
        for name in (
            "direct_model_cost_usd",
            "validation_cost_usd",
            "tool_cost_usd",
            "rework_cost_if_failed_usd",
            "escalation_cost_if_failed_usd",
            "capacity_units",
            "quota_units",
        ):
            _finite(getattr(self, name), name)
        if self.capacity_units <= 0 or self.quota_units < 0:
            raise PortfolioAuditError("capacity/quota units are invalid")
        if self.latency_p50_ms < 0 or self.latency_p95_ms < self.latency_p50_ms:
            raise PortfolioAuditError("latency quantiles are invalid")
        for item in (*self.capabilities, *self.residency_tags):
            _bounded(item, "option attribute")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["capabilities"] = list(self.capabilities)
        result["residency_tags"] = list(self.residency_tags)
        return result


@dataclass(frozen=True, slots=True)
class PortfolioProblem:
    comparison_group_hash: str
    topology_kind: str
    nodes: tuple[PortfolioNode, ...]
    edges: tuple[PortfolioEdge, ...]
    options: tuple[PortfolioOption, ...]
    hard_budget_usd: float
    deadline_ms: int
    provider_quotas: Mapping[str, float]
    pool_capacities: Mapping[str, float]
    minimum_node_success: float = 0.0
    minimum_portfolio_success: float = 0.0
    value_of_time_per_second: float = 0.0
    failure_loss_usd: float = 0.0
    handoff_cost_per_1k_tokens: float = 0.000012
    cross_provider_handoff_multiplier: float = 1.0
    same_provider_handoff_multiplier: float = 0.25
    correlation_penalty_usd: float = 0.01
    maximum_management_depth: int = 2
    source_revision: str = "rule-prior-v1"
    schema_version: str = PORTFOLIO_AUDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sha256(self.comparison_group_hash, "comparison_group_hash")
        if self.schema_version != PORTFOLIO_AUDIT_SCHEMA_VERSION:
            raise PortfolioAuditError("portfolio schema version is unsupported")
        if self.topology_kind not in TOPOLOGY_KINDS:
            raise PortfolioAuditError("topology kind is invalid")
        if not self.nodes or len(self.nodes) > 32:
            raise PortfolioAuditError("portfolio must contain 1 to 32 nodes")
        if len(self.edges) > 256:
            raise PortfolioAuditError("portfolio supports at most 256 edges")
        if self.maximum_management_depth < 0 or self.maximum_management_depth > 2:
            raise PortfolioAuditError("management depth is capped at two")
        _bounded(self.source_revision, "source_revision")
        for name in (
            "hard_budget_usd",
            "value_of_time_per_second",
            "failure_loss_usd",
            "handoff_cost_per_1k_tokens",
            "cross_provider_handoff_multiplier",
            "same_provider_handoff_multiplier",
            "correlation_penalty_usd",
        ):
            _finite(getattr(self, name), name)
        if self.deadline_ms < 0:
            raise PortfolioAuditError("deadline cannot be negative")
        _probability(self.minimum_node_success, "minimum_node_success")
        _probability(
            self.minimum_portfolio_success, "minimum_portfolio_success"
        )
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise PortfolioAuditError("node ids must be unique")
        known = set(node_ids)
        for edge in self.edges:
            if edge.from_node not in known or edge.to_node not in known:
                raise PortfolioAuditError("edge references an unknown node")
        edge_keys = [
            (edge.from_node, edge.to_node, edge.kind) for edge in self.edges
        ]
        if len(edge_keys) != len(set(edge_keys)):
            raise PortfolioAuditError("duplicate portfolio edge is not allowed")
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise PortfolioAuditError("option ids must be unique")
        option_nodes = {option.node_id for option in self.options}
        if not option_nodes <= known or option_nodes != known:
            raise PortfolioAuditError("every node requires at least one option")
        counts: dict[str, int] = defaultdict(int)
        for option in self.options:
            counts[option.node_id] += 1
            if _evidence_rank(option.evidence_level) < 0:
                raise PortfolioAuditError("option evidence level is invalid")
        if any(value > 8 for value in counts.values()):
            raise PortfolioAuditError("each node supports at most eight options")
        if any(node.management_depth > self.maximum_management_depth for node in self.nodes):
            raise PortfolioAuditError("node exceeds the management-depth cap")
        for name, value in (*self.provider_quotas.items(), *self.pool_capacities.items()):
            _bounded(str(name), "resource key")
            _finite(value, "resource limit")
        _topological_order(self.nodes, self.edges)

    @property
    def problem_id(self) -> str:
        return "portfolio_problem_" + _digest(self.to_dict())[:32]

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparison_group_hash": self.comparison_group_hash,
            "topology_kind": self.topology_kind,
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
            "options": [item.to_dict() for item in self.options],
            "hard_budget_usd": self.hard_budget_usd,
            "deadline_ms": self.deadline_ms,
            "provider_quotas": dict(sorted(self.provider_quotas.items())),
            "pool_capacities": dict(sorted(self.pool_capacities.items())),
            "minimum_node_success": self.minimum_node_success,
            "minimum_portfolio_success": self.minimum_portfolio_success,
            "value_of_time_per_second": self.value_of_time_per_second,
            "failure_loss_usd": self.failure_loss_usd,
            "handoff_cost_per_1k_tokens": self.handoff_cost_per_1k_tokens,
            "cross_provider_handoff_multiplier": self.cross_provider_handoff_multiplier,
            "same_provider_handoff_multiplier": self.same_provider_handoff_multiplier,
            "correlation_penalty_usd": self.correlation_penalty_usd,
            "maximum_management_depth": self.maximum_management_depth,
            "source_revision": self.source_revision,
            "schema_version": self.schema_version,
        }


def _topological_order(
    nodes: Sequence[PortfolioNode], edges: Sequence[PortfolioEdge]
) -> tuple[str, ...]:
    indegree = {node.node_id: 0 for node in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        indegree[edge.to_node] += 1
        children[edge.from_node].append(edge.to_node)
    queue = deque(sorted(key for key, value in indegree.items() if value == 0))
    ordered: list[str] = []
    while queue:
        current = queue.popleft()
        ordered.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(ordered) != len(nodes):
        raise PortfolioAuditError("portfolio DAG contains a cycle")
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class PortfolioAuditSolution:
    audit_id: str
    problem_id: str
    comparison_group_hash: str
    topology_kind: str
    feasible: bool
    selected_options: Mapping[str, str]
    metrics: Mapping[str, Any]
    constraint_checks: Mapping[str, bool]
    rejection_reasons: tuple[str, ...]
    solver_audit: Mapping[str, Any]
    evidence_levels: tuple[str, ...]
    directional_evidence_eligible: bool
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = PORTFOLIO_AUDIT_SCHEMA_VERSION
    policy_revision: str = PORTFOLIO_AUDIT_POLICY_REVISION

    def __post_init__(self) -> None:
        if not self.advisory_only or self.routing_authority or self.may_change_actual_route:
            raise PortfolioAuditError("portfolio audit cannot control execution")
        if self.commercial_claims_allowed:
            raise PortfolioAuditError("v0.5.6.4.3 cannot grant commercial claims")
        expected = "portfolio_audit_" + _digest(self.identity())[:32]
        if self.audit_id != expected:
            raise PortfolioAuditError("portfolio audit id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "comparison_group_hash": self.comparison_group_hash,
            "topology_kind": self.topology_kind,
            "feasible": self.feasible,
            "selected_options": dict(sorted(self.selected_options.items())),
            "metrics": dict(self.metrics),
            "constraint_checks": dict(sorted(self.constraint_checks.items())),
            "rejection_reasons": list(self.rejection_reasons),
            "solver_audit": dict(self.solver_audit),
            "evidence_levels": list(self.evidence_levels),
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"audit_id": self.audit_id, **self.identity()}


def _node_layers(
    order: Sequence[str], edges: Sequence[PortfolioEdge]
) -> dict[str, int]:
    parents: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        parents[edge.to_node].append(edge.from_node)
    layers: dict[str, int] = {}
    for node_id in order:
        layers[node_id] = (
            max(layers[parent] for parent in parents[node_id]) + 1
            if parents[node_id]
            else 0
        )
    return layers


class GlobalPortfolioAuditEngine:
    """Bounded deterministic beam search over whole-DAG placement options."""

    def __init__(self, *, beam_width: int = 256, maximum_states: int = 20_000) -> None:
        if beam_width < 1 or maximum_states < 1:
            raise PortfolioAuditError("solver bounds must be positive")
        self.beam_width = min(4096, int(beam_width))
        self.maximum_states = min(1_000_000, int(maximum_states))

    def solve(self, problem: PortfolioProblem) -> PortfolioAuditSolution:
        order = _topological_order(problem.nodes, problem.edges)
        node_map = {node.node_id: node for node in problem.nodes}
        options: dict[str, list[PortfolioOption]] = defaultdict(list)
        rejection_counts: dict[str, int] = defaultdict(int)
        for option in problem.options:
            node = node_map[option.node_id]
            reason = self._static_rejection(problem, node, option)
            if reason:
                rejection_counts[reason] += 1
            else:
                options[option.node_id].append(option)
        missing = [node_id for node_id in order if not options[node_id]]
        evidence_levels = tuple(
            sorted({option.evidence_level for option in problem.options})
        )
        directional = bool(evidence_levels) and min(
            _evidence_rank(item) for item in evidence_levels
        ) >= 2
        if missing:
            return self._blocked_solution(
                problem,
                evidence_levels,
                directional,
                reasons=tuple(f"no_feasible_option:{item}" for item in missing),
                rejection_counts=rejection_counts,
            )

        states: list[dict[str, PortfolioOption]] = [{}]
        explored = 0
        beam_pruned = 0
        hard_pruned = 0
        truncated = False
        for node_id in order:
            expanded: list[tuple[float, dict[str, PortfolioOption]]] = []
            for state in states:
                for option in sorted(options[node_id], key=lambda item: item.option_id):
                    explored += 1
                    if explored > self.maximum_states:
                        truncated = True
                        break
                    trial = {**state, node_id: option}
                    if not self._partial_constraints(problem, trial):
                        hard_pruned += 1
                        continue
                    expanded.append((self._partial_score(problem, trial), trial))
                if truncated:
                    break
            if not expanded:
                return self._blocked_solution(
                    problem,
                    evidence_levels,
                    directional,
                    reasons=("no_globally_feasible_assignment",),
                    rejection_counts=rejection_counts,
                    explored=explored,
                    hard_pruned=hard_pruned,
                    truncated=truncated,
                )
            expanded.sort(
                key=lambda item: (
                    item[0],
                    tuple(
                        option.option_id
                        for _, option in sorted(item[1].items())
                    ),
                )
            )
            beam_pruned += max(0, len(expanded) - self.beam_width)
            states = [state for _, state in expanded[: self.beam_width]]
            if truncated:
                break

        complete = [state for state in states if len(state) == len(order)]
        evaluated: list[tuple[float, dict[str, PortfolioOption], dict[str, Any]]] = []
        for state in complete:
            metrics = self._metrics(problem, state)
            checks = self._constraint_checks(problem, state, metrics)
            if all(checks.values()):
                evaluated.append((metrics["expected_total_loss_usd"], state, metrics))
            else:
                hard_pruned += 1
        if not evaluated:
            return self._blocked_solution(
                problem,
                evidence_levels,
                directional,
                reasons=("no_complete_assignment_passed_hard_constraints",),
                rejection_counts=rejection_counts,
                explored=explored,
                hard_pruned=hard_pruned,
                beam_pruned=beam_pruned,
                truncated=truncated,
            )
        evaluated.sort(
            key=lambda item: (
                item[0],
                item[2]["critical_path_p95_ms"],
                tuple(
                    option.option_id for _, option in sorted(item[1].items())
                ),
            )
        )
        _, selected, metrics = evaluated[0]
        checks = self._constraint_checks(problem, selected, metrics)
        solver_audit = {
            "algorithm": "bounded-deterministic-beam-search",
            "beam_width": self.beam_width,
            "maximum_states": self.maximum_states,
            "states_explored": explored,
            "hard_constraint_pruned": hard_pruned,
            "beam_pruned": beam_pruned,
            "search_truncated": truncated,
            "optimality_proven": not truncated and beam_pruned == 0,
            "capacity_model": "topological-layer-conservative",
            "static_rejections": dict(sorted(rejection_counts.items())),
        }
        identity = self._identity(
            problem,
            feasible=True,
            selected=selected,
            metrics=metrics,
            checks=checks,
            reasons=(),
            solver_audit=solver_audit,
            evidence_levels=evidence_levels,
            directional=directional,
        )
        return PortfolioAuditSolution(
            audit_id="portfolio_audit_" + _digest(identity)[:32],
            problem_id=problem.problem_id,
            comparison_group_hash=problem.comparison_group_hash,
            topology_kind=problem.topology_kind,
            feasible=True,
            selected_options={
                node_id: option.option_id for node_id, option in selected.items()
            },
            metrics=metrics,
            constraint_checks=checks,
            rejection_reasons=(),
            solver_audit=solver_audit,
            evidence_levels=evidence_levels,
            directional_evidence_eligible=directional,
        )

    @staticmethod
    def _static_rejection(
        problem: PortfolioProblem, node: PortfolioNode, option: PortfolioOption
    ) -> str | None:
        if option.success_lower < problem.minimum_node_success:
            return "node_success_floor"
        if node.allowed_providers and option.provider not in node.allowed_providers:
            return "provider_not_allowed"
        if not set(node.required_capabilities) <= set(option.capabilities):
            return "capability_missing"
        if not set(node.required_residency_tags) <= set(option.residency_tags):
            return "residency_mismatch"
        if option.provider not in problem.provider_quotas:
            return "provider_quota_missing"
        if option.resource_pool not in problem.pool_capacities:
            return "pool_capacity_missing"
        return None

    @staticmethod
    def _partial_constraints(
        problem: PortfolioProblem, selected: Mapping[str, PortfolioOption]
    ) -> bool:
        quota: dict[str, float] = defaultdict(float)
        for option in selected.values():
            quota[option.provider] += option.quota_units
            if quota[option.provider] > float(problem.provider_quotas[option.provider]) + 1e-12:
                return False
        node_map = {node.node_id: node for node in problem.nodes}
        for edge in problem.edges:
            if edge.from_node not in selected or edge.to_node not in selected:
                continue
            child = node_map[edge.to_node]
            producer = selected[edge.from_node]
            validator = selected[edge.to_node]
            if child.kind == "validation":
                if validator.model_id == producer.model_id:
                    return False
                if child.risk_level in {"high", "critical"} and (
                    validator.failure_domain == producer.failure_domain
                ):
                    return False
        return True

    def _partial_score(
        self,
        problem: PortfolioProblem,
        selected: Mapping[str, PortfolioOption],
    ) -> float:
        return float(self._metrics(problem, selected)["expected_total_loss_usd"])

    def _metrics(
        self,
        problem: PortfolioProblem,
        selected: Mapping[str, PortfolioOption],
    ) -> dict[str, Any]:
        node_map = {node.node_id: node for node in problem.nodes}
        direct = sum(item.direct_model_cost_usd for item in selected.values())
        validation = sum(item.validation_cost_usd for item in selected.values())
        tools = sum(item.tool_cost_usd for item in selected.values())
        rework = sum(
            (1.0 - item.success_lower) * item.rework_cost_if_failed_usd
            for item in selected.values()
        )
        escalation = sum(
            (1.0 - item.success_lower) * item.escalation_cost_if_failed_usd
            for item in selected.values()
        )
        handoff = 0.0
        invalidation = 0.0
        for edge in problem.edges:
            if edge.from_node not in selected or edge.to_node not in selected:
                continue
            parent = selected[edge.from_node]
            child = selected[edge.to_node]
            multiplier = (
                problem.same_provider_handoff_multiplier
                if parent.provider == child.provider
                else problem.cross_provider_handoff_multiplier
            )
            handoff += (
                edge.transfer_tokens
                / 1000.0
                * problem.handoff_cost_per_1k_tokens
                * multiplier
            )
            invalidation += (
                (1.0 - parent.success_lower)
                * edge.invalidation_cost_usd
                * max(0.25, node_map[edge.to_node].criticality)
            )
        correlation = 0.0
        for left, right in combinations(selected.values(), 2):
            if left.failure_domain != right.failure_domain:
                continue
            correlation += (
                min(1.0 - left.success_lower, 1.0 - right.success_lower)
                * problem.correlation_penalty_usd
            )
        portfolio_success = math.prod(
            item.success_lower for item in selected.values()
        )
        critical_path = self._critical_path(problem, selected)
        latency_loss = (
            critical_path / 1000.0 * problem.value_of_time_per_second
        )
        failure_loss = (1.0 - portfolio_success) * problem.failure_loss_usd
        expected_spend = (
            direct
            + validation
            + tools
            + handoff
            + rework
            + escalation
            + invalidation
            + correlation
        )
        total_loss = expected_spend + latency_loss + failure_loss
        return {
            "direct_model_cost_usd": round(direct, 8),
            "validation_cost_usd": round(validation, 8),
            "tool_cost_usd": round(tools, 8),
            "handoff_cost_usd": round(handoff, 8),
            "expected_rework_usd": round(rework, 8),
            "expected_escalation_usd": round(escalation, 8),
            "expected_invalidation_usd": round(invalidation, 8),
            "correlated_failure_penalty_usd": round(correlation, 8),
            "expected_spend_usd": round(expected_spend, 8),
            "latency_loss_usd": round(latency_loss, 8),
            "failure_loss_usd": round(failure_loss, 8),
            "expected_total_loss_usd": round(total_loss, 8),
            "portfolio_success_lower": round(portfolio_success, 8),
            "critical_path_p95_ms": int(critical_path),
            "node_count": len(selected),
        }

    @staticmethod
    def _critical_path(
        problem: PortfolioProblem,
        selected: Mapping[str, PortfolioOption],
    ) -> int:
        order = _topological_order(problem.nodes, problem.edges)
        parents: dict[str, list[PortfolioEdge]] = defaultdict(list)
        for edge in problem.edges:
            parents[edge.to_node].append(edge)
        finish: dict[str, int] = {}
        for node_id in order:
            if node_id not in selected:
                continue
            previous = [
                finish[edge.from_node] + edge.transfer_latency_ms
                for edge in parents[node_id]
                if edge.from_node in finish
            ]
            finish[node_id] = max(previous, default=0) + selected[node_id].latency_p95_ms
        return max(finish.values(), default=0)

    @staticmethod
    def _capacity_ok(
        problem: PortfolioProblem,
        selected: Mapping[str, PortfolioOption],
    ) -> bool:
        order = _topological_order(problem.nodes, problem.edges)
        layers = _node_layers(order, problem.edges)
        usage: dict[tuple[int, str], float] = defaultdict(float)
        for node_id, option in selected.items():
            usage[(layers[node_id], option.resource_pool)] += option.capacity_units
        return all(
            value <= float(problem.pool_capacities[pool]) + 1e-12
            for (_, pool), value in usage.items()
        )

    def _constraint_checks(
        self,
        problem: PortfolioProblem,
        selected: Mapping[str, PortfolioOption],
        metrics: Mapping[str, Any],
    ) -> dict[str, bool]:
        provider_usage: dict[str, float] = defaultdict(float)
        for option in selected.values():
            provider_usage[option.provider] += option.quota_units
        return {
            "all_nodes_assigned": len(selected) == len(problem.nodes),
            "node_success_floor": all(
                item.success_lower >= problem.minimum_node_success
                for item in selected.values()
            ),
            "portfolio_success_floor": (
                float(metrics["portfolio_success_lower"])
                >= problem.minimum_portfolio_success
            ),
            "hard_budget": (
                float(metrics["expected_spend_usd"])
                <= problem.hard_budget_usd + 1e-12
            ),
            "deadline": (
                int(metrics["critical_path_p95_ms"]) <= problem.deadline_ms
            ),
            "provider_quotas": all(
                usage <= float(problem.provider_quotas[provider]) + 1e-12
                for provider, usage in provider_usage.items()
            ),
            "parallel_pool_capacity": self._capacity_ok(problem, selected),
            "validator_independence": self._partial_constraints(problem, selected),
        }

    def _blocked_solution(
        self,
        problem: PortfolioProblem,
        evidence_levels: tuple[str, ...],
        directional: bool,
        *,
        reasons: tuple[str, ...],
        rejection_counts: Mapping[str, int],
        explored: int = 0,
        hard_pruned: int = 0,
        beam_pruned: int = 0,
        truncated: bool = False,
    ) -> PortfolioAuditSolution:
        checks = {"feasible_assignment": False}
        solver_audit = {
            "algorithm": "bounded-deterministic-beam-search",
            "beam_width": self.beam_width,
            "maximum_states": self.maximum_states,
            "states_explored": explored,
            "hard_constraint_pruned": hard_pruned,
            "beam_pruned": beam_pruned,
            "search_truncated": truncated,
            "optimality_proven": False,
            "capacity_model": "topological-layer-conservative",
            "static_rejections": dict(sorted(rejection_counts.items())),
        }
        identity = self._identity(
            problem,
            feasible=False,
            selected={},
            metrics={},
            checks=checks,
            reasons=reasons,
            solver_audit=solver_audit,
            evidence_levels=evidence_levels,
            directional=directional,
        )
        return PortfolioAuditSolution(
            audit_id="portfolio_audit_" + _digest(identity)[:32],
            problem_id=problem.problem_id,
            comparison_group_hash=problem.comparison_group_hash,
            topology_kind=problem.topology_kind,
            feasible=False,
            selected_options={},
            metrics={},
            constraint_checks=checks,
            rejection_reasons=reasons,
            solver_audit=solver_audit,
            evidence_levels=evidence_levels,
            directional_evidence_eligible=directional,
        )

    @staticmethod
    def _identity(
        problem: PortfolioProblem,
        *,
        feasible: bool,
        selected: Mapping[str, PortfolioOption],
        metrics: Mapping[str, Any],
        checks: Mapping[str, bool],
        reasons: Sequence[str],
        solver_audit: Mapping[str, Any],
        evidence_levels: Sequence[str],
        directional: bool,
    ) -> dict[str, Any]:
        return {
            "problem_id": problem.problem_id,
            "comparison_group_hash": problem.comparison_group_hash,
            "topology_kind": problem.topology_kind,
            "feasible": feasible,
            "selected_options": {
                node_id: option.option_id for node_id, option in sorted(selected.items())
            },
            "metrics": dict(metrics),
            "constraint_checks": dict(sorted(checks.items())),
            "rejection_reasons": list(reasons),
            "solver_audit": dict(solver_audit),
            "evidence_levels": list(evidence_levels),
            "directional_evidence_eligible": directional,
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "commercial_claims_allowed": False,
            "schema_version": PORTFOLIO_AUDIT_SCHEMA_VERSION,
            "policy_revision": PORTFOLIO_AUDIT_POLICY_REVISION,
        }


@dataclass(frozen=True, slots=True)
class PortfolioScenarioComparison:
    comparison_id: str
    comparison_group_hash: str
    preferred_audit_id: str | None
    alternatives: tuple[Mapping[str, Any], ...]
    all_feasible: bool
    directional_evidence_eligible: bool
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = PORTFOLIO_AUDIT_SCHEMA_VERSION
    policy_revision: str = PORTFOLIO_AUDIT_POLICY_REVISION

    def __post_init__(self) -> None:
        if not self.advisory_only or self.routing_authority or self.may_change_actual_route:
            raise PortfolioAuditError("scenario comparison cannot control routing")
        if self.commercial_claims_allowed:
            raise PortfolioAuditError("scenario comparison cannot grant claims")
        expected = "portfolio_compare_" + _digest(self.identity())[:32]
        if self.comparison_id != expected:
            raise PortfolioAuditError("scenario comparison id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "comparison_group_hash": self.comparison_group_hash,
            "preferred_audit_id": self.preferred_audit_id,
            "alternatives": [dict(item) for item in self.alternatives],
            "all_feasible": self.all_feasible,
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"comparison_id": self.comparison_id, **self.identity()}


def compare_portfolio_scenarios(
    solutions: Sequence[PortfolioAuditSolution],
) -> PortfolioScenarioComparison:
    items = tuple(solutions)
    if len(items) < 2:
        raise PortfolioAuditError("at least two portfolio scenarios are required")
    groups = {item.comparison_group_hash for item in items}
    if len(groups) != 1:
        raise PortfolioAuditError("portfolio scenarios are not comparable")
    topology_kinds = {item.topology_kind for item in items}
    if len(topology_kinds) != len(items):
        raise PortfolioAuditError("portfolio topology kinds must be unique")
    feasible = [item for item in items if item.feasible]
    preferred = min(
        feasible,
        key=lambda item: (
            float(item.metrics["expected_total_loss_usd"]),
            int(item.metrics["critical_path_p95_ms"]),
            item.audit_id,
        ),
        default=None,
    )
    alternatives = tuple(
        {
            "audit_id": item.audit_id,
            "topology_kind": item.topology_kind,
            "feasible": item.feasible,
            "expected_total_loss_usd": (
                item.metrics.get("expected_total_loss_usd")
                if item.feasible
                else None
            ),
            "critical_path_p95_ms": (
                item.metrics.get("critical_path_p95_ms")
                if item.feasible
                else None
            ),
            "delta_from_preferred_usd": (
                round(
                    float(item.metrics["expected_total_loss_usd"])
                    - float(preferred.metrics["expected_total_loss_usd"]),
                    8,
                )
                if preferred is not None and item.feasible
                else None
            ),
        }
        for item in sorted(items, key=lambda item: item.topology_kind)
    )
    directional = bool(items) and all(
        item.directional_evidence_eligible for item in items
    )
    identity = {
        "comparison_group_hash": next(iter(groups)),
        "preferred_audit_id": preferred.audit_id if preferred else None,
        "alternatives": [dict(item) for item in alternatives],
        "all_feasible": len(feasible) == len(items),
        "directional_evidence_eligible": directional,
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "commercial_claims_allowed": False,
        "schema_version": PORTFOLIO_AUDIT_SCHEMA_VERSION,
        "policy_revision": PORTFOLIO_AUDIT_POLICY_REVISION,
    }
    return PortfolioScenarioComparison(
        comparison_id="portfolio_compare_" + _digest(identity)[:32],
        comparison_group_hash=next(iter(groups)),
        preferred_audit_id=preferred.audit_id if preferred else None,
        alternatives=alternatives,
        all_feasible=len(feasible) == len(items),
        directional_evidence_eligible=directional,
    )


def public_portfolio_audit_policy() -> dict[str, Any]:
    return {
        "schema_version": PORTFOLIO_AUDIT_SCHEMA_VERSION,
        "policy_revision": PORTFOLIO_AUDIT_POLICY_REVISION,
        "topology_kinds": list(TOPOLOGY_KINDS),
        "objective_terms": [
            "direct_model_cost",
            "validation_cost",
            "tool_cost",
            "handoff_cost",
            "expected_rework",
            "expected_escalation",
            "expected_invalidation",
            "correlated_failure_penalty",
            "latency_loss",
            "failure_loss",
        ],
        "hard_constraints": [
            "node_success_floor",
            "portfolio_success_floor",
            "budget",
            "deadline",
            "provider_quota",
            "parallel_pool_capacity",
            "capability",
            "residency",
            "validator_independence",
            "management_depth_two",
        ],
        "solver": "bounded-deterministic-beam-search",
        "current_evidence": "E0-contract-only",
        "advisory_only": True,
        "routing_authority": "legacy-resource-optimizer",
        "may_change_actual_route": False,
        "external_calls_required": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "GlobalPortfolioAuditEngine",
    "PORTFOLIO_AUDIT_POLICY_REVISION",
    "PORTFOLIO_AUDIT_SCHEMA_VERSION",
    "PortfolioAuditError",
    "PortfolioAuditSolution",
    "PortfolioEdge",
    "PortfolioNode",
    "PortfolioOption",
    "PortfolioProblem",
    "PortfolioScenarioComparison",
    "compare_portfolio_scenarios",
    "public_portfolio_audit_policy",
]
