"""Append-only export receipts for the v0.5.8.1 evidence console."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .evidence_console import (
    EVIDENCE_EXPORT_SCHEMA_VERSION,
    validate_evidence_export,
)


class EvidenceConsoleStoreError(ValueError):
    """Raised when an evidence export receipt is invalid or corrupted."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EvidenceConsoleStore:
    """Persists only immutable export receipts, never a duplicate fact store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

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

    def record_export(
        self,
        package: Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        validate_evidence_export(package)
        if package.get("schema_version") != EVIDENCE_EXPORT_SCHEMA_VERSION:
            raise EvidenceConsoleStoreError("unsupported export schema")
        integrity = package.get("integrity")
        if not isinstance(integrity, Mapping):
            raise EvidenceConsoleStoreError("export integrity is missing")
        package_hash = str(integrity.get("package_hash") or "")
        if len(package_hash) != 64:
            raise EvidenceConsoleStoreError("package hash is invalid")
        manifest = package.get("manifest")
        scope = package.get("scope")
        console = package.get("console")
        if not isinstance(manifest, Mapping) or not isinstance(scope, Mapping):
            raise EvidenceConsoleStoreError("export manifest or scope is invalid")
        if not isinstance(console, Mapping):
            raise EvidenceConsoleStoreError("console snapshot is missing")
        scope_kind = str(scope.get("kind") or "")
        if scope_kind not in {"workspace", "run"}:
            raise EvidenceConsoleStoreError("export scope is invalid")
        run_id = scope.get("run_id")
        if scope_kind == "run" and not run_id:
            raise EvidenceConsoleStoreError("run export requires run_id")
        if scope_kind == "workspace" and run_id is not None:
            raise EvidenceConsoleStoreError("workspace export cannot contain run_id")
        source_count = int(manifest.get("source_count") or 0)
        record_count = int(manifest.get("record_count") or 0)
        coverage = console.get("coverage") or {}
        if source_count < 0 or record_count < 0 or not isinstance(coverage, Mapping):
            raise EvidenceConsoleStoreError("export counts are invalid")
        timestamp = created_at or str(package.get("generated_at") or utc_now())
        export_id = "evidence_export_" + package_hash[:24]
        receipt = {
            "export_id": export_id,
            "schema_version": EVIDENCE_EXPORT_SCHEMA_VERSION,
            "scope": scope_kind,
            "run_id": str(run_id) if run_id is not None else None,
            "package_hash": package_hash,
            "source_count": source_count,
            "record_count": record_count,
            "evidence_counts": {
                key: int(coverage.get(key) or 0)
                for key in ("actual", "predicted", "simulated", "missing")
            },
            "commercial_claims_allowed": False,
            "created_at": timestamp,
        }
        raw = _canonical_json(receipt)
        receipt_hash = _hash(raw)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT receipt_json,receipt_hash FROM evidence_export_receipts "
                    "WHERE export_id=?",
                    (export_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["receipt_json"] != raw
                        or existing["receipt_hash"] != receipt_hash
                    ):
                        raise EvidenceConsoleStoreError(
                            "export receipt id was reused with different content"
                        )
                    connection.execute("COMMIT")
                    return receipt, True
                connection.execute(
                    "INSERT INTO evidence_export_receipts("
                    "export_id,run_id,scope,schema_version,package_hash,"
                    "source_count,record_count,evidence_counts_json,"
                    "commercial_claims_allowed,receipt_json,receipt_hash,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        export_id,
                        receipt["run_id"],
                        scope_kind,
                        EVIDENCE_EXPORT_SCHEMA_VERSION,
                        package_hash,
                        source_count,
                        record_count,
                        _canonical_json(receipt["evidence_counts"]),
                        0,
                        raw,
                        receipt_hash,
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return receipt, False

    def list_exports(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 1_000:
            raise EvidenceConsoleStoreError("export receipt limit is invalid")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT receipt_json,receipt_hash FROM evidence_export_receipts "
                "ORDER BY created_at DESC,export_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            raw = str(row["receipt_json"])
            if _hash(raw) != row["receipt_hash"]:
                raise EvidenceConsoleStoreError(
                    "persisted export receipt integrity failed"
                )
            result.append(json.loads(raw))
        return result

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total,"
                "COALESCE(SUM(scope='run'),0) AS run_total,"
                "COALESCE(SUM(scope='workspace'),0) AS workspace_total "
                "FROM evidence_export_receipts"
            ).fetchone()
        return {
            "evidence_export_receipts": int(row["total"]),
            "evidence_run_exports": int(row["run_total"]),
            "evidence_workspace_exports": int(row["workspace_total"]),
        }


__all__ = ["EvidenceConsoleStore", "EvidenceConsoleStoreError"]
