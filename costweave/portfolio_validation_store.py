"""Immutable persistence for exact portfolio validation and sensitivity."""

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
from .portfolio_audit import PortfolioAuditError
from .portfolio_validation import (
    ExactOracleResult,
    PortfolioDifferentialReport,
    PortfolioSensitivityReport,
)


class PortfolioValidationStore:
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

    def record_oracle(self, result: ExactOracleResult) -> bool:
        value = result.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT result_json FROM portfolio_oracle_results "
                    "WHERE oracle_id=?",
                    (result.oracle_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["result_json"], value):
                        raise PortfolioAuditError("portfolio oracle id was reused")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO portfolio_oracle_results("
                    "oracle_id,problem_id,comparison_group_hash,topology_kind,"
                    "status,feasible,optimality_proven,"
                    "directional_evidence_eligible,advisory_only,"
                    "routing_authority,may_change_actual_route,"
                    "commercial_claims_allowed,result_json,result_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.oracle_id,
                        result.problem_id,
                        result.comparison_group_hash,
                        result.topology_kind,
                        result.status,
                        None if result.feasible is None else int(result.feasible),
                        int(result.optimality_proven),
                        int(result.directional_evidence_eligible),
                        int(result.advisory_only),
                        int(result.routing_authority),
                        int(result.may_change_actual_route),
                        int(result.commercial_claims_allowed),
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

    def record_differential(
        self, report: PortfolioDifferentialReport
    ) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = connection.execute(
                    "SELECT problem_id FROM portfolio_audit_reports "
                    "WHERE audit_id=?",
                    (report.candidate_audit_id,),
                ).fetchone()
                oracle = connection.execute(
                    "SELECT problem_id FROM portfolio_oracle_results "
                    "WHERE oracle_id=?",
                    (report.oracle_id,),
                ).fetchone()
                if (
                    candidate is None
                    or oracle is None
                    or candidate["problem_id"] != report.problem_id
                    or oracle["problem_id"] != report.problem_id
                ):
                    raise PortfolioAuditError(
                        "differential requires persisted matching candidate and oracle"
                    )
                existing = connection.execute(
                    "SELECT report_json FROM portfolio_differential_reports "
                    "WHERE report_id=? OR (candidate_audit_id=? AND oracle_id=?)",
                    (
                        report.report_id,
                        report.candidate_audit_id,
                        report.oracle_id,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or not self._same(
                        existing[0]["report_json"], value
                    ):
                        raise PortfolioAuditError(
                            "portfolio differential identity was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO portfolio_differential_reports("
                    "report_id,problem_id,candidate_audit_id,oracle_id,"
                    "validation_state,exact_validation_passed,"
                    "directional_evidence_eligible,advisory_only,"
                    "routing_authority,may_change_actual_route,"
                    "commercial_claims_allowed,report_json,report_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.report_id,
                        report.problem_id,
                        report.candidate_audit_id,
                        report.oracle_id,
                        report.validation_state,
                        int(report.exact_validation_passed),
                        int(report.directional_evidence_eligible),
                        int(report.advisory_only),
                        int(report.routing_authority),
                        int(report.may_change_actual_route),
                        int(report.commercial_claims_allowed),
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

    def record_sensitivity(
        self, report: PortfolioSensitivityReport
    ) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                oracle = connection.execute(
                    "SELECT problem_id,status FROM portfolio_oracle_results "
                    "WHERE oracle_id=?",
                    (report.baseline_oracle_id,),
                ).fetchone()
                if (
                    oracle is None
                    or oracle["problem_id"] != report.problem_id
                    or oracle["status"] != "optimal"
                ):
                    raise PortfolioAuditError(
                        "sensitivity requires a persisted exact optimal baseline"
                    )
                existing = connection.execute(
                    "SELECT report_json FROM portfolio_sensitivity_reports "
                    "WHERE sensitivity_id=?",
                    (report.sensitivity_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same(existing["report_json"], value):
                        raise PortfolioAuditError(
                            "portfolio sensitivity id was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO portfolio_sensitivity_reports("
                    "sensitivity_id,problem_id,baseline_oracle_id,"
                    "robustness_grade,tested_point_count,"
                    "directional_evidence_eligible,advisory_only,"
                    "routing_authority,may_change_actual_route,"
                    "commercial_claims_allowed,report_json,report_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        report.sensitivity_id,
                        report.problem_id,
                        report.baseline_oracle_id,
                        report.robustness_grade,
                        report.tested_point_count,
                        int(report.directional_evidence_eligible),
                        int(report.advisory_only),
                        int(report.routing_authority),
                        int(report.may_change_actual_route),
                        int(report.commercial_claims_allowed),
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
                "portfolio_oracle_results",
                "oracle_id",
                "result_json",
                "result_hash",
            ),
            (
                "portfolio_differential_reports",
                "report_id",
                "report_json",
                "report_hash",
            ),
            (
                "portfolio_sensitivity_reports",
                "sensitivity_id",
                "report_json",
                "report_hash",
            ),
        }
        if (table, identifier_column, json_column, hash_column) not in allowed:
            raise PortfolioAuditError("unsupported portfolio validation record")
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
            raise PortfolioAuditError(
                "stored portfolio validation record failed integrity"
            )
        return json.loads(raw)

    def oracle(self, oracle_id: str) -> dict[str, Any] | None:
        return self._read(
            table="portfolio_oracle_results",
            identifier_column="oracle_id",
            identifier=oracle_id,
            json_column="result_json",
            hash_column="result_hash",
        )

    def differential(self, report_id: str) -> dict[str, Any] | None:
        return self._read(
            table="portfolio_differential_reports",
            identifier_column="report_id",
            identifier=report_id,
            json_column="report_json",
            hash_column="report_hash",
        )

    def sensitivity(self, sensitivity_id: str) -> dict[str, Any] | None:
        return self._read(
            table="portfolio_sensitivity_reports",
            identifier_column="sensitivity_id",
            identifier=sensitivity_id,
            json_column="report_json",
            hash_column="report_hash",
        )

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                "portfolio_oracle_results": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_oracle_results"
                    ).fetchone()[0]
                ),
                "portfolio_oracle_proofs": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_oracle_results "
                        "WHERE optimality_proven=1"
                    ).fetchone()[0]
                ),
                "portfolio_oracle_skips": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_oracle_results "
                        "WHERE status='skipped_bounds'"
                    ).fetchone()[0]
                ),
                "portfolio_differential_reports": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_differential_reports"
                    ).fetchone()[0]
                ),
                "portfolio_suboptimal_candidates": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_differential_reports "
                        "WHERE validation_state='suboptimal_candidate'"
                    ).fetchone()[0]
                ),
                "portfolio_sensitivity_reports": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_sensitivity_reports"
                    ).fetchone()[0]
                ),
                "portfolio_validation_route_interventions": int(
                    connection.execute(
                        "SELECT (SELECT COUNT(*) FROM portfolio_oracle_results "
                        "WHERE routing_authority=1 OR may_change_actual_route=1) + "
                        "(SELECT COUNT(*) FROM portfolio_differential_reports "
                        "WHERE routing_authority=1 OR may_change_actual_route=1) + "
                        "(SELECT COUNT(*) FROM portfolio_sensitivity_reports "
                        "WHERE routing_authority=1 OR may_change_actual_route=1)"
                    ).fetchone()[0]
                ),
            }


__all__ = ["PortfolioValidationStore"]
