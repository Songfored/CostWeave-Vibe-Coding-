"""Contract and local-runtime acceptance tests for V066-WP06."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from costweave.engine import RunManager
from costweave.event_store import IdempotencyConflictError
from costweave.runtime_backend import (
    LocalRuntimeBackend,
    RuntimeBackendContractError,
    RuntimeEventPageV1,
    RuntimeSubmitRequestV1,
    public_runtime_backend_policy,
)
from costweave.server import build_server


class RuntimeBackendContractTests(unittest.TestCase):
    def test_submit_contract_is_strict_and_round_trips(self):
        request = RuntimeSubmitRequestV1(
            goal="define a safe local runtime",
            idempotency_key="wp06-contract-001",
            metadata={"client": "test"},
        )
        restored = RuntimeSubmitRequestV1.from_dict(request.to_dict())
        self.assertEqual(request, restored)
        with self.assertRaises(RuntimeBackendContractError):
            RuntimeSubmitRequestV1.from_dict({**request.to_dict(), "unknown": True})
        with self.assertRaises(RuntimeBackendContractError):
            RuntimeSubmitRequestV1(
                goal="define a safe local runtime",
                idempotency_key="wp06-contract-002",
                budget=float("nan"),
            )
        with self.assertRaises(RuntimeBackendContractError):
            RuntimeSubmitRequestV1(
                goal="define a safe local runtime",
                idempotency_key="wp06-contract-003",
                metadata={"authorization": "Bearer synthetic-secret-value"},
            )

    def test_policy_keeps_third_party_runtime_adapter_only(self):
        policy = public_runtime_backend_policy()
        self.assertTrue(policy["local_first"])
        self.assertFalse(policy["external_calls_enabled_by_default"])
        self.assertEqual("adapter-only", policy["third_party_runtime_mode"])


class LocalRuntimeBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        # Keep the contract tests deterministic: the backend contract is under
        # test, not the simulated executor's timing.
        self.started = []
        self.manager._start = lambda record, *, resume: self.started.append((str(record.id), resume))
        self.backend = LocalRuntimeBackend(self.manager)

    def tearDown(self):
        self.manager.shutdown(wait_timeout=2)
        self.temporary.cleanup()

    def test_submit_checkpoint_resume_events_and_idempotent_replay(self):
        request = RuntimeSubmitRequestV1(
            goal="persist a runtime backend operation",
            idempotency_key="wp06-submit-001",
        )
        first = self.backend.submit(request)
        replay = self.backend.submit(request)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.run_id, replay.run_id)
        self.assertEqual(1, len(self.started))
        self.assertFalse(self.started[0][1])

        checkpoint = self.backend.checkpoint(
            first.run_id,
            project_id="local-default",
            idempotency_key="wp06-checkpoint-001",
            expected_revision=first.runtime_revision,
            source="test.checkpoint",
        )
        checkpoint_replay = self.backend.checkpoint(
            first.run_id,
            project_id="local-default",
            idempotency_key="wp06-checkpoint-001",
            expected_revision=first.runtime_revision,
            source="test.checkpoint",
        )
        self.assertFalse(checkpoint.replayed)
        self.assertTrue(checkpoint_replay.replayed)
        self.assertGreaterEqual(checkpoint_replay.runtime_revision, checkpoint.runtime_revision)

        resumed = self.backend.resume(
            first.run_id,
            project_id="local-default",
            idempotency_key="wp06-resume-001",
            expected_revision=checkpoint_replay.runtime_revision,
            source="test.resume",
        )
        self.assertFalse(resumed.replayed)
        self.assertTrue(any(item == (first.run_id, True) for item in self.started))

        page = self.backend.events(first.run_id, project_id="local-default", limit=2)
        self.assertIsInstance(page, RuntimeEventPageV1)
        self.assertEqual(2, len(page.events))
        self.assertTrue(page.has_more)
        raw = json.dumps(page.to_dict(), ensure_ascii=False)
        self.assertNotIn("synthetic-secret", raw)

        exported = resumed.to_dict()
        exported["snapshot"]["request"]["goal"] = "mutated"
        self.assertNotEqual("mutated", self.manager.get(first.run_id).request.goal)

    def test_submit_key_reuse_with_different_metadata_is_rejected(self):
        first = self.backend.submit(
            RuntimeSubmitRequestV1(
                goal="metadata belongs to the command identity",
                idempotency_key="wp06-submit-meta",
                metadata={"client": "one"},
            )
        )
        with self.assertRaises(IdempotencyConflictError):
            self.backend.submit(
                RuntimeSubmitRequestV1(
                    goal="metadata belongs to the command identity",
                    idempotency_key="wp06-submit-meta",
                    metadata={"client": "two"},
                )
            )
        self.assertEqual(first.run_id, self.manager.list()[0]["id"])

    def test_checkpoint_and_resume_do_not_alias_one_idempotency_key(self):
        first = self.backend.submit(
            RuntimeSubmitRequestV1(
                goal="operation identity is part of the durable command",
                idempotency_key="wp06-submit-op-001",
            )
        )
        checkpoint = self.backend.checkpoint(
            first.run_id,
            project_id="local-default",
            idempotency_key="wp06-shared-operation-key",
            expected_revision=first.runtime_revision,
            source="test.operation",
        )
        with self.assertRaises(IdempotencyConflictError):
            self.backend.resume(
                first.run_id,
                project_id="local-default",
                idempotency_key="wp06-shared-operation-key",
                expected_revision=checkpoint.runtime_revision,
                source="test.operation",
            )

    def test_cancel_and_project_scope_are_fail_closed(self):
        request = RuntimeSubmitRequestV1(
            goal="cancel a local runtime safely",
            idempotency_key="wp06-submit-002",
        )
        first = self.backend.submit(request)
        cancelled = self.backend.cancel(
            first.run_id,
            project_id="local-default",
            idempotency_key="wp06-cancel-001",
            expected_revision=first.runtime_revision,
        )
        replay = self.backend.cancel(
            first.run_id,
            project_id="local-default",
            idempotency_key="wp06-cancel-001",
            expected_revision=first.runtime_revision,
        )
        self.assertEqual("cancelled", cancelled.status)
        self.assertFalse(cancelled.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(KeyError):
            self.backend.events(first.run_id, project_id="other-project")


class RuntimeBackendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        self.manager._start = lambda record, *, resume: None
        self.server = build_server("127.0.0.1", 0, manager=self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload=None, *, key: str | None = None, project: str = "local-default"):
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
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_runtime_http_contract(self):
        status, policy = self.request("GET", "/api/v1/runtime/status")
        self.assertEqual(200, status)
        self.assertEqual("local-runtime", policy["backend"]["backend_id"])
        self.assertTrue(policy["policy"]["local_first"])

        status, submitted = self.request(
            "POST",
            "/api/v1/runtime/submit",
            {"goal": "submit through the runtime boundary"},
            key="wp06-http-001",
        )
        self.assertEqual(202, status)
        run_id = submitted["run_id"]
        revision = submitted["runtime_revision"]
        status, conflict = self.request(
            "POST",
            "/api/v1/runtime/submit",
            {"goal": "submit through the runtime boundary", "metadata": {"client": "changed"}},
            key="wp06-http-001",
        )
        self.assertEqual(409, status)
        self.assertIn(conflict["error"], {"idempotency_conflict", "runtime_conflict"})
        status, checkpoint = self.request(
            "POST",
            f"/api/v1/runtime/{run_id}/checkpoint",
            {"expected_revision": revision, "source": "http.test"},
            key="wp06-http-checkpoint-001",
        )
        self.assertEqual(202, status)
        status, events = self.request(
            "GET", f"/api/v1/runtime/{run_id}/events?limit=1000"
        )
        self.assertEqual(200, status)
        self.assertTrue(events["events"])
        self.assertEqual(checkpoint["run_id"], events["run_id"])

        status, _ = self.request(
            "GET", f"/api/v1/runtime/{run_id}/events", project="foreign-project"
        )
        self.assertEqual(404, status)

        status, body = self.request(
            "POST",
            "/api/v1/runtime/submit",
            {"goal": "submit without an idempotency key"},
        )
        self.assertEqual(400, status)
        self.assertEqual("idempotency_key_required", body["error"])


if __name__ == "__main__":
    unittest.main()
