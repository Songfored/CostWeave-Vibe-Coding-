"""Persistence for v0.5.6.1 experiment reports and observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .baseline_runner import ExperimentReport
from .evaluation_protocol import (
    EVALUATION_PROTOCOL_VERSION,
    EvaluationProtocolError,
    canonical_json,
)
from .security_boundary import (
    normalize_data_residency,
    normalize_project_id,
)


class EvaluationProtocolStore:
    """Use evaluation_runs as the report fact and append its observation matrix."""

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

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def record(
        self,
        report: ExperimentReport,
        *,
        project_id: str | None = None,
        data_residency: str | None = None,
    ) -> bool:
        report_payload = report.to_dict()
        if (project_id is None) != (data_residency is None):
            raise EvaluationProtocolError(
                "evaluation ownership requires project and data residency"
            )
        security_scope = (
            {
                "visibility": "project",
                "project_id": normalize_project_id(str(project_id)),
                "data_residency": normalize_data_residency(
                    str(data_residency)
                ),
            }
            if project_id is not None
            else {
                "visibility": "internal_evaluation_protocol",
                "project_id": None,
                "data_residency": None,
            }
        )
        payload = {**report_payload, "security_scope": security_scope}
        raw = canonical_json(payload)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT result_json FROM evaluation_runs "
                    "WHERE evaluation_id=?",
                    (report.report_id,),
                ).fetchone()
                if existing is not None:
                    existing_payload = json.loads(existing["result_json"])
                    legacy_match = (
                        "security_scope" not in existing_payload
                        and canonical_json(existing_payload)
                        == canonical_json(report_payload)
                    )
                    if (
                        not legacy_match
                        and canonical_json(existing_payload) != raw
                    ):
                        raise EvaluationProtocolError(
                            "evaluation id was reused for different evidence"
                        )
                    observation_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM evaluation_observations "
                            "WHERE evaluation_id=?",
                            (report.report_id,),
                        ).fetchone()[0]
                    )
                    if observation_count != len(report.observations):
                        raise EvaluationProtocolError(
                            "persisted evaluation observation matrix is incomplete"
                        )
                    if legacy_match:
                        connection.execute(
                            "UPDATE evaluation_runs SET result_json=? "
                            "WHERE evaluation_id=?",
                            (raw, report.report_id),
                        )
                    connection.execute("COMMIT")
                    return False

                connection.execute(
                    "INSERT INTO evaluation_runs("
                    "evaluation_id,revision,sample_size,result_json,passed,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (
                        report.report_id,
                        EVALUATION_PROTOCOL_VERSION,
                        len(report.observations),
                        raw,
                        int(report.passed),
                        report.spec.registered_at,
                    ),
                )
                for observation in report.observations:
                    item = observation.to_dict()
                    observation_raw = canonical_json(item)
                    connection.execute(
                        "INSERT INTO evaluation_observations("
                        "observation_id,evaluation_id,experiment_id,task_id,"
                        "project_id,partition_name,baseline_id,blind_alias,"
                        "evidence_level,provider_calls,observation_json,"
                        "observation_hash,recorded_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            observation.observation_id,
                            report.report_id,
                            report.spec.experiment_id,
                            observation.task_id,
                            observation.project_id,
                            observation.partition,
                            observation.baseline_id,
                            observation.blind_alias,
                            observation.outcome.evidence_level,
                            observation.outcome.provider_calls,
                            observation_raw,
                            self._hash(observation_raw),
                            report.spec.registered_at,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM evaluation_runs "
                "WHERE evaluation_id=? AND revision=?",
                (evaluation_id, EVALUATION_PROTOCOL_VERSION),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT result_json FROM evaluation_runs "
                "WHERE revision=? ORDER BY created_at,evaluation_id",
                (EVALUATION_PROTOCOL_VERSION,),
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def observations(self, evaluation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT observation_json FROM evaluation_observations "
                "WHERE evaluation_id=? "
                "ORDER BY partition_name,task_id,baseline_id",
                (evaluation_id,),
            ).fetchall()
        return [json.loads(row["observation_json"]) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            reports = int(
                connection.execute(
                    "SELECT COUNT(*) FROM evaluation_runs WHERE revision=?",
                    (EVALUATION_PROTOCOL_VERSION,),
                ).fetchone()[0]
            )
            observations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM evaluation_observations"
                ).fetchone()[0]
            )
        return {
            "protocol_evaluation_runs": reports,
            "evaluation_observations": observations,
        }


__all__ = ["EvaluationProtocolStore"]
