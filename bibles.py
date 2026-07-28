"""Versioned creative, film, character, location and asset bibles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from .intent import MediaDemandProfile


BIBLE_SCHEMA_REVISION = "media-bible-v1"


def _identity(kind: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return f"{kind}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class CreativeBrief:
    brief_id: str
    goal: str
    audience: str
    platform: str
    duration_seconds: float
    locked_requirements: tuple[str, ...]
    production_strategy: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FilmBible:
    bible_id: str
    revision: int
    visual_style: str
    color_palette: tuple[str, ...]
    cinematography: tuple[str, ...]
    editing_rules: tuple[str, ...]
    sound_rules: tuple[str, ...]
    locked_intent_hash: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CharacterBible:
    character_id: str
    revision: int
    identity: str
    appearance: tuple[str, ...]
    wardrobe: tuple[str, ...]
    immutable_traits: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LocationBible:
    location_id: str
    revision: int
    identity: str
    lighting: str
    orientation: tuple[str, ...]
    props: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AssetRegistry:
    registry_id: str
    revision: int
    assets: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BibleBundle:
    brief: CreativeBrief
    film: FilmBible
    characters: tuple[CharacterBible, ...]
    locations: tuple[LocationBible, ...]
    assets: AssetRegistry

    def to_dict(self) -> dict:
        return {
            "brief": self.brief.to_dict(),
            "film": self.film.to_dict(),
            "characters": [item.to_dict() for item in self.characters],
            "locations": [item.to_dict() for item in self.locations],
            "assets": self.assets.to_dict(),
            "schema_revision": BIBLE_SCHEMA_REVISION,
        }


class BibleBuilder:
    def build(self, goal: str, profile: MediaDemandProfile) -> BibleBundle:
        lock_payload = {
            "goal": goal,
            "locked_requirements": profile.locked_requirements,
        }
        lock_hash = hashlib.sha256(
            json.dumps(
                lock_payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        ).hexdigest()
        brief_data = {
            "goal": goal,
            "duration": profile.duration_seconds,
            "strategy": profile.strategy,
            "locked": profile.locked_requirements,
        }
        brief = CreativeBrief(
            _identity("brief", brief_data),
            goal,
            "目标平台受众",
            "vertical-social" if profile.aspect_ratio == "9:16" else "general-screen",
            profile.duration_seconds,
            profile.locked_requirements,
            profile.strategy,
        )
        film = FilmBible(
            _identity("film_bible", {**brief_data, "revision": 1}),
            1,
            "cinematic coherent realism",
            ("deep teal", "warm amber", "neutral skin"),
            ("motivated camera movement", "consistent lens language"),
            ("preserve screen direction", "cut on motivated action"),
            ("dialogue intelligible", "music does not mask key action"),
            lock_hash,
        )
        characters = tuple(
            CharacterBible(
                _identity("character", {"index": index, "lock": lock_hash}),
                1,
                f"character-{index:02d}",
                ("stable facial identity", "consistent body proportions"),
                ("locked primary wardrobe",),
                profile.locked_requirements,
            )
            for index in range(1, profile.character_count + 1)
        )
        locations = tuple(
            LocationBible(
                _identity("location", {"index": index, "lock": lock_hash}),
                1,
                f"location-{index:02d}",
                "motivated light with stable time of day",
                ("screen-left", "screen-right", "camera axis"),
                ("continuity prop",),
            )
            for index in range(1, profile.location_count + 1)
        )
        assets = AssetRegistry(
            _identity("asset_registry", {"lock": lock_hash}),
            1,
            tuple(
                {
                    "asset_id": f"reference-{index:02d}",
                    "kind": "character_reference",
                    "sensitivity": "internal",
                    "license_status": "user-review-required",
                }
                for index in range(1, len(characters) + 1)
            ),
        )
        return BibleBundle(brief, film, characters, locations, assets)

