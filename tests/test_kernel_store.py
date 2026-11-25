import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from costweave.kernel.store import KernelStore, RunConflictError


def op(id, requires, produces, cost):
    return {"id": id, "requires": requires, "produces": produces,
            "executor": "test-worker", "validator": "test-check", "cost_units": cost}


def job(budget=6):
    return {
        "goals": ["final"], "inputs": {"seed": {"z": 3, "a": "蓝"}},
        "budget_units": budget, "operations": [
            op("draft", ["seed"], "draft", 2),
            op("deliver", ["draft"], "final", 4),
            op("direct", ["seed"], "final", 5),
        ],
        "acceptance": {"final": {"validator": "test-check", "config": {}}},
    }


class KernelStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "ledger.sqlite3"
        self.store = KernelStore(self.path)

    def test_idempotent_creation_freezes_job_and_input_hashes(self):
        spec = job()
        self.store.create("x", spec)
        snapshot = self.store.snapshot("x")
        seed_digest = hashlib.sha256('{"a":"蓝","z":3}'.encode()).hexdigest()
        self.assertEqual(seed_digest, snapshot["artifact_hashes"]["seed"])
        self.assertEqual(seed_digest, self.store.events("x")[1]["sha256"])
        spec["operations"].reverse()
        self.store.create("x", spec)
        self.assertEqual(2, len(self.store.events("x")))
        spec["inputs"]["seed"]["z"] = 42
        with self.assertRaises(RunConflictError):
            self.store.create("x", spec)
        self.assertEqual(snapshot, self.store.snapshot("x"))

    def test_transaction_admission_checks_inputs_goals_price_and_repeated_work(self):
        self.store.create("x", job(4))
        with self.assertRaises(ValueError):
            self.store.claim("x", "unknown")
        self.assertIsNone(self.store.claim("x", "deliver"))
        self.assertIsNone(self.store.claim("x", "direct"))
        first = self.store.claim("x", "draft")
        self.assertTrue(first)
        self.assertEqual(2, self.store.snapshot("x")["held_units"])
        self.assertFalse(self.store.finish("x", "blocked", "over_budget", expected_excluded=[]))
        self.store.settle("x", first, accepted=False, actual_units=1)
        self.assertIsNone(self.store.claim("x", "draft"))
        self.assertFalse(self.store.finish("x", "blocked", "over_budget", expected_excluded=[]))
        self.assertTrue(self.store.finish("x", "blocked", "over_budget", expected_excluded=["draft"]))
        self.assertEqual(["draft"], self.store.snapshot("x")["excluded"])
        self.assertEqual((1, 0), (self.store.snapshot("x")["spent_units"], self.store.snapshot("x")["held_units"]))

    def test_competing_connections_have_one_reservation(self):
        self.store.create("x", job())
        barrier = Barrier(8)

        def claim(index):
            other = KernelStore(self.path)
            barrier.wait(5)
            return other.claim("x", "draft" if index % 2 else "direct")

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = [result for result in pool.map(claim, range(8)) if result]
        self.assertEqual(1, len(ids))
        state = self.store.snapshot("x")
        self.assertEqual(ids[0], state["active_attempt"]["id"])
        self.assertEqual("needs_reconciliation", state["status"])
        claims = [e for e in self.store.events("x") if e["type"] == "attempt_claimed"]
        self.assertEqual([state["artifact_hashes"]], [e["input_hashes"] for e in claims])

    def test_committed_claim_survives_process_exit_without_releasing_budget(self):
        self.store.create("x", job())
        script = ("from costweave.kernel.store import KernelStore; import sys, os; "
                  "store = KernelStore(sys.argv[1]); assert store.claim('x', 'direct'); os._exit(17)")
        process = subprocess.run([sys.executable, "-B", "-c", script, str(self.path)],
                                 cwd=Path(__file__).resolve().parents[1], timeout=20, capture_output=True)
        self.assertEqual(17, process.returncode, process.stderr)
        restored = KernelStore(self.path)
        state = restored.snapshot("x")
        self.assertEqual(("claimed", 5, 0), (state["active_attempt"]["phase"], state["held_units"], state["spent_units"]))
        self.assertIsNone(restored.claim("x", "direct"))
        self.assertIsNone(restored.claim("x", "draft"))
        restored.settle("x", state["active_attempt"]["id"], accepted=False, reason="confirmed_failed",
                        actual_units=2, require_unreturned=True)
        self.assertTrue(restored.claim("x", "draft"))

    def test_returned_value_is_private_durable_and_idempotent_until_validated(self):
        self.store.create("x", job())
        attempt = self.store.claim("x", "direct")
        value = {"body": "private unverified output"}
        self.store.record_result("x", attempt, value, 2, receipt_id="receipt-9")
        restored = KernelStore(self.path)
        staged = restored.snapshot("x")
        self.assertEqual(("returned", value, 2, "receipt-9"),
                         tuple(staged["active_attempt"][k] for k in ("phase", "value", "actual_units", "receipt_id")))
        self.assertNotIn("final", staged["artifacts"])
        self.assertNotIn("private unverified output", json.dumps(restored.events("x")))
        for changed in ({"value": None, "actual_units": 2}, {"value": value, "actual_units": 3}):
            with self.assertRaises(ValueError):
                restored.record_result("x", attempt, receipt_id="receipt-9", **changed)
        with self.assertRaises(ValueError):
            restored.settle("x", attempt, accepted=False, reason="confirmed_failed", require_unreturned=True)
        restored.record_result("x", attempt, value, 2, receipt_id="receipt-9")
        restored.settle("x", attempt, accepted=True, reason="accepted")
        restored.settle("x", attempt, accepted=True, reason="accepted", actual_units=2)
        restored.record_result("x", attempt, value, 2, receipt_id="receipt-9")
        restored.mark_uncertain("x", attempt, "late_worker")
        with self.assertRaises(ValueError):
            restored.settle("x", attempt, accepted=False)
        state = restored.snapshot("x")
        self.assertEqual(value, state["artifacts"]["final"])
        self.assertEqual((2, 0, None), (state["spent_units"], state["held_units"], state["active_attempt"]))
        self.assertIsNone(restored.claim("x", "draft"))
        self.assertTrue(restored.finish("x", "complete", "complete"))
        self.assertTrue(restored.finish("x", "complete", "complete"))
        types = [e["type"] for e in restored.events("x")]
        self.assertEqual(["run_created", "input_recorded", "attempt_claimed", "result_recorded", "attempt_settled",
                          "run_finished"], types)

    def test_invalid_and_over_cap_receipts_preserve_the_same_reservation(self):
        self.store.create("x", job())
        attempt = self.store.claim("x", "direct")
        for amount in (6, -1, True, 3.5):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.store.record_result("x", attempt, "x", amount)
            with self.assertRaises(ValueError):
                self.store.settle("x", attempt, accepted=True, value="x", actual_units=amount)
        with self.assertRaises(ValueError):
            self.store.record_result("x", attempt, {1: "not a string"})
        self.store.mark_uncertain("x", attempt, "invalid_receipt")
        self.store.mark_uncertain("x", attempt, "invalid_receipt")
        self.assertEqual("invalid_receipt", self.store.snapshot("x")["reason"])
        self.assertEqual(5, self.store.snapshot("x")["held_units"])
        self.store.record_result("x", attempt, None)
        self.store.settle("x", attempt, accepted=False, reason="bad_json")
        state = self.store.snapshot("x")
        self.assertEqual(5, state["spent_units"])
        self.assertNotIn("final", state["artifacts"])
        self.assertIsNone(self.store.claim("x", "draft"))

    def test_admission_denial_stops_all_routes_in_same_transaction(self):
        self.store.create("x", job())
        attempt = self.store.claim("x", "direct")
        self.store.settle("x", attempt, accepted=False, actual_units=0, stop_reason="denied")
        self.assertIsNone(self.store.claim("x", "draft"))
        state = self.store.snapshot("x")
        self.assertEqual(("blocked", "denied", 0), (state["status"], state["reason"], state["spent_units"]))
        self.assertEqual(["attempt_settled", "run_finished"], [e["type"] for e in self.store.events("x")[-2:]])

    def test_failed_journal_write_rolls_back_settlement_and_publication(self):
        self.store.create("x", job())
        attempt = self.store.claim("x", "direct")
        self.store.record_result("x", attempt, {"x": "output"}, actual_units=4)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER fail_settlement BEFORE INSERT ON kernel_events "
                       "WHEN NEW.type='attempt_settled' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.settle("x", attempt, accepted=True)
        state = self.store.snapshot("x")
        self.assertEqual(("returned", 0, 5), (state["active_attempt"]["phase"], state["spent_units"], state["held_units"]))
        self.assertNotIn("final", state["artifacts"])

    def test_in_memory_store_and_missing_run(self):
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(KeyError):
            store.events("absent")
        spec = job()
        spec["inputs"]["final"] = None
        store.create("seed-complete", spec)
        self.assertIsNone(store.claim("seed-complete", "draft"))
        self.assertTrue(store.finish("seed-complete", "complete", "complete"))
