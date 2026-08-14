"""Result-blind policies used by the v0.6.0 tournament harness.

Policies receive only :class:`CaseViewV1`.  They cannot inspect quality,
acceptance, cost, latency, or any response field, which prevents an accidental
oracle policy from being deployed as a benchmark candidate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from ..intelligence.common import bounded_text, unique_texts
from .contracts import CaseViewV1


def _seed(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("seed must be int")
    if not 0 <= value <= (2**31 - 1):
        raise ValueError("seed must be between 0 and 2^31-1")
    return value


def _decision(
    *,
    case: CaseViewV1,
    policy_id: str,
    selected_model_id: str | None,
    reason_codes: tuple[str, ...],
) -> "PolicyDecisionV1":
    selected = None if selected_model_id is None else bounded_text(selected_model_id, "selected_model_id")
    reasons = unique_texts(reason_codes, "reason_codes", maximum_items=32)
    if not reasons:
        raise ValueError("reason_codes must contain at least one item")
    if selected is None:
        return PolicyDecisionV1(case.case_id, policy_id, None, True, reasons)
    if selected not in case.model_ids:
        return PolicyDecisionV1(case.case_id, policy_id, None, True, reasons + ("model_unavailable",))
    return PolicyDecisionV1(case.case_id, policy_id, selected, False, reasons)


@dataclass(frozen=True, slots=True)
class PolicyDecisionV1:
    case_id: str
    policy_id: str
    selected_model_id: str | None
    abstained: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))
        if self.selected_model_id is not None:
            object.__setattr__(self, "selected_model_id", bounded_text(self.selected_model_id, "selected_model_id"))
        if not isinstance(self.abstained, bool):
            raise TypeError("abstained must be bool")
        if self.abstained and self.selected_model_id is not None:
            raise ValueError("abstained decisions must not select a model")
        if not self.abstained and self.selected_model_id is None:
            raise ValueError("non-abstained decisions must select a model")
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=32))
        if not self.reason_codes:
            raise ValueError("reason_codes must contain at least one item")

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "policy_id": self.policy_id,
            "selected_model_id": self.selected_model_id,
            "abstained": self.abstained,
            "reason_codes": list(self.reason_codes),
        }


@runtime_checkable
class TournamentPolicy(Protocol):
    @property
    def policy_id(self) -> str: ...

    def decide(self, case: CaseViewV1) -> PolicyDecisionV1: ...


@dataclass(frozen=True, slots=True)
class FixedModelPolicy:
    model_id: str
    policy_id: str = "fixed-model-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", bounded_text(self.model_id, "model_id"))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))

    def decide(self, case: CaseViewV1) -> PolicyDecisionV1:
        if not isinstance(case, CaseViewV1):
            raise TypeError("case must be CaseViewV1")
        if self.model_id not in case.model_ids:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("fixed_model_unavailable",))
        return _decision(case=case, policy_id=self.policy_id, selected_model_id=self.model_id, reason_codes=("fixed_model",))


@dataclass(frozen=True, slots=True)
class DeterministicRandomPolicy:
    seed: int
    policy_id: str = "deterministic-random-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "seed", _seed(self.seed))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))

    def decide(self, case: CaseViewV1) -> PolicyDecisionV1:
        if not isinstance(case, CaseViewV1):
            raise TypeError("case must be CaseViewV1")
        models = tuple(sorted(case.model_ids))
        if not models:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("no_models",))
        digest = hashlib.sha256(f"{self.seed}:{case.case_id}".encode("utf-8")).digest()
        selected = models[int.from_bytes(digest[:8], "big") % len(models)]
        return _decision(case=case, policy_id=self.policy_id, selected_model_id=selected, reason_codes=("deterministic_hash",))


@dataclass(frozen=True, slots=True)
class StaticTaskFamilyPolicy:
    family_to_model: Mapping[str, str]
    policy_id: str = "static-task-family-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.family_to_model, Mapping):
            raise TypeError("family_to_model must be a mapping")
        normalized: dict[str, str] = {}
        for family, model in self.family_to_model.items():
            family_name = bounded_text(family, "task_family", maximum=64)
            if family_name in normalized:
                raise ValueError("duplicate task family")
            normalized[family_name] = bounded_text(model, "model_id")
        if not normalized:
            raise ValueError("family_to_model must not be empty")
        object.__setattr__(self, "family_to_model", dict(sorted(normalized.items())))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))

    def decide(self, case: CaseViewV1) -> PolicyDecisionV1:
        if not isinstance(case, CaseViewV1):
            raise TypeError("case must be CaseViewV1")
        model = self.family_to_model.get(case.task_family)
        if model is None:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("unregistered_task_family",))
        if model not in case.model_ids:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("static_model_unavailable",))
        return _decision(case=case, policy_id=self.policy_id, selected_model_id=model, reason_codes=("static_task_family",))


@dataclass(frozen=True, slots=True)
class RecordedDecisionPolicy:
    decisions: Mapping[str, str | None]
    policy_id: str = "recorded-decision-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.decisions, Mapping):
            raise TypeError("decisions must be a mapping")
        normalized: dict[str, str | None] = {}
        for case_id, model_id in self.decisions.items():
            case = bounded_text(case_id, "case_id", maximum=200)
            if case in normalized:
                raise ValueError("duplicate case id")
            normalized[case] = None if model_id is None else bounded_text(model_id, "model_id")
        object.__setattr__(self, "decisions", dict(sorted(normalized.items())))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))

    def decide(self, case: CaseViewV1) -> PolicyDecisionV1:
        if not isinstance(case, CaseViewV1):
            raise TypeError("case must be CaseViewV1")
        if case.case_id not in self.decisions:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("record_missing",))
        model = self.decisions[case.case_id]
        if model is None:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("recorded_abstain",))
        if model not in case.model_ids:
            return _decision(case=case, policy_id=self.policy_id, selected_model_id=None, reason_codes=("recorded_model_unavailable",))
        return _decision(case=case, policy_id=self.policy_id, selected_model_id=model, reason_codes=("recorded_decision",))


__all__ = [
    "DeterministicRandomPolicy",
    "FixedModelPolicy",
    "PolicyDecisionV1",
    "RecordedDecisionPolicy",
    "StaticTaskFamilyPolicy",
    "TournamentPolicy",
]
