"""Provider-neutral adapter interface for planning work orders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

from ..planning_contracts import ResultEnvelope, WorkOrder


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    adapter_id: str
    deployment: str
    modalities: tuple[str, ...] = ("text",)
    tools: tuple[str, ...] = ()
    max_context_tokens: int = 0
    paid_calls_enabled: bool = False

    def __post_init__(self) -> None:
        if self.deployment not in {"mock", "local", "remote"}:
            raise ValueError("unsupported adapter deployment")

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["modalities"] = list(self.modalities)
        data["tools"] = list(self.tools)
        return data


class ProviderAdapter(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        raise NotImplementedError

    @abstractmethod
    def execute(self, work_order: WorkOrder) -> ResultEnvelope:
        raise NotImplementedError

