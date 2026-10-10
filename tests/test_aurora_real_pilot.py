from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from costweave.aurora_real_pilot import (
    ALL_PREMIUM,
    AuroraRealPilotError,
    _execute_policy,
    _apply_deterministic_deployment_arithmetic,
    _deterministic_artifact,
    _deterministic_provider_selection,
    _extract_repaired_json_object,
    _failure_category,
    _fallback_route,
    _ledger_policy_metrics,
    _normalize_model_value,
    _preflight_budget_gate,
    _request,
    _select_reservation_wave,
    freeze_routes,
    run_real,
)
from costweave.aurora_rescue_eval import NODES, oracle_artifact, validate_node


class AuroraRealPilotSchedulerTests(unittest.TestCase):
    def test_v11_paid_entry_requires_fresh_replay_before_any_switch_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AuroraRealPilotError, "fresh-replay"):
                run_real(
                    root=Path(directory),
                    budget_usd=0.1,
                    fresh_replay=False,
                )

    def test_v11_paid_entry_requires_explicit_cny_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AuroraRealPilotError, "CNY hard cap"):
                run_real(
                    root=Path(directory),
                    budget_usd=0.1,
                    fresh_replay=True,
                )

    def test_deterministic_nodes_pass_the_same_independent_validator(self) -> None:
        for node_id in (
            "requirements_extract",
            "contract_truth",
            "capacity_admission",
            "residency_analysis",
            "media_analysis",
        ):
            dependencies = {
                dependency: oracle_artifact(dependency)
                for dependency in next(
                    node for node in NODES if node.node_id == node_id
                ).dependencies
            }
            self.assertTrue(
                validate_node(
                    node_id,
                    _deterministic_artifact(node_id),
                    dependencies,
                    incident_revealed=False,
                ).accepted,
                node_id,
            )

    def test_ledger_attribution_includes_unreported_transport_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE external_calls(task_id TEXT,status TEXT,actual_cost REAL)"
                )
                connection.executemany(
                    "INSERT INTO external_calls VALUES(?,?,?)",
                    [
                        (
                            "task_aurora_costweave_dynamic_a_primary",
                            "completed",
                            0.12,
                        ),
                        (
                            "task_aurora_costweave_dynamic_a_fallback",
                            "failed",
                            0.08,
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            metrics = _ledger_policy_metrics(
                path,
                [
                    {
                        "policy_id": "costweave_dynamic",
                        "nodes": [
                            {
                                "attempts": [
                                    {
                                        "attempt": "primary",
                                        "accepted": True,
                                        "actual_cost_usd": 0.1,
                                        "output_tokens": 10,
                                    },
                                    {
                                        "attempt": "fallback",
                                        "accepted": False,
                                        "actual_cost_usd": 0.02,
                                        "output_tokens": 5,
                                    },
                                ]
                            }
                        ],
                    }
                ],
            )["costweave_dynamic"]
        self.assertEqual(0.2, metrics["ledger_actual_cost_usd"])
        self.assertEqual(0.08, metrics["unreported_transport_cost_usd"])
        self.assertEqual(0.1, metrics["estimated_cost_leakage_usd"])
        self.assertEqual(5, metrics["wasted_output_tokens"])

    def test_lossless_normalization_repairs_wrapper_and_aliases(self) -> None:
        value, repairs = _normalize_model_value(
            "final_assembly",
            {
                "result": {
                    "status": "accepted",
                    "deliverable_ids": ["a"],
                    "final_cost_usd": 1,
                    "final_capacity_rpm": 2,
                    "final_p95_seconds": 3,
                    "hard_gates_passed": True,
                    "traceability": {},
                }
            },
        )
        self.assertEqual(["a"], value["deliverables"])
        self.assertEqual(1, value["final_setup_usd"])
        self.assertIn("unwrap:result", repairs)
        self.assertIn("alias:deliverable_ids->deliverables", repairs)

    def test_deployment_route_rows_are_normalized_and_arithmetic_is_verified(self) -> None:
        value, repairs = _normalize_model_value(
            "deployment_plan",
            {
                "regional_routes": [
                    {"region": "CN", "provider": "A", "rpm": 1000},
                    {"region": "EU", "provider": "B", "rpm": 700},
                    {"region": "US", "provider": "C", "rpm": 500},
                ],
                "total_capacity_rpm": 9999,
                "p95_seconds": 0,
                "residency_violations": [],
                "rollback_supported": True,
            },
        )
        self.assertIn("shape:regional_route_rows->mapping", repairs)
        corrected, repair = _apply_deterministic_deployment_arithmetic(
            "deployment_plan", value
        )
        self.assertEqual(2200, corrected["total_capacity_rpm"])
        self.assertEqual(2.8, corrected["p95_seconds"])
        self.assertEqual("deterministic:deployment_arithmetic_corrected", repair)
        dependencies = {
            dependency: oracle_artifact(dependency)
            for dependency in next(
                node for node in NODES if node.node_id == "deployment_plan"
            ).dependencies
        }
        self.assertTrue(
            validate_node("deployment_plan", corrected, dependencies).accepted
        )

    def test_deterministic_arithmetic_does_not_hide_invalid_routes(self) -> None:
        invalid = {
            "regional_routes": {
                "CN": {"A": 999},
                "EU": {"B": 700},
                "US": {"C": 500},
            },
            "total_capacity_rpm": 2199,
            "p95_seconds": 2.8,
            "residency_violations": [],
            "rollback_supported": True,
        }
        unchanged, repair = _apply_deterministic_deployment_arithmetic(
            "deployment_plan", invalid
        )
        self.assertEqual(invalid, unchanged)
        self.assertEqual(
            "deterministic:deployment_arithmetic_not_applied", repair
        )
        dependencies = {
            dependency: oracle_artifact(dependency)
            for dependency in next(
                node for node in NODES if node.node_id == "deployment_plan"
            ).dependencies
        }
        self.assertFalse(
            validate_node("deployment_plan", unchanged, dependencies).accepted
        )

    def test_route_row_normalization_rejects_lossy_or_boolean_values(self) -> None:
        base = {
            "total_capacity_rpm": 2200,
            "p95_seconds": 2.8,
            "residency_violations": [],
            "rollback_supported": True,
        }
        for row in (
            {"region": "CN", "provider": "A", "rpm": True},
            {"region": "CN", "provider": "A", "rpm": 1000, "note": "extra"},
        ):
            with self.subTest(row=row):
                value, repairs = _normalize_model_value(
                    "deployment_plan",
                    {**base, "regional_routes": [row]},
                )
                self.assertIsInstance(value["regional_routes"], list)
                self.assertNotIn("shape:regional_route_rows->mapping", repairs)

        invalid_mapping = {
            **base,
            "regional_routes": {
                "CN": {"A": True},
                "EU": {"B": 700},
                "US": {"C": 500},
            },
        }
        unchanged, repair = _apply_deterministic_deployment_arithmetic(
            "deployment_plan", invalid_mapping
        )
        self.assertEqual(invalid_mapping, unchanged)
        self.assertEqual("deterministic:deployment_arithmetic_not_applied", repair)

    def test_provider_dictionary_ids_never_enter_a_set_unhashably(self) -> None:
        artifact = oracle_artifact("provider_selection")
        artifact["selected_providers"] = [
            {"provider_id": "A"},
            {"id": "B"},
            {"provider": "C"},
        ]
        dependencies = {
            dependency: oracle_artifact(dependency)
            for dependency in next(
                node for node in NODES if node.node_id == "provider_selection"
            ).dependencies
        }
        validation = validate_node(
            "provider_selection", artifact, dependencies
        )
        self.assertTrue(validation.accepted, validation.diagnostic)

    def test_deterministic_provider_solver_recomputes_both_phases(self) -> None:
        self.assertEqual(
            ["A", "B", "C"],
            _deterministic_provider_selection(incident=False)[
                "selected_providers"
            ],
        )
        self.assertEqual(
            ["A", "C", "D"],
            _deterministic_provider_selection(incident=True)[
                "selected_providers"
            ],
        )

    def test_bounded_json_repair_handles_trailing_comma_and_closure(self) -> None:
        value, repairs = _extract_repaired_json_object(
            '{"shots":[],"new_keyframe_count":0,'
            '"traceable_claims":true,"estimated_generation_calls":0,'
        )
        self.assertEqual([], value["shots"])
        self.assertIn("json:trailing_comma", repairs)
        self.assertIn("json:balanced_closure", repairs)

    def test_media_package_requests_json_mode_and_budgeted_gpt_fallback(self) -> None:
        order, request = _request(
            node_id="media_package",
            policy_id="costweave_dynamic",
            objective="fixture",
            max_output_tokens=2048,
            cost_limit=0.1,
            attempt="primary",
        )
        self.assertTrue(request.structured_output)
        self.assertEqual(8, order.output_schema["properties"]["shots"]["minItems"])
        self.assertEqual(
            {"slot": "gpt", "reasoning_effort": "low"},
            _fallback_route(
                "media_package",
                {"slot": "claude", "reasoning_effort": "medium"},
            ),
        )

    def test_both_planners_use_larger_json_budget_and_have_gpt_fallback(self) -> None:
        for node_id in ("planner_a", "planner_b"):
            _, request = _request(
                node_id=node_id,
                policy_id="costweave_dynamic",
                objective="fixture",
                max_output_tokens=1024,
                cost_limit=0.1,
                attempt="primary",
            )
            self.assertEqual(1024, request.max_output_tokens)
            self.assertTrue(request.structured_output)
            self.assertEqual(
                {"slot": "gpt", "reasoning_effort": "low"},
                _fallback_route(
                    node_id,
                    {"slot": "claude", "reasoning_effort": "high"},
                ),
            )

    def test_length_finish_reason_is_truncation_not_json_format_failure(self) -> None:
        self.assertEqual(
            "truncated",
            _failure_category(
                {
                    "status": "completed",
                    "accepted": False,
                    "diagnostic": "truncated_output",
                    "finish_reasons": ["length"],
                }
            ),
        )

    def test_semantic_security_review_failure_remains_retryable(self) -> None:
        self.assertEqual(
            "semantic",
            _failure_category(
                {
                    "status": "completed",
                    "accepted": False,
                    "diagnostic": "security_review_oracle_rejected",
                }
            ),
        )
        self.assertEqual(
            "hard_security",
            _failure_category(
                {
                    "status": "failed",
                    "diagnostic": "secret_exposure_detected",
                }
            ),
        )

    def test_worst_case_projection_is_soft_when_each_call_fits(self) -> None:
        rows = [
            {
                "required_work_order_cost_limit_usd": 0.6,
                "affordable": True,
            }
            for _ in range(3)
        ]
        gate = _preflight_budget_gate(rows, budget_usd=1.0)
        self.assertTrue(gate["hard_gate_passed"])
        self.assertTrue(gate["staged_admission_required"])
        self.assertEqual(1.8, gate["projected_worst_case_usd"])

    def test_single_call_larger_than_total_budget_remains_blocked(self) -> None:
        gate = _preflight_budget_gate(
            [
                {
                    "required_work_order_cost_limit_usd": 1.1,
                    "affordable": False,
                }
            ],
            budget_usd=1.0,
        )
        self.assertFalse(gate["hard_gate_passed"])

    def test_runtime_admission_defers_without_aborting_the_wave(self) -> None:
        plans = {
            "requirements_extract": {
                "provider_id": "p",
                "required_work_order_cost_limit_usd": 0.4,
            },
            "contract_truth": {
                "provider_id": "p",
                "required_work_order_cost_limit_usd": 0.4,
            },
            "security_review": {
                "provider_id": "p",
                "required_work_order_cost_limit_usd": 0.45,
            },
        }
        admitted, deferred = _select_reservation_wave(
            ["requirements_extract", "contract_truth", "security_review"],
            plans,
            total_remaining_usd=0.85,
            provider_remaining_usd={"p": 0.85},
        )
        self.assertEqual(["requirements_extract", "security_review"], admitted)
        self.assertEqual(["contract_truth"], deferred)

    def test_final_gate_report_accepts_verified_boolean_and_mapping(self) -> None:
        context = {node.node_id: oracle_artifact(node.node_id) for node in NODES}
        for gate_report in (
            True,
            {"budget": True, "capacity": True, "security": True},
        ):
            artifact = oracle_artifact("final_assembly")
            artifact["hard_gates_passed"] = gate_report
            self.assertTrue(
                validate_node(
                    "final_assembly",
                    artifact,
                    context,
                    incident_revealed=True,
                ).accepted
            )

    def test_same_stage_dependency_runs_in_a_later_wave(self) -> None:
        def execute_node(**kwargs):
            node_id = kwargs["node_id"]
            return (
                {
                    "node_id": node_id,
                    "status": "completed",
                    "accepted": True,
                    "actual_cost_usd": 0.0,
                    "elapsed_ms": 1.0,
                    "fallback_used": False,
                },
                oracle_artifact(node_id),
            )

        with patch(
            "costweave.aurora_real_pilot._execute_node",
            side_effect=execute_node,
        ), patch(
            "costweave.aurora_real_pilot._run_virtual_revisions",
            return_value=([], True),
        ):
            result = _execute_policy(
                policy_id=ALL_PREMIUM,
                routes=freeze_routes()[ALL_PREMIUM],
                providers={},
                store=object(),
            )

        stage_five = next(
            stage for stage in result["stages"] if stage["stage"] == 5
        )
        deployment_wave = next(
            index
            for index, wave in enumerate(stage_five["waves"])
            if "deployment_plan" in wave
        )
        rollback_wave = next(
            index
            for index, wave in enumerate(stage_five["waves"])
            if "rollback_plan" in wave
        )
        self.assertLess(deployment_wave, rollback_wave)
        self.assertEqual(24, result["accepted_primary_artifacts"])
        self.assertTrue(result["accepted"])


if __name__ == "__main__":
    unittest.main()
