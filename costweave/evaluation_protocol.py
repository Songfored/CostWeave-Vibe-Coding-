"""Frozen evaluation contracts for CostWeave v0.5.6.1.

The protocol deliberately separates simulated fixture evidence from future
real-provider evidence.  Nothing in this module calls a provider or grants a
policy permission to influence production routing.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from importlib.resources import files
from typing import Any, Iterable, Mapping

from .domain import TaskContract, TaskStatus
from .success_contract import SuccessContract


EVALUATION_PROTOCOL_VERSION = "v0.5.6.1"
SEED_TASK_SET_VERSION = "v0.5.6.1-seed-1"
SIMULATED_EVIDENCE_LEVEL = "E0-simulated"
SEED_DATA_FILE = "v056_seed_tasks.json"

BASELINE_STRONGEST = "strongest_feasible"
BASELINE_CHEAPEST = "cheapest_feasible"
BASELINE_HUMAN_STATIC = "human_static"
BASELINE_V04_FIXED = "v04_fixed_rules"
BASELINE_V05_ADAPTIVE = "v05_adaptive"
BASELINE_IDS = (
    BASELINE_STRONGEST,
    BASELINE_CHEAPEST,
    BASELINE_HUMAN_STATIC,
    BASELINE_V04_FIXED,
    BASELINE_V05_ADAPTIVE,
)

PARTITIONS = ("train", "calibration", "test")
FORBIDDEN_BLIND_FIELDS = (
    "baseline_id",
    "model_id",
    "model_name",
    "provider",
    "policy_revision",
    "price",
    "cost",
    "route",
)


class EvaluationProtocolError(ValueError):
    """Raised when an experiment can no longer be compared fairly."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_digest(value: Any, *, length: int = 32) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True, slots=True)
class EvaluationLimits:
    """One shared resource envelope, applied independently to every baseline."""

    max_cost_usd: float
    max_calls: int
    max_wall_seconds: float
    max_tool_calls: int

    def __post_init__(self) -> None:
        if self.max_cost_usd <= 0:
            raise EvaluationProtocolError("baseline cost limit must be positive")
        if self.max_calls <= 0 or self.max_tool_calls < 0:
            raise EvaluationProtocolError("call limits are invalid")
        if self.max_wall_seconds <= 0:
            raise EvaluationProtocolError("wall-time limit must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_cost_usd": round(float(self.max_cost_usd), 8),
            "max_calls": self.max_calls,
            "max_wall_seconds": round(float(self.max_wall_seconds), 6),
            "max_tool_calls": self.max_tool_calls,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationLimits":
        return cls(
            max_cost_usd=float(value["max_cost_usd"]),
            max_calls=int(value["max_calls"]),
            max_wall_seconds=float(value["max_wall_seconds"]),
            max_tool_calls=int(value["max_tool_calls"]),
        )


@dataclass(frozen=True, slots=True)
class BlindReviewSpec:
    enabled: bool = True
    randomize_output_order: bool = True
    separate_hard_and_soft_review: bool = True
    forbidden_identity_fields: tuple[str, ...] = FORBIDDEN_BLIND_FIELDS

    def __post_init__(self) -> None:
        required = set(FORBIDDEN_BLIND_FIELDS)
        if not required.issubset(self.forbidden_identity_fields):
            raise EvaluationProtocolError(
                "blind review must hide route, provider, policy and price identity"
            )
        if not self.separate_hard_and_soft_review:
            raise EvaluationProtocolError(
                "hard acceptance and soft quality review must remain separate"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "randomize_output_order": self.randomize_output_order,
            "separate_hard_and_soft_review": self.separate_hard_and_soft_review,
            "forbidden_identity_fields": list(self.forbidden_identity_fields),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BlindReviewSpec":
        return cls(
            enabled=bool(value.get("enabled", True)),
            randomize_output_order=bool(
                value.get("randomize_output_order", True)
            ),
            separate_hard_and_soft_review=bool(
                value.get("separate_hard_and_soft_review", True)
            ),
            forbidden_identity_fields=tuple(
                value.get("forbidden_identity_fields", FORBIDDEN_BLIND_FIELDS)
            ),
        )


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    experiment_id: str
    random_seed: int
    task_set_version: str
    catalog_revision: str
    catalog_hash: str
    prompt_version: str
    policy_versions: Mapping[str, str]
    acceptance_revision: str
    limits: EvaluationLimits
    manual_assignment_rules: Mapping[str, str]
    default_manual_model: str
    quality_floor: float = 0.62
    partitions: tuple[str, ...] = PARTITIONS
    blind_review: BlindReviewSpec = field(default_factory=BlindReviewSpec)
    real_provider_calls_allowed: bool = False
    registered_at: str = "2026-07-29T00:00:00Z"
    schema_version: str = EVALUATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.experiment_id.startswith("experiment_"):
            raise EvaluationProtocolError(
                "experiment id must use the experiment_ namespace"
            )
        if self.random_seed < 0:
            raise EvaluationProtocolError("random seed cannot be negative")
        if not self.task_set_version or not self.catalog_hash:
            raise EvaluationProtocolError(
                "task-set and catalog snapshots are required"
            )
        if set(self.policy_versions) != set(BASELINE_IDS):
            raise EvaluationProtocolError(
                "exactly five frozen baseline policy revisions are required"
            )
        if not 0.5 <= self.quality_floor <= 0.99:
            raise EvaluationProtocolError("quality floor must be between 0.5 and 0.99")
        if tuple(self.partitions) != PARTITIONS:
            raise EvaluationProtocolError(
                "v0.5.6.1 freezes train/calibration/test partition names"
            )
        if not self.default_manual_model:
            raise EvaluationProtocolError("manual baseline needs a default model")
        if self.real_provider_calls_allowed:
            raise EvaluationProtocolError(
                "v0.5.6.1 forbids real provider calls"
            )

    @property
    def fingerprint(self) -> str:
        return stable_digest(self.to_dict())

    def ordered_task_ids(self, task_ids: Iterable[str]) -> tuple[str, ...]:
        unique = tuple(dict.fromkeys(str(item) for item in task_ids))
        return tuple(
            sorted(
                unique,
                key=lambda task_id: (
                    stable_digest(
                        {
                            "seed": self.random_seed,
                            "task_id": task_id,
                            "task_set": self.task_set_version,
                        },
                        length=16,
                    ),
                    task_id,
                ),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "random_seed": self.random_seed,
            "task_set_version": self.task_set_version,
            "catalog_revision": self.catalog_revision,
            "catalog_hash": self.catalog_hash,
            "prompt_version": self.prompt_version,
            "policy_versions": dict(sorted(self.policy_versions.items())),
            "acceptance_revision": self.acceptance_revision,
            "limits": self.limits.to_dict(),
            "manual_assignment_rules": dict(
                sorted(self.manual_assignment_rules.items())
            ),
            "default_manual_model": self.default_manual_model,
            "quality_floor": round(self.quality_floor, 4),
            "partitions": list(self.partitions),
            "blind_review": self.blind_review.to_dict(),
            "real_provider_calls_allowed": self.real_provider_calls_allowed,
            "registered_at": self.registered_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExperimentSpec":
        return cls(
            experiment_id=str(value["experiment_id"]),
            random_seed=int(value["random_seed"]),
            task_set_version=str(value["task_set_version"]),
            catalog_revision=str(value["catalog_revision"]),
            catalog_hash=str(value["catalog_hash"]),
            prompt_version=str(value["prompt_version"]),
            policy_versions=dict(value["policy_versions"]),
            acceptance_revision=str(value["acceptance_revision"]),
            limits=EvaluationLimits.from_dict(value["limits"]),
            manual_assignment_rules=dict(value["manual_assignment_rules"]),
            default_manual_model=str(value["default_manual_model"]),
            quality_floor=float(value.get("quality_floor", 0.62)),
            partitions=tuple(value.get("partitions", PARTITIONS)),
            blind_review=BlindReviewSpec.from_dict(value["blind_review"]),
            real_provider_calls_allowed=bool(
                value.get("real_provider_calls_allowed", False)
            ),
            registered_at=str(
                value.get("registered_at", "2026-07-29T00:00:00Z")
            ),
            schema_version=str(
                value.get("schema_version", EVALUATION_PROTOCOL_VERSION)
            ),
        )


_TASK_FIELDS = {item.name for item in fields(TaskContract)}


def _task_contract_from_dict(value: Mapping[str, Any]) -> TaskContract:
    payload = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key in _TASK_FIELDS
    }
    if "status" in payload and not isinstance(payload["status"], TaskStatus):
        payload["status"] = TaskStatus(str(payload["status"]))
    return TaskContract(**payload)


@dataclass(slots=True)
class BenchmarkTask:
    benchmark_id: str
    project_id: str
    partition: str
    collected_at: str
    user_goal: str
    task_contract: TaskContract
    success_contract: SuccessContract
    hard_constraints: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    allowed_data: tuple[str, ...]
    reference_answer: str | None
    independent_validator: str | None
    source_kind: str = "synthetic-seed"
    schema_version: str = EVALUATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.benchmark_id.startswith("benchmark_"):
            raise EvaluationProtocolError(
                "benchmark task id must use the benchmark_ namespace"
            )
        if self.partition not in PARTITIONS:
            raise EvaluationProtocolError(
                f"unsupported task partition: {self.partition}"
            )
        if not self.project_id or not self.user_goal.strip():
            raise EvaluationProtocolError("project id and user goal are required")
        if self.task_contract.id != self.benchmark_id:
            raise EvaluationProtocolError(
                "benchmark id and TaskContract id must match"
            )
        if self.success_contract.task_id != self.benchmark_id:
            raise EvaluationProtocolError(
                "SuccessContract must be preregistered for the same task"
            )
        if not self.success_contract.criteria:
            raise EvaluationProtocolError(
                "every benchmark task needs acceptance criteria"
            )
        missing_tools = set(self.task_contract.requires_tools) - set(
            self.allowed_tools
        )
        if missing_tools:
            raise EvaluationProtocolError(
                "required tools must be included in the common allowance"
            )
        if not self.reference_answer and not self.independent_validator:
            raise EvaluationProtocolError(
                "a reference answer or independent validator is required"
            )

    def task_copy(self) -> TaskContract:
        return _task_contract_from_dict(self.task_contract.to_dict())

    def execution_view(self) -> "ExecutionTaskView":
        return ExecutionTaskView(
            task_id=self.benchmark_id,
            user_goal=self.user_goal,
            task_contract=self.task_copy(),
            hard_constraints=self.hard_constraints,
            forbidden_actions=self.forbidden_actions,
            allowed_tools=self.allowed_tools,
            allowed_data=self.allowed_data,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "project_id": self.project_id,
            "partition": self.partition,
            "collected_at": self.collected_at,
            "user_goal": self.user_goal,
            "task_contract": self.task_contract.to_dict(),
            "success_contract": self.success_contract.to_dict(),
            "hard_constraints": list(self.hard_constraints),
            "forbidden_actions": list(self.forbidden_actions),
            "allowed_tools": list(self.allowed_tools),
            "allowed_data": list(self.allowed_data),
            "reference_answer": self.reference_answer,
            "independent_validator": self.independent_validator,
            "source_kind": self.source_kind,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BenchmarkTask":
        return cls(
            benchmark_id=str(value["benchmark_id"]),
            project_id=str(value["project_id"]),
            partition=str(value["partition"]),
            collected_at=str(value["collected_at"]),
            user_goal=str(value["user_goal"]),
            task_contract=_task_contract_from_dict(value["task_contract"]),
            success_contract=SuccessContract.from_dict(
                dict(value["success_contract"])
            ),
            hard_constraints=tuple(value.get("hard_constraints", ())),
            forbidden_actions=tuple(value.get("forbidden_actions", ())),
            allowed_tools=tuple(value.get("allowed_tools", ())),
            allowed_data=tuple(value.get("allowed_data", ())),
            reference_answer=(
                str(value["reference_answer"])
                if value.get("reference_answer")
                else None
            ),
            independent_validator=(
                str(value["independent_validator"])
                if value.get("independent_validator")
                else None
            ),
            source_kind=str(value.get("source_kind", "synthetic-seed")),
            schema_version=str(
                value.get("schema_version", EVALUATION_PROTOCOL_VERSION)
            ),
        )

    @classmethod
    def from_seed(cls, value: Mapping[str, Any]) -> "BenchmarkTask":
        benchmark_id = str(value["benchmark_id"])
        task = TaskContract(
            id=benchmark_id,
            title=str(value["title"]),
            objective=str(value["user_goal"]),
            task_type=str(value["task_type"]),
            required_capabilities=list(value["required_capabilities"]),
            acceptance_criteria=list(value["acceptance_criteria"]),
            critical_criteria=list(
                value.get(
                    "critical_criteria",
                    value["acceptance_criteria"][:1],
                )
            ),
            output_schema=[
                "summary",
                "deliverable",
                "evidence",
                "confidence",
            ],
            difficulty=int(value["difficulty"]),
            risk_level=str(value.get("risk_level", "normal")),
            required_modalities=list(
                value.get("required_modalities", ["text"])
            ),
            requires_tools=list(value.get("requires_tools", [])),
            requires_freshness=bool(
                value.get("requires_freshness", False)
            ),
            estimated_input_tokens=int(
                value.get("estimated_input_tokens", 4_000)
            ),
            estimated_output_tokens=int(
                value.get("estimated_output_tokens", 1_500)
            ),
            uncertainty=float(value.get("uncertainty", 0.15)),
            criticality=float(value.get("criticality", 0.7)),
            classification_confidence=float(
                value.get("classification_confidence", 0.9)
            ),
        )
        success = SuccessContract.from_task(task)
        task.success_contract = success.to_dict()
        return cls(
            benchmark_id=benchmark_id,
            project_id=str(value["project_id"]),
            partition=str(value["partition"]),
            collected_at=str(value["collected_at"]),
            user_goal=str(value["user_goal"]),
            task_contract=task,
            success_contract=success,
            hard_constraints=tuple(
                value.get(
                    "hard_constraints",
                    ("不得扩展任务范围", "不得虚构证据"),
                )
            ),
            forbidden_actions=tuple(
                value.get(
                    "forbidden_actions",
                    (
                        "network_access",
                        "paid_provider_call",
                        "irreversible_action",
                    ),
                )
            ),
            allowed_tools=tuple(value.get("allowed_tools", ())),
            allowed_data=tuple(
                value.get("allowed_data", ("embedded-fixture",))
            ),
            reference_answer=(
                str(value["reference_answer"])
                if value.get("reference_answer")
                else None
            ),
            independent_validator=str(
                value.get("independent_validator", "contract-fixture-v1")
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionTaskView:
    """The only task payload an execution backend may receive.

    Reference answers, validator identity, project partition and route identity
    are intentionally absent.
    """

    task_id: str
    user_goal: str
    task_contract: TaskContract
    hard_constraints: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    allowed_data: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_goal": self.user_goal,
            "task_contract": self.task_contract.to_dict(),
            "hard_constraints": list(self.hard_constraints),
            "forbidden_actions": list(self.forbidden_actions),
            "allowed_tools": list(self.allowed_tools),
            "allowed_data": list(self.allowed_data),
        }


def validate_task_set(tasks: Iterable[BenchmarkTask]) -> tuple[BenchmarkTask, ...]:
    materialized = tuple(tasks)
    if not materialized:
        raise EvaluationProtocolError("benchmark task set cannot be empty")
    ids = [item.benchmark_id for item in materialized]
    if len(ids) != len(set(ids)):
        raise EvaluationProtocolError("benchmark task ids must be unique")
    project_partitions: dict[str, set[str]] = {}
    for item in materialized:
        project_partitions.setdefault(item.project_id, set()).add(
            item.partition
        )
        if not item.success_contract.criteria:
            raise EvaluationProtocolError(
                f"{item.benchmark_id} has no preregistered success contract"
            )
    leaked = sorted(
        project
        for project, partitions in project_partitions.items()
        if len(partitions) != 1
    )
    if leaked:
        raise EvaluationProtocolError(
            "project-level partition leakage: " + ",".join(leaked)
        )
    return materialized


def load_seed_tasks() -> tuple[BenchmarkTask, ...]:
    raw = files("costweave.benchmarks").joinpath(SEED_DATA_FILE).read_text(
        encoding="utf-8"
    )
    payload = json.loads(raw)
    if payload.get("task_set_version") != SEED_TASK_SET_VERSION:
        raise EvaluationProtocolError("seed task-set version mismatch")
    tasks = tuple(BenchmarkTask.from_seed(item) for item in payload["tasks"])
    return validate_task_set(tasks)


def seed_task_set_hash(tasks: Iterable[BenchmarkTask]) -> str:
    return stable_digest([item.to_dict() for item in tasks])


def build_default_experiment_spec(
    *,
    catalog_revision: str,
    catalog_hash: str,
    random_seed: int = 5601,
) -> ExperimentSpec:
    identity = {
        "task_set_version": SEED_TASK_SET_VERSION,
        "catalog_revision": catalog_revision,
        "catalog_hash": catalog_hash,
        "random_seed": random_seed,
        "protocol": EVALUATION_PROTOCOL_VERSION,
    }
    return ExperimentSpec(
        experiment_id=f"experiment_{stable_digest(identity)}",
        random_seed=random_seed,
        task_set_version=SEED_TASK_SET_VERSION,
        catalog_revision=catalog_revision,
        catalog_hash=catalog_hash,
        prompt_version="benchmark-prompt-v0.5.6.1",
        policy_versions={
            BASELINE_STRONGEST: "baseline-strongest-v1",
            BASELINE_CHEAPEST: "baseline-cheapest-v1",
            BASELINE_HUMAN_STATIC: "baseline-human-static-v1",
            BASELINE_V04_FIXED: "resource-portfolio-v1",
            BASELINE_V05_ADAPTIVE: "planning-governor-0.5.3",
        },
        acceptance_revision="success-contract-v1",
        limits=EvaluationLimits(
            max_cost_usd=25.0,
            max_calls=100,
            max_wall_seconds=3_600,
            max_tool_calls=100,
        ),
        manual_assignment_rules={
            "coding": "gpt-5.6-terra",
            "research": "gpt-5.6-terra",
            "analysis": "gpt-5.6-terra",
            "data": "gemini-3.1-pro-preview",
            "long_context": "gpt-5.6-terra",
            "writing": "gpt-5.6-luna",
            "translation": "gpt-5.6-luna",
            "vision": "gemini-3.1-pro-preview",
            "safety": "gpt-5.6-sol",
            "risk": "gpt-5.6-sol",
        },
        default_manual_model="gpt-5.6-terra",
    )


__all__ = [
    "BASELINE_CHEAPEST",
    "BASELINE_HUMAN_STATIC",
    "BASELINE_IDS",
    "BASELINE_STRONGEST",
    "BASELINE_V04_FIXED",
    "BASELINE_V05_ADAPTIVE",
    "BenchmarkTask",
    "BlindReviewSpec",
    "EVALUATION_PROTOCOL_VERSION",
    "EvaluationLimits",
    "EvaluationProtocolError",
    "ExecutionTaskView",
    "ExperimentSpec",
    "SEED_TASK_SET_VERSION",
    "SIMULATED_EVIDENCE_LEVEL",
    "build_default_experiment_spec",
    "canonical_json",
    "load_seed_tasks",
    "seed_task_set_hash",
    "stable_digest",
    "validate_task_set",
]
