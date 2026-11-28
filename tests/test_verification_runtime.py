"""Exercise calibrated choices through the real ledger, including deadlines across restarts."""

import copy
import tempfile
import unittest
from pathlib import Path

from costweave.kernel.catalog import compile_request
from costweave.kernel.contracts import Job
from costweave.kernel.planner import decide
from costweave.kernel.ports import default_registry
from costweave.kernel.runtime import Runtime, public_state
from costweave.kernel.store import KernelStore
from costweave.kernel.verification_value import fit_verification_model


def task():
    rows = []
    for signal in ("clear", "disputed"):
        for i in range(300):
            rows.append({"case_id": f"{signal}{i}", "stratum": signal, "outcomes": {
                "direct": {"correct": signal == "clear" or i < 12, "cost_units": 1, "elapsed_ms": 3},
                "check": {"correct": True, "cost_units": 2, "elapsed_ms": 12},
            }})
    model = fit_verification_model(rows, baseline="direct", actions=["direct", "check"])
    return {"goals": ["result"], "inputs": {"signal": {"group": "disputed"}}, "budget_units": 2,
            "operations": [
                {"id": route, "produces": "result", "requires": ["signal"], "executor": "literal",
                 "config": {"value": route}, "validator": "present", "cost_units": cost}
                for route, cost in (("check", 2), ("direct", 1))
            ], "acceptance": {"signal": {"validator": "present", "config": {}},
                              "result": {"validator": "present", "config": {}}},
            "verification_policy": {"artifact": "signal", "field": "group", "model": model,
                                    "deadline_ms": 25, "min_on_time_correct": 0.70,
                                    "route_ms": {"direct": 3, "check": 12}, "fallback_operation": "direct"}}


class RuntimeVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = task()

    def test_catalog_preview_and_dispatch_match_audited_state(self):
        request = copy.deepcopy(self.raw)
        catalog = {key: request.pop(key) for key in ("operations", "acceptance")}
        job = compile_request(request, catalog)
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        rt = Runtime(store, clock_ms=lambda: 1000)
        preview = rt.preview(job)
        self.assertEqual(("check",), preview.operations)
        self.assertEqual(("direct",), decide(job, {"signal": {"group": "clear"}}, [], 2).operations)
        self.assertEqual("no_eligible_verification_route", decide(job, job.inputs, [], 1).status)
        self.assertEqual("no_eligible_verification_route", decide(job, job.inputs, [], 2, remaining_ms=5).status)
        state = rt.submit("a", job)
        self.assertEqual((1025, "check", 2), (public_state(state)["deadline_at_ms"], state["artifacts"]["result"], state["spent_units"]))
        claim = next(event for event in store.events("a") if event["type"] == "attempt_claimed")
        self.assertEqual(preview.to_dict(), claim["plan"])
        assessment = claim["plan"]["verification"]
        self.assertEqual(assessment["artifact_sha256"], state["artifact_hashes"]["signal"])
        self.assertEqual(assessment["candidates"]["check"]["corrected"], 288)
        self.assertEqual(state, rt.resume("a"))

    def test_sparse_and_known_failing_populations_do_not_share_the_same_fallback(self):
        job = Job.from_dict(self.raw)
        unknown = decide(job, {"signal": {"group": "new-source"}}, [], 2)
        self.assertEqual(("direct",), unknown.operations)
        self.assertEqual("fixed_fallback_without_quality_estimate", unknown.verification["selected_reason"])
        self.assertNotEqual("ready", decide(job, job.inputs, [], 2, remaining_ms=5).status)

    def test_failed_complete_route_does_not_reuse_initial_cohort_to_price_a_retry(self):
        raw = copy.deepcopy(self.raw)
        raw["verification_policy"]["min_on_time_correct"] = 0
        raw["budget_units"] = 4
        raw["acceptance"]["result"] = {"validator": "equals", "config": {"expected": "approved"}}
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        rt = Runtime(store, clock_ms=lambda: 1000)
        state = rt.submit("rejected", Job.from_dict(raw))
        self.assertEqual(("blocked", "verification_route_failed", 2),
                         (state["status"], state["reason"], state["spent_units"]))
        self.assertEqual(1, sum(e["type"] == "attempt_claimed" for e in store.events("rejected")))

    def test_unknown_external_result_keeps_reservation_until_late_reconciliation(self):
        raw = copy.deepcopy(self.raw)
        raw["operations"][0]["executor"] = "lost"
        registry = default_registry()

        def lost(op, inputs, *, attempt_id):
            raise RuntimeError("lost response")

        registry.register_executor("lost", lost)
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        clock = [1000]
        rt = Runtime(store, registry, clock_ms=lambda: clock[0])
        started = rt.submit("lost", Job.from_dict(raw))
        attempt = started["active_attempt"]["id"]
        clock[0] += 30
        pending = rt.resume("lost")
        self.assertEqual(("needs_reconciliation", 2), (pending["status"], pending["held_units"]))
        receipt = {"outcome": "returned", "value": "approved", "actual_units": 1}
        final = rt.reconcile("lost", attempt, receipt)
        self.assertEqual(("deadline_expired", 1, 0), (final["reason"], final["spent_units"], final["held_units"]))
        self.assertNotIn("result", final["artifacts"])
        self.assertEqual(final, rt.reconcile("lost", attempt, receipt))

    def test_late_and_exact_deadline_results_are_charged_once(self):
        for duration, terminal in ((25, "complete"), (26, "blocked")):
            with self.subTest(duration=duration):
                store = KernelStore(":memory:")
                self.addCleanup(store.close)
                clock = [1000]
                registry = default_registry()

                def slow(op, inputs, *, attempt_id):
                    clock[0] += duration
                    return "answer"

                registry.register_executor("slow", slow)
                raw = copy.deepcopy(self.raw)
                raw["operations"][0]["executor"] = "slow"
                rt = Runtime(store, registry, clock_ms=lambda: clock[0])
                st = public_state(rt.submit("run", Job.from_dict(raw)))
                self.assertEqual((terminal, 2, 0), (st["status"], st["spent_units"], st["held_units"]))
                self.assertEqual({"result": "answer"} if duration == 25 else {}, st["outputs"])
                self.assertEqual(st, public_state(rt.resume("run")))

    def test_deadline_is_anchored_once_and_survives_wait_and_restart(self):
        raw = copy.deepcopy(self.raw)
        raw["inputs"]["signal"]["missing"] = True
        raw["evidence_rules"] = [{"artifact": "signal", "field": "missing", "equals": True,
                                  "request": "detail", "reason": "Please supply the original document"}]
        raw["acceptance"]["detail"] = {"validator": "present", "config": {}}
        job = Job.from_dict(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.db"
            store = KernelStore(path)
            pending = Runtime(store, clock_ms=lambda: 1000).submit("case", job)
            self.assertEqual(("waiting_for_input", 1025), (pending["status"], pending["deadline_at_ms"]))
            store.close()
            other = KernelStore(path)
            self.addCleanup(other.close)
            rt = Runtime(other, clock_ms=lambda: 1026)
            viewed = rt.inspect("case")
            self.assertEqual("deadline_expired", viewed["reason"])
            self.assertNotIn("clarification", viewed)
            self.assertFalse(any(e["type"] == "run_finished" for e in other.events("case")))
            state = rt.submit("case", job)
            self.assertEqual(("blocked", "deadline_expired", 0, 1025),
                             (state["status"], state["reason"], state["spent_units"], state["deadline_at_ms"]))
            self.assertNotIn("detail", state["artifacts"])
            self.assertEqual(1, sum(e["type"] == "run_finished" for e in other.events("case")))
