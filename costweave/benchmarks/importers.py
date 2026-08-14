"""Fail-closed importers for result-blind routing benchmark records.

The importer is intentionally boring: it reads bounded UTF-8 files, maps
declared columns, hashes any raw prompt/prediction immediately, and hands the
result to the strict :mod:`contracts` validators.  It never evaluates input
as Python and never stores raw prompt or prediction text in a
``RoutingRecordV1``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..intelligence.common import (
    TOURNAMENT_SCHEMA_VERSION,
    bounded_text,
    finite_number,
    validate_sha256,
)
from ..intelligence.decision_trace import DecisionTraceV2
from ..security_boundary import normalize_project_id
from .contracts import RoutingRecordV1, validate_routing_records


MAX_IMPORT_BYTES = 64 * 1024 * 1024
MAX_IMPORT_RECORDS = 1_000_000


class BenchmarkImportError(ValueError):
    """Raised when an external benchmark cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class FieldMappingV1:
    """Map canonical routing fields to external column names.

    ``prompt`` and ``prompt_hash`` are alternatives, as are ``prediction``
    and ``response_hash``.  The raw alternatives are hashed and never copied
    into the resulting contract.  All other fields are one-to-one mappings;
    ``None`` means that a value must be supplied through ``defaults``.
    """

    source_record_id: str | None = "source_record_id"
    case_id: str | None = "case_id"
    partition: str | None = "partition"
    task_family: str | None = "task_family"
    prompt: str | None = "prompt"
    prompt_hash: str | None = "prompt_hash"
    model_id: str | None = "model_id"
    prediction: str | None = "prediction"
    response_hash: str | None = "response_hash"
    quality_score: str | None = "quality_score"
    accepted: str | None = "accepted"
    input_tokens: str | None = "input_tokens"
    output_tokens: str | None = "output_tokens"
    cost_usd: str | None = "cost_usd"
    latency_ms: str | None = "latency_ms"
    source_dataset: str | None = "source_dataset"
    source_version: str | None = "source_version"
    price_snapshot_id: str | None = "price_snapshot_id"
    evidence_level: str | None = "evidence_level"
    observed_at: str | None = "observed_at"
    schema_version: str | None = "schema_version"

    def __post_init__(self) -> None:
        names: list[str] = []
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None:
                if not isinstance(value, str):
                    raise TypeError(f"mapping field {item.name} must be str or None")
                text = value.strip()
                if not text:
                    raise ValueError(f"mapping field {item.name} must not be empty")
                if text in names:
                    raise ValueError(f"mapping source column is used more than once: {text!r}")
                names.append(text)
                object.__setattr__(self, item.name, text)
        if self.prompt is None and self.prompt_hash is None:
            raise ValueError("mapping must provide prompt or prompt_hash")
        if self.prediction is None and self.response_hash is None:
            raise ValueError("mapping must provide prediction or response_hash")

    @property
    def canonical_fields(self) -> tuple[str, ...]:
        return tuple(item.name for item in fields(self))


def _path_bytes(path: str | os.PathLike[str]) -> bytes:
    candidate = Path(path)
    try:
        if not candidate.is_file():
            raise BenchmarkImportError("import path must be a regular file")
        size = candidate.stat().st_size
        if size > MAX_IMPORT_BYTES:
            raise BenchmarkImportError("import file exceeds the 64 MiB limit")
        return candidate.read_bytes()
    except OSError as exc:
        raise BenchmarkImportError("cannot read import file") from exc


def _jsonl_rows(path: str | os.PathLike[str]) -> list[tuple[int, Mapping[str, Any], str]]:
    try:
        raw = _path_bytes(path)
        file_hash = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkImportError("import file must be strict UTF-8") from exc
    rows: list[tuple[int, Mapping[str, Any], str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkImportError(f"invalid JSON on line {line_number}") from exc
        if not isinstance(value, Mapping):
            raise BenchmarkImportError(f"JSONL line {line_number} must be an object")
        rows.append((line_number, value, file_hash))
        if len(rows) > MAX_IMPORT_RECORDS:
            raise BenchmarkImportError("import exceeds the 1,000,000 record limit")
    return rows


def _csv_rows(path: str | os.PathLike[str]) -> list[tuple[int, Mapping[str, Any], str]]:
    try:
        raw = _path_bytes(path)
        file_hash = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BenchmarkImportError("import file must be strict UTF-8") from exc
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None or any(not isinstance(name, str) or not name.strip() for name in reader.fieldnames):
            raise BenchmarkImportError("CSV must contain a non-empty header")
        rows: list[tuple[int, Mapping[str, Any], str]] = []
        for line_number, row in enumerate(reader, start=2):
            if row is None:
                continue
            if all(value is None or not str(value).strip() for value in row.values()):
                continue
            if None in row:
                raise BenchmarkImportError(f"CSV row {line_number} has more values than the header")
            rows.append((line_number, row, file_hash))
            if len(rows) > MAX_IMPORT_RECORDS:
                raise BenchmarkImportError("import exceeds the 1,000,000 record limit")
        return rows
    except csv.Error as exc:
        raise BenchmarkImportError("invalid CSV") from exc


def _present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _raw_hash(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise BenchmarkImportError(f"{name} must be a string when supplied")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1"}:
            return True
        if text in {"false", "0"}:
            return False
    raise BenchmarkImportError(f"{name} must be true/false or 1/0")


def _int(value: Any, name: str, *, nullable: bool = False) -> int | None:
    if value is None or (nullable and isinstance(value, str) and not value.strip()):
        if nullable:
            return None
        raise BenchmarkImportError(f"{name} is required")
    if isinstance(value, bool):
        raise BenchmarkImportError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise BenchmarkImportError(f"{name} must be an integer") from exc
    raise BenchmarkImportError(f"{name} must be an integer")


def _float(value: Any, name: str) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise BenchmarkImportError(f"{name} is required")
    try:
        return finite_number(float(value) if isinstance(value, str) else value, name)
    except (TypeError, ValueError) as exc:
        raise BenchmarkImportError(f"{name} must be a finite number") from exc


_RECORD_FIELDS = frozenset(item.name for item in fields(RoutingRecordV1))


def _map_row(
    row: Mapping[str, Any],
    *,
    line_number: int,
    file_hash: str,
    mapping: FieldMappingV1,
    defaults: Mapping[str, Any],
) -> RoutingRecordV1:
    if not isinstance(row, Mapping):
        raise BenchmarkImportError(f"row {line_number} must be an object")
    if not isinstance(defaults, Mapping):
        raise TypeError("defaults must be a mapping")
    unknown_defaults = set(defaults) - _RECORD_FIELDS
    if unknown_defaults:
        raise BenchmarkImportError(f"unknown default fields: {sorted(unknown_defaults)!r}")

    def value_for(name: str, *, required: bool = True) -> Any:
        column = getattr(mapping, name)
        if column is not None and column in row and _present(row[column]):
            return row[column]
        if name in defaults and _present(defaults[name]):
            return defaults[name]
        if not required:
            return None
        raise BenchmarkImportError(f"row {line_number} is missing {name}")

    prompt_hash_value = value_for("prompt_hash", required=False) if mapping.prompt_hash is not None else None
    prompt_value = value_for("prompt", required=False) if mapping.prompt is not None else None
    if prompt_value is not None:
        calculated = _raw_hash(prompt_value, "prompt")
        if prompt_hash_value is not None:
            try:
                if validate_sha256(prompt_hash_value, "prompt_hash") != calculated:
                    raise BenchmarkImportError(f"row {line_number} prompt hash mismatch")
            except (TypeError, ValueError) as exc:
                raise BenchmarkImportError(f"row {line_number} prompt_hash is invalid") from exc
        prompt_hash = calculated
    elif prompt_hash_value is not None:
        try:
            prompt_hash = validate_sha256(prompt_hash_value, "prompt_hash")
        except (TypeError, ValueError) as exc:
            raise BenchmarkImportError(f"row {line_number} prompt_hash is invalid") from exc
    else:
        raise BenchmarkImportError(f"row {line_number} is missing prompt or prompt_hash")

    response_hash_value = value_for("response_hash", required=False) if mapping.response_hash is not None else None
    prediction_value = value_for("prediction", required=False) if mapping.prediction is not None else None
    if prediction_value is not None:
        calculated = _raw_hash(prediction_value, "prediction")
        if response_hash_value is not None:
            try:
                if validate_sha256(response_hash_value, "response_hash") != calculated:
                    raise BenchmarkImportError(f"row {line_number} response hash mismatch")
            except (TypeError, ValueError) as exc:
                raise BenchmarkImportError(f"row {line_number} response_hash is invalid") from exc
        response_hash = calculated
    elif response_hash_value is not None:
        try:
            response_hash = validate_sha256(response_hash_value, "response_hash")
        except (TypeError, ValueError) as exc:
            raise BenchmarkImportError(f"row {line_number} response_hash is invalid") from exc
    else:
        raise BenchmarkImportError(f"row {line_number} is missing prediction or response_hash")

    source_record_id = value_for("source_record_id", required=False)
    if source_record_id is None:
        source_record_id = "record_" + hashlib.sha256(
            f"{file_hash}:{line_number}:{value_for('model_id')}".encode("utf-8")
        ).hexdigest()[:32]
    latency = value_for("latency_ms", required=False)
    if latency is not None:
        latency = _int(latency, "latency_ms", nullable=True)
    payload: dict[str, Any] = {
        "source_record_id": source_record_id,
        "case_id": value_for("case_id"),
        "partition": value_for("partition"),
        "task_family": value_for("task_family"),
        "prompt_hash": prompt_hash,
        "model_id": value_for("model_id"),
        "response_hash": response_hash,
        "quality_score": _float(value_for("quality_score"), "quality_score"),
        "accepted": _bool(value_for("accepted"), "accepted"),
        "input_tokens": _int(value_for("input_tokens"), "input_tokens"),
        "output_tokens": _int(value_for("output_tokens"), "output_tokens"),
        "cost_usd": _float(value_for("cost_usd"), "cost_usd"),
        "latency_ms": latency,
        "source_dataset": value_for("source_dataset"),
        "source_version": value_for("source_version"),
        "price_snapshot_id": value_for("price_snapshot_id"),
        "evidence_level": value_for("evidence_level"),
        "observed_at": value_for("observed_at"),
        "schema_version": value_for("schema_version", required=False) or TOURNAMENT_SCHEMA_VERSION,
    }
    try:
        return RoutingRecordV1(**payload)
    except (TypeError, ValueError) as exc:
        raise BenchmarkImportError(f"row {line_number} violates RoutingRecordV1: {exc}") from exc


def _import_rows(
    rows: Iterable[tuple[int, Mapping[str, Any], str]],
    *,
    mapping: FieldMappingV1,
    defaults: Mapping[str, Any] | None,
) -> tuple[RoutingRecordV1, ...]:
    if not isinstance(mapping, FieldMappingV1):
        raise TypeError("mapping must be FieldMappingV1")
    items = tuple(
        _map_row(row, line_number=line_number, file_hash=file_hash, mapping=mapping, defaults=defaults or {})
        for line_number, row, file_hash in rows
    )
    if not items:
        raise BenchmarkImportError("import contains no records")
    try:
        return validate_routing_records(items)
    except (TypeError, ValueError) as exc:
        raise BenchmarkImportError(f"imported records failed validation: {exc}") from exc


def import_jsonl(
    path: str | os.PathLike[str],
    *,
    mapping: FieldMappingV1,
    defaults: Mapping[str, Any] | None = None,
) -> tuple[RoutingRecordV1, ...]:
    """Import bounded UTF-8 JSONL without retaining raw text fields."""

    return _import_rows(_jsonl_rows(path), mapping=mapping, defaults=defaults)


def import_csv(
    path: str | os.PathLike[str],
    *,
    mapping: FieldMappingV1,
    defaults: Mapping[str, Any] | None = None,
) -> tuple[RoutingRecordV1, ...]:
    """Import bounded UTF-8 CSV using the standard-library CSV parser only."""

    return _import_rows(_csv_rows(path), mapping=mapping, defaults=defaults)


def llmrouterbench_mapping() -> FieldMappingV1:
    """Return an explicit mapping for the common LLMRouterBench export shape."""

    return FieldMappingV1(
        source_record_id=None,
        case_id="task_id",
        partition=None,
        task_family=None,
        prompt="origin_query",
        prompt_hash=None,
        model_id=None,
        prediction="prediction",
        response_hash=None,
        quality_score="score",
        accepted=None,
        input_tokens="prompt_tokens",
        output_tokens="completion_tokens",
        cost_usd="cost",
        latency_ms=None,
        source_dataset=None,
        source_version=None,
        price_snapshot_id=None,
        evidence_level=None,
        observed_at=None,
        schema_version=None,
    )


def _external_trace_rows(source: str | os.PathLike[str] | Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if isinstance(source, (str, os.PathLike)):
        return [row for _, row, _ in _jsonl_rows(source)]
    rows = list(source)
    if len(rows) > MAX_IMPORT_RECORDS:
        raise BenchmarkImportError("import exceeds the 1,000,000 record limit")
    return rows


def import_external_decisions(
    source: str | os.PathLike[str] | Iterable[Mapping[str, Any]],
    *,
    project_id: str | None = None,
) -> tuple[DecisionTraceV2, ...]:
    """Import immutable decision traces while forbidding project mixing."""

    expected_project = normalize_project_id(project_id) if project_id is not None else None
    traces: list[DecisionTraceV2] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(_external_trace_rows(source), start=1):
        if not isinstance(row, Mapping):
            raise BenchmarkImportError(f"external trace {index} must be an object")
        try:
            trace = DecisionTraceV2.from_dict(row)
        except (TypeError, ValueError) as exc:
            raise BenchmarkImportError(f"external trace {index} is invalid: {exc}") from exc
        if expected_project is not None and trace.project_id != expected_project:
            raise BenchmarkImportError("external traces cross the requested project boundary")
        if trace.trace_id in seen_ids:
            raise BenchmarkImportError("duplicate external trace id")
        seen_ids.add(trace.trace_id)
        traces.append(trace)
    if not traces:
        raise BenchmarkImportError("external trace import contains no records")
    projects = {trace.project_id for trace in traces}
    if len(projects) != 1:
        raise BenchmarkImportError("external trace import cannot mix projects")
    return tuple(sorted(traces, key=lambda trace: (trace.case_id, trace.trace_id)))


__all__ = [
    "BenchmarkImportError",
    "FieldMappingV1",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_RECORDS",
    "import_csv",
    "import_external_decisions",
    "import_jsonl",
    "llmrouterbench_mapping",
]
