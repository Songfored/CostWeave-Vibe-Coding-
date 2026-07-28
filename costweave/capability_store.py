"""Persistent hierarchical Beta posteriors learned from attributed outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .calibration import CALIBRATION_REVISION, BetaPosterior
from .contracts_v4 import utc_now


class CapabilityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def scope_keys(model_id: str, task_type: str, risk_level: str) -> tuple[tuple[str, str], ...]:
        return (
            ("exact", f"{model_id}|{task_type}|{risk_level}"),
            ("model_task", f"{model_id}|{task_type}"),
            ("model", model_id),
            ("global", "*"),
        )

    def update(
        self,
        *,
        model_id: str,
        task_type: str,
        risk_level: str,
        success: bool,
    ) -> None:
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                for level, scope_key in self.scope_keys(
                    model_id, task_type, risk_level
                ):
                    row = connection.execute(
                        "SELECT successes,failures FROM capability_posteriors "
                        "WHERE scope_key=?",
                        (scope_key,),
                    ).fetchone()
                    posterior = BetaPosterior(
                        float(row["successes"]) if row else 1.0,
                        float(row["failures"]) if row else 1.0,
                    ).update(success)
                    connection.execute(
                        "INSERT INTO capability_posteriors(scope_key,level,"
                        "successes,failures,sample_size,revision,updated_at) "
                        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET "
                        "level=excluded.level,successes=excluded.successes,"
                        "failures=excluded.failures,sample_size=excluded.sample_size,"
                        "revision=excluded.revision,updated_at=excluded.updated_at",
                        (
                            scope_key,
                            level,
                            posterior.successes,
                            posterior.failures,
                            posterior.samples,
                            CALIBRATION_REVISION,
                            utc_now(),
                        ),
                    )
                    calibrator_id = "calibrator_" + hashlib.sha256(
                        f"{level}:{scope_key}:{CALIBRATION_REVISION}".encode(
                            "utf-8"
                        )
                    ).hexdigest()[:32]
                    connection.execute(
                        "INSERT INTO forecast_calibrators(calibrator_id,"
                        "level,scope_key,revision,parameters_json,sample_size,"
                        "active,created_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(level,scope_key,revision) DO UPDATE SET "
                        "parameters_json=excluded.parameters_json,"
                        "sample_size=excluded.sample_size,active=1",
                        (
                            calibrator_id,
                            level,
                            scope_key,
                            CALIBRATION_REVISION,
                            json.dumps(
                                {
                                    "successes": posterior.successes,
                                    "failures": posterior.failures,
                                },
                                sort_keys=True,
                            ),
                            int(posterior.samples),
                            1,
                            utc_now(),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def candidates(
        self, *, model_id: str, task_type: str, risk_level: str
    ) -> list[tuple[str, BetaPosterior]]:
        keys = self.scope_keys(model_id, task_type, risk_level)
        result = []
        with self._connection() as connection:
            for level, scope_key in keys:
                row = connection.execute(
                    "SELECT successes,failures FROM capability_posteriors "
                    "WHERE scope_key=?",
                    (scope_key,),
                ).fetchone()
                if row:
                    result.append(
                        (
                            level,
                            BetaPosterior(
                                float(row["successes"]),
                                float(row["failures"]),
                            ),
                        )
                    )
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM capability_posteriors "
                    "ORDER BY level,scope_key"
                )
            ]

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM capability_posteriors"
            ).fetchone()[0]
        return {"capability_posteriors": int(count)}
