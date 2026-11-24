from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from costweave.kernel.compiler import ModelSpec, SubtaskSpec, TaskSpec, compile_task_hypergraph
from costweave.kernel.ports import KnownFailure, default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore
from costweave.kernel.verifiers import CRCCalibrator, CalibrationSample


ROOT = Path(__file__).resolve().parents[1]


class KernelCompilerTests(unittest.TestCase):
    def _models(self) -> list[ModelSpec]:
        return [
            ModelSpec(
                id="cheap-a",
                executor="m_cheap_a",
                provider_family="family_a",
                input_price_per_mtok_usd=0.2,
                output_price_per_mtok_usd=0.6,
                context_window_tokens=16_000,
                quote_output_tokens=1024,
                latency_p50_ms=300,
                latency_p95_ms=600,
                base_pass_prob=0.80,
                modalities=("text",),
            ),
            ModelSpec(
                id="cheap-b",
                executor="m_cheap_b",
                provider_family="family_b",
                input_price_per_mtok_usd=0.25,
                output_price_per_mtok_usd=0.75,
                context_window_tokens=32_000,
                quote_output_tokens=1024,
                latency_p50_ms=350,
                latency_p95_ms=700,
                base_pass_prob=0.82,
                modalities=("text",),
            ),
            ModelSpec(
                id="frontier",
                executor="m_frontier",
                provider_family="family_c",
                input_price_per_mtok_usd=3.0,
                output_price_per_mtok_usd=15.0,
                context_window_tokens=128_000,
                quote_output_tokens=1024,
                latency_p50_ms=1400,
                latency_p95_ms=2600,
                base_pass_prob=0.96,
                modalities=("text", "vision"),
            ),
            ModelSpec(
                id="offline-unroutable",
                executor="m_offline",
                provider_family="local",
                input_price_per_mtok_usd=0.0,
                output_price_per_mtok_usd=0.0,
                routable=False,
            ),
        ]

    def test_monolithic_baselines_modality_and_context_window_filtering(self) -> None:
        models = self._models()
        # 1) Long-doc chunks exceed cheap-a's 16k window in full form, but fit in chunk form!
        big_chunks = ["A" * 20_000 for _ in range(3)]  # ~15k tokens total > 16k - 1024 for cheap-a
        task = TaskSpec(
            goal_name="report",
            prompt="Summarize chunks",
            inputs={"sections": big_chunks},
            chunks_input_key="sections",
            budget_units=100_000,
            acceptance_validator="json_object",
            acceptance_config={"required_keys": ["ok"]},
            max_parallel=4,
            latency_weight_units_per_sec=200.0,
        )
        job = compile_task_hypergraph(task, models)
        op_ids = {op.id for op in job.operations}
        self.assertNotIn("direct_offline-unroutable", op_ids)
        self.assertNotIn("direct_cheap-a", op_ids)
        self.assertIn("direct_cheap-b", op_ids)
        self.assertIn("direct_frontier", op_ids)
        self.assertIn("slice_0", op_ids)
        self.assertIn("map_0_cheap-a", op_ids)
        self.assertIn("collect_mapped", op_ids)
        self.assertIn("reduce_frontier", op_ids)

        # 2) Modality filtering drops text-only models when vision is required.
        vis_task = TaskSpec(
            goal_name="caption",
            prompt="Describe image",
            inputs={"img": "ref"},
            budget_units=50_000,
            acceptance_validator="present",
            required_modalities=("text", "vision"),
        )
        vis_job = compile_task_hypergraph(vis_task, models)
        self.assertEqual({"direct_frontier"}, {op.id for op in vis_job.operations})

    def test_contract_locked_fanout_and_gvr_execution(self) -> None:
        models = self._models()
        task = TaskSpec(
            goal_name="bundle",
            prompt="Build bundle",
            inputs={"spec": "two modules"},
            budget_units=50_000,
            acceptance_validator="json_object",
            acceptance_config={"required_keys": ["a", "b"]},
            latency_weight_units_per_sec=400.0,
            max_parallel=4,
            subtasks=(
                SubtaskSpec(
                    name="part_a",
                    prompt="Build part A",
                    validator="json_schema_strict",
                    validator_config={"type": "object", "required": ["x"]},
                ),
                SubtaskSpec(
                    name="part_b",
                    prompt="Build part B",
                    validator="json_schema_strict",
                    validator_config={"type": "object", "required": ["y"]},
                ),
            ),
        )
        job = compile_task_hypergraph(task, models)
        self.assertIn("gvr_draft_cheap-a", {op.id for op in job.operations})
        self.assertIn("sub_part_a_cheap-a", {op.id for op in job.operations})
        self.assertIn("synth_frontier", {op.id for op in job.operations})

        reg = default_registry()
        reg.register_executor("m_cheap_a", lambda op, inp, *, attempt_id: {"x": 1} if "part A" in op.config["prompt"] else {"y": 2} if "part B" in op.config["prompt"] else {"a": 1, "b": 2})
        reg.register_executor("m_cheap_b", lambda op, inp, *, attempt_id: {"a": 1, "b": 2})
        reg.register_executor("m_frontier", lambda op, inp, *, attempt_id: {"a": 1, "b": 2})

        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        state = Runtime(store, reg).submit("fanout-run", job)
        self.assertEqual("complete", state["status"])
        self.assertEqual({"a": 1, "b": 2}, state["artifacts"]["bundle"])

    def test_open_ended_crc_fail_closed_vs_calibrated_consensus(self) -> None:
        models = self._models()
        task = TaskSpec(
            goal_name="verdict",
            prompt="Assess compliance risk",
            inputs={"case": "vendor clause"},
            budget_units=50_000,
            acceptance_validator="present",
            max_risk_alpha=0.05,
            failure_penalty_units=100_000,
            corruption_penalty_units=50_000,
            latency_weight_units_per_sec=500.0,
            max_parallel=4,
        )

        # Uncalibrated (n=0): dual-cheap consensus false_pass_risk == 1.0 -> BSAO* skips consensus
        uncal_res = CRCCalibrator().calibrate((), alpha=0.05)
        uncal_job = compile_task_hypergraph(task, models, crc=uncal_res)
        cons_op = next(op for op in uncal_job.operations if op.id == "cons_gate")
        self.assertEqual(1.0, cons_op.false_pass_risk)

        reg = default_registry()
        calls: list[str] = []

        def exec_a(op, inp, *, attempt_id):
            calls.append("a")
            return {"compliant": True}

        def exec_b(op, inp, *, attempt_id):
            calls.append("b")
            return {"compliant": True}

        def exec_frontier(op, inp, *, attempt_id):
            calls.append("frontier")
            return {"compliant": True}

        reg.register_executor("m_cheap_a", exec_a)
        reg.register_executor("m_cheap_b", exec_b)
        reg.register_executor("m_frontier", exec_frontier)

        store1 = KernelStore(":memory:")
        self.addCleanup(store1.close)
        plan_uncal = Runtime(store1, reg).preview(uncal_job)
        self.assertNotIn("cons_left_cheap-a", plan_uncal.operations)

        # Calibrated CRC with 100 samples (10 disagreements at score=0, 90 agreements with 0 false passes)
        samples = [CalibrationSample(0.0, 1.0) for _ in range(10)] + [CalibrationSample(1.0, 0.0) for _ in range(90)]
        cal_res = CRCCalibrator().calibrate(samples, alpha=0.05)
        cal_job = compile_task_hypergraph(task, models, crc=cal_res)
        cal_gate = next(op for op in cal_job.operations if op.id == "cons_gate")
        self.assertLessEqual(cal_gate.false_pass_risk, 0.05)

        store2 = KernelStore(":memory:")
        self.addCleanup(store2.close)
        state = Runtime(store2, reg).submit("cal-cons-ok", cal_job)
        self.assertEqual("complete", state["status"])
        self.assertEqual({"compliant": True}, state["artifacts"]["verdict"])
        self.assertEqual(["a", "b"], sorted(calls))

        # Now disagreement on cheap models -> falls back to frontier automatically!
        calls.clear()
        reg2 = default_registry()
        reg2.register_executor("m_cheap_a", lambda op, inp, *, attempt_id: (calls.append("a"), {"compliant": True})[1])
        reg2.register_executor("m_cheap_b", lambda op, inp, *, attempt_id: (calls.append("b"), {"compliant": False})[1])
        reg2.register_executor("m_frontier", lambda op, inp, *, attempt_id: (calls.append("frontier"), {"compliant": False})[1])

        store3 = KernelStore(":memory:")
        self.addCleanup(store3.close)
        state3 = Runtime(store3, reg2).submit("cal-cons-escalate", cal_job)
        self.assertEqual("complete", state3["status"])
        self.assertEqual({"compliant": False}, state3["artifacts"]["verdict"])
        self.assertIn("frontier", calls)

    def test_cli_compile_models_plan_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "task.json"
            mpath = Path(tmp) / "models.json"
            task_obj = TaskSpec(
                goal_name="out",
                prompt="Collect fields",
                inputs={"doc": "hello"},
                budget_units=20_000,
                acceptance_validator="present",
            )
            models_obj = [
                ModelSpec(
                    id="local-collect",
                    executor="collect",
                    provider_family="local",
                    input_price_per_mtok_usd=0.0,
                    output_price_per_mtok_usd=0.0,
                ).to_dict()
            ]
            tpath.write_text(json.dumps(task_obj.to_dict()), encoding="utf-8")
            mpath.write_text(json.dumps(models_obj), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", "-m", "costweave", "plan", str(tpath), "--compile-models", str(mpath)],
                cwd=ROOT,
                capture_output=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual("ready", out["status"])
            self.assertEqual(["direct_local-collect"], out["operations"])


if __name__ == "__main__":
    unittest.main()
