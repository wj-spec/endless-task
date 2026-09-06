from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Header, HTTPException

from endless_task.api.app import AppSettings, create_app
from endless_task.domain.models import (
    ArtifactKind,
    KnowledgeProposalType,
    MemoryKind,
    NotificationKind,
)
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)


# E2E markdown 渲染样例：由 [e2e:markdown] 用户消息触发（前端 content-rendering spec 用）
_E2E_MARKDOWN_REPLY = (
    "# E2E 渲染标题\n\n"
    "见 [OpenAI](https://openai.com) 官方文档与来源 [K1]。\n\n"
    "| 名称 | 值 |\n| --- | --- |\n| 甲 | 1 |\n| 乙 | 2 |\n\n"
    "```python\nprint(\"hello\")\n```\n"
)
class E2EApprovalTool:
    definition = ToolDefinition(
        name="e2e_approval",
        description="执行 E2E 审批验证操作",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_WRITE,
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=1.0,
    )

    def __init__(self) -> None:
        self.execution_count = 0

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        cancellation_token.raise_if_cancelled()
        self.execution_count += 1
        return ToolResult(
            tool_call_id=call.id,
            content=f"E2E 受控操作已执行：{call.arguments['value']}",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


class E2EProvider:
    """Deterministic provider with opt-in pause and failure controls."""

    name = "e2e"

    def __init__(self) -> None:
        self._gates: dict[str, asyncio.Event] = {}

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        user_content = next(
            (
                message.content
                for message in reversed(request.messages)
                if message.role == "user"
            ),
            "",
        )

        if "[e2e:approval]" in user_content:
            tool_result = next(
                (
                    message
                    for message in reversed(request.messages)
                    if message.role == "tool" and message.name == "e2e_approval"
                ),
                None,
            )
            if tool_result is None:
                yield ProviderTextDelta("E2E 等待审批")
                yield ProviderToolCall(
                    id=f"approval_{request.request_id}",
                    name="e2e_approval",
                    arguments={"value": "发布验收"},
                )
                yield ProviderCompleted(
                    finish_reason="tool_calls",
                    input_tokens=12,
                    output_tokens=4,
                )
                return

            if "未授权" in tool_result.content:
                yield ProviderTextDelta("E2E 操作已拒绝，未执行受控操作。")
            else:
                yield ProviderTextDelta("E2E 审批通过，受控操作已完成。")
            yield ProviderCompleted(
                finish_reason="stop",
                input_tokens=14,
                output_tokens=8,
            )
            return

        if "[e2e:markdown]" in user_content:
            yield ProviderTextDelta(_E2E_MARKDOWN_REPLY)
            yield ProviderCompleted(
                finish_reason="stop",
                input_tokens=12,
                output_tokens=8,
            )
            return

        yield ProviderTextDelta("E2E 确定性回复")

        if "[e2e:fail]" in user_content:
            raise ProviderError(
                "e2e_provider_failure",
                "E2E 模拟执行失败。",
                retryable=True,
            )

        if "[e2e:pause]" in user_content:
            gate = asyncio.Event()
            self._gates[request.request_id] = gate
            resume_task = asyncio.create_task(gate.wait())
            cancel_task = asyncio.create_task(cancellation_token.wait())
            done, pending = await asyncio.wait(
                (resume_task, cancel_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._gates.pop(request.request_id, None)
            if cancel_task in done:
                raise RuntimeCancelled()
            yield ProviderTextDelta("，恢复后完成")

        await asyncio.sleep(0.02)
        cancellation_token.raise_if_cancelled()
        yield ProviderCompleted(
            finish_reason="stop",
            input_tokens=12,
            output_tokens=8,
        )

    def resume_all(self) -> int:
        gates = tuple(self._gates.values())
        for gate in gates:
            gate.set()
        return len(gates)

    @property
    def paused_count(self) -> int:
        return len(self._gates)


if os.environ.get("ENDLESS_TASK_E2E") != "1":
    raise RuntimeError("The E2E fixture server requires ENDLESS_TASK_E2E=1")

database_path = Path(
    os.environ.get("ENDLESS_TASK_E2E_DB", "/tmp/endless-task-playwright.db")
).resolve()
for candidate in (
    database_path,
    Path(f"{database_path}-shm"),
    Path(f"{database_path}-wal"),
):
    candidate.unlink(missing_ok=True)

provider = E2EProvider()
approval_tool = E2EApprovalTool()
tool_registry = ToolRegistry()
tool_registry.register(approval_tool)
app = create_app(
    settings=AppSettings(
        database_path=database_path,
        provider_name="fake",
        model="e2e-model",
        heartbeat_seconds=0.1,
        memory_proposals_enabled=False,
        artifact_proposals_enabled=False,
        task_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        knowledge_decay_enabled=False,
        scheduler_enabled=False,
        task_run_review_enabled=False,
        notifications_enabled=False,
    ),
    provider=provider,
    tool_registry=tool_registry,
)

E2E_MODEL_CREDENTIALS = {
    "Bearer e2e-model-key-one": "key-one",
    "Bearer e2e-model-key-two": "key-two",
}
e2e_model_service_state: dict[str, str | None] = {
    "lastCredentialVersion": None,
}


@app.get("/__e2e/openai/models")
async def e2e_model_catalog(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    credential_version = E2E_MODEL_CREDENTIALS.get(authorization or "")
    if credential_version is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_api_key", "message": "invalid credential"},
        )
    e2e_model_service_state["lastCredentialVersion"] = credential_version
    return {
        "object": "list",
        "data": [
            {
                "id": "e2e-alpha",
                "object": "model",
                "created": 0,
                "owned_by": "e2e",
            },
            {
                "id": "e2e-beta",
                "object": "model",
                "created": 0,
                "owned_by": "e2e",
            },
        ],
    }


@app.get("/__e2e/model-service")
async def e2e_model_service_status() -> dict[str, str | None]:
    return dict(e2e_model_service_state)


@app.get("/__e2e/provider")
async def provider_state() -> dict[str, int]:
    return {
        "pausedCount": provider.paused_count,
        "approvalExecutionCount": approval_tool.execution_count,
    }


@app.post("/__e2e/provider/resume")
async def resume_provider() -> dict[str, int]:
    return {"resumedCount": provider.resume_all()}


@app.post("/__e2e/fixtures/failed-auto-restored-run", status_code=200)
async def fixture_failed_auto_restored_run(
    body: dict,
) -> dict:
    """② fixture：为一条会话制造 FAILED run + run_auto_restored 事件（仅 E2E）。"""
    from endless_task.runtime_v2 import Actor, RunStatus, TranscriptEntryType

    conversation_id = str(body.get("conversationId", "")).strip()
    container = app.state.container
    repo = container.runtime_v2_repository
    conversation = container.chat_repository.get_conversation(conversation_id)
    lanes = repo.list_lanes(conversation.id)
    lane = next(
        (l for l in lanes if getattr(l, "kind", None) is not None and l.kind.value == "main"),
        lanes[0] if lanes else repo.create_lane(conversation_id=conversation.id),
    )
    trigger = repo.append_entry(
        conversation_id=conversation.id,
        lane_id=lane.id,
        type=TranscriptEntryType.USER_MESSAGE,
        actor=Actor.USER,
        payload={"content": "（E2E fixture 注入的失败 run）"},
        context_policy={"include_in_llm": True, "transform": "full"},
    )
    run = repo.create_run(
        conversation_id=conversation.id,
        lane_id=lane.id,
        trigger_entry_id=trigger.id,
    )
    repo.update_run_status(
        run.id,
        RunStatus.FAILED,
        error_code="provider_timeout",
        safe_message="模型调用超时（E2E fixture）。",
    )
    repo.append_runtime_event(
        run_id=run.id,
        event_type="run_failed",
        payload={"errorCode": "provider_timeout", "safeMessage": "模型调用超时（E2E fixture）。"},
    )
    repo.append_runtime_event(
        run_id=run.id,
        event_type="run_auto_restored",
        payload={
            "errorCode": "provider_timeout",
            "workspaces": 1,
            "restored": 2,
            "skipped": 0,
            "trigger": "e2e_fixture",
        },
    )
    return {"runId": run.id}


@app.post("/__e2e/proposals", status_code=201)
async def seed_proposal(body: dict[str, str]) -> dict[str, str]:
    conversation_id = body["conversationId"]
    turn_id = body["turnId"]
    kind = body["kind"]
    marker = f"{conversation_id}:{turn_id}"
    container = app.state.container

    if kind == "artifact":
        proposal = container.artifact_proposal_repository.create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            title="E2E 发布记录",
            kind=ArtifactKind.MARKDOWN,
            content=f"# E2E 发布记录\n\n用于验证 Artifact 结果入口。\n\n{marker}",
            reason="验证接受后可打开工作区结果。",
        )
    elif kind == "knowledge":
        proposal = container.knowledge_proposal_repository.create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            proposal_type=KnowledgeProposalType.ADD_SOURCE,
            payload={
                "title": "E2E 知识条目",
                "content": f"用于验证知识结果入口。{marker}",
                "reason": "验证接受后可打开知识页。",
            },
        )
    elif kind == "memory":
        proposal = container.proposal_repository.create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            kind=MemoryKind.PREFERENCE,
            content=f"E2E 用户偏好简洁回答。{marker}",
            reason="验证接受后可打开记忆页。",
        )
    elif kind == "task":
        proposal = container.task_proposal_repository.create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            title="E2E 每周发布检查",
            commitment=f"每周检查发布状态。{marker}",
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            reason="验证接受后可打开已安排页。",
        )
    else:
        raise ValueError(f"Unsupported E2E proposal kind: {kind}")

    return {"id": proposal.id, "kind": kind}

@app.post("/__e2e/notifications", status_code=201)
async def seed_notification(body: dict[str, str]) -> dict[str, str]:
    """P1-2 通知推送 E2E fixture：按生产 TaskNotificationService 写点路径
    落库一条未读通知并 append hub 事件（notification.created）。"""
    conversation_id = body["conversationId"]
    container = app.state.container
    created = container.notification_repository.record(
        kind=NotificationKind.RUN_COMPLETED,
        task_id="e2e-task",
        run_id=f"e2e-run-{conversation_id}",
        conversation_id=conversation_id,
        title="E2E 后台任务完成",
        body="验证 hub SSE 推送：未读角标应即时出现。",
    )
    container.hub_event_repository.append(
        "notification.created",
        conversation_id=conversation_id,
        data={
            "kind": NotificationKind.RUN_COMPLETED.value,
            "conversationId": conversation_id,
            "title": "E2E 后台任务完成",
        },
    )
    if not created:
        raise HTTPException(status_code=409, detail="duplicate e2e notification")
    return {"conversationId": conversation_id}
