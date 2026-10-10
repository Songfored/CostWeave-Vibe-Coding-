"""Revisioned, round-scoped provider bills; never convert points into currency.

Aggregate bills identify a blended rate only. They cannot identify separate
input/output/cache prices, and allocations must not be called per-call bills.
"""
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from .experiment_lab import LabInputError, write_json
from .billing_calibration import reconcile_provider_invoice

CURRENCIES = {"CNY", "USD", "POINTS"}


class InvoiceBook:
    def __init__(self, root):
        self.path = root / "invoices.json"
        self.history = json.loads(self.path.read_text("utf-8")) if self.path.exists() else []

    def latest(self, round_id):
        return next((row for row in reversed(self.history) if row["round_id"] == round_id), None)

    def record(self, round_, data):
        if round_["mode"] != "real" or round_["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
            raise LabInputError("只能为已结束的真实实验登记账单。")
        if data.get("scope_confirmed") is not True:
            raise LabInputError("请确认账单金额只对应这一轮、这一接口；混入其他用量时无法准确对账。")
        currency = data.get("currency", "CNY")
        if currency not in CURRENCIES or type(data.get("amount")) is bool:
            raise LabInputError("账单单位或金额无效。")
        try:
            amount = Decimal(str(data.get("amount")))
            if not amount.is_finite() or amount < 0 or amount > Decimal("10000000"):
                raise LabInputError("账单金额须为有限非负数。")
            amount_text = format(amount.quantize(Decimal("0.0000000001")), "f")
        except (InvalidOperation, TypeError):
            raise LabInputError("账单金额格式无效。") from None
        evidence = str(data.get("source_reference", "")).strip()
        if not 1 <= len(evidence) <= 500:
            raise LabInputError("请填写账单来源或账单编号（不需要密钥）。")
        payload = {"round_id": round_["id"], "amount": amount_text, "currency": currency,
                   "source_reference": evidence, "scope_confirmed": True}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        old = self.latest(round_["id"])
        if old and old["evidence_sha256"] == fingerprint:
            return old
        if old and data.get("supersedes") != old["id"]:
            raise LabInputError("该轮已有账单；修正时需指定当前账单版本，旧记录会保留。")
        revision_hash = hashlib.sha256((fingerprint + (old["id"] if old else "")).encode()).hexdigest()
        row = {**payload, "id": "invoice_" + revision_hash[:24], "evidence_sha256": fingerprint,
               "created_at": datetime.now(UTC).isoformat(), "supersedes": old["id"] if old else None,
               "calibration_scope": round_["calibration_scope"], "origin": "user_reported_provider_bill",
               "externally_verified": False}
        history = [*self.history, row]
        write_json(self.path, history)
        self.history = history
        return row

    def calibration(self, scope, rounds):
        samples = []
        for round_ in rounds:
            invoice = self.latest(round_["id"])
            if not invoice or invoice["calibration_scope"] != scope:
                continue
            calls = round_["calls"]
            # A failed/ambiguous request may be billed without known tokens.
            if round_["status"] != "completed" or not calls or any(c["status"] != "completed" for c in calls):
                continue
            tokens = sum(c["usage"]["input_tokens"] + c["usage"]["output_tokens"] for c in calls)
            if tokens > 0:
                samples.append((invoice, tokens))
        if not samples:
            return None
        samples.sort(key=lambda sample: sample[0]["created_at"])
        currency = samples[-1][0]["currency"]
        samples = [(invoice, tokens) for invoice, tokens in samples if invoice["currency"] == currency][-10:]
        total = sum((Decimal(invoice["amount"]) for invoice, _ in samples), Decimal(0))
        tokens = sum(tokens for _, tokens in samples)
        rate = total * Decimal(1000000) / Decimal(tokens)
        return {"method": "observed_blended_per_million_total_tokens", "currency": currency,
                "rate": str(rate), "sample_rounds": len(samples), "sample_tokens": tokens,
                "invoice_ids": [i["id"] for i, _ in samples], "confidence": "limited_empirical",
                "separate_input_output_rates_identifiable": False,
                "note": "仅适用于相同接口配置与相似任务；包含当前输入/输出/缓存组合，不是供应商挂牌单价。"}

    def round_cost(self, round_):
        invoice = self.latest(round_["id"])
        estimate = round_.get("estimated_cost")
        if not invoice:
            return {"status": "awaiting_bill", "actual_amount": None, "estimated": estimate}
        residual = None
        if estimate and estimate["currency"] == invoice["currency"]:
            residual = str(Decimal(invoice["amount"]) - Decimal(estimate["amount"]))
            if invoice["currency"] == "CNY":
                # Keep the project's established CNY reconciliation validation.
                reconcile_provider_invoice(round_["connection"]["id"],
                    estimated_cny=float(estimate["amount"]), provider_observed_cny=float(invoice["amount"]))
        return {"status": "reconciled_from_user_bill", "actual_amount": invoice["amount"],
                "currency": invoice["currency"], "estimated": estimate, "residual": residual,
                "invoice": invoice, "per_call_actual_cost_known": False}
