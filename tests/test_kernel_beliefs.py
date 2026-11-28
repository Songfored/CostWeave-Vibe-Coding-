from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from costweave.kernel.beliefs import (
    FEATURE_DIM,
    BeliefEngine,
    BeliefStore,
    clipped_dr_empirical_bernstein,
    LaplaceIRTModel,
    OutputLengthForecaster,
    extract_features,
    should_probe,
)
from costweave.kernel.compiler import ModelSpec, TaskSpec, compile_task_hypergraph


ROOT = Path(__file__).resolve().parents[1]


class KernelBeliefTests(unittest.TestCase):
    def test_cold_start_lcb_and_laplace_convergence_by_domain(self) -> None:
        code_prompt = "def parse():\n```python\nclass Helper:\n return {}\n``` strict exact"
        math_prompt = "calculate sum total usd amount and clause average ratio 123 456"
        f_code = extract_features(code_prompt, {"code": "class X: pass"})
        f_math = extract_features(math_prompt, {"nums": [10, 20, 30]})
        self.assertEqual(FEATURE_DIM, len(f_code))
        self.assertEqual(FEATURE_DIM, len(f_math))

        engine = BeliefEngine()
        # Cold-start: LCB is strictly lower than mean due to posterior uncertainty.
        mean0, lcb0, ucb0 = engine.irt.predict_pass_prob("model-code", f_code, prior_prob=0.85)
        self.assertAlmostEqual(0.85, mean0, places=4)
        self.assertLess(lcb0, mean0 - 0.08)
        self.assertGreater(ucb0, mean0)

        # Train: model-code succeeds on coding (40 obs) and fails on math (40 obs);
        # model-math has the opposite strength.
        for _ in range(40):
            engine.observe("model-code", code_prompt, {"code": "x"}, passed=True, completion_tokens=220)
            engine.observe("model-code", math_prompt, {"nums": [1, 2]}, passed=False, completion_tokens=220)
            engine.observe("model-math", code_prompt, {"code": "x"}, passed=False, completion_tokens=180)
            engine.observe("model-math", math_prompt, {"nums": [1, 2]}, passed=True, completion_tokens=180)

        m_code_on_code, lcb_cc, _ = engine.irt.predict_pass_prob("model-code", f_code)
        m_code_on_math, lcb_cm, _ = engine.irt.predict_pass_prob("model-code", f_math)
        m_math_on_code, lcb_mc, _ = engine.irt.predict_pass_prob("model-math", f_code)
        m_math_on_math, lcb_mm, _ = engine.irt.predict_pass_prob("model-math", f_math)
        self.assertGreater(lcb_cc, lcb_mc + 0.30)
        self.assertGreater(lcb_mm, lcb_cm + 0.30)
        self.assertGreater(m_code_on_code, 0.90)
        self.assertGreater(m_math_on_math, 0.90)

        # End-to-end check inside compile_task_hypergraph
        models = [
            ModelSpec(
                id="model-code", executor="exec_a", provider_family="fam_a",
                input_price_per_mtok_usd=0.3, output_price_per_mtok_usd=0.9, base_pass_prob=0.80,
            ),
            ModelSpec(
                id="model-math", executor="exec_b", provider_family="fam_b",
                input_price_per_mtok_usd=0.3, output_price_per_mtok_usd=0.9, base_pass_prob=0.80,
            ),
        ]
        code_task = TaskSpec(
            goal_name="out", prompt=code_prompt, inputs={"code": "class X: pass"},
            budget_units=10_000, acceptance_validator="json_object",
        )
        compiled = compile_task_hypergraph(code_task, models, beliefs=engine)
        op_code = next(op for op in compiled.operations if op.id == "direct_model-code")
        op_math = next(op for op in compiled.operations if op.id == "direct_model-math")
        self.assertGreater(op_code.pass_prob, op_math.pass_prob + 0.25)

    def test_output_length_forecaster_adjusts_expected_and_quote_units(self) -> None:
        prompt = "Summarize JSON status"
        features = extract_features(prompt, {"k": "v"})
        engine = BeliefEngine()
        for _ in range(25):
            engine.observe("terse-model", prompt, {"k": "v"}, passed=True, completion_tokens=64)
            engine.observe("verbose-model", prompt, {"k": "v"}, passed=True, completion_tokens=1600)

        m_terse = ModelSpec(
            id="terse-model", executor="e1", provider_family="a",
            input_price_per_mtok_usd=1.0, output_price_per_mtok_usd=2.0, quote_output_tokens=2048,
        )
        m_verb = ModelSpec(
            id="verbose-model", executor="e2", provider_family="b",
            input_price_per_mtok_usd=1.0, output_price_per_mtok_usd=2.0, quote_output_tokens=2048,
        )
        _, exp_t, q_t = engine.predict_route(m_terse, prompt, {"k": "v"}, prompt_tokens=100)
        _, exp_v, q_v = engine.predict_route(m_verb, prompt, {"k": "v"}, prompt_tokens=100)
        self.assertLess(exp_t, exp_v * 0.2)
        self.assertLess(q_t, q_v * 0.35)
        self.assertGreaterEqual(q_t, exp_t)
        self.assertGreaterEqual(q_v, exp_v)

    def test_should_probe_voi_gate(self) -> None:
        self.assertTrue(should_probe(0.45, baseline_units=5000, probe_units=300, remaining_budget=10000))
        self.assertFalse(should_probe(0.01, baseline_units=5000, probe_units=300, remaining_budget=10000))
        self.assertFalse(should_probe(0.45, baseline_units=5000, probe_units=300, remaining_budget=200))

    def test_clipped_dr_rejects_noisy_policy_promotes_better_and_rolls_back_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "beliefs.sqlite3"
            store = BeliefStore(db)
            try:
                self.assertEqual("v0-baseline", store.active_version()["version_id"])

                cand_bad = BeliefEngine()
                # Noisy/degraded observations: q_cand overly optimistic while realized utilities are negative
                bad_obs = [
                    {
                        "utility": -5.0 if i % 2 == 0 else 1.0,
                        "baseline_value": 2.0,
                        "q_cand": 6.0,
                        "propensity": 0.1,
                        "action_matches": True,
                    }
                    for i in range(40)
                ]
                rep_bad = store.evaluate_and_promote(cand_bad, bad_obs)
                self.assertFalse(rep_bad["promoted"])
                self.assertEqual("v0-baseline", store.active_version()["version_id"])

                # Cleanly superior policy -> Empirical Bernstein LCB > 0 -> promoted!
                cand_good = BeliefEngine()
                cand_good.observe("cheap", "test", {}, passed=True, completion_tokens=100)
                good_obs = [
                    {
                        "utility": 12.0,
                        "baseline_value": 3.0,
                        "q_cand": 11.5,
                        "propensity": 0.5,
                        "action_matches": True,
                    }
                    for _ in range(30)
                ]
                # Bounds come from the utility/weight contract, prior to evaluation.
                rep_good = store.evaluate_and_promote(cand_good, good_obs, score_bounds=(-10, 15),
                                                      version_id="v1-calibrated")
                self.assertTrue(rep_good["promoted"])
                self.assertEqual("v1-calibrated", store.active_version()["version_id"])
                loaded = store.load_active_engine()
                self.assertEqual(cand_good.to_dict(), loaded.to_dict())

                # Simulate drift spike -> automatic rollback to v0-baseline
                drift = store.check_drift_and_rollback(false_passes=5, sample_count=30, max_alpha=0.05)
                self.assertTrue(drift["drift_detected"])
                self.assertTrue(drift["rolled_back"])
                self.assertEqual("v0-baseline", store.active_version()["version_id"])
            finally:
                store.close()

            # Verify CLI beliefs status & rollback
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", "-m", "costweave", "--db", str(db), "beliefs", "status"],
                cwd=ROOT,
                capture_output=True,
                encoding="utf-8",
                timeout=20,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertEqual("v0-baseline", json.loads(proc.stdout)["version_id"])

    def test_rare_unobserved_losses_require_predeclared_support(self) -> None:
        same_scores = [{"utility": 0.01}] * 20
        store = BeliefStore(":memory:")
        try:
            no_bounds = store.evaluate_and_promote(BeliefEngine(), same_scores)
            self.assertFalse(no_bounds["promoted"])
            self.assertIsNone(no_bounds["stats"]["lcb"])
            rare_loss = store.evaluate_and_promote(BeliefEngine(), same_scores, score_bounds=(-1, 1))
            self.assertLess(rare_loss["stats"]["lcb"], 0.0)
            self.assertFalse(rare_loss["promoted"])
            with self.assertRaisesRegex(ValueError, "outside declared"):
                clipped_dr_empirical_bernstein([{"utility": -2}], score_bounds=(-1, 1))
            clipped = store.evaluate_and_promote(BeliefEngine(), [{"utility": 1, "ips_weight": 12}] * 200,
                                                 score_bounds=(-12, 12), clip_max=10)
            self.assertGreater(clipped["stats"]["lcb"], 0)
            self.assertFalse(clipped["promoted"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
