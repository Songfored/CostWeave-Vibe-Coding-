"""Local, bounded manual experiments; reuses the external-call authority.

This console does not turn the production simulator into a real DAG executor.
It never reads the legacy .env.local or spends on startup/configuration.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import re
import secrets
import threading
import webbrowser
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .adapters import (AdapterRuntimePolicy, DeepSeekChatHTTPAdapter,
                       ExperimentalAdapterConfig, ExternalExecutionRequest,
                       OpenAICompatibleHTTPAdapter)
from .event_store import EventStore
from .experimental_calls import ExperimentBudget, ExperimentalCallError, ExperimentalCallStore
from .provider_setup import _atomic_write_text, _validate_api_key
from .real_smoke_eval import _work_order
from .security_boundary import validate_endpoint

PROFILES = ("compatible", "deepseek", "openai", "responses")
MAX_OUTPUT_TOKENS = 512


class LabInputError(ValueError):
    """A controlled, credential-free validation message suitable for the UI."""


TASKS = {
    "arithmetic": {"title": "计算与格式", "expected": {"answer": 402, "checksum": "cswae"}},
    "orders": {"title": "订单统计", "expected": {"ids": ["A", "C"], "quantity": 4, "revenue": 51},
               "prompt": "Return only JSON with exactly ids, quantity, revenue. Orders: A quantity 3 unit price 12 status paid; B quantity 2 unit price 20 status cancelled; C quantity 1 unit price 15 status paid. Count only paid orders, preserving ID order. Compute total quantity and total revenue."},
    "empty": {"title": "空结果边界", "expected": {"ids": [], "quantity": 0, "revenue": 0},
              "prompt": "Return only JSON with exactly ids, quantity, revenue. Orders: B quantity 2 unit price 20 status cancelled. Count only paid orders, preserving ID order. Compute total quantity and total revenue. Empty results must use an empty list and numeric zeros."},
}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def protect(value: bytes, *, decrypt: bool = False) -> bytes:
    """Windows DPAPI, scoped to the current signed-in user; no plaintext fallback."""
    if os.name != "nt":
        raise LabInputError("此启动入口使用 Windows 当前用户加密存储。")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise LabInputError("无法读取或保存当前 Windows 用户的加密配置。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def endpoint(value: str, profile: str = "compatible") -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.query or parsed.fragment or len(value) > 1000:
        raise LabInputError("接口地址不可包含查询参数或片段。")
    suffix = "/responses" if profile == "responses" else "/chat/completions"
    if value.endswith(("/responses", "/chat/completions")) and not value.endswith(suffix):
        raise LabInputError("接口地址与所选协议不一致。")
    if not value.endswith(suffix):
        value += suffix
    validate_endpoint(value, deployment="remote")
    return value


def exact_result(content: str, expected: dict) -> dict:
    def equal(left, right):
        if type(left) is not type(right):
            return False
        if isinstance(right, dict):
            return left.keys() == right.keys() and all(equal(left[k], v) for k, v in right.items())
        if isinstance(right, list):
            return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
        return left == right
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LabInputError("duplicate key")
            result[key] = value
        return result
    try:
        result = json.loads(content, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        accepted = type(result) is dict and equal(result, expected)
        return {"accepted": bool(accepted), "reason": "通过" if accepted else "字段、类型或计算结果不匹配"}
    except (ValueError, TypeError, RecursionError):
        return {"accepted": False, "reason": "未返回严格 JSON 对象（不接受重复字段或 Markdown 围栏）"}


def require_usage(value):
    usage = value.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                                         for k in ("prompt_tokens", "completion_tokens")):
        raise ExperimentalCallError("provider_usage_missing_or_invalid")


class CompatibleLabAdapter(OpenAICompatibleHTTPAdapter):
    PROFILE_REVISION = "manual-lab-compatible-v1"

    def build_payload(self, request):
        payload = dict(super().build_payload(request))
        payload.pop("reasoning_effort", None)
        return payload

    def parse_response(self, value):
        require_usage(value)
        return super().parse_response(value)


class DeepSeekLabAdapter(DeepSeekChatHTTPAdapter):
    def parse_response(self, value):
        require_usage(value)
        return super().parse_response(value)


class OpenAILabAdapter(CompatibleLabAdapter):
    PROFILE_REVISION = "manual-lab-openai-chat-v1"

    def build_payload(self, request):
        payload = dict(super().build_payload(request))
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        return payload


class ResponsesLabAdapter(CompatibleLabAdapter):
    PROFILE_REVISION = "experiment-responses-text-v1"

    def build_payload(self, request):
        chat = super().build_payload(request)
        return {"model": self.config.model_id, "instructions": chat["messages"][0]["content"],
                "input": chat["messages"][1]["content"], "max_output_tokens": request.max_output_tokens,
                "store": False}

    def parse_response(self, value):
        if value.get("status") != "completed" or not isinstance(value.get("output"), list):
            raise ExperimentalCallError("responses_not_completed")
        parts = []
        for item in value["output"]:
            if not isinstance(item, dict):
                raise ExperimentalCallError("invalid_response_item")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or not isinstance(item.get("content"), list):
                raise ExperimentalCallError("unexpected_response_item")
            for part in item["content"]:
                if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise ExperimentalCallError("nontext_or_refused_response")
                parts.append(part["text"])
        if not parts:
            raise ExperimentalCallError("response_text_missing")
        usage = value.get("usage")
        if not isinstance(usage, dict):
            raise ExperimentalCallError("response_usage_missing")
        translated = {"id": value.get("id"), "model": value.get("model"),
                      "choices": [{"message": {"content": "".join(parts)}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                                "prompt_tokens_details": usage.get("input_tokens_details", {}),
                                "completion_tokens_details": usage.get("output_tokens_details", {})}}
        return super().parse_response(translated)


PROVIDER_DEFAULT_MODEL = "provider-default-unknown"


def lab_adapter_type(profile, provider_default=False):
    base = {"compatible": CompatibleLabAdapter, "openai": OpenAILabAdapter,
            "deepseek": DeepSeekLabAdapter, "responses": ResponsesLabAdapter}[profile]
    if not provider_default:
        return base

    class DefaultModelAdapter(base):
        PROFILE_REVISION = base.PROFILE_REVISION + "-omit-model-v1"

        def build_payload(self, request):
            payload = dict(super().build_payload(request))
            payload.pop("model", None)
            return payload

    return DefaultModelAdapter


class ExperimentLab:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_file = self.root / "connections.json"
        self.lock = threading.RLock()
        self.busy = False
        self.jobs = {}
        self.connections = json.loads(self.config_file.read_text("utf-8")) if self.config_file.exists() else {}
        # Pending records survive interruptions, never resume automatically.
        for report in sorted((self.root / "reports").glob("*.json"))[-100:]:
            row = json.loads(report.read_text("utf-8"))
            if row.get("status") == "running":
                row.update(status="interrupted", message="上次进程中断；费用可能已发生，不会自动重试。")
                write_json(report, row)
            self.jobs[row["id"]] = row

    def status(self):
        with self.lock:
            public = [{k: v for k, v in item.items() if k != "credential"}
                      for item in self.connections.values()]
            return {"connections": public, "jobs": list(self.jobs.values())[-100:],
                    "busy": self.busy, "tasks": [{"id": k, **v} for k, v in TASKS.items()],
                    "report_directory": str(self.root / "reports"),
                    "max_output_tokens": MAX_OUTPUT_TOKENS}

    def discover_models(self, data):
        from .adapters.http import UrllibHTTPTransport
        profile = data.get("profile", "compatible")
        if profile not in PROFILES:
            raise LabInputError("不支持所选协议。")
        address = endpoint(str(data.get("endpoint", "")), profile)
        key = data.get("api_key", "")
        if not key:
            with self.lock:
                old = self.connections.get(data.get("id"), {})
                if old.get("endpoint") != address or not old.get("credential"):
                    raise LabInputError("请填写 API Key；修改地址后需要重新填写。")
                key = protect(base64.b64decode(old["credential"]), decrypt=True).decode()
        key = _validate_api_key(key)
        suffix = "/responses" if profile == "responses" else "/chat/completions"
        url = address[:-len(suffix)] + "/models"
        try:
            response = UrllibHTTPTransport().get_models(url=url, headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
            if response.status_code != 200:
                raise LabInputError(f"模型列表读取失败（HTTP {response.status_code}）。可手动填写服务商的调用别名；不会猜测型号或试发推理请求。")
            value = json.loads(response.body)
            rows = value.get("data") if isinstance(value, dict) else None
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError("invalid list")
            models = sorted({item["id"] for item in rows if isinstance(item, dict)
                             and isinstance(item.get("id"), str) and 0 < len(item["id"]) <= 180
                             and all(ord(c) >= 33 and not c.isspace() for c in item["id"])
                             and key not in item["id"]})
            return {"models": models, "source": "provider_models_endpoint", "verified_identity": False,
                    "message": "服务商声明的可调用型号，未经身份验证。" if models else "接口未提供可用型号；请向服务商获取调用别名。"}
        except LabInputError:
            raise
        except Exception:
            raise LabInputError("无法读取模型列表：请检查地址、密钥、网络或服务商是否支持 /models。不会自动调用模型猜测身份。") from None

    def save(self, data):
        with self.lock:
            if self.busy:
                raise LabInputError("实验运行中，请完成后再修改接口。")
            identity = data.get("id") or "channel_" + secrets.token_hex(8)
            if not re.fullmatch(r"channel_[a-f0-9]{16}", identity):
                raise LabInputError("接口编号无效。")
            old = self.connections.get(identity, {})
            profile = data.get("profile", "compatible")
            if not isinstance(profile, str) or profile not in PROFILES:
                raise LabInputError("不支持所选协议。")
            provider_default = data.get("provider_default", False)
            if type(provider_default) is not bool:
                raise LabInputError("默认模型选项无效。")
            model = PROVIDER_DEFAULT_MODEL if provider_default else str(data.get("model", "")).strip()
            label = str(data.get("label", "")).strip()
            if not label or len(label) > 80 or not model or len(model) > 180 or any(c.isspace() for c in model):
                raise LabInputError("请填写名称及调用别名，或选择使用接口默认模型。")
            address = endpoint(str(data.get("endpoint", "")), profile)
            credential = old.get("credential")
            raw_key = data.get("api_key", "")
            if raw_key:
                credential = base64.b64encode(protect(_validate_api_key(raw_key).encode())).decode()
            if not credential:
                raise LabInputError("首次保存请填写 API Key。")
            if old and not raw_key and address != old["endpoint"]:
                raise LabInputError("修改接口地址时请重新填写 API Key，避免把旧密钥发送到新地址。")
            prices = {}
            for field in ("input_price", "output_price"):
                value = data.get(field)
                if value in (None, ""):
                    prices[field] = None
                else:
                    number = float(value)
                    if type(value) is bool or not math.isfinite(number) or not 0 <= number <= 10000:
                        raise LabInputError("价格应为有限非负数，单位为美元 / 百万 Token。")
                    prices[field] = number
            item = {"id": identity, "label": label, "model": model, "endpoint": address,
                    "profile": profile, "provider_default": provider_default, "credential": credential, **prices}
            same_secret = bool(old) and (not raw_key or protect(base64.b64decode(old["credential"]), decrypt=True).decode() == raw_key)
            unchanged = same_secret and old.get("provider_default", False) == provider_default and all(old.get(k) == item[k] for k in ("endpoint", "model", "profile"))
            item["configuration_revision"] = (old.get("configuration_revision", secrets.token_hex(12))
                                                 if unchanged else secrets.token_hex(12))
            updated = {**self.connections, identity: item}
            write_json(self.config_file, updated)
            self.connections = updated
            return {"saved": True, "id": identity}

    def _persist(self, row):
        write_json(self.root / "reports" / (row["id"] + ".json"), row)
        self.jobs[row["id"]] = row

    def manual(self, data):
        with self.lock:
            task = data.get("task")
            content = data.get("output")
            if not isinstance(task, str) or task not in TASKS or not isinstance(content, str) or not 1 <= len(content) <= 16000:
                raise LabInputError("请选择任务并粘贴原始答案（最多 16000 字）。")
            claimed_model = str(data.get("model", "未填写"))[:180]
            row = {"id": "lab_" + secrets.token_hex(12), "mode": "manual", "task": task,
                   "status": "completed", "created_at": datetime.now(UTC).isoformat(),
                   "output": content, "validation": exact_result(content, TASKS[task]["expected"]),
                   "message": "人工提交结果；接口身份、Token 和费用未自动核验。",
                   "external_call_attempts": 0, "benchmark_claims_allowed": False}
            row["claimed_model"] = claimed_model
            self._persist(row)
            return {"id": row["id"]}

    def start(self, data):
        with self.lock:
            automation = getattr(self, "automation", None)
            if automation is not None and automation.active():
                raise LabInputError("自动实验运行中，请等待完成。")
            if self.busy:
                raise LabInputError("已有实验运行；不会并发重复调用。")
            mode = data.get("mode", "offline")
            if not isinstance(mode, str) or mode not in {"offline", "real"}:
                raise LabInputError("实验模式无效。")
            task = data.get("task", "arithmetic")
            if not isinstance(task, str) or task not in TASKS:
                raise LabInputError("请选择内置评测任务。")
            item = None
            budget = 0.0
            if mode == "real":
                if data.get("authorize") is not True:
                    raise LabInputError("需要在页面明确授权本次付费调用。")
                item = self.connections.get(data.get("connection"))
                if not item:
                    raise LabInputError("请先保存接口。")
                if item["input_price"] is None or item["output_price"] is None:
                    raise LabInputError("未知计费无法落实金额预算。请补充提供商美元单价；点数计费暂请使用人工评测入口。")
                budget = float(data.get("budget_usd", 0))
                if not math.isfinite(budget) or not 0 < budget <= 0.5:
                    raise LabInputError("本次预算须在 0 到 0.50 美元之间。")
            identity = "lab_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(6)
            row = {"id": identity, "mode": mode, "task": task, "status": "running",
                   "created_at": datetime.now(UTC).isoformat(), "authorized_budget_usd": budget,
                   "max_logical_calls": 1, "max_attempts": 1, "max_output_tokens": MAX_OUTPUT_TOKENS,
                   "benchmark_claims_allowed": False, "invoice_reconciled": False,
                   "message": "正在运行"}
            self._persist(row)
            self.busy = True
            threading.Thread(target=self._run, args=(row.copy(), item, budget), daemon=True).start()
            return {"id": identity}

    def _run(self, row, item, budget):
        try:
            task = TASKS[row["task"]]
            if row["mode"] == "offline":
                content = json.dumps(task["expected"], ensure_ascii=False)
                row.update(external_call_attempts=0, evidence="offline-validator-fixture",
                           message="离线演示完成；不代表模型能力。")
            else:
                identity = row["id"]
                database = self.root / "runs" / identity / "runtime.sqlite3"
                database.parent.mkdir(parents=True)
                EventStore(database)
                store = ExperimentalCallStore(database)
                exp_id = "experiment_" + identity
                store.register_experiment(ExperimentBudget(
                    experiment_id=exp_id, total_cost_limit_usd=budget,
                    provider_cost_limits_usd={item["id"]: budget}, max_logical_calls=1,
                    max_attempts_per_call=1, purpose="manual_lab_single_task",
                    expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat()))
                order = _work_order(item["id"], budget)
                order = replace(order, work_order_id="work_order_" + identity,
                                task_id="task_" + identity, idempotency_key="order_" + identity)
                if "prompt" in task:
                    order = replace(order, objective=task["prompt"], assumptions=(),
                                    output_schema={"type": "object", "required": list(task["expected"]),
                                                   "additionalProperties": False,
                                                   "properties": {"ids": {"type": "array", "items": {"type": "string"}},
                                                                  "quantity": {"type": "integer"}, "revenue": {"type": "integer"}}},
                                    acceptance_tests=("Result matches deterministic reference exactly.",))
                request = ExternalExecutionRequest(experiment_id=exp_id, run_id="run_" + identity,
                    task_id=order.task_id, work_order=order, max_output_tokens=MAX_OUTPUT_TOKENS,
                    idempotency_key="call_" + identity)
                env_name = "COSTWEAVE_LAB_" + secrets.token_hex(12).upper()
                adapter_class = lab_adapter_type(item["profile"], item.get("provider_default", False))
                adapter = adapter_class(ExperimentalAdapterConfig(
                    adapter_id="manual-lab", provider=item["id"], endpoint=item["endpoint"],
                    model_id=item["model"], deployment="remote", input_price_per_mtok_usd=item["input_price"],
                    output_price_per_mtok_usd=item["output_price"], secret_env=env_name,
                    reasoning_effort="low", timeout_seconds=45, max_retries=0), store=store,
                    policy=AdapterRuntimePolicy(True, True))
                estimate = adapter.estimate_reservation(request)
                if estimate["required_work_order_cost_limit_usd"] > budget:
                    raise LabInputError("单次调用的保守费用预留超过本次预算，已在联网前阻止。")
                row.update(connection={k: v for k, v in item.items() if k != "credential"},
                           reservation=estimate, evidence="real-call-unvalidated")
                os.environ[env_name] = protect(base64.b64decode(item["credential"]), decrypt=True).decode()
                try:
                    receipt = adapter.execute(request)
                finally:
                    os.environ.pop(env_name, None)
                content = str(receipt.result.output.get("content", ""))
                row.update(telemetry=dict(receipt.telemetry), external_call_id=receipt.external_call_id,
                           message="调用完成；费用为按填写单价估算，未与账单核对。")
            row.update(status="completed", output=content,
                       validation=exact_result(content, task["expected"]))
        except Exception as exc:
            # Never reflect provider response bodies, credentials or arbitrary exceptions.
            message = "调用未完成；请检查接口类型、密钥、模型权限和余额。不会自动重试。"
            if str(exc) == "单次调用的保守费用预留超过本次预算，已在联网前阻止。":
                message = str(exc)
            row.update(status="failed", error_type=type(exc).__name__, message=message,
                       cost_status="unknown_if_request_was_sent")
        finally:
            row["finished_at"] = datetime.now(UTC).isoformat()
            with self.lock:
                try:
                    self._persist(row)
                finally:
                    self.busy = False


def build_lab_server(root: Path, port: int = 0):
    # Imports deferred so callers can set isolated paths before server globals load.
    from http import HTTPStatus
    from .catalog_store import CatalogStore
    from .server import CostWeaveHandler, RequestError, build_server

    lab = ExperimentLab(root)
    from .automated_experiments import AutomatedExperiments
    automation = AutomatedExperiments(lab)
    lab.automation = automation
    class LabHandler(CostWeaveHandler):
        def do_PUT(self):
            self._cancel_absolute_deadline()
            self._drain_rejected_body(max_bytes=32000)
            self._json({"message": "此入口不支持该操作。"}, HTTPStatus.METHOD_NOT_ALLOWED)

        do_DELETE = do_PUT
        do_PATCH = do_PUT

        def do_GET(self):
            self._cancel_absolute_deadline()
            path = urlparse(self.path).path
            try:
                self._authorize_request(path)
                if path == "/api/v1/lab":
                    self._json(lab.status())
                elif path == "/api/v1/lab/automation":
                    self._json({**automation.status(), "connections": lab.status()["connections"]})
                elif path in {"/", "/auto.html", "/auto.js", "/auto.css", "/lab.html", "/lab.js", "/lab.css"}:
                    self._static("/auto.html" if path == "/" else path)
                else:
                    self._json({"message": "页面不存在"}, HTTPStatus.NOT_FOUND)
            except RequestError as exc:
                self._json({"message": str(exc)}, exc.status)
            except ValueError:
                self._json({"message": "请求参数无效。"}, HTTPStatus.BAD_REQUEST)

        def do_POST(self):
            self._cancel_absolute_deadline()
            path = urlparse(self.path).path
            try:
                self._authorize_request(path, require_json=True)
                if path not in {"/api/v1/lab/connections", "/api/v1/lab/run", "/api/v1/lab/manual", "/api/v1/lab/stop",
                                "/api/v1/lab/automation/connect", "/api/v1/lab/automation/run",
                                "/api/v1/lab/automation/cancel", "/api/v1/lab/automation/invoice", "/api/v1/lab/automation/models"}:
                    self._drain_rejected_body(max_bytes=32000)
                    self._json({"message": "接口不存在"}, HTTPStatus.NOT_FOUND)
                    return
                data = self._read_write_json(max_bytes=32000)
                if "/automation/" in path:
                    action = path.rsplit("/", 1)[1]
                    if action == "models":
                        value = lab.discover_models(data)
                    elif action == "connect":
                        value = automation.save_and_enqueue(data)
                    elif action == "invoice":
                        value = automation.invoice(data)
                    elif action == "cancel":
                        value = automation.cancel(data.get("id"))
                    else:
                        if data.get("mode", "real") == "real" and data.get("authorize") is not True:
                            raise LabInputError("开始新一轮需要明确授权。")
                        value = automation.enqueue(data.get("connection"), mode=data.get("mode", "real"))
                    self._json(value)
                    return
                if path.endswith("/stop"):
                    with lab.lock:
                        if lab.busy or automation.active():
                            raise LabInputError("请等当前实验完成后关闭。")
                        self._json({"stopped": True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                operation = {"connections": lab.save, "run": lab.start, "manual": lab.manual}[path.rsplit("/", 1)[1]]
                self._json(operation(data))
            except RequestError as exc:
                self._drain_rejected_body(max_bytes=32000)
                self._json({"message": str(exc)}, exc.status)
            except LabInputError as exc:
                self._json({"message": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (ValueError, ExperimentalCallError):
                self._json({"message": "配置或实验参数无效。请检查必填项、HTTPS 地址、单价及预算；修改地址时需重填密钥。"}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self._json({"message": "本地配置读写失败；请检查目录权限。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    server = build_server(port=port, catalog_store=CatalogStore(root / "catalog.json"),
                          runtime_db_path=str(root / "console.sqlite3"))
    server.RequestHandlerClass = LabHandler
    server.lab = lab
    return server


def main():
    parser = argparse.ArgumentParser(description="CostWeave 自动实验台")
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "work/manual-lab")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = args.data.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # One process owns these configuration files, including across double clicks.
    import msvcrt
    instance_lock = (root / "session.lock").open("a+b")
    if instance_lock.tell() == 0:
        instance_lock.write(b"0")
        instance_lock.flush()
    instance_lock.seek(0)
    try:
        msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            url = protect((root / "session.dpapi").read_bytes(), decrypt=True).decode()
            print("实验台已经运行：\n" + url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
        except (OSError, ValueError):
            print("实验台正在启动，请稍后再次打开。", flush=True)
        return
    # Explicitly isolate from legacy local settings and paid switches.
    os.environ.update(COSTWEAVE_CATALOG_PATH=str(root / "catalog.json"),
                      COSTWEAVE_RUNTIME_DB=str(root / "console.sqlite3"),
                      COSTWEAVE_EXPERIMENTAL_ADAPTERS="0", COSTWEAVE_PAID_CALLS="0",
                      COSTWEAVE_AURORA_PAID_AUTHORIZED="0")
    server = build_lab_server(root, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/#session={server.session_token}"
    (root / "session.dpapi").write_bytes(protect(url.encode()))
    print("CostWeave 自动实验台（可在页面底部停止服务）\n" + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
