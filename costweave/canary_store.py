"""Append-only SQLite persistence for v0.5.7.4 runtime canaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .canary_control import (
    CanaryAssignment,
    CanaryControlError,
    CanaryDecision,
    CanaryDeployment,
    CanaryWindow,
    assign_canary_policy,
    canary_runtime_enabled,
    evaluate_canary_window,
)
from .evaluation_protocol import canonical_json


class CanaryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _hash(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _same(raw: str, value: Mapping[str, Any]) -> bool:
        return json.loads(raw) == dict(value)

    @staticmethod
    def _deployment(raw: str) -> CanaryDeployment:
        value = json.loads(raw)
        value["allowed_task_families"] = tuple(value["allowed_task_families"])
        value["prohibited_task_families"] = tuple(value["prohibited_task_families"])
        return CanaryDeployment(**value)

    def start_deployment(self, item: CanaryDeployment) -> bool:
        """Validate registry lineage and append the initial 5% pointer."""

        if not canary_runtime_enabled():
            raise CanaryControlError("runtime canary is disabled; explicit opt-in is required")
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT deployment_json FROM canary_deployments WHERE deployment_id=?",
                (item.deployment_id,),
            ).fetchone()
            if existing:
                if not self._same(existing["deployment_json"], value):
                    raise CanaryControlError("canary deployment identity was reused")
                return False
            latest = connection.execute(
                "SELECT event_type FROM canary_runtime_pointer_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if latest is not None and latest["event_type"] not in {"rolled_back", "completed"}:
                raise CanaryControlError("another canary deployment is already active")
            candidate = connection.execute(
                "SELECT policy_name,policy_version,content_hash,manifest_json FROM registered_policies WHERE registry_policy_id=?",
                (item.candidate_policy_id,),
            ).fetchone()
            stable = connection.execute(
                "SELECT policy_name,policy_version,content_hash,manifest_json FROM registered_policies WHERE registry_policy_id=?",
                (item.stable_policy_id,),
            ).fetchone()
            if candidate is None or stable is None:
                raise CanaryControlError("canary deployment requires registered candidate and stable policies")
            if candidate["policy_version"] != item.candidate_policy_version or stable["policy_version"] != item.stable_policy_version:
                raise CanaryControlError("canary deployment policy version does not match the registry")
            if candidate["content_hash"] != item.candidate_manifest_hash or stable["content_hash"] != item.stable_manifest_hash:
                raise CanaryControlError("canary deployment manifest hash does not match the registry")

            def current_state(policy_id: str) -> str:
                row = connection.execute(
                    "SELECT to_state FROM policy_state_transitions WHERE registry_policy_id=? ORDER BY to_revision DESC LIMIT 1",
                    (policy_id,),
                ).fetchone()
                return str(row["to_state"]) if row else "draft"

            if current_state(item.candidate_policy_id) != "canary":
                raise CanaryControlError("candidate policy must be in the reviewed canary state")
            if current_state(item.stable_policy_id) != "active":
                raise CanaryControlError("rollback policy must be an active reviewed registry asset")
            pointer = connection.execute(
                "SELECT registry_policy_id FROM policy_registry_pointer_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if pointer is None or pointer["registry_policy_id"] != item.stable_policy_id:
                raise CanaryControlError("stable policy must be the current registry rollback pointer")
            gate = connection.execute(
                "SELECT registry_policy_id,policy_version,stage,eligible FROM policy_promotion_gate_reports WHERE gate_report_id=?",
                (item.canary_gate_report_id,),
            ).fetchone()
            if (
                gate is None
                or gate["registry_policy_id"] != item.candidate_policy_id
                or gate["policy_version"] != item.candidate_policy_version
                or gate["stage"] != "canary"
                or not gate["eligible"]
            ):
                raise CanaryControlError("canary deployment requires its eligible persisted canary gate")
            candidate_manifest = json.loads(candidate["manifest_json"])
            if (
                candidate_manifest.get("rollback_policy_name") != stable["policy_name"]
                or candidate_manifest.get("rollback_policy_version") != stable["policy_version"]
            ):
                raise CanaryControlError("signed candidate rollback target does not match stable policy")
            if tuple(candidate_manifest.get("allowed_task_families", ())) != item.allowed_task_families:
                raise CanaryControlError("canary allowed scope changed after policy signing")
            if tuple(candidate_manifest.get("prohibited_task_families", ())) != item.prohibited_task_families:
                raise CanaryControlError("canary prohibited scope changed after policy signing")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO canary_deployments(deployment_id,candidate_policy_id,candidate_policy_version,stable_policy_id,stable_policy_version,canary_gate_report_id,activation_approval_id,initial_stage_pct,automatic_rollback,high_risk_exploration_rate,provider_credentials_granted,tool_permissions_expanded,deployment_json,deployment_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.deployment_id, item.candidate_policy_id, item.candidate_policy_version,
                        item.stable_policy_id, item.stable_policy_version,
                        item.canary_gate_report_id, item.activation_approval_id,
                        item.initial_stage_pct, 1, 0.0, 0, 0, raw, self._hash(raw), item.started_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO canary_runtime_pointer_events(event_id,deployment_id,event_type,stage_pct,selected_policy_id,decision_id,in_flight_snapshots_preserved,new_runs_use_pointer_immediately,execution_side_effect_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.deployment_id, item.deployment_id, "started", 5,
                        item.stable_policy_id, None, 1, 1, 0, item.started_at,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def current_state(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT p.sequence,p.deployment_id,p.event_type,p.stage_pct,p.selected_policy_id,p.decision_id,p.created_at,d.deployment_json,d.deployment_hash "
                "FROM canary_runtime_pointer_events p JOIN canary_deployments d ON d.deployment_id=p.deployment_id "
                "ORDER BY p.sequence DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        raw = str(row["deployment_json"])
        if self._hash(raw) != row["deployment_hash"]:
            raise CanaryControlError("persisted canary deployment integrity failed")
        return {
            "sequence": int(row["sequence"]),
            "deployment_id": row["deployment_id"],
            "event_type": row["event_type"],
            "stage_pct": int(row["stage_pct"]),
            "selected_policy_id": row["selected_policy_id"],
            "decision_id": row["decision_id"],
            "created_at": row["created_at"],
            "deployment": json.loads(raw),
            "accepting_assignments": row["event_type"] in {"started", "advanced", "completed"},
        }

    def active_deployment(self) -> tuple[CanaryDeployment, int] | None:
        state = self.current_state()
        if state is None or not state["accepting_assignments"]:
            return None
        return self._deployment(canonical_json(state["deployment"])), int(state["stage_pct"])

    def assignment_context(self) -> tuple[CanaryDeployment, int, bool] | None:
        """Return the latest deployment, including a rolled-back stable pointer."""

        state = self.current_state()
        if state is None:
            return None
        return (
            self._deployment(canonical_json(state["deployment"])),
            int(state["stage_pct"]),
            bool(state["accepting_assignments"]),
        )

    def assignment_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT assignment_json,assignment_hash FROM canary_run_assignments WHERE run_id=?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["assignment_json"])
        if self._hash(raw) != row["assignment_hash"]:
            raise CanaryControlError("persisted canary assignment integrity failed")
        return json.loads(raw)

    def record_assignment(self, item: CanaryAssignment) -> bool:
        value = item.to_dict()
        raw = canonical_json(value)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                created = self._record_assignment_locked(
                    connection, item=item, value=value, raw=raw
                )
                connection.execute("COMMIT")
                return created
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _record_assignment_locked(
        self,
        connection: sqlite3.Connection,
        *,
        item: CanaryAssignment,
        value: Mapping[str, Any],
        raw: str,
    ) -> bool:
        existing = connection.execute(
            "SELECT assignment_json FROM canary_run_assignments WHERE assignment_id=? OR run_id=?",
            (item.assignment_id, item.run_id),
        ).fetchall()
        if existing:
            if len(existing) != 1 or not self._same(existing[0]["assignment_json"], value):
                raise CanaryControlError("run already has a different immutable policy snapshot")
            return False
        state = connection.execute(
            "SELECT event_type,stage_pct FROM canary_runtime_pointer_events WHERE deployment_id=? ORDER BY sequence DESC LIMIT 1",
            (item.deployment_id,),
        ).fetchone()
        if state is None or int(state["stage_pct"]) != item.stage_pct:
            raise CanaryControlError("canary pointer changed before assignment was committed")
        current = connection.execute(
            "SELECT deployment_id,event_type,stage_pct FROM "
            "canary_runtime_pointer_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if (
            current is None
            or current["deployment_id"] != item.deployment_id
            or current["event_type"] != state["event_type"]
            or int(current["stage_pct"]) != item.stage_pct
        ):
            raise CanaryControlError(
                "canary assignment does not use the current runtime pointer"
            )
        if state["event_type"] == "rolled_back" and item.cohort != "stable-only":
            raise CanaryControlError("a rolled-back deployment only permits stable snapshots")
        deployment_row = connection.execute(
            "SELECT deployment_json,deployment_hash FROM canary_deployments WHERE deployment_id=?",
            (item.deployment_id,),
        ).fetchone()
        if deployment_row is None or self._hash(deployment_row["deployment_json"]) != deployment_row["deployment_hash"]:
            raise CanaryControlError("canary assignment requires an intact deployment")
        deployment = self._deployment(str(deployment_row["deployment_json"]))
        expected = assign_canary_policy(
            deployment,
            run_id=item.run_id,
            stage_pct=item.stage_pct,
            risk_level=item.risk_level,
            task_families=item.task_families,
            exposure_enabled=state["event_type"] != "rolled_back",
            assigned_at=item.assigned_at,
        )
        if expected != item:
            raise CanaryControlError("canary assignment does not match deterministic admission")
        connection.execute(
            "INSERT INTO canary_run_assignments(assignment_id,deployment_id,run_id,stage_pct,bucket,risk_level,eligible_for_exploration,cohort,selected_policy_id,selected_policy_version,policy_snapshot_hash,runtime_policy_changed,provider_call_executed,assignment_json,assignment_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.assignment_id, item.deployment_id, item.run_id, item.stage_pct,
                item.bucket, item.risk_level, int(item.eligible_for_exploration), item.cohort,
                item.selected_policy_id, item.selected_policy_version, item.policy_snapshot_hash,
                int(item.runtime_policy_changed), 0, raw, self._hash(raw), item.assigned_at,
            ),
        )
        return True

    def assign_for_run(
        self,
        *,
        run_id: str,
        risk_level: str,
        task_families: tuple[str, ...],
    ) -> CanaryAssignment | None:
        """Read the pointer and persist its run snapshot in one transaction."""

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT assignment_json,assignment_hash FROM "
                    "canary_run_assignments WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if existing is not None:
                    raw = str(existing["assignment_json"])
                    if self._hash(raw) != existing["assignment_hash"]:
                        raise CanaryControlError(
                            "persisted canary assignment integrity failed"
                        )
                    value = json.loads(raw)
                    value["task_families"] = tuple(value["task_families"])
                    value["reasons"] = tuple(value["reasons"])
                    assignment = CanaryAssignment(**value)
                    connection.execute("COMMIT")
                    return assignment

                state = connection.execute(
                    "SELECT p.event_type,p.stage_pct,d.deployment_json,"
                    "d.deployment_hash FROM canary_runtime_pointer_events p "
                    "JOIN canary_deployments d ON d.deployment_id=p.deployment_id "
                    "ORDER BY p.sequence DESC LIMIT 1"
                ).fetchone()
                if state is None:
                    connection.execute("COMMIT")
                    return None
                deployment_raw = str(state["deployment_json"])
                if self._hash(deployment_raw) != state["deployment_hash"]:
                    raise CanaryControlError(
                        "persisted canary deployment integrity failed"
                    )
                deployment = self._deployment(deployment_raw)
                assignment = assign_canary_policy(
                    deployment,
                    run_id=str(run_id),
                    stage_pct=int(state["stage_pct"]),
                    risk_level=str(risk_level),
                    task_families=tuple(task_families),
                    exposure_enabled=state["event_type"] != "rolled_back",
                )
                value = assignment.to_dict()
                raw = canonical_json(value)
                self._record_assignment_locked(
                    connection,
                    item=assignment,
                    value=value,
                    raw=raw,
                )
                connection.execute("COMMIT")
                return assignment
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def record_window_decision(self, window: CanaryWindow, decision: CanaryDecision) -> bool:
        if decision.window_id != window.window_id or decision.deployment_id != window.deployment_id:
            raise CanaryControlError("canary decision does not belong to its observation")
        if evaluate_canary_window(window, evaluated_at=decision.evaluated_at) != decision:
            raise CanaryControlError("canary decision does not match automatic guardrail evaluation")
        window_value = window.to_dict()
        decision_value = decision.to_dict()
        window_raw = canonical_json(window_value)
        decision_raw = canonical_json(decision_value)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT decision_json FROM canary_stage_decisions WHERE decision_id=? OR window_id=?",
                (decision.decision_id, window.window_id),
            ).fetchall()
            if existing:
                if len(existing) != 1 or not self._same(existing[0]["decision_json"], decision_value):
                    raise CanaryControlError("canary decision identity was reused")
                return False
            state = connection.execute(
                "SELECT event_type,stage_pct FROM canary_runtime_pointer_events WHERE deployment_id=? ORDER BY sequence DESC LIMIT 1",
                (window.deployment_id,),
            ).fetchone()
            if state is None or state["event_type"] == "rolled_back" or int(state["stage_pct"]) != window.stage_pct:
                raise CanaryControlError("canary observation describes a stale deployment stage")
            deployment = connection.execute(
                "SELECT candidate_policy_id,stable_policy_id FROM canary_deployments WHERE deployment_id=?",
                (window.deployment_id,),
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO canary_observation_windows(window_id,deployment_id,stage_pct,candidate_cases,control_cases,hard_budget_violation_count,permission_violation_count,duplicate_external_side_effect_count,high_risk_candidate_cases,observation_json,observation_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        window.window_id, window.deployment_id, window.stage_pct,
                        window.candidate_cases, window.control_cases,
                        window.hard_budget_violation_count, window.permission_violation_count,
                        window.duplicate_external_side_effect_count, window.high_risk_candidate_cases,
                        window_raw, self._hash(window_raw), window.observed_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO canary_stage_decisions(decision_id,deployment_id,window_id,from_stage_pct,to_stage_pct,action,runtime_pointer_changed,automatic_rollback_executed,in_flight_snapshots_preserved,new_runs_use_decision_immediately,execution_side_effect_count,decision_json,decision_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision.decision_id, decision.deployment_id, decision.window_id,
                        decision.from_stage_pct, decision.to_stage_pct, decision.action,
                        int(decision.runtime_pointer_changed), int(decision.automatic_rollback_executed),
                        1, 1, 0, decision_raw, self._hash(decision_raw), decision.evaluated_at,
                    ),
                )
                if decision.action in {"advance", "rollback", "complete"}:
                    event_type = {
                        "advance": "advanced",
                        "rollback": "rolled_back",
                        "complete": "completed",
                    }[decision.action]
                    selected = (
                        deployment["stable_policy_id"]
                        if decision.action == "rollback"
                        else deployment["candidate_policy_id"]
                    )
                    connection.execute(
                        "INSERT INTO canary_runtime_pointer_events(event_id,deployment_id,event_type,stage_pct,selected_policy_id,decision_id,in_flight_snapshots_preserved,new_runs_use_pointer_immediately,execution_side_effect_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            decision.decision_id, decision.deployment_id, event_type,
                            decision.to_stage_pct, selected, decision.decision_id,
                            1, 1, 0, decision.evaluated_at,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def counts(self) -> dict[str, int]:
        tables = {
            "canary_deployments": "canary_deployments",
            "canary_pointer_events": "canary_runtime_pointer_events",
            "canary_assignments": "canary_run_assignments",
            "canary_observation_windows": "canary_observation_windows",
            "canary_stage_decisions": "canary_stage_decisions",
        }
        with self._connection() as connection:
            result = {
                key: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for key, table in tables.items()
            }
            result.update(
                {
                    "canary_candidate_assignments": int(connection.execute("SELECT COUNT(*) FROM canary_run_assignments WHERE cohort='candidate'").fetchone()[0]),
                    "canary_control_assignments": int(connection.execute("SELECT COUNT(*) FROM canary_run_assignments WHERE cohort='control'").fetchone()[0]),
                    "canary_stable_only_assignments": int(connection.execute("SELECT COUNT(*) FROM canary_run_assignments WHERE cohort='stable-only'").fetchone()[0]),
                    "canary_high_risk_candidate_assignments": int(connection.execute("SELECT COUNT(*) FROM canary_run_assignments WHERE cohort='candidate' AND risk_level NOT IN ('normal','low')").fetchone()[0]),
                    "canary_runtime_policy_changes": int(connection.execute("SELECT COUNT(*) FROM canary_run_assignments WHERE runtime_policy_changed=1").fetchone()[0]),
                    "canary_runtime_rollbacks": int(connection.execute("SELECT COUNT(*) FROM canary_stage_decisions WHERE action='rollback'").fetchone()[0]),
                    "canary_stage_advances": int(connection.execute("SELECT COUNT(*) FROM canary_stage_decisions WHERE action='advance'").fetchone()[0]),
                    "canary_provider_calls": int(connection.execute("SELECT COALESCE(SUM(provider_call_executed),0) FROM canary_run_assignments").fetchone()[0]),
                    "canary_execution_side_effects": int(connection.execute("SELECT COALESCE(SUM(execution_side_effect_count),0) FROM canary_stage_decisions").fetchone()[0]),
                }
            )
        return result


__all__ = ["CanaryStore"]
