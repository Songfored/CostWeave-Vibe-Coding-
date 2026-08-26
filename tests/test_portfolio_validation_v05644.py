import sqlite3
import tempfile
import unittest
from dataclasses import replace

from costweave.event_store import EventStore
from costweave.portfolio_audit import (
    GlobalPortfolioAuditEngine,
    PortfolioAuditError,
)
from costweave.portfolio_audit_store import PortfolioAuditStore
from costweave.portfolio_validation import (
    ExactPortfolioOracle,
    PortfolioSensitivityAnalyzer,
    SensitivityProbe,
    compare_with_exact_oracle,
    public_portfolio_validation_policy,
)
from costweave.portfolio_validation_store import PortfolioValidationStore
from tests.test_portfolio_audit_v05643 import base_problem


class ExactPortfolioOracleTests(unittest.TestCase):
    def test_exact_oracle_proves_the_best_small_dag_assignment(self):
        result = ExactPortfolioOracle().solve(base_problem())
        self.assertEqual("optimal", result.status)
        self.assertTrue(result.optimality_proven)
        self.assertTrue(result.feasible)
        self.assertEqual("root-strong", result.selected_options["root"])
        self.assertEqual(2, result.search_audit["candidate_combinations"])
        self.assertEqual(2, result.search_audit["assignments_examined"])

    def test_exact_oracle_is_deterministic(self):
        oracle = ExactPortfolioOracle()
        self.assertEqual(
            oracle.solve(base_problem()).oracle_id,
            oracle.solve(base_problem()).oracle_id,
        )

    def test_oracle_fails_closed_outside_node_and_combination_bounds(self):
        by_nodes = ExactPortfolioOracle(maximum_nodes=3).solve(base_problem())
        by_combinations = ExactPortfolioOracle(
            maximum_combinations=1
        ).solve(base_problem())
        self.assertEqual("skipped_bounds", by_nodes.status)
        self.assertEqual("node_limit", by_nodes.search_audit["skipped_reason"])
        self.assertEqual("skipped_bounds", by_combinations.status)
        self.assertEqual(
            "combination_limit", by_combinations.search_audit["skipped_reason"]
        )
        self.assertFalse(by_nodes.optimality_proven)
        self.assertIsNone(by_nodes.feasible)

    def test_oracle_can_prove_hard_constraint_infeasibility(self):
        result = ExactPortfolioOracle().solve(
            replace(base_problem(), hard_budget_usd=0.01)
        )
        self.assertEqual("infeasible_proven", result.status)
        self.assertFalse(result.feasible)
        self.assertTrue(result.optimality_proven)

    def test_default_beam_matches_exact_objective_and_claim(self):
        problem = base_problem()
        beam = GlobalPortfolioAuditEngine().solve(problem)
        oracle = ExactPortfolioOracle().solve(problem)
        report = compare_with_exact_oracle(beam, oracle)
        self.assertEqual("exact_selection_match", report.validation_state)
        self.assertTrue(report.exact_validation_passed)
        self.assertEqual(0.0, report.candidate_regret_usd)
        self.assertTrue(report.candidate_optimality_claim_valid)

    def test_oracle_detects_real_beam_pruning_regret(self):
        problem = replace(
            base_problem(),
            failure_loss_usd=0,
            correlation_penalty_usd=0,
            value_of_time_per_second=0,
        )
        narrow = GlobalPortfolioAuditEngine(beam_width=1).solve(problem)
        oracle = ExactPortfolioOracle().solve(problem)
        report = compare_with_exact_oracle(narrow, oracle)
        self.assertEqual("root-cheap", narrow.selected_options["root"])
        self.assertEqual("root-strong", oracle.selected_options["root"])
        self.assertEqual("suboptimal_candidate", report.validation_state)
        self.assertFalse(report.exact_validation_passed)
        self.assertGreater(report.candidate_regret_usd, 0.2)
        self.assertFalse(report.routing_authority)

    def test_skipped_oracle_never_validates_a_candidate(self):
        problem = base_problem()
        candidate = GlobalPortfolioAuditEngine().solve(problem)
        skipped = ExactPortfolioOracle(maximum_combinations=1).solve(problem)
        report = compare_with_exact_oracle(candidate, skipped)
        self.assertEqual("not_run_bounds", report.validation_state)
        self.assertFalse(report.exact_validation_passed)
        self.assertIsNone(report.objective_match)

    def test_differential_requires_the_identical_problem(self):
        candidate = GlobalPortfolioAuditEngine().solve(base_problem())
        other = ExactPortfolioOracle().solve(
            replace(base_problem(), hard_budget_usd=9.0)
        )
        with self.assertRaises(PortfolioAuditError):
            compare_with_exact_oracle(candidate, other)

    def test_public_validation_contracts_reject_inconsistent_claims(self):
        problem = base_problem()
        candidate = GlobalPortfolioAuditEngine().solve(problem)
        oracle = ExactPortfolioOracle().solve(problem)
        report = compare_with_exact_oracle(candidate, oracle)
        sensitivity = PortfolioSensitivityAnalyzer().analyze(
            problem,
            (
                SensitivityProbe(
                    "success", "root-strong", "success_lower", (0.7, 0.97)
                ),
            ),
        )
        with self.assertRaises(PortfolioAuditError):
            replace(oracle, feasible=False, selected_options={})
        with self.assertRaises(PortfolioAuditError):
            replace(report, validation_state="invented")
        with self.assertRaises(PortfolioAuditError):
            replace(sensitivity, robustness_grade="perfect")


class PortfolioSensitivityTests(unittest.TestCase):
    @staticmethod
    def probes():
        return (
            SensitivityProbe(
                "cost", "root-strong", "direct_model_cost_usd",
                (0.12, 0.4, 0.8, 1.2),
            ),
            SensitivityProbe(
                "success", "root-strong", "success_lower",
                (0.7, 0.8, 0.9, 0.97),
            ),
            SensitivityProbe(
                "latency", "root-strong", "latency_p95_ms",
                (120, 4000, 6000),
            ),
            SensitivityProbe(
                "rework", "root-strong", "rework_cost_if_failed_usd",
                (0.2, 2, 10),
            ),
        )

    def test_sensitivity_reports_route_flip_thresholds(self):
        result = PortfolioSensitivityAnalyzer().analyze(
            base_problem(), self.probes()
        )
        self.assertEqual(14, result.tested_point_count)
        by_id = {item["probe_id"]: item for item in result.probe_results}
        self.assertEqual(0.8, by_id["cost"]["nearest_flip_value"])
        self.assertEqual(0.7, by_id["success"]["nearest_flip_value"])
        self.assertEqual(6000.0, by_id["latency"]["nearest_flip_value"])
        self.assertEqual(10.0, by_id["rework"]["nearest_flip_value"])
        self.assertEqual("moderate", result.robustness_grade)
        self.assertFalse(result.routing_authority)

    def test_sensitivity_is_deterministic_and_scope_limited(self):
        analyzer = PortfolioSensitivityAnalyzer()
        first = analyzer.analyze(base_problem(), self.probes())
        second = analyzer.analyze(base_problem(), self.probes())
        self.assertEqual(first.sensitivity_id, second.sensitivity_id)
        self.assertFalse(first.commercial_claims_allowed)

    def test_e2_sensitivity_is_directional_but_still_non_authoritative(self):
        problem = base_problem(evidence="E2-replay")
        probe = SensitivityProbe(
            "success", "root-strong", "success_lower", (0.7, 0.97)
        )
        result = PortfolioSensitivityAnalyzer().analyze(problem, (probe,))
        self.assertTrue(result.directional_evidence_eligible)
        self.assertFalse(result.may_change_actual_route)

    def test_invalid_probes_fail_closed(self):
        analyzer = PortfolioSensitivityAnalyzer()
        with self.assertRaises(PortfolioAuditError):
            SensitivityProbe("bad", "root", "latency_p95_ms", (1.5,))
        with self.assertRaises(PortfolioAuditError):
            SensitivityProbe("bad", "root", "success_lower", (1.1,))
        with self.assertRaises(PortfolioAuditError):
            analyzer.analyze(
                base_problem(),
                (SensitivityProbe("unknown", "missing", "success_lower", (0.5,)),),
            )
        duplicate = SensitivityProbe(
            "same", "root-strong", "success_lower", (0.7,)
        )
        with self.assertRaises(PortfolioAuditError):
            analyzer.analyze(base_problem(), (duplicate, duplicate))

    def test_public_policy_discloses_exact_bounds_and_e0_scope(self):
        policy = public_portfolio_validation_policy()
        self.assertEqual("v0.5.6.4.4", policy["schema_version"])
        self.assertEqual(
            100_000, policy["exact_oracle"]["default_maximum_combinations"]
        )
        self.assertEqual("tested-points-only", policy["sensitivity_claim_scope"])
        self.assertFalse(policy["may_change_actual_route"])
        self.assertFalse(policy["external_calls_required"])


class PortfolioValidationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        EventStore(self.path)
        self.audit_store = PortfolioAuditStore(self.path)
        self.store = PortfolioValidationStore(self.path)
        self.problem = base_problem()
        self.candidate = GlobalPortfolioAuditEngine().solve(self.problem)
        self.oracle = ExactPortfolioOracle().solve(self.problem)
        self.differential = compare_with_exact_oracle(
            self.candidate, self.oracle
        )
        self.sensitivity = PortfolioSensitivityAnalyzer().analyze(
            self.problem,
            (
                SensitivityProbe(
                    "success",
                    "root-strong",
                    "success_lower",
                    (0.7, 0.97),
                ),
            ),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_validation_records_are_immutable_linked_and_counted(self):
        self.audit_store.record_audit(self.candidate)
        self.assertTrue(self.store.record_oracle(self.oracle))
        self.assertFalse(self.store.record_oracle(self.oracle))
        self.assertTrue(self.store.record_differential(self.differential))
        self.assertFalse(self.store.record_differential(self.differential))
        self.assertTrue(self.store.record_sensitivity(self.sensitivity))
        self.assertFalse(self.store.record_sensitivity(self.sensitivity))
        self.assertEqual(
            self.oracle.oracle_id,
            self.store.oracle(self.oracle.oracle_id)["oracle_id"],
        )
        self.assertEqual(
            self.differential.report_id,
            self.store.differential(self.differential.report_id)["report_id"],
        )
        self.assertEqual(
            self.sensitivity.sensitivity_id,
            self.store.sensitivity(self.sensitivity.sensitivity_id)[
                "sensitivity_id"
            ],
        )
        counts = self.store.counts()
        self.assertEqual(1, counts["portfolio_oracle_proofs"])
        self.assertEqual(1, counts["portfolio_differential_reports"])
        self.assertEqual(1, counts["portfolio_sensitivity_reports"])
        self.assertEqual(0, counts["portfolio_validation_route_interventions"])

    def test_links_fail_closed_and_integrity_tampering_is_detected(self):
        with self.assertRaises(PortfolioAuditError):
            self.store.record_differential(self.differential)
        with self.assertRaises(PortfolioAuditError):
            self.store.record_sensitivity(self.sensitivity)
        self.store.record_oracle(self.oracle)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE portfolio_oracle_results SET result_json='{}' "
                "WHERE oracle_id=?",
                (self.oracle.oracle_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(PortfolioAuditError):
            self.store.oracle(self.oracle.oracle_id)


if __name__ == "__main__":
    unittest.main()
