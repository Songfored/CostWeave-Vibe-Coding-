"""One-step local provider configuration and read-only model discovery.

Secrets are kept in the ignored ``.env.local`` file.  The generated provider
registry contains only public connection metadata and model identifiers.
Model discovery uses fixed, reviewed endpoints and never performs inference.
Relay endpoints are not published in source: their base URL is read from the
ignored ``.env.local`` (for example ``COSTWEAVE_TRAVELTOKEN_BASE_URL``).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


PROVIDER_REGISTRY_SCHEMA_VERSION = "v0.7.1-provider-setup-1"
DEFAULT_ENV_FILE = ".env.local"
DEFAULT_REGISTRY_FILE = "work/local-data/providers.local.json"
MAX_DISCOVERY_RESPONSE_BYTES = 1_048_576
MIN_API_KEY_LENGTH = 8
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ProviderSetupError(RuntimeError):
    """Safe, user-facing provider setup error."""


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    provider_id: str
    display_name: str
    trust: str
    base_url: str
    secret_env: str
    default_model: str
    preferred_models: tuple[str, ...]
    model_prefixes: tuple[str, ...]
    secret_aliases: tuple[str, ...] = ()
    protocol: str = "openai-compatible"
    base_url_env: str | None = None

    @property
    def models_url(self) -> str | None:
        if not self.base_url:
            return None
        return self.base_url.rstrip("/") + "/models"

    @property
    def chat_completions_url(self) -> str | None:
        if not self.base_url:
            return None
        return self.base_url.rstrip("/") + "/chat/completions"

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("preferred_models")
        value.pop("model_prefixes")
        value.pop("secret_aliases")
        value["models_url"] = self.models_url
        value["chat_completions_url"] = self.chat_completions_url
        return value


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        provider_id="deepseek-official",
        display_name="DeepSeek 官方平台",
        trust="official",
        base_url="https://api.deepseek.com",
        secret_env="COSTWEAVE_DEEPSEEK_API_KEY",
        default_model="deepseek-v4-flash",
        preferred_models=("deepseek-v4-pro", "deepseek-v4-flash"),
        model_prefixes=("deepseek-v4", "deepseek-"),
    ),
    ProviderPreset(
        provider_id="traveltoken-gpt",
        display_name="TravelToken GPT 中转",
        trust="relay-unverified",
        base_url="",
        base_url_env="COSTWEAVE_TRAVELTOKEN_BASE_URL",
        secret_env="COSTWEAVE_TRAVELTOKEN_GPT_API_KEY",
        default_model="gpt-5.5",
        preferred_models=("gpt-5.5",),
        model_prefixes=("gpt-5.5", "gpt-"),
        secret_aliases=("COSTWEAVE_GPT_API_KEY",),
    ),
    ProviderPreset(
        provider_id="traveltoken-claude",
        display_name="TravelToken Claude 中转",
        trust="relay-unverified",
        base_url="",
        base_url_env="COSTWEAVE_TRAVELTOKEN_BASE_URL",
        secret_env="COSTWEAVE_TRAVELTOKEN_CLAUDE_API_KEY",
        default_model="claude-sonnet-4-6",
        preferred_models=(
            "claude-sonnet-4-6",
            "claude-sonnet-4.6",
            "claude-4.6-sonnet",
        ),
        model_prefixes=("claude-sonnet-4-6", "claude-sonnet", "claude-"),
        secret_aliases=("COSTWEAVE_CLAUDE_API_KEY",),
    ),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def resolve_presets(
    env: Mapping[str, str] | None = None,
) -> tuple[ProviderPreset, ...]:
    """Fill relay base URLs from local configuration (never from source)."""

    values = dict(os.environ)
    values.update(env or {})
    resolved: list[ProviderPreset] = []
    for preset in PROVIDER_PRESETS:
        if preset.base_url_env and not preset.base_url:
            raw = str(values.get(preset.base_url_env, "")).strip()
            if raw and not raw.lower().startswith("https://"):
                raise ProviderSetupError(f"{preset.base_url_env} 必须是 https:// 地址")
            preset = replace(preset, base_url=raw.rstrip("/"))
        resolved.append(preset)
    return tuple(resolved)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _validate_api_key(value: str) -> str:
    key = value.strip()
    if len(key.encode("utf-8")) < MIN_API_KEY_LENGTH:
        raise ProviderSetupError("API Key 长度不足，请重新复制完整密钥")
    if any(char.isspace() for char in key) or "\x00" in key:
        raise ProviderSetupError("API Key 中包含空格或非法字符")
    if len(key.encode("utf-8")) > 4096:
        raise ProviderSetupError("API Key 长度异常")
    return key


def _parse_env(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.exists():
        return [], {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if _ENV_NAME.fullmatch(name):
            values[name] = value.strip()
    return lines, values


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    lines, _values = _parse_env(path)
    normalized = {str(key): _validate_api_key(str(value)) for key, value in updates.items()}
    if any(not _ENV_NAME.fullmatch(name) for name in normalized):
        raise ProviderSetupError("内部密钥变量名无效")
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        name = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if name in normalized:
            if name not in seen:
                output.append(f"{name}={normalized[name]}")
                seen.add(name)
            continue
        output.append(line)
    missing = [name for name in normalized if name not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# Local provider credentials; never commit this file.")
        output.extend(f"{name}={normalized[name]}" for name in missing)
    _atomic_write_text(path, "\n".join(output).rstrip() + "\n")


def _extract_model_ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        rows = value.get("data", value.get("models", ()))
    elif isinstance(value, list):
        rows = value
    else:
        rows = ()
    found: set[str] = set()
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, str):
                model_id = row.strip()
            elif isinstance(row, Mapping):
                model_id = str(row.get("id") or row.get("name") or "").strip()
            else:
                model_id = ""
            if model_id and len(model_id) <= 256 and not any(char.isspace() for char in model_id):
                found.add(model_id)
    return tuple(sorted(found))


def discover_models(
    preset: ProviderPreset,
    api_key: str,
    *,
    timeout_seconds: float = 15.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[str, ...]:
    key = _validate_api_key(api_key)
    if not preset.models_url:
        raise ProviderSetupError("该渠道尚未在 .env.local 中配置接口地址")
    request = urllib.request.Request(
        preset.models_url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "CostWeave/provider-setup",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_DISCOVERY_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise ProviderSetupError("密钥未通过平台验证") from None
        if error.code == 404:
            raise ProviderSetupError("该渠道没有提供标准模型列表接口") from None
        raise ProviderSetupError(f"模型查询失败（HTTP {error.code}）") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderSetupError("模型查询失败，请检查网络或稍后重试") from None
    if len(raw) > MAX_DISCOVERY_RESPONSE_BYTES:
        raise ProviderSetupError("模型列表响应异常过大")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderSetupError("模型列表不是有效 JSON") from None
    models = _extract_model_ids(payload)
    if not models:
        raise ProviderSetupError("平台返回了空模型列表")
    return models


def select_model(preset: ProviderPreset, models: Iterable[str]) -> str | None:
    available = tuple(dict.fromkeys(str(item).strip() for item in models if str(item).strip()))
    lowered = {item.lower(): item for item in available}
    for preferred in preset.preferred_models:
        match = lowered.get(preferred.lower())
        if match:
            return match
    for prefix in preset.model_prefixes:
        matches = sorted(
            (item for item in available if item.lower().startswith(prefix.lower())),
            key=lambda item: (len(item), item.lower()),
        )
        if matches:
            return matches[0]
    return None


def build_registry(
    results: Mapping[str, Mapping[str, Any]],
    *,
    generated_at: str | None = None,
    presets: Iterable[ProviderPreset] | None = None,
) -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    for preset in (PROVIDER_PRESETS if presets is None else presets):
        result = dict(results.get(preset.provider_id, {}))
        models = tuple(str(item) for item in result.get("models", ()))
        selected = result.get("selected_model") or select_model(preset, models)
        providers.append(
            {
                **preset.public(),
                "configured": bool(result.get("configured", False)),
                "discovery_status": str(result.get("discovery_status", "not_configured")),
                "discovered_models": list(models),
                "selected_model": selected,
                "selection_verified": bool(selected and selected in models),
                "last_checked_at": result.get("last_checked_at"),
                "diagnostic": result.get("diagnostic"),
                "pricing_status": "catalog_snapshot_required",
                "inference_enabled": False,
            }
        )
    return {
        "schema_version": PROVIDER_REGISTRY_SCHEMA_VERSION,
        "generated_at": generated_at or utc_now(),
        "contains_secrets": False,
        "model_discovery_only": True,
        "providers": providers,
    }


def save_registry(path: Path, registry: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def configure(
    *,
    root: Path,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    discovery: Callable[[ProviderPreset, str], tuple[str, ...]] = discover_models,
) -> dict[str, Any]:
    env_path = root / DEFAULT_ENV_FILE
    registry_path = root / DEFAULT_REGISTRY_FILE
    _lines, current = _parse_env(env_path)
    presets = resolve_presets(current)
    keys: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}
    for preset in presets:
        if not preset.base_url:
            results[preset.provider_id] = {
                "configured": False,
                "discovery_status": "not_configured",
                "diagnostic": f"missing_{preset.base_url_env or 'base_url'}",
            }
            continue
        key = current.get(preset.secret_env, "")
        if not key:
            key = next(
                (
                    current[name]
                    for name in preset.secret_aliases
                    if current.get(name)
                ),
                "",
            )
        if not key:
            key = prompt_secret(f"请输入 {preset.display_name} API Key: ").strip()
        if not key:
            results[preset.provider_id] = {
                "configured": False,
                "discovery_status": "not_configured",
                "diagnostic": "missing_api_key",
            }
            continue
        key = _validate_api_key(key)
        keys[preset.secret_env] = key
        try:
            models = discovery(preset, key)
            selected = select_model(preset, models)
            results[preset.provider_id] = {
                "configured": True,
                "discovery_status": "verified" if selected else "no_matching_model",
                "models": models,
                "selected_model": selected,
                "last_checked_at": utc_now(),
                "diagnostic": None if selected else "target_model_not_advertised",
            }
        except ProviderSetupError as error:
            results[preset.provider_id] = {
                "configured": True,
                "discovery_status": "check_failed",
                "models": (),
                "selected_model": None,
                "last_checked_at": utc_now(),
                "diagnostic": str(error),
            }
    if keys:
        update_env_file(env_path, keys)
    registry = build_registry(results, presets=presets)
    save_registry(registry_path, registry)
    return registry


def public_summary(registry: Mapping[str, Any]) -> str:
    labels = {
        "verified": "模型列表验证通过并已自动选型",
        "no_matching_model": "已连接，但没有找到目标模型",
        "check_failed": "已保存密钥，模型查询待重试",
        "not_configured": "未配置",
    }
    lines = ["", "CostWeave 模型接口配置结果："]
    for item in registry.get("providers", []):
        status = str(item.get("discovery_status", "not_configured"))
        model = item.get("selected_model") or "—"
        relay_note = (
            "；中转背后模型身份仍待真实评测核验"
            if item.get("trust") == "relay-unverified" and status == "verified"
            else ""
        )
        lines.append(
            f"- {item.get('display_name')}: {labels.get(status, status)}；模型：{model}{relay_note}"
        )
    lines.extend(
        (
            "",
            "本次只查询模型列表，没有发送推理请求，也没有产生 CostWeave 付费评测。",
            "密钥保存在 Git 忽略的 .env.local；公开配置不含密钥。",
        )
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置 CostWeave 的真实模型接口")
    parser.add_argument(
        "command", nargs="?", choices=("configure", "status"), default="configure"
    )
    parser.add_argument("--root", type=Path, default=project_root())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    registry_path = root / DEFAULT_REGISTRY_FILE
    if args.command == "status":
        if not registry_path.is_file():
            print("尚未配置模型接口，请运行 scripts\\configure-models.ps1")
            return 1
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        print(public_summary(registry))
        return 0
    try:
        registry = configure(root=root)
    except (ProviderSetupError, OSError) as error:
        print(f"配置失败：{error}")
        return 1
    print(public_summary(registry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
