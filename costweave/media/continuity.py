"""Deterministic technical and cross-shot continuity checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .shot_contract import ShotContract


CONTINUITY_REVISION = "media-continuity-v1"


@dataclass(frozen=True, slots=True)
class ContinuityResult:
    check_id: str
    shot_id: str
    passed: bool
    technical_passed: bool
    identity_passed: bool
    location_passed: bool
    action_passed: bool
    boundary_passed: bool
    defects: tuple[str, ...]
    suspect_shots: tuple[str, ...]
    revision: str = CONTINUITY_REVISION

    def to_dict(self) -> dict:
        return asdict(self)


class ContinuityValidator:
    def validate(
        self,
        shot: ShotContract,
        artifact: dict[str, Any],
        *,
        previous_artifact: dict[str, Any] | None = None,
        next_shot_id: str | None = None,
    ) -> ContinuityResult:
        technical = (
            artifact.get("resolution") == shot.resolution
            and int(artifact.get("fps", 0)) == shot.fps
            and abs(
                float(artifact.get("duration_seconds", 0))
                - shot.duration_seconds
            ) <= 0.05
        )
        identity = tuple(artifact.get("character_refs", ())) == shot.character_refs
        location = artifact.get("location_ref") == shot.location_ref
        action = artifact.get("action") == shot.action
        boundary = (
            previous_artifact is None
            or not shot.reference_dependencies
            or previous_artifact.get("end_state")
            == artifact.get("start_state")
        )
        defects = tuple(
            label
            for passed, label in (
                (technical, "technical_format"),
                (identity, "identity_drift"),
                (location, "location_drift"),
                (action, "action_direction"),
                (boundary, "boundary_discontinuity"),
            )
            if not passed
        )
        suspects = []
        if "boundary_discontinuity" in defects:
            suspects.extend(shot.reference_dependencies)
            if next_shot_id:
                suspects.append(next_shot_id)
        raw = {
            "shot": shot.shot_id,
            "artifact": artifact.get("artifact_id"),
            "defects": defects,
        }
        check_id = "continuity_" + hashlib.sha256(
            json.dumps(raw, sort_keys=True).encode("utf-8")
        ).hexdigest()[:26]
        return ContinuityResult(
            check_id,
            shot.shot_id,
            not defects,
            technical,
            identity,
            location,
            action,
            boundary,
            defects,
            tuple(sorted(set(suspects))),
        )

    @staticmethod
    def affected_neighbors(
        shots: tuple[ShotContract, ...],
        failed_shot_id: str,
        *,
        boundary_changed: bool,
    ) -> set[str]:
        affected = {failed_shot_id}
        if not boundary_changed:
            return affected
        for shot in shots:
            if failed_shot_id in shot.reference_dependencies:
                affected.add(shot.shot_id)
            if shot.shot_id == failed_shot_id:
                affected.update(shot.reference_dependencies)
        return affected

