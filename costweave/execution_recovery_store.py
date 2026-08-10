"""Immutable, project-scoped persistence for v0.6.4 execution recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts_v4 import utc_now
from .event_store import EventStore
from .intelligence.common import canonical_json
from .intelligence.execution_recovery import (
    ExecutionRecoveryDecisionV1,
    ProgressBeliefV1,
)
from .security_boundary import normalize_project_id


class ExecutionRecoveryStoreError(RuntimeError):
    """Raised when an immutable recovery record cannot be trusted."""


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload(value: Any) -> tuple[str, str]:
    raw = canonical_json(value)
    return raw, _sha256(raw)


class ExecutionRecoveryStore:
    """Reuse the EventStore database; never create a second runtime store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._runtime = EventStore(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
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
    def _key(value: str, name: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 200:
            raise ValueError(f"{name} must contain 8-200 characters")
        return value.strip()

    @classmethod
    def _belief_row(cls, row: sqlite3.Row) -> ProgressBeliefV1:
        raw = str(row["belief_json"])
        if _sha256(raw) != row["belief_hash"]:
            raise ExecutionRecoveryStoreError("persisted execution belief hash mismatch")
        item = ProgressBeliefV1.from_dict(json.loads(raw))
        if item.belief_id != row["belief_id"] or row["project_id"] != item.project_id:
            raise ExecutionRecoveryStoreError("persisted execution belief identity mismatch")
        return item

    @classmethod
    def _decision_row(cls, row: sqlite3.Row) -> ExecutionRecoveryDecisionV1:
        raw = str(row["decision_json"])
        if _sha256(raw) != row["decision_hash"]:
            raise ExecutionRecoveryStoreError("persisted execution recovery decision hash mismatch")
        item = ExecutionRecoveryDecisionV1.from_dict(json.loads(raw))
        if item.decision_id != row["decision_id"] or item.project_id != row["project_id"]:
            raise ExecutionRecoveryStoreError("persisted execution recovery decision identity mismatch")
        return item

    def record_belief(self, belief: ProgressBeliefV1) -> bool:
        project = self._project(belief.project_id)
        raw, digest = _payload(belief.to_dict())
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT belief_hash,belief_json FROM execution_beliefs_v064 WHERE project_id=? AND belief_id=?",
                    (project, belief.belief_id),
                ).fetchone()
                if existing is not None:
                    if existing["belief_hash"] != digest or existing["belief_json"] != raw:
                        raise ExecutionRecoveryStoreError("belief id was reused with different content")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO execution_beliefs_v064(project_id,belief_id,case_id,belief_json,belief_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (project, belief.belief_id, belief.case_id, raw, digest, utc_now()),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def belief(self, *, project_id: str, belief_id: str) -> ProgressBeliefV1 | None:
        project = self._project(project_id)
        key = self._key(belief_id, "belief_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_beliefs_v064 WHERE project_id=? AND belief_id=?",
                (project, key),
            ).fetchone()
        return self._belief_row(row) if row is not None else None

    def record_decision(self, decision: ExecutionRecoveryDecisionV1, *, idempotency_key: str, request_hash: str) -> bool:
        project = self._project(decision.project_id)
        key = self._key(idempotency_key, "idempotency_key")
        if not isinstance(request_hash, str) or len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        decision_id = decision.decision_id or decision.computed_id
        if decision_id != decision.computed_id and not decision_id.startswith("recovery_decision_"):
            raise ExecutionRecoveryStoreError("decision_id has an invalid prefix")
        raw, digest = _payload(decision.to_dict())
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                idem = connection.execute(
                    "SELECT request_hash,decision_id FROM execution_recovery_idempotency_v064 WHERE project_id=? AND idempotency_key=?",
                    (project, key),
                ).fetchone()
                if idem is not None:
                    if idem["request_hash"] != request_hash or idem["decision_id"] != decision_id:
                        raise ExecutionRecoveryStoreError("execution recovery idempotency key was reused with different content")
                    connection.execute("COMMIT")
                    return False
                existing = connection.execute(
                    "SELECT decision_hash,decision_json FROM execution_recovery_decisions_v064 WHERE project_id=? AND decision_id=?",
                    (project, decision_id),
                ).fetchone()
                inserted = False
                if existing is not None:
                    if existing["decision_hash"] != digest or existing["decision_json"] != raw:
                        raise ExecutionRecoveryStoreError("decision id was reused with different content")
                else:
                    connection.execute(
                        "INSERT INTO execution_recovery_decisions_v064(project_id,decision_id,case_id,action,decision_json,decision_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                        (project, decision_id, decision.case_id, decision.action, raw, digest, utc_now()),
                    )
                    inserted = True
                connection.execute(
                    "INSERT INTO execution_recovery_idempotency_v064(project_id,idempotency_key,request_hash,decision_id,created_at) VALUES(?,?,?,?,?)",
                    (project, key, request_hash, decision_id, utc_now()),
                )
                connection.execute("COMMIT")
                return inserted
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def decision_for_idempotency(self, *, project_id: str, idempotency_key: str, request_hash: str) -> ExecutionRecoveryDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(idempotency_key, "idempotency_key")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_hash,decision_id FROM execution_recovery_idempotency_v064 WHERE project_id=? AND idempotency_key=?",
                (project, key),
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise ExecutionRecoveryStoreError("execution recovery idempotency key was reused with different request content")
            decision = connection.execute(
                "SELECT * FROM execution_recovery_decisions_v064 WHERE project_id=? AND decision_id=?",
                (project, row["decision_id"]),
            ).fetchone()
        if decision is None:
            raise ExecutionRecoveryStoreError("idempotency record points to missing decision")
        return self._decision_row(decision)

    def decision(self, *, project_id: str, decision_id: str) -> ExecutionRecoveryDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(decision_id, "decision_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_recovery_decisions_v064 WHERE project_id=? AND decision_id=?",
                (project, key),
            ).fetchone()
        return self._decision_row(row) if row is not None else None

    def decisions(self, *, project_id: str, limit: int = 25) -> list[ExecutionRecoveryDecisionV1]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_recovery_decisions_v064 WHERE project_id=? ORDER BY created_at DESC,decision_id DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        return [self._decision_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "execution_beliefs_v064",
                    "execution_recovery_decisions_v064",
                    "execution_recovery_idempotency_v064",
                )
            }


__all__ = ["ExecutionRecoveryStore", "ExecutionRecoveryStoreError"]
