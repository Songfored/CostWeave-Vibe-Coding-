import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from costweave import execution_recovery_cli, joint_search_cli
from costweave.intelligence.execution_recovery import build_demo_snapshot
from costweave.intelligence.joint_search import build_demo_problem


class AdaptiveInputCliTests(unittest.TestCase):
    def run_with_input(self, main, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "输入.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--input", str(path)])
            result = json.loads(output.getvalue())
        self.assertEqual((0, 0), (result["provider_calls"], result["network_calls"]))
        return result

    def test_search_reports_supplied_window_on_candidates(self):
        problem = build_demo_problem().to_dict()
        problem["latency_scenarios"] = {"w": {
            node["node_id"]: {route["route_id"]: 2 for route in node["options"]}
            for node in problem["route_sets"]
        }}
        report = self.run_with_input(joint_search_cli.main, problem)
        self.assertGreater(report["candidate_count"], 0)
        for candidate in report["candidates"]:
            self.assertEqual(1, candidate["latency_replay"]["matched_scenarios"])

    def test_recovery_reports_missing_paired_outcomes_on_supplied_snapshot(self):
        snapshot = build_demo_snapshot().to_dict()
        snapshot["repair_allocation"] = {
            "checkpoint_sha256": "a" * 64, "validator_revision": "checks-v1",
            "baseline_policy_id": "original", "final_node_ids": ["final"], "remaining_actions": 1,
            "authorized_plan_ids": ["retry"], "plans": [{
                "plan_id": "retry", "action": "RETRY_NODE", "target_node_ids": ["draft"],
                "max_cost_usd": 0.06, "max_latency_ms": 600, "action_count": 1,
            }], "pairs": [],
        }
        report = self.run_with_input(execution_recovery_cli.main, snapshot)
        self.assertEqual("REQUEST_MISSING_INFO", report["action"])
        self.assertEqual(["draft"], report["decision"]["final_repair"]["blockers"])
