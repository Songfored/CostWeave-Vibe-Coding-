"""Immutable SQLite persistence for the v0.5.7.3 policy registry."""

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
from .policy_registry import (
    PolicyManifest,
    PolicyRegistryError,
    PolicyRollbackReceipt,
    PolicyTransitionReceipt,
    PromotionEvidence,
    PromotionGateReport,
    RegistryPolicyCard,
)


class PolicyRegistryStore:
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

    @staticmethod
    def _policy_link(connection: sqlite3.Connection, policy_id: str, version: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT policy_version,signature_verified FROM registered_policies WHERE registry_policy_id=?",
            (policy_id,),
        ).fetchone()
        if row is None or row["policy_version"] != version:
            raise PolicyRegistryError("registry artifact requires its signed policy version")
        return row

    @staticmethod
    def _state(connection: sqlite3.Connection, policy_id: str) -> tuple[str, int]:
        if connection.execute(
            "SELECT 1 FROM registered_policies WHERE registry_policy_id=?", (policy_id,)
        ).fetchone() is None:
            raise PolicyRegistryError("registered policy does not exist")
        row = connection.execute(
            "SELECT to_state,to_revision FROM policy_state_transitions WHERE registry_policy_id=? ORDER BY to_revision DESC LIMIT 1",
            (policy_id,),
        ).fetchone()
        return ("draft", 0) if row is None else (str(row["to_state"]), int(row["to_revision"]))

    def record_manifest(self, item: PolicyManifest, *, signing_key: bytes) -> bool:
        if not item.verify_signature(signing_key):
            raise PolicyRegistryError("policy manifest signature verification failed")
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT manifest_json FROM registered_policies WHERE registry_policy_id=? OR content_hash=? OR (policy_name=? AND policy_version=?)",
                (item.registry_policy_id, item.content_hash, item.policy_name, item.policy_version),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["manifest_json"], value):
                    raise PolicyRegistryError("registered policy identity or version was reused")
                return False
            connection.execute(
                "INSERT INTO registered_policies(registry_policy_id,policy_name,policy_version,signer_key_id,content_hash,signature,signature_verified,initial_state,advisory_only,routing_authority,may_change_actual_route,runtime_activation_executed,manifest_json,manifest_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.registry_policy_id, item.policy_name, item.policy_version, item.signer_key_id, item.content_hash, item.signature, 1, item.initial_state, int(item.advisory_only), int(item.routing_authority), int(item.may_change_actual_route), int(item.runtime_activation_executed), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_evidence(self, item: PromotionEvidence) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            self._policy_link(connection, item.registry_policy_id, item.policy_version)
            rows = connection.execute(
                "SELECT evidence_json FROM policy_promotion_evidence WHERE evidence_id=?",
                (item.evidence_id,),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["evidence_json"], value):
                    raise PolicyRegistryError("promotion evidence identity was reused")
                return False
            connection.execute(
                "INSERT INTO policy_promotion_evidence(evidence_id,registry_policy_id,policy_version,evidence_level,data_origin,independent_evaluation,case_count,cohort_hash,advisory_only,may_change_actual_route,evidence_json,evidence_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.evidence_id, item.registry_policy_id, item.policy_version, item.evidence_level, item.data_origin, int(item.independent_evaluation), item.case_count, item.cohort_hash, int(item.advisory_only), int(item.may_change_actual_route), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_gate(self, item: PromotionGateReport) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            self._policy_link(connection, item.registry_policy_id, item.policy_version)
            evidence = connection.execute(
                "SELECT registry_policy_id,policy_version FROM policy_promotion_evidence WHERE evidence_id=?",
                (item.evidence_id,),
            ).fetchone()
            if evidence is None or evidence["registry_policy_id"] != item.registry_policy_id or evidence["policy_version"] != item.policy_version:
                raise PolicyRegistryError("promotion gate requires matching persisted evidence")
            rows = connection.execute(
                "SELECT report_json FROM policy_promotion_gate_reports WHERE gate_report_id=? OR (registry_policy_id=? AND evidence_id=? AND stage=?)",
                (item.gate_report_id, item.registry_policy_id, item.evidence_id, item.stage),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["report_json"], value):
                    raise PolicyRegistryError("promotion gate identity was reused")
                return False
            connection.execute(
                "INSERT INTO policy_promotion_gate_reports(gate_report_id,registry_policy_id,policy_version,evidence_id,stage,eligible,decision_code,registry_transition_allowed,routing_authority,may_change_actual_route,runtime_activation_executed,report_json,report_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.gate_report_id, item.registry_policy_id, item.policy_version, item.evidence_id, item.stage, int(item.eligible), item.decision_code, int(item.registry_transition_allowed), int(item.routing_authority), int(item.may_change_actual_route), int(item.runtime_activation_executed), raw, self._hash(raw), utc_now()),
            )
        return True

    def _insert_transition(self, connection: sqlite3.Connection, item: PolicyTransitionReceipt) -> None:
        self._policy_link(connection, item.registry_policy_id, item.policy_version)
        state, revision = self._state(connection, item.registry_policy_id)
        if state != item.from_state or revision != item.from_revision:
            raise PolicyRegistryError("stale policy transition revision")
        if item.gate_report_id:
            gate = connection.execute(
                "SELECT registry_policy_id,eligible FROM policy_promotion_gate_reports WHERE gate_report_id=?",
                (item.gate_report_id,),
            ).fetchone()
            if gate is None or gate["registry_policy_id"] != item.registry_policy_id or not gate["eligible"]:
                raise PolicyRegistryError("policy transition requires its eligible persisted gate")
        value = item.to_dict()
        raw = canonical_json(value)
        connection.execute(
            "INSERT INTO policy_state_transitions(transition_id,registry_policy_id,policy_version,from_state,to_state,from_revision,to_revision,gate_report_id,signature_verified,approval_id,reason_code,registry_pointer_changed,runtime_route_changed,execution_side_effect_count,transition_json,transition_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item.transition_id, item.registry_policy_id, item.policy_version, item.from_state, item.to_state, item.from_revision, item.to_revision, item.gate_report_id, int(item.signature_verified), item.approval_id, item.reason_code, int(item.registry_pointer_changed), int(item.runtime_route_changed), item.execution_side_effect_count, raw, self._hash(raw), utc_now()),
        )
        if item.to_state == "active":
            connection.execute(
                "INSERT INTO policy_registry_pointer_events(event_id,registry_policy_id,source_type,runtime_route_changed,created_at) VALUES(?,?,?,?,?)",
                (item.transition_id, item.registry_policy_id, "activation", 0, utc_now()),
            )
        elif item.registry_pointer_changed and item.to_state in {"frozen", "retired"}:
            pointer = connection.execute(
                "SELECT registry_policy_id FROM policy_registry_pointer_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if pointer is not None and pointer["registry_policy_id"] == item.registry_policy_id:
                connection.execute(
                    "INSERT INTO policy_registry_pointer_events(event_id,registry_policy_id,source_type,runtime_route_changed,created_at) VALUES(?,?,?,?,?)",
                    (item.transition_id, None, "freeze" if item.to_state == "frozen" else "retirement", 0, utc_now()),
                )

    def record_transition(self, item: PolicyTransitionReceipt) -> bool:
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT transition_json FROM policy_state_transitions WHERE transition_id=?",
                (item.transition_id,),
            ).fetchone()
            if existing:
                if not self._same(existing["transition_json"], item.to_dict()):
                    raise PolicyRegistryError("policy transition identity was reused")
                return False
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_transition(connection, item)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def record_card(self, item: RegistryPolicyCard) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            self._policy_link(connection, item.registry_policy_id, item.policy_version)
            state, revision = self._state(connection, item.registry_policy_id)
            if state != item.current_state or revision != item.current_revision:
                raise PolicyRegistryError("policy card describes a stale registry state")
            rows = connection.execute(
                "SELECT card_json FROM registry_policy_cards WHERE policy_card_id=?",
                (item.policy_card_id,),
            ).fetchall()
            if rows:
                if len(rows) != 1 or not self._same(rows[0]["card_json"], value):
                    raise PolicyRegistryError("policy card identity was reused")
                return False
            connection.execute(
                "INSERT INTO registry_policy_cards(policy_card_id,registry_policy_id,policy_version,current_state,current_revision,signature_verified,latest_gate_report_id,advisory_only,routing_authority,runtime_route_changed,card_json,card_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item.policy_card_id, item.registry_policy_id, item.policy_version, item.current_state, item.current_revision, int(item.signature_verified), item.latest_gate_report_id, int(item.advisory_only), int(item.routing_authority), int(item.runtime_route_changed), raw, self._hash(raw), utc_now()),
            )
        return True

    def record_rollback(self, freeze: PolicyTransitionReceipt, item: PolicyRollbackReceipt) -> bool:
        """Atomically freeze the current registry asset and repoint its registry target."""

        if freeze.transition_id != item.freeze_transition_id or freeze.registry_policy_id != item.frozen_policy_id or freeze.to_state != "frozen":
            raise PolicyRegistryError("rollback does not contain its matching freeze receipt")
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT rollback_json FROM policy_registry_rollbacks WHERE rollback_id=?",
                (item.rollback_id,),
            ).fetchone()
            if existing:
                if not self._same(existing["rollback_json"], value):
                    raise PolicyRegistryError("policy rollback identity was reused")
                return False
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_transition(connection, freeze)
                target = self._policy_link(connection, item.rollback_policy_id, item.rollback_policy_version)
                target_state, _ = self._state(connection, item.rollback_policy_id)
                if target_state != item.rollback_policy_state:
                    raise PolicyRegistryError("rollback target registry state changed")
                connection.execute(
                    "INSERT INTO policy_registry_rollbacks(rollback_id,frozen_policy_id,freeze_transition_id,rollback_policy_id,rollback_policy_state,current_policy_frozen,registry_pointer_changed,runtime_route_changed,runtime_rollback_executed,execution_side_effect_count,rollback_json,rollback_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item.rollback_id, item.frozen_policy_id, item.freeze_transition_id, item.rollback_policy_id, item.rollback_policy_state, int(item.current_policy_frozen), int(item.registry_pointer_changed), int(item.runtime_route_changed), int(item.runtime_rollback_executed), item.execution_side_effect_count, raw, self._hash(raw), utc_now()),
                )
                connection.execute(
                    "INSERT INTO policy_registry_pointer_events(event_id,registry_policy_id,source_type,runtime_route_changed,created_at) VALUES(?,?,?,?,?)",
                    (item.rollback_id, item.rollback_policy_id, "rollback", 0, utc_now()),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def current_state(self, policy_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            state, revision = self._state(connection, policy_id)
        return {"registry_policy_id": policy_id, "state": state, "revision": revision}

    def current_registry_pointer(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT sequence,event_id,registry_policy_id,source_type,created_at FROM policy_registry_pointer_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        if row is None or row["registry_policy_id"] is None:
            return None
        return dict(row)

    def manifest(self, policy_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT manifest_json,manifest_hash FROM registered_policies WHERE registry_policy_id=?",
                (policy_id,),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["manifest_json"])
        if self._hash(raw) != row["manifest_hash"]:
            raise PolicyRegistryError("persisted policy manifest integrity failed")
        return json.loads(raw)

    def counts(self) -> dict[str, int]:
        tables = {
            "policy_registry_policies": "registered_policies",
            "policy_registry_evidence": "policy_promotion_evidence",
            "policy_registry_gate_reports": "policy_promotion_gate_reports",
            "policy_registry_transitions": "policy_state_transitions",
            "policy_registry_cards": "registry_policy_cards",
            "policy_registry_rollbacks": "policy_registry_rollbacks",
            "policy_registry_pointer_events": "policy_registry_pointer_events",
        }
        with self._connection() as connection:
            result = {
                key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for key, table in tables.items()
            }
            result["policy_registry_eligible_gates"] = int(
                connection.execute("SELECT COUNT(*) FROM policy_promotion_gate_reports WHERE eligible=1").fetchone()[0]
            )
            for state in ("evaluated", "shadow", "canary", "active", "frozen", "retired"):
                result[f"policy_registry_{state}_transitions"] = int(
                    connection.execute("SELECT COUNT(*) FROM policy_state_transitions WHERE to_state=?", (state,)).fetchone()[0]
                )
        result.update(
            {
                "policy_registry_runtime_route_changes": 0,
                "policy_registry_runtime_activations": 0,
                "policy_registry_runtime_rollbacks": 0,
                "policy_registry_execution_side_effects": 0,
            }
        )
        return result


__all__ = ["PolicyRegistryStore"]
