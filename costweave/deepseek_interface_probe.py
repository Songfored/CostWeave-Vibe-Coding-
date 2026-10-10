"""One-call, zero-retry DeepSeek structured-output verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .adapters import (
    AdapterRuntimePolicy,
    DeepSeekChatHTTPAdapter,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
)
from .event_store import EventStore
from .experimental_calls import (
    ExperimentBudget,
    ExperimentalCallError,
    ExperimentalCallStore,
)
from .planning_contracts import WorkOrder
from .provider_setup import DEFAULT_REGISTRY_FILE, project_root, utc_now
from .real_smoke_eval import PRICE_ASSUMPTIONS, _atomic_json, _load_registry


SCHEMA_VERSION = "v0.7.1-deepseek-interface-probe-1"
EXPERIMENT_ID = "experiment_deepseek_interface_20260901_v1"
RUN_ID = "run_deepseek_interface_20260901_v1"
PROVIDER_ID = "deepseek-official"
MAX_BUDGET_USD = 0.02
MAX_OUTPUT_TOKENS = 256
EXPECTED = {"answer": 402, "checksum": "ds-json-v1"}


class DeepSeekInterfaceProbeError(RuntimeError):
    """Safe user-facing probe error."""


def _work_order(budget_usd: float) -> WorkOrder:
    return WorkOrder(
        work_order_id="work_order_deepseek_interface_probe_v1",
        task_id="task_deepseek_interface_probe_v1",
        objective=(
            "This is a synthetic interface test. Compute 137 + 265. Return a JSON "
            "object with answer equal to the integer result and checksum exactly "
            "'ds-json-v1'. Do not add any other keys."
        ),
        dependencies=(),
        input_artifacts=(),
        context_manifest=(),
        assumptions=("Synthetic data only; no project or personal data is supplied.",),
        allowed_tools=(),
        forbidden_actions=(
            "Do not access files, tools, network, secrets, or external systems.",
            "Do not expand scope.",
        ),
        output_schema={
            "type": "object",
            "required": ["answer", "checksum"],
            "additionalProperties": False,
        },
        acceptance_tests=(
            "answer equals 402",
            "checksum equals ds-json-v1",
            "no additional keys",
        ),
        cost_limit=budget_usd,
        time_limit_ms=45_000,
        escalation_conditions=("Unable to produce the exact JSON object.",),
        idempotency_key="deepseek-interface-work-order-v1",
    )


def _validate(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = None
    checks = {
        "valid_json_object": isinstance(value, dict),
        "exact_keys": isinstance(value, dict) and set(value) == set(EXPECTED),
        "answer_correct": isinstance(value, dict) and value.get("answer") == 402,
        "checksum_correct": (
            isinstance(value, dict) and value.get("checksum") == "ds-json-v1"
        ),
    }
    return {"accepted": all(checks.values()), "checks": checks}


def run_probe(
    *, root: Path, budget_usd: float, registry_path: Path | None = None
) -> dict[str, Any]:
    budget = float(budget_usd)
    if not math.isfinite(budget) or not 0 < budget <= MAX_BUDGET_USD:
        raise DeepSeekInterfaceProbeError("预算必须大于 0 且不超过 0.02 美元")
    if (
        os.environ.get("COSTWEAVE_EXPERIMENTAL_ADAPTERS") != "1"
        or os.environ.get("COSTWEAVE_PAID_CALLS") != "1"
    ):
        raise DeepSeekInterfaceProbeError("缺少本次运行的双重显式付费授权开关")

    providers = _load_registry(registry_path or root / DEFAULT_REGISTRY_FILE)
    provider = next(
        (item for item in providers if item.get("provider_id") == PROVIDER_ID), None
    )
    if provider is None:
        raise DeepSeekInterfaceProbeError("本地目录没有可用的 DeepSeek 官方配置")

    runtime_path = root / "work/local-data/runtime-v4.sqlite3"
    report_path = root / "work/local-data/real-eval/deepseek-interface-probe.json"
    EventStore(runtime_path)
    store = ExperimentalCallStore(runtime_path)
    store.register_experiment(
        ExperimentBudget(
            experiment_id=EXPERIMENT_ID,
            total_cost_limit_usd=budget,
            provider_cost_limits_usd={PROVIDER_ID: budget},
            max_logical_calls=1,
            max_attempts_per_call=1,
            purpose="one_call_deepseek_json_interface_verification",
            expires_at=(datetime.now(UTC) + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        )
    )
    order = _work_order(budget)
    price = PRICE_ASSUMPTIONS[PROVIDER_ID]
    adapter = DeepSeekChatHTTPAdapter(
        ExperimentalAdapterConfig(
            adapter_id="deepseek-interface-probe",
            provider=PROVIDER_ID,
            endpoint=str(provider["chat_completions_url"]),
            model_id=str(provider["selected_model"]),
            deployment="remote",
            input_price_per_mtok_usd=price.input_per_mtok_usd,
            output_price_per_mtok_usd=price.output_per_mtok_usd,
            timeout_seconds=45.0,
            max_retries=0,
            secret_env=str(provider["secret_env"]),
            reasoning_effort="low",
        ),
        store=store,
        policy=AdapterRuntimePolicy(
            experimental_calls_enabled=True, paid_calls_enabled=True
        ),
    )
    request = ExternalExecutionRequest(
        experiment_id=EXPERIMENT_ID,
        run_id=RUN_ID,
        task_id=order.task_id,
        work_order=order,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        idempotency_key="deepseek-interface-call-v1",
    )
    result: dict[str, Any] = {
        "status": "failed",
        "accepted": False,
        "model_requested": provider["selected_model"],
        "trust": provider.get("trust"),
        "raw_output_persisted": False,
    }
    try:
        receipt = adapter.execute(request)
        content = str(receipt.result.output.get("content", ""))
        validation = _validate(content)
        telemetry = dict(receipt.telemetry)
        result.update(
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
                "provider_response_ids": telemetry.get("provider_response_ids", []),
                "reported_models": telemetry.get("reported_models", []),
                "provider_profile_revision": telemetry.get(
                    "provider_profile_revision"
                ),
                "actual_cost_usd": telemetry.get("actual_cost_usd"),
                "latency_ms": telemetry.get("request_latency_ms"),
                "output_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        )
    except ExperimentalCallError as error:
        result.update(
            {
                "error": type(error).__name__,
                "diagnostic": str(error),
            }
        )

    ledger = store.experiment_summary(EXPERIMENT_ID)
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "experiment_id": EXPERIMENT_ID,
        "run_id": RUN_ID,
        "authorized_budget_usd": budget,
        "provider_id": PROVIDER_ID,
        "synthetic_data_only": True,
        "project_files_sent": False,
        "personal_data_sent": False,
        "automatic_retries": 0,
        "ledger_summary": ledger,
        "budget_exceeded": float(ledger["actual_cost_usd"]) > budget + 1e-12,
        "result": result,
        "commercial_claims_allowed": False,
    }
    _atomic_json(report_path, report)
    return {**report, "report_path": str(report_path.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行一次 DeepSeek 官方 JSON 接口验证")
    parser.add_argument("--budget-usd", required=True, type=float)
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args(argv)
    report = run_probe(root=args.root.resolve(), budget_usd=args.budget_usd)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"]["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
