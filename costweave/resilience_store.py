"""Append-only resilience campaigns, checkpoints and integrity audits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .security_boundary import normalize_project_id


class ResilienceStoreError(RuntimeError):
    pass


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ResilienceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def create_campaign(
        self,
        *,
        project_id: str,
        campaign_kind: str,
        spec: Mapping[str, Any],
        evidence_level: str = "E0-simulated",
    ) -> tuple[dict[str, Any], bool]:
        project_id = normalize_project_id(project_id)
        if campaign_kind not in {"stress", "fault", "migration", "recovery"}:
            raise ValueError("invalid resilience campaign kind")
        if evidence_level not in {"E0-simulated", "E1-replayed"}:
            raise ValueError("invalid resilience evidence level")
        normalized = {
            "project_id": project_id,
            "campaign_kind": campaign_kind,
            "evidence_level": evidence_level,
            "spec": dict(spec),
        }
        raw = _canonical_json(normalized)
        digest = _sha256(raw)
        campaign_id = "campaign_" + digest[:32]
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM resilience_campaigns WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchone()
                if existing is not None:
                    if existing["spec_hash"] != digest:
                        raise ResilienceStoreError(
                            "campaign identity reused for different content"
                        )
                    connection.rollback()
                    return self._campaign(existing), True
                connection.execute(
                    "INSERT INTO resilience_campaigns(campaign_id,project_id,"
                    "campaign_kind,evidence_level,spec_json,spec_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        campaign_id,
                        project_id,
                        campaign_kind,
                        evidence_level,
                        raw,
                        digest,
                        created_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "campaign_id": campaign_id,
            **normalized,
            "spec_hash": digest,
            "created_at": created_at,
        }, False

    def record_observation(
        self,
        campaign_id: str,
        *,
        observation_kind: str,
        observation: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        allowed = {
            "admission", "rate_limit", "provider_failure", "failover",
            "out_of_memory", "deadline_warning", "cancellation", "crash",
            "restore", "resource_leak", "summary",
        }
        if observation_kind not in allowed:
            raise ValueError("invalid resilience observation kind")
        key = " ".join(str(idempotency_key).strip().split())
        if len(key) < 8:
            raise ValueError("observation idempotency key is too short")
        normalized = {
            "campaign_id": str(campaign_id),
            "observation_kind": observation_kind,
            "observation": dict(observation),
        }
        raw = _canonical_json(normalized)
        digest = _sha256(raw)
        observation_id = "observation_" + _sha256(
            f"{campaign_id}:{key}"
        )[:32]
        observed_at = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM resilience_observations WHERE observation_id=?",
                    (observation_id,),
                ).fetchone()
                if existing is not None:
                    if existing["observation_hash"] != digest:
                        raise ResilienceStoreError(
                            "observation idempotency key reused for different content"
                        )
                    connection.rollback()
                    return self._observation(existing), True
                connection.execute(
                    "INSERT INTO resilience_observations(observation_id,campaign_id,"
                    "observation_kind,observation_json,observation_hash,observed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        observation_id,
                        campaign_id,
                        observation_kind,
                        raw,
                        digest,
                        observed_at,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResilienceStoreError(str(exc)) from exc
            except Exception:
                connection.rollback()
                raise
        return {
            "observation_id": observation_id,
            **normalized,
            "observation_hash": digest,
            "observed_at": observed_at,
        }, False

    def record_checkpoint(
        self,
        *,
        project_id: str,
        run_id: str,
        runtime_revision: int,
        plan: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        project_id = normalize_project_id(project_id)
        revision = max(0, int(runtime_revision))
        normalized = {
            "project_id": project_id,
            "run_id": str(run_id),
            "runtime_revision": revision,
            "plan": dict(plan),
        }
        raw = _canonical_json(normalized)
        digest = _sha256(raw)
        checkpoint_id = "checkpoint_" + _sha256(
            f"{run_id}:{revision}"
        )[:32]
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM recovery_checkpoints_v0583 WHERE checkpoint_id=?",
                    (checkpoint_id,),
                ).fetchone()
                if existing is not None:
                    if existing["plan_hash"] != digest:
                        raise ResilienceStoreError(
                            "checkpoint revision reused for different recovery plan"
                        )
                    connection.rollback()
                    return self._checkpoint(existing), True
                connection.execute(
                    "INSERT INTO recovery_checkpoints_v0583(checkpoint_id,project_id,"
                    "run_id,runtime_revision,plan_json,plan_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        checkpoint_id,
                        project_id,
                        run_id,
                        revision,
                        raw,
                        digest,
                        created_at,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResilienceStoreError(str(exc)) from exc
            except Exception:
                connection.rollback()
                raise
        return {
            "checkpoint_id": checkpoint_id,
            **normalized,
            "plan_hash": digest,
            "created_at": created_at,
        }, False

    def record_recovery_receipt(
        self,
        checkpoint_id: str,
        *,
        outcome: str,
        detail: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        if outcome not in {"planned", "resumed", "refused_terminal", "failed"}:
            raise ValueError("invalid recovery outcome")
        key = " ".join(str(idempotency_key).strip().split())
        if len(key) < 8:
            raise ValueError("recovery receipt idempotency key is too short")
        normalized = {
            "checkpoint_id": str(checkpoint_id),
            "outcome": outcome,
            "detail": dict(detail),
        }
        raw = _canonical_json(normalized)
        digest = _sha256(raw)
        receipt_id = "recovery_" + _sha256(f"{checkpoint_id}:{key}")[:32]
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM recovery_receipts_v0583 WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                if existing is not None:
                    if existing["receipt_hash"] != digest:
                        raise ResilienceStoreError(
                            "recovery receipt key reused for different content"
                        )
                    connection.rollback()
                    return self._receipt(existing), True
                connection.execute(
                    "INSERT INTO recovery_receipts_v0583(receipt_id,checkpoint_id,"
                    "outcome,receipt_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?)",
                    (receipt_id, checkpoint_id, outcome, raw, digest, created_at),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ResilienceStoreError(str(exc)) from exc
            except Exception:
                connection.rollback()
                raise
        return {
            "receipt_id": receipt_id,
            **normalized,
            "receipt_hash": digest,
            "created_at": created_at,
        }, False

    def integrity_audit(self) -> dict[str, Any]:
        errors: list[str] = []
        with self._connection() as connection:
            events = connection.execute(
                "SELECT e.run_id,e.stream_revision,e.payload_json,e.envelope_json,"
                "i.payload_hash,i.envelope_hash FROM events e "
                "LEFT JOIN event_integrity i USING(event_id) "
                "ORDER BY e.run_id,e.stream_revision"
            ).fetchall()
            projections = connection.execute(
                "SELECT name,entity_id,state_json,state_hash FROM projections "
                "ORDER BY name,entity_id"
            ).fetchall()
        expected_revision: dict[str, int] = {}
        for row in events:
            run_id = str(row["run_id"])
            expected = expected_revision.get(run_id, 0) + 1
            actual = int(row["stream_revision"])
            if actual != expected:
                errors.append(
                    f"event_revision_gap:{run_id}:expected_{expected}:got_{actual}"
                )
            expected_revision[run_id] = actual
            if row["payload_hash"] != _sha256(str(row["payload_json"])):
                errors.append(f"event_payload_hash:{run_id}:{actual}")
            if row["envelope_hash"] != _sha256(str(row["envelope_json"])):
                errors.append(f"event_envelope_hash:{run_id}:{actual}")
        for row in projections:
            if row["state_hash"] != _sha256(str(row["state_json"])):
                errors.append(
                    f"projection_hash:{row['name']}:{row['entity_id']}"
                )
        return {
            "healthy": not errors,
            "events_checked": len(events),
            "event_integrity_coverage": (
                round(
                    sum(
                        row["payload_hash"] is not None
                        and row["envelope_hash"] is not None
                        for row in events
                    ) / len(events),
                    6,
                )
                if events
                else 1.0
            ),
            "projections_checked": len(projections),
            "streams_checked": len(expected_revision),
            "errors": errors[:100],
        }

    def list_campaigns(
        self, *, project_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        project_id = normalize_project_id(project_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM resilience_campaigns WHERE project_id=? "
                "ORDER BY created_at DESC,campaign_id DESC LIMIT ?",
                (project_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._campaign(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "resilience_campaigns",
                    "resilience_observations",
                    "recovery_checkpoints_v0583",
                    "recovery_receipts_v0583",
                )
            }

    @staticmethod
    def _campaign(row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["spec_json"])
        return {
            "campaign_id": row["campaign_id"],
            **value,
            "spec_hash": row["spec_hash"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _observation(row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["observation_json"])
        return {
            "observation_id": row["observation_id"],
            **value,
            "observation_hash": row["observation_hash"],
            "observed_at": row["observed_at"],
        }

    @staticmethod
    def _checkpoint(row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["plan_json"])
        return {
            "checkpoint_id": row["checkpoint_id"],
            **value,
            "plan_hash": row["plan_hash"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, Any]:
        value = json.loads(row["receipt_json"])
        return {
            "receipt_id": row["receipt_id"],
            **value,
            "receipt_hash": row["receipt_hash"],
            "created_at": row["created_at"],
        }


__all__ = ["ResilienceStore", "ResilienceStoreError"]
