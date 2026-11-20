import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from costweave.execution_recovery_service import CONFIRM_TOKEN, ExecutionRecoveryService
from costweave.execution_recovery_store import ExecutionRecoveryStore
from costweave.intelligence.execution_recovery import EconomicRecoveryPlanner, build_demo_snapshot
from costweave.intelligence.final_repair import repair_state_id, select_final_repair


def plan(key="retry@1", action="RETRY_NODE", targets=("draft",), price=.06):
    return {"plan_id": key, "action": action, "target_node_ids": list(targets),
            "max_cost_usd": price, "max_latency_ms": 600, "action_count": 1}


def request(*plans):
    plans = plans or (plan(),)
    return {"checkpoint_sha256": "a" * 64, "validator_revision": "hidden-suite-v1",
            "baseline_policy_id": "standard@1", "final_node_ids": ["final"], "remaining_actions": 1,
            "authorized_plan_ids": [p["plan_id"] for p in plans], "plans": list(plans), "pairs": []}


def observe(snapshot, req, key="retry@1", count=24, candidate_cost=.03, baseline_cost=.02, win=True):
    state = repair_state_id(snapshot, req)
    for i in range(count):
        uid = f"{key}-{i}"
        req["pairs"].append({
            "pair_id": uid,
            "baseline": {"state_id": state, "run_id": uid + "-b", "policy_id": req["baseline_policy_id"],
                         "final_passed": False, "cost_usd": baseline_cost},
            "candidate": {"state_id": state, "run_id": uid + "-c", "policy_id": key,
                          "final_passed": win, "cost_usd": candidate_cost},
        })


class FinalRepairTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_demo_snapshot()

    def test_missing_evidence_returns_free_diagnosis_but_legacy_remains_available(self):
        req = request()
        result = select_final_repair(self.snapshot, req)
        self.assertEqual("REQUEST_MISSING_INFO", result["action"])
        self.assertEqual(0, result["diagnosis"]["provider_calls"])
        self.assertEqual(["retry@1"], result["diagnosis"]["missing_paired_evidence_for"])
        self.assertIsNone(result["candidates"][0]["paired_final_pass_gain"])
        legacy, _ = EconomicRecoveryPlanner().decide(self.snapshot)
        self.assertNotIn("final_repair", legacy.to_dict())
        self.assertEqual("recovery_decision_9467c1ef6575aa3b0993b48be9fdcc94", legacy.computed_id)
        no_failure, _ = EconomicRecoveryPlanner().decide(replace(self.snapshot, failure_node_id=None))
        self.assertNotIn("final_repair", no_failure.to_dict())

    def test_missing_one_arm_checkpoint_and_duplicate_run_are_not_samples(self):
        req = request()
        observe(self.snapshot, req, count=1)
        req["pairs"][0]["candidate"]["state_id"] = "b" * 64
        result = select_final_repair(self.snapshot, req)
        self.assertEqual([], result["candidates"][0]["pair_ids"])
        self.assertEqual(["retry@1-0"], result["candidates"][0]["excluded_pair_ids"])
        req["pairs"].append({**req["pairs"][0], "pair_id": "duplicate-attempt"})
        with self.assertRaisesRegex(ValueError, "distinct run"):
            select_final_repair(self.snapshot, req)

    def test_multiple_required_blockers_need_joint_evidence_optional_group_is_irrelevant(self):
        template = next(n for n in self.snapshot.nodes if n.node_id == "draft")
        alternative = replace(template, node_id="secondary")
        optional = replace(template, node_id="optional", status="rejected")
        final = next(n for n in self.snapshot.nodes if n.node_id == "final")
        final = replace(final, dependencies=("polish", "secondary", "optional"),
                        dependency_types={"polish": "hard", "secondary": "validation", "optional": "soft"})
        snap = replace(self.snapshot, nodes=tuple(n for n in self.snapshot.nodes if n.node_id != "final") +
                       (alternative, optional, final))
        joint = plan("joint@1", "REPLAN_SUBGRAPH", ("draft", "secondary"))
        req = request(plan(), joint)
        observe(snap, req, "joint@1")
        observe(snap, req)
        result = select_final_repair(snap, req)
        self.assertEqual(["draft", "secondary"], result["blockers"])
        self.assertEqual("joint@1", result["selected_plan_id"])
        self.assertFalse(next(p for p in result["candidates"] if p["plan_id"] == "retry@1")["eligible"])

    def test_authorization_budget_cost_overruns_and_attempt_limits_dominate_evidence(self):
        for scenario in ("authorization", "budget", "time", "attempt", "observed_overrun"):
            with self.subTest(scenario=scenario):
                snap, req = self.snapshot, request()
                if scenario == "authorization":
                    req["authorized_plan_ids"] = []
                elif scenario == "budget":
                    snap = replace(snap, budget_remaining_usd=.01)
                elif scenario == "time":
                    snap = replace(snap, deadline_remaining_ms=50)
                elif scenario == "attempt":
                    snap = replace(snap, nodes=tuple(
                        replace(n, attempts=n.max_attempts) if n.node_id == "draft" else n for n in snap.nodes
                    ))
                observe(snap, req, candidate_cost=.09 if scenario == "observed_overrun" else .03)
                self.assertEqual("STOP_AND_RETURN_PARTIAL", select_final_repair(snap, req)["action"])

    def test_diagnostic_and_repair_are_one_versioned_plan_with_measured_tail_cost(self):
        diagnosis = {**plan("diagnose@2", "DIAGNOSE_THEN_RETRY"), "diagnostic_revision": "required-blockers-v1"}
        req = request(plan(), diagnosis)
        observe(self.snapshot, req, candidate_cost=.04)
        observe(self.snapshot, req, "diagnose@2", candidate_cost=.025)
        result = select_final_repair(self.snapshot, req)
        self.assertEqual("diagnose@2", result["selected_plan_id"])
        self.assertEqual("DIAGNOSE_THEN_RETRY", result["action"])
        selected = next(p for p in result["candidates"] if p["plan_id"] == result["selected_plan_id"])
        self.assertEqual("required-blockers-v1", selected["diagnostic_revision"])
        self.assertEqual(0, result["diagnosis"]["provider_calls"])
        self.assertAlmostEqual(.005, selected["mean_incremental_cost_usd"])
        self.assertEqual(24, len(selected["paired_runs"]))
        changed = {**req, "validator_revision": "different-hidden-suite"}
        self.assertIsNone(select_final_repair(self.snapshot, changed)["selected_plan_id"])

    def test_unknown_missing_and_protected_failures_cannot_authorize_a_paid_repair(self):
        for kind, action in ((None, "REQUEST_MISSING_INFO"), ("unknown", "REQUEST_MISSING_INFO"),
                             ("novel-schema", "REQUEST_MISSING_INFO"), ("hard_security", "STOP_AND_RETURN_PARTIAL"),
                             ("approval_required", "STOP_AND_RETURN_PARTIAL"), ("budget", "STOP_AND_RETURN_PARTIAL")):
            with self.subTest(kind=kind):
                snap = replace(self.snapshot, failure_class=kind, nodes=tuple(
                    replace(n, failure_class=kind) if n.node_id == "draft" else n for n in self.snapshot.nodes
                ))
                req = request()
                observe(snap, req)
                result = select_final_repair(snap, req)
                self.assertEqual(action, result["action"])
                self.assertFalse(result["candidates"][0]["eligible"])
                self.assertIsNone(result["selected_plan_id"])

    def test_a_diagnostic_label_cannot_hide_a_second_model_call(self):
        req = request({**plan("invented@1", "DIAGNOSE_THEN_RETRY"), "diagnostic_revision": "ask-another-model"})
        observe(self.snapshot, req, "invented@1")
        result = select_final_repair(self.snapshot, req)
        self.assertEqual("STOP_AND_RETURN_PARTIAL", result["action"])
        self.assertIn("unregistered_local_diagnostic", result["candidates"][0]["reason_codes"])

    def test_no_required_failure_continues_and_one_favourable_pair_cannot_force_a_repair(self):
        req = request()
        observe(self.snapshot, req, count=1)
        self.assertEqual("REQUEST_MISSING_INFO", select_final_repair(self.snapshot, req)["action"])
        snap = replace(self.snapshot, nodes=tuple(
            replace(n, status="validated") if n.node_id == "draft" else n for n in self.snapshot.nodes
        ))
        self.assertEqual("CONTINUE", select_final_repair(snap, req)["action"])

    def test_normal_service_persists_and_replays_the_measured_decision(self):
        req = request()
        observe(self.snapshot, req)
        payload = {"confirm": CONFIRM_TOKEN, "snapshot": {**self.snapshot.to_dict(), "repair_allocation": req}}
        with tempfile.TemporaryDirectory() as temp:
            service = ExecutionRecoveryService(ExecutionRecoveryStore(Path(temp) / "runtime.sqlite3"))
            kwargs = dict(project_id=self.snapshot.project_id, idempotency_key="paired-repair-test-01", payload=payload)
            result, replayed = service.decide(**kwargs)
            self.assertFalse(replayed)
            decision = result["decision"]
            self.assertEqual("RETRY_NODE", decision["action"])
            self.assertEqual("retry@1", decision["final_repair"]["selected_plan_id"])
            self.assertEqual([], decision["options"])
            self.assertIsNone(decision["decision_confidence"])
            again, replayed = service.decide(**kwargs)
            self.assertTrue(replayed)
            self.assertEqual(decision, again["decision"])
            self.assertEqual(decision, service.get(project_id=kwargs["project_id"], decision_id=decision["decision_id"])["decision"])
