"""Provider-invoice calibration without weakening conservative budget reserves."""

from __future__ import annotations

import json
import math
import hashlib
import sqlite3
import os
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


DEFAULT_BILLING_CALIBRATION_FILE = (
    Path(__file__).resolve().parent / "benchmarks" / "billing_calibration.v1.json"
)


class BillingCalibrationError(ValueError):
    """Raised when a billing calibration document is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class BilledCostEstimate:
    provider_id: str
    amount_cny: float
    lower_cny: float
    upper_cny: float
    method: str
    confidence: str
    reference_cost_usd: float
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    estimated_cache_read_cost_cny: float | None = None
    estimated_cache_creation_cost_cny: float | None = None
    time_band: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "amount_cny": self.amount_cny,
            "lower_cny": self.lower_cny,
            "upper_cny": self.upper_cny,
            "currency": "CNY",
            "method": self.method,
            "confidence": self.confidence,
            "reference_cost_usd": self.reference_cost_usd,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "estimated_cache_read_cost_cny": self.estimated_cache_read_cost_cny,
            "estimated_cache_creation_cost_cny": self.estimated_cache_creation_cost_cny,
            "time_band": self.time_band,
        }


@dataclass(frozen=True, slots=True)
class BillingReconciliation:
    provider_id: str
    estimated_cny: float
    provider_observed_cny: float
    final_billed_cny: float
    residual_cny: float
    residual_fraction_of_invoice: float | None
    final_source: str = "provider_invoice"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "estimated_cny": self.estimated_cny,
            "provider_observed_cny": self.provider_observed_cny,
            "final_billed_cny": self.final_billed_cny,
            "residual_cny": self.residual_cny,
            "residual_fraction_of_invoice": self.residual_fraction_of_invoice,
            "currency": "CNY",
            "final_source": self.final_source,
        }


@dataclass(frozen=True, slots=True)
class CallBillingReconciliation:
    external_call_id: str
    attempt_ordinal: int
    provider_id: str
    estimated_cny: float
    provider_billed_cny: float
    residual_cny: float
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    observed_at: str
    evidence_sha256: str
    currency: str = "CNY"

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_call_id": self.external_call_id,
            "attempt_ordinal": self.attempt_ordinal,
            "provider_id": self.provider_id,
            "estimated_billed_cost_cny": self.estimated_cny,
            "provider_billed_cost_cny": self.provider_billed_cny,
            "billing_residual_cny": self.residual_cny,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "observed_at": self.observed_at,
            "evidence_sha256": self.evidence_sha256,
            "currency": self.currency,
            "invoice_reconciled": True,
        }


def load_billing_calibration(
    path: Path | None = None,
) -> Mapping[str, Any]:
    source = (path or DEFAULT_BILLING_CALIBRATION_FILE).resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BillingCalibrationError("计费校准文件不可读取") from error
    if document.get("schema_version") != "costweave-billing-calibration-v1":
        raise BillingCalibrationError("计费校准版本不受支持")
    if document.get("account_currency") != "CNY":
        raise BillingCalibrationError("当前真实实验只允许人民币账单校准")
    providers = document.get("providers")
    if not isinstance(providers, Mapping) or not providers:
        raise BillingCalibrationError("计费校准缺少渠道")
    for provider_id, row in providers.items():
        if not isinstance(provider_id, str) or not isinstance(row, Mapping):
            raise BillingCalibrationError("计费校准渠道格式无效")
        error_fraction = float(row.get("relative_error_fraction", -1))
        if not math.isfinite(error_fraction) or not 0 <= error_fraction <= 0.5:
            raise BillingCalibrationError("计费校准误差范围无效")
    return document


def _parse_utc(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _deepseek_time_band(row: Mapping[str, Any], occurred_at: datetime) -> str:
    if occurred_at.weekday() >= 5:
        return "off_peak"
    windows = row.get("peak_utc_weekday_windows") or ()
    for window in windows:
        if (
            isinstance(window, list)
            and len(window) == 2
            and int(window[0]) <= occurred_at.hour < int(window[1])
        ):
            return "peak"
    return "off_peak"


def estimate_billed_cost_cny(
    provider_id: str,
    *,
    reference_cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_read_input_tokens: int | None = None,
    cache_creation_input_tokens: int = 0,
    occurred_at: str | datetime | None = None,
    calibration: Mapping[str, Any] | None = None,
) -> BilledCostEstimate:
    """Estimate actual CNY billing while retaining the reference USD cost.

    Relay accounts use a provider-specific numeric multiplier because their UI
    settles in CNY despite rendering a dollar symbol. DeepSeek is recomputed
    with official peak/off-peak and cache-hit rates before applying the
    empirically reconciled CNY conversion.
    """

    document = calibration or load_billing_calibration()
    providers = document["providers"]
    if provider_id not in providers:
        raise BillingCalibrationError(f"缺少渠道计费校准：{provider_id}")
    row = providers[provider_id]
    reference = float(reference_cost_usd)
    if not math.isfinite(reference) or reference < 0:
        raise BillingCalibrationError("参考成本必须是非负有限数")
    cache_read = int(
        cached_input_tokens
        if cache_read_input_tokens is None
        else cache_read_input_tokens
    )
    cache_creation = int(cache_creation_input_tokens)
    tokens = (int(input_tokens), int(output_tokens), cache_read, cache_creation)
    if any(value < 0 for value in tokens) or tokens[2] > tokens[0]:
        raise BillingCalibrationError("Token 计费数据无效")

    method = str(row["method"])
    time_band: str | None = None
    cache_read_cost_cny: float | None = None
    cache_creation_cost_cny: float | None = None
    if method == "reference_numeric_multiplier":
        amount = reference * float(row["reference_to_billed_cny_multiplier"])
    elif method == "official_time_band_then_empirical_cny":
        occurred = _parse_utc(occurred_at)
        time_band = _deepseek_time_band(row, occurred)
        rates = row["rates_usd_per_mtok"][time_band]
        uncached = tokens[0] - tokens[2]
        adjusted_reference = (
            uncached * float(rates["cache_miss_input"])
            + tokens[2] * float(rates["cache_hit_input"])
            + tokens[1] * float(rates["output"])
        ) / 1_000_000
        amount = adjusted_reference * float(row["effective_cny_per_usd"])
        cache_read_cost_cny = (
            tokens[2]
            * float(rates["cache_hit_input"])
            / 1_000_000
            * float(row["effective_cny_per_usd"])
        )
        cache_creation_cost_cny = (
            tokens[3]
            * float(rates["cache_miss_input"])
            / 1_000_000
            * float(row["effective_cny_per_usd"])
        )
    else:
        raise BillingCalibrationError(f"未知计费校准方法：{method}")

    error_fraction = float(row["relative_error_fraction"])
    amount = round(amount, 10)
    return BilledCostEstimate(
        provider_id=provider_id,
        amount_cny=amount,
        lower_cny=round(max(0.0, amount * (1 - error_fraction)), 10),
        upper_cny=round(amount * (1 + error_fraction), 10),
        method=method,
        confidence=str(row.get("confidence", "unknown")),
        reference_cost_usd=round(reference, 10),
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        estimated_cache_read_cost_cny=(
            round(cache_read_cost_cny, 10)
            if cache_read_cost_cny is not None
            else None
        ),
        estimated_cache_creation_cost_cny=(
            round(cache_creation_cost_cny, 10)
            if cache_creation_cost_cny is not None
            else None
        ),
        time_band=time_band,
    )


def calibration_receipt(
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document = calibration or load_billing_calibration()
    return {
        "schema_version": document["schema_version"],
        "revision": document["revision"],
        "account_currency": document["account_currency"],
        "observed_at": document["observed_at"],
        "providers": {
            provider_id: {
                "method": row["method"],
                "confidence": row.get("confidence"),
                "relative_error_fraction": row["relative_error_fraction"],
            }
            for provider_id, row in document["providers"].items()
        },
    }


def reconcile_provider_invoice(
    provider_id: str,
    *,
    estimated_cny: float,
    provider_observed_cny: float,
) -> BillingReconciliation:
    """Make a provider invoice authoritative and retain the estimator error."""

    if isinstance(estimated_cny, bool) or isinstance(provider_observed_cny, bool):
        raise BillingCalibrationError("对账金额不能是布尔值")
    estimated = float(estimated_cny)
    observed = float(provider_observed_cny)
    if any(not math.isfinite(value) or value < 0 for value in (estimated, observed)):
        raise BillingCalibrationError("对账金额必须是非负有限数")
    residual = round(observed - estimated, 10)
    return BillingReconciliation(
        provider_id=provider_id,
        estimated_cny=round(estimated, 10),
        provider_observed_cny=round(observed, 10),
        final_billed_cny=round(observed, 10),
        residual_cny=residual,
        residual_fraction_of_invoice=(
            round(residual / observed, 10) if observed else None
        ),
    )


def reconcile_call_invoice(
    external_call_id: str,
    *,
    attempt_ordinal: int,
    provider_id: str,
    estimated_cny: float,
    provider_billed_cny: float,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    observed_at: str | datetime | None = None,
    evidence_sha256: str,
) -> CallBillingReconciliation:
    """Attach one provider invoice row without changing the pricing model."""

    if not external_call_id.startswith("external_call_"):
        raise BillingCalibrationError("逐调用对账缺少合法调用 ID")
    if type(attempt_ordinal) is not int or attempt_ordinal <= 0:
        raise BillingCalibrationError("逐调用对账序号必须为正数")
    if not provider_id.strip():
        raise BillingCalibrationError("逐调用对账缺少渠道")
    if len(evidence_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in evidence_sha256.lower()
    ):
        raise BillingCalibrationError("逐调用对账证据哈希无效")
    cache_read = int(cache_read_input_tokens)
    cache_creation = int(cache_creation_input_tokens)
    if cache_read < 0 or cache_creation < 0:
        raise BillingCalibrationError("逐调用缓存 Token 不能为负数")
    reconciliation = reconcile_provider_invoice(
        provider_id,
        estimated_cny=estimated_cny,
        provider_observed_cny=provider_billed_cny,
    )
    return CallBillingReconciliation(
        external_call_id=external_call_id,
        attempt_ordinal=attempt_ordinal,
        provider_id=provider_id,
        estimated_cny=reconciliation.estimated_cny,
        provider_billed_cny=reconciliation.provider_observed_cny,
        residual_cny=reconciliation.residual_cny,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
        observed_at=_parse_utc(observed_at).isoformat().replace("+00:00", "Z"),
        evidence_sha256=evidence_sha256.lower(),
    )


def record_call_invoice(runtime_path: Path, receipt: CallBillingReconciliation) -> bool:
    """Store immutable matched invoice evidence beside, never inside, a ledger.

    One file per call/attempt gives create-if-absent idempotency. Existing
    terminal receipts and conservative USD reservations remain unchanged.
    """
    row = receipt.as_dict()
    with closing(sqlite3.connect(runtime_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        match = connection.execute(
            "SELECT ec.provider FROM external_calls ec JOIN external_call_attempts a "
            "ON a.external_call_id=ec.external_call_id WHERE ec.external_call_id=? AND a.ordinal=?",
            (receipt.external_call_id, receipt.attempt_ordinal),
        ).fetchone()
    if match is None or match[0] != receipt.provider_id:
        raise BillingCalibrationError("账单与调用渠道或尝试序号不匹配")
    # Revalidate even directly constructed dataclass instances.
    validated = reconcile_call_invoice(
        receipt.external_call_id, attempt_ordinal=receipt.attempt_ordinal,
        provider_id=receipt.provider_id, estimated_cny=receipt.estimated_cny,
        provider_billed_cny=receipt.provider_billed_cny,
        cache_read_input_tokens=receipt.cache_read_input_tokens,
        cache_creation_input_tokens=receipt.cache_creation_input_tokens,
        observed_at=receipt.observed_at, evidence_sha256=receipt.evidence_sha256,
    ).as_dict()
    if row != validated:
        raise BillingCalibrationError("账单回执字段不一致")
    directory = runtime_path.with_name(runtime_path.name + ".invoices")
    directory.mkdir(exist_ok=True)
    key = hashlib.sha256(f"{receipt.external_call_id}:{receipt.attempt_ordinal}".encode()).hexdigest()
    target = directory / f"{key}.json"
    payload = json.dumps(validated, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=directory, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != payload:
                raise BillingCalibrationError("同一调用尝试已有不同账单；禁止静默覆盖")
            return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def load_call_invoices(runtime_path: Path) -> list[Mapping[str, Any]]:
    directory = runtime_path.with_name(runtime_path.name + ".invoices")
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]
    if any(not isinstance(row, Mapping) or type(row.get("attempt_ordinal")) is not int for row in rows):
        raise BillingCalibrationError("逐调用账单格式无效")
    return rows


__all__ = [
    "BilledCostEstimate",
    "BillingReconciliation",
    "CallBillingReconciliation",
    "BillingCalibrationError",
    "DEFAULT_BILLING_CALIBRATION_FILE",
    "calibration_receipt",
    "estimate_billed_cost_cny",
    "load_billing_calibration",
    "reconcile_provider_invoice",
    "reconcile_call_invoice",
    "record_call_invoice",
    "load_call_invoices",
]
