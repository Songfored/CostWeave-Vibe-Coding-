"""Immutable project-scoped persistence for CostWeave v0.6.5 safe learning."""

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
from .intelligence.common import canonical_json, validate_sha256
from .intelligence.safe_learning import (
    CounterfactualReplayV1,
    CreditAssignmentV1,
    LearningEstimateV1,
    PromotionAssessmentV1,
    SafeLearningCandidateV1,
)
from .security_boundary import normalize_project_id


class SafeLearningStoreError(RuntimeError):
    """Raised when an immutable safe-learning record cannot be trusted."""


_KINDS = {
    "credit": (CreditAssignmentV1, "safe_learning_credits_v065", "credit_id", "credit_json", "credit_hash"),
    "replay": (CounterfactualReplayV1, "safe_learning_replays_v065", "replay_id", "replay_json", "replay_hash"),
    "estimate": (LearningEstimateV1, "safe_learning_estimates_v065", "estimate_id", "estimate_json", "estimate_hash"),
    "candidate": (SafeLearningCandidateV1, "safe_learning_candidates_v065", "candidate_id", "candidate_json", "candidate_hash"),
    "assessment": (PromotionAssessmentV1, "safe_learning_assessments_v065", "assessment_id", "assessment_json", "assessment_hash"),
}


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SafeLearningStore:
    """Reuse the EventStore database and keep learning evidence append-only."""

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
    def _hash(value: str) -> str:
        try:
            return validate_sha256(value, "request_hash")
        except (TypeError, ValueError) as exc:
            raise SafeLearningStoreError(str(exc)) from exc

    @staticmethod
    def _key(value: str, name: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 200:
            raise SafeLearningStoreError(f"{name} must contain 8-200 characters")
        return value.strip()

    @staticmethod
    def _payload(item: Any) -> tuple[str, str]:
        try:
            raw = canonical_json(item.to_dict())
        except (TypeError, ValueError) as exc:
            raise SafeLearningStoreError("record must contain finite JSON values") from exc
        return raw, _sha256(raw)

    @classmethod
    def _row_item(cls, row: sqlite3.Row, kind: str) -> Any:
        item_cls, _, id_column, json_column, hash_column = _KINDS[kind]
        raw = str(row[json_column])
        if _sha256(raw) != row[hash_column]:
            raise SafeLearningStoreError(f"persisted {kind} hash mismatch")
        item = item_cls.from_dict(json.loads(raw))
        if getattr(item, id_column) != row[id_column] or item.project_id != row["project_id"]:
            raise SafeLearningStoreError(f"persisted {kind} identity mismatch")
        return item

    def _insert_record(self, connection: sqlite3.Connection, *, kind: str, item: Any) -> bool:
        """Insert or verify one immutable record using the caller's transaction."""
        item_cls, table, id_column, json_column, hash_column = _KINDS[kind]
        if not isinstance(item, item_cls):
            raise SafeLearningStoreError(f"item is not a {kind} contract")
        project = self._project(item.project_id)
        raw, digest = self._payload(item)
        item_id = getattr(item, id_column)
        existing = connection.execute(
            f"SELECT {json_column},{hash_column} FROM {table} WHERE project_id=? AND {id_column}=?",
            (project, item_id),
        ).fetchone()
        if existing is not None:
            if existing[json_column] != raw or existing[hash_column] != digest:
                raise SafeLearningStoreError(f"{kind} id was reused with different content")
            return False

        columns = ["project_id", id_column]
        values: list[Any] = [project, item_id]
        if kind in ("credit", "replay"):
            columns.append("trace_id")
            values.append(getattr(item, "trace_id"))
        elif kind in ("estimate", "candidate"):
            columns.append("task_family")
            values.append(getattr(item, "task_family"))
        elif kind == "assessment":
            columns.extend(["candidate_id", "stage"])
            values.extend([getattr(item, "candidate_id"), getattr(item, "stage")])
        columns.extend([json_column, hash_column, "created_at"])
        values.extend([raw, digest, utc_now()])
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values),
        )
        return True

    def _record(self, *, kind: str, item: Any, idempotency_key: str | None = None, request_hash: str | None = None) -> bool:
        item_cls, table, id_column, json_column, hash_column = _KINDS[kind]
        if not isinstance(item, item_cls):
            raise SafeLearningStoreError(f"item is not a {kind} contract")
        project = self._project(item.project_id)
        idem = self._key(idempotency_key, "idempotency_key") if idempotency_key is not None else None
        req_hash = self._hash(request_hash) if request_hash is not None else None
        item_id = getattr(item, id_column)
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if idem is not None:
                    existing = connection.execute(
                        "SELECT request_hash,object_kind,object_id FROM safe_learning_idempotency_v065 WHERE project_id=? AND idempotency_key=?",
                        (project, idem),
                    ).fetchone()
                    if existing is not None:
                        if existing["request_hash"] != req_hash or existing["object_kind"] != kind or existing["object_id"] != item_id:
                            raise SafeLearningStoreError("safe-learning idempotency key was reused with different content")
                        connection.execute("COMMIT")
                        return False
                inserted = self._insert_record(connection, kind=kind, item=item)
                if idem is not None:
                    connection.execute(
                        "INSERT INTO safe_learning_idempotency_v065(project_id,idempotency_key,request_hash,object_kind,object_id,created_at) VALUES(?,?,?,?,?,?)",
                        (project, idem, req_hash, kind, item_id, utc_now()),
                    )
                connection.execute("COMMIT")
                return inserted
            except SafeLearningStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise SafeLearningStoreError(str(exc)) from exc
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def record_credit(self, item: CreditAssignmentV1, *, idempotency_key: str | None = None, request_hash: str | None = None) -> bool:
        return self._record(kind="credit", item=item, idempotency_key=idempotency_key, request_hash=request_hash)

    def record_replay(self, item: CounterfactualReplayV1) -> bool:
        return self._record(kind="replay", item=item)

    def record_estimate(self, item: LearningEstimateV1) -> bool:
        return self._record(kind="estimate", item=item)

    def record_candidate(self, item: SafeLearningCandidateV1) -> bool:
        return self._record(kind="candidate", item=item)

    def record_assessment(self, item: PromotionAssessmentV1) -> bool:
        return self._record(kind="assessment", item=item)

    def record_evaluation(
        self,
        estimate: LearningEstimateV1,
        candidate: SafeLearningCandidateV1,
        assessment: PromotionAssessmentV1,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> bool:
        """Persist a complete evaluation and its idempotency key atomically."""
        project = self._project(estimate.project_id)
        if self._project(candidate.project_id) != project or self._project(assessment.project_id) != project:
            raise SafeLearningStoreError("evaluation records must belong to the same project")
        if candidate.estimate_id != estimate.estimate_id:
            raise SafeLearningStoreError("candidate estimate_id mismatch")
        if assessment.estimate_id != estimate.estimate_id or assessment.candidate_id != candidate.candidate_id:
            raise SafeLearningStoreError("assessment links do not match evaluation records")
        idem = self._key(idempotency_key, "idempotency_key")
        req_hash = self._hash(request_hash)
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT request_hash,object_kind FROM safe_learning_idempotency_v065 "
                    "WHERE project_id=? AND idempotency_key=?",
                    (project, idem),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != req_hash or existing["object_kind"] != "assessment":
                        raise SafeLearningStoreError("safe-learning idempotency key was reused with different content")
                    connection.execute("COMMIT")
                    return False
                self._insert_record(connection, kind="estimate", item=estimate)
                self._insert_record(connection, kind="candidate", item=candidate)
                self._insert_record(connection, kind="assessment", item=assessment)
                connection.execute(
                    "INSERT INTO safe_learning_idempotency_v065"
                    "(project_id,idempotency_key,request_hash,object_kind,object_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (project, idem, req_hash, "assessment", assessment.assessment_id, utc_now()),
                )
                connection.execute("COMMIT")
                return True
            except SafeLearningStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise SafeLearningStoreError(str(exc)) from exc
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def evaluation_for_idempotency(
        self, *, project_id: str, idempotency_key: str, request_hash: str,
    ) -> tuple[PromotionAssessmentV1, LearningEstimateV1, SafeLearningCandidateV1] | None:
        project = self._project(project_id)
        idem = self._key(idempotency_key, "idempotency_key")
        req_hash = self._hash(request_hash)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_hash,object_kind,object_id FROM safe_learning_idempotency_v065 WHERE project_id=? AND idempotency_key=?",
                (project, idem),
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != req_hash:
                raise SafeLearningStoreError("safe-learning idempotency key was reused with different request")
            if row["object_kind"] != "assessment":
                raise SafeLearningStoreError("idempotency key belongs to another safe-learning operation")
            target = connection.execute(
                "SELECT * FROM safe_learning_assessments_v065 WHERE project_id=? AND assessment_id=?",
                (project, row["object_id"]),
            ).fetchone()
            if target is None:
                raise SafeLearningStoreError("idempotency record points to missing assessment")
            assessment = self._row_item(target, "assessment")
            estimate_row = connection.execute(
                "SELECT * FROM safe_learning_estimates_v065 WHERE project_id=? AND estimate_id=?",
                (project, assessment.estimate_id),
            ).fetchone()
            candidate_row = connection.execute(
                "SELECT * FROM safe_learning_candidates_v065 WHERE project_id=? AND candidate_id=?",
                (project, assessment.candidate_id),
            ).fetchone()
            if estimate_row is None or candidate_row is None:
                raise SafeLearningStoreError("idempotent evaluation points to an incomplete record set")
            estimate = self._row_item(estimate_row, "estimate")
            candidate = self._row_item(candidate_row, "candidate")
            if candidate.estimate_id != estimate.estimate_id:
                raise SafeLearningStoreError("idempotent evaluation links are inconsistent")
        return assessment, estimate, candidate

    def credit_for_idempotency(self, *, project_id: str, idempotency_key: str, request_hash: str) -> CreditAssignmentV1 | None:
        project = self._project(project_id)
        idem = self._key(idempotency_key, "idempotency_key")
        req_hash = self._hash(request_hash)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_hash,object_kind,object_id FROM safe_learning_idempotency_v065 WHERE project_id=? AND idempotency_key=?",
                (project, idem),
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != req_hash or row["object_kind"] != "credit":
                raise SafeLearningStoreError("safe-learning idempotency key was reused with different request")
            target = connection.execute(
                "SELECT * FROM safe_learning_credits_v065 WHERE project_id=? AND credit_id=?",
                (project, row["object_id"]),
            ).fetchone()
        if target is None:
            raise SafeLearningStoreError("idempotency record points to missing credit")
        return self._row_item(target, "credit")

    def _get(self, *, project_id: str, kind: str, object_id: str) -> Any | None:
        project = self._project(project_id)
        item_cls, table, id_column, _, _ = _KINDS[kind]
        key = self._key(object_id, id_column)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE project_id=? AND {id_column}=?",
                (project, key),
            ).fetchone()
        return self._row_item(row, kind) if row is not None else None

    def credit(self, *, project_id: str, credit_id: str) -> CreditAssignmentV1 | None:
        return self._get(project_id=project_id, kind="credit", object_id=credit_id)

    def replay(self, *, project_id: str, replay_id: str) -> CounterfactualReplayV1 | None:
        return self._get(project_id=project_id, kind="replay", object_id=replay_id)

    def estimate(self, *, project_id: str, estimate_id: str) -> LearningEstimateV1 | None:
        return self._get(project_id=project_id, kind="estimate", object_id=estimate_id)

    def candidate(self, *, project_id: str, candidate_id: str) -> SafeLearningCandidateV1 | None:
        return self._get(project_id=project_id, kind="candidate", object_id=candidate_id)

    def assessment(self, *, project_id: str, assessment_id: str) -> PromotionAssessmentV1 | None:
        return self._get(project_id=project_id, kind="assessment", object_id=assessment_id)

    def get(self, *, project_id: str, object_id: str) -> tuple[str, Any] | None:
        for kind in _KINDS:
            item = self._get(project_id=project_id, kind=kind, object_id=object_id)
            if item is not None:
                return kind, item
        return None

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for _, table, _, _, _ in _KINDS.values()
            } | {
                "safe_learning_idempotency_v065": int(connection.execute("SELECT COUNT(*) FROM safe_learning_idempotency_v065").fetchone()[0])
            }


__all__ = ["SafeLearningStore", "SafeLearningStoreError"]
