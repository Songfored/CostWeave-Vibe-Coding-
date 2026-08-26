"""Advisory service facade for CostWeave v0.6.5 safe learning."""

from __future__ import annotations

from typing import Any, Mapping

from .intelligence.common import CREDIT_COMPONENTS, EVIDENCE_LEVELS, stable_digest
from .intelligence.safe_learning import (
    SAFE_LEARNING_CONFIRM_TOKEN,
    SAFE_LEARNING_POLICY_REVISION,
    SAFE_LEARNING_SCHEMA_VERSION,
    CounterfactualReplayV1,
    CreditAssignmentV1,
    LearningEstimateV1,
    LoggedDecisionV1,
    PromotionAssessmentV1,
    SafeLearningCandidateV1,
    TraceCreditInputV1,
    assign_credit,
    counterfactual_replay,
    estimate_policy,
)
from .safe_learning_store import SafeLearningStore, SafeLearningStoreError
from .security_boundary import DEFAULT_DATA_RESIDENCY, normalize_project_id


class SafeLearningServiceError(ValueError):
    """Raised for malformed or unsafe safe-learning requests."""


CONFIRM_TOKEN = SAFE_LEARNING_CONFIRM_TOKEN


class SafeLearningService:
    def __init__(self, store: SafeLearningStore) -> None:
        self.store = store

    def status(self, *, project_id: str = "local-default") -> dict[str, Any]:
        project = normalize_project_id(project_id)
        return {
            "schema_version": SAFE_LEARNING_SCHEMA_VERSION,
            "policy_version": SAFE_LEARNING_POLICY_REVISION,
            "project_id": project,
            "data_residency": DEFAULT_DATA_RESIDENCY,
            "components": list(CREDIT_COMPONENTS),
            "credit_methods": ["deterministic", "counterfactual_replay", "hierarchical_estimate", "ips", "dr"],
            "learning_stages": ["offline", "shadow", "canary"],
            "runtime_authority": "advisory-only",
            "training_enabled": False,
            "route_intervention": False,
            "automatic_promotion_allowed": False,
            "commercial_claims_allowed": False,
            "provider_calls": 0,
            "network_calls": 0,
            "external_calls": 0,
            "evidence_levels": list(EVIDENCE_LEVELS),
            "counts": self.store.counts(project_id=project),
        }

    @staticmethod
    def _confirm(payload: Mapping[str, Any], allowed: set[str]) -> None:
        if not isinstance(payload, Mapping):
            raise SafeLearningServiceError("request must be a JSON object")
        if payload.get("confirm") != CONFIRM_TOKEN:
            raise SafeLearningServiceError(f"confirm must equal {CONFIRM_TOKEN}")
        unknown = set(payload) - allowed
        if unknown:
            raise SafeLearningServiceError(f"unknown safe-learning fields: {sorted(unknown)!r}")

    @staticmethod
    def _trace(payload: Mapping[str, Any]) -> TraceCreditInputV1:
        try:
            return TraceCreditInputV1.from_dict(payload["trace"])
        except (TypeError, ValueError, KeyError) as exc:
            raise SafeLearningServiceError(str(exc)) from exc

    def credit(self, *, project_id: str, idempotency_key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        self._confirm(payload, {"confirm", "trace", "method"})
        trace = self._trace(payload)
        if trace.project_id != project:
            raise SafeLearningServiceError("trace project_id must match request project")
        method = payload.get("method", "deterministic")
        if method == "llm_attribution":
            raise SafeLearningServiceError("llm_attribution is evidence only and cannot be used by safe-learning")
        try:
            credit = assign_credit(trace, method=method)
        except (TypeError, ValueError) as exc:
            raise SafeLearningServiceError(str(exc)) from exc
        request_hash = stable_digest(payload, length=64)
        try:
            existing = self.store.credit_for_idempotency(project_id=project, idempotency_key=idempotency_key, request_hash=request_hash)
            if existing is not None:
                return self._credit_result(existing, replayed=True), True
            inserted = self.store.record_credit(credit, idempotency_key=idempotency_key, request_hash=request_hash)
        except Exception as exc:
            if isinstance(exc, SafeLearningServiceError):
                raise
            raise
        return self._credit_result(credit, replayed=not inserted), not inserted

    @staticmethod
    def _credit_result(credit: CreditAssignmentV1, *, replayed: bool) -> dict[str, Any]:
        return {
            "schema_version": SAFE_LEARNING_SCHEMA_VERSION,
            "policy_revision": SAFE_LEARNING_POLICY_REVISION,
            "credit": credit.to_dict(), "replayed": replayed,
            "training_enabled": False, "advisory_only": True,
            "route_intervention": False, "provider_calls": 0,
            "network_calls": 0, "commercial_claims_allowed": False,
        }

    def replay(self, *, project_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        self._confirm(payload, {"confirm", "trace", "replaced_component", "alternative_effect", "assumptions"})
        trace = self._trace(payload)
        if trace.project_id != project:
            raise SafeLearningServiceError("trace project_id must match request project")
        try:
            replay = counterfactual_replay(
                trace, payload["replaced_component"], payload["alternative_effect"],
                assumptions=tuple(payload.get("assumptions", ())),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise SafeLearningServiceError(str(exc)) from exc
        self.store.record_replay(replay)
        return {
            "schema_version": SAFE_LEARNING_SCHEMA_VERSION,
            "replay": replay.to_dict(), "advisory_only": True,
            "route_intervention": False, "provider_calls": 0, "network_calls": 0,
        }

    @staticmethod
    def _observations(payload: Mapping[str, Any], project: str) -> tuple[LoggedDecisionV1, ...]:
        raw = payload.get("observations")
        if not isinstance(raw, (list, tuple)) or not raw:
            raise SafeLearningServiceError("observations must be a non-empty list")
        result: list[LoggedDecisionV1] = []
        for value in raw:
            try:
                item = LoggedDecisionV1.from_dict(value)
            except (TypeError, ValueError, KeyError) as exc:
                raise SafeLearningServiceError(str(exc)) from exc
            if item.project_id != project:
                raise SafeLearningServiceError("all observations must match request project")
            result.append(item)
        return tuple(result)

    def evaluate(self, *, project_id: str, idempotency_key: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        allowed = {
            "confirm", "policy_id", "policy_version", "task_family", "method",
            "observations", "candidate_policy_id", "candidate_policy_version",
            "champion_policy_id", "champion_policy_version", "champion_estimate",
            "rollback_policy_id", "stage",
        }
        self._confirm(payload, allowed)
        request_hash = stable_digest(payload, length=64)
        try:
            existing = self.store.evaluation_for_idempotency(project_id=project, idempotency_key=idempotency_key, request_hash=request_hash)
        except SafeLearningStoreError:
            raise
        except Exception as exc:
            raise SafeLearningServiceError(str(exc)) from exc
        if existing is not None:
            existing_assessment, existing_estimate, existing_candidate = existing
            return self._assessment_result(
                existing_assessment,
                estimate=existing_estimate,
                candidate=existing_candidate,
                replayed=True,
            ), True
        observations = self._observations(payload, project)
        try:
            estimate = estimate_policy(
                observations, policy_id=payload["policy_id"], policy_version=payload["policy_version"],
                task_family=payload["task_family"], method=payload.get("method", "dr"),
            )
            candidate = SafeLearningCandidateV1.create(
                project_id=project, candidate_policy_id=payload.get("candidate_policy_id", payload["policy_id"]),
                candidate_policy_version=payload.get("candidate_policy_version", payload["policy_version"]),
                champion_policy_id=payload["champion_policy_id"],
                champion_policy_version=payload["champion_policy_version"],
                task_family=payload["task_family"], estimate=estimate,
                champion_estimate=payload["champion_estimate"],
                rollback_policy_id=payload["rollback_policy_id"],
            )
            assessment = PromotionAssessmentV1.assess_candidate(
                candidate, estimate, stage=payload.get("stage", "shadow"),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise SafeLearningServiceError(str(exc)) from exc
        try:
            inserted = self.store.record_evaluation(
                estimate,
                candidate,
                assessment,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except SafeLearningStoreError:
            raise
        except Exception as exc:
            raise SafeLearningServiceError(str(exc)) from exc
        if not inserted:
            existing = self.store.evaluation_for_idempotency(
                project_id=project,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if existing is None:
                raise SafeLearningServiceError("idempotent evaluation disappeared after commit")
            existing_assessment, existing_estimate, existing_candidate = existing
            return self._assessment_result(
                existing_assessment,
                estimate=existing_estimate,
                candidate=existing_candidate,
                replayed=True,
            ), True
        return self._assessment_result(assessment, estimate=estimate, candidate=candidate, replayed=False), False

    @staticmethod
    def _assessment_result(assessment: PromotionAssessmentV1, *, estimate: LearningEstimateV1 | None = None, candidate: SafeLearningCandidateV1 | None = None, replayed: bool) -> dict[str, Any]:
        return {
            "schema_version": SAFE_LEARNING_SCHEMA_VERSION,
            "policy_revision": SAFE_LEARNING_POLICY_REVISION,
            "assessment": assessment.to_dict(),
            "estimate": None if estimate is None else estimate.to_dict(),
            "candidate": None if candidate is None else candidate.to_dict(),
            "replayed": replayed, "training_enabled": False,
            "advisory_only": True, "route_intervention": False,
            "provider_calls": 0, "network_calls": 0,
            "commercial_claims_allowed": False,
        }

    def get(self, *, project_id: str, record_id: str) -> dict[str, Any] | None:
        found = self.store.get(project_id=project_id, object_id=record_id)
        if found is None:
            return None
        kind, item = found
        return {
            "schema_version": SAFE_LEARNING_SCHEMA_VERSION,
            "policy_revision": SAFE_LEARNING_POLICY_REVISION,
            "kind": kind, "record": item.to_dict(), "advisory_only": True,
            "route_intervention": False, "training_enabled": False,
        }


__all__ = ["CONFIRM_TOKEN", "SafeLearningService", "SafeLearningServiceError"]
