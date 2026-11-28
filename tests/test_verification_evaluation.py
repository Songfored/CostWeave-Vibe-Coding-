import copy
import unittest

from scripts.evaluate_verification_value import freeze, partition, score


def case(i, split):
    return {"id": f"{split}-{i}", "group": f"hash-{split}-{i}", "split": split, "stratum": "normal",
            "truth": 42, "budget_units": 4, "deadline_ms": 60, "actions": {
                "direct": {"answer": 42, "cost_units": 1, "elapsed_ms": 5},
                "check": {"answer": None, "cost_units": 2, "elapsed_ms": 8},
                "original": {"answer": 42, "cost_units": 4, "elapsed_ms": 70}}}


class VerificationReplayTests(unittest.TestCase):
    def setUp(self):
        self.protocol = {"min_support": 1, "alpha": 0.05, "max_error_rate": 0.5, "max_false_delivery_rate": 0.1,
                         "min_on_time_correct_rate": 0.7, "quotes": {"direct": 1, "check": 2, "original": 4},
                         "route_ms": {"direct": 5, "check": 8, "original": 50}, "fallback_operation": "original"}
        self.cal = [case(i, "calibration") for i in range(25)]
        self.hold = [case(i, "holdout") for i in range(2)]

    def test_frozen_selection_is_blind_to_holdout_and_accounting_includes_abstentions(self):
        frozen = freeze(partition(self.cal, calibration_only=True)["calibration"], self.protocol)
        self.assertEqual("direct", frozen["fixed_comparator"])
        self.hold[1]["truth"] = 84
        splits = partition(self.cal + self.hold)
        report = score(splits, self.protocol, frozen)
        self.assertEqual(1, report["holdout"]["direct"]["false_delivery"])
        self.assertEqual((2, 4), (report["holdout"]["check"]["abstained"], report["holdout"]["check"]["spent_units"]))
        self.assertEqual((2, 8), (report["holdout"]["original"]["abstained"], report["holdout"]["original"]["spent_units"]))
        for row in report["runs"]:
            self.assertEqual(["direct"], row["arms"]["adaptive"]["route"])
        self.assertNotIn("advantage", report["verdict"])

    def test_overlap_bad_quotes_and_modified_freeze_are_rejected(self):
        bad = copy.deepcopy(self.hold[0])
        bad["group"] = self.cal[0]["group"]
        with self.assertRaisesRegex(ValueError, "overlapping"):
            partition(self.cal + [bad])
        splits = partition(self.cal + self.hold)
        frozen = freeze(splits["calibration"], self.protocol)
        changed = {**frozen, "fixed_comparator": "check"}
        with self.assertRaisesRegex(ValueError, "changed"):
            score(splits, self.protocol, changed)
        splits["holdout"][0]["actions"]["direct"]["cost_units"] = 3
        with self.assertRaisesRegex(ValueError, "quote"):
            score(splits, self.protocol, frozen)
