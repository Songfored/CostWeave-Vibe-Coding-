from __future__ import annotations

import dataclasses
import unittest

from costweave.domain import RunRequest, WorkerProfile
from costweave.local_classifier import (
    HeuristicDifficultyClassifier,
    LightweightTaskClassifier,
)
from costweave.planner import HeuristicPlanner, TaskAnalysis
from costweave.planning_contracts import (
    DIFFICULTY_DIMENSIONS,
    DifficultyInterval,
    ExpectedLoss,
    PlanningContractError,
    PlanningMode,
    TaskDifficultyVector,
)
from costweave.planning_governor import PlanningGovernor


def worker(
    worker_id: str,
    provider: str,
    *,
    local: bool,
    reasoning: float = 0.82,
    availability: float = 0.99,
    routable: bool = True,
) -> WorkerProfile:
    return WorkerProfile(
        id=worker_id,
        name=worker_id,
        specialty="planning and validation",
        capabilities={
            "planning": 0.9,
            "analysis": 0.88,
            "risk": 0.86,
            "validation": 0.87,
            "cost": 0.82,
        },
        cost_per_task=0.03,
        latency_factor=1.0,
        reliability=0.93,
        local=local,
        provider=provider,
        model_id=f"{worker_id}-model",
        reasoning=reasoning,
        availability=availability,
        routable=routable,
    )


def analysis(
    *,
    difficulty: int = 5,
    ambiguity: float = 0.25,
    uncertainty: float = 0.25,
    stakes: str = "normal",
    task_types: list[str] | None = None,
    decomposition_score: float = 0.72,
    estimate_confidence: float = 0.86,
    classification_confidence: float = 0.88,
    complexity: dict[str, float] | None = None,
) -> TaskAnalysis:
    return TaskAnalysis(
        task_types=task_types or ["analysis"],
        difficulty=difficulty,
        ambiguity=ambiguity,
        uncertainty=uncertainty,
        stakes=stakes,
        needs_fresh_data=False,
        needs_tools=[],
        modalities=["text"],
        decomposition_score=decomposition_score,
        estimate_confidence=estimate_confidence,
        risk_signals=[],
        primary_intent="analyze",
        intent_scores=[],
        operations=["analyze"],
        deliverables=["report"],
        constraints=[],
        classifications=[],
        classification_confidence=classification_confidence,
        complexity=complexity or {
            "domain": 0.4,
            "coordination": 0.35,
            "tools": 0.2,
            "evidence": 0.35,
        },
        clarification_questions=[],
        planning_action="continue",
    )


def difficulty_vector(
    *,
    expected: float,
    lower: float | None = None,
    upper: float | None = None,
    confidence: float = 0.86,
    **overrides: float,
) -> TaskDifficultyVector:
    dimensions = {name: expected for name in DIFFICULTY_DIMENSIONS}
    dimensions.update(overrides)
    return TaskDifficultyVector(
        dimensions=dimensions,
        overall=DifficultyInterval(
            lower=max(0.0, expected - 0.08) if lower is None else lower,
            expected=expected,
            upper=min(1.0, expected + 0.08) if upper is None else upper,
        ),
        confidence=confidence,
        trigger_features=("controlled test vector",),
        classifier_id="fixed-test-classifier",
        classifier_version="1",
    )


class FixedClassifier(LightweightTaskClassifier):
    classifier_id = "fixed-test-classifier"
    version = "1"

    def __init__(self, value: TaskDifficultyVector) -> None:
        self.value = value

    def classify(
        self,
        request: RunRequest,
        task_analysis: TaskAnalysis,
    ) -> TaskDifficultyVector:
        return self.value


class DifficultyContractTests(unittest.TestCase):
    def test_vector_requires_exactly_eleven_frozen_dimensions(self):
        vector = difficulty_vector(expected=0.4)
        self.assertEqual(11, len(vector.dimensions))
        invalid = dict(vector.dimensions)
        invalid.pop("ambiguity")
        with self.assertRaises(PlanningContractError):
            dataclasses.replace(vector, dimensions=invalid)

    def test_interval_must_be_ordered(self):
        with self.assertRaises(PlanningContractError):
            DifficultyInterval(lower=0.6, expected=0.4, upper=0.8)

    def test_expected_loss_total_is_auditable(self):
        loss = ExpectedLoss(1, 2, 3, 4, 5)
        self.assertEqual(15, loss.total)
        self.assertEqual(15, loss.to_dict()["total"])

    def test_local_classifier_is_deterministic_and_bounded(self):
        request = RunRequest(goal="summarize a short local document")
        task_analysis = HeuristicPlanner().analyze(request)
        classifier = HeuristicDifficultyClassifier()
        first = classifier.classify(request, task_analysis)
        second = classifier.classify(request, task_analysis)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertLessEqual(first.overall.lower, first.overall.expected)
        self.assertLessEqual(first.overall.expected, first.overall.upper)
        self.assertEqual(set(DIFFICULTY_DIMENSIONS), set(first.dimensions))

    def test_short_deadline_increases_pressure(self):
        classifier = HeuristicDifficultyClassifier()
        task_analysis = analysis()
        normal = classifier.classify(
            RunRequest(goal="prepare a bounded analysis"),
            task_analysis,
        )
        urgent = classifier.classify(
            RunRequest(
                goal="prepare a bounded analysis",
                max_concurrency=12,
                deadline_seconds=1,
            ),
            task_analysis,
        )
        self.assertGreater(
            urgent.dimensions["concurrency_deadline_pressure"],
            normal.dimensions["concurrency_deadline_pressure"],
        )


class PlanningGovernorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workers = [
            worker("local-a", "local", local=True, reasoning=0.84),
            worker("remote-a", "provider-a", local=False, reasoning=0.93),
            worker("remote-b", "provider-b", local=False, reasoning=0.9),
            worker("remote-c", "provider-c", local=False, reasoning=0.88),
        ]

    def decide(
        self,
        vector: TaskDifficultyVector,
        *,
        request: RunRequest | None = None,
        task_analysis: TaskAnalysis | None = None,
        workers: list[WorkerProfile] | None = None,
    ):
        governor = PlanningGovernor(classifier=FixedClassifier(vector))
        return governor.decide(
            request or RunRequest(goal="controlled planning task", budget=2),
            task_analysis or analysis(),
            self.workers if workers is None else workers,
        )

    def test_simple_task_never_triggers_multi_planner(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.16,
                lower=0.1,
                upper=0.23,
                ambiguity=0.08,
                verification_difficulty=0.12,
                error_loss=0.14,
            )
        )
        self.assertEqual(PlanningMode.NO_DELEGATION, decision.mode)
        selected = next(
            item
            for item in decision.candidates
            if item.mode == decision.mode
        )
        self.assertEqual(
            selected.expected_loss.total,
            min(
                item.expected_loss.total
                for item in decision.candidates
                if item.feasible
            ),
        )
        multi = next(
            item
            for item in decision.candidates
            if item.mode == PlanningMode.MULTI_PLANNER
        )
        self.assertFalse(multi.feasible)

    def test_low_confidence_cannot_silently_enter_weak_route(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.48,
                confidence=0.52,
                ambiguity=0.45,
                error_loss=0.55,
            ),
            task_analysis=analysis(
                estimate_confidence=0.55,
                uncertainty=0.62,
            ),
        )
        self.assertTrue(
            decision.pre_execution_escalation["required"]
        )
        self.assertIn(
            decision.mode,
            {PlanningMode.MULTI_PLANNER, PlanningMode.EXPERT_OVERRIDE},
        )

    def test_high_risk_task_escalates_before_execution(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.67,
                upper=0.79,
                error_loss=0.9,
                verification_difficulty=0.82,
            ),
            task_analysis=analysis(
                difficulty=8,
                stakes="high",
                task_types=["safety"],
            ),
        )
        self.assertTrue(
            decision.pre_execution_escalation["required"]
        )
        self.assertNotIn(
            decision.mode,
            {
                PlanningMode.NO_DELEGATION,
                PlanningMode.DIRECT_DELEGATION,
                PlanningMode.SINGLE_PLANNER,
            },
        )

    def test_cross_domain_high_value_task_selects_multi_planner(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.65,
                upper=0.78,
                ambiguity=0.3,
                decomposition_uncertainty=0.72,
                cross_domain_coupling=0.82,
                verification_difficulty=0.7,
                error_loss=0.82,
            ),
            task_analysis=analysis(
                difficulty=8,
                stakes="high",
                task_types=["coding", "strategy", "safety"],
                complexity={
                    "domain": 0.8,
                    "coordination": 0.9,
                    "tools": 0.7,
                    "evidence": 0.75,
                },
            ),
        )
        self.assertEqual(PlanningMode.MULTI_PLANNER, decision.mode)
        self.assertEqual(3, decision.panel_policy.size)
        self.assertGreater(
            decision.value_of_deliberation,
            decision.vod_margin,
        )

    def test_vod_gate_can_reject_unprofitable_multi_planner(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.35,
                lower=0.27,
                upper=0.43,
                ambiguity=0.1,
                decomposition_uncertainty=0.2,
                cross_domain_coupling=0.1,
                verification_difficulty=0.25,
                error_loss=0.22,
            )
        )
        multi = next(
            item
            for item in decision.candidates
            if item.mode == PlanningMode.MULTI_PLANNER
        )
        self.assertFalse(multi.feasible)
        self.assertTrue(any("VoD" in reason for reason in multi.hard_rejections))

    def test_missing_local_capacity_triggers_remote_help(self):
        remote_only = [
            worker("remote-a", "provider-a", local=False),
            worker("remote-b", "provider-b", local=False),
        ]
        decision = self.decide(
            difficulty_vector(expected=0.5, upper=0.62),
            workers=remote_only,
        )
        escalation = decision.pre_execution_escalation
        self.assertTrue(escalation["required"])
        self.assertTrue(escalation["capacity"]["requires_remote_help"])
        self.assertEqual(0, escalation["capacity"]["local_candidates"])

    def test_local_concurrency_overload_is_detected_at_start(self):
        one_local = [worker("local-a", "local", local=True)]
        decision = self.decide(
            difficulty_vector(expected=0.52, upper=0.64),
            request=RunRequest(
                goal="parallel workload",
                budget=2,
                max_concurrency=12,
            ),
            workers=one_local,
        )
        capacity = decision.pre_execution_escalation["capacity"]
        self.assertEqual(1.0, capacity["concurrency_pressure"])
        self.assertTrue(capacity["requires_remote_help"])

    def test_expert_route_is_infeasible_without_remote_worker(self):
        decision = self.decide(
            difficulty_vector(expected=0.4),
            workers=[worker("local-a", "local", local=True)],
        )
        expert = next(
            item
            for item in decision.candidates
            if item.mode == PlanningMode.EXPERT_OVERRIDE
        )
        self.assertFalse(expert.feasible)
        self.assertTrue(expert.hard_rejections)

    def test_decision_exposes_all_routes_and_loss_components(self):
        decision = self.decide(difficulty_vector(expected=0.4))
        payload = decision.to_dict()
        self.assertEqual(6, len(payload["candidates"]))
        self.assertEqual(
            {mode.value for mode in PlanningMode},
            {item["mode"] for item in payload["candidates"]},
        )
        for item in payload["candidates"]:
            self.assertEqual(
                {
                    "money",
                    "time",
                    "failure",
                    "error_risk",
                    "coordination",
                    "total",
                },
                set(item["expected_loss"]),
            )

    def test_selected_route_is_feasible_and_rejections_are_explained(self):
        decision = self.decide(difficulty_vector(expected=0.46))
        selected = next(
            item
            for item in decision.candidates
            if item.mode == decision.mode
        )
        self.assertTrue(selected.feasible)
        self.assertEqual(5, len(decision.rejected_modes))
        self.assertTrue(all(decision.rejected_modes.values()))

    def test_planning_budget_respects_hard_ratio(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.68,
                cross_domain_coupling=0.85,
                error_loss=0.8,
            ),
            task_analysis=analysis(stakes="high"),
        )
        self.assertLessEqual(
            decision.planning_budget.task_budget_ratio,
            0.25,
        )

    def test_identical_inputs_produce_stable_fingerprint(self):
        vector = difficulty_vector(expected=0.46)
        first = self.decide(vector)
        second = self.decide(vector)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.fingerprint(), second.fingerprint())

    def test_multi_planner_assignments_are_provider_diverse(self):
        decision = self.decide(
            difficulty_vector(
                expected=0.65,
                upper=0.78,
                cross_domain_coupling=0.85,
                error_loss=0.84,
                verification_difficulty=0.75,
            ),
            task_analysis=analysis(
                stakes="high",
                task_types=["coding", "strategy", "safety"],
            ),
        )
        self.assertEqual(PlanningMode.MULTI_PLANNER, decision.mode)
        assignments = decision.panel_policy.selected_assignments
        providers = {
            item["provider"]
            for item in assignments
            if item["worker_id"] is not None
        }
        self.assertEqual(3, len(assignments))
        self.assertGreaterEqual(len(providers), 2)


if __name__ == "__main__":
    unittest.main()
