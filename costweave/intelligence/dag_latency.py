"""Replay aligned node/route service times through a slot-limited task graph."""

from __future__ import annotations

import heapq
import math
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from .common import bounded_text, finite_number, stable_digest, validate_exact_fields, validate_sha256


Scenarios = Mapping[str, Mapping[str, Mapping[str, float]]]


def validate_scenarios(scenarios: Scenarios, routes: Mapping[str, set[str]]) -> Scenarios:
    """Detach inputs and reject measurements that cannot identify a declared route."""
    if not isinstance(scenarios, Mapping) or len(scenarios) > 256:
        raise ValueError("latency_scenarios must be a mapping with at most 256 windows")
    windows = {}
    entries = 0
    for window, timings in scenarios.items():
        if bounded_text(window, "window_id") != window:
            raise ValueError("window_id must have no surrounding whitespace")
        if not isinstance(timings, Mapping):
            raise ValueError("window must map node IDs to measured routes")
        nodes = {}
        for node, measurements in timings.items():
            if node not in routes or not isinstance(measurements, Mapping):
                raise ValueError("window must refer to a declared node and its routes")
            values = {}
            for route, ms in measurements.items():
                if route not in routes[node]:
                    raise ValueError("window refers to an unknown node/route pair")
                value = finite_number(ms, "service_time_ms", minimum=0.0)
                values[route] = value
            entries += 1 + len(values)
            if entries > 65_536:
                raise ValueError("latency scenario set exceeds the observation limit")
            nodes[node] = MappingProxyType(values)
        windows[window] = MappingProxyType(nodes)
    return MappingProxyType(windows)


def scenario_dict(scenarios: Scenarios) -> dict:
    return {window: {node: dict(times) for node, times in nodes.items()} for window, nodes in scenarios.items()}


@dataclass(frozen=True, slots=True)
class DagLatencyReplay:
    scenario_set_hash: str
    scenario_count: int
    matched_scenarios: int
    status: str
    planning_overhead_ms: float
    deadline_ms: float
    slot_limit: int
    mean_ms: float | None = None
    p95_ms: float | None = None
    deadline_miss_rate: float | None = None
    peak_slots: int | None = None
    method: str = "paired-window-dag-v1"

    def __post_init__(self) -> None:
        validate_sha256(self.scenario_set_hash, "scenario_set_hash")
        if self.method != "paired-window-dag-v1":
            raise ValueError("unsupported latency replay method")
        if type(self.slot_limit) is not int or not 1 <= self.slot_limit <= 1000:
            raise ValueError("invalid slot_limit")
        if type(self.scenario_count) is not int or not 1 <= self.scenario_count <= 256:
            raise ValueError("invalid scenario_count")
        if type(self.matched_scenarios) is not int or not 0 <= self.matched_scenarios <= self.scenario_count:
            raise ValueError("invalid matched_scenarios")
        finite_number(self.planning_overhead_ms, "planning_overhead_ms", minimum=0)
        finite_number(self.deadline_ms, "deadline_ms", minimum=0)
        if self.status == "complete":
            if self.matched_scenarios != self.scenario_count:
                raise ValueError("complete replay requires every window")
            finite_number(self.mean_ms, "mean_ms", minimum=self.planning_overhead_ms)
            finite_number(self.p95_ms, "p95_ms", minimum=self.planning_overhead_ms)
            if finite_number(self.deadline_miss_rate, "deadline_miss_rate", minimum=0) > 1:
                raise ValueError("invalid deadline_miss_rate")
            if type(self.peak_slots) is not int or not 1 <= self.peak_slots <= self.slot_limit:
                raise ValueError("invalid peak_slots")
        elif self.status in ("missing_observations", "insufficient_slots"):
            if self.status == "missing_observations" and self.matched_scenarios == self.scenario_count:
                raise ValueError("missing observations require an unmatched window")
            if any(value is not None for value in (self.mean_ms, self.p95_ms, self.deadline_miss_rate, self.peak_slots)):
                raise ValueError("incomplete replay cannot report timing statistics")
        else:
            raise ValueError("unsupported latency replay status")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping) -> DagLatencyReplay:
        validate_exact_fields(value, required=frozenset(cls.__dataclass_fields__))
        return cls(**value)


class DagLatencyReplayer:
    """One replay cohort per search. All candidates use exactly the same windows.

    A service time includes the observed work and validation for that node
    and route, excluding planner time and waiting for a shared slot. Recovery
    effects absent from the supplied service times are not simulated.
    """

    def __init__(self, dependencies: Mapping[str, Sequence[str]], slots: int, scenarios: Scenarios) -> None:
        if type(slots) is not int or not 1 <= slots <= 1000:
            raise ValueError("slots must be an integer between 1 and 1000")
        if not dependencies or not scenarios:
            raise ValueError("latency replay requires a graph and at least one scenario")
        self.dependencies = dependencies
        self.children = {node: [] for node in dependencies}
        for node, parents in dependencies.items():
            for parent in parents:
                self.children[parent].append(node)
        self.slots = slots
        self.scenarios = scenarios
        self.scenario_set_hash = stable_digest(scenario_dict(scenarios), length=64)
        self._cache = {}

    def _makespan(self, times: Mapping[str, float], demands: Mapping[str, int]) -> tuple[float, int]:
        remaining = {node: len(parents) for node, parents in self.dependencies.items()}
        # Each size class exposes its first ready node. A blocked wide node
        # does not prevent a narrower node from using free slots.
        ready: dict[int, list[str]] = {}
        for node, count in remaining.items():
            if count == 0:
                heapq.heappush(ready.setdefault(demands[node], []), node)
        active = []
        clock = 0.0
        free = self.slots
        peak = 0
        completed = 0
        while ready or active:
            while free and ready:
                node = min((queue[0] for units, queue in ready.items() if units <= free), default=None)
                if node is None:
                    break
                units = demands[node]
                heapq.heappop(ready[units])
                if not ready[units]:
                    del ready[units]
                free -= units
                finish = finite_number(clock + times[node], "replayed finish time", minimum=0)
                heapq.heappush(active, (finish, node))
            peak = max(peak, self.slots - free)
            clock = active[0][0]
            while active and active[0][0] == clock:
                _, node = heapq.heappop(active)
                completed += 1
                free += demands[node]
                for child in self.children[node]:
                    remaining[child] -= 1
                    if remaining[child] == 0:
                        heapq.heappush(ready.setdefault(demands[child], []), child)
        if completed != len(times):
            raise ValueError("latency replay requires an acyclic graph")
        return clock, peak

    def _durations(self, routes: Mapping[str, str], demands: Mapping[str, int]):
        windows = []
        for timings in self.scenarios.values():
            if len(routes) == len(self.dependencies) and all(route in timings.get(node, {}) for node, route in routes.items()):
                windows.append({node: timings[node][route] for node, route in routes.items()})
        matched = len(windows)
        if matched != len(self.scenarios):
            return "missing_observations", matched, (), None
        if max(demands.values()) > self.slots:
            return "insufficient_slots", matched, (), None
        results = [self._makespan(window, demands) for window in windows]
        return "complete", matched, sorted(duration for duration, _ in results), max(peak for _, peak in results)

    def predict(self, routes: Mapping[str, str], demands: Mapping[str, int],
                planning_ms: float, deadline_ms: float) -> DagLatencyReplay:
        # Recovery and management variants often share the same node schedule.
        key = tuple(sorted((node, route, demands[node]) for node, route in routes.items()))
        if key not in self._cache:
            self._cache[key] = self._durations(routes, demands)
        status, matched, durations, peak = self._cache[key]
        count = len(self.scenarios)
        args = (self.scenario_set_hash, count, matched, status, planning_ms, deadline_ms, self.slots)
        if status != "complete":
            return DagLatencyReplay(*args)
        p95 = durations[math.ceil(0.95 * count) - 1] + planning_ms
        late = sum(ms + planning_ms > deadline_ms for ms in durations)
        mean = math.fsum(duration / count for duration in durations) + planning_ms
        return DagLatencyReplay(*args, mean, p95, late / count, peak)
