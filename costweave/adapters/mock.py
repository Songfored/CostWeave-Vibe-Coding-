"""Deterministic failure, latency and limit simulation for adapter tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .base import AdapterCapabilities, ProviderAdapter
from ..planning_contracts import ResultEnvelope, WorkOrder


@dataclass(frozen=True, slots=True)
class MockScenario:
    outcome: str = "completed"
    latency_ms: int = 120
    cost: float = 0.0
    confidence: float = 0.82

    def __post_init__(self) -> None:
        if self.outcome not in {
            "completed",
            "failed",
            "rate_limited",
            "escalated",
        }:
            raise ValueError("unsupported mock outcome")
        if self.latency_ms < 0 or self.cost < 0:
            raise ValueError("mock measurements cannot be negative")
        if not 0 <= self.confidence <= 1:
            raise ValueError("mock confidence must be between zero and one")


class MockProvider(ProviderAdapter):
    def __init__(
        self,
        scenario: MockScenario | None = None,
        *,
        adapter_id: str = "mock-provider",
    ) -> None:
        self.scenario = scenario or MockScenario()
        self._capabilities = AdapterCapabilities(
            adapter_id=adapter_id,
            deployment="mock",
            modalities=("text", "image-metadata"),
            tools=("structured-output",),
            max_context_tokens=128_000,
            paid_calls_enabled=False,
        )
        self.calls = 0

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities

    def execute(self, work_order: WorkOrder) -> ResultEnvelope:
        self.calls += 1
        outcome = self.scenario.outcome
        escalated = outcome in {"rate_limited", "escalated"}
        digest = hashlib.sha256(
            work_order.work_order_id.encode("utf-8")
        ).hexdigest()[:16]
        accepted = outcome == "completed"
        return ResultEnvelope(
            work_order_id=work_order.work_order_id,
            status=outcome,
            output={
                "summary": (
                    "deterministic mock result"
                    if accepted
                    else "mock provider did not complete the work order"
                ),
                "receipt": digest,
            },
            evidence=({
                "kind": "mock-receipt",
                "value": digest,
                "paid_call": False,
            },),
            acceptance_results=tuple(
                {
                    "criterion": criterion,
                    "passed": accepted,
                }
                for criterion in work_order.acceptance_tests
            ),
            assumptions_used=work_order.assumptions,
            unresolved_risks=(
                ()
                if accepted
                else ("mock scenario intentionally did not complete",)
            ),
            actual_cost=self.scenario.cost,
            actual_latency_ms=self.scenario.latency_ms,
            confidence=self.scenario.confidence,
            escalation_requested=escalated,
            escalation_reason=(
                f"mock outcome requires escalation: {outcome}"
                if escalated
                else ""
            ),
        )

