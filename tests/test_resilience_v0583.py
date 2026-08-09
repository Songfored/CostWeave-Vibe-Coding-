from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from costweave.catalog_store import CatalogStore
from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest
from costweave.event_store import EventStore, RUNTIME_SCHEMA_VERSION
from costweave.resilience import (
    BoundedStressHarness,
    DeadlineRiskPredictor,
    DeadlineSample,
    DeterministicFaultInjector,
    FaultDirective,
    FaultKind,
    FaultSignal,
    ProviderFailoverController,
    ProviderRoute,
    RecoveryPlanner,
    public_resilience_policy,
)
from costweave.resilience_store import ResilienceStore, ResilienceStoreError
from costweave.runtime_maintenance import RuntimeMaintenance
from costweave.server import build_server


class ResilienceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.kernel = ControlKernel(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migration_028_and_integrity_audit(self) -> None:
        health = self.kernel.health()
        self.assertEqual("v5.8.3", RUNTIME_SCHEMA_VERSION)
        self.assertEqual(28, health["migrations"])
        self.assertTrue(health["resilience"]["integrity"]["healthy"])
        self.assertEqual(1.0, health["resilience"]["integrity"]["event_integrity_coverage"])

    def test_fault_injection_is_default_off_and_deterministic(self) -> None:
        directive = FaultDirective(
            point="before_execute",
            kind=FaultKind.OUT_OF_MEMORY,
            trigger_on_call=2,
            repeat=2,
            provider="local-gpu",
        )
        disabled = DeterministicFaultInjector([directive])
        for _ in range(4):
            disabled.hit("before_execute", provider="local-gpu")
        enabled = DeterministicFaultInjector([directive], enabled=True)
        enabled.hit("before_execute", provider="local-gpu")
        with self.assertRaises(FaultSignal):
            enabled.hit("before_execute", provider="local-gpu")
        with self.assertRaises(FaultSignal):
            enabled.hit("before_execute", provider="local-gpu")
        enabled.hit("before_execute", provider="local-gpu")

    def test_provider_failure_and_oom_fail_over_within_gate(self) -> None:
        async def exercise(kind: FaultKind):
            controller = ProviderFailoverController(failure_threshold=1)

            async def failed():
                raise FaultSignal(kind, point="execute", provider="primary")

            async def healthy():
                return {"ok": True}

            return await controller.execute(
                [ProviderRoute("primary", failed), ProviderRoute("backup", healthy)]
            )

        for kind in (FaultKind.PROVIDER_DOWN, FaultKind.OUT_OF_MEMORY):
            result, receipt = asyncio.run(exercise(kind))
            self.assertTrue(result["ok"])
            self.assertEqual("backup", receipt.selected_provider)
            self.assertEqual(("primary", "backup"), receipt.attempted_providers)
            self.assertEqual((kind.value,), receipt.failure_kinds)
            self.assertTrue(receipt.within_30_seconds)
            self.assertIn("primary", receipt.open_providers)

    def test_ambiguous_side_effect_is_never_failed_over(self) -> None:
        calls = 0

        async def ambiguous():
            raise FaultSignal(
                FaultKind.AMBIGUOUS_SIDE_EFFECT,
                point="provider_response",
                provider="primary",
            )

        async def backup():
            nonlocal calls
            calls += 1
            return "must-not-run"

        controller = ProviderFailoverController()
        with self.assertRaises(FaultSignal):
            asyncio.run(
                controller.execute(
                    [ProviderRoute("primary", ambiguous), ProviderRoute("backup", backup)]
                )
            )
        self.assertEqual(0, calls)

    def test_deadline_warning_simulation_gate(self) -> None:
        samples = [
            DeadlineSample(650 + index, 700, 800, 0.8, True)
            for index in range(12)
        ] + [
            DeadlineSample(20 + index, 120, 1_000, 0.1, False)
            for index in range(12)
        ]
        report = DeadlineRiskPredictor.evaluate(samples)
        self.assertEqual(1.0, report.recall)
        self.assertEqual(1.0, report.auroc)
        self.assertTrue(report.meets_simulation_gate)
        self.assertEqual("E0-simulated", report.evidence_level)

    def test_recovery_preserves_validated_unpolluted_work(self) -> None:
        plan = RecoveryPlanner.build(
            run_status="executing",
            tasks=[
                {"id": "accepted", "status": "validated"},
                {"id": "interrupted", "status": "running"},
                {"id": "polluted", "status": "validated"},
            ],
            artifacts=[
                {
                    "artifact_id": "good-output",
                    "producer_task_id": "accepted",
                    "status": "validated",
                },
                {
                    "artifact_id": "bad-output",
                    "producer_task_id": "polluted",
                    "status": "stale",
                    "polluted": True,
                },
            ],
        )
        self.assertEqual(("accepted",), plan.preserve_task_ids)
        self.assertEqual(("interrupted", "polluted"), plan.restart_task_ids)
        self.assertEqual(("good-output",), plan.preserve_artifact_ids)
        terminal = RecoveryPlanner.build(
            run_status="completed", tasks=[{"id": "done"}], artifacts=[]
        )
        self.assertTrue(terminal.terminal_run)
        self.assertEqual((), terminal.restart_task_ids)

    def test_bounded_concurrency_has_no_worker_leak(self) -> None:
        barrier = threading.Barrier(8)

        def operation(index: int) -> int:
            if index < 8:
                barrier.wait(timeout=2)
            return index * 2

        report = BoundedStressHarness.run(
            operation, operations=64, concurrency=8
        )
        self.assertEqual(64, report.succeeded)
        self.assertEqual(0, report.failed)
        self.assertEqual(8, report.peak_in_flight)
        self.assertEqual(0, report.leaked_workers)

    def test_concurrent_observations_are_complete_and_idempotent(self) -> None:
        campaign, replayed = self.kernel.resilience_store.create_campaign(
            project_id="local-default",
            campaign_kind="stress",
            spec={"operations": 48, "concurrency": 12},
        )
        self.assertFalse(replayed)

        def write(index: int) -> None:
            self.kernel.resilience_store.record_observation(
                campaign["campaign_id"],
                observation_kind="admission",
                observation={"index": index, "accepted": True},
                idempotency_key=f"stress-observation-{index:04d}",
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(write, range(48)))
        self.assertEqual(48, self.kernel.resilience_store.counts()["resilience_observations"])
        _, replayed = self.kernel.resilience_store.record_observation(
            campaign["campaign_id"],
            observation_kind="admission",
            observation={"index": 0, "accepted": True},
            idempotency_key="stress-observation-0000",
        )
        self.assertTrue(replayed)
        with self.assertRaises(ResilienceStoreError):
            self.kernel.resilience_store.record_observation(
                campaign["campaign_id"],
                observation_kind="admission",
                observation={"index": 0, "accepted": False},
                idempotency_key="stress-observation-0000",
            )

    def test_checkpoint_is_immutable_and_semantically_idempotent(self) -> None:
        record, _ = self.kernel.create_run(
            RunRequest(goal="recover an interrupted task"),
            idempotency_key="resilience-create-run",
        )
        prepared = self.kernel.prepare_recovery(record)
        repeated = self.kernel.prepare_recovery(record)
        self.assertEqual(
            prepared["checkpoint"]["checkpoint_id"],
            repeated["checkpoint"]["checkpoint_id"],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE recovery_checkpoints_v0583 SET plan_hash=?",
                    ("0" * 64,),
                )
            connection.rollback()

    def test_backup_has_logical_snapshot_and_restores_projection(self) -> None:
        record, _ = self.kernel.create_run(
            RunRequest(goal="preserve event and projection integrity"),
            idempotency_key="backup-logical-snapshot",
        )
        maintenance = RuntimeMaintenance(self.path)
        backup = maintenance.create_backup()
        self.assertTrue(backup["logical_snapshot_matches_source"])
        verification = maintenance.verify_backup(backup["backup_id"])
        self.assertTrue(verification["logical_snapshot_match"])
        restored = maintenance.restore_backup(
            backup["backup_id"], runtime_closed=True
        )
        self.assertTrue(restored["logical_snapshot_match"])
        reopened = EventStore(self.path)
        self.assertIsNotNone(reopened.get_projection(str(record.id)))

    def test_tampered_logical_manifest_is_refused(self) -> None:
        maintenance = RuntimeMaintenance(self.path)
        backup = maintenance.create_backup()
        manifest_path = maintenance.backup_dir / backup["backup_id"]
        manifest_path = manifest_path.with_suffix(".json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["logical_snapshot"]["event_root"] = "0" * 64
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        self.assertFalse(
            maintenance.verify_backup(backup["backup_id"])[
                "logical_snapshot_match"
            ]
        )
        with self.assertRaises(RuntimeError):
            maintenance.restore_backup(
                backup["backup_id"], runtime_closed=True
            )

    def test_event_tampering_is_reported(self) -> None:
        self.kernel.create_run(
            RunRequest(goal="audit event hashes"),
            idempotency_key="event-integrity-tamper",
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE events SET payload_json='{}'")
            connection.commit()
        audit = self.kernel.resilience_store.integrity_audit()
        self.assertFalse(audit["healthy"])
        self.assertTrue(any("event_payload_hash" in item for item in audit["errors"]))

    def test_public_policy_is_explicitly_offline(self) -> None:
        policy = public_resilience_policy()
        self.assertFalse(policy["fault_injection_default_enabled"])
        self.assertFalse(policy["real_provider_faults_triggered"])
        self.assertFalse(policy["commercial_claims_allowed"])


class ResilienceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.catalog = CatalogStore(Path(cls.temp.name) / "catalog.json")
        cls.server = build_server(
            "127.0.0.1", 0, catalog_store=cls.catalog
        )
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
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def test_status_and_offline_campaign_api(self) -> None:
        status, resilience = self.request("/api/v1/resilience/status")
        self.assertEqual(200, status)
        self.assertEqual("v0.5.8.3", resilience["schema_version"])
        self.assertTrue(resilience["integrity"]["healthy"])
        self.assertEqual(28, resilience["runtime"]["migrations"])
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": "api-resilience-campaign-001",
        }
        payload = {
            "campaign_kind": "fault",
            "spec": {"fault": "provider_down", "calls": 0},
        }
        created, first = self.request(
            "/api/v1/resilience/campaigns", payload, headers
        )
        replayed, second = self.request(
            "/api/v1/resilience/campaigns", payload, headers
        )
        self.assertEqual(201, created)
        self.assertEqual(200, replayed)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertFalse(first["campaign"]["spec"]["real_provider_calls"])

    def test_campaign_api_rejects_real_provider_calls(self) -> None:
        status, payload = self.request(
            "/api/v1/resilience/campaigns",
            {
                "campaign_kind": "fault",
                "spec": {"real_provider_calls": True},
            },
            {
                "Content-Type": "application/json",
                "Idempotency-Key": "api-resilience-campaign-002",
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("resilience_campaign_rejected", payload["error"])


if __name__ == "__main__":
    unittest.main()
