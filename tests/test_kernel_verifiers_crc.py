from __future__ import annotations

import math
import os
import random
import unittest

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import default_registry
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore
from costweave.kernel.verifiers import CRCCalibrator, CalibrationSample, agreement_score


class KernelVerifiersCRCTests(unittest.TestCase):
    def test_v0_json_schema_strict_and_invariants(self) -> None:
        reg = default_registry()
        op_schema = Operation("s", (), "doc", "literal", "json_schema_strict", 1, {
            "value": {"items": [{"amount": 4}, {"amount": 6}], "total": 10, "quotes": ["exact phrase"]},
            "schema": {
                "type": "object",
                "required": ["items", "total", "quotes"],
                "additional_properties": False,
                "properties": {
                    "items": {"type": "array", "min_items": 1},
                    "total": {"type": "integer", "minimum": 1},
                    "quotes": {"type": "array", "items": {"type": "string"}},
                },
            },
        })
        self.assertTrue(reg.validate(op_schema, op_schema.config["value"], {}))
        self.assertFalse(reg.validate(op_schema, {"items": [], "total": 0, "quotes": [], "extra": 1}, {}))

        op_inv = Operation("inv", ["source"], "out", "literal", "invariants", 1, {
            "sum_equals": {"items_field": "items", "item_key": "amount", "total_field": "total"},
            "exact_substring_grounding": {"quotes_field": "quotes", "source_artifact": "source"},
            "non_empty_fields": ["quotes"],
        })
        src = {"source": "The report states exact phrase in section 2."}
        self.assertTrue(reg.validate(op_inv, op_schema.config["value"], src))
        # Fails when quote is hallucinated
        self.assertFalse(reg.validate(op_inv, {"items": [{"amount": 10}], "total": 10, "quotes": ["hallucinated"]}, src))
        # Fails when sum does not conserve
        self.assertFalse(reg.validate(op_inv, {"items": [{"amount": 4}], "total": 10, "quotes": ["exact phrase"]}, src))

    def test_v0_python_check_sandbox_timeout_and_zero_secret_leak(self) -> None:
        reg = default_registry()
        os.environ["COSTWEAVE_SANDBOX_SECRET_SENTINEL"] = "top-secret-value-xyz"
        try:
            op_ok = Operation("py1", ["x"], "y", "literal", "python_check", 1, {
                "value": {"ans": 42},
                "code": "import os\nassert 'COSTWEAVE_SANDBOX_SECRET_SENTINEL' not in os.environ\nassert value['ans'] == inputs['x'] * 2",
            })
            self.assertTrue(reg.validate(op_ok, {"ans": 42}, {"x": 21}))
            self.assertFalse(reg.validate(op_ok, {"ans": 99}, {"x": 21}))

            op_timeout = Operation("py2", [], "y", "literal", "python_check", 1, {
                "code": "while True:\n    pass",
                "timeout_seconds": 0.2,
            })
            self.assertFalse(reg.validate(op_timeout, 1, {}))
        finally:
            os.environ.pop("COSTWEAVE_SANDBOX_SECRET_SENTINEL", None)

    def test_v1_orthogonal_consensus_rejects_same_family_and_routes_disagreement(self) -> None:
        reg = default_registry()
        bad_job = Job(
            ["answer"], {"q": 1}, 10,
            [
                Operation("a", ["q"], "left", "literal", "present", 1, {"value": 42}, provider_family="openai"),
                Operation("b", ["q"], "right", "literal", "present", 1, {"value": 42}, provider_family="openai"),
                Operation("merge", ["left", "right"], "answer", "consensus_pick", "orthogonal_consensus", 0, {
                    "left_artifact": "left", "right_artifact": "right",
                    "left_family": "openai", "right_family": "openai",
                }),
            ],
            acceptance={"answer": {"validator": "present", "config": {}}},
        )
        with self.assertRaisesRegex(ValueError, "distinct provider families"):
            reg.check(bad_job)

        # Disagreeing dual cheap consensus triggers escalation to frontier fallback
        job = Job(
            ["answer"], {"q": 1}, 25,
            [
                Operation("left_ds", ["q"], "left", "literal", "present", 1, {"value": {"res": 4}},
                          provider_family="deepseek", pass_prob=0.9),
                Operation("right_gm", ["q"], "right", "literal", "present", 1, {"value": {"res": 5}},
                          provider_family="google", pass_prob=0.9),
                Operation("merge", ["left", "right"], "answer", "consensus_pick", "orthogonal_consensus", 0, {
                    "left_artifact": "left", "right_artifact": "right",
                    "left_family": "deepseek", "right_family": "google", "min_threshold": 1.0,
                }, pass_prob=0.8),
                Operation("frontier", ["q"], "answer", "literal", "present", 20, {"value": {"res": 4}},
                          provider_family="anthropic", pass_prob=0.99),
            ],
            acceptance={"answer": {"validator": "present", "config": {}}},
            planning_policy="bsao",
            failure_penalty_units=80,
        )
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        state = Runtime(store, reg).submit("ortho-escalate", job)
        self.assertEqual("complete", state["status"])
        self.assertEqual({"res": 4}, state["artifacts"]["answer"])
        self.assertEqual(22, state["spent_units"])  # 1 + 1 + 0 (rejected) + 20
        self.assertAlmostEqual(1.0, agreement_score({"a": 100.0}, {"a": 100.00001}, mode="numeric_rel_tol", rel_tol=1e-4))

    def test_crc_calibrator_cold_start_and_monte_carlo_risk_bound(self) -> None:
        cal = CRCCalibrator()
        cold = cal.calibrate([], alpha=0.05, delta=0.05)
        self.assertFalse(cold.admissible)
        self.assertEqual(math.inf, cold.threshold)
        self.assertEqual(1.0, cold.risk_ucb)

        # Small n=25 even with 0 errors has UCB = 1 - 0.05^(1/25) ~ 0.113 > 0.05 -> fail-closed
        small = cal.calibrate([CalibrationSample(0.95, 0.0) for _ in range(25)], alpha=0.05, delta=0.05)
        self.assertFalse(small.admissible)

        # Monte Carlo verification: n=80, alpha=0.05, delta=0.05 over 400 independent splits
        rng = random.Random(4242)
        violations = 0
        trials = 400
        alpha = 0.05
        for _ in range(trials):
            def draw():
                s = rng.random()
                # Higher score has lower probability of being corrupted
                err_p = 0.01 if s >= 0.8 else 0.25
                return CalibrationSample(s, 1.0 if rng.random() < err_p else 0.0)

            cal_set = [draw() for _ in range(80)]
            res = cal.calibrate(cal_set, alpha=alpha, delta=0.05)
            if not res.admissible:
                continue
            # Check true conditional test risk over large holdout sample
            # True marginal risk P(loss=1 and score >= tau):
            true_risk = (0.20 * 0.01) if res.threshold >= 0.8 else (0.20 * 0.01 + max(0.0, 0.8 - res.threshold) * 0.25)
            if true_risk > alpha + 1e-12:
                violations += 1
        self.assertLessEqual(violations / trials, 0.05)


if __name__ == "__main__":
    unittest.main()
