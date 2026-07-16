from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunMode(StrEnum):
    ECONOMY = "economy"
    BALANCED = "balanced"
    TURBO = "turbo"


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VALIDATED = "validated"
    SUSPECT = "suspect"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


class ValidationLevel(StrEnum):
    SCHEMA = "L0-schema"
    SEMANTIC = "L1-semantic"
    SPECIALIST = "L2-specialist"
    SENIOR = "L3-senior"


@dataclass(slots=True)
class RunRequest:
    goal: str
    mode: RunMode = RunMode.BALANCED
    budget: float = 1.0
    quality_floor: float = 0.78
    max_concurrency: int = 4
    simulate_replan: bool = False

    def normalized(self) -> "RunRequest":
        return RunRequest(
            goal=" ".join(self.goal.strip().split()),
            mode=self.mode,
            budget=max(0.05, min(float(self.budget), 100.0)),
            quality_floor=max(0.5, min(float(self.quality_floor), 0.99)),
            max_concurrency=max(1, min(int(self.max_concurrency), 12)),
            simulate_replan=bool(self.simulate_replan),
        )


@dataclass(slots=True)
class WorkerProfile:
    id: str
    name: str
    specialty: str
    capabilities: dict[str, float]
    cost_per_task: float
    latency_factor: float
    reliability: float
    local: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TaskContract:
    id: str
    title: str
    objective: str
    task_type: str
    dependencies: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    output_schema: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    priority: int = 5
    parallel_group: str = "default"
    selected_worker: str | None = None
    predicted_success: float = 0.0
    estimated_cost: float = 0.0
    estimated_latency_ms: int = 0
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    result: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    plan_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(slots=True)
class Plan:
    version: int
    difficulty: int
    task_types: list[str]
    rationale: str
    tasks: list[TaskContract]
    predicted_success: float
    estimate_confidence: float
    risks: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "difficulty": self.difficulty,
            "task_types": self.task_types,
            "rationale": self.rationale,
            "predicted_success": round(self.predicted_success, 4),
            "estimate_confidence": round(self.estimate_confidence, 4),
            "risks": self.risks,
            "tasks": [task.to_dict() for task in self.tasks],
        }


@dataclass(slots=True)
class Event:
    at: str
    kind: str
    message: str
    task_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

