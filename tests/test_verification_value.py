import copy
import json
import unittest

from costweave.kernel.verification_value import (
    estimate_action, fit_verification_model, validate_verification_model,
)


def outcome(correct, cost, elapsed):
    return {"correct": correct, "cost_units": cost, "elapsed_ms": elapsed}


def example_rows():
    return [
        {"case_id": "private-1", "stratum": "conflict",
         "outcomes": {"direct": outcome(False, 1, 2), "verify": outcome(True, 3, 11)}},
        {"case_id": "private-2", "stratum": "conflict",
         "outcomes": {"direct": outcome(True, 2, 3), "verify": outcome(False, 4, 20)}},
        {"case_id": "private-3", "stratum": "conflict",
         "outcomes": {"direct": outcome(True, 2, 3), "verify": outcome(True, 3, 5)}},
        {"case_id": "private-4", "stratum": "conflict",
         "outcomes": {"direct": outcome(False, 3, 4), "verify": outcome(True, 4, 5)}},
    ]


def fit(rows, min_support=3):
    return fit_verification_model(rows, baseline="direct", actions=["verify", "direct"], min_support=min_support)


class VerificationValueTests(unittest.TestCase):
    def test_pairs_keep_harmed_cases_costs_and_late_outcomes(self):
        model = fit(example_rows())
        full = estimate_action(model, "conflict", "verify")
        self.assertEqual(("supported", 4, 2, 1, 1, 0.25, 0.75),
                         (full["status"], full["pairs"], full["corrected"], full["spoiled"],
                          full["ties"], full["net_gain"], full["quality"]))
        self.assertEqual((3.5, 10.25, 1.5, 7.25),
                         tuple(full[k] for k in ("mean_cost_units", "mean_elapsed_ms",
                                                 "mean_extra_cost_units", "mean_extra_elapsed_ms")))
        cut = estimate_action(model, "conflict", "verify", remaining_ms=5)
        self.assertEqual((1, 1, 0, .5), (cut["corrected"], cut["spoiled"], cut["net_gain"], cut["quality"]))
        self.assertEqual(0, estimate_action(model, "conflict", "direct")["gain_lower"])
        self.assertLessEqual(full["gain_lower"], full["net_gain"])
        self.assertLessEqual(full["quality_lower"], full["quality"])

    def test_unknown_sparse_and_partly_observed_arms_remain_unestimated(self):
        rows = example_rows()
        del rows[0]["outcomes"]["verify"]
        model = fit(rows)
        missing = estimate_action(model, "conflict", "verify")
        self.assertEqual(("insufficient_support", 3, 1, None),
                         (missing["status"], missing["pairs"], missing["missing_pairs"], missing["quality_lower"]))
        self.assertEqual("supported", estimate_action(model, "conflict", "direct")["status"])
        self.assertIsNone(estimate_action(fit(rows, min_support=5), "conflict", "direct")["net_gain"])
        self.assertEqual("unknown_stratum", estimate_action(model, "different", "direct")["status"])
        del rows[1]["outcomes"]["direct"]
        self.assertEqual(2, estimate_action(fit(rows), "conflict", "verify")["pairs"])

    def test_positive_lower_bound_requires_sample_support_and_never_exceeds_point(self):
        rows = [{"case_id": str(i), "stratum": "mismatch",
                 "outcomes": {"direct": outcome(False, 4, 7), "verify": outcome(True, 5, 8)}} for i in range(200)]
        small = estimate_action(fit(rows[:3]), "mismatch", "verify")
        large = estimate_action(fit(rows), "mismatch", "verify")
        self.assertGreater(large["quality_lower"], small["quality_lower"])
        self.assertGreater(large["gain_lower"], 0)
        late = estimate_action(fit(rows), "mismatch", "verify", remaining_ms=7)
        self.assertEqual((0, 0, 0), (late["quality"], late["net_gain"], late["quality_lower"]))

    def test_aggregate_is_deterministic_serializable_and_excludes_case_ids(self):
        original = example_rows()
        model = fit(original)
        self.assertEqual(model, fit(reversed(original)))
        encoded = json.dumps(model)
        self.assertNotIn("private-", encoded)
        loaded = json.loads(encoded)
        validate_verification_model(loaded)
        original[0]["outcomes"]["verify"]["elapsed_ms"] = 50
        self.assertEqual(model, loaded)

    def test_invalid_observations_models_and_duplicate_cases_are_rejected(self):
        rows = example_rows()
        with self.assertRaisesRegex(ValueError, "unique"):
            fit(rows + rows)
        for key, value in (("correct", 1), ("cost_units", True), ("elapsed_ms", -1)):
            bad = copy.deepcopy(rows)
            bad[0]["outcomes"]["direct"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                fit(bad)
        for bad in (True, -1, float("nan"), 1):
            with self.subTest(alpha=bad), self.assertRaises(ValueError):
                fit_verification_model(rows, baseline="direct", actions=["direct", "verify"], alpha=bad)
        model = fit(rows)
        cell = model["strata"]["conflict"]["routes"]["verify"]
        cell["times"][0][2] += 2
        with self.assertRaisesRegex(ValueError, "exceed"):
            validate_verification_model(model)
