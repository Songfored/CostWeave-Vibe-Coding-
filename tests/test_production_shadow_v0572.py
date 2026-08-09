from __future__ import annotations

import unittest
from dataclasses import replace
import sqlite3
import tempfile

from costweave.event_store import EventStore
from costweave.production_shadow import (
    ProductionLikeShadowEngine,
    ProductionShadowCandidate,
    ProductionShadowDecision,
    ProductionShadowError,
    ProductionShadowFailureReceipt,
    ProductionShadowInput,
    ProductionShadowPolicy,
    ShadowActualOutcome,
    build_production_shadow_audit,
    build_shadow_comparison,
    public_production_shadow_policy,
)
from costweave.production_shadow_store import ProductionShadowStore


def shadow_input(
    index: int = 1,
    *,
    age_ms: float = 10.0,
    evidence: str = "E0-fixture",
    origin: str = "test_fixture",
) -> ProductionShadowInput:
    return ProductionShadowInput.create(
        anonymous_task_id=f"task_{index:032x}",
        task_feature_schema_version="outcome-features-v1",
        task_features={
            "task_family": "code",
            "difficulty_lower": 0.4,
            "difficulty_upper": 0.6,
            "required_capabilities": ["coding"],
        },
        catalog_revision="catalog-r1",
        catalog_snapshot_hash="a" * 64,
        capacity_revision="capacity-r1",
        capacity_snapshot_hash="b" * 64,
        capacity_captured_at="2026-08-04T00:00:00Z",
        capacity_age_ms=age_ms,
        capacity_freshness_limit_ms=30_000.0,
        actual_route_id="balanced",
        actual_route_fingerprint="c" * 64,
        baseline_predicted_quality=0.85,
        baseline_predicted_total_cost_usd=0.40,
        baseline_predicted_latency_p95_ms=400.0,
        baseline_end_to_end_estimated_cost_usd=1.0,
        baseline_end_to_end_estimated_latency_ms=1_000.0,
        end_to_end_cost_budget_usd=1.0,
        end_to_end_latency_budget_ms=1_000.0,
        risk_level="low",
        reversible=True,
        permission_sensitive=False,
        evidence_level=evidence,
        data_origin=origin,
    )


def candidates(
    *,
    capacity_hash: str = "b" * 64,
    feasible: bool = True,
) -> tuple[ProductionShadowCandidate, ...]:
    return (
        ProductionShadowCandidate(
            "cheap", "d" * 64, capacity_hash, 0.80, 0.20, 300, 0.50,
            feasible, feasible,
        ),
        ProductionShadowCandidate(
            "balanced", "e" * 64, capacity_hash, 0.90, 0.40, 400, 0.30,
            feasible, feasible,
        ),
        ProductionShadowCandidate(
            "fast", "f" * 64, capacity_hash, 0.82, 0.50, 200, 0.40,
            feasible, feasible,
        ),
    )


def policy(rule: str) -> ProductionShadowPolicy:
    return ProductionShadowPolicy(
        policy_id=rule,
        policy_version="v1",
        selection_rule=rule,
        frozen_route_scores=(
            {"cheap": 0.1, "balanced": 0.3, "fast": 0.2}
            if rule == "frozen_score"
            else None
        ),
    )


def decision(
    item: ProductionShadowInput,
    *,
    rule: str = "minimum_expected_loss",
    compute_cost: float = 0.01,
    compute_latency: float = 20.0,
) -> ProductionShadowDecision:
    return ProductionLikeShadowEngine().observe(
        item,
        candidates(capacity_hash=item.capacity_snapshot_hash),
        policy(rule),
        shadow_compute_cost_usd=compute_cost,
        shadow_compute_latency_ms=compute_latency,
    )


def outcome(
    item: ProductionShadowInput,
    index: int = 1,
    *,
    evidence: str = "E0-fixture",
    origin: str = "test_fixture",
    independent: bool = False,
) -> ShadowActualOutcome:
    return ShadowActualOutcome(
        outcome_id=f"shadowout_{index:032x}",
        input_id=item.input_id,
        actual_route_id=item.actual_route_id,
        accepted=True,
        actual_total_cost_usd=0.42,
        actual_latency_ms=430.0,
        capacity_admission_correct=True,
        evidence_level=evidence,
        data_origin=origin,
        independently_validated=independent,
    )


class ProductionShadowInputTests(unittest.TestCase):
    def test_input_is_deterministic_and_matches_baseline_snapshots(self):
        left = shadow_input()
        right = shadow_input()
        self.assertEqual(left, right)
        self.assertTrue(left.same_features_as_baseline)
        self.assertTrue(left.same_catalog_as_baseline)
        self.assertTrue(left.same_capacity_as_baseline)
        self.assertFalse(left.routing_authority)

    def test_raw_prompts_identifiers_and_secrets_are_rejected(self):
        for features in (
            {"prompt": "write code"},
            {"user_id": "person"},
            {"safe": "api_key=secret"},
        ):
            with self.subTest(features=features), self.assertRaises(ProductionShadowError):
                ProductionShadowInput.create(
                    **{
                        key: value
                        for key, value in shadow_input().payload().items()
                        if key not in {
                            "task_features", "task_feature_hash",
                            "same_features_as_baseline", "same_catalog_as_baseline",
                            "same_capacity_as_baseline", "advisory_only",
                            "routing_authority", "may_change_actual_route",
                            "schema_version",
                        }
                    },
                    task_features=features,
                )

    def test_stale_capacity_is_explicit(self):
        item = shadow_input(age_ms=30_001)
        result = decision(item)
        self.assertEqual("advice_missing", result.state)
        self.assertEqual("capacity_snapshot_stale", result.missing_reason)
        self.assertTrue(result.baseline_execution_continues)


class ProductionShadowDecisionTests(unittest.TestCase):
    def test_selection_rules_choose_expected_routes(self):
        item = shadow_input()
        expected = {
            "minimum_expected_loss": "balanced",
            "minimum_cost": "cheap",
            "maximum_quality_lower": "balanced",
            "minimum_latency_p95": "fast",
            "frozen_score": "cheap",
        }
        for rule, route_id in expected.items():
            with self.subTest(rule=rule):
                result = decision(item, rule=rule)
                self.assertEqual(route_id, result.selected_route_id)
                self.assertEqual(result, decision(item, rule=rule))

    def test_advice_never_changes_actual_route_or_creates_side_effects(self):
        result = decision(shadow_input(), rule="minimum_cost")
        self.assertEqual("balanced", result.actual_route_id)
        self.assertEqual("cheap", result.selected_route_id)
        self.assertFalse(result.actual_route_changed)
        self.assertEqual(0, result.execution_side_effect_count)
        self.assertEqual(0, result.external_call_count)
        self.assertFalse(result.suggestion["applied_to_actual_route"])

    def test_candidates_must_use_the_baseline_capacity_snapshot(self):
        item = shadow_input()
        with self.assertRaises(ProductionShadowError):
            ProductionLikeShadowEngine().observe(
                item,
                candidates(capacity_hash="9" * 64),
                policy("minimum_cost"),
                shadow_compute_cost_usd=0.01,
                shadow_compute_latency_ms=20,
            )

    def test_missing_reasons_cover_empty_infeasible_budget_and_deadline(self):
        item = shadow_input()
        engine = ProductionLikeShadowEngine()
        cases = (
            ((), 0.01, 20, "no_candidates"),
            (candidates(feasible=False), 0.01, 20, "no_feasible_candidate"),
            (candidates(), 1.01, 20, "shadow_budget_exceeded"),
            (candidates(), 0.01, 1001, "shadow_deadline_exceeded"),
        )
        for routes, cost, latency, expected in cases:
            with self.subTest(expected=expected):
                result = engine.observe(
                    item,
                    routes,
                    policy("minimum_cost"),
                    shadow_compute_cost_usd=cost,
                    shadow_compute_latency_ms=latency,
                )
                self.assertEqual(expected, result.missing_reason)

    def test_cost_and_latency_overhead_are_measured(self):
        result = decision(shadow_input(), compute_cost=0.10, compute_latency=100)
        self.assertEqual(0.10, result.cost_overhead_ratio)
        self.assertEqual(0.10, result.latency_overhead_ratio)
        self.assertTrue(result.cost_overhead_principle_passed)
        exceeded = decision(shadow_input(), compute_cost=0.11)
        self.assertFalse(exceeded.cost_overhead_principle_passed)

    def test_failure_isolation_keeps_baseline_running_and_hides_message(self):
        item = shadow_input()

        def broken(*args, **kwargs):
            raise RuntimeError("secret internal detail")

        result = ProductionLikeShadowEngine().observe_isolated(
            item,
            candidates(),
            policy("minimum_cost"),
            shadow_compute_cost_usd=0.01,
            shadow_compute_latency_ms=5,
            observer=broken,
        )
        self.assertIsInstance(result, ProductionShadowFailureReceipt)
        self.assertEqual("shadow_internal_error", result.error_code)
        self.assertTrue(result.baseline_execution_continues)
        self.assertFalse(result.error_message_persisted)
        self.assertNotIn("secret", str(result.to_dict()))

    def test_authority_contradictions_are_rejected(self):
        with self.assertRaises(ProductionShadowError):
            replace(decision(shadow_input()), actual_route_changed=True)


class ProductionShadowComparisonTests(unittest.TestCase):
    def test_same_route_observes_outcome_but_makes_no_savings_claim(self):
        item = shadow_input()
        report = build_shadow_comparison(item, decision(item), outcome(item))
        self.assertTrue(report.same_route)
        self.assertTrue(report.counterfactual_outcome_known)
        self.assertEqual(0.02, report.baseline_cost_error_usd)
        self.assertEqual(30.0, report.baseline_latency_error_ms)
        self.assertFalse(report.counterfactual_savings_claim_allowed)

    def test_different_suggested_route_keeps_counterfactual_unknown(self):
        item = shadow_input()
        report = build_shadow_comparison(
            item, decision(item, rule="minimum_cost"), outcome(item)
        )
        self.assertFalse(report.same_route)
        self.assertFalse(report.counterfactual_outcome_known)
        self.assertFalse(report.actual_route_changed)

    def test_mismatched_actual_route_is_rejected(self):
        item = shadow_input()
        with self.assertRaises(ProductionShadowError):
            build_shadow_comparison(
                item,
                decision(item),
                replace(outcome(item), actual_route_id="different"),
            )


class ProductionShadowAuditTests(unittest.TestCase):
    def test_e0_batch_reports_coverage_missing_reasons_and_blocked_evidence(self):
        first = shadow_input(1)
        second = shadow_input(2, age_ms=30_001)
        first_decision = decision(first, rule="minimum_cost")
        second_decision = decision(second)
        report = build_production_shadow_audit(
            (first, second),
            (first_decision, second_decision),
            (),
            (build_shadow_comparison(first, first_decision, outcome(first, 1)),),
        )
        self.assertEqual(0.5, report.coverage_ratio)
        self.assertEqual(1, report.missing_reason_counts["capacity_snapshot_stale"])
        self.assertEqual(1, report.counterfactual_unknown_count)
        self.assertEqual("blocked_evidence", report.state)
        self.assertFalse(report.directional_evidence_eligible)

    def test_failure_receipts_count_as_isolated_missing_work(self):
        item = shadow_input()

        def broken(*args, **kwargs):
            raise RuntimeError("failure")

        failed = ProductionLikeShadowEngine().observe_isolated(
            item,
            candidates(),
            policy("minimum_cost"),
            shadow_compute_cost_usd=0.01,
            shadow_compute_latency_ms=10,
            observer=broken,
        )
        report = build_production_shadow_audit((item,), (), (failed,), ())
        self.assertEqual(1, report.failure_count)
        self.assertEqual(1, report.missing_reason_counts["shadow_internal_error"])
        self.assertTrue(report.checks["all_failures_isolated"])
        self.assertTrue(report.baseline_execution_continuity_proven)

    def test_thirty_real_complete_cases_allow_directional_shadow_audit(self):
        inputs = tuple(
            shadow_input(
                index + 1,
                evidence="E3-real",
                origin="human_benchmark",
            )
            for index in range(30)
        )
        decisions = tuple(decision(item) for item in inputs)
        comparisons = tuple(
            build_shadow_comparison(
                item,
                selected,
                outcome(
                    item,
                    index + 1,
                    evidence="E3-real",
                    origin="human_benchmark",
                    independent=True,
                ),
            )
            for index, (item, selected) in enumerate(zip(inputs, decisions))
        )
        report = build_production_shadow_audit(inputs, decisions, (), comparisons)
        self.assertEqual("shadow_directional", report.state)
        self.assertTrue(report.directional_evidence_eligible)
        self.assertTrue(report.cost_overhead_principle_passed)
        self.assertEqual(1.0, report.comparison_coverage_ratio)
        self.assertFalse(report.policy_promotion_allowed)

    def test_directional_batch_exposes_overhead_failure(self):
        inputs = tuple(
            shadow_input(index + 1, evidence="E3-real", origin="production")
            for index in range(30)
        )
        decisions = tuple(
            decision(item, compute_cost=0.11) for item in inputs
        )
        comparisons = tuple(
            build_shadow_comparison(
                item,
                selected,
                outcome(
                    item,
                    index + 1,
                    evidence="E3-real",
                    origin="production",
                    independent=True,
                ),
            )
            for index, (item, selected) in enumerate(zip(inputs, decisions))
        )
        report = build_production_shadow_audit(inputs, decisions, (), comparisons)
        self.assertEqual("shadow_overhead_exceeded", report.state)
        self.assertFalse(report.cost_overhead_principle_passed)
        self.assertGreater(report.cost_overhead_ratio_p95, 0.10)

    def test_every_input_requires_exactly_one_result(self):
        item = shadow_input()
        with self.assertRaises(ProductionShadowError):
            build_production_shadow_audit((item,), (), (), ())

    def test_public_policy_discloses_zero_authority_and_evidence_gate(self):
        value = public_production_shadow_policy()
        self.assertEqual("v0.5.7.2", value["schema_version"])
        self.assertTrue(value["same_capacity_snapshot_required"])
        self.assertEqual(0.10, value["overhead"]["maximum_end_to_end_cost_ratio"])
        self.assertEqual("unknown", value["counterfactual_outcome_default"])
        self.assertFalse(value["routing_authority"])
        self.assertFalse(value["policy_promotion_allowed"])
        self.assertFalse(value["commercial_claims_allowed"])


class ProductionShadowPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = ProductionShadowStore(self.path)
        self.input = shadow_input()
        self.decision = decision(self.input, rule="minimum_cost")
        self.outcome = outcome(self.input)
        self.comparison = build_shadow_comparison(
            self.input, self.decision, self.outcome
        )
        self.audit = build_production_shadow_audit(
            (self.input,),
            (self.decision,),
            (),
            (self.comparison,),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_and_empty_registry(self):
        self.assertEqual("v6.1.0", self.health["runtime_schema"])
        self.assertEqual(31, self.health["migrations"])
        counts = self.store.counts()
        self.assertEqual(0, counts["production_shadow_inputs"])
        self.assertEqual(0, counts["production_shadow_route_interventions"])

    def test_records_are_linked_idempotent_and_integrity_checked(self):
        self.assertTrue(self.store.record_input(self.input))
        self.assertFalse(self.store.record_input(self.input))
        self.assertTrue(self.store.record_decision(self.decision))
        self.assertFalse(self.store.record_decision(self.decision))
        self.assertTrue(self.store.record_outcome(self.outcome))
        self.assertFalse(self.store.record_outcome(self.outcome))
        self.assertTrue(self.store.record_comparison(self.comparison))
        self.assertFalse(self.store.record_comparison(self.comparison))
        self.assertTrue(self.store.record_audit(self.audit))
        self.assertFalse(self.store.record_audit(self.audit))
        self.assertEqual(
            self.audit.report_id,
            self.store.audit(self.audit.report_id)["report_id"],
        )
        counts = self.store.counts()
        self.assertEqual(1, counts["production_shadow_decisions"])
        self.assertEqual(1, counts["production_shadow_comparisons"])
        self.assertEqual(0, counts["production_shadow_execution_side_effects"])

    def test_linked_evidence_and_exclusive_result_are_required(self):
        with self.assertRaises(ProductionShadowError):
            self.store.record_decision(self.decision)
        self.store.record_input(self.input)
        self.store.record_decision(self.decision)

        def broken(*args, **kwargs):
            raise RuntimeError("fail")

        failure = ProductionLikeShadowEngine().observe_isolated(
            self.input,
            candidates(),
            policy("minimum_cost"),
            shadow_compute_cost_usd=0.01,
            shadow_compute_latency_ms=5,
            observer=broken,
        )
        with self.assertRaises(ProductionShadowError):
            self.store.record_failure(failure)
        with self.assertRaises(ProductionShadowError):
            self.store.record_audit(self.audit)

    def test_failure_can_be_persisted_without_affecting_baseline(self):
        self.store.record_input(self.input)

        def broken(*args, **kwargs):
            raise RuntimeError("private detail")

        failure = ProductionLikeShadowEngine().observe_isolated(
            self.input,
            candidates(),
            policy("minimum_cost"),
            shadow_compute_cost_usd=0.01,
            shadow_compute_latency_ms=5,
            observer=broken,
        )
        self.assertTrue(self.store.record_failure(failure))
        self.assertEqual(1, self.store.counts()["production_shadow_failures"])
        connection = sqlite3.connect(self.path)
        try:
            raw = connection.execute(
                "SELECT failure_json FROM production_shadow_failures"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("private detail", raw)

    def test_database_rejects_mutation_deletion_and_authority(self):
        self.store.record_input(self.input)
        self.store.record_decision(self.decision)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE production_shadow_decisions SET routing_authority=1 WHERE decision_id=?",
                    (self.decision.decision_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM production_shadow_inputs WHERE input_id=?",
                    (self.input.input_id,),
                )
        finally:
            connection.close()

    def test_tampered_persisted_payload_is_detected(self):
        self.store.record_input(self.input)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER trg_production_shadow_input_immutable")
            connection.execute(
                "UPDATE production_shadow_inputs SET input_json='{}' WHERE input_id=?",
                (self.input.input_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ProductionShadowError):
            self.store.shadow_input(self.input.input_id)


if __name__ == "__main__":
    unittest.main()
