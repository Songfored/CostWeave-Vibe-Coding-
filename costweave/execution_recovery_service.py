"""Offline advisory service for v0.6.4 execution-time recovery."""

from __future__ import annotations

from typing import Any, Mapping

from .intelligence.common import stable_digest
from .intelligence.execution_recovery import (
    EXECUTION_ACTIONS,
    EXECUTION_RECOVERY_POLICY_REVISION,
    EXECUTION_RECOVERY_SCHEMA_VERSION,
    EconomicRecoveryPlanner,
    ExecutionRecoveryDecisionV1,
    ExecutionSnapshotV1,
)
from .execution_recovery_store import ExecutionRecoveryStore
from .security_boundary import DEFAULT_DATA_RESIDENCY, normalize_project_id


class ExecutionRecoveryServiceError(ValueError):
    """Raised for malformed or unsafe execution-recovery requests."""


CONFIRM_TOKEN = "RUN_EXECUTION_RECOVERY"


class ExecutionRecoveryService:
    def __init__(self, store: ExecutionRecoveryStore, planner: EconomicRecoveryPlanner | None = None) -> None:
        self.store = store
        self.planner = planner or EconomicRecoveryPlanner()

    def status(self, *, project_id: str = "local-default") -> dict[str, Any]:
        project = normalize_project_id(project_id)
        return {
            "schema_version": EXECUTION_RECOVERY_SCHEMA_VERSION,
            "policy_version": EXECUTION_RECOVERY_POLICY_REVISION,
            "project_id": project,
            "data_residency": DEFAULT_DATA_RESIDENCY,
            "available_actions": list(EXECUTION_ACTIONS),
            "runtime_authority": "advisory-only",
            "production_route_intervention": False,
            "progress_belief": True,
            "economic_recovery": True,
            "pollution_scoping": True,
            "orthogonal_validator_awareness": True,
            "sunk_cost_excluded": True,
            "external_calls": 0,
            "provider_calls": 0,
            "network_calls": 0,
            "commercial_claims_allowed": False,
            "training_allowed": False,
            "counts": self.store.counts(project_id=project),
        }

    @staticmethod
    def _parse(payload: Mapping[str, Any]) -> ExecutionSnapshotV1:
        if not isinstance(payload, Mapping):
            raise ExecutionRecoveryServiceError("request must be a JSON object")
        if payload.get("confirm") != CONFIRM_TOKEN:
            raise ExecutionRecoveryServiceError(f"confirm must equal {CONFIRM_TOKEN}")
        unknown = set(payload) - {"confirm", "snapshot"}
        if unknown:
            raise ExecutionRecoveryServiceError(f"unknown execution recovery fields: {sorted(unknown)!r}")
        try:
            return ExecutionSnapshotV1.from_dict(payload.get("snapshot"))
        except (TypeError, ValueError, KeyError) as exc:
            raise ExecutionRecoveryServiceError(str(exc)) from exc

    def decide(self, *, project_id: str, idempotency_key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        snapshot = self._parse(payload)
        if snapshot.project_id != project:
            raise ExecutionRecoveryServiceError("snapshot project_id must match request project")
        request_hash = stable_digest(payload, length=64)
        existing = self.store.decision_for_idempotency(project_id=project, idempotency_key=idempotency_key, request_hash=request_hash)
        if existing is not None:
            return self._result(existing, replayed=True), True
        decision, belief = self.planner.decide(snapshot)
        bound_id = "recovery_decision_" + stable_digest({"decision": decision.to_dict(), "request_hash": request_hash}, length=32)
        decision = ExecutionRecoveryDecisionV1.from_dict({**decision.to_dict(), "decision_id": bound_id, "trace_hash": ""})
        self.store.record_belief(belief)
        inserted = self.store.record_decision(decision, idempotency_key=idempotency_key, request_hash=request_hash)
        return self._result(decision, replayed=not inserted), not inserted

    @staticmethod
    def _result(decision: ExecutionRecoveryDecisionV1, *, replayed: bool) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_RECOVERY_SCHEMA_VERSION,
            "project_id": decision.project_id,
            "decision": decision.to_dict(),
            "replayed": replayed,
            "advisory_only": True,
            "provider_calls": 0,
            "network_calls": 0,
            "commercial_claims_allowed": False,
        }

    def get(self, *, project_id: str, decision_id: str) -> dict[str, Any] | None:
        decision = self.store.decision(project_id=project_id, decision_id=decision_id)
        return None if decision is None else self._result(decision, replayed=False)


__all__ = ["CONFIRM_TOKEN", "EXECUTION_RECOVERY_SCHEMA_VERSION", "ExecutionRecoveryService", "ExecutionRecoveryServiceError"]
