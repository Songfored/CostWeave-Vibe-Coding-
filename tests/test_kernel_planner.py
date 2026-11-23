import itertools
import random
import unittest
from dataclasses import FrozenInstanceError

from costweave.kernel.contracts import Job, MAX_UNITS, canonical_json
from costweave.kernel.planner import plan


def operation(id, requires, produces, cost, role=None):
    row = {"id": id, "requires": requires, "produces": produces,
           "executor": "fixture", "validator": "type", "cost_units": cost}
    if role is not None:
        row["route_role"] = role
    return row


def job_dict(goals, operations, inputs=None, budget=50, limit=10_000):
    return {
        "goals": goals, "inputs": inputs or {}, "budget_units": budget,
        "operations": operations, "search_limit": limit,
        "acceptance": {name: {"validator": "type", "config": {}}
                       for name in set(goals) | {op["produces"] for op in operations}},
    }


def shared_job(budget=50, limit=10_000):
    return Job.from_dict(job_dict(["left", "right"], [
        operation("survey", [], "shared", 5),
        operation("left-alone", [], "left", 4),
        operation("right-alone", [], "right", 4),
        operation("reuse-left", ["shared"], "left", 1),
        operation("reuse-right", ["shared"], "right", 1),
    ], budget=budget, limit=limit))


def exhaustive_cost(job, available, excluded):
    # Independent oracle: enumerate subsets of operations and close each subset
    # under its prerequisites. It does not use queue priorities or planner states.
    operations = [op for op in job.operations if op.id not in excluded]
    minimum = None
    for size in range(len(operations) + 1):
        for subset in itertools.combinations(operations, size):
            price = sum(op.cost_units for op in subset)
            if minimum is not None and price >= minimum:
                continue
            artifacts = set(available)
            while True:
                before = len(artifacts)
                artifacts.update(op.produces for op in subset if set(op.requires) <= artifacts)
                if len(artifacts) == before:
                    break
            if set(job.goals) <= artifacts:
                minimum = price
    return minimum


class KernelContractTests(unittest.TestCase):
    def test_job_is_detached_immutable_and_round_trips_to_plain_json(self):
        raw = job_dict(["receipt"], [operation("emit", ["source"], "receipt", 1)],
                       inputs={"source": ["中文", {"n": 2}]})
        raw["operations"][0]["config"] = {"templates": ["one"]}
        raw["acceptance"]["receipt"]["config"] = {"kinds": ["receipt"]}
        job = Job.from_dict(raw)
        raw["inputs"]["source"].clear()
        job.inputs["source"].clear()
        job.operations[0].config["templates"].clear()
        job.acceptance["receipt"]["config"]["kinds"].clear()
        self.assertEqual(["中文", {"n": 2}], job.inputs["source"])
        self.assertEqual(["one"], job.operations[0].config["templates"])
        self.assertEqual(["receipt"], job.acceptance["receipt"]["config"]["kinds"])
        self.assertEqual(job, Job.from_dict(job.to_dict()))
        self.assertEqual('{"a":2,"b":1}', canonical_json({"b": 1, "a": 2}))
        with self.assertRaises(FrozenInstanceError):
            job.budget_units = 8

    def test_job_rejects_invalid_units_unknown_references_and_duplicates(self):
        base = job_dict(["goal"], [operation("make", [], "goal", 1)])
        invalid = [
            {**base, "budget_units": True}, {**base, "budget_units": MAX_UNITS + 1},
            {**base, "search_limit": 0}, {**base, "goals": ["goal", "goal"]},
            {**base, "operations": base["operations"] * 2},
            {**base, "operations": [operation("make", [], "goal", False)]},
            {**base, "operations": [operation("make", ["not-declared"], "goal", 1)]},
            {**base, "operations": [operation("make", ["goal", "goal"], "goal", 1)]},
            {**base, "inputs": {"goal": float("nan")}},
            {**base, "inputs": {"x": {1: "ambiguous"}}},
            {**base, "operations": [{**base["operations"][0], "config": None}]},
            {**base, "acceptance": {"missing": {"validator": "checks", "config": {}}}},
            {**base, "input": {}},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                Job.from_dict(raw)

    def test_goals_and_interchangeable_producers_require_independent_acceptance(self):
        raw = job_dict(["goal"], [
            operation("a", [], "input", 1), operation("b", [], "input", 2),
            operation("finish", ["input"], "goal", 0),
        ])
        self.assertEqual(3, len(Job.from_dict(raw).operations))
        for missing in ("goal", "input"):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                Job.from_dict({**raw, "acceptance": {k: v for k, v in raw["acceptance"].items() if k != missing}})
        with self.assertRaises(ValueError):
            Job.from_dict({**job_dict(["goal"], [], inputs={"goal": 1}), "acceptance": {}})


class KernelPlannerTests(unittest.TestCase):
    def assert_executable(self, job, result, available=(), excluded=()):
        by_id = {op.id: op for op in job.operations}
        artifacts = set(available)
        cost = 0
        for key in result.operations:
            op = by_id[key]
            self.assertNotIn(key, excluded)
            self.assertTrue(set(op.requires) <= artifacts)
            self.assertNotIn(op.produces, artifacts)
            artifacts.add(op.produces)
            cost += op.cost_units
        self.assertTrue(set(job.goals) <= artifacts)
        self.assertEqual(cost, result.cost_units)

    def test_shared_setup_beats_the_cheapest_individual_goal_producers(self):
        job = shared_job()
        result = plan(job, [], [], 7)
        self.assertEqual(("ready", 7, True), (result.status, result.cost_units, result.optimal))
        self.assertEqual("survey", result.operations[0])
        self.assertEqual({"reuse-left", "reuse-right"}, set(result.operations[1:]))
        self.assert_executable(job, result)
        self.assertEqual("over_budget", plan(job, [], [], 6).status)
        alternative = plan(job, [], ["survey"], 8)
        self.assertEqual(8, alternative.cost_units)
        self.assert_executable(job, alternative, excluded=["survey"])

    def test_replanning_uses_only_unfinished_outputs_and_remaining_budget(self):
        job = shared_job()
        result = plan(job, ["shared", "left"], ["left-alone"], 1)
        self.assertEqual(("reuse-right",), result.operations)
        self.assertEqual(1, result.cost_units)
        self.assertEqual("complete", plan(job, ["left", "right"], [], 0).status)
        for remaining in (-1, True, job.budget_units + 1):
            with self.assertRaises(ValueError):
                plan(job, [], [], remaining)

    def test_protects_a_complete_fallback_only_when_a_failed_probe_would_exhaust_its_budget(self):
        raw = job_dict(["receipt"], [
            operation("summary", [], "receipt", 1, "speculative"),
            operation("parse", [], "items", 3, "fallback"),
            operation("assemble", ["items"], "receipt", 2, "fallback"),
        ])
        legacy = Job.from_dict(raw)
        guarded = Job.from_dict({**raw, "planning_policy": "preserve_fallback"})
        self.assertEqual(("summary",), plan(legacy, [], [], 5).operations)
        result = plan(guarded, [], [], 5)
        self.assertEqual(("parse", "assemble"), result.operations)
        self.assertFalse(result.optimal)
        self.assert_executable(guarded, result)
        for remaining in (1, 4, 6):
            with self.subTest(remaining=remaining):
                self.assertEqual(("summary",), plan(guarded, [], [], remaining).operations)

        # An accepted intermediate reduces the remaining fallback quote to two.
        self.assertEqual(("assemble",), plan(guarded, ["items"], [], 2).operations)
        self.assertEqual(("summary",), plan(guarded, ["items"], [], 3).operations)
        self.assertEqual(("summary",), plan(guarded, [], ["parse"], 5).operations)
        self.assertEqual(("parse", "assemble"), plan(guarded, [], ["summary"], 5).operations)

    def test_guard_needs_declared_roles_and_a_fallback_for_every_unfinished_goal(self):
        for shortcut_role, rebuild_role in (("standard", "fallback"), ("speculative", "standard")):
            with self.subTest(shortcut=shortcut_role, rebuild=rebuild_role):
                raw = job_dict(["receipt"], [
                    operation("summary", [], "receipt", 1, shortcut_role),
                    operation("rebuild", [], "receipt", 5, rebuild_role),
                ])
                job = Job.from_dict({**raw, "planning_policy": "preserve_fallback"})
                self.assertEqual(("summary",), plan(job, [], [], 5).operations)

        raw = job_dict(["receipt", "audit"], [
            operation("summary", [], "receipt", 1, "speculative"),
            operation("rebuild", [], "receipt", 4, "fallback"),
            operation("log", ["receipt"], "audit", 1),
        ])
        job = Job.from_dict({**raw, "planning_policy": "preserve_fallback"})
        self.assertEqual(("summary", "log"), plan(job, [], [], 5).operations)
        self.assertEqual(("rebuild",), plan(job, ["audit"], [], 4).operations)

    def test_free_probe_can_spend_the_entire_budget_on_fallback_after_rejection(self):
        raw = job_dict(["receipt"], [
            operation("summary", [], "receipt", 0, "speculative"),
            operation("rebuild", [], "receipt", 5, "fallback"),
        ])
        job = Job.from_dict({**raw, "planning_policy": "preserve_fallback"})
        self.assertEqual(("summary",), plan(job, [], [], 5).operations)
        self.assertEqual(("rebuild",), plan(job, [], ["summary"], 5).operations)

    def test_a_capped_fallback_search_must_not_treat_its_witness_as_a_minimum(self):
        # The fallback witness uses independent producers for 8. Reusing the
        # survey actually costs 7 and still fits after the 1-unit probe fails.
        raw = shared_job(limit=1).to_dict()
        for op in raw["operations"]:
            op["route_role"] = "fallback"
        raw["operations"].append(operation("probe", [], "shared", 1, "speculative"))
        job = Job.from_dict({**raw, "planning_policy": "preserve_fallback"})
        self.assertEqual("probe", plan(job, [], [], 8).operations[0])

    def test_unreachable_cyclic_inputs_and_zero_cost_seeded_cycles(self):
        raw = job_dict(["report"], [
            operation("a-from-b", ["b"], "a", 0), operation("b-from-a", ["a"], "b", 0),
            operation("seed-a", [], "a", 0), operation("finish", ["a", "b"], "report", 0),
        ])
        job = Job.from_dict(raw)
        result = plan(job, [], [], 0)
        self.assertEqual(("seed-a", "b-from-a", "finish"), result.operations)
        self.assertEqual("unreachable", plan(job, [], ["seed-a"], 0).status)

    def test_search_limits_preserve_an_affordable_witness_or_report_uncertainty(self):
        job = shared_job(limit=1)
        candidate = plan(job, [], [], 8)
        self.assertEqual(("ready", 8, False), (candidate.status, candidate.cost_units, candidate.optimal))
        self.assert_executable(job, candidate)
        unknown = plan(job, [], [], 7)
        self.assertEqual(("search_limit", (), None), (unknown.status, unknown.operations, unknown.cost_units))
        self.assertEqual("over_budget", plan(job, [], [], 0).status)

    def test_equivalent_job_orderings_return_identical_plans(self):
        raw = shared_job().to_dict()
        expected = plan(Job.from_dict(raw), [], [], 20)
        raw["operations"].reverse()
        raw["goals"].reverse()
        self.assertEqual(expected, plan(Job.from_dict(raw), [], [], 20))

    def test_small_random_graphs_match_exhaustive_operation_subsets(self):
        rng = random.Random(517)
        names = list("abcd")
        for case in range(120):
            outputs = names + rng.choices(names, k=3)
            rows = [operation(f"op-{i}", rng.sample(names, k=rng.randrange(3)), out, rng.randrange(5))
                    for i, out in enumerate(outputs)]
            goals = rng.sample(names, k=rng.randint(1, 3))
            job = Job.from_dict(job_dict(goals, rows))
            available = rng.sample(names, k=rng.randrange(3))
            excluded = rng.sample([op.id for op in job.operations], k=rng.randrange(3))
            minimum = exhaustive_cost(job, available, excluded)
            for budget in {0, 5, 50}:
                with self.subTest(case=case, budget=budget):
                    result = plan(job, available, excluded, budget)
                    if minimum is None:
                        self.assertEqual("unreachable", result.status)
                    elif minimum > budget:
                        self.assertEqual("over_budget", result.status)
                    else:
                        self.assertIn(result.status, {"ready", "complete"})
                        self.assertTrue(result.optimal)
                        self.assertEqual(minimum, result.cost_units)
                        self.assert_executable(job, result, available, excluded)
