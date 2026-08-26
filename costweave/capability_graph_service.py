"""Offline service boundary for the v0.6.2 calibrated capability router."""

from __future__ import annotations

from typing import Any, Mapping

from .capability_graph_store import CapabilityGraphStore, CapabilityGraphStoreError
from .contracts_v4 import utc_now
from .intelligence.capability_graph import (
    CAPABILITY_GRAPH_POLICY_REVISION,
    CAPABILITY_GRAPH_SCHEMA_VERSION,
    CapabilityEvidenceV1,
    CapabilityGraphRouter,
    CapabilityNodeV1,
    CalibratedRouteDecisionV1,
    RouteCandidateV1,
    RouteContextV1,
    detect_drift,
)
from .intelligence.common import stable_digest, stable_id
from .security_boundary import DEFAULT_DATA_RESIDENCY, normalize_project_id


class CapabilityGraphServiceError(ValueError):
    """Raised for invalid, unsafe or incomplete capability-router requests."""


CONFIRM_TOKEN = "RUN_CAPABILITY_ROUTER"


class CapabilityGraphService:
    def __init__(self, store: CapabilityGraphStore, router: CapabilityGraphRouter | None = None) -> None:
        self.store = store
        self.router = router or CapabilityGraphRouter()

    def status(self, *, project_id: str = "local-default") -> dict[str, Any]:
        project = normalize_project_id(project_id)
        evidence = self.store.evidence(project_id=project, limit=500)
        posteriors = self.store.posteriors(project_id=project, limit=100)
        levels = sorted({item.evidence_level for item in evidence})
        return {
            "schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION,
            "policy_version": CAPABILITY_GRAPH_POLICY_REVISION,
            "calibration_revision": "decayed-beta-conformal-v1",
            "project_id": project,
            "data_residency": DEFAULT_DATA_RESIDENCY,
            "node_types": ["TaskFamily", "ModelRevision", "ReasoningTier", "Tool", "Modality", "ProviderDeployment", "Validator"],
            "evidence_levels": levels,
            "calibration_status": "calibrated" if any(item.calibration_status == "calibrated" for item in posteriors) else "prior",
            "runtime_authority": "advisory-only",
            "router_intervention_enabled": False,
            "external_calls": 0,
            "provider_calls": 0,
            "network_calls": 0,
            "commercial_claims_allowed": False,
            "training_allowed": False,
            "counts": self.store.counts(project_id=project),
            "evidence_sample_count": sum(item.sample_count for item in evidence),
            "posteriors_with_real_evidence": sum(item.sample_count > 0 for item in posteriors),
        }

    @staticmethod
    def _require_confirm(payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise CapabilityGraphServiceError("request must be a JSON object")
        if payload.get("confirm") != CONFIRM_TOKEN:
            raise CapabilityGraphServiceError(f"confirm must equal {CONFIRM_TOKEN}")

    @staticmethod
    def _parse_evidence(payload: Mapping[str, Any]) -> CapabilityEvidenceV1:
        CapabilityGraphService._require_confirm(payload)
        allowed = {"confirm", "evidence"}
        unknown = set(payload) - allowed
        if unknown:
            raise CapabilityGraphServiceError(f"unknown capability evidence fields: {sorted(unknown)!r}")
        raw = payload.get("evidence")
        if not isinstance(raw, Mapping):
            raise CapabilityGraphServiceError("evidence must be a JSON object")
        try:
            return CapabilityEvidenceV1.from_dict(raw)
        except (TypeError, ValueError, KeyError) as exc:
            raise CapabilityGraphServiceError(str(exc)) from exc

    def ingest(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        evidence = self._parse_evidence(payload)
        if evidence.project_id != project:
            raise CapabilityGraphServiceError("evidence project_id must match request project")
        request_hash = stable_digest(payload, length=64)
        existing = None
        # The store's idempotency lookup is intentionally performed by the
        # write path; a duplicate insert is safe and returns inserted=False.
        inserted = self.store.record_evidence(evidence, idempotency_key=idempotency_key, request_hash=request_hash)
        for node in self._nodes_for_evidence(evidence):
            self.store.record_node(node, project_id=project)
        result = {
            "schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION,
            "project_id": project,
            "evidence": evidence.to_dict(),
            "inserted": inserted,
            "replayed": not inserted,
            "provider_calls": 0,
            "network_calls": 0,
            "commercial_claims_allowed": False,
        }
        return result, not inserted

    @staticmethod
    def _nodes_for_evidence(evidence: CapabilityEvidenceV1):
        yield from (
            CapabilityNodeV1(
                node_id=f"task:{evidence.task_family}", node_type="TaskFamily", label=evidence.task_family
            ),
            CapabilityNodeV1(
                node_id=f"model:{evidence.model_revision}", node_type="ModelRevision", label=evidence.model_revision
            ),
            CapabilityNodeV1(
                node_id=f"reasoning:{evidence.reasoning_tier}", node_type="ReasoningTier", label=evidence.reasoning_tier
            ),
            CapabilityNodeV1(
                node_id=f"deployment:{evidence.provider_deployment}", node_type="ProviderDeployment", label=evidence.provider_deployment
            ),
            CapabilityNodeV1(
                node_id=f"validator:{evidence.validator_id}", node_type="Validator", label=evidence.validator_id
            ),
        )
        for tool_id in evidence.tool_ids:
            yield CapabilityNodeV1(node_id=f"tool:{tool_id}", node_type="Tool", label=tool_id)
        for modality_id in evidence.modality_ids:
            yield CapabilityNodeV1(node_id=f"modality:{modality_id}", node_type="Modality", label=modality_id)

    @staticmethod
    def _parse_decision_payload(payload: Mapping[str, Any]) -> tuple[RouteContextV1, tuple[RouteCandidateV1, ...], str]:
        CapabilityGraphService._require_confirm(payload)
        allowed = {"confirm", "context", "candidates", "now", "calibration_set_id"}
        unknown = set(payload) - allowed
        if unknown:
            raise CapabilityGraphServiceError(f"unknown capability router fields: {sorted(unknown)!r}")
        try:
            context = RouteContextV1.from_dict(payload.get("context"))
        except (TypeError, ValueError, KeyError) as exc:
            raise CapabilityGraphServiceError(str(exc)) from exc
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, (list, tuple)):
            raise CapabilityGraphServiceError("candidates must be a list")
        try:
            candidates = tuple(RouteCandidateV1.from_dict(item) for item in raw_candidates)
        except (TypeError, ValueError, KeyError) as exc:
            raise CapabilityGraphServiceError(str(exc)) from exc
        now = payload.get("now", utc_now())
        if not isinstance(now, str):
            raise CapabilityGraphServiceError("now must be an ISO timestamp string")
        calibration_set_id = payload.get("calibration_set_id", "unavailable")
        if not isinstance(calibration_set_id, str):
            raise CapabilityGraphServiceError("calibration_set_id must be a string")
        return context, candidates, now

    def decide(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        context, candidates, now = self._parse_decision_payload(payload)
        if context.project_id != project:
            raise CapabilityGraphServiceError("context project_id must match request project")
        request_hash = stable_digest(payload, length=64)
        existing = self.store.decision_for_idempotency(project_id=project, idempotency_key=idempotency_key, request_hash=request_hash)
        if existing is not None:
            return {"schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION, "project_id": project, "decision": existing.to_dict(), "replayed": True, "provider_calls": 0, "network_calls": 0, "commercial_claims_allowed": False}, True
        decision_id = stable_id("capability_decision", {"project_id": project, "idempotency_key": idempotency_key, "request_hash": request_hash})
        evidence = self.store.evidence(project_id=project, limit=5000)
        decision, posteriors = self.router.decide(context=context, candidates=candidates, evidence=evidence, now=now, decision_id=decision_id, calibration_set_id=str(payload.get("calibration_set_id", "unavailable")))
        for posterior in posteriors:
            self.store.record_posterior(posterior)
        inserted = self.store.record_decision(decision, idempotency_key=idempotency_key, request_hash=request_hash)
        return {"schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION, "project_id": project, "decision": decision.to_dict(), "replayed": not inserted, "provider_calls": 0, "network_calls": 0, "commercial_claims_allowed": False}, not inserted

    def get(self, *, project_id: str, decision_id: str) -> dict[str, Any] | None:
        decision = self.store.decision(project_id=project_id, decision_id=decision_id)
        if decision is None:
            return None
        return {"schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION, "decision": decision.to_dict(), "provider_calls": 0, "network_calls": 0}

    def drift(self, *, baseline: Mapping[str, Any], current: Mapping[str, Any], now: str | None = None) -> dict[str, Any]:
        report = detect_drift(baseline=baseline, current=current, now=now or utc_now())
        return {"schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION, "policy_version": CAPABILITY_GRAPH_POLICY_REVISION, "report": report.to_dict(), "strategy_promotion_frozen": report.freeze_candidate, "commercial_claims_allowed": False}


__all__ = ["CAPABILITY_GRAPH_SCHEMA_VERSION", "CONFIRM_TOKEN", "CapabilityGraphService", "CapabilityGraphServiceError"]
