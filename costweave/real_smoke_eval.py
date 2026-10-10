"""Bounded three-provider real-call smoke evaluation for v0.7.1.

This is deliberately not a benchmark.  It proves the paid execution path,
usage attribution, deterministic validation and conservative cost accounting
with one call per configured provider and no automatic retries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .adapters import (
    AdapterRuntimePolicy,
    DeepSeekChatHTTPAdapter,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
    OpenAICompatibleHTTPAdapter,
)
from .billing_calibration import (
    calibration_receipt,
    estimate_billed_cost_cny,
    load_billing_calibration,
)
from .event_store import EventStore
from .experimental_calls import (
    ExperimentBudget,
    ExperimentalCallError,
    ExperimentalCallStore,
)
from .planning_contracts import WorkOrder
from .provider_setup import DEFAULT_REGISTRY_FILE, project_root, utc_now


REAL_SMOKE_SCHEMA_VERSION = "v0.7.1-real-smoke-1"
MAX_AUTHORIZED_BUDGET_USD = 0.50
MAX_OUTPUT_TOKENS = 512
EXPERIMENT_ID = "experiment_real_smoke_20260831_v1"
RUN_ID = "run_real_smoke_20260831_v1"
EXPECTED_ANSWER = 402
EXPECTED_CHECKSUM = "cswae"


class RealSmokeError(RuntimeError):
    """Safe real-evaluation setup or execution error."""


@dataclass(frozen=True, slots=True)
class PriceAssumption:
    input_per_mtok_usd: float
    output_per_mtok_usd: float
    source_url: str
    basis: str


PRICE_ASSUMPTIONS: Mapping[str, PriceAssumption] = {
    "deepseek-official": PriceAssumption(
        input_per_mtok_usd=1.32,
        output_per_mtok_usd=3.96,
        source_url="https://api-docs.deepseek.com/quick_start/pricing/",
        basis="official_peak_list_price_conservative",
    ),
    "traveltoken-gpt": PriceAssumption(
        input_per_mtok_usd=5.0,
        output_per_mtok_usd=30.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.5",
        basis="official_list_price_before_relay_discount_conservative",
    ),
    "traveltoken-claude": PriceAssumption(
        input_per_mtok_usd=3.0,
        output_per_mtok_usd=15.0,
        source_url="https://platform.claude.com/docs/en/models/sonnet-4-6/overview",
        basis="official_list_price_before_relay_discount_conservative",
    ),
}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _extract_json_object(content: str) -> Mapping[str, Any] | None:
    text = str(content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        left, right = text.find("{"), text.rfind("}")
        if left < 0 or right <= left:
            return None
        try:
            value = json.loads(text[left : right + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, Mapping) else None


def validate_output(content: str) -> dict[str, Any]:
    value = _extract_json_object(content)
    checks = {
        "valid_json_object": value is not None,
        "answer_is_402": bool(value and value.get("answer") == EXPECTED_ANSWER),
        "checksum_is_cswae": bool(
            value and str(value.get("checksum", "")).lower() == EXPECTED_CHECKSUM
        ),
        "no_extra_keys": bool(
            value and set(value) == {"answer", "checksum"}
        ),
    }
    return {
        "accepted": all(checks.values()),
        "checks": checks,
        "parsed": (
            {
                "answer": value.get("answer"),
                "checksum": value.get("checksum"),
            }
            if value is not None
            else None
        ),
    }


def _load_registry(path: Path) -> tuple[Mapping[str, Any], ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RealSmokeError("模型渠道配置不可读取，请重新运行一键配置") from error
    rows = document.get("providers")
    if not isinstance(rows, list):
        raise RealSmokeError("模型渠道配置格式无效")
    by_id = {
        str(item.get("provider_id")): item
        for item in rows
        if isinstance(item, Mapping)
    }
    required = tuple(PRICE_ASSUMPTIONS)
    if any(provider_id not in by_id for provider_id in required):
        raise RealSmokeError("模型渠道配置不完整")
    selected = tuple(by_id[provider_id] for provider_id in required)
    for item in selected:
        if (
            item.get("discovery_status") != "verified"
            or not item.get("selection_verified")
            or not item.get("selected_model")
            or not item.get("chat_completions_url")
            or not item.get("secret_env")
        ):
            raise RealSmokeError("存在尚未通过模型列表验证的渠道")
        if not os.environ.get(str(item["secret_env"]), "").strip():
            raise RealSmokeError("存在未加载密钥的渠道")
    return selected


def _work_order(provider_id: str, cost_limit: float) -> WorkOrder:
    task_id = f"task_real_smoke_{provider_id.replace('-', '_')}"
    return WorkOrder(
        work_order_id=f"work_order_real_smoke_{provider_id.replace('-', '_')}",
        task_id=task_id,
        objective=(
            "Return exactly one JSON object and no markdown. Compute "
            "(17 * 23) + 11 for the integer field 'answer'. For the string "
            "field 'checksum', take characters at 1-based odd positions of "
            "'CostWeave' and lowercase them. Use exactly the keys answer and checksum."
        ),
        dependencies=(),
        input_artifacts=(),
        context_manifest=(),
        assumptions=("Character positions are one-based.",),
        allowed_tools=(),
        forbidden_actions=(
            "Do not call tools.",
            "Do not add fields or prose.",
        ),
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["answer", "checksum"],
            "properties": {
                "answer": {"type": "integer"},
                "checksum": {"type": "string"},
            },
        },
        acceptance_tests=(
            "answer equals 402",
            "checksum equals cswae",
            "output has exactly two keys",
        ),
        cost_limit=cost_limit,
        time_limit_ms=45_000,
        escalation_conditions=("The requested output cannot be represented as JSON.",),
        idempotency_key=f"work-real-smoke-{provider_id}-v1",
    )


def run_real_smoke(
    *,
    root: Path,
    budget_usd: float,
    registry_path: Path | None = None,
    runtime_path: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    budget = float(budget_usd)
    if (
        not math.isfinite(budget)
        or budget <= 0
        or budget > MAX_AUTHORIZED_BUDGET_USD + 1e-12
    ):
        raise RealSmokeError("本轮预算必须在 0 到 0.50 美元之间")
    if os.environ.get("COSTWEAVE_EXPERIMENTAL_ADAPTERS") != "1" or os.environ.get(
        "COSTWEAVE_PAID_CALLS"
    ) != "1":
        raise RealSmokeError("真实调用需要本次运行的双重显式授权开关")

    registry_file = registry_path or root / DEFAULT_REGISTRY_FILE
    runtime_file = runtime_path or root / "work/local-data/runtime-v4.sqlite3"
    report_file = report_path or root / "work/local-data/real-eval/first-real-smoke.json"
    providers = _load_registry(registry_file)
    billing_calibration = load_billing_calibration()
    EventStore(runtime_file)
    store = ExperimentalCallStore(runtime_file)
    per_provider = budget / len(providers)
    provider_limits = {
        str(item["provider_id"]): per_provider for item in providers
    }
    store.register_experiment(
        ExperimentBudget(
            experiment_id=EXPERIMENT_ID,
            total_cost_limit_usd=budget,
            provider_cost_limits_usd=provider_limits,
            max_logical_calls=len(providers),
            max_attempts_per_call=1,
            purpose="first_bounded_real_smoke",
            expires_at=(datetime.now(UTC) + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
    )

    call_results: list[dict[str, Any]] = []
    policy = AdapterRuntimePolicy(
        experimental_calls_enabled=True,
        paid_calls_enabled=True,
    )
    for item in providers:
        provider_id = str(item["provider_id"])
        model_id = str(item["selected_model"])
        price = PRICE_ASSUMPTIONS[provider_id]
        work_order = _work_order(provider_id, per_provider)
        request = ExternalExecutionRequest(
            experiment_id=EXPERIMENT_ID,
            run_id=RUN_ID,
            task_id=work_order.task_id,
            work_order=work_order,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            idempotency_key=f"call-real-smoke-{provider_id}-v1",
        )
        adapter_type = (
            DeepSeekChatHTTPAdapter
            if provider_id == "deepseek-official"
            else OpenAICompatibleHTTPAdapter
        )
        adapter = adapter_type(
            ExperimentalAdapterConfig(
                adapter_id=f"real-smoke-{provider_id}",
                provider=provider_id,
                endpoint=str(item["chat_completions_url"]),
                model_id=model_id,
                deployment="remote",
                input_price_per_mtok_usd=price.input_per_mtok_usd,
                output_price_per_mtok_usd=price.output_per_mtok_usd,
                timeout_seconds=45.0,
                max_retries=0,
                secret_env=str(item["secret_env"]),
                reasoning_effort="low",
            ),
            store=store,
            policy=policy,
        )
        row: dict[str, Any] = {
            "provider_id": provider_id,
            "model_requested": model_id,
            "trust": item.get("trust"),
            "price_assumption": {
                "input_per_mtok_usd": price.input_per_mtok_usd,
                "output_per_mtok_usd": price.output_per_mtok_usd,
                "source_url": price.source_url,
                "basis": price.basis,
            },
            "status": "failed",
            "accepted": False,
        }
        try:
            receipt = adapter.execute(request)
            content = str(receipt.result.output.get("content", ""))
            validation = validate_output(content)
            telemetry = dict(receipt.telemetry)
            reference_cost_usd = float(telemetry.get("actual_cost_usd") or 0.0)
            billed_cost = estimate_billed_cost_cny(
                provider_id,
                reference_cost_usd=reference_cost_usd,
                input_tokens=int(telemetry.get("input_tokens") or 0),
                output_tokens=int(telemetry.get("output_tokens") or 0),
                cached_input_tokens=int(telemetry.get("cached_input_tokens") or 0),
                occurred_at=datetime.now(UTC),
                calibration=billing_calibration,
            )
            row.update(
                {
                    "status": "completed",
                    "accepted": validation["accepted"],
                    "validation": validation,
                    "external_call_id": receipt.external_call_id,
                    "replayed": receipt.replayed,
                    "input_tokens": telemetry.get("input_tokens"),
                    "output_tokens": telemetry.get("output_tokens"),
                    "cached_input_tokens": telemetry.get("cached_input_tokens"),
                    "reasoning_tokens": telemetry.get("reasoning_tokens"),
                    "reasoning_only_response_count": telemetry.get(
                        "reasoning_only_response_count"
                    ),
                    "finish_reasons": telemetry.get("finish_reasons", []),
                    "provider_profile_revision": telemetry.get(
                        "provider_profile_revision"
                    ),
                    # Backward-compatible alias. This is a conservative
                    # reference/list-price estimate, not an invoice amount.
                    "estimated_cost_usd": reference_cost_usd,
                    "reference_list_cost_usd": reference_cost_usd,
                    "estimated_billed_cost_cny": billed_cost.amount_cny,
                    "estimated_billed_cost_cny_lower": billed_cost.lower_cny,
                    "estimated_billed_cost_cny_upper": billed_cost.upper_cny,
                    "billing_estimate": billed_cost.as_dict(),
                    "cost_status": (
                        "official_reference_price_plus_cny_invoice_calibration"
                        if item.get("trust") == "official"
                        else "relay_cny_invoice_calibrated_from_account_history"
                    ),
                    "latency_ms": telemetry.get("request_latency_ms"),
                    "provider_response_ids": telemetry.get("provider_response_ids", []),
                    "reported_models": telemetry.get("reported_models", []),
                    "output_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "raw_output_persisted_in_report": False,
                }
            )
        except ExperimentalCallError as error:
            row.update(
                {
                    "error": type(error).__name__,
                    "diagnostic": str(error),
                    "cost_status": "unknown_after_failed_call",
                }
            )
        call_results.append(row)

    estimated_total = round(
        sum(float(item.get("estimated_cost_usd") or 0.0) for item in call_results),
        10,
    )
    estimated_billed_total_cny = round(
        sum(
            float(item.get("estimated_billed_cost_cny") or 0.0)
            for item in call_results
        ),
        10,
    )
    estimated_billed_lower_cny = round(
        sum(
            float(item.get("estimated_billed_cost_cny_lower") or 0.0)
            for item in call_results
        ),
        10,
    )
    estimated_billed_upper_cny = round(
        sum(
            float(item.get("estimated_billed_cost_cny_upper") or 0.0)
            for item in call_results
        ),
        10,
    )
    report = {
        "schema_version": REAL_SMOKE_SCHEMA_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "run_id": RUN_ID,
        "created_at": utc_now(),
        "authorized_budget_usd": budget,
        "reference_list_cost_usd": estimated_total,
        "estimated_total_cost_usd": estimated_total,
        "estimated_billed_cost_cny": estimated_billed_total_cny,
        "estimated_billed_cost_cny_lower": estimated_billed_lower_cny,
        "estimated_billed_cost_cny_upper": estimated_billed_upper_cny,
        "billing_calibration": calibration_receipt(billing_calibration),
        "provider_invoice_reconciled": False,
        "budget_exceeded": estimated_total > budget + 1e-12,
        "call_count": len(call_results),
        "completed_count": sum(item["status"] == "completed" for item in call_results),
        "accepted_count": sum(bool(item.get("accepted")) for item in call_results),
        "all_accepted": all(bool(item.get("accepted")) for item in call_results),
        "commercial_claims_allowed": False,
        "benchmark_claims_allowed": False,
        "purpose": "execution-path-and-accounting-smoke-only",
        "calls": call_results,
    }
    _atomic_json(report_file, report)
    return {**report, "report_path": str(report_file.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行三渠道小预算真实冒烟评测")
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args(argv)
    try:
        report = run_real_smoke(root=args.root.resolve(), budget_usd=args.budget_usd)
    except RealSmokeError as error:
        print(json.dumps({"status": "blocked", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["completed_count"] == report["call_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
