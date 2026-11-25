"""Contracts for jobs that request verified artifacts and the operations that produce them."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .verification_value import validate_verification_model


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


@dataclass(frozen=True, slots=True)
class EvidenceRule:
    artifact: str
    field: str
    equals: str | int | float | bool | None
    operation: str | None = None
    request: str | None = None
    reason: str | None = None
    acquire: bool = False

    def to_dict(self) -> dict:
        row = {"artifact": self.artifact, "field": self.field, "equals": self.equals}
        if self.operation is not None:
            row["operation"] = self.operation
        else:
            row.update(request=self.request, reason=self.reason)
        if self.acquire:
            row["acquire"] = True
        return row


def parse_evidence_rules(raw: Any) -> tuple[EvidenceRule, ...]:
    """Parse ordered equality rules over one field of an accepted JSON object."""
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_OPERATIONS:
        raise ValueError(f"evidence_rules must be an array of at most {MAX_OPERATIONS} items")
    rules = []
    for row in raw:
        keys = {"artifact", "field", "equals"}
        selects_operation = isinstance(row, Mapping) and "operation" in row
        keys.update({"operation"} if selects_operation else {"request", "reason"})
        _fields(row, keys, {"acquire"}, "evidence rule")
        if "acquire" in row and type(row["acquire"]) is not bool:
            raise ValueError("evidence acquire must be a boolean")
        for key in keys - {"equals"}:
            _name(row[key], f"evidence {key}")
        if row["equals"] is not None and type(row["equals"]) not in (str, int, float, bool):
            raise ValueError("evidence equals requires a finite JSON scalar")
        canonical_json(dict(row))
        rules.append(EvidenceRule(**row))
    return tuple(rules)


@dataclass(frozen=True, slots=True, init=False)
class Operation:
    id: str
    requires: tuple[str, ...]
    produces: str
    executor: str
    validator: str
    cost_units: int
    route_role: str
    _config_json: str

    def __init__(self, id: str, requires: list[str] | tuple[str, ...], produces: str, executor: str,
                 validator: str, cost_units: int, config: dict | None = None, *, route_role: str = "standard"):
        for field, value in (("id", id), ("produces", produces), ("executor", executor), ("validator", validator)):
            object.__setattr__(self, field, _name(value, field))
        if route_role not in ("standard", "speculative", "fallback"):
            raise ValueError("route_role must be standard, speculative, or fallback")
        object.__setattr__(self, "route_role", route_role)
        object.__setattr__(self, "requires", _names(requires, "requires"))
        object.__setattr__(self, "cost_units", _integer(cost_units, "cost_units", 0, MAX_UNITS))
        object.__setattr__(self, "_config_json", _json_object({} if config is None else config, "config"))

    @property
    def config(self) -> dict:
        return json.loads(self._config_json)

    @classmethod
    def from_dict(cls, raw: Mapping) -> Operation:
        _fields(raw, {"id", "requires", "produces", "executor", "validator", "cost_units"},
                {"config", "route_role"}, "operation")
        if "config" in raw and raw["config"] is None:
            raise ValueError("config must be a JSON object")
        return cls(**raw)

    def to_dict(self) -> dict:
        result = {"id": self.id, "requires": list(self.requires), "produces": self.produces,
                  "executor": self.executor, "validator": self.validator, "cost_units": self.cost_units,
                  "config": self.config}
        if self.route_role != "standard":
            result["route_role"] = self.route_role
        return result


@dataclass(frozen=True, slots=True, init=False)
class Job:
    goals: tuple[str, ...]
    operations: tuple[Operation, ...]
    budget_units: int
    search_limit: int
    planning_policy: str
    evidence_rules: tuple[EvidenceRule, ...]
    _inputs_json: str
    _acceptance_json: str
    _verification_json: str | None

    def __init__(self, goals: list[str] | tuple[str, ...], inputs: dict, budget_units: int,
                 operations: list[Operation] | tuple[Operation, ...], search_limit: int = DEFAULT_SEARCH_LIMIT,
                 acceptance: dict | None = None, *, planning_policy: str = "lowest_cost",
                 evidence_rules: list[dict] | tuple[dict, ...] = (), verification_policy: dict | None = None):
        if planning_policy not in ("lowest_cost", "preserve_fallback"):
            raise ValueError("planning_policy must be lowest_cost or preserve_fallback")
        object.__setattr__(self, "planning_policy", planning_policy)
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
        rules = parse_evidence_rules(evidence_rules)
        requestable = {rule.request for rule in rules if rule.request is not None}
        declared = inputs_keys | outputs.keys() | requestable
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
        if missing := ({rule.artifact for rule in rules} | requestable) - acceptance.keys():
            raise ValueError(f"evidence and requested inputs require an artifact acceptance contract: {sorted(missing)}")
        if missing := {rule.operation for rule in rules if rule.operation is not None} - {op.id for op in operations}:
            raise ValueError(f"unknown evidence operations: {sorted(missing)}")
        for artifact, contract in acceptance.items():
            _fields(contract, {"validator", "config"}, set(), f"acceptance for {artifact}")
            _name(contract["validator"], "artifact validator")
            if not isinstance(contract["config"], dict):
                raise ValueError("artifact validator config must be a JSON object")
        for artifact, count in outputs.items():
            if count > 1 and artifact not in acceptance:
                raise ValueError(f"multiple producers for {artifact!r} require an artifact acceptance contract")
        if verification_policy is not None:
            _validate_verification_policy(verification_policy, goals, operations, acceptance)
            if planning_policy != "lowest_cost" or any(rule.operation or rule.acquire for rule in rules):
                raise ValueError("verification_policy supports clarification rules before terminal route selection")
            if any(rule.request in goals for rule in rules):
                raise ValueError("a clarification cannot supply the calibrated terminal goal")
        object.__setattr__(self, "_verification_json",
                           canonical_json(verification_policy) if verification_policy is not None else None)
        object.__setattr__(self, "goals", tuple(sorted(goals)))
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "evidence_rules", rules)
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

    @property
    def verification_policy(self) -> dict | None:
        return json.loads(self._verification_json) if self._verification_json is not None else None

    @classmethod
    def from_dict(cls, raw: Mapping) -> Job:
        _fields(raw, {"goals", "inputs", "operations", "budget_units"},
                {"search_limit", "acceptance", "planning_policy", "evidence_rules", "verification_policy"}, "job")
        rows = raw["operations"]
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_OPERATIONS:
            raise ValueError(f"operations must be an array of at most {MAX_OPERATIONS} items")
        if "verification_policy" in raw and raw["verification_policy"] is None:
            raise ValueError("verification_policy must be an object")
        if "acceptance" in raw and raw["acceptance"] is None:
            raise ValueError("acceptance must be an object")
        return cls(raw["goals"], raw["inputs"], raw["budget_units"], tuple(Operation.from_dict(row) for row in rows),
                   raw.get("search_limit", DEFAULT_SEARCH_LIMIT), raw.get("acceptance"),
                   planning_policy=raw.get("planning_policy", "lowest_cost"), evidence_rules=raw.get("evidence_rules", ()),
                   verification_policy=raw.get("verification_policy"))

    def to_dict(self) -> dict:
        result = {"goals": list(self.goals), "inputs": self.inputs, "budget_units": self.budget_units,
                  "operations": [op.to_dict() for op in self.operations], "acceptance": self.acceptance,
                  "search_limit": self.search_limit}
        # Omitting defaults preserves the canonical hashes of previously stored jobs.
        if self.planning_policy != "lowest_cost":
            result["planning_policy"] = self.planning_policy
        if self.evidence_rules:
            result["evidence_rules"] = [rule.to_dict() for rule in self.evidence_rules]
        if self._verification_json is not None:
            result["verification_policy"] = self.verification_policy
        return result


def _validate_verification_policy(policy, goals, operations, acceptance):
    _fields(policy, {"artifact", "field", "model", "min_on_time_correct", "fallback_operation",
                     "route_ms", "deadline_ms"}, set(), "verification_policy")
    if len(goals) != 1 or _name(policy["artifact"], "verification artifact") not in acceptance:
        raise ValueError("verification_policy requires one goal and an accepted observation artifact")
    if policy["artifact"] == goals[0]:
        raise ValueError("the calibrated outcome cannot be its own observation")
    _name(policy["field"], "verification field")
    _integer(policy["deadline_ms"], "deadline_ms", 1, MAX_UNITS)
    floor = policy["min_on_time_correct"]
    if type(floor) not in (int, float) or not 0 <= floor <= 1:
        raise ValueError("min_on_time_correct must be a probability")
    validate_verification_model(policy["model"])
    durations = policy["route_ms"]
    if not isinstance(durations, dict) or not durations or durations.keys() - set(policy["model"]["actions"]):
        raise ValueError("route_ms must declare a nonempty subset of calibrated actions")
    terminal = {op.id for op in operations if op.produces == goals[0]}
    if durations.keys() - terminal:
        raise ValueError("every calibrated route must be a declared operation producing the final goal")
    for duration in durations.values():
        _integer(duration, "route duration estimate", 0, MAX_UNITS)
    fallback = policy["fallback_operation"]
    if fallback is not None and _name(fallback, "fallback_operation") not in durations:
        raise ValueError("fallback_operation must be a configured route")
