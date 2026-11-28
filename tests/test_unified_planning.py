import hashlib
import random
import unittest

from costweave.kernel.beliefs import BeliefEngine
from costweave.kernel.catalog import compile_request
from costweave.kernel.compiler import ModelSpec, TaskSpec, compile_task_hypergraph
from costweave.kernel.contracts import Job, Operation, canonical_json
from costweave.kernel.verification_value import fit_verification_model
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

    def test_deadline_optimum_agrees_with_exhaustive_small_graphs(self):
        rng = random.Random(20261127)
        for case in range(80):
            ops = []
            for name, product, needs in (("slow", "mid", ()), ("fast", "mid", ()), ("extra", "side", ()),
                                         ("join", "goal", ("mid", "side")), ("end", "goal", ("mid",)),
                                         ("direct", "goal", ())):
                ops.append(operation(name, product, rng.randint(1, 5), needs=needs, ms=rng.randint(1, 8)))
            budget, horizon = rng.randint(2, 10), rng.randint(3, 16)
            def enumerate_costs(available, cost, elapsed):
                if "goal" in available:
                    return [cost]
                values = []
                for op in ops:
                    if (op.produces not in available and set(op.requires) <= available
                            and cost + op.cost_units <= budget and elapsed + op.latency_p50_ms <= horizon):
                        values.extend(enumerate_costs(available | {op.produces}, cost + op.cost_units,
                                                      elapsed + op.latency_p50_ms))
                return values
            oracle = enumerate_costs(set(), 0, 0)
            model = job(ops, budget=budget, deadline_ms=horizon, failure_penalty_units=100, planning_policy="bsao")
            result = decide(model, {}, (), budget)
            with self.subTest(case=case):
                self.assertEqual(bool(oracle), result.status == "ready")
                if oracle:
                    self.assertEqual(min(oracle), result.cost_units)
                    self.assertTrue(result.optimal)

    def test_rational_fallback_changes_with_probe_probability(self):
        for probability, first in ((0.10, "reliable"), (0.80, "probe")):
            model = job((operation("probe", "goal", 2, pass_prob=probability),
                         operation("reliable", "goal", 5)), budget=7, planning_policy="bsao")
            self.assertEqual(first, decide(model, {}, (), 7).operations[0])

    def test_guard_depends_only_on_accepted_strictly_typed_nested_values(self):
        condition = {"artifact": "scan", "path": ["entries", 0, "approved"], "equals": True}
        ops = [operation("read", "scan", 1), operation("retry-read", "scan", 2),
               operation("short", "goal", 1, when=condition), operation("manual", "goal", 7)]
        for policy in ("bsao", "lowest_cost", "preserve_fallback"):
            task = job(ops, planning_policy=policy)
            with self.subTest(policy=policy):
                self.assertEqual("read", decide(task, {}, (), 10).operations[0])
                self.assertFalse(decide(task, {}, (), 10).optimal)
                self.assertEqual("retry-read", decide(task, {}, ["read"], 9).operations[0])
                self.assertEqual(("manual",), decide(task, {}, ["read", "retry-read"], 7).operations)
                self.assertEqual("awaiting_receipt",
                                 decide(task, {}, ["read", "retry-read"], 8, anticipated=["scan"]).status)
                for observed in (False, 1, 1.0, None):
                    rejected = decide(task, {"scan": {"entries": [{"approved": observed}]}}, (), 9)
                    self.assertEqual(("manual",), rejected.operations)
                    self.assertEqual("falsified", rejected.audit["guards"]["short"]["state"])
                matched = decide(task, {"scan": {"entries": [{"approved": True}]}}, (), 9)
                self.assertEqual(("short",), matched.operations)
                self.assertEqual(64, len(matched.audit["guards"]["short"]["artifact_sha256"]))
        raw = job(ops).to_dict()
        catalog = {key: raw.pop(key) for key in ("operations", "acceptance")}
        compiled = compile_request(raw, catalog)
        self.assertEqual({"read", "retry-read", "manual", "short"}, {op.id for op in compiled.operations})
        self.assertEqual(Job.from_dict(compiled.to_dict()), compiled)
        self.assertEqual((), next(op for op in compiled.operations if op.id == "short").requires)

    def test_mandatory_rule_prices_legal_continuation_before_dispatch(self):
        guard = {"artifact": "report", "path": ["source", 0], "equals": "conflict"}
        rules = [{**guard, "operation": "inspect"}]
        ops = [operation("inspect", "source", 2), operation("verify", "goal", 4, needs=("source",), ms=20),
               operation("copy", "goal", 1, route_role="fallback")]
        rules.append({**guard, "operation": "verify"})
        model = job(ops, inputs={"report": {"source": ["conflict"]}}, rules=rules, budget=6,
                    deadline_ms=50, planning_policy="preserve_fallback")
        self.assertEqual("over_budget", decide(model, model.inputs, (), 5).status)
        self.assertEqual("deadline_unreachable", decide(model, model.inputs, (), 6, remaining_ms=19).status)
        self.assertEqual("evidence_action_rejected", decide(model, model.inputs, ["verify"], 6).status)
        decision = decide(model, model.inputs, (), 6)
        self.assertEqual((("inspect",), 2), (decision.operations, decision.cost_units))
        self.assertEqual(["source", 0], decision.to_dict()["evidence"]["path"])
        unreachable = job((ops[0], operation("gate", "block", 0, needs=["block"]),
                           operation("end", "goal", 1, needs=["block"])), inputs=model.inputs, rules=rules[:1])
        self.assertEqual("unreachable", decide(unreachable, model.inputs, (), 10).status)

    def test_deadline_and_risk_apply_to_every_unconditional_frontend(self):
        routes = [operation("slow", "goal", 1, ms=60), operation("unsafe", "goal", 2, false_pass_risk=0.2),
                  operation("safe", "goal", 3, ms=5)]
        for policy in ("lowest_cost", "preserve_fallback", "bsao"):
            model = job(routes, planning_policy=policy, deadline_ms=50)
            self.assertEqual(("safe",), decide(model, {}, (), 10, remaining_ms=10, risk_remaining=0.1).operations)
            self.assertEqual("deadline_expired", decide(model, {}, (), 10, remaining_ms=0).status)

    def test_new_contracts_round_trip_model_identity_and_rules_without_changing_legacy_hashes(self):
        model = ModelSpec("tiny", "literal", "local", 0, 0)
        guard = {"artifact": "signal", "path": ["values", 0], "equals": 7}
        description = {"goal_name": "goal", "prompt": "return the amount", "budget_units": 2,
                       "inputs": {"signal": {"values": [7]}},
                       "acceptance": {"signal": {"validator": "present", "config": {}}},
                       "evidence_rules": [{**guard, "operation": "direct_tiny"}],
                       "operation_guards": {"direct_tiny": guard}}
        spec = TaskSpec.from_dict(description)
        self.assertEqual(spec, TaskSpec.from_dict(spec.to_dict()))
        beliefs = BeliefEngine()
        actual = compile_task_hypergraph(spec, [model], beliefs=beliefs)
        digest = hashlib.sha256(canonical_json(beliefs.to_dict()).encode()).hexdigest()
        self.assertEqual(digest, actual.belief_model_sha256)
        self.assertEqual(actual, Job.from_dict(actual.to_dict()))
        self.assertEqual(("direct_tiny",), decide(actual, actual.inputs, (), 2).operations)
        ordinary = compile_task_hypergraph(spec, [model])
        self.assertNotIn("belief_model_sha256", ordinary.to_dict())
        self.assertEqual("bsao", ordinary.planning_policy)
        self.assertEqual("lowest_cost", job([operation("plain", "goal", 1)]).planning_policy)
        for patch in ({"evidence_rules": [{**guard, "field": "unrelated", "operation": "direct_tiny"}]},
                      {"operation_guards": {"typo": guard}}):
            with self.assertRaises(ValueError):
                compile_task_hypergraph(TaskSpec.from_dict({**description, **patch}), [model])

    def test_verification_only_task_selects_legacy_route_explicit_conflict_rejected(self):
        op = "direct_tiny"
        data = [{"case_id": "sample", "stratum": "clear",
                 "outcomes": {op: {"correct": True, "cost_units": 0, "elapsed_ms": 1}}}]
        policy = {"artifact": "signal", "field": "kind", "model": fit_verification_model(data, baseline=op, actions=[op]),
                  "deadline_ms": 100, "min_on_time_correct": 0, "fallback_operation": op, "route_ms": {op: 1}}
        request = {"goal_name": "goal", "prompt": "check", "inputs": {"signal": {"kind": "clear"}}, "budget_units": 1,
                   "verification_policy": policy, "acceptance": {"signal": {"validator": "present", "config": {}}}}
        models = [ModelSpec("tiny", "literal", "local", 0, 0, latency_p50_ms=1, latency_p95_ms=1)]
        task = compile_task_hypergraph(TaskSpec.from_dict(request), models)
        self.assertEqual("lowest_cost", task.planning_policy)
        self.assertEqual((op,), decide(task, task.inputs, (), 1).operations)
        with self.assertRaisesRegex(ValueError, "verification_policy"):
            compile_task_hypergraph(TaskSpec.from_dict({**request, "planning_policy": "bsao"}), models)

    def test_report_acquisition_checks_the_entire_chain_and_shared_alternatives(self):
        rule = {"artifact": "report", "field": "ok", "equals": True, "acquire": True, "operation": "deliver"}
        no_finish = job([operation("scan", "page", 1), operation("read", "report", 2, needs=["page"]),
                         operation("deliver", "goal", 1)], rules=[rule], budget=3)
        self.assertEqual("over_budget", decide(no_finish, {}, (), 3).status)
        shared = job([operation("cheap", "report", 2), operation("scan", "page", 2),
                      operation("read", "report", 1, needs=["page"]),
                      operation("deliver", "goal", 1, needs=["page"])], rules=[rule], budget=4)
        choice = decide(shared, {}, (), 4)
        self.assertEqual((("scan", "read"), 3), (choice.operations, choice.cost_units))
