from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from costweave.control_kernel import ControlKernel
from costweave.release_readiness import audit_artifact_kernel_v2, build_release_status
from scripts.run_paired_benchmark import run_benchmark


class PairedBenchmarkAndReleaseV2Tests(unittest.TestCase):
    def test_release_readiness_includes_healthy_kernel_v2_audit(self) -> None:
        audit = audit_artifact_kernel_v2()
        self.assertTrue(audit["healthy"], audit)
        self.assertTrue(audit["bsao_cliff_safe"])
        self.assertTrue(audit["parallel_budget_conserved"])
        self.assertTrue(audit["crc_fail_closed"])
        self.assertTrue(audit["streaming_default_off"])

        with tempfile.TemporaryDirectory() as tmp:
            kernel = ControlKernel(Path(tmp) / "runtime.sqlite3")
            status = build_release_status(kernel)
            gates = {g["gate_id"]: g for g in status["gates"]}
            self.assertIn("artifact-kernel-v2-self-test", gates)
            self.assertTrue(gates["artifact-kernel-v2-self-test"]["passed"])
            self.assertTrue(status["engineering_ready"])

    def test_paired_120_case_benchmark_meets_cost_latency_quality_gates(self) -> None:
        report = run_benchmark(seed=20261124, bootstrap_samples=200)
        self.assertEqual(120, report["cases"])
        self.assertTrue(report["crc_calibration"]["admissible"])

        policies = report["policies"]
        for name, summary in policies.items():
            self.assertEqual(0, summary["budget_overruns"], name)

        cw = policies["costweave_next"]
        fr = policies["always_frontier"]
        naive = policies["naive_cascade"]
        self.assertGreaterEqual(cw["verified_completion_rate"], 0.98)
        self.assertGreaterEqual(cw["ground_truth_pass_rate"], fr["ground_truth_pass_rate"] - 0.01)

        cost_ci = report["paired_vs_always_frontier"]["cost_reduction_pct"]
        self.assertGreaterEqual(cost_ci["mean_difference"], 70.0)
        self.assertGreaterEqual(cost_ci["lower"], 65.0)

        pass_ci = report["paired_vs_always_frontier"]["ground_truth_pass_diff"]
        self.assertGreaterEqual(pass_ci["lower"], -0.02)

        by_fam = report["by_family"]
        # Long-doc Map-Reduce slashes P95 critical-path latency cleanly below always_frontier
        self.assertLess(
            by_fam["long_doc_mapreduce"]["costweave_next"]["p95_latency_ms"],
            by_fam["long_doc_mapreduce"]["always_frontier"]["p95_latency_ms"],
        )
        # Open-ended Split-CRC keeps silent corruption below 4% and strictly beats uncalibrated naive routing
        self.assertLessEqual(by_fam["compliance_open"]["costweave_next"]["silent_corruption_rate"], 0.04)
        self.assertLess(
            by_fam["compliance_open"]["costweave_next"]["silent_corruption_rate"],
            by_fam["compliance_open"]["naive_cascade"]["silent_corruption_rate"],
        )
        self.assertLess(cw["silent_corruption_rate"], naive["silent_corruption_rate"])

    def test_external_traces_jsonl_replay(self) -> None:
        trace_case = {
            "case_id": "ext-trace-1",
            "family": "coding_fanout",
            "task": {
                "goal_name": "bundle",
                "prompt": "Build small verified bundle",
                "inputs": {"spec": "mini"},
                "budget_units": 50000,
                "acceptance_validator": "json_object",
                "acceptance_config": {"required_keys": ["ok"]},
            },
            "outcomes": {
                "direct_cheap-text": {"value": {"ok": True}, "actual_units": 80, "latency_ms": 150, "ground_truth_ok": True},
                "direct_frontier": {"value": {"ok": True}, "actual_units": 2500, "latency_ms": 900, "ground_truth_ok": True},
                "gvr_draft_cheap-text": {"value": {"draft": True}, "actual_units": 60, "latency_ms": 120, "ground_truth_ok": True},
                "gvr_repair_cheap-code": {"value": {"ok": True}, "actual_units": 70, "latency_ms": 130, "ground_truth_ok": True},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "traces.jsonl"
            tpath.write_text(json.dumps(trace_case) + "\n", encoding="utf-8")
            rep = run_benchmark(seed=1, bootstrap_samples=50, traces_path=tpath)
            self.assertEqual(1, rep["cases"])
            self.assertEqual(1.0, rep["policies"]["costweave_next"]["verified_completion_rate"])
            self.assertEqual(0, rep["policies"]["costweave_next"]["budget_overruns"])


if __name__ == "__main__":
    unittest.main()
