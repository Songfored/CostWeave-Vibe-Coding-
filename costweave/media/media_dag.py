"""Film project and shot-dependency construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .bibles import BibleBuilder, BibleBundle
from .intent import MediaDemandProfile
from .shot_contract import ShotContract


MEDIA_DAG_REVISION = "media-dag-v1"


@dataclass(frozen=True, slots=True)
class MediaProject:
    project_id: str
    run_id: str
    profile: MediaDemandProfile
    bibles: BibleBundle
    shots: tuple[ShotContract, ...]
    workflow_kind: str
    stages: tuple[str, ...]
    status: str
    revision: str = MEDIA_DAG_REVISION

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "profile": self.profile.to_dict(),
            "bibles": self.bibles.to_dict(),
            "shots": [shot.to_dict() for shot in self.shots],
            "workflow_kind": self.workflow_kind,
            "stages": list(self.stages),
            "status": self.status,
            "revision": self.revision,
        }


class MediaProjectBuilder:
    def __init__(self) -> None:
        self.bible_builder = BibleBuilder()

    def build(
        self,
        *,
        run_id: str,
        goal: str,
        profile: MediaDemandProfile,
    ) -> MediaProject:
        if not profile.is_media:
            raise ValueError("cannot build media project for non-media goal")
        bibles = self.bible_builder.build(goal, profile)
        count = profile.shot_count
        duration = (
            profile.duration_seconds / count
            if profile.requires_film_dag
            else 1.0
        )
        shot_prefix = hashlib.sha256(
            run_id.encode("utf-8")
        ).hexdigest()[:24]
        shots = []
        previous_end = "scene-established"
        for index in range(1, count + 1):
            shot_id = f"shot_{shot_prefix}_{index:03d}"
            dependencies = (
                (f"shot_{shot_prefix}_{index - 1:03d}",)
                if index > 1 and index % 3 == 0
                else ()
            )
            start_state = previous_end if index > 1 else f"beat-{index}-start"
            end_state = f"beat-{index}-end"
            shots.append(
                ShotContract(
                    shot_id=shot_id,
                    sequence=index,
                    duration_seconds=round(duration, 4),
                    resolution=profile.resolution,
                    fps=profile.fps,
                    aspect_ratio=profile.aspect_ratio,
                    shot_type=(
                        "establishing"
                        if index == 1
                        else "medium_close_up"
                        if index % 2 == 0
                        else "tracking_medium"
                    ),
                    character_refs=tuple(
                        item.character_id for item in bibles.characters
                    ),
                    location_ref=bibles.locations[
                        (index - 1) % len(bibles.locations)
                    ].location_id,
                    asset_refs=tuple(
                        item["asset_id"] for item in bibles.assets.assets
                    ),
                    reference_dependencies=dependencies,
                    action=f"story beat {index}",
                    camera="motivated slow movement",
                    lighting=bibles.locations[
                        (index - 1) % len(bibles.locations)
                    ].lighting,
                    start_state=start_state,
                    end_state=end_state,
                    acceptance_criteria=(
                        "technical format valid",
                        "character identity consistent",
                        "location and action direction consistent",
                        "start and end frames connect",
                    ),
                    locked_intent=profile.locked_requirements,
                    generation_policy={
                        "candidate_count": profile.candidate_count,
                        "max_rerenders": profile.max_rerenders,
                        "preview_required": profile.requires_film_dag,
                        "final_render_requires_approval": (
                            profile.strategy != "auto_director"
                        ),
                        "boundary_predecessor": (
                            f"shot_{shot_prefix}_{index - 1:03d}"
                            if index > 1
                            else None
                        ),
                    },
                )
            )
            previous_end = end_state
        identity = {
            "run_id": run_id,
            "profile": profile.to_dict(),
            "film_bible": bibles.film.bible_id,
        }
        project_id = "media_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True).encode("utf-8")
        ).hexdigest()[:28]
        return MediaProject(
            project_id,
            run_id,
            profile,
            bibles,
            tuple(shots),
            "film_dag" if profile.requires_film_dag else "single_image",
            (
                (
                    "creative_brief",
                    "bibles",
                    "shot_contracts",
                    "keyframes",
                    "animatic",
                    "preview_approval",
                    "final_render",
                    "continuity",
                    "assembly",
                )
                if profile.requires_film_dag
                else ("creative_brief", "prompt", "image_render", "validation")
            ),
            "awaiting_preview_approval"
            if profile.requires_film_dag
            and profile.strategy != "auto_director"
            else "ready",
        )

    @staticmethod
    def ready_shots(
        shots: tuple[ShotContract, ...],
        completed: set[str],
    ) -> list[str]:
        return [
            shot.shot_id
            for shot in shots
            if shot.shot_id not in completed
            and set(shot.reference_dependencies) <= completed
        ]
