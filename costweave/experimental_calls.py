"""Hard-budget persistence for v0.5.6.3 experimental external calls."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contracts_v4 import utc_now
from .evaluation_protocol import canonical_json


EXPERIMENTAL_CALL_SCHEMA_VERSION = "v0.5.6.3"
EXPERIMENTAL_CALL_POLICY_REVISION = "external-call-policy-v1"
USAGE_ONLY_POLICY_REVISION = "external-call-usage-only-v1"
REAL_UNVALIDATED_EVIDENCE = "E2-real-unvalidated"


class ExperimentalCallError(RuntimeError):
    """Base error for guarded external calls."""


class ExternalCallsDisabledError(ExperimentalCallError):
    """Raised before network I/O when the explicit switch is off."""


class ExternalBudgetExceededError(ExperimentalCallError):
    """Raised before network I/O when a hard limit cannot be reserved."""


class ExternalIdempotencyError(ExperimentalCallError):
    """Raised when an idempotency key is reused for different content."""


class ExternalCallInProgressError(ExperimentalCallError):
    """Raised when the same logical call already owns a reservation."""


@dataclass(frozen=True, slots=True)
class ExperimentBudget:
    experiment_id: str
    total_cost_limit_usd: float
    provider_cost_limits_usd: Mapping[str, float]
    max_logical_calls: int
    max_attempts_per_call: int
    purpose: str = "directional_real_evaluation"
    created_at: str = ""
    expires_at: str | None = None
    revision: str = EXPERIMENTAL_CALL_POLICY_REVISION
    accounting_mode: str = "priced"

    def __post_init__(self) -> None:
        if self.accounting_mode not in {"priced", "usage_only"}:
            raise ExperimentalCallError("invalid accounting mode")
        if self.accounting_mode == "usage_only":
            if self.total_cost_limit_usd != 0 or any(self.provider_cost_limits_usd.values()):
                raise ExperimentalCallError("usage-only budgets must not claim monetary limits")
            object.__setattr__(self, "revision", USAGE_ONLY_POLICY_REVISION)
        elif self.revision == USAGE_ONLY_POLICY_REVISION:
            raise ExperimentalCallError("usage-only revision requires explicit accounting mode")
        if not self.experiment_id.startswith("experiment_"):
            raise ExperimentalCallError("experiment id is invalid")
        if (
            not math.isfinite(float(self.total_cost_limit_usd))
            or (self.total_cost_limit_usd <= 0 and self.accounting_mode != "usage_only")
        ):
            raise ExperimentalCallError("experiment budget must be positive")
        if not self.provider_cost_limits_usd:
            raise ExperimentalCallError("provider budgets are required")
        if any(
            not key
            or not math.isfinite(float(value))
            or float(value) < 0
            for key, value in self.provider_cost_limits_usd.items()
        ):
            raise ExperimentalCallError("provider budget is invalid")
        if any(
            float(value) > self.total_cost_limit_usd + 1e-9
            for value in self.provider_cost_limits_usd.values()
        ):
            raise ExperimentalCallError(
                "a provider limit cannot exceed the experiment total"
            )
        if self.max_logical_calls <= 0 or self.max_attempts_per_call <= 0:
            raise ExperimentalCallError("call and attempt limits are required")
        if self.created_at:
            _parse_time(self.created_at)
        if self.expires_at:
            _parse_time(self.expires_at)

    @property
    def timestamp(self) -> str:
        return self.created_at or utc_now()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.accounting_mode == "priced":
            result.pop("accounting_mode")  # Preserve existing registration hashes.
        result["provider_cost_limits_usd"] = {
            key: float(value)
            for key, value in sorted(
                self.provider_cost_limits_usd.items()
            )
        }
        return result


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CallReservation:
    external_call_id: str
    experiment_id: str
    idempotency_key: str
    request_hash: str
    status: str
    reserved_cost_usd: float
    replayed: bool
    result: Mapping[str, Any] | None = None
    telemetry: Mapping[str, Any] | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalAttempt:
    ordinal: int
    status: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    actual_cost_usd: float = 0.0
    request_latency_ms: int = 0
    queue_latency_ms: int = 0
    status_code: int | None = None
    rate_limited: bool = False
    cancelled: bool = False
    error_code: str | None = None
    response_hash: str | None = None
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if self.ordinal <= 0:
            raise ExperimentalCallError("attempt ordinal must be positive")
        if self.status not in {
            "completed",
            "failed",
            "rate_limited",
            "cancelled",
        }:
            raise ExperimentalCallError("attempt status is invalid")
        numeric = (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.request_latency_ms,
            self.queue_latency_ms,
        )
        if (
            any(value < 0 for value in numeric)
            or not math.isfinite(float(self.actual_cost_usd))
            or self.actual_cost_usd < 0
        ):
            raise ExperimentalCallError("attempt measurements cannot be negative")

    @property
    def timestamp(self) -> str:
        return self.recorded_at or utc_now()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExperimentalCallStore:
    """Atomic experiment, provider and retry budget owner."""

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
    def _hash(value: Any) -> str:
        return hashlib.sha256(
            canonical_json(value).encode("utf-8")
        ).hexdigest()

    def register_experiment(self, budget: ExperimentBudget) -> bool:
        config = budget.to_dict()
        config_hash = self._hash(config)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT config_hash FROM external_experiments "
                "WHERE experiment_id=?",
                (budget.experiment_id,),
            ).fetchone()
            if existing is not None:
                if existing["config_hash"] != config_hash:
                    raise ExternalIdempotencyError(
                        "experiment id was reused for a different budget"
                    )
                return False
            connection.execute(
                "INSERT INTO external_experiments("
                "experiment_id,revision,status,purpose,total_cost_limit,"
                "provider_limits_json,max_logical_calls,"
                "max_attempts_per_call,config_hash,created_at,expires_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    budget.experiment_id,
                    budget.revision,
                    "active",
                    budget.purpose,
                    budget.total_cost_limit_usd,
                    canonical_json(
                        dict(budget.provider_cost_limits_usd)
                    ),
                    budget.max_logical_calls,
                    budget.max_attempts_per_call,
                    config_hash,
                    budget.timestamp,
                    budget.expires_at,
                ),
            )
        return True

    @staticmethod
    def _row_reservation(row: sqlite3.Row, replayed: bool) -> CallReservation:
        return CallReservation(
            external_call_id=str(row["external_call_id"]),
            experiment_id=str(row["experiment_id"]),
            idempotency_key=str(row["idempotency_key"]),
            request_hash=str(row["request_hash"]),
            status=str(row["status"]),
            reserved_cost_usd=float(row["reserved_cost"]),
            replayed=replayed,
            result=(
                json.loads(row["result_json"])
                if row["result_json"]
                else None
            ),
            telemetry=(
                json.loads(row["telemetry_json"])
                if row["telemetry_json"]
                else None
            ),
            error_code=(str(row["error_code"]) if row["error_code"] else None),
        )

    def reserve(
        self,
        *,
        experiment_id: str,
        run_id: str,
        task_id: str,
        deployment_id: str,
        provider: str,
        model_id: str,
        adapter_revision: str,
        reasoning_effort: str,
        idempotency_key: str,
        request_fingerprint: Mapping[str, Any],
        worst_case_cost_per_attempt_usd: float,
        max_attempts: int,
        accounting_mode: str = "priced",
    ) -> CallReservation:
        if not idempotency_key.strip() or len(idempotency_key) > 240:
            raise ExternalIdempotencyError("bounded idempotency key is required")
        if (
            not math.isfinite(float(worst_case_cost_per_attempt_usd))
            or worst_case_cost_per_attempt_usd < 0
            or max_attempts <= 0
        ):
            raise ExternalBudgetExceededError("invalid call reservation")
        request_hash = self._hash(request_fingerprint)
        reserved_cost = round(
            worst_case_cost_per_attempt_usd * max_attempts, 10
        )
        call_id = "external_call_" + self._hash(
            {
                "experiment_id": experiment_id,
                "idempotency_key": idempotency_key,
            }
        )[:32]
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                policy_row = connection.execute("SELECT revision FROM external_experiments WHERE experiment_id=?", (experiment_id,)).fetchone()
                if policy_row is not None:
                    expected_mode = "usage_only" if policy_row["revision"] == USAGE_ONLY_POLICY_REVISION else "priced"
                    if accounting_mode != expected_mode:
                        raise ExternalBudgetExceededError("adapter and experiment accounting modes disagree")
                existing = connection.execute(
                    "SELECT * FROM external_calls WHERE experiment_id=? "
                    "AND idempotency_key=?",
                    (experiment_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise ExternalIdempotencyError(
                            "idempotency key was reused for another request"
                        )
                    connection.execute("COMMIT")
                    return self._row_reservation(existing, True)

                experiment = connection.execute(
                    "SELECT * FROM external_experiments WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchone()
                if experiment is None:
                    raise ExternalBudgetExceededError(
                        "experiment budget is not registered"
                    )
                mode = "usage_only" if experiment["revision"] == USAGE_ONLY_POLICY_REVISION else "priced"
                if accounting_mode != mode:
                    raise ExternalBudgetExceededError("adapter and experiment accounting modes disagree")
                if mode == "usage_only" and (reserved_cost != 0 or max_attempts != 1):
                    raise ExternalBudgetExceededError("usage-only calls require zero price placeholders and no retries")
                if experiment["status"] != "active":
                    raise ExternalBudgetExceededError(
                        "experiment is not active"
                    )
                if experiment["expires_at"] and _parse_time(
                    experiment["expires_at"]
                ) <= datetime.now(UTC):
                    raise ExternalBudgetExceededError("experiment has expired")
                if max_attempts > int(experiment["max_attempts_per_call"]):
                    raise ExternalBudgetExceededError(
                        "call retry limit exceeds experiment policy"
                    )
                call_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM external_calls "
                        "WHERE experiment_id=?",
                        (experiment_id,),
                    ).fetchone()[0]
                )
                if call_count >= int(experiment["max_logical_calls"]):
                    raise ExternalBudgetExceededError(
                        "experiment logical-call limit reached"
                    )
                provider_limits = json.loads(
                    experiment["provider_limits_json"]
                )
                if provider not in provider_limits:
                    raise ExternalBudgetExceededError(
                        "provider has no explicit experiment budget"
                    )
                rows = connection.execute(
                    "SELECT provider,status,reserved_cost,actual_cost "
                    "FROM external_calls WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchall()
                total_committed = sum(
                    float(
                        row["reserved_cost"]
                        if row["status"] == "reserved"
                        else row["actual_cost"]
                    )
                    for row in rows
                )
                provider_committed = sum(
                    float(
                        row["reserved_cost"]
                        if row["status"] == "reserved"
                        else row["actual_cost"]
                    )
                    for row in rows
                    if row["provider"] == provider
                )
                if (
                    total_committed + reserved_cost
                    > float(experiment["total_cost_limit"]) + 1e-10
                ):
                    raise ExternalBudgetExceededError(
                        "experiment hard budget would be exceeded"
                    )
                if (
                    provider_committed + reserved_cost
                    > float(provider_limits[provider]) + 1e-10
                ):
                    raise ExternalBudgetExceededError(
                        "provider hard budget would be exceeded"
                    )
                connection.execute(
                    "INSERT INTO external_calls("
                    "external_call_id,experiment_id,run_id,task_id,"
                    "deployment_id,provider,model_id,adapter_revision,"
                    "reasoning_effort,idempotency_key,request_hash,status,"
                    "reserved_cost,actual_cost,attempt_count,created_at,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        call_id,
                        experiment_id,
                        run_id,
                        task_id,
                        deployment_id,
                        provider,
                        model_id,
                        adapter_revision,
                        reasoning_effort,
                        idempotency_key,
                        request_hash,
                        "reserved",
                        reserved_cost,
                        0.0,
                        0,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM external_calls WHERE external_call_id=?",
                    (call_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._row_reservation(row, False)

    def record_attempt(
        self, external_call_id: str, attempt: ExternalAttempt
    ) -> bool:
        payload = attempt.to_dict()
        recorded_at = attempt.timestamp
        attempt_id = "external_attempt_" + self._hash(
            {"call": external_call_id, "ordinal": attempt.ordinal}
        )[:32]
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM external_call_attempts "
                    "WHERE external_call_id=? AND ordinal=?",
                    (external_call_id, attempt.ordinal),
                ).fetchone()
                if existing is not None:
                    stored = {
                        "ordinal": int(existing["ordinal"]),
                        "status": existing["status"],
                        "input_tokens": int(existing["input_tokens"]),
                        "output_tokens": int(existing["output_tokens"]),
                        "cached_input_tokens": int(
                            existing["cached_input_tokens"]
                        ),
                        "actual_cost_usd": float(existing["actual_cost"]),
                        "request_latency_ms": int(
                            existing["request_latency_ms"]
                        ),
                        "queue_latency_ms": int(existing["queue_latency_ms"]),
                        "status_code": existing["status_code"],
                        "rate_limited": bool(existing["rate_limited"]),
                        "cancelled": bool(existing["cancelled"]),
                        "error_code": existing["error_code"],
                        "response_hash": existing["response_hash"],
                        "recorded_at": existing["recorded_at"],
                    }
                    expected = dict(payload)
                    # An omitted timestamp means "use the first persisted
                    # timestamp".  This keeps a retried write idempotent
                    # without pretending two different explicit timestamps
                    # are the same attempt.
                    expected["recorded_at"] = (
                        attempt.recorded_at or stored["recorded_at"]
                    )
                    if canonical_json(stored) != canonical_json(expected):
                        raise ExternalIdempotencyError(
                            "attempt ordinal was reused for different telemetry"
                        )
                    connection.execute("COMMIT")
                    return False
                call = connection.execute(
                    "SELECT status FROM external_calls "
                    "WHERE external_call_id=?",
                    (external_call_id,),
                ).fetchone()
                if call is None or call["status"] != "reserved":
                    raise ExperimentalCallError(
                        "attempt target is not an active reservation"
                    )
                connection.execute(
                    "INSERT INTO external_call_attempts("
                    "attempt_id,external_call_id,ordinal,status,status_code,"
                    "input_tokens,output_tokens,cached_input_tokens,"
                    "actual_cost,request_latency_ms,queue_latency_ms,"
                    "rate_limited,cancelled,error_code,response_hash,"
                    "recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        external_call_id,
                        attempt.ordinal,
                        attempt.status,
                        attempt.status_code,
                        attempt.input_tokens,
                        attempt.output_tokens,
                        attempt.cached_input_tokens,
                        attempt.actual_cost_usd,
                        attempt.request_latency_ms,
                        attempt.queue_latency_ms,
                        int(attempt.rate_limited),
                        int(attempt.cancelled),
                        attempt.error_code,
                        attempt.response_hash,
                        recorded_at,
                    ),
                )
                connection.execute(
                    "UPDATE external_calls SET attempt_count=("
                    "SELECT COUNT(*) FROM external_call_attempts "
                    "WHERE external_call_id=?),actual_cost=("
                    "SELECT COALESCE(SUM(actual_cost),0) "
                    "FROM external_call_attempts WHERE external_call_id=?),"
                    "updated_at=? WHERE external_call_id=?",
                    (
                        external_call_id,
                        external_call_id,
                        recorded_at,
                        external_call_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def finalize(
        self,
        external_call_id: str,
        *,
        status: str,
        result: Mapping[str, Any] | None,
        telemetry: Mapping[str, Any],
        error_code: str | None = None,
    ) -> bool:
        if status not in {"completed", "failed", "cancelled"}:
            raise ExperimentalCallError("terminal call status is invalid")
        result_raw = canonical_json(result) if result is not None else None
        telemetry_raw = canonical_json(telemetry)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                call = connection.execute(
                    "SELECT * FROM external_calls WHERE external_call_id=?",
                    (external_call_id,),
                ).fetchone()
                if call is None:
                    raise ExperimentalCallError("external call does not exist")
                if call["status"] != "reserved":
                    same = (
                        call["status"] == status
                        and call["result_json"] == result_raw
                        and call["telemetry_json"] == telemetry_raw
                        and call["error_code"] == error_code
                    )
                    if not same:
                        raise ExternalIdempotencyError(
                            "terminal call receipt is immutable"
                        )
                    connection.execute("COMMIT")
                    return False
                actual = float(call["actual_cost"])
                reserved = float(call["reserved_cost"])
                if actual > reserved + 1e-9:
                    connection.execute(
                        "UPDATE external_experiments SET status='frozen' "
                        "WHERE experiment_id=?",
                        (call["experiment_id"],),
                    )
                    connection.execute(
                        "UPDATE external_calls SET status='failed',"
                        "error_code='actual_cost_exceeded_reservation',"
                        "updated_at=? WHERE external_call_id=?",
                        (utc_now(), external_call_id),
                    )
                    connection.execute("COMMIT")
                    raise ExternalBudgetExceededError(
                        "actual cost exceeded the reserved worst case"
                    )
                connection.execute(
                    "UPDATE external_calls SET status=?,result_json=?,"
                    "telemetry_json=?,error_code=?,updated_at=? "
                    "WHERE external_call_id=?",
                    (
                        status,
                        result_raw,
                        telemetry_raw,
                        error_code,
                        utc_now(),
                        external_call_id,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return True

    def call(self, external_call_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM external_calls WHERE external_call_id=?",
                (external_call_id,),
            ).fetchone()
            if row is None:
                return None
            attempts = connection.execute(
                "SELECT * FROM external_call_attempts "
                "WHERE external_call_id=? ORDER BY ordinal",
                (external_call_id,),
            ).fetchall()
            policy = connection.execute("SELECT revision FROM external_experiments WHERE experiment_id=?", (row["experiment_id"],)).fetchone()
        item = dict(row)
        item["result"] = (
            json.loads(item.pop("result_json"))
            if item["result_json"]
            else None
        )
        item["telemetry"] = (
            json.loads(item.pop("telemetry_json"))
            if item["telemetry_json"]
            else None
        )
        item["attempts"] = [dict(attempt) for attempt in attempts]
        if policy["revision"] == USAGE_ONLY_POLICY_REVISION:
            item.update(accounting_mode="usage_only", actual_cost=None, reserved_cost=None)
            for attempt in item["attempts"]:
                attempt["actual_cost"] = None
        return item

    def experiment_summary(self, experiment_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            experiment = connection.execute(
                "SELECT * FROM external_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if experiment is None:
                raise ExperimentalCallError("experiment does not exist")
            rows = connection.execute(
                "SELECT provider,status,reserved_cost,actual_cost "
                "FROM external_calls WHERE experiment_id=?",
                (experiment_id,),
            ).fetchall()
        provider = {}
        for row in rows:
            bucket = provider.setdefault(
                str(row["provider"]),
                {"calls": 0, "actual_cost_usd": 0.0},
            )
            bucket["calls"] += 1
            bucket["actual_cost_usd"] += float(row["actual_cost"])
        summary = {
            "experiment_id": experiment_id,
            "status": experiment["status"],
            "logical_calls": len(rows),
            "actual_cost_usd": round(
                sum(float(row["actual_cost"]) for row in rows), 10
            ),
            "active_reserved_cost_usd": round(
                sum(
                    float(row["reserved_cost"])
                    for row in rows
                    if row["status"] == "reserved"
                ),
                10,
            ),
            "providers": provider,
        }
        if experiment["revision"] == USAGE_ONLY_POLICY_REVISION:
            summary.update(accounting_mode="usage_only", actual_cost_usd=None, active_reserved_cost_usd=None)
            for bucket in provider.values():
                bucket["actual_cost_usd"] = None
        return summary

    def complete_experiment(self, experiment_id: str) -> bool:
        """Close a normally finished experiment after every call is terminal.

        Completion is idempotent.  A frozen experiment remains immutable, and
        an active reservation prevents closure so reports cannot claim a clean
        finish while a provider call is still outstanding.
        """

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                experiment = connection.execute(
                    "SELECT status FROM external_experiments WHERE experiment_id=?",
                    (experiment_id,),
                ).fetchone()
                if experiment is None:
                    raise ExperimentalCallError("experiment does not exist")
                status = str(experiment["status"])
                if status == "completed":
                    connection.execute("COMMIT")
                    return False
                if status != "active":
                    raise ExperimentalCallError(
                        f"experiment in {status} state cannot be completed"
                    )
                active = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM external_calls "
                        "WHERE experiment_id=? AND status='reserved'",
                        (experiment_id,),
                    ).fetchone()[0]
                )
                if active:
                    raise ExperimentalCallError(
                        "experiment has active call reservations"
                    )
                connection.execute(
                    "UPDATE external_experiments SET status='completed' "
                    "WHERE experiment_id=? AND status='active'",
                    (experiment_id,),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return True

    def budget_headroom(
        self, experiment_id: str, *, provider: str,
        pending_reservations: Mapping[str, tuple[str, float]] | None = None,
    ) -> dict[str, Any]:
        """Return a conservative read-only budget snapshot for route choice.

        This snapshot is advisory: ``reserve`` remains the atomic authority and
        repeats every check, so a concurrent call cannot overspend based on a
        stale fallback decision. A caller dispatching concurrent work may
        include the primary tasks it already admitted locally. Count their
        allowance only until the corresponding reservation appears in this read.
        """

        with self._connection() as connection:
            experiment = connection.execute(
                "SELECT * FROM external_experiments WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if experiment is None:
                raise ExperimentalCallError("experiment does not exist")
            provider_limits = json.loads(experiment["provider_limits_json"])
            if provider not in provider_limits:
                raise ExternalBudgetExceededError(
                    "provider has no explicit experiment budget"
                )
            rows = connection.execute(
                "SELECT task_id,provider,status,reserved_cost,actual_cost "
                "FROM external_calls WHERE experiment_id=?",
                (experiment_id,),
            ).fetchall()
        committed = lambda row: float(
            row["reserved_cost"]
            if row["status"] == "reserved"
            else row["actual_cost"]
        )
        total_committed = sum(committed(row) for row in rows)
        provider_committed = sum(
            committed(row) for row in rows if row["provider"] == provider
        )
        registered = {row["task_id"] for row in rows}
        outstanding = [
            (owner, max(0.0, allowance))
            for task, (owner, allowance) in (pending_reservations or {}).items()
            if task not in registered
        ]
        total_committed += sum(allowance for _, allowance in outstanding)
        provider_committed += sum(allowance for owner, allowance in outstanding if owner == provider)
        total_limit = float(experiment["total_cost_limit"])
        provider_limit = float(provider_limits[provider])
        remaining_calls = max(0, int(experiment["max_logical_calls"]) - len(rows) - len(outstanding))
        if experiment["revision"] == USAGE_ONLY_POLICY_REVISION:
            return {"experiment_id": experiment_id, "provider": provider,
                    "experiment_status": str(experiment["status"]), "accounting_mode": "usage_only",
                    "total_remaining_usd": None, "provider_remaining_usd": None,
                    "logical_calls_remaining": remaining_calls,
                    "authoritative_at_reserve": True}
        return {
            "experiment_id": experiment_id,
            "provider": provider,
            "experiment_status": str(experiment["status"]),
            "total_remaining_usd": round(
                max(0.0, total_limit - total_committed), 10
            ),
            "provider_remaining_usd": round(
                max(0.0, provider_limit - provider_committed), 10
            ),
            "logical_calls_remaining": remaining_calls,
            "authoritative_at_reserve": True,
        }

    def counts(
        self, *, experiment_ids: Iterable[str] | None = None
    ) -> dict[str, int]:
        scoped_ids = (
            None
            if experiment_ids is None
            else tuple(sorted({str(item) for item in experiment_ids if str(item)}))
        )
        with self._connection() as connection:
            if scoped_ids is not None:
                if not scoped_ids:
                    return {
                        "external_experiments": 0,
                        "external_calls": 0,
                        "external_call_attempts": 0,
                        "shadow_decisions": 0,
                        "shadow_comparisons": 0,
                    }
                placeholders = ",".join("?" for _ in scoped_ids)
                return {
                    "external_experiments": int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM external_experiments WHERE experiment_id IN ({placeholders})",
                            scoped_ids,
                        ).fetchone()[0]
                    ),
                    "external_calls": int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM external_calls WHERE experiment_id IN ({placeholders})",
                            scoped_ids,
                        ).fetchone()[0]
                    ),
                    "external_call_attempts": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM external_call_attempts a "
                            "JOIN external_calls c ON c.external_call_id=a.external_call_id "
                            f"WHERE c.experiment_id IN ({placeholders})",
                            scoped_ids,
                        ).fetchone()[0]
                    ),
                    "shadow_decisions": int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM shadow_decisions WHERE experiment_id IN ({placeholders})",
                            scoped_ids,
                        ).fetchone()[0]
                    ),
                    "shadow_comparisons": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM shadow_comparisons c "
                            "JOIN shadow_decisions d ON d.shadow_decision_id=c.shadow_decision_id "
                            f"WHERE d.experiment_id IN ({placeholders})",
                            scoped_ids,
                        ).fetchone()[0]
                    ),
                }
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "external_experiments",
                    "external_calls",
                    "external_call_attempts",
                    "shadow_decisions",
                    "shadow_comparisons",
                )
            }


__all__ = [
    "EXPERIMENTAL_CALL_POLICY_REVISION",
    "EXPERIMENTAL_CALL_SCHEMA_VERSION",
    "REAL_UNVALIDATED_EVIDENCE",
    "CallReservation",
    "ExperimentBudget",
    "ExperimentalCallError",
    "ExperimentalCallStore",
    "ExternalAttempt",
    "ExternalBudgetExceededError",
    "ExternalCallInProgressError",
    "ExternalCallsDisabledError",
    "ExternalIdempotencyError",
]
