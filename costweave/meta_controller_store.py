"""Immutable v0.6.1 MetaController and planning-budget persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .event_store import EventStore
from .intelligence.common import canonical_json, stable_id, validate_timestamp
from .intelligence.meta_controller import (
    BudgetAmountV1,
    MetaDecisionV1,
    PlanningBudgetV1,
)
from .security_boundary import normalize_project_id


class MetaControllerStoreError(RuntimeError):
    """Raised when an immutable MetaController or budget operation fails."""


def _sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _signed_zero() -> dict[str, float | int]:
    return {
        "money_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "wall_clock_ms": 0,
        "planner_calls": 0,
        "judge_calls": 0,
        "probe_calls": 0,
    }


def _signed_amount(amount: BudgetAmountV1, sign: int = 1) -> dict[str, float | int]:
    if sign not in {-1, 1}:
        raise ValueError("sign must be -1 or 1")
    return {key: (value * sign) for key, value in amount.to_dict().items()}


def _payload(value: Mapping[str, Any], name: str) -> tuple[str, str]:
    try:
        raw = canonical_json(dict(value))
    except (TypeError, ValueError) as exc:
        raise MetaControllerStoreError(f"{name} must contain finite JSON values") from exc
    return raw, _sha256(raw)


class MetaControllerStore:
    """Use the existing EventStore database; never create a second runtime."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._runtime = EventStore(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
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

    @staticmethod
    def _project(value: str) -> str:
        return normalize_project_id(value)

    @staticmethod
    def _key(value: str, name: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 200:
            raise ValueError(f"{name} must contain 8-200 characters")
        return value.strip()

    @staticmethod
    def _row_decision(row: sqlite3.Row) -> MetaDecisionV1:
        raw = str(row["decision_json"])
        if _sha256(raw) != row["decision_hash"]:
            raise MetaControllerStoreError("persisted MetaDecision hash mismatch")
        return MetaDecisionV1.from_dict(json.loads(raw))

    @staticmethod
    def _row_budget(row: sqlite3.Row) -> PlanningBudgetV1:
        raw = str(row["budget_json"])
        if _sha256(raw) != row["budget_hash"]:
            raise MetaControllerStoreError("persisted budget hash mismatch")
        return PlanningBudgetV1.from_dict(json.loads(raw))

    def record_decision(
        self,
        decision: MetaDecisionV1,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> bool:
        if not isinstance(decision, MetaDecisionV1):
            raise TypeError("decision must be MetaDecisionV1")
        key = None if idempotency_key is None else self._key(idempotency_key, "idempotency_key")
        if (key is None) != (request_hash is None):
            raise ValueError("idempotency_key and request_hash must be supplied together")
        if request_hash is not None:
            if not isinstance(request_hash, str) or len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
                raise ValueError("request_hash must be a lowercase SHA-256 digest")
        raw, digest = _payload(decision.to_dict(), "decision")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if key is not None:
                    idempotency = connection.execute(
                        "SELECT request_hash,decision_id FROM meta_decision_idempotency_v061 WHERE project_id=? AND idempotency_key=?",
                        (decision.project_id, key),
                    ).fetchone()
                    if idempotency is not None:
                        if idempotency["request_hash"] != request_hash:
                            raise MetaControllerStoreError("idempotency key was reused with different request content")
                        if idempotency["decision_id"] != decision.decision_id:
                            raise MetaControllerStoreError("idempotency key points to a different decision")
                        connection.execute("COMMIT")
                        return False
                existing = connection.execute(
                    "SELECT * FROM meta_decisions_v061 WHERE project_id=? AND decision_id=?",
                    (decision.project_id, decision.decision_id),
                ).fetchone()
                if existing is not None:
                    if existing["decision_hash"] != digest or existing["decision_json"] != raw:
                        raise MetaControllerStoreError("decision id was reused with different content")
                    inserted = False
                else:
                    connection.execute(
                        "INSERT INTO meta_decisions_v061(project_id,decision_id,case_id,belief_hash,action,decision_json,decision_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (decision.project_id, decision.decision_id, decision.case_id, decision.belief_hash, decision.action, raw, digest, decision.created_at),
                    )
                    inserted = True
                if key is not None:
                    connection.execute(
                        "INSERT INTO meta_decision_idempotency_v061(project_id,idempotency_key,request_hash,decision_id,created_at) VALUES(?,?,?,?,?)",
                        (decision.project_id, key, request_hash, decision.decision_id, decision.created_at),
                    )
                connection.execute("COMMIT")
                return inserted
            except MetaControllerStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise MetaControllerStoreError(str(exc)) from exc

    def decision_for_idempotency(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> MetaDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(idempotency_key, "idempotency_key")
        if not isinstance(request_hash, str) or len(request_hash) != 64 or any(char not in "0123456789abcdef" for char in request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_hash,decision_id FROM meta_decision_idempotency_v061 WHERE project_id=? AND idempotency_key=?",
                (project, key),
            ).fetchone()
            if row is None:
                return None
            if row["request_hash"] != request_hash:
                raise MetaControllerStoreError("idempotency key was reused with different request content")
            decision = connection.execute(
                "SELECT * FROM meta_decisions_v061 WHERE project_id=? AND decision_id=?",
                (project, row["decision_id"]),
            ).fetchone()
        if decision is None:
            raise MetaControllerStoreError("idempotency record points to missing decision")
        return self._row_decision(decision)

    def decision(self, *, project_id: str, decision_id: str) -> MetaDecisionV1 | None:
        project = self._project(project_id)
        key = self._key(decision_id, "decision_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM meta_decisions_v061 WHERE project_id=? AND decision_id=?",
                (project, key),
            ).fetchone()
        return self._row_decision(row) if row is not None else None

    def decisions(self, *, project_id: str, limit: int = 25) -> list[MetaDecisionV1]:
        project = self._project(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM meta_decisions_v061 WHERE project_id=? ORDER BY created_at DESC,decision_id DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        return [self._row_decision(row) for row in rows]

    def create_budget(self, budget: PlanningBudgetV1) -> bool:
        if not isinstance(budget, PlanningBudgetV1):
            raise TypeError("budget must be PlanningBudgetV1")
        project = self._project(budget.project_id)
        if project != budget.project_id:
            raise MetaControllerStoreError("budget project_id must be normalized")
        raw, digest = _payload(budget.to_dict(), "budget")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM planning_budget_accounts_v061 WHERE project_id=? AND budget_id=?",
                    (project, budget.budget_id),
                ).fetchone()
                if existing is not None:
                    if existing["budget_hash"] != digest or existing["budget_json"] != raw:
                        raise MetaControllerStoreError("budget id was reused with different content")
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "INSERT INTO planning_budget_accounts_v061(project_id,budget_id,budget_json,budget_hash,created_at) VALUES(?,?,?,?,?)",
                    (project, budget.budget_id, raw, digest, budget.created_at),
                )
                connection.execute("COMMIT")
                return True
            except MetaControllerStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise MetaControllerStoreError(str(exc)) from exc

    def budget(self, *, project_id: str, budget_id: str) -> PlanningBudgetV1 | None:
        project = self._project(project_id)
        key = self._key(budget_id, "budget_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM planning_budget_accounts_v061 WHERE project_id=? AND budget_id=?",
                (project, key),
            ).fetchone()
        return self._row_budget(row) if row is not None else None

    @staticmethod
    def _amount_from_signed(value: Mapping[str, Any], name: str) -> BudgetAmountV1:
        normalized: dict[str, Any] = {}
        for key, raw in value.items():
            if key not in _signed_zero():
                raise MetaControllerStoreError(f"{name} contains unknown field")
            normalized[key] = raw
        # A signed delta is intentionally not accepted as a BudgetAmount.  It
        # is checked dimension-by-dimension by the accumulator instead.
        required = set(_signed_zero())
        if set(normalized) != required:
            raise MetaControllerStoreError(f"{name} has incomplete dimensions")
        return BudgetAmountV1(
            money_usd=abs(float(normalized["money_usd"])),
            input_tokens=abs(int(normalized["input_tokens"])),
            output_tokens=abs(int(normalized["output_tokens"])),
            wall_clock_ms=abs(int(normalized["wall_clock_ms"])),
            planner_calls=abs(int(normalized["planner_calls"])),
            judge_calls=abs(int(normalized["judge_calls"])),
            probe_calls=abs(int(normalized["probe_calls"])),
        )

    @staticmethod
    def _apply_delta(target: dict[str, float], delta: Mapping[str, Any]) -> None:
        for key in target:
            raw = delta.get(key)
            if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise MetaControllerStoreError("budget event delta must contain finite numbers")
            target[key] += float(raw)

    def _snapshot_locked(self, connection: sqlite3.Connection, project: str, budget_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM planning_budget_accounts_v061 WHERE project_id=? AND budget_id=?",
            (project, budget_id),
        ).fetchone()
        if row is None:
            raise MetaControllerStoreError("budget not found")
        budget = self._row_budget(row)
        reserved = {key: 0.0 for key in budget.limits.to_dict()}
        spent = {key: 0.0 for key in budget.limits.to_dict()}
        events = connection.execute(
            # Ledger semantics follow append order, not caller-provided wall
            # clock values. This keeps same-timestamp and skewed-clock
            # reserve/settlement pairs deterministic and fail-closed.
            "SELECT event_json,event_hash FROM planning_budget_events_v061 WHERE project_id=? AND budget_id=? ORDER BY rowid",
            (project, budget_id),
        ).fetchall()
        for event in events:
            raw = str(event["event_json"])
            if _sha256(raw) != event["event_hash"]:
                raise MetaControllerStoreError("persisted budget event hash mismatch")
            item = json.loads(raw)
            self._apply_delta(reserved, item["reserved_delta"])
            self._apply_delta(spent, item["spent_delta"])
        for name, value in {**reserved, **spent}.items():
            if value < -1e-9:
                raise MetaControllerStoreError(f"budget {name} became negative")
        limits = budget.limits.to_dict()
        available = {key: max(0.0, float(limits[key]) - reserved[key] - spent[key]) for key in limits}
        return {"budget": budget.to_dict(), "reserved": reserved, "spent": spent, "available": available, "event_count": len(events)}

    def budget_snapshot(self, *, project_id: str, budget_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        key = self._key(budget_id, "budget_id")
        with self._connection() as connection:
            return self._snapshot_locked(connection, project, key)

    def _reservation_events(self, connection: sqlite3.Connection, project: str, budget_id: str, reservation_id: str) -> dict[str, sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM planning_budget_events_v061 WHERE project_id=? AND budget_id=? AND reservation_id=?",
            (project, budget_id, reservation_id),
        ).fetchall()
        result: dict[str, sqlite3.Row] = {}
        for row in rows:
            raw = str(row["event_json"])
            if _sha256(raw) != row["event_hash"]:
                raise MetaControllerStoreError("persisted reservation event hash mismatch")
            result[str(row["event_type"])] = row
        return result

    def _budget_event(
        self,
        *,
        project_id: str,
        budget_id: str,
        reservation_id: str,
        event_type: str,
        amount: BudgetAmountV1,
        recorded_at: str | None,
    ) -> bool:
        project = self._project(project_id)
        budget_key = self._key(budget_id, "budget_id")
        reservation_key = self._key(reservation_id, "reservation_id")
        if event_type not in {"reserve", "release", "commit"}:
            raise ValueError("unsupported budget event type")
        if not isinstance(amount, BudgetAmountV1):
            raise TypeError("amount must be BudgetAmountV1")
        timestamp = validate_timestamp(recorded_at or utc_now(), "recorded_at")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                snapshot = self._snapshot_locked(connection, project, budget_key)
                events = self._reservation_events(connection, project, budget_key, reservation_key)
                if event_type in events:
                    existing_raw = json.loads(events[event_type]["event_json"])
                    if existing_raw["amount"] != amount.to_dict():
                        raise MetaControllerStoreError("reservation event was reused with different amount")
                    connection.execute("COMMIT")
                    return False
                if event_type == "reserve":
                    if events:
                        raise MetaControllerStoreError("reservation already has a terminal event")
                    limits = snapshot["budget"]["limits"]
                    projected = {key: float(snapshot["reserved"][key]) + float(snapshot["spent"][key]) + float(value) for key, value in amount.to_dict().items()}
                    if any(projected[key] > float(limits[key]) + 1e-9 for key in projected):
                        raise MetaControllerStoreError("planning budget exceeded")
                    reserved_delta = _signed_amount(amount, 1)
                    spent_delta = _signed_zero()
                else:
                    if "reserve" not in events or "release" in events or "commit" in events:
                        raise MetaControllerStoreError("reservation is not active")
                    reserved_raw = json.loads(events["reserve"]["event_json"])
                    reserved_amount = BudgetAmountV1.from_dict(reserved_raw["amount"])
                    if reserved_amount.to_dict() != amount.to_dict():
                        raise MetaControllerStoreError("settlement amount must equal reservation amount")
                    reserved_delta = _signed_amount(amount, -1)
                    spent_delta = _signed_amount(amount, 1) if event_type == "commit" else _signed_zero()
                event_payload = {"amount": amount.to_dict(), "reserved_delta": reserved_delta, "spent_delta": spent_delta}
                raw, digest = _payload(event_payload, "budget event")
                event_id = stable_id("budget_event", {"project_id": project, "budget_id": budget_key, "reservation_id": reservation_key, "event_type": event_type, "event_hash": digest})
                connection.execute(
                    "INSERT INTO planning_budget_events_v061(project_id,event_id,budget_id,reservation_id,event_type,event_json,event_hash,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (project, event_id, budget_key, reservation_key, event_type, raw, digest, timestamp),
                )
                connection.execute("COMMIT")
                return True
            except MetaControllerStoreError:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            except Exception as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise MetaControllerStoreError(str(exc)) from exc

    def reserve(self, *, project_id: str, budget_id: str, reservation_id: str, amount: BudgetAmountV1, recorded_at: str | None = None) -> bool:
        return self._budget_event(project_id=project_id, budget_id=budget_id, reservation_id=reservation_id, event_type="reserve", amount=amount, recorded_at=recorded_at)

    def release(self, *, project_id: str, budget_id: str, reservation_id: str, amount: BudgetAmountV1, recorded_at: str | None = None) -> bool:
        return self._budget_event(project_id=project_id, budget_id=budget_id, reservation_id=reservation_id, event_type="release", amount=amount, recorded_at=recorded_at)

    def commit(self, *, project_id: str, budget_id: str, reservation_id: str, amount: BudgetAmountV1, recorded_at: str | None = None) -> bool:
        return self._budget_event(project_id=project_id, budget_id=budget_id, reservation_id=reservation_id, event_type="commit", amount=amount, recorded_at=recorded_at)

    def counts(self, *, project_id: str | None = None) -> dict[str, int]:
        project = self._project(project_id) if project_id is not None else None
        with self._connection() as connection:
            suffix = " WHERE project_id=?" if project is not None else ""
            params = (project,) if project is not None else ()
            decisions = connection.execute("SELECT COUNT(*) FROM meta_decisions_v061" + suffix, params).fetchone()[0]
            idempotency = connection.execute("SELECT COUNT(*) FROM meta_decision_idempotency_v061" + suffix, params).fetchone()[0]
            accounts = connection.execute("SELECT COUNT(*) FROM planning_budget_accounts_v061" + suffix, params).fetchone()[0]
            events = connection.execute("SELECT COUNT(*) FROM planning_budget_events_v061" + suffix, params).fetchone()[0]
        return {
            "meta_decisions_v061": int(decisions),
            "meta_decision_idempotency_v061": int(idempotency),
            "planning_budget_accounts_v061": int(accounts),
            "planning_budget_events_v061": int(events),
        }


__all__ = ["MetaControllerStore", "MetaControllerStoreError"]
