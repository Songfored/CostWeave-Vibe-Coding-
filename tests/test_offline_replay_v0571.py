from __future__ import annotations

import unittest
from dataclasses import replace
import sqlite3
import tempfile

from costweave.event_store import EventStore
from costweave.offline_replay import (
    ApproximateOracleReport,
    ObservedRouteOutcome,
    OfflineReplayBenchmark,
    OfflineReplayEngine,
    OfflineReplayError,
    ReplayBenchmarkReport,
    ReplayCase,
    ReplayPolicySpec,
    ReplayRouteCandidate,
    ReplayVersionSnapshot,
    build_approximate_oracle,
    public_offline_replay_policy,
)
from costweave.offline_replay_store import OfflineReplayStore


def snapshot(*, evidence: str = "E0-fixture") -> ReplayVersionSnapshot:
    return ReplayVersionSnapshot.create(
        policy_id="legacy-resource-optimizer",
        policy_version="v1",
        feature_schema_version="outcome-features-v1",
        task_contract_schema_version="v4.1",
        catalog_snapshot_hash="a" * 64,
        prompt_revision="prompt-v1",
        tool_revision="tools-v1",
        capacity_snapshot_hash="b" * 64,
        objective_revision="expected-loss-v1",
        constraint_revision="constraints-v1",
        code_revision="costweave-test-v1",
        captured_at="2026-08-04T00:00:00Z",
        evidence_level=evidence,
    )


def routes(*, feasible: bool = True) -> tuple[ReplayRouteCandidate, ...]:
    return (
        ReplayRouteCandidate("cheap", "c" * 64, 0.10, 0.80, 0.70, 100, feasible),
        ReplayRouteCandidate("balanced", "d" * 64, 0.30, 0.40, 0.90, 200, feasible),
        ReplayRouteCandidate("strong", "e" * 64, 0.80, 0.50, 0.98, 400, feasible),
    )


def replay_case(
    *,
    index: int = 1,
    snap: ReplayVersionSnapshot | None = None,
    risk: str = "low",
    reversible: bool = True,
    sensitive: bool = False,
    candidates: tuple[ReplayRouteCandidate, ...] | None = None,
) -> ReplayCase:
    snap = snap or snapshot()
    return ReplayCase.create(
        snapshot_id=snap.snapshot_id,
        anonymous_task_id=f"task_{index:032x}",
        task_family="code",
        risk_level=risk,
        reversible=reversible,
        permission_sensitive=sensitive,
        recorded_selected_route_id="balanced",
        candidates=candidates or routes(),
    )


def policy(rule: str, name: str | None = None) -> ReplayPolicySpec:
    return ReplayPolicySpec(
        policy_id=name or rule,
        policy_version="v1",
        selection_rule=rule,
        frozen_route_scores=(
            {"cheap": 0.9, "balanced": 0.2, "strong": 0.4}
            if rule == "frozen_score"
            else None
        ),
    )


def outcomes(
    case: ReplayCase,
    *,
    evidence: str = "E3-independent-real",
    origin: str = "human_benchmark",
    independent: bool = True,
    include: tuple[str, ...] = ("cheap", "balanced", "strong"),
    base: int = 0,
) -> tuple[ObservedRouteOutcome, ...]:
    losses = {"cheap": 0.90, "balanced": 0.40, "strong": 0.60}
    result = []
    for offset, route_id in enumerate(include):
        result.append(
            ObservedRouteOutcome(
                outcome_id=f"routeout_{base + offset:032x}",
                case_id=case.case_id,
                route_id=route_id,
                accepted=True,
                realized_total_loss=losses[route_id],
                evidence_level=evidence,
                data_origin=origin,
                independently_validated=independent,
                selection_propensity=1 / 3,
            )
        )
    return tuple(result)


class SnapshotAndReplayTests(unittest.TestCase):
    def test_snapshot_is_deterministic_and_has_no_execution_authority(self):
        left = snapshot()
        right = snapshot()
        self.assertEqual(left, right)
        self.assertFalse(left.external_calls_allowed)
        self.assertFalse(left.routing_authority)
        self.assertFalse(left.may_change_actual_route)

    def test_case_is_order_independent_and_binds_snapshot(self):
        snap = snapshot()
        left = replay_case(snap=snap)
        right = replay_case(snap=snap, candidates=tuple(reversed(routes())))
        self.assertEqual(left, right)
        with self.assertRaises(OfflineReplayError):
            OfflineReplayEngine().replay(snapshot(evidence="E3-real"), left, policy("recorded_route"))

    def test_selection_rules_choose_expected_routes(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        engine = OfflineReplayEngine()
        expected = {
            "minimum_expected_loss": "balanced",
            "minimum_direct_cost": "cheap",
            "maximum_success_lower": "strong",
            "frozen_score": "balanced",
            "recorded_route": "balanced",
        }
        for rule, route_id in expected.items():
            with self.subTest(rule=rule):
                result = engine.replay(snap, case, policy(rule))
                self.assertEqual(route_id, result.selected_route_id)
                self.assertEqual(0, result.external_call_count)
                self.assertEqual(result, engine.replay(snap, case, policy(rule)))

    def test_hard_constraints_filter_and_can_leave_no_route(self):
        snap = snapshot()
        candidates = list(routes())
        candidates[0] = replace(candidates[0], hard_constraints_passed=False)
        case = replay_case(snap=snap, candidates=tuple(candidates))
        self.assertEqual(
            "balanced",
            OfflineReplayEngine().replay(snap, case, policy("minimum_direct_cost")).selected_route_id,
        )
        empty = replay_case(snap=snap, candidates=routes(feasible=False))
        result = OfflineReplayEngine().replay(snap, empty, policy("minimum_direct_cost"))
        self.assertEqual("no_feasible_route", result.state)
        self.assertIsNone(result.selected_route_id)

    def test_frozen_scores_must_cover_every_route(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        bad = ReplayPolicySpec("bad", "v1", "frozen_score", {"cheap": 0.1})
        with self.assertRaises(OfflineReplayError):
            OfflineReplayEngine().replay(snap, case, bad)


class ApproximateOracleTests(unittest.TestCase):
    def test_oracle_picks_best_observed_accepted_route(self):
        snap = snapshot(evidence="E3-real")
        case = replay_case(snap=snap)
        report = build_approximate_oracle(snap, case, outcomes(case))
        self.assertEqual("balanced", report.oracle_route_id)
        self.assertEqual(0.40, report.oracle_realized_total_loss)
        self.assertTrue(report.directional_evidence_eligible)
        self.assertFalse(report.global_optimality_proven)

    def test_unobserved_routes_remain_explicitly_unknown(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        report = build_approximate_oracle(
            snap, case, outcomes(case, include=("balanced",), evidence="E0-fixture", origin="test_fixture")
        )
        self.assertEqual(("cheap", "strong"), report.unknown_route_ids)
        self.assertAlmostEqual(1 / 3, report.coverage_ratio)
        self.assertFalse(report.directional_evidence_eligible)

    def test_multi_route_evaluation_is_forbidden_for_sensitive_scope(self):
        snap = snapshot()
        for case in (
            replay_case(snap=snap, risk="high"),
            replay_case(snap=snap, reversible=False),
            replay_case(snap=snap, sensitive=True),
        ):
            with self.subTest(case=case.case_id):
                report = build_approximate_oracle(snap, case, outcomes(case))
                self.assertEqual("subset_forbidden", report.state)
                self.assertIsNone(report.oracle_route_id)

    def test_empty_and_rejected_outcomes_do_not_invent_oracle(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        self.assertEqual("no_observed_routes", build_approximate_oracle(snap, case, ()).state)
        rejected = tuple(replace(item, accepted=False) for item in outcomes(case))
        self.assertEqual("no_accepted_route", build_approximate_oracle(snap, case, rejected).state)

    def test_oracle_rejects_duplicate_route_outcomes(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        items = outcomes(case)
        duplicate = replace(items[0], outcome_id="routeout_ffffffffffffffffffffffffffffffff")
        with self.assertRaises(OfflineReplayError):
            build_approximate_oracle(snap, case, (items[0], duplicate))


class ReplayBenchmarkTests(unittest.TestCase):
    def test_multiple_baselines_have_known_regret(self):
        snap = snapshot(evidence="E3-real")
        case = replay_case(snap=snap)
        report = OfflineReplayBenchmark().evaluate(
            snap,
            (case,),
            (policy("minimum_expected_loss"), policy("maximum_success_lower")),
            outcomes(case),
        )
        by_policy = {item["policy_key"]: item for item in report.regret_results}
        self.assertEqual(0.0, by_policy["minimum_expected_loss@v1"]["normalized_regret"])
        self.assertEqual(0.5, by_policy["maximum_success_lower@v1"]["normalized_regret"])
        self.assertEqual("incomplete_counterfactuals", report.state)

    def test_unknown_selected_counterfactual_is_not_scored_as_zero(self):
        snap = snapshot(evidence="E3-real")
        case = replay_case(snap=snap)
        report = OfflineReplayBenchmark().evaluate(
            snap,
            (case,),
            (policy("minimum_expected_loss"), policy("maximum_success_lower")),
            outcomes(case, include=("cheap", "balanced")),
        )
        strong = next(item for item in report.regret_results if item["policy_key"].startswith("maximum"))
        self.assertEqual("selected_counterfactual_unknown", strong["state"])
        self.assertIsNone(strong["normalized_regret"])
        self.assertFalse(report.checks["all_selected_outcomes_observed"])

    def test_selected_route_only_logging_is_reported_as_bias(self):
        snap = snapshot(evidence="E3-real")
        case = replay_case(snap=snap)
        report = OfflineReplayBenchmark().evaluate(
            snap,
            (case,),
            (policy("minimum_expected_loss"), policy("recorded_route")),
            outcomes(case, include=("balanced",)),
        )
        self.assertEqual(1, report.bias_diagnostics["selected_route_only_case_count"])
        self.assertFalse(report.checks["no_selected_route_only_bias"])

    def test_e0_fixture_metrics_are_blocked(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        report = OfflineReplayBenchmark().evaluate(
            snap,
            (case,),
            (policy("minimum_expected_loss"), policy("recorded_route")),
            outcomes(case, evidence="E0-fixture", origin="test_fixture", independent=False),
        )
        self.assertEqual("blocked_evidence", report.state)
        self.assertFalse(report.normalized_regret_threshold_assessment_allowed)
        self.assertFalse(report.commercial_claims_allowed)

    def test_thirty_complete_real_cases_allow_directional_threshold_assessment(self):
        snap = snapshot(evidence="E3-real")
        cases = tuple(replay_case(index=index + 1, snap=snap) for index in range(30))
        all_outcomes = tuple(
            item
            for index, case in enumerate(cases)
            for item in outcomes(case, base=index * 3)
        )
        report = OfflineReplayBenchmark().evaluate(
            snap,
            cases,
            (policy("minimum_expected_loss"), policy("recorded_route")),
            all_outcomes,
        )
        self.assertEqual("directional_replay", report.state)
        self.assertTrue(report.normalized_regret_threshold_assessment_allowed)
        for summary in report.policy_summaries.values():
            self.assertEqual(30, summary["observed_directional_case_count"])
            self.assertTrue(summary["median_threshold_passed"])
            self.assertTrue(summary["p90_threshold_passed"])
            self.assertFalse(summary["threshold_claim_allowed"])

    def test_report_rejects_schema_and_state_contradictions(self):
        snap = snapshot()
        case = replay_case(snap=snap)
        report = OfflineReplayBenchmark().evaluate(
            snap,
            (case,),
            (policy("minimum_expected_loss"), policy("recorded_route")),
            outcomes(case, evidence="E0-fixture", origin="test_fixture", independent=False),
        )
        with self.assertRaises(OfflineReplayError):
            replace(report, schema_version="v9")
        with self.assertRaises(OfflineReplayError):
            replace(report, state="directional_replay")

    def test_public_policy_discloses_non_authoritative_boundaries(self):
        value = public_offline_replay_policy()
        self.assertTrue(value["historical_version_snapshot_required"])
        self.assertEqual("unknown", value["approximate_oracle"]["unobserved_counterfactual_state"])
        self.assertFalse(value["routing_authority"])
        self.assertFalse(value["policy_promotion_allowed"])
        self.assertFalse(value["commercial_claims_allowed"])


class OfflineReplayPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = OfflineReplayStore(self.path)
        self.snap = snapshot(evidence="E3-real")
        self.case = replay_case(snap=self.snap)
        self.policies = (
            policy("minimum_expected_loss"),
            policy("recorded_route"),
        )
        self.outcomes = outcomes(self.case)
        self.replays = tuple(
            OfflineReplayEngine().replay(self.snap, self.case, item)
            for item in self.policies
        )
        self.oracle = build_approximate_oracle(
            self.snap, self.case, self.outcomes
        )
        self.benchmark = OfflineReplayBenchmark().evaluate(
            self.snap,
            (self.case,),
            self.policies,
            self.outcomes,
        )

    def tearDown(self):
        self.temp.cleanup()

    def persist_inputs(self):
        self.store.record_snapshot(self.snap)
        self.store.record_case(self.case)
        for item in self.outcomes:
            self.store.record_outcome(item)

    def test_migration_and_empty_registry(self):
        self.assertEqual("v5.7.1", self.health["runtime_schema"])
        self.assertEqual(22, self.health["migrations"])
        counts = self.store.counts()
        self.assertEqual(0, counts["replay_version_snapshots"])
        self.assertEqual(0, counts["offline_replay_route_interventions"])

    def test_linked_records_are_idempotent_and_integrity_checked(self):
        self.persist_inputs()
        self.assertFalse(self.store.record_snapshot(self.snap))
        self.assertFalse(self.store.record_case(self.case))
        for item in self.replays:
            self.assertTrue(self.store.record_replay(item))
            self.assertFalse(self.store.record_replay(item))
        self.assertTrue(self.store.record_oracle(self.oracle))
        self.assertFalse(self.store.record_oracle(self.oracle))
        self.assertTrue(self.store.record_benchmark(self.benchmark))
        self.assertFalse(self.store.record_benchmark(self.benchmark))
        self.assertEqual(
            self.benchmark.report_id,
            self.store.benchmark(self.benchmark.report_id)["report_id"],
        )
        counts = self.store.counts()
        self.assertEqual(2, counts["offline_replay_results"])
        self.assertEqual(1, counts["approximate_oracle_reports"])
        self.assertEqual(0, counts["offline_replay_global_optimality_claims"])

    def test_linked_evidence_is_required(self):
        with self.assertRaises(OfflineReplayError):
            self.store.record_case(self.case)
        self.store.record_snapshot(self.snap)
        self.store.record_case(self.case)
        with self.assertRaises(OfflineReplayError):
            self.store.record_oracle(self.oracle)
        with self.assertRaises(OfflineReplayError):
            self.store.record_benchmark(self.benchmark)

    def test_database_rejects_mutation_deletion_and_authority(self):
        self.persist_inputs()
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE replay_version_snapshots SET routing_authority=1 WHERE snapshot_id=?",
                    (self.snap.snapshot_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM offline_replay_cases WHERE case_id=?",
                    (self.case.case_id,),
                )
        finally:
            connection.close()

    def test_tampered_persisted_payload_is_detected(self):
        self.store.record_snapshot(self.snap)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TRIGGER trg_replay_snapshot_immutable")
            connection.execute(
                "UPDATE replay_version_snapshots SET snapshot_json='{}' WHERE snapshot_id=?",
                (self.snap.snapshot_id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(OfflineReplayError):
            self.store.snapshot(self.snap.snapshot_id)


if __name__ == "__main__":
    unittest.main()
