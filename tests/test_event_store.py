import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from importlib.resources import files

from costweave.contracts_v4 import (
    CommandEnvelope,
    EventEnvelope,
    new_run_id,
    utc_now,
)
from costweave.event_store import (
    EventStore,
    IdempotencyConflictError,
    ProjectionUpdate,
    RevisionConflictError,
)
from costweave.projections import canonical_task_id


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.store = EventStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def state(run_id, status="created", tasks=None):
        return {
            "projection_schema_version": "v4.2",
            "id": run_id,
            "request": {"goal": "测试持久运行"},
            "status": status,
            "plan": None if tasks is None else {"tasks": tasks},
            "events": [],
            "metrics": {},
            "created_at": "2026-07-22T00:00:00Z",
            "started_at": None,
            "finished_at": None,
            "error": None,
            "runtime": {"revision": 0},
        }

    def commit(self, run_id, *, key, expected, status="created", payload=None, command=None):
        state = self.state(run_id, status)
        state["runtime"]["revision"] = expected + 1
        command = command or CommandEnvelope.create(
            run_id, "run.create", payload or {"goal": "测试持久运行"}
        )
        event = EventEnvelope.create(
            run_id,
            "run.created" if expected == 0 else "run.state.changed",
            "tests",
            {"projection": state},
        )
        return self.store.commit(
            command,
            [event],
            idempotency_key=key,
            expected_revision=expected,
            projection_updates=[ProjectionUpdate("run", run_id, state)],
            result={"run_id": run_id},
        )

    def test_fresh_database_uses_wal_and_applies_migration_once(self):
        health = self.store.health()
        self.assertEqual("wal", health["journal_mode"])
        self.assertEqual(9, health["migrations"])
        reopened = EventStore(self.path)
        self.assertEqual(9, reopened.health()["migrations"])

    def test_v42_database_is_upgraded_without_losing_existing_tables(self):
        legacy_path = f"{self.temp.name}/legacy-v42.sqlite3"
        sql = (
            files("costweave.migrations")
            .joinpath("001_runtime.sql")
            .read_text(encoding="utf-8")
        )
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(version,checksum,applied_at) "
                "VALUES(1,?,?)",
                (checksum, utc_now()),
            )
            connection.commit()
        finally:
            connection.close()
        upgraded = EventStore(legacy_path)
        self.assertEqual("v5.1", upgraded.health()["runtime_schema"])
        self.assertEqual(9, upgraded.health()["migrations"])
        with closing(sqlite3.connect(legacy_path)) as check:
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(
            {
                "events",
                "artifacts",
                "artifact_links",
                "messages",
                "resource_pools",
                "resource_reservations",
                "approvals",
                "placements",
                "cost_ledger",
                "cache_metadata",
                "policy_revisions",
                "validation_results",
                "criterion_results",
                "failure_records",
                "plan_revisions",
                "forecast_snapshots",
                "outcomes",
                "forecast_calibrators",
                "capability_posteriors",
                "evaluation_runs",
                "media_projects",
                "media_bible_revisions",
                "shot_contracts",
                "prompt_revisions",
                "media_render_attempts",
                "media_continuity_checks",
                "media_cost_ledger",
            }
            <= tables
        )

    def test_revision_starts_at_zero_and_advances_contiguously(self):
        run_id = str(new_run_id())
        self.assertEqual(0, self.store.current_revision(run_id))
        first = self.commit(run_id, key="revision-key-0001", expected=0)
        second = self.commit(
            run_id, key="revision-key-0002", expected=1, status="planning"
        )
        self.assertEqual((1, 2), (first.revision, second.revision))
        self.assertEqual([1, 2], [item["revision"] for item in self.store.list_events(run_id)])

    def test_fixed_event_stream_replays_to_identical_projection(self):
        run_id = str(new_run_id())
        self.commit(run_id, key="replay-key-0001", expected=0)
        self.commit(run_id, key="replay-key-0002", expected=1, status="planning")
        self.assertEqual(self.store.get_projection(run_id), self.store.replay(run_id))

    def test_duplicate_semantic_command_is_replayed_before_revision_check(self):
        run_id = str(new_run_id())
        first_command = CommandEnvelope.create(run_id, "run.create", {"goal": "same"})
        first = self.commit(
            run_id,
            key="duplicate-key-0001",
            expected=0,
            payload={"goal": "same"},
            command=first_command,
        )
        duplicate = CommandEnvelope.create(run_id, "run.create", {"goal": "same"})
        replayed = self.commit(
            run_id,
            key="duplicate-key-0001",
            expected=0,
            payload={"goal": "same"},
            command=duplicate,
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replayed.replayed)
        self.assertEqual(1, self.store.counts()["events"])

    def test_same_idempotency_key_with_different_payload_conflicts(self):
        run_id = str(new_run_id())
        self.commit(
            run_id, key="conflict-key-0001", expected=0, payload={"goal": "one"}
        )
        with self.assertRaises(IdempotencyConflictError):
            self.commit(
                run_id,
                key="conflict-key-0001",
                expected=1,
                payload={"goal": "two"},
            )
        self.assertEqual(1, self.store.counts()["events"])

    def test_stale_expected_revision_has_zero_writes(self):
        run_id = str(new_run_id())
        self.commit(run_id, key="stale-key-0001", expected=0)
        before = self.store.counts()
        with self.assertRaises(RevisionConflictError):
            self.commit(run_id, key="stale-key-0002", expected=0, status="planning")
        self.assertEqual(before, self.store.counts())

    def test_corrupt_projection_falls_back_to_event_replay(self):
        run_id = str(new_run_id())
        self.commit(run_id, key="corrupt-key-0001", expected=0)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE projections SET state_hash='bad' WHERE name='run' AND entity_id=?",
                (run_id,),
            )
            connection.commit()
        restored = self.store.get_projection(run_id)
        self.assertEqual("created", restored["status"])
        self.assertEqual(1, restored["runtime"]["revision"])

    def test_two_runs_have_independent_revision_streams(self):
        first, second = str(new_run_id()), str(new_run_id())
        self.commit(first, key="two-runs-key-01", expected=0)
        self.commit(second, key="two-runs-key-02", expected=0)
        self.commit(first, key="two-runs-key-03", expected=1, status="planning")
        self.assertEqual(2, self.store.current_revision(first))
        self.assertEqual(1, self.store.current_revision(second))

    def test_same_semantic_task_id_is_scoped_by_run(self):
        run_ids = [str(new_run_id()), str(new_run_id())]
        for index, run_id in enumerate(run_ids):
            state = self.state(run_id, tasks=[{"id": "goal-alignment", "status": "pending", "attempt": 0}])
            state["runtime"]["revision"] = 1
            command = CommandEnvelope.create(run_id, "run.create", {"index": index})
            event = EventEnvelope.create(run_id, "run.created", "tests", {"projection": state})
            self.store.commit(
                command,
                [event],
                idempotency_key=f"task-scope-key-{index:04d}",
                expected_revision=0,
                projection_updates=[ProjectionUpdate("run", run_id, state)],
                result={"run_id": run_id},
            )
        expected = {canonical_task_id(run_id, "goal-alignment") for run_id in run_ids}
        with closing(sqlite3.connect(self.path)) as connection:
            actual = {row[0] for row in connection.execute("SELECT task_id FROM tasks")}
        self.assertEqual(expected, actual)

    def test_event_pagination_has_no_gaps_or_duplicates(self):
        run_id = str(new_run_id())
        self.commit(run_id, key="page-key-0001", expected=0)
        self.commit(run_id, key="page-key-0002", expected=1, status="planning")
        first = self.store.list_events(run_id, after_revision=0, limit=1)
        second = self.store.list_events(run_id, after_revision=first[-1]["revision"], limit=1)
        self.assertEqual([1, 2], [first[0]["revision"], second[0]["revision"]])

    def test_concurrent_writers_with_same_revision_only_one_commits(self):
        run_id = str(new_run_id())
        self.commit(run_id, key="race-seed-0001", expected=0)
        barrier = threading.Barrier(2)
        outcomes = []

        def writer(index):
            try:
                barrier.wait(timeout=2)
                self.commit(
                    run_id,
                    key=f"race-writer-{index:04d}",
                    expected=1,
                    status="planning",
                    payload={"writer": index},
                )
                outcomes.append("ok")
            except RevisionConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(["ok", "conflict"], outcomes)
        self.assertEqual(2, self.store.counts()["events"])

    def test_counts_include_run_task_attempt_and_projection_indexes(self):
        run_id = str(new_run_id())
        task = {
            "id": "branch-analysis",
            "status": "validated",
            "attempt": 1,
            "selected_worker": "local-model",
            "estimated_cost": 0.01,
            "result": {"summary": "ok"},
            "validation": {"passed": True},
        }
        state = self.state(run_id, status="completed", tasks=[task])
        state["runtime"]["revision"] = 1
        command = CommandEnvelope.create(run_id, "run.create", {"goal": "indexed"})
        event = EventEnvelope.create(run_id, "run.created", "tests", {"projection": state})
        self.store.commit(
            command,
            [event],
            idempotency_key="index-count-key-0001",
            expected_revision=0,
            projection_updates=[ProjectionUpdate("run", run_id, state)],
            result={"run_id": run_id},
        )
        counts = self.store.counts()
        self.assertEqual((1, 1, 1), (counts["runs"], counts["tasks"], counts["attempts"]))


if __name__ == "__main__":
    unittest.main()
