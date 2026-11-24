"""Append-only security state for CostWeave v0.5.8.2."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .commands import validate_idempotency_key
from .contracts_v4 import utc_now
from .security_boundary import (
    DEFAULT_DATA_RESIDENCY,
    SECURITY_SCHEMA_VERSION,
    normalize_data_residency,
    normalize_project_id,
    redact_structure,
)


class _ProjectOperationGate:
    """Reentrant shared-active / exclusive-revocation barrier per project."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers: dict[int, int] = {}
        self._writers_waiting = 0
        self._writer_thread: int | None = None
        self._writer_depth = 0

    @contextmanager
    def shared(self):
        ident = threading.get_ident()
        with self._cond:
            if self._writer_thread == ident:
                self._readers[ident] = self._readers.get(ident, 0) + 1
            elif ident in self._readers:
                self._readers[ident] += 1
            else:
                while self._writer_thread is not None or self._writers_waiting > 0:
                    self._cond.wait()
                self._readers[ident] = 1
        try:
            yield
        finally:
            with self._cond:
                remaining = self._readers[ident] - 1
                if remaining <= 0:
                    del self._readers[ident]
                    if not self._readers:
                        self._cond.notify_all()
                else:
                    self._readers[ident] = remaining

    @contextmanager
    def exclusive(self):
        ident = threading.get_ident()
        with self._cond:
            if self._writer_thread == ident:
                self._writer_depth += 1
            else:
                self._writers_waiting += 1
                try:
                    while self._writer_thread is not None or any(t != ident for t in self._readers):
                        self._cond.wait()
                    self._writer_thread = ident
                    self._writer_depth = 1
                finally:
                    self._writers_waiting -= 1
        try:
            yield
        finally:
            with self._cond:
                self._writer_depth -= 1
                if self._writer_depth <= 0:
                    self._writer_thread = None
                    self._writer_depth = 0
                    self._cond.notify_all()


_PROJECT_OPERATION_REGISTRY_LOCK = threading.Lock()
_PROJECT_OPERATION_GATES: dict[tuple[str, str], _ProjectOperationGate] = {}


def _project_operation_gate(path: Path, project_id: str) -> _ProjectOperationGate:
    key = (str(path.resolve(strict=False)).casefold(), project_id)
    with _PROJECT_OPERATION_REGISTRY_LOCK:
        return _PROJECT_OPERATION_GATES.setdefault(key, _ProjectOperationGate())


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

    @contextmanager
    def active_project_operation(self, project_id: str):
        """Serialize an authorized mutation with project revocation.

        Callers must keep the context only around the final durable mutation.
        A revocation either happens before the check (and the mutation fails) or
        after the mutation has completed; it can no longer interleave between
        authorization and commit.
        """

        project = normalize_project_id(project_id)
        with _project_operation_gate(self.path, project).shared():
            yield self.ensure_project_active(project)

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
        with (
            _project_operation_gate(self.path, project).exclusive(),
            self._lock,
            self._connection() as connection,
        ):
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
        data_residency: str,
        adapter_id: str,
        provider: str,
        deployment_id: str,
        endpoint: str,
        model_id: str,
        adapter_revision: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        provider_idempotency_key: str | None,
        compensation_strategy: str | None,
        origin_external_call_id: str | None = None,
        authority_nonce: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        project = self.ensure_project_active(project_id)
        residency = normalize_data_residency(data_residency)
        key = validate_idempotency_key(idempotency_key)
        scoped_key = (
            key
            if residency == DEFAULT_DATA_RESIDENCY
            else f"residency:{residency}:{key}"
        )
        scoped_key_hash = _digest(scoped_key)
        if not re_safe_identity(adapter_id) or not re_safe_identity(operation):
            raise SecurityStoreError("side-effect adapter or operation identity is invalid")
        destination = {
            "provider": _contract_identity(provider, "provider"),
            "deployment_id": _contract_identity(
                deployment_id, "deployment_id"
            ),
            "endpoint": _contract_identity(endpoint, "endpoint", maximum=4096),
            "model_id": _contract_identity(model_id, "model_id"),
            "adapter_revision": _contract_identity(
                adapter_revision, "adapter_revision"
            ),
        }
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
        origin_call_id = (
            None
            if origin_external_call_id is None
            else _contract_identity(
                origin_external_call_id,
                "origin_external_call_id",
                maximum=192,
            )
        )
        if origin_call_id is not None:
            with self._connection() as connection:
                origin = connection.execute(
                    "SELECT provider,deployment_id,model_id,adapter_revision "
                    "FROM external_calls WHERE external_call_id=?",
                    (origin_call_id,),
                ).fetchone()
            if origin is None or any(
                str(origin[key]) != destination[key]
                for key in (
                    "provider",
                    "deployment_id",
                    "model_id",
                    "adapter_revision",
                )
            ):
                raise SecurityStoreError(
                    "side-effect origin call does not match the destination"
                )
        nonce_hash = None
        if authority_nonce is not None:
            nonce = _contract_identity(
                authority_nonce, "authority_nonce", maximum=256
            )
            nonce_hash = _digest(nonce)
        side_effect_id = "side_effect_" + _digest(
            {
                "project_id": project,
                "data_residency": residency,
                "idempotency_key": scoped_key,
                **destination,
            }
        )[:24]
        expected_contract = {
            "project_id": project,
            "data_residency": residency,
            "adapter_id": adapter_id,
            **destination,
            "operation": operation,
            "request_hash": request_hash,
            "provider_idempotency_forwarded": provider_key is not None,
            "provider_idempotency_key_hash": (
                _digest(provider_key) if provider_key is not None else None
            ),
            "compensation_strategy": compensation,
        }
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
            if not _side_effect_contract_matches(
                persisted, expected_contract
            ):
                raise SecurityStateConflictError(
                    "side-effect idempotency key was reused for different content"
                )
            return persisted, True
        contract = {
            "side_effect_id": side_effect_id,
            "project_id": project,
            "data_residency": residency,
            "adapter_id": adapter_id,
            **destination,
            "operation": operation,
            "idempotency_key_hash": scoped_key_hash,
            "request_hash": request_hash,
            "provider_idempotency_forwarded": provider_key is not None,
            "provider_idempotency_key_hash": (
                _digest(provider_key) if provider_key is not None else None
            ),
            "compensation_strategy": compensation,
            "ambiguous_auto_replay": False,
            "origin_external_call_id": origin_call_id,
            "authority_nonce_hash": nonce_hash,
            "created_at": created_at or utc_now(),
            "schema_version": SECURITY_SCHEMA_VERSION,
        }
        raw = _canonical_json(contract)
        contract_hash = _digest(raw)
        with (
            _project_operation_gate(self.path, project).exclusive(),
            self._lock,
            self._connection() as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM security_project_tombstones WHERE project_id=?",
                    (project,),
                ).fetchone() is not None:
                    raise ProjectRevokedError(
                        "project access has been logically revoked"
                    )
                existing = connection.execute(
                    "SELECT contract_json,contract_hash FROM external_side_effect_contracts WHERE side_effect_id=?",
                    (side_effect_id,),
                ).fetchone()
                if existing is not None:
                    persisted_raw = str(existing["contract_json"])
                    if _digest(persisted_raw) != existing["contract_hash"]:
                        raise SecurityStoreError("side-effect contract integrity failed")
                    persisted = json.loads(persisted_raw)
                    if not _side_effect_contract_matches(
                        persisted, expected_contract
                    ):
                        raise SecurityStateConflictError(
                            "side-effect idempotency key was reused for different content"
                        )
                    connection.execute("COMMIT")
                    return persisted, True
                keyed = connection.execute(
                    "SELECT contract_json,contract_hash FROM "
                    "external_side_effect_contracts "
                    "WHERE project_id=? AND idempotency_key_hash=?",
                    (project, scoped_key_hash),
                ).fetchone()
                if keyed is not None:
                    keyed_raw = str(keyed["contract_json"])
                    if _digest(keyed_raw) != keyed["contract_hash"]:
                        raise SecurityStoreError(
                            "side-effect contract integrity failed"
                        )
                    raise SecurityStateConflictError(
                        "side-effect idempotency key was reused for a "
                        "different destination or request"
                    )
                connection.execute(
                    "INSERT INTO external_side_effect_contracts(side_effect_id,project_id,adapter_id,operation,idempotency_key_hash,request_hash,provider_idempotency_forwarded,compensation_strategy,ambiguous_auto_replay,contract_json,contract_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        side_effect_id,
                        project,
                        adapter_id,
                        operation,
                        scoped_key_hash,
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
        authority_nonce: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if status not in {"completed", "failed", "ambiguous", "reconciled"}:
            raise SecurityStoreError("side-effect event status is invalid")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                contract_row = connection.execute(
                    "SELECT project_id,contract_json,contract_hash "
                    "FROM external_side_effect_contracts WHERE side_effect_id=?",
                    (side_effect_id,),
                ).fetchone()
                if contract_row is None:
                    raise SecurityStoreError("side-effect contract is missing")
                contract_raw = str(contract_row["contract_json"])
                if _digest(contract_raw) != contract_row["contract_hash"]:
                    raise SecurityStoreError("side-effect contract integrity failed")
                contract = json.loads(contract_raw)
                expected_nonce_hash = str(
                    contract.get("authority_nonce_hash") or ""
                )
                supplied_nonce_hash = (
                    _digest(authority_nonce)
                    if isinstance(authority_nonce, str) and authority_nonce
                    else ""
                )
                if (
                    not expected_nonce_hash
                    or not hmac.compare_digest(
                        expected_nonce_hash, supplied_nonce_hash
                    )
                ):
                    raise SecurityStoreError(
                        "side-effect terminal event lacks authority proof"
                    )
                if connection.execute(
                    "SELECT 1 FROM security_project_tombstones WHERE project_id=?",
                    (str(contract_row["project_id"]),),
                ).fetchone() is not None:
                    raise ProjectRevokedError(
                        "project access has been logically revoked"
                    )
                safe_detail = redact_structure(dict(detail or {}))
                self._validate_side_effect_source(
                    connection,
                    contract=contract,
                    status=status,
                    detail=safe_detail,
                )
                latest_row = connection.execute(
                    "SELECT event_json,event_hash FROM external_side_effect_events "
                    "WHERE side_effect_id=? ORDER BY rowid DESC LIMIT 1",
                    (side_effect_id,),
                ).fetchone()
                if latest_row is None:
                    raise SecurityStoreError("side-effect reservation event is missing")
                latest_raw = str(latest_row["event_json"])
                if _digest(latest_raw) != latest_row["event_hash"]:
                    raise SecurityStoreError("side-effect event integrity failed")
                latest = json.loads(latest_raw)
                if latest.get("status") == status and latest.get("detail") == safe_detail:
                    event, replayed = self._append_side_effect_event(
                        connection,
                        side_effect_id=side_effect_id,
                        status=status,
                        detail=safe_detail,
                        created_at=created_at or utc_now(),
                    )
                    connection.execute("COMMIT")
                    return event, replayed
                allowed = {
                    "reserved": {"completed", "failed", "ambiguous"},
                    "ambiguous": {"reconciled"},
                }
                if status not in allowed.get(str(latest.get("status")), set()):
                    raise SecurityStateConflictError(
                        "side-effect terminal state transition is invalid"
                    )
                event, replayed = self._append_side_effect_event(
                    connection,
                    side_effect_id=side_effect_id,
                    status=status,
                    detail=safe_detail,
                    created_at=created_at or utc_now(),
                )
                connection.execute("COMMIT")
                return event, replayed
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_side_effect_source(
        connection: sqlite3.Connection,
        *,
        contract: Mapping[str, Any],
        status: str,
        detail: Mapping[str, Any],
    ) -> None:
        origin_call_id = str(contract.get("origin_external_call_id") or "")
        if not origin_call_id or detail.get("external_call_id") != origin_call_id:
            raise SecurityStoreError(
                "side-effect event is not bound to its origin external call"
            )
        transport_called = detail.get("transport_called")
        if not isinstance(transport_called, bool):
            raise SecurityStoreError(
                "side-effect event requires authoritative transport state"
            )
        if status in {"completed", "ambiguous"} and not transport_called:
            raise SecurityStoreError(
                "side-effect terminal state requires a transport attempt"
            )
        source = connection.execute(
            "SELECT external_call_id,provider,deployment_id,model_id,"
            "adapter_revision,status,result_json,telemetry_json "
            "FROM external_calls WHERE external_call_id=?",
            (origin_call_id,),
        ).fetchone()
        if source is None or any(
            str(source[key]) != str(contract.get(key) or "")
            for key in (
                "provider",
                "deployment_id",
                "model_id",
                "adapter_revision",
            )
        ):
            raise SecurityStoreError(
                "side-effect source call does not match its immutable contract"
            )
        if status not in {"completed", "reconciled"}:
            if source["status"] not in {"reserved", "failed", "cancelled"}:
                raise SecurityStoreError(
                    "side-effect failure state does not match its source call"
                )
            return
        if source["status"] != "completed" or not source["result_json"] or not source["telemetry_json"]:
            raise SecurityStoreError(
                "completed side effect requires a completed durable source call"
            )
        try:
            result = json.loads(str(source["result_json"]))
            telemetry = json.loads(str(source["telemetry_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SecurityStoreError(
                "side-effect source receipt is malformed"
            ) from error
        expected = {
            "external_call_id": origin_call_id,
            "project_id": contract.get("project_id"),
            "data_residency": contract.get("data_residency"),
            "adapter_id": contract.get("adapter_id"),
            "provider": contract.get("provider"),
            "deployment_id": contract.get("deployment_id"),
            "endpoint": contract.get("endpoint"),
            "model_id": contract.get("model_id"),
            "adapter_revision": contract.get("adapter_revision"),
            "operation": contract.get("operation"),
            "request_hash": contract.get("request_hash"),
            "provider_idempotency_key_hash": contract.get(
                "provider_idempotency_key_hash"
            ),
            "side_effect_id": contract.get("side_effect_id"),
        }
        if not isinstance(telemetry, Mapping) or any(
            telemetry.get(key) != value for key, value in expected.items()
        ):
            raise SecurityStoreError(
                "completed side-effect telemetry does not match its contract"
            )
        if (
            not isinstance(result, Mapping)
            or result.get("external_call_id") != origin_call_id
            or not isinstance(result.get("telemetry"), Mapping)
            or any(
                result["telemetry"].get(key) != value
                for key, value in expected.items()
            )
        ):
            raise SecurityStoreError(
                "completed side-effect result does not match its source receipt"
            )

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


def _contract_identity(
    value: str, name: str, *, maximum: int = 512
) -> str:
    if not isinstance(value, str):
        raise SecurityStoreError(f"side-effect {name} must be text")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise SecurityStoreError(f"side-effect {name} is invalid")
    return normalized


def _side_effect_contract_matches(
    persisted: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    try:
        persisted_residency = normalize_data_residency(
            str(
                persisted.get("data_residency")
                or DEFAULT_DATA_RESIDENCY
            )
        )
    except ValueError:
        return False
    return persisted_residency == expected["data_residency"] and all(
        persisted.get(key) == value
        for key, value in expected.items()
        if key != "data_residency"
    )


__all__ = [
    "ProjectRevokedError",
    "SecurityStateConflictError",
    "SecurityStore",
    "SecurityStoreError",
]
