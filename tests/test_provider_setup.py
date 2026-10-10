from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from costweave.provider_setup import (
    DEFAULT_ENV_FILE,
    DEFAULT_REGISTRY_FILE,
    PROVIDER_PRESETS,
    ProviderSetupError,
    build_registry,
    configure,
    resolve_presets,
    select_model,
    update_env_file,
)

RELAY_LINE = "COSTWEAVE_TRAVELTOKEN_BASE_URL=https://relay.example.invalid/v1\n"


class ProviderSetupTests(unittest.TestCase):
    def test_model_selection_prefers_exact_target(self):
        deepseek, gpt, claude = PROVIDER_PRESETS
        self.assertEqual(
            "deepseek-v4-pro",
            select_model(deepseek, ("deepseek-v4-flash", "deepseek-v4-pro")),
        )
        self.assertEqual("gpt-5.5", select_model(gpt, ("gpt-5.4", "gpt-5.5")))
        self.assertEqual(
            "claude-sonnet-4-6",
            select_model(claude, ("claude-haiku-4-5", "claude-sonnet-4-6")),
        )

    def test_env_update_preserves_unrelated_values_and_never_writes_registry_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / DEFAULT_ENV_FILE
            env_path.write_text("COSTWEAVE_RUNTIME_V4=1\n", encoding="utf-8")
            secret = "sk-test-secret-value"
            update_env_file(env_path, {"COSTWEAVE_DEEPSEEK_API_KEY": secret})
            content = env_path.read_text(encoding="utf-8")
            self.assertIn("COSTWEAVE_RUNTIME_V4=1", content)
            self.assertIn(secret, content)
            registry = build_registry({})
            self.assertNotIn(secret, json.dumps(registry))
            self.assertFalse(registry["contains_secrets"])

    def test_invalid_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderSetupError):
                update_env_file(
                    Path(directory) / DEFAULT_ENV_FILE,
                    {"COSTWEAVE_DEEPSEEK_API_KEY": "short"},
                )

    def test_configure_queries_each_key_and_writes_public_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEFAULT_ENV_FILE).write_text(
                "COSTWEAVE_RUNTIME_V4=1\n" + RELAY_LINE, encoding="utf-8"
            )
            entered = iter(("deepseek-secret", "gpt-relay-secret", "claude-relay-secret"))
            advertised = {
                "deepseek-official": ("deepseek-v4-flash", "deepseek-v4-pro"),
                "traveltoken-gpt": ("gpt-5.5",),
                "traveltoken-claude": ("claude-sonnet-4-6",),
            }
            registry = configure(
                root=root,
                prompt_secret=lambda _prompt: next(entered),
                discovery=lambda preset, _key: advertised[preset.provider_id],
            )
            saved = json.loads((root / DEFAULT_REGISTRY_FILE).read_text(encoding="utf-8"))
            self.assertEqual(registry, saved)
            self.assertEqual(
                ["deepseek-v4-pro", "gpt-5.5", "claude-sonnet-4-6"],
                [item["selected_model"] for item in registry["providers"]],
            )
            public = json.dumps(saved)
            self.assertNotIn("deepseek-secret", public)
            self.assertTrue(all(item["selection_verified"] for item in saved["providers"]))
            self.assertTrue(all(not item["inference_enabled"] for item in saved["providers"]))

    def test_discovery_failure_keeps_safe_non_inference_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEFAULT_ENV_FILE).write_text(RELAY_LINE, encoding="utf-8")
            prompts = iter(("deepseek-secret", "gpt-relay-secret", "claude-relay-secret"))

            def fail(_preset, _key):
                raise ProviderSetupError("模型查询失败")

            registry = configure(
                root=root,
                prompt_secret=lambda _prompt: next(prompts),
                discovery=fail,
            )
            self.assertTrue(all(item["configured"] for item in registry["providers"]))
            self.assertTrue(
                all(item["discovery_status"] == "check_failed" for item in registry["providers"])
            )
            self.assertTrue(all(item["selected_model"] is None for item in registry["providers"]))

    def test_existing_legacy_secret_names_are_reused_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / DEFAULT_ENV_FILE).write_text(
                "\n".join(
                    (
                        "COSTWEAVE_DEEPSEEK_API_KEY=deepseek-existing",
                        "COSTWEAVE_GPT_API_KEY=gpt-existing",
                        "COSTWEAVE_CLAUDE_API_KEY=claude-existing",
                        RELAY_LINE.strip(),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            observed = []

            def discovery(preset, key):
                observed.append((preset.provider_id, key))
                return (preset.default_model,)

            registry = configure(
                root=root,
                prompt_secret=lambda _prompt: self.fail("existing keys should not prompt"),
                discovery=discovery,
            )
            self.assertEqual(3, len(observed))
            self.assertTrue(all(item["selection_verified"] for item in registry["providers"]))
            env_text = (root / DEFAULT_ENV_FILE).read_text(encoding="utf-8")
            self.assertIn("COSTWEAVE_TRAVELTOKEN_GPT_API_KEY=gpt-existing", env_text)
            self.assertIn("COSTWEAVE_TRAVELTOKEN_CLAUDE_API_KEY=claude-existing", env_text)

    def test_relay_url_is_not_in_source_and_missing_url_skips_without_prompt(self):
        relays = [item for item in PROVIDER_PRESETS if item.trust == "relay-unverified"]
        self.assertTrue(relays)
        self.assertTrue(all(item.base_url == "" and item.base_url_env for item in relays))
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COSTWEAVE_TRAVELTOKEN_BASE_URL", None)
            root = Path(directory)
            (root / DEFAULT_ENV_FILE).write_text(
                "COSTWEAVE_DEEPSEEK_API_KEY=deepseek-existing\n", encoding="utf-8"
            )
            observed = []

            def discovery(preset, _key):
                observed.append(preset.provider_id)
                return (preset.default_model,)

            registry = configure(
                root=root,
                prompt_secret=lambda _prompt: self.fail("relay without URL must not prompt"),
                discovery=discovery,
            )
            self.assertEqual(["deepseek-official"], observed)
            by_id = {item["provider_id"]: item for item in registry["providers"]}
            for relay in relays:
                self.assertFalse(by_id[relay.provider_id]["configured"])
                self.assertIsNone(by_id[relay.provider_id]["chat_completions_url"])

    def test_relay_url_must_be_https(self):
        with self.assertRaises(ProviderSetupError):
            resolve_presets({"COSTWEAVE_TRAVELTOKEN_BASE_URL": "http://relay.example.invalid/v1"})


if __name__ == "__main__":
    unittest.main()
