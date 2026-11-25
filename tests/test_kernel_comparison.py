import unittest

from scripts.compare_artifact_runtime import compare


class RuntimeComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = compare()
        cls.rows = {case["case"]: case for case in cls.report["cases"]}

    def test_fixed_route_search_reconsiders_affordable_fallback(self):
        old = self.rows["inline_fallback_fits"]["direct"]
        self.assertEqual(["SELECT", "SELECT"], old["search_decisions"])
        self.assertEqual(["read-summary", "calculate-inline"], old["attempts"])
        self.assertTrue(old["final_passed"])

    def test_switching_topology_passes_the_same_oracle_and_charges_the_first_failure(self):
        stale = self.rows["stale_summary"]
        self.assertFalse(stale["direct"]["final_passed"])
        self.assertTrue(stale["decomposed"]["final_passed"])
        self.assertTrue(stale["kernel"]["final_passed"])
        self.assertEqual(["read-summary", "normalize", "count", "sum", "assemble"], stale["kernel"]["attempts"])
        self.assertEqual(6, stale["kernel"]["spent_units"])
        self.assertEqual(5, stale["decomposed"]["spent_units"])
        valid = self.rows["valid_summary"]
        self.assertEqual(["read-summary"], valid["kernel"]["attempts"])

    def test_reuse_and_budget_are_consistent_across_arms(self):
        for arm in ("direct", "decomposed", "kernel", "preserve_fallback"):
            self.assertEqual([], self.rows["accepted_goal"][arm]["attempts"])
            self.assertFalse(self.rows["insufficient_budget"][arm]["final_passed"])
            for case in self.rows.values():
                self.assertLessEqual(case[arm]["spent_units"], case["budget_units"])
        for arm in ("decomposed", "kernel", "preserve_fallback"):
            self.assertNotIn("normalize", self.rows["accepted_shared_input"][arm]["attempts"])
        self.assertEqual("over_budget", self.rows["insufficient_budget"]["kernel"]["reason"])
        original = self.report["original_six_summary"]
        self.assertEqual({"final_passes": 5, "spent_units": 18}, original["kernel"])
        self.assertEqual(original["decomposed"], original["kernel"])

    def test_guard_trades_cost_for_completion_only_when_the_fallback_budget_is_at_risk(self):
        # The policy must make the same first decision for fresh and stale
        # summaries; only the independent acceptance check sees their quality.
        for name, baseline, guarded in (
            ("valid_summary_budget_5", (True, 1), (True, 5)),
            ("stale_summary_budget_5", (False, 1), (True, 5)),
            ("valid_summary", (True, 1), (True, 1)),
            ("stale_summary", (True, 6), (True, 6)),
        ):
            with self.subTest(case=name):
                row = self.rows[name]
                for arm, expected in (("kernel", baseline), ("preserve_fallback", guarded)):
                    result = row[arm]
                    self.assertEqual(expected, (result["final_passed"], result["spent_units"]))
                first = "normalize" if row["budget_units"] == 5 else "read-summary"
                self.assertEqual(first, row["preserve_fallback"]["attempts"][0])
                decision = "take_fallback" if row["budget_units"] == 5 else "fallback_still_fits"
                self.assertEqual({"speculative_operation": "read-summary", "remaining_if_rejected": row["budget_units"] - 1,
                                  "fallback_cost_units": 5, "decision": decision},
                                 row["preserve_fallback"]["first_fallback_assessment"])
                self.assertNotEqual(row["job_sha256"], row["preserve_fallback"]["job_sha256"])
        self.assertEqual({"final_passes": 6, "spent_units": 20}, self.report["summary"]["kernel"])
        self.assertEqual({"final_passes": 7, "spent_units": 28}, self.report["summary"]["preserve_fallback"])
