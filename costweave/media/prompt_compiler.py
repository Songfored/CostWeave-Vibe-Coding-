"""Controlled model-specific prompt compilation without semantic drift."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .bibles import BibleBundle
from .shot_contract import ShotContract


PROMPT_COMPILER_REVISION = "media-prompt-compiler-v1"


@dataclass(frozen=True, slots=True)
class PromptRevision:
    prompt_id: str
    shot_id: str
    revision: int
    target_adapter: str
    prompt: str
    parameters: dict[str, Any]
    inherited_refs: tuple[str, ...]
    locked_intent_hash: str
    semantic_diff: dict[str, Any]
    estimated_gain: float
    added_cost: float
    compiler_revision: str = PROMPT_COMPILER_REVISION

    def to_dict(self) -> dict:
        return asdict(self)


class PromptCompiler:
    def compile(
        self,
        shot: ShotContract,
        bibles: BibleBundle,
        *,
        target_adapter: str = "simulated-media-v1",
        revision: int = 1,
        locked_overrides: tuple[str, ...] | None = None,
    ) -> PromptRevision:
        if (
            locked_overrides is not None
            and tuple(locked_overrides) != shot.locked_intent
        ):
            raise ValueError("PromptCompiler cannot modify locked creative intent")
        inherited = (
            bibles.film.bible_id,
            *(item.character_id for item in bibles.characters),
            *(item.location_id for item in bibles.locations),
            bibles.assets.registry_id,
        )
        additions = {
            "composition": shot.shot_type,
            "camera": shot.camera,
            "lighting": shot.lighting,
            "action": shot.action,
            "start_state": shot.start_state,
            "end_state": shot.end_state,
            "negative_constraints": (
                "no identity drift",
                "no wardrobe change",
                "no screen-direction reversal",
                "no unintended text",
            ),
        }
        prompt = (
            f"LOCKED CREATIVE INTENT: {' | '.join(shot.locked_intent)}\n"
            f"STYLE: {bibles.film.visual_style}; "
            f"PALETTE: {', '.join(bibles.film.color_palette)}\n"
            f"SHOT: {shot.shot_type}; ACTION: {shot.action}; "
            f"CAMERA: {shot.camera}; LIGHTING: {shot.lighting}; "
            f"START: {shot.start_state}; END: {shot.end_state}; "
            f"NEGATIVE: {', '.join(additions['negative_constraints'])}"
        )
        identity = {
            "shot": shot.shot_id,
            "revision": revision,
            "adapter": target_adapter,
            "prompt": prompt,
        }
        prompt_id = "prompt_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:28]
        return PromptRevision(
            prompt_id,
            shot.shot_id,
            revision,
            target_adapter,
            prompt,
            {
                "duration_seconds": shot.duration_seconds,
                "resolution": shot.resolution,
                "fps": shot.fps,
                "aspect_ratio": shot.aspect_ratio,
                "seed": int(
                    hashlib.sha256(shot.shot_id.encode()).hexdigest()[:8],
                    16,
                ),
                "candidate_count": shot.generation_policy["candidate_count"],
            },
            tuple(inherited),
            shot.locked_intent_hash,
            {
                "locked_changed": False,
                "added": additions,
                "removed": [],
            },
            estimated_gain=max(0.0, 0.12 / revision),
            added_cost=round(0.0025 * revision, 6),
        )

    @staticmethod
    def should_refine(
        *,
        revision: int,
        max_revisions: int,
        marginal_gain: float,
        added_cost: float,
        remaining_budget: float,
    ) -> bool:
        return (
            revision < max_revisions
            and marginal_gain >= 0.02
            and added_cost <= remaining_budget
        )

