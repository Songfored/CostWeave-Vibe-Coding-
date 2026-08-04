"""Deterministic offline replay and approximate-oracle audit for v0.5.7.1."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .evaluation_protocol import canonical_json


OFFLINE_REPLAY_SCHEMA_VERSION = "v0.5.7.1"
OFFLINE_REPLAY_POLICY_REVISION = "offline-replay-oracle-v1"
REPLAY_CASE_SCHEMA_VERSION = "offline-replay-case-v1"
ROUTE_OUTCOME_SCHEMA_VERSION = "observed-route-outcome-v1"

MAXIMUM_REPLAY_CASES = 250
MAXIMUM_ROUTES_PER_CASE = 64
MAXIMUM_BASELINES = 16
MINIMUM_THRESHOLD_CASES = 30
MEDIAN_NORMALIZED_REGRET_MAXIMUM = 0.08
P90_NORMALIZED_REGRET_MAXIMUM = 0.20

SELECTION_RULES = frozenset(
    {
        "minimum_expected_loss",
        "minimum_direct_cost",
        "maximum_success_lower",
        "frozen_score",
        "recorded_route",
    }
)
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
REAL_DATA_ORIGINS = frozenset({"production", "human_benchmark"})
ALL_DATA_ORIGINS = REAL_DATA_ORIGINS | {"test_fixture", "simulated"}


class OfflineReplayError(ValueError):
    """Raised when replay or historical evidence is invalid."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OfflineReplayError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise OfflineReplayError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise OfflineReplayError(f"{name} is below its minimum")
    if maximum is not None and number > maximum:
        raise OfflineReplayError(f"{name} exceeds its maximum")
    return number


def _bounded(value: str, name: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 160 or "\n" in text or "\r" in text:
        raise OfflineReplayError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("bearer ", "api_key=", "password=", "secret=")
    ):
        raise OfflineReplayError(f"{name} appears to contain a secret")
    return text


def _sha256(value: str, name: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise OfflineReplayError(f"{name} must be a sha256 digest")
    return text


def _timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise OfflineReplayError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise OfflineReplayError(f"{name} must include timezone")
    return str(value)


def _evidence_rank(value: str) -> int:
    prefix = str(value).split("-", 1)[0].upper()
    return {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4, "E5": 5}.get(
        prefix, -1
    )


def _anonymous(value: str, prefix: str, name: str) -> str:
    text = str(value)
    suffix = text.removeprefix(prefix)
    if (
        not text.startswith(prefix)
        or len(suffix) != 32
        or any(char not in "0123456789abcdef" for char in suffix.lower())
    ):
        raise OfflineReplayError(f"{name} must be anonymous")
    return text


def _unique(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(_bounded(item, name) for item in values)
    if len(result) != len(set(result)):
        raise OfflineReplayError(f"{name} values must be unique")
    return result


@dataclass(frozen=True, slots=True)
class ReplayVersionSnapshot:
    snapshot_id: str
    policy_id: str
    policy_version: str
    feature_schema_version: str
    task_contract_schema_version: str
    catalog_snapshot_hash: str
    prompt_revision: str
    tool_revision: str
    capacity_snapshot_hash: str
    objective_revision: str
    constraint_revision: str
    code_revision: str
    captured_at: str
    evidence_level: str
    advisory_only: bool = True
    external_calls_allowed: bool = False
    routing_authority: bool = False
    may_change_actual_route: bool = False
    schema_version: str = OFFLINE_REPLAY_SCHEMA_VERSION
    policy_revision: str = OFFLINE_REPLAY_POLICY_REVISION

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "policy_version",
            "feature_schema_version",
            "task_contract_schema_version",
            "prompt_revision",
            "tool_revision",
            "objective_revision",
            "constraint_revision",
            "code_revision",
        ):
            _bounded(str(getattr(self, name)), name)
        _sha256(self.catalog_snapshot_hash, "catalog_snapshot_hash")
        _sha256(self.capacity_snapshot_hash, "capacity_snapshot_hash")
        _timestamp(self.captured_at, "captured_at")
        if _evidence_rank(self.evidence_level) < 0:
            raise OfflineReplayError("snapshot evidence level is invalid")
        if (
            self.schema_version != OFFLINE_REPLAY_SCHEMA_VERSION
            or self.policy_revision != OFFLINE_REPLAY_POLICY_REVISION
        ):
            raise OfflineReplayError("snapshot schema is invalid")
        if (
            not self.advisory_only
            or self.external_calls_allowed
            or self.routing_authority
            or self.may_change_actual_route
        ):
            raise OfflineReplayError("offline replay snapshot cannot execute")
        expected = "replaysnap_" + _digest(self.identity())[:32]
        if self.snapshot_id != expected:
            raise OfflineReplayError("replay snapshot id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "feature_schema_version": self.feature_schema_version,
            "task_contract_schema_version": self.task_contract_schema_version,
            "catalog_snapshot_hash": self.catalog_snapshot_hash,
            "prompt_revision": self.prompt_revision,
            "tool_revision": self.tool_revision,
            "capacity_snapshot_hash": self.capacity_snapshot_hash,
            "objective_revision": self.objective_revision,
            "constraint_revision": self.constraint_revision,
            "code_revision": self.code_revision,
            "captured_at": self.captured_at,
            "evidence_level": self.evidence_level,
            "advisory_only": self.advisory_only,
            "external_calls_allowed": self.external_calls_allowed,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, **self.identity()}

    @classmethod
    def create(cls, **values: Any) -> "ReplayVersionSnapshot":
        identity = {
            **values,
            "advisory_only": True,
            "external_calls_allowed": False,
            "routing_authority": False,
            "may_change_actual_route": False,
            "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
            "policy_revision": OFFLINE_REPLAY_POLICY_REVISION,
        }
        return cls(snapshot_id="replaysnap_" + _digest(identity)[:32], **identity)


@dataclass(frozen=True, slots=True)
class ReplayRouteCandidate:
    route_id: str
    route_fingerprint: str
    direct_cost_usd: float
    expected_total_loss: float
    success_lower: float
    latency_p95_ms: float
    hard_constraints_passed: bool

    def __post_init__(self) -> None:
        _bounded(self.route_id, "route_id")
        _sha256(self.route_fingerprint, "route_fingerprint")
        _finite(self.direct_cost_usd, "direct_cost_usd", minimum=0.0)
        _finite(self.expected_total_loss, "expected_total_loss", minimum=0.0)
        _finite(self.success_lower, "success_lower", minimum=0.0, maximum=1.0)
        _finite(self.latency_p95_ms, "latency_p95_ms", minimum=0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "route_fingerprint": self.route_fingerprint,
            "direct_cost_usd": self.direct_cost_usd,
            "expected_total_loss": self.expected_total_loss,
            "success_lower": self.success_lower,
            "latency_p95_ms": self.latency_p95_ms,
            "hard_constraints_passed": self.hard_constraints_passed,
        }


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    snapshot_id: str
    anonymous_task_id: str
    task_family: str
    risk_level: str
    reversible: bool
    permission_sensitive: bool
    recorded_selected_route_id: str
    candidates: tuple[ReplayRouteCandidate, ...]
    case_hash: str
    schema_version: str = REPLAY_CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REPLAY_CASE_SCHEMA_VERSION:
            raise OfflineReplayError("replay case schema is invalid")
        _anonymous(self.anonymous_task_id, "task_", "anonymous task id")
        _bounded(self.snapshot_id, "snapshot_id")
        _bounded(self.task_family, "task_family")
        if self.risk_level not in RISK_LEVELS:
            raise OfflineReplayError("replay case risk level is invalid")
        if not 2 <= len(self.candidates) <= MAXIMUM_ROUTES_PER_CASE:
            raise OfflineReplayError("replay case route count is invalid")
        route_ids = [item.route_id for item in self.candidates]
        if len(route_ids) != len(set(route_ids)):
            raise OfflineReplayError("replay route ids must be unique")
        if self.recorded_selected_route_id not in route_ids:
            raise OfflineReplayError("recorded route is not a candidate")
        expected_hash = _digest(self.payload())
        if self.case_hash != expected_hash:
            raise OfflineReplayError("replay case hash is invalid")
        expected_id = "replaycase_" + expected_hash[:32]
        if self.case_id != expected_id:
            raise OfflineReplayError("replay case id is invalid")

    @property
    def multi_route_evaluation_allowed(self) -> bool:
        return (
            self.risk_level not in {"high", "critical"}
            and self.reversible
            and not self.permission_sensitive
        )

    def payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "anonymous_task_id": self.anonymous_task_id,
            "task_family": self.task_family,
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "permission_sensitive": self.permission_sensitive,
            "recorded_selected_route_id": self.recorded_selected_route_id,
            "candidates": [
                item.to_dict()
                for item in sorted(self.candidates, key=lambda row: row.route_id)
            ],
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            **self.payload(),
            "case_hash": self.case_hash,
            "multi_route_evaluation_allowed": self.multi_route_evaluation_allowed,
        }

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        anonymous_task_id: str,
        task_family: str,
        risk_level: str,
        reversible: bool,
        permission_sensitive: bool,
        recorded_selected_route_id: str,
        candidates: Sequence[ReplayRouteCandidate],
    ) -> "ReplayCase":
        ordered = tuple(sorted(candidates, key=lambda row: row.route_id))
        payload = {
            "snapshot_id": snapshot_id,
            "anonymous_task_id": anonymous_task_id,
            "task_family": task_family,
            "risk_level": risk_level,
            "reversible": bool(reversible),
            "permission_sensitive": bool(permission_sensitive),
            "recorded_selected_route_id": recorded_selected_route_id,
            "candidates": [item.to_dict() for item in ordered],
            "schema_version": REPLAY_CASE_SCHEMA_VERSION,
        }
        case_hash = _digest(payload)
        return cls(
            case_id="replaycase_" + case_hash[:32],
            snapshot_id=snapshot_id,
            anonymous_task_id=anonymous_task_id,
            task_family=task_family,
            risk_level=risk_level,
            reversible=bool(reversible),
            permission_sensitive=bool(permission_sensitive),
            recorded_selected_route_id=recorded_selected_route_id,
            candidates=ordered,
            case_hash=case_hash,
        )


@dataclass(frozen=True, slots=True)
class ReplayPolicySpec:
    policy_id: str
    policy_version: str
    selection_rule: str
    frozen_route_scores: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        _bounded(self.policy_id, "replay_policy_id")
        _bounded(self.policy_version, "replay_policy_version")
        if self.selection_rule not in SELECTION_RULES:
            raise OfflineReplayError("replay selection rule is invalid")
        if self.selection_rule == "frozen_score":
            if not self.frozen_route_scores:
                raise OfflineReplayError("frozen-score policy requires route scores")
            for route_id, score in self.frozen_route_scores.items():
                _bounded(route_id, "frozen_score_route")
                _finite(score, "frozen_route_score")
        elif self.frozen_route_scores is not None:
            raise OfflineReplayError("route scores only belong to frozen_score")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "selection_rule": self.selection_rule,
            "frozen_route_scores": (
                None
                if self.frozen_route_scores is None
                else dict(sorted(self.frozen_route_scores.items()))
            ),
        }


@dataclass(frozen=True, slots=True)
class OfflineReplayResult:
    replay_id: str
    snapshot_id: str
    case_id: str
    case_hash: str
    policy_id: str
    policy_version: str
    selection_rule: str
    state: str
    selected_route_id: str | None
    candidate_order: tuple[str, ...]
    recorded_selection_match: bool | None
    decision_fingerprint: str
    deterministic_replay: bool = True
    external_call_count: int = 0
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    schema_version: str = OFFLINE_REPLAY_SCHEMA_VERSION
    policy_revision: str = OFFLINE_REPLAY_POLICY_REVISION

    def __post_init__(self) -> None:
        if (
            self.schema_version != OFFLINE_REPLAY_SCHEMA_VERSION
            or self.policy_revision != OFFLINE_REPLAY_POLICY_REVISION
        ):
            raise OfflineReplayError("replay result schema is invalid")
        if self.state not in {"replayed", "no_feasible_route"}:
            raise OfflineReplayError("replay result state is invalid")
        if (self.state == "replayed") != (self.selected_route_id is not None):
            raise OfflineReplayError("replay selection state is inconsistent")
        if self.state == "replayed" and self.recorded_selection_match is None:
            raise OfflineReplayError("replay match state is missing")
        _sha256(self.case_hash, "case_hash")
        _sha256(self.decision_fingerprint, "decision_fingerprint")
        _unique(self.candidate_order, "replay_candidate_order")
        if (
            not self.deterministic_replay
            or self.external_call_count != 0
            or not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
        ):
            raise OfflineReplayError("offline replay cannot execute or mutate routing")
        expected = "replay_" + _digest(self.identity())[:32]
        if self.replay_id != expected:
            raise OfflineReplayError("offline replay id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "case_id": self.case_id,
            "case_hash": self.case_hash,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "selection_rule": self.selection_rule,
            "state": self.state,
            "selected_route_id": self.selected_route_id,
            "candidate_order": list(self.candidate_order),
            "recorded_selection_match": self.recorded_selection_match,
            "decision_fingerprint": self.decision_fingerprint,
            "deterministic_replay": self.deterministic_replay,
            "external_call_count": self.external_call_count,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"replay_id": self.replay_id, **self.identity()}


class OfflineReplayEngine:
    def replay(
        self,
        snapshot: ReplayVersionSnapshot,
        case: ReplayCase,
        policy: ReplayPolicySpec,
    ) -> OfflineReplayResult:
        if case.snapshot_id != snapshot.snapshot_id:
            raise OfflineReplayError("replay case does not match snapshot")
        feasible = tuple(
            item for item in case.candidates if item.hard_constraints_passed
        )
        if policy.selection_rule == "frozen_score":
            assert policy.frozen_route_scores is not None
            if set(policy.frozen_route_scores) != {
                item.route_id for item in case.candidates
            }:
                raise OfflineReplayError(
                    "frozen policy scores must cover every replay route"
                )

        def ordering(item: ReplayRouteCandidate) -> tuple[Any, ...]:
            if policy.selection_rule == "minimum_expected_loss":
                return (item.expected_total_loss, item.route_id)
            if policy.selection_rule == "minimum_direct_cost":
                return (item.direct_cost_usd, item.route_id)
            if policy.selection_rule == "maximum_success_lower":
                return (-item.success_lower, item.expected_total_loss, item.route_id)
            if policy.selection_rule == "frozen_score":
                assert policy.frozen_route_scores is not None
                return (float(policy.frozen_route_scores[item.route_id]), item.route_id)
            return (
                0 if item.route_id == case.recorded_selected_route_id else 1,
                item.route_id,
            )

        ordered = tuple(sorted(feasible, key=ordering))
        selected = ordered[0].route_id if ordered else None
        state = "replayed" if selected is not None else "no_feasible_route"
        match = (
            selected == case.recorded_selected_route_id
            if selected is not None
            else None
        )
        decision_fingerprint = _digest(
            {
                "snapshot_id": snapshot.snapshot_id,
                "case_hash": case.case_hash,
                "policy": policy.to_dict(),
                "candidate_order": [item.route_id for item in ordered],
                "selected_route_id": selected,
            }
        )
        identity = {
            "snapshot_id": snapshot.snapshot_id,
            "case_id": case.case_id,
            "case_hash": case.case_hash,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "selection_rule": policy.selection_rule,
            "state": state,
            "selected_route_id": selected,
            "candidate_order": [item.route_id for item in ordered],
            "recorded_selection_match": match,
            "decision_fingerprint": decision_fingerprint,
            "deterministic_replay": True,
            "external_call_count": 0,
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
            "policy_revision": OFFLINE_REPLAY_POLICY_REVISION,
        }
        return OfflineReplayResult(
            replay_id="replay_" + _digest(identity)[:32],
            snapshot_id=snapshot.snapshot_id,
            case_id=case.case_id,
            case_hash=case.case_hash,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            selection_rule=policy.selection_rule,
            state=state,
            selected_route_id=selected,
            candidate_order=tuple(item.route_id for item in ordered),
            recorded_selection_match=match,
            decision_fingerprint=decision_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class ObservedRouteOutcome:
    outcome_id: str
    case_id: str
    route_id: str
    accepted: bool
    realized_total_loss: float
    evidence_level: str
    data_origin: str
    independently_validated: bool
    selection_propensity: float | None
    schema_version: str = ROUTE_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _anonymous(self.outcome_id, "routeout_", "route outcome id")
        _bounded(self.case_id, "outcome_case_id")
        _bounded(self.route_id, "outcome_route_id")
        _finite(self.realized_total_loss, "realized_total_loss", minimum=0.0)
        if _evidence_rank(self.evidence_level) < 0:
            raise OfflineReplayError("route outcome evidence is invalid")
        if self.data_origin not in ALL_DATA_ORIGINS:
            raise OfflineReplayError("route outcome origin is invalid")
        if self.selection_propensity is not None:
            _finite(
                self.selection_propensity,
                "selection_propensity",
                minimum=1e-12,
                maximum=1.0,
            )
        if self.schema_version != ROUTE_OUTCOME_SCHEMA_VERSION:
            raise OfflineReplayError("route outcome schema is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "case_id": self.case_id,
            "route_id": self.route_id,
            "accepted": self.accepted,
            "realized_total_loss": self.realized_total_loss,
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "independently_validated": self.independently_validated,
            "selection_propensity": self.selection_propensity,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ApproximateOracleReport:
    oracle_id: str
    snapshot_id: str
    case_id: str
    case_hash: str
    outcome_ids: tuple[str, ...]
    outcome_set_hash: str
    state: str
    oracle_route_id: str | None
    oracle_realized_total_loss: float | None
    observed_route_ids: tuple[str, ...]
    unknown_route_ids: tuple[str, ...]
    coverage_ratio: float
    directional_evidence_eligible: bool
    approximate_only: bool = True
    global_optimality_proven: bool = False
    external_call_count: int = 0
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = OFFLINE_REPLAY_SCHEMA_VERSION
    policy_revision: str = OFFLINE_REPLAY_POLICY_REVISION

    def __post_init__(self) -> None:
        if (
            self.schema_version != OFFLINE_REPLAY_SCHEMA_VERSION
            or self.policy_revision != OFFLINE_REPLAY_POLICY_REVISION
        ):
            raise OfflineReplayError("approximate oracle schema is invalid")
        if self.state not in {
            "subset_forbidden",
            "no_observed_routes",
            "no_accepted_route",
            "approximate_oracle",
        }:
            raise OfflineReplayError("approximate oracle state is invalid")
        if (self.state == "approximate_oracle") != (
            self.oracle_route_id is not None
            and self.oracle_realized_total_loss is not None
        ):
            raise OfflineReplayError("approximate oracle result is inconsistent")
        _sha256(self.case_hash, "case_hash")
        _sha256(self.outcome_set_hash, "outcome_set_hash")
        _unique(self.outcome_ids, "oracle_outcome_id")
        observed = _unique(self.observed_route_ids, "observed_route_id")
        unknown = _unique(self.unknown_route_ids, "unknown_route_id")
        if set(observed) & set(unknown):
            raise OfflineReplayError("observed and unknown routes overlap")
        _finite(self.coverage_ratio, "coverage_ratio", minimum=0.0, maximum=1.0)
        if not math.isclose(
            self.coverage_ratio,
            len(observed) / max(len(observed) + len(unknown), 1),
            abs_tol=1e-9,
        ):
            raise OfflineReplayError("oracle coverage ratio is inconsistent")
        if (
            not self.approximate_only
            or self.global_optimality_proven
            or self.external_call_count != 0
            or not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
            or self.commercial_claims_allowed
        ):
            raise OfflineReplayError("approximate oracle cannot claim authority")
        expected = "approxoracle_" + _digest(self.identity())[:32]
        if self.oracle_id != expected:
            raise OfflineReplayError("approximate oracle id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "case_id": self.case_id,
            "case_hash": self.case_hash,
            "outcome_ids": list(self.outcome_ids),
            "outcome_set_hash": self.outcome_set_hash,
            "state": self.state,
            "oracle_route_id": self.oracle_route_id,
            "oracle_realized_total_loss": self.oracle_realized_total_loss,
            "observed_route_ids": list(self.observed_route_ids),
            "unknown_route_ids": list(self.unknown_route_ids),
            "coverage_ratio": self.coverage_ratio,
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "approximate_only": self.approximate_only,
            "global_optimality_proven": self.global_optimality_proven,
            "external_call_count": self.external_call_count,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"oracle_id": self.oracle_id, **self.identity()}


def build_approximate_oracle(
    snapshot: ReplayVersionSnapshot,
    case: ReplayCase,
    outcomes: Sequence[ObservedRouteOutcome],
) -> ApproximateOracleReport:
    if case.snapshot_id != snapshot.snapshot_id:
        raise OfflineReplayError("oracle case does not match snapshot")
    items = tuple(sorted(outcomes, key=lambda row: row.outcome_id))
    outcome_ids = [item.outcome_id for item in items]
    if len(outcome_ids) != len(set(outcome_ids)):
        raise OfflineReplayError("route outcome ids must be unique")
    route_ids = {item.route_id for item in case.candidates}
    observed_routes: set[str] = set()
    for item in items:
        if item.case_id != case.case_id or item.route_id not in route_ids:
            raise OfflineReplayError("route outcome does not match replay case")
        if item.route_id in observed_routes:
            raise OfflineReplayError("one route cannot have duplicate outcomes")
        observed_routes.add(item.route_id)
    unknown_routes = route_ids - observed_routes
    accepted = tuple(item for item in items if item.accepted)
    if not case.multi_route_evaluation_allowed:
        state = "subset_forbidden"
        best = None
    elif not items:
        state = "no_observed_routes"
        best = None
    elif not accepted:
        state = "no_accepted_route"
        best = None
    else:
        state = "approximate_oracle"
        best = min(accepted, key=lambda row: (row.realized_total_loss, row.route_id))
    real_e2 = bool(items) and all(
        _evidence_rank(item.evidence_level) >= 2
        and item.data_origin in REAL_DATA_ORIGINS
        and item.independently_validated
        for item in items
    )
    directional = (
        state == "approximate_oracle"
        and real_e2
        and len(observed_routes) >= 2
        and len(accepted) >= 2
    )
    outcome_set_hash = _digest([item.to_dict() for item in items])
    coverage = len(observed_routes) / len(route_ids)
    identity = {
        "snapshot_id": snapshot.snapshot_id,
        "case_id": case.case_id,
        "case_hash": case.case_hash,
        "outcome_ids": outcome_ids,
        "outcome_set_hash": outcome_set_hash,
        "state": state,
        "oracle_route_id": None if best is None else best.route_id,
        "oracle_realized_total_loss": (
            None if best is None else best.realized_total_loss
        ),
        "observed_route_ids": sorted(observed_routes),
        "unknown_route_ids": sorted(unknown_routes),
        "coverage_ratio": round(coverage, 10),
        "directional_evidence_eligible": directional,
        "approximate_only": True,
        "global_optimality_proven": False,
        "external_call_count": 0,
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "commercial_claims_allowed": False,
        "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
        "policy_revision": OFFLINE_REPLAY_POLICY_REVISION,
    }
    return ApproximateOracleReport(
        oracle_id="approxoracle_" + _digest(identity)[:32],
        snapshot_id=snapshot.snapshot_id,
        case_id=case.case_id,
        case_hash=case.case_hash,
        outcome_ids=tuple(outcome_ids),
        outcome_set_hash=outcome_set_hash,
        state=state,
        oracle_route_id=None if best is None else best.route_id,
        oracle_realized_total_loss=None if best is None else best.realized_total_loss,
        observed_route_ids=tuple(sorted(observed_routes)),
        unknown_route_ids=tuple(sorted(unknown_routes)),
        coverage_ratio=round(coverage, 10),
        directional_evidence_eligible=directional,
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise OfflineReplayError("percentile requires values")
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


@dataclass(frozen=True, slots=True)
class ReplayBenchmarkReport:
    report_id: str
    snapshot_id: str
    case_set_hash: str
    policy_set_hash: str
    outcome_set_hash: str
    case_count: int
    policy_count: int
    replay_results: tuple[Mapping[str, Any], ...]
    oracle_results: tuple[Mapping[str, Any], ...]
    regret_results: tuple[Mapping[str, Any], ...]
    policy_summaries: Mapping[str, Mapping[str, Any]]
    bias_diagnostics: Mapping[str, Any]
    checks: Mapping[str, bool]
    state: str
    normalized_regret_threshold_assessment_allowed: bool
    external_call_count: int = 0
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    policy_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = OFFLINE_REPLAY_SCHEMA_VERSION
    policy_revision: str = OFFLINE_REPLAY_POLICY_REVISION

    def __post_init__(self) -> None:
        if (
            self.schema_version != OFFLINE_REPLAY_SCHEMA_VERSION
            or self.policy_revision != OFFLINE_REPLAY_POLICY_REVISION
        ):
            raise OfflineReplayError("replay benchmark schema is invalid")
        for value, name in (
            (self.case_set_hash, "case_set_hash"),
            (self.policy_set_hash, "policy_set_hash"),
            (self.outcome_set_hash, "outcome_set_hash"),
        ):
            _sha256(value, name)
        if not 1 <= self.case_count <= MAXIMUM_REPLAY_CASES:
            raise OfflineReplayError("benchmark case count is invalid")
        if not 2 <= self.policy_count <= MAXIMUM_BASELINES:
            raise OfflineReplayError("benchmark policy count is invalid")
        required_checks = {
            "real_e2_outcomes_only",
            "independent_validation",
            "minimum_threshold_cases",
            "all_selected_outcomes_observed",
            "all_cases_multi_route_allowed",
            "no_selected_route_only_bias",
            "selection_propensity_complete",
        }
        if set(self.checks) != required_checks:
            raise OfflineReplayError("benchmark checks are incomplete")
        if self.state not in {
            "blocked_evidence",
            "incomplete_counterfactuals",
            "directional_replay",
        }:
            raise OfflineReplayError("benchmark state is invalid")
        if self.normalized_regret_threshold_assessment_allowed != all(
            self.checks.values()
        ):
            raise OfflineReplayError("benchmark threshold gate is inconsistent")
        expected_state = (
            "blocked_evidence"
            if not self.checks["real_e2_outcomes_only"]
            or not self.checks["independent_validation"]
            else (
                "directional_replay"
                if self.normalized_regret_threshold_assessment_allowed
                else "incomplete_counterfactuals"
            )
        )
        if self.state != expected_state:
            raise OfflineReplayError("benchmark state contradicts its checks")
        if (
            self.external_call_count != 0
            or not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
            or self.policy_promotion_allowed
            or self.commercial_claims_allowed
        ):
            raise OfflineReplayError("offline benchmark cannot control production")
        expected = "replaybench_" + _digest(self.identity())[:32]
        if self.report_id != expected:
            raise OfflineReplayError("replay benchmark id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "case_set_hash": self.case_set_hash,
            "policy_set_hash": self.policy_set_hash,
            "outcome_set_hash": self.outcome_set_hash,
            "case_count": self.case_count,
            "policy_count": self.policy_count,
            "replay_results": list(self.replay_results),
            "oracle_results": list(self.oracle_results),
            "regret_results": list(self.regret_results),
            "policy_summaries": {
                key: dict(sorted(value.items()))
                for key, value in sorted(self.policy_summaries.items())
            },
            "bias_diagnostics": dict(sorted(self.bias_diagnostics.items())),
            "checks": dict(sorted(self.checks.items())),
            "state": self.state,
            "normalized_regret_threshold_assessment_allowed": (
                self.normalized_regret_threshold_assessment_allowed
            ),
            "external_call_count": self.external_call_count,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.identity()}


class OfflineReplayBenchmark:
    def __init__(self, engine: OfflineReplayEngine | None = None) -> None:
        self.engine = engine or OfflineReplayEngine()

    def evaluate(
        self,
        snapshot: ReplayVersionSnapshot,
        cases: Sequence[ReplayCase],
        policies: Sequence[ReplayPolicySpec],
        outcomes: Sequence[ObservedRouteOutcome],
    ) -> ReplayBenchmarkReport:
        case_items = tuple(sorted(cases, key=lambda row: row.case_id))
        policy_items = tuple(
            sorted(policies, key=lambda row: (row.policy_id, row.policy_version))
        )
        outcome_items = tuple(sorted(outcomes, key=lambda row: row.outcome_id))
        if not 1 <= len(case_items) <= MAXIMUM_REPLAY_CASES:
            raise OfflineReplayError("benchmark case count is out of range")
        if not 2 <= len(policy_items) <= MAXIMUM_BASELINES:
            raise OfflineReplayError("benchmark requires multiple baselines")
        if len({item.case_id for item in case_items}) != len(case_items):
            raise OfflineReplayError("benchmark replay cases must be unique")
        policy_keys = [f"{item.policy_id}@{item.policy_version}" for item in policy_items]
        if len(policy_keys) != len(set(policy_keys)):
            raise OfflineReplayError("benchmark policies must be unique")
        if any(item.snapshot_id != snapshot.snapshot_id for item in case_items):
            raise OfflineReplayError("benchmark cases do not share the snapshot")
        case_by_id = {item.case_id: item for item in case_items}
        outcomes_by_case: dict[str, list[ObservedRouteOutcome]] = {
            item.case_id: [] for item in case_items
        }
        for item in outcome_items:
            if item.case_id not in case_by_id:
                raise OfflineReplayError("benchmark outcome has an unknown case")
            outcomes_by_case[item.case_id].append(item)

        replay_results: list[dict[str, Any]] = []
        oracle_objects: dict[str, ApproximateOracleReport] = {}
        oracle_results: list[dict[str, Any]] = []
        regret_results: list[dict[str, Any]] = []
        normalized_by_policy: dict[str, list[float]] = {
            key: [] for key in policy_keys
        }
        selected_outcomes_observed = True
        selected_only_case_count = 0
        total_candidate_routes = 0
        observed_route_count = 0
        unknown_counterfactual_count = 0

        for case in case_items:
            case_outcomes = tuple(outcomes_by_case[case.case_id])
            outcome_by_route = {item.route_id: item for item in case_outcomes}
            if len(outcome_by_route) != len(case_outcomes):
                raise OfflineReplayError("benchmark contains duplicate route outcomes")
            oracle = build_approximate_oracle(snapshot, case, case_outcomes)
            oracle_objects[case.case_id] = oracle
            oracle_results.append(oracle.to_dict())
            total_candidate_routes += len(case.candidates)
            observed_route_count += len(oracle.observed_route_ids)
            unknown_counterfactual_count += len(oracle.unknown_route_ids)
            if set(oracle.observed_route_ids) == {case.recorded_selected_route_id}:
                selected_only_case_count += 1

            for policy, policy_key in zip(policy_items, policy_keys):
                replay = self.engine.replay(snapshot, case, policy)
                replay_results.append(replay.to_dict())
                selected_outcome = (
                    None
                    if replay.selected_route_id is None
                    else outcome_by_route.get(replay.selected_route_id)
                )
                selected_outcomes_observed = (
                    selected_outcomes_observed and selected_outcome is not None
                )
                state: str
                regret: float | None
                normalized: float | None
                evidence_eligible = False
                if replay.selected_route_id is None:
                    state = "no_feasible_route"
                    regret = None
                    normalized = None
                elif oracle.state != "approximate_oracle":
                    state = "oracle_unknown"
                    regret = None
                    normalized = None
                elif selected_outcome is None:
                    state = "selected_counterfactual_unknown"
                    regret = None
                    normalized = None
                elif (
                    not selected_outcome.accepted
                    and selected_outcome.realized_total_loss
                    < float(oracle.oracle_realized_total_loss)
                ):
                    state = "loss_semantics_inconsistent"
                    regret = None
                    normalized = None
                else:
                    state = "observed_regret"
                    regret = max(
                        0.0,
                        selected_outcome.realized_total_loss
                        - float(oracle.oracle_realized_total_loss),
                    )
                    normalized = regret / max(
                        abs(float(oracle.oracle_realized_total_loss)), 1e-12
                    )
                    evidence_eligible = (
                        oracle.directional_evidence_eligible
                        and _evidence_rank(selected_outcome.evidence_level) >= 2
                        and selected_outcome.data_origin in REAL_DATA_ORIGINS
                        and selected_outcome.independently_validated
                    )
                    if evidence_eligible:
                        normalized_by_policy[policy_key].append(normalized)
                regret_results.append(
                    {
                        "case_id": case.case_id,
                        "policy_key": policy_key,
                        "selected_route_id": replay.selected_route_id,
                        "oracle_route_id": oracle.oracle_route_id,
                        "state": state,
                        "routing_regret": None if regret is None else round(regret, 10),
                        "normalized_regret": (
                            None if normalized is None else round(normalized, 10)
                        ),
                        "counterfactual_known": selected_outcome is not None,
                        "directional_evidence_eligible": evidence_eligible,
                        "commercial_claims_allowed": False,
                    }
                )

        summaries: dict[str, dict[str, Any]] = {}
        for key in policy_keys:
            values = normalized_by_policy[key]
            median = statistics.median(values) if values else None
            p90 = _percentile(values, 0.90) if values else None
            summaries[key] = {
                "observed_directional_case_count": len(values),
                "median_normalized_regret": (
                    None if median is None else round(median, 10)
                ),
                "p90_normalized_regret": None if p90 is None else round(p90, 10),
                "median_threshold_passed": (
                    None
                    if median is None
                    else median <= MEDIAN_NORMALIZED_REGRET_MAXIMUM
                ),
                "p90_threshold_passed": (
                    None if p90 is None else p90 <= P90_NORMALIZED_REGRET_MAXIMUM
                ),
                "threshold_claim_allowed": False,
            }

        real_e2 = bool(outcome_items) and all(
            _evidence_rank(item.evidence_level) >= 2
            and item.data_origin in REAL_DATA_ORIGINS
            for item in outcome_items
        )
        independently_validated = bool(outcome_items) and all(
            item.independently_validated for item in outcome_items
        )
        propensity_complete = bool(outcome_items) and all(
            item.selection_propensity is not None for item in outcome_items
        )
        all_multi_route = all(
            item.multi_route_evaluation_allowed for item in case_items
        )
        no_selected_only_bias = selected_only_case_count == 0
        minimum_cases = len(case_items) >= MINIMUM_THRESHOLD_CASES and all(
            len(values) >= MINIMUM_THRESHOLD_CASES
            for values in normalized_by_policy.values()
        )
        checks = {
            "real_e2_outcomes_only": real_e2,
            "independent_validation": independently_validated,
            "minimum_threshold_cases": minimum_cases,
            "all_selected_outcomes_observed": selected_outcomes_observed,
            "all_cases_multi_route_allowed": all_multi_route,
            "no_selected_route_only_bias": no_selected_only_bias,
            "selection_propensity_complete": propensity_complete,
        }
        threshold_allowed = all(checks.values())
        if not real_e2 or not independently_validated:
            state = "blocked_evidence"
        elif not threshold_allowed:
            state = "incomplete_counterfactuals"
        else:
            state = "directional_replay"
        bias = {
            "total_candidate_routes": total_candidate_routes,
            "observed_route_count": observed_route_count,
            "unknown_counterfactual_count": unknown_counterfactual_count,
            "outcome_coverage_ratio": round(
                observed_route_count / max(total_candidate_routes, 1), 10
            ),
            "selected_route_only_case_count": selected_only_case_count,
            "selection_propensity_coverage_ratio": round(
                sum(item.selection_propensity is not None for item in outcome_items)
                / max(len(outcome_items), 1),
                10,
            ),
            "unobserved_counterfactuals_marked_unknown": True,
            "approximate_oracle_never_claims_global_optimality": True,
        }
        replay_results.sort(
            key=lambda row: (
                row["case_id"],
                row["policy_id"],
                row["policy_version"],
            )
        )
        oracle_results.sort(key=lambda row: row["case_id"])
        regret_results.sort(key=lambda row: (row["case_id"], row["policy_key"]))
        case_set_hash = _digest([item.to_dict() for item in case_items])
        policy_set_hash = _digest([item.to_dict() for item in policy_items])
        outcome_set_hash = _digest([item.to_dict() for item in outcome_items])
        identity = {
            "snapshot_id": snapshot.snapshot_id,
            "case_set_hash": case_set_hash,
            "policy_set_hash": policy_set_hash,
            "outcome_set_hash": outcome_set_hash,
            "case_count": len(case_items),
            "policy_count": len(policy_items),
            "replay_results": replay_results,
            "oracle_results": oracle_results,
            "regret_results": regret_results,
            "policy_summaries": {
                key: dict(sorted(value.items()))
                for key, value in sorted(summaries.items())
            },
            "bias_diagnostics": dict(sorted(bias.items())),
            "checks": dict(sorted(checks.items())),
            "state": state,
            "normalized_regret_threshold_assessment_allowed": threshold_allowed,
            "external_call_count": 0,
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "policy_promotion_allowed": False,
            "commercial_claims_allowed": False,
            "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
            "policy_revision": OFFLINE_REPLAY_POLICY_REVISION,
        }
        return ReplayBenchmarkReport(
            report_id="replaybench_" + _digest(identity)[:32],
            snapshot_id=snapshot.snapshot_id,
            case_set_hash=case_set_hash,
            policy_set_hash=policy_set_hash,
            outcome_set_hash=outcome_set_hash,
            case_count=len(case_items),
            policy_count=len(policy_items),
            replay_results=tuple(replay_results),
            oracle_results=tuple(oracle_results),
            regret_results=tuple(regret_results),
            policy_summaries=summaries,
            bias_diagnostics=bias,
            checks=checks,
            state=state,
            normalized_regret_threshold_assessment_allowed=threshold_allowed,
        )


def public_offline_replay_policy() -> dict[str, Any]:
    return {
        "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
        "policy_revision": OFFLINE_REPLAY_POLICY_REVISION,
        "historical_version_snapshot_required": True,
        "selection_rules": sorted(SELECTION_RULES),
        "multiple_baselines_required": True,
        "approximate_oracle": {
            "multi_route_allowed_subset_only": True,
            "high_risk_multi_route_execution_allowed": False,
            "unobserved_counterfactual_state": "unknown",
            "global_optimality_claim_allowed": False,
        },
        "normalized_regret_thresholds": {
            "minimum_directional_cases": MINIMUM_THRESHOLD_CASES,
            "median_maximum": MEDIAN_NORMALIZED_REGRET_MAXIMUM,
            "p90_maximum": P90_NORMALIZED_REGRET_MAXIMUM,
            "current_threshold_assessment_allowed": False,
        },
        "current_evidence": "E0-contract-only",
        "external_calls_required": False,
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "policy_promotion_allowed": False,
        "fixture_metrics_are_claimable": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "MEDIAN_NORMALIZED_REGRET_MAXIMUM",
    "MINIMUM_THRESHOLD_CASES",
    "OFFLINE_REPLAY_POLICY_REVISION",
    "OFFLINE_REPLAY_SCHEMA_VERSION",
    "P90_NORMALIZED_REGRET_MAXIMUM",
    "ApproximateOracleReport",
    "ObservedRouteOutcome",
    "OfflineReplayBenchmark",
    "OfflineReplayEngine",
    "OfflineReplayError",
    "OfflineReplayResult",
    "ReplayBenchmarkReport",
    "ReplayCase",
    "ReplayPolicySpec",
    "ReplayRouteCandidate",
    "ReplayVersionSnapshot",
    "build_approximate_oracle",
    "public_offline_replay_policy",
]
