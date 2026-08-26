"""Immutable persistence for the v0.6.2 capability evidence graph."""

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
from .intelligence.capability_graph import (
    CapabilityEvidenceV1,
    CapabilityNodeV1,
    CapabilityPosteriorV2,
    CalibratedRouteDecisionV1,
)
from .intelligence.common import canonical_json, stable_digest
from .security_boundary import normalize_project_id


class CapabilityGraphStoreError(RuntimeError):
    """Raised for immutable graph persistence or integrity failures."""


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _payload(value: Mapping[str, Any], name: str) -> tuple[str, str]:
    try:
        raw = canonical_json(dict(value))
    except (TypeError, ValueError) as exc:
        raise CapabilityGraphStoreError(f"{name} must contain finite JSON values") from exc
    return raw, _sha256(raw)


class CapabilityGraphStore:
    """Use the EventStore database; never create a second runtime database."""

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

    @staticmethod
    def _row_evidence(row: sqlite3.Row) -> CapabilityEvidenceV1:
        raw = str(row["evidence_json"])
        if _sha256(raw) != row["evidence_hash"]:
            raise CapabilityGraphStoreError("persisted capability evidence hash mismatch")
        item = CapabilityEvidenceV1.from_dict(json.loads(raw))
        if item.evidence_id != row["evidence_id"] or item.project_id != row["project_id"]:
            raise CapabilityGraphStoreError("persisted capability evidence identity mismatch")
        return item

    @staticmethod
    def _row_posterior(row: sqlite3.Row) -> CapabilityPosteriorV2:
        raw = str(row["posterior_json"])
        if _sha256(raw) != row["posterior_hash"]:
            raise CapabilityGraphStoreError("persisted capability posterior hash mismatch")
        return CapabilityPosteriorV2.from_dict(json.loads(raw))

    @staticmethod
    def _row_decision(row: sqlite3.Row) -> CalibratedRouteDecisionV1:
        raw = str(row["decision_json"])
        if _sha256(raw) != row["decision_hash"]:
            raise CapabilityGraphStoreError("persisted capability decision hash mismatch")
        item = CalibratedRouteDecisionV1.from_dict(json.loads(raw))
        if item.decision_id != row["decision_id"] or item.project_id != row["project_id"]:
            raise CapabilityGraphStoreError("persisted capability decision identity mismatch")
        return item

    def record_node(self, node: CapabilityNodeV1, *, project_id: str) -> bool:
        project = self._project(project_id)
        raw = canonical_json(node.to_dict())
        digest = node.node_hash
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT node_hash,node_json FROM capability_nodes_v062 WHERE project_id=? AND node_id=?", (project, node.node_id)).fetchone()
                if existing is not None:
                    if existing["node_hash"] != digest or existing["node_json"] != raw:
                        raise CapabilityGraphStoreError("node id was reused with different content")
                    connection.execute("COMMIT")
                    return False
                connection.execute("INSERT INTO capability_nodes_v062(project_id,node_id,node_type,node_json,node_hash,created_at) VALUES(?,?,?,?,?,?)", (project, node.node_id, node.node_type, raw, digest, utc_now()))
                connection.execute("COMMIT")
                return True
            except CapabilityGraphStoreError:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise CapabilityGraphStoreError(str(exc)) from exc

    def record_evidence(self, evidence: CapabilityEvidenceV1, *, idempotency_key: str | None = None, request_hash: str | None = None) -> bool:
        project = self._project(evidence.project_id)
        if project != evidence.project_id:
            raise CapabilityGraphStoreError("evidence project_id must be normalized")
        key = None if idempotency_key is None else self._key(idempotency_key, "idempotency_key")
        if (key is None) != (request_hash is None):
            raise ValueError("idempotency_key and request_hash must be supplied together")
        if request_hash is not None and (not isinstance(request_hash, str) or len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash)):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        raw, digest = _payload(evidence.to_dict(), "evidence")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if key is not None:
                    idem = connection.execute("SELECT request_hash,evidence_id FROM capability_evidence_idempotency_v062 WHERE project_id=? AND idempotency_key=?", (project, key)).fetchone()
                    if idem is not None:
                        if idem["request_hash"] != request_hash or idem["evidence_id"] != evidence.evidence_id:
                            raise CapabilityGraphStoreError("evidence idempotency key was reused with different content")
                        connection.execute("COMMIT")
                        return False
                existing = connection.execute("SELECT evidence_hash,evidence_json FROM capability_evidence_v062 WHERE project_id=? AND evidence_id=?", (project, evidence.evidence_id)).fetchone()
                if existing is not None:
                    if existing["evidence_hash"] != digest or existing["evidence_json"] != raw:
                        raise CapabilityGraphStoreError("evidence id was reused with different content")
                    inserted = False
                else:
                    connection.execute("INSERT INTO capability_evidence_v062(project_id,evidence_id,condition_key,evidence_json,evidence_hash,recorded_at) VALUES(?,?,?,?,?,?)", (project, evidence.evidence_id, evidence.condition_key, raw, digest, utc_now()))
                    inserted = True
                if key is not None:
                    connection.execute("INSERT INTO capability_evidence_idempotency_v062(project_id,idempotency_key,request_hash,evidence_id,created_at) VALUES(?,?,?,?,?)", (project, key, request_hash, evidence.evidence_id, utc_now()))
                connection.execute("COMMIT")
                return inserted
            except CapabilityGraphStoreError:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise CapabilityGraphStoreError(str(exc)) from exc

    def evidence(self, *, project_id: str, limit: int = 500) -> list[CapabilityEvidenceV1]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM capability_evidence_v062 WHERE project_id=? ORDER BY recorded_at DESC,evidence_id DESC LIMIT ?", (project, limit)).fetchall()
        return [self._row_evidence(row) for row in rows]

    def record_posterior(self, posterior: CapabilityPosteriorV2) -> bool:
        raw, digest = _payload(posterior.to_dict(), "posterior")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute("SELECT posterior_hash,posterior_json FROM capability_calibrations_v062 WHERE project_id=? AND posterior_id=?", (posterior.project_id, posterior.posterior_id)).fetchone()
                if existing is not None:
                    if existing["posterior_hash"] != digest or existing["posterior_json"] != raw:
                        raise CapabilityGraphStoreError("posterior id was reused with different content")
                    connection.execute("COMMIT")
                    return False
                connection.execute("INSERT INTO capability_calibrations_v062(project_id,posterior_id,condition_key,posterior_json,posterior_hash,created_at) VALUES(?,?,?,?,?,?)", (posterior.project_id, posterior.posterior_id, posterior.condition_key, raw, digest, posterior.as_of))
                connection.execute("COMMIT")
                return True
            except CapabilityGraphStoreError:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise CapabilityGraphStoreError(str(exc)) from exc

    def posterior(self, *, project_id: str, posterior_id: str) -> CapabilityPosteriorV2 | None:
        project = self._project(project_id)
        key = self._key(posterior_id, "posterior_id")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM capability_calibrations_v062 WHERE project_id=? AND posterior_id=?", (project, key)).fetchone()
        return self._row_posterior(row) if row is not None else None

    def posteriors(self, *, project_id: str, limit: int = 100) -> list[CapabilityPosteriorV2]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM capability_calibrations_v062 WHERE project_id=? ORDER BY created_at DESC,posterior_id DESC LIMIT ?", (project, limit)).fetchall()
        return [self._row_posterior(row) for row in rows]

    def record_decision(self, decision: CalibratedRouteDecisionV1, *, idempotency_key: str, request_hash: str) -> bool:
        project = self._project(decision.project_id)
        if project != decision.project_id:
            raise CapabilityGraphStoreError("decision project_id must be normalized")
        key = self._key(idempotency_key, "idempotency_key")
        if not isinstance(request_hash, str) or len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        raw, digest = _payload(decision.to_dict(), "decision")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                idem = connection.execute("SELECT request_hash,decision_id FROM capability_route_idempotency_v062 WHERE project_id=? AND idempotency_key=?", (project, key)).fetchone()
                if idem is not None:
                    if idem["request_hash"] != request_hash or idem["decision_id"] != decision.decision_id:
                        raise CapabilityGraphStoreError("route idempotency key was reused with different content")
                    connection.execute("COMMIT")
                    return False
                existing = connection.execute("SELECT decision_hash,decision_json FROM capability_route_decisions_v062 WHERE project_id=? AND decision_id=?", (project, decision.decision_id)).fetchone()
                if existing is not None:
                    if existing["decision_hash"] != digest or existing["decision_json"] != raw:
                        raise CapabilityGraphStoreError("decision id was reused with different content")
                    inserted = False
                else:
                    connection.execute("INSERT INTO capability_route_decisions_v062(project_id,decision_id,decision_json,decision_hash,created_at) VALUES(?,?,?,?,?)", (project, decision.decision_id, raw, digest, decision.created_at))
                    inserted = True
                connection.execute("INSERT INTO capability_route_idempotency_v062(project_id,idempotency_key,request_hash,decision_id,created_at) VALUES(?,?,?,?,?)", (project, key, request_hash, decision.decision_id, decision.created_at))
                connection.execute("COMMIT")
                return inserted
            except CapabilityGraphStoreError:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction: connection.execute("ROLLBACK")
                raise CapabilityGraphStoreError(str(exc)) from exc

    def decision_for_idempotency(self, *, project_id: str, idempotency_key: str, request_hash: str) -> CalibratedRouteDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(idempotency_key, "idempotency_key")
        with self._connection() as connection:
            row = connection.execute("SELECT request_hash,decision_id FROM capability_route_idempotency_v062 WHERE project_id=? AND idempotency_key=?", (project, key)).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise CapabilityGraphStoreError("route idempotency key was reused with different request content")
            decision = connection.execute("SELECT * FROM capability_route_decisions_v062 WHERE project_id=? AND decision_id=?", (project, row["decision_id"])).fetchone()
        if decision is None:
            raise CapabilityGraphStoreError("route idempotency record points to missing decision")
        return self._row_decision(decision)

    def decision(self, *, project_id: str, decision_id: str) -> CalibratedRouteDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(decision_id, "decision_id")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM capability_route_decisions_v062 WHERE project_id=? AND decision_id=?", (project, key)).fetchone()
        return self._row_decision(row) if row is not None else None

    def decisions(self, *, project_id: str, limit: int = 25) -> list[CalibratedRouteDecisionV1]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM capability_route_decisions_v062 WHERE project_id=? ORDER BY created_at DESC,decision_id DESC LIMIT ?", (project, limit)).fetchall()
        return [self._row_decision(row) for row in rows]

    def counts(self, *, project_id: str | None = None) -> dict[str, int]:
        project = self._project(project_id) if project_id is not None else None
        with self._connection() as connection:
            tables = {
                "capability_nodes_v062": "capability_nodes_v062",
                "capability_evidence_v062": "capability_evidence_v062",
                "capability_evidence_idempotency_v062": "capability_evidence_idempotency_v062",
                "capability_calibrations_v062": "capability_calibrations_v062",
                "capability_route_decisions_v062": "capability_route_decisions_v062",
                "capability_route_idempotency_v062": "capability_route_idempotency_v062",
            }
            suffix = " WHERE project_id=?" if project is not None else ""
            params = (project,) if project is not None else ()
            return {key: int(connection.execute(f"SELECT COUNT(*) FROM {table}{suffix}", params).fetchone()[0]) for key, table in tables.items()}


__all__ = ["CapabilityGraphStore", "CapabilityGraphStoreError"]
