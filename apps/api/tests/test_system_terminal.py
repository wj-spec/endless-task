"""S3 系统终端入口：平台分支、失败回落与端点接线。"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.workspace_runtime.system_terminal import open_system_terminal
from tests.fixtures.workspace_client import create_bound_workspace


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._fail = fail

    def __call__(self, command, *, cwd, **kwargs):
        self.calls.append((list(command), cwd))
        if self._fail:
            raise OSError("spawn denied")
        return object()


class OpenSystemTerminalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_macos_opens_terminal_app_at_directory(self) -> None:
        spawn = _Recorder()

        result = open_system_terminal(self.root, platform="darwin", spawn=spawn)

        self.assertTrue(result.opened)
        self.assertEqual("open -a Terminal", result.launcher)
        self.assertEqual(
            [(["open", "-a", "Terminal", str(self.root)], str(self.root))],
            spawn.calls,
        )

    def test_macos_spawn_failure_is_reported(self) -> None:
        result = open_system_terminal(
            self.root, platform="darwin", spawn=_Recorder(fail=True)
        )

        self.assertFalse(result.opened)
        self.assertIn("无法打开系统终端", result.message)

    def test_linux_prefers_configured_terminal(self) -> None:
        spawn = _Recorder()

        result = open_system_terminal(
            self.root,
            platform="linux",
            spawn=spawn,
            which=lambda name: f"/usr/bin/{name}",
            environment={"TERMINAL": "kitty"},
        )

        self.assertTrue(result.opened)
        self.assertEqual("kitty", result.launcher)
        self.assertEqual("/usr/bin/kitty", spawn.calls[0][0][0])

    def test_linux_falls_back_through_candidates(self) -> None:
        spawn = _Recorder()
        found = {"konsole"}

        result = open_system_terminal(
            self.root,
            platform="linux",
            spawn=spawn,
            which=lambda name: f"/usr/bin/{name}" if name in found else None,
            environment={},
        )

        self.assertTrue(result.opened)
        self.assertEqual("konsole", result.launcher)
        self.assertIn(str(self.root), spawn.calls[0][0])

    def test_linux_without_any_terminal_reports_message(self) -> None:
        result = open_system_terminal(
            self.root,
            platform="linux",
            spawn=_Recorder(),
            which=lambda name: None,
            environment={},
        )

        self.assertFalse(result.opened)
        self.assertIn("未找到可用的终端程序", result.message)

    def test_windows_is_explicitly_unsupported(self) -> None:
        result = open_system_terminal(self.root, platform="win32", spawn=_Recorder())

        self.assertFalse(result.opened)
        self.assertIn("Windows 暂不支持", result.message)

    def test_missing_directory_is_reported(self) -> None:
        result = open_system_terminal(
            self.root / "nope", platform="darwin", spawn=_Recorder()
        )

        self.assertFalse(result.opened)
        self.assertEqual("工作区目录不可用。", result.message)


@asynccontextmanager
async def _client(database_path: Path):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
        ),
        provider=FakeProvider(chunks=("ok",)),
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class TerminalOpenApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temp.name) / "terminal.db"

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def test_open_returns_launcher_without_spawning_real_terminal(self) -> None:
        async with _client(self.database_path) as client:
            workspace_id = await create_bound_workspace(client)
            spawn = _Recorder()
            with mock.patch(
                "endless_task.api.routes.workspaces.open_system_terminal",
                side_effect=lambda root: open_system_terminal(
                    root, platform="darwin", spawn=spawn
                ),
            ):
                response = await client.post(
                    f"/workspaces/{workspace_id}/terminal/open"
                )

            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertTrue(body["opened"])
            self.assertEqual("open -a Terminal", body["launcher"])
            self.assertEqual(1, len(spawn.calls))

    async def test_unknown_workspace_is_not_found(self) -> None:
        async with _client(self.database_path) as client:
            response = await client.post("/workspaces/missing/terminal/open")
            self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
