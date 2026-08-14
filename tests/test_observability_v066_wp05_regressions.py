import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from costweave.domain import Event, RunRequest, RunStatus
from costweave.control_kernel import ControlKernel
from costweave.engine import RunManager, RunRecord
from costweave.observability import get_trace_recorder, provider_call_span
from costweave.release_readiness import build_release_status
from costweave.server import CostWeaveHandler


class IdempotencyTraceRegistryRegressionTests(unittest.TestCase):
    def test_idempotent_create_keeps_provider_spans_on_live_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = RunManager(
                runtime_db_path=str(Path(temporary) / "runtime.sqlite3"),
                recover=False,
            )
            try:
                request = RunRequest(
                    goal="verify idempotent trace registry ownership",
                    budget=1,
                    max_concurrency=1,
                )
                first = manager.create(
                    request,
                    idempotency_key="wp05-regression-idempotent-create",
                )
                replayed = manager.create(
                    request,
                    idempotency_key="wp05-regression-idempotent-create",
                )

                self.assertIs(first, replayed)
                self.assertIs(
                    first.telemetry,
                    get_trace_recorder(first.trace_id),
                )

                before = sum(
                    span["stage"] == "provider_call"
                    for span in manager.trace(first.id)["spans"]
                )
                with provider_call_span(
                    first.trace_id,
                    backend_id="qa-backend",
                    provider="qa-provider",
                    model="qa-model",
                    request_id="qa-request",
                ):
                    pass
                after = sum(
                    span["stage"] == "provider_call"
                    for span in manager.trace(first.id)["spans"]
                )
                self.assertEqual(before + 1, after)
            finally:
                manager.shutdown(wait_timeout=10)

    def test_concurrent_idempotent_create_has_one_authoritative_recorder(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = RunManager(
                runtime_db_path=str(Path(temporary) / "runtime.sqlite3"),
                recover=False,
            )
            request = RunRequest(
                goal="verify concurrent idempotent trace ownership",
                budget=1,
                max_concurrency=1,
            )
            original_create_run = manager.control_kernel.create_run
            after_kernel = threading.Barrier(2)

            def coordinated_create_run(*args, **kwargs):
                result = original_create_run(*args, **kwargs)
                after_kernel.wait(timeout=10)
                if not result[1]:
                    # Force the replayed caller to enter RunManager's lock
                    # first; this was the registry/record mismatch ordering.
                    time.sleep(0.1)
                return result

            try:
                with (
                    patch.object(
                        manager.control_kernel,
                        "create_run",
                        side_effect=coordinated_create_run,
                    ),
                    patch.object(manager, "_start") as start,
                    ThreadPoolExecutor(max_workers=2) as executor,
                ):
                    futures = [
                        executor.submit(
                            manager.create,
                            request,
                            idempotency_key="wp05-concurrent-idempotency",
                        )
                        for _ in range(2)
                    ]
                    first, second = [future.result(timeout=15) for future in futures]

                self.assertIs(first, second)
                self.assertIs(first, manager.records[first.id])
                self.assertIs(first.telemetry, get_trace_recorder(first.trace_id))
                start.assert_called_once_with(first, resume=False)
            finally:
                manager.shutdown(wait_timeout=10)


class RestartTraceTimingRegressionTests(unittest.TestCase):
    def test_replayed_children_remain_inside_root_lifetime(self):
        record = RunRecord(
            id="run_wp05_timing_regression",
            request=RunRequest(goal="verify replayed trace timing"),
            status=RunStatus.COMPLETED,
            created_at="2026-08-14T00:00:00Z",
            finished_at="2026-08-14T00:00:10Z",
            events=[
                Event(
                    at="2026-01-01T00:00:05Z",
                    kind="task_validated",
                    message="historical event",
                    task_id="task-timing",
                    detail={"passed": True},
                )
            ],
            register_telemetry_on_init=False,
        )

        exported = record.telemetry.export()
        root = next(
            span for span in exported["spans"]
            if span["span_id"] == exported["root_span_id"]
        )
        children = [
            span for span in exported["spans"]
            if span["span_id"] != exported["root_span_id"]
        ]

        self.assertEqual("2026-01-01T00:00:05Z", root["start_time"])
        self.assertEqual("2026-08-14T00:00:10Z", root["end_time"])
        self.assertTrue(children)
        for child in children:
            self.assertGreaterEqual(child["start_time"], root["start_time"])
            self.assertLessEqual(child["end_time"], root["end_time"])


class StrictJsonBoundaryRegressionTests(unittest.TestCase):
    def test_server_rejects_non_finite_json_numbers(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    CostWeaveHandler._validate_json_shape({"value": value})

    def test_release_gates_execute_observability_security_self_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            kernel = ControlKernel(Path(temporary) / "runtime.sqlite3")
            status = build_release_status(kernel)
        gates = {item["gate_id"]: item for item in status["gates"]}
        for gate_id in (
            "otel-redaction-self-test",
            "otel-strict-json-self-test",
            "otel-deep-immutability-self-test",
        ):
            self.assertIn(gate_id, gates)
            self.assertTrue(gates[gate_id]["passed"], gates[gate_id])
        self.assertTrue(status["observability_audit"]["healthy"])


if __name__ == "__main__":
    unittest.main()
