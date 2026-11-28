import unittest

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.planner import decide
from costweave.kernel.solver import BSAOPlanner


def operation(name, product, cost, *, needs=(), ms=0, **kwargs):
    return Operation(name, needs, product, "literal", "present", cost, {"value": 1},
                     latency_p50_ms=ms, **kwargs)


def job(ops, *, inputs=None, rules=(), budget=10, **kwargs):
    inputs = {} if inputs is None else inputs
    names = set(inputs) | {op.produces for op in ops}
    acceptance = {name: {"validator": "present", "config": {}} for name in names}
    return Job(["goal"], inputs, budget, ops, acceptance=acceptance, evidence_rules=rules, **kwargs)


class UnifiedPlanningTests(unittest.TestCase):
    def test_cache_does_not_confuse_equal_artifacts_with_different_time_left(self):
        fast = operation("fast", "mid", 2, ms=30)
        slow = operation("slow", "mid", 1, ms=80)
        finish = operation("end", "goal", 1, needs=("mid",), ms=50)
        for options in ((fast, finish), (fast, slow, finish)):
            task = job(options, planning_policy="bsao", deadline_ms=100, failure_penalty_units=100)
            answer = BSAOPlanner(task).plan({}, (), 10)
            self.assertEqual(("ready", ("fast", "end"), 3), (answer.status, answer.operations, answer.cost_units))
