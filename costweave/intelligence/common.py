"""Shared deterministic validators for the CostWeave v0.6 contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping


_PREFIX_RE = re.compile(r"^[a-z_]{2,32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DECISION_INTELLIGENCE_SCHEMA_VERSION = "v0.6.0"
TOURNAMENT_SCHEMA_VERSION = "v0.6.0"
TOURNAMENT_POLICY_REVISION = "decision-intelligence-tournament-v2"
CLAIM_POLICY_REVISION = "competitive-claim-gate-v1"
META_CONTROLLER_SCHEMA_VERSION = "v0.6.1"
META_CONTROLLER_POLICY_REVISION = "rational-meta-controller-v1"
BUDGET_SCHEMA_VERSION = "v0.6.1"

EVIDENCE_LEVELS = (
    "E0-simulated",
    "E1-public-replay",
    "E2-directional-real",
    "E3-isolated-real",
    "E4-formal-benchmark",
    "E5-shadow-canary",
)

MANAGEMENT_MODES = ("no_delegation", "planner", "multiplanner", "expert")
META_ACTIONS = ("fast", "deliberate", "probe", "expert")
RISK_LEVELS = ("low", "medium", "high", "critical")
PARTITIONS = ("train", "calibration", "test")
RECOVERY_POLICIES = (
    "none",
    "retry_node",
    "switch_model",
    "subgraph_first",
    "full_replan",
    "stop_partial",
)
CREDIT_COMPONENTS = (
    "decomposition",
    "routing",
    "handoff",
    "execution",
    "validation",
    "recovery",
)


def _reject_bool(value: Any, name: str) -> None:
    if isinstance(value, bool):
        raise TypeError(f"{name} must not be bool")


def _ensure_json_value(value: Any, path: str = "value") -> None:
    """Reject values which ``json.dumps`` could coerce ambiguously."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _ensure_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} mapping keys must be str")
            _ensure_json_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} is not a JSON value")


def canonical_json(value: Any) -> str:
    """Return the only JSON representation accepted by v0.6 contracts."""

    _ensure_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_digest(value: Any, *, length: int = 32) -> str:
    """Hash a JSON value and return a deterministic lowercase hex prefix."""

    if isinstance(length, bool) or not isinstance(length, int):
        raise TypeError("length must be int")
    if not 1 <= length <= 64:
        raise ValueError("length must be between 1 and 64")
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def stable_id(prefix: str, identity: Mapping[str, Any]) -> str:
    """Create a content ID whose timestamp-independent suffix is 32 hex chars."""

    if not isinstance(prefix, str) or not _PREFIX_RE.fullmatch(prefix):
        raise ValueError("prefix must contain 2-32 lowercase letters or underscores")
    if not isinstance(identity, Mapping):
        raise TypeError("identity must be a mapping")
    return f"{prefix}_{stable_digest(dict(identity), length=32)}"


def bounded_text(
    value: Any,
    name: str,
    *,
    minimum: int = 1,
    maximum: int = 200,
) -> str:
    """Validate and trim a bounded human-readable or identifier string."""

    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        raise TypeError("minimum must be int")
    if isinstance(maximum, bool) or not isinstance(maximum, int):
        raise TypeError("maximum must be int")
    if minimum < 0 or maximum < minimum:
        raise ValueError("invalid text bounds")
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    text = value.strip()
    if not minimum <= len(text) <= maximum:
        raise ValueError(f"{name} length must be between {minimum} and {maximum}")
    return text


def finite_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
) -> float:
    """Validate a finite numeric value without treating bool as a number."""

    _reject_bool(value, name)
    if not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be int or float")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None:
        _reject_bool(minimum, "minimum")
        if number < float(minimum):
            raise ValueError(f"{name} must be >= {minimum}")
    return number


def probability(value: Any, name: str) -> float:
    """Validate a finite probability in the closed interval [0, 1]."""

    number = finite_number(value, name, minimum=0.0)
    if number > 1.0:
        raise ValueError(f"{name} must be <= 1")
    return number


def unique_texts(
    values: Iterable[Any],
    name: str,
    *,
    maximum_items: int = 64,
) -> tuple[str, ...]:
    """Validate ordered text values and reject duplicates instead of deduplicating."""

    if isinstance(maximum_items, bool) or not isinstance(maximum_items, int):
        raise TypeError("maximum_items must be int")
    if maximum_items < 0:
        raise ValueError("maximum_items must be non-negative")
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if index >= maximum_items:
            raise ValueError(f"{name} exceeds maximum_items={maximum_items}")
        text = bounded_text(value, f"{name}[{index}]")
        if text in seen:
            raise ValueError(f"{name} contains duplicate text: {text!r}")
        seen.add(text)
        result.append(text)
    return tuple(result)


def validate_timestamp(value: Any, name: str) -> str:
    """Validate an ISO-8601 timestamp carrying an explicit timezone."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    text = value.strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def validate_sha256(value: Any, name: str) -> str:
    """Validate a lowercase, full-length SHA-256 hexadecimal digest."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def validate_exact_fields(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    """Reject missing required keys and every key outside the declared schema."""

    if not isinstance(value, Mapping):
        raise TypeError("value must be a mapping")
    if not required.isdisjoint(optional):
        raise ValueError("required and optional fields must be disjoint")
    keys = set(value.keys())
    if any(not isinstance(key, str) for key in keys):
        raise TypeError("field names must be str")
    missing = set(required) - keys
    unknown = keys - set(required) - set(optional)
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)!r}")
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)!r}")


@dataclass(frozen=True, slots=True)
class ProbabilityInterval:
    mean: float
    lower: float
    upper: float

    def __post_init__(self) -> None:
        mean = probability(self.mean, "mean")
        lower = probability(self.lower, "lower")
        upper = probability(self.upper, "upper")
        if lower > mean or mean > upper:
            raise ValueError("probability interval must satisfy lower <= mean <= upper")
        object.__setattr__(self, "mean", mean)
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)

    def to_dict(self) -> dict[str, float]:
        return {"mean": self.mean, "lower": self.lower, "upper": self.upper}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProbabilityInterval":
        validate_exact_fields(value, required=frozenset({"mean", "lower", "upper"}))
        return cls(mean=value["mean"], lower=value["lower"], upper=value["upper"])


@dataclass(frozen=True, slots=True)
class NonNegativeEstimate:
    mean: float
    p95: float

    def __post_init__(self) -> None:
        mean = finite_number(self.mean, "mean", minimum=0.0)
        p95 = finite_number(self.p95, "p95", minimum=0.0)
        if mean > p95:
            raise ValueError("estimate must satisfy mean <= p95")
        object.__setattr__(self, "mean", mean)
        object.__setattr__(self, "p95", p95)

    def to_dict(self) -> dict[str, float]:
        return {"mean": self.mean, "p95": self.p95}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NonNegativeEstimate":
        validate_exact_fields(value, required=frozenset({"mean", "p95"}))
        return cls(mean=value["mean"], p95=value["p95"])


__all__ = [
    "CLAIM_POLICY_REVISION",
    "BUDGET_SCHEMA_VERSION",
    "CREDIT_COMPONENTS",
    "DECISION_INTELLIGENCE_SCHEMA_VERSION",
    "EVIDENCE_LEVELS",
    "MANAGEMENT_MODES",
    "META_CONTROLLER_POLICY_REVISION",
    "META_CONTROLLER_SCHEMA_VERSION",
    "META_ACTIONS",
    "NonNegativeEstimate",
    "PARTITIONS",
    "ProbabilityInterval",
    "RECOVERY_POLICIES",
    "RISK_LEVELS",
    "TOURNAMENT_POLICY_REVISION",
    "TOURNAMENT_SCHEMA_VERSION",
    "bounded_text",
    "canonical_json",
    "finite_number",
    "probability",
    "stable_digest",
    "stable_id",
    "unique_texts",
    "validate_exact_fields",
    "validate_sha256",
    "validate_timestamp",
]
