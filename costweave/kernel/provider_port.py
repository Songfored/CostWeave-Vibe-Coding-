"""Bind a host-configured model and its existing external-call authority to a kernel port."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal, ROUND_CEILING

from ..adapters.http import BaseExperimentalHTTPAdapter, ExternalExecutionRequest
from ..experimental_calls import ExternalBudgetExceededError, ExternalCallsDisabledError, ExternalCostOverrunError
from ..planning_contracts import WorkOrder
from ..security_boundary import AdapterAdmissionError, DEFAULT_DATA_RESIDENCY, DEFAULT_PROJECT_ID, PromptInjectionError
from .contracts import Operation, canonical_json
from .ports import AdmissionDenied, ExecutionResult, KnownFailure, Registry


def _unique_fields(pairs: list[tuple]) -> dict:
    item = dict(pairs)
    if len(item) != len(pairs):
        raise ValueError("model returned duplicate JSON object keys")
    return item


def register_model_route(
    registry: Registry,
    name: str,
    adapter: BaseExperimentalHTTPAdapter,
    *,
    experiment_id: str,
    run_id: str,
    max_output_tokens: int,
    usd_per_unit: float = 0.000001,
    project_id: str = DEFAULT_PROJECT_ID,
    data_residency: str = DEFAULT_DATA_RESIDENCY,
    security_approval_id: str | None = None,
) -> None:
    """Register one preconfigured model route for this run.

    A job supplies config.prompt and declared input artifacts. The host owns
    the endpoint, prices, output cap, experiment ledger, approval and rate
    of USD per kernel unit. Every alternative in a run needs the same unit.
    The existing receipt preserves its billing source. A single-attempt
    provider billing header settles the measured charge; paid usage estimates,
    omitted usage and mixed/retried attempts consume the full kernel quote.
    An unpriced local adapter can settle at zero without a billing header.
    A transport with uncertain outcome leaves reconciliation to the caller.
    """
    if type(usd_per_unit) not in (int, float) or not math.isfinite(usd_per_unit) or usd_per_unit <= 0:
        raise ValueError("usd_per_unit must be positive and finite")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65_536:
        raise ValueError("max_output_tokens must be between 1 and 65536")
    if not experiment_id.startswith("experiment_") or not run_id.strip():
        raise ValueError("model route requires a registered experiment and a run identity")
    unit = Decimal(str(usd_per_unit))

    def check_config(op: Operation) -> None:
        prompt = op.config.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{op.id} requires config.prompt")
        if not math.isfinite(float(op.cost_units * unit)):
            raise ValueError("model quote is outside representable USD range")

    def generate(op: Operation, inputs: Mapping, *, attempt_id: str) -> ExecutionResult:
        check_config(op)
        digest = hashlib.sha256(canonical_json([run_id, op.id, attempt_id]).encode()).hexdigest()
        key = "kernel_" + digest
        order = WorkOrder(
            work_order_id="work_order_" + digest, task_id=op.id,
            objective=canonical_json({"instruction": op.config["prompt"], "artifacts": dict(inputs)}),
            dependencies=op.requires, input_artifacts=op.requires, context_manifest=(), assumptions=(),
            allowed_tools=(), forbidden_actions=("expand scope",),
            output_schema={"format": "one JSON value"}, acceptance_tests=("Pass the host's artifact validator",),
            cost_limit=float(op.cost_units * unit), time_limit_ms=max(1, math.ceil(adapter.config.timeout_seconds * 1000)),
            escalation_conditions=("required input is missing",), idempotency_key=key,
        )
        request = ExternalExecutionRequest(
            experiment_id=experiment_id, run_id=run_id, task_id=op.id, work_order=order,
            max_output_tokens=max_output_tokens, idempotency_key=key, project_id=project_id,
            data_residency=data_residency, security_approval_id=security_approval_id,
        )
        try:
            receipt = adapter.execute(request)
        except ExternalCostOverrunError:
            # A bill can exceed its quote after dispatch. Retain the kernel hold.
            raise
        except (ExternalCallsDisabledError, ExternalBudgetExceededError, PromptInjectionError, AdapterAdmissionError) as exc:
            raise AdmissionDenied(type(exc).__name__) from exc
        # The legacy ledger also calls price-table estimates "actual_cost".
        # Missing usage can therefore yield a zero estimate on a paid route.
        actual = Decimal(str(receipt.result.actual_cost))
        measured = int((actual / unit).to_integral_value(rounding=ROUND_CEILING))
        if not 0 <= measured <= op.cost_units:
            raise ValueError("model receipt exceeds the kernel reservation")
        single_billed_call = (
            receipt.telemetry.get("attempt_count") == 1
            and receipt.telemetry.get("billing_sources") == ["provider_billing_header"]
        )
        units = measured if single_billed_call or not adapter.config.paid else None
        try:
            value = json.loads(receipt.result.output["content"], object_pairs_hook=_unique_fields)
            canonical_json(value)
        except (KeyError, ValueError, TypeError, RecursionError) as exc:
            raise KnownFailure("model returned an invalid JSON artifact", actual_units=units) from exc
        return ExecutionResult(value, units, receipt.external_call_id)

    registry.register_executor(name, generate, check_config=check_config)
