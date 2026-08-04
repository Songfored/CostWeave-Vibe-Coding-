"""Immutable persistence for v0.5.7.2 production-like shadow evidence."""

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
from .production_shadow import (
    ProductionShadowAuditReport,
    ProductionShadowComparison,
    ProductionShadowDecision,
    ProductionShadowError,
    ProductionShadowFailureReceipt,
    ProductionShadowInput,
    ShadowActualOutcome,
)


class ProductionShadowStore:
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
            raise ProductionShadowError(f"persisted {table} integrity failed")
        return json.loads(raw)

    def record_input(self, item: ProductionShadowInput) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT input_json FROM production_shadow_inputs WHERE input_id=? OR input_hash=?",
                (item.input_id, item.input_hash),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["input_json"], value):
                    raise ProductionShadowError("production shadow input identity was reused")
                return False
            connection.execute(
                "INSERT INTO production_shadow_inputs(input_id,input_hash,task_feature_hash,catalog_snapshot_hash,capacity_snapshot_hash,actual_route_id,actual_route_fingerprint,evidence_level,data_origin,same_features_as_baseline,same_catalog_as_baseline,same_capacity_as_baseline,advisory_only,routing_authority,may_change_actual_route,input_json,payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.input_id, item.input_hash, item.task_feature_hash, item.catalog_snapshot_hash, item.capacity_snapshot_hash, item.actual_route_id, item.actual_route_fingerprint, item.evidence_level, item.data_origin, int(item.same_features_as_baseline), int(item.same_catalog_as_baseline), int(item.same_capacity_as_baseline), int(item.advisory_only), int(item.routing_authority), int(item.may_change_actual_route), raw, self._hash(raw), utc_now()),
            )
        return True

    def _linked_input(self, connection: sqlite3.Connection, input_id: str, input_hash: str, actual_route_id: str, actual_route_fingerprint: str) -> None:
        row = connection.execute(
            "SELECT input_hash,actual_route_id,actual_route_fingerprint FROM production_shadow_inputs WHERE input_id=?",
            (input_id,),
        ).fetchone()
        if row is None or row["input_hash"] != input_hash or row["actual_route_id"] != actual_route_id or row["actual_route_fingerprint"] != actual_route_fingerprint:
            raise ProductionShadowError("shadow result requires its persisted baseline input")

    def record_decision(self, item: ProductionShadowDecision) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            self._linked_input(connection, item.input_id, item.input_hash, item.actual_route_id, item.actual_route_fingerprint)
            if connection.execute("SELECT 1 FROM production_shadow_failures WHERE input_id=? AND policy_id=? AND policy_version=?", (item.input_id, item.policy_id, item.policy_version)).fetchone() is not None:
                raise ProductionShadowError("shadow input already has an isolated failure")
            rows = connection.execute(
                "SELECT decision_json FROM production_shadow_decisions WHERE decision_id=? OR (input_id=? AND policy_id=? AND policy_version=?)",
                (item.decision_id, item.input_id, item.policy_id, item.policy_version),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["decision_json"], value):
                    raise ProductionShadowError("production shadow decision identity was reused")
                return False
            connection.execute(
                "INSERT INTO production_shadow_decisions(decision_id,input_id,policy_id,policy_version,state,missing_reason,baseline_execution_continues,actual_route_changed,external_call_count,execution_side_effect_count,advisory_only,routing_authority,policy_promotion_allowed,decision_json,decision_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.decision_id, item.input_id, item.policy_id, item.policy_version, item.state, item.missing_reason, int(item.baseline_execution_continues), int(item.actual_route_changed), item.external_call_count, item.execution_side_effect_count, int(item.advisory_only), int(item.routing_authority), int(item.policy_promotion_allowed), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_failure(self, item: ProductionShadowFailureReceipt) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            self._linked_input(connection, item.input_id, item.input_hash, item.actual_route_id, item.actual_route_fingerprint)
            if connection.execute("SELECT 1 FROM production_shadow_decisions WHERE input_id=? AND policy_id=? AND policy_version=?", (item.input_id, item.policy_id, item.policy_version)).fetchone() is not None:
                raise ProductionShadowError("shadow input already has a decision")
            rows = connection.execute(
                "SELECT failure_json FROM production_shadow_failures WHERE failure_id=? OR (input_id=? AND policy_id=? AND policy_version=?)",
                (item.failure_id, item.input_id, item.policy_id, item.policy_version),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["failure_json"], value):
                    raise ProductionShadowError("production shadow failure identity was reused")
                return False
            connection.execute(
                "INSERT INTO production_shadow_failures(failure_id,input_id,policy_id,policy_version,error_code,baseline_execution_continues,actual_route_changed,error_message_persisted,external_call_count,execution_side_effect_count,advisory_only,routing_authority,failure_json,failure_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.failure_id, item.input_id, item.policy_id, item.policy_version, item.error_code, int(item.baseline_execution_continues), int(item.actual_route_changed), int(item.error_message_persisted), item.external_call_count, item.execution_side_effect_count, int(item.advisory_only), int(item.routing_authority), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_outcome(self, item: ShadowActualOutcome) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            baseline = connection.execute("SELECT actual_route_id FROM production_shadow_inputs WHERE input_id=?", (item.input_id,)).fetchone()
            if baseline is None or baseline["actual_route_id"] != item.actual_route_id:
                raise ProductionShadowError("shadow outcome does not match a persisted baseline route")
            rows = connection.execute(
                "SELECT outcome_json FROM production_shadow_outcomes WHERE outcome_id=? OR input_id=?",
                (item.outcome_id, item.input_id),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["outcome_json"], value):
                    raise ProductionShadowError("production shadow outcome identity was reused")
                return False
            connection.execute(
                "INSERT INTO production_shadow_outcomes(outcome_id,input_id,actual_route_id,evidence_level,data_origin,independently_validated,outcome_json,outcome_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (item.outcome_id, item.input_id, item.actual_route_id, item.evidence_level, item.data_origin, int(item.independently_validated), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_comparison(self, item: ProductionShadowComparison) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            links = connection.execute(
                "SELECT (SELECT input_id FROM production_shadow_decisions WHERE decision_id=?) AS decision_input,(SELECT input_id FROM production_shadow_outcomes WHERE outcome_id=?) AS outcome_input,(SELECT actual_route_id FROM production_shadow_inputs WHERE input_id=?) AS actual_route",
                (item.decision_id, item.outcome_id, item.input_id),
            ).fetchone()
            if links is None or links["decision_input"] != item.input_id or links["outcome_input"] != item.input_id or links["actual_route"] != item.actual_route_id:
                raise ProductionShadowError("shadow comparison requires matching persisted evidence")
            rows = connection.execute(
                "SELECT comparison_json FROM production_shadow_comparisons WHERE comparison_id=? OR input_id=? OR decision_id=? OR outcome_id=?",
                (item.comparison_id, item.input_id, item.decision_id, item.outcome_id),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["comparison_json"], value):
                    raise ProductionShadowError("production shadow comparison identity was reused")
                return False
            connection.execute(
                "INSERT INTO production_shadow_comparisons(comparison_id,input_id,decision_id,outcome_id,counterfactual_outcome_known,counterfactual_savings_claim_allowed,actual_route_changed,advisory_only,routing_authority,comparison_json,comparison_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.comparison_id, item.input_id, item.decision_id, item.outcome_id, int(item.counterfactual_outcome_known), int(item.counterfactual_savings_claim_allowed), int(item.actual_route_changed), int(item.advisory_only), int(item.routing_authority), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_audit(self, item: ProductionShadowAuditReport) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            payloads: dict[str, list[dict[str, Any]]] = {}
            for key, table, column, json_column, ids in (
                ("inputs", "production_shadow_inputs", "input_id", "input_json", item.input_ids),
                ("decisions", "production_shadow_decisions", "decision_id", "decision_json", item.decision_ids),
                ("failures", "production_shadow_failures", "failure_id", "failure_json", item.failure_ids),
                ("comparisons", "production_shadow_comparisons", "comparison_id", "comparison_json", item.comparison_ids),
            ):
                found: list[dict[str, Any]] = []
                for identity in ids:
                    row = connection.execute(f"SELECT {json_column} FROM {table} WHERE {column}=?", (identity,)).fetchone()
                    if row is None:
                        raise ProductionShadowError("shadow audit requires all persisted evidence")
                    found.append(json.loads(row[json_column]))
                payloads[key] = found
            if self._hash(canonical_json(payloads)) != item.batch_hash:
                raise ProductionShadowError("shadow audit batch hash is inconsistent")
            rows = connection.execute(
                "SELECT report_json FROM production_shadow_audit_reports WHERE report_id=? OR batch_hash=?",
                (item.report_id, item.batch_hash),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["report_json"], value):
                    raise ProductionShadowError("production shadow audit identity was reused")
                return False
            connection.execute(
                "INSERT INTO production_shadow_audit_reports(report_id,batch_hash,state,directional_evidence_eligible,cost_overhead_principle_passed,baseline_execution_continuity_proven,execution_side_effect_count,actual_route_change_count,external_call_count,advisory_only,routing_authority,policy_promotion_allowed,commercial_claims_allowed,report_json,report_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.report_id, item.batch_hash, item.state, int(item.directional_evidence_eligible), int(item.cost_overhead_principle_passed), int(item.baseline_execution_continuity_proven), item.execution_side_effect_count, item.actual_route_change_count, item.external_call_count, int(item.advisory_only), int(item.routing_authority), int(item.policy_promotion_allowed), int(item.commercial_claims_allowed), raw, self._hash(raw), utc_now()),
            )
        return True

    def shadow_input(self, value: str) -> dict[str, Any] | None:
        return self._read("production_shadow_inputs", "input_id", "input_json", "payload_hash", value)

    def audit(self, value: str) -> dict[str, Any] | None:
        return self._read("production_shadow_audit_reports", "report_id", "report_json", "report_hash", value)

    def counts(self) -> dict[str, int]:
        names = {
            "production_shadow_inputs": "production_shadow_inputs",
            "production_shadow_decisions": "production_shadow_decisions",
            "production_shadow_failures": "production_shadow_failures",
            "production_shadow_outcomes": "production_shadow_outcomes",
            "production_shadow_comparisons": "production_shadow_comparisons",
            "production_shadow_audit_reports": "production_shadow_audit_reports",
        }
        with self._connection() as connection:
            result = {
                key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for key, table in names.items()
            }
        result.update(
            {
                "production_shadow_external_calls": 0,
                "production_shadow_execution_side_effects": 0,
                "production_shadow_route_interventions": 0,
                "production_shadow_production_promotions": 0,
            }
        )
        return result


__all__ = ["ProductionShadowStore"]
