"""Image/video intent recognition and workload profiling."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum


MEDIA_PROFILE_REVISION = "media-demand-v1"


class MediaKind(StrEnum):
    NONE = "none"
    SINGLE_IMAGE = "single_image"
    IMAGE_SET = "image_set"
    SHORT_VIDEO = "short_video"
    FILM = "film"


class ProductionStrategy(StrEnum):
    AUTO = "auto_director"
    SUPERVISED = "supervised_production"
    MANUAL = "manual_director"


@dataclass(frozen=True, slots=True)
class MediaDemandProfile:
    kind: str
    is_media: bool
    requires_film_dag: bool
    duration_seconds: float
    resolution: str
    fps: int
    aspect_ratio: str
    sound_required: bool
    character_count: int
    location_count: int
    shot_count: int
    continuity_span: int
    candidate_count: int
    max_rerenders: int
    strategy: str
    locked_requirements: tuple[str, ...]
    revision: str = MEDIA_PROFILE_REVISION

    def to_dict(self) -> dict:
        return asdict(self)


class MediaIntentAnalyzer:
    VIDEO_WORDS = (
        "视频", "短片", "电影", "影片", "动画", "广告片",
        "mv", "预告片", "分镜", "镜头",
    )
    IMAGE_WORDS = (
        "图片", "图像", "海报", "插画", "封面", "单图",
        "照片", "概念图",
    )

    def analyze(self, goal: str) -> MediaDemandProfile:
        normalized = " ".join(str(goal).strip().split())
        lower = normalized.lower()
        video = any(word in lower for word in self.VIDEO_WORDS)
        image = any(word in lower for word in self.IMAGE_WORDS)
        if not video and not image:
            return MediaDemandProfile(
                MediaKind.NONE.value, False, False, 0.0,
                "", 0, "", False, 0, 0, 0, 0, 0, 0,
                ProductionStrategy.SUPERVISED.value, (),
            )

        duration = self._duration(lower) if video else 0.0
        if video and duration <= 0:
            duration = 30.0
        kind = (
            MediaKind.FILM
            if any(word in lower for word in ("电影", "影片", "完整动画"))
            and duration > 60
            else MediaKind.SHORT_VIDEO
            if video
            else MediaKind.IMAGE_SET
            if any(word in lower for word in ("图组", "多张", "系列"))
            else MediaKind.SINGLE_IMAGE
        )
        film_dag = kind in {MediaKind.SHORT_VIDEO, MediaKind.FILM}
        shot_count = (
            max(6, min(10, round(duration / 4)))
            if film_dag
            else 1
        )
        strategy = ProductionStrategy.SUPERVISED
        if any(word in lower for word in ("全自动", "自动导演")):
            strategy = ProductionStrategy.AUTO
        elif any(word in lower for word in ("手动", "逐镜头")):
            strategy = ProductionStrategy.MANUAL
        locked = tuple(
            clause.strip()
            for clause in re.split(r"[。；;\n]", normalized)
            if clause.strip()
            and any(
                word in clause
                for word in (
                    "必须", "不能", "不得", "保持", "主角",
                    "角色", "品牌", "logo", "LOGO", "风格",
                )
            )
        ) or (normalized,)
        return MediaDemandProfile(
            kind=kind.value,
            is_media=True,
            requires_film_dag=film_dag,
            duration_seconds=duration,
            resolution=(
                "3840x2160" if "4k" in lower else "1920x1080"
            ),
            fps=30 if "30fps" in lower else 24,
            aspect_ratio=(
                "9:16" if any(word in lower for word in ("竖屏", "9:16"))
                else "1:1" if "1:1" in lower
                else "16:9"
            ),
            sound_required=any(
                word in lower
                for word in ("声音", "配音", "音乐", "对白", "音效")
            ),
            character_count=max(
                1,
                len(set(re.findall(r"角色[一二三四五\d]?|主角|配角", normalized))),
            ),
            location_count=max(
                1,
                len(set(re.findall(r"场景[一二三四五\d]?|地点|室内|室外", normalized))),
            ),
            shot_count=shot_count,
            continuity_span=min(shot_count, 3 if film_dag else 1),
            candidate_count=2 if film_dag else 1,
            max_rerenders=1,
            strategy=strategy.value,
            locked_requirements=locked,
        )

    @staticmethod
    def _duration(goal: str) -> float:
        seconds = re.search(r"(\d+(?:\.\d+)?)\s*秒", goal)
        if seconds:
            return float(seconds.group(1))
        minutes = re.search(r"(\d+(?:\.\d+)?)\s*分(?:钟)?", goal)
        if minutes:
            return float(minutes.group(1)) * 60
        return 0.0

