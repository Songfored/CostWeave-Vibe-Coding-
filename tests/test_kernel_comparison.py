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
        for arm in ("direct", "decomposed", "kernel"):
            self.assertEqual([], self.rows["accepted_goal"][arm]["attempts"])
            self.assertFalse(self.rows["insufficient_budget"][arm]["final_passed"])
            for case in self.rows.values():
                self.assertLessEqual(case[arm]["spent_units"], case["budget_units"])
        for arm in ("decomposed", "kernel"):
            self.assertNotIn("normalize", self.rows["accepted_shared_input"][arm]["attempts"])
        self.assertEqual("over_budget", self.rows["insufficient_budget"]["kernel"]["reason"])
        self.assertEqual({"final_passes": 5, "spent_units": 18}, self.report["summary"]["kernel"])
        self.assertEqual(self.report["summary"]["decomposed"], self.report["summary"]["kernel"])
