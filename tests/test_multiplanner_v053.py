from __future__ import annotations

import asyncio
import json
import unittest

from costweave.domain import Plan, RunRequest, TaskContract
from costweave.multiplanner import MultiPlannerEngine, PlanCompiler
from costweave.planner import TaskAnalysis
from costweave.planning_contracts import (
    AdjudicationResult,
    AnonymousProposal,
    ManagementLimits,
    PanelPolicy,
    PlanningBudget,
    PlanningContractError,
    PlanningDecision,
    PlanningMode,
    ProposalScorecard,
)


def analysis(*, stakes: str = "high") -> TaskAnalysis:
    return TaskAnalysis(
        task_types=["coding", "strategy", "safety"],
        difficulty=8,
        ambiguity=0.35,
        uncertainty=0.4,
        stakes=stakes,
        needs_fresh_data=False,
        needs_tools=[],
        modalities=["text"],
        decomposition_score=0.82,
        estimate_confidence=0.86,
        risk_signals=["high-loss"],
        primary_intent="build",
        intent_scores=[],
        operations=["build", "validate"],
        deliverables=["code", "plan"],
        constraints=["budget is hard", "parallel work must be dependency-safe"],
        classifications=[],
        classification_confidence=0.88,
        complexity={
            "domain": 0.8,
            "coordination": 0.8,
            "tools": 0.5,
            "evidence": 0.9,
        },
        clarification_questions=[],
        planning_action="continue",
    )


def decision(*, calls: int = 3) -> PlanningDecision:
    return PlanningDecision(
        decision_id="planning-v053-test",
        mode=PlanningMode.MULTI_PLANNER,
        confidence=0.86,
        reasons=("material cross-domain coupling",),
        rejected_modes={"SINGLE_PLANNER": "insufficient perspectives"},
        planning_budget=PlanningBudget(
            max_cost=0.3,
            max_latency_ms=3000,
            max_calls=calls,
            task_budget_ratio=0.15,
        ),
        panel_policy=PanelPolicy(
            size=3,
            required_diversity=("evaluation_focus",),
            selected_roles=(
                "domain-planner",
                "economics-planner",
                "risk-planner",
            ),
        ),
        stop_conditions=("one adjudication maximum",),
        escalation_conditions=("hard constraint failure",),
        limits=ManagementLimits(),
    )


def anonymous(
    alias: str,
    *,
    parallelism: str = "balanced",
    budget_posture: str = "balanced",
    verification: str = "per-node",
) -> AnonymousProposal:
    return AnonymousProposal(
        alias=alias,
        objective_interpretation="build a reliable service",
        decomposition_outline=("scope", "implement", "verify"),
        assumptions=("budget is hard",),
        risks=("integration drift",),
        acceptance_strategy=("run contract tests",),
        hard_constraints=("budget is hard",),
        strategy={
            "parallelism": parallelism,
            "budget_posture": budget_posture,
            "risk_posture": "contract-first",
            "verification": verification,
        },
        evidence=("request", "test contract"),
        confidence=0.82,
    )


class MultiPlannerEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = MultiPlannerEngine()
        self.request = RunRequest(
            goal="Build and validate a cross-domain safety-critical service",
            budget=2.0,
            quality_floor=0.88,
        )

    def run_panel(self, *, calls: int = 3):
        return asyncio.run(
            self.engine.deliberate(
                decision(calls=calls),
                self.request,
                analysis(),
            )
        )

    def test_three_roles_run_as_one_parallel_wave(self):
        proposals, receipt = self.run_panel()
        self.assertEqual(3, len(proposals))
        self.assertTrue(receipt.parallel)
        self.assertEqual(3, receipt.calls_used)
        self.assertLessEqual(receipt.actual_cost, 0.3)
        self.assertEqual(
            receipt.actual_latency_ms,
            max(item.estimated_planning_latency_ms for item in proposals),
        )

    def test_scoring_surface_is_identity_blind(self):
        _, receipt = self.run_panel()
        payload = json.dumps(
            [item.to_dict() for item in receipt.anonymous_proposals]
        )
        self.assertNotIn("planner_role", payload)
        self.assertNotIn("provider", payload)
        self.assertNotIn("model_id", payload)
        self.assertEqual(["A", "B", "C"], [
            item.alias for item in receipt.anonymous_proposals
        ])

    def test_every_proposal_has_constraint_scorecard(self):
        _, receipt = self.run_panel()
        self.assertEqual(3, len(receipt.scorecards))
        self.assertTrue(all(card.hard_constraints_passed for card in receipt.scorecards))
        self.assertTrue(all(0 <= card.total <= 1 for card in receipt.scorecards))
        self.assertTrue(all(len(card.scores) == 6 for card in receipt.scorecards))

    def test_material_strategy_conflict_is_explicit(self):
        _, receipt = self.run_panel()
        self.assertTrue(any(
            item.conflict_type == "parallelism_vs_control"
            and item.severity == "high"
            for item in receipt.conflicts
        ))
        self.assertEqual(1, receipt.adjudication.rounds_used)
        self.assertEqual("merged", receipt.adjudication.outcome)

    def test_adjudication_never_recurses(self):
        with self.assertRaises(PlanningContractError):
            AdjudicationResult(
                outcome="merged",
                rounds_used=2,
                selected_aliases=("A", "B"),
                rejected_aliases=(),
                rejection_reasons={},
                compiler_directives=(),
                reasons=("invalid recursion",),
                confidence=0.8,
            )

    def test_call_budget_degrades_panel_before_execution(self):
        proposals, receipt = self.run_panel(calls=2)
        self.assertEqual(2, len(proposals))
        self.assertTrue(receipt.budget_degraded)
        self.assertLessEqual(receipt.calls_used, 2)
        self.assertLessEqual(receipt.actual_cost, 0.3)

    def test_clear_leader_stops_without_adjudication(self):
        proposals = [anonymous("A"), anonymous("B")]
        cards = [
            ProposalScorecard(
                alias="A",
                scores={"utility": 0.95},
                total=0.95,
                hard_constraints_passed=True,
            ),
            ProposalScorecard(
                alias="B",
                scores={"utility": 0.65},
                total=0.65,
                hard_constraints_passed=True,
            ),
        ]
        result, early = self.engine.adjudicate(
            proposals,
            cards,
            [],
        )
        self.assertTrue(early)
        self.assertEqual(0, result.rounds_used)
        self.assertEqual(("A",), result.selected_aliases)

    def test_selection_does_not_use_majority_vote(self):
        proposals = [anonymous("A"), anonymous("B"), anonymous("C")]
        cards = [
            ProposalScorecard(
                alias="A", scores={"utility": 0.91}, total=0.91,
                hard_constraints_passed=True,
            ),
            ProposalScorecard(
                alias="B", scores={"utility": 0.70}, total=0.70,
                hard_constraints_passed=True,
            ),
            ProposalScorecard(
                alias="C", scores={"utility": 0.69}, total=0.69,
                hard_constraints_passed=True,
            ),
        ]
        result, _ = self.engine.adjudicate(proposals, cards, [])
        self.assertEqual(("A",), result.selected_aliases)
        self.assertIn("lower constraint-weighted utility", result.rejection_reasons["B"])

    def test_receipt_fingerprint_is_deterministic(self):
        _, first = self.run_panel()
        _, second = self.run_panel()
        self.assertEqual(
            first.to_dict()["fingerprint"],
            second.to_dict()["fingerprint"],
        )


class PlanCompilerTests(unittest.TestCase):
    def test_compiler_adds_audit_trace_without_new_management_layer(self):
        engine = MultiPlannerEngine()
        _, receipt = asyncio.run(
            engine.deliberate(
                decision(),
                RunRequest(goal="Build a safe service", budget=2),
                analysis(),
            )
        )
        task = TaskContract(
            id="implement",
            title="Implement",
            objective="Implement service",
            task_type="coding",
            criticality=0.9,
            risk_level="high",
            acceptance_criteria=["tests pass"],
        )
        plan = Plan(
            version=1,
            difficulty=8,
            task_types=["coding"],
            rationale="Base plan.",
            tasks=[task],
            predicted_success=0.8,
            estimate_confidence=0.8,
            risks=[],
        )
        compiled = PlanCompiler().compile(plan, receipt)
        audit = compiled.analysis["multiplanner"]
        self.assertEqual(1, audit["rounds_used"])
        self.assertEqual(2, ManagementLimits().max_management_depth)
        self.assertIn("Trace this node", " ".join(task.acceptance_criteria))
        self.assertIn("rollback evidence", " ".join(task.acceptance_criteria).lower())
        self.assertIn("MultiPlanner compiled", compiled.rationale)


if __name__ == "__main__":
    unittest.main()
