from __future__ import annotations

import dataclasses
import unittest

from costweave.adapters import MockProvider, MockScenario
from costweave.deliberation import DeliberationSimulator
from costweave.domain import RunMode, RunRequest, WorkerProfile
from costweave.planner import HeuristicPlanner
from costweave.planning_contracts import (
    ManagementLimits,
    PanelPolicy,
    PlanningBudget,
    PlanningContractError,
    PlanningDecision,
    PlanningMode,
    ResultEnvelope,
    WorkOrder,
)
from costweave.role_registry import RoleRegistry


def worker(
    worker_id: str,
    provider: str,
    *,
    local: bool = False,
    risk: float = 0.7,
) -> WorkerProfile:
    return WorkerProfile(
        id=worker_id,
        name=worker_id,
        specialty="planner",
        capabilities={
            "planning": 0.88,
            "analysis": 0.86,
            "risk": risk,
            "validation": risk,
        },
        cost_per_task=0.03,
        latency_factor=1.0,
        reliability=0.91,
        local=local,
        provider=provider,
        model_id=f"{worker_id}-model",
        reasoning=0.87,
    )


def work_order() -> WorkOrder:
    return WorkOrder(
        work_order_id="work-1",
        task_id="task-1",
        objective="produce a bounded proposal",
        dependencies=(),
        input_artifacts=(),
        context_manifest=({"kind": "goal", "value": "test"},),
        assumptions=("offline simulation",),
        allowed_tools=("structured-output",),
        forbidden_actions=("network",),
        output_schema={"summary": "string"},
        acceptance_tests=("summary exists",),
        cost_limit=0.1,
        time_limit_ms=1000,
        escalation_conditions=("contract cannot be satisfied",),
        idempotency_key="work-order-key",
    )


class PlanningContractTests(unittest.TestCase):
    def test_management_depth_is_frozen_at_two(self):
        self.assertEqual(2, ManagementLimits().max_management_depth)
        with self.assertRaises(PlanningContractError):
            ManagementLimits(max_management_depth=3)

    def test_recursive_governor_is_forbidden(self):
        with self.assertRaises(PlanningContractError):
            ManagementLimits(governor_can_spawn_governor=True)

    def test_recursive_planner_is_forbidden(self):
        with self.assertRaises(PlanningContractError):
            ManagementLimits(planner_can_spawn_planner=True)

    def test_planning_budget_cannot_exceed_quarter(self):
        with self.assertRaises(PlanningContractError):
            PlanningBudget(1.0, 1000, 1, 0.251)

    def test_multi_planner_requires_two_or_three_roles(self):
        with self.assertRaises(PlanningContractError):
            PlanningDecision(
                decision_id="decision",
                mode=PlanningMode.MULTI_PLANNER,
                confidence=0.8,
                reasons=("test",),
                rejected_modes={},
                planning_budget=PlanningBudget(0.1, 1000, 1, 0.1),
                panel_policy=PanelPolicy(
                    size=1,
                    selected_roles=("domain-planner",),
                ),
                stop_conditions=("stop",),
                escalation_conditions=("escalate",),
            )

    def test_panel_size_must_match_roles(self):
        with self.assertRaises(PlanningContractError):
            PanelPolicy(size=2, selected_roles=("domain-planner",))

    def test_work_order_is_json_safe(self):
        payload = work_order().to_dict()
        self.assertEqual("v0.5.2", payload["schema_version"])
        self.assertEqual(["summary exists"], payload["acceptance_tests"])

    def test_work_order_requires_acceptance_tests(self):
        with self.assertRaises(PlanningContractError):
            dataclasses.replace(work_order(), acceptance_tests=())

    def test_result_escalation_requires_reason(self):
        with self.assertRaises(PlanningContractError):
            ResultEnvelope(
                work_order_id="work-1",
                status="escalated",
                output={},
                evidence=(),
                acceptance_results=(),
                assumptions_used=(),
                unresolved_risks=(),
                actual_cost=0,
                actual_latency_ms=1,
                confidence=0.5,
                escalation_requested=True,
            )


class RoleRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = RoleRegistry()
        self.workers = [
            worker("alpha", "provider-a"),
            worker("beta", "provider-b"),
            worker("gamma", "provider-c"),
        ]

    def test_registry_defines_three_complementary_roles(self):
        self.assertEqual(
            {
                "domain-planner",
                "economics-planner",
                "risk-planner",
            },
            {item.role_id for item in self.registry.list()},
        )

    def test_roles_cannot_spawn_more_planners(self):
        for role in self.registry.list():
            self.assertEqual(2, role.management_level)
            self.assertIn("spawn_planner", role.forbidden_actions)

    def test_assignments_are_provider_diverse_when_possible(self):
        assignments = self.registry.assign(
            (
                "domain-planner",
                "economics-planner",
                "risk-planner",
            ),
            self.workers,
        )
        self.assertEqual(3, len({item.provider for item in assignments}))

    def test_assignment_survives_empty_catalog(self):
        assignment = self.registry.assign(("domain-planner",), [])[0]
        self.assertIsNone(assignment.worker_id)
        self.assertEqual(0.0, assignment.score)

    def test_unroutable_workers_are_ignored(self):
        only = dataclasses.replace(self.workers[0], routable=False)
        assignment = self.registry.assign(("domain-planner",), [only])[0]
        self.assertIsNone(assignment.worker_id)

    def test_cost_sensitive_assignment_avoids_equivalent_premium_model(self):
        cheap = dataclasses.replace(
            self.workers[0],
            id="cheap",
            model_id="cheap",
            input_price_per_mtok=0.2,
            output_price_per_mtok=0.8,
        )
        premium = dataclasses.replace(
            cheap,
            id="premium",
            model_id="premium",
            input_price_per_mtok=10,
            output_price_per_mtok=50,
        )
        assignment = self.registry.assign(
            ("domain-planner",),
            [premium, cheap],
            prefer_diversity=False,
            reasoning_floor=0.7,
            cost_sensitivity=0.24,
        )[0]
        self.assertEqual("cheap", assignment.worker_id)

    def test_reasoning_floor_refuses_inadequate_cheap_model(self):
        weak = dataclasses.replace(
            self.workers[0],
            id="weak",
            model_id="weak",
            reasoning=0.45,
            input_price_per_mtok=0,
            output_price_per_mtok=0,
        )
        capable = dataclasses.replace(
            self.workers[1],
            id="capable",
            model_id="capable",
            reasoning=0.86,
            input_price_per_mtok=2,
            output_price_per_mtok=8,
        )
        assignment = self.registry.assign(
            ("domain-planner",),
            [weak, capable],
            prefer_diversity=False,
            reasoning_floor=0.7,
            cost_sensitivity=0.24,
        )[0]
        self.assertEqual("capable", assignment.worker_id)


class DeliberationSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.request = RunRequest(
            goal="设计并验证一个成本感知的多模型任务流程",
            mode=RunMode.BALANCED,
            budget=2.0,
            quality_floor=0.8,
        )
        self.planner = HeuristicPlanner()
        self.base = self.planner.analyze(self.request)
        self.workers = [
            worker("alpha", "provider-a"),
            worker("beta", "provider-b"),
            worker("gamma", "provider-c"),
        ]
        self.simulator = DeliberationSimulator()

    def decide(self, **changes):
        analysis = dataclasses.replace(self.base, **changes)
        return self.simulator.decide(
            self.request, analysis, self.workers
        )

    def test_no_delegation_for_trivial_clear_task(self):
        decision = self.decide(
            difficulty=2,
            ambiguity=0.05,
            estimate_confidence=0.9,
            stakes="normal",
        )
        self.assertEqual(PlanningMode.NO_DELEGATION, decision.mode)
        self.assertEqual(0, decision.planning_budget.max_calls)

    def test_direct_delegation_for_small_single_domain_task(self):
        decision = self.decide(
            difficulty=4,
            ambiguity=0.05,
            estimate_confidence=0.9,
            classification_confidence=0.9,
            task_types=["coding"],
            stakes="normal",
        )
        self.assertEqual(PlanningMode.DIRECT_DELEGATION, decision.mode)

    def test_single_planner_is_normal_fallback(self):
        decision = self.decide(
            difficulty=5,
            ambiguity=0.1,
            uncertainty=0.15,
            estimate_confidence=0.85,
            task_types=["analysis"],
            stakes="normal",
        )
        self.assertEqual(PlanningMode.SINGLE_PLANNER, decision.mode)

    def test_multi_planner_for_complex_cross_domain_task(self):
        decision = self.decide(
            difficulty=8,
            ambiguity=0.3,
            uncertainty=0.45,
            estimate_confidence=0.82,
            task_types=["coding", "research"],
            stakes="elevated",
        )
        self.assertEqual(PlanningMode.MULTI_PLANNER, decision.mode)
        self.assertEqual(3, decision.panel_policy.size)

    def test_high_stakes_forces_multi_planner(self):
        decision = self.decide(
            difficulty=6,
            ambiguity=0.1,
            estimate_confidence=0.85,
            stakes="high",
        )
        self.assertEqual(PlanningMode.MULTI_PLANNER, decision.mode)
        self.assertLessEqual(
            decision.planning_budget.task_budget_ratio, 0.25
        )

    def test_safety_branch_cannot_be_directly_delegated(self):
        decision = self.decide(
            difficulty=4,
            ambiguity=0.05,
            estimate_confidence=0.9,
            classification_confidence=0.9,
            task_types=["safety"],
            stakes="elevated",
        )
        self.assertEqual(PlanningMode.MULTI_PLANNER, decision.mode)

    def test_low_confidence_uses_expert_override(self):
        decision = self.decide(
            difficulty=6,
            ambiguity=0.2,
            estimate_confidence=0.55,
            stakes="normal",
        )
        self.assertEqual(PlanningMode.EXPERT_OVERRIDE, decision.mode)

    def test_high_ambiguity_requests_clarification(self):
        decision = self.decide(
            difficulty=6,
            ambiguity=0.7,
            estimate_confidence=0.5,
            stakes="normal",
        )
        self.assertEqual(PlanningMode.ASK_USER, decision.mode)

    def test_same_input_produces_same_fingerprint(self):
        first = self.decide(
            difficulty=8,
            ambiguity=0.3,
            estimate_confidence=0.8,
            task_types=["coding", "research"],
        )
        second = self.decide(
            difficulty=8,
            ambiguity=0.3,
            estimate_confidence=0.8,
            task_types=["coding", "research"],
        )
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_proposals_respect_panel_and_budget(self):
        analysis = dataclasses.replace(
            self.base,
            difficulty=8,
            ambiguity=0.3,
            estimate_confidence=0.8,
            task_types=["coding", "research"],
        )
        decision = self.simulator.decide(
            self.request, analysis, self.workers
        )
        proposals = self.simulator.proposals(
            decision, self.request, analysis
        )
        self.assertEqual(decision.panel_policy.size, len(proposals))
        self.assertLessEqual(
            sum(item.estimated_planning_cost for item in proposals),
            decision.planning_budget.max_cost + 1e-9,
        )

    def test_no_delegation_has_no_proposals(self):
        analysis = dataclasses.replace(
            self.base,
            difficulty=2,
            ambiguity=0.05,
            estimate_confidence=0.9,
            stakes="normal",
        )
        decision = self.simulator.decide(
            self.request, analysis, self.workers
        )
        self.assertEqual(
            [], self.simulator.proposals(decision, self.request, analysis)
        )


class MockProviderTests(unittest.TestCase):
    def test_default_adapter_never_enables_paid_calls(self):
        provider = MockProvider()
        self.assertFalse(provider.capabilities.paid_calls_enabled)

    def test_completed_result_is_deterministic(self):
        provider = MockProvider()
        first = provider.execute(work_order())
        second = provider.execute(work_order())
        self.assertEqual(first.output, second.output)
        self.assertEqual(0.0, first.actual_cost)
        self.assertEqual(2, provider.calls)

    def test_rate_limit_requests_escalation(self):
        provider = MockProvider(MockScenario(outcome="rate_limited"))
        result = provider.execute(work_order())
        self.assertTrue(result.escalation_requested)
        self.assertEqual("rate_limited", result.status)


if __name__ == "__main__":
    unittest.main()
