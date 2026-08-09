"""Offline service boundary for the v0.6.1 Rational MetaController."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .contracts_v4 import utc_now
from .intelligence.meta_controller import (
    BudgetAmountV1,
    MetaControllerEngine,
    MetaDecisionV1,
    PlannerCommitteeV1,
    PlannerMemberV1,
)
from .intelligence.common import stable_digest
from .intelligence.beliefs import SystemBeliefV1, TaskBeliefV1
from .intelligence.strategy import StrategyCandidateV1
from .meta_controller_store import MetaControllerStore, MetaControllerStoreError
from .security_boundary import normalize_project_id


class MetaControllerServiceError(ValueError):
    """Raised for invalid or unsafe MetaController service requests."""


CONFIRM_TOKEN = "RUN_META_CONTROLLER"


class RationalMetaControllerService:
    def __init__(self, store: MetaControllerStore, engine: MetaControllerEngine | None = None) -> None:
        self.store = store
        self.engine = engine or MetaControllerEngine()

    def status(self, *, project_id: str = "local-default") -> dict[str, Any]:
        project = normalize_project_id(project_id)
        return {
            "schema_version": "v0.6.1",
            "policy_id": "rational-meta-controller",
            "policy_version": "rational-meta-controller-v1",
            "project_id": project,
            "actions": ["fast", "deliberate", "probe", "expert"],
            "management_depth_limit": 2,
            "external_calls": 0,
            "provider_calls": 0,
            "network_calls": 0,
            "runtime_authority": "advisory-only",
            "counts": self.store.counts(),
        }

    @staticmethod
    def _parse_request(payload: Mapping[str, Any]) -> tuple[TaskBeliefV1, SystemBeliefV1, tuple[StrategyCandidateV1, ...], tuple[PlannerMemberV1, ...], BudgetAmountV1 | None, str | None]:
        if not isinstance(payload, Mapping):
            raise MetaControllerServiceError("request must be a JSON object")
        allowed = {"confirm", "task_belief", "system_belief", "candidates", "planner_members", "available_budget", "now"}
        unknown = set(payload) - allowed
        if unknown:
            raise MetaControllerServiceError(f"unknown MetaController fields: {sorted(unknown)!r}")
        if payload.get("confirm") != CONFIRM_TOKEN:
            raise MetaControllerServiceError(f"confirm must equal {CONFIRM_TOKEN}")
        task = TaskBeliefV1.from_dict(payload.get("task_belief"))
        system = SystemBeliefV1.from_dict(payload.get("system_belief"))
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)):
            raise MetaControllerServiceError("candidates must be a list")
        candidates = tuple(StrategyCandidateV1.from_dict(item) for item in raw_candidates)
        raw_members = payload.get("planner_members", [])
        if not isinstance(raw_members, (list, tuple)):
            raise MetaControllerServiceError("planner_members must be a list")
        members = tuple(PlannerMemberV1.from_dict(item) for item in raw_members)
        available = payload.get("available_budget")
        budget = BudgetAmountV1.from_dict(available) if available is not None else None
        now = payload.get("now")
        if now is not None and not isinstance(now, str):
            raise MetaControllerServiceError("now must be an ISO timestamp string")
        return task, system, candidates, members, budget, now

    def decide(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key.strip()) <= 200:
            raise MetaControllerServiceError("Idempotency-Key must contain 8-200 characters")
        task, system, candidates, members, budget, now = self._parse_request(payload)
        key = idempotency_key.strip()
        request_hash = stable_digest(payload, length=64)
        existing = self.store.decision_for_idempotency(
            project_id=project,
            idempotency_key=key,
            request_hash=request_hash,
        )
        if existing is not None:
            return {
                "schema_version": "v0.6.1",
                "project_id": project,
                "idempotency_key": key,
                "decision": existing.to_dict(),
                "committee": None,
                "replayed": True,
                "provider_calls": 0,
                "network_calls": 0,
                "commercial_claims_allowed": False,
            }, True
        decision, committee = self.engine.decide(
            project_id=project,
            task_belief=task,
            system_belief=system,
            candidates=candidates,
            planner_members=members,
            available_budget=budget,
            now=now,
        )
        created = self.store.record_decision(
            decision,
            idempotency_key=key,
            request_hash=request_hash,
        )
        result = {
            "schema_version": "v0.6.1",
            "project_id": project,
            "idempotency_key": key,
            "decision": decision.to_dict(),
            "committee": committee.to_dict() if isinstance(committee, PlannerCommitteeV1) else None,
            "replayed": not created,
            "provider_calls": 0,
            "network_calls": 0,
            "commercial_claims_allowed": False,
        }
        return result, not created

    def get(self, *, project_id: str, decision_id: str) -> dict[str, Any] | None:
        decision = self.store.decision(project_id=project_id, decision_id=decision_id)
        if decision is None:
            return None
        return {"schema_version": "v0.6.1", "decision": decision.to_dict(), "provider_calls": 0, "network_calls": 0}


__all__ = ["CONFIRM_TOKEN", "MetaControllerServiceError", "RationalMetaControllerService"]
