import unittest

from scripts.evaluate_unified_receipts import fit_routes, paired_interval, replay_bsao


class UnifiedReceiptExperimentTests(unittest.TestCase):
    def test_observable_rejection_and_invisible_misread_are_distinct(self):
        price = {"direct": 1, "check": 5, "original": 8}
        terms = {"max_risk_alpha": 1, "failure_penalty_units": 100, "corruption_penalty_units": 0}
        protocol = {"quotes": price, "route_ms": {key: 1 for key in price}}
        params = {"*": {r: {"pass_prob": .8 if r == "direct" else 1., "false_pass_risk": 0.} for r in price}}
        observations = {r: {"answer": 100, "cost_units": p, "elapsed_ms": 1} for r, p in price.items()}
        case = {"budget_units": 6, "deadline_ms": 100, "stratum": "any", "actions": observations, "truth": 100}
        observations["direct"]["answer"] = 90
        wrong = replay_bsao(case, params, protocol, terms)
        self.assertEqual((["direct"], True, 1), (wrong["route"], wrong["false_delivery"], wrong["spent_units"]))
        observations["direct"]["answer"] = None
        recovered = replay_bsao(case, params, protocol, terms)
        self.assertEqual((["direct", "check"], True, 6),
                         (recovered["route"], recovered["on_time_correct"], recovered["spent_units"]))

    def test_no_timely_correct_cost_is_undefined_and_small_groups_use_pool(self):
        runs = [{"spent_units": 1, "on_time_correct": False}]
        self.assertEqual({"defined_draws": 0, "draws": 8, "ci95": None}, paired_interval(runs, runs, 1, 8))
        row = {"stratum": "rare", "truth": 7, "actions": {r: {"answer": v} for r, v in
                                                          (("direct", 6), ("check", None), ("original", 7))}}
        model = fit_routes([row], 12)
        self.assertEqual({"*"}, set(model))
        self.assertEqual(2 / 3, model["*"]["direct"]["false_pass_risk"])
        self.assertEqual(1 / 3, model["*"]["check"]["pass_prob"])
