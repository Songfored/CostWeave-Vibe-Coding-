from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from costweave.deepseek_interface_probe import (
    DeepSeekInterfaceProbeError,
    _validate,
    _work_order,
    run_probe,
)


class DeepSeekInterfaceProbeTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("COSTWEAVE_EXPERIMENTAL_ADAPTERS", None)
        os.environ.pop("COSTWEAVE_PAID_CALLS", None)

    def test_exact_validation(self):
        self.assertTrue(
            _validate('{"answer":402,"checksum":"ds-json-v1"}')["accepted"]
        )
        self.assertFalse(
            _validate('{"answer":402,"checksum":"ds-json-v1","extra":true}')[
                "accepted"
            ]
        )
        self.assertFalse(_validate("not json")["accepted"])

    def test_work_order_is_synthetic_tool_free_and_bounded(self):
        order = _work_order(0.02)
        self.assertEqual((), order.allowed_tools)
        self.assertEqual(0.02, order.cost_limit)
        self.assertIn("Synthetic data only", order.assumptions[0])

    def test_probe_rejects_without_paid_switches_before_registry_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(DeepSeekInterfaceProbeError):
                run_probe(root=Path(temporary), budget_usd=0.02)

    def test_probe_rejects_budget_above_two_cents(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(DeepSeekInterfaceProbeError):
                run_probe(root=Path(temporary), budget_usd=0.020001)


if __name__ == "__main__":
    unittest.main()
