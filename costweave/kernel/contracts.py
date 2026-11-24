"""Contracts for jobs that request verified artifacts and the operations that produce them."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


MAX_UNITS = 2**63 - 1
DEFAULT_SEARCH_LIMIT = 10_000
MAX_SEARCH_LIMIT = 100_000
MAX_OPERATIONS = 256
MAX_ARTIFACTS = 512


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or value.strip() != value or not value.isprintable():
        raise ValueError(f"{field} must be a printable name of 1 to 160 characters without surrounding spaces")
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, field: str, minimum: float, maximum: float, *, allow_zero: bool = True) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    numeric = float(value)
    if (numeric < minimum if allow_zero else numeric <= minimum) or numeric > maximum:
        bound = f"[{minimum}, {maximum}]" if allow_zero else f"({minimum}, {maximum}]"
        raise ValueError(f"{field} must be in {bound}")
    return numeric


def _names(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ARTIFACTS:
        raise ValueError(f"{field} must be an array of at most {MAX_ARTIFACTS} names")
    names = tuple(_name(name, field) for name in value)
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate name in {field}")
    return names


def _fields(raw: Any, required: set[str], optional: set[str], label: str) -> None:
    if not isinstance(raw, Mapping) or not required <= raw.keys() or raw.keys() - (required | optional):
        raise ValueError(f"{label} has missing or unrecognized fields")


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise ValueError("JSON exceeds the maximum nesting depth of 64")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        raise ValueError("expected finite JSON values and string object keys")
    for child in children:
        _check_json(child, depth + 1)


def canonical_json(value: Any) -> str:
    """Return a deterministic UTF-8 compatible encoding of a strict JSON value."""
    _check_json(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    encoded.encode("utf-8")  # Reject unpaired surrogates before storage or dispatch.
    return encoded


def _json_object(value: Any, label: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return canonical_json(value)


@dataclass(frozen=True, slots=True, init=False)
class Operation:
    id: str
    requires: tuple[str, ...]
    produces: str
    executor: str
    validator: str
    cost_units: int
    route_role: str
    expected_units: float | None
    pass_prob: float
    false_pass_risk: float
    latency_p50_ms: int
    latency_p95_ms: int
    provider_family: str
    max_attempts: int
    _config_json: str

    def __init__(
        self,
        id: str,
        requires: list[str] | tuple[str, ...],
        produces: str,
        executor: str,
        validator: str,
        cost_units: int,
        config: dict | None = None,
        *,
        route_role: str = "standard",
        expected_units: float | None = None,
        pass_prob: float = 1.0,
        false_pass_risk: float = 0.0,
        latency_p50_ms: int = 0,
        latency_p95_ms: int = 0,
        provider_family: str = "local",
        max_attempts: int = 1,
    ):
        for field, value in (("id", id), ("produces", produces), ("executor", executor), ("validator", validator)):
            object.__setattr__(self, field, _name(value, field))
        if route_role not in ("standard", "speculative", "fallback"):
            raise ValueError("route_role must be standard, speculative, or fallback")
        object.__setattr__(self, "route_role", route_role)
        object.__setattr__(self, "requires", _names(requires, "requires"))
        units = _integer(cost_units, "cost_units", 0, MAX_UNITS)
        object.__setattr__(self, "cost_units", units)
        exp = None if expected_units is None else _number(expected_units, "expected_units", 0.0, float(units))
        object.__setattr__(self, "expected_units", exp)
        object.__setattr__(self, "pass_prob", _number(pass_prob, "pass_prob", 0.0, 1.0, allow_zero=False))
        object.__setattr__(self, "false_pass_risk", _number(false_pass_risk, "false_pass_risk", 0.0, 1.0))
        p50 = _integer(latency_p50_ms, "latency_p50_ms", 0, 86_400_000)
        p95 = _integer(latency_p95_ms, "latency_p95_ms", p50, 86_400_000)
        object.__setattr__(self, "latency_p50_ms", p50)
        object.__setattr__(self, "latency_p95_ms", p95)
        object.__setattr__(self, "provider_family", _name(provider_family, "provider_family"))
        object.__setattr__(self, "max_attempts", _integer(max_attempts, "max_attempts", 1, 16))
        object.__setattr__(self, "_config_json", _json_object({} if config is None else config, "config"))

    @property
    def effective_expected_units(self) -> float:
        return float(self.cost_units) if self.expected_units is None else self.expected_units

    @property
    def config(self) -> dict:
        return json.loads(self._config_json)

    @classmethod
    def from_dict(cls, raw: Mapping) -> Operation:
        _fields(
            raw,
            {"id", "requires", "produces", "executor", "validator", "cost_units"},
            {
                "config", "route_role", "expected_units", "pass_prob", "false_pass_risk",
                "latency_p50_ms", "latency_p95_ms", "provider_family", "max_attempts",
            },
            "operation",
        )
        if "config" in raw and raw["config"] is None:
            raise ValueError("config must be a JSON object")
        return cls(**raw)

    def to_dict(self) -> dict:
        result = {"id": self.id, "requires": list(self.requires), "produces": self.produces,
                  "executor": self.executor, "validator": self.validator, "cost_units": self.cost_units,
                  "config": self.config}
        if self.route_role != "standard":
            result["route_role"] = self.route_role
        if self.expected_units is not None:
            result["expected_units"] = self.expected_units
        if self.pass_prob != 1.0:
            result["pass_prob"] = self.pass_prob
        if self.false_pass_risk != 0.0:
            result["false_pass_risk"] = self.false_pass_risk
        if self.latency_p50_ms != 0:
            result["latency_p50_ms"] = self.latency_p50_ms
        if self.latency_p95_ms != 0:
            result["latency_p95_ms"] = self.latency_p95_ms
        if self.provider_family != "local":
            result["provider_family"] = self.provider_family
        if self.max_attempts != 1:
            result["max_attempts"] = self.max_attempts
        return result


@dataclass(frozen=True, slots=True, init=False)
class Job:
    goals: tuple[str, ...]
    operations: tuple[Operation, ...]
    budget_units: int
    search_limit: int
    planning_policy: str
    max_risk_alpha: float
    failure_penalty_units: int | None
    corruption_penalty_units: int
    deadline_ms: int | None
    latency_weight_units_per_sec: float
    max_parallel: int
    _inputs_json: str
    _acceptance_json: str

    def __init__(
        self,
        goals: list[str] | tuple[str, ...],
        inputs: dict,
        budget_units: int,
        operations: list[Operation] | tuple[Operation, ...],
        search_limit: int = DEFAULT_SEARCH_LIMIT,
        acceptance: dict | None = None,
        *,
        planning_policy: str = "lowest_cost",
        max_risk_alpha: float = 1.0,
        failure_penalty_units: int | None = None,
        corruption_penalty_units: int = 0,
        deadline_ms: int | None = None,
        latency_weight_units_per_sec: float = 0.0,
        max_parallel: int = 1,
    ):
        if planning_policy not in ("lowest_cost", "preserve_fallback", "bsao"):
            raise ValueError("planning_policy must be lowest_cost, preserve_fallback, or bsao")
        object.__setattr__(self, "planning_policy", planning_policy)
        object.__setattr__(self, "max_risk_alpha", _number(max_risk_alpha, "max_risk_alpha", 0.0, 1.0))
        fail_pen = None if failure_penalty_units is None else _integer(failure_penalty_units, "failure_penalty_units", 0, MAX_UNITS)
        object.__setattr__(self, "failure_penalty_units", fail_pen)
        object.__setattr__(self, "corruption_penalty_units", _integer(corruption_penalty_units, "corruption_penalty_units", 0, MAX_UNITS))
        deadline = None if deadline_ms is None else _integer(deadline_ms, "deadline_ms", 1, 86_400_000)
        object.__setattr__(self, "deadline_ms", deadline)
        object.__setattr__(self, "latency_weight_units_per_sec", _number(latency_weight_units_per_sec, "latency_weight_units_per_sec", 0.0, float(MAX_UNITS)))
        object.__setattr__(self, "max_parallel", _integer(max_parallel, "max_parallel", 1, 64))
        goals = _names(goals, "goals")
        if not goals:
            raise ValueError("at least one goal is required")
        if not isinstance(operations, (list, tuple)) or len(operations) > MAX_OPERATIONS:
            raise ValueError(f"operations must be an array of at most {MAX_OPERATIONS} items")
        if not all(isinstance(op, Operation) for op in operations):
            raise ValueError("operations must contain Operation instances")
        operations = tuple(sorted(operations, key=lambda op: op.id))
        if len({op.id for op in operations}) != len(operations):
            raise ValueError("duplicate operation id")
        inputs_json = _json_object(inputs, "inputs")
        inputs_keys = {_name(key, "input artifact") for key in inputs}
        outputs = Counter(op.produces for op in operations)
        declared = inputs_keys | outputs.keys()
        if len(declared) > MAX_ARTIFACTS:
            raise ValueError(f"a job may declare at most {MAX_ARTIFACTS} artifacts")
        references = set(goals).union(*(op.requires for op in operations))
        if missing := references - declared:
            raise ValueError(f"unknown artifact references: {sorted(missing)}")
        acceptance = {} if acceptance is None else acceptance
        if not isinstance(acceptance, dict) or acceptance.keys() - declared:
            raise ValueError("acceptance must map declared artifacts to validator contracts")
        if missing := set(goals) - acceptance.keys():
            raise ValueError(f"final goals require an artifact acceptance contract: {sorted(missing)}")
        for artifact, contract in acceptance.items():
            _fields(contract, {"validator", "config"}, set(), f"acceptance for {artifact}")
            _name(contract["validator"], "artifact validator")
            if not isinstance(contract["config"], dict):
                raise ValueError("artifact validator config must be a JSON object")
        for artifact, count in outputs.items():
            if count > 1 and artifact not in acceptance:
                raise ValueError(f"multiple producers for {artifact!r} require an artifact acceptance contract")
        object.__setattr__(self, "goals", tuple(sorted(goals)))
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "budget_units", _integer(budget_units, "budget_units", 0, MAX_UNITS))
        object.__setattr__(self, "search_limit", _integer(search_limit, "search_limit", 1, MAX_SEARCH_LIMIT))
        object.__setattr__(self, "_inputs_json", inputs_json)
        object.__setattr__(self, "_acceptance_json", _json_object(acceptance, "acceptance"))

    @property
    def inputs(self) -> dict:
        # Executors receive ordinary JSON values. Mutating a view never changes the frozen job.
        return json.loads(self._inputs_json)

    @property
    def acceptance(self) -> dict:
        return json.loads(self._acceptance_json)

    @classmethod
    def from_dict(cls, raw: Mapping) -> Job:
        _fields(
            raw,
            {"goals", "inputs", "operations", "budget_units"},
            {
                "search_limit", "acceptance", "planning_policy",
                "max_risk_alpha", "failure_penalty_units", "corruption_penalty_units",
                "deadline_ms", "latency_weight_units_per_sec", "max_parallel",
            },
            "job",
        )
        rows = raw["operations"]
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_OPERATIONS:
            raise ValueError(f"operations must be an array of at most {MAX_OPERATIONS} items")
        if "acceptance" in raw and raw["acceptance"] is None:
            raise ValueError("acceptance must be an object")
        return cls(
            raw["goals"],
            raw["inputs"],
            raw["budget_units"],
            tuple(Operation.from_dict(row) for row in rows),
            raw.get("search_limit", DEFAULT_SEARCH_LIMIT),
            raw.get("acceptance"),
            planning_policy=raw.get("planning_policy", "lowest_cost"),
            max_risk_alpha=raw.get("max_risk_alpha", 1.0),
            failure_penalty_units=raw.get("failure_penalty_units"),
            corruption_penalty_units=raw.get("corruption_penalty_units", 0),
            deadline_ms=raw.get("deadline_ms"),
            latency_weight_units_per_sec=raw.get("latency_weight_units_per_sec", 0.0),
            max_parallel=raw.get("max_parallel", 1),
        )

    def to_dict(self) -> dict:
        result = {"goals": list(self.goals), "inputs": self.inputs, "budget_units": self.budget_units,
                  "operations": [op.to_dict() for op in self.operations], "acceptance": self.acceptance,
                  "search_limit": self.search_limit}
        # Omitting defaults preserves the canonical hashes of previously stored jobs.
        if self.planning_policy != "lowest_cost":
            result["planning_policy"] = self.planning_policy
        if self.max_risk_alpha != 1.0:
            result["max_risk_alpha"] = self.max_risk_alpha
        if self.failure_penalty_units is not None:
            result["failure_penalty_units"] = self.failure_penalty_units
        if self.corruption_penalty_units != 0:
            result["corruption_penalty_units"] = self.corruption_penalty_units
        if self.deadline_ms is not None:
            result["deadline_ms"] = self.deadline_ms
        if self.latency_weight_units_per_sec != 0.0:
            result["latency_weight_units_per_sec"] = self.latency_weight_units_per_sec
        if self.max_parallel != 1:
            result["max_parallel"] = self.max_parallel
        return result
