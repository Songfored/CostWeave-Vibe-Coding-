"""Offline, deterministic tournament runner for v0.6.0 routing policies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..intelligence.common import (
    PARTITIONS,
    TOURNAMENT_POLICY_REVISION,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_digest,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_timestamp,
)
from .claims import ClaimGateResultV1, evaluate_claim_gate
from .contracts import CaseViewV1, DatasetManifestV1, RoutingRecordV1, validate_routing_records
from .metrics import PolicyMetricsV1, evaluate_policy_decisions, summarize_policy
from .policies import PolicyDecisionV1, TournamentPolicy
from .statistics import BootstrapInterval, paired_bootstrap_cost_reduction_pct, paired_bootstrap_difference


class TournamentError(ValueError):
    """Raised when a tournament contract or its offline evidence is invalid."""


def _seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("random_seed must be int")
    if not 0 <= value <= (2**31 - 1):
        raise ValueError("random_seed must be between 0 and 2^31-1")
    return value


def _bootstrap(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("bootstrap_samples must be int")
    if not 100 <= value <= 200_000:
        raise ValueError("bootstrap_samples must be between 100 and 200,000")
    return value


@dataclass(frozen=True, slots=True)
class TournamentSpecV1:
    project_id: str
    dataset_id: str
    partition: str
    strong_model_id: str
    cheap_model_id: str
    baseline_policy_id: str
    acceptance_threshold: float
    quality_weight: float
    cost_weight: float
    latency_weight: float
    bootstrap_samples: int
    random_seed: int
    minimum_claim_cases: int
    registered_at: str
    policy_revision: str = TOURNAMENT_POLICY_REVISION

    def __post_init__(self) -> None:
        from ..security_boundary import normalize_project_id

        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "dataset_id", bounded_text(self.dataset_id, "dataset_id"))
        if self.partition not in PARTITIONS:
            raise ValueError("unsupported partition")
        object.__setattr__(self, "strong_model_id", bounded_text(self.strong_model_id, "strong_model_id"))
        object.__setattr__(self, "cheap_model_id", bounded_text(self.cheap_model_id, "cheap_model_id"))
        if self.strong_model_id == self.cheap_model_id:
            raise ValueError("strong_model_id and cheap_model_id must differ")
        object.__setattr__(self, "baseline_policy_id", bounded_text(self.baseline_policy_id, "baseline_policy_id"))
        object.__setattr__(self, "acceptance_threshold", probability(self.acceptance_threshold, "acceptance_threshold"))
        weights = []
        for name in ("quality_weight", "cost_weight", "latency_weight"):
            value = finite_number(getattr(self, name), name, minimum=0.0)
            object.__setattr__(self, name, value)
            weights.append(value)
        if not any(value > 0 for value in weights):
            raise ValueError("at least one utility weight must be positive")
        object.__setattr__(self, "bootstrap_samples", _bootstrap(self.bootstrap_samples))
        object.__setattr__(self, "random_seed", _seed(self.random_seed))
        if isinstance(self.minimum_claim_cases, bool) or not isinstance(self.minimum_claim_cases, int) or self.minimum_claim_cases < 30:
            raise ValueError("minimum_claim_cases must be at least 30")
        object.__setattr__(self, "registered_at", validate_timestamp(self.registered_at, "registered_at"))
        if self.policy_revision != TOURNAMENT_POLICY_REVISION:
            raise ValueError(f"policy_revision must be {TOURNAMENT_POLICY_REVISION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "dataset_id": self.dataset_id,
            "partition": self.partition,
            "strong_model_id": self.strong_model_id,
            "cheap_model_id": self.cheap_model_id,
            "baseline_policy_id": self.baseline_policy_id,
            "acceptance_threshold": self.acceptance_threshold,
            "quality_weight": self.quality_weight,
            "cost_weight": self.cost_weight,
            "latency_weight": self.latency_weight,
            "bootstrap_samples": self.bootstrap_samples,
            "random_seed": self.random_seed,
            "minimum_claim_cases": self.minimum_claim_cases,
            "policy_revision": self.policy_revision,
        }

    @property
    def tournament_id(self) -> str:
        return stable_id("tournament", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["registered_at"] = self.registered_at
        result["tournament_id"] = self.tournament_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TournamentSpecV1":
        fields = frozenset({
            "project_id", "dataset_id", "partition", "strong_model_id", "cheap_model_id", "baseline_policy_id",
            "acceptance_threshold", "quality_weight", "cost_weight", "latency_weight", "bootstrap_samples",
            "random_seed", "minimum_claim_cases", "registered_at", "policy_revision",
        })
        validate_exact_fields(value, required=fields, optional=frozenset({"tournament_id"}))
        item = cls(**{key: value[key] for key in fields})
        if "tournament_id" in value and value["tournament_id"] != item.tournament_id:
            raise ValueError("tournament_id does not match content")
        return item


@dataclass(frozen=True, slots=True)
class TournamentReportV1:
    tournament_id: str
    project_id: str
    dataset_id: str
    manifest_hash: str
    spec: Mapping[str, Any]
    policy_metrics: Mapping[str, Mapping[str, Any]]
    candidate_vs_baseline: Mapping[str, Mapping[str, Any]]
    oracle_diagnostics: Mapping[str, Any]
    bootstrap: Mapping[str, Mapping[str, Any]]
    claim_gates: Mapping[str, Mapping[str, Any]]
    leakage_checks: Mapping[str, Any]
    evidence_level: str
    network_calls: int
    provider_calls: int
    generated_at: str
    report_hash: str | None = None

    def __post_init__(self) -> None:
        from ..security_boundary import normalize_project_id

        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "tournament_id", bounded_text(self.tournament_id, "tournament_id"))
        object.__setattr__(self, "dataset_id", bounded_text(self.dataset_id, "dataset_id"))
        from ..intelligence.common import validate_sha256, EVIDENCE_LEVELS

        object.__setattr__(self, "manifest_hash", validate_sha256(self.manifest_hash, "manifest_hash"))
        if not isinstance(self.spec, Mapping) or not isinstance(self.policy_metrics, Mapping) or not isinstance(self.candidate_vs_baseline, Mapping) or not isinstance(self.oracle_diagnostics, Mapping) or not isinstance(self.bootstrap, Mapping) or not isinstance(self.claim_gates, Mapping) or not isinstance(self.leakage_checks, Mapping):
            raise TypeError("report sections must be mappings")
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise ValueError("unsupported evidence level")
        for name in ("network_calls", "provider_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        object.__setattr__(self, "generated_at", validate_timestamp(self.generated_at, "generated_at"))
        if self.report_hash is not None:
            object.__setattr__(self, "report_hash", validate_sha256(self.report_hash, "report_hash"))
            if self.report_hash != self.compute_report_hash():
                raise ValueError("report_hash does not match report content")
        else:
            object.__setattr__(self, "report_hash", self.compute_report_hash())

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "tournament_id": self.tournament_id,
            "project_id": self.project_id,
            "dataset_id": self.dataset_id,
            "manifest_hash": self.manifest_hash,
            "spec": self.spec,
            "policy_metrics": self.policy_metrics,
            "candidate_vs_baseline": self.candidate_vs_baseline,
            "oracle_diagnostics": self.oracle_diagnostics,
            "bootstrap": self.bootstrap,
            "claim_gates": self.claim_gates,
            "leakage_checks": self.leakage_checks,
            "evidence_level": self.evidence_level,
            "network_calls": self.network_calls,
            "provider_calls": self.provider_calls,
        }

    def compute_report_hash(self) -> str:
        return stable_digest(self._hash_payload(), length=64)

    def to_dict(self) -> dict[str, Any]:
        result = self._hash_payload()
        result["generated_at"] = self.generated_at
        result["report_hash"] = self.report_hash or self.compute_report_hash()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TournamentReportV1":
        fields = frozenset({
            "tournament_id", "project_id", "dataset_id", "manifest_hash", "spec", "policy_metrics",
            "candidate_vs_baseline", "oracle_diagnostics", "bootstrap", "claim_gates", "leakage_checks",
            "evidence_level", "network_calls", "provider_calls", "generated_at",
        })
        validate_exact_fields(value, required=fields, optional=frozenset({"report_hash"}))
        return cls(**{key: value[key] for key in fields}, report_hash=value.get("report_hash"))


def _interval_scale(interval: BootstrapInterval, factor: float) -> BootstrapInterval:
    return BootstrapInterval(
        mean_difference=interval.mean_difference * factor,
        lower=interval.lower * factor,
        upper=interval.upper * factor,
        samples=interval.samples,
        seed=interval.seed,
    )


def _relative_regression_pct(candidate: Sequence[float], baseline: Sequence[float], *, samples: int, seed: int) -> BootstrapInterval | None:
    if len(candidate) != len(baseline) or not candidate:
        raise ValueError("latency sequences must be non-empty and paired")
    baseline_total = sum(baseline)
    if baseline_total <= 0:
        return None
    from random import Random

    import math

    point = (sum(candidate) - baseline_total) / baseline_total * 100.0
    rng = Random(seed)
    draws: list[float] = []
    for _ in range(samples):
        indexes = [rng.randrange(len(candidate)) for _ in range(len(candidate))]
        base = sum(baseline[index] for index in indexes)
        if base > 0:
            draws.append((sum(candidate[index] for index in indexes) - base) / base * 100.0)
    if not draws:
        return None
    draws.sort()
    lower = draws[max(0, min(len(draws) - 1, math.ceil(0.025 * len(draws)) - 1))]
    upper = draws[max(0, min(len(draws) - 1, math.ceil(0.975 * len(draws)) - 1))]
    return BootstrapInterval(point, lower, upper, len(draws), seed)


class TournamentRunner:
    """Run a tournament entirely in memory; no network or provider calls."""

    def run(
        self,
        *,
        spec: TournamentSpecV1,
        manifest: DatasetManifestV1,
        records: Sequence[RoutingRecordV1],
        policies: Sequence[TournamentPolicy],
        generated_at: str | None = None,
    ) -> TournamentReportV1:
        if not isinstance(spec, TournamentSpecV1) or not isinstance(manifest, DatasetManifestV1):
            raise TypeError("spec and manifest must be v1 contracts")
        try:
            ordered = validate_routing_records(records)
        except (TypeError, ValueError) as exc:
            raise TournamentError(f"records are invalid: {exc}") from exc
        actual_manifest_hash = stable_digest([item.to_dict() for item in ordered], length=64)
        if actual_manifest_hash != manifest.records_hash:
            raise TournamentError("manifest hash does not match records")
        if spec.dataset_id != manifest.dataset_id:
            raise TournamentError("spec dataset_id does not match manifest")
        selected = tuple(item for item in ordered if item.partition == spec.partition)
        if not selected:
            raise TournamentError("requested partition contains no records")
        by_case: dict[str, list[RoutingRecordV1]] = {}
        for item in selected:
            by_case.setdefault(item.case_id, []).append(item)
        for case_id, case_records in by_case.items():
            models = {item.model_id for item in case_records}
            if spec.strong_model_id not in models or spec.cheap_model_id not in models:
                raise TournamentError(f"strong and cheap models must both exist for case {case_id}")
        if not policies:
            raise TournamentError("at least one policy is required")
        if any(not isinstance(policy, TournamentPolicy) for policy in policies):
            raise TournamentError("all policies must implement TournamentPolicy")
        policy_ids = [policy.policy_id for policy in policies]
        if len(policy_ids) != len(set(policy_ids)):
            raise TournamentError("policy IDs must be unique")
        if spec.baseline_policy_id not in policy_ids:
            raise TournamentError("baseline policy is not registered")
        cases = tuple(
            CaseViewV1(
                case_id=case_id,
                task_family=case_records[0].task_family,
                prompt_hash=case_records[0].prompt_hash,
                model_ids=tuple(sorted(item.model_id for item in case_records)),
            )
            for case_id, case_records in sorted(by_case.items())
        )
        decisions_by_policy: dict[str, tuple[PolicyDecisionV1, ...]] = {}
        for policy in policies:
            decisions: list[PolicyDecisionV1] = []
            for case in cases:
                try:
                    decision = policy.decide(case)
                except Exception as exc:
                    raise TournamentError(f"policy {policy.policy_id} failed on case {case.case_id}") from exc
                if not isinstance(decision, PolicyDecisionV1):
                    raise TournamentError("policy returned an invalid decision object")
                if decision.policy_id != policy.policy_id or decision.case_id != case.case_id:
                    raise TournamentError("policy decision identity mismatch")
                if not decision.abstained and decision.selected_model_id not in case.model_ids:
                    raise TournamentError("policy selected an unavailable model")
                decisions.append(decision)
            decisions_by_policy[policy.policy_id] = tuple(decisions)
        baseline_decisions = decisions_by_policy[spec.baseline_policy_id]
        if all(decision.abstained for decision in baseline_decisions):
            raise TournamentError("baseline cannot abstain for every case")
        metrics_by_policy: dict[str, PolicyMetricsV1] = {}
        outcomes_by_policy: dict[str, tuple[Any, ...]] = {}
        for policy in policies:
            outcomes = evaluate_policy_decisions(
                selected,
                decisions_by_policy[policy.policy_id],
                quality_weight=spec.quality_weight,
                cost_weight=spec.cost_weight,
                latency_weight=spec.latency_weight,
            )
            outcomes_by_policy[policy.policy_id] = outcomes
            metrics_by_policy[policy.policy_id] = summarize_policy(outcomes)
        baseline_metrics = metrics_by_policy[spec.baseline_policy_id]
        baseline_outcomes = outcomes_by_policy[spec.baseline_policy_id]
        baseline_by_case = {item.case_id: item for item in baseline_outcomes}
        candidate_vs_baseline: dict[str, dict[str, Any]] = {}
        bootstrap_reports: dict[str, dict[str, Any]] = {}
        claim_reports: dict[str, dict[str, Any]] = {}
        for policy in policies:
            if policy.policy_id == spec.baseline_policy_id:
                continue
            candidate_metrics = metrics_by_policy[policy.policy_id]
            candidate_outcomes = outcomes_by_policy[policy.policy_id]
            candidate_by_case = {item.case_id: item for item in candidate_outcomes}
            candidate_quality = [candidate_by_case[case.case_id].quality_score or 0.0 for case in cases]
            baseline_quality = [baseline_by_case[case.case_id].quality_score or 0.0 for case in cases]
            quality_interval = _interval_scale(
                paired_bootstrap_difference(candidate_quality, baseline_quality, samples=spec.bootstrap_samples, seed=spec.random_seed), 100.0
            )
            candidate_cost = [candidate_by_case[case.case_id].cost_usd or 0.0 for case in cases]
            baseline_cost = [baseline_by_case[case.case_id].cost_usd or 0.0 for case in cases]
            cost_interval = paired_bootstrap_cost_reduction_pct(candidate_cost, baseline_cost, samples=spec.bootstrap_samples, seed=spec.random_seed)
            candidate_latency = [float(candidate_by_case[case.case_id].latency_ms or 0) for case in cases]
            baseline_latency = [float(baseline_by_case[case.case_id].latency_ms or 0) for case in cases]
            latency_interval = _relative_regression_pct(candidate_latency, baseline_latency, samples=spec.bootstrap_samples, seed=spec.random_seed)
            cost_complete = candidate_metrics.decided_cases == candidate_metrics.total_cases and baseline_metrics.decided_cases == baseline_metrics.total_cases
            latency_complete = all(item.latency_ms is not None for item in candidate_outcomes if not item.abstained) and all(item.latency_ms is not None for item in baseline_outcomes if not item.abstained)
            comparison = {
                "candidate_policy_id": policy.policy_id,
                "baseline_policy_id": spec.baseline_policy_id,
                "quality_difference_pp": quality_interval.to_dict(),
                "cost_reduction_pct": cost_interval.to_dict() if cost_interval else None,
                "latency_regression_pct": latency_interval.to_dict() if latency_interval else None,
                "candidate_coverage": candidate_metrics.coverage,
                "baseline_coverage": baseline_metrics.coverage,
            }
            candidate_vs_baseline[policy.policy_id] = comparison
            bootstrap_reports[policy.policy_id] = {
                "quality_difference_pp": quality_interval.to_dict(),
                "cost_reduction_pct": cost_interval.to_dict() if cost_interval else None,
                "latency_regression_pct": latency_interval.to_dict() if latency_interval else None,
            }
            claim = evaluate_claim_gate(
                candidate_metrics=candidate_metrics,
                baseline_metrics=baseline_metrics,
                quality_difference_pp=quality_interval,
                cost_reduction_pct=cost_interval,
                latency_regression_pct=latency_interval,
                case_count=len(cases),
                evidence_level=manifest.evidence_level,
                partition=spec.partition,
                candidate_is_oracle=False,
                blind_fields_exposed=False,
                cost_complete=cost_complete,
                latency_complete=latency_complete,
                minimum_claim_cases=spec.minimum_claim_cases,
            )
            claim_reports[policy.policy_id] = claim.to_dict()
        generated = generated_at or "2026-08-09T00:00:00Z"
        report = TournamentReportV1(
            tournament_id=spec.tournament_id,
            project_id=spec.project_id,
            dataset_id=manifest.dataset_id,
            manifest_hash=manifest.records_hash,
            spec=spec.to_dict(),
            policy_metrics={key: value.to_dict() for key, value in sorted(metrics_by_policy.items())},
            candidate_vs_baseline={key: value for key, value in sorted(candidate_vs_baseline.items())},
            oracle_diagnostics={
                "oracle_policy_id": None,
                "policy_oracle_agreement": {key: value.oracle_agreement for key, value in sorted(metrics_by_policy.items())},
                "oracle_is_diagnostic_only": True,
            },
            bootstrap={key: value for key, value in sorted(bootstrap_reports.items())},
            claim_gates={key: value for key, value in sorted(claim_reports.items())},
            leakage_checks={
                "blind_fields_exposed": False,
                "prompt_fields_exposed": False,
                "prediction_fields_exposed": False,
                "provider_calls": 0,
                "network_calls": 0,
            },
            evidence_level=manifest.evidence_level,
            network_calls=0,
            provider_calls=0,
            generated_at=generated,
        )
        return report


__all__ = [
    "TournamentError",
    "TournamentReportV1",
    "TournamentRunner",
    "TournamentSpecV1",
]
