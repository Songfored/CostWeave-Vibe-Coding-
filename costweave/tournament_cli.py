"""Command-line entry point for the offline v0.6.0 tournament."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Sequence

from .benchmarks.contracts import DatasetManifestV1, RoutingRecordV1, build_dataset_manifest, validate_routing_records
from .benchmarks.importers import (
    MAX_IMPORT_BYTES,
    BenchmarkImportError,
    FieldMappingV1,
    import_csv,
    import_external_decisions,
    import_jsonl,
)
from .benchmarks.policies import DeterministicRandomPolicy, FixedModelPolicy, StaticTaskFamilyPolicy
from .benchmarks.tournament import TournamentError, TournamentRunner, TournamentSpecV1


DEFAULT_BOOTSTRAP_SAMPLES = 2_000
DEFAULT_RANDOM_SEED = 600


def _load_seed() -> tuple[tuple[RoutingRecordV1, ...], DatasetManifestV1]:
    path = files("costweave.benchmarks").joinpath("v060_seed_routing_records.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = validate_routing_records(tuple(RoutingRecordV1.from_dict(item) for item in raw))
    return records, build_dataset_manifest(
        records,
        source_uri="package://costweave/benchmarks/v060_seed_routing_records.json",
        license_id="MIT",
        created_at="2026-08-09T00:00:00Z",
    )


def _read_json_file(path: str, *, label: str) -> Any:
    """Read a user-supplied JSON file without leaking paths or bypassing limits."""

    candidate = Path(path)
    try:
        if not candidate.is_file():
            raise BenchmarkImportError(f"{label} must be a regular file")
        if candidate.stat().st_size > MAX_IMPORT_BYTES:
            raise BenchmarkImportError(f"{label} exceeds the 64 MiB limit")
        raw = candidate.read_bytes()
    except BenchmarkImportError:
        raise
    except OSError as exc:
        raise BenchmarkImportError(f"cannot read {label}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BenchmarkImportError(f"{label} must be strict UTF-8") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BenchmarkImportError(f"{label} is not valid JSON") from exc


def _mapping(path: str | None) -> FieldMappingV1:
    if path is None:
        return FieldMappingV1(schema_version=None)
    value = _read_json_file(path, label="mapping file")
    if not isinstance(value, Mapping):
        raise BenchmarkImportError("mapping file must contain a JSON object")
    return FieldMappingV1(**dict(value))


def _load_dataset(path: str, format_name: str, mapping_path: str | None) -> tuple[tuple[RoutingRecordV1, ...], DatasetManifestV1]:
    candidate = Path(path)
    mapping = _mapping(mapping_path)
    if format_name == "json":
        raw = _read_json_file(path, label="JSON dataset")
        if not isinstance(raw, list):
            raise BenchmarkImportError("JSON dataset must contain a list of records")
        records = validate_routing_records(tuple(RoutingRecordV1.from_dict(item) for item in raw))
    elif format_name == "jsonl":
        records = import_jsonl(candidate, mapping=mapping, defaults={})
    elif format_name == "csv":
        records = import_csv(candidate, mapping=mapping, defaults={})
    else:
        raise BenchmarkImportError("format must be json, jsonl or csv")
    manifest = build_dataset_manifest(records, source_uri=str(candidate.resolve()), license_id="UNKNOWN", created_at="2026-08-09T00:00:00Z")
    return records, manifest


def _policies(*, strong_model: str, cheap_model: str, model_ids: set[str], seed: int):
    policies = [
        FixedModelPolicy(strong_model, policy_id="strong-only"),
        FixedModelPolicy(cheap_model, policy_id="cheap-only"),
    ]
    balanced = "seed-balanced" if "seed-balanced" in model_ids else next((item for item in sorted(model_ids) if item not in {strong_model, cheap_model}), None)
    if balanced is not None:
        policies.append(FixedModelPolicy(balanced, policy_id="balanced-only"))
    policies.append(DeterministicRandomPolicy(seed, policy_id=f"random-seed-{seed}"))
    return tuple(policies)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CostWeave v0.6.0 offline routing tournament")
    parser.add_argument("--dataset", help="JSON, JSONL or CSV dataset; omitted uses the packaged E0 seed")
    parser.add_argument("--format", choices=("json", "jsonl", "csv"), default="json")
    parser.add_argument("--mapping", help="JSON FieldMappingV1 for JSONL/CSV")
    parser.add_argument("--external-decisions", action="append", default=[])
    parser.add_argument("--partition", choices=("train", "calibration", "test"), default="test")
    parser.add_argument("--strong-model", default="seed-strong")
    parser.add_argument("--cheap-model", default="seed-cheap")
    parser.add_argument("--baseline", default="strong-only")
    parser.add_argument("--bootstrap-samples", type=int, default=DEFAULT_BOOTSTRAP_SAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output", help="create a new report file; existing files are never overwritten")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        records, manifest = _load_seed() if args.dataset is None else _load_dataset(args.dataset, args.format, args.mapping)
        for path in args.external_decisions:
            import_external_decisions(path)
        model_ids = {record.model_id for record in records}
        policies = _policies(strong_model=args.strong_model, cheap_model=args.cheap_model, model_ids=model_ids, seed=args.seed)
        if args.baseline != "strong-only":
            policies = tuple(policy for policy in policies if policy.policy_id != "strong-only") + (FixedModelPolicy(args.strong_model, policy_id=args.baseline),)
        spec = TournamentSpecV1(
            project_id="local-default",
            dataset_id=manifest.dataset_id,
            partition=args.partition,
            strong_model_id=args.strong_model,
            cheap_model_id=args.cheap_model,
            baseline_policy_id=args.baseline,
            acceptance_threshold=0.8,
            quality_weight=1.0,
            cost_weight=0.1,
            latency_weight=0.01,
            bootstrap_samples=args.bootstrap_samples,
            random_seed=args.seed,
            minimum_claim_cases=30,
            registered_at="2026-08-09T00:00:00Z",
        )
        report = TournamentRunner().run(spec=spec, manifest=manifest, records=records, policies=policies)
        rendered = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":"))
        if args.output:
            destination = Path(args.output)
            with destination.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered + "\n")
        else:
            print(rendered)
        return 0
    except (BenchmarkImportError, TournamentError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"tournament input error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"tournament failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
