"""Immutable persistence for v0.5.7.1 offline replay evidence."""

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
from .offline_replay import (
    ApproximateOracleReport,
    ObservedRouteOutcome,
    OfflineReplayError,
    OfflineReplayResult,
    ReplayBenchmarkReport,
    ReplayCase,
    ReplayVersionSnapshot,
)


class OfflineReplayStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
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

    def _read(self, table: str, id_column: str, json_column: str, hash_column: str, value: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {json_column},{hash_column} FROM {table} WHERE {id_column}=?",
                (value,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row[json_column])
        if self._hash(raw) != row[hash_column]:
            raise OfflineReplayError(f"persisted {table} integrity failed")
        return json.loads(raw)

    def record_snapshot(self, snapshot: ReplayVersionSnapshot) -> bool:
        value = snapshot.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT snapshot_json FROM replay_version_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                if not self._same(existing["snapshot_json"], value):
                    raise OfflineReplayError("replay snapshot identity was reused")
                return False
            connection.execute(
                "INSERT INTO replay_version_snapshots(snapshot_id,policy_id,policy_version,evidence_level,advisory_only,external_calls_allowed,routing_authority,may_change_actual_route,snapshot_json,snapshot_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot.snapshot_id, snapshot.policy_id, snapshot.policy_version, snapshot.evidence_level, int(snapshot.advisory_only), int(snapshot.external_calls_allowed), int(snapshot.routing_authority), int(snapshot.may_change_actual_route), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_case(self, case: ReplayCase) -> bool:
        value = case.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            if connection.execute("SELECT 1 FROM replay_version_snapshots WHERE snapshot_id=?", (case.snapshot_id,)).fetchone() is None:
                raise OfflineReplayError("replay case requires a persisted snapshot")
            rows = connection.execute(
                "SELECT case_json FROM offline_replay_cases WHERE case_id=? OR case_hash=?",
                (case.case_id, case.case_hash),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["case_json"], value):
                    raise OfflineReplayError("replay case identity was reused")
                return False
            connection.execute(
                "INSERT INTO offline_replay_cases(case_id,snapshot_id,case_hash,task_family,risk_level,multi_route_evaluation_allowed,case_json,payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (case.case_id, case.snapshot_id, case.case_hash, case.task_family, case.risk_level, int(case.multi_route_evaluation_allowed), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_outcome(self, outcome: ObservedRouteOutcome) -> bool:
        value = outcome.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            case = connection.execute("SELECT case_json FROM offline_replay_cases WHERE case_id=?", (outcome.case_id,)).fetchone()
            if case is None:
                raise OfflineReplayError("route outcome requires a persisted replay case")
            route_ids = {item["route_id"] for item in json.loads(case["case_json"])["candidates"]}
            if outcome.route_id not in route_ids:
                raise OfflineReplayError("route outcome is not a candidate of its case")
            rows = connection.execute(
                "SELECT outcome_json FROM observed_route_outcomes WHERE outcome_id=? OR (case_id=? AND route_id=?)",
                (outcome.outcome_id, outcome.case_id, outcome.route_id),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["outcome_json"], value):
                    raise OfflineReplayError("route outcome identity was reused")
                return False
            connection.execute(
                "INSERT INTO observed_route_outcomes(outcome_id,case_id,route_id,evidence_level,data_origin,independently_validated,outcome_json,outcome_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (outcome.outcome_id, outcome.case_id, outcome.route_id, outcome.evidence_level, outcome.data_origin, int(outcome.independently_validated), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_replay(self, replay: OfflineReplayResult) -> bool:
        value = replay.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            case = connection.execute("SELECT snapshot_id,case_hash FROM offline_replay_cases WHERE case_id=?", (replay.case_id,)).fetchone()
            if case is None or case["snapshot_id"] != replay.snapshot_id or case["case_hash"] != replay.case_hash:
                raise OfflineReplayError("replay result requires its persisted snapshot and case")
            rows = connection.execute(
                "SELECT replay_json FROM offline_replay_results WHERE replay_id=? OR (case_id=? AND policy_id=? AND policy_version=?)",
                (replay.replay_id, replay.case_id, replay.policy_id, replay.policy_version),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["replay_json"], value):
                    raise OfflineReplayError("offline replay identity was reused")
                return False
            connection.execute(
                "INSERT INTO offline_replay_results(replay_id,snapshot_id,case_id,policy_id,policy_version,state,external_call_count,advisory_only,routing_authority,may_change_actual_route,replay_json,replay_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (replay.replay_id, replay.snapshot_id, replay.case_id, replay.policy_id, replay.policy_version, replay.state, replay.external_call_count, int(replay.advisory_only), int(replay.routing_authority), int(replay.may_change_actual_route), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_oracle(self, report: ApproximateOracleReport) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            case = connection.execute("SELECT snapshot_id,case_hash FROM offline_replay_cases WHERE case_id=?", (report.case_id,)).fetchone()
            if case is None or case["snapshot_id"] != report.snapshot_id or case["case_hash"] != report.case_hash:
                raise OfflineReplayError("oracle requires its persisted snapshot and case")
            persisted = connection.execute(
                "SELECT outcome_id,outcome_json FROM observed_route_outcomes WHERE case_id=? ORDER BY outcome_id",
                (report.case_id,),
            ).fetchall()
            if tuple(row["outcome_id"] for row in persisted) != report.outcome_ids:
                raise OfflineReplayError("oracle outcome set is not fully persisted")
            outcome_payloads = [json.loads(row["outcome_json"]) for row in persisted]
            if self._hash(canonical_json(outcome_payloads)) != report.outcome_set_hash:
                raise OfflineReplayError("oracle outcome set hash is inconsistent")
            rows = connection.execute(
                "SELECT oracle_json FROM approximate_oracle_reports WHERE oracle_id=? OR (case_id=? AND outcome_set_hash=?)",
                (report.oracle_id, report.case_id, report.outcome_set_hash),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["oracle_json"], value):
                    raise OfflineReplayError("approximate oracle identity was reused")
                return False
            connection.execute(
                "INSERT INTO approximate_oracle_reports(oracle_id,snapshot_id,case_id,outcome_set_hash,state,directional_evidence_eligible,approximate_only,global_optimality_proven,external_call_count,advisory_only,routing_authority,may_change_actual_route,commercial_claims_allowed,oracle_json,oracle_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (report.oracle_id, report.snapshot_id, report.case_id, report.outcome_set_hash, report.state, int(report.directional_evidence_eligible), int(report.approximate_only), int(report.global_optimality_proven), report.external_call_count, int(report.advisory_only), int(report.routing_authority), int(report.may_change_actual_route), int(report.commercial_claims_allowed), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_benchmark(self, report: ReplayBenchmarkReport) -> bool:
        value = report.to_dict()
        raw = canonical_json(value)
        replay_ids = tuple(item["replay_id"] for item in report.replay_results)
        oracle_ids = tuple(item["oracle_id"] for item in report.oracle_results)
        with self._lock, self._connection() as connection:
            if connection.execute("SELECT 1 FROM replay_version_snapshots WHERE snapshot_id=?", (report.snapshot_id,)).fetchone() is None:
                raise OfflineReplayError("benchmark requires a persisted snapshot")
            for table, column, values in (
                ("offline_replay_results", "replay_id", replay_ids),
                ("approximate_oracle_reports", "oracle_id", oracle_ids),
            ):
                found = 0
                for value_id in values:
                    found += connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value_id,)).fetchone()[0]
                if found != len(values):
                    raise OfflineReplayError("benchmark requires all linked replay evidence")
            rows = connection.execute(
                "SELECT report_json FROM replay_benchmark_reports WHERE report_id=? OR (snapshot_id=? AND case_set_hash=? AND policy_set_hash=? AND outcome_set_hash=?)",
                (report.report_id, report.snapshot_id, report.case_set_hash, report.policy_set_hash, report.outcome_set_hash),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["report_json"], value):
                    raise OfflineReplayError("replay benchmark identity was reused")
                return False
            connection.execute(
                "INSERT INTO replay_benchmark_reports(report_id,snapshot_id,case_set_hash,policy_set_hash,outcome_set_hash,state,threshold_assessment_allowed,external_call_count,advisory_only,routing_authority,may_change_actual_route,policy_promotion_allowed,commercial_claims_allowed,report_json,report_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (report.report_id, report.snapshot_id, report.case_set_hash, report.policy_set_hash, report.outcome_set_hash, report.state, int(report.normalized_regret_threshold_assessment_allowed), report.external_call_count, int(report.advisory_only), int(report.routing_authority), int(report.may_change_actual_route), int(report.policy_promotion_allowed), int(report.commercial_claims_allowed), raw, self._hash(raw), utc_now()),
            )
        return True

    def snapshot(self, value: str) -> dict[str, Any] | None:
        return self._read("replay_version_snapshots", "snapshot_id", "snapshot_json", "snapshot_hash", value)

    def replay_case(self, value: str) -> dict[str, Any] | None:
        return self._read("offline_replay_cases", "case_id", "case_json", "payload_hash", value)

    def benchmark(self, value: str) -> dict[str, Any] | None:
        return self._read("replay_benchmark_reports", "report_id", "report_json", "report_hash", value)

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            names = {
                "replay_version_snapshots": "replay_version_snapshots",
                "offline_replay_cases": "offline_replay_cases",
                "observed_route_outcomes": "observed_route_outcomes",
                "offline_replay_results": "offline_replay_results",
                "approximate_oracle_reports": "approximate_oracle_reports",
                "replay_benchmark_reports": "replay_benchmark_reports",
            }
            result = {
                key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for key, table in names.items()
            }
            result.update(
                {
                    "offline_replay_external_calls": 0,
                    "offline_replay_route_interventions": 0,
                    "offline_replay_global_optimality_claims": 0,
                    "offline_replay_production_promotions": 0,
                }
            )
            return result


__all__ = ["OfflineReplayStore"]
