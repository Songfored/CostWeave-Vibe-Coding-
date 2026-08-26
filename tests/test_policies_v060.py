"""Tests for result-blind v0.6.0 tournament policies."""

from __future__ import annotations

import hashlib
import unittest

from costweave.benchmarks.contracts import CaseViewV1
from costweave.benchmarks.policies import (
    DeterministicRandomPolicy,
    FixedModelPolicy,
    PolicyDecisionV1,
    RecordedDecisionPolicy,
    StaticTaskFamilyPolicy,
    TournamentPolicy,
)


def _case(case_id: str = "case-1", family: str = "code", models: tuple[str, ...] = ("model-a", "model-b")) -> CaseViewV1:
    return CaseViewV1(
        case_id=case_id,
        task_family=family,
        prompt_hash=hashlib.sha256(case_id.encode("utf-8")).hexdigest(),
        model_ids=models,
    )


class PolicyContractsV060Tests(unittest.TestCase):
    def test_decision_abstain_invariant_and_round_trip_dict(self):
        decision = PolicyDecisionV1("case-1", "policy-1", None, True, ("missing",))
        self.assertEqual(
            {"case_id": "case-1", "policy_id": "policy-1", "selected_model_id": None, "abstained": True, "reason_codes": ["missing"]},
            decision.to_dict(),
        )
        with self.assertRaises(ValueError):
            PolicyDecisionV1("case-1", "policy-1", "model-a", True, ("bad",))
        with self.assertRaises(ValueError):
            PolicyDecisionV1("case-1", "policy-1", None, False, ("bad",))
        with self.assertRaises(ValueError):
            PolicyDecisionV1("case-1", "policy-1", None, True, ())

    def test_policies_satisfy_protocol_without_receiving_actual_records(self):
        for policy in (
            FixedModelPolicy("model-a"),
            DeterministicRandomPolicy(1),
            StaticTaskFamilyPolicy({"code": "model-a"}),
            RecordedDecisionPolicy({"case-1": "model-a"}),
        ):
            self.assertIsInstance(policy, TournamentPolicy)
            result = policy.decide(_case())
            self.assertIsInstance(result, PolicyDecisionV1)

    def test_fixed_model_selects_or_abstains(self):
        selected = FixedModelPolicy("model-a").decide(_case())
        self.assertEqual("model-a", selected.selected_model_id)
        self.assertFalse(selected.abstained)
        abstained = FixedModelPolicy("not-present").decide(_case())
        self.assertTrue(abstained.abstained)
        self.assertIsNone(abstained.selected_model_id)

    def test_deterministic_random_is_stable_and_seed_sensitive(self):
        case = _case("case-stable", models=("model-a", "model-b", "model-c"))
        first = DeterministicRandomPolicy(7).decide(case)
        second = DeterministicRandomPolicy(7).decide(case)
        self.assertEqual(first, second)
        self.assertIn(first.selected_model_id, case.model_ids)
        self.assertIn(DeterministicRandomPolicy(8).decide(case).selected_model_id, case.model_ids)
        with self.assertRaises(ValueError):
            DeterministicRandomPolicy(2**31)
        with self.assertRaises(TypeError):
            DeterministicRandomPolicy(True)

    def test_deterministic_random_uses_case_id_not_result_values(self):
        first = DeterministicRandomPolicy(3).decide(_case("case-a"))
        second = DeterministicRandomPolicy(3).decide(_case("case-b"))
        self.assertNotEqual(first.case_id, second.case_id)
        self.assertIn(first.selected_model_id, ("model-a", "model-b"))
        self.assertIn(second.selected_model_id, ("model-a", "model-b"))

    def test_static_family_routes_only_registered_families(self):
        policy = StaticTaskFamilyPolicy({"code": "model-a", "research": "model-b"})
        self.assertEqual("model-a", policy.decide(_case()).selected_model_id)
        self.assertEqual("model-b", policy.decide(_case(family="research")).selected_model_id)
        unknown = policy.decide(_case(family="writing"))
        self.assertTrue(unknown.abstained)
        unavailable = policy.decide(_case(models=("model-z", "model-y")))
        self.assertTrue(unavailable.abstained)

    def test_recorded_policy_abstains_for_missing_or_unavailable(self):
        policy = RecordedDecisionPolicy({"case-1": "model-a", "case-2": None})
        self.assertEqual("model-a", policy.decide(_case("case-1")).selected_model_id)
        self.assertTrue(policy.decide(_case("case-2")).abstained)
        self.assertTrue(policy.decide(_case("case-3")).abstained)
        self.assertTrue(policy.decide(_case("case-1", models=("model-x", "model-y"))).abstained)

    def test_policy_ids_and_mappings_are_validated(self):
        with self.assertRaises(ValueError):
            FixedModelPolicy("model-a", policy_id=" ")
        with self.assertRaises(ValueError):
            StaticTaskFamilyPolicy({})
        with self.assertRaises(TypeError):
            StaticTaskFamilyPolicy([])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            RecordedDecisionPolicy({" ": "model-a"})


if __name__ == "__main__":
    unittest.main()
