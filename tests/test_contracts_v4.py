import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from costweave.compat_v4 import (
    clear_trace,
    contracts_v4_enabled,
    set_run_status,
    set_task_status,
    trace_snapshot,
    v3_snapshot_to_v4,
    v4_snapshot_to_v3,
)
from costweave.contracts_v4 import (
    CONTRACT_SCHEMA_VERSION,
    CommandEnvelope,
    ContractError,
    EventEnvelope,
    SchemaVersionError,
    canonical_identifier,
    new_artifact_id,
    new_event_id,
    new_run_id,
    new_task_id,
    validate_identifier,
)
from costweave.domain import RunRequest, RunStatus, TaskStatus
from costweave.engine import OrchestrationEngine, RunRecord
from costweave.state_guard import (
    InvalidTransitionError,
    StateKind,
    UnknownStateError,
    is_terminal,
    validate_transition,
)


class ContractIdentifierTests(unittest.TestCase):
    def test_all_public_identifiers_have_valid_kind_and_shape(self):
        generated = {
            "run": str(new_run_id()),
            "task": str(new_task_id()),
            "event": str(new_event_id()),
            "artifact": str(new_artifact_id()),
        }
        for kind, value in generated.items():
            with self.subTest(kind=kind):
                self.assertEqual(value, validate_identifier(value, kind))
                self.assertEqual(36 + len(kind) - 3, len(value))

    def test_malformed_and_wrong_kind_identifiers_are_rejected(self):
        with self.assertRaises(ContractError):
            validate_identifier("run_not-a-uuid", "run")
        with self.assertRaises(ContractError):
            validate_identifier(str(new_run_id()), "task")

    def test_legacy_identifier_canonicalization_is_stable(self):
        first = canonical_identifier("run", "legacy-run")
        second = canonical_identifier("run", "legacy-run")
        other = canonical_identifier("run", "other-run")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(first, validate_identifier(first, "run"))


class EnvelopeTests(unittest.TestCase):
    def test_command_envelope_json_round_trip(self):
        envelope = CommandEnvelope.create(
            str(new_run_id()),
            "run.create",
            {"goal": "分析产品", "limits": [1, 2]},
            task_id=str(new_task_id()),
        )
        restored = CommandEnvelope.from_json(envelope.to_json())
        self.assertEqual(envelope, restored)
        self.assertEqual(CONTRACT_SCHEMA_VERSION, restored.schema_version)
        self.assertEqual([1, 2], restored.to_dict()["payload"]["limits"])

    def test_event_envelope_json_round_trip(self):
        envelope = EventEnvelope.create(
            str(new_run_id()),
            "state.transition.accepted",
            "tests",
            {"current": "created", "target": "planning"},
        )
        restored = EventEnvelope.from_json(envelope.to_json())
        self.assertEqual(envelope, restored)
        self.assertEqual(envelope.to_dict(), json.loads(envelope.to_json()))

    def test_wrong_schema_version_is_rejected(self):
        payload = EventEnvelope.create(
            str(new_run_id()),
            "state.transition.accepted",
            "tests",
        ).to_dict()
        payload["schema_version"] = "v9"
        with self.assertRaises(SchemaVersionError):
            EventEnvelope.from_dict(payload)

    def test_unknown_fields_are_rejected_instead_of_silently_lost(self):
        payload = CommandEnvelope.create(
            str(new_run_id()),
            "run.create",
        ).to_dict()
        payload["unexpected"] = True
        with self.assertRaises(ContractError):
            CommandEnvelope.from_dict(payload)

    def test_payload_is_immutable_and_json_finite(self):
        envelope = EventEnvelope.create(
            str(new_run_id()),
            "state.transition.accepted",
            "tests",
            {"nested": {"items": [1, 2]}},
        )
        with self.assertRaises(TypeError):
            envelope.payload["new"] = "value"
        with self.assertRaises(ContractError):
            EventEnvelope.create(
                str(new_run_id()),
                "state.transition.accepted",
                "tests",
                {"score": float("nan")},
            )


class StateGuardTests(unittest.TestCase):
    def test_legal_run_transition_returns_deterministic_decision(self):
        decision = validate_transition(RunStatus.CREATED, RunStatus.PLANNING)
        self.assertEqual(StateKind.RUN, decision.state_kind)
        self.assertEqual("created", decision.current)
        self.assertEqual("planning", decision.target)
        self.assertTrue(decision.changed)

    def test_same_state_is_an_idempotent_noop(self):
        decision = validate_transition(TaskStatus.PENDING, TaskStatus.PENDING)
        self.assertEqual(StateKind.TASK, decision.state_kind)
        self.assertFalse(decision.changed)

    def test_terminal_state_rollback_is_rejected(self):
        self.assertTrue(is_terminal(RunStatus.COMPLETED))
        self.assertTrue(is_terminal(TaskStatus.VALIDATED))
        with self.assertRaises(InvalidTransitionError):
            validate_transition(RunStatus.COMPLETED, RunStatus.EXECUTING)
        with self.assertRaises(InvalidTransitionError):
            validate_transition(TaskStatus.VALIDATED, TaskStatus.PENDING)

    def test_unknown_state_and_cross_domain_target_are_rejected(self):
        with self.assertRaises(UnknownStateError):
            validate_transition("missing", "planning")
        with self.assertRaises(UnknownStateError):
            validate_transition(RunStatus.CREATED, TaskStatus.PENDING)

    def test_replan_task_transitions_are_explicitly_supported(self):
        self.assertTrue(
            validate_transition(TaskStatus.REJECTED, TaskStatus.INVALIDATED).changed
        )
        self.assertTrue(
            validate_transition(TaskStatus.REJECTED, TaskStatus.PENDING).changed
        )


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        clear_trace()

    def tearDown(self):
        clear_trace()

    def test_v3_snapshot_round_trip_is_lossless(self):
        legacy = {
            "id": "legacy-run",
            "status": "created",
            "events": [],
            "metrics": {"spent": 0.0},
        }
        adapted = v3_snapshot_to_v4(legacy)
        self.assertEqual(CONTRACT_SCHEMA_VERSION, adapted["contract"]["schema_version"])
        self.assertFalse(adapted["contract"]["trace_persistent"])
        self.assertEqual(legacy, v4_snapshot_to_v3(adapted))

    def test_run_record_snapshot_adds_contract_only_when_enabled(self):
        record = RunRecord(
            id="legacy-run",
            request=RunRequest("分析一个产品需求").normalized(),
        )
        with patch.dict(os.environ, {"COSTWEAVE_CONTRACTS_V4": "1"}):
            self.assertIn("contract", record.snapshot())
        with patch.dict(os.environ, {"COSTWEAVE_CONTRACTS_V4": "0"}):
            self.assertNotIn("contract", record.snapshot())

    def test_accepted_and_rejected_transitions_record_source(self):
        record = SimpleNamespace(id="legacy-run", status=RunStatus.CREATED)
        with patch.dict(os.environ, {"COSTWEAVE_CONTRACTS_V4": "1"}):
            set_run_status(record, RunStatus.PLANNING, source="tests.accepted")
            with self.assertRaises(InvalidTransitionError):
                set_run_status(record, RunStatus.COMPLETED, source="tests.rejected")
        trace = trace_snapshot("legacy-run")
        self.assertEqual(2, len(trace))
        self.assertTrue(trace[0]["payload"]["accepted"])
        self.assertEqual("tests.accepted", trace[0]["source"])
        self.assertFalse(trace[1]["payload"]["accepted"])
        self.assertIn("illegal run transition", trace[1]["payload"]["reason"])

    def test_task_transition_trace_uses_canonical_task_id(self):
        record = SimpleNamespace(id="legacy-run")
        task = SimpleNamespace(id="semantic-task", status=TaskStatus.PENDING)
        with patch.dict(os.environ, {"COSTWEAVE_CONTRACTS_V4": "1"}):
            set_task_status(
                record,
                task,
                TaskStatus.RUNNING,
                source="tests.task",
            )
        trace = trace_snapshot("legacy-run")
        self.assertEqual(
            canonical_identifier("task", "semantic-task"),
            trace[0]["task_id"],
        )

    def test_disabled_feature_preserves_v3_direct_assignment_behavior(self):
        record = SimpleNamespace(id="legacy-run", status=RunStatus.CREATED)
        with patch.dict(os.environ, {"COSTWEAVE_CONTRACTS_V4": "0"}):
            self.assertFalse(contracts_v4_enabled())
            set_run_status(record, RunStatus.COMPLETED, source="tests.disabled")
        self.assertEqual(RunStatus.COMPLETED, record.status)
        self.assertEqual((), trace_snapshot())


class FeatureFlagEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_feature_keeps_complete_v3_run_flow(self):
        clear_trace()
        record = RunRecord(
            id="legacy-engine-run",
            request=RunRequest(
                "分析一个产品需求并形成报告",
                budget=2.0,
                max_concurrency=3,
            ).normalized(),
        )
        with patch.dict(os.environ, {"COSTWEAVE_CONTRACTS_V4": "0"}):
            await OrchestrationEngine().run(record)
            snapshot = record.snapshot()
        self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
        self.assertNotIn("contract", snapshot)
        self.assertEqual((), trace_snapshot())


if __name__ == "__main__":
    unittest.main()
