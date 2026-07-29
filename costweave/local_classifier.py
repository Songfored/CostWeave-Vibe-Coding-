"""Local lightweight difficulty-classifier interface for v0.5.2."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .domain import RunRequest
from .planner import TaskAnalysis
from .planning_contracts import DifficultyInterval, TaskDifficultyVector


CLASSIFIER_VERSION = "heuristic-difficulty-1.0"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class LightweightTaskClassifier(ABC):
    """Replaceable local interface; implementations must not call a provider."""

    classifier_id = "abstract-local-classifier"
    version = "0"

    @abstractmethod
    def classify(
        self,
        request: RunRequest,
        analysis: TaskAnalysis,
    ) -> TaskDifficultyVector:
        raise NotImplementedError


class HeuristicDifficultyClassifier(LightweightTaskClassifier):
    classifier_id = "local-heuristic-difficulty"
    version = CLASSIFIER_VERSION

    _irreversible_terms = (
        "删除",
        "清空",
        "覆盖",
        "迁移",
        "生产环境执行",
        "发布到生产",
        "支付",
        "转账",
        "不可逆",
    )
    _novelty_terms = (
        "创新",
        "全新",
        "未知",
        "首次",
        "前沿",
        "研究",
        "推导",
        "发明",
    )

    def classify(
        self,
        request: RunRequest,
        analysis: TaskAnalysis,
    ) -> TaskDifficultyVector:
        goal = request.goal.lower()
        complexity = analysis.complexity
        modalities = max(0, len(analysis.modalities) - 1)
        context_load = min(1.0, len(goal) / 1_200)
        deadline_pressure = 0.0
        if request.deadline_seconds is not None:
            deadline_pressure = _clamp(
                (30.0 - request.deadline_seconds) / 30.0
            )
        concurrency_pressure = _clamp(
            (request.max_concurrency - 2) / 10
        )
        high_stakes = analysis.stakes == "high"
        elevated = analysis.stakes == "elevated"
        dimensions = {
            "ambiguity": _clamp(analysis.ambiguity),
            "decomposition_uncertainty": _clamp(
                analysis.uncertainty * 0.62
                + (1 - analysis.decomposition_score) * 0.38
            ),
            "novelty": _clamp(
                analysis.uncertainty * 0.45
                + sum(term in goal for term in self._novelty_terms) * 0.11
                + complexity.get("evidence", 0.0) * 0.2
            ),
            "domain_specialization": _clamp(
                complexity.get("domain", 0.0) * 0.68
                + (0.32 if high_stakes else 0.16 if elevated else 0.0)
            ),
            "cross_domain_coupling": _clamp(
                complexity.get("coordination", 0.0) * 0.68
                + max(0, len(analysis.task_types) - 1) * 0.12
            ),
            "context_multimodal_load": _clamp(
                context_load * 0.62 + modalities * 0.25
            ),
            "tool_chain_depth": _clamp(
                complexity.get("tools", 0.0) * 0.7
                + len(analysis.needs_tools) * 0.1
            ),
            "verification_difficulty": _clamp(
                complexity.get("evidence", 0.0) * 0.32
                + complexity.get("domain", 0.0) * 0.28
                + (0.28 if "safety" in analysis.task_types else 0.0)
                + (0.12 if analysis.needs_fresh_data else 0.0)
            ),
            "error_loss": _clamp(
                0.88 if high_stakes
                else 0.58 if elevated
                else 0.22 + analysis.difficulty * 0.025
            ),
            "irreversibility": _clamp(
                sum(term in goal for term in self._irreversible_terms) * 0.24
            ),
            "concurrency_deadline_pressure": _clamp(
                concurrency_pressure * 0.45 + deadline_pressure * 0.55
            ),
        }
        weights = {
            "ambiguity": 0.10,
            "decomposition_uncertainty": 0.12,
            "novelty": 0.08,
            "domain_specialization": 0.10,
            "cross_domain_coupling": 0.10,
            "context_multimodal_load": 0.07,
            "tool_chain_depth": 0.07,
            "verification_difficulty": 0.12,
            "error_loss": 0.12,
            "irreversibility": 0.07,
            "concurrency_deadline_pressure": 0.05,
        }
        expected = sum(
            dimensions[key] * weights[key] for key in weights
        )
        expected = _clamp(
            expected * 0.72 + (analysis.difficulty / 10) * 0.28
        )
        confidence = _clamp(
            analysis.estimate_confidence * 0.62
            + analysis.classification_confidence * 0.28
            + (1 - analysis.ambiguity) * 0.1
        )
        width = min(
            0.28,
            0.045
            + (1 - confidence) * 0.24
            + analysis.uncertainty * 0.08,
        )
        triggers = self._triggers(
            dimensions, analysis, request
        )
        return TaskDifficultyVector(
            dimensions=dimensions,
            overall=DifficultyInterval(
                lower=_clamp(expected - width),
                expected=expected,
                upper=_clamp(expected + width),
            ),
            confidence=confidence,
            trigger_features=tuple(triggers),
            classifier_id=self.classifier_id,
            classifier_version=self.version,
        )

    @staticmethod
    def _triggers(
        dimensions: dict[str, float],
        analysis: TaskAnalysis,
        request: RunRequest,
    ) -> list[str]:
        labels = {
            "ambiguity": "目标歧义",
            "decomposition_uncertainty": "拆解不确定性",
            "novelty": "新颖性",
            "domain_specialization": "领域专业性",
            "cross_domain_coupling": "跨域耦合",
            "context_multimodal_load": "上下文/多模态负载",
            "tool_chain_depth": "工具链深度",
            "verification_difficulty": "验收难度",
            "error_loss": "错误损失",
            "irreversibility": "不可逆性",
            "concurrency_deadline_pressure": "并发/期限压力",
        }
        ranked = sorted(
            dimensions.items(), key=lambda item: (-item[1], item[0])
        )
        triggers = [
            f"{labels[key]} {value:.0%}"
            for key, value in ranked[:4]
            if value >= 0.18
        ]
        if analysis.estimate_confidence < 0.68:
            triggers.append("原始任务判断置信度低于 68%")
        if request.deadline_seconds is not None:
            triggers.append(
                f"期限 {request.deadline_seconds:g} 秒"
            )
        return triggers or ["未发现显著高难度特征"]
