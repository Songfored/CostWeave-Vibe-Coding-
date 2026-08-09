from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from costweave.event_store import EventStore
from costweave.policy_registry import (
    PolicyManifest,
    PolicyRegistryError,
    PromotionEvidence,
    build_policy_card,
    build_registry_rollback,
    evaluate_promotion,
    freeze_policy,
    public_policy_registry_policy,
    transition_policy,
)
from costweave.policy_registry_store import PolicyRegistryStore


KEY = b"registry-test-key-material-32bytes!!"
OTHER_KEY = b"different-test-key-material-32bytes"
NOW = "2026-08-04T00:00:00+00:00"


def manifest(
    name: str = "candidate",
    version: str = "1.0.0",
    *,
    rollback_name: str = "baseline",
    rollback_version: str = "0.9.0",
    multiplanner: bool = False,
) -> PolicyManifest:
    return PolicyManifest.create(
        policy_name=name,
        policy_version=version,
        source_candidate_id="candidate-evidence",
        source_replay_report_id="replay-evidence",
        source_shadow_report_id="shadow-evidence",
        code_revision="git-abc123",
        objective_revision="cps-v1",
        constraint_revision="hard-constraints-v1",
        allowed_task_families=("code", "research"),
        prohibited_task_families=("critical-security",),
        rollback_policy_name=rollback_name,
        rollback_policy_version=rollback_version,
        multiplanner_default_requested=multiplanner,
        signer_key_id="test-key-1",
        signing_key=KEY,
        created_at=NOW,
    )


def evidence(
    item: PolicyManifest,
    *,
    level: str = "E4",
    origin: str = "production",
    cases: int = 1_000,
    independent: bool = True,
    final_lower: float = -1.0,
    high_risk: float = 0.0,
    cps_lower: float = 15.1,
    p95: float = 15.0,
    handoff: float = 0.019,
    shadow_overhead: float = 0.10,
    difficult_delta: float = 0.0,
    regret: float = 0.0,
    canary: bool = True,
    hard_budget: int = 0,
    quality_baseline: str = "strongest-feasible-model",
    cps_baseline: str = "v0.4-fixed-policy",
) -> PromotionEvidence:
    return PromotionEvidence.create(
        registry_policy_id=item.registry_policy_id,
        policy_version=item.policy_version,
        cohort_hash=hashlib.sha256(
            f"{item.registry_policy_id}:{cases}:{cps_lower}:{origin}".encode()
        ).hexdigest(),
        quality_baseline_id=quality_baseline,
        cps_baseline_id=cps_baseline,
        evidence_level=level,
        data_origin=origin,
        independent_evaluation=independent,
        case_count=cases,
        offline_replay_passed=True,
        production_shadow_passed=True,
        shadow_cost_overhead_p95_ratio=shadow_overhead,
        canary_evidence_present=canary,
        final_success_delta_pp=0.5,
        final_success_delta_ci95_lower_pp=final_lower,
        high_risk_success_delta_pp=high_risk,
        cps_improvement_pct=20.0,
        cps_improvement_ci95_lower_pct=cps_lower,
        parallel_p95_improvement_pct=p95,
        handoff_rework_rate=handoff,
        hard_budget_violation_count=hard_budget,
        permission_violation_count=0,
        concurrency_violation_count=0,
        call_count_violation_count=0,
        calibration_passed=True,
        data_quality_passed=True,
        privacy_passed=True,
        drift_passed=True,
        multiplanner_default_requested=item.multiplanner_default_requested,
        difficult_success_delta_pp=difficult_delta,
        routing_regret_improvement_pct=regret,
        created_at=NOW,
    )


class PolicyRegistryContractTests(unittest.TestCase):
    def test_signed_manifest_is_versioned_scoped_and_runtime_inert(self):
        item = manifest()
        self.assertTrue(item.verify_signature(KEY))
        self.assertFalse(item.verify_signature(OTHER_KEY))
        self.assertEqual("draft", item.initial_state)
        self.assertFalse(item.routing_authority)
        self.assertFalse(item.runtime_activation_executed)
        self.assertFalse(item.commercial_claims_allowed)

    def test_manifest_tampering_and_short_key_are_rejected(self):
        item = manifest()
        with self.assertRaises(PolicyRegistryError):
            replace(item, objective_revision="changed")
        with self.assertRaises(PolicyRegistryError):
            PolicyManifest.create(
                policy_name="bad",
                policy_version="1",
                source_candidate_id="c",
                source_replay_report_id="r",
                source_shadow_report_id="s",
                code_revision="g",
                objective_revision="o",
                constraint_revision="q",
                allowed_task_families=("code",),
                prohibited_task_families=(),
                rollback_policy_name="base",
                rollback_policy_version="0",
                multiplanner_default_requested=False,
                signer_key_id="key",
                signing_key=b"short",
                created_at=NOW,
            )

    def test_simulated_or_dependent_evidence_cannot_pass_evaluation(self):
        item = manifest()
        report = evaluate_promotion(
            item,
            evidence(item, origin="simulated", independent=False),
            stage="evaluation",
            evaluated_at=NOW,
        )
        self.assertFalse(report.eligible)
        self.assertIn("real_data_origin", report.reasons)
        self.assertIn("independent_evaluation", report.reasons)
        self.assertFalse(report.may_change_actual_route)

    def test_all_formal_canary_boundaries_pass(self):
        item = manifest()
        report = evaluate_promotion(
            item, evidence(item), stage="canary", evaluated_at=NOW
        )
        self.assertTrue(report.eligible)
        self.assertTrue(report.checks["final_success_non_inferiority_ci95"])
        self.assertTrue(report.checks["parallel_p95_improvement"])
        self.assertTrue(report.checks["handoff_rework_rate"])
        with self.assertRaises(TypeError):
            report.checks["handoff_rework_rate"] = False

    def test_strict_cps_and_handoff_boundaries_fail(self):
        item = manifest()
        cps = evaluate_promotion(
            item,
            evidence(item, cps_lower=15.0),
            stage="canary",
            evaluated_at=NOW,
        )
        handoff = evaluate_promotion(
            item,
            evidence(item, handoff=0.02),
            stage="canary",
            evaluated_at=NOW,
        )
        self.assertIn("cps_improvement_ci95", cps.reasons)
        self.assertIn("handoff_rework_rate", handoff.reasons)

    def test_quality_risk_latency_and_hard_violation_fail_closed(self):
        item = manifest()
        report = evaluate_promotion(
            item,
            evidence(
                item,
                final_lower=-1.01,
                high_risk=-0.01,
                p95=14.99,
                hard_budget=1,
            ),
            stage="canary",
            evaluated_at=NOW,
        )
        self.assertEqual(
            {
                "final_success_non_inferiority_ci95",
                "high_risk_success_non_decline",
                "parallel_p95_improvement",
                "hard_budget_violations",
            },
            set(report.reasons),
        )

    def test_required_baselines_cannot_be_substituted(self):
        item = manifest()
        report = evaluate_promotion(
            item,
            evidence(
                item,
                quality_baseline="cheap-model",
                cps_baseline="unversioned-policy",
            ),
            stage="canary",
            evaluated_at=NOW,
        )
        self.assertIn("strongest_feasible_quality_baseline", report.reasons)
        self.assertIn("v0_4_fixed_cps_baseline", report.reasons)

    def test_multiplanner_default_requires_success_or_regret_value(self):
        item = manifest(multiplanner=True)
        blocked = evaluate_promotion(
            item, evidence(item), stage="canary", evaluated_at=NOW
        )
        success = evaluate_promotion(
            item,
            evidence(item, difficult_delta=5.0),
            stage="canary",
            evaluated_at=NOW,
        )
        regret = evaluate_promotion(
            item,
            evidence(item, regret=15.0),
            stage="canary",
            evaluated_at=NOW,
        )
        self.assertIn("multiplanner_default_value", blocked.reasons)
        self.assertTrue(success.eligible)
        self.assertTrue(regret.eligible)

    def test_stage_specific_evidence_and_case_floors(self):
        item = manifest()
        evaluation_report = evaluate_promotion(
            item,
            evidence(item, level="E2", cases=30, canary=False),
            stage="evaluation",
            evaluated_at=NOW,
        )
        shadow_report = evaluate_promotion(
            item,
            evidence(item, level="E3", cases=30, canary=False),
            stage="shadow",
            evaluated_at=NOW,
        )
        activation_report = evaluate_promotion(
            item,
            evidence(item, level="E3", cases=999, canary=False),
            stage="activation",
            evaluated_at=NOW,
        )
        self.assertTrue(evaluation_report.eligible)
        self.assertTrue(shadow_report.eligible)
        self.assertFalse(activation_report.eligible)
        self.assertIn("evidence_level", activation_report.reasons)
        self.assertIn("minimum_case_count", activation_report.reasons)
        self.assertIn("canary_evidence", activation_report.reasons)

    def test_state_machine_requires_order_gate_and_approval(self):
        item = manifest()
        ev = evidence(item)
        evaluation_gate = evaluate_promotion(item, ev, stage="evaluation", evaluated_at=NOW)
        first = transition_policy(
            item,
            current_state="draft",
            current_revision=0,
            target_state="evaluated",
            signature_verified=True,
            gate_report=evaluation_gate,
            transitioned_at=NOW,
        )
        self.assertEqual(1, first.to_revision)
        with self.assertRaises(PolicyRegistryError):
            transition_policy(
                item,
                current_state="evaluated",
                current_revision=1,
                target_state="canary",
                signature_verified=True,
                gate_report=evaluate_promotion(item, ev, stage="canary", evaluated_at=NOW),
                approval_id="approval",
            )
        with self.assertRaises(PolicyRegistryError):
            transition_policy(
                item,
                current_state="shadow",
                current_revision=2,
                target_state="canary",
                signature_verified=True,
                gate_report=evaluate_promotion(item, ev, stage="canary", evaluated_at=NOW),
            )

    def test_active_is_registry_pointer_only(self):
        item = manifest()
        gate = evaluate_promotion(item, evidence(item), stage="activation", evaluated_at=NOW)
        receipt = transition_policy(
            item,
            current_state="canary",
            current_revision=3,
            target_state="active",
            signature_verified=True,
            gate_report=gate,
            approval_id="human-approval-1",
            transitioned_at=NOW,
        )
        self.assertTrue(receipt.registry_pointer_changed)
        self.assertFalse(receipt.runtime_route_changed)
        self.assertEqual(0, receipt.execution_side_effect_count)

    def test_policy_card_explains_failed_gate_without_authority(self):
        item = manifest()
        gate = evaluate_promotion(
            item,
            evidence(item, origin="test_fixture", cases=10),
            stage="evaluation",
            evaluated_at=NOW,
        )
        card = build_policy_card(
            item,
            current_state="draft",
            current_revision=0,
            signature_verified=True,
            latest_gate_report=gate,
            known_strengths=("bounded-cost",),
            failure_modes=("distribution-shift",),
            unsupported_scopes=("critical-security",),
            created_at=NOW,
        )
        self.assertIn("blocked_by_real_data_origin", card.decision_explanation)
        self.assertEqual("evaluation", card.next_required_stage)
        self.assertFalse(card.routing_authority)

    def test_public_contract_exposes_exact_claim_boundaries(self):
        policy = public_policy_registry_policy()
        self.assertEqual("v0.5.7.3", policy["schema_version"])
        self.assertFalse(policy["registry_active_is_runtime_active"])
        self.assertEqual(1_000, policy["minimum_cases"]["activation"])
        self.assertEqual(0, policy["promotion_gates"]["hard_violation_counts"])
        self.assertFalse(policy["commercial_claims_allowed"])


class PolicyRegistryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = PolicyRegistryStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def _register(self, item: PolicyManifest) -> None:
        self.assertTrue(self.store.record_manifest(item, signing_key=KEY))

    def _promote_to_active(self, item: PolicyManifest) -> None:
        ev = evidence(item)
        self.store.record_evidence(ev)
        state = "draft"
        revision = 0
        for target, stage in (
            ("evaluated", "evaluation"),
            ("shadow", "shadow"),
            ("canary", "canary"),
            ("active", "activation"),
        ):
            gate = evaluate_promotion(item, ev, stage=stage, evaluated_at=NOW)
            self.store.record_gate(gate)
            receipt = transition_policy(
                item,
                current_state=state,
                current_revision=revision,
                target_state=target,
                signature_verified=True,
                gate_report=gate,
                approval_id="approval" if target in {"canary", "active"} else None,
                transitioned_at=NOW,
            )
            self.store.record_transition(receipt)
            state, revision = target, revision + 1

    def test_migration_24_and_empty_registry(self):
        self.assertEqual(31, self.health["migrations"])
        counts = self.store.counts()
        self.assertEqual(0, counts["policy_registry_policies"])
        self.assertEqual(0, counts["policy_registry_runtime_activations"])

    def test_manifest_is_signature_verified_and_idempotent(self):
        item = manifest()
        self._register(item)
        self.assertFalse(self.store.record_manifest(item, signing_key=KEY))
        with self.assertRaises(PolicyRegistryError):
            self.store.record_manifest(manifest("other", "2"), signing_key=OTHER_KEY)
        self.assertEqual(item.registry_policy_id, self.store.manifest(item.registry_policy_id)["registry_policy_id"])

    def test_linked_evidence_gate_and_transition_are_persisted(self):
        item = manifest()
        self._register(item)
        ev = evidence(item)
        gate = evaluate_promotion(item, ev, stage="evaluation", evaluated_at=NOW)
        self.assertTrue(self.store.record_evidence(ev))
        self.assertTrue(self.store.record_gate(gate))
        receipt = transition_policy(
            item,
            current_state="draft",
            current_revision=0,
            target_state="evaluated",
            signature_verified=True,
            gate_report=gate,
            transitioned_at=NOW,
        )
        self.assertTrue(self.store.record_transition(receipt))
        self.assertEqual(
            {"registry_policy_id": item.registry_policy_id, "state": "evaluated", "revision": 1},
            self.store.current_state(item.registry_policy_id),
        )

    def test_stale_transition_is_rejected(self):
        item = manifest()
        self._register(item)
        ev = evidence(item)
        self.store.record_evidence(ev)
        gate = evaluate_promotion(item, ev, stage="evaluation", evaluated_at=NOW)
        self.store.record_gate(gate)
        receipt = transition_policy(
            item,
            current_state="draft",
            current_revision=0,
            target_state="evaluated",
            signature_verified=True,
            gate_report=gate,
            transitioned_at=NOW,
        )
        self.store.record_transition(receipt)
        other_receipt = transition_policy(
            item,
            current_state="draft",
            current_revision=0,
            target_state="evaluated",
            signature_verified=True,
            gate_report=gate,
            transitioned_at="2026-08-04T00:00:01+00:00",
        )
        with self.assertRaises(PolicyRegistryError):
            self.store.record_transition(other_receipt)

    def test_active_pointer_never_claims_runtime_activation(self):
        item = manifest()
        self._register(item)
        self._promote_to_active(item)
        pointer = self.store.current_registry_pointer()
        self.assertEqual(item.registry_policy_id, pointer["registry_policy_id"])
        counts = self.store.counts()
        self.assertEqual(1, counts["policy_registry_active_transitions"])
        self.assertEqual(0, counts["policy_registry_runtime_route_changes"])
        self.assertEqual(0, counts["policy_registry_runtime_activations"])

    def test_one_click_freeze_clears_active_registry_pointer(self):
        item = manifest()
        self._register(item)
        self._promote_to_active(item)
        receipt = freeze_policy(
            item,
            current_state="active",
            current_revision=4,
            signature_verified=True,
            reason_code="emergency-stop",
            transitioned_at=NOW,
        )
        self.assertTrue(receipt.registry_pointer_changed)
        self.assertTrue(self.store.record_transition(receipt))
        self.assertIsNone(self.store.current_registry_pointer())
        self.assertEqual("frozen", self.store.current_state(item.registry_policy_id)["state"])

    def test_one_click_rollback_is_atomic_and_runtime_inert(self):
        baseline = manifest(
            "baseline", "0.9.0", rollback_name="emergency", rollback_version="0.1"
        )
        candidate = manifest()
        self._register(baseline)
        self._register(candidate)
        self._promote_to_active(baseline)
        self._promote_to_active(candidate)
        freeze = freeze_policy(
            candidate,
            current_state="active",
            current_revision=4,
            signature_verified=True,
            reason_code="quality-regression",
            transitioned_at=NOW,
        )
        rollback = build_registry_rollback(
            candidate,
            freeze,
            baseline,
            rollback_policy_state="active",
            reason_code="quality-regression",
            created_at=NOW,
        )
        self.assertTrue(self.store.record_rollback(freeze, rollback))
        self.assertEqual("frozen", self.store.current_state(candidate.registry_policy_id)["state"])
        self.assertEqual(baseline.registry_policy_id, self.store.current_registry_pointer()["registry_policy_id"])
        self.assertEqual(0, self.store.counts()["policy_registry_runtime_rollbacks"])

    def test_card_must_match_current_state(self):
        item = manifest()
        self._register(item)
        card = build_policy_card(
            item,
            current_state="draft",
            current_revision=0,
            signature_verified=True,
            latest_gate_report=None,
            known_strengths=("deterministic",),
            failure_modes=("drift",),
            unsupported_scopes=("security",),
            created_at=NOW,
        )
        self.assertTrue(self.store.record_card(card))
        self.assertFalse(self.store.record_card(card))

    def test_tables_are_immutable(self):
        item = manifest()
        self._register(item)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE registered_policies SET policy_version='tampered' WHERE registry_policy_id=?",
                    (item.registry_policy_id,),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
