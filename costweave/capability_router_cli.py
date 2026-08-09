"""Offline v0.6.2 capability graph and calibrated router demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from .capability_graph_service import CONFIRM_TOKEN, CapabilityGraphService
from .capability_graph_store import CapabilityGraphStore
from .intelligence.capability_graph import (
    CapabilityEvidenceV1,
    RouteCandidateV1,
    RouteContextV1,
)
from .intelligence.common import NonNegativeEstimate


NOW = "2026-08-09T00:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def demo_evidence(*, project_id: str = "local-default") -> CapabilityEvidenceV1:
    return CapabilityEvidenceV1(
        evidence_id="demo-capability-evidence-v062",
        project_id=project_id,
        task_family="code",
        model_revision="local-model@2026-08",
        reasoning_tier="medium",
        provider_deployment="local:default",
        source_kind="controlled_experiment",
        source_id="offline-fixture-v062",
        source_version="1",
        selection_mechanism="deterministic_fixture",
        evidence_level="E0-simulated",
        failure_correlation_group="local-model-family",
        successes=8,
        failures=2,
        retry_events=1,
        escalation_events=0,
        rework_events=1,
        correlated_failure_events=0,
        cost_usd=NonNegativeEstimate(mean=0.001, p95=0.002),
        latency_ms=NonNegativeEstimate(mean=120, p95=240),
        observed_at=NOW,
        source_digest=_digest("offline-fixture-v062"),
    )


def demo_payload(*, project_id: str = "local-default") -> dict:
    context = RouteContextV1(project_id=project_id, task_family="code", risk_level="low", ood_score=0.02, deadline_remaining_ms=1000, budget_remaining_usd=0.05)
    candidates = (
        RouteCandidateV1(route_id="local-medium", model_revision="local-model@2026-08", reasoning_tier="medium", provider_deployment="local:default", base_cost_usd=NonNegativeEstimate(mean=0.001, p95=0.002), base_latency_ms=NonNegativeEstimate(mean=120, p95=240), failure_correlation_group="local-model-family"),
        RouteCandidateV1(route_id="strong-fallback", model_revision="strong-model@2026-08", reasoning_tier="high", provider_deployment="offline:strong", base_cost_usd=NonNegativeEstimate(mean=0.02, p95=0.03), base_latency_ms=NonNegativeEstimate(mean=350, p95=600), failure_correlation_group="strong-model-family"),
    )
    return {"confirm": CONFIRM_TOKEN, "context": context.to_dict(), "candidates": [item.to_dict() for item in candidates], "now": NOW, "calibration_set_id": "offline-fixture"}


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="costweave-v062-router-") as directory:
        service = CapabilityGraphService(CapabilityGraphStore(Path(directory) / "runtime.sqlite3"))
        evidence = demo_evidence()
        service.ingest(project_id="local-default", idempotency_key="cli-evidence-v062", payload={"confirm": CONFIRM_TOKEN, "evidence": evidence.to_dict()})
        result, _ = service.decide(project_id="local-default", idempotency_key="cli-route-v062", payload=demo_payload())
        return {"status": service.status(), "decision": result}


def main() -> int:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.2 offline capability evidence graph router")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

