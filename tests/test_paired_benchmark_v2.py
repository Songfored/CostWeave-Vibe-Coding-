from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from costweave.control_kernel import ControlKernel
from costweave.release_readiness import audit_artifact_kernel_v2, build_release_status
from scripts.run_paired_benchmark import (
    BENCHMARK_MODELS, _build_crc_calibration, _case_family_prompt, _policy_job, _synthetic_truth, run_benchmark,
)


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

    def test_120_synthetic_cases_expose_structurally_valid_but_wrong_totals(self) -> None:
        report = run_benchmark(seed=20261124, bootstrap_samples=50)
        self.assertEqual("synthetic_fixture", report["provenance"])
        self.assertEqual(120, report["cases"])
        self.assertTrue(report["crc_calibration"]["admissible"])
        rows = report["records"]["costweave_next"]
        self.assertEqual(120, len({r["case_id"] for r in rows}))
        clause_rows = [r for r in rows if r["family"] == "long_doc_mapreduce"]
        self.assertEqual(40, len(clause_rows))
        self.assertTrue(all(r["completed"] and r["silent_corruption"] for r in clause_rows))
        self.assertEqual(0.0, report["by_family"]["long_doc_mapreduce"]["costweave_next"]["ground_truth_pass_rate"])
        self.assertEqual(120, report["paired_vs_always_frontier"]["ground_truth_pass_diff_observed_pairs"])

    def test_isolated_ablations_keep_common_inputs_and_routes(self) -> None:
        crc, _ = _build_crc_calibration(20261124)
        for family in ("coding_fanout", "long_doc_mapreduce", "compliance_open"):
            task, _ = _case_family_prompt(family, 4)

            def graph(policy):
                return _policy_job(policy, task, BENCHMARK_MODELS, beliefs=None, crc_res=crc).to_dict()

            base, serial = graph("costweave_next"), graph("costweave_next_serial")
            self.assertEqual({**base, "max_parallel": 1}, serial)
            self.assertEqual({**serial, "planning_policy": "lowest_cost"}, graph("matched_lowest_cost_serial"))
            off = graph("costweave_next_no_decomposition_serial")
            shared = [op for op in serial["operations"] if not op["id"].startswith(
                ("sub_", "synth_", "map_", "reduce_", "collect_mapped", "slice_")
            )]
            self.assertEqual(shared, off["operations"])
            retained_artifacts = set(serial["inputs"]) | {op["produces"] for op in shared}
            self.assertEqual({key: val for key, val in serial["acceptance"].items() if key in retained_artifacts},
                             off["acceptance"])
            self.assertEqual({**serial, "acceptance": off["acceptance"], "operations": shared}, off)
            if family == "compliance_open":
                self.assertEqual(serial, off)

    def test_clause_reference_checks_individual_source_ids_and_amounts(self) -> None:
        spec, _ = _case_family_prompt("long_doc_mapreduce", 4)
        clauses = [{"id": i, "amount": a} for i, a in enumerate((100, 150, 200, 250))]
        self.assertTrue(_synthetic_truth(spec, "long_doc_mapreduce", 44, {"clauses": clauses, "total": 700}))
        wrong = {"clauses": [{"id": i, "amount": 100} for i in range(4)], "total": 400}
        self.assertFalse(_synthetic_truth(spec, "long_doc_mapreduce", 44, wrong))
        clauses[0]["amount"], clauses[3]["amount"] = 250, 100
        self.assertFalse(_synthetic_truth(spec, "long_doc_mapreduce", 44, {"clauses": clauses, "total": 700}))

    def _external_report(self, rows):
        task = {"goal_name": "bundle", "prompt": "Build small verified bundle", "inputs": {"spec": "mini"},
                "budget_units": 50_000, "acceptance_validator": "json_object",
                "acceptance_config": {"required_keys": ["ok"]}}
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "traces.jsonl"
            cases = [{"case_id": str(i), "family": "coding_fanout", "task": task, **row}
                     for i, row in enumerate(rows)]
            tpath.write_text("".join(json.dumps(row) + "\n" for row in cases), encoding="utf-8")
            return run_benchmark(seed=1, bootstrap_samples=10, traces_path=tpath)

    def test_external_missing_observations_are_unknown_and_never_replaced_by_simulations(self) -> None:
        report = self._external_report([
            {"outcomes": {"direct_frontier": {"value": {"ok": True}, "latency_ms": 7, "actual_units": 80,
                                              "ground_truth_ok": False}}},
            {"outcomes": {"direct_frontier": {"value": {"ok": True}, "latency_ms": 9}}},
            {},
        ])
        self.assertEqual("external_trace", report["provenance"])
        self.assertEqual(0, report["crc_calibration"]["sample_count"])
        wrong, missing_bill, missing_route = report["records"]["always_frontier"]
        self.assertTrue(wrong["completed"])
        self.assertTrue(wrong["silent_corruption"])
        self.assertFalse(wrong["ground_truth_ok"])
        self.assertEqual(80, wrong["cost_units"])
        self.assertTrue(missing_bill["completed"])
        self.assertIsNone(missing_bill["ground_truth_ok"])
        self.assertIsNone(missing_bill["cost_units"])
        self.assertIsNone(missing_route["completed"])
        self.assertTrue(missing_route["missing_outcome"])
        summary = report["policies"]["always_frontier"]
        self.assertEqual(1, summary["observed_cases"]["ground_truth_ok"])
        self.assertEqual(80, summary["observed_cost_units"])
        self.assertIsNone(summary["ground_truth_pass_rate"])
        self.assertIsNone(summary["total_cost_units"])
        self.assertIsNone(report["paired_vs_always_frontier"]["cost_reduction_pct"])

    def test_external_reference_and_unclipped_overquote_receipt(self) -> None:
        report = self._external_report([
            {"reference": {"value": {"ok": False}}, "outcomes": {"direct_frontier": {
                "value": {"ok": True}, "actual_units": 2, "ground_truth_ok": True, "latency_ms": 5}}},
            {"outcomes": {"direct_frontier": {"value": {"ok": True}, "actual_units": 75_000,
                                              "latency_ms": 5, "ground_truth_ok": True}}},
        ])
        wrong, excessive = report["records"]["always_frontier"]
        self.assertFalse(wrong["ground_truth_ok"])
        self.assertTrue(excessive["needs_reconciliation"])
        self.assertIsNone(excessive["ground_truth_ok"])
        self.assertEqual(75_000, excessive["cost_units"])
        self.assertTrue(excessive["budget_overrun"])
        self.assertEqual(["direct_frontier"], excessive["quote_overruns"])
        self.assertEqual(75_002, report["policies"]["always_frontier"]["total_cost_units"])
        with self.assertRaisesRegex(ValueError, "ground_truth_ok"):
            self._external_report([{"outcomes": {"direct_frontier": {"ground_truth_ok": "false"}}}])


if __name__ == "__main__":
    unittest.main()
