"""Atomic resource and cost reservations with recoverable leases."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .admission import ResourceRequest
from .contracts_v4 import canonical_identifier, utc_now
from .resource_inventory import ResourcePool


class ReservationError(RuntimeError):
    pass


class CapacityUnavailable(ReservationError):
    pass


class BudgetUnavailable(ReservationError):
    pass


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    run_id: str
    task_id: str
    attempt: int
    pool_id: str
    amount: float
    replayed: bool = False


class ReservationManager:
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

    def register_pool(self, pool: ResourcePool) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO resource_pools(pool_id,provider,local,"
                "slots_total,cpu_cores_total,gpu_count_total,vram_mb_total,"
                "queue_limit,token_capacity,token_refill_per_second,state,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(pool_id) DO UPDATE SET provider=excluded.provider,"
                "local=excluded.local,slots_total=excluded.slots_total,"
                "cpu_cores_total=excluded.cpu_cores_total,"
                "gpu_count_total=excluded.gpu_count_total,"
                "vram_mb_total=excluded.vram_mb_total,"
                "queue_limit=excluded.queue_limit,"
                "token_capacity=excluded.token_capacity,"
                "token_refill_per_second=excluded.token_refill_per_second,"
                "state=excluded.state,updated_at=excluded.updated_at",
                (
                    pool.pool_id,
                    pool.provider,
                    int(pool.local),
                    pool.slots_total,
                    pool.cpu_cores_total,
                    pool.gpu_count_total,
                    pool.vram_mb_total,
                    pool.queue_limit,
                    pool.token_capacity,
                    pool.token_refill_per_second,
                    pool.state,
                    utc_now(),
                ),
            )
            used = connection.execute(
                "SELECT COALESCE(SUM(slots),0),"
                "COALESCE(SUM(cpu_cores),0),"
                "COALESCE(SUM(gpu_count),0),"
                "COALESCE(SUM(vram_mb),0),COUNT(*) "
                "FROM resource_reservations WHERE pool_id=? "
                "AND status='active'",
                (pool.pool_id,),
            ).fetchone()
            captured = utc_now()
            snapshot_id = "snapshot_" + hashlib.sha256(
                f"{pool.pool_id}:{captured}".encode("utf-8")
            ).hexdigest()[:32]
            connection.execute(
                "INSERT INTO resource_snapshots(snapshot_id,pool_id,"
                "slots_used,cpu_cores_used,gpu_count_used,vram_mb_used,"
                "queue_depth,captured_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    pool.pool_id,
                    int(used[0]),
                    float(used[1]),
                    int(used[2]),
                    int(used[3]),
                    int(used[4]),
                    captured,
                ),
            )

    def availability(self, pool_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            pool = connection.execute(
                "SELECT * FROM resource_pools WHERE pool_id=?", (pool_id,)
            ).fetchone()
            if pool is None:
                raise KeyError(pool_id)
            used = connection.execute(
                "SELECT COALESCE(SUM(slots),0) slots,"
                "COALESCE(SUM(cpu_cores),0) cpu,"
                "COALESCE(SUM(gpu_count),0) gpu,"
                "COALESCE(SUM(vram_mb),0) vram,"
                "COUNT(*) queue_depth FROM resource_reservations "
                "WHERE pool_id=? AND status='active'",
                (pool_id,),
            ).fetchone()
        return {
            "slots": int(pool["slots_total"] - used["slots"]),
            "cpu_cores": float(pool["cpu_cores_total"] - used["cpu"]),
            "gpu_count": int(pool["gpu_count_total"] - used["gpu"]),
            "vram_mb": int(pool["vram_mb_total"] - used["vram"]),
            "queue_depth": int(used["queue_depth"]),
        }

    def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        pool: ResourcePool,
        request: ResourceRequest,
        estimated_cost: float,
        budget: float,
        spent: float,
        lease_seconds: float = 30.0,
    ) -> Reservation:
        canonical_task = canonical_identifier(
            "task", f"{run_id}:{task_id}"
        )
        reservation_id = "reservation_" + hashlib.sha256(
            f"{run_id}:{canonical_task}:{attempt}".encode("utf-8")
        ).hexdigest()[:32]
        cost_id = "costres_" + hashlib.sha256(
            reservation_id.encode("utf-8")
        ).hexdigest()[:32]
        expires = (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(1.0, float(lease_seconds)))
        ).isoformat().replace("+00:00", "Z")
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT rr.*,cr.amount FROM resource_reservations rr "
                    "JOIN cost_reservations cr USING(reservation_id) "
                    "WHERE rr.reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    return Reservation(
                        reservation_id,
                        run_id,
                        canonical_task,
                        attempt,
                        pool.pool_id,
                        float(existing["amount"]),
                        True,
                    )
                used = connection.execute(
                    "SELECT COALESCE(SUM(slots),0) slots,"
                    "COALESCE(SUM(cpu_cores),0) cpu,"
                    "COALESCE(SUM(gpu_count),0) gpu,"
                    "COALESCE(SUM(vram_mb),0) vram "
                    "FROM resource_reservations WHERE pool_id=? "
                    "AND status='active'",
                    (pool.pool_id,),
                ).fetchone()
                if (
                    used["slots"] + request.slots > pool.slots_total
                    or used["cpu"] + request.cpu_cores
                    > pool.cpu_cores_total > 0
                    or used["gpu"] + request.gpu_count
                    > pool.gpu_count_total
                    or used["vram"] + request.vram_mb
                    > pool.vram_mb_total
                ):
                    raise CapacityUnavailable("resource capacity changed")
                active_cost = connection.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM cost_reservations "
                    "WHERE run_id=? AND status='active'",
                    (run_id,),
                ).fetchone()[0]
                if (
                    float(spent)
                    + float(active_cost)
                    + float(estimated_cost)
                    > float(budget) + 1e-9
                ):
                    raise BudgetUnavailable("budget cannot cover reservation")
                now = utc_now()
                connection.execute(
                    "INSERT INTO resource_reservations(reservation_id,run_id,"
                    "task_id,attempt,pool_id,slots,cpu_cores,gpu_count,vram_mb,"
                    "status,lease_expires_at,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reservation_id,
                        run_id,
                        canonical_task,
                        attempt,
                        pool.pool_id,
                        request.slots,
                        request.cpu_cores,
                        request.gpu_count,
                        request.vram_mb,
                        "active",
                        expires,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO cost_reservations(cost_reservation_id,"
                    "reservation_id,run_id,task_id,amount,status,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        cost_id,
                        reservation_id,
                        run_id,
                        canonical_task,
                        estimated_cost,
                        "active",
                        now,
                    ),
                )
                connection.commit()
                return Reservation(
                    reservation_id,
                    run_id,
                    canonical_task,
                    attempt,
                    pool.pool_id,
                    estimated_cost,
                )
            except Exception:
                connection.rollback()
                raise

    def release(
        self,
        reservation_id: str,
        *,
        reason: str,
        actual_cost: float | None = None,
    ) -> bool:
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status FROM resource_reservations "
                    "WHERE reservation_id=?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(reservation_id)
                if row["status"] != "active":
                    connection.rollback()
                    return False
                now = utc_now()
                connection.execute(
                    "UPDATE resource_reservations SET status='released',"
                    "released_at=?,release_reason=? WHERE reservation_id=?",
                    (now, reason, reservation_id),
                )
                connection.execute(
                    "UPDATE cost_reservations SET status='released',"
                    "released_at=?,actual_cost=? WHERE reservation_id=?",
                    (now, actual_cost, reservation_id),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def release_orphans(self) -> int:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT reservation_id FROM resource_reservations "
                "WHERE status='active'"
            ).fetchall()
        for row in rows:
            self.release(
                row["reservation_id"],
                reason="startup-orphan-recovery",
            )
        return len(rows)

    def resources(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            pools = connection.execute(
                "SELECT * FROM resource_pools ORDER BY provider,pool_id"
            ).fetchall()
        result = []
        for row in pools:
            data = dict(row)
            data["local"] = bool(data["local"])
            data["availability"] = self.availability(data["pool_id"])
            result.append(data)
        return result

    def summary(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) total,"
                "COALESCE(SUM(CASE WHEN status='active' THEN 1 ELSE 0 END),0) active "
                "FROM resource_reservations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            costs = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='active' "
                "THEN amount ELSE 0 END),0) reserved,"
                "COALESCE(SUM(actual_cost),0) actual "
                "FROM cost_reservations WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return {
            "reservations": int(row["total"]),
            "active_leases": int(row["active"]),
            "reserved_cost": float(costs["reserved"]),
            "released_actual_cost": float(costs["actual"]),
        }

    def save_circuit(self, provider: str, snapshot: dict[str, Any]) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO provider_circuits(provider,state,"
                "consecutive_failures,opened_at,probe_in_flight,updated_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "state=excluded.state,"
                "consecutive_failures=excluded.consecutive_failures,"
                "opened_at=excluded.opened_at,"
                "probe_in_flight=excluded.probe_in_flight,"
                "updated_at=excluded.updated_at",
                (
                    provider,
                    snapshot["state"],
                    snapshot["consecutive_failures"],
                    snapshot.get("opened_at"),
                    int(bool(snapshot.get("probe_in_flight"))),
                    utc_now(),
                ),
            )

    def load_circuit(self, provider: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state,consecutive_failures,opened_at,"
                "probe_in_flight FROM provider_circuits WHERE provider=?",
                (provider,),
            ).fetchone()
        return dict(row) if row is not None else None

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "resource_pools",
                    "resource_snapshots",
                    "resource_reservations",
                    "cost_reservations",
                    "approvals",
                    "provider_circuits",
                )
            }
