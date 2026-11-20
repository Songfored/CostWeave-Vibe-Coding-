"""Joint-search replay tests use synthetic, aligned timing windows."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from costweave.intelligence.dag_latency import DagLatencyReplayer
from costweave.intelligence.joint_search import (
    JointRouteOptionV1, JointRouteSetV1, JointSearchProblemV1, JointStrategySearcher,
    JointTaskNodeV1,
)
from costweave.joint_search_service import CONFIRM_TOKEN, JointSearchService
from costweave.joint_search_store import JointSearchStore


def windows():
    return {
        str(i): {
            "a": {"fixed": 100 if i < 4 else 10},
            "b": {"together": 100 if i < 4 else 10, "apart": 100 if 4 <= i < 8 else 10},
        }
        for i in range(100)
    }


def problem(timings, **settings):
    def option(route):
        return JointRouteOptionV1(route, "worker", "medium", "checks")

    return JointSearchProblemV1(
        project_id="local-default", case_id="parallel", task_family="code",
        nodes=(JointTaskNodeV1("a", "a" * 64), JointTaskNodeV1("b", "b" * 64)),
        route_sets=(JointRouteSetV1("a", (option("fixed"),)),
                    JointRouteSetV1("b", (option("apart"), option("together")))),
        management_modes=("planner",), recovery_policies=("retry_node",),
        min_success_lower=0, deadline_ms=40, latency_scenarios=timings, **settings,
    )


class DagReplayTests(unittest.TestCase):
    def test_equal_node_p95s_can_produce_tenfold_difference_in_graph_p95(self):
        data = windows()
        replayer = DagLatencyReplayer({"a": (), "b": ()}, 2, data)
        results = []
        for option in ("together", "apart"):
            times = sorted(window["b"][option] for window in data.values())
            self.assertEqual(10, times[94])
            results.append(replayer.predict({"a": "fixed", "b": option}, {"a": 1, "b": 1}, 0, 20))
        joined, split = results
        self.assertEqual((10, 100), (joined.p95_ms, split.p95_ms))
        self.assertEqual((0.04, 0.08), (joined.deadline_miss_rate, split.deadline_miss_rate))
        self.assertAlmostEqual(13.6, joined.mean_ms)
        self.assertEqual(joined.scenario_set_hash, split.scenario_set_hash)

    def test_service_selects_using_replay_and_round_trips_without_touching_old_records(self):
        with tempfile.TemporaryDirectory() as temp:
            service = JointSearchService(JointSearchStore(str(Path(temp) / "replay.sqlite3")))

            def decide(item, key):
                return service.decide(project_id="local-default", idempotency_key=key,
                                      payload={"confirm": CONFIRM_TOKEN, "problem": item.to_dict()})[0]["decision"]

            old = problem({})
            self.assertNotIn("latency_scenarios", old.to_dict())
            old_decision = decide(old, "legacy-lookup")
            traced = replace(old, latency_scenarios=windows())
            self.assertEqual(traced, JointSearchProblemV1.from_dict(traced.to_dict()))
            decision = decide(traced, "traced-lookup")
            choice = decision["selected_candidate"]
            self.assertEqual("together", dict(choice["node_routes"])["b"])
            self.assertEqual(30, choice["latency_replay"]["p95_ms"])  # 20ms planner + 10ms graph
            self.assertEqual(0.04, choice["latency_replay"]["deadline_miss_rate"])
            for expected in (old_decision, decision):
                persisted = service.get(project_id="local-default", decision_id=expected["decision_id"])
                self.assertEqual(expected, persisted["decision"])

    def test_incomplete_candidate_does_not_benefit_from_cherry_picking_windows(self):
        data = windows()
        del data["1"]["b"]["together"]
        request = problem(data)
        decision, candidates = JointStrategySearcher().search(request)
        self.assertIsNone(decision.selected_candidate)
        for candidate in candidates:
            if dict(candidate.node_routes)["b"] == "together":
                self.assertEqual("missing_observations", candidate.latency_replay.status)
                self.assertEqual(99, candidate.latency_replay.matched_scenarios)
                self.assertIsNone(candidate.effective_p95_ms)
                self.assertFalse(candidate.hard_constraint_checks["deadline"])

    def test_duplicate_route_source_does_not_consume_a_replay_candidate_slot(self):
        request = problem(windows(), historical_route_ids={"a": "fixed", "b": "apart"})
        request = replace(request, budget=replace(request.budget, max_candidates=2))
        decision, candidates = JointStrategySearcher().search(request)
        self.assertEqual("SELECT", decision.action)
        self.assertEqual(
            {"safety_baseline": 120, "planner": 30},
            {candidate.source: candidate.effective_p95_ms for candidate in candidates},
        )
        self.assertEqual("planner", decision.selected_candidate.source)
        legacy = replace(request, latency_scenarios={})
        _, legacy_candidates = JointStrategySearcher().search(legacy)
        self.assertEqual({"safety_baseline", "historical"}, {c.source for c in legacy_candidates})

    def test_dependent_work_starts_as_soon_as_a_slot_and_its_inputs_are_ready(self):
        data = {"w": {"a": {"r": 1}, "b": {"r": 10}, "c": {"r": 6}}}
        graph = {"a": (), "b": (), "c": ("a",)}
        routes = dict.fromkeys(graph, "r")
        demands = dict.fromkeys(graph, 1)
        for slots, expected in ((1, 17), (2, 10)):
            replay = DagLatencyReplayer(graph, slots, data).predict(routes, demands, 0, expected)
            self.assertEqual((expected, 0, slots), (replay.p95_ms, replay.deadline_miss_rate, replay.peak_slots))
        two_slots = DagLatencyReplayer(graph, 2, data)
        self.assertEqual(10, two_slots.predict(routes, demands, 0, 17).p95_ms)
        demands["b"] = 2
        self.assertEqual(17, two_slots.predict(routes, demands, 0, 17).p95_ms)
        demands["a"] = 3
        replay = DagLatencyReplayer(graph, 2, data).predict(routes, demands, 0, 100)
        self.assertEqual("insufficient_slots", replay.status)
        self.assertIsNone(replay.p95_ms)

    def test_capacity_can_serialize_independent_tasks_and_zero_duration_is_measured(self):
        request = problem({"w": {"a": {"fixed": 0}, "b": {"together": 4}}}, max_parallelism=1)
        decision, _ = JointStrategySearcher().search(request)
        chosen = decision.selected_candidate
        self.assertEqual((24, 1), (chosen.effective_p95_ms, chosen.parallel_width))

    def test_reject_unknown_routes_and_invalid_latencies(self):
        for timings in ({"w": {"extra": {"fixed": 2}}}, {"w": {"a": {"extra": 2}}},
                        {"w": {"a": {"fixed": -1}}}, {"w": {"a": {"fixed": True}}},
                        {"w": {"a": {"fixed": float("nan")}}}, {"w": {"a": {"fixed": None}}}):
            with self.subTest(timings=timings), self.assertRaises((ValueError, TypeError)):
                problem(timings)
