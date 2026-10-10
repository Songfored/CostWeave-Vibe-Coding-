from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from costweave.comprehensive_data_pilot import (
    COSTWEAVE_DYNAMIC,
    ComprehensivePilotError,
    NODE_MAP,
    NODES,
    POLICY_ORDER,
    REQUIRED_TESTS,
    STAGES,
    _execute_node,
    _execute_policy,
    _oracle_artifact,
    freeze_policy_routes,
    judge_final,
    normalize_meta_decision,
    plan_node_reservation,
    run_offline_preflight,
    run_real_pilot,
    validate_node,
)
from costweave.event_store import EventStore
from costweave.experimental_calls import ExperimentBudget, ExperimentalCallStore


class ComprehensiveDataPilotTests(unittest.TestCase):
    class OfflineHeadroomStore:
        def __init__(self, headroom=None):
            self.headroom = headroom or {}

        def budget_headroom(self, _experiment_id, *, provider):
            remaining = float(self.headroom.get(provider, 1.0))
            return {
                "experiment_status": "active",
                "total_remaining_usd": remaining,
                "provider_remaining_usd": remaining,
                "logical_calls_remaining": 10,
                "authoritative_at_reserve": True,
            }

    @staticmethod
    def _attempt_row(
        *,
        status="failed",
        accepted=False,
        error=None,
        error_code=None,
        cost=0.01,
        slot="gpt",
        attempt_label="primary",
    ):
        return {
            "node_id": "bug_diagnosis",
            "stage": 2,
            "slot": slot,
            "reasoning_effort": "high",
            "provider_id": f"fixture-{slot}",
            "model_requested": f"fixture-{slot}-model",
            "status": status,
            "accepted": accepted,
            "error": error,
            "error_code": error_code,
            "estimated_cost_usd": cost,
            "elapsed_ms": 10,
            "attempt_label": attempt_label,
        }

    def _run_fallback_case(self, primary, fallback, *, headroom=None):
        calls = []

        def fake_once(**kwargs):
            calls.append(kwargs["attempt_label"])
            if kwargs["attempt_label"] == "primary":
                return dict(primary), None
            artifact = (
                _oracle_artifact("bug_diagnosis")
                if fallback.get("accepted")
                else None
            )
            return dict(fallback), artifact

        def fake_plan(**kwargs):
            provider = {
                "claude": "traveltoken-claude",
                "gpt": "traveltoken-gpt",
                "deepseek": "deepseek-official",
            }[kwargs["route"]["slot"]]
            required = 0.02
            return None, None, {
                "provider_id": provider,
                "required_work_order_cost_limit_usd": required,
                "affordable": required <= kwargs["max_call_budget_usd"],
            }

        with patch(
            "costweave.comprehensive_data_pilot._execute_node_once",
            side_effect=fake_once,
        ), patch(
            "costweave.comprehensive_data_pilot.plan_node_reservation",
            side_effect=fake_plan,
        ):
            row, artifact = _execute_node(
                node=NODE_MAP["bug_diagnosis"],
                policy_id=COSTWEAVE_DYNAMIC,
                route={"slot": "gpt", "reasoning_effort": "low"},
                artifacts={
                    "planner_a": _oracle_artifact("planner_a"),
                    "planner_b": _oracle_artifact("planner_b"),
                },
                providers={},
                store=self.OfflineHeadroomStore(headroom),
                max_call_budget_usd=0.2,
                fallback_enabled=True,
            )
        return row, artifact, calls

    def test_frozen_dag_has_parallel_branches_and_no_missing_dependency(self):
        self.assertEqual(19, len(NODES))
        self.assertTrue(any(len(stage) > 1 for stage in STAGES))
        known = set()
        for stage in STAGES:
            for node_id in stage:
                self.assertTrue(set(NODE_MAP[node_id].dependencies) <= known)
            known.update(stage)

    def test_four_policies_freeze_model_and_effort_for_every_node(self):
        routes = freeze_policy_routes()
        self.assertEqual(set(POLICY_ORDER), set(routes))
        for policy in routes.values():
            self.assertEqual(set(NODE_MAP), set(policy))
            self.assertTrue(all(route["slot"] in {"deepseek", "gpt", "claude"} for route in policy.values()))
            self.assertTrue(all(route["reasoning_effort"] in {"low", "medium", "high"} for route in policy.values()))
        self.assertEqual("low", routes[COSTWEAVE_DYNAMIC]["requirements_extract"]["reasoning_effort"])
        self.assertEqual("high", routes[COSTWEAVE_DYNAMIC]["meta_decision"]["reasoning_effort"])
        self.assertEqual("low", routes[COSTWEAVE_DYNAMIC]["bug_diagnosis"]["reasoning_effort"])
        self.assertEqual("claude", routes[COSTWEAVE_DYNAMIC]["patch_design"]["slot"])

    def test_oracle_artifacts_pass_all_local_validators(self):
        for node in NODES:
            value = _oracle_artifact(node.node_id)
            result = validate_node(node, json.dumps(value, ensure_ascii=False))
            self.assertTrue(result["accepted"], (node.node_id, result))
        self.assertTrue(judge_final(_oracle_artifact("final_assembly"))["accepted"])

    def test_semantic_faults_are_not_schema_only_passes(self):
        bad = dict(_oracle_artifact("provider_costing"))
        bad["currency_unit"] = "USD_PER_THOUSAND"
        self.assertFalse(validate_node(NODE_MAP["provider_costing"], json.dumps(bad))["accepted"])
        bad = dict(_oracle_artifact("deployment_design"))
        bad["residency_violations"] = ["EU->A"]
        self.assertFalse(validate_node(NODE_MAP["deployment_design"], json.dumps(bad))["accepted"])
        bad_final = dict(_oracle_artifact("final_assembly"))
        bad_final["tests"] = sorted(REQUIRED_TESTS - {"all_zero_rejected"})
        self.assertFalse(judge_final(bad_final)["accepted"])

    def test_bounded_meta_and_merge_accept_equivalent_structures(self):
        meta = _oracle_artifact("meta_decision")
        meta["planner_count"] = 3
        result = validate_node(NODE_MAP["meta_decision"], json.dumps(meta))
        self.assertTrue(result["accepted"])
        merge = _oracle_artifact("plan_merge")
        merge["bounded_management"] = {"levels": 2, "scope": "bounded"}
        result = validate_node(NODE_MAP["plan_merge"], json.dumps(merge))
        self.assertTrue(result["accepted"])

    def test_meta_normalization_accepts_equivalent_relay_encodings(self):
        equivalent = {
            "planning_mode": "multi-agent planner",
            "planners": ["risk", "delivery"],
            "reason": "independent high-risk branches",
            "management_levels": 2,
        }
        normalized = normalize_meta_decision(equivalent)
        self.assertEqual("multiplanner", normalized["topology"])
        self.assertEqual(2, normalized["planner_count"])
        self.assertTrue(normalized["bounded"])
        result = validate_node(
            NODE_MAP["meta_decision"], json.dumps(equivalent)
        )
        self.assertTrue(result["accepted"], result)

        contradictory = dict(equivalent)
        contradictory["planning_mode"] = "planner"
        contradictory["planners"] = ["only"]
        self.assertFalse(
            validate_node(
                NODE_MAP["meta_decision"], json.dumps(contradictory)
            )["accepted"]
        )

    def test_equivalent_algorithm_and_zero_violation_encodings_pass(self):
        patch = _oracle_artifact("patch_design")
        patch["algorithm"] = ["largest_remainder_cents", "floor", "distribute"]
        self.assertTrue(validate_node(NODE_MAP["patch_design"], json.dumps(patch))["accepted"])
        deployment = _oracle_artifact("deployment_design")
        deployment["residency_violations"] = 0
        self.assertTrue(validate_node(NODE_MAP["deployment_design"], json.dumps(deployment))["accepted"])

    def test_offline_preflight_uses_no_external_calls(self):
        result = run_offline_preflight()
        self.assertTrue(result["passed"])
        self.assertEqual(0, result["external_calls"])
        self.assertTrue(all(result["fault_checks"].values()))

    def test_dynamic_reservation_uses_exact_provider_and_node_payload(self):
        providers = {
            "deepseek-official": {
                "chat_completions_url": "https://api.deepseek.test/chat/completions",
                "selected_model": "deepseek-v4-pro",
                "secret_env": "DEEPSEEK_TEST_KEY",
                "trust": "official",
            },
            "traveltoken-gpt": {
                "chat_completions_url": "https://relay.test/v1/chat/completions",
                "selected_model": "gpt-5.5",
                "secret_env": "GPT_TEST_KEY",
                "trust": "relay-unverified",
            },
            "traveltoken-claude": {
                "chat_completions_url": "https://relay.test/v1/chat/completions",
                "selected_model": "claude-sonnet-4-6",
                "secret_env": "CLAUDE_TEST_KEY",
                "trust": "relay-unverified",
            },
        }
        artifacts = {
            "planner_a": _oracle_artifact("planner_a"),
            "planner_b": _oracle_artifact("planner_b"),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "reservation.sqlite3"
            EventStore(runtime)
            store = ExperimentalCallStore(runtime)
            _, gpt_request, gpt_plan = plan_node_reservation(
                node=NODE_MAP["bug_diagnosis"],
                policy_id="reservation-test-gpt",
                route={"slot": "gpt", "reasoning_effort": "low"},
                artifacts=artifacts,
                providers=providers,
                store=store,
                max_call_budget_usd=1.0,
            )
            _, ds_request, ds_plan = plan_node_reservation(
                node=NODE_MAP["bug_diagnosis"],
                policy_id="reservation-test-ds",
                route={"slot": "deepseek", "reasoning_effort": "low"},
                artifacts=artifacts,
                providers=providers,
                store=store,
                max_call_budget_usd=1.0,
            )
        self.assertTrue(gpt_plan["affordable"])
        self.assertTrue(ds_plan["affordable"])
        self.assertGreater(
            gpt_plan["required_work_order_cost_limit_usd"],
            ds_plan["required_work_order_cost_limit_usd"],
        )
        self.assertEqual(
            gpt_plan["required_work_order_cost_limit_usd"],
            gpt_request.work_order.cost_limit,
        )
        self.assertEqual(
            ds_plan["required_work_order_cost_limit_usd"],
            ds_request.work_order.cost_limit,
        )

    def test_budget_headroom_is_read_only_and_provider_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "headroom.sqlite3"
            EventStore(runtime)
            store = ExperimentalCallStore(runtime)
            store.register_experiment(
                ExperimentBudget(
                    experiment_id="experiment_headroom_test",
                    total_cost_limit_usd=0.3,
                    provider_cost_limits_usd={"provider-a": 0.2},
                    max_logical_calls=3,
                    max_attempts_per_call=1,
                )
            )
            before = store.budget_headroom(
                "experiment_headroom_test", provider="provider-a"
            )
            after = store.budget_headroom(
                "experiment_headroom_test", provider="provider-a"
            )
        self.assertEqual(before, after)
        self.assertEqual(0.3, before["total_remaining_usd"])
        self.assertEqual(0.2, before["provider_remaining_usd"])
        self.assertEqual(3, before["logical_calls_remaining"])

    def test_parallel_failure_is_scoped_to_dependent_branches(self):
        routes = freeze_policy_routes()[COSTWEAVE_DYNAMIC]

        def fake_execute_node(**kwargs):
            node = kwargs["node"]
            if node.node_id == "provider_costing":
                return ({
                    "node_id": node.node_id,
                    "stage": node.stage,
                    "status": "failed",
                    "accepted": False,
                    "error": "TimeoutError",
                    "elapsed_ms": 90_000,
                    "estimated_cost_usd": 0.01,
                }, None)
            artifact = _oracle_artifact(node.node_id)
            return ({
                "node_id": node.node_id,
                "stage": node.stage,
                "status": "completed",
                "accepted": True,
                "elapsed_ms": 10,
                "estimated_cost_usd": 0.001,
            }, artifact)

        with patch(
            "costweave.comprehensive_data_pilot._execute_node",
            side_effect=fake_execute_node,
        ):
            result = _execute_policy(
                policy_id=COSTWEAVE_DYNAMIC,
                routes=routes,
                providers={},
                store=None,
                max_call_budget_usd=1.0,
            )
        rows = {row["node_id"]: row for row in result["nodes"]}
        self.assertEqual("failed", rows["provider_costing"]["status"])
        self.assertEqual("completed", rows["patch_tests"]["status"])
        self.assertEqual("completed", rows["security_review"]["status"])
        self.assertEqual(
            "skipped_missing_dependencies",
            rows["financial_review"]["status"],
        )
        self.assertIn(
            "provider_costing", rows["financial_review"]["missing_dependencies"]
        )

    def test_repeat_run_is_fail_closed_without_fresh_authorization_gate(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "COSTWEAVE_EXPERIMENTAL_ADAPTERS": "1",
                "COSTWEAVE_PAID_CALLS": "1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                ComprehensivePilotError, "尚未获得新的显式授权"
            ):
                run_real_pilot(root=Path(directory), budget_usd=0.1)

    def test_offline_fault_matrix_recovers_retryable_critical_failures(self):
        cases = {
            "timeout": self._attempt_row(error="TimeoutError"),
            "rate_limit": self._attempt_row(
                error="ExperimentalCallError",
                error_code="provider_rate_limited",
            ),
            "provider_unavailable": self._attempt_row(
                error="ExperimentalCallError",
                error_code="provider_unavailable",
            ),
            "invalid_json": self._attempt_row(
                error="ExperimentalCallError",
                error_code="invalid_json",
            ),
            "secret_inspection_unavailable": self._attempt_row(
                error="ExperimentalCallError",
                error_code="secret_inspection_failed",
            ),
            "lease_expired": self._attempt_row(
                error="ExperimentalCallError",
                error_code="secret_lease_expired",
            ),
            "secret_unavailable": self._attempt_row(
                error="ExternalCallsDisabledError",
                error_code="secret_unavailable",
            ),
            "semantic_rejection": self._attempt_row(
                status="completed",
                accepted=False,
                error=None,
                error_code=None,
            ),
            "primary_budget_unaffordable": self._attempt_row(
                status="blocked_budget_preflight",
                error="DynamicCallBudgetExceeded",
                cost=0.0,
            ),
        }
        for name, primary in cases.items():
            with self.subTest(name=name):
                fallback = self._attempt_row(
                    status="completed",
                    accepted=True,
                    error=None,
                    error_code=None,
                    cost=0.02,
                    slot="claude",
                    attempt_label="fallback",
                )
                row, artifact, calls = self._run_fallback_case(
                    primary, fallback
                )
                self.assertEqual(["primary", "fallback"], calls)
                self.assertEqual("recovered", row["fallback_status"])
                self.assertTrue(row["fallback_used"])
                self.assertIsNotNone(artifact)
                self.assertEqual(2, len(row["attempts"]))

    def test_offline_fault_matrix_denies_unsafe_or_unfunded_fallbacks(self):
        exposure = self._attempt_row(
            error="ExperimentalCallError",
            error_code="secret_exposure_detected",
        )
        fallback = self._attempt_row(
            status="completed",
            accepted=True,
            slot="claude",
            attempt_label="fallback",
        )
        row, artifact, calls = self._run_fallback_case(exposure, fallback)
        self.assertEqual(["primary"], calls)
        self.assertFalse(row["fallback_used"])
        self.assertIsNone(artifact)

        nearly_spent = self._attempt_row(
            error="TimeoutError",
            cost=0.19,
        )
        row, artifact, calls = self._run_fallback_case(
            nearly_spent,
            fallback,
        )
        self.assertEqual(["primary"], calls)
        self.assertEqual(0.01, row["fallback_call_ceiling_usd"])
        self.assertEqual(
            "not_attempted_budget_or_capability", row["fallback_status"]
        )
        self.assertIsNone(artifact)

        timeout = self._attempt_row(error="TimeoutError")
        row, artifact, calls = self._run_fallback_case(
            timeout,
            fallback,
            headroom={"traveltoken-claude": 0.001},
        )
        self.assertEqual(["primary"], calls)
        self.assertEqual(
            "not_attempted_budget_or_capability", row["fallback_status"]
        )
        self.assertFalse(row["fallback_used"])
        self.assertIsNone(artifact)

    def test_offline_fault_matrix_stops_after_one_failed_fallback(self):
        primary = self._attempt_row(
            error="ExperimentalCallError", error_code="transport_error"
        )
        fallback = self._attempt_row(
            error="ExperimentalCallError",
            error_code="provider_unavailable",
            cost=0.02,
            slot="claude",
            attempt_label="fallback",
        )
        row, artifact, calls = self._run_fallback_case(primary, fallback)
        self.assertEqual(["primary", "fallback"], calls)
        self.assertEqual("fallback_failed", row["status"])
        self.assertEqual("failed", row["fallback_status"])
        self.assertIsNone(artifact)
        self.assertEqual(2, len(row["attempts"]))

    def test_offline_fault_matrix_does_not_enable_fallback_for_other_policies(self):
        primary = self._attempt_row(error="TimeoutError")
        calls = []

        def fake_once(**kwargs):
            calls.append(kwargs["attempt_label"])
            return dict(primary), None

        with patch(
            "costweave.comprehensive_data_pilot._execute_node_once",
            side_effect=fake_once,
        ):
            row, artifact = _execute_node(
                node=NODE_MAP["bug_diagnosis"],
                policy_id="manual_static",
                route={"slot": "gpt", "reasoning_effort": "low"},
                artifacts={},
                providers={},
                store=self.OfflineHeadroomStore(),
                max_call_budget_usd=0.2,
                fallback_enabled=False,
            )
        self.assertEqual(["primary"], calls)
        self.assertEqual("disabled_or_noncritical", row["fallback_status"])
        self.assertIsNone(artifact)


if __name__ == "__main__":
    unittest.main()
