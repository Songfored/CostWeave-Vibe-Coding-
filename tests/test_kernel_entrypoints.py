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
from costweave.kernel.store import KernelStore, RunConflictError


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "artifact-runtime"
EVIDENCE = ROOT / "examples" / "evidence-invoice"


def read(name):
    return json.loads((EXAMPLE / name).read_text(encoding="utf-8"))


class KernelEntrypointTests(unittest.TestCase):
    def test_catalog_preserves_requested_inputs_and_evidence_only_operations(self):
        job = json.loads((EVIDENCE / "job.json").read_text(encoding="utf-8"))
        catalog = {key: job.pop(key) for key in ("operations", "acceptance")}
        catalog["operations"].append({"id": "record", "requires": ["check"], "produces": "note",
                                      "executor": "collect", "validator": "present", "cost_units": 0})
        job["evidence_rules"].append({"artifact": "check", "field": "finding", "equals": "special",
                                      "operation": "record"})
        compiled = compile_request(job, catalog)
        self.assertEqual({"record", "assemble-handoff"}, {op.id for op in compiled.operations})
        self.assertIn("line_items", compiled.acceptance)
        self.assertEqual(job["evidence_rules"], compiled.to_dict()["evidence_rules"])

    def test_cli_provides_requested_input_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "invoice.db"
            def invoke(*args, code):
                result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "costweave",
                                         "--db", str(db), *map(str, args)], cwd=ROOT,
                                        capture_output=True, encoding="utf-8", timeout=20)
                self.assertEqual(code, result.returncode, result.stderr)
                return json.loads(result.stdout)
            preview = invoke("plan", EVIDENCE / "job.json", code=4)
            self.assertEqual("waiting_for_input", preview["status"])
            self.assertFalse(db.exists())
            waiting = invoke("run", EVIDENCE / "job.json", "--id", "invoice", code=4)
            self.assertEqual("line_items", waiting["clarification"]["artifact"])
            self.assertEqual(waiting, invoke("resume", "invoice", code=4))
            args = ("provide", "invoice", "line_items", EVIDENCE / "line-items.json")
            final = invoke(*args, code=0)
            self.assertEqual(("complete", 1), (final["status"], final["spent_units"]))
            self.assertIn("line_items", final["outputs"]["handoff"])
            self.assertEqual(final, invoke(*args, code=0))

    def test_legacy_jobs_keep_identity_when_default_policy_and_roles_are_explicit(self):
        legacy = {
            "goals": ["receipt"], "inputs": {}, "budget_units": 2, "search_limit": 10000,
            "operations": [{"id": "read", "requires": [], "produces": "receipt", "executor": "emit",
                            "validator": "present", "cost_units": 2, "config": {}}],
            "acceptance": {"receipt": {"validator": "present", "config": {}}},
        }
        explicit = {**legacy, "planning_policy": "lowest_cost",
                    "operations": [{**legacy["operations"][0], "route_role": "standard"}]}
        self.assertEqual(legacy, Job.from_dict(explicit).to_dict())
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        store.create("old-run", legacy)
        original = store.snapshot("old-run")
        store.create("old-run", explicit)
        self.assertEqual(original, store.snapshot("old-run"))
        for change in ({"planning_policy": "preserve_fallback"},
                       {"operations": [{**legacy["operations"][0], "route_role": "speculative"}]}):
            with self.subTest(change=change), self.assertRaises(RunConflictError):
                store.create("old-run", {**legacy, **change})

    def test_catalog_freezes_policy_and_route_roles_and_rejects_unknown_values(self):
        request = {**read("handoff.json"), "planning_policy": "preserve_fallback"}
        catalog = read("catalog.json")
        for op in catalog["operations"]:
            op["route_role"] = "speculative" if op["id"] == "read-preview" else "fallback"
        job = compile_request(request, catalog)
        restored = Job.from_dict(job.to_dict())
        self.assertEqual(job, restored)
        self.assertEqual("preserve_fallback", restored.planning_policy)
        self.assertEqual({op["id"]: op["route_role"] for op in catalog["operations"]},
                         {op.id: op.route_role for op in restored.operations})
        for invalid in (None, [], "unknown"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "planning_policy"):
                    Job.from_dict({**job.to_dict(), "planning_policy": invalid})
                catalog["operations"][0]["route_role"] = invalid
                with self.assertRaisesRegex(ValueError, "route_role"):
                    compile_request(request, catalog)

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
