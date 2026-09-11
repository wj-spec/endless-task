"""v2 零散路由（从 app.py 搬出；路径、状态码、响应体零改动）。

集合：`/api/v2/metrics`、`/api/v2/hub/events`、`/api/v2/approvals/{id}`、
`/api/v2/eval/batches*`、`/api/v2/trajectory*`、`/api/v2/skills/packages`、
`/api/v2/temporary-conversations/{id}/promote`。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Query
from fastapi.responses import Response, StreamingResponse

from endless_task.domain.repositories import NotFoundError
from endless_task.runtime_v2 import ToolApprovalDecision

from ..container import AppContainer
from ..errors import ApiRequestError
from ..hub_support import hub_event_sse
from ..schemas.v2_misc import ResolveApprovalBody
from ..serialization import conversation_json


TRAJECTORY_BUNDLE_FILES = (
    "manifest.json",
    "events.jsonl",
    "spans.jsonl",
    "messages.redacted.jsonl",
    "tool-outcomes.redacted.jsonl",
)


def register_v2_misc_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/api/v2/skills/packages")
    async def list_runtime_v2_skill_packages(
        workspace_id: Optional[str] = Query(None, alias="workspaceId"),
    ) -> dict[str, object]:
        """P2-2a：skill 包 registry 只读查看（M5；flag 关时返回 enabled=false）。

        默认发现用户级技能根；传入 workspaceId 时叠加该工作区根（同 locator
        rank 语义：workspace 优先于 user）。包体内容与校验由 M5 离线单测覆盖，
        这里只暴露 registry 的可见视图，不执行导入/回滚。
        """
        if not container.settings.skill_packages_enabled:
            return {
                "enabled": False,
                "mode": "legacy",
                "packages": [],
                "conflicts": [],
            }
        from endless_task.skills import InMemorySkillRegistry, SkillRoot

        roots: list[SkillRoot] = []
        user_skills_dir = container.settings.database_path.parent / "skills"
        if user_skills_dir.exists():
            roots.append(SkillRoot(user_skills_dir, "user", 2))
        if workspace_id is not None:
            text = workspace_id.strip()
            if not text:
                raise ApiRequestError("invalid_request", "workspaceId 不能为空。")
            if text == "general":
                raise ApiRequestError(
                    "invalid_request", "通用工作区没有绑定目录，无技能包根。"
                )
            try:
                workspace = container.workspace_repository.get_workspace(text)
            except NotFoundError as error:
                raise NotFoundError(f"Unknown workspace: {text}") from error
            if workspace.root_path:
                root_path = Path(workspace.root_path).expanduser()
                if root_path.exists():
                    roots.append(SkillRoot(root_path, "workspace", 1))
        registry = InMemorySkillRegistry()
        report = registry.discover(tuple(roots))
        packages = [
            {
                "scope": revision.locator.scope,
                "name": revision.locator.name,
                "version": revision.version,
                "state": revision.state.value,
                "valid": revision.valid,
                "quarantined": revision.quarantined,
                "invocable": revision.invocable,
                "requiredTools": list(revision.required_tools),
                "requiredCapabilities": list(revision.required_capabilities),
            }
            for revision in report.revisions
        ]
        return {
            "enabled": True,
            "mode": "packages",
            "packages": packages,
            "conflicts": [
                {"code": diagnostic.code, "message": diagnostic.message}
                for diagnostic in report.conflicts
            ],
        }


    def _trajectory_export_root() -> Path:
        return container.settings.database_path.parent / "v2_trajectory_exports"


    @app.get("/api/v2/trajectory")
    async def list_trajectory_bundles() -> dict[str, object]:
        """P2-1a：失败/显式导出 trajectory bundle 的只读列表（开发者向）。"""
        root = _trajectory_export_root()
        items: list[dict[str, object]] = []
        if root.exists():
            for child in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
                if not child.is_dir():
                    continue
                manifest_path = child / "manifest.json"
                if not manifest_path.exists():
                    continue
                files = [
                    {"name": name, "size": (child / name).stat().st_size}
                    for name in TRAJECTORY_BUNDLE_FILES
                    if (child / name).exists()
                ]
                items.append({"runId": child.name, "files": files})
        return {"root": str(root), "items": items}


    @app.get("/api/v2/trajectory/{run_id}")
    async def get_trajectory_bundle(run_id: str) -> dict[str, object]:
        bundle_dir = _trajectory_export_root() / run_id
        manifest_path = bundle_dir / "manifest.json"
        if not bundle_dir.is_dir() or not manifest_path.exists():
            raise NotFoundError(f"Unknown trajectory bundle: {run_id}")
        manifest: dict[str, object] = {}
        try:
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                manifest = parsed
        except (OSError, ValueError):
            manifest = {}
        file_names = [
            name
            for name in TRAJECTORY_BUNDLE_FILES
            if (bundle_dir / name).exists()
        ]
        return {
            "runId": run_id,
            "fileNames": file_names,
            "manifest": manifest,
        }


    @app.get("/api/v2/trajectory/{run_id}/files/{file_name}")
    async def get_trajectory_bundle_file(
        run_id: str, file_name: str
    ) -> dict[str, object]:
        if file_name not in TRAJECTORY_BUNDLE_FILES:
            raise ApiRequestError(
                "invalid_request", f"不允许读取文件：{file_name}"
            )
        bundle_dir = _trajectory_export_root() / run_id
        target = bundle_dir / file_name
        if not bundle_dir.is_dir() or not target.exists():
            raise NotFoundError(f"Unknown trajectory bundle file: {run_id}/{file_name}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = ""
        return {"runId": run_id, "fileName": file_name, "content": content}


    @app.get("/api/v2/eval/batches")
    async def list_eval_batches() -> dict[str, object]:
        """P2-1c：离线 eval 批次只读列表（开发者向）。"""
        from endless_task.eval.storage import SqliteEvalRepository

        repository = SqliteEvalRepository(container.database)
        return {
            "items": [
                {
                    "id": batch.id,
                    "mode": batch.mode,
                    "status": batch.status,
                    "runCount": batch.run_count,
                    "createdAt": batch.created_at,
                    "judgeProvider": batch.judge_provider,
                    "judgeModel": batch.judge_model,
                    "aggregate": batch.aggregate,
                }
                for batch in repository.list_batches()
            ]
        }


    @app.get("/api/v2/eval/batches/{batch_id}")
    async def get_eval_batch(batch_id: str) -> dict[str, object]:
        from endless_task.eval.storage import SqliteEvalRepository

        repository = SqliteEvalRepository(container.database)
        batch = repository.get_batch(batch_id)  # unknown -> 404
        with container.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM eval_run_results WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        result_count = int(row["count"]) if row else 0
        return {
            "id": batch.id,
            "mode": batch.mode,
            "status": batch.status,
            "runCount": batch.run_count,
            "resultCount": result_count,
            "createdAt": batch.created_at,
            "judgeProvider": batch.judge_provider,
            "judgeModel": batch.judge_model,
            "aggregate": batch.aggregate,
        }


    @app.post("/api/v2/temporary-conversations/{conversation_id}/promote")
    async def promote_runtime_v2_temporary_conversation(
        conversation_id: str,
    ) -> dict[str, object]:
        await container.runtime_v2_gateway.promote_temporary_conversation(
            conversation_id
        )
        return {
            "conversation": conversation_json(
                container.chat_repository.get_conversation(conversation_id)
            )
        }


    @app.delete(
        "/api/v2/temporary-conversations/{conversation_id}",
        status_code=204,
    )
    async def delete_runtime_v2_temporary_conversation(
        conversation_id: str,
    ) -> Response:
        await container.runtime_v2_gateway.delete_temporary_conversation(
            conversation_id
        )
        return Response(status_code=204)


    @app.get("/api/v2/metrics")
    async def get_runtime_v2_metrics() -> dict[str, object]:
        return container.runtime_v2_gateway.metrics_summary()


    @app.get("/api/v2/hub/events")
    async def get_hub_v2_events(
        after_seq: int = Query(0, ge=0),
        idle_seconds: float = Query(0.0, ge=0),
    ) -> StreamingResponse:
        """P1-2 hub 全局事件流（跨会话通知/提案变更）。

        与 conversation-scoped 的 `/api/v2/conversations/{id}/events` 不同，hub
        角标由 App 顶层全局消费；本端点以全库唯一 event_seq 为游标推送变更信号，
        前端 `useAssistantHub` 订阅后降轮询为兜底。idle_seconds>0 时服务端在空转
        超过该秒数后主动结束连接（客户端 EventSource 自动重连），便于保鲜与测试。
        """
        latest_seq = container.hub_event_repository.latest_seq()
        if after_seq > latest_seq:
            raise ApiRequestError(
                "invalid_after_seq",
                "after_seq 超过 hub 事件游标。",
            )

        async def stream() -> AsyncIterator[str]:
            cursor = after_seq
            last_activity = asyncio.get_running_loop().time()
            last_heartbeat = last_activity
            poll_interval = min(0.05, container.settings.heartbeat_seconds)
            while True:
                events = container.hub_event_repository.list_after(cursor)
                emitted = False
                for event in events:
                    if event.event_seq <= cursor:
                        continue
                    cursor = event.event_seq
                    emitted = True
                    yield hub_event_sse(event)
                now = asyncio.get_running_loop().time()
                if emitted:
                    last_activity = now
                    last_heartbeat = now
                    continue
                if idle_seconds > 0 and now - last_activity >= idle_seconds:
                    return
                if (
                    not emitted
                    and now - last_heartbeat >= container.settings.heartbeat_seconds
                ):
                    last_heartbeat = now
                    yield ": heartbeat\n\n"
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


    @app.post("/api/v2/approvals/{approval_id}")
    async def resolve_runtime_v2_approval(
        approval_id: str,
        body: ResolveApprovalBody,
    ) -> dict[str, object]:
        decision = (
            ToolApprovalDecision.APPROVE
            if body.decision == "approve"
            else ToolApprovalDecision.MODIFY
            if body.decision == "modify"
            else ToolApprovalDecision.DENY
        )
        resolved = await container.runtime_v2_gateway.resolve_approval(
            approval_id,
            decision,
            modified_arguments=body.arguments,
        )
        return {"approvalId": approval_id, "resolved": resolved}
