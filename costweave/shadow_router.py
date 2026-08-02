"""Strictly advisory shadow routing for CostWeave v0.5.6.3."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts_v4 import utc_now
from .domain import RunMode, TaskContract
from .evaluation_protocol import canonical_json
from .experimental_calls import ExperimentalCallError
from .router import PredictiveRouter


SHADOW_SCHEMA_VERSION = "v0.5.6.3"
SHADOW_POLICY_REVISION = "shadow-router-policy-v1"


class ShadowIsolationError(ExperimentalCallError):
    """Raised when an advisory evaluation changes authoritative state."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    shadow_decision_id: str
    experiment_id: str
    run_id: str
    task_id: str
    policy_revision: str
    catalog_revision: str
    actual_route_hash: str
    task_fingerprint: str
    recommendation: Mapping[str, Any]
    candidates: tuple[Mapping[str, Any], ...]
    rejections: tuple[Mapping[str, Any], ...]
    decision_hash: str
    created_at: str
    advisory_only: bool = True
    intervention_count: int = 0
    schema_version: str = SHADOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.shadow_decision_id.startswith("shadow_decision_"):
            raise ShadowIsolationError("shadow decision id is invalid")
        if not self.experiment_id.startswith("experiment_"):
            raise ShadowIsolationError("shadow experiment id is invalid")
        if not self.run_id or not self.task_id:
            raise ShadowIsolationError("shadow attribution is required")
        if not self.advisory_only or self.intervention_count != 0:
            raise ShadowIsolationError("shadow decisions cannot intervene")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["recommendation"] = dict(self.recommendation)
        result["candidates"] = [dict(item) for item in self.candidates]
        result["rejections"] = [dict(item) for item in self.rejections]
        return result


class ShadowRouter:
    """Compute a counterfactual route while preserving the actual route."""

    def __init__(
        self,
        router: PredictiveRouter,
        *,
        policy_revision: str = SHADOW_POLICY_REVISION,
    ) -> None:
        self.router = router.freeze()
        self.policy_revision = policy_revision

    def observe(
        self,
        *,
        experiment_id: str,
        run_id: str,
        task: TaskContract,
        actual_route: Mapping[str, Any],
        mode: RunMode = RunMode.BALANCED,
        quality_floor: float = 0.78,
        max_shadow_estimated_cost_usd: float | None = None,
    ) -> ShadowDecision:
        if not experiment_id.startswith("experiment_"):
            raise ShadowIsolationError("shadow experiment id is invalid")
        if not 0.5 <= float(quality_floor) <= 0.99:
            raise ShadowIsolationError("shadow quality floor is invalid")
        if (
            max_shadow_estimated_cost_usd is not None
            and float(max_shadow_estimated_cost_usd) < 0
        ):
            raise ShadowIsolationError("shadow cost ceiling is invalid")

        before_raw = canonical_json(task.to_dict())
        task_fingerprint = _hash(task.to_dict())
        ranked, rejected = self.router.rank_candidates_with_rejections(
            task, mode
        )
        after_raw = canonical_json(task.to_dict())
        if after_raw != before_raw:
            raise ShadowIsolationError(
                "shadow routing mutated the authoritative task"
            )

        candidates = tuple(item.public() for item in ranked)
        eligible = [
            item
            for item in candidates
            if float(item["success_lower_bound"]) >= quality_floor
            and (
                max_shadow_estimated_cost_usd is None
                or float(item["estimated_cost"])
                <= max_shadow_estimated_cost_usd + 1e-10
            )
        ]
        recommendation: dict[str, Any]
        if eligible:
            recommendation = {
                "candidate_found": True,
                "candidate": eligible[0],
                "quality_floor": round(float(quality_floor), 4),
                "cost_ceiling_usd": max_shadow_estimated_cost_usd,
                "applied_to_actual_route": False,
            }
        else:
            recommendation = {
                "candidate_found": False,
                "candidate": None,
                "reason": "no_candidate_passed_shadow_constraints",
                "quality_floor": round(float(quality_floor), 4),
                "cost_ceiling_usd": max_shadow_estimated_cost_usd,
                "applied_to_actual_route": False,
            }

        actual_route_hash = _hash(dict(actual_route))
        identity = {
            "experiment_id": experiment_id,
            "run_id": run_id,
            "task_id": task.id,
            "policy_revision": self.policy_revision,
        }
        decision_id = "shadow_decision_" + _hash(identity)[:32]
        decision_body = {
            **identity,
            "catalog_revision": self.router.catalog_revision,
            "actual_route_hash": actual_route_hash,
            "task_fingerprint": task_fingerprint,
            "recommendation": recommendation,
            "candidates": candidates,
            "rejections": rejected,
            "advisory_only": True,
            "intervention_count": 0,
        }
        return ShadowDecision(
            shadow_decision_id=decision_id,
            experiment_id=experiment_id,
            run_id=run_id,
            task_id=task.id,
            policy_revision=self.policy_revision,
            catalog_revision=self.router.catalog_revision,
            actual_route_hash=actual_route_hash,
            task_fingerprint=task_fingerprint,
            recommendation=recommendation,
            candidates=candidates,
            rejections=tuple(dict(item) for item in rejected),
            decision_hash=_hash(decision_body),
            created_at=utc_now(),
        )


class ShadowStore:
    """Immutable storage for advice and later actual-only comparisons."""

    _OUTCOME_FIELDS = {
        "model_id",
        "accepted",
        "actual_cost_usd",
        "actual_latency_ms",
        "status",
        "evidence_level",
    }

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
    def _row_decision(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "shadow_decision_id": row["shadow_decision_id"],
            "experiment_id": row["experiment_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "policy_revision": row["policy_revision"],
            "catalog_revision": row["catalog_revision"],
            "actual_route_hash": row["actual_route_hash"],
            "task_fingerprint": row["task_fingerprint"],
            "recommendation": json.loads(row["recommendation_json"]),
            "candidates": json.loads(row["candidates_json"]),
            "rejections": json.loads(row["rejections_json"]),
            "decision_hash": row["decision_hash"],
            "advisory_only": bool(row["advisory_only"]),
            "intervention_count": int(row["intervention_count"]),
            "created_at": row["created_at"],
            "schema_version": SHADOW_SCHEMA_VERSION,
        }

    def record_decision(self, decision: ShadowDecision) -> bool:
        if not decision.advisory_only or decision.intervention_count != 0:
            raise ShadowIsolationError("intervening shadow decision rejected")
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT decision_hash FROM shadow_decisions "
                "WHERE experiment_id=? AND task_id=? AND policy_revision=?",
                (
                    decision.experiment_id,
                    decision.task_id,
                    decision.policy_revision,
                ),
            ).fetchone()
            if existing is not None:
                if existing["decision_hash"] != decision.decision_hash:
                    raise ShadowIsolationError(
                        "shadow decision identity was reused for new advice"
                    )
                return False
            connection.execute(
                "INSERT INTO shadow_decisions("
                "shadow_decision_id,experiment_id,run_id,task_id,"
                "policy_revision,catalog_revision,actual_route_hash,"
                "task_fingerprint,recommendation_json,candidates_json,"
                "rejections_json,decision_hash,advisory_only,"
                "intervention_count,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.shadow_decision_id,
                    decision.experiment_id,
                    decision.run_id,
                    decision.task_id,
                    decision.policy_revision,
                    decision.catalog_revision,
                    decision.actual_route_hash,
                    decision.task_fingerprint,
                    canonical_json(dict(decision.recommendation)),
                    canonical_json(list(decision.candidates)),
                    canonical_json(list(decision.rejections)),
                    decision.decision_hash,
                    1,
                    0,
                    decision.created_at,
                ),
            )
        return True

    def decision(self, shadow_decision_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM shadow_decisions WHERE shadow_decision_id=?",
                (shadow_decision_id,),
            ).fetchone()
        return self._row_decision(row) if row is not None else None

    def list_decisions(
        self, experiment_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_decisions WHERE experiment_id=? "
                "ORDER BY created_at,shadow_decision_id LIMIT ?",
                (experiment_id, bounded),
            ).fetchall()
        return [self._row_decision(row) for row in rows]

    def record_comparison(
        self,
        shadow_decision_id: str,
        actual_outcome: Mapping[str, Any],
    ) -> dict[str, Any]:
        unexpected = set(actual_outcome) - self._OUTCOME_FIELDS
        if unexpected:
            raise ShadowIsolationError(
                "actual outcome contains non-governed fields"
            )
        outcome = {key: actual_outcome.get(key) for key in self._OUTCOME_FIELDS}
        if not isinstance(outcome.get("model_id"), str):
            raise ShadowIsolationError("actual model attribution is required")
        outcome_hash = _hash(outcome)
        with self._lock, self._connection() as connection:
            decision = connection.execute(
                "SELECT * FROM shadow_decisions WHERE shadow_decision_id=?",
                (shadow_decision_id,),
            ).fetchone()
            if decision is None:
                raise ShadowIsolationError("shadow decision does not exist")
            recommendation = json.loads(decision["recommendation_json"])
            suggested = recommendation.get("candidate") or {}
            comparison = {
                "shadow_decision_id": shadow_decision_id,
                "actual_model_id": outcome["model_id"],
                "suggested_model_id": suggested.get("model_id"),
                "same_route": (
                    suggested.get("model_id") == outcome["model_id"]
                    if suggested
                    else None
                ),
                "actual_outcome": outcome,
                "counterfactual_outcome_known": False,
                "counterfactual_savings_claim_allowed": False,
                "advisory_only": True,
                "intervention_count": 0,
                "schema_version": SHADOW_SCHEMA_VERSION,
            }
            comparison_id = "shadow_comparison_" + _hash(
                {"shadow_decision_id": shadow_decision_id}
            )[:32]
            existing = connection.execute(
                "SELECT actual_outcome_hash,comparison_json "
                "FROM shadow_comparisons WHERE shadow_decision_id=?",
                (shadow_decision_id,),
            ).fetchone()
            comparison_raw = canonical_json(comparison)
            if existing is not None:
                if (
                    existing["actual_outcome_hash"] != outcome_hash
                    or existing["comparison_json"] != comparison_raw
                ):
                    raise ShadowIsolationError(
                        "shadow comparison is immutable"
                    )
                return json.loads(existing["comparison_json"])
            connection.execute(
                "INSERT INTO shadow_comparisons("
                "comparison_id,shadow_decision_id,actual_outcome_hash,"
                "comparison_json,created_at) VALUES(?,?,?,?,?)",
                (
                    comparison_id,
                    shadow_decision_id,
                    outcome_hash,
                    comparison_raw,
                    utc_now(),
                ),
            )
        return comparison

    def counts(self) -> dict[str, int]:
        with self._connection() as connection:
            decisions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_decisions"
                ).fetchone()[0]
            )
            comparisons = int(
                connection.execute(
                    "SELECT COUNT(*) FROM shadow_comparisons"
                ).fetchone()[0]
            )
            interventions = int(
                connection.execute(
                    "SELECT COALESCE(SUM(intervention_count),0) "
                    "FROM shadow_decisions"
                ).fetchone()[0]
            )
        return {
            "shadow_decisions": decisions,
            "shadow_comparisons": comparisons,
            "shadow_interventions": interventions,
        }
