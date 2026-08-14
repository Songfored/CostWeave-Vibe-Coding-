"""Strict, result-blind benchmark data contracts for CostWeave v0.6.0."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..intelligence.beliefs import TaskBeliefV1
from ..intelligence.common import (
    EVIDENCE_LEVELS,
    PARTITIONS,
    TOURNAMENT_SCHEMA_VERSION,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_id,
    stable_digest,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
    validate_timestamp,
)


_TASK_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _optional_non_negative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, name)


def _task_family(value: Any) -> str:
    family = bounded_text(value, "task_family")
    if _TASK_FAMILY_RE.fullmatch(family) is None:
        raise ValueError("task_family has an invalid format")
    return family


@dataclass(frozen=True, slots=True)
class RoutingRecordV1:
    source_record_id: str
    case_id: str
    partition: str
    task_family: str
    prompt_hash: str
    model_id: str
    response_hash: str
    quality_score: float
    accepted: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int | None
    source_dataset: str
    source_version: str
    price_snapshot_id: str
    evidence_level: str
    observed_at: str
    schema_version: str = TOURNAMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_record_id", bounded_text(self.source_record_id, "source_record_id"))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        if self.partition not in PARTITIONS:
            raise ValueError(f"unsupported partition: {self.partition!r}")
        object.__setattr__(self, "task_family", _task_family(self.task_family))
        object.__setattr__(self, "prompt_hash", validate_sha256(self.prompt_hash, "prompt_hash"))
        object.__setattr__(self, "model_id", bounded_text(self.model_id, "model_id"))
        object.__setattr__(self, "response_hash", validate_sha256(self.response_hash, "response_hash"))
        object.__setattr__(self, "quality_score", probability(self.quality_score, "quality_score"))
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be bool")
        object.__setattr__(self, "input_tokens", _non_negative_int(self.input_tokens, "input_tokens"))
        object.__setattr__(self, "output_tokens", _non_negative_int(self.output_tokens, "output_tokens"))
        object.__setattr__(self, "cost_usd", finite_number(self.cost_usd, "cost_usd", minimum=0.0))
        object.__setattr__(self, "latency_ms", _optional_non_negative_int(self.latency_ms, "latency_ms"))
        object.__setattr__(self, "source_dataset", bounded_text(self.source_dataset, "source_dataset"))
        object.__setattr__(self, "source_version", bounded_text(self.source_version, "source_version"))
        object.__setattr__(self, "price_snapshot_id", bounded_text(self.price_snapshot_id, "price_snapshot_id"))
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(f"unsupported evidence_level: {self.evidence_level!r}")
        object.__setattr__(self, "observed_at", validate_timestamp(self.observed_at, "observed_at"))
        if self.schema_version != TOURNAMENT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {TOURNAMENT_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_record_id": self.source_record_id,
            "case_id": self.case_id,
            "partition": self.partition,
            "task_family": self.task_family,
            "prompt_hash": self.prompt_hash,
            "model_id": self.model_id,
            "response_hash": self.response_hash,
            "quality_score": self.quality_score,
            "accepted": self.accepted,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "source_dataset": self.source_dataset,
            "source_version": self.source_version,
            "price_snapshot_id": self.price_snapshot_id,
            "evidence_level": self.evidence_level,
            "observed_at": self.observed_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RoutingRecordV1":
        validate_exact_fields(
            value,
            required=frozenset(
                {
                    "source_record_id", "case_id", "partition", "task_family", "prompt_hash", "model_id",
                    "response_hash", "quality_score", "accepted", "input_tokens", "output_tokens", "cost_usd",
                    "latency_ms", "source_dataset", "source_version", "price_snapshot_id", "evidence_level",
                    "observed_at", "schema_version",
                }
            ),
        )
        return cls(**{key: value[key] for key in value})


@dataclass(frozen=True, slots=True)
class DatasetManifestV1:
    dataset_name: str
    dataset_version: str
    source_uri: str
    license_id: str
    record_count: int
    case_count: int
    model_ids: tuple[str, ...]
    partition_counts: Mapping[str, int]
    records_hash: str
    evidence_level: str
    latency_coverage: float
    created_at: str
    schema_version: str = TOURNAMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_name", bounded_text(self.dataset_name, "dataset_name"))
        object.__setattr__(self, "dataset_version", bounded_text(self.dataset_version, "dataset_version"))
        object.__setattr__(self, "source_uri", bounded_text(self.source_uri, "source_uri"))
        object.__setattr__(self, "license_id", bounded_text(self.license_id, "license_id"))
        record_count = _non_negative_int(self.record_count, "record_count")
        case_count = _non_negative_int(self.case_count, "case_count")
        if record_count == 0 or case_count == 0:
            raise ValueError("dataset manifest must contain records and cases")
        if case_count > record_count:
            raise ValueError("case_count cannot exceed record_count")
        object.__setattr__(self, "record_count", record_count)
        object.__setattr__(self, "case_count", case_count)
        models = unique_texts(self.model_ids, "model_ids", maximum_items=1_000_000)
        if not models:
            raise ValueError("model_ids must not be empty")
        object.__setattr__(self, "model_ids", tuple(sorted(models)))
        if not isinstance(self.partition_counts, Mapping):
            raise TypeError("partition_counts must be a mapping")
        if set(self.partition_counts) != set(PARTITIONS):
            raise ValueError("partition_counts keys must exactly match partitions")
        counts = {partition: _non_negative_int(self.partition_counts[partition], f"partition_counts[{partition}]") for partition in PARTITIONS}
        if sum(counts.values()) != record_count:
            raise ValueError("partition counts must sum to record_count")
        object.__setattr__(self, "partition_counts", counts)
        object.__setattr__(self, "records_hash", validate_sha256(self.records_hash, "records_hash"))
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(f"unsupported evidence_level: {self.evidence_level!r}")
        object.__setattr__(self, "latency_coverage", probability(self.latency_coverage, "latency_coverage"))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != TOURNAMENT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {TOURNAMENT_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "dataset_name": self.dataset_name,
            "dataset_version": self.dataset_version,
            "source_uri": self.source_uri,
            "license_id": self.license_id,
            "record_count": self.record_count,
            "case_count": self.case_count,
            "model_ids": list(self.model_ids),
            "partition_counts": dict(sorted(self.partition_counts.items())),
            "records_hash": self.records_hash,
            "evidence_level": self.evidence_level,
            "latency_coverage": self.latency_coverage,
            "schema_version": self.schema_version,
        }

    @property
    def dataset_id(self) -> str:
        return stable_id("dataset", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["created_at"] = self.created_at
        result["dataset_id"] = self.dataset_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetManifestV1":
        fields = frozenset(
            {
                "dataset_name", "dataset_version", "source_uri", "license_id", "record_count", "case_count",
                "model_ids", "partition_counts", "records_hash", "evidence_level", "latency_coverage",
                "created_at", "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"dataset_id"}))
        item = cls(
            dataset_name=value["dataset_name"],
            dataset_version=value["dataset_version"],
            source_uri=value["source_uri"],
            license_id=value["license_id"],
            record_count=value["record_count"],
            case_count=value["case_count"],
            model_ids=tuple(value["model_ids"]),
            partition_counts=value["partition_counts"],
            records_hash=value["records_hash"],
            evidence_level=value["evidence_level"],
            latency_coverage=value["latency_coverage"],
            created_at=value["created_at"],
            schema_version=value["schema_version"],
        )
        if "dataset_id" in value and value["dataset_id"] != item.dataset_id:
            raise ValueError("dataset_id does not match content")
        return item


def validate_routing_records(records: Iterable[RoutingRecordV1]) -> tuple[RoutingRecordV1, ...]:
    items = tuple(records)
    if not items:
        raise ValueError("routing records cannot be empty")
    if len(items) > 1_000_000:
        raise ValueError("routing records exceed the 1,000,000 record limit")
    source_ids: set[str] = set()
    case_models: set[tuple[str, str]] = set()
    case_prompt: dict[str, str] = {}
    prompt_partition: dict[str, str] = {}
    dataset_price: dict[tuple[str, str], str] = {}
    case_model_counts: dict[str, set[str]] = {}
    for item in items:
        if not isinstance(item, RoutingRecordV1):
            raise TypeError("records must contain RoutingRecordV1")
        if item.source_record_id in source_ids:
            raise ValueError("source_record_id must be unique")
        source_ids.add(item.source_record_id)
        pair = (item.case_id, item.model_id)
        if pair in case_models:
            raise ValueError("case_id/model_id pair must be unique")
        case_models.add(pair)
        prior_prompt = case_prompt.setdefault(item.case_id, item.prompt_hash)
        if prior_prompt != item.prompt_hash:
            raise ValueError("same case cannot have different prompt hashes")
        prior_partition = prompt_partition.setdefault(item.prompt_hash, item.partition)
        if prior_partition != item.partition:
            raise ValueError("same prompt hash cannot cross partitions")
        version_key = (item.source_dataset, item.source_version)
        prior_price = dataset_price.setdefault(version_key, item.price_snapshot_id)
        if prior_price != item.price_snapshot_id:
            raise ValueError("price snapshot must be consistent within a dataset version")
        case_model_counts.setdefault(item.case_id, set()).add(item.model_id)
    if any(len(models) < 2 for models in case_model_counts.values()):
        raise ValueError("each case must contain at least two models")
    partition_order = {partition: index for index, partition in enumerate(PARTITIONS)}
    return tuple(sorted(items, key=lambda item: (partition_order[item.partition], item.case_id, item.model_id, item.source_record_id)))


def build_dataset_manifest(
    records: Iterable[RoutingRecordV1],
    *,
    source_uri: str,
    license_id: str,
    created_at: str,
) -> DatasetManifestV1:
    ordered = validate_routing_records(records)
    dataset_versions = {(item.source_dataset, item.source_version) for item in ordered}
    if len(dataset_versions) != 1:
        raise ValueError("manifest requires one dataset name/version")
    dataset_name, dataset_version = next(iter(dataset_versions))
    evidence_levels = {item.evidence_level for item in ordered}
    if len(evidence_levels) != 1:
        raise ValueError("manifest requires one evidence level")
    records_hash = stable_digest([item.to_dict() for item in ordered], length=64)
    model_ids = tuple(sorted({item.model_id for item in ordered}))
    partition_counts = {partition: sum(item.partition == partition for item in ordered) for partition in PARTITIONS}
    case_count = len({item.case_id for item in ordered})
    latency_coverage = sum(item.latency_ms is not None for item in ordered) / len(ordered)
    return DatasetManifestV1(
        dataset_name=dataset_name,
        dataset_version=dataset_version,
        source_uri=source_uri,
        license_id=license_id,
        record_count=len(ordered),
        case_count=case_count,
        model_ids=model_ids,
        partition_counts=partition_counts,
        records_hash=records_hash,
        evidence_level=next(iter(evidence_levels)),
        latency_coverage=latency_coverage,
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class CaseViewV1:
    case_id: str
    task_family: str
    prompt_hash: str
    model_ids: tuple[str, ...]
    task_belief: TaskBeliefV1 | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "task_family", _task_family(self.task_family))
        object.__setattr__(self, "prompt_hash", validate_sha256(self.prompt_hash, "prompt_hash"))
        models = unique_texts(self.model_ids, "model_ids", maximum_items=256)
        if not models:
            raise ValueError("model_ids must not be empty")
        object.__setattr__(self, "model_ids", tuple(sorted(models)))
        if self.task_belief is not None and not isinstance(self.task_belief, TaskBeliefV1):
            raise TypeError("task_belief must be TaskBeliefV1 or None")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "case_id": self.case_id,
            "task_family": self.task_family,
            "prompt_hash": self.prompt_hash,
            "model_ids": list(self.model_ids),
        }
        if self.task_belief is not None:
            result["task_belief"] = self.task_belief.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CaseViewV1":
        validate_exact_fields(value, required=frozenset({"case_id", "task_family", "prompt_hash", "model_ids"}), optional=frozenset({"task_belief"}))
        task_belief = value.get("task_belief")
        return cls(
            case_id=value["case_id"],
            task_family=value["task_family"],
            prompt_hash=value["prompt_hash"],
            model_ids=tuple(value["model_ids"]),
            task_belief=TaskBeliefV1.from_dict(task_belief) if task_belief is not None else None,
        )


__all__ = [
    "CaseViewV1",
    "DatasetManifestV1",
    "RoutingRecordV1",
    "build_dataset_manifest",
    "validate_routing_records",
]
