from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from costweave.catalog_store import CatalogStore
from costweave.contracts_v4 import canonical_identifier
from costweave.event_store import EventStore, RUNTIME_SCHEMA_VERSION
from costweave.reservations import ReservationError, ReservationManager
from costweave.resource_inventory import ResourcePool
from costweave.admission import ResourceRequest
from costweave.runtime_maintenance import RuntimeMaintenance
from costweave.server import CostWeaveHandler, CostWeaveServer


class RuntimeMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        EventStore(self.path)
        self.maintenance = RuntimeMaintenance(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_database_is_unhealthy(self):
        self.assertFalse(
            RuntimeMaintenance(self.path.with_name("missing.db"))
            .diagnostics()["healthy"]
        )

    def test_fresh_database_is_healthy(self):
        self.assertTrue(self.maintenance.diagnostics()["healthy"])

    def test_diagnostics_include_sha256(self):
        self.assertEqual(64, len(self.maintenance.diagnostics()["sha256"]))

    def test_diagnostics_report_eight_migrations(self):
        self.assertEqual(25, self.maintenance.diagnostics()["migrations"])

    def test_backup_is_created_and_verified(self):
        backup = self.maintenance.create_backup()
        self.assertTrue(backup["verified"])
        self.assertTrue(self.maintenance.verify_backup(backup["backup_id"])["healthy"])

    def test_backup_manifest_is_listed(self):
        backup = self.maintenance.create_backup()
        self.assertEqual(backup["backup_id"], self.maintenance.list_backups()[0]["backup_id"])

    def test_backup_has_integrity_match(self):
        self.maintenance.create_backup()
        self.assertTrue(self.maintenance.list_backups()[0]["integrity_match"])

    def test_invalid_backup_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.maintenance.verify_backup("../runtime.sqlite3")

    def test_missing_backup_is_rejected(self):
        with self.assertRaises(KeyError):
            self.maintenance.verify_backup("runtime-20260101T000000Z-deadbeef.sqlite3")

    def test_restore_requires_stopped_runtime(self):
        backup = self.maintenance.create_backup()
        with self.assertRaises(RuntimeError):
            self.maintenance.restore_backup(backup["backup_id"])

    def test_verified_backup_can_restore_offline(self):
        backup = self.maintenance.create_backup()
        result = self.maintenance.restore_backup(
            backup["backup_id"], runtime_closed=True
        )
        self.assertTrue(result["restored"])

    def test_tampered_backup_is_not_restored(self):
        backup = self.maintenance.create_backup()
        path = self.maintenance.backup_dir / backup["backup_id"]
        path.write_bytes(path.read_bytes() + b"tamper")
        with self.assertRaises(RuntimeError):
            self.maintenance.restore_backup(
                backup["backup_id"], runtime_closed=True
            )


class SchemaHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = EventStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def table_names(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }

    def test_runtime_schema_is_v49(self):
        self.assertEqual("v5.7.4", RUNTIME_SCHEMA_VERSION)

    def test_schema_has_event_integrity_table(self):
        self.assertIn("event_integrity", self.table_names())

    def test_schema_has_media_commands_table(self):
        self.assertIn("media_commands", self.table_names())

    def test_schema_migration_is_idempotent(self):
        EventStore(self.path)
        self.assertEqual(25, self.store.health()["migrations"])

    def test_database_uses_wal(self):
        self.assertEqual("wal", self.store.health()["journal_mode"])

    def test_quick_check_passes(self):
        self.assertEqual(
            ["ok"], RuntimeMaintenance(self.path).diagnostics()["quick_check"]
        )


class CatalogHardeningTests(unittest.TestCase):
    def test_invalid_revision_falls_back_without_startup_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "catalog.json"
            path.write_text(
                json.dumps({"models": [], "revision": "<script>"}),
                encoding="utf-8",
            )
            store = CatalogStore(path)
            self.assertIsNotNone(store.payload()["metadata"]["load_warning"])

    def test_v1_catalog_path_normalises(self):
        handler = object.__new__(CostWeaveHandler)
        handler._legacy_api = False
        self.assertEqual(
            "/api/catalog/models/x",
            handler._normalise_api_path("/api/v1/catalog/models/x"),
        )

    def test_legacy_catalog_path_is_marked(self):
        handler = object.__new__(CostWeaveHandler)
        handler._legacy_api = False
        handler._normalise_api_path("/api/catalog")
        self.assertTrue(handler._legacy_api)


class ReservationHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        EventStore(self.path)
        self.manager = ReservationManager(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO runs(run_id,revision,status,request_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                ("run_test", 0, "running", "{}", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00Z"),
            )
            task_id = canonical_identifier("task", "run_test:task_test")
            connection.execute(
                "INSERT INTO tasks(run_id,task_id,legacy_task_id,revision,"
                "status,task_json) VALUES(?,?,?,?,?,?)",
                ("run_test", task_id, "task_test", 0, "pending", "{}"),
            )
            connection.commit()
        self.pool = ResourcePool(
            pool_id="pool_test",
            provider="local",
            local=True,
            slots_total=1,
            cpu_cores_total=4,
            gpu_count_total=0,
            vram_mb_total=0,
            queue_limit=4,
            token_capacity=100,
            token_refill_per_second=10,
        )
        self.manager.register_pool(self.pool)
        self.request = ResourceRequest(slots=1, cpu_cores=1)

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, **changes):
        values = dict(
            run_id="run_test",
            task_id="task_test",
            attempt=1,
            pool=self.pool,
            request=self.request,
            estimated_cost=0.1,
            budget=1.0,
            spent=0.0,
        )
        values.update(changes)
        return self.manager.reserve(**values)

    def test_active_attempt_replays(self):
        first = self.reserve()
        self.assertEqual(first.reservation_id, self.reserve().reservation_id)

    def test_released_attempt_cannot_replay(self):
        first = self.reserve()
        self.manager.release(first.reservation_id, reason="done")
        with self.assertRaises(ReservationError):
            self.reserve()

    def test_expired_lease_frees_capacity(self):
        self.reserve(lease_seconds=1)
        time.sleep(1.05)
        self.assertEqual(1, self.manager.availability("pool_test")["slots"])

    def test_same_identity_with_different_cost_is_rejected(self):
        self.reserve()
        with self.assertRaises(ReservationError):
            self.reserve(estimated_cost=0.2)


class HttpBoundaryHardeningTests(unittest.TestCase):
    def test_server_workers_are_daemons(self):
        self.assertTrue(CostWeaveServer.daemon_threads)

    def test_server_close_does_not_wait_for_stalled_workers(self):
        self.assertFalse(CostWeaveServer.block_on_close)

    def test_server_accept_queue_is_bounded(self):
        self.assertEqual(64, CostWeaveServer.request_queue_size)

    def test_json_depth_limit_accepts_small_document(self):
        CostWeaveHandler._validate_json_shape({"a": [1, 2, 3]})

    def test_json_depth_limit_rejects_deep_document(self):
        value = {}
        current = value
        for _ in range(34):
            current["x"] = {}
            current = current["x"]
        with self.assertRaises(ValueError):
            CostWeaveHandler._validate_json_shape(value)

    def test_json_node_limit_rejects_large_document(self):
        with self.assertRaises(ValueError):
            CostWeaveHandler._validate_json_shape(list(range(10_001)))


if __name__ == "__main__":
    unittest.main()
