"""Immutable, project-scoped persistence for v0.6.3 joint search."""

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
from .intelligence.joint_search import JointSearchCandidateV1, JointSearchDecisionV1
from .security_boundary import normalize_project_id


class JointSearchStoreError(RuntimeError):
    """Raised when an immutable joint-search record cannot be trusted."""


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload(value: Any) -> tuple[str, str]:
    raw = canonical_json(value)
    return raw, _sha256(raw)


class JointSearchStore:
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
    def _candidate_row(cls, row: sqlite3.Row) -> JointSearchCandidateV1:
        raw = str(row["candidate_json"])
        if _sha256(raw) != row["candidate_hash"]:
            raise JointSearchStoreError("persisted joint candidate hash mismatch")
        item = JointSearchCandidateV1.from_dict(json.loads(raw))
        if item.computed_id != row["candidate_id"] or row["project_id"] != str(row["project_id"]):
            raise JointSearchStoreError("persisted joint candidate identity mismatch")
        return item

    @classmethod
    def _decision_row(cls, row: sqlite3.Row) -> JointSearchDecisionV1:
        raw = str(row["decision_json"])
        if _sha256(raw) != row["decision_hash"]:
            raise JointSearchStoreError("persisted joint decision hash mismatch")
        item = JointSearchDecisionV1.from_dict(json.loads(raw))
        if item.decision_id != row["decision_id"] or item.project_id != row["project_id"]:
            raise JointSearchStoreError("persisted joint decision identity mismatch")
        return item

    def record_candidate(self, candidate: JointSearchCandidateV1, *, project_id: str) -> bool:
        project = self._project(project_id)
        if candidate.strategy.case_id != candidate.case_id:
            raise JointSearchStoreError("candidate case identity mismatch")
        raw, digest = _payload(candidate.to_dict())
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT candidate_hash,candidate_json FROM joint_search_candidates_v063 WHERE project_id=? AND candidate_id=?", (project, candidate.computed_id)).fetchone()
                if existing is not None:
                    if existing["candidate_hash"] != digest or existing["candidate_json"] != raw:
                        raise JointSearchStoreError("candidate id was reused with different content")
                    connection.execute("COMMIT")
                    return False
                connection.execute("INSERT INTO joint_search_candidates_v063(project_id,candidate_id,case_id,source,candidate_json,candidate_hash,created_at) VALUES(?,?,?,?,?,?,?)", (project, candidate.computed_id, candidate.case_id, candidate.source, raw, digest, utc_now()))
                connection.execute("COMMIT")
                return True
            except JointSearchStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise JointSearchStoreError(str(exc)) from exc

    def candidate(self, *, project_id: str, candidate_id: str) -> JointSearchCandidateV1 | None:
        project = self._project(project_id)
        key = self._key(candidate_id, "candidate_id")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM joint_search_candidates_v063 WHERE project_id=? AND candidate_id=?", (project, key)).fetchone()
        return self._candidate_row(row) if row is not None else None

    def candidates(self, *, project_id: str, limit: int = 200) -> list[JointSearchCandidateV1]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM joint_search_candidates_v063 WHERE project_id=? ORDER BY created_at DESC,candidate_id DESC LIMIT ?", (project, limit)).fetchall()
        return [self._candidate_row(row) for row in rows]

    def record_decision(self, decision: JointSearchDecisionV1, *, idempotency_key: str, request_hash: str) -> bool:
        project = self._project(decision.project_id)
        if project != decision.project_id:
            raise JointSearchStoreError("decision project_id must be normalized")
        key = self._key(idempotency_key, "idempotency_key")
        if not isinstance(request_hash, str) or len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        raw, digest = _payload(decision.to_dict())
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                idem = connection.execute("SELECT request_hash,decision_id FROM joint_search_idempotency_v063 WHERE project_id=? AND idempotency_key=?", (project, key)).fetchone()
                if idem is not None:
                    if idem["request_hash"] != request_hash or idem["decision_id"] != decision.decision_id:
                        raise JointSearchStoreError("joint search idempotency key was reused with different content")
                    connection.execute("COMMIT")
                    return False
                existing = connection.execute("SELECT decision_hash,decision_json FROM joint_search_decisions_v063 WHERE project_id=? AND decision_id=?", (project, decision.decision_id)).fetchone()
                if existing is not None:
                    if existing["decision_hash"] != digest or existing["decision_json"] != raw:
                        raise JointSearchStoreError("decision id was reused with different content")
                    inserted = False
                else:
                    connection.execute("INSERT INTO joint_search_decisions_v063(project_id,decision_id,case_id,action,decision_json,decision_hash,created_at) VALUES(?,?,?,?,?,?,?)", (project, decision.decision_id, decision.case_id, decision.action, raw, digest, utc_now()))
                    inserted = True
                connection.execute("INSERT INTO joint_search_idempotency_v063(project_id,idempotency_key,request_hash,decision_id,created_at) VALUES(?,?,?,?,?)", (project, key, request_hash, decision.decision_id, utc_now()))
                connection.execute("COMMIT")
                return inserted
            except JointSearchStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise JointSearchStoreError(str(exc)) from exc

    def decision_for_idempotency(self, *, project_id: str, idempotency_key: str, request_hash: str) -> JointSearchDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(idempotency_key, "idempotency_key")
        with self._connection() as connection:
            row = connection.execute("SELECT request_hash,decision_id FROM joint_search_idempotency_v063 WHERE project_id=? AND idempotency_key=?", (project, key)).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise JointSearchStoreError("joint search idempotency key was reused with different request content")
            decision = connection.execute("SELECT * FROM joint_search_decisions_v063 WHERE project_id=? AND decision_id=?", (project, row["decision_id"])).fetchone()
        if decision is None:
            raise JointSearchStoreError("joint search idempotency record points to missing decision")
        return self._decision_row(decision)

    def decision(self, *, project_id: str, decision_id: str) -> JointSearchDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(decision_id, "decision_id")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM joint_search_decisions_v063 WHERE project_id=? AND decision_id=?", (project, key)).fetchone()
        return self._decision_row(row) if row is not None else None

    def decisions(self, *, project_id: str, limit: int = 25) -> list[JointSearchDecisionV1]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM joint_search_decisions_v063 WHERE project_id=? ORDER BY created_at DESC,decision_id DESC LIMIT ?", (project, limit)).fetchall()
        return [self._decision_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            tables = {
                "joint_search_candidates_v063": "joint_search_candidates_v063",
                "joint_search_decisions_v063": "joint_search_decisions_v063",
                "joint_search_idempotency_v063": "joint_search_idempotency_v063",
            }
            return {key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for key, table in tables.items()}


__all__ = ["JointSearchStore", "JointSearchStoreError"]
