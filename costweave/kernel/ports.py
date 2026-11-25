"""Host-owned executor and validator bindings for artifact jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import Job, Operation, canonical_json


class KnownFailure(RuntimeError):
    """A definitive unsuccessful attempt. A different producer may be tried."""

    def __init__(self, message: str, *, actual_units: int | None = None):
        super().__init__(message)
        self.actual_units = actual_units


class AdmissionDenied(KnownFailure):
    """A host policy denied dispatch; stop the run before considering substitutes."""

    def __init__(self, message: str):
        super().__init__(message, actual_units=0)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    value: Any
    actual_units: int | None = None
    receipt_id: str | None = None


class Executor(Protocol):
    def __call__(self, operation: Operation, inputs: Mapping[str, Any], *, attempt_id: str) -> Any: ...


Validator = Callable[[Operation, Any, Mapping[str, Any]], bool]
ConfigCheck = Callable[[Operation], None]


def _rule_operation(artifact: str, rule: Mapping) -> Operation:
    # Validators use the same interface at publication and for initial inputs.
    return Operation(artifact, (), artifact, "artifact-acceptance", rule["validator"], 0, rule["config"])


def _inputs_for(op: Operation, inputs: Mapping[str, Any]) -> dict:
    return {name: deepcopy(inputs[name]) for name in op.requires}


class Registry:
    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}
        self._validators: dict[str, Validator] = {}
        self._executor_checks: dict[str, ConfigCheck] = {}
        self._validator_checks: dict[str, ConfigCheck] = {}

    def register_executor(self, name: str, fn: Executor, *, check_config: ConfigCheck | None = None) -> None:
        if not isinstance(name, str) or not name.strip() or not callable(fn) or name in self._executors:
            raise ValueError(f"invalid or duplicate executor: {name!r}")
        self._executors[name] = fn
        if check_config is not None:
            self._executor_checks[name] = check_config

    def register_validator(self, name: str, fn: Validator, *, check_config: ConfigCheck | None = None) -> None:
        if not isinstance(name, str) or not name.strip() or not callable(fn) or name in self._validators:
            raise ValueError(f"invalid or duplicate validator: {name!r}")
        self._validators[name] = fn
        if check_config is not None:
            self._validator_checks[name] = check_config

    def _check_validator(self, op: Operation) -> None:
        if op.validator not in self._validators:
            raise ValueError(f"unregistered validator: {op.validator}")
        if op.validator in self._validator_checks:
            self._validator_checks[op.validator](op)

    def check(self, job: Job) -> None:
        for op in job.operations:
            if op.executor not in self._executors:
                raise ValueError(f"unregistered executor: {op.executor}")
            if op.executor in self._executor_checks:
                self._executor_checks[op.executor](op)
            self._check_validator(op)
        for artifact, rule in job.acceptance.items():
            self._check_validator(_rule_operation(artifact, rule))

    def perform(self, operation: Operation, inputs: Mapping, *, attempt_id: str) -> ExecutionResult:
        raw = self._executors[operation.executor](operation, _inputs_for(operation, inputs), attempt_id=attempt_id)
        result = raw if isinstance(raw, ExecutionResult) else ExecutionResult(raw)
        if result.actual_units is not None and (
            type(result.actual_units) is not int or not 0 <= result.actual_units <= operation.cost_units
        ):
            # The host reported a discrepant charge. Keep the reservation for reconciliation.
            raise ValueError("executor settlement exceeds its quote or is invalid")
        try:
            canonical_json(result.value)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            raise KnownFailure("executor returned invalid JSON", actual_units=result.actual_units) from exc
        return result

    def execute(self, operation: Operation, inputs: Mapping, *, attempt_id: str) -> Any:
        return self.perform(operation, inputs, attempt_id=attempt_id).value

    def validate(self, operation: Operation, value: Any, inputs: Mapping) -> bool:
        verdict = self._validators[operation.validator](operation, deepcopy(value), _inputs_for(operation, inputs))
        if type(verdict) is not bool:
            raise TypeError("validator must return a boolean verdict")
        return verdict

    def accept(self, artifact_name: str, rule: Mapping, value: Any) -> bool:
        return self.validate(_rule_operation(artifact_name, rule), value, {})


def _required(field: str) -> ConfigCheck:
    def check(op: Operation) -> None:
        if field not in op.config:
            raise ValueError(f"{op.id} requires config.{field}")
    return check


def _check_pick(op: Operation) -> None:
    config = op.config
    path = config.get("path", [])
    if config.get("source") not in op.requires or not isinstance(path, list) or any(
        not (isinstance(step, str) or type(step) is int and step >= 0) for step in path
    ):
        raise ValueError(f"{op.id} requires a declared source and a path of keys or non-negative indexes")


def _pick(op: Operation, inputs: Mapping, *, attempt_id: str) -> Any:
    config = op.config
    value = inputs[config["source"]]
    for step in config.get("path", []):
        if isinstance(step, str) and isinstance(value, dict) and step in value:
            value = value[step]
        elif type(step) is int and isinstance(value, list) and 0 <= step < len(value):
            value = value[step]
        else:
            raise KnownFailure("selected path is absent from the input artifact")
    return value


def _check_keys(op: Operation) -> None:
    keys = op.config.get("required_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError(f"{op.id} requires a list of JSON object keys")


def default_registry() -> Registry:
    registry = Registry()
    registry.register_executor("literal", lambda op, inputs, *, attempt_id: op.config["value"],
                               check_config=_required("value"))
    registry.register_executor("collect", lambda op, inputs, *, attempt_id: dict(inputs))
    registry.register_executor("pick", _pick, check_config=_check_pick)
    registry.register_validator("present", lambda op, value, inputs: value is not None)
    registry.register_validator("equals", lambda op, value, inputs: canonical_json(value) == canonical_json(op.config["expected"]),
                                check_config=_required("expected"))
    registry.register_validator("json_object", lambda op, value, inputs: isinstance(value, dict) and
                                all(key in value for key in op.config.get("required_keys", [])), check_config=_check_keys)
    return registry
