"""Structured shot contracts with immutable creative intent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


SHOT_CONTRACT_REVISION = "shot-contract-v1"


@dataclass(frozen=True, slots=True)
class ShotContract:
    shot_id: str
    sequence: int
    duration_seconds: float
    resolution: str
    fps: int
    aspect_ratio: str
    shot_type: str
    character_refs: tuple[str, ...]
    location_ref: str
    asset_refs: tuple[str, ...]
    reference_dependencies: tuple[str, ...]
    action: str
    camera: str
    lighting: str
    start_state: str
    end_state: str
    acceptance_criteria: tuple[str, ...]
    locked_intent: tuple[str, ...]
    generation_policy: dict[str, Any]
    prompt_revision: int = 0
    status: str = "planned"
    revision: str = SHOT_CONTRACT_REVISION

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("shot duration must be positive")
        if not re.fullmatch(r"\d+x\d+", self.resolution):
            raise ValueError("resolution must be WIDTHxHEIGHT")
        if not 1 <= self.fps <= 120:
            raise ValueError("fps must be in [1,120]")
        if self.generation_policy.get("candidate_count", 0) < 1:
            raise ValueError("candidate_count must be positive")
        if self.generation_policy.get("max_rerenders", -1) < 0:
            raise ValueError("max_rerenders cannot be negative")

    @property
    def locked_intent_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.locked_intent,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["locked_intent_hash"] = self.locked_intent_hash
        return payload

