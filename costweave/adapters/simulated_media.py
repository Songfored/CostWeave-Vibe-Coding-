"""Deterministic metadata-only media adapter; creates no real media."""

from __future__ import annotations

import hashlib
from typing import Any

from ..media.shot_contract import ShotContract


class PreviewApprovalRequired(PermissionError):
    pass


class SimulatedMediaAdapter:
    adapter_id = "simulated-media-v1"

    def render(
        self,
        shot: ShotContract,
        *,
        stage: str,
        attempt: int = 1,
        preview_approved: bool = False,
        inject_failure: str | None = None,
    ) -> dict[str, Any]:
        if stage not in {"keyframe", "animatic", "final"}:
            raise ValueError("unknown media render stage")
        if (
            stage == "final"
            and shot.generation_policy.get(
                "final_render_requires_approval", False
            )
            and not preview_approved
        ):
            raise PreviewApprovalRequired(
                "final render requires approved animatic"
            )
        artifact_id = "media_artifact_" + hashlib.sha256(
            f"{shot.shot_id}:{stage}:{attempt}".encode("utf-8")
        ).hexdigest()[:26]
        characters = list(shot.character_refs)
        location = shot.location_ref
        action = shot.action
        start_state = shot.start_state
        if inject_failure == "identity" and characters:
            characters[0] = "drifted-character"
        elif inject_failure == "location":
            location = "drifted-location"
        elif inject_failure == "action":
            action = "reversed-action"
        elif inject_failure == "boundary":
            start_state = "incompatible-start"
        scale = {"keyframe": 0.12, "animatic": 0.32, "final": 1.0}[stage]
        pixel_factor = (
            int(shot.resolution.split("x")[0])
            * int(shot.resolution.split("x")[1])
            / (1920 * 1080)
        )
        return {
            "artifact_id": artifact_id,
            "shot_id": shot.shot_id,
            "stage": stage,
            "attempt": attempt,
            "simulated": True,
            "contains_real_media": False,
            "duration_seconds": shot.duration_seconds,
            "resolution": shot.resolution,
            "fps": shot.fps,
            "character_refs": characters,
            "location_ref": location,
            "action": action,
            "start_state": start_state,
            "end_state": shot.end_state,
            "estimated_cost": round(
                0.018
                * shot.duration_seconds
                * pixel_factor
                * shot.generation_policy["candidate_count"]
                * scale,
                8,
            ),
        }
