from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .domain import WorkerProfile
from .model_catalog import MODELS, catalog_metadata


SCHEMA_VERSION = 1
MODEL_FIELDS = {item.name for item in fields(WorkerProfile)}
LIST_FIELDS = {"modalities", "tools", "strengths", "limitations"}
FLOAT_FIELDS = {
    "cost_per_task",
    "latency_factor",
    "reliability",
    "reasoning",
    "speed",
    "input_price_per_mtok",
    "output_price_per_mtok",
    "data_confidence",
    "availability",
}
OPTIONAL_FLOAT_FIELDS = {"cached_input_price_per_mtok"}
INT_FIELDS = {"context_window", "max_output_tokens"}
BOOL_FIELDS = {"local", "preview", "routable", "custom"}
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,99}$")


class CatalogError(ValueError):
    pass


class CatalogConflictError(CatalogError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clone(worker: WorkerProfile) -> WorkerProfile:
    return WorkerProfile(**worker.to_dict())


def _list_value(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise CatalogError(f"{field_name} 不是有效 JSON 数组") from exc
        else:
            value = [part.strip() for part in re.split(r"[|；;]", stripped) if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise CatalogError(f"{field_name} 必须是字符串数组")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"{field_name} 只能包含非空字符串")
        if len(item.strip()) > 240:
            raise CatalogError(f"{field_name} 中的单项长度不能超过 240")
        result.append(item.strip())
    if len(result) > 64:
        raise CatalogError(f"{field_name} 最多允许 64 项")
    return list(dict.fromkeys(result))


def _bool_value(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "是"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "0", "no", "否", ""}:
        return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise CatalogError(f"{field_name} 必须是布尔值")


def _float_value(value: Any, field_name: str, *, optional: bool = False) -> float | None:
    if optional and value in {None, ""}:
        return None
    if isinstance(value, bool):
        raise CatalogError(f"{field_name} 必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{field_name} 必须是数字") from exc
    if number != number or number in {float("inf"), float("-inf")}:
        raise CatalogError(f"{field_name} 必须是有限数字")
    return number


def _capabilities(value: Any) -> dict[str, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CatalogError("capabilities 不是有效 JSON 对象") from exc
    if not isinstance(value, dict) or not value:
        raise CatalogError("capabilities 必须是非空对象")
    if len(value) > 80:
        raise CatalogError("capabilities 最多允许 80 项")
    result: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", key):
            raise CatalogError(f"无效能力名称：{key}")
        number = _float_value(raw, f"capabilities.{key}")
        if number is None or not 0 <= number <= 1:
            raise CatalogError(f"capabilities.{key} 必须在 0 到 1 之间")
        result[key] = number
    return result


def normalize_model(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CatalogError("模型数据必须是 JSON 对象")
    unknown = set(payload) - MODEL_FIELDS
    if unknown:
        raise CatalogError(f"存在未知字段：{', '.join(sorted(unknown))}")

    data = dict(payload)
    if not partial:
        required = {
            "id", "name", "specialty", "capabilities", "cost_per_task",
            "latency_factor", "reliability",
        }
        missing = required - set(data)
        if missing:
            raise CatalogError(f"缺少字段：{', '.join(sorted(missing))}")

    for field_name in ("id", "name", "specialty", "provider", "tier", "pricing_currency"):
        if field_name not in data:
            continue
        if not isinstance(data[field_name], str) or not data[field_name].strip():
            raise CatalogError(f"{field_name} 必须是非空字符串")
        data[field_name] = data[field_name].strip()

    if "id" in data and not MODEL_ID_RE.fullmatch(data["id"]):
        raise CatalogError("id 只能包含字母、数字、点、下划线、冒号或短横线")
    if "model_id" in data:
        if data["model_id"] is None:
            data["model_id"] = data.get("id")
        elif not isinstance(data["model_id"], str) or len(data["model_id"].strip()) > 160:
            raise CatalogError("model_id 必须是长度不超过 160 的字符串")
        else:
            data["model_id"] = data["model_id"].strip()
    for field_name in ("name", "provider", "tier"):
        if field_name in data and len(data[field_name]) > 120:
            raise CatalogError(f"{field_name} 长度不能超过 120")
    if "specialty" in data and len(data["specialty"]) > 500:
        raise CatalogError("specialty 长度不能超过 500")

    if "capabilities" in data:
        data["capabilities"] = _capabilities(data["capabilities"])
    for field_name in LIST_FIELDS & set(data):
        data[field_name] = _list_value(data[field_name], field_name)
    for field_name in FLOAT_FIELDS & set(data):
        number = _float_value(data[field_name], field_name)
        if field_name in {"reliability", "reasoning", "speed", "data_confidence", "availability"}:
            if number is None or not 0 <= number <= 1:
                raise CatalogError(f"{field_name} 必须在 0 到 1 之间")
        elif number is None or number < 0:
            raise CatalogError(f"{field_name} 不能小于 0")
        data[field_name] = number
    for field_name in OPTIONAL_FLOAT_FIELDS & set(data):
        number = _float_value(data[field_name], field_name, optional=True)
        if number is not None and number < 0:
            raise CatalogError(f"{field_name} 不能小于 0")
        data[field_name] = number
    for field_name in INT_FIELDS & set(data):
        if isinstance(data[field_name], bool):
            raise CatalogError(f"{field_name} 必须是整数")
        try:
            number = int(data[field_name])
        except (TypeError, ValueError) as exc:
            raise CatalogError(f"{field_name} 必须是整数") from exc
        if not 1 <= number <= 20_000_000:
            raise CatalogError(f"{field_name} 必须在 1 到 20,000,000 之间")
        data[field_name] = number
    for field_name in BOOL_FIELDS & set(data):
        data[field_name] = _bool_value(data[field_name], field_name)

    if "pricing_currency" in data:
        data["pricing_currency"] = data["pricing_currency"].upper()
        if not re.fullmatch(r"[A-Z]{3}", data["pricing_currency"]):
            raise CatalogError("pricing_currency 必须是三位货币代码")
    if "source_url" in data and data["source_url"]:
        parsed = urlparse(str(data["source_url"]))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CatalogError("source_url 必须是 http 或 https 地址")
        if len(data["source_url"]) > 1000:
            raise CatalogError("source_url 长度不能超过 1000")
    if "verified_at" in data and data["verified_at"]:
        try:
            date.fromisoformat(str(data["verified_at"]))
        except ValueError as exc:
            raise CatalogError("verified_at 必须是 YYYY-MM-DD 日期") from exc
    return data


def model_from_payload(payload: dict[str, Any]) -> WorkerProfile:
    normalized = normalize_model(payload)
    defaults = WorkerProfile(
        id=normalized["id"],
        name=normalized["name"],
        specialty=normalized["specialty"],
        capabilities=normalized["capabilities"],
        cost_per_task=normalized["cost_per_task"],
        latency_factor=normalized["latency_factor"],
        reliability=normalized["reliability"],
    ).to_dict()
    defaults.update(normalized)
    defaults["model_id"] = defaults.get("model_id") or defaults["id"]
    if (
        defaults["routable"]
        and not defaults["local"]
        and defaults["pricing_currency"] != "USD"
    ):
        raise CatalogError("非 USD 云模型在没有汇率快照时必须设置 routable=false")
    return WorkerProfile(**defaults)


def _catalog_hash(models: Iterable[WorkerProfile]) -> str:
    payload = [model.to_dict() for model in sorted(models, key=lambda item: item.id)]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


class CatalogStore:
    """Thread-safe, validated and versioned model catalog."""

    def __init__(
        self,
        path: Path | str | None = None,
        defaults: Iterable[WorkerProfile] = MODELS,
    ) -> None:
        self._lock = threading.RLock()
        self._defaults = tuple(_clone(item) for item in defaults)
        self.path = Path(path) if path is not None else self.default_path()
        self._models = {item.id: _clone(item) for item in self._defaults}
        self._revision = 1
        self._updated_at = _utc_now()
        self._source_note = "内置厂商资料快照"
        self._load_warning: str | None = None
        if self.path.is_file():
            try:
                self._load()
            except CatalogError:
                self._source_note = "持久化目录损坏，已回退内置模型目录"
                self._load_warning = (
                    "持久化模型目录无法读取，已回退内置基线；"
                    "请检查文件或在页面恢复默认。"
                )

    @staticmethod
    def default_path() -> Path:
        explicit = os.environ.get("COSTWEAVE_CATALOG_PATH")
        if explicit:
            return Path(explicit).expanduser()
        return Path.home().joinpath(".costweave", "model_catalog.json")

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> tuple[WorkerProfile, ...]:
        with self._lock:
            return tuple(_clone(item) for item in self._models.values())

    def get(self, model_id: str) -> WorkerProfile:
        with self._lock:
            try:
                return _clone(self._models[model_id])
            except KeyError as exc:
                raise CatalogError(f"模型不存在：{model_id}") from exc

    def payload(self) -> dict[str, Any]:
        with self._lock:
            models = tuple(self._models.values())
            metadata = {
                **catalog_metadata(),
                "schema_version": SCHEMA_VERSION,
                "catalog_revision": self._revision,
                "catalog_hash": _catalog_hash(models),
                "updated_at": self._updated_at,
                "source_note": self._source_note,
                "editable": True,
                "persistent": True,
                "customized_models": sum(item.custom for item in models),
                "load_warning": self._load_warning,
            }
            return {
                "models": [item.to_dict() for item in models],
                "metadata": metadata,
            }

    def create(self, payload: dict[str, Any], *, expected_revision: int | None = None) -> WorkerProfile:
        normalized = dict(payload)
        normalized["custom"] = True
        model = model_from_payload(normalized)
        with self._lock:
            self._check_revision(expected_revision)
            if model.id in self._models:
                raise CatalogConflictError(f"模型已存在：{model.id}")
            candidate = dict(self._models)
            candidate[model.id] = model
            self._replace_models(candidate, f"新增模型 {model.id}")
            return _clone(model)

    def update(
        self,
        model_id: str,
        payload: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> WorkerProfile:
        with self._lock:
            self._check_revision(expected_revision)
            if model_id not in self._models:
                raise CatalogError(f"模型不存在：{model_id}")
            patch = normalize_model(payload, partial=True)
            if "id" in patch and patch["id"] != model_id:
                raise CatalogError("不能通过编辑操作修改模型 id")
            merged = self._models[model_id].to_dict()
            merged.update(patch)
            merged["custom"] = True
            model = model_from_payload(merged)
            candidate = dict(self._models)
            candidate[model_id] = model
            self._replace_models(candidate, f"更新模型 {model_id}")
            return _clone(model)

    def delete(self, model_id: str, *, expected_revision: int | None = None) -> None:
        with self._lock:
            self._check_revision(expected_revision)
            if model_id not in self._models:
                raise CatalogError(f"模型不存在：{model_id}")
            remaining = [item for key, item in self._models.items() if key != model_id]
            self._validate_catalog(remaining)
            candidate = dict(self._models)
            del candidate[model_id]
            self._replace_models(candidate, f"删除模型 {model_id}")

    def reset(self, *, expected_revision: int | None = None) -> None:
        with self._lock:
            self._check_revision(expected_revision)
            candidate = {item.id: _clone(item) for item in self._defaults}
            self._replace_models(candidate, "恢复内置模型目录")

    def import_data(
        self,
        raw: str | bytes | list[dict[str, Any]] | dict[str, Any],
        *,
        format: str = "json",
        mode: str = "merge",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if format not in {"json", "csv"}:
            raise CatalogError("format 必须是 json 或 csv")
        if mode not in {"merge", "replace"}:
            raise CatalogError("mode 必须是 merge 或 replace")
        payloads = self._parse_import(raw, format)
        if not payloads:
            raise CatalogError("导入文件中没有模型")
        if len(payloads) > 500:
            raise CatalogError("单次最多导入 500 个模型")

        models: list[WorkerProfile] = []
        ids: set[str] = set()
        for index, payload in enumerate(payloads, start=1):
            try:
                payload = dict(payload)
                payload["custom"] = True
                model = model_from_payload(payload)
            except CatalogError as exc:
                raise CatalogError(f"第 {index} 个模型无效：{exc}") from exc
            if model.id in ids:
                raise CatalogError(f"导入文件包含重复 id：{model.id}")
            ids.add(model.id)
            models.append(model)

        with self._lock:
            self._check_revision(expected_revision)
            if mode == "replace":
                candidate = {item.id: item for item in models}
            else:
                candidate = dict(self._models)
                candidate.update({item.id: item for item in models})
            self._validate_catalog(candidate.values())
            created = sum(item.id not in self._models for item in models)
            updated = len(models) - created
            self._replace_models(candidate, f"{mode} 导入 {len(models)} 个模型")
            return {
                "created": created,
                "updated": updated,
                "total": len(self._models),
                "revision": self._revision,
            }

    def export_json(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, indent=2)

    def export_csv(self) -> str:
        output = io.StringIO(newline="")
        field_names = [item.name for item in fields(WorkerProfile)]
        writer = csv.DictWriter(output, fieldnames=field_names)
        writer.writeheader()
        for model in self.snapshot():
            row = model.to_dict()
            row["capabilities"] = json.dumps(row["capabilities"], ensure_ascii=False, separators=(",", ":"))
            for field_name in LIST_FIELDS:
                row[field_name] = json.dumps(row[field_name], ensure_ascii=False, separators=(",", ":"))
            for key, value in row.items():
                if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t"}:
                    row[key] = "'" + value
            writer.writerow(row)
        return output.getvalue()

    def _parse_import(
        self,
        raw: str | bytes | list[dict[str, Any]] | dict[str, Any],
        format: str,
    ) -> list[dict[str, Any]]:
        if format == "csv":
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8-sig")
            if not isinstance(raw, str):
                raise CatalogError("CSV 导入数据必须是文本")
            raw = raw.removeprefix("\ufeff")
            try:
                return [dict(row) for row in csv.DictReader(io.StringIO(raw))]
            except csv.Error as exc:
                raise CatalogError(f"CSV 解析失败：{exc}") from exc

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CatalogError(f"JSON 解析失败：{exc.msg}") from exc
        if isinstance(raw, dict):
            raw = raw.get("models")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise CatalogError("JSON 必须是模型数组，或包含 models 数组的对象")
        return raw

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != self._revision:
            raise CatalogConflictError(
                f"目录已从版本 {expected_revision} 更新到 {self._revision}，请刷新后重试"
            )

    @staticmethod
    def _validate_catalog(models: Iterable[WorkerProfile]) -> None:
        items = list(models)
        if not items:
            raise CatalogError("模型目录不能为空")
        if not any(item.routable for item in items):
            raise CatalogError("至少需要保留一个可路由模型")
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise CatalogError("模型 id 不能重复")

    def _replace_models(
        self,
        candidate: dict[str, WorkerProfile],
        note: str,
    ) -> None:
        self._validate_catalog(candidate.values())
        previous = (
            self._models,
            self._revision,
            self._updated_at,
            self._source_note,
            self._load_warning,
        )
        self._models = candidate
        self._revision += 1
        self._updated_at = _utc_now()
        self._source_note = note
        self._load_warning = None
        try:
            self._persist()
        except OSError:
            (
                self._models,
                self._revision,
                self._updated_at,
                self._source_note,
                self._load_warning,
            ) = previous
            raise

    def _persist(self) -> None:
        document = {
            "schema_version": SCHEMA_VERSION,
            "revision": self._revision,
            "updated_at": self._updated_at,
            "source_note": self._source_note,
            "models": [item.to_dict() for item in self._models.values()],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            models = [model_from_payload(item) for item in document["models"]]
            self._validate_catalog(models)
        except (OSError, KeyError, TypeError, json.JSONDecodeError, CatalogError) as exc:
            raise CatalogError(f"无法加载模型目录 {self.path.name}：{exc}") from exc
        self._models = {item.id: item for item in models}
        self._revision = max(1, int(document.get("revision", 1)))
        self._updated_at = str(document.get("updated_at") or _utc_now())
        self._source_note = str(document.get("source_note") or "从持久化目录加载")


CATALOG_STORE = CatalogStore()
