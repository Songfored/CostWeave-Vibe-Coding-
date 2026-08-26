"""Project-scoped immutable persistence for CostWeave v0.6.0 decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .event_store import EventStore
from .intelligence.common import (
    DECISION_INTELLIGENCE_SCHEMA_VERSION,
    bounded_text,
    canonical_json,
    stable_digest,
    validate_exact_fields,
    validate_sha256,
    validate_timestamp,
)
from .security_boundary import normalize_project_id


OBJECT_KINDS = (
    "task_belief",
    "system_belief",
    "capability_posterior",
    "strategy_candidate",
    "decision_trace",
    "outcome_credit",
)


class DecisionIntelligenceStoreError(RuntimeError):
    """Raised when an immutable decision object cannot be persisted safely."""


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _limit(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if not 1 <= value <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


class DecisionIntelligenceStore:
    """Persist v0.6 objects in the existing EventStore SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # EventStore is used only as the single migration/runtime owner; all
        # decision rows remain in the same SQLite file.
        self._runtime = EventStore(self.path)

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

    @staticmethod
    def _project(value: str) -> str:
        return normalize_project_id(value)

    @staticmethod
    def _object_id(value: str) -> str:
        return bounded_text(value, "object_id", minimum=8, maximum=200)

    @staticmethod
    def _key(value: str, name: str) -> str:
        return bounded_text(value, name, minimum=8, maximum=200)

    @staticmethod
    def _payload(payload: Mapping[str, Any], name: str) -> tuple[str, str]:
        if not isinstance(payload, Mapping):
            raise DecisionIntelligenceStoreError(f"{name} must be a JSON object")
        try:
            raw = canonical_json(dict(payload))
        except (TypeError, ValueError) as exc:
            raise DecisionIntelligenceStoreError(f"{name} must contain finite JSON values") from exc
        return raw, _sha256(raw)

    @staticmethod
    def _row_object(row: sqlite3.Row) -> dict[str, Any]:
        raw = str(row["payload_json"])
        digest = _sha256(raw)
        if digest != row["payload_hash"]:
            raise DecisionIntelligenceStoreError("persisted decision object hash mismatch")
        return {
            "project_id": row["project_id"],
            "object_id": row["object_id"],
            "object_kind": row["object_kind"],
            "schema_version": row["schema_version"],
            "payload": json.loads(raw),
            "payload_hash": row["payload_hash"],
            "recorded_at": row["recorded_at"],
        }

    @staticmethod
    def _row_tournament(row: sqlite3.Row) -> dict[str, Any]:
        raw = str(row["report_json"])
        if _sha256(raw) != row["report_hash"]:
            raise DecisionIntelligenceStoreError("persisted tournament report hash mismatch")
        return {
            "project_id": row["project_id"],
            "tournament_id": row["tournament_id"],
            "idempotency_key": row["idempotency_key"],
            "dataset_id": row["dataset_id"],
            "manifest_hash": row["manifest_hash"],
            "report": json.loads(raw),
            "report_hash": row["report_hash"],
            "created_at": row["created_at"],
        }

    def record_object(
        self,
        *,
        project_id: str,
        object_kind: str,
        object_id: str,
        payload: Mapping[str, Any],
        recorded_at: str,
    ) -> bool:
        project = self._project(project_id)
        if object_kind not in OBJECT_KINDS:
            raise DecisionIntelligenceStoreError("unsupported object_kind")
        object_key = self._object_id(object_id)
        timestamp = validate_timestamp(recorded_at, "recorded_at")
        raw, digest = self._payload(payload, "payload")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM decision_objects_v060 WHERE project_id=? AND object_id=?",
                    (project, object_key),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != digest or existing["payload_json"] != raw or existing["object_kind"] != object_kind:
                        raise DecisionIntelligenceStoreError("object id was reused with different content")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO decision_objects_v060(project_id,object_id,object_kind,schema_version,payload_json,payload_hash,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (project, object_key, object_kind, DECISION_INTELLIGENCE_SCHEMA_VERSION, raw, digest, timestamp),
                )
                connection.execute("COMMIT")
                return True
            except DecisionIntelligenceStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DecisionIntelligenceStoreError(str(exc)) from exc
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def object(self, *, project_id: str, object_id: str) -> dict[str, Any] | None:
        project = self._project(project_id)
        object_key = self._object_id(object_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM decision_objects_v060 WHERE project_id=? AND object_id=?",
                (project, object_key),
            ).fetchone()
        return self._row_object(row) if row is not None else None

    def list_objects(
        self,
        *,
        project_id: str,
        object_kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        project = self._project(project_id)
        count = _limit(limit, maximum=100)
        if object_kind is not None and object_kind not in OBJECT_KINDS:
            raise DecisionIntelligenceStoreError("unsupported object_kind")
        query = "SELECT * FROM decision_objects_v060 WHERE project_id=?"
        params: list[Any] = [project]
        if object_kind is not None:
            query += " AND object_kind=?"
            params.append(object_kind)
        query += " ORDER BY recorded_at DESC,object_id DESC LIMIT ?"
        params.append(count)
        with self._connection() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return [self._row_object(row) for row in rows]

    def record_tournament(
        self,
        *,
        project_id: str,
        tournament_id: str,
        idempotency_key: str,
        dataset_id: str,
        manifest_hash: str,
        report: Mapping[str, Any],
        created_at: str,
    ) -> tuple[dict[str, Any], bool]:
        project = self._project(project_id)
        tournament_key = self._key(tournament_id, "tournament_id")
        idem = self._key(idempotency_key, "idempotency_key")
        dataset = self._key(dataset_id, "dataset_id")
        manifest = validate_sha256(manifest_hash, "manifest_hash")
        timestamp = validate_timestamp(created_at, "created_at")
        raw, report_hash = self._payload(report, "report")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM tournament_runs_v060 WHERE project_id=? AND idempotency_key=?",
                    (project, idem),
                ).fetchone()
                if existing is None:
                    existing = connection.execute(
                        "SELECT * FROM tournament_runs_v060 WHERE project_id=? AND tournament_id=?",
                        (project, tournament_key),
                    ).fetchone()
                if existing is not None:
                    same = (
                        existing["tournament_id"] == tournament_key
                        and existing["idempotency_key"] == idem
                        and existing["dataset_id"] == dataset
                        and existing["manifest_hash"] == manifest
                        and existing["report_hash"] == report_hash
                        and existing["report_json"] == raw
                    )
                    if not same:
                        raise DecisionIntelligenceStoreError("tournament id or idempotency key was reused with different content")
                    result = self._row_tournament(existing)
                    connection.execute("COMMIT")
                    return result, True
                connection.execute(
                    "INSERT INTO tournament_runs_v060(project_id,tournament_id,idempotency_key,dataset_id,manifest_hash,report_json,report_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (project, tournament_key, idem, dataset, manifest, raw, report_hash, timestamp),
                )
                connection.execute("COMMIT")
            except DecisionIntelligenceStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DecisionIntelligenceStoreError(str(exc)) from exc
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return {
            "project_id": project,
            "tournament_id": tournament_key,
            "idempotency_key": idem,
            "dataset_id": dataset,
            "manifest_hash": manifest,
            "report": json.loads(raw),
            "report_hash": report_hash,
            "created_at": timestamp,
        }, False

    def tournament(self, *, project_id: str, tournament_id: str) -> dict[str, Any] | None:
        project = self._project(project_id)
        tournament_key = self._key(tournament_id, "tournament_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tournament_runs_v060 WHERE project_id=? AND tournament_id=?",
                (project, tournament_key),
            ).fetchone()
        return self._row_tournament(row) if row is not None else None

    def tournaments(self, *, project_id: str, limit: int = 25) -> list[dict[str, Any]]:
        project = self._project(project_id)
        count = _limit(limit, maximum=100)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tournament_runs_v060 WHERE project_id=? ORDER BY created_at DESC,tournament_id DESC LIMIT ?",
                (project, count),
            ).fetchall()
        return [self._row_tournament(row) for row in rows]

    def counts(self, *, project_id: str | None = None) -> dict[str, int]:
        project = self._project(project_id) if project_id is not None else None
        with self._connection() as connection:
            suffix = " WHERE project_id=?" if project is not None else ""
            params = (project,) if project is not None else ()
            objects = connection.execute(
                "SELECT COUNT(*) FROM decision_objects_v060" + suffix, params
            ).fetchone()[0]
            tournaments = connection.execute(
                "SELECT COUNT(*) FROM tournament_runs_v060" + suffix, params
            ).fetchone()[0]
        return {
            "decision_objects_v060": int(objects),
            "tournament_runs_v060": int(tournaments),
        }


__all__ = ["DecisionIntelligenceStore", "DecisionIntelligenceStoreError", "OBJECT_KINDS"]
