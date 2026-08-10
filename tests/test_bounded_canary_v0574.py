from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from costweave.canary_control import (
    CanaryControlError,
    CanaryDeployment,
    CanaryWindow,
    assign_canary_policy,
    evaluate_canary_window,
    public_canary_policy,
)
from costweave.canary_store import CanaryStore
from costweave.event_store import EventStore
from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest
from costweave.policy_registry import (
    PolicyManifest,
    PromotionEvidence,
    evaluate_promotion,
    transition_policy,
)
from costweave.policy_registry_store import PolicyRegistryStore


KEY = b"canary-test-key-material-32bytes!!!!"
NOW = "2026-08-04T00:00:00+00:00"


def manifest(
    name: str,
    version: str,
    *,
    rollback_name: str,
    rollback_version: str,
) -> PolicyManifest:
    return PolicyManifest.create(
        policy_name=name,
        policy_version=version,
        source_candidate_id=f"candidate-{name}",
        source_replay_report_id=f"replay-{name}",
        source_shadow_report_id=f"shadow-{name}",
        code_revision="git-canary",
        objective_revision="cps-v1",
        constraint_revision="hard-constraints-v1",
        allowed_task_families=("coding", "research"),
        prohibited_task_families=("critical-security",),
        rollback_policy_name=rollback_name,
        rollback_policy_version=rollback_version,
        multiplanner_default_requested=False,
        signer_key_id="canary-test-key",
        signing_key=KEY,
        created_at=NOW,
    )


def evidence(item: PolicyManifest) -> PromotionEvidence:
    return PromotionEvidence.create(
        registry_policy_id=item.registry_policy_id,
        policy_version=item.policy_version,
        cohort_hash=hashlib.sha256(item.registry_policy_id.encode()).hexdigest(),
        quality_baseline_id="strongest-feasible-model",
        cps_baseline_id="v0.4-fixed-policy",
        evidence_level="E4",
        data_origin="production",
        independent_evaluation=True,
        case_count=1_000,
        offline_replay_passed=True,
        production_shadow_passed=True,
        shadow_cost_overhead_p95_ratio=0.05,
        canary_evidence_present=True,
        final_success_delta_pp=1.0,
        final_success_delta_ci95_lower_pp=0.0,
        high_risk_success_delta_pp=0.0,
        cps_improvement_pct=20.0,
        cps_improvement_ci95_lower_pct=16.0,
        parallel_p95_improvement_pct=15.0,
        handoff_rework_rate=0.01,
        hard_budget_violation_count=0,
        permission_violation_count=0,
        concurrency_violation_count=0,
        call_count_violation_count=0,
        calibration_passed=True,
        data_quality_passed=True,
        privacy_passed=True,
        drift_passed=True,
        multiplanner_default_requested=False,
        difficult_success_delta_pp=0.0,
        routing_regret_improvement_pct=0.0,
        created_at=NOW,
    )


def window(deployment_id: str, **changes) -> CanaryWindow:
    values = {
        "deployment_id": deployment_id,
        "stage_pct": 5,
        "candidate_cases": 50,
        "control_cases": 950,
        "candidate_success_rate": 0.90,
        "control_success_rate": 0.90,
        "success_delta_ci95_lower_pp": -1.0,
        "candidate_severe_error_count": 0,
        "control_severe_error_count": 0,
        "hard_budget_violation_count": 0,
        "permission_violation_count": 0,
        "candidate_p95_latency_ms": 110.0,
        "control_p95_latency_ms": 100.0,
        "drift_gate_passed": True,
        "calibration_gate_passed": True,
        "duplicate_external_side_effect_count": 0,
        "high_risk_candidate_cases": 0,
        "explanation_present": True,
        "policy_version_present": True,
        "rollback_target_present": True,
        "observed_at": NOW,
    }
    values.update(changes)
    return CanaryWindow.create(**values)


class BoundedCanaryContractTests(unittest.TestCase):
    def setUp(self):
        self.stable = manifest(
            "stable", "1.0.0", rollback_name="emergency", rollback_version="0"
        )
        self.candidate = manifest(
            "candidate", "1.1.0", rollback_name="stable", rollback_version="1.0.0"
        )
        self.gate = evaluate_promotion(
            self.candidate, evidence(self.candidate), stage="canary", evaluated_at=NOW
        )
        self.deployment = CanaryDeployment.create(
            candidate_policy_id=self.candidate.registry_policy_id,
            candidate_policy_version=self.candidate.policy_version,
            stable_policy_id=self.stable.registry_policy_id,
            stable_policy_version=self.stable.policy_version,
            candidate_manifest_hash=self.candidate.content_hash,
            stable_manifest_hash=self.stable.content_hash,
            canary_gate_report_id=self.gate.gate_report_id,
            activation_approval_id="approval-canary-1",
            allowed_task_families=self.candidate.allowed_task_families,
            prohibited_task_families=self.candidate.prohibited_task_families,
            explanation="Bounded low-risk candidate exposure with hard rollback guards",
            started_at=NOW,
        )

    def test_public_contract_is_default_off_and_never_expands_permissions(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COSTWEAVE_CANARY_ENABLED", None)
            policy = public_canary_policy()
        self.assertEqual("v0.5.7.4", policy["schema_version"])
        self.assertFalse(policy["runtime_enabled"])
        self.assertEqual([5, 25, 50, 100], policy["stages_pct"])
        self.assertEqual(0.0, policy["high_risk_exploration_rate"])
        self.assertFalse(policy["provider_credentials_granted"])
        self.assertFalse(policy["tool_permissions_expanded"])
        self.assertFalse(policy["commercial_claims_allowed"])

    def test_assignment_is_deterministic_scoped_and_high_risk_is_never_explored(self):
        first = assign_canary_policy(
            self.deployment,
            run_id="run-deterministic",
            stage_pct=25,
            risk_level="normal",
            task_families=("coding",),
            assigned_at=NOW,
        )
        second = assign_canary_policy(
            self.deployment,
            run_id="run-deterministic",
            stage_pct=25,
            risk_level="normal",
            task_families=("coding",),
            assigned_at=NOW,
        )
        self.assertEqual(first, second)
        high = assign_canary_policy(
            self.deployment,
            run_id="run-high-risk",
            stage_pct=100,
            risk_level="high",
            task_families=("coding",),
            assigned_at=NOW,
        )
        prohibited = assign_canary_policy(
            self.deployment,
            run_id="run-prohibited",
            stage_pct=100,
            risk_level="normal",
            task_families=("critical-security",),
            assigned_at=NOW,
        )
        self.assertEqual("stable-only", high.cohort)
        self.assertIn("risk_not_explorable", high.reasons)
        self.assertEqual("stable-only", prohibited.cohort)
        self.assertFalse(high.runtime_policy_changed)

    def test_rolled_back_assignment_is_forced_to_stable(self):
        assignment = assign_canary_policy(
            self.deployment,
            run_id="run-after-rollback",
            stage_pct=100,
            risk_level="normal",
            task_families=("coding",),
            exposure_enabled=False,
            assigned_at=NOW,
        )
        self.assertEqual("stable-only", assignment.cohort)
        self.assertEqual(self.stable.registry_policy_id, assignment.selected_policy_id)
        self.assertIn("deployment_rolled_back", assignment.reasons)

    def test_exact_quality_and_latency_boundaries_advance_one_stage(self):
        decision = evaluate_canary_window(window(self.deployment.deployment_id), evaluated_at=NOW)
        self.assertEqual("advance", decision.action)
        self.assertEqual(25, decision.to_stage_pct)
        self.assertTrue(decision.checks["success_non_inferiority_ci95"])
        self.assertTrue(decision.checks["p95_latency_degradation"])
        with self.assertRaises(TypeError):
            decision.checks["drift_gate"] = False

    def test_insufficient_sample_holds_without_skipping_stage(self):
        decision = evaluate_canary_window(
            window(self.deployment.deployment_id, candidate_cases=49), evaluated_at=NOW
        )
        self.assertEqual("hold", decision.action)
        self.assertEqual(5, decision.to_stage_pct)
        self.assertEqual(("minimum_candidate_cases",), decision.reasons)

    def test_each_safety_violation_triggers_immediate_automatic_rollback(self):
        cases = (
            {"success_delta_ci95_lower_pp": -1.01},
            {"candidate_severe_error_count": 1},
            {"hard_budget_violation_count": 1},
            {"permission_violation_count": 1},
            {"candidate_p95_latency_ms": 110.01},
            {"drift_gate_passed": False},
            {"calibration_gate_passed": False},
            {"duplicate_external_side_effect_count": 1},
            {"high_risk_candidate_cases": 1},
            {"explanation_present": False},
            {"policy_version_present": False},
            {"rollback_target_present": False},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                decision = evaluate_canary_window(
                    window(self.deployment.deployment_id, **changes), evaluated_at=NOW
                )
                self.assertEqual("rollback", decision.action)
                self.assertTrue(decision.automatic_rollback_executed)
                self.assertTrue(decision.in_flight_snapshots_preserved)
                self.assertTrue(decision.new_runs_use_decision_immediately)


class CanaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.registry = PolicyRegistryStore(self.path)
        self.store = CanaryStore(self.path)
        self.stable = manifest(
            "stable", "1.0.0", rollback_name="emergency", rollback_version="0"
        )
        self.candidate = manifest(
            "candidate", "1.1.0", rollback_name="stable", rollback_version="1.0.0"
        )
        self._register_and_promote(self.stable, target="active")
        self.candidate_gate = self._register_and_promote(self.candidate, target="canary")
        self.deployment = CanaryDeployment.create(
            candidate_policy_id=self.candidate.registry_policy_id,
            candidate_policy_version=self.candidate.policy_version,
            stable_policy_id=self.stable.registry_policy_id,
            stable_policy_version=self.stable.policy_version,
            candidate_manifest_hash=self.candidate.content_hash,
            stable_manifest_hash=self.stable.content_hash,
            canary_gate_report_id=self.candidate_gate.gate_report_id,
            activation_approval_id="approval-canary-1",
            allowed_task_families=self.candidate.allowed_task_families,
            prohibited_task_families=self.candidate.prohibited_task_families,
            explanation="Persisted bounded deployment",
            started_at=NOW,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _register_and_promote(self, item: PolicyManifest, *, target: str):
        self.registry.record_manifest(item, signing_key=KEY)
        ev = evidence(item)
        self.registry.record_evidence(ev)
        state = "draft"
        revision = 0
        latest = None
        for next_state, stage in (
            ("evaluated", "evaluation"),
            ("shadow", "shadow"),
            ("canary", "canary"),
            ("active", "activation"),
        ):
            gate = evaluate_promotion(item, ev, stage=stage, evaluated_at=NOW)
            self.registry.record_gate(gate)
            receipt = transition_policy(
                item,
                current_state=state,
                current_revision=revision,
                target_state=next_state,
                signature_verified=True,
                gate_report=gate,
                approval_id=("approval" if next_state in {"canary", "active"} else None),
                transitioned_at=NOW,
            )
            self.registry.record_transition(receipt)
            latest = gate
            state, revision = next_state, revision + 1
            if next_state == target:
                return latest
        raise AssertionError("target state was not reached")

    def _start(self):
        with patch.dict(os.environ, {"COSTWEAVE_CANARY_ENABLED": "1"}):
            return self.store.start_deployment(self.deployment)

    def test_migration_25_and_empty_canary_runtime(self):
        self.assertEqual(33, self.health["migrations"])
        self.assertEqual("v6.3.0", self.health["runtime_schema"])
        self.assertEqual(0, self.store.counts()["canary_deployments"])

    def test_explicit_opt_in_and_registry_lineage_are_required(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COSTWEAVE_CANARY_ENABLED", None)
            with self.assertRaises(CanaryControlError):
                self.store.start_deployment(self.deployment)
        self.assertTrue(self._start())
        self.assertFalse(self._start())
        state = self.store.current_state()
        self.assertEqual("started", state["event_type"])
        self.assertEqual(5, state["stage_pct"])

    def test_assignment_is_immutable_and_a_healthy_window_advances(self):
        self._start()
        assignment = assign_canary_policy(
            self.deployment,
            run_id="run-store-1",
            stage_pct=5,
            risk_level="normal",
            task_families=("coding",),
            assigned_at=NOW,
        )
        self.assertTrue(self.store.record_assignment(assignment))
        self.assertFalse(self.store.record_assignment(assignment))
        decision = evaluate_canary_window(window(self.deployment.deployment_id), evaluated_at=NOW)
        self.assertTrue(self.store.record_window_decision(window(self.deployment.deployment_id), decision))
        self.assertEqual("advanced", self.store.current_state()["event_type"])
        self.assertEqual(25, self.store.current_state()["stage_pct"])

    def test_rollback_preserves_old_snapshot_and_forces_new_runs_to_stable(self):
        self._start()
        before = assign_canary_policy(
            self.deployment,
            run_id="run-before-rollback",
            stage_pct=5,
            risk_level="normal",
            task_families=("coding",),
            assigned_at=NOW,
        )
        self.store.record_assignment(before)
        failing = window(self.deployment.deployment_id, hard_budget_violation_count=1)
        decision = evaluate_canary_window(failing, evaluated_at=NOW)
        self.store.record_window_decision(failing, decision)
        self.assertEqual("rolled_back", self.store.current_state()["event_type"])
        self.assertEqual(before.to_dict(), self.store.assignment_for_run("run-before-rollback"))
        after = assign_canary_policy(
            self.deployment,
            run_id="run-after-rollback",
            stage_pct=5,
            risk_level="normal",
            task_families=("coding",),
            exposure_enabled=False,
            assigned_at=NOW,
        )
        self.assertTrue(self.store.record_assignment(after))
        self.assertEqual("stable-only", after.cohort)
        counts = self.store.counts()
        self.assertEqual(1, counts["canary_runtime_rollbacks"])
        self.assertEqual(0, counts["canary_high_risk_candidate_assignments"])
        self.assertEqual(0, counts["canary_provider_calls"])
        self.assertEqual(0, counts["canary_execution_side_effects"])

    def test_append_only_tables_reject_tampering(self):
        self._start()
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE canary_deployments SET initial_stage_pct=25 WHERE deployment_id=?",
                    (self.deployment.deployment_id,),
                )
        finally:
            connection.close()

    def test_control_kernel_persists_in_flight_snapshot_across_runtime_restart(self):
        self._start()
        kernel = ControlKernel(self.path)
        first, _ = kernel.create_run(
            RunRequest(goal="implement a bounded coding change"),
            idempotency_key="canary-run-before",
        )
        analysis = SimpleNamespace(stakes="normal", task_types=["coding"])
        snapshot = kernel.assign_runtime_policy(first, analysis)
        self.assertIsNotNone(snapshot)
        failing = window(self.deployment.deployment_id, permission_violation_count=1)
        decision = kernel.record_canary_window(failing, evaluated_at=NOW)
        self.assertEqual("rollback", decision.action)

        second, _ = kernel.create_run(
            RunRequest(goal="implement another bounded coding change"),
            idempotency_key="canary-run-after",
        )
        second_snapshot = kernel.assign_runtime_policy(second, analysis)
        self.assertEqual("stable-only", second_snapshot["cohort"])
        self.assertEqual(self.stable.registry_policy_id, second_snapshot["selected_policy_id"])
        self.assertEqual(snapshot, first.policy_snapshot)

        recovered = {record.id: record for record in ControlKernel(self.path).load_records()}
        self.assertEqual(snapshot, recovered[first.id].policy_snapshot)
        self.assertEqual(second_snapshot, recovered[second.id].policy_snapshot)


if __name__ == "__main__":
    unittest.main()
