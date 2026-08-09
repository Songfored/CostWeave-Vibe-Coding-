"""Persistence for forecasts, attributed outcomes and evaluation runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .calibration import CALIBRATION_REVISION
from .capability_store import CapabilityStore
from .contracts_v4 import utc_now
from .evaluation import run_fixed_benchmark
from .outcome_forecast import (
    FORECAST_POLICY_REVISION,
    OutcomeCategory,
    OutcomeForecast,
    OutcomeRecord,
)


class OutcomeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.capabilities = CapabilityStore(path)
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

    def record_forecast(self, forecast: OutcomeForecast) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO forecast_snapshots(forecast_id,run_id,"
                "policy_revision,calibration_revision,seed,sample_size,"
                "forecast_json,created_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (
                    forecast.forecast_id,
                    forecast.run_id,
                    forecast.policy_revision,
                    forecast.calibration_revision,
                    forecast.seed,
                    forecast.sample_size,
                    json.dumps(
                        forecast.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    utc_now(),
                ),
            )

    def forecast(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT forecast_json FROM forecast_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return json.loads(row["forecast_json"]) if row else None

    @staticmethod
    def _duration(record: Any) -> float:
        if not record.started_at or not record.finished_at:
            return 0.0
        start = datetime.fromisoformat(record.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(record.finished_at.replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds())

    @staticmethod
    def _category(record: Any) -> str:
        if record.status.value != "completed":
            return OutcomeCategory.FAILED.value
        actions = {
            item.get("action") for item in record.recovery_history
        }
        if "upgrade_node" in actions or "replan_global" in actions:
            return OutcomeCategory.UPGRADED.value
        if actions:
            return OutcomeCategory.REPAIRED.value
        return OutcomeCategory.FIRST_PASS.value

    def build_outcome(self, record: Any) -> OutcomeRecord:
        category = self._category(record)
        tasks = list(record.plan.tasks) if record.plan else []
        scores = [
            float(task.validation.get("score", 0.0))
            for task in tasks
            if task.validation
        ]
        models = sorted(
            {task.selected_worker for task in tasks if task.selected_worker}
        ) or ["unassigned"]
        catalog_revision = (
            record.plan.model_snapshot if record.plan else "unknown"
        )
        attribution = {
            "model_ids": models,
            "prompt_version": "success-contract-v1",
            "tool_version": "handoff-envelope-v1",
            "policy_revisions": {
                "forecast": FORECAST_POLICY_REVISION,
                "calibration": CALIBRATION_REVISION,
                "recovery": "recovery-policy-v1",
                "routing": (
                    (record.plan.routing_summary or {}).get(
                        "strategy_revision", "resource-portfolio-v1"
                    )
                    if record.plan
                    else "unknown"
                ),
            },
            "data_versions": {
                "catalog": catalog_revision or "unknown",
                "contracts": "v4.1",
                "runtime": "v5.8.3",
            },
            "plan_revision": record.plan.version if record.plan else 0,
        }
        identity = hashlib.sha256(
            f"{record.id}:{category}".encode("utf-8")
        ).hexdigest()[:32]
        return OutcomeRecord(
            outcome_id=f"outcome_{identity}",
            run_id=str(record.id),
            category=category,
            success=category != OutcomeCategory.FAILED.value,
            quality=sum(scores) / len(scores) if scores else 0.0,
            cost=float(record.spent),
            completion_seconds=self._duration(record),
            critical_false_pass=any(
                bool(
                    task.validation
                    and task.validation.get("passed")
                    and task.validation.get("critical_failures")
                )
                for task in tasks
            ),
            attribution=attribution,
            recorded_at=utc_now(),
        )

    def record_outcome(self, record: Any) -> OutcomeRecord:
        outcome = self.build_outcome(record)
        inserted = False
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO outcomes(outcome_id,run_id,category,"
                "success,quality,cost,completion_seconds,critical_false_pass,"
                "attribution_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    outcome.outcome_id,
                    outcome.run_id,
                    outcome.category,
                    int(outcome.success),
                    outcome.quality,
                    outcome.cost,
                    outcome.completion_seconds,
                    int(outcome.critical_false_pass),
                    json.dumps(
                        outcome.attribution,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    outcome.recorded_at,
                ),
            )
            inserted = cursor.rowcount == 1
        if inserted and record.plan:
            for task in record.plan.tasks:
                if task.selected_worker:
                    passed = bool(
                        task.validation
                        and task.validation.get("passed")
                    )
                    self.capabilities.update(
                        model_id=task.selected_worker,
                        task_type=task.task_type,
                        risk_level=task.risk_level,
                        success=passed,
                    )
                    placement = dict(task.placement or {})
                    deployment = dict(
                        placement.get("deployment") or {}
                    )
                    profile = dict(
                        placement.get("inference_profile") or {}
                    )
                    if placement and profile:
                        self.capabilities.update_deployment(
                            model_id=str(
                                deployment.get("model_id")
                                or placement.get("model_id")
                                or task.selected_worker
                            ),
                            provider=str(
                                deployment.get("provider")
                                or placement.get("provider")
                                or "unknown"
                            ),
                            endpoint=str(
                                deployment.get("endpoint")
                                or (
                                    f"{placement.get('provider', 'unknown')}"
                                    f"://{placement.get('model_id', task.selected_worker)}"
                                )
                            ),
                            reasoning_effort=str(
                                deployment.get("reasoning_effort")
                                or profile.get(
                                    "reasoning_effort", "medium"
                                )
                            ),
                            task_type=task.task_type,
                            risk_level=task.risk_level,
                            success=passed,
                        )
        return outcome

    def outcomes(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM outcomes ORDER BY recorded_at,outcome_id"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["success"] = bool(item["success"])
            item["critical_false_pass"] = bool(item["critical_false_pass"])
            item["attribution"] = json.loads(item.pop("attribution_json"))
            result.append(item)
        return result

    def ensure_fixed_evaluation(self) -> dict[str, Any]:
        result = run_fixed_benchmark()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO evaluation_runs(evaluation_id,"
                "revision,sample_size,result_json,passed,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    result["evaluation_id"],
                    result["revision"],
                    result["sample_size"],
                    json.dumps(result, sort_keys=True),
                    int(bool(result["passed"])),
                    utc_now(),
                ),
            )
        return result

    def evaluations(self) -> list[dict[str, Any]]:
        self.ensure_fixed_evaluation()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT result_json FROM evaluation_runs "
                "ORDER BY created_at,evaluation_id"
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "forecast_snapshots",
                    "outcomes",
                    "forecast_calibrators",
                    "evaluation_runs",
                )
            }
        return {**counts, **self.capabilities.counts()}
