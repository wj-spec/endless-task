"""S1 用户显式调用技能：API 与运行期注入。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import RunStatus
from tests.fixtures.v2_client import send_message, wait_for_run_terminal


class RecordingProvider:
    """记录每次请求的 messages，用于断言注入内容。"""

    name = "recording"

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        yield ProviderTextDelta("好的。")
        yield ProviderCompleted(finish_reason="stop")


def _write_skill(root: Path, name: str, *, extra: str = "", body: str = "步骤一：先做 A") -> None:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的说明\n{extra}---\n{body}\n",
        encoding="utf-8",
    )


def _messages_text(provider: RecordingProvider) -> str:
    parts: list[str] = []
    for request in provider.requests:
        for message in getattr(request, "messages", ()):
            content = getattr(message, "content", "")
            if isinstance(content, str):
                parts.append(f"[{getattr(message, 'role', '')}]{content}")
    return "\n".join(parts)


class SkillInvocationApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = self.data_dir / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.provider = RecordingProvider()

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

    async def test_invocable_endpoint_lists_user_skills(self) -> None:
        _write_skill(self.skills_dir, "review-notes")
        _write_skill(self.skills_dir, "model-only", extra="user-invocable: false\n")
        _write_skill(self.skills_dir, "broken", extra="", body="")

        app, lifespan, client = await self._client()
        try:
            response = await client.get("/skills/invocable")
            self.assertEqual(200, response.status_code)
            names = [item["name"] for item in response.json()["items"]]
            self.assertEqual(["review-notes"], names)
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        del app

    async def test_slash_command_injects_skill_body(self) -> None:
        _write_skill(
            self.skills_dir,
            "review-notes",
            body="步骤一：先读 diff\n步骤二：写评审意见",
        )
        app, lifespan, client = await self._client()
        try:
            workspace = await client.post(
                "/workspaces", json={"name": "技能区", "rootPath": str(self._temp.name)}
            )
            workspace_id = workspace.json()["workspace"]["id"]
            conversation = await client.post(
                "/conversations", json={"workspaceId": workspace_id}
            )
            conversation_id = conversation.json()["id"]

            handle = await send_message(
                client,
                conversation_id,
                "/review-notes 帮我评审这次改动",
                idempotency_key="skill-invoke-1",
            )
            self.assertEqual(
                [{"name": "review-notes", "status": "ok", "message": ""}],
                handle.get("requestedSkills"),
            )
            status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)

            text = _messages_text(self.provider)
            self.assertIn('<skill_request name="review-notes">', text)
            self.assertIn("步骤二：写评审意见", text)
            # 技能内容以 user-role 注入，不获得 system 级权威
            self.assertIn("[user]<skill_request name=", text)
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)

    async def test_unknown_and_disabled_names_are_reported(self) -> None:
        _write_skill(self.skills_dir, "disabled-skill")
        app, lifespan, client = await self._client()
        try:
            workspace = await client.post(
                "/workspaces", json={"name": "技能区2", "rootPath": str(self._temp.name)}
            )
            workspace_id = workspace.json()["workspace"]["id"]
            await client.patch(
                f"/skills/user/disabled-skill?workspace={workspace_id}",
                json={"disabled": True},
            )
            conversation = await client.post(
                "/conversations", json={"workspaceId": workspace_id}
            )
            conversation_id = conversation.json()["id"]

            handle = await send_message(
                client,
                conversation_id,
                "/disabled-skill 试试 /no-such-skill 和 /tmp 目录",
                idempotency_key="skill-invoke-2",
            )
            statuses = {item["name"]: item["status"] for item in handle["requestedSkills"]}
            # 已禁用 → 明确通知；不存在的名字与路径静默忽略
            self.assertEqual({"disabled-skill": "disabled"}, statuses)

            status = await wait_for_run_terminal(app.state.container, handle["runId"])
            self.assertEqual(RunStatus.COMPLETED, status)
            self.assertNotIn("<skill_request", _messages_text(self.provider))
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
