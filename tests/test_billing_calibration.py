from __future__ import annotations

import unittest

from costweave.billing_calibration import (
    BillingCalibrationError,
    calibration_receipt,
    estimate_billed_cost_cny,
    load_billing_calibration,
    reconcile_call_invoice,
    reconcile_provider_invoice,
)


class BillingCalibrationTests(unittest.TestCase):
    def test_relay_gpt_matches_observed_thirty_percent_invoice(self):
        result = estimate_billed_cost_cny(
            "traveltoken-gpt", reference_cost_usd=1.328
        )
        self.assertEqual(0.3984, result.amount_cny)
        self.assertEqual("CNY", result.as_dict()["currency"])

    def test_relay_claude_matches_observed_quarter_invoice(self):
        result = estimate_billed_cost_cny(
            "traveltoken-claude", reference_cost_usd=3.2266
        )
        self.assertEqual(0.80665, result.amount_cny)
        self.assertLessEqual(0.8067, result.upper_cny)

    def test_deepseek_uses_off_peak_and_cache_hit_rates(self):
        result = estimate_billed_cost_cny(
            "deepseek-official",
            reference_cost_usd=999,
            input_tokens=1_000_000,
            cached_input_tokens=100_000,
            output_tokens=100_000,
            occurred_at="2026-09-02T05:00:00Z",
        )
        expected_usd = 0.9 * 0.66 + 0.1 * 0.022 + 0.1 * 1.98
        self.assertAlmostEqual(expected_usd * 6.759094, result.amount_cny, places=8)
        self.assertEqual("off_peak", result.time_band)
        self.assertAlmostEqual(
            0.1 * 0.022 * 6.759094,
            result.estimated_cache_read_cost_cny,
            places=8,
        )

    def test_deepseek_uses_peak_weekday_window(self):
        result = estimate_billed_cost_cny(
            "deepseek-official",
            reference_cost_usd=0,
            input_tokens=1_000_000,
            occurred_at="2026-09-02T06:30:00Z",
        )
        self.assertAlmostEqual(1.32 * 6.759094, result.amount_cny, places=8)
        self.assertEqual("peak", result.time_band)

    def test_invalid_tokens_fail_closed(self):
        with self.assertRaises(BillingCalibrationError):
            estimate_billed_cost_cny(
                "deepseek-official",
                reference_cost_usd=0,
                input_tokens=1,
                cached_input_tokens=2,
            )

    def test_receipt_omits_account_balances_and_personal_data(self):
        document = load_billing_calibration()
        receipt = calibration_receipt(document)
        self.assertEqual("CNY", receipt["account_currency"])
        self.assertNotIn("evidence", str(receipt))

    def test_provider_invoice_overrides_estimate_and_preserves_residual(self):
        result = reconcile_provider_invoice(
            "traveltoken-gpt",
            estimated_cny=0.2917935,
            provider_observed_cny=0.3984,
        )
        self.assertEqual(0.3984, result.final_billed_cny)
        self.assertAlmostEqual(0.1066065, result.residual_cny)
        self.assertEqual("provider_invoice", result.final_source)

    def test_per_call_reconciliation_preserves_cache_evidence(self):
        estimate = estimate_billed_cost_cny(
            "traveltoken-claude",
            reference_cost_usd=0.01,
            input_tokens=1300,
            output_tokens=320,
            cached_input_tokens=1297,
            cache_read_input_tokens=1297,
            cache_creation_input_tokens=64,
        )
        self.assertEqual(1297, estimate.cache_read_input_tokens)
        self.assertEqual(64, estimate.cache_creation_input_tokens)
        self.assertIsNone(estimate.estimated_cache_read_cost_cny)
        self.assertIsNone(estimate.estimated_cache_creation_cost_cny)
        reconciled = reconcile_call_invoice(
            external_call_id="external_call_fixture_1",
            attempt_ordinal=1,
            provider_id="traveltoken-claude",
            estimated_cny=estimate.amount_cny,
            provider_billed_cny=0.00242,
            cache_read_input_tokens=1297,
            cache_creation_input_tokens=64,
            observed_at="2026-09-02T18:13:50+08:00",
            evidence_sha256="a" * 64,
        )
        self.assertTrue(reconciled.as_dict()["invoice_reconciled"])
        self.assertEqual(1297, reconciled.cache_read_input_tokens)
        self.assertAlmostEqual(
            0.00242 - estimate.amount_cny, reconciled.residual_cny
        )


if __name__ == "__main__":
    unittest.main()
