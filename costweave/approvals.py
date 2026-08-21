"""Persistent non-blocking task approvals."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .commands import validate_idempotency_key
from .contracts_v4 import canonical_identifier, utc_now


class ApprovalError(ValueError):
    pass


class ApprovalConflictError(ApprovalError):
    pass


class ApprovalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def ensure(
        self,
        *,
        run_id: str,
        task_id: str,
        kind: str,
        reason: str,
    ) -> tuple[dict[str, Any], bool]:
        canonical_task = canonical_identifier(
            "task", f"{run_id}:{task_id}"
        )
        approval_id = "approval_" + hashlib.sha256(
            f"{run_id}:{canonical_task}:{kind}".encode("utf-8")
        ).hexdigest()[:32]
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM approvals WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
                if row is not None:
                    if (
                        row["run_id"] != run_id
                        or row["task_id"] != canonical_task
                        or row["approval_kind"] != kind
                        or row["reason"] != reason
                    ):
                        raise ApprovalConflictError(
                            "approval identity was reused for a different intent"
                        )
                    connection.rollback()
                    return dict(row), True
                connection.execute(
                    "INSERT INTO approvals(approval_id,run_id,task_id,"
                    "approval_kind,status,reason,revision,requested_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        approval_id,
                        run_id,
                        canonical_task,
                        kind,
                        "pending",
                        reason,
                        1,
                        utc_now(),
                    ),
                )
                connection.commit()
                return self.get(approval_id), False
            except Exception:
                connection.rollback()
                raise

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def is_approved_for(
        self,
        approval_id: str,
        *,
        run_id: str,
        task_id: str,
        kind: str,
        reason: str,
    ) -> bool:
        """Verify a resolved approval against its complete admitted intent."""

        canonical_task = canonical_identifier("task", f"{run_id}:{task_id}")
        row = self.get(str(approval_id))
        return bool(
            row is not None
            and row.get("run_id") == run_id
            and row.get("task_id") == canonical_task
            and row.get("approval_kind") == kind
            and row.get("reason") == reason
            and row.get("status") == "approved"
        )

    def list(
        self,
        *,
        status: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals"
                + where
                + " ORDER BY requested_at",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        note: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        if decision not in {"approved", "rejected"}:
            raise ApprovalError("decision must be approved or rejected")
        key = validate_idempotency_key(idempotency_key)
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                keyed = connection.execute(
                    "SELECT * FROM approvals WHERE resolution_key=?",
                    (key,),
                ).fetchone()
                if keyed is not None:
                    if (
                        keyed["approval_id"] != approval_id
                        or keyed["status"] != decision
                    ):
                        raise ApprovalConflictError(
                            "resolution key was reused for another decision"
                        )
                    connection.rollback()
                    return dict(keyed), True
                row = connection.execute(
                    "SELECT * FROM approvals WHERE approval_id=?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(approval_id)
                if int(row["revision"]) != int(expected_revision):
                    raise ApprovalConflictError(
                        f"expected approval revision {expected_revision}, "
                        f"current is {row['revision']}"
                    )
                if row["status"] != "pending":
                    raise ApprovalConflictError("approval is already resolved")
                connection.execute(
                    "UPDATE approvals SET status=?,revision=revision+1,"
                    "resolved_at=?,resolution_note=?,resolution_key=? "
                    "WHERE approval_id=?",
                    (decision, utc_now(), note, key, approval_id),
                )
                connection.commit()
                return self.get(approval_id), False
            except Exception:
                connection.rollback()
                raise
