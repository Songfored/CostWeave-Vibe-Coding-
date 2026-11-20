from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from costweave import aurora_real_pilot as pilot
from costweave.aurora_checkpoint import CHECKPOINT_VERSION, analyze_report, terminal_checkpoint


def skipped(node, *deps):
    return {"node_id": node, "status": "skipped_missing_dependencies",
            "accepted": False, "missing_dependencies": list(deps)}


def rejected(node, **extra):
    return {"node_id": node, "status": "completed", "accepted": False, **extra}


def passed(node):
    return {"node_id": node, "status": "completed", "accepted": True}


class AuroraCheckpointTests(unittest.TestCase):
    def test_traces_reported_diamond_and_preserves_security_gate(self):
        rows = [
            skipped("final_assembly", "a", "b"), skipped("a", "x", "gate"), skipped("b", "x"),
            rejected("x", checks={"good": True, "total": False}),
            rejected("gate", status="failed", error="PromptInjectionError", diagnostic="private text"),
            rejected("irrelevant"),
        ]
        checkpoint = terminal_checkpoint("dynamic", rows, final_accepted=False)
        self.assertEqual("blocked", checkpoint["status"])
        self.assertEqual(
            [
                {"node_id": "gate", "reason": "authorization_required", "failed_checks": [],
                 "automatic_model_fallback_allowed": False},
                {"node_id": "x", "reason": "validation_rejected", "failed_checks": ["total"]},
            ],
            checkpoint["blockers"],
        )
        self.assertNotIn("private text", json.dumps(checkpoint))

    def test_missing_conflicting_and_cyclic_evidence_stays_unknown(self):
        rows = [skipped("final_assembly", "cycle", "missing", "accepted", "failed"),
                skipped("cycle", "final_assembly"), passed("accepted"), rejected("failed")]
        checkpoint = terminal_checkpoint("baseline", rows, final_accepted=False)
        self.assertEqual("incomplete_evidence", checkpoint["status"])
        self.assertEqual(["accepted", "final_assembly", "missing"], checkpoint["unknown_node_ids"])
        self.assertEqual(["failed"], [b["node_id"] for b in checkpoint["blockers"]])
        self.assertEqual("incomplete_evidence", terminal_checkpoint("p", rows, final_accepted=True)["status"])
        self.assertEqual("accepted", terminal_checkpoint("p", [passed("final_assembly")], final_accepted=True)["status"])

    def test_traces_discarded_provisional_replan_to_rejected_revision(self):
        rows = [
            skipped("final_assembly", "incident_replan"), passed("incident_replan"),
            passed("provider_selection_v2"), passed("financial_review_v2"),
            rejected("deployment_plan_v2"), skipped("security_review_v2", "deployment_plan_v2"),
        ]
        checkpoint = terminal_checkpoint("p", rows, final_accepted=False)
        self.assertEqual(["deployment_plan_v2"], [b["node_id"] for b in checkpoint["blockers"]])
        self.assertEqual("blocked", checkpoint["status"])
        rows = [skipped("final_assembly", "incident_replan"), passed("incident_replan")]
        self.assertEqual(["incident_replan"], terminal_checkpoint("p", rows, final_accepted=False)["unknown_node_ids"])

    def test_invalid_rows_and_unknown_status_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            terminal_checkpoint("p", [passed("x"), passed("x")], final_accepted=False)
        rows = [skipped("final_assembly", "node"), rejected("node", status="unrecognized")]
        self.assertEqual(["node"], terminal_checkpoint("p", rows, final_accepted=False)["unknown_node_ids"])

    def test_live_policy_checkpoint_matches_offline_analysis(self):
        def reject(node_id, **kwargs):
            return rejected(node_id, checks={"sample_check": False}), None

        with patch.object(pilot, "_execute_node", side_effect=reject):
            policy = pilot._execute_policy(
                policy_id=pilot.ALL_PREMIUM, routes=pilot.freeze_routes()[pilot.ALL_PREMIUM],
                providers={}, store=object(),
            )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            path.write_text(json.dumps({"policy_results": [policy]}), encoding="utf-8")
            self.assertEqual(policy["final_repair_checkpoint"], analyze_report(path)["policy_checkpoints"][0])
        self.assertEqual("blocked", policy["final_repair_checkpoint"]["status"])
        self.assertTrue(policy["final_repair_checkpoint"]["blockers"])

    def test_read_only_cli_ignores_paid_environment_and_embedded_results(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.json"
            policy = {"policy_id": "p", "accepted": True, "hard_gates_passed": True,
                      "nodes": [passed("final_assembly")], "final_repair_checkpoint": {"status": "blocked"}}
            path.write_text(json.dumps({"policy_results": [policy]}), encoding="utf-8")
            original = path.read_bytes()
            output = io.StringIO()
            with patch.object(pilot, "run_preflight") as preflight, patch.object(pilot, "run_real") as run:
                with redirect_stdout(output):
                    self.assertEqual(0, pilot.main(["--analyze-report", str(path)]))
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    pilot.main(["--analyze-report", str(path), "--fresh-replay"])
                preflight.assert_not_called()
                run.assert_not_called()
            analysis = json.loads(output.getvalue())
            self.assertEqual(0, analysis["provider_calls"])
            self.assertEqual(hashlib.sha256(original).hexdigest(), analysis["report_sha256"])
            self.assertEqual("accepted", analysis["policy_checkpoints"][0]["status"])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))
            policy["hard_gates_passed"] = False
            path.write_text(json.dumps({"policy_results": [policy]}), encoding="utf-8")
            self.assertEqual("incomplete_evidence", analyze_report(path)["policy_checkpoints"][0]["status"])

    def test_concurrent_publication_creates_one_complete_immutable_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "frozen.json"
            barrier = Barrier(4)

            def publish(index):
                barrier.wait(5)
                try:
                    return pilot._publish_new_json(path, {"index": index})
                except pilot.AuroraRealPilotError:
                    return None

            with ThreadPoolExecutor(max_workers=4) as pool:
                attempts = list(pool.map(publish, range(4)))
            winner = json.loads(path.read_bytes())["index"]
            self.assertEqual(1, sum(result is not None for result in attempts))
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), attempts[winner])
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_freeze_precedes_ledger_and_claim_survives_failed_start(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "work/local-data/real-eval"
            output.mkdir(parents=True)
            historical = output / "aurora-real-pilot-v11-r3-0691-frozen-result.json"
            historical.write_bytes(b"archived")
            switches = {s: "1" for s in ("COSTWEAVE_EXPERIMENTAL_ADAPTERS", "COSTWEAVE_PAID_CALLS", pilot.PAID_AUTHORIZATION_ENV)}
            args = {"root": root, "budget_usd": 0.01, "budget_cny": 1, "fresh_replay": True}
            with patch.dict(pilot.os.environ, switches), patch.object(pilot, "run_preflight", return_value={"passed": True}), \
                    patch.object(pilot, "_load_registry", return_value=[]), patch.object(pilot, "EventStore", side_effect=RuntimeError("stop")) as store:
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    pilot.run_real(**args)
                with self.assertRaisesRegex(pilot.AuroraRealPilotError, "already exists"):
                    pilot.run_real(**args)
            store.assert_called_once()
            freeze = json.loads((output / f"aurora-real-pilot-{pilot.PILOT_SUFFIX}-frozen.json").read_bytes())
            self.assertEqual(pilot.EXPERIMENT_ID, freeze["experiment_id"])
            self.assertEqual(pilot.RUN_ID, freeze["run_id"])
            self.assertEqual(pilot.SCHEMA_VERSION, freeze["schema_version"])
            self.assertEqual(CHECKPOINT_VERSION, freeze["checkpoint_schema_version"])
            self.assertIn("aurora_real_pilot.py", freeze["source_sha256"])
            self.assertFalse(freeze["paid_calls_started"])
            self.assertEqual(b"archived", historical.read_bytes())
