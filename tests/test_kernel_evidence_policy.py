import hashlib
import unittest

from costweave.kernel.contracts import Job, canonical_json
from costweave.kernel.planner import decide, plan
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore


def operation(name, requires, cost):
    return {"id": name, "requires": requires, "produces": "receipt",
            "executor": "fixture", "validator": "present", "cost_units": cost}


def fixture():
    return {
        "goals": ["receipt"], "inputs": {"report": {"finding": "conflict"}}, "budget_units": 5,
        "operations": [operation("copy", ["report"], 1), operation("recompute", ["report"], 4),
                       operation("from-lines", ["lines", "report"], 2)],
        "acceptance": {key: {"validator": "present", "config": {}} for key in ("report", "receipt", "lines")},
        "evidence_rules": [
            {"artifact": "report", "field": "finding", "equals": "conflict", "operation": "recompute"},
            {"artifact": "report", "field": "finding", "equals": "missing",
             "request": "lines", "reason": "Provide the original line items"},
            {"artifact": "report", "field": "finding", "equals": "missing", "operation": "from-lines"},
        ],
    }


class EvidencePolicyTests(unittest.TestCase):
    def test_explicit_acquisition_can_unlock_a_request_before_goal_reachability(self):
        raw = {
            "goals": ["receipt"], "inputs": {}, "budget_units": 5,
            "operations": [
                {"id": "scan", "requires": [], "produces": "page", "executor": "literal",
                 "validator": "present", "cost_units": 1, "config": {"value": {"finding": "missing"}}},
                {"id": "check", "requires": ["page"], "produces": "check", "executor": "pick",
                 "validator": "present", "cost_units": 2, "config": {"source": "page"}},
                {"id": "finish", "requires": ["check", "lines"], "produces": "receipt", "executor": "collect",
                 "validator": "present", "cost_units": 2},
            ],
            "acceptance": {key: {"validator": "present", "config": {}} for key in ("receipt", "lines", "check")},
            "evidence_rules": [{"artifact": "check", "field": "finding", "equals": "missing", "request": "lines",
                                "reason": "Supply original line items", "acquire": True}],
        }
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        runtime = Runtime(store)
        preview = runtime.preview(Job.from_dict(raw))
        self.assertEqual((("scan", "check"), 3, "acquire"),
                         (preview.operations, preview.cost_units, preview.evidence.kind))
        waiting = runtime.submit("invoice", Job.from_dict(raw))
        self.assertEqual(("waiting_for_input", 3, "lines"),
                         (waiting["status"], waiting["spent_units"], waiting["clarification"]["artifact"]))
        supplied = runtime.provide("invoice", "lines", [12, 10])
        self.assertEqual(("complete", 5), (supplied["status"], supplied["spent_units"]))
        small = runtime.submit("tight", Job.from_dict({**raw, "budget_units": 2}))
        self.assertEqual(("over_budget", 0), (small["reason"], small["spent_units"]))
        del raw["evidence_rules"][0]["acquire"]
        passive = runtime.submit("passive", Job.from_dict(raw))
        self.assertEqual(("unreachable", 0), (passive["reason"], passive["spent_units"]))

    def test_values_select_actions_and_receipt_identifies_the_whole_observation(self):
        job = Job.from_dict(fixture())
        report = {"finding": "conflict", "sources": ["invoice-1", "ledger-2"]}
        decision = decide(job, {"report": report}, (), 5)
        self.assertEqual(("recompute",), decision.operations)
        self.assertEqual(4, decision.cost_units)
        self.assertFalse(decision.optimal)
        self.assertEqual({
            "policy_version": 1, "rule_index": 0, "field": "finding", "artifact": "report",
            "operation": "recompute",
            "artifact_sha256": hashlib.sha256(canonical_json(report).encode()).hexdigest(),
        }, decision.to_dict()["evidence"])
        unmatched = decide(job, {"report": {"finding": "clear"}}, (), 5)
        self.assertEqual(("copy",), unmatched.operations)
        self.assertIsNone(unmatched.evidence)
        with self.assertRaisesRegex(ValueError, "accepted values"):
            plan(job, ["report"], (), 5)

    def test_wait_satisfied_then_choose_repair_without_mutating_job(self):
        raw = fixture()
        job = Job.from_dict(raw)
        raw["evidence_rules"][1]["reason"] = "changed"
        state = {"report": {"finding": "missing"}}
        decision = decide(job, state, (), 5)
        self.assertEqual("waiting_for_input", decision.status)
        self.assertEqual("lines", decision.evidence.request)
        self.assertEqual("Provide the original line items", decision.evidence.reason)
        self.assertEqual(job, Job.from_dict(job.to_dict()))
        state["lines"] = [4, 5]
        repaired = decide(job, state, (), 5)
        self.assertEqual(("from-lines",), repaired.operations)
        self.assertEqual(2, repaired.evidence.rule_index)
        state["receipt"] = {"total": 9}
        self.assertEqual("complete", decide(job, state, (), 1).status)

    def test_mandatory_action_blocks_on_unavailable_dependencies_rejection_or_budget(self):
        raw = fixture()
        raw["evidence_rules"] = raw["evidence_rules"][2:] + raw["evidence_rules"][0:2]
        job = Job.from_dict(raw)
        for report, excluded, budget, status in [
            ("missing", (), 5, "evidence_prerequisite_missing"),
            ("conflict", ["recompute"], 5, "evidence_action_rejected"),
            ("conflict", (), 2, "over_budget"),
        ]:
            with self.subTest(status=status):
                decision = decide(job, {"report": {"finding": report}}, excluded, budget)
                self.assertEqual(status, decision.status)
                self.assertEqual((), decision.operations)
                self.assertIsNotNone(decision.evidence)

    def test_predicates_distinguish_boolean_number_null_and_absent(self):
        for expected, observed, matched in [(True, 1, False), (1, True, False), (None, None, True),
                                            (1, 1.0, False), (False, False, True)]:
            raw = fixture()
            raw["evidence_rules"][0]["equals"] = expected
            job = Job.from_dict(raw)
            with self.subTest(expected=expected, observed=observed):
                choice = decide(job, {"report": {"finding": observed}}, (), 5)
                self.assertEqual(matched, choice.operations == ("recompute",))
                self.assertEqual(("copy",), decide(job, {"report": {}}, (), 5).operations)

    def test_only_valid_fields_admitted_evidence_and_known_operations(self):
        for replacement in [
            {"operation": "not-an-operation"}, {"field": []}, {"equals": {"$gt": 0}},
            {"equals": float("nan")}, {"artifact": "no-such-artifact"}, {"extra": "not-allowed"}, {"acquire": 1},
            {"request": "lines", "reason": "both operation and request"},
        ]:
            raw = fixture()
            raw["evidence_rules"][0].update(replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                Job.from_dict(raw)
        for required in ["report", "lines"]:
            raw = fixture()
            del raw["acceptance"][required]
            with self.subTest(required=required), self.assertRaises(ValueError):
                Job.from_dict(raw)
        raw = fixture()
        del raw["evidence_rules"], raw["acceptance"]["lines"]
        raw["operations"].pop()
        job = Job.from_dict(raw)
        self.assertNotIn("evidence_rules", job.to_dict())
        self.assertEqual(plan(job, job.inputs.keys(), (), 5), decide(job, job.inputs, (), 5))
