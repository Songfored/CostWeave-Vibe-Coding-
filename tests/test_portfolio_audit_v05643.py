import sqlite3
import tempfile
import unittest
from dataclasses import replace

from costweave.event_store import EventStore
from costweave.portfolio_audit import (
    GlobalPortfolioAuditEngine,
    PortfolioAuditError,
    PortfolioEdge,
    PortfolioNode,
    PortfolioOption,
    PortfolioProblem,
    compare_portfolio_scenarios,
    public_portfolio_audit_policy,
)
from costweave.portfolio_audit_store import PortfolioAuditStore


GROUP = "a" * 64


def option(
    option_id,
    node_id,
    model_id,
    provider,
    pool,
    domain,
    lower,
    cost,
    latency,
    capability,
    *,
    evidence="E0-rule-prior",
):
    return PortfolioOption(
        option_id=option_id,
        node_id=node_id,
        model_id=model_id,
        deployment_id=f"deploy-{option_id}",
        provider=provider,
        resource_pool=pool,
        failure_domain=domain,
        success_mean=min(1.0, lower + 0.02),
        success_lower=lower,
        direct_model_cost_usd=cost,
        validation_cost_usd=0.0,
        tool_cost_usd=0.0,
        rework_cost_if_failed_usd=0.2,
        escalation_cost_if_failed_usd=0.1,
        latency_p50_ms=max(0, latency // 2),
        latency_p95_ms=latency,
        capabilities=(capability,),
        residency_tags=("eu",),
        evidence_level=evidence,
    )


def base_problem(*, topology="fine_grained", evidence="E0-rule-prior"):
    nodes = (
        PortfolioNode("root", "planning", "high", 1.0, ("architecture",)),
        PortfolioNode("frontend", "execution", "medium", 0.8, ("build",)),
        PortfolioNode("backend", "execution", "medium", 0.7, ("build",)),
        PortfolioNode("validator", "validation", "high", 0.9, ("review",)),
    )
    edges = (
        PortfolioEdge("root", "frontend", transfer_tokens=1000,
                      transfer_latency_ms=10, invalidation_cost_usd=0.6),
        PortfolioEdge("root", "backend", transfer_tokens=1000,
                      transfer_latency_ms=10, invalidation_cost_usd=0.6),
        PortfolioEdge("frontend", "validator", "validation", 500, 5, 0.05),
        PortfolioEdge("backend", "validator", "validation", 500, 5, 0.05),
    )
    options = (
        option("root-cheap", "root", "small-planner", "alpha", "alpha-pool",
               "alpha-domain", 0.70, 0.02, 100, "architecture", evidence=evidence),
        option("root-strong", "root", "strong-planner", "beta", "beta-pool",
               "beta-domain", 0.97, 0.12, 120, "architecture", evidence=evidence),
        option("frontend-default", "frontend", "builder-front", "alpha",
               "alpha-pool", "alpha-domain", 0.90, 0.03, 200, "build",
               evidence=evidence),
        option("backend-default", "backend", "builder-back", "alpha",
               "alpha-pool", "alpha-domain", 0.90, 0.03, 300, "build",
               evidence=evidence),
        option("validator-independent", "validator", "reviewer", "audit",
               "audit-pool", "audit-domain", 0.98, 0.04, 50, "review",
               evidence=evidence),
    )
    return PortfolioProblem(
        comparison_group_hash=GROUP,
        topology_kind=topology,
        nodes=nodes,
        edges=edges,
        options=options,
        hard_budget_usd=10.0,
        deadline_ms=5000,
        provider_quotas={"alpha": 10, "beta": 10, "audit": 10},
        pool_capacities={"alpha-pool": 4, "beta-pool": 2, "audit-pool": 2},
        minimum_node_success=0.6,
        minimum_portfolio_success=0.5,
        value_of_time_per_second=0.001,
        failure_loss_usd=0.2,
        correlation_penalty_usd=0.1,
    )


class PortfolioAuditTests(unittest.TestCase):
    def setUp(self):
        self.engine = GlobalPortfolioAuditEngine()

    def test_strong_upstream_can_beat_cheaper_option_by_avoiding_rework(self):
        result = self.engine.solve(base_problem())
        self.assertTrue(result.feasible)
        self.assertEqual("root-strong", result.selected_options["root"])
        self.assertGreater(result.metrics["expected_invalidation_usd"], 0)

    def test_critical_path_respects_parallel_branches(self):
        result = self.engine.solve(base_problem())
        self.assertEqual(485, result.metrics["critical_path_p95_ms"])
        sequential = sum(
            item.latency_p95_ms for item in base_problem().options
            if item.option_id in result.selected_options.values()
        )
        self.assertLess(result.metrics["critical_path_p95_ms"], sequential)

    def test_parallel_pool_capacity_is_a_hard_constraint(self):
        problem = base_problem()
        problem = replace(
            problem,
            pool_capacities={**problem.pool_capacities, "alpha-pool": 1},
        )
        result = self.engine.solve(problem)
        self.assertFalse(result.feasible)
        self.assertFalse(result.routing_authority)

    def test_provider_quota_forces_the_alternate_planner(self):
        problem = base_problem()
        problem = replace(
            problem,
            provider_quotas={**problem.provider_quotas, "beta": 0},
        )
        result = self.engine.solve(problem)
        self.assertTrue(result.feasible)
        self.assertEqual("root-cheap", result.selected_options["root"])

    def test_high_risk_validator_must_use_an_independent_failure_domain(self):
        problem = base_problem()
        bad = replace(
            problem.options[-1],
            failure_domain="alpha-domain",
        )
        result = self.engine.solve(
            replace(problem, options=(*problem.options[:-1], bad))
        )
        self.assertFalse(result.feasible)

    def test_capability_and_residency_filters_fail_closed(self):
        problem = base_problem()
        root = replace(
            problem.nodes[0],
            required_capabilities=("architecture", "security"),
            required_residency_tags=("eu",),
        )
        result = self.engine.solve(
            replace(problem, nodes=(root, *problem.nodes[1:]))
        )
        self.assertFalse(result.feasible)
        self.assertIn("no_feasible_option:root", result.rejection_reasons)

    def test_budget_and_deadline_block_infeasible_portfolios(self):
        problem = base_problem()
        self.assertFalse(
            self.engine.solve(replace(problem, hard_budget_usd=0.01)).feasible
        )
        self.assertFalse(
            self.engine.solve(replace(problem, deadline_ms=100)).feasible
        )

    def test_correlated_failure_penalty_is_visible(self):
        problem = base_problem()
        same_domain = replace(
            problem.options[1],
            failure_domain="alpha-domain",
        )
        same = self.engine.solve(
            replace(
                problem,
                options=(same_domain, *problem.options[2:]),
                provider_quotas={"beta": 10, "alpha": 10, "audit": 10},
            )
        )
        different = self.engine.solve(
            replace(problem, options=(problem.options[1], *problem.options[2:]))
        )
        self.assertGreater(
            same.metrics["correlated_failure_penalty_usd"],
            different.metrics["correlated_failure_penalty_usd"],
        )

    def test_e0_is_not_directional_and_e2_still_has_no_authority(self):
        e0 = self.engine.solve(base_problem())
        e2 = self.engine.solve(base_problem(evidence="E2-replay"))
        self.assertFalse(e0.directional_evidence_eligible)
        self.assertTrue(e2.directional_evidence_eligible)
        self.assertTrue(e2.advisory_only)
        self.assertFalse(e2.routing_authority)
        self.assertFalse(e2.may_change_actual_route)
        self.assertFalse(e2.commercial_claims_allowed)

    def test_solver_is_deterministic_and_discloses_beam_pruning(self):
        first = self.engine.solve(base_problem())
        second = self.engine.solve(base_problem())
        self.assertEqual(first.audit_id, second.audit_id)
        narrow = GlobalPortfolioAuditEngine(beam_width=1).solve(base_problem())
        self.assertGreater(narrow.solver_audit["beam_pruned"], 0)
        self.assertFalse(narrow.solver_audit["optimality_proven"])

    def test_scenario_comparison_is_advisory(self):
        fine = self.engine.solve(base_problem(topology="fine_grained"))
        consolidated_problem = base_problem(topology="consolidated")
        cheaper = tuple(
            replace(item, direct_model_cost_usd=item.direct_model_cost_usd / 2)
            for item in consolidated_problem.options
        )
        consolidated = self.engine.solve(
            replace(consolidated_problem, options=cheaper)
        )
        comparison = compare_portfolio_scenarios((fine, consolidated))
        self.assertEqual(consolidated.audit_id, comparison.preferred_audit_id)
        self.assertFalse(comparison.routing_authority)
        self.assertFalse(comparison.commercial_claims_allowed)

    def test_scenarios_from_different_groups_are_rejected(self):
        first = self.engine.solve(base_problem())
        second_problem = replace(
            base_problem(topology="consolidated"),
            comparison_group_hash="b" * 64,
        )
        with self.assertRaises(PortfolioAuditError):
            compare_portfolio_scenarios((first, self.engine.solve(second_problem)))

    def test_cycle_and_management_depth_above_two_are_rejected(self):
        problem = base_problem()
        with self.assertRaises(PortfolioAuditError):
            replace(
                problem,
                edges=(*problem.edges, PortfolioEdge("validator", "root")),
            )
        deep = replace(problem.nodes[0], management_depth=3)
        with self.assertRaises(PortfolioAuditError):
            replace(problem, nodes=(deep, *problem.nodes[1:]))

    def test_contract_rejects_duplicate_edges_unknown_evidence_and_fake_comparison(self):
        problem = base_problem()
        with self.assertRaises(PortfolioAuditError):
            replace(problem, edges=(*problem.edges, problem.edges[0]))
        bad_evidence = replace(problem.options[0], evidence_level="unknown")
        with self.assertRaises(PortfolioAuditError):
            replace(problem, options=(bad_evidence, *problem.options[1:]))
        first = self.engine.solve(problem)
        with self.assertRaises(PortfolioAuditError):
            compare_portfolio_scenarios((first, first))

    def test_public_policy_states_scope_and_zero_authority(self):
        policy = public_portfolio_audit_policy()
        self.assertEqual("v0.5.6.4.3", policy["schema_version"])
        self.assertEqual("bounded-deterministic-beam-search", policy["solver"])
        self.assertFalse(policy["may_change_actual_route"])
        self.assertFalse(policy["external_calls_required"])
        self.assertFalse(policy["commercial_claims_allowed"])


class PortfolioAuditStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        EventStore(self.path)
        self.store = PortfolioAuditStore(self.path)
        self.engine = GlobalPortfolioAuditEngine()

    def tearDown(self):
        self.temp.cleanup()

    def test_audits_and_comparisons_are_immutable_and_counted(self):
        fine = self.engine.solve(base_problem(topology="fine_grained"))
        consolidated = self.engine.solve(base_problem(topology="consolidated"))
        comparison = compare_portfolio_scenarios((fine, consolidated))
        self.assertTrue(self.store.record_audit(fine))
        self.assertFalse(self.store.record_audit(fine))
        self.assertTrue(self.store.record_audit(consolidated))
        self.assertTrue(self.store.record_comparison(comparison))
        self.assertFalse(self.store.record_comparison(comparison))
        self.assertEqual(fine.audit_id, self.store.audit(fine.audit_id)["audit_id"])
        self.assertEqual(
            comparison.comparison_id,
            self.store.comparison(comparison.comparison_id)["comparison_id"],
        )
        counts = self.store.counts()
        self.assertEqual(2, counts["portfolio_audits"])
        self.assertEqual(1, counts["portfolio_scenario_comparisons"])
        self.assertEqual(0, counts["portfolio_route_interventions"])

    def test_integrity_tampering_is_detected(self):
        result = self.engine.solve(base_problem())
        self.store.record_audit(result)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE portfolio_audit_reports SET report_json='{}' "
                "WHERE audit_id=?",
                (result.audit_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(PortfolioAuditError):
            self.store.audit(result.audit_id)


if __name__ == "__main__":
    unittest.main()
