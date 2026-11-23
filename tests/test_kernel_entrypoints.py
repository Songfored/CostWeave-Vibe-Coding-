import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from costweave.kernel.catalog import compile_request
from costweave.kernel.contracts import Job
from costweave.kernel.ports import default_registry
from costweave.kernel.runtime import Runtime, public_state
from costweave.kernel.store import KernelStore


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "artifact-runtime"


def read(name):
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


class KernelEntrypointTests(unittest.TestCase):
    def test_catalog_selects_applicable_recipes_and_freezes_the_selected_contract(self):
        catalog = read("catalog.json")
        unused = {**catalog["operations"][0], "id": "unused", "requires": ["other-input"]}
        catalog["operations"].append(unused)
        request = read("handoff.json")
        job = compile_request(request, catalog)
        self.assertNotIn("unused", [op.id for op in job.operations])
        catalog["operations"][0]["config"]["path"] = ["changed"]
        preview = next(op for op in job.operations if op.id == "read-preview")
        self.assertEqual(["preview"], preview.config["path"])
        with self.assertRaisesRegex(ValueError, "cannot produce"):
            compile_request({**request, "inputs": {}}, catalog)

    def test_resume_rechecks_saved_raw_result_without_dispatch(self):
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        registry = default_registry()
        calls = []
        validations = []

        def generate(operation, inputs, *, attempt_id):
            calls.append(attempt_id)
            return {"answer": 42}

        def flaky_validator(operation, value, inputs):
            validations.append(value)
            if len(validations) == 1:
                raise RuntimeError("validator unavailable")
            return value == {"answer": 42}

        registry.register_executor("generate", generate)
        registry.register_validator("checked-answer", flaky_validator)
        job = Job.from_dict({
            "inputs": {}, "goals": ["report"], "budget_units": 2,
            "acceptance": {"report": {"validator": "checked-answer", "config": {}}},
            "operations": [{"id": "one", "requires": [], "produces": "report", "cost_units": 2,
                            "executor": "generate", "validator": "present"}],
        })
        waiting = public_state(Runtime(store, registry).submit("cached", job))
        self.assertEqual(("needs_reconciliation", {}, 2), (waiting["status"], waiting["outputs"], waiting["held_units"]))
        self.assertNotIn("value", waiting["active_attempt"])
        final = public_state(Runtime(store, registry).resume("cached"))
        self.assertEqual(("complete", 2, {"report": {"answer": 42}}),
                         (final["status"], final["spent_units"], final["outputs"]))
        self.assertEqual((1, 2), (len(calls), len(validations)))

    def test_initial_goal_must_pass_before_it_is_committed(self):
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        request = {**read("handoff.json"), "inputs": {"handoff": {"owner": "Lin"}}}
        with self.assertRaisesRegex(ValueError, "failed its acceptance"):
            Runtime(store).submit("invalid-seed", compile_request(request, read("catalog.json")))
        with self.assertRaises(KeyError):
            store.snapshot("invalid-seed")

    def test_python_entrypoint_plans_without_a_database_and_reuses_run_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "ledger.db"
            def cli(*args):
                process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "costweave",
                                          "--db", str(db), *map(str, args)], cwd=ROOT,
                                         capture_output=True, encoding="utf-8", timeout=20)
                self.assertEqual(0, process.returncode, process.stderr)
                return json.loads(process.stdout)
            job = [EXAMPLE / "handoff.json", "--catalog", EXAMPLE / "catalog.json"]
            self.assertEqual(["read-preview"], cli("plan", *job)["operations"])
            self.assertFalse(db.exists())
            expected = cli("run", *job, "--id", "entry")
            self.assertEqual({"owner": "Lin", "hours": 4}, expected["outputs"]["handoff"])
            self.assertEqual(4, expected["spent_units"])
            journal = cli("events", "entry")
            choices = [event for event in journal if event["type"] == "attempt_claimed"]
            self.assertEqual(["read-preview"], choices[0]["plan"]["operations"])
            self.assertEqual(4, choices[1]["remaining_units"])
            self.assertEqual("assemble", choices[1]["plan"]["operations"][-1])
            self.assertEqual(expected, cli("run", *job, "--id", "entry"))
            self.assertEqual(journal, cli("events", "entry"))

    def test_completed_competing_attempt_invalidates_a_stale_plan(self):
        job = compile_request(read("handoff.json"), read("catalog.json"))
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        store.create("race", job.to_dict())
        attempt = store.claim("race", "read-preview", expected_excluded=[])
        store.settle("race", attempt, accepted=False, reason="validation_rejected")
        self.assertIsNone(store.claim("race", "read-hours", expected_excluded=[]))
        self.assertIsNotNone(store.claim("race", "read-hours", expected_excluded=["read-preview"]))
