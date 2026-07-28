"""Planner-owned, auditable plan patch proposal contracts."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from .contracts_v4 import utc_now


PLAN_PATCH_REVISION = "plan-patch-v1"


@dataclass(frozen=True, slots=True)
class PlanPatch:
    patch_id: str
    base_revision: int
    target_revision: int
    scope: str
    failed_task_id: str
    affected_task_ids: tuple[str, ...]
    operations: tuple[dict[str, Any], ...]
    rationale: str
    created_at: str

    @classmethod
    def propose(
        cls,
        *,
        base_revision: int,
        scope: str,
        failed_task_id: str,
        affected_task_ids: set[str] | list[str],
        rationale: str,
    ) -> "PlanPatch":
        if scope not in {"node", "subgraph", "global"}:
            raise ValueError(f"unsupported plan patch scope: {scope}")
        affected = tuple(sorted(set(affected_task_ids)))
        raw = (
            f"{base_revision}:{scope}:{failed_task_id}:"
            f"{','.join(affected)}:{rationale}"
        )
        identifier = "patch_" + hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:32]
        return cls(
            patch_id=identifier,
            base_revision=int(base_revision),
            target_revision=int(base_revision) + 1,
            scope=scope,
            failed_task_id=failed_task_id,
            affected_task_ids=affected,
            operations=(
                {
                    "op": "replace_failed_node",
                    "task_id": failed_task_id,
                },
                {
                    "op": "revalidate_affected_scope",
                    "task_ids": list(affected),
                },
            ),
            rationale=rationale,
            created_at=utc_now(),
        )

    def validate(self) -> None:
        if self.target_revision != self.base_revision + 1:
            raise ValueError("plan patch revision must advance by one")
        if self.failed_task_id not in self.affected_task_ids:
            raise ValueError("failed task must be inside patch scope")
        if not self.operations:
            raise ValueError("plan patch must contain operations")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["affected_task_ids"] = list(self.affected_task_ids)
        data["operations"] = [dict(item) for item in self.operations]
        return data
