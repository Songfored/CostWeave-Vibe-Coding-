"""Append-only v0.5 stable release attestations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .security_boundary import normalize_project_id


class ReleaseStoreError(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReleaseStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def create_attestation(
        self,
        *,
        project_id: str,
        manifest: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        project_id = normalize_project_id(project_id)
        key = " ".join(str(idempotency_key).strip().split())
        if len(key) < 8 or len(key) > 200:
            raise ValueError("release attestation idempotency key is invalid")
        payload = dict(manifest)
        if not payload.get("manifest_hash"):
            raise ValueError("release manifest hash is required")
        raw = _canonical_json(payload)
        digest = _sha256(raw)
        attestation_id = "release_" + _sha256(f"{project_id}:{key}")[:32]
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM release_attestations_v0584 "
                    "WHERE project_id=? AND idempotency_key=?",
                    (project_id, key),
                ).fetchone()
                if existing is not None:
                    if existing["manifest_hash"] != digest:
                        raise ReleaseStoreError(
                            "release attestation key reused for a different manifest"
                        )
                    connection.rollback()
                    return self._row(existing), True
                connection.execute(
                    "INSERT INTO release_attestations_v0584("
                    "attestation_id,project_id,idempotency_key,manifest_json,"
                    "manifest_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (attestation_id, project_id, key, raw, digest, created_at),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ReleaseStoreError(str(exc)) from exc
            except Exception:
                connection.rollback()
                raise
        return {
            "attestation_id": attestation_id,
            "project_id": project_id,
            "manifest": payload,
            "manifest_hash": digest,
            "created_at": created_at,
        }, False

    def list_attestations(
        self, *, project_id: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        project_id = normalize_project_id(project_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM release_attestations_v0584 WHERE project_id=? "
                "ORDER BY created_at DESC,attestation_id DESC LIMIT ?",
                (project_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._row(row) for row in rows]

    def get_by_key(
        self, *, project_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        project_id = normalize_project_id(project_id)
        key = " ".join(str(idempotency_key).strip().split())
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM release_attestations_v0584 "
                "WHERE project_id=? AND idempotency_key=?",
                (project_id, key),
            ).fetchone()
        return self._row(row) if row is not None else None

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM release_attestations_v0584"
            ).fetchone()[0]
        return {"release_attestations_v0584": int(count)}

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attestation_id": row["attestation_id"],
            "project_id": row["project_id"],
            "manifest": json.loads(row["manifest_json"]),
            "manifest_hash": row["manifest_hash"],
            "created_at": row["created_at"],
        }


__all__ = ["ReleaseStore", "ReleaseStoreError"]
