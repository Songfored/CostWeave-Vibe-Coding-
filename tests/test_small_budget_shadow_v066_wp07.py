"""Acceptance tests for V066-WP07 small-budget Shadow Runtime."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from costweave.adapters.http import AdapterRuntimePolicy
from costweave.engine import RunManager
from costweave.event_store import EventStore
from costweave.experimental_calls import (
    ExperimentalCallStore,
    ExternalIdempotencyError,
)
from costweave.planning_contracts import WorkOrder
from costweave.server import build_server
from costweave.small_budget_shadow import (
    ShadowExperimentConfigV1,
    ShadowProbeMeasurementV1,
    ShadowProbeRequestV1,
    SmallBudgetShadowError,
    SmallBudgetShadowNotFoundError,
    SmallBudgetShadowRuntime,
    public_small_budget_shadow_policy,
)


def expiry() -> str:
    return (
        datetime.now(UTC) + timedelta(hours=1)
    ).isoformat().replace("+00:00", "Z")


def work_order(*, task_id: str = "task_wp07", key: str = "work-wp07-001") -> WorkOrder:
    return WorkOrder(
        work_order_id="work_order_wp07",
        task_id=task_id,
        objective="Classify a bounded synthetic fixture.",
        dependencies=(),
        input_artifacts=(),
        context_manifest=(),
        assumptions=("The fixture contains no personal data.",),
        allowed_tools=(),
        forbidden_actions=("Do not call tools.",),
        output_schema={"type": "object", "required": ["label"]},
        acceptance_tests=("label is present",),
        cost_limit=0.10,
        time_limit_ms=5_000,
        escalation_conditions=("input is ambiguous",),
        idempotency_key=key,
    )


def config(**changes) -> ShadowExperimentConfigV1:
    values = {
        "idempotency_key": "wp07-config-001",
        "provider": "fixture-provider",
        "model_id": "fixture-model",
        "deployment": "local",
        "data_scope": "metrics_only",
        "hard_budget_usd": 0.20,
        "provider_budget_usd": 0.20,
        "max_cost_per_probe_usd": 0.05,
        "max_logical_calls": 2,
        "max_attempts_per_probe": 1,
        "max_output_tokens_per_probe": 512,
        "expires_at": expiry(),
        "project_id": "local-default",
        "live_calls_requested": True,
        "network_consent": True,
    }
    values.update(changes)
    return ShadowExperimentConfigV1(**values)


def probe(item: ShadowExperimentConfigV1, *, key: str = "wp07-probe-001") -> ShadowProbeRequestV1:
    return ShadowProbeRequestV1(
        experiment_id=item.experiment_id,
        project_id=item.project_id,
        work_order=work_order(key=key),
        baseline_route_id="baseline-route",
        baseline_route_fingerprint="a" * 64,
        max_output_tokens=256,
        idempotency_key=key,
    )


class FixtureExecutor:
    def __init__(
        self,
        *,
        fail: bool = False,
        output_hash: str | None = None,
        output_tokens: int = 20,
    ):
        self.fail = fail
        self.output_hash = output_hash
        self.output_tokens = output_tokens
        self.calls = 0

    def execute(self, config, request):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic provider detail must not escape")
        return ShadowProbeMeasurementV1(
            accepted=True,
            actual_cost_usd=0.02,
            input_tokens=100,
            output_tokens=self.output_tokens,
            cached_input_tokens=0,
            latency_ms=40,
            output_hash=self.output_hash,
            evidence_level="E0-fixture",
        )


class SmallBudgetShadowContractTests(unittest.TestCase):
    def test_config_is_bounded_strict_and_publicly_redacted(self):
        item = config(
            provider="remote-fixture",
            deployment="remote",
            secret_env="WP07_SYNTHETIC_API_KEY",
            paid_consent=True,
            data_scope="metrics_and_output_hash",
        )
        public = json.dumps(item.to_public_dict(), sort_keys=True)
        self.assertNotIn("WP07_SYNTHETIC_API_KEY", public)
        self.assertNotIn("idempotency_key", public)
        self.assertTrue(item.to_public_dict()["secret_configured"])
        self.assertEqual(5.0, public_small_budget_shadow_policy()["limits"]["maximum_total_budget_usd"])
        with self.assertRaises(SmallBudgetShadowError):
            config(hard_budget_usd=5.01, provider_budget_usd=5.01)
        with self.assertRaises(SmallBudgetShadowError):
            config(hard_budget_usd=float("nan"))
        with self.assertRaises(SmallBudgetShadowError):
            config(model_id="sk-synthetic-secret-12345678")
        with self.assertRaises(SmallBudgetShadowError):
            ShadowExperimentConfigV1.from_dict(
                {**item.fingerprint_payload(), "idempotency_key": "k" * 8, "unknown": True}
            )


class SmallBudgetShadowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime.sqlite3"
        EventStore(self.path)
        self.store = ExperimentalCallStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_policy_blocks_before_reservation(self):
        executor = FixtureExecutor()
        runtime = SmallBudgetShadowRuntime(
            self.store,
            executor=executor,
            adapter_policy=AdapterRuntimePolicy(False, False),
        )
        item = config()
        session = runtime.configure(item)
        self.assertFalse(session.armed)
        self.assertEqual("experimental_calls_disabled", session.block_reason)
        result = runtime.probe(probe(item))
        self.assertEqual("blocked", result.status)
        self.assertTrue(result.baseline_execution_continues)
        self.assertFalse(result.actual_route_changed)
        self.assertEqual(0, result.external_call_count)
        self.assertEqual(0, executor.calls)
        self.assertEqual(0, self.store.counts()["external_calls"])

    def test_enabled_fixture_executes_once_and_replays_metrics_only(self):
        executor = FixtureExecutor()
        runtime = SmallBudgetShadowRuntime(
            self.store,
            executor=executor,
            adapter_policy=AdapterRuntimePolicy(True, False),
        )
        item = config()
        runtime.configure(item)
        request = probe(item)
        first = runtime.probe(request)
        second = runtime.probe(request)
        self.assertEqual("completed", first.status)
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(1, executor.calls)
        self.assertEqual(1, first.external_call_count)
        self.assertEqual(0, second.external_call_count)
        stored = self.store.call(first.external_call_id)
        encoded = json.dumps(stored, sort_keys=True)
        self.assertNotIn(request.work_order.objective, encoded)
        self.assertNotIn("output", stored["result"])
        self.assertFalse(stored["telemetry"]["raw_prompt_persisted"])
        self.assertFalse(stored["telemetry"]["raw_output_persisted"])
        self.assertEqual(0.02, runtime.summary(item.experiment_id, project_id=item.project_id)["budget"]["actual_cost_usd"])

    def test_project_scope_and_configuration_idempotency_fail_closed(self):
        runtime = SmallBudgetShadowRuntime(self.store)
        item = config()
        first = runtime.configure(item)
        replay = runtime.configure(item)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(SmallBudgetShadowNotFoundError):
            runtime.session(item.experiment_id, project_id="other-project")
        with self.assertRaises(ExternalIdempotencyError):
            runtime.configure(replace(item, model_id="changed-model"))

        remote = config(
            idempotency_key="wp07-secret-reference-key",
            provider="remote-fixture",
            deployment="remote",
            secret_env="WP07_FIRST_SECRET_REFERENCE",
            paid_consent=True,
        )
        runtime.configure(remote)
        with self.assertRaises(ExternalIdempotencyError):
            runtime.configure(
                replace(remote, secret_env="WP07_SECOND_SECRET_REFERENCE")
            )

    def test_remote_session_requires_secret_presence_before_arming(self):
        name = "WP07_SYNTHETIC_REMOTE_KEY"
        previous = os.environ.pop(name, None)
        try:
            executor = FixtureExecutor(output_hash="c" * 64)
            runtime = SmallBudgetShadowRuntime(
                self.store,
                executor=executor,
                adapter_policy=AdapterRuntimePolicy(True, True),
            )
            item = config(
                provider="remote-fixture",
                deployment="remote",
                secret_env=name,
                paid_consent=True,
                data_scope="metrics_and_output_hash",
            )
            blocked = runtime.configure(item)
            self.assertFalse(blocked.armed)
            self.assertEqual("secret_unavailable", blocked.block_reason)
            os.environ[name] = "synthetic-value-never-persisted"
            armed = runtime.configure(item)
            self.assertTrue(armed.armed)
            result = runtime.probe(probe(item))
            self.assertEqual("completed", result.status)
            persisted = json.dumps(
                self.store.call(result.external_call_id), sort_keys=True
            )
            self.assertNotIn(os.environ[name], persisted)
            self.assertNotIn(name, persisted)
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def test_secret_prompt_and_output_token_overrun_fail_closed(self):
        item = config()
        bad_order = replace(
            work_order(), objective="Bearer synthetic-secret-token-value"
        )
        with self.assertRaises(SmallBudgetShadowError):
            ShadowProbeRequestV1(
                experiment_id=item.experiment_id,
                project_id=item.project_id,
                work_order=bad_order,
                baseline_route_id="baseline-route",
                baseline_route_fingerprint="a" * 64,
                max_output_tokens=256,
                idempotency_key="wp07-secret-probe",
            )

        executor = FixtureExecutor(output_tokens=300)
        runtime = SmallBudgetShadowRuntime(
            self.store,
            executor=executor,
            adapter_policy=AdapterRuntimePolicy(True, False),
        )
        runtime.configure(item)
        result = runtime.probe(probe(item, key="wp07-token-overrun"))
        self.assertEqual("failed", result.status)
        self.assertEqual("shadow_executor_failed", result.reason)
        self.assertIsNone(result.measurement)
        self.assertEqual(
            0.02,
            runtime.summary(
                item.experiment_id, project_id=item.project_id
            )["budget"]["actual_cost_usd"],
        )

    def test_executor_failure_is_isolated_and_budget_limit_blocks_later_probe(self):
        executor = FixtureExecutor(fail=True)
        runtime = SmallBudgetShadowRuntime(
            self.store,
            executor=executor,
            adapter_policy=AdapterRuntimePolicy(True, False),
        )
        item = config(max_logical_calls=1)
        runtime.configure(item)
        failed = runtime.probe(probe(item, key="wp07-probe-fail"))
        self.assertEqual("failed", failed.status)
        self.assertEqual("shadow_executor_failed", failed.reason)
        self.assertNotIn("synthetic provider", json.dumps(failed.to_dict()))
        self.assertTrue(failed.baseline_execution_continues)
        self.assertEqual(
            item.max_cost_per_probe_usd,
            runtime.summary(
                item.experiment_id, project_id=item.project_id
            )["budget"]["actual_cost_usd"],
        )
        blocked = runtime.probe(probe(item, key="wp07-probe-later"))
        self.assertEqual("blocked", blocked.status)
        self.assertEqual("hard_budget_exceeded", blocked.reason)
        self.assertEqual(1, executor.calls)


class SmallBudgetShadowHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        self.server = build_server("127.0.0.1", 0, manager=self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method, path, payload=None, *, key=None, project="project-wp07"):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"X-CostWeave-Project": project}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_http_config_is_scoped_redacted_and_probe_is_default_blocked(self):
        status, policy = self.request("GET", "/api/v1/runtime/shadow/status")
        self.assertEqual(200, status)
        self.assertFalse(policy["policy"]["live_calls_enabled_by_default"])
        self.assertEqual(0, policy["runtime_counts"]["external_calls"])

        payload = {
            "provider": "remote-fixture",
            "model_id": "fixture-model",
            "deployment": "remote",
            "data_scope": "metrics_only",
            "hard_budget_usd": 0.20,
            "provider_budget_usd": 0.20,
            "max_cost_per_probe_usd": 0.05,
            "max_logical_calls": 2,
            "max_attempts_per_probe": 1,
            "max_output_tokens_per_probe": 512,
            "expires_at": expiry(),
            "secret_env": "WP07_HTTP_SYNTHETIC_KEY",
            "live_calls_requested": True,
            "network_consent": True,
            "paid_consent": True,
        }
        status, session = self.request(
            "POST",
            "/api/v1/runtime/shadow/experiments",
            payload,
            key="wp07-http-config",
        )
        self.assertEqual(201, status)
        self.assertFalse(session["armed"])
        self.assertNotIn("secret_env", session)
        self.assertNotIn("WP07_HTTP_SYNTHETIC_KEY", json.dumps(session))
        experiment_id = session["experiment_id"]

        status, own = self.request(
            "GET", f"/api/v1/runtime/shadow/experiments/{experiment_id}"
        )
        self.assertEqual(200, status)
        self.assertEqual(experiment_id, own["session"]["experiment_id"])
        status, _ = self.request(
            "GET",
            f"/api/v1/runtime/shadow/experiments/{experiment_id}",
            project="foreign-project",
        )
        self.assertEqual(404, status)

        probe_payload = {
            "work_order": work_order().to_dict(),
            "baseline_route_id": "baseline-route",
            "baseline_route_fingerprint": "b" * 64,
            "max_output_tokens": 256,
        }
        status, receipt = self.request(
            "POST",
            f"/api/v1/runtime/shadow/experiments/{experiment_id}/probes",
            probe_payload,
            key="wp07-http-probe",
        )
        self.assertEqual(200, status)
        self.assertEqual("blocked", receipt["status"])
        self.assertEqual(0, receipt["external_call_count"])
        self.assertFalse(receipt["actual_route_changed"])

        status, invalid = self.request(
            "POST",
            "/api/v1/runtime/shadow/experiments",
            {**payload, "api_key": "synthetic-secret-value"},
            key="wp07-http-invalid",
        )
        self.assertEqual(400, status)
        self.assertNotIn("synthetic-secret-value", json.dumps(invalid))

        status, mismatch = self.request(
            "POST",
            "/api/v1/runtime/shadow/experiments",
            {**payload, "data_residency": "cn"},
            key="wp07-http-residency-mismatch",
        )
        self.assertEqual(403, status)
        self.assertEqual("data_residency_forbidden", mismatch["error"])

    def test_health_and_status_survive_unavailable_persistent_runtime(self):
        isolated = tempfile.TemporaryDirectory()
        manager = RunManager(
            runtime_db_path=str(Path(isolated.name) / "runtime.sqlite3"),
            recover=False,
        )
        # Exercise the optional-runtime server path without mutating the
        # process-wide feature flag used by parallel/legacy tests.
        manager.control_kernel = None
        server = build_server("127.0.0.1", 0, manager=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_base = self.base
        try:
            self.base = f"http://127.0.0.1:{server.server_address[1]}"
            status, health = self.request("GET", "/api/health")
            self.assertEqual(200, status)
            self.assertFalse(
                health["small_budget_shadow"]["runtime_available"]
            )
            status, shadow = self.request(
                "GET", "/api/v1/runtime/shadow/status"
            )
            self.assertEqual(200, status)
            self.assertFalse(shadow["runtime_available"])
            self.assertEqual(0, shadow["runtime_counts"]["external_calls"])
        finally:
            self.base = original_base
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            isolated.cleanup()


if __name__ == "__main__":
    unittest.main()
