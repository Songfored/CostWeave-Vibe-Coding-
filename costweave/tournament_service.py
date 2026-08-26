"""Service boundary for the local-only v0.6.0 tournament API."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Mapping

from .benchmarks.contracts import RoutingRecordV1, build_dataset_manifest, validate_routing_records
from .benchmarks.policies import DeterministicRandomPolicy, FixedModelPolicy
from .benchmarks.tournament import TournamentError, TournamentRunner, TournamentSpecV1
from .decision_intelligence_store import DecisionIntelligenceStore
from .intelligence.common import DECISION_INTELLIGENCE_SCHEMA_VERSION, bounded_text
from .security_boundary import DEFAULT_DATA_RESIDENCY, normalize_project_id


class TournamentServiceError(ValueError):
    """Raised for an API-level tournament contract violation."""


def bundled_seed() -> tuple[tuple[RoutingRecordV1, ...], Any]:
    path = files("costweave.benchmarks").joinpath("v060_seed_routing_records.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = validate_routing_records(tuple(RoutingRecordV1.from_dict(item) for item in raw))
    manifest = build_dataset_manifest(
        records,
        source_uri="package://costweave/benchmarks/v060_seed_routing_records.json",
        license_id="MIT",
        created_at="2026-08-09T00:00:00Z",
    )
    return records, manifest


class OfflineTournamentService:
    """Keep HTTP handlers thin and enforce a zero-provider-call boundary."""

    def __init__(self, store: DecisionIntelligenceStore) -> None:
        if not isinstance(store, DecisionIntelligenceStore):
            raise TypeError("store must be DecisionIntelligenceStore")
        self.store = store

    def status(self, *, project_id: str = "local-default") -> dict[str, Any]:
        project = normalize_project_id(project_id)
        records, manifest = bundled_seed()
        return {
            "schema_version": DECISION_INTELLIGENCE_SCHEMA_VERSION,
            "mode": "offline_seed_tournament",
            "project_id": project,
            "data_residency": DEFAULT_DATA_RESIDENCY,
            "dataset_id": manifest.dataset_id,
            "case_count": manifest.case_count,
            "record_count": manifest.record_count,
            "evidence_level": manifest.evidence_level,
            "provider_calls": 0,
            "network_calls": 0,
            "defaults": {
                "partition": "test",
                "strong_model": "seed-strong",
                "cheap_model": "seed-cheap",
                "baseline": "strong-only",
                "bootstrap_samples": 2000,
                "seed": 600,
            },
            "runtime_counts": self.store.counts(project_id=project),
        }

    def run(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        confirm: str,
        bootstrap_samples: int = 2000,
        seed: int = 600,
    ) -> tuple[dict[str, Any], bool]:
        if confirm != "RUN_OFFLINE_TOURNAMENT":
            raise TournamentServiceError("confirm must equal RUN_OFFLINE_TOURNAMENT")
        key = bounded_text(idempotency_key, "idempotency_key", minimum=8, maximum=200)
        if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int):
            raise TournamentServiceError("bootstrap_samples must be an integer")
        if not 100 <= bootstrap_samples <= 10_000:
            raise TournamentServiceError("bootstrap_samples must be between 100 and 10,000")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
            raise TournamentServiceError("seed must be between 0 and 2^31-1")
        records, manifest = bundled_seed()
        spec = TournamentSpecV1(
            project_id=project_id,
            dataset_id=manifest.dataset_id,
            partition="test",
            strong_model_id="seed-strong",
            cheap_model_id="seed-cheap",
            baseline_policy_id="strong-only",
            acceptance_threshold=0.8,
            quality_weight=1.0,
            cost_weight=0.1,
            latency_weight=0.01,
            bootstrap_samples=bootstrap_samples,
            random_seed=seed,
            minimum_claim_cases=30,
            registered_at="2026-08-09T00:00:00Z",
        )
        policies = (
            FixedModelPolicy("seed-strong", policy_id="strong-only"),
            FixedModelPolicy("seed-cheap", policy_id="cheap-only"),
            FixedModelPolicy("seed-balanced", policy_id="balanced-only"),
            DeterministicRandomPolicy(seed, policy_id=f"random-seed-{seed}"),
        )
        try:
            report = TournamentRunner().run(spec=spec, manifest=manifest, records=records, policies=policies)
        except (TournamentError, TypeError, ValueError) as exc:
            raise TournamentServiceError(str(exc)) from exc
        stored, replayed = self.store.record_tournament(
            project_id=spec.project_id,
            tournament_id=spec.tournament_id,
            idempotency_key=key,
            dataset_id=manifest.dataset_id,
            manifest_hash=manifest.records_hash,
            report=report.to_dict(),
            created_at=report.generated_at,
        )
        return {"tournament": stored, "report": report.to_dict(), "replayed": replayed}, replayed

    def get(self, *, project_id: str, tournament_id: str) -> dict[str, Any] | None:
        return self.store.tournament(project_id=project_id, tournament_id=tournament_id)


__all__ = ["OfflineTournamentService", "TournamentServiceError", "bundled_seed"]
