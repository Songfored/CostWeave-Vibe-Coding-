"""Bounded recovery decisions and persistent validation/recovery records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .contracts_v4 import canonical_identifier, utc_now
from .failure_classifier import FailureClass, FailureRecord
from .plan_patch import PlanPatch


RECOVERY_POLICY_REVISION = "recovery-policy-v1"


class RecoveryAction(StrEnum):
    RETRY_NODE = "retry_node"
    UPGRADE_NODE = "upgrade_node"
    REPLAN_SUBGRAPH = "replan_subgraph"
    REPLAN_GLOBAL = "replan_global"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    recovery_count: int
    max_recoveries: int
    task_attempt: int
    max_task_attempts: int
    remaining_budget: float
    estimated_recovery_cost: float
    elapsed_seconds: float
    time_limit_seconds: float


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: str
    reason: str
    scope: str
    allowed: bool
    estimated_cost: float
    policy_revision: str = RECOVERY_POLICY_REVISION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RecoveryPolicy:
    def decide(
        self,
        failure: FailureRecord,
        context: RecoveryContext,
    ) -> RecoveryDecision:
        stop_reason = None
        if context.recovery_count >= context.max_recoveries:
            stop_reason = "recovery count limit reached"
        elif context.elapsed_seconds >= context.time_limit_seconds:
            stop_reason = "recovery time limit reached"
        elif (
            context.estimated_recovery_cost
            > context.remaining_budget + 1e-9
        ):
            stop_reason = "recovery budget exhausted"
        if stop_reason:
            return RecoveryDecision(
                RecoveryAction.STOP.value,
                stop_reason,
                "none",
                False,
                context.estimated_recovery_cost,
            )

        category = FailureClass(failure.classification)
        if category == FailureClass.TRANSIENT:
            if context.task_attempt >= context.max_task_attempts:
                return RecoveryDecision(
                    RecoveryAction.STOP.value,
                    "task attempt limit reached",
                    "none",
                    False,
                    context.estimated_recovery_cost,
                )
            action, scope = RecoveryAction.RETRY_NODE, "node"
        elif category in {
            FailureClass.CAPABILITY,
            FailureClass.EVIDENCE,
            FailureClass.POLICY,
        }:
            action, scope = RecoveryAction.UPGRADE_NODE, "node"
        elif category == FailureClass.ROOT_ASSUMPTION:
            action, scope = RecoveryAction.REPLAN_GLOBAL, "global"
        else:
            action, scope = RecoveryAction.REPLAN_SUBGRAPH, "subgraph"
        return RecoveryDecision(
            action.value,
            f"{category.value} maps to {action.value}",
            scope,
            True,
            context.estimated_recovery_cost,
        )


class EvidenceRecoveryStore:
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

    def record_validation(
        self,
        *,
        run_id: str,
        task: Any,
        validation: dict[str, Any],
    ) -> str:
        canonical_task = canonical_identifier(
            "task", f"{run_id}:{task.id}"
        )
        raw = f"{run_id}:{canonical_task}:{task.attempt}"
        result_id = "validation_" + hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:32]
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO validation_results(validation_id,run_id,"
                    "task_id,attempt,contract_revision,level,passed,score,"
                    "false_positive_risk,false_negative_risk,result_json,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id,task_id,attempt) DO UPDATE SET "
                    "contract_revision=excluded.contract_revision,"
                    "level=excluded.level,passed=excluded.passed,"
                    "score=excluded.score,"
                    "false_positive_risk=excluded.false_positive_risk,"
                    "false_negative_risk=excluded.false_negative_risk,"
                    "result_json=excluded.result_json",
                    (
                        result_id,
                        run_id,
                        canonical_task,
                        task.attempt,
                        validation["contract_revision"],
                        validation["level"],
                        int(bool(validation["passed"])),
                        float(validation["score"]),
                        float(validation["false_positive_risk"]),
                        float(validation["false_negative_risk"]),
                        json.dumps(
                            validation,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        utc_now(),
                    ),
                )
                connection.execute(
                    "DELETE FROM criterion_results "
                    "WHERE validation_id=?",
                    (result_id,),
                )
                for item in validation.get("criterion_results", []):
                    connection.execute(
                        "INSERT INTO criterion_results(validation_id,"
                        "criterion_id,criterion_text,critical,passed,"
                        "evidence_json,independent_evidence,reason) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            result_id,
                            item["criterion_id"],
                            item["criterion"],
                            int(bool(item["critical"])),
                            int(bool(item["passed"])),
                            json.dumps(
                                item.get("evidence_refs", []),
                                ensure_ascii=False,
                            ),
                            int(bool(item["independent_evidence"])),
                            item.get("reason", ""),
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return result_id

    def record_failure(
        self,
        *,
        run_id: str,
        task: Any,
        failure: FailureRecord,
        decision: RecoveryDecision,
        affected_task_ids: list[str],
        patch: PlanPatch | None = None,
    ) -> None:
        canonical_task = canonical_identifier(
            "task", f"{run_id}:{task.id}"
        )
        with self._lock, self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO failure_records(failure_id,run_id,task_id,"
                    "attempt,classifier_revision,classification,confidence,"
                    "signals_json,source,recovery_action,recovery_reason,"
                    "affected_tasks_json,estimated_recovery_cost,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(failure_id) DO UPDATE SET "
                    "recovery_action=excluded.recovery_action,"
                    "recovery_reason=excluded.recovery_reason,"
                    "affected_tasks_json=excluded.affected_tasks_json,"
                    "estimated_recovery_cost="
                    "excluded.estimated_recovery_cost",
                    (
                        failure.failure_id,
                        run_id,
                        canonical_task,
                        failure.attempt,
                        "failure-classifier-v1",
                        failure.classification,
                        failure.confidence,
                        json.dumps(list(failure.signals)),
                        failure.source,
                        decision.action,
                        decision.reason,
                        json.dumps(sorted(affected_task_ids)),
                        decision.estimated_cost,
                        failure.created_at,
                    ),
                )
                if patch is not None:
                    connection.execute(
                        "INSERT INTO plan_revisions(run_id,plan_revision,"
                        "patch_id,failure_id,scope,patch_json,status,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(run_id,plan_revision) DO UPDATE SET "
                        "patch_json=excluded.patch_json,"
                        "status=excluded.status",
                        (
                            run_id,
                            patch.target_revision,
                            patch.patch_id,
                            failure.failure_id,
                            patch.scope,
                            json.dumps(
                                patch.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            "applied",
                            patch.created_at,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def validations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM validation_results WHERE run_id=? "
                "ORDER BY created_at,validation_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["passed"] = bool(item["passed"])
            item["result"] = json.loads(item.pop("result_json"))
            result.append(item)
        return result

    def failures(self, run_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM failure_records WHERE run_id=? "
                "ORDER BY created_at,failure_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["signals"] = json.loads(item.pop("signals_json"))
            item["affected_tasks"] = json.loads(
                item.pop("affected_tasks_json")
            )
            result.append(item)
        return result

    def summary(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            validations = connection.execute(
                "SELECT COUNT(*) total,"
                "COALESCE(SUM(CASE WHEN passed=1 THEN 1 ELSE 0 END),0) "
                "passed FROM validation_results WHERE run_id=?",
                (run_id,),
            ).fetchone()
            failures = connection.execute(
                "SELECT COUNT(*) total FROM failure_records WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return {
            "validation_results": int(validations["total"]),
            "passed_validations": int(validations["passed"]),
            "failed_validations": int(
                validations["total"] - validations["passed"]
            ),
            "failure_records": int(failures["total"]),
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
                    "validation_results",
                    "criterion_results",
                    "failure_records",
                    "plan_revisions",
                )
            }
