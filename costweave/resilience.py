"""Deterministic stress, fault, deadline and recovery contracts for v0.5.8.3."""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .admission import CircuitBreaker, CircuitState


RESILIENCE_SCHEMA_VERSION = "v0.5.8.3"
RESILIENCE_POLICY_REVISION = "stress-fault-migration-1"


class FaultKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_DOWN = "provider_down"
    OUT_OF_MEMORY = "out_of_memory"
    PROCESS_CRASH = "process_crash"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AMBIGUOUS_SIDE_EFFECT = "ambiguous_side_effect"


class FaultSignal(RuntimeError):
    """A typed, synthetic fault. It is never enabled by default."""

    def __init__(
        self,
        kind: FaultKind,
        *,
        point: str,
        provider: str | None = None,
        retry_after_ms: int = 0,
    ) -> None:
        super().__init__(f"injected {kind.value} at {point}")
        self.kind = FaultKind(kind)
        self.point = str(point)
        self.provider = provider
        self.retry_after_ms = max(0, int(retry_after_ms))

    @property
    def safe_to_failover(self) -> bool:
        return self.kind != FaultKind.AMBIGUOUS_SIDE_EFFECT


@dataclass(frozen=True, slots=True)
class FaultDirective:
    point: str
    kind: FaultKind
    trigger_on_call: int = 1
    repeat: int = 1
    provider: str | None = None
    retry_after_ms: int = 0

    def normalized(self) -> "FaultDirective":
        point = " ".join(str(self.point).strip().split())
        if not point:
            raise ValueError("fault point is required")
        return FaultDirective(
            point=point,
            kind=FaultKind(self.kind),
            trigger_on_call=max(1, int(self.trigger_on_call)),
            repeat=max(1, min(int(self.repeat), 10_000)),
            provider=(
                " ".join(str(self.provider).strip().split())
                if self.provider
                else None
            ),
            retry_after_ms=max(0, min(int(self.retry_after_ms), 300_000)),
        )


class DeterministicFaultInjector:
    """Thread-safe, call-count based fault injection for offline campaigns."""

    def __init__(
        self,
        directives: Iterable[FaultDirective] = (),
        *,
        enabled: bool = False,
    ) -> None:
        self.enabled = bool(enabled)
        self.directives = tuple(item.normalized() for item in directives)
        self._calls: dict[tuple[str, str | None], int] = {}
        self._lock = threading.RLock()

    def hit(self, point: str, *, provider: str | None = None) -> None:
        if not self.enabled:
            return
        key = (str(point), provider)
        with self._lock:
            ordinal = self._calls.get(key, 0) + 1
            self._calls[key] = ordinal
            for directive in self.directives:
                if directive.point != point:
                    continue
                if directive.provider not in {None, provider}:
                    continue
                first = directive.trigger_on_call
                if first <= ordinal < first + directive.repeat:
                    raise FaultSignal(
                        directive.kind,
                        point=point,
                        provider=provider,
                        retry_after_ms=directive.retry_after_ms,
                    )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "directives": [
                    {
                        "point": item.point,
                        "kind": item.kind.value,
                        "trigger_on_call": item.trigger_on_call,
                        "repeat": item.repeat,
                        "provider": item.provider,
                        "retry_after_ms": item.retry_after_ms,
                    }
                    for item in self.directives
                ],
                "calls": {
                    f"{point}:{provider or '*'}": count
                    for (point, provider), count in sorted(self._calls.items())
                },
            }


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    provider: str
    operation: Callable[[], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class FailoverReceipt:
    selected_provider: str
    attempted_providers: tuple[str, ...]
    failure_kinds: tuple[str, ...]
    open_providers: tuple[str, ...]
    failover_count: int
    elapsed_ms: int
    within_30_seconds: bool
    evidence_level: str = "E0-simulated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_provider": self.selected_provider,
            "attempted_providers": list(self.attempted_providers),
            "failure_kinds": list(self.failure_kinds),
            "open_providers": list(self.open_providers),
            "failover_count": self.failover_count,
            "elapsed_ms": self.elapsed_ms,
            "within_30_seconds": self.within_30_seconds,
            "evidence_level": self.evidence_level,
        }


class ProviderFailoverController:
    """Fail fast across bounded routes without replaying ambiguous effects."""

    def __init__(
        self,
        *,
        failure_threshold: int = 1,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_timeout = max(0.01, float(reset_timeout))
        self.clock = clock
        self.circuits: dict[str, CircuitBreaker] = {}

    def _circuit(self, provider: str) -> CircuitBreaker:
        if provider not in self.circuits:
            self.circuits[provider] = CircuitBreaker(
                failure_threshold=self.failure_threshold,
                reset_timeout=self.reset_timeout,
                clock=self.clock,
            )
        return self.circuits[provider]

    async def execute(
        self,
        routes: Sequence[ProviderRoute],
        *,
        deadline_seconds: float = 30.0,
        side_effect_replay_safe: bool = True,
    ) -> tuple[Any, FailoverReceipt]:
        if not routes:
            raise ValueError("at least one provider route is required")
        deadline_seconds = max(0.001, min(float(deadline_seconds), 30.0))
        started = self.clock()
        attempted: list[str] = []
        failures: list[str] = []
        last_fault: FaultSignal | None = None
        for route in routes:
            if self.clock() - started > deadline_seconds:
                last_fault = FaultSignal(
                    FaultKind.DEADLINE_EXCEEDED,
                    point="provider_failover",
                    provider=route.provider,
                )
                break
            circuit = self._circuit(route.provider)
            if not circuit.allow():
                continue
            attempted.append(route.provider)
            try:
                result = route.operation()
                if inspect.isawaitable(result):
                    result = await result
                circuit.success()
                elapsed_ms = max(0, round((self.clock() - started) * 1000))
                open_providers = tuple(
                    sorted(
                        provider
                        for provider, item in self.circuits.items()
                        if item.state == CircuitState.OPEN
                    )
                )
                return result, FailoverReceipt(
                    selected_provider=route.provider,
                    attempted_providers=tuple(attempted),
                    failure_kinds=tuple(failures),
                    open_providers=open_providers,
                    failover_count=max(0, len(attempted) - 1),
                    elapsed_ms=elapsed_ms,
                    within_30_seconds=elapsed_ms <= 30_000,
                )
            except FaultSignal as exc:
                last_fault = exc
                failures.append(exc.kind.value)
                circuit.failure()
                if not exc.safe_to_failover or not side_effect_replay_safe:
                    raise
        if last_fault is not None:
            raise last_fault
        raise RuntimeError("all provider circuits are open")


@dataclass(frozen=True, slots=True)
class DeadlineSample:
    queue_wait_ms: int
    service_p95_ms: int
    deadline_remaining_ms: int
    capacity_pressure: float
    observed_miss: bool


@dataclass(frozen=True, slots=True)
class DeadlineEvaluation:
    sample_size: int
    positives: int
    recall: float | None
    auroc: float | None
    threshold: float
    evidence_level: str

    @property
    def meets_simulation_gate(self) -> bool:
        return bool(
            self.recall is not None
            and self.auroc is not None
            and self.recall >= 0.90
            and self.auroc >= 0.85
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "positives": self.positives,
            "recall": self.recall,
            "auroc": self.auroc,
            "threshold": self.threshold,
            "meets_simulation_gate": self.meets_simulation_gate,
            "evidence_level": self.evidence_level,
        }


class DeadlineRiskPredictor:
    """Transparent warning score; it is an evaluation baseline, not a model."""

    @staticmethod
    def score(sample: DeadlineSample) -> float:
        remaining = max(1, int(sample.deadline_remaining_ms))
        pressure = max(0.0, min(float(sample.capacity_pressure), 2.0))
        predicted = max(0, int(sample.queue_wait_ms)) + max(
            0, int(sample.service_p95_ms)
        ) * (1.0 + 0.35 * pressure)
        ratio = predicted / remaining
        return round(1.0 / (1.0 + math.exp(-5.0 * (ratio - 1.0))), 8)

    @classmethod
    def evaluate(
        cls,
        samples: Sequence[DeadlineSample],
        *,
        threshold: float = 0.5,
        evidence_level: str = "E0-simulated",
    ) -> DeadlineEvaluation:
        if not samples:
            return DeadlineEvaluation(0, 0, None, None, threshold, evidence_level)
        scored = [(cls.score(item), bool(item.observed_miss)) for item in samples]
        positives = [score for score, label in scored if label]
        negatives = [score for score, label in scored if not label]
        true_positive = sum(score >= threshold for score in positives)
        recall = true_positive / len(positives) if positives else None
        if positives and negatives:
            concordant = 0.0
            for positive in positives:
                for negative in negatives:
                    if positive > negative:
                        concordant += 1.0
                    elif positive == negative:
                        concordant += 0.5
            auroc = concordant / (len(positives) * len(negatives))
        else:
            auroc = None
        return DeadlineEvaluation(
            sample_size=len(samples),
            positives=len(positives),
            recall=(round(recall, 6) if recall is not None else None),
            auroc=(round(auroc, 6) if auroc is not None else None),
            threshold=threshold,
            evidence_level=evidence_level,
        )


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    terminal_run: bool
    restart_task_ids: tuple[str, ...]
    preserve_task_ids: tuple[str, ...]
    preserve_artifact_ids: tuple[str, ...]
    invalidated_artifact_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "terminal_run": self.terminal_run,
            "restart_task_ids": list(self.restart_task_ids),
            "preserve_task_ids": list(self.preserve_task_ids),
            "preserve_artifact_ids": list(self.preserve_artifact_ids),
            "invalidated_artifact_ids": list(self.invalidated_artifact_ids),
            "reason": self.reason,
        }


class RecoveryPlanner:
    TERMINAL_RUN_STATES = frozenset({"completed", "cancelled", "failed"})

    @classmethod
    def build(
        cls,
        *,
        run_status: str,
        tasks: Sequence[Mapping[str, Any]],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> RecoveryPlan:
        terminal = str(run_status) in cls.TERMINAL_RUN_STATES
        if terminal:
            return RecoveryPlan(
                terminal_run=True,
                restart_task_ids=(),
                preserve_task_ids=tuple(
                    sorted(str(item.get("id") or item.get("task_id")) for item in tasks)
                ),
                preserve_artifact_ids=tuple(
                    sorted(str(item.get("id") or item.get("artifact_id")) for item in artifacts)
                ),
                invalidated_artifact_ids=(),
                reason="terminal runs are never restarted automatically",
            )
        valid_artifacts: list[str] = []
        invalid_artifacts: list[str] = []
        contaminated_producers: set[str] = set()
        for item in artifacts:
            artifact_id = str(item.get("id") or item.get("artifact_id"))
            producer = str(item.get("producer_task_id") or "")
            status = str(item.get("status") or "")
            polluted = bool(item.get("polluted") or item.get("contaminated"))
            if status == "validated" and not polluted:
                valid_artifacts.append(artifact_id)
            else:
                invalid_artifacts.append(artifact_id)
                if producer:
                    contaminated_producers.add(producer)
        preserve: list[str] = []
        restart: list[str] = []
        for item in tasks:
            task_id = str(item.get("id") or item.get("task_id"))
            status = str(item.get("status") or "pending")
            if status == "validated" and task_id not in contaminated_producers:
                preserve.append(task_id)
            else:
                restart.append(task_id)
        return RecoveryPlan(
            terminal_run=False,
            restart_task_ids=tuple(sorted(restart)),
            preserve_task_ids=tuple(sorted(preserve)),
            preserve_artifact_ids=tuple(sorted(valid_artifacts)),
            invalidated_artifact_ids=tuple(sorted(invalid_artifacts)),
            reason=(
                "validated, unpolluted work is preserved; only incomplete or "
                "polluted work is restarted"
            ),
        )


@dataclass(frozen=True, slots=True)
class StressReport:
    operations: int
    concurrency: int
    succeeded: int
    failed: int
    peak_in_flight: int
    elapsed_ms: int
    leaked_workers: int
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operations": self.operations,
            "concurrency": self.concurrency,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "peak_in_flight": self.peak_in_flight,
            "elapsed_ms": self.elapsed_ms,
            "leaked_workers": self.leaked_workers,
            "errors": list(self.errors),
            "evidence_level": "E0-simulated",
        }


class BoundedStressHarness:
    """Run bounded local operations; never makes provider calls itself."""

    @staticmethod
    def run(
        operation: Callable[[int], Any],
        *,
        operations: int = 100,
        concurrency: int = 8,
    ) -> StressReport:
        operations = max(1, min(int(operations), 10_000))
        concurrency = max(1, min(int(concurrency), 64, operations))
        in_flight = 0
        peak = 0
        lock = threading.RLock()
        errors: list[str] = []

        def guarded(index: int) -> Any:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                return operation(index)
            finally:
                with lock:
                    in_flight -= 1

        started = time.monotonic()
        succeeded = 0
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="costweave-stress",
        ) as executor:
            futures = [executor.submit(guarded, index) for index in range(operations)]
            for future in as_completed(futures):
                try:
                    future.result()
                    succeeded += 1
                except Exception as exc:  # the report must retain injected failures
                    errors.append(f"{type(exc).__name__}:{exc}")
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        leaked = sum(
            thread.is_alive() and thread.name.startswith("costweave-stress")
            for thread in threading.enumerate()
        )
        return StressReport(
            operations=operations,
            concurrency=concurrency,
            succeeded=succeeded,
            failed=operations - succeeded,
            peak_in_flight=peak,
            elapsed_ms=elapsed_ms,
            leaked_workers=leaked,
            errors=tuple(errors[:100]),
        )


def public_resilience_policy() -> dict[str, Any]:
    return {
        "schema_version": RESILIENCE_SCHEMA_VERSION,
        "policy_revision": RESILIENCE_POLICY_REVISION,
        "fault_injection_default_enabled": False,
        "real_provider_faults_triggered": False,
        "campaign_evidence_level": "E0-simulated",
        "provider_failover_deadline_seconds": 30,
        "deadline_warning_gate": {"recall": 0.90, "auroc": 0.85},
        "terminal_runs_restart_automatically": False,
        "validated_unpolluted_work_reexecuted": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "RESILIENCE_SCHEMA_VERSION",
    "RESILIENCE_POLICY_REVISION",
    "FaultKind",
    "FaultSignal",
    "FaultDirective",
    "DeterministicFaultInjector",
    "ProviderRoute",
    "FailoverReceipt",
    "ProviderFailoverController",
    "DeadlineSample",
    "DeadlineEvaluation",
    "DeadlineRiskPredictor",
    "RecoveryPlan",
    "RecoveryPlanner",
    "StressReport",
    "BoundedStressHarness",
    "public_resilience_policy",
]
