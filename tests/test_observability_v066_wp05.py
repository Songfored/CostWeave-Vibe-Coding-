import json
import tempfile
import unittest
from pathlib import Path

from costweave.domain import Event, RunRequest
from costweave.engine import RunManager, RunRecord
from costweave.control_kernel import ControlKernel
from costweave.gateway_backend import GatewayMessageV1, GatewayRequestV1
from costweave.gateway_openai import (
    FakeOpenAICompatibleServer,
    OpenAICompatibleGatewayBackend,
    OpenAICompatibleGatewayConfig,
    OpenAICompatibleRuntimePolicy,
)
from costweave.observability import (
    OBSERVABILITY_SCHEMA_VERSION,
    TRACE_STAGES,
    ObservabilityError,
    TracePolicyV1,
    TraceRecorder,
    TraceSpanV1,
    get_trace_recorder,
    public_observability_policy,
    register_trace_recorder,
    sanitize_attributes,
    trace_id_for_run,
    unregister_trace_recorder,
)
from costweave.release_readiness import build_release_status


class ObservabilityContractTests(unittest.TestCase):
    def test_policy_is_local_only_and_prompt_capture_is_non_negotiable(self):
        public = public_observability_policy()
        self.assertEqual(OBSERVABILITY_SCHEMA_VERSION, public["schema_version"])
        self.assertTrue(public["local_recording_enabled"])
        self.assertFalse(public["external_export_enabled"])
        self.assertFalse(public["prompt_capture_enabled"])
        self.assertFalse(public["secret_capture_enabled"])
        with self.assertRaises(ValueError):
            TracePolicyV1(capture_prompts=True)
        with self.assertRaises(ValueError):
            TracePolicyV1(capture_secrets=True)

    def test_trace_ids_are_otel_shape_and_stable_for_a_run(self):
        first = trace_id_for_run("run_abc")
        second = trace_id_for_run("run_abc")
        self.assertEqual(first, second)
        self.assertEqual(32, len(first))
        self.assertTrue(all(char in "0123456789abcdef" for char in first))

    def test_span_redacts_prompt_and_secret_values(self):
        span = TraceSpanV1(
            trace_id="a" * 32,
            span_id="b" * 16,
            parent_span_id=None,
            name="costweave.goal",
            stage="goal",
            start_time="2026-08-14T00:00:00Z",
            end_time="2026-08-14T00:00:01Z",
            status="ok",
            attributes={
                "prompt": "never persist this user prompt",
                "Authorization": "Bearer sk-secret-value-123456",
                "model": "local-model",
            },
        )
        raw = json.dumps(span.to_dict(), ensure_ascii=False)
        self.assertNotIn("never persist this user prompt", raw)
        self.assertNotIn("sk-secret-value-123456", raw)
        self.assertIn("redacted", raw)
        self.assertIn("local-model", raw)

    def test_common_secret_aliases_and_cloud_credentials_never_remain_plaintext(self):
        fixtures = {
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "auth": "Basic dXNlcjpwYXNzd29yZA==",
            "session": "session-cookie-secret-value",
            "pwd": "correct-horse-battery-staple",
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "token": "plain-token-secret",
            "provider_value": "github_pat_1234567890abcdefghijklmnop",
            "sas_value": "AccountKey=abcdefghijklmnopqrstuvwxyz123456",
        }
        secret_as_key = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        safe = sanitize_attributes({**fixtures, secret_as_key: "value"})
        raw = json.dumps(safe, ensure_ascii=False)
        for secret in fixtures.values():
            self.assertNotIn(secret, raw)
        self.assertNotIn(secret_as_key, raw)
        self.assertNotIn("sha256", raw)
        self.assertGreaterEqual(
            raw.count('"redacted": true'),
            len(fixtures) + 1,
        )

    def test_nested_connection_and_oauth_credentials_never_remain_plaintext(self):
        secrets = (
            "plain-db-password",
            "postgresql://alice:plain-db-password@db.internal/app",
            "mysql://root:second-password@db.internal/app",
            "opaque-oauth-credential",
            "Server=db;Password=connection-password",
            "redis://:cache-password@cache.internal/0",
        )
        safe = sanitize_attributes({
            "metadata": {
                "connections": [
                    {"pass": secrets[0]},
                    {"database_url": secrets[1]},
                    {"dsn": secrets[2]},
                    {"oauth": secrets[3]},
                    {"connection_string": secrets[4]},
                    {"endpoint": secrets[5]},
                ]
            }
        })
        raw = json.dumps(safe, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, raw)
        self.assertGreaterEqual(raw.count('"redacted": true'), len(secrets))

    def test_dot_and_colon_secret_keys_and_secret_span_names_are_redacted(self):
        fixtures = {
            "api.key": "dotted-api-key-value",
            "access:key": "colon-access-key-value",
            "account.key": "dotted-account-key-value",
            "auth:value": "colon-auth-value",
            "pass:code": "colon-pass-value",
            "oauth:value": "colon-oauth-value",
            "dsn:value": "colon-dsn-value",
            "database.url": "dotted-database-value",
            "connection.string": "dotted-connection-value",
            "private.key": "dotted-private-key-value",
            "signing:key": "colon-signing-key-value",
            "ssh.key": "dotted-ssh-key-value",
            "api..key": "repeated-dot-api-key-value",
            "access::key": "repeated-colon-access-key-value",
            "account--key": "repeated-dash-account-key-value",
            "database..url": "repeated-dot-database-value",
            "connection::string": "repeated-colon-connection-value",
            "private..key": "repeated-dot-private-key-value",
        }
        raw = json.dumps(
            sanitize_attributes({"nested": [{"secrets": fixtures}]}),
            ensure_ascii=False,
        )
        for secret in fixtures.values():
            self.assertNotIn(secret, raw)

        secret_name = "sk-abcdefgh12345678"
        span = TraceSpanV1(
            trace_id="e" * 32,
            span_id="f" * 16,
            parent_span_id=None,
            name=secret_name,
            stage="task",
            start_time="2026-08-14T00:00:00Z",
        )
        self.assertEqual("redacted.span", span.name)
        self.assertNotIn(secret_name, json.dumps(span.to_dict()))

        recorder = TraceRecorder(trace_id="d" * 32)
        recorded = recorder.record_event(secret_name)
        self.assertIsNotNone(recorded)
        self.assertEqual("costweave.redacted.event", recorded.name)
        exported = json.dumps(recorder.export())
        self.assertNotIn(secret_name, exported)
        for event_name in (
            "Bearer synthetic-token-value",
            "Basic dXNlcjpwYXNz",
        ):
            with self.subTest(event_name=event_name):
                event_recorder = TraceRecorder(trace_id="c" * 32)
                event_span = event_recorder.record_event(event_name)
                self.assertIsNotNone(event_span)
                self.assertEqual("costweave.redacted.event", event_span.name)
                event_export = json.dumps(event_recorder.export()).lower()
                self.assertNotIn(event_name.lower(), event_export)

    def test_token_metric_keys_are_exempt_only_for_finite_numbers(self):
        metrics = sanitize_attributes({
            "input_tokens": 12,
            "output_tokens": 3,
            "max_tokens": 64,
        })
        self.assertEqual(
            {"input_tokens": 12, "output_tokens": 3, "max_tokens": 64},
            metrics,
        )
        for key in ("input_tokens", "output_tokens", "max_tokens", "token"):
            with self.subTest(key=key):
                raw = json.dumps(
                    sanitize_attributes({key: "plain-secret-value"}),
                    ensure_ascii=False,
                )
                self.assertNotIn("plain-secret-value", raw)
                self.assertIn("redacted", raw)

    def test_prompt_and_secret_markers_do_not_retain_unsalted_digests(self):
        safe = sanitize_attributes({
            "prompt": "a short guessable prompt",
            "messages": [{"role": "user", "value": "guessable"}],
            "authorization": "Digest username=admin,response=guessable",
        })
        raw = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn("a short guessable prompt", raw)
        self.assertNotIn("guessable", raw)
        self.assertNotIn("sha256", raw)

    def test_event_shape_is_allowlisted_and_deeply_immutable(self):
        span = TraceSpanV1(
            trace_id="1" * 32,
            span_id="2" * 16,
            parent_span_id=None,
            name="costweave.task",
            stage="task",
            start_time="2026-08-14T00:00:00Z",
            end_time="2026-08-14T00:00:01Z",
            status="ok",
            attributes={"metadata": {"nested": {"value": "original"}}},
            events=({
                "name": "task.event",
                "timestamp": "2026-08-14T00:00:00Z",
                "attributes": {"jwt": "eyJhbGciOiJIUzI1NiJ9.payload.signature"},
                "raw_payload": "must-not-survive",
            },),
        )
        exported = span.to_dict()
        self.assertEqual(
            {"name", "timestamp", "attributes"},
            set(exported["events"][0]),
        )
        self.assertNotIn("must-not-survive", json.dumps(exported))
        with self.assertRaises(TypeError):
            span.attributes["metadata"] = "mutated"
        with self.assertRaises(TypeError):
            span.attributes["metadata"]["nested"]["value"] = "mutated"
        with self.assertRaises(TypeError):
            span.events[0]["attributes"]["jwt"] = "mutated"
        exported["attributes"]["metadata"]["nested"]["value"] = "external-change"
        self.assertEqual(
            "original",
            span.to_dict()["attributes"]["metadata"]["nested"]["value"],
        )
        recorder = TraceRecorder(trace_id="9" * 32)
        recorder.record_stage(
            "task",
            attributes={"metadata": {"nested": {"value": "recorder-original"}}},
        )
        first_export = recorder.export()
        first_export["spans"][1]["attributes"]["metadata"]["nested"]["value"] = "changed"
        self.assertEqual(
            "recorder-original",
            recorder.export()["spans"][1]["attributes"]["metadata"]["nested"]["value"],
        )

    def test_non_finite_numbers_and_invalid_time_ranges_fail_closed(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ObservabilityError):
                    sanitize_attributes({"score": value})
                with self.assertRaises(ObservabilityError):
                    sanitize_attributes({"input_tokens": value})
        with self.assertRaises(ObservabilityError):
            TraceSpanV1(
                trace_id="3" * 32,
                span_id="4" * 16,
                parent_span_id=None,
                name="costweave.task",
                stage="task",
                start_time="2026-08-14T00:00:00",
            )
        with self.assertRaises(ObservabilityError):
            TraceSpanV1(
                trace_id="3" * 32,
                span_id="4" * 16,
                parent_span_id=None,
                name="costweave.task",
                stage="task",
                start_time="2026-08-14T00:00:01Z",
                end_time="2026-08-14T00:00:00Z",
            )

    def test_root_time_bounds_are_explicit_and_strict(self):
        recorder = TraceRecorder(
            trace_id="5" * 32,
            root_start_time="2026-08-14T00:00:00+08:00",
            root_end_time="2026-08-14T00:00:01+08:00",
        )
        root = recorder.spans()[0]
        self.assertEqual("2026-08-14T00:00:00+08:00", root.start_time)
        self.assertEqual("2026-08-14T00:00:01+08:00", root.end_time)
        with self.assertRaises(ObservabilityError):
            TraceRecorder(
                trace_id="6" * 32,
                root_start_time="2026-08-14T00:00:01Z",
                root_end_time="2026-08-14T00:00:00Z",
            )

    def test_failure_events_are_error_spans(self):
        recorder = TraceRecorder(trace_id="7" * 32)
        failures = {
            "task_rejected",
            "node_validation_failed",
            "recovery_stopped",
            "failed",
            "cancelled",
        }
        for kind in failures:
            recorder.record_event(kind)
        statuses = {
            span.attributes["event_kind"]: span.status
            for span in recorder.spans()[1:]
        }
        self.assertEqual({kind: "error" for kind in failures}, statuses)

    def test_configured_event_and_attribute_limits_are_honored(self):
        recorder = TraceRecorder(
            trace_id="8" * 32,
            policy=TracePolicyV1(
                max_events_per_span=64,
                max_attribute_bytes=16_000,
            ),
        )
        handle = recorder.start_span(
            "costweave.task.limit",
            stage="task",
            attributes={f"field_{index}": "x" * 100 for index in range(120)},
        )
        for index in range(50):
            handle.add_event(f"event.{index}", {"index": index})
        span = handle.finish()
        self.assertIsNotNone(span)
        self.assertEqual(50, len(span.events))
        self.assertNotIn("attributes_truncated", span.attributes)

    def test_registry_does_not_evict_live_recorders_at_fixed_capacity(self):
        recorders = [
            TraceRecorder(trace_id=f"a{index + 1:031x}")
            for index in range(1_025)
        ]
        try:
            for recorder in recorders:
                register_trace_recorder(recorder)
            self.assertIs(recorders[0], get_trace_recorder(recorders[0].trace_id))
            self.assertIs(recorders[-1], get_trace_recorder(recorders[-1].trace_id))
        finally:
            for recorder in recorders:
                unregister_trace_recorder(recorder.trace_id)

    def test_recorder_builds_required_stages_and_otel_export(self):
        recorder = TraceRecorder(trace_id="c" * 32)
        recorder.record_stage("plan", attributes={"task_count": 2})
        recorder.record_stage("task", attributes={"task_id": "task-1"})
        recorder.record_provider_call(
            backend_id="local-backend",
            provider="openai-compatible",
            model="local-model",
            request_id="request-1",
        )
        recorder.record_stage("validation", attributes={"passed": True})
        recorder.record_stage("recovery", attributes={"action": "continue"})
        recorder.finish_root("ok")
        exported = recorder.export()
        self.assertEqual(OBSERVABILITY_SCHEMA_VERSION, exported["schema_version"])
        self.assertEqual("c" * 32, exported["trace_id"])
        stages = {item["stage"] for item in exported["spans"]}
        self.assertTrue(set(TRACE_STAGES).issubset(stages))
        self.assertIn("resourceSpans", recorder.to_otel())
        self.assertEqual(55, len(exported["traceparent"]))
        for span in exported["spans"]:
            self.assertEqual("c" * 32, span["trace_id"])
            self.assertNotIn("prompt", json.dumps(span).lower())

    def test_recorder_is_bounded_and_counts_dropped_spans(self):
        recorder = TraceRecorder(
            trace_id="d" * 32,
            policy=TracePolicyV1(max_spans=3),
        )
        for index in range(20):
            recorder.record_stage("task", attributes={"index": index})
        summary = recorder.summary()
        self.assertEqual(3, summary["span_count"])
        self.assertGreater(summary["dropped_span_count"], 0)

    def test_event_replay_reconstructs_trace_without_raw_messages(self):
        record = RunRecord(id="run-replay", request=RunRequest(goal="private goal"))
        record.events.append(Event(
            at="2026-08-14T00:00:00Z",
            kind="task_validated",
            message="private prompt should never be copied",
            task_id="task-1",
            detail={"passed": True},
        ))
        # A fresh recorder is the persistence/restart reconstruction path.
        replayed = TraceRecorder(trace_id=record.trace_id)
        replayed.replay_events(record.events)
        raw = json.dumps(replayed.export(), ensure_ascii=False)
        self.assertNotIn("private prompt should never be copied", raw)
        self.assertIn("validation", raw)


class GatewayTraceIntegrationTests(unittest.TestCase):
    def test_openai_compatible_call_creates_provider_span(self):
        recorder = TraceRecorder(trace_id="e" * 32)
        config = OpenAICompatibleGatewayConfig(
            backend_id="local-backend",
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
        )
        backend = OpenAICompatibleGatewayBackend(
            config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            transport=FakeOpenAICompatibleServer(content="ok"),
        )
        request = GatewayRequestV1.create(
            trace_id=recorder.trace_id,
            project_id="local-default",
            model="local-model",
            messages=(GatewayMessageV1(role="user", content="hidden prompt"),),
            max_output_tokens=16,
            idempotency_key="wp05-gateway-call",
        )
        from costweave.observability import register_trace_recorder, unregister_trace_recorder

        register_trace_recorder(recorder)
        try:
            response = backend.submit(request)
        finally:
            unregister_trace_recorder(recorder.trace_id)
        self.assertEqual("completed", response.status)
        spans = recorder.export()["spans"]
        self.assertTrue(any(item["stage"] == "provider_call" for item in spans))
        raw = json.dumps(spans, ensure_ascii=False)
        self.assertNotIn("hidden prompt", raw)


class RuntimeTraceIntegrationTests(unittest.TestCase):
    def test_run_projection_and_persisted_events_share_trace_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = RunManager(
                runtime_db_path=str(Path(temporary) / "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest(goal="trace a local run", max_concurrency=1),
                idempotency_key="wp05-run-001",
            )
            manager.shutdown(wait_timeout=60)
            self.assertFalse(manager.threads[record.id].is_alive())
            snapshot = record.snapshot()
            self.assertEqual(record.trace_id, snapshot["trace_id"])
            self.assertEqual(record.trace_id, snapshot["observability"]["trace_id"])
            events = manager.events(record.id)
            self.assertTrue(events)
            for event in events:
                self.assertEqual(record.trace_id, event["payload"]["trace_id"])
            trace = manager.trace(record.id)
            self.assertEqual(record.trace_id, trace["trace_id"])
            self.assertTrue(trace["spans"])

    def test_release_gate_blocks_requested_external_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            kernel = ControlKernel(Path(temporary) / "runtime.sqlite3")
            status = build_release_status(
                kernel,
                telemetry_policy={
                    "external_export_enabled": True,
                    "prompt_capture_enabled": False,
                    "secret_capture_enabled": False,
                },
            )
            self.assertFalse(status["engineering_ready"])
            self.assertEqual("blocked", status["release_status"])


if __name__ == "__main__":
    unittest.main()
