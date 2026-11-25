import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.runtime import Runtime, public_state
from costweave.kernel.store import KernelStore, RunConflictError


def clarification_job():
    return Job(
        ["invoice"], {"report": {"finding": "missing"}}, 2,
        [Operation("read", ["lines"], "invoice", "pick", "present", 2,
                   {"source": "lines", "path": ["amount"]})],
        acceptance={
            "report": {"validator": "json_object", "config": {"required_keys": ["finding"]}},
            "lines": {"validator": "json_object", "config": {"required_keys": ["amount"]}},
            "invoice": {"validator": "equals", "config": {"expected": 18}},
        },
        evidence_rules=[{"artifact": "report", "field": "finding", "equals": "missing",
                         "request": "lines", "reason": "Supply the original invoice lines"}],
    )


class ClarificationRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "ledger.sqlite3"
        self.store = KernelStore(self.path)
        self.runtime = Runtime(self.store)
        self.job = clarification_job()

    def test_wait_is_durable_validated_and_resumes_at_most_once(self):
        preview = self.runtime.preview(self.job)
        waiting = self.runtime.submit("one", self.job)
        self.assertEqual(("waiting_for_input", 0, 0),
                         (waiting["status"], waiting["held_units"], waiting["spent_units"]))
        self.assertEqual({"artifact": "lines", "reason": "Supply the original invoice lines"},
                         public_state(waiting)["clarification"])
        restored = Runtime(KernelStore(self.path))
        self.assertEqual(waiting, restored.resume("one"))
        asked = self.store.events("one")[-1]
        self.assertEqual("input_requested", asked["type"])
        self.assertEqual(preview.to_dict(), asked["plan"])
        self.assertEqual(waiting["artifact_hashes"]["report"], asked["plan"]["evidence"]["artifact_sha256"])
        with self.assertRaises(ValueError):
            restored.provide("one", "lines", {"unrelated": True})
        with self.assertRaises(ValueError):
            restored.provide("one", "report", {"finding": "missing"})
        with self.assertRaises(ValueError):
            restored.provide("one", "invoice", 18)
        self.assertEqual(waiting, self.store.snapshot("one"))
        supplied = {"amount": 18, "internal": "private clarification detail"}
        complete = restored.provide("one", "lines", supplied)
        self.assertEqual(("complete", 2, {"invoice": 18}), (complete["status"], complete["spent_units"],
                         public_state(complete)["outputs"]))
        self.assertEqual(complete, self.runtime.provide("one", "lines", dict(reversed(supplied.items()))))
        with self.assertRaises(RunConflictError):
            self.runtime.provide("one", "lines", {"amount": 19})
        events = self.store.events("one")
        self.assertEqual(1, sum(e["type"] == "input_requested" for e in events))
        self.assertEqual(1, sum(e["type"] == "input_supplied" for e in events))
        self.assertEqual(1, sum(e["type"] == "attempt_claimed" for e in events))
        self.assertNotIn("private clarification detail", json.dumps(events))

    def test_racing_answers_and_dispatch_cannot_overwrite_each_other(self):
        self.runtime.submit("race", self.job)
        barrier = Barrier(2)

        def provide(note):
            other = Runtime(KernelStore(self.path))
            barrier.wait(5)
            try:
                other.provide("race", "lines", {"amount": 18, "note": note})
                return True
            except RunConflictError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(1, sum(pool.map(provide, ["first", "second"])))
        state = self.runtime.resume("race")
        self.assertEqual(("complete", 2), (state["status"], state["spent_units"]))
        self.assertEqual(1, sum(e["type"] == "attempt_claimed" for e in self.store.events("race")))

    def test_stale_snapshot_cannot_claim_finish_or_request_after_supply(self):
        waiting = self.runtime.submit("stale", self.job)
        stale = {"expected_hashes": waiting["artifact_hashes"], "expected_excluded": []}
        self.assertFalse(self.store.finish("stale", "blocked", "unreachable", **stale))
        self.store.provide_validated("stale", "lines", {"amount": 18})
        self.assertIsNone(self.store.claim("stale", "read", **stale))
        self.assertFalse(self.store.finish("stale", "blocked", "unreachable", **stale))
        self.assertFalse(self.store.request_input("stale", self.runtime.preview(self.job), **stale))
        self.assertEqual("complete", self.runtime.resume("stale")["status"])

    def test_supply_event_failure_rolls_back_answer_and_keeps_wait(self):
        waiting = self.runtime.submit("rollback", self.job)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER reject_input BEFORE INSERT ON kernel_events "
                       "WHEN NEW.type='input_supplied' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.runtime.provide("rollback", "lines", {"amount": 18})
        self.assertEqual(waiting, self.store.snapshot("rollback"))

    def test_existing_ledger_reopens_without_changing_a_completed_run(self):
        legacy = Job(["done"], {"done": 1}, 0, [],
                     acceptance={"done": {"validator": "present", "config": {}}})
        original = self.runtime.submit("old", legacy)
        events = self.store.events("old")
        # The previous schema has the same base tables, without the clarification table.
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE kernel_clarifications")
        db.close()
        upgraded = KernelStore(self.path)
        self.assertEqual(original, Runtime(upgraded).resume("old"))
        self.assertEqual(events, upgraded.events("old"))
        self.assertEqual("waiting_for_input", Runtime(upgraded).submit("new", self.job)["status"])
