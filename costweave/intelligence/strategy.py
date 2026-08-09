"""Whole-strategy and DAG contracts for CostWeave v0.6.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .common import (
    DECISION_INTELLIGENCE_SCHEMA_VERSION,
    MANAGEMENT_MODES,
    RECOVERY_POLICIES,
    NonNegativeEstimate,
    ProbabilityInterval,
    bounded_text,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class StrategyNodeV1:
    node_id: str
    objective_hash: str
    dependencies: tuple[str, ...]
    executor_id: str
    reasoning_tier: str
    tool_ids: tuple[str, ...]
    parallel_group: str
    validator_id: str
    predicted_success: ProbabilityInterval
    predicted_cost_usd: NonNegativeEstimate
    predicted_latency_ms: NonNegativeEstimate

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", bounded_text(self.node_id, "node_id"))
        object.__setattr__(self, "objective_hash", validate_sha256(self.objective_hash, "objective_hash"))
        dependencies = unique_texts(self.dependencies, "dependencies", maximum_items=256)
        if self.node_id in dependencies:
            raise ValueError("a node cannot depend on itself")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "executor_id", bounded_text(self.executor_id, "executor_id"))
        object.__setattr__(self, "reasoning_tier", bounded_text(self.reasoning_tier, "reasoning_tier"))
        object.__setattr__(self, "tool_ids", unique_texts(self.tool_ids, "tool_ids", maximum_items=32))
        object.__setattr__(self, "parallel_group", bounded_text(self.parallel_group, "parallel_group", minimum=0))
        object.__setattr__(self, "validator_id", bounded_text(self.validator_id, "validator_id"))
        if not isinstance(self.predicted_success, ProbabilityInterval):
            raise TypeError("predicted_success must be ProbabilityInterval")
        if not isinstance(self.predicted_cost_usd, NonNegativeEstimate):
            raise TypeError("predicted_cost_usd must be NonNegativeEstimate")
        if not isinstance(self.predicted_latency_ms, NonNegativeEstimate):
            raise TypeError("predicted_latency_ms must be NonNegativeEstimate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "objective_hash": self.objective_hash,
            "dependencies": list(self.dependencies),
            "executor_id": self.executor_id,
            "reasoning_tier": self.reasoning_tier,
            "tool_ids": list(self.tool_ids),
            "parallel_group": self.parallel_group,
            "validator_id": self.validator_id,
            "predicted_success": self.predicted_success.to_dict(),
            "predicted_cost_usd": self.predicted_cost_usd.to_dict(),
            "predicted_latency_ms": self.predicted_latency_ms.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyNodeV1":
        validate_exact_fields(
            value,
            required=frozenset(
                {
                    "node_id", "objective_hash", "dependencies", "executor_id", "reasoning_tier",
                    "tool_ids", "parallel_group", "validator_id", "predicted_success",
                    "predicted_cost_usd", "predicted_latency_ms",
                }
            ),
        )
        return cls(
            node_id=value["node_id"],
            objective_hash=value["objective_hash"],
            dependencies=tuple(value["dependencies"]),
            executor_id=value["executor_id"],
            reasoning_tier=value["reasoning_tier"],
            tool_ids=tuple(value["tool_ids"]),
            parallel_group=value["parallel_group"],
            validator_id=value["validator_id"],
            predicted_success=ProbabilityInterval.from_dict(value["predicted_success"]),
            predicted_cost_usd=NonNegativeEstimate.from_dict(value["predicted_cost_usd"]),
            predicted_latency_ms=NonNegativeEstimate.from_dict(value["predicted_latency_ms"]),
        )


@dataclass(frozen=True, slots=True)
class StrategyCandidateV1:
    case_id: str
    management_mode: str
    nodes: tuple[StrategyNodeV1, ...]
    planning_cost_usd: NonNegativeEstimate
    planning_latency_ms: NonNegativeEstimate
    predicted_success: ProbabilityInterval
    predicted_total_cost_usd: NonNegativeEstimate
    predicted_total_latency_ms: NonNegativeEstimate
    hard_constraint_checks: Mapping[str, bool]
    recovery_policy: str
    evidence_ids: tuple[str, ...]
    policy_revision: str
    schema_version: str = DECISION_INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        if self.management_mode not in MANAGEMENT_MODES:
            raise ValueError(f"unsupported management_mode: {self.management_mode!r}")
        if not isinstance(self.nodes, (tuple, list)):
            raise TypeError("nodes must be a tuple")
        if not 1 <= len(self.nodes) <= 256:
            raise ValueError("nodes must contain between 1 and 256 items")
        normalized_nodes: list[StrategyNodeV1] = []
        node_ids: set[str] = set()
        for node in self.nodes:
            if not isinstance(node, StrategyNodeV1):
                raise TypeError("nodes must contain StrategyNodeV1")
            if node.node_id in node_ids:
                raise ValueError("node_id must be unique within Candidate")
            node_ids.add(node.node_id)
            normalized_nodes.append(node)
        if self.management_mode == "no_delegation" and len(normalized_nodes) != 1:
            raise ValueError("no_delegation candidates must contain exactly one node")
        for node in normalized_nodes:
            missing = set(node.dependencies) - node_ids
            if missing:
                raise ValueError(f"node dependencies do not exist: {sorted(missing)!r}")
        object.__setattr__(self, "nodes", tuple(normalized_nodes))
        object.__setattr__(self, "planning_cost_usd", self._estimate(self.planning_cost_usd, "planning_cost_usd"))
        object.__setattr__(self, "planning_latency_ms", self._estimate(self.planning_latency_ms, "planning_latency_ms"))
        if not isinstance(self.predicted_success, ProbabilityInterval):
            raise TypeError("predicted_success must be ProbabilityInterval")
        object.__setattr__(self, "predicted_total_cost_usd", self._estimate(self.predicted_total_cost_usd, "predicted_total_cost_usd"))
        object.__setattr__(self, "predicted_total_latency_ms", self._estimate(self.predicted_total_latency_ms, "predicted_total_latency_ms"))
        if self.predicted_total_cost_usd.mean < self.planning_cost_usd.mean:
            raise ValueError("predicted total cost cannot be below planning cost")
        if not isinstance(self.hard_constraint_checks, Mapping):
            raise TypeError("hard_constraint_checks must be a mapping")
        if not self.hard_constraint_checks:
            raise ValueError("at least one hard constraint check is required")
        checks: dict[str, bool] = {}
        for key, value in self.hard_constraint_checks.items():
            if not isinstance(key, str) or not key.strip():
                raise TypeError("hard constraint keys must be non-empty strings")
            if not isinstance(value, bool):
                raise TypeError("hard constraint values must be bool")
            checks[key] = value
        object.__setattr__(self, "hard_constraint_checks", dict(sorted(checks.items())))
        if self.recovery_policy not in RECOVERY_POLICIES:
            raise ValueError(f"unsupported recovery_policy: {self.recovery_policy!r}")
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "policy_revision", bounded_text(self.policy_revision, "policy_revision"))
        if self.schema_version != DECISION_INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DECISION_INTELLIGENCE_SCHEMA_VERSION}")
        self.topological_order()

    @staticmethod
    def _estimate(value: Any, name: str) -> NonNegativeEstimate:
        if not isinstance(value, NonNegativeEstimate):
            raise TypeError(f"{name} must be NonNegativeEstimate")
        return value

    @property
    def feasible(self) -> bool:
        return all(self.hard_constraint_checks.values())

    def topological_order(self) -> tuple[str, ...]:
        dependencies = {node.node_id: set(node.dependencies) for node in self.nodes}
        order: list[str] = []
        remaining = set(dependencies)
        while remaining:
            ready = sorted(node_id for node_id in remaining if not (dependencies[node_id] & remaining))
            if not ready:
                raise ValueError("strategy graph contains a cycle")
            order.extend(ready)
            remaining.difference_update(ready)
        return tuple(order)

    def _identity(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "management_mode": self.management_mode,
            "nodes": [node.to_dict() for node in self.nodes],
            "planning_cost_usd": self.planning_cost_usd.to_dict(),
            "planning_latency_ms": self.planning_latency_ms.to_dict(),
            "predicted_success": self.predicted_success.to_dict(),
            "predicted_total_cost_usd": self.predicted_total_cost_usd.to_dict(),
            "predicted_total_latency_ms": self.predicted_total_latency_ms.to_dict(),
            "hard_constraint_checks": dict(sorted(self.hard_constraint_checks.items())),
            "recovery_policy": self.recovery_policy,
            "evidence_ids": list(self.evidence_ids),
            "policy_revision": self.policy_revision,
            "schema_version": self.schema_version,
        }

    @property
    def strategy_id(self) -> str:
        return stable_id("strategy", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["strategy_id"] = self.strategy_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StrategyCandidateV1":
        fields = frozenset(
            {
                "case_id", "management_mode", "nodes", "planning_cost_usd", "planning_latency_ms",
                "predicted_success", "predicted_total_cost_usd", "predicted_total_latency_ms",
                "hard_constraint_checks", "recovery_policy", "evidence_ids", "policy_revision",
                "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"strategy_id"}))
        item = cls(
            case_id=value["case_id"],
            management_mode=value["management_mode"],
            nodes=tuple(StrategyNodeV1.from_dict(node) for node in value["nodes"]),
            planning_cost_usd=NonNegativeEstimate.from_dict(value["planning_cost_usd"]),
            planning_latency_ms=NonNegativeEstimate.from_dict(value["planning_latency_ms"]),
            predicted_success=ProbabilityInterval.from_dict(value["predicted_success"]),
            predicted_total_cost_usd=NonNegativeEstimate.from_dict(value["predicted_total_cost_usd"]),
            predicted_total_latency_ms=NonNegativeEstimate.from_dict(value["predicted_total_latency_ms"]),
            hard_constraint_checks=value["hard_constraint_checks"],
            recovery_policy=value["recovery_policy"],
            evidence_ids=tuple(value["evidence_ids"]),
            policy_revision=value["policy_revision"],
            schema_version=value["schema_version"],
        )
        if "strategy_id" in value and value["strategy_id"] != item.strategy_id:
            raise ValueError("strategy_id does not match content")
        return item


__all__ = ["StrategyCandidateV1", "StrategyNodeV1"]
