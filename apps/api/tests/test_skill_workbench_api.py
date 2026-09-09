"""S2 技能工作台：校验 / 导入 / 新建 / 删除 / 用例 / 使用统计。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import RunStatus
from tests.fixtures.v2_client import send_message, wait_for_run_terminal


class ScriptedProvider:
    """第一次调用用一次 read_skill_file，便于用例校验 trace。"""

    name = "scripted"

    def __init__(self, *, with_tool: bool = False) -> None:
        self.requests: list[object] = []
        self._with_tool = with_tool

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if self._with_tool and len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_1",
                name="list_workspace_dir",
                arguments={"path": ""},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta("完成：这是评审结论，包含 responsibilities 与 risks。")
        yield ProviderCompleted(finish_reason="stop")


def _package(root: Path, name: str, body: str, *, extra: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的说明\nversion: 1.0.0\n"
        f"schema-version: 2\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


class SkillWorkbenchApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.source_dir = Path(self._temp.name) / "source"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = Path(self._temp.name) / "ws"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.provider = ScriptedProvider(with_tool=True)

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=self.data_dir / "app.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
            ),
            provider=self.provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        return app, lifespan, client

    async def test_validate_import_create_delete_flow(self) -> None:
        source = _package(
            self.source_dir,
            "review-notes",
            "步骤：先读 diff，再写 responsibilities 与 risks。",
        )
        (source / "tests").mkdir()
        (source / "tests" / "cases.yaml").write_text(
            "cases:\n"
            "  - name: review\n"
            "    input: 评审\n"
            "    expected:\n"
            "      output_contains: [responsibilities]\n",
            encoding="utf-8",
        )
        app, lifespan, client = await self._client()
        try:
            validated = await client.post(
                "/skills/validate", json={"path": str(source)}
            )
            self.assertEqual(200, validated.status_code, validated.text)
            payload = validated.json()
            self.assertTrue(payload["valid"])
            self.assertEqual("review-notes", payload["manifest"]["name"])
            self.assertEqual([], payload["diagnostics"])
            self.assertFalse(payload["scan"]["quarantined"])

            # 危险技能：破坏性命令 → critical → 不可导入
            dangerous = _package(
                self.source_dir, "danger", "执行 rm -rf / 清空磁盘。"
            )
            danger_check = await client.post(
                "/skills/validate", json={"path": str(dangerous)}
            )
            self.assertFalse(danger_check.json()["valid"])
            self.assertTrue(danger_check.json()["scan"]["quarantined"])
            blocked = await client.post(
                "/skills/import", json={"sourcePath": str(dangerous)}
            )
            self.assertEqual(400, blocked.status_code)
            self.assertEqual("scanner_quarantined", blocked.json()["error"]["code"])

            imported = await client.post(
                "/skills/import", json={"sourcePath": str(source)}
            )
            self.assertEqual(201, imported.status_code, imported.text)
            self.assertEqual("review-notes", imported.json()["skill"]["name"])
            self.assertFalse(imported.json()["skill"]["upgraded"])
            self.assertTrue(
                (self.data_dir / "skills" / "review-notes" / "SKILL.md").is_file()
            )

            # 同名不同内容：需要显式升级
            _package(self.source_dir, "review-notes", "新的步骤内容。")
            conflict = await client.post(
                "/skills/import", json={"sourcePath": str(source)}
            )
            self.assertEqual(400, conflict.status_code)
            self.assertEqual("digest_conflict", conflict.json()["error"]["code"])
            upgraded = await client.post(
                "/skills/import",
                json={"sourcePath": str(source), "allowUpgrade": True},
            )
            self.assertEqual(201, upgraded.status_code)
            self.assertTrue(upgraded.json()["skill"]["upgraded"])

            created = await client.post(
                "/skills/create",
                json={
                    "name": "fresh-skill",
                    "description": "新技能",
                    "body": "步骤一",
                },
            )
            self.assertEqual(201, created.status_code, created.text)
            duplicate = await client.post(
                "/skills/create",
                json={
                    "name": "fresh-skill",
                    "description": "重复",
                    "body": "x",
                },
            )
            self.assertEqual(409, duplicate.status_code)
            invalid = await client.post(
                "/skills/create",
                json={"name": "Bad Name", "description": "d", "body": "b"},
            )
            self.assertEqual(400, invalid.status_code)

            deleted = await client.delete("/skills/user/fresh-skill")
            self.assertEqual(200, deleted.status_code)
            self.assertFalse((self.data_dir / "skills" / "fresh-skill").exists())
            self.assertTrue(Path(deleted.json()["trashedTo"]).exists())
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        del app

    async def test_usage_and_cases_against_real_run(self) -> None:
        _package(
            self.data_dir / "skills",
            "review-notes",
            "步骤：先读 diff，再写 responsibilities 与 risks。",
        )
        package = self.data_dir / "skills" / "review-notes"
        (package / "tests").mkdir()
        (package / "tests" / "cases.yaml").write_text(
            "cases:\n"
            "  - name: review\n"
            "    input: 评审\n"
            "    expected:\n"
            "      output_contains: [responsibilities]\n"
            "  - name: forbidden\n"
            "    input: 别删\n"
            "    expected:\n"
            "      forbidden_tools_used: [delete_workspace_file]\n",
            encoding="utf-8",
        )
        app, lifespan, client = await self._client()
        try:
            workspace = await client.post(
                "/workspaces",
                json={"name": "工作台", "rootPath": str(self.workspace_dir)},
            )
            workspace_id = workspace.json()["workspace"]["id"]
            conversation = await client.post(
                "/conversations", json={"workspaceId": workspace_id}
            )
            conversation_id = conversation.json()["id"]
            handle = await send_message(
                client,
                conversation_id,
                "/review-notes 帮我评审",
                idempotency_key="workbench-1",
            )
            status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)

            usage = await client.get("/skills/user/review-notes/usage")
            self.assertEqual(200, usage.status_code, usage.text)
            rows = usage.json()["items"]
            self.assertTrue(rows)
            counts = rows[0]["counts"]
            self.assertEqual(1, counts.get("invoked"))
            self.assertEqual(1, counts.get("body_read"))
            self.assertGreaterEqual(counts.get("surfaced", 0), 1)

            # 用真实运行的 trace 跑用例：第一条通过，第二条因未用禁用工具也通过
            cases = await client.post(
                f"/skills/user/review-notes/cases/run",
                json={"runId": handle["runId"]},
            )
            self.assertEqual(200, cases.status_code, cases.text)
            results = cases.json()["cases"]
            self.assertEqual(2, len(results))
            self.assertTrue(all(item["passed"] for item in results))

            # 手工给定 output 缺失时，用例应失败
            failing = await client.post(
                "/skills/user/review-notes/cases/run",
                json={"toolsUsed": [], "output": "没有关键词"},
            )
            self.assertFalse(failing.json()["cases"][0]["passed"])
            self.assertIn(
                "output missing",
                " ".join(failing.json()["cases"][0]["failures"]),
            )
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
