import copy
import unittest

from scripts.compare_receipt_readers import compare
from scripts.evaluate_verification_value import ROUTES, freeze, partition


PROTOCOL = {
    "min_on_time_correct_rate": 0.5, "max_false_delivery_rate": 0.5,
    "max_error_rate": 0.5, "alpha": 0.05, "min_support": 4, "fallback_operation": "original",
    "route_ms": {"direct": 3, "check": 8, "original": 15}, "quotes": {"direct": 1, "check": 2, "original": 4},
}


def receipt(split, index, answers, *, budget=4, late=None):
    actions = {name: {"answer": answer, "cost_units": 1, "elapsed_ms": 100 if name == late else PROTOCOL["route_ms"][name]}
               for name, answer in zip(ROUTES, answers, strict=True)}
    return {"id": f"{split}-{index}", "group": f"image-{split}-{index}", "split": split, "stratum": "normal",
            "truth": 10, "budget_units": budget, "deadline_ms": 80, "actions": actions}


class PairedReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old_cal = [receipt("calibration", i, (11, 10, None)) for i in range(25)]
        new_cal = [receipt("calibration", i, (10, None, 11)) for i in range(25)]
        old_frozen, new_frozen = [freeze(partition(cal, calibration_only=True)["calibration"], PROTOCOL)
                                  for cal in (old_cal, new_cal)]
        old_hold = [receipt("holdout", 0, (11, 10, None)), receipt("holdout", 1, (10, 11, 11)),
                    receipt("holdout", 2, (None, 11, 11)), receipt("holdout", 3, (10, 10, 10), budget=0)]
        new_hold = [receipt("holdout", 0, (10, 10, None), late="check"), receipt("holdout", 1, (11, None, 10)),
                    receipt("holdout", 2, (10, 10, 11), late="direct"), receipt("holdout", 3, (10, 10, 10), budget=0)]
        cls.inputs = {"old_calibration": old_cal, "new_calibration": new_cal, "old_holdout": old_hold,
                      "new_holdout": list(reversed(new_hold)), "old_freeze": old_frozen, "new_freeze": new_frozen,
                      "protocol": PROTOCOL}

    def test_extraction_and_actual_timely_delivery_have_distinct_paired_changes(self):
        report = compare(**self.inputs)
        self.assertEqual((2, 3), tuple(report[k]["admissible_oracle_on_time_correct"] for k in ("old", "new")))
        self.assertEqual((3, 4), tuple(report[k]["raw_oracle_correct"] for k in ("old", "new")))
        self.assertEqual(("check", "direct"), tuple(report[k]["calibration_selected_fixed"] for k in ("old", "new")))
        new_direct = report["new"]["raw_extraction"]["direct"]
        self.assertEqual((3, .75, 1), (new_direct["correct"], new_direct["accuracy_per_task"], new_direct["coverage_rate"]))
        price = report["new"]["holdout_arms"]["original"]
        self.assertEqual((3, 12), (price["spent_units"], price["total_quoted_units"]))
        raw = report["paired_raw_extraction"]["direct"]
        dispatched = report["paired_runtime_delivery"]["direct"]
        self.assertEqual((2, 1), (raw["correct_gained"], raw["correct_lost"]))
        self.assertEqual((1, 1, 1, 1), tuple(dispatched[k] for k in
                                          ("correct_gained", "correct_lost", "wrong_avoided", "wrong_introduced")))
        self.assertIn("adaptive", report["paired_runtime_delivery"])
        self.assertIn("calibration_selected_fixed", report["paired_runtime_delivery"])

    def test_protocol_freeze_and_cohort_mismatches_rejected(self):
        for key, value in (("truth", 500), ("budget_units", 7), ("group", "other-image"),
                           ("id", "different-document"), ("deadline_ms", 3), ("stratum", "other")):
            arguments = copy.deepcopy(self.inputs)
            arguments["new_holdout"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "do not match"):
                compare(**arguments)
        with self.assertRaisesRegex(ValueError, "changed"):
            compare(**{**self.inputs, "new_freeze": self.inputs["old_freeze"]})
        with self.assertRaisesRegex(ValueError, "changed"):
            compare(**{**self.inputs, "protocol": {**PROTOCOL, "alpha": 0.01}})
        swapped = copy.deepcopy(self.inputs)
        swapped["old_holdout"][0]["group"] = swapped["old_calibration"][0]["group"]
        with self.assertRaisesRegex(ValueError, "overlapping"):
            compare(**swapped)

    def test_compares_excluded_cohorts_and_rejects_unpaired_scoreable_cases(self):
        arguments = dict(self.inputs)
        arguments["old_holdout"] = {"records": arguments["old_holdout"], "metadata": {"excluded": [{"id": "skipped"}]}}
        with self.assertRaisesRegex(ValueError, "excluded"):
            compare(**arguments)
        arguments["new_holdout"] = {"records": arguments["new_holdout"],
                                    "metadata": {"excluded": [{"id": "skipped", "reason": "duplicate"}]}}
        with self.assertRaisesRegex(ValueError, "excluded"):
            compare(**arguments)
        arguments["new_holdout"] = self.inputs["new_holdout"]
        arguments["new_holdout"] = arguments["new_holdout"][1:]
        arguments["old_holdout"] = self.inputs["old_holdout"]
        with self.assertRaisesRegex(ValueError, "do not match"):
            compare(**arguments)
