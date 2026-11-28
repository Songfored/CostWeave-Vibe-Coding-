import json
import unittest

from scripts.compare_invoice_evidence import ACTION_MS, ARMS, CASES, compare, make_job


class InvoicePolicyComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = compare()
        cls.rows = {case["case"]: case for case in cls.report["cases"]}

    def test_catalog_inputs_and_operational_acceptance_are_shared_and_oracle_is_withheld(self):
        for case in json.loads(CASES.read_text(encoding="utf-8")):
            shared = make_job(case, "lowest_cost").to_dict()
            for arm in ARMS:
                with self.subTest(case=case["id"], arm=arm):
                    job = make_job(case, arm).to_dict()
                    for key in ("inputs", "operations", "acceptance", "budget_units", "goals"):
                        self.assertEqual(shared[key], job[key])
                    changed_audit = dict(case, audited_cents=case["audited_cents"] + 1, reply=[1234], reply_ms=100)
                    self.assertEqual(job, make_job(changed_audit, arm).to_dict())

    def test_observed_agreement_disagreement_and_origin_change_the_next_action(self):
        for name, finding, attempts in (
            ("matching_statement", "agree", ["compare-statement", "post-summary"]),
            ("disputed_statement", "disagree", ["compare-statement", "open-archive", "sum-lines"]),
            ("same_ocr_source", "same_source", ["compare-statement", "open-archive", "sum-lines"]),
        ):
            with self.subTest(case=name):
                arms = self.rows[name]["arms"]
                self.assertEqual(finding, arms["evidence"]["finding"])
                self.assertEqual(attempts, arms["evidence"]["attempts"])
                self.assertTrue(arms["evidence"]["correct_delivery"])
                self.assertEqual(["open-archive", "sum-lines"], arms["fixed_original"]["attempts"])
                self.assertEqual(["post-summary"], arms["lowest_cost"]["attempts"])

    def test_wrong_agreement_is_visible_to_independent_audit(self):
        arms = self.rows["matching_but_wrong"]["arms"]
        self.assertTrue(arms["evidence"]["false_delivery"])
        self.assertFalse(arms["evidence"]["correct_delivery"])
        self.assertTrue(arms["fixed_original"]["correct_delivery"])

    def test_requests_can_remain_open_or_resume_and_miss_the_deadline(self):
        for arm in ("fixed_original", "evidence"):
            pending = self.rows["missing_unresolved"]["arms"][arm]
            self.assertTrue(pending["clarification_requested"])
            self.assertEqual("waiting_for_input", pending["status"])
            self.assertIsNone(pending["elapsed_ms"])
            self.assertIsNone(pending["issued_cents"])
            self.assertFalse(pending["on_time_correct"])
            resumed = self.rows["missing_replied_late"]["arms"][arm]
            self.assertTrue(resumed["clarification_requested"])
            self.assertTrue(resumed["correct_delivery"])
            self.assertFalse(resumed["on_time_correct"])
            self.assertEqual(90 + sum(ACTION_MS[op] for op in resumed["attempts"]), resumed["elapsed_ms"])

    def test_non_deliveries_count_towards_cost_and_policy_limits_hold(self):
        blocked = self.rows["budget_exhausted"]["arms"]["evidence"]
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual(1, blocked["spent_units"])
        for arm in ARMS:
            results = [case["arms"][arm] for case in self.rows.values()]
            for case in self.rows.values():
                self.assertLessEqual(case["arms"][arm]["spent_units"], case["budget_units"])
            summary = self.report["summary"][arm]
            on_time = sum(result["on_time_correct"] for result in results)
            spend = sum(result["spent_units"] for result in results)
            self.assertEqual(spend, summary["spent_units"])
            self.assertEqual(on_time, summary["on_time_correct"])
            self.assertEqual(spend / on_time, summary["spent_per_on_time_correct"])
