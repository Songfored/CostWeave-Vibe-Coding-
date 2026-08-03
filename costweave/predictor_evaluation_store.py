"""Immutable persistence for offline predictor evaluation contracts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .evaluation_protocol import canonical_json
from .predictor_evaluation import (
    ChallengerComparison,
    PredictorCandidateManifest,
    PredictorEvaluationError,
    PredictorEvaluationReport,
)


class PredictorEvaluationStore:
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

    @staticmethod
    def _same(raw: str, value: Mapping[str, Any]) -> bool:
        return canonical_json(json.loads(raw)) == canonical_json(value)

    def record_artifact(self, artifact: PredictorCandidateManifest) -> bool:
        value = artifact.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                admission = connection.execute(
                    "SELECT dataset_content_hash,training_allowed "
                    "FROM predictor_admission_reports WHERE report_id=?",
                    (artifact.admission_report_id,),
                ).fetchone()
                if admission is None or not bool(admission["training_allowed"]):
                    raise PredictorEvaluationError(
                        "predictor artifact requires a persisted admitted dataset"
                    )
                if admission["dataset_content_hash"] != artifact.dataset_content_hash:
                    raise PredictorEvaluationError(
                        "predictor artifact dataset version does not match admission"
                    )
                existing = connection.execute(
                    "SELECT artifact_json FROM predictor_candidate_artifacts "
                    "WHERE artifact_id=?",
                    (artifact.artifact_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["artifact_json"], value):
                        raise PredictorEvaluationError(
                            "predictor artifact id was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO predictor_candidate_artifacts("
                    "artifact_id,admission_report_id,dataset_content_hash,method,"
                    "status,routing_authority,policy_promotion_allowed,"
                    "artifact_json,artifact_hash,model_payload_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        artifact.artifact_id,
                        artifact.admission_report_id,
                        artifact.dataset_content_hash,
                        artifact.method,
                        artifact.status,
                        int(artifact.routing_authority),
                        int(artifact.policy_promotion_allowed),
                        raw,
                        self._hash(raw),
                        artifact.model_payload_hash,
                        artifact.created_at,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def record_report(self, report: PredictorEvaluationReport) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                artifact = connection.execute(
                    "SELECT admission_report_id FROM predictor_candidate_artifacts "
                    "WHERE artifact_id=?",
                    (report.predictor_artifact_id,),
                ).fetchone()
                if (
                    artifact is None
                    or artifact["admission_report_id"] != report.admission_report_id
                ):
                    raise PredictorEvaluationError(
                        "evaluation report does not match a persisted artifact"
                    )
                existing = connection.execute(
                    "SELECT report_json FROM predictor_evaluation_reports "
                    "WHERE report_id=?",
                    (report.report_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["report_json"], value):
                        raise PredictorEvaluationError(
                            "evaluation report id was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO predictor_evaluation_reports("
                    "report_id,predictor_artifact_id,admission_report_id,cohort_hash,"
                    "state,offline_evaluation_passed,eligible_for_shadow,"
                    "routing_authority,policy_promotion_allowed,report_json,"
                    "report_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.report_id,
                        report.predictor_artifact_id,
                        report.admission_report_id,
                        report.cohort_hash,
                        report.state,
                        int(report.offline_evaluation_passed),
                        int(report.eligible_for_shadow),
                        int(report.routing_authority),
                        int(report.policy_promotion_allowed),
                        raw,
                        self._hash(raw),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def record_comparison(self, comparison: ChallengerComparison) -> bool:
        value = comparison.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_evaluation_reports "
                        "WHERE report_id IN (?,?)",
                        (
                            comparison.champion_report_id,
                            comparison.challenger_report_id,
                        ),
                    ).fetchone()[0]
                )
                if count != 2:
                    raise PredictorEvaluationError(
                        "comparison requires two persisted evaluation reports"
                    )
                existing = connection.execute(
                    "SELECT comparison_json FROM predictor_challenger_comparisons "
                    "WHERE comparison_id=?",
                    (comparison.comparison_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["comparison_json"], value):
                        raise PredictorEvaluationError("comparison id was reused")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO predictor_challenger_comparisons("
                    "comparison_id,champion_report_id,challenger_report_id,"
                    "eligible_for_shadow_comparison,routing_authority,"
                    "policy_promotion_allowed,comparison_json,comparison_hash,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        comparison.comparison_id,
                        comparison.champion_report_id,
                        comparison.challenger_report_id,
                        int(comparison.eligible_for_shadow_comparison),
                        int(comparison.routing_authority),
                        int(comparison.policy_promotion_allowed),
                        raw,
                        self._hash(raw),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def _read(
        self,
        *,
        table: str,
        identifier_column: str,
        identifier: str,
        json_column: str,
        hash_column: str,
    ) -> dict[str, Any] | None:
        allowed = {
            (
                "predictor_candidate_artifacts",
                "artifact_id",
                "artifact_json",
                "artifact_hash",
            ),
            (
                "predictor_evaluation_reports",
                "report_id",
                "report_json",
                "report_hash",
            ),
            (
                "predictor_challenger_comparisons",
                "comparison_id",
                "comparison_json",
                "comparison_hash",
            ),
        }
        if (table, identifier_column, json_column, hash_column) not in allowed:
            raise PredictorEvaluationError("unsupported evaluation record type")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {json_column},{hash_column} FROM {table} "
                f"WHERE {identifier_column}=?",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row[json_column])
        if self._hash(raw) != row[hash_column]:
            raise PredictorEvaluationError(
                "stored predictor evaluation record failed integrity"
            )
        return json.loads(raw)

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self._read(
            table="predictor_candidate_artifacts",
            identifier_column="artifact_id",
            identifier=artifact_id,
            json_column="artifact_json",
            hash_column="artifact_hash",
        )

    def report(self, report_id: str) -> dict[str, Any] | None:
        return self._read(
            table="predictor_evaluation_reports",
            identifier_column="report_id",
            identifier=report_id,
            json_column="report_json",
            hash_column="report_hash",
        )

    def comparison(self, comparison_id: str) -> dict[str, Any] | None:
        return self._read(
            table="predictor_challenger_comparisons",
            identifier_column="comparison_id",
            identifier=comparison_id,
            json_column="comparison_json",
            hash_column="comparison_hash",
        )

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                "predictor_candidate_artifacts": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_candidate_artifacts"
                    ).fetchone()[0]
                ),
                "predictor_evaluation_reports": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_evaluation_reports"
                    ).fetchone()[0]
                ),
                "predictor_shadow_candidates": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_evaluation_reports "
                        "WHERE eligible_for_shadow=1"
                    ).fetchone()[0]
                ),
                "predictor_challenger_comparisons": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_challenger_comparisons"
                    ).fetchone()[0]
                ),
                "predictor_production_promotions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM predictor_candidate_artifacts "
                        "WHERE routing_authority=1 OR policy_promotion_allowed=1"
                    ).fetchone()[0]
                ),
            }


__all__ = ["PredictorEvaluationStore"]
