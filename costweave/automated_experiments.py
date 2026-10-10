"""One finite experiment campaign per saved connection; no scheduler or model judge.

The shared Adapter/ExternalCallAuthority owns every network attempt. This module
only generates tasks, sequences bounded calls and reconciles round-level bills.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .adapters import (AdapterRuntimePolicy, ExperimentalAdapterConfig,
                       ExternalExecutionRequest, HTTPResponse)
from .automation_billing import InvoiceBook
from .automation_tasks import (CALLS_PER_ROUND, INPUT_BYTE_LIMIT, OUTPUT_TOKEN_LIMIT,
                               TASKSET_VERSION, experiment_tasks)
from .event_store import EventStore
from .experimental_calls import ExperimentBudget, ExperimentalCallStore
from .experiment_lab import (CompatibleLabAdapter, DeepSeekLabAdapter, OpenAILabAdapter, ResponsesLabAdapter,
                             LabInputError, endpoint, exact_result, protect, write_json, lab_adapter_type, PROVIDER_DEFAULT_MODEL)
from .planning_contracts import WorkOrder


def now():
    return datetime.now(UTC).isoformat()


class FixtureTransport:
    evidence_level = "E0-transport-fixture"

    def __init__(self, expected):
        self.expected = expected

    def post(self, **kwargs):
        # Synthetic counts are explicitly labelled; they never train cost models.
        value = {"id": "offline-fixture", "model": "offline-fixture",
                 "choices": [{"message": {"content": json.dumps(self.expected)}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 30}}
        return HTTPResponse(200, json.dumps(value).encode(), {}, 0)


class AutomatedExperiments:
    def __init__(self, lab, *, transport_factory=None):
        self.lab = lab
        self.root = lab.root / "automation"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = lab.lock
        self.rounds = {}
        self.worker = None
        self.transport_factory = transport_factory  # Tests only; never configured via HTTP.
        self.book = InvoiceBook(self.root)
        for path in (self.root / "rounds").glob("*/report.json"):
            row = json.loads(path.read_text("utf-8"))
            if row["status"] in {"queued", "running"}:
                row.update(status="interrupted", finished_at=now(),
                           message="进程曾中断；已停止，不自动重发可能已计费的请求。")
                self._persist(row)
            self.rounds[row["id"]] = row

    def _persist(self, row):
        write_json(self.root / "rounds" / row["id"] / "report.json", row)
        self.rounds[row["id"]] = row

    def active(self):
        return any(r["status"] in {"queued", "running"} for r in self.rounds.values())

    def save_and_enqueue(self, data):
        if data.get("auto_run") is not True:
            raise LabInputError("此入口会产生真实模型调用，请通过“保存接口并自动评测”提交。")
        with self.lock:
            if len([r for r in self.rounds.values() if r["status"] in {"queued", "running"}]) >= 16:
                raise LabInputError("队列已有 16 轮，请等待现有实验完成。")
            # Lost HTTP responses/repeated submissions must not create duplicate paid rounds.
            if not data.get("id") and data.get("api_key"):
                address = endpoint(str(data.get("endpoint", "")), data.get("profile", "compatible"))
                for saved in self.lab.connections.values():
                    if (saved["endpoint"] == address and saved["model"] == (PROVIDER_DEFAULT_MODEL if data.get("provider_default") is True else str(data.get("model", "")).strip())
                            and saved.get("provider_default", False) == data.get("provider_default", False)
                            and saved["profile"] == data.get("profile", "compatible")
                            and protect(base64.b64decode(saved["credential"]), decrypt=True).decode() == str(data["api_key"]).strip()):
                        data = {**data, "id": saved["id"]}
                        break
            # Save uses DPAPI and endpoint checks from the manual experiment lab.
            result = self.lab.save(data)
            return {**self.enqueue(result["id"], deduplicate=True), "connection_id": result["id"]}

    def _scope(self, connection):
        # DPAPI encryption is randomized; use a stable revision kept by lab.save.
        raw = json.dumps({k: connection.get(k) for k in
                          ("id", "endpoint", "model", "profile", "configuration_revision")}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def enqueue(self, connection_id=None, *, mode="real", deduplicate=False):
        with self.lock:
            if self.lab.busy:
                raise LabInputError("手动实验运行中，请结束后启动自动流程。")
            if mode not in {"real", "offline"}:
                raise LabInputError("无效实验模式。")
            if sum(r["status"] in {"queued", "running"} for r in self.rounds.values()) >= 16:
                raise LabInputError("实验队列已满。")
            if mode == "real":
                if connection_id not in self.lab.connections:
                    raise LabInputError("请先填写接口。")
                connection = dict(self.lab.connections[connection_id])
            else:
                connection = {"id": "offline-fixture", "model": "offline-fixture", "label": "离线全流程演示",
                              "endpoint": "https://example.com/v1/chat/completions", "profile": "compatible",
                              "configuration_revision": "offline-v1"}
            scope = self._scope(connection)
            if deduplicate:
                previous = next((r for r in self.rounds.values() if r["calibration_scope"] == scope and r["mode"] == mode), None)
                if previous:
                    return {"id": previous["id"], "deduplicated": True}
            identity = "auto_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(6)
            row = {"id": identity, "mode": mode, "status": "queued", "created_at": now(),
                   "connection": {k: v for k, v in connection.items() if k != "credential"},
                   "calibration_scope": scope, "taskset": TASKSET_VERSION, "tasks": experiment_tasks(),
                   "limits": {"calls": CALLS_PER_ROUND, "attempts_per_call": 1,
                              "output_tokens_per_call": OUTPUT_TOKEN_LIMIT, "input_bytes_per_call": INPUT_BYTE_LIMIT,
                              "timeout_seconds_per_call": 60, "wall_seconds": 600},
                   "accounting_mode": "usage_only", "monetary_limit": None,
                   "authorization": "user_requested_finite_automatic_experiments" if mode == "real" else "offline_fixture",
                   "calls": [], "cancel_requested": False, "message": "已排队",
                   "commercial_claims_allowed": False, "production_routing_changed": False}
            self._persist(row)
            # In-memory credential snapshot avoids reading edited credentials mid-run.
            row_directory = self.root / "rounds" / identity
            if mode == "real":
                write_json(row_directory / "connection.private.json", connection)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._drain, daemon=True)
                self.worker.start()
            return {"id": identity, "deduplicated": False}

    def _drain(self):
        while True:
            with self.lock:
                row = next((r for r in self.rounds.values() if r["status"] == "queued"), None)
                if row is None:
                    self.worker = None
                    return
                row.update(status="running", started_at=now(), message="自动实验运行中")
                row["calibration_snapshot"] = self.book.calibration(row["calibration_scope"], self.rounds.values()) if row["mode"] == "real" else None
                self._persist(row)
            try:
                self._execute_round(row)
            except Exception:
                with self.lock:
                    row.update(status="failed", finished_at=now(), message="本地实验执行异常；已停止该轮，不自动重发。")
                    self._persist(row)

    def _execute_round(self, row):
        directory = self.root / "rounds" / row["id"]
        connection = (json.loads((directory / "connection.private.json").read_text("utf-8"))
                      if row["mode"] == "real" else row["connection"])
        database = directory / "runtime.sqlite3"
        EventStore(database)
        store = ExperimentalCallStore(database)
        exp_id = "experiment_" + row["id"]
        store.register_experiment(ExperimentBudget(
            experiment_id=exp_id, total_cost_limit_usd=0, provider_cost_limits_usd={connection["id"]: 0},
            max_logical_calls=CALLS_PER_ROUND, max_attempts_per_call=1,
            purpose="finite_automatic_usage_only_experiment", accounting_mode="usage_only",
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat()))
        begin = time.monotonic()
        for task in row["tasks"]:
            with self.lock:
                if row["cancel_requested"] or time.monotonic()-begin >= 600:
                    row.update(status="cancelled", message="已停止后续任务；在途请求可能已计费。")
                    break
                call = {"task": task["id"], "category": task["category"], "status": "running", "started_at": now()}
                row["calls"].append(call)
                self._persist(row)  # Persist intent before any model request.
            try:
                self._execute_task(row, task, call, connection, store, exp_id)
            except Exception:
                # Recover the guarded ledger even when the adapter raises after sending.
                with store._connection() as db:
                    records = db.execute("SELECT external_call_id FROM external_calls WHERE experiment_id=? AND task_id=?",
                                         (exp_id, "task_" + task["id"])).fetchall()
                for record in records:
                    saved = store.call(record[0])
                    call.update(external_call_id=record[0], telemetry=saved.get("telemetry"),
                                attempts=[{k: a[k] for k in ("ordinal", "status", "status_code", "error_code")} for a in saved["attempts"]])
                call.update(status="failed", billing_status="unknown_after_failure",
                            message="调用失败、用量缺失或协议不兼容；本接口本轮停止，不自动重试。")
                if connection.get("provider_default"):
                    call["message"] += " 本次未发送 model 字段；若接口要求型号，请填写服务商调用别名后再试。"
                row.update(status="failed", message=call["message"])
            finally:
                call["finished_at"] = now()
                with self.lock:
                    self._persist(row)
            if call["status"] == "failed":
                break
        else:
            row.update(status="completed", message="本轮自动实验完成，等待提供商账单。" if row["mode"] == "real" else "离线全流程完成；结果和用量均为测试替身，不代表模型能力。")
        try:
            store.complete_experiment(exp_id)
        except Exception:
            row["ledger_not_closed"] = True
        with self.lock:
            row["finished_at"] = now()
            row["wall_seconds"] = round(time.monotonic()-begin, 3)
            row["summary"] = self._summary(row)
            snapshot = row.get("calibration_snapshot")
            if snapshot and all(c["status"] == "completed" for c in row["calls"]):
                total_tokens = row["summary"]["input_tokens"] + row["summary"]["output_tokens"]
                row["estimated_cost"] = {"amount": str(Decimal(snapshot["rate"])*Decimal(total_tokens)/Decimal(1000000)),
                                         "currency": snapshot["currency"], "method": snapshot["method"],
                                         "invoice_ids": snapshot["invoice_ids"], "is_actual_bill": False}
            self._persist(row)

    def _execute_task(self, row, task, call, connection, store, exp_id):
        work = WorkOrder(work_order_id="work_order_" + row["id"] + task["id"],
            task_id="task_" + task["id"], objective=task["prompt"], dependencies=(),
            input_artifacts=(), context_manifest=(), assumptions=(), allowed_tools=(),
            forbidden_actions=("Do not use tools or external information.",), output_schema=task["schema"],
            acceptance_tests=("Strict JSON types and values must match the independent reference.",),
            cost_limit=0, time_limit_ms=60000, escalation_conditions=("Cannot fulfill the output contract.",),
            idempotency_key=row["id"] + "_" + task["id"])
        request = ExternalExecutionRequest(exp_id, "run_" + row["id"], work.task_id, work,
                                           OUTPUT_TOKEN_LIMIT, work.idempotency_key)
        adapter_type = lab_adapter_type(connection["profile"], connection.get("provider_default", False))
        env = "COSTWEAVE_AUTO_" + secrets.token_hex(12).upper()
        transport = (FixtureTransport(task["expected"]) if row["mode"] == "offline" else
                     self.transport_factory(task) if self.transport_factory else None)
        adapter = adapter_type(ExperimentalAdapterConfig(
            adapter_id="automatic-experiments", provider=connection["id"], endpoint=connection["endpoint"],
            model_id=connection["model"], deployment="remote", secret_env=env,
            timeout_seconds=60, max_retries=0, reasoning_effort="low", accounting_mode="usage_only"),
            store=store, policy=AdapterRuntimePolicy(True, True), transport=transport)
        estimate = adapter.estimate_reservation(request)
        if estimate["input_token_upper_bound"] > INPUT_BYTE_LIMIT:
            raise LabInputError("input bound exceeded")
        os.environ[env] = ("offline-lab-fixture-value" if row["mode"] == "offline" else
                           protect(base64.b64decode(connection["credential"]), decrypt=True).decode())
        try:
            receipt = adapter.execute(request)
        finally:
            os.environ.pop(env, None)
        telemetry = dict(receipt.telemetry)
        content = str(receipt.result.output.get("content", ""))
        usage = {k: telemetry[k] for k in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")}
        if usage["input_tokens"] > INPUT_BYTE_LIMIT or usage["output_tokens"] > OUTPUT_TOKEN_LIMIT:
            call.update(usage=usage, telemetry=telemetry, external_call_id=receipt.external_call_id)
            raise LabInputError("provider token bound violation")
        call.update(status="completed", output=content, usage=usage, telemetry=telemetry,
                    external_call_id=receipt.external_call_id, validation=exact_result(content, task["expected"]),
                    latency_ms=telemetry["request_latency_ms"], accounting_mode="usage_only", actual_cost=None,
                    input_bytes_bound=estimate["input_token_upper_bound"], usage_synthetic=row["mode"] == "offline")

    @staticmethod
    def _summary(row):
        completed = [c for c in row["calls"] if c["status"] == "completed"]
        passed = sum(bool(c["validation"]["accepted"]) for c in completed)
        return {"planned": CALLS_PER_ROUND, "completed": len(completed), "passed": passed,
                "failed_calls": sum(c["status"] == "failed" for c in row["calls"]),
                "unrun": CALLS_PER_ROUND - len(row["calls"]),
                "pass_fraction_of_planned": passed/CALLS_PER_ROUND,
                "input_tokens": sum(c["usage"]["input_tokens"] for c in completed),
                "output_tokens": sum(c["usage"]["output_tokens"] for c in completed),
                "median_latency_ms": statistics.median(c["latency_ms"] for c in completed) if completed else None,
                "usage_complete": len(completed) == len(row["calls"]),
                "benchmark_claims_allowed": False}

    def cancel(self, identity):
        with self.lock:
            row = self.rounds.get(identity)
            if not row:
                raise LabInputError("找不到实验轮次。")
            if row["status"] == "queued":
                row.update(status="cancelled", finished_at=now(), message="排队任务已取消，未发起请求。")
            elif row["status"] == "running":
                row["cancel_requested"] = True
            self._persist(row)
            return {"id": identity, "cancel_requested": True}

    def invoice(self, data):
        with self.lock:
            row = self.rounds.get(data.get("round_id"))
            if row is None:
                raise LabInputError("找不到对应实验轮次。")
            invoice = self.book.record(row, data)
            calibration = self.book.calibration(row["calibration_scope"], self.rounds.values())
            write_json(self.root / "rounds" / row["id"] / "reconciled-report.json",
                       {**row, "cost": self.book.round_cost(row), "calibration": calibration})
            return {"invoice": invoice, "calibration": calibration}

    def status(self):
        with self.lock:
            rows = []
            for row in sorted(self.rounds.values(), key=lambda r: r["created_at"], reverse=True):
                rows.append({**row, "cost": self.book.round_cost(row),
                             "calibration": self.book.calibration(row["calibration_scope"], self.rounds.values())})
            return json.loads(json.dumps({"rounds": rows[:100], "busy": self.active(), "taskset": TASKSET_VERSION,
                    "report_directory": str(self.root / "rounds"), "invoice_history": self.book.history,
                    "policy": {"calls_per_round": CALLS_PER_ROUND, "output_tokens_per_call": OUTPUT_TOKEN_LIMIT,
                               "unknown_prices_allowed": True, "monetary_cap_guaranteed": False,
                               "auto_retry": False, "automatic_repeat_rounds": False}}))
