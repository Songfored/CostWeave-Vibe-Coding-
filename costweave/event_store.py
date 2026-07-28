"""SQLite event store and deterministic projections for CostWeave v0.4.9."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable, Mapping

from .commands import command_fingerprint, validate_idempotency_key
from .contracts_v4 import (
    CONTRACT_SCHEMA_VERSION,
    CommandEnvelope,
    EventEnvelope,
    canonical_identifier,
    utc_now,
)


RUNTIME_SCHEMA_VERSION = "v4.9"


class EventStoreError(RuntimeError):
    """Base error for the persistent runtime."""


class RevisionConflictError(EventStoreError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected revision {expected}, current revision is {actual}")
        self.expected = expected
        self.actual = actual


class IdempotencyConflictError(EventStoreError):
    """Raised when a key or command ID is reused for different content."""


class ProjectionIntegrityError(EventStoreError):
    """Raised when neither a projection nor its event stream can be trusted."""


@dataclass(frozen=True, slots=True)
class ProjectionUpdate:
    name: str
    entity_id: str
    state: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CommitResult:
    revision: int
    replayed: bool
    result: Mapping[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _state_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EventStore:
    """Append-only per-run streams with transactional read projections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._lock, self._connection() as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise EventStoreError("SQLite refused WAL mode")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations("
                "version INTEGER PRIMARY KEY,checksum TEXT NOT NULL,"
                "applied_at TEXT NOT NULL)"
            )
            migration_root = files("costweave.migrations")
            for version, filename in (
                (1, "001_runtime.sql"),
                (2, "002_artifacts.sql"),
                (3, "003_parallel_runtime.sql"),
                (4, "004_cost_economics.sql"),
                (5, "005_evidence_recovery.sql"),
                (6, "006_outcome_evaluation.sql"),
                (7, "007_media_workflow.sql"),
                (8, "008_release_hardening.sql"),
            ):
                sql = migration_root.joinpath(filename).read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                existing = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=?",
                    (version,),
                ).fetchone()
                if existing is not None:
                    if existing["checksum"] != checksum:
                        raise EventStoreError(
                            f"runtime migration {version} checksum mismatch"
                        )
                    continue
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version,checksum,applied_at) "
                    "VALUES(?,?,?)",
                    (version, checksum, utc_now()),
                )
            self._backfill_event_integrity(connection)

    @staticmethod
    def _backfill_event_integrity(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT event_id,payload_json,envelope_json FROM events "
            "WHERE event_id NOT IN (SELECT event_id FROM event_integrity)"
        ).fetchall()
        for row in rows:
            connection.execute(
                "INSERT INTO event_integrity(event_id,payload_hash,envelope_hash,"
                "recorded_at) VALUES(?,?,?,?)",
                (
                    row["event_id"],
                    _state_hash(str(row["payload_json"])),
                    _state_hash(str(row["envelope_json"])),
                    utc_now(),
                ),
            )

    def health(self) -> dict[str, Any]:
        with self._connection() as connection:
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            migrations = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
        return {
            "runtime_schema": RUNTIME_SCHEMA_VERSION,
            "event_store": "sqlite",
            "journal_mode": str(journal).lower(),
            "migrations": int(migrations),
            "database_name": self.path.name,
        }

    def current_revision(self, run_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row["revision"]) if row is not None else 0

    def replay_command(
        self,
        command: CommandEnvelope,
        *,
        idempotency_key: str,
    ) -> CommitResult | None:
        """Return a prior semantic command result before optimistic checks."""

        key = validate_idempotency_key(idempotency_key)
        fingerprint = command_fingerprint(command)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT command_id,idempotency_key,request_hash,result_json "
                "FROM commands WHERE command_id=? OR idempotency_key=?",
                (command.command_id, key),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise IdempotencyConflictError(
                "command_id and idempotency key refer to different commands"
            )
        row = rows[0]
        if row["request_hash"] != fingerprint:
            raise IdempotencyConflictError(
                "command_id or idempotency key was reused for different content"
            )
        result = json.loads(row["result_json"])
        return CommitResult(
            revision=int(result["revision"]),
            replayed=True,
            result=result,
        )

    def commit(
        self,
        command: CommandEnvelope,
        events: Iterable[EventEnvelope],
        *,
        idempotency_key: str,
        expected_revision: int,
        projection_updates: Iterable[ProjectionUpdate],
        result: Mapping[str, Any],
        attempt_ids: Mapping[str, str] | None = None,
    ) -> CommitResult:
        key = validate_idempotency_key(idempotency_key)
        fingerprint = command_fingerprint(command)
        event_list = tuple(events)
        updates = tuple(projection_updates)
        if not event_list:
            raise EventStoreError("a committed command must emit at least one event")
        if any(event.run_id != command.run_id for event in event_list):
            raise EventStoreError("command and events must share a run_id")
        result_json = _canonical_json(dict(result))
        now = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing_rows = connection.execute(
                    "SELECT command_id,idempotency_key,request_hash,result_json "
                    "FROM commands WHERE command_id=? OR idempotency_key=?",
                    (command.command_id, key),
                ).fetchall()
                if len(existing_rows) > 1:
                    raise IdempotencyConflictError(
                        "command_id and idempotency key refer to different commands"
                    )
                existing = existing_rows[0] if existing_rows else None
                if existing is not None:
                    if existing["request_hash"] != fingerprint:
                        raise IdempotencyConflictError(
                            "command_id or idempotency key was reused for different content"
                        )
                    stored = json.loads(existing["result_json"])
                    connection.rollback()
                    return CommitResult(
                        revision=int(stored["revision"]),
                        replayed=True,
                        result=stored,
                    )

                row = connection.execute(
                    "SELECT revision FROM runs WHERE run_id=?", (command.run_id,)
                ).fetchone()
                actual = int(row["revision"]) if row is not None else 0
                if actual != expected_revision:
                    raise RevisionConflictError(expected_revision, actual)

                final_revision = actual + len(event_list)
                stored_result = {**dict(result), "revision": final_revision}
                result_json = _canonical_json(stored_result)
                connection.execute(
                    "INSERT INTO commands(command_id,idempotency_key,run_id,kind,request_hash,result_json,committed_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        command.command_id,
                        key,
                        command.run_id,
                        command.kind,
                        fingerprint,
                        result_json,
                        now,
                    ),
                )
                for offset, event in enumerate(event_list, start=1):
                    revision = actual + offset
                    attempt_id = (attempt_ids or {}).get(event.event_id)
                    payload_json = _canonical_json(event.to_dict()["payload"])
                    envelope_json = event.to_json()
                    connection.execute(
                        "INSERT INTO events(event_id,run_id,stream_revision,task_id,attempt_id,command_id,schema_version,kind,occurred_at,source,payload_json,envelope_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            event.event_id,
                            event.run_id,
                            revision,
                            event.task_id,
                            attempt_id,
                            command.command_id,
                            event.schema_version,
                            event.kind,
                            event.occurred_at,
                            event.source,
                            payload_json,
                            envelope_json,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO event_integrity(event_id,payload_hash,"
                        "envelope_hash,recorded_at) VALUES(?,?,?,?)",
                        (
                            event.event_id,
                            _state_hash(payload_json),
                            _state_hash(envelope_json),
                            now,
                        ),
                    )
                for update in updates:
                    self._write_projection(connection, update, final_revision, now)
                connection.commit()
                return CommitResult(
                    revision=final_revision,
                    replayed=False,
                    result=stored_result,
                )
            except Exception:
                connection.rollback()
                raise

    def _write_projection(
        self,
        connection: sqlite3.Connection,
        update: ProjectionUpdate,
        revision: int,
        now: str,
    ) -> None:
        state = dict(update.state)
        raw = _canonical_json(state)
        connection.execute(
            "INSERT INTO projections(name,entity_id,revision,schema_version,state_json,state_hash,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(name,entity_id) DO UPDATE SET revision=excluded.revision,schema_version=excluded.schema_version,state_json=excluded.state_json,state_hash=excluded.state_hash,updated_at=excluded.updated_at",
            (
                update.name,
                update.entity_id,
                revision,
                RUNTIME_SCHEMA_VERSION,
                raw,
                _state_hash(raw),
                now,
            ),
        )
        if update.name == "run":
            self._write_run_indexes(connection, state, revision, now)

    def _write_run_indexes(
        self,
        connection: sqlite3.Connection,
        state: Mapping[str, Any],
        revision: int,
        now: str,
    ) -> None:
        run_id = str(state["id"])
        request = dict(state.get("request") or {})
        connection.execute(
            "INSERT INTO runs(run_id,revision,status,request_json,created_at,started_at,finished_at,error,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision,status=excluded.status,request_json=excluded.request_json,created_at=excluded.created_at,started_at=excluded.started_at,finished_at=excluded.finished_at,error=excluded.error,updated_at=excluded.updated_at",
            (
                run_id,
                revision,
                str(state["status"]),
                _canonical_json(request),
                str(state.get("created_at") or now),
                state.get("started_at"),
                state.get("finished_at"),
                state.get("error"),
                now,
            ),
        )
        connection.execute("DELETE FROM attempts WHERE run_id=?", (run_id,))
        for task in ((state.get("plan") or {}).get("tasks") or []):
            legacy_task_id = str(task["id"])
            task_id = canonical_identifier("task", f"{run_id}:{legacy_task_id}")
            connection.execute(
                "INSERT INTO tasks(run_id,task_id,legacy_task_id,revision,status,task_json) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,task_id) DO UPDATE SET "
                "legacy_task_id=excluded.legacy_task_id,revision=excluded.revision,"
                "status=excluded.status,task_json=excluded.task_json",
                (
                    run_id,
                    task_id,
                    legacy_task_id,
                    revision,
                    str(task.get("status", "pending")),
                    _canonical_json(task),
                ),
            )
            attempt_count = max(0, int(task.get("attempt", 0)))
            for ordinal in range(1, attempt_count + 1):
                attempt_id = canonical_identifier(
                    "attempt", f"{run_id}:{legacy_task_id}:{ordinal}"
                )
                connection.execute(
                    "INSERT INTO attempts(attempt_id,run_id,task_id,ordinal,status,worker_id,started_at,finished_at,result_json,validation_json,estimated_cost) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        run_id,
                        task_id,
                        ordinal,
                        str(task.get("status", "pending")),
                        task.get("selected_worker"),
                        task.get("started_at"),
                        task.get("finished_at"),
                        _canonical_json(task["result"]) if task.get("result") is not None else None,
                        _canonical_json(task["validation"]) if task.get("validation") is not None else None,
                        float(task.get("estimated_cost", 0.0)),
                    ),
                )

    def get_projection(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revision,schema_version,state_json,state_hash FROM projections WHERE name='run' AND entity_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["state_json"])
        if _state_hash(raw) != row["state_hash"] or row["schema_version"] != RUNTIME_SCHEMA_VERSION:
            return self.replay(run_id)
        state = json.loads(raw)
        state.setdefault("runtime", {})["revision"] = int(row["revision"])
        return state

    def list_projections(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT entity_id FROM projections WHERE name='run' ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [state for row in rows if (state := self.get_projection(row["entity_id"]))]

    def list_events(
        self,
        run_id: str,
        *,
        after_revision: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT e.stream_revision,e.command_id,e.attempt_id,"
                "e.payload_json,e.envelope_json,i.payload_hash,i.envelope_hash "
                "FROM events e LEFT JOIN event_integrity i USING(event_id) "
                "WHERE e.run_id=? AND e.stream_revision>? "
                "ORDER BY e.stream_revision LIMIT ?",
                (run_id, max(0, int(after_revision)), max(1, min(int(limit), 1000))),
            ).fetchall()
        result = []
        for row in rows:
            revision = int(row["stream_revision"])
            if (
                row["payload_hash"] is None
                or row["envelope_hash"] is None
                or _state_hash(str(row["payload_json"])) != row["payload_hash"]
                or _state_hash(str(row["envelope_json"])) != row["envelope_hash"]
            ):
                raise ProjectionIntegrityError(
                    f"event integrity check failed at revision {revision}"
                )
            result.append({
                **json.loads(row["envelope_json"]),
                "revision": revision,
                "command_id": row["command_id"],
                "attempt_id": row["attempt_id"],
            })
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT run_id,task_id,legacy_task_id,revision,status,task_json "
                "FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        contract = json.loads(row["task_json"])
        contract.pop("result", None)
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "legacy_task_id": row["legacy_task_id"],
            "revision": int(row["revision"]),
            "status": row["status"],
            "contract": contract,
        }

    def replay(self, run_id: str) -> dict[str, Any] | None:
        """Rebuild the run projection solely from the append-only event stream."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT e.event_id,e.stream_revision,e.payload_json,e.envelope_json,"
                "i.payload_hash,i.envelope_hash FROM events e "
                "LEFT JOIN event_integrity i USING(event_id) "
                "WHERE e.run_id=? ORDER BY e.stream_revision",
                (run_id,),
            ).fetchall()
        state: dict[str, Any] | None = None
        revision = 0
        for row in rows:
            expected = revision + 1
            actual = int(row["stream_revision"])
            if actual != expected:
                raise ProjectionIntegrityError(
                    f"event stream revision gap: expected {expected}, got {actual}"
                )
            if (
                row["payload_hash"] is None
                or row["envelope_hash"] is None
                or _state_hash(str(row["payload_json"])) != row["payload_hash"]
                or _state_hash(str(row["envelope_json"])) != row["envelope_hash"]
            ):
                raise ProjectionIntegrityError(
                    f"event integrity check failed at revision {actual}"
                )
            payload = json.loads(row["payload_json"])
            projection = payload.get("projection")
            if not isinstance(projection, dict):
                raise ProjectionIntegrityError("event does not contain a run projection")
            state = projection
            revision = actual
        if state is not None:
            state.setdefault("runtime", {})["revision"] = revision
        return state

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "commands",
                    "events",
                    "runs",
                    "tasks",
                    "attempts",
                    "projections",
                )
            }
