from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from endless_task.api import AppSettings
from endless_task.api.app import _provider_from_settings
from endless_task.runtime import (
    FakeProvider,
    OpenAICompatibleProvider,
    UnconfiguredProvider,
)


@contextmanager
def isolated_cwd():
    with tempfile.TemporaryDirectory() as directory:
        previous = Path.cwd()
        os.chdir(directory)
        try:
            yield Path(directory)
        finally:
            os.chdir(previous)

class ProviderConfigurationTest(unittest.TestCase):
    def test_deepseek_environment_defaults_and_secret_repr(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "secret-value",
                "ENDLESS_TASK_DB_PATH": "/tmp/endless-task-config-test.db",
            },
            clear=True,
        ):
            settings = AppSettings.from_environment()

        self.assertEqual("deepseek", settings.provider_name)
        self.assertEqual("deepseek-chat", settings.model)
        self.assertEqual("https://api.deepseek.com", settings.base_url)
        self.assertEqual("secret-value", settings.api_key)
        self.assertNotIn("secret-value", repr(settings))

    def test_env_file_is_loaded_without_overriding_real_environment(self) -> None:
        with isolated_cwd() as directory:
            (directory / ".env").write_text(
                "# local configuration\n"
                "ENDLESS_TASK_PROVIDER=deepseek\n"
                "DEEPSEEK_API_KEY=file-key\n"
                'ENDLESS_TASK_MODEL="file-model"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = AppSettings.from_environment()

            self.assertEqual("deepseek", settings.provider_name)
            self.assertEqual("file-model", settings.model)
            self.assertEqual("file-key", settings.api_key)

            with patch.dict(
                os.environ,
                {"ENDLESS_TASK_MODEL": "env-model"},
                clear=True,
            ):
                settings = AppSettings.from_environment()

            self.assertEqual("env-model", settings.model)
            self.assertEqual("file-key", settings.api_key)

    def test_env_file_path_can_be_configured(self) -> None:
        with isolated_cwd() as directory:
            env_path = directory / "custom.env"
            env_path.write_text(
                "ENDLESS_TASK_PROVIDER=deepseek\nDEEPSEEK_API_KEY=custom-key\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"ENDLESS_TASK_ENV_FILE": str(env_path)},
                clear=True,
            ):
                settings = AppSettings.from_environment()

            self.assertEqual("deepseek", settings.provider_name)
            self.assertEqual("custom-key", settings.api_key)

    def test_tool_execution_limits_are_loaded_from_environment(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_MAX_TOOL_CALLS_PER_TURN": "6",
                "ENDLESS_TASK_MAX_CONCURRENT_TOOL_CALLS": "3",
                "ENDLESS_TASK_MAX_TOOL_ARGUMENT_BYTES": "1024",
                "ENDLESS_TASK_MAX_TOTAL_TOOL_ARGUMENT_BYTES": "4096",
                "ENDLESS_TASK_MAX_TOOL_ARGUMENT_DEPTH": "12",
                "ENDLESS_TASK_MAX_TOOL_ARGUMENT_NODES": "500",
            },
            clear=True,
        ):
            settings = AppSettings.from_environment()

        self.assertEqual(6, settings.max_tool_calls_per_turn)
        self.assertEqual(3, settings.max_concurrent_tool_calls)
        self.assertEqual(1024, settings.max_tool_argument_bytes)
        self.assertEqual(4096, settings.max_total_tool_argument_bytes)
        self.assertEqual(12, settings.max_tool_argument_depth)
        self.assertEqual(500, settings.max_tool_argument_nodes)

    def test_fake_is_default_and_needs_no_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = AppSettings(database_path=Path(directory) / "app.db")
            provider = _provider_from_settings(settings)
        self.assertIsInstance(provider, FakeProvider)

    def test_missing_deepseek_key_uses_safe_unconfigured_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = AppSettings(
                database_path=Path(directory) / "app.db",
                provider_name="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
            )
            provider = _provider_from_settings(settings)
        self.assertIsInstance(provider, UnconfiguredProvider)
        self.assertEqual("deepseek", provider.name)


class ProviderSdkConstructionTest(unittest.IsolatedAsyncioTestCase):
    async def test_deepseek_configuration_constructs_real_sdk_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = AppSettings(
                database_path=Path(directory) / "app.db",
                provider_name="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key="construction-only",
            )
            provider = _provider_from_settings(settings)
            self.assertIsInstance(provider, OpenAICompatibleProvider)
            self.assertEqual("deepseek", provider.name)
            await provider.close()
