"""Regressions at the boundary between the evidence policy and the parallel ledger."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import ExecutionResult, default_registry
from costweave.kernel.runtime import Runtime, public_state
from costweave.kernel.store import KernelStore


def present(*names):
    return {name: {"validator": "present", "config": {}} for name in names}


class UnifiedRuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.db"
        self.store = KernelStore(self.path)
        self.now = 1000

    def clock(self):
        return self.now

    def timed_job(self, slots=2):
        return Job(["final"], {}, 5,
                   [Operation("work", [], "final", "worker", "present", 3, {}, latency_p50_ms=1)],
                   acceptance=present("final"), planning_policy="bsao", deadline_ms=40,
                   max_parallel=slots)

    def test_late_paid_result_never_publishes_in_either_execution_mode(self):
        registry = default_registry()

        def worker(op, inputs, *, attempt_id):
            self.now += 41
            return ExecutionResult("value", actual_units=2)

        registry.register_executor("worker", worker)
        for slots in (1, 2):
            with self.subTest(slots=slots):
                start = self.now
                runtime = Runtime(self.store, registry, clock_ms=self.clock)
                run_id = f"late-{slots}"
                state = runtime.submit(run_id, self.timed_job(slots))
                self.assertEqual(("blocked", "deadline_expired", 2, 0, start + 40),
                                 tuple(state[k] for k in ("status", "reason", "spent_units", "held_units", "deadline_at_ms")))
                self.assertNotIn("final", state["artifacts"])
                claim = next(e for e in self.store.events(run_id) if e["type"] == "attempt_claimed")
                receipt = {"outcome": "returned", "value": "value", "actual_units": 2}
                self.assertEqual(state, runtime.reconcile(run_id, claim["attempt_id"], receipt))

    def test_legacy_deadline_origin_and_unknown_receipt_survive_reopening(self):
        for run_id in ("anchored", "unanchored"):
            self.store.create(run_id, self.timed_job().to_dict(), clock_ms=self.clock)
        attempt = self.store.claim("unanchored", "work", clock_ms=self.clock)
        self.assertIsNotNone(attempt)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM kernel_deadlines")
            db.execute("UPDATE kernel_events SET data=json_remove(data, '$.started_at_ms', '$.deadline_at_ms') "
                       "WHERE run_id='unanchored' AND type='run_created'")
        restored = KernelStore(self.path)
        self.assertEqual(1040, restored.snapshot("anchored")["deadline_at_ms"])
        pending = restored.snapshot("unanchored")
        self.assertEqual(("needs_reconciliation", 3), (pending["status"], pending["held_units"]))
        restored.settle("unanchored", attempt, accepted=True, value="late", actual_units=1, clock_ms=self.clock)
        state = restored.snapshot("unanchored")
        self.assertEqual(("blocked", "deadline_origin_unknown", 1, 0, {}),
                         tuple(state[k] for k in ("status", "reason", "spent_units", "held_units", "artifacts")))
        self.assertEqual(state, KernelStore(self.path).snapshot("unanchored"))

    def test_risk_is_reserved_until_a_receipt_decides_whether_to_spend_it(self):
        ops = [Operation(key, [], key, "literal", "present", 1, {"value": 1}, false_pass_risk=0.06)
               for key in ("a", "b", "c")]
        job = Job(["a", "b", "c"], {}, 5, ops, acceptance=present("a", "b", "c"),
                  max_risk_alpha=0.1, max_parallel=3, planning_policy="bsao")
        self.store.create("risk", job.to_dict())
        first = self.store.claim("risk", "a")
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim("risk", "b"))
        self.assertEqual(0.06, public_state(self.store.snapshot("risk"))["risk_held"])
        self.store.settle("risk", first, accepted=False, actual_units=0)
        second = self.store.claim("risk", "b")
        self.assertIsNotNone(second)
        self.store.settle("risk", second, accepted=True, value=1)
        self.assertIsNone(self.store.claim("risk", "c"))
        state = self.store.snapshot("risk")
        self.assertEqual((0.0, 0.06), (state["risk_held"], state["risk_spent"]))

    def test_inflight_name_and_rejected_value_never_authorize_guard(self):
        ops = [Operation("observe", [], "report", "literal", "present", 1, {"value": {}}),
               Operation("guarded", [], "final", "literal", "present", 1, {"value": "x"},
                         when={"artifact": "report", "path": ["checks", 0, "ok"], "equals": True})]
        job = Job(["final"], {}, 5, ops, acceptance=present("report", "final"), max_parallel=2)
        self.store.create("guard", job.to_dict())
        first = self.store.claim("guard", "observe")
        self.assertIsNotNone(first)
        self.assertIsNone(self.store.claim("guard", "guarded"))
        self.store.record_result("guard", first, {"checks": [{"ok": 1}]})
        self.assertIsNone(self.store.claim("guard", "guarded"))
        self.store.settle("guard", first, accepted=True)
        self.assertIsNone(self.store.claim("guard", "guarded"))
        runtime = Runtime(self.store)
        state = runtime.resume("guard")
        self.assertEqual(("blocked", 1), (state["status"], state["spent_units"]))

    def test_parallel_evidence_acquisition_cannot_be_reused_to_launch_a_fallback(self):
        ops = [Operation("report", [], "report", "literal", "present", 1, {"value": {"decision": [True]}}),
               Operation("careful", [], "final", "literal", "present", 2, {"value": "checked"}),
               Operation("cheap", [], "final", "literal", "present", 1, {"value": "cheap"})]
        job = Job(["final"], {}, 5, ops, acceptance=present("final", "report"),
                  planning_policy="bsao", max_parallel=3, deadline_ms=30,
                  evidence_rules=[{"artifact": "report", "path": ["decision", 0], "equals": True,
                                   "operation": "careful", "acquire": True}])
        runtime = Runtime(self.store, clock_ms=self.clock)
        acquisition = runtime.preview(job)
        self.assertEqual(("report",), acquisition.operations)
        self.store.create("acquire", job.to_dict(), clock_ms=self.clock)
        with_fallback = replace(acquisition, operations=("cheap",), cost_units=1)
        self.assertIsNone(self.store.claim("acquire", "cheap", plan=with_fallback, clock_ms=self.clock))
        result = runtime.resume("acquire")
        claims = [e for e in self.store.events("acquire") if e["type"] == "attempt_claimed"]
        self.assertEqual(["report", "careful"], [e["op_id"] for e in claims])
        self.assertEqual(result["artifact_hashes"]["report"], claims[1]["plan"]["evidence"]["artifact_sha256"])
        self.assertEqual(("complete", 3, "checked"), (result["status"], result["spent_units"], result["artifacts"]["final"]))

    def test_backup_preserves_wait_and_original_deadline(self):
        job = Job(["final"], {"report": {"status": "missing"}}, 2,
                  [Operation("pick", ["answer"], "final", "literal", "present", 1, {"value": "done"})],
                  acceptance=present("report", "answer", "final"), max_parallel=2,
                  planning_policy="bsao", deadline_ms=40,
                  evidence_rules=[{"artifact": "report", "field": "status", "equals": "missing",
                                   "request": "answer", "reason": "Supply answer"}])
        runtime = Runtime(self.store, clock_ms=self.clock)
        waiting = runtime.submit("wait", job)
        self.assertEqual("waiting_for_input", waiting["status"])
        copy = self.path.with_name("backup.db")
        self.assertTrue(self.store.backup_and_verify(copy)["logical_hash_match"])
        restored = Runtime(KernelStore(copy), clock_ms=self.clock)
        self.assertEqual(waiting, restored.resume("wait"))
        self.now += 40
        state = restored.provide("wait", "answer", "too late")
        self.assertEqual(("blocked", "deadline_expired", 0, 0),
                         tuple(state[k] for k in ("status", "reason", "held_units", "spent_units")))
        self.assertNotIn("answer", state["artifacts"])
        self.assertEqual(waiting, self.store.snapshot("wait"))
