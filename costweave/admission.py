"""Capacity, deadline, token-bucket and circuit admission gates."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from .resource_inventory import ResourcePool


class AdmissionCode(StrEnum):
    ADMITTED = "admitted"
    QUEUED = "queued"
    IMPOSSIBLE_CAPACITY = "impossible_capacity"
    DEADLINE_IMPOSSIBLE = "deadline_impossible"
    BACKPRESSURE = "backpressure"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    slots: int = 1
    cpu_cores: float = 1.0
    gpu_count: int = 0
    vram_mb: int = 0
    tokens: int = 0


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    accepted: bool
    code: AdmissionCode
    reason: str
    retry_after_ms: int = 0


class TokenBucket:
    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = max(1.0, float(capacity))
        self.refill_per_second = max(0.0, float(refill_per_second))
        self.tokens = self.capacity
        self.clock = clock
        self.updated_at = clock()
        self._lock = threading.RLock()

    def consume(self, amount: float) -> bool:
        with self._lock:
            now = self.clock()
            elapsed = max(0.0, now - self.updated_at)
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.refill_per_second,
            )
            self.updated_at = now
            requested = max(0.0, float(amount))
            if requested > self.tokens:
                return False
            self.tokens -= requested
            return True


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        reset_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_timeout = max(0.01, float(reset_timeout))
        self.clock = clock
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self.probe_in_flight = False
        self._lock = threading.RLock()

    def allow(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if self.opened_at is None:
                    return False
                if self.clock() - self.opened_at < self.reset_timeout:
                    return False
                self.state = CircuitState.HALF_OPEN
            if self.probe_in_flight:
                return False
            self.probe_in_flight = True
            return True

    def success(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.opened_at = None
            self.probe_in_flight = False

    def failure(self) -> None:
        with self._lock:
            self.probe_in_flight = False
            self.consecutive_failures += 1
            if (
                self.state == CircuitState.HALF_OPEN
                or self.consecutive_failures >= self.failure_threshold
            ):
                self.state = CircuitState.OPEN
                self.opened_at = self.clock()

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at,
            "probe_in_flight": self.probe_in_flight,
        }

    def restore(self, snapshot: dict) -> None:
        with self._lock:
            self.state = CircuitState(snapshot.get("state", "closed"))
            self.consecutive_failures = max(
                0, int(snapshot.get("consecutive_failures", 0))
            )
            self.opened_at = (
                float(snapshot["opened_at"])
                if snapshot.get("opened_at") is not None
                else None
            )
            self.probe_in_flight = False


class AdmissionController:
    @staticmethod
    def evaluate(
        pool: ResourcePool,
        request: ResourceRequest,
        *,
        available_slots: int,
        available_cpu: float,
        available_gpu: int,
        available_vram: int,
        queue_depth: int,
        deadline_remaining_ms: int | None,
        estimated_latency_ms: int,
        bucket: TokenBucket,
        circuit: CircuitBreaker,
    ) -> AdmissionDecision:
        if (
            request.slots > pool.slots_total
            or request.cpu_cores > pool.cpu_cores_total > 0
            or request.gpu_count > pool.gpu_count_total
            or request.vram_mb > pool.vram_mb_total
        ):
            return AdmissionDecision(
                False,
                AdmissionCode.IMPOSSIBLE_CAPACITY,
                "task resource requirements exceed pool capacity",
            )
        if (
            deadline_remaining_ms is not None
            and estimated_latency_ms > deadline_remaining_ms
        ):
            return AdmissionDecision(
                False,
                AdmissionCode.DEADLINE_IMPOSSIBLE,
                "estimated latency exceeds the remaining deadline",
            )
        if queue_depth >= pool.queue_limit:
            return AdmissionDecision(
                False,
                AdmissionCode.BACKPRESSURE,
                "resource pool queue limit reached",
                50,
            )
        if (
            request.slots > available_slots
            or request.cpu_cores > available_cpu > 0
            or request.gpu_count > available_gpu
            or request.vram_mb > available_vram
        ):
            return AdmissionDecision(
                False,
                AdmissionCode.QUEUED,
                "capacity is temporarily occupied",
                25,
            )
        if not circuit.allow():
            return AdmissionDecision(
                False,
                AdmissionCode.CIRCUIT_OPEN,
                "provider circuit is open",
                100,
            )
        if not bucket.consume(request.tokens):
            return AdmissionDecision(
                False,
                AdmissionCode.BACKPRESSURE,
                "provider token bucket is depleted",
                50,
            )
        return AdmissionDecision(True, AdmissionCode.ADMITTED, "admitted")
