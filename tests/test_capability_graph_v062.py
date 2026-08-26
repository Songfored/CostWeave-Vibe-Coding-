import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from costweave.capability_graph_store import CapabilityGraphStore, CapabilityGraphStoreError
from costweave.capability_graph_service import CONFIRM_TOKEN, CapabilityGraphService, CapabilityGraphServiceError
from costweave.intelligence.capability_graph import (
    CapabilityCalibrator,
    CapabilityEvidenceV1,
    CapabilityGraphRouter,
    CapabilityNodeV1,
    CapabilityPriorV1,
    RouteCandidateV1,
    RouteContextV1,
    detect_drift,
)
from costweave.intelligence.common import NonNegativeEstimate, ProbabilityInterval


NOW = "2026-08-09T00:00:00Z"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def evidence(*, project="local-default", observed_at=NOW, model="model@1", successes=8, failures=2, correlated=0, validator="validator:none"):
    return CapabilityEvidenceV1(
        evidence_id=f"evidence-{model}-{observed_at}", project_id=project, task_family="code", model_revision=model,
        reasoning_tier="medium", provider_deployment="local:default", source_kind="controlled_experiment",
        source_id=f"fixture-{model}", source_version="1", selection_mechanism="deterministic_fixture",
        evidence_level="E0-simulated", failure_correlation_group="family-a", successes=successes, failures=failures,
        retry_events=1 if successes + failures else 0, escalation_events=0, rework_events=1,
        correlated_failure_events=correlated, cost_usd=NonNegativeEstimate(mean=.01, p95=.02),
        latency_ms=NonNegativeEstimate(mean=100, p95=200), observed_at=observed_at,
        source_digest=digest(f"fixture-{model}"), validator_id=validator,
    )


def candidate(route="route-a", model="model@1", validator="validator:none", group="family-a"):
    return RouteCandidateV1(route_id=route, model_revision=model, reasoning_tier="medium", provider_deployment="local:default", validator_id=validator, failure_correlation_group=group, base_cost_usd=NonNegativeEstimate(mean=.01, p95=.02), base_latency_ms=NonNegativeEstimate(mean=100, p95=200))


class CapabilityContractTests(unittest.TestCase):
    def test_evidence_round_trip_and_condition_key(self):
        item = evidence()
        self.assertEqual(item, CapabilityEvidenceV1.from_dict(item.to_dict()))
        self.assertEqual(10, item.sample_count)
        self.assertEqual(item.condition_key, CapabilityEvidenceV1.from_dict({**item.to_dict(), "condition_key": item.condition_key}).condition_key)
        with self.assertRaises(ValueError):
            CapabilityEvidenceV1.from_dict({**item.to_dict(), "sample_count": 9})

    def test_evidence_rejects_invalid_provenance_and_counts(self):
        item = evidence()
        with self.assertRaises(ValueError):
            CapabilityEvidenceV1.from_dict({**item.to_dict(), "source_digest": "bad"})
        with self.assertRaises(ValueError):
            CapabilityEvidenceV1.from_dict({**item.to_dict(), "successes": 0, "failures": 0})

    def test_cold_start_prior_is_wide_and_not_real_calibration(self):
        posterior = CapabilityCalibrator().posterior(project_id="local-default", task_family="code", model_revision="new@1", reasoning_tier="medium", provider_deployment="local:default", validator_id="validator:none", tool_ids=(), modality_ids=(), evidence=(), now=NOW, prior=CapabilityPriorV1())
        self.assertEqual("prior", posterior.calibration_status)
        self.assertEqual(0, posterior.sample_count)
        self.assertGreaterEqual(posterior.success.lower, 0.0)
        self.assertGreater(posterior.success.upper - posterior.success.lower, .6)

    def test_calibration_is_condition_specific_and_reproducible(self):
        items = (evidence(), evidence(model="other@1"))
        calibrator = CapabilityCalibrator()
        kwargs = dict(project_id="local-default", task_family="code", model_revision="model@1", reasoning_tier="medium", provider_deployment="local:default", validator_id="validator:none", tool_ids=(), modality_ids=(), evidence=items, now=NOW)
        first = calibrator.posterior(**kwargs)
        second = calibrator.posterior(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(10, first.sample_count)
        self.assertEqual(("fixture-model@1",), first.source_ids)

    def test_stale_evidence_is_flagged_and_drift_freezes(self):
        old = evidence(observed_at="2025-01-01T00:00:00Z")
        posterior = CapabilityCalibrator(stale_after_days=30).posterior(project_id="local-default", task_family="code", model_revision="model@1", reasoning_tier="medium", provider_deployment="local:default", validator_id="validator:none", tool_ids=(), modality_ids=(), evidence=(old,), now=NOW)
        self.assertTrue(posterior.stale)
        self.assertEqual("stale", posterior.calibration_status)
        report = detect_drift(baseline={"task_distribution": 1, "price": 1, "latency": 100, "error_rate": .1, "model_revision": "a"}, current={"task_distribution": 2, "price": 2, "latency": 250, "error_rate": .5, "model_revision": "b"}, now=NOW)
        self.assertTrue(report.freeze_candidate)
        self.assertIn("model_revision_drift", report.reason_codes)

    def test_router_routes_low_risk_but_escalates_ood_and_high_risk(self):
        router = CapabilityGraphRouter()
        context = RouteContextV1(project_id="local-default", task_family="code", risk_level="low", ood_score=.01, deadline_remaining_ms=1000, budget_remaining_usd=.1)
        decision, posteriors = router.decide(context=context, candidates=(candidate(),), evidence=(evidence(),), now=NOW, decision_id="decision-low")
        self.assertEqual("ROUTE", decision.action)
        self.assertEqual("route-a", decision.selected_route_id)
        self.assertEqual(1, len(posteriors))
        ood = RouteContextV1(project_id="local-default", task_family="code", risk_level="high", ood_score=.9, deadline_remaining_ms=1000, budget_remaining_usd=.1)
        decision, _ = router.decide(context=ood, candidates=(candidate(),), evidence=(evidence(),), now=NOW, decision_id="decision-ood")
        self.assertEqual("ESCALATE", decision.action)
        self.assertIn("ood_task", decision.reason_codes)

    def test_router_requires_independent_validator_when_requested(self):
        router = CapabilityGraphRouter()
        context = RouteContextV1(project_id="local-default", task_family="code", risk_level="medium", independent_validator_required=True)
        decision, _ = router.decide(context=context, candidates=(candidate(),), evidence=(evidence(),), now=NOW, decision_id="decision-validator")
        self.assertEqual("ESCALATE", decision.action)
        self.assertIn("independent_validator_missing", decision.reason_codes)


class CapabilityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = CapabilityGraphStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_evidence_and_decision_are_idempotent_and_project_scoped(self):
        item = evidence()
        request_hash = digest("request")
        self.assertTrue(self.store.record_evidence(item, idempotency_key="evidence-key-v062", request_hash=request_hash))
        self.assertFalse(self.store.record_evidence(item, idempotency_key="evidence-key-v062", request_hash=request_hash))
        self.assertEqual(1, len(self.store.evidence(project_id="local-default")))
        router = CapabilityGraphRouter()
        context = RouteContextV1(project_id="local-default", task_family="code")
        decision, _ = router.decide(context=context, candidates=(candidate(),), evidence=(item,), now=NOW, decision_id="route-decision-v062")
        self.assertTrue(self.store.record_decision(decision, idempotency_key="route-key-v062", request_hash=request_hash))
        self.assertFalse(self.store.record_decision(decision, idempotency_key="route-key-v062", request_hash=request_hash))
        self.assertIsNone(self.store.decision(project_id="other-project", decision_id=decision.decision_id))

    def test_hash_tampering_and_immutable_tables_fail_closed(self):
        item = evidence()
        self.store.record_evidence(item)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE capability_evidence_v062 SET evidence_json='{}' WHERE evidence_id=?", (item.evidence_id,))
        finally:
            connection.close()
        self.assertEqual(item, self.store.evidence(project_id="local-default")[0])

    def test_service_requires_confirmation_and_preserves_no_calls(self):
        service = CapabilityGraphService(self.store)
        with self.assertRaises(CapabilityGraphServiceError):
            service.ingest(project_id="local-default", idempotency_key="bad-key-v062", payload={})
        result, replayed = service.ingest(project_id="local-default", idempotency_key="good-key-v062", payload={"confirm": CONFIRM_TOKEN, "evidence": evidence().to_dict()})
        self.assertFalse(replayed)
        self.assertEqual(0, result["provider_calls"])
        self.assertEqual("advisory-only", service.status()["runtime_authority"])


if __name__ == "__main__":
    unittest.main()
