"""Persistent Artifact, lineage and handoff message store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .artifacts import (
    Artifact,
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactStatus,
    StaleArtifactError,
)
from .contracts_v4 import canonical_identifier, utc_now
from .domain import Plan
from .graph_compiler import CompiledGraph
from .messages import MessageEnvelope, MessageError, StalePlanRevisionError


class ArtifactStore:
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

    def put(self, artifact: Artifact) -> tuple[Artifact, bool]:
        artifact.verify_integrity()
        raw = json.dumps(
            dict(artifact.content),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=?",
                    (artifact.artifact_id,),
                ).fetchone()
                if existing is not None:
                    restored = self._row_to_artifact(existing)
                    if restored.to_dict() != artifact.to_dict():
                        raise ArtifactIntegrityError(
                            "immutable artifact ID was reused for different metadata"
                        )
                    connection.rollback()
                    return restored, True
                for parent_id in artifact.input_artifact_refs:
                    parent = connection.execute(
                        "SELECT run_id,status,content_json,content_hash FROM artifacts "
                        "WHERE artifact_id=?",
                        (parent_id,),
                    ).fetchone()
                    if parent is None:
                        raise ArtifactError(
                            f"missing input artifact producer: {parent_id}"
                        )
                    if parent["run_id"] != artifact.run_id:
                        raise ArtifactError("cross-run artifact reference is forbidden")
                    if parent["status"] != ArtifactStatus.VALIDATED.value:
                        raise StaleArtifactError(
                            f"input artifact is not validated: {parent_id}"
                        )
                    actual = hashlib.sha256(
                        str(parent["content_json"]).encode("utf-8")
                    ).hexdigest()
                    if actual != parent["content_hash"]:
                        raise ArtifactIntegrityError(
                            f"input artifact hash mismatch: {parent_id}"
                        )
                now = utc_now()
                connection.execute(
                    "INSERT INTO artifacts(artifact_id,run_id,producer_task_id,"
                    "artifact_type,schema_ref,schema_version,plan_revision,attempt,"
                    "status,sensitivity,producer_model,input_refs_json,content_json,"
                    "content_hash,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        artifact.artifact_id,
                        artifact.run_id,
                        artifact.producer_task_id,
                        artifact.artifact_type,
                        artifact.schema_ref,
                        artifact.schema_version,
                        artifact.plan_revision,
                        artifact.attempt,
                        artifact.status.value,
                        artifact.sensitivity,
                        artifact.producer_model,
                        json.dumps(list(artifact.input_artifact_refs)),
                        raw,
                        artifact.content_hash,
                        artifact.created_at,
                        now,
                    ),
                )
                for parent_id in artifact.input_artifact_refs:
                    connection.execute(
                        "INSERT INTO artifact_links(run_id,parent_artifact_id,"
                        "child_artifact_id,link_kind) VALUES(?,?,?,?)",
                        (
                            artifact.run_id,
                            parent_id,
                            artifact.artifact_id,
                            "derived_from",
                        ),
                    )
                connection.commit()
                return artifact, False
            except Exception:
                connection.rollback()
                raise

    def get(self, artifact_id: str) -> Artifact | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            return None
        artifact = self._row_to_artifact(row)
        artifact.verify_integrity()
        return artifact

    def list_run(
        self,
        run_id: str,
        *,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at,artifact_id",
                (run_id,),
            ).fetchall()
        return [
            self._row_to_artifact(row).to_dict(include_content=include_content)
            for row in rows
        ]

    def task_artifacts(
        self,
        run_id: str,
        legacy_task_ids: Iterable[str],
        *,
        validated_only: bool = True,
    ) -> list[Artifact]:
        canonical_ids = [
            canonical_identifier("task", f"{run_id}:{task_id}")
            for task_id in legacy_task_ids
        ]
        if not canonical_ids:
            return []
        placeholders = ",".join("?" for _ in canonical_ids)
        query = (
            f"SELECT * FROM artifacts WHERE run_id=? AND producer_task_id IN "
            f"({placeholders})"
        )
        params: list[Any] = [run_id, *canonical_ids]
        if validated_only:
            query += " AND status=?"
            params.append(ArtifactStatus.VALIDATED.value)
        query += " ORDER BY created_at,artifact_id"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        artifacts = [self._row_to_artifact(row) for row in rows]
        for artifact in artifacts:
            artifact.verify_integrity()
        return artifacts

    def mark_stale_for_tasks(
        self,
        run_id: str,
        legacy_task_ids: Iterable[str],
    ) -> list[str]:
        canonical_ids = [
            canonical_identifier("task", f"{run_id}:{task_id}")
            for task_id in legacy_task_ids
        ]
        if not canonical_ids:
            return []
        placeholders = ",".join("?" for _ in canonical_ids)
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"SELECT artifact_id FROM artifacts WHERE run_id=? "
                    f"AND producer_task_id IN ({placeholders}) AND status!=?",
                    [run_id, *canonical_ids, ArtifactStatus.STALE.value],
                ).fetchall()
                ids = [str(row["artifact_id"]) for row in rows]
                if ids:
                    id_placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"UPDATE artifacts SET status=?,updated_at=? "
                        f"WHERE artifact_id IN ({id_placeholders})",
                        [ArtifactStatus.STALE.value, utc_now(), *ids],
                    )
                connection.commit()
                return ids
            except Exception:
                connection.rollback()
                raise

    def put_message(self, message: MessageEnvelope) -> tuple[MessageEnvelope, bool]:
        self._validate_plan_revision(message.run_id, message.plan_revision)
        for artifact_id in message.input_artifact_refs:
            artifact = self.get(artifact_id)
            if artifact is None:
                raise MessageError(f"missing handoff artifact: {artifact_id}")
            if artifact.run_id != message.run_id:
                raise MessageError("cross-run handoff is forbidden")
            if artifact.status != ArtifactStatus.VALIDATED:
                raise StaleArtifactError(
                    f"handoff artifact is not validated: {artifact_id}"
                )
        raw = message.to_json()
        fingerprint = message.fingerprint()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT envelope_json,envelope_hash FROM messages "
                    "WHERE message_id=? OR "
                    "(run_id=? AND recipient_task_id=? AND plan_revision=? AND attempt=?)",
                    (
                        message.message_id,
                        message.run_id,
                        message.recipient_task_id,
                        message.plan_revision,
                        message.attempt,
                    ),
                ).fetchone()
                if existing is not None:
                    if existing["envelope_hash"] != fingerprint:
                        raise MessageError(
                            "message idempotency identity was reused for different content"
                        )
                    connection.rollback()
                    return (
                        MessageEnvelope.from_dict(
                            json.loads(existing["envelope_json"])
                        ),
                        True,
                    )
                connection.execute(
                    "INSERT INTO messages(message_id,run_id,recipient_task_id,"
                    "plan_revision,attempt,kind,envelope_json,envelope_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        message.message_id,
                        message.run_id,
                        message.recipient_task_id,
                        message.plan_revision,
                        message.attempt,
                        message.kind,
                        raw,
                        fingerprint,
                        message.issued_at,
                    ),
                )
                connection.commit()
                return message, False
            except Exception:
                connection.rollback()
                raise

    def sync_graph(self, graph: CompiledGraph) -> None:
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM task_dependencies WHERE run_id=? AND plan_revision=?",
                    (graph.run_id, graph.plan_revision),
                )
                for edge in graph.edges:
                    connection.execute(
                        "INSERT INTO task_dependencies(run_id,plan_revision,task_id,"
                        "dependency_task_id,dependency_kind,required_schema)"
                        " VALUES(?,?,?,?,?,?)",
                        (
                            graph.run_id,
                            graph.plan_revision,
                            canonical_identifier(
                                "task", f"{graph.run_id}:{edge['to_task']}"
                            ),
                            canonical_identifier(
                                "task", f"{graph.run_id}:{edge['from_task']}"
                            ),
                            edge["kind"],
                            edge.get("required_schema"),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def graph(self, run_id: str, plan: Plan) -> dict[str, Any]:
        with self._connection() as connection:
            links = connection.execute(
                "SELECT parent_artifact_id,child_artifact_id,link_kind "
                "FROM artifact_links WHERE run_id=? ORDER BY parent_artifact_id,"
                "child_artifact_id",
                (run_id,),
            ).fetchall()
        return {
            "run_id": run_id,
            "plan_revision": plan.version,
            "tasks": [
                {
                    "task_id": task.id,
                    "status": task.status.value,
                    "dependencies": [
                        {
                            "task_id": dependency,
                            "kind": task.dependency_types.get(dependency, "hard"),
                        }
                        for dependency in task.dependencies
                    ],
                    "input_artifact_refs": list(task.input_artifact_refs),
                    "artifact_ids": list(task.artifact_ids),
                }
                for task in plan.tasks
            ],
            "artifact_links": [dict(row) for row in links],
        }

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "artifacts",
                    "artifact_links",
                    "task_dependencies",
                    "messages",
                )
            }

    def _validate_plan_revision(self, run_id: str, plan_revision: int) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM projections "
                "WHERE name='run' AND entity_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise StalePlanRevisionError("run projection does not exist")
        state = json.loads(row["state_json"])
        actual = int((state.get("plan") or {}).get("version") or 0)
        if actual != int(plan_revision):
            raise StalePlanRevisionError(
                f"message plan revision {plan_revision} is stale; current is {actual}"
            )

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact.from_dict(
            {
                "schema_version": row["schema_version"],
                "artifact_id": row["artifact_id"],
                "run_id": row["run_id"],
                "producer_task_id": row["producer_task_id"],
                "artifact_type": row["artifact_type"],
                "schema_ref": row["schema_ref"],
                "content": json.loads(row["content_json"]),
                "content_hash": row["content_hash"],
                "plan_revision": row["plan_revision"],
                "status": row["status"],
                "producer_model": row["producer_model"],
                "input_artifact_refs": json.loads(row["input_refs_json"]),
                "attempt": row["attempt"],
                "created_at": row["created_at"],
                "sensitivity": row["sensitivity"],
            }
        )
