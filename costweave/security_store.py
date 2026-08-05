"""Append-only security state for CostWeave v0.5.8.2."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .commands import validate_idempotency_key
from .contracts_v4 import utc_now
from .security_boundary import (
    SECURITY_SCHEMA_VERSION,
    normalize_project_id,
    redact_structure,
)


class SecurityStoreError(ValueError):
    """Raised when security state is invalid or corrupted."""


class SecurityStateConflictError(SecurityStoreError):
    """Raised when a stable identity is reused for different content."""


class ProjectRevokedError(SecurityStoreError):
    """Raised when a logically deleted project attempts new access."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SecurityStore:
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

    def project_status(self, project_id: str) -> dict[str, Any]:
        project = normalize_project_id(project_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT tombstone_json,tombstone_hash FROM security_project_tombstones WHERE project_id=?",
                (project,),
            ).fetchone()
        if row is None:
            return {
                "project_id": project,
                "status": "active",
                "logical_access_revoked": False,
                "physical_rows_deleted": False,
                "schema_version": SECURITY_SCHEMA_VERSION,
            }
        raw = str(row["tombstone_json"])
        if _digest(raw) != row["tombstone_hash"]:
            raise SecurityStoreError("project tombstone integrity failed")
        return json.loads(raw)

    def ensure_project_active(self, project_id: str) -> str:
        project = normalize_project_id(project_id)
        if self.project_status(project)["logical_access_revoked"]:
            raise ProjectRevokedError("project access has been logically revoked")
        return project

    def revoke_project(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        reason: str = "user_requested",
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        project = normalize_project_id(project_id)
        key = validate_idempotency_key(idempotency_key)
        if reason not in {"user_requested", "retention_expired", "security_response"}:
            raise SecurityStoreError("project revocation reason is invalid")
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT tombstone_json,tombstone_hash FROM security_project_tombstones WHERE project_id=?",
                (project,),
            ).fetchone()
        if existing is not None:
            raw = str(existing["tombstone_json"])
            if _digest(raw) != existing["tombstone_hash"]:
                raise SecurityStoreError("project tombstone integrity failed")
            persisted = json.loads(raw)
            if (
                persisted.get("reason") != reason
                or persisted.get("idempotency_key_hash") != _digest(key)
            ):
                raise SecurityStateConflictError(
                    "project already has a different revocation tombstone"
                )
            return persisted, True
        tombstone = {
            "project_id": project,
            "status": "revoked",
            "logical_access_revoked": True,
            "physical_rows_deleted": False,
            "reason": reason,
            "idempotency_key_hash": _digest(key),
            "created_at": created_at or utc_now(),
            "schema_version": SECURITY_SCHEMA_VERSION,
        }
        raw = _canonical_json(tombstone)
        digest = _digest(raw)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT tombstone_json,tombstone_hash FROM security_project_tombstones WHERE project_id=?",
                    (project,),
                ).fetchone()
                if existing is not None:
                    persisted_raw = str(existing["tombstone_json"])
                    if _digest(persisted_raw) != existing["tombstone_hash"]:
                        raise SecurityStoreError("project tombstone integrity failed")
                    persisted = json.loads(persisted_raw)
                    if (
                        persisted.get("reason") != reason
                        or persisted.get("idempotency_key_hash") != _digest(key)
                    ):
                        raise SecurityStateConflictError(
                            "project already has a different revocation tombstone"
                        )
                    connection.execute("COMMIT")
                    return persisted, True
                connection.execute(
                    "INSERT INTO security_project_tombstones(project_id,reason,idempotency_key_hash,tombstone_json,tombstone_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (project, reason, _digest(key), raw, digest, tombstone["created_at"]),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return tombstone, False

    def reserve_side_effect(
        self,
        *,
        project_id: str,
        adapter_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        provider_idempotency_key: str | None,
        compensation_strategy: str | None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        project = self.ensure_project_active(project_id)
        key = validate_idempotency_key(idempotency_key)
        if not re_safe_identity(adapter_id) or not re_safe_identity(operation):
            raise SecurityStoreError("side-effect adapter or operation identity is invalid")
        if len(request_hash) != 64:
            raise SecurityStoreError("side-effect request hash is invalid")
        provider_key = provider_idempotency_key or None
        compensation = compensation_strategy or None
        if provider_key is None and compensation is None:
            raise SecurityStoreError(
                "external side effect requires Provider idempotency or compensation"
            )
        if provider_key is not None:
            provider_key = validate_idempotency_key(provider_key)
        if compensation is not None and compensation not in {
            "manual_reconciliation",
            "provider_cancel",
            "not_required_idempotent",
        }:
            raise SecurityStoreError("compensation strategy is invalid")
        side_effect_id = "side_effect_" + _digest(
            {"project_id": project, "idempotency_key": key}
        )[:24]
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT contract_json,contract_hash FROM external_side_effect_contracts WHERE side_effect_id=?",
                (side_effect_id,),
            ).fetchone()
        if existing is not None:
            raw = str(existing["contract_json"])
            if _digest(raw) != existing["contract_hash"]:
                raise SecurityStoreError("side-effect contract integrity failed")
            persisted = json.loads(raw)
            if (
                persisted.get("project_id") != project
                or persisted.get("adapter_id") != adapter_id
                or persisted.get("operation") != operation
                or persisted.get("request_hash") != request_hash
                or persisted.get("provider_idempotency_forwarded") != (provider_key is not None)
                or persisted.get("compensation_strategy") != compensation
            ):
                raise SecurityStateConflictError(
                    "side-effect idempotency key was reused for different content"
                )
            return persisted, True
        contract = {
            "side_effect_id": side_effect_id,
            "project_id": project,
            "adapter_id": adapter_id,
            "operation": operation,
            "idempotency_key_hash": _digest(key),
            "request_hash": request_hash,
            "provider_idempotency_forwarded": provider_key is not None,
            "compensation_strategy": compensation,
            "ambiguous_auto_replay": False,
            "created_at": created_at or utc_now(),
            "schema_version": SECURITY_SCHEMA_VERSION,
        }
        raw = _canonical_json(contract)
        contract_hash = _digest(raw)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT contract_json,contract_hash FROM external_side_effect_contracts WHERE side_effect_id=?",
                    (side_effect_id,),
                ).fetchone()
                if existing is not None:
                    persisted_raw = str(existing["contract_json"])
                    if _digest(persisted_raw) != existing["contract_hash"]:
                        raise SecurityStoreError("side-effect contract integrity failed")
                    persisted = json.loads(persisted_raw)
                    if (
                        persisted.get("project_id") != project
                        or persisted.get("adapter_id") != adapter_id
                        or persisted.get("operation") != operation
                        or persisted.get("request_hash") != request_hash
                        or persisted.get("provider_idempotency_forwarded") != (provider_key is not None)
                        or persisted.get("compensation_strategy") != compensation
                    ):
                        raise SecurityStateConflictError(
                            "side-effect idempotency key was reused for different content"
                        )
                    connection.execute("COMMIT")
                    return persisted, True
                connection.execute(
                    "INSERT INTO external_side_effect_contracts(side_effect_id,project_id,adapter_id,operation,idempotency_key_hash,request_hash,provider_idempotency_forwarded,compensation_strategy,ambiguous_auto_replay,contract_json,contract_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        side_effect_id,
                        project,
                        adapter_id,
                        operation,
                        _digest(key),
                        request_hash,
                        int(provider_key is not None),
                        compensation,
                        0,
                        raw,
                        contract_hash,
                        contract["created_at"],
                    ),
                )
                self._append_side_effect_event(
                    connection,
                    side_effect_id=side_effect_id,
                    status="reserved",
                    detail={"transport_called": False},
                    created_at=contract["created_at"],
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return contract, False

    def record_side_effect_event(
        self,
        side_effect_id: str,
        *,
        status: str,
        detail: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if status not in {"completed", "failed", "ambiguous", "reconciled"}:
            raise SecurityStoreError("side-effect event status is invalid")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM external_side_effect_contracts WHERE side_effect_id=?",
                    (side_effect_id,),
                ).fetchone() is None:
                    raise SecurityStoreError("side-effect contract is missing")
                event, replayed = self._append_side_effect_event(
                    connection,
                    side_effect_id=side_effect_id,
                    status=status,
                    detail=detail or {},
                    created_at=created_at or utc_now(),
                )
                connection.execute("COMMIT")
                return event, replayed
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _append_side_effect_event(
        self,
        connection: sqlite3.Connection,
        *,
        side_effect_id: str,
        status: str,
        detail: Mapping[str, Any],
        created_at: str,
    ) -> tuple[dict[str, Any], bool]:
        safe_detail = redact_structure(dict(detail))
        identity = {
            "side_effect_id": side_effect_id,
            "status": status,
            "detail": safe_detail,
            "schema_version": SECURITY_SCHEMA_VERSION,
        }
        event_id = "side_effect_event_" + _digest(identity)[:24]
        event = {"event_id": event_id, **identity, "created_at": created_at}
        raw = _canonical_json(event)
        digest = _digest(raw)
        existing = connection.execute(
            "SELECT event_json,event_hash FROM external_side_effect_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            persisted_raw = str(existing["event_json"])
            if _digest(persisted_raw) != existing["event_hash"]:
                raise SecurityStoreError("side-effect event integrity failed")
            persisted = json.loads(persisted_raw)
            if any(persisted.get(key) != value for key, value in identity.items()):
                raise SecurityStateConflictError("side-effect event integrity conflict")
            return persisted, True
        connection.execute(
            "INSERT INTO external_side_effect_events(event_id,side_effect_id,status,event_json,event_hash,created_at) VALUES(?,?,?,?,?,?)",
            (event_id, side_effect_id, status, raw, digest, created_at),
        )
        return event, False

    def side_effect(self, side_effect_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT contract_json,contract_hash FROM external_side_effect_contracts WHERE side_effect_id=?",
                (side_effect_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT event_json,event_hash FROM external_side_effect_events WHERE side_effect_id=? ORDER BY created_at,event_id",
                (side_effect_id,),
            ).fetchall()
        if row is None:
            return None
        raw = str(row["contract_json"])
        if _digest(raw) != row["contract_hash"]:
            raise SecurityStoreError("side-effect contract integrity failed")
        parsed_events: list[dict[str, Any]] = []
        for event_row in events:
            event_raw = str(event_row["event_json"])
            if _digest(event_raw) != event_row["event_hash"]:
                raise SecurityStoreError("side-effect event integrity failed")
            parsed_events.append(json.loads(event_raw))
        return {"contract": json.loads(raw), "events": parsed_events}

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM security_project_tombstones) AS tombstones,"
                "(SELECT COUNT(*) FROM external_side_effect_contracts) AS contracts,"
                "(SELECT COUNT(*) FROM external_side_effect_events) AS events,"
                "(SELECT COUNT(*) FROM external_side_effect_events WHERE status='ambiguous') AS ambiguous"
            ).fetchone()
        return {
            "security_project_tombstones": int(row["tombstones"]),
            "external_side_effect_contracts": int(row["contracts"]),
            "external_side_effect_events": int(row["events"]),
            "ambiguous_external_side_effects": int(row["ambiguous"]),
        }


def re_safe_identity(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", str(value)))


__all__ = [
    "ProjectRevokedError",
    "SecurityStateConflictError",
    "SecurityStore",
    "SecurityStoreError",
]
