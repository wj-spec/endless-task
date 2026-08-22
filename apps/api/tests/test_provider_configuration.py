from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from endless_task.api import AppSettings
from endless_task.api.app import _provider_from_settings
from endless_task.runtime import (
    FakeProvider,
    OpenAICompatibleProvider,
    UnconfiguredProvider,
)


class ProviderConfigurationTest(unittest.TestCase):
    def test_deepseek_environment_defaults_and_secret_repr(self) -> None:
        with patch.dict(
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
