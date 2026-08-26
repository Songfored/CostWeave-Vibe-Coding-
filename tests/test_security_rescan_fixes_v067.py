from __future__ import annotations

import json
import hashlib
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest, RunStatus
from costweave.decision_intelligence_store import DecisionIntelligenceStore
from costweave.engine import RunManager
from costweave.event_store import EventStore
from costweave.security_store import (
    ProjectRevokedError,
    SecurityStateConflictError,
    SecurityStore,
)
from costweave.server import build_server
from costweave.tournament_service import OfflineTournamentService


VIDEO_GOAL = "制作30秒AI电影，主角必须保持一致，包含配音"


def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before the deadline")
        time.sleep(0.01)


class AbsoluteRequestDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        self.server = build_server(
            "127.0.0.1",
            0,
            manager=self.manager,
            max_active_requests=1,
            header_deadline_seconds=0.2,
            body_deadline_seconds=0.2,
            rejected_body_drain_seconds=0.1,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def assert_health_recovers(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/health",
            headers={"X-CostWeave-Session": self.server.session_token},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(200, response.status)

    def test_header_trickle_has_an_absolute_deadline(self) -> None:
        attacker = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        stopped = threading.Event()
        attacker.sendall(
            b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Trickle: "
        )

        def trickle() -> None:
            while not stopped.is_set():
                try:
                    attacker.sendall(b"a")
                except OSError:
                    return
                time.sleep(0.03)

        sender = threading.Thread(target=trickle, daemon=True)
        sender.start()
        try:
            wait_until(lambda: self.server.request_capacity()["active"] == 1)
            wait_until(lambda: self.server.request_capacity()["active"] == 0)
            self.assert_health_recovers()
        finally:
            stopped.set()
            attacker.close()
            sender.join(timeout=1)

    def test_body_trickle_has_an_absolute_deadline(self) -> None:
        attacker = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        stopped = threading.Event()
        headers = (
            "POST /api/v1/runs HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"X-CostWeave-Session: {self.server.session_token}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 512\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        attacker.sendall(headers + b"{")

        def trickle() -> None:
            while not stopped.is_set():
                try:
                    attacker.sendall(b" ")
                except OSError:
                    return
                time.sleep(0.03)

        sender = threading.Thread(target=trickle, daemon=True)
        sender.start()
        try:
            wait_until(lambda: self.server.request_capacity()["active"] == 1)
            wait_until(lambda: self.server.request_capacity()["active"] == 0)
            self.assert_health_recovers()
        finally:
            stopped.set()
            attacker.close()
            sender.join(timeout=1)

    def test_overload_drain_cannot_block_the_accept_loop(self) -> None:
        # Keep the single worker occupied long enough for this test even when
        # the complete suite is running on a slow Windows host.
        self.server.header_deadline_seconds = 2.0
        rejected_before = self.server.request_capacity()["rejected"]
        blocker = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        flood: socket.socket | None = None
        try:
            blocker.sendall(
                b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Block: "
            )
            wait_until(lambda: self.server.request_capacity()["active"] == 1)

            flood = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            flood.sendall(
                b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 999999\r\n\r\n"
                + b"x" * 16_384
            )
            wait_until(
                lambda: self.server.request_capacity()["rejected"]
                >= rejected_before + 1
            )

            started = time.monotonic()
            response = b""
            connection_aborted = False
            with socket.create_connection(
                ("127.0.0.1", self.port), timeout=2
            ) as probe:
                probe.sendall(
                    b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                )
                try:
                    response = probe.recv(4096)
                except (ConnectionAbortedError, ConnectionResetError):
                    # Windows may replace a just-written overload response
                    # with a reset while closing. A prompt reset is still a
                    # fail-closed rejection; the server-side counter below
                    # proves that the accept loop processed this connection.
                    connection_aborted = True
            wait_until(
                lambda: self.server.request_capacity()["rejected"]
                >= rejected_before + 2
            )
            self.assertLess(time.monotonic() - started, 1.0)
            if not connection_aborted:
                self.assertIn(b" 503 ", response.split(b"\r\n", 1)[0])
        finally:
            if flood is not None:
                flood.close()
            blocker.close()
        wait_until(lambda: self.server.request_capacity()["active"] == 0)
        self.assert_health_recovers()


class ResidencyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "runtime.sqlite3"),
            recover=False,
        )
        self.server = build_server("127.0.0.1", 0, manager=manager)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_offline_lab_families_fail_closed_for_nonlocal_residency(self) -> None:
        paths = (
            "/api/v1/tournaments/status",
            "/api/v1/meta-controller/status",
            "/api/v1/capability-router/status",
            "/api/v1/joint-search/status",
            "/api/v1/execution-recovery/status",
            "/api/v1/safe-learning/status",
            "/api/v1/resilience/status",
            "/api/v1/release/status",
        )
        for path in paths:
            with self.subTest(path=path):
                request = urllib.request.Request(
                    self.base + path,
                    headers={
                        "X-CostWeave-Session": self.server.session_token,
                        "X-CostWeave-Project": "project-residency",
                        "X-CostWeave-Data-Residency": "us-west",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=3)
                try:
                    self.assertEqual(422, caught.exception.code)
                    payload = json.loads(caught.exception.read())
                    self.assertEqual(
                        "data_residency_unsupported", payload["error"]
                    )
                finally:
                    caught.exception.close()

    def test_run_and_side_effect_identities_include_residency(self) -> None:
        path = Path(self.temporary.name) / "identity.sqlite3"
        kernel = ControlKernel(path)
        local, _ = kernel.create_run(
            RunRequest(
                "分析跨驻留区任务的合理路由方案",
                project_id="project-residency",
                data_residency="local",
            ),
            idempotency_key="same-create-key-001",
        )
        remote, _ = kernel.create_run(
            RunRequest(
                "分析跨驻留区任务的合理路由方案",
                project_id="project-residency",
                data_residency="us-west",
            ),
            idempotency_key="same-create-key-001",
        )
        self.assertNotEqual(local.id, remote.id)

        store = kernel.security_store
        common = {
            "project_id": "project-residency",
            "adapter_id": "fixture-adapter",
            "provider": "fixture-provider",
            "deployment_id": "fixture-deployment",
            "endpoint": "https://provider.example/v1/chat/completions",
            "model_id": "fixture-model",
            "adapter_revision": "fixture-v1",
            "operation": "model_inference",
            "idempotency_key": "same-side-effect-key-001",
            "request_hash": "a" * 64,
            "provider_idempotency_key": "provider-side-effect-key-001",
            "compensation_strategy": "not_required_idempotent",
        }
        local_effect, _ = store.reserve_side_effect(
            **common, data_residency="local"
        )
        remote_effect, _ = store.reserve_side_effect(
            **common, data_residency="us-west"
        )
        self.assertNotEqual(
            local_effect["side_effect_id"], remote_effect["side_effect_id"]
        )

    def test_lab_status_counts_are_project_scoped(self) -> None:
        path = Path(self.temporary.name) / "status-counts.sqlite3"
        EventStore(path)
        payload = "{}"
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with closing(sqlite3.connect(path)) as connection:
            for project, object_id in (
                ("project-alpha", "belief-alpha"),
                ("project-bravo", "belief-bravo"),
            ):
                connection.execute(
                    "INSERT INTO decision_objects_v060(project_id,object_id,object_kind,schema_version,payload_json,payload_hash,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        project,
                        object_id,
                        "task_belief",
                        "v0.6.0",
                        payload,
                        payload_hash,
                        "2026-08-24T00:00:00Z",
                    ),
                )
            connection.commit()
        store = DecisionIntelligenceStore(path)
        alpha = OfflineTournamentService(store).status(
            project_id="project-alpha"
        )
        bravo = OfflineTournamentService(store).status(
            project_id="project-bravo"
        )
        self.assertEqual(1, alpha["runtime_counts"]["decision_objects_v060"])
        self.assertEqual(1, bravo["runtime_counts"]["decision_objects_v060"])
        self.assertEqual("local", alpha["data_residency"])
        self.assertEqual(2, store.counts()["decision_objects_v060"])


class MediaOwnerAndRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "runtime.sqlite3"
        self.kernel = ControlKernel(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_duplicate_shot_id_is_still_bound_to_its_run_owner(self) -> None:
        first, _ = self.kernel.create_run(
            RunRequest(VIDEO_GOAL), idempotency_key="media-owner-first-001"
        )
        second, _ = self.kernel.create_run(
            RunRequest(VIDEO_GOAL), idempotency_key="media-owner-second-001"
        )
        first_project = self.kernel.ensure_media_project(first)
        second_project = self.kernel.ensure_media_project(second)
        first_shot = str(first_project["shots"][0]["shot_id"])
        old_second_shot = str(second_project["shots"][0]["shot_id"])

        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            media_project_id, raw_project = connection.execute(
                "SELECT project_id,project_json FROM media_projects WHERE run_id=?",
                (str(second.id),),
            ).fetchone()
            project_json = json.loads(raw_project)
            project_json["shots"][0]["shot_id"] = first_shot
            raw_contract = connection.execute(
                "SELECT contract_json FROM shot_contracts WHERE project_id=? AND shot_id=?",
                (media_project_id, old_second_shot),
            ).fetchone()[0]
            contract_json = json.loads(raw_contract)
            contract_json["shot_id"] = first_shot
            connection.execute(
                "DELETE FROM prompt_revisions WHERE project_id=? AND shot_id=?",
                (media_project_id, old_second_shot),
            )
            connection.execute(
                "UPDATE shot_contracts SET shot_id=?,contract_json=? WHERE project_id=? AND shot_id=?",
                (
                    first_shot,
                    json.dumps(contract_json, ensure_ascii=False),
                    media_project_id,
                    old_second_shot,
                ),
            )
            connection.execute(
                "UPDATE media_projects SET project_json=? WHERE project_id=?",
                (
                    json.dumps(project_json, ensure_ascii=False),
                    media_project_id,
                ),
            )
            connection.execute(
                "INSERT INTO media_render_attempts(attempt_id,project_id,shot_id,stage,attempt,status,simulated,artifact_json,cost,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "attempt_collision_fixture",
                    media_project_id,
                    first_shot,
                    "final",
                    1,
                    "accepted",
                    1,
                    "{}",
                    0.0,
                    "2026-08-24T00:00:00Z",
                ),
            )
            connection.commit()

        result = self.kernel.approve_media_shot(
            str(second.id),
            first_shot,
            idempotency_key="approve-colliding-shot-001",
        )
        self.assertEqual(first_shot, result["shot_id"])
        first_state = self.kernel.media_runtime.shot(str(first.id), first_shot)
        second_state = self.kernel.media_runtime.shot(str(second.id), first_shot)
        self.assertEqual("pending", first_state["approval_status"])
        self.assertEqual("approved", second_state["approval_status"])

    def test_revocation_blocks_every_later_run_commit(self) -> None:
        record, _ = self.kernel.create_run(
            RunRequest(
                "验证项目撤销后的提交拒绝行为",
                project_id="project-revocation",
            ),
            idempotency_key="revoke-commit-create-001",
        )
        revision = record.runtime_revision
        self.kernel.revoke_project(
            "project-revocation",
            idempotency_key="revoke-commit-project-001",
        )
        with self.assertRaises(ProjectRevokedError):
            self.kernel.set_run_status(
                record, RunStatus.PLANNING, source="test.post_revocation"
            )
        with self.assertRaises(ProjectRevokedError):
            self.kernel.invalidate_tasks(
                record,
                [],
                reason="post-revocation must not touch artifact state",
            )
        with self.assertRaises(ProjectRevokedError):
            self.kernel.record_actual_cost(
                record,
                SimpleNamespace(estimated_cost=0.0),
                actual_total=0.0,
            )
        with self.assertRaises(ProjectRevokedError):
            self.kernel.release_reservation(
                record,
                "reservation_missing_after_revocation",
                reason="post-revocation must not settle budget state",
                actual_cost=0.0,
            )
        admission_called = False

        class AdmissionFixture:
            @staticmethod
            def start_decision(_record, _task, _worker):
                nonlocal admission_called
                admission_called = True
                return object()

        with self.assertRaises(ProjectRevokedError):
            self.kernel.admit_scheduled_task(
                record,
                AdmissionFixture(),
                SimpleNamespace(),
                SimpleNamespace(),
            )
        self.assertFalse(admission_called)
        with self.assertRaises(ProjectRevokedError):
            self.kernel.assign_runtime_policy(
                record,
                SimpleNamespace(stakes="normal", task_types=("coding",)),
            )
        forged_request = RunRequest(
            "伪造另一个仍活跃的项目范围",
            project_id="project-still-active",
        ).normalized()
        with self.assertRaisesRegex(AttributeError, "identity is immutable"):
            record.request = forged_request
        original_request = record.request
        object.__setattr__(record, "request", forged_request)
        try:
            protected_operations = (
                lambda: self.kernel.invalidate_tasks(
                    record,
                    [],
                    reason="forged in-memory scope must not authorize writes",
                ),
                lambda: self.kernel.ensure_media_project(record),
                lambda: self.kernel.record_scope(record),
                lambda: self.kernel.record_project_status(record),
                lambda: self.kernel.cancel_run(
                    record,
                    idempotency_key="forged-cancel-scope-001",
                    expected_revision=record.runtime_revision,
                ),
                lambda: self.kernel.runtime_backend_checkpoint(
                    record,
                    idempotency_key="forged-checkpoint-scope-001",
                    expected_revision=record.runtime_revision,
                    checkpoint_id="checkpoint_forged_scope",
                    source="test.forged_scope",
                ),
            )
            for operation in protected_operations:
                with self.assertRaises(SecurityStateConflictError):
                    operation()
        finally:
            object.__setattr__(record, "request", original_request)
        self.assertEqual(revision, record.runtime_revision)
        self.assertEqual(RunStatus.CREATED, record.status)

    def test_run_manager_reads_fail_closed_on_forged_in_memory_scope(self) -> None:
        manager = RunManager(
            runtime_db_path=str(Path(self.temporary.name) / "manager-scope.sqlite3"),
            recover=False,
        )
        assert manager.control_kernel is not None
        record, _ = manager.control_kernel.create_run(
            RunRequest(
                "验证运行查询使用持久化归属",
                project_id="project-authoritative-scope",
            ),
            idempotency_key="manager-scope-create-001",
        )
        with manager.lock:
            manager.records[record.id] = record
        original_request = record.request
        forged_request = RunRequest(
            "伪造运行查询归属",
            project_id="project-forged-scope",
        ).normalized()
        object.__setattr__(record, "request", forged_request)
        try:
            self.assertIsNone(manager.get(record.id))
            self.assertEqual([], manager.list())
            self.assertIsNone(
                manager.get(record.id, project_id="project-forged-scope")
            )
        finally:
            object.__setattr__(record, "request", original_request)
        self.assertIs(record, manager.get(record.id))

    def test_revocation_waits_for_an_authorized_final_mutation_boundary(self) -> None:
        EventStore(self.path)
        store = SecurityStore(self.path)
        entered = threading.Event()
        release = threading.Event()
        revoked = threading.Event()

        def authorized_operation() -> None:
            with store.active_project_operation("project-locking"):
                entered.set()
                release.wait(timeout=2)

        def revoke() -> None:
            store.revoke_project(
                "project-locking",
                idempotency_key="revoke-locking-project-001",
            )
            revoked.set()

        operation_thread = threading.Thread(target=authorized_operation)
        operation_thread.start()
        self.assertTrue(entered.wait(timeout=1))
        revoke_thread = threading.Thread(target=revoke)
        revoke_thread.start()
        time.sleep(0.05)
        self.assertFalse(revoked.is_set())
        release.set()
        operation_thread.join(timeout=2)
        revoke_thread.join(timeout=2)
        self.assertTrue(revoked.is_set())


if __name__ == "__main__":
    unittest.main()
