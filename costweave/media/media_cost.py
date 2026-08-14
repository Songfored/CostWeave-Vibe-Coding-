"""Idempotent simulated media cost ledger."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any


MEDIA_COST_REVISION = "media-cost-v1"
MEDIA_COST_CATEGORIES = (
    "storyboard",
    "keyframe",
    "animatic",
    "final_render",
    "rerender",
    "continuity_validation",
    "postprocess",
    "storage",
    "assembly",
)


@dataclass(frozen=True, slots=True)
class MediaCostEntry:
    entry_id: str
    project_id: str
    shot_id: str | None
    category: str
    amount: float
    seconds: float
    accepted: bool
    attempt: int
    revision: str = MEDIA_COST_REVISION

    def to_dict(self) -> dict:
        return asdict(self)


class MediaCostLedger:
    def __init__(self) -> None:
        self._entries: dict[str, MediaCostEntry] = {}

    def add(
        self,
        *,
        project_id: str,
        shot_id: str | None,
        category: str,
        amount: float,
        seconds: float = 0.0,
        accepted: bool = False,
        attempt: int = 1,
    ) -> MediaCostEntry:
        if category not in MEDIA_COST_CATEGORIES:
            raise ValueError("unknown media cost category")
        if amount < 0 or seconds < 0:
            raise ValueError("cost and seconds cannot be negative")
        raw = f"{project_id}:{shot_id}:{category}:{attempt}"
        entry_id = "media_cost_" + hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:28]
        entry = MediaCostEntry(
            entry_id,
            project_id,
            shot_id,
            category,
            round(float(amount), 8),
            round(float(seconds), 4),
            bool(accepted),
            int(attempt),
        )
        existing = self._entries.get(entry_id)
        if existing is not None and existing != entry:
            raise ValueError("media cost identity conflict")
        self._entries[entry_id] = entry
        return entry

    def entries(self) -> list[MediaCostEntry]:
        return list(self._entries.values())

    def summary(self) -> dict[str, Any]:
        total = sum(item.amount for item in self._entries.values())
        accepted_seconds = sum(
            item.seconds
            for item in self._entries.values()
            if item.accepted and item.category in {"final_render", "rerender"}
        )
        accepted_shots = sum(
            item.accepted
            for item in self._entries.values()
            if item.category in {"final_render", "rerender"}
        )
        return {
            "total": round(total, 8),
            "accepted_seconds": round(accepted_seconds, 4),
            "accepted_shots": accepted_shots,
            "cost_per_accepted_second": (
                round(total / accepted_seconds, 8)
                if accepted_seconds
                else None
            ),
            "cost_per_accepted_shot": (
                round(total / accepted_shots, 8)
                if accepted_shots
                else None
            ),
            "categories": {
                category: round(
                    sum(
                        item.amount
                        for item in self._entries.values()
                        if item.category == category
                    ),
                    8,
                )
                for category in MEDIA_COST_CATEGORIES
            },
        }

