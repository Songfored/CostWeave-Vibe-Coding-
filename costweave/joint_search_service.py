"""Offline advisory service for v0.6.3 joint strategy search."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts_v4 import utc_now
from .intelligence.common import stable_digest
from .intelligence.joint_search import (
    JOINT_SEARCH_POLICY_REVISION,
    JOINT_SEARCH_SCHEMA_VERSION,
    JOINT_SEARCHERS,
    JointSearchDecisionV1,
    JointSearchProblemV1,
    JointStrategySearcher,
)
from .joint_search_store import JointSearchStore
from .security_boundary import DEFAULT_DATA_RESIDENCY, normalize_project_id


class JointSearchServiceError(ValueError):
    """Raised for invalid or unsafe joint-search requests."""


CONFIRM_TOKEN = "RUN_JOINT_SEARCH"


class JointSearchService:
    def __init__(self, store: JointSearchStore, searcher: JointStrategySearcher | None = None) -> None:
        self.store = store
        self.searcher = searcher or JointStrategySearcher()

    def status(self, *, project_id: str = "local-default") -> dict[str, Any]:
        project = normalize_project_id(project_id)
        return {
            "schema_version": JOINT_SEARCH_SCHEMA_VERSION,
            "policy_version": JOINT_SEARCH_POLICY_REVISION,
            "project_id": project,
            "data_residency": DEFAULT_DATA_RESIDENCY,
            "available_searchers": list(JOINT_SEARCHERS),
            "runtime_authority": "advisory-only",
            "production_route_intervention": False,
            "anytime_enabled": True,
            "prompt_handoff_compiler": True,
            "external_calls": 0,
            "provider_calls": 0,
            "network_calls": 0,
            "commercial_claims_allowed": False,
            "training_allowed": False,
            "counts": self.store.counts(project_id=project),
        }

    @staticmethod
    def _require_confirm(payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise JointSearchServiceError("request must be a JSON object")
        if payload.get("confirm") != CONFIRM_TOKEN:
            raise JointSearchServiceError(f"confirm must equal {CONFIRM_TOKEN}")

    @staticmethod
    def _parse(payload: Mapping[str, Any]) -> tuple[JointSearchProblemV1, str]:
        JointSearchService._require_confirm(payload)
        allowed = {"confirm", "problem", "searcher"}
        unknown = set(payload) - allowed
        if unknown:
            raise JointSearchServiceError(f"unknown joint search fields: {sorted(unknown)!r}")
        try:
            problem = JointSearchProblemV1.from_dict(payload.get("problem"))
        except (TypeError, ValueError, KeyError) as exc:
            raise JointSearchServiceError(str(exc)) from exc
        searcher = payload.get("searcher", "beam")
        if searcher not in JOINT_SEARCHERS:
            raise JointSearchServiceError(f"unsupported searcher: {searcher!r}")
        return problem, searcher

    def decide(self, *, project_id: str, idempotency_key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        problem, searcher = self._parse(payload)
        if problem.project_id != project:
            raise JointSearchServiceError("problem project_id must match request project")
        request_hash = stable_digest(payload, length=64)
        existing = self.store.decision_for_idempotency(project_id=project, idempotency_key=idempotency_key, request_hash=request_hash)
        if existing is not None:
            return self._result(existing, replayed=True), True
        decision, candidates = self.searcher.search(problem, searcher=searcher)
        # The engine's deterministic id is based on the evaluated candidates;
        # bind it to the request identity before persistence to prevent a
        # cross-request replay from being mistaken for the same decision.
        bound_id = f"joint_decision_{stable_digest({'decision': decision.to_dict(), 'request_hash': request_hash}, length=32)}"
        decision = JointSearchDecisionV1.from_dict({**decision.to_dict(), "decision_id": bound_id, "trace_hash": ""})
        for candidate in candidates:
            self.store.record_candidate(candidate, project_id=project)
        inserted = self.store.record_decision(decision, idempotency_key=idempotency_key, request_hash=request_hash)
        return self._result(decision, replayed=not inserted), not inserted

    @staticmethod
    def _result(decision: JointSearchDecisionV1, *, replayed: bool) -> dict[str, Any]:
        return {
            "schema_version": JOINT_SEARCH_SCHEMA_VERSION,
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


__all__ = ["CONFIRM_TOKEN", "JOINT_SEARCH_SCHEMA_VERSION", "JointSearchService", "JointSearchServiceError"]
