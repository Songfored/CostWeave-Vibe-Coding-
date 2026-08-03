"""Immutable storage for advisory whole-DAG portfolio audits."""

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
from .portfolio_audit import (
    PortfolioAuditError,
    PortfolioAuditSolution,
    PortfolioScenarioComparison,
)


class PortfolioAuditStore:
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

    def record_audit(self, audit: PortfolioAuditSolution) -> bool:
        value = audit.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT report_json FROM portfolio_audit_reports "
                    "WHERE audit_id=? OR problem_id=?",
                    (audit.audit_id, audit.problem_id),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or not self._same(
                        existing[0]["report_json"], value
                    ):
                        raise PortfolioAuditError(
                            "portfolio audit id or problem id was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO portfolio_audit_reports("
                    "audit_id,problem_id,comparison_group_hash,topology_kind,"
                    "feasible,directional_evidence_eligible,advisory_only,"
                    "routing_authority,may_change_actual_route,"
                    "commercial_claims_allowed,report_json,report_hash,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        audit.audit_id,
                        audit.problem_id,
                        audit.comparison_group_hash,
                        audit.topology_kind,
                        int(audit.feasible),
                        int(audit.directional_evidence_eligible),
                        int(audit.advisory_only),
                        int(audit.routing_authority),
                        int(audit.may_change_actual_route),
                        int(audit.commercial_claims_allowed),
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

    def record_comparison(
        self, comparison: PortfolioScenarioComparison
    ) -> bool:
        value = comparison.to_dict()
        raw = canonical_json(value)
        members = tuple(
            str(item["audit_id"]) for item in comparison.alternatives
        )
        if len(set(members)) != len(members):
            raise PortfolioAuditError("portfolio comparison repeats an audit")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ",".join("?" for _ in members)
                rows = connection.execute(
                    "SELECT audit_id,comparison_group_hash "
                    "FROM portfolio_audit_reports WHERE audit_id IN ("
                    + placeholders
                    + ")",
                    members,
                ).fetchall()
                if len(rows) != len(members) or any(
                    row["comparison_group_hash"]
                    != comparison.comparison_group_hash
                    for row in rows
                ):
                    raise PortfolioAuditError(
                        "comparison requires persisted audits from one group"
                    )
                existing = connection.execute(
                    "SELECT comparison_json FROM portfolio_scenario_comparisons "
                    "WHERE comparison_id=? OR comparison_group_hash=?",
                    (
                        comparison.comparison_id,
                        comparison.comparison_group_hash,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) != 1 or not self._same(
                        existing[0]["comparison_json"], value
                    ):
                        raise PortfolioAuditError(
                            "comparison id or comparison group was reused"
                        )
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO portfolio_scenario_comparisons("
                    "comparison_id,comparison_group_hash,preferred_audit_id,"
                    "all_feasible,directional_evidence_eligible,advisory_only,"
                    "routing_authority,may_change_actual_route,"
                    "commercial_claims_allowed,comparison_json,comparison_hash,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        comparison.comparison_id,
                        comparison.comparison_group_hash,
                        comparison.preferred_audit_id,
                        int(comparison.all_feasible),
                        int(comparison.directional_evidence_eligible),
                        int(comparison.advisory_only),
                        int(comparison.routing_authority),
                        int(comparison.may_change_actual_route),
                        int(comparison.commercial_claims_allowed),
                        raw,
                        self._hash(raw),
                        utc_now(),
                    ),
                )
                for rank, audit_id in enumerate(members):
                    connection.execute(
                        "INSERT INTO portfolio_comparison_members("
                        "comparison_id,audit_id,member_rank) VALUES(?,?,?)",
                        (comparison.comparison_id, audit_id, rank),
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
                "portfolio_audit_reports",
                "audit_id",
                "report_json",
                "report_hash",
            ),
            (
                "portfolio_scenario_comparisons",
                "comparison_id",
                "comparison_json",
                "comparison_hash",
            ),
        }
        if (table, identifier_column, json_column, hash_column) not in allowed:
            raise PortfolioAuditError("unsupported portfolio record type")
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
                "stored portfolio audit record failed integrity"
            )
        return json.loads(raw)

    def audit(self, audit_id: str) -> dict[str, Any] | None:
        return self._read(
            table="portfolio_audit_reports",
            identifier_column="audit_id",
            identifier=audit_id,
            json_column="report_json",
            hash_column="report_hash",
        )

    def comparison(self, comparison_id: str) -> dict[str, Any] | None:
        return self._read(
            table="portfolio_scenario_comparisons",
            identifier_column="comparison_id",
            identifier=comparison_id,
            json_column="comparison_json",
            hash_column="comparison_hash",
        )

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                "portfolio_audits": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_audit_reports"
                    ).fetchone()[0]
                ),
                "feasible_portfolio_audits": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_audit_reports "
                        "WHERE feasible=1"
                    ).fetchone()[0]
                ),
                "directional_portfolio_audits": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_audit_reports "
                        "WHERE directional_evidence_eligible=1"
                    ).fetchone()[0]
                ),
                "portfolio_scenario_comparisons": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_scenario_comparisons"
                    ).fetchone()[0]
                ),
                "portfolio_route_interventions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM portfolio_audit_reports "
                        "WHERE routing_authority=1 "
                        "OR may_change_actual_route=1"
                    ).fetchone()[0]
                ),
            }


__all__ = ["PortfolioAuditStore"]
