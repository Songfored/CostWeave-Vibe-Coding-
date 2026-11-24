"""Governed outcome datasets for CostWeave v0.5.6.2.

This module turns immutable evaluation observations into privacy-preserving
learning copies.  It deliberately does not train or promote a routing policy.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .evaluation_protocol import canonical_json


OUTCOME_DATASET_SCHEMA_VERSION = "v0.5.6.2"
LABEL_SCHEMA_VERSION = "outcome-label-v1"
FEATURE_SCHEMA_VERSION = "outcome-features-v1"
DATASET_BUILDER_REVISION = "outcome-dataset-builder-0.5.6.2"

LABEL_ACCEPTED = "accepted"
LABEL_REJECTED = "rejected"
LABEL_ABSTAINED = "abstained"
LABEL_VERDICTS = (LABEL_ACCEPTED, LABEL_REJECTED, LABEL_ABSTAINED)

LABEL_SOURCE_AUTOMATED_HARD = "automated_hard_validation"
LABEL_SOURCE_INDEPENDENT_MODEL = "independent_model_validation"
LABEL_SOURCE_HUMAN_BLIND = "human_blind_review"
LABEL_SOURCE_USER = "user_feedback"
LABEL_SOURCE_SYSTEM = "system_terminal_outcome"
LABEL_SOURCES = (
    LABEL_SOURCE_AUTOMATED_HARD,
    LABEL_SOURCE_INDEPENDENT_MODEL,
    LABEL_SOURCE_HUMAN_BLIND,
    LABEL_SOURCE_USER,
    LABEL_SOURCE_SYSTEM,
)

PARTITIONS = ("train", "calibration", "test")
PURPOSES = ("evaluation", "calibration", "offline_training")
SAMPLE_TIER_SEED = "seed_validation"
SAMPLE_TIER_INTERNAL = "internal_validation"
SAMPLE_TIER_FORMAL = "formal_benchmark"

SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|private[_-]?key)\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{12,}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b1[3-9]\d{9}\b"),
)


class OutcomeGovernanceError(RuntimeError):
    """Raised when evidence cannot safely enter a governed dataset."""


class TrainingFrozenError(OutcomeGovernanceError):
    """Raised when a consumer attempts to train from frozen evidence."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _hmac_identifier(secret: bytes, namespace: str, value: str) -> str:
    digest = hmac.new(
        secret,
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{namespace}_{digest}"


def _safe_evidence_refs(
    secret: bytes, refs: Iterable[str]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                _hmac_identifier(secret, "evidence", str(ref))
                for ref in refs
                if str(ref)
            }
        )
    )


@dataclass(frozen=True, slots=True)
class GovernanceConfig:
    privacy_salt: str
    purpose: str = "evaluation"
    ttl_days: int = 365
    created_at: str = ""
    allow_simulated_training: bool = False

    def __post_init__(self) -> None:
        if len(self.privacy_salt.encode("utf-8")) < 16:
            raise OutcomeGovernanceError(
                "privacy salt must contain at least 16 bytes"
            )
        if self.purpose not in PURPOSES:
            raise OutcomeGovernanceError(
                f"unsupported dataset purpose: {self.purpose}"
            )
        if not 1 <= self.ttl_days <= 3650:
            raise OutcomeGovernanceError("ttl_days must be between 1 and 3650")
        if self.created_at:
            _parse_time(self.created_at)

    @property
    def timestamp(self) -> str:
        return self.created_at or _utc_now()

    @property
    def secret(self) -> bytes:
        return self.privacy_salt.encode("utf-8")


@dataclass(frozen=True, slots=True)
class LabelEvidence:
    label_event_id: str
    sample_id: str
    source: str
    verdict: str
    independent: bool
    blinded: bool
    delayed: bool
    evidence_level: str
    evaluator_revision: str
    evidence_refs: tuple[str, ...]
    recorded_at: str
    schema_version: str = LABEL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.source not in LABEL_SOURCES:
            raise OutcomeGovernanceError(
                f"unsupported label source: {self.source}"
            )
        if self.verdict not in LABEL_VERDICTS:
            raise OutcomeGovernanceError(
                f"unsupported label verdict: {self.verdict}"
            )
        if not self.label_event_id.startswith("label_"):
            raise OutcomeGovernanceError("label event id is invalid")
        if not self.sample_id.startswith("sample_"):
            raise OutcomeGovernanceError("sample id is invalid")
        if not self.evidence_level or not self.evaluator_revision:
            raise OutcomeGovernanceError(
                "label evidence and evaluator revision are required"
            )
        _parse_time(self.recorded_at)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    @classmethod
    def create(
        cls,
        *,
        sample_id: str,
        source: str,
        verdict: str,
        independent: bool,
        blinded: bool,
        delayed: bool,
        evidence_level: str,
        evaluator_revision: str,
        evidence_refs: Sequence[str],
        recorded_at: str,
    ) -> "LabelEvidence":
        identity = {
            "sample_id": sample_id,
            "source": source,
            "verdict": verdict,
            "independent": independent,
            "blinded": blinded,
            "delayed": delayed,
            "evidence_level": evidence_level,
            "evaluator_revision": evaluator_revision,
            "evidence_refs": sorted(set(evidence_refs)),
            "recorded_at": recorded_at,
        }
        event_id = "label_" + _digest(identity)[:32]
        return cls(
            label_event_id=event_id,
            sample_id=sample_id,
            source=source,
            verdict=verdict,
            independent=independent,
            blinded=blinded,
            delayed=delayed,
            evidence_level=evidence_level,
            evaluator_revision=evaluator_revision,
            evidence_refs=tuple(sorted(set(evidence_refs))),
            recorded_at=recorded_at,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LabelEvidence":
        return cls(
            label_event_id=str(value["label_event_id"]),
            sample_id=str(value["sample_id"]),
            source=str(value["source"]),
            verdict=str(value["verdict"]),
            independent=bool(value["independent"]),
            blinded=bool(value["blinded"]),
            delayed=bool(value["delayed"]),
            evidence_level=str(value["evidence_level"]),
            evaluator_revision=str(value["evaluator_revision"]),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            recorded_at=str(value["recorded_at"]),
            schema_version=str(
                value.get("schema_version", LABEL_SCHEMA_VERSION)
            ),
        )


def resolve_labels(events: Sequence[LabelEvidence]) -> dict[str, Any]:
    """Resolve without discarding disagreement or overwriting earlier events."""

    accepted = [item for item in events if item.verdict == LABEL_ACCEPTED]
    rejected = [item for item in events if item.verdict == LABEL_REJECTED]
    independent_positive = [item for item in accepted if item.independent]
    if accepted and rejected:
        state = "conflict"
        success: bool | None = None
    elif independent_positive:
        state = "success"
        success = True
    elif rejected:
        state = "failure"
        success = False
    else:
        state = "unlabelled"
        success = None
    return {
        "state": state,
        "success": success,
        "independent_success_evidence": bool(independent_positive),
        "event_count": len(events),
        "positive_sources": sorted({item.source for item in accepted}),
        "negative_sources": sorted({item.source for item in rejected}),
        "conflict": bool(accepted and rejected),
    }


@dataclass(frozen=True, slots=True)
class GovernedOutcome:
    sample_id: str
    source_observation_hash: str
    anonymous_task_id: str
    anonymous_project_id: str
    anonymous_user_id: str
    partition: str
    collected_at: str
    source_kind: str
    evidence_level: str
    task_features: Mapping[str, Any]
    contract_summary: Mapping[str, Any]
    candidate_routes: Mapping[str, Any]
    selected_route: Mapping[str, Any] | None
    actuals: Mapping[str, Any]
    execution_trace: Mapping[str, Any]
    failure: Mapping[str, Any] | None
    version_bindings: Mapping[str, Any]
    lineage: Mapping[str, Any]
    dedupe_group: str
    sample_weight: float
    purpose: str
    expires_at: str
    redaction: Mapping[str, Any]
    label_events: tuple[LabelEvidence, ...]
    resolved_label: Mapping[str, Any]
    schema_version: str = OUTCOME_DATASET_SCHEMA_VERSION
    feature_schema_version: str = FEATURE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.partition not in PARTITIONS:
            raise OutcomeGovernanceError(
                f"unsupported dataset partition: {self.partition}"
            )
        if not 0 < self.sample_weight <= 1:
            raise OutcomeGovernanceError("sample weight must be in (0,1]")
        if self.purpose not in PURPOSES:
            raise OutcomeGovernanceError("sample purpose is invalid")
        _parse_time(self.collected_at)
        _parse_time(self.expires_at)
        expected = resolve_labels(self.label_events)
        if canonical_json(expected) != canonical_json(self.resolved_label):
            raise OutcomeGovernanceError("resolved label is not reproducible")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["label_events"] = [
            item.to_dict() for item in self.label_events
        ]
        return result

    def append_label(self, event: LabelEvidence) -> "GovernedOutcome":
        if event.sample_id != self.sample_id:
            raise OutcomeGovernanceError(
                "label event belongs to another sample"
            )
        existing = {
            item.label_event_id: canonical_json(item.to_dict())
            for item in self.label_events
        }
        raw = canonical_json(event.to_dict())
        if event.label_event_id in existing:
            if existing[event.label_event_id] != raw:
                raise OutcomeGovernanceError(
                    "label event id was reused for different evidence"
                )
            return self
        events = tuple(
            sorted(
                (*self.label_events, event),
                key=lambda item: (item.recorded_at, item.label_event_id),
            )
        )
        return replace(
            self,
            label_events=events,
            resolved_label=resolve_labels(events),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GovernedOutcome":
        return cls(
            sample_id=str(value["sample_id"]),
            source_observation_hash=str(value["source_observation_hash"]),
            anonymous_task_id=str(value["anonymous_task_id"]),
            anonymous_project_id=str(value["anonymous_project_id"]),
            anonymous_user_id=str(value["anonymous_user_id"]),
            partition=str(value["partition"]),
            collected_at=str(value["collected_at"]),
            source_kind=str(value["source_kind"]),
            evidence_level=str(value["evidence_level"]),
            task_features=dict(value["task_features"]),
            contract_summary=dict(value["contract_summary"]),
            candidate_routes=dict(value["candidate_routes"]),
            selected_route=(
                dict(value["selected_route"])
                if value.get("selected_route")
                else None
            ),
            actuals=dict(value["actuals"]),
            execution_trace=dict(value["execution_trace"]),
            failure=(
                dict(value["failure"]) if value.get("failure") else None
            ),
            version_bindings=dict(value["version_bindings"]),
            lineage=dict(value["lineage"]),
            dedupe_group=str(value["dedupe_group"]),
            sample_weight=float(value["sample_weight"]),
            purpose=str(value["purpose"]),
            expires_at=str(value["expires_at"]),
            redaction=dict(value["redaction"]),
            label_events=tuple(
                LabelEvidence.from_dict(item)
                for item in value.get("label_events", ())
            ),
            resolved_label=dict(value["resolved_label"]),
            schema_version=str(
                value.get(
                    "schema_version", OUTCOME_DATASET_SCHEMA_VERSION
                )
            ),
            feature_schema_version=str(
                value.get(
                    "feature_schema_version", FEATURE_SCHEMA_VERSION
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class OutcomeDataset:
    dataset_id: str
    source_report_id: str
    source_report_hash: str
    created_at: str
    purpose: str
    samples: tuple[GovernedOutcome, ...]
    quality_report: Mapping[str, Any]
    content_hash: str
    builder_revision: str = DATASET_BUILDER_REVISION
    schema_version: str = OUTCOME_DATASET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.dataset_id.startswith("dataset_"):
            raise OutcomeGovernanceError("dataset id is invalid")
        ids = [item.sample_id for item in self.samples]
        if len(ids) != len(set(ids)):
            raise OutcomeGovernanceError("dataset contains duplicate sample ids")
        expected = _digest([item.to_dict() for item in self.samples])
        if expected != self.content_hash:
            raise OutcomeGovernanceError("dataset content hash is invalid")

    def manifest(self) -> dict[str, Any]:
        partition_counts = {
            partition: sum(
                item.partition == partition for item in self.samples
            )
            for partition in PARTITIONS
        }
        return {
            "dataset_id": self.dataset_id,
            "source_report_id": self.source_report_id,
            "source_report_hash": self.source_report_hash,
            "created_at": self.created_at,
            "purpose": self.purpose,
            "sample_count": len(self.samples),
            "effective_task_count": round(
                sum(item.sample_weight for item in self.samples), 6
            ),
            "partition_counts": partition_counts,
            "evidence_levels": sorted(
                {item.evidence_level for item in self.samples}
            ),
            "content_hash": self.content_hash,
            "builder_revision": self.builder_revision,
            "schema_version": self.schema_version,
            "governance_passed": bool(
                self.quality_report.get("governance_passed")
            ),
            "training_allowed": bool(
                self.quality_report.get("training_allowed")
            ),
            "training_frozen": not bool(
                self.quality_report.get("training_allowed")
            ),
            "freeze_reasons": list(
                self.quality_report.get("freeze_reasons", ())
            ),
            "sample_tier": self.quality_report.get("sample_tier"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.manifest(),
            "quality_report": dict(self.quality_report),
            "samples": [item.to_dict() for item in self.samples],
        }


def _sample_tier(effective_count: float) -> str:
    if effective_count >= 1000:
        return SAMPLE_TIER_FORMAL
    if effective_count >= 200:
        return SAMPLE_TIER_INTERNAL
    return SAMPLE_TIER_SEED


def _task_features(task: Mapping[str, Any]) -> dict[str, Any]:
    contract = dict(task.get("task_contract") or {})
    return {
        "task_type": str(contract.get("task_type") or "unknown"),
        "difficulty": int(contract.get("difficulty") or 0),
        "risk_level": str(contract.get("risk_level") or "unknown"),
        "criticality": float(contract.get("criticality") or 0.0),
        "classification_confidence": float(
            contract.get("classification_confidence") or 0.0
        ),
        "estimated_input_tokens": int(
            contract.get("estimated_input_tokens") or 0
        ),
        "estimated_output_tokens": int(
            contract.get("estimated_output_tokens") or 0
        ),
        "required_capabilities": sorted(
            str(item)
            for item in contract.get("required_capabilities", ())
        ),
        "required_modalities": sorted(
            str(item)
            for item in contract.get("required_modalities", ())
        ),
        "requires_tools": sorted(
            str(item) for item in contract.get("requires_tools", ())
        ),
        "requires_freshness": bool(contract.get("requires_freshness")),
        "requires_approval": bool(contract.get("requires_approval")),
        "dependency_count": len(contract.get("dependencies", ())),
        "acceptance_criteria_count": len(
            contract.get("acceptance_criteria", ())
        ),
    }


def _contract_summary(
    secret: bytes, task: Mapping[str, Any]
) -> dict[str, Any]:
    success = dict(task.get("success_contract") or {})
    criteria = list(success.get("criteria") or ())
    return {
        "contract_revision": str(success.get("revision") or "unknown"),
        "criterion_count": len(criteria),
        "critical_criterion_count": sum(
            bool(item.get("critical")) for item in criteria
        ),
        "independent_evidence_criterion_count": sum(
            bool(item.get("independent_evidence_required"))
            for item in criteria
        ),
        "minimum_score": float(success.get("minimum_score") or 0.0),
        "required_result_fields": sorted(
            str(item)
            for item in success.get("required_result_fields", ())
        ),
        "contract_fingerprint": _hmac_identifier(
            secret,
            "contract",
            canonical_json(
                {
                    "criteria": criteria,
                    "minimum_score": success.get("minimum_score"),
                    "required_result_fields": success.get(
                        "required_result_fields", ()
                    ),
                }
            ),
        ),
    }


def _failure(outcome: Mapping[str, Any]) -> dict[str, Any] | None:
    if bool(outcome.get("accepted")):
        return None
    status = str(outcome.get("status") or "unknown")
    if status == "route_rejected":
        category = "routing_infeasible"
    elif status == "budget_rejected":
        category = "budget_exhausted"
    elif status == "evaluated":
        category = "validation_failed"
    else:
        category = "execution_failed"
    failed_criteria = [
        str(item.get("criterion_id"))
        for item in outcome.get("criteria_results", ())
        if not bool(item.get("passed"))
    ]
    return {
        "category": category,
        "root_cause": status,
        "failed_criterion_count": len(failed_criteria),
        "failed_criterion_hash": _digest(failed_criteria),
        "evidence_level": str(
            outcome.get("evidence_level") or "unknown"
        ),
    }


def _safe_route(
    secret: bytes, route: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    if not route:
        return None
    allowed = {
        "baseline_id",
        "policy_revision",
        "model_id",
        "model_name",
        "provider",
        "predicted_success",
        "success_lower_bound",
        "estimated_cost_usd",
        "estimated_latency_ms",
        "planning_overhead_ms",
        "handoff_overhead_ms",
    }
    result = {
        key: route[key]
        for key in sorted(allowed)
        if route.get(key) is not None
    }
    result["route_receipt_hash"] = _hmac_identifier(
        secret,
        "route",
        str(route.get("receipt_id") or canonical_json(route)),
    )
    return result


def _initial_label(
    *,
    secret: bytes,
    sample_id: str,
    task: Mapping[str, Any],
    outcome: Mapping[str, Any],
    recorded_at: str,
) -> LabelEvidence:
    status = str(outcome.get("status") or "unknown")
    hard_pass = bool(outcome.get("hard_pass"))
    accepted = bool(outcome.get("accepted"))
    if status == "evaluated":
        source = LABEL_SOURCE_AUTOMATED_HARD
        independent = bool(task.get("independent_validator"))
        verdict = (
            LABEL_ACCEPTED
            if accepted and hard_pass
            else LABEL_REJECTED
        )
        evaluator = str(
            task.get("independent_validator")
            or outcome.get("backend_revision")
            or "unknown-validator"
        )
    else:
        source = LABEL_SOURCE_SYSTEM
        independent = False
        verdict = LABEL_REJECTED
        evaluator = str(outcome.get("backend_revision") or status)
    refs = [
        str(item.get("evidence_ref"))
        for item in outcome.get("criteria_results", ())
        if item.get("evidence_ref")
    ]
    return LabelEvidence.create(
        sample_id=sample_id,
        source=source,
        verdict=verdict,
        independent=independent,
        blinded=False,
        delayed=False,
        evidence_level=str(outcome.get("evidence_level") or "unknown"),
        evaluator_revision=evaluator,
        evidence_refs=_safe_evidence_refs(secret, refs),
        recorded_at=recorded_at,
    )


def _sensitive_findings(value: Any) -> list[str]:
    raw = canonical_json(value)
    return [
        f"pattern_{index}"
        for index, pattern in enumerate(SENSITIVE_PATTERNS, start=1)
        if pattern.search(raw)
    ]


def _quality_report(
    samples: Sequence[GovernedOutcome],
    *,
    source_report: Mapping[str, Any],
    config: GovernanceConfig,
    project_partitions: Mapping[str, set[str]],
) -> dict[str, Any]:
    sample_count = len(samples)
    effective_count = sum(item.sample_weight for item in samples)
    labelled = sum(
        item.resolved_label.get("state") != "unlabelled"
        for item in samples
    )
    conflicts = sum(
        bool(item.resolved_label.get("conflict")) for item in samples
    )
    accidental_duplicates = sample_count - len(
        {item.source_observation_hash for item in samples}
    )
    leakage_projects = sorted(
        project
        for project, partitions in project_partitions.items()
        if len(partitions) > 1
    )
    traceable = sum(
        all(
            item.version_bindings.get(key)
            not in (None, "", "unknown", [], {})
            for key in (
                "dataset_schema",
                "source_schema",
                "runner_revision",
                "backend_revision",
                "policy_revision",
                "contract_revision",
            )
        )
        for item in samples
    )
    reconciled = sum(
        bool(item.actuals.get("cost_reconciled")) for item in samples
    )
    candidate_complete = sum(
        item.candidate_routes.get("capture_status") == "complete"
        for item in samples
    )
    propensity_captured = sum(
        item.candidate_routes.get("selection_probability") is not None
        for item in samples
    )
    sensitive = [
        {"sample_id": item.sample_id, "patterns": findings}
        for item in samples
        if (findings := _sensitive_findings(item.to_dict()))
    ]
    missing = []
    for item in samples:
        for field_name, value in (
            ("task_features", item.task_features),
            ("contract_summary", item.contract_summary),
            ("actuals", item.actuals),
            ("version_bindings", item.version_bindings),
            ("lineage", item.lineage),
            ("label_events", item.label_events),
        ):
            if value in (None, "", [], {}, ()):
                missing.append(
                    {"sample_id": item.sample_id, "field": field_name}
                )
    governance_failures = []
    if missing:
        governance_failures.append("required_fields_missing")
    if accidental_duplicates:
        governance_failures.append("duplicate_observations")
    if leakage_projects:
        governance_failures.append("project_partition_leakage")
    if sensitive:
        governance_failures.append("sensitive_data_detected")
    if reconciled != sample_count:
        governance_failures.append("cost_ledger_mismatch")
    if traceable != sample_count:
        governance_failures.append("version_traceability_incomplete")

    freeze_reasons = list(governance_failures)
    evidence_levels = sorted({item.evidence_level for item in samples})
    if any(level.startswith("E0") for level in evidence_levels):
        if not config.allow_simulated_training:
            freeze_reasons.append("simulated_evidence_not_trainable")
    if effective_count < 200:
        freeze_reasons.append("insufficient_effective_sample_size")
    if conflicts / max(1, sample_count) > 0.05:
        freeze_reasons.append("label_conflict_rate_exceeded")
    if labelled / max(1, sample_count) < 0.80:
        freeze_reasons.append("label_coverage_below_80_percent")
    if not bool(source_report.get("passed")):
        freeze_reasons.append("source_evaluation_failed")
    if candidate_complete != sample_count:
        freeze_reasons.append("candidate_route_capture_incomplete")
    if propensity_captured != sample_count:
        freeze_reasons.append("selection_propensity_incomplete")
    freeze_reasons = sorted(set(freeze_reasons))
    governance_passed = not governance_failures
    training_allowed = governance_passed and not freeze_reasons
    return {
        "schema_version": OUTCOME_DATASET_SCHEMA_VERSION,
        "sample_count": sample_count,
        "effective_task_count": round(effective_count, 6),
        "sample_tier": _sample_tier(effective_count),
        "missing_required_field_count": len(missing),
        "missing_required_fields": missing,
        "labelled_sample_count": labelled,
        "label_coverage": round(labelled / max(1, sample_count), 6),
        "label_conflict_count": conflicts,
        "label_conflict_rate": round(
            conflicts / max(1, sample_count), 6
        ),
        "accidental_duplicate_count": accidental_duplicates,
        "accidental_duplicate_rate": round(
            accidental_duplicates / max(1, sample_count), 6
        ),
        "route_variant_count": max(
            0, sample_count - int(round(effective_count))
        ),
        "project_leakage_count": len(leakage_projects),
        "project_leakage_hashes": [
            _digest(item)[:16] for item in leakage_projects
        ],
        "version_traceable_count": traceable,
        "version_traceability_rate": round(
            traceable / max(1, sample_count), 6
        ),
        "cost_reconciled_count": reconciled,
        "cost_reconciliation_rate": round(
            reconciled / max(1, sample_count), 6
        ),
        "candidate_route_complete_count": candidate_complete,
        "candidate_route_capture_rate": round(
            candidate_complete / max(1, sample_count), 6
        ),
        "selection_propensity_count": propensity_captured,
        "selection_propensity_rate": round(
            propensity_captured / max(1, sample_count), 6
        ),
        "sensitive_finding_count": len(sensitive),
        "sensitive_findings": sensitive,
        "evidence_levels": evidence_levels,
        "governance_failures": governance_failures,
        "governance_passed": governance_passed,
        "training_allowed": training_allowed,
        "freeze_reasons": freeze_reasons,
        "commercial_claims_allowed": False,
    }


def build_outcome_dataset(
    report: Mapping[str, Any],
    config: GovernanceConfig,
) -> OutcomeDataset:
    """Build one deterministic learning copy from a v0.5.6.1 report."""

    required = {
        "report_id",
        "schema_version",
        "runner_revision",
        "tasks",
        "observations",
        "passed",
    }
    missing = sorted(required - set(report))
    if missing:
        raise OutcomeGovernanceError(
            "source report is missing fields: " + ", ".join(missing)
        )
    tasks = {
        str(item["benchmark_id"]): dict(item)
        for item in report["tasks"]
    }
    observations = [dict(item) for item in report["observations"]]
    observation_ids = [
        str(item.get("observation_id")) for item in observations
    ]
    if len(observation_ids) != len(set(observation_ids)):
        raise OutcomeGovernanceError(
            "source report contains duplicate observation ids"
        )
    created_at = config.timestamp
    expires_at = (
        _parse_time(created_at) + timedelta(days=config.ttl_days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    secret = config.secret

    task_dedupe_groups: dict[str, str] = {}
    for task_id, task in tasks.items():
        task_dedupe_groups[task_id] = _hmac_identifier(
            config.secret,
            "dedupe",
            canonical_json(
                {
                    "features": _task_features(task),
                    "contract": _contract_summary(config.secret, task),
                }
            ),
        )
    variant_counts: dict[str, int] = {}
    for item in observations:
        task_id = str(item.get("task_id"))
        if task_id not in task_dedupe_groups:
            raise OutcomeGovernanceError(
                f"observation references unknown task: {task_id}"
            )
        group = task_dedupe_groups[task_id]
        variant_counts[group] = variant_counts.get(group, 0) + 1

    project_partitions: dict[str, set[str]] = {}
    samples: list[GovernedOutcome] = []
    for observation in sorted(
        observations,
        key=lambda item: (
            str(item.get("partition")),
            str(item.get("task_id")),
            str(item.get("baseline_id")),
        ),
    ):
        task_id = str(observation.get("task_id"))
        if task_id not in tasks:
            raise OutcomeGovernanceError(
                f"observation references unknown task: {task_id}"
            )
        task = tasks[task_id]
        project_id = str(observation.get("project_id"))
        partition = str(observation.get("partition"))
        if partition not in PARTITIONS:
            raise OutcomeGovernanceError(
                f"invalid observation partition: {partition}"
            )
        if partition != str(task.get("partition")):
            raise OutcomeGovernanceError(
                "observation and task partitions disagree"
            )
        project_partitions.setdefault(project_id, set()).add(partition)
        source_observation_hash = _digest(observation)
        sample_id = "sample_" + _digest(
            {
                "source_report_id": report["report_id"],
                "source_observation_hash": source_observation_hash,
                "schema": OUTCOME_DATASET_SCHEMA_VERSION,
            }
        )[:32]
        features = _task_features(task)
        contract = _contract_summary(secret, task)
        dedupe_group = task_dedupe_groups[task_id]
        outcome = dict(observation.get("outcome") or {})
        raw_route = (
            dict(observation["route"])
            if observation.get("route")
            else None
        )
        route = _safe_route(secret, raw_route)
        components = {
            key: float(value)
            for key, value in (
                outcome.get("cost_breakdown_usd") or {}
            ).items()
        }
        component_total = round(sum(components.values()), 8)
        reported_total = round(
            float(outcome.get("total_cost_usd") or 0.0), 8
        )
        duration_components = {
            key: int(value)
            for key, value in (
                outcome.get("duration_breakdown_ms") or {}
            ).items()
        }
        evidence_level = str(
            outcome.get("evidence_level")
            or report.get("evidence_level")
            or "unknown"
        )
        initial_label = _initial_label(
            secret=secret,
            sample_id=sample_id,
            task=task,
            outcome=outcome,
            recorded_at=created_at,
        )
        policy_revision = (
            str(route.get("policy_revision"))
            if route
            else f"rejected:{observation.get('baseline_id', 'unknown')}"
        )
        backend_revision = str(
            outcome.get("backend_revision") or "unknown"
        )
        sample = GovernedOutcome(
            sample_id=sample_id,
            source_observation_hash=source_observation_hash,
            anonymous_task_id=_hmac_identifier(
                secret, "task", task_id
            ),
            anonymous_project_id=_hmac_identifier(
                secret, "project", project_id
            ),
            anonymous_user_id=_hmac_identifier(
                secret,
                "user",
                str(task.get("user_id") or f"project:{project_id}"),
            ),
            partition=partition,
            collected_at=str(task.get("collected_at") or created_at),
            source_kind=str(task.get("source_kind") or "unknown"),
            evidence_level=evidence_level,
            task_features=features,
            contract_summary=contract,
            candidate_routes={
                "capture_status": "selected_route_only",
                "candidates": [route] if route else [],
                "rejections": [],
                "selection_probability": 1.0 if route else None,
                "propensity_status": (
                    "deterministic_policy" if route else "route_rejected"
                ),
            },
            selected_route=route,
            actuals={
                "status": str(outcome.get("status") or "unknown"),
                "accepted": bool(outcome.get("accepted")),
                "hard_pass": bool(outcome.get("hard_pass")),
                "soft_quality": float(
                    outcome.get("soft_quality") or 0.0
                ),
                "token_usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "capture_status": "not_available_in_fixture",
                },
                "cost_breakdown_usd": components,
                "reported_total_cost_usd": reported_total,
                "reconciled_total_cost_usd": component_total,
                "cost_reconciled": math.isclose(
                    component_total, reported_total, abs_tol=1e-8
                ),
                "duration_breakdown_ms": duration_components,
                "total_duration_ms": int(
                    outcome.get("total_duration_ms")
                    or sum(duration_components.values())
                ),
                "queue_duration_ms": None,
                "tool_calls": int(outcome.get("tool_calls") or 0),
                "provider_calls": int(
                    outcome.get("provider_calls") or 0
                ),
            },
            execution_trace={
                "attempts": int(outcome.get("attempts") or 0),
                "simulated_calls": int(
                    outcome.get("simulated_calls") or 0
                ),
                "checkpoints": [],
                "retries": max(
                    0, int(outcome.get("attempts") or 0) - 1
                ),
                "upgrades": 0,
                "recoveries": 0,
                "human_interventions": 0,
            },
            failure=_failure(outcome),
            version_bindings={
                "dataset_schema": OUTCOME_DATASET_SCHEMA_VERSION,
                "feature_schema": FEATURE_SCHEMA_VERSION,
                "label_schema": LABEL_SCHEMA_VERSION,
                "source_schema": str(report["schema_version"]),
                "runner_revision": str(report["runner_revision"]),
                "backend_revision": backend_revision,
                "policy_revision": policy_revision,
                "contract_revision": str(
                    contract["contract_revision"]
                ),
                "model_id": (
                    str(route.get("model_id")) if route else "route_rejected"
                ),
                "provider": (
                    str(route.get("provider")) if route else "route_rejected"
                ),
                "deployment_id": (
                    (
                        f"{route.get('provider', 'unknown')}:"
                        f"{route.get('model_id', 'unknown')}"
                    )
                    if route
                    else "route_rejected"
                ),
                "reasoning_effort": "not_captured",
                "adapter_revision": backend_revision,
                "prompt_revision": "success-contract-v1",
                "catalog_revision": str(
                    (report.get("spec") or {}).get(
                        "catalog_revision", "unknown"
                    )
                ),
                "task_set_hash": str(
                    report.get("task_set_hash") or "unknown"
                ),
            },
            lineage={
                "source_type": "evaluation_observation",
                "source_report_hash": _digest(report),
                "source_observation_hash": source_observation_hash,
                "run_trace_hash": _hmac_identifier(
                    secret,
                    "trace",
                    (
                        f"{report['report_id']}:"
                        f"{observation.get('observation_id')}"
                    ),
                ),
                "artifact_trace_status": "not_applicable_fixture",
                "message_trace_status": "not_applicable_fixture",
                "validation_trace_status": "captured_as_label_evidence",
                "ledger_trace_status": "reconciled_from_observation",
            },
            dedupe_group=dedupe_group,
            sample_weight=round(
                1.0 / variant_counts[dedupe_group], 12
            ),
            purpose=config.purpose,
            expires_at=expires_at,
            redaction={
                "raw_goal_retained": False,
                "raw_project_id_retained": False,
                "raw_user_id_retained": False,
                "raw_evidence_refs_retained": False,
                "identifier_method": "HMAC-SHA256",
                "sensitive_text_policy": "deny-by-default",
            },
            label_events=(initial_label,),
            resolved_label=resolve_labels((initial_label,)),
        )
        samples.append(sample)

    quality = _quality_report(
        samples,
        source_report=report,
        config=config,
        project_partitions=project_partitions,
    )
    sample_payload = [item.to_dict() for item in samples]
    content_hash = _digest(sample_payload)
    source_report_hash = _digest(report)
    dataset_id = "dataset_" + _digest(
        {
            "source_report_hash": source_report_hash,
            "content_hash": content_hash,
            "purpose": config.purpose,
            "schema": OUTCOME_DATASET_SCHEMA_VERSION,
        }
    )[:32]
    return OutcomeDataset(
        dataset_id=dataset_id,
        source_report_id=str(report["report_id"]),
        source_report_hash=source_report_hash,
        created_at=created_at,
        purpose=config.purpose,
        samples=tuple(samples),
        quality_report=quality,
        content_hash=content_hash,
    )


def assert_training_ready(dataset: OutcomeDataset) -> None:
    if not bool(dataset.quality_report.get("training_allowed")):
        reasons = ", ".join(
            dataset.quality_report.get("freeze_reasons", ())
        )
        raise TrainingFrozenError(
            f"dataset {dataset.dataset_id} is frozen: {reasons}"
        )


def _write_immutable(path: Path, raw: str) -> None:
    encoded = raw.encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise OutcomeGovernanceError(
                f"refusing to overwrite different evidence: {path.name}"
            )
        return
    path.write_bytes(encoded)


def export_outcome_dataset(
    dataset: OutcomeDataset, output_dir: str | Path
) -> dict[str, Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest = dataset.manifest()
    quality = dict(dataset.quality_report)
    samples = "\n".join(
        canonical_json(item.to_dict()) for item in dataset.samples
    ) + "\n"
    labels = "\n".join(
        canonical_json(event.to_dict())
        for sample in dataset.samples
        for event in sample.label_events
    ) + "\n"
    data_card = (
        "# CostWeave Outcome Dataset Card\n\n"
        f"- Dataset: `{dataset.dataset_id}`\n"
        f"- Schema: `{dataset.schema_version}`\n"
        f"- Samples: {len(dataset.samples)}\n"
        f"- Effective tasks: {manifest['effective_task_count']}\n"
        f"- Evidence: {', '.join(manifest['evidence_levels'])}\n"
        f"- Governance passed: {manifest['governance_passed']}\n"
        f"- Training allowed: {manifest['training_allowed']}\n"
        f"- Freeze reasons: {', '.join(manifest['freeze_reasons'])}\n\n"
        "Raw goals, project identifiers, user identifiers and evidence "
        "references are excluded from the learning copy. This dataset does "
        "not authorize policy training, deployment or commercial claims.\n"
    )
    paths = {
        "manifest": root / "manifest.json",
        "quality": root / "quality-report.json",
        "samples": root / "samples.jsonl",
        "labels": root / "labels.jsonl",
        "data_card": root / "DATA_CARD.md",
    }
    _write_immutable(
        paths["manifest"],
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    _write_immutable(
        paths["quality"],
        json.dumps(quality, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    _write_immutable(paths["samples"], samples)
    _write_immutable(paths["labels"], labels)
    _write_immutable(paths["data_card"], data_card)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a governed CostWeave outcome learning copy"
    )
    parser.add_argument("experiment", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--privacy-salt",
        help=(
            "privacy salt; prefer COSTWEAVE_DATASET_PRIVACY_SALT "
            "to avoid command history"
        ),
    )
    parser.add_argument(
        "--purpose", choices=PURPOSES, default="evaluation"
    )
    parser.add_argument("--ttl-days", type=int, default=365)
    parser.add_argument(
        "--created-at",
        help="fixed ISO-8601 creation time for reproducible fixtures",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="optional CostWeave SQLite database for governed persistence",
    )
    args = parser.parse_args(argv)
    privacy_salt = args.privacy_salt or os.environ.get(
        "COSTWEAVE_DATASET_PRIVACY_SALT"
    )
    if not privacy_salt:
        parser.error(
            "set COSTWEAVE_DATASET_PRIVACY_SALT or --privacy-salt"
        )
    report = json.loads(args.experiment.read_text(encoding="utf-8"))
    dataset = build_outcome_dataset(
        report,
        GovernanceConfig(
            privacy_salt=privacy_salt,
            purpose=args.purpose,
            ttl_days=args.ttl_days,
            created_at=args.created_at or "",
        ),
    )
    paths = export_outcome_dataset(dataset, args.output_dir)
    persisted = None
    if args.database is not None:
        from .event_store import EventStore
        from .outcome_dataset_store import OutcomeDatasetStore

        EventStore(args.database)
        persisted = OutcomeDatasetStore(args.database).record(dataset)
    print(
        json.dumps(
            {
                **dataset.manifest(),
                "database": (
                    {
                        "path": str(args.database),
                        "inserted": persisted,
                    }
                    if args.database is not None
                    else None
                ),
                "files": {
                    key: str(value) for key, value in paths.items()
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DATASET_BUILDER_REVISION",
    "FEATURE_SCHEMA_VERSION",
    "GovernanceConfig",
    "GovernedOutcome",
    "LABEL_SCHEMA_VERSION",
    "LABEL_SOURCE_AUTOMATED_HARD",
    "LABEL_SOURCE_HUMAN_BLIND",
    "LABEL_SOURCE_INDEPENDENT_MODEL",
    "LABEL_SOURCE_SYSTEM",
    "LABEL_SOURCE_USER",
    "LABEL_SOURCES",
    "LABEL_ACCEPTED",
    "LABEL_REJECTED",
    "LabelEvidence",
    "OUTCOME_DATASET_SCHEMA_VERSION",
    "OutcomeDataset",
    "OutcomeGovernanceError",
    "TrainingFrozenError",
    "assert_training_ready",
    "build_outcome_dataset",
    "export_outcome_dataset",
    "resolve_labels",
]
