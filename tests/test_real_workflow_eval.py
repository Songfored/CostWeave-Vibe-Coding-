from __future__ import annotations

import unittest

from costweave.adapters import ExperimentalAdapterConfig
from costweave.experimental_calls import ExperimentalCallError
from costweave.real_workflow_eval import (
    MANUAL_ROUTES,
    WORKFLOWS,
    freeze_routes,
    validate_final,
    validate_node,
)


class RealWorkflowEvalTests(unittest.TestCase):
    def test_relay_reservation_multiplier_is_bounded(self):
        config = ExperimentalAdapterConfig(
            adapter_id="fixture", provider="relay", endpoint="https://example.test/v1/chat/completions",
            model_id="fixture-model", deployment="remote", input_reservation_multiplier=4.0,
        )
        self.assertEqual(4.0, config.input_reservation_multiplier)
        with self.assertRaises(ExperimentalCallError):
            ExperimentalAdapterConfig(
                adapter_id="fixture", provider="relay", endpoint="https://example.test/v1/chat/completions",
                model_id="fixture-model", deployment="remote", input_reservation_multiplier=.5,
            )

    def test_two_workflows_have_six_nodes_and_real_parallel_stages(self):
        self.assertEqual(2, len(WORKFLOWS))
        for workflow in WORKFLOWS:
            self.assertEqual(6, len(workflow.nodes))
            self.assertTrue(any(len(stage) > 1 for stage in workflow.stages))
            self.assertEqual(set(workflow.node_map()), set(MANUAL_ROUTES[workflow.workflow_id]))

    def test_node_validator_requires_json_and_all_keys(self):
        node = WORKFLOWS[0].node_map()["planner"]
        accepted = validate_node(
            node,
            '{"work_items":[],"parallel_group":[],"success_conditions":[]}',
        )
        self.assertTrue(accepted["accepted"])
        self.assertFalse(validate_node(node, '{"work_items":[]}')["accepted"])
        self.assertFalse(validate_node(node, "not json")["accepted"])

    def test_costweave_routes_are_frozen_before_execution(self):
        providers = {
            "deepseek-official": {"selected_model": "deepseek-v4-pro"},
            "traveltoken-gpt": {"selected_model": "gpt-5.5"},
            "traveltoken-claude": {"selected_model": "claude-sonnet-4-6"},
        }
        frozen = freeze_routes(providers)
        self.assertEqual(64, len(frozen["sha256"]))
        self.assertEqual(set(item.workflow_id for item in WORKFLOWS), set(frozen["workflows"]))
        for workflow in WORKFLOWS:
            self.assertEqual(set(workflow.node_map()), set(frozen["workflows"][workflow.workflow_id]["routes"]))

    def test_software_final_validator_is_exact(self):
        valid = {
            "status": "completed",
            "root_cause_ids": ["zero_sum_division", "independent_rounding_loses_total"],
            "required_guards": ["total_nonnegative", "weights_nonempty", "weights_nonnegative", "positive_weight_exists"],
            "allocation_algorithm": "largest_remainder_cents",
            "tests": ["all_zero_rejected", "thirds_sum_exactly", "negative_weight_rejected", "normal_case_preserved"],
            "review_passed": True,
        }
        self.assertTrue(validate_final("W09-software-fix", valid)["accepted"])
        valid["status"] = "accepted"
        self.assertTrue(validate_final("W09-software-fix", valid)["accepted"])
        valid["status"] = "verified"
        self.assertTrue(validate_final("W09-software-fix", valid)["accepted"])
        valid["review_passed"] = False
        self.assertFalse(validate_final("W09-software-fix", valid)["accepted"])

    def test_multiplanner_final_validator_is_exact(self):
        valid = {
            "status": "completed",
            "selected_option": "B",
            "selected_cost": 110,
            "selected_days": 8,
            "rejected_reason_codes": ["A_residency_violation", "C_budget_violation", "C_deadline_violation"],
            "conflict_id": "B_marketing_vs_contract_price",
            "authoritative_source": "signed_contract",
            "all_hard_constraints_passed": True,
        }
        self.assertTrue(validate_final("W10-multiplanner-decision", valid)["accepted"])
        valid["status"] = "verified"
        self.assertTrue(validate_final("W10-multiplanner-decision", valid)["accepted"])
        valid["selected_cost"] = 90
        self.assertFalse(validate_final("W10-multiplanner-decision", valid)["accepted"])


if __name__ == "__main__":
    unittest.main()
