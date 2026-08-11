import json
import math
import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from importlib.resources import files

from costweave.intelligence.common import (
    NonNegativeEstimate,
    ProbabilityInterval,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_digest,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
    validate_timestamp,
)
from costweave.intelligence.beliefs import (
    ProviderBeliefV1,
    SystemBeliefV1,
    TaskBeliefV1,
)
from costweave.intelligence.capability import CapabilityPosteriorV1
from costweave.intelligence.strategy import StrategyCandidateV1, StrategyNodeV1
from costweave.intelligence.decision_trace import DecisionTraceV2
from costweave.intelligence.credit import CREDIT_METHODS, OutcomeCreditV1
from costweave.intelligence.common import CREDIT_COMPONENTS
from costweave.event_store import EventStore, RUNTIME_SCHEMA_VERSION
from costweave.decision_intelligence_store import DecisionIntelligenceStore, DecisionIntelligenceStoreError
from costweave.control_kernel import ControlKernel
from costweave.benchmarks.contracts import (
    CaseViewV1,
    DatasetManifestV1,
    RoutingRecordV1,
    build_dataset_manifest,
    validate_routing_records,
)


class CommonContractTests(unittest.TestCase):
    def test_probability_interval_normal_and_round_trip(self):
        item = ProbabilityInterval(mean=0.5, lower=0.2, upper=0.8)
        self.assertEqual(
            {"mean": 0.5, "lower": 0.2, "upper": 0.8},
            item.to_dict(),
        )
        self.assertEqual(item, ProbabilityInterval.from_dict(item.to_dict()))

    def test_non_negative_estimate_boundary_and_round_trip(self):
        item = NonNegativeEstimate(mean=0, p95=0)
        self.assertEqual({"mean": 0.0, "p95": 0.0}, item.to_dict())
        self.assertEqual(item, NonNegativeEstimate.from_dict(item.to_dict()))

    def test_finite_and_probability_helpers_reject_nan_infinity_and_bool(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                finite_number(value, "value")
        with self.assertRaises(TypeError):
            finite_number(True, "value")
        with self.assertRaises(ValueError):
            probability(1.01, "value")
        with self.assertRaises(ValueError):
            probability(-0.01, "value")

    def test_probability_interval_rejects_invalid_order_and_bool(self):
        with self.assertRaises(ValueError):
            ProbabilityInterval(mean=0.5, lower=0.6, upper=0.8)
        with self.assertRaises(ValueError):
            ProbabilityInterval(mean=0.5, lower=0.2, upper=0.4)
        with self.assertRaises(TypeError):
            ProbabilityInterval(mean=True, lower=0.0, upper=1.0)

    def test_non_negative_estimate_rejects_negative_and_wrong_order(self):
        with self.assertRaises(ValueError):
            NonNegativeEstimate(mean=-1, p95=1)
        with self.assertRaises(ValueError):
            NonNegativeEstimate(mean=2, p95=1)
        with self.assertRaises(TypeError):
            NonNegativeEstimate(mean=True, p95=1)

    def test_text_bounds_and_duplicate_detection(self):
        self.assertEqual("hello", bounded_text("  hello ", "name"))
        with self.assertRaises(ValueError):
            bounded_text("", "name")
        with self.assertRaises(ValueError):
            unique_texts(["a", "a"], "items")
        with self.assertRaises(ValueError):
            unique_texts(["a", "b"], "items", maximum_items=1)

    def test_exact_fields_reject_unknown_and_missing(self):
        validate_exact_fields(
            {"required": 1, "optional": 2},
            required=frozenset({"required"}),
            optional=frozenset({"optional"}),
        )
        with self.assertRaises(ValueError):
            validate_exact_fields({"required": 1, "extra": 2}, required=frozenset({"required"}))
        with self.assertRaises(ValueError):
            validate_exact_fields({}, required=frozenset({"required"}))

    def test_hashes_are_deterministic_and_stable_id_has_valid_prefix(self):
        first = stable_digest({"b": 2, "a": 1})
        second = stable_digest({"a": 1, "b": 2})
        self.assertEqual(first, second)
        self.assertEqual(32, len(first))
        identifier = stable_id("task", {"goal": "分析"})
        self.assertRegex(identifier, r"^task_[0-9a-f]{32}$")
        self.assertEqual(identifier, stable_id("task", {"goal": "分析"}))
        with self.assertRaises(ValueError):
            stable_id("Task", {})

    def test_timestamp_requires_timezone(self):
        self.assertEqual("2026-08-09T12:00:00+08:00", validate_timestamp("2026-08-09T12:00:00+08:00", "at"))
        self.assertEqual("2026-08-09T04:00:00Z", validate_timestamp("2026-08-09T04:00:00Z", "at"))
        with self.assertRaises(ValueError):
            validate_timestamp("2026-08-09T12:00:00", "at")
        with self.assertRaises(ValueError):
            validate_timestamp("not-a-time", "at")

    def test_sha256_requires_lowercase_full_digest(self):
        digest = "a" * 64
        self.assertEqual(digest, validate_sha256(digest, "digest"))
        with self.assertRaises(ValueError):
            validate_sha256("A" * 64, "digest")
        with self.assertRaises(ValueError):
            validate_sha256("a" * 63, "digest")

    def test_canonical_json_is_compact_unicode_sorted_and_finite(self):
        self.assertEqual('{"a":1,"z":"中文"}', canonical_json({"z": "中文", "a": 1}))
        self.assertEqual('{"number":1.5}', canonical_json({"number": 1.5}))
        for value in (float("nan"), float("inf"), b"bytes", object(), {1: "bad"}):
            with self.assertRaises((TypeError, ValueError)):
                canonical_json(value)
        with self.assertRaises((TypeError, ValueError)):
            canonical_json({"bad": float("-inf")})

    def test_contract_values_are_finite_json_values(self):
        for value in (ProbabilityInterval(0.5, 0.0, 1.0).to_dict(), NonNegativeEstimate(1, 2).to_dict()):
            encoded = canonical_json(value)
            self.assertEqual(value, json.loads(encoded))
            self.assertTrue(all(math.isfinite(float(item)) for item in value.values()))


class BeliefContractTests(unittest.TestCase):
    @staticmethod
    def _objective_hash() -> str:
        import hashlib

        return hashlib.sha256("objective".encode("utf-8")).hexdigest()

    @classmethod
    def _provider(cls, provider_id: str = "local", deployment_id: str = "cpu") -> ProviderBeliefV1:
        return ProviderBeliefV1(
            provider_id=provider_id,
            deployment_id=deployment_id,
            available=True,
            queue_latency_ms=NonNegativeEstimate(10, 20),
            completion_latency_ms=NonNegativeEstimate(100, 200),
            telemetry_age_ms=5,
            price_snapshot_id="price-1",
        )

    @classmethod
    def _task(cls, *, observed_at: str = "2026-08-09T12:00:00+08:00", case_id: str = "case-1") -> TaskBeliefV1:
        return TaskBeliefV1(
            case_id=case_id,
            objective_hash=cls._objective_hash(),
            task_family_probabilities={"code": 0.7, "research": 0.3},
            required_modalities=("text",),
            required_tools=("filesystem",),
            risk_level="low",
            reversible=True,
            external_side_effects=False,
            decomposability=ProbabilityInterval(0.8, 0.7, 0.9),
            parallelizability=ProbabilityInterval(0.6, 0.5, 0.7),
            verification_difficulty=ProbabilityInterval(0.2, 0.1, 0.3),
            epistemic_uncertainty=0.1,
            aleatoric_uncertainty=0.2,
            ood_score=0.05,
            hard_constraints={"offline": True},
            evidence_ids=("evidence-1",),
            observed_at=observed_at,
        )

    @classmethod
    def _system(cls, *, observed_at: str = "2026-08-09T12:00:00+08:00", providers=None) -> SystemBeliefV1:
        return SystemBeliefV1(
            case_id="case-1",
            local_queue_depth=2,
            local_available_slots=1,
            local_gpu_memory_available_mb=None,
            budget_remaining_usd=2.5,
            deadline_remaining_ms=5000,
            provider_beliefs=tuple(providers if providers is not None else (cls._provider(),)),
            telemetry_age_ms=10,
            evidence_ids=("system-evidence-1",),
            observed_at=observed_at,
        )

    def test_task_and_system_beliefs_round_trip(self):
        task = self._task()
        self.assertEqual(task, TaskBeliefV1.from_dict(task.to_dict()))
        system = self._system()
        self.assertEqual(system, SystemBeliefV1.from_dict(system.to_dict()))

    def test_task_family_probabilities_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            replace(self._task(), task_family_probabilities={"code": 0.8, "research": 0.3})

    def test_invalid_task_family_key_is_rejected(self):
        with self.assertRaises(ValueError):
            TaskBeliefV1(
                case_id="case-1",
                objective_hash=self._objective_hash(),
                task_family_probabilities={"Bad Key": 1.0},
                required_modalities=(),
                required_tools=(),
                risk_level="low",
                reversible=True,
                external_side_effects=False,
                decomposability=ProbabilityInterval(0.5, 0.0, 1.0),
                parallelizability=ProbabilityInterval(0.5, 0.0, 1.0),
                verification_difficulty=ProbabilityInterval(0.5, 0.0, 1.0),
                epistemic_uncertainty=0.0,
                aleatoric_uncertainty=0.0,
                ood_score=0.0,
                hard_constraints={},
                evidence_ids=(),
                observed_at="2026-08-09T12:00:00+08:00",
            )

    def test_duplicate_provider_pair_is_rejected(self):
        provider = self._provider()
        with self.assertRaises(ValueError):
            self._system(providers=(provider, provider))

    def test_negative_capacity_is_rejected(self):
        with self.assertRaises(ValueError):
            SystemBeliefV1(
                case_id="case-1",
                local_queue_depth=-1,
                local_available_slots=0,
                local_gpu_memory_available_mb=None,
                budget_remaining_usd=1,
                deadline_remaining_ms=None,
                provider_beliefs=(),
                telemetry_age_ms=0,
                evidence_ids=(),
                observed_at="2026-08-09T12:00:00+08:00",
            )

    def test_unknown_gpu_memory_must_be_none(self):
        with self.assertRaises(ValueError):
            replace(self._system(), local_gpu_memory_available_mb=0)
        self.assertIsNone(self._system().local_gpu_memory_available_mb)

    def test_observed_time_does_not_change_belief_id(self):
        self.assertEqual(
            self._task().belief_id,
            self._task(observed_at="2026-08-09T13:00:00+08:00").belief_id,
        )
        self.assertEqual(
            self._system().belief_id,
            self._system(observed_at="2026-08-09T13:00:00+08:00").belief_id,
        )

    def test_business_field_changes_belief_id(self):
        self.assertNotEqual(self._task().belief_id, self._task(case_id="case-2").belief_id)
        self.assertNotEqual(
            self._system().belief_id,
            self._system(providers=(self._provider("cloud", "deployment-1"),)).belief_id,
        )

    def test_array_limits_are_enforced(self):
        base = self._task()
        with self.assertRaises(ValueError):
            replace(base, required_tools=tuple(f"tool-{i}" for i in range(65)))
        with self.assertRaises(ValueError):
            self._system(providers=tuple(self._provider("provider", f"deployment-{i}") for i in range(129)))

    def test_hard_constraints_must_be_finite_json_object(self):
        base = self._task()
        with self.assertRaises((TypeError, ValueError)):
            replace(base, hard_constraints={"bad": object()})

    def test_schema_and_boolean_fields_are_strict(self):
        base = self._task()
        with self.assertRaises(ValueError):
            replace(base, schema_version="v9")
        with self.assertRaises(TypeError):
            replace(base, reversible=1)


class CapabilityContractTests(unittest.TestCase):
    @staticmethod
    def _posterior(*, evidence_count: int = 0, evidence_level: str = "E0-simulated", as_of: str = "2026-08-09T12:00:00+08:00") -> CapabilityPosteriorV1:
        return CapabilityPosteriorV1(
            candidate_id="candidate-1",
            task_family="code",
            model_revision="model-r1",
            reasoning_tier="medium",
            provider_deployment="local/cpu",
            success=ProbabilityInterval(0.8, 0.7, 0.9),
            cost_usd=NonNegativeEstimate(0.01, 0.02),
            latency_ms=NonNegativeEstimate(100, 200),
            retry_probability=0.1,
            escalation_probability=0.2,
            rework_probability=0.1,
            evidence_count=evidence_count,
            evidence_level=evidence_level,
            correlation_group="local-cpu",
            source_ids=("source-1",),
            as_of=as_of,
        )

    def test_e0_zero_sample_is_allowed_and_round_trips(self):
        item = self._posterior()
        self.assertEqual(item, CapabilityPosteriorV1.from_dict(item.to_dict()))

    def test_e2_zero_sample_is_rejected(self):
        with self.assertRaises(ValueError):
            self._posterior(evidence_level="E2-directional-real")

    def test_unknown_evidence_level_is_rejected(self):
        with self.assertRaises(ValueError):
            self._posterior(evidence_level="E9-private")

    def test_invalid_correlation_group_is_rejected(self):
        with self.assertRaises(ValueError):
            replace(self._posterior(), correlation_group=" ")

    def test_time_does_not_change_posterior_id(self):
        self.assertEqual(
            self._posterior().posterior_id,
            self._posterior(as_of="2026-08-09T13:00:00+08:00").posterior_id,
        )

    def test_probability_boundaries_are_validated(self):
        self.assertEqual(0.0, replace(self._posterior(), retry_probability=0.0).retry_probability)
        self.assertEqual(1.0, replace(self._posterior(), escalation_probability=1.0).escalation_probability)
        with self.assertRaises(ValueError):
            replace(self._posterior(), rework_probability=1.01)

    def test_model_self_report_field_is_not_accepted(self):
        payload = self._posterior().to_dict()
        payload["self_reported_confidence"] = 0.99
        with self.assertRaises(ValueError):
            CapabilityPosteriorV1.from_dict(payload)


class StrategyContractTests(unittest.TestCase):
    @staticmethod
    def _hash() -> str:
        import hashlib

        return hashlib.sha256(b"strategy-objective").hexdigest()

    @classmethod
    def _node(cls, node_id: str, dependencies: tuple[str, ...] = ()) -> StrategyNodeV1:
        return StrategyNodeV1(
            node_id=node_id,
            objective_hash=cls._hash(),
            dependencies=dependencies,
            executor_id="executor-local",
            reasoning_tier="medium",
            tool_ids=("filesystem",),
            parallel_group="group-1",
            validator_id="deterministic",
            predicted_success=ProbabilityInterval(0.8, 0.7, 0.9),
            predicted_cost_usd=NonNegativeEstimate(0.1, 0.2),
            predicted_latency_ms=NonNegativeEstimate(100, 200),
        )

    @classmethod
    def _candidate(cls, *, nodes=None, management_mode: str = "planner", checks=None, recovery_policy: str = "none", total_cost: NonNegativeEstimate | None = None) -> StrategyCandidateV1:
        return StrategyCandidateV1(
            case_id="case-1",
            management_mode=management_mode,
            nodes=tuple(nodes if nodes is not None else (cls._node("a"), cls._node("b", ("a",)), cls._node("c", ("a",)))),
            planning_cost_usd=NonNegativeEstimate(0.05, 0.1),
            planning_latency_ms=NonNegativeEstimate(20, 40),
            predicted_success=ProbabilityInterval(0.8, 0.7, 0.9),
            predicted_total_cost_usd=total_cost or NonNegativeEstimate(0.3, 0.5),
            predicted_total_latency_ms=NonNegativeEstimate(250, 400),
            hard_constraint_checks=checks or {"budget": True},
            recovery_policy=recovery_policy,
            evidence_ids=("evidence-1",),
            policy_revision="policy-v1",
        )

    def test_normal_dag_and_round_trip(self):
        candidate = self._candidate()
        self.assertEqual(("a", "b", "c"), candidate.topological_order())
        self.assertTrue(candidate.feasible)
        self.assertEqual(candidate, StrategyCandidateV1.from_dict(candidate.to_dict()))

    def test_topological_order_is_deterministic_with_sorted_same_layer(self):
        candidate = self._candidate(nodes=(self._node("root"), self._node("z", ("root",)), self._node("a", ("root",))))
        self.assertEqual(("root", "a", "z"), candidate.topological_order())

    def test_cycle_is_rejected(self):
        with self.assertRaises(ValueError):
            self._candidate(nodes=(self._node("a", ("b",)), self._node("b", ("a",))))

    def test_missing_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            self._candidate(nodes=(self._node("a", ("missing",)),))

    def test_self_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            self._node("a", ("a",))

    def test_duplicate_node_is_rejected(self):
        node = self._node("a")
        with self.assertRaises(ValueError):
            self._candidate(nodes=(node, node))

    def test_no_delegation_requires_one_node(self):
        with self.assertRaises(ValueError):
            self._candidate(management_mode="no_delegation")
        candidate = self._candidate(nodes=(self._node("only"),), management_mode="no_delegation")
        self.assertEqual(("only",), candidate.topological_order())

    def test_false_hard_check_is_storable_but_not_feasible(self):
        candidate = self._candidate(checks={"budget": False})
        self.assertFalse(candidate.feasible)

    def test_total_cost_below_planning_cost_is_rejected(self):
        with self.assertRaises(ValueError):
            self._candidate(total_cost=NonNegativeEstimate(0.01, 0.02))

    def test_unknown_recovery_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            self._candidate(recovery_policy="unknown")


class DecisionTraceContractTests(unittest.TestCase):
    @staticmethod
    def _trace(
        *,
        project_id: str = "Project_A",
        created_at: str = "2026-08-09T12:00:00+08:00",
        candidate_probabilities=None,
        selected_strategy_id: str = "strategy-a",
        reason_codes=("cost_budget",),
        meta_action: str = "fast",
        policy_id: str = "policy-main",
        policy_version: str = "v1",
    ) -> DecisionTraceV2:
        return DecisionTraceV2(
            project_id=project_id,
            case_id="case-1",
            task_belief_id="task_belief_" + "a" * 32,
            system_belief_id="system_belief_" + "b" * 32,
            candidate_strategy_ids=("strategy-a", "strategy-b"),
            candidate_probabilities=candidate_probabilities or {"strategy-a": 0.7, "strategy-b": 0.3},
            selected_strategy_id=selected_strategy_id,
            meta_action=meta_action,
            reason_codes=reason_codes,
            evidence_ids=("evidence-1",),
            policy_id=policy_id,
            policy_version=policy_version,
            created_at=created_at,
        )

    def test_normal_round_trip_and_project_normalization(self):
        item = self._trace()
        self.assertEqual("project_a", item.project_id)
        self.assertEqual(item, DecisionTraceV2.from_dict(item.to_dict()))

    def test_probability_key_missing_is_rejected(self):
        with self.assertRaises(ValueError):
            self._trace(candidate_probabilities={"strategy-a": 1.0})

    def test_probability_key_extra_is_rejected(self):
        with self.assertRaises(ValueError):
            self._trace(candidate_probabilities={"strategy-a": 0.5, "strategy-b": 0.4, "strategy-c": 0.1})

    def test_probability_sum_is_validated(self):
        with self.assertRaises(ValueError):
            self._trace(candidate_probabilities={"strategy-a": 0.7, "strategy-b": 0.4})

    def test_selected_probability_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            self._trace(candidate_probabilities={"strategy-a": 0.0, "strategy-b": 1.0})

    def test_time_does_not_change_trace_id(self):
        self.assertEqual(
            self._trace().trace_id,
            self._trace(created_at="2026-08-09T13:00:00+08:00").trace_id,
        )

    def test_policy_change_changes_trace_id(self):
        self.assertNotEqual(self._trace().trace_id, self._trace(policy_version="v2").trace_id)

    def test_empty_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            self._trace(reason_codes=())

    def test_unknown_meta_action_is_rejected(self):
        with self.assertRaises(ValueError):
            self._trace(meta_action="unknown")


class CreditContractTests(unittest.TestCase):
    @staticmethod
    def _weights() -> dict[str, float]:
        return {component: 1.0 / len(CREDIT_COMPONENTS) for component in CREDIT_COMPONENTS}

    @classmethod
    def _credit(cls, *, method: str = "deterministic", evidence_level: str = "E0-simulated", usable_for_learning: bool = False, created_at: str = "2026-08-09T12:00:00+08:00", weights=None) -> OutcomeCreditV1:
        return OutcomeCreditV1(
            project_id="Project_A",
            trace_id="decision_trace_" + "a" * 32,
            accepted=True,
            total_cost_usd=0.2,
            total_latency_ms=200,
            component_weights=weights or cls._weights(),
            method=method,
            evidence_level=evidence_level,
            usable_for_learning=usable_for_learning,
            evidence_ids=("evidence-1",),
            created_at=created_at,
        )

    def test_normal_round_trip(self):
        item = self._credit()
        self.assertEqual(item, OutcomeCreditV1.from_dict(item.to_dict()))

    def test_component_missing_or_extra_is_rejected(self):
        weights = self._weights()
        weights.pop("recovery")
        with self.assertRaises(ValueError):
            self._credit(weights=weights)
        weights = self._weights()
        weights["extra"] = 0.0
        with self.assertRaises(ValueError):
            self._credit(weights=weights)

    def test_component_weight_sum_is_validated(self):
        weights = {component: 0.0 for component in CREDIT_COMPONENTS}
        weights["execution"] = 0.5
        with self.assertRaises(ValueError):
            self._credit(weights=weights)

    def test_llm_attribution_cannot_be_used_for_learning(self):
        with self.assertRaises(ValueError):
            self._credit(method="llm_attribution", usable_for_learning=True)

    def test_e1_cannot_be_used_for_learning(self):
        with self.assertRaises(ValueError):
            self._credit(evidence_level="E1-public-replay", usable_for_learning=True)

    def test_e2_can_explicitly_remain_unusable(self):
        item = self._credit(evidence_level="E2-directional-real", usable_for_learning=False)
        self.assertFalse(item.usable_for_learning)

    def test_cost_and_latency_reject_negative_or_bool(self):
        with self.assertRaises(ValueError):
            replace(self._credit(), total_cost_usd=-0.1)
        with self.assertRaises(ValueError):
            replace(self._credit(), total_latency_ms=-1)
        with self.assertRaises(TypeError):
            replace(self._credit(), total_latency_ms=True)

    def test_time_does_not_change_credit_id(self):
        self.assertEqual(
            self._credit().credit_id,
            self._credit(created_at="2026-08-09T13:00:00+08:00").credit_id,
        )


class Migration030Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.store = EventStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_database_has_migration_030_and_v6_runtime(self):
        self.assertEqual("v6.5.0", RUNTIME_SCHEMA_VERSION)
        self.assertEqual(35, self.store.health()["migrations"])
        with closing(sqlite3.connect(self.path)) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue({"decision_objects_v060", "tournament_runs_v060"} <= tables)

    def test_migration_001_to_035_checksums_match_files(self):
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT version,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(list(range(1, 36)), [row[0] for row in rows])
        expected = []
        for version in range(1, 36):
            filename = f"{version:03d}_" + {
                1: "runtime.sql", 2: "artifacts.sql", 3: "parallel_runtime.sql", 4: "cost_economics.sql",
                5: "evidence_recovery.sql", 6: "outcome_evaluation.sql", 7: "media_workflow.sql", 8: "release_hardening.sql",
                9: "deliberation_contracts.sql", 10: "planning_governor.sql", 11: "multiplanner.sql", 12: "cross_provider_economics.sql",
                13: "handoff_recovery.sql", 14: "evaluation_protocol.sql", 15: "outcome_dataset_governance.sql", 16: "experimental_adapters_shadow.sql",
                17: "predictor_admission.sql", 18: "predictor_offline_evaluation.sql", 19: "portfolio_audit.sql", 20: "portfolio_validation.sql",
                21: "policy_candidates.sql", 22: "offline_replay.sql", 23: "production_shadow.sql", 24: "policy_registry.sql",
                25: "bounded_canary.sql", 26: "evidence_console.sql", 27: "security_boundary.sql", 28: "resilience.sql",
                29: "release_attestations.sql", 30: "decision_intelligence.sql", 31: "rational_meta_controller.sql", 32: "capability_evidence_graph.sql", 33: "joint_plan_route_schedule_search.sql", 34: "execution_time_belief_economic_recovery.sql", 35: "trace_credit_assignment_safe_learning.sql",
            }[version]
            raw = files("costweave.migrations").joinpath(filename).read_bytes()
            expected.append(hashlib.sha256(raw).hexdigest())
        self.assertEqual(expected, [row[1] for row in rows])

    def test_four_immutability_triggers_are_present_and_enforced(self):
        with closing(sqlite3.connect(self.path)) as connection:
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_v060_no_%'"
                )
            }
            self.assertEqual(
                {
                    "decision_objects_v060_no_update", "decision_objects_v060_no_delete",
                    "tournament_runs_v060_no_update", "tournament_runs_v060_no_delete",
                },
                triggers,
            )
            connection.execute(
                "INSERT INTO decision_objects_v060 VALUES (?,?,?,?,?,?,?)",
                ("local-default", "task_belief_" + "a" * 32, "task_belief", "v0.6.0", "{}", "a" * 64, "2026-08-09T12:00:00Z"),
            )
            connection.execute(
                "INSERT INTO tournament_runs_v060 VALUES (?,?,?,?,?,?,?,?)",
                ("local-default", "tournament_1", "idem_1", "dataset_1", "b" * 64, "{}", "c" * 64, "2026-08-09T12:00:00Z"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE decision_objects_v060 SET payload_json='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM decision_objects_v060")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE tournament_runs_v060 SET report_json='{}'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM tournament_runs_v060")

    def test_primary_kind_and_idempotency_constraints_are_enforced(self):
        with closing(sqlite3.connect(self.path)) as connection:
            row = ("local-default", "task_belief_" + "a" * 32, "task_belief", "v0.6.0", "{}", "a" * 64, "2026-08-09T12:00:00Z")
            connection.execute("INSERT INTO decision_objects_v060 VALUES (?,?,?,?,?,?,?)", row)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO decision_objects_v060 VALUES (?,?,?,?,?,?,?)", row)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO decision_objects_v060 VALUES (?,?,?,?,?,?,?)",
                    ("local-default", "bad_" + "b" * 32, "unknown", "v0.6.0", "{}", "b" * 64, "2026-08-09T12:00:00Z"),
                )
            tournament = ("local-default", "tournament_1", "idem_1", "dataset_1", "b" * 64, "{}", "c" * 64, "2026-08-09T12:00:00Z")
            connection.execute("INSERT INTO tournament_runs_v060 VALUES (?,?,?,?,?,?,?,?)", tournament)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO tournament_runs_v060 VALUES (?,?,?,?,?,?,?,?)",
                    ("local-default", "tournament_2", "idem_1", "dataset_2", "d" * 64, "{}", "e" * 64, "2026-08-09T12:00:01Z"),
                )


class DecisionIntelligenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.store = DecisionIntelligenceStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _payload(index: int = 1) -> dict[str, object]:
        return {"value": index, "kind": "task"}

    def test_record_get_list_and_counts_are_project_scoped(self):
        self.assertTrue(
            self.store.record_object(
                project_id="Project_A",
                object_kind="task_belief",
                object_id="object_0001",
                payload=self._payload(),
                recorded_at="2026-08-09T12:00:00+08:00",
            )
        )
        item = self.store.object(project_id="project_a", object_id="object_0001")
        self.assertEqual({"value": 1, "kind": "task"}, item["payload"])
        self.assertIsNone(self.store.object(project_id="project_b", object_id="object_0001"))
        self.assertEqual(1, len(self.store.list_objects(project_id="project_a")))
        self.assertEqual({"decision_objects_v060": 1, "tournament_runs_v060": 0}, self.store.counts())

    def test_same_object_content_is_idempotent_and_different_content_conflicts(self):
        kwargs = {
            "project_id": "project_a",
            "object_kind": "task_belief",
            "object_id": "object_0001",
            "payload": self._payload(),
            "recorded_at": "2026-08-09T12:00:00Z",
        }
        self.assertTrue(self.store.record_object(**kwargs))
        self.assertFalse(self.store.record_object(**kwargs))
        with self.assertRaises(DecisionIntelligenceStoreError):
            self.store.record_object(**{**kwargs, "payload": self._payload(2)})

    def test_kind_filter_limit_and_invalid_inputs(self):
        for index, kind in enumerate(("task_belief", "system_belief", "decision_trace"), start=1):
            self.store.record_object(
                project_id="project_a",
                object_kind=kind,
                object_id=f"object_{index:04d}",
                payload=self._payload(index),
                recorded_at=f"2026-08-09T12:00:0{index}Z",
            )
        self.assertEqual(1, len(self.store.list_objects(project_id="project_a", object_kind="system_belief")))
        with self.assertRaises(ValueError):
            self.store.list_objects(project_id="project_a", limit=0)
        with self.assertRaises(DecisionIntelligenceStoreError):
            self.store.list_objects(project_id="project_a", object_kind="unknown")

    def test_tournament_idempotency_and_key_conflicts(self):
        kwargs = {
            "project_id": "project_a",
            "tournament_id": "tournament_0001",
            "idempotency_key": "idempotency_01",
            "dataset_id": "dataset_01",
            "manifest_hash": "a" * 64,
            "report": {"accepted": 1},
            "created_at": "2026-08-09T12:00:00Z",
        }
        first, replayed = self.store.record_tournament(**kwargs)
        self.assertFalse(replayed)
        second, replayed = self.store.record_tournament(**kwargs)
        self.assertTrue(replayed)
        self.assertEqual(first, second)
        with self.assertRaises(DecisionIntelligenceStoreError):
            self.store.record_tournament(**{**kwargs, "report": {"accepted": 0}})

    def test_tournament_project_isolation_and_limit(self):
        base = {
            "tournament_id": "tournament_0001",
            "idempotency_key": "idempotency_01",
            "dataset_id": "dataset_01",
            "manifest_hash": "a" * 64,
            "report": {"accepted": 1},
            "created_at": "2026-08-09T12:00:00Z",
        }
        self.store.record_tournament(project_id="project_a", **base)
        self.store.record_tournament(project_id="project_b", **base)
        self.assertEqual(1, len(self.store.tournaments(project_id="project_a")))
        self.assertIsNone(self.store.tournament(project_id="project_b", tournament_id="tournament_missing"))
        with self.assertRaises(ValueError):
            self.store.tournaments(project_id="project_a", limit=101)

    def test_tournament_id_reuse_with_different_key_is_conflict(self):
        base = {
            "project_id": "project_a",
            "tournament_id": "tournament_0001",
            "idempotency_key": "idempotency_01",
            "dataset_id": "dataset_01",
            "manifest_hash": "a" * 64,
            "report": {"accepted": 1},
            "created_at": "2026-08-09T12:00:00Z",
        }
        self.store.record_tournament(**base)
        with self.assertRaises(DecisionIntelligenceStoreError):
            self.store.record_tournament(**{**base, "idempotency_key": "idempotency_02"})

    def test_invalid_kind_and_project_are_rejected(self):
        with self.assertRaises(DecisionIntelligenceStoreError):
            self.store.record_object(
                project_id="project_a",
                object_kind="unknown",
                object_id="object_0001",
                payload={},
                recorded_at="2026-08-09T12:00:00Z",
            )
        with self.assertRaises(ValueError):
            self.store.list_objects(project_id="bad project")

    def test_counts_include_tournament_rows(self):
        self.store.record_tournament(
            project_id="project_a",
            tournament_id="tournament_0001",
            idempotency_key="idempotency_01",
            dataset_id="dataset_01",
            manifest_hash="a" * 64,
            report={"accepted": 1},
            created_at="2026-08-09T12:00:00Z",
        )
        self.assertEqual({"decision_objects_v060": 0, "tournament_runs_v060": 1}, self.store.counts())


class ControlKernelDecisionIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.kernel = ControlKernel(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_kernel_owns_store_on_same_runtime_and_merges_counts(self):
        self.assertEqual(self.kernel.store.path, self.kernel.decision_intelligence_store.path)
        self.kernel.decision_intelligence_store.record_object(
            project_id="project_a",
            object_kind="task_belief",
            object_id="object_0001",
            payload={"value": 1},
            recorded_at="2026-08-09T12:00:00Z",
        )
        health = self.kernel.health()
        self.assertEqual(1, health["decision_objects_v060"])
        self.assertEqual(0, health["tournament_runs_v060"])

    def test_kernel_does_not_create_a_second_database(self):
        self.assertEqual(self.kernel.store.path.resolve(), self.kernel.decision_intelligence_store.path.resolve())
        self.assertEqual(35, self.kernel.health()["migrations"])

    def test_payload_must_be_finite_json_and_ids_are_bounded(self):
        with self.assertRaises(DecisionIntelligenceStoreError):
            self.kernel.decision_intelligence_store.record_object(
                project_id="project_a",
                object_kind="task_belief",
                object_id="object_0001",
                payload={"bad": float("nan")},
                recorded_at="2026-08-09T12:00:00Z",
            )
        with self.assertRaises(ValueError):
            self.kernel.decision_intelligence_store.record_object(
                project_id="project_a",
                object_kind="task_belief",
                object_id="short",
                payload={},
                recorded_at="2026-08-09T12:00:00Z",
            )


class RoutingContractTests(unittest.TestCase):
    @staticmethod
    def _hash(text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def _record(
        cls,
        *,
        source_record_id: str = "record_0001",
        case_id: str = "case-1",
        model_id: str = "model-a",
        partition: str = "test",
        prompt_hash: str | None = None,
        price_snapshot_id: str = "price-1",
        latency_ms: int | None = 120,
    ) -> RoutingRecordV1:
        return RoutingRecordV1(
            source_record_id=source_record_id,
            case_id=case_id,
            partition=partition,
            task_family="code",
            prompt_hash=prompt_hash or cls._hash(case_id),
            model_id=model_id,
            response_hash=cls._hash(source_record_id),
            quality_score=0.8,
            accepted=True,
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.01,
            latency_ms=latency_ms,
            source_dataset="seed",
            source_version="v1",
            price_snapshot_id=price_snapshot_id,
            evidence_level="E0-simulated",
            observed_at="2026-08-09T12:00:00Z",
        )

    @classmethod
    def _records(cls):
        return (
            cls._record(source_record_id="record_0002", model_id="model-b"),
            cls._record(source_record_id="record_0001", model_id="model-a"),
        )

    def test_record_round_trip_and_validation(self):
        item = self._record()
        self.assertEqual(item, RoutingRecordV1.from_dict(item.to_dict()))
        self.assertEqual(2, len(validate_routing_records(self._records())))

    def test_quality_tokens_cost_latency_and_hashes_are_strict(self):
        with self.assertRaises(ValueError):
            replace(self._record(), quality_score=1.1)
        with self.assertRaises(TypeError):
            replace(self._record(), input_tokens=True)
        with self.assertRaises(ValueError):
            replace(self._record(), cost_usd=-1)
        with self.assertRaises(ValueError):
            replace(self._record(), latency_ms=-1)
        with self.assertRaises(ValueError):
            replace(self._record(), prompt_hash="bad")

    def test_empty_and_oversized_record_sets_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_routing_records(())
        with self.assertRaises(ValueError):
            validate_routing_records([self._record()] * 1_000_001)

    def test_duplicate_source_id_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_routing_records((self._record(), self._record(model_id="model-b")))

    def test_duplicate_case_model_pair_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_routing_records((self._record(), self._record(source_record_id="record_0002")))

    def test_same_case_different_prompt_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_routing_records((self._record(), self._record(source_record_id="record_0002", model_id="model-b", prompt_hash=self._hash("other"))))

    def test_prompt_hash_cross_partition_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_routing_records((self._record(), self._record(source_record_id="record_0002", model_id="model-b", partition="train")))

    def test_case_requires_two_models(self):
        with self.assertRaises(ValueError):
            validate_routing_records((self._record(),))

    def test_price_snapshot_must_be_consistent(self):
        with self.assertRaises(ValueError):
            validate_routing_records((self._record(), self._record(source_record_id="record_0002", model_id="model-b", price_snapshot_id="price-2")))

    def test_records_are_sorted_by_partition_case_model_and_source(self):
        ordered = validate_routing_records(
            (
                self._record(source_record_id="record_0002", model_id="model-b", partition="train"),
                self._record(source_record_id="record_0001", model_id="model-a", partition="train"),
            )
        )
        self.assertEqual(("record_0001", "record_0002"), tuple(item.source_record_id for item in ordered))

    def test_manifest_round_trip_and_time_independent_id(self):
        manifest = build_dataset_manifest(
            self._records(), source_uri="local://seed.json", license_id="MIT", created_at="2026-08-09T12:00:00Z"
        )
        self.assertEqual(manifest, DatasetManifestV1.from_dict(manifest.to_dict()))
        later = build_dataset_manifest(
            self._records(), source_uri="local://seed.json", license_id="MIT", created_at="2026-08-09T13:00:00Z"
        )
        self.assertEqual(manifest.dataset_id, later.dataset_id)

    def test_manifest_rejects_mixed_dataset_version_or_evidence(self):
        with self.assertRaises(ValueError):
            build_dataset_manifest(
                (self._record(), replace(self._record(source_record_id="record_0002", model_id="model-b"), source_version="v2")),
                source_uri="local://seed.json", license_id="MIT", created_at="2026-08-09T12:00:00Z"
            )
        with self.assertRaises(ValueError):
            build_dataset_manifest(
                (self._record(), replace(self._record(source_record_id="record_0002", model_id="model-b"), evidence_level="E1-public-replay")),
                source_uri="local://seed.json", license_id="MIT", created_at="2026-08-09T12:00:00Z"
            )

    def test_case_view_is_blind_and_round_trips(self):
        view = CaseViewV1(
            case_id="case-1",
            task_family="code",
            prompt_hash=self._hash("case-1"),
            model_ids=("model-b", "model-a"),
        )
        payload = view.to_dict()
        self.assertEqual(view, CaseViewV1.from_dict(payload))
        for forbidden in ("quality_score", "accepted", "response_hash", "ground_truth", "input_tokens", "output_tokens", "cost_usd", "latency_ms"):
            self.assertNotIn(forbidden, payload)

    def test_invalid_partition_and_extra_blind_field_are_rejected(self):
        with self.assertRaises(ValueError):
            replace(self._record(), partition="invalid")
        payload = CaseViewV1("case-1", "code", self._hash("case-1"), ("model-a", "model-b")).to_dict()
        payload["quality_score"] = 0.9
        with self.assertRaises(ValueError):
            CaseViewV1.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
