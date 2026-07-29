from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunMode(StrEnum):
    ECONOMY = "economy"
    BALANCED = "balanced"
    TURBO = "turbo"
    QUALITY = "quality"


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    EXECUTING = "executing"
    REPLANNING = "replanning"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
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
    deadline_seconds: float | None = None
    recovery_budget_ratio: float = 0.2
    value_of_time_per_second: float | None = None
    max_recoveries: int = 2
    max_task_attempts: int = 2
    recovery_time_limit_seconds: float = 30.0

    def normalized(self) -> "RunRequest":
        return RunRequest(
            goal=" ".join(self.goal.strip().split()),
            mode=self.mode,
            budget=max(0.05, min(float(self.budget), 100.0)),
            quality_floor=max(0.5, min(float(self.quality_floor), 0.99)),
            max_concurrency=max(1, min(int(self.max_concurrency), 12)),
            simulate_replan=bool(self.simulate_replan),
            deadline_seconds=(
                max(0.05, min(float(self.deadline_seconds), 86_400.0))
                if self.deadline_seconds is not None
                else None
            ),
            recovery_budget_ratio=max(
                0.0, min(float(self.recovery_budget_ratio), 0.5)
            ),
            value_of_time_per_second=(
                max(
                    0.0,
                    min(float(self.value_of_time_per_second), 10.0),
                )
                if self.value_of_time_per_second is not None
                else None
            ),
            max_recoveries=max(0, min(int(self.max_recoveries), 8)),
            max_task_attempts=max(
                1, min(int(self.max_task_attempts), 5)
            ),
            recovery_time_limit_seconds=max(
                0.1,
                min(
                    float(self.recovery_time_limit_seconds),
                    86_400.0,
                ),
            ),
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
    provider: str = "local"
    model_id: str | None = None
    tier: str = "utility"
    reasoning: float = 0.5
    speed: float = 0.5
    context_window: int = 32_000
    max_output_tokens: int = 8_000
    input_price_per_mtok: float = 0.0
    cached_input_price_per_mtok: float | None = None
    output_price_per_mtok: float = 0.0
    pricing_currency: str = "USD"
    modalities: list[str] = field(default_factory=lambda: ["text"])
    tools: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    source_url: str = ""
    verified_at: str = ""
    preview: bool = False
    routable: bool = True
    data_confidence: float = 0.8
    availability: float = 0.99
    custom: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TaskContract:
    id: str
    title: str
    objective: str
    task_type: str
    dependencies: list[str] = field(default_factory=list)
    dependency_types: dict[str, str] = field(default_factory=dict)
    input_artifact_refs: list[str] = field(default_factory=list)
    input_artifact_schemas: dict[str, str] = field(default_factory=dict)
    output_artifact_specs: list[dict[str, Any]] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    handoff_message_id: str | None = None
    data_permissions: list[str] = field(
        default_factory=lambda: ["public", "internal"]
    )
    resource_slots: int = 1
    cpu_cores: float = 1.0
    gpu_count: int = 0
    vram_mb: int = 0
    requires_approval: bool = False
    approval_kind: str = "task-execution"
    approval_reason: str = ""
    approval_id: str | None = None
    queue_state: str = "pending"
    blocked_reason: str = ""
    reservation_id: str | None = None
    transient_retries: int = 0
    placement_id: str | None = None
    placement: dict[str, Any] = field(default_factory=dict)
    backup_placements: list[dict[str, Any]] = field(
        default_factory=list
    )
    inference_profile_revision: str | None = None
    direct_model_cost: float = 0.0
    forecast_cost_breakdown: dict[str, float] = field(
        default_factory=dict
    )
    critical_criteria: list[str] = field(default_factory=list)
    success_contract: dict[str, Any] = field(default_factory=dict)
    failure_record: dict[str, Any] | None = None
    recovery_history: list[dict[str, Any]] = field(
        default_factory=list
    )
    required_capabilities: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    output_schema: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    priority: int = 5
    parallel_group: str = "default"
    difficulty: int = 1
    risk_level: str = "normal"
    required_modalities: list[str] = field(default_factory=lambda: ["text"])
    requires_tools: list[str] = field(default_factory=list)
    requires_freshness: bool = False
    estimated_input_tokens: int = 4_000
    estimated_output_tokens: int = 1_500
    capability_weights: dict[str, float] = field(default_factory=dict)
    uncertainty: float = 0.0
    criticality: float = 0.5
    classification_confidence: float = 0.75
    escalation_policy: str = "evidence-triggered"
    handoff_prompt: str = ""
    selected_worker: str | None = None
    predicted_success: float = 0.0
    predicted_success_lower_bound: float = 0.0
    estimated_cost: float = 0.0
    estimated_latency_ms: int = 0
    routing_rationale: str = ""
    routing_candidates: list[dict[str, Any]] = field(default_factory=list)
    routing_rejections: list[dict[str, Any]] = field(default_factory=list)
    route_confidence: float = 0.0
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
    analysis: dict[str, Any] = field(default_factory=dict)
    routing_summary: dict[str, Any] = field(default_factory=dict)
    model_snapshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "difficulty": self.difficulty,
            "task_types": self.task_types,
            "rationale": self.rationale,
            "predicted_success": round(self.predicted_success, 4),
            "estimate_confidence": round(self.estimate_confidence, 4),
            "risks": self.risks,
            "analysis": self.analysis,
            "routing_summary": self.routing_summary,
            "model_snapshot": self.model_snapshot,
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
