"""Placement contracts plus persistent placement and cost-ledger store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts_v4 import canonical_identifier, utc_now
from .cost_model import COST_CATEGORIES, CostBreakdown


PLACEMENT_POLICY_REVISION = "placement-policy-v1"


@dataclass(frozen=True, slots=True)
class PlacementCandidate:
    placement_id: str
    task_id: str
    plan_revision: int
    model_id: str
    model_revision: str
    provider: str
    resource_pool: str
    inference_profile: dict[str, Any]
    predicted_quality_mean: float
    predicted_quality_lower: float
    predicted_start_p50_ms: int
    predicted_finish_p95_ms: int
    execution_p50_ms: int
    direct_model_cost_usd: float
    expected_total_cost_usd: float
    forecast_cost: dict[str, Any]
    reservation: dict[str, Any]
    deployment: dict[str, Any]
    posterior: dict[str, Any]
    admission: dict[str, Any]
    latency_forecast: dict[str, Any]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def placement_id(
    task_id: str,
    model_id: str,
    pool_id: str,
    inference_revision: str,
    plan_revision: int,
) -> str:
    digest = hashlib.sha256(
        f"{task_id}:{plan_revision}:{model_id}:{pool_id}:"
        f"{inference_revision}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    return f"placement_{digest}"


class PlacementStore:
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

    def ensure_policy(
        self,
        *,
        revision_id: str = PLACEMENT_POLICY_REVISION,
        config: dict[str, Any] | None = None,
    ) -> None:
        raw = json.dumps(
            config or {"strategy": "constrained-greedy-local-swap"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO policy_revisions(revision_id,policy_kind,"
                "config_json,checksum,active,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(revision_id) DO NOTHING",
                (
                    revision_id,
                    "placement",
                    raw,
                    checksum,
                    1,
                    utc_now(),
                ),
            )

    def sync_run(self, run_id: str, tasks: list[Any]) -> int:
        self.ensure_policy()
        written = 0
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for task in tasks:
                    candidates = [
                        task.placement,
                        *(task.backup_placements or []),
                    ]
                    for rank, candidate in enumerate(candidates):
                        if not candidate:
                            continue
                        canonical_task = canonical_identifier(
                            "task", f"{run_id}:{task.id}"
                        )
                        connection.execute(
                            "INSERT INTO placements(placement_id,run_id,"
                            "task_id,policy_revision,model_id,model_revision,"
                            "provider,resource_pool,inference_profile_json,"
                            "plan_revision,status,rank,predicted_quality_lower,"
                            "predicted_start_p50_ms,predicted_finish_p95_ms,"
                            "forecast_cost_json,expected_total_cost,"
                            "decision_json,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(run_id,task_id,plan_revision,rank) "
                            "DO UPDATE SET "
                            "policy_revision=excluded.policy_revision,"
                            "model_id=excluded.model_id,"
                            "model_revision=excluded.model_revision,"
                            "provider=excluded.provider,"
                            "resource_pool=excluded.resource_pool,"
                            "inference_profile_json=excluded.inference_profile_json,"
                            "status=excluded.status,"
                            "predicted_quality_lower=excluded.predicted_quality_lower,"
                            "predicted_start_p50_ms=excluded.predicted_start_p50_ms,"
                            "predicted_finish_p95_ms=excluded.predicted_finish_p95_ms,"
                            "forecast_cost_json=excluded.forecast_cost_json,"
                            "expected_total_cost=excluded.expected_total_cost,"
                            "decision_json=excluded.decision_json",
                            (
                                candidate["placement_id"],
                                run_id,
                                canonical_task,
                                PLACEMENT_POLICY_REVISION,
                                candidate["model_id"],
                                candidate["model_revision"],
                                candidate["provider"],
                                candidate["resource_pool"],
                                json.dumps(
                                    candidate["inference_profile"],
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                candidate["plan_revision"],
                                "selected" if rank == 0 else "backup",
                                rank,
                                candidate["predicted_quality_lower"],
                                candidate["predicted_start_p50_ms"],
                                candidate["predicted_finish_p95_ms"],
                                json.dumps(
                                    candidate["forecast_cost"],
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                candidate["expected_total_cost_usd"],
                                json.dumps(
                                    {
                                        "deployment": candidate.get(
                                            "deployment", {}
                                        ),
                                        "posterior": candidate.get(
                                            "posterior", {}
                                        ),
                                        "admission": candidate.get(
                                            "admission", {}
                                        ),
                                        "latency_forecast": candidate.get(
                                            "latency_forecast", {}
                                        ),
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                utc_now(),
                            ),
                        )
                        written += 1
                    if task.placement:
                        self._write_ledger(
                            connection,
                            run_id=run_id,
                            task_id=canonical_identifier(
                                "task", f"{run_id}:{task.id}"
                            ),
                            placement=task.placement,
                            phase="forecast",
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return written

    def record_actual(
        self,
        *,
        run_id: str,
        task: Any,
        actual_total: float,
    ) -> None:
        if not task.placement:
            return
        forecast = CostBreakdown.from_mapping(
            task.placement["forecast_cost"]["p50"]
        )
        scale = (
            max(0.0, float(actual_total)) / forecast.total
            if forecast.total > 0
            else 0.0
        )
        actual = {
            category: float(getattr(forecast, category)) * scale
            for category in COST_CATEGORIES
        }
        placement = {
            **task.placement,
            "forecast_cost": {"p50": actual},
        }
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._write_ledger(
                    connection,
                    run_id=run_id,
                    task_id=canonical_identifier(
                        "task", f"{run_id}:{task.id}"
                    ),
                    placement=placement,
                    phase="actual",
                    attempt=max(1, int(getattr(task, "attempt", 1))),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _write_ledger(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        task_id: str,
        placement: dict[str, Any],
        phase: str,
        attempt: int | None = None,
    ) -> None:
        components = placement["forecast_cost"]["p50"]
        for category in COST_CATEGORIES:
            amount = float(components.get(category, 0.0))
            attribution = (
                f"{run_id}:{task_id}:{phase}:"
                f"{attempt if attempt is not None else 'plan'}:"
                f"{placement['placement_id']}:{category}"
            )
            entry_id = "ledger_" + hashlib.sha256(
                attribution.encode("utf-8")
            ).hexdigest()[:32]
            connection.execute(
                "INSERT INTO cost_ledger(entry_id,run_id,task_id,"
                "placement_id,phase,category,amount,attribution_key,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(attribution_key) DO UPDATE SET "
                "amount=excluded.amount",
                (
                    entry_id,
                    run_id,
                    task_id,
                    placement["placement_id"],
                    phase,
                    category,
                    amount,
                    attribution,
                    utc_now(),
                ),
            )

    def list_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM placements WHERE run_id=? "
                "ORDER BY task_id,rank",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["inference_profile"] = json.loads(
                item.pop("inference_profile_json")
            )
            item["forecast_cost"] = json.loads(
                item.pop("forecast_cost_json")
            )
            decision = json.loads(
                item.pop("decision_json", "{}")
            )
            item.update(decision)
            result.append(item)
        return result

    def ledger(
        self, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if run_id is None:
                rows = connection.execute(
                    "SELECT * FROM cost_ledger ORDER BY created_at,entry_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cost_ledger WHERE run_id=? "
                    "ORDER BY created_at,entry_id",
                    (run_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT phase,COALESCE(SUM(amount),0) total,"
                "COUNT(*) entries FROM cost_ledger WHERE run_id=? "
                "GROUP BY phase",
                (run_id,),
            ).fetchall()
            placements = connection.execute(
                "SELECT COUNT(*) total,"
                "COALESCE(SUM(CASE WHEN status='selected' THEN 1 ELSE 0 END),0) "
                "selected FROM placements WHERE run_id=?",
                (run_id,),
            ).fetchone()
        phases = {
            row["phase"]: {
                "total": float(row["total"]),
                "entries": int(row["entries"]),
            }
            for row in rows
        }
        return {
            "placements": int(placements["total"]),
            "selected_placements": int(placements["selected"]),
            "forecast_total": phases.get(
                "forecast", {"total": 0.0}
            )["total"],
            "actual_total": phases.get(
                "actual", {"total": 0.0}
            )["total"],
            "ledger_entries": sum(
                item["entries"] for item in phases.values()
            ),
        }

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "placements",
                    "cost_ledger",
                    "cache_metadata",
                    "policy_revisions",
                )
            }
