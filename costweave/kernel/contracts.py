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


def _path(raw) -> tuple[str | int, ...]:
    if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 64:
        raise ValueError("path must contain 1 to 64 JSON keys or nonnegative array indices")
    for part in raw:
        if type(part) is not str and not (type(part) is int and part >= 0):
            raise ValueError("invalid path component")
    return tuple(raw)


def _scalar(value):
    if value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("equals requires a finite JSON scalar")
    canonical_json(value)
    return value


def _matches_path(value, path, expected) -> bool:
    for key in path:
        if type(key) is str and isinstance(value, dict) and key in value:
            value = value[key]
        elif type(key) is int and isinstance(value, list) and key < len(value):
            value = value[key]
        else:
            return False
    return type(value) is type(expected) and value == expected


@dataclass(frozen=True, slots=True)
class ArtifactCondition:
    artifact: str
    path: tuple[str | int, ...]
    equals: str | int | float | bool | None

    @classmethod
    def from_dict(cls, raw: Mapping) -> ArtifactCondition:
        _fields(raw, {"artifact", "path", "equals"}, set(), "operation guard")
        return cls(_name(raw["artifact"], "guard artifact"), _path(raw["path"]), _scalar(raw["equals"]))

    def to_dict(self) -> dict:
        return {"artifact": self.artifact, "path": list(self.path), "equals": self.equals}


@dataclass(frozen=True, slots=True)
class EvidenceRule:
    artifact: str
    field: str | None
    equals: str | int | float | bool | None
    operation: str | None = None
    request: str | None = None
    reason: str | None = None
    acquire: bool = False
    path: tuple[str | int, ...] | None = None

    def matches(self, artifacts: Mapping) -> bool:
        return self.artifact in artifacts and _matches_path(
            artifacts[self.artifact], self.path or (self.field,), self.equals)

    def to_dict(self) -> dict:
        row = {"artifact": self.artifact, "equals": self.equals}
        if self.field is not None:
            row["field"] = self.field
        if self.path is not None:
            row["path"] = list(self.path)
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
        keys = {"artifact", "equals"}
        selects_operation = isinstance(row, Mapping) and "operation" in row
        keys.update({"operation"} if selects_operation else {"request", "reason"})
        _fields(row, keys, {"acquire", "field", "path"}, "evidence rule")
        if "acquire" in row and type(row["acquire"]) is not bool:
            raise ValueError("evidence acquire must be a boolean")
        for key in keys - {"equals", "reason"}:
            _name(row[key], f"evidence {key}")
        field = _name(row["field"], "evidence field") if "field" in row else None
        path = _path(row["path"]) if "path" in row else None
        if field is None and path is None:
            raise ValueError("evidence rule requires field or path")
        if field is not None and path is not None and path != (field,):
            raise ValueError("evidence field and path disagree")
        if "reason" in row:
            reason = row["reason"]
            if not isinstance(reason, str) or not 1 <= len(reason) <= 1000 or reason.strip() != reason:
                raise ValueError("reason must contain 1 to 1000 characters without surrounding whitespace")
        _scalar(row["equals"])
        canonical_json(dict(row))
        rules.append(EvidenceRule(**{**row, "field": field, "path": path}))
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
    expected_units: float | None
    pass_prob: float
    false_pass_risk: float
    latency_p50_ms: int
    latency_p95_ms: int
    provider_family: str
    max_attempts: int
    _config_json: str
    _guard: ArtifactCondition | None

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
        latency_p95_ms: int | None = None,
        provider_family: str = "local",
        max_attempts: int = 1,
        when: Mapping | None = None,
    ):
        for field, value in (("id", id), ("produces", produces), ("executor", executor), ("validator", validator)):
            object.__setattr__(self, field, _name(value, field))
        if route_role not in ("standard", "speculative", "fallback"):
            raise ValueError("route_role must be standard, speculative, or fallback")
        object.__setattr__(self, "route_role", route_role)
        object.__setattr__(self, "requires", _names(requires, "requires"))
        object.__setattr__(self, "_guard", ArtifactCondition.from_dict(when) if when is not None else None)
        units = _integer(cost_units, "cost_units", 0, MAX_UNITS)
        object.__setattr__(self, "cost_units", units)
        exp = None if expected_units is None else _number(expected_units, "expected_units", 0.0, float(units))
        object.__setattr__(self, "expected_units", exp)
        object.__setattr__(self, "pass_prob", _number(pass_prob, "pass_prob", 0.0, 1.0, allow_zero=False))
        object.__setattr__(self, "false_pass_risk", _number(false_pass_risk, "false_pass_risk", 0.0, 1.0))
        p50 = _integer(latency_p50_ms, "latency_p50_ms", 0, 86_400_000)
        p95 = p50 if latency_p95_ms is None else _integer(latency_p95_ms, "latency_p95_ms", p50, 86_400_000)
        object.__setattr__(self, "latency_p50_ms", p50)
        object.__setattr__(self, "latency_p95_ms", p95)
        object.__setattr__(self, "provider_family", _name(provider_family, "provider_family"))
        object.__setattr__(self, "max_attempts", _integer(max_attempts, "max_attempts", 1, 16))
        object.__setattr__(self, "_config_json", _json_object({} if config is None else config, "config"))

    @property
    def when(self) -> dict | None:
        return self._guard.to_dict() if self._guard is not None else None

    @property
    def dependencies(self) -> tuple[str, ...]:
        """Planning includes the observation even when the executor does not consume its value."""
        if self._guard is not None and self._guard.artifact not in self.requires:
            return (*self.requires, self._guard.artifact)
        return self.requires

    def guard_state(self, artifacts: Mapping) -> str:
        guard = self._guard
        if guard is None:
            return "satisfied"
        if guard.artifact not in artifacts:
            return "pending"
        return "satisfied" if _matches_path(artifacts[guard.artifact], guard.path, guard.equals) else "falsified"

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
                "latency_p50_ms", "latency_p95_ms", "provider_family", "max_attempts", "when",
            },
            "operation",
        )
        if "config" in raw and raw["config"] is None:
            raise ValueError("config must be a JSON object")
        if "when" in raw and raw["when"] is None:
            raise ValueError("when must be an operation guard")
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
        if self.latency_p95_ms != self.latency_p50_ms:
            result["latency_p95_ms"] = self.latency_p95_ms
        if self.provider_family != "local":
            result["provider_family"] = self.provider_family
        if self.max_attempts != 1:
            result["max_attempts"] = self.max_attempts
        if self._guard is not None:
            result["when"] = self._guard.to_dict()
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
    evidence_rules: tuple[EvidenceRule, ...]
    _inputs_json: str
    _acceptance_json: str
    belief_model_sha256: str | None
    _verification_json: str | None

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
        evidence_rules: list[dict] | tuple[dict, ...] = (),
        verification_policy: dict | None = None,
        belief_model_sha256: str | None = None,
    ):
        if belief_model_sha256 is not None and (
                not isinstance(belief_model_sha256, str) or len(belief_model_sha256) != 64
                or any(ch not in "0123456789abcdef" for ch in belief_model_sha256)):
            raise ValueError("belief_model_sha256 requires a lowercase SHA-256 digest")
        object.__setattr__(self, "belief_model_sha256", belief_model_sha256)
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
        rules = parse_evidence_rules(evidence_rules)
        requestable = {rule.request for rule in rules if rule.request is not None}
        declared = inputs_keys | outputs.keys() | requestable
        if len(declared) > MAX_ARTIFACTS:
            raise ValueError(f"a job may declare at most {MAX_ARTIFACTS} artifacts")
        references = set(goals).union(*(op.dependencies for op in operations))
        if missing := references - declared:
            raise ValueError(f"unknown artifact references: {sorted(missing)}")
        acceptance = {} if acceptance is None else acceptance
        if not isinstance(acceptance, dict) or acceptance.keys() - declared:
            raise ValueError("acceptance must map declared artifacts to validator contracts")
        if missing := set(goals) - acceptance.keys():
            raise ValueError(f"final goals require an artifact acceptance contract: {sorted(missing)}")
        guarded = {op._guard.artifact for op in operations if op._guard is not None}
        if missing := ({rule.artifact for rule in rules} | requestable | guarded) - acceptance.keys():
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
        _fields(
            raw,
            {"goals", "inputs", "operations", "budget_units"},
            {
                "search_limit", "acceptance", "planning_policy", "evidence_rules", "verification_policy", "belief_model_sha256",
                "max_risk_alpha", "failure_penalty_units", "corruption_penalty_units",
                "deadline_ms", "latency_weight_units_per_sec", "max_parallel",
            },
            "job",
        )
        rows = raw["operations"]
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_OPERATIONS:
            raise ValueError(f"operations must be an array of at most {MAX_OPERATIONS} items")
        if "verification_policy" in raw and raw["verification_policy"] is None:
            raise ValueError("verification_policy must be an object")
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
            evidence_rules=raw.get("evidence_rules", ()),
            verification_policy=raw.get("verification_policy"),
            belief_model_sha256=raw.get("belief_model_sha256"),
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
        if self.evidence_rules:
            result["evidence_rules"] = [rule.to_dict() for rule in self.evidence_rules]
        if self.belief_model_sha256 is not None:
            result["belief_model_sha256"] = self.belief_model_sha256
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
