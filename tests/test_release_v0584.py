from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from costweave.adapters import AdapterRuntimePolicy
from costweave.catalog_store import CatalogStore
from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest
from costweave.event_store import RUNTIME_SCHEMA_VERSION
from costweave.release_cli import verify_fresh_install
from costweave.release_readiness import (
    EXPECTED_MIGRATIONS,
    audit_event_replay,
    build_release_status,
    public_release_policy,
)
from costweave.release_store import ReleaseStoreError
from costweave.server import build_server


class StableReleaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.kernel = ControlKernel(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migration_029_and_stable_engineering_gates(self) -> None:
        status = build_release_status(
            self.kernel, adapter_policy=AdapterRuntimePolicy().public()
        )
        self.assertEqual("v6.3.0", RUNTIME_SCHEMA_VERSION)
        self.assertEqual(33, EXPECTED_MIGRATIONS)
        self.assertEqual(33, self.kernel.health()["migrations"])
        self.assertTrue(status["engineering_ready"])
        self.assertEqual("stable_engineering", status["release_status"])
        self.assertFalse(status["production_ready"])
        self.assertFalse(status["commercial_claims_allowed"])
        self.assertEqual(64, len(status["manifest_hash"]))

    def test_every_projection_is_replayed(self) -> None:
        for index in range(3):
            self.kernel.create_run(
                RunRequest(goal=f"replay stable decision {index}"),
                idempotency_key=f"stable-replay-run-{index:02d}",
            )
        audit = audit_event_replay(self.kernel.store)
        self.assertTrue(audit["healthy"])
        self.assertTrue(audit["complete"])
        self.assertEqual(3, audit["runs_checked"])

    def test_replay_audit_reports_tampered_event(self) -> None:
        self.kernel.create_run(
            RunRequest(goal="detect release replay tampering"),
            idempotency_key="stable-replay-tamper",
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE events SET payload_json='{}'")
            connection.commit()
        audit = audit_event_replay(self.kernel.store)
        self.assertFalse(audit["healthy"])
        self.assertEqual(1, len(audit["mismatches"]))

    def test_enabled_external_or_paid_calls_block_attestation(self) -> None:
        for policy in (
            {"experimental_calls_enabled": True, "paid_calls_enabled": False},
            {"experimental_calls_enabled": False, "paid_calls_enabled": True},
        ):
            status = build_release_status(self.kernel, adapter_policy=policy)
            self.assertFalse(status["engineering_ready"])
            self.assertEqual("blocked", status["release_status"])

    def test_release_attestation_is_immutable_and_idempotent(self) -> None:
        manifest = build_release_status(
            self.kernel, adapter_policy=AdapterRuntimePolicy().public()
        )
        first, replayed = self.kernel.release_store.create_attestation(
            project_id="local-default",
            manifest=manifest,
            idempotency_key="stable-attestation-001",
        )
        second, repeated = self.kernel.release_store.create_attestation(
            project_id="local-default",
            manifest=manifest,
            idempotency_key="stable-attestation-001",
        )
        self.assertFalse(replayed)
        self.assertTrue(repeated)
        self.assertEqual(first["attestation_id"], second["attestation_id"])
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "DELETE FROM release_attestations_v0584"
                )
            connection.rollback()

    def test_attestation_key_reuse_with_different_manifest_is_rejected(self) -> None:
        manifest = build_release_status(
            self.kernel, adapter_policy=AdapterRuntimePolicy().public()
        )
        self.kernel.release_store.create_attestation(
            project_id="local-default",
            manifest=manifest,
            idempotency_key="stable-attestation-conflict",
        )
        changed = {**manifest, "engineering_ready": False}
        with self.assertRaises(ReleaseStoreError):
            self.kernel.release_store.create_attestation(
                project_id="local-default",
                manifest=changed,
                idempotency_key="stable-attestation-conflict",
            )

    def test_attestations_are_project_scoped(self) -> None:
        manifest = build_release_status(
            self.kernel, adapter_policy=AdapterRuntimePolicy().public()
        )
        self.kernel.release_store.create_attestation(
            project_id="project-alpha",
            manifest=manifest,
            idempotency_key="stable-project-alpha",
        )
        self.assertEqual(
            1,
            len(
                self.kernel.release_store.list_attestations(
                    project_id="project-alpha"
                )
            ),
        )
        self.assertEqual(
            [],
            self.kernel.release_store.list_attestations(
                project_id="project-beta"
            ),
        )

    def test_public_policy_separates_verified_and_unverified_scope(self) -> None:
        policy = public_release_policy()
        self.assertEqual("v0.6.3", policy["schema_version"])
        self.assertIn("EventLog-only projection replay", policy["verified_scope"])
        self.assertIn(
            "real cost reduction percentage", policy["unverified_claims"]
        )
        self.assertFalse(policy["production_ready"])

    def test_fresh_install_cli_contract_is_offline_and_reproducible(self) -> None:
        report = verify_fresh_install(timeout_seconds=30)
        self.assertTrue(report["success"])
        self.assertEqual("completed", report["run"]["status"])
        self.assertEqual(0, report["external_call_attempts"])
        self.assertTrue(report["release"]["replay_audit"]["healthy"])
        self.assertTrue(report["backup"]["logical_snapshot_match"])


class StableReleaseApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.catalog = CatalogStore(Path(cls.temp.name) / "catalog.json")
        cls.server = build_server("127.0.0.1", 0, catalog_store=cls.catalog)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    @classmethod
    def request(cls, path: str, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}",
            data=data,
            headers=headers or {},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def test_release_status_and_idempotent_attestation(self) -> None:
        status, release = self.request("/api/v1/release/status")
        self.assertEqual(200, status)
        self.assertTrue(release["engineering_ready"])
        self.assertEqual(33, release["runtime"]["migrations"])
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "stable-api-attestation-001",
        }
        payload = {"confirm": "ATTEST_STABLE_ENGINEERING"}
        created, first = self.request(
            "/api/v1/release/attestations", payload, headers
        )
        replayed, second = self.request(
            "/api/v1/release/attestations", payload, headers
        )
        self.assertEqual(201, created)
        self.assertEqual(200, replayed)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(
            first["attestation"]["attestation_id"],
            second["attestation"]["attestation_id"],
        )

    def test_release_attestation_requires_explicit_confirmation(self) -> None:
        status, payload = self.request(
            "/api/v1/release/attestations",
            {"confirm": "yes"},
            {
                "Content-Type": "application/json",
                "Idempotency-Key": "stable-api-attestation-002",
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("release_attestation_rejected", payload["error"])

    def test_release_status_is_project_scoped(self) -> None:
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "stable-api-project-alpha",
            "X-CostWeave-Project": "project-alpha",
        }
        created, _ = self.request(
            "/api/v1/release/attestations",
            {"confirm": "ATTEST_STABLE_ENGINEERING"},
            headers,
        )
        self.assertEqual(201, created)
        status, default = self.request("/api/v1/release/status")
        self.assertEqual(200, status)
        self.assertNotIn(
            "project-alpha",
            {item["project_id"] for item in default["attestations"]},
        )


if __name__ == "__main__":
    unittest.main()
