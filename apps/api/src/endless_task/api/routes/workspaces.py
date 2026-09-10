"""workspaces 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
见 docs/product-improvements/05-code-health/01-monolith-audit.md。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, WebSocket
from fastapi.responses import Response

from endless_task.domain.repositories import InvalidStateError, NotFoundError
from endless_task.workspace_runtime.system_terminal import open_system_terminal
from endless_task.workspace_runtime.terminal import (
    DEFAULT_COLS as TERMINAL_DEFAULT_COLS,
    DEFAULT_ROWS as TERMINAL_DEFAULT_ROWS,
)

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.workspaces import (
    TerminalCreateBody,
    WorkspaceBody,
    WorkspaceFileWriteBody,
    WorkspacePatchBody,
)
from ..serialization import workspace_json

logger = logging.getLogger(__name__)


def register_workspaces_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/workspaces")
    async def list_workspaces() -> dict[str, object]:
        workspaces = container.workspace_repository.list_workspaces()
        return {
            "items": [workspace_json(item) for item in workspaces],
            # 前端用它把工作区本地路径显示为 ~ 开头（根目录简写）。
            "homePath": str(Path.home()),
        }


    @app.post("/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceBody) -> dict[str, object]:
        workspace = container.workspace_repository.create_workspace(body.name)
        if body.rootPath is not None and body.rootPath.strip():
            workspace = container.workspace_repository.bind_root_path(
                workspace.id, body.rootPath
            )
        return {"workspace": workspace_json(workspace)}


    @app.patch("/workspaces/{workspace_id}")
    async def patch_workspace(
        workspace_id: str, body: WorkspacePatchBody
    ) -> dict[str, object]:
        if body.rootPath is None:
            workspace = container.workspace_repository.unbind_root_path(workspace_id)
        else:
            workspace = container.workspace_repository.bind_root_path(
                workspace_id, body.rootPath
            )
        return {"workspace": workspace_json(workspace)}


    @app.delete("/workspaces/{workspace_id}", status_code=204)
    async def delete_workspace(workspace_id: str) -> Response:
        # 级联删除：该工作区名下的会话（含后代分支）一并删除；先清理其相关的
        # 提醒/任务/提案，再删除会话，最后删除工作区注册。
        conversation_ids = container.chat_repository.list_workspace_conversation_ids(
            workspace_id
        )
        for target_id in conversation_ids:
            descendant_ids = container.chat_repository.list_descendant_ids(target_id)
            for delete_id in (target_id, *descendant_ids):
                container.reminder_repository.cancel_reminders_for_conversation(
                    delete_id
                )
                container.task_repository.cancel_tasks_for_conversation(delete_id)
                container.task_proposal_repository.cancel_proposals_for_conversation(
                    delete_id
                )
                container.proposal_repository.cancel_proposals_for_conversation(
                    delete_id
                )
        for target_id in conversation_ids:
            try:
                container.chat_repository.delete_conversation(target_id)
            except InvalidStateError as error:
                raise ApiRequestError(
                    "workspace_conversation_active",
                    f"工作区下的会话「{target_id}」仍在运行，请先停止后重试。",
                    status_code=409,
                ) from error
        removed = container.workspace_repository.delete_workspace(workspace_id)
        if not removed:
            raise ApiRequestError(
                "workspace_not_found",
                "所选工作区已不存在。",
                status_code=404,
            )
        return Response(status_code=204)


    @app.get("/workspaces/{workspace_id}/file-preview")
    async def preview_workspace_file(
        workspace_id: str,
        path: str = Query(..., min_length=1, max_length=1024),
        start_line: int = Query(1, ge=1),
        line_count: int = Query(200, ge=1, le=2000),
    ) -> dict[str, object]:
        """17 切片③：工作区文件只读预览（引用溯源跳转落点）。

        按相对工作区根的 path 返回行数组（含行号），供前端引用卡点击后在
        抽屉中定位到具体行。路径安全与 read_workspace_file 同源
        （resolve_read_path_with_variants + UTF-8 校验 + 大小上限）。
        """
        from endless_task.tooling import ToolError as _WsToolError
        from endless_task.workspace_runtime.fs_tools import (
            _decode_utf8 as _ws_decode_utf8,
        )
        from endless_task.workspace_runtime.path_safety import (
            resolve_read_path_with_variants,
        )

        def _map_tool_error(error: _WsToolError) -> None:
            status = 404 if error.code == "path_not_found" else 400
            message = getattr(error, "safe_message", None) or str(error)
            raise ApiRequestError(error.code, message, status_code=status) from None

        try:
            workspace = container.workspace_repository.get_workspace(workspace_id)
        except NotFoundError:
            raise ApiRequestError("workspace_not_found", "工作区不存在。", status_code=404) from None
        if not workspace.root_path:
            raise ApiRequestError(
                "workspace_not_bound",
                "该工作区未绑定本地目录。",
            )
        root = Path(workspace.root_path).expanduser().resolve()
        if not root.is_dir():
            raise ApiRequestError(
                "workspace_not_bound",
                "该工作区绑定目录不可用。",
            )
        try:
            resolved = resolve_read_path_with_variants(root, path)
        except _WsToolError as error:
            _map_tool_error(error)
        canonical = resolved.canonical
        if not canonical.exists():
            raise ApiRequestError("path_not_found", "文件不存在。", status_code=404)
        if not canonical.is_file():
            raise ApiRequestError("path_is_directory", "路径指向目录。")
        if canonical.stat().st_size > container.settings.max_file_bytes:
            raise ApiRequestError(
                "file_too_large",
                f"文件超过读取上限（{container.settings.max_file_bytes} 字节）。",
            )
        raw = canonical.read_bytes()
        try:
            text = _ws_decode_utf8(raw)
        except _WsToolError as error:
            _map_tool_error(error)
        lines = text.splitlines() or [""]
        total = len(lines)
        end_line = min(total, start_line + line_count - 1)
        rows = [
            {"line": index + 1, "text": lines[index]}
            for index in range(start_line - 1, end_line)
        ]
        return {
            "path": resolved.original_raw,
            "workspaceId": workspace_id,
            "totalLines": total,
            "startLine": start_line,
            "endLine": end_line,
            "variant": resolved.variant,
            "lines": rows,
        }


    @app.get("/workspaces/{workspace_id}/tree")
    async def list_workspace_tree(
        workspace_id: str,
        path: str = Query("", max_length=1024),
        show_hidden: bool = Query(False),
    ) -> dict[str, object]:
        """P0 文件面板：工作区文件树（单层、只读、工作区根内）。

        与 `file-preview` 同源：路径解析走 `resolve_workspace_path`（拒绝绝对
        路径与 `..`、canonicalize 后校验 containment），列目录复用
        `browse_directory`（默认隐藏点文件、单层最多 200 项）。
        """
        from endless_task.tooling import ToolError as _WsToolError
        from endless_task.workspace_runtime.browse import (
            MAX_BROWSE_ITEMS as _WS_MAX_BROWSE_ITEMS,
            browse_directory as _ws_browse_directory,
        )
        from endless_task.workspace_runtime.path_safety import (
            resolve_workspace_path as _ws_resolve_workspace_path,
        )

        try:
            workspace = container.workspace_repository.get_workspace(workspace_id)
        except NotFoundError:
            raise ApiRequestError(
                "workspace_not_found", "工作区不存在。", status_code=404
            ) from None
        if not workspace.root_path:
            raise ApiRequestError(
                "workspace_not_bound", "该工作区未绑定本地目录。"
            )
        root = Path(workspace.root_path).expanduser().resolve()
        if not root.is_dir():
            raise ApiRequestError(
                "workspace_not_bound", "该工作区绑定目录不可用。"
            )

        raw_path = (path or "").strip()
        if raw_path in ("", ".", "/"):
            target = root
            display_path = ""
        else:
            try:
                resolved = _ws_resolve_workspace_path(root, raw_path)
            except _WsToolError as error:
                status = 404 if error.code == "path_not_found" else 400
                message = getattr(error, "safe_message", None) or str(error)
                raise ApiRequestError(
                    error.code, message, status_code=status
                ) from None
            target = resolved.canonical
            display_path = resolved.original_raw

        if not target.exists():
            raise ApiRequestError("path_not_found", "目录不存在。", status_code=404)
        if not target.is_dir():
            raise ApiRequestError("path_is_file", "路径指向文件，不是目录。")

        current, items = _ws_browse_directory(str(target), show_hidden=show_hidden)
        entries = []
        for item in items:
            entry_path = Path(item.path)
            try:
                relative_path = str(entry_path.relative_to(root))
            except ValueError:
                continue
            # 符号链接可能指向工作区外：解析后仍必须落在根内，否则不进文件树。
            try:
                if not entry_path.resolve(strict=False).is_relative_to(root):
                    continue
            except OSError:
                continue
            entries.append(
                {
                    "name": item.name,
                    "relativePath": relative_path.replace(os.sep, "/"),
                    "kind": item.kind,
                    "size": item.size,
                    "isHidden": item.is_hidden,
                }
            )
        return {
            "workspaceId": workspace_id,
            "path": display_path,
            "currentPath": current,
            "rootPath": str(root),
            "entries": entries,
            "truncated": len(items) >= _WS_MAX_BROWSE_ITEMS,
        }


    @app.post("/workspaces/{workspace_id}/terminal/open")
    async def open_workspace_terminal(workspace_id: str) -> dict[str, object]:
        """S3：在绑定的工作区目录打开用户的系统终端。

        这是**用户的终端**：其中执行的命令不经过应用的逐条确认（前端需明示）。
        打开失败不抛 500，返回 ``opened=false`` 与可读原因。
        """
        _workspace, root = _workspace_root_or_error(workspace_id)
        result = open_system_terminal(root)
        return {
            "opened": result.opened,
            "cwd": result.cwd,
            "launcher": result.launcher,
            "message": result.message,
        }


    @app.post("/workspaces/{workspace_id}/terminals", status_code=201)
    async def create_workspace_terminal(
        workspace_id: str,
        body: Optional[TerminalCreateBody] = None,
    ) -> dict[str, object]:
        """S4：创建一个持久 PTY 会话（每工作区上限 3 个）。"""
        service = container.terminal_service
        if service is None:
            raise ApiRequestError(
                "terminal_unavailable", "当前部署未启用内嵌终端。", status_code=503
            )
        _workspace, root = _workspace_root_or_error(workspace_id)
        name = (body.name if body is not None else None) or ""
        rows = body.rows if body is not None and body.rows else TERMINAL_DEFAULT_ROWS
        cols = body.cols if body is not None and body.cols else TERMINAL_DEFAULT_COLS
        try:
            session = await service.create(
                workspace_id=workspace_id,
                cwd=root,
                name=name[:64],
                rows=rows,
                cols=cols,
            )
        except ValueError as error:
            raise ApiRequestError("terminal_create_failed", str(error)) from error
        return {"terminal": session.snapshot().as_dict()}


    @app.get("/workspaces/{workspace_id}/terminals")
    async def list_workspace_terminals(workspace_id: str) -> dict[str, object]:
        service = container.terminal_service
        if service is None:
            return {"items": []}
        _workspace_root_or_error(workspace_id)
        return {
            "items": [
                session.snapshot().as_dict()
                for session in service.list_for_workspace(workspace_id)
            ]
        }


    @app.delete("/workspaces/{workspace_id}/terminals/{session_id}")
    async def close_workspace_terminal(
        workspace_id: str, session_id: str
    ) -> dict[str, object]:
        service = container.terminal_service
        if service is None:
            return {"closed": False}
        closed = await service.close(
            workspace_id=workspace_id, session_id=session_id
        )
        return {"closed": closed}


    @app.websocket("/workspaces/{workspace_id}/terminals/{session_id}")
    async def workspace_terminal_socket(
        websocket: WebSocket, workspace_id: str, session_id: str
    ) -> None:
        """S4：终端双向通道（input/resize/signal ↔ output/status）。

        原始字节直通前端（交给 xterm.js 仿真）；会话不随 WS 断开而销毁。
        """
        service = container.terminal_service
        await websocket.accept()
        if service is None:
            await websocket.send_json(
                {"type": "error", "message": "当前部署未启用内嵌终端。"}
            )
            await websocket.close(code=1011)
            return
        try:
            session = service.get(workspace_id=workspace_id, session_id=session_id)
        except KeyError:
            await websocket.send_json(
                {"type": "error", "message": "终端会话不存在。"}
            )
            await websocket.close(code=1008)
            return

        outgoing: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=2048)

        def push(frame: dict[str, object]) -> None:
            try:
                outgoing.put_nowait(frame)
            except asyncio.QueueFull:
                pass

        def on_output(text: str) -> None:
            push({"type": "output", "data": text})

        def on_exit(status) -> None:
            push(
                {
                    "type": "exit",
                    "code": status.exit_code,
                    "signal": status.signal,
                }
            )

        unsubscribe_output = session.add_listener(on_output)
        unsubscribe_exit = session.add_exit_listener(on_exit)
        tail = session.read(offset=0, count=500)
        push(
            {
                "type": "ready",
                "terminal": session.snapshot().as_dict(),
                "scrollback": tail.text,
            }
        )
        if not session.alive:
            push(
                {
                    "type": "exit",
                    "code": session.status.exit_code,
                    "signal": session.status.signal,
                }
            )

        async def sender() -> None:
            while True:
                frame = await outgoing.get()
                await websocket.send_json(frame)

        async def receiver() -> None:
            while True:
                message = await websocket.receive_json()
                kind = message.get("type") if isinstance(message, dict) else None
                if kind == "input":
                    session.write(str(message.get("data", "")))
                elif kind == "resize":
                    try:
                        session.resize(
                            int(message.get("rows", 0)),
                            int(message.get("cols", 0)),
                        )
                    except (TypeError, ValueError):
                        continue
                elif kind == "signal":
                    session.signal(str(message.get("signal", "SIGINT")))
                elif kind == "ping":
                    push({"type": "pong"})

        sender_task = asyncio.create_task(sender())
        receiver_task = asyncio.create_task(receiver())
        try:
            done, pending = await asyncio.wait(
                {sender_task, receiver_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
        except Exception:  # noqa: BLE001 断链/协议异常都不影响会话本身
            logger.debug("Terminal socket loop failed", exc_info=True)
        finally:
            unsubscribe_output()
            unsubscribe_exit()
            with contextlib.suppress(Exception):
                await websocket.close()


    def _workspace_root_or_error(workspace_id: str) -> tuple[object, Path]:
        """解析工作区与其绑定根目录（供文件读写端点共用）。"""
        try:
            workspace = container.workspace_repository.get_workspace(workspace_id)
        except NotFoundError:
            raise ApiRequestError(
                "workspace_not_found", "工作区不存在。", status_code=404
            ) from None
        if not workspace.root_path:
            raise ApiRequestError("workspace_not_bound", "该工作区未绑定本地目录。")
        root = Path(workspace.root_path).expanduser().resolve()
        if not root.is_dir():
            raise ApiRequestError("workspace_not_bound", "该工作区绑定目录不可用。")
        return workspace, root


    def _resolve_workspace_file_or_error(root: Path, path: str) -> object:
        from endless_task.tooling import ToolError as _WsToolError
        from endless_task.workspace_runtime.path_safety import (
            resolve_workspace_path as _ws_resolve_workspace_path,
        )

        try:
            return _ws_resolve_workspace_path(root, path)
        except _WsToolError as error:
            status = 404 if error.code == "path_not_found" else 400
            message = getattr(error, "safe_message", None) or str(error)
            raise ApiRequestError(error.code, message, status_code=status) from None


    def _file_version_of(raw: bytes, stat_result) -> str:
        import hashlib as _hashlib

        return (
            f"{_hashlib.sha256(raw).hexdigest()[:16]}"
            f":{stat_result.st_mtime_ns}:{stat_result.st_size}"
        )


    @app.get("/workspaces/{workspace_id}/file")
    async def read_workspace_file_api(
        workspace_id: str,
        path: str = Query(..., min_length=1, max_length=1024),
    ) -> dict[str, object]:
        """P1：读取单个工作区文件（完整内容 + 版本 token），供编辑器使用。"""
        from endless_task.tooling import ToolError as _WsToolError
        from endless_task.workspace_runtime.fs_tools import _decode_utf8 as _ws_decode

        _workspace, root = _workspace_root_or_error(workspace_id)
        resolved = _resolve_workspace_file_or_error(root, path)
        canonical = resolved.canonical
        if not canonical.exists():
            raise ApiRequestError("path_not_found", "文件不存在。", status_code=404)
        if canonical.is_dir():
            raise ApiRequestError("path_is_directory", "路径指向目录。")
        stat_result = canonical.stat()
        if stat_result.st_size > container.settings.max_file_bytes:
            raise ApiRequestError(
                "file_too_large",
                f"文件超过编辑上限（{container.settings.max_file_bytes} 字节），"
                "请用系统程序打开。",
            )
        raw = canonical.read_bytes()
        try:
            text = _ws_decode(raw)
        except _WsToolError as error:
            message = getattr(error, "safe_message", None) or str(error)
            raise ApiRequestError(error.code, message) from None
        return {
            "workspaceId": workspace_id,
            "path": resolved.original_raw,
            "version": _file_version_of(raw, stat_result),
            "size": stat_result.st_size,
            "totalLines": len(text.splitlines()) or 1,
            "content": text,
        }


    @app.put("/workspaces/{workspace_id}/file")
    async def write_workspace_file_api(
        workspace_id: str,
        body: WorkspaceFileWriteBody,
        path: str = Query(..., min_length=1, max_length=1024),
        conversation_id: Optional[str] = Query(None),
    ) -> dict[str, object]:
        """P1：保存单个工作区文件（If-Match 版本 token，冲突返回 409）。

        与 `write_workspace_file` 工具共用路径锁、undo journal 与审计日志；
        区别是这里由人操作（`approver="user"`），并以版本 token 做乐观并发。
        """
        from endless_task.tooling import ToolError as _WsToolError
        from endless_task.workspace_runtime.effect_log import EffectReceipt
        from endless_task.workspace_runtime.fs_tools import (
            _PATH_LOCKS as _ws_path_locks,
            _read_text_or_none as _ws_read_text_or_none,
            sha256_text as _ws_sha256_text,
        )

        if not conversation_id:
            raise ApiRequestError(
                "conversation_required", "保存工作区文件需要 conversation_id。"
            )
        try:
            conversation = container.chat_repository.get_conversation(conversation_id)
        except NotFoundError:
            raise ApiRequestError(
                "conversation_not_found", "会话不存在。", status_code=404
            ) from None
        if conversation.workspace_id != workspace_id:
            raise ApiRequestError(
                "conversation_workspace_mismatch",
                "该会话不属于此工作区，无法记录撤销日志。",
            )

        _workspace, root = _workspace_root_or_error(workspace_id)
        resolved = _resolve_workspace_file_or_error(root, path)
        canonical = resolved.canonical
        if canonical.exists() and canonical.is_dir():
            raise ApiRequestError("path_is_directory", "路径指向目录，不能覆盖为文件。")
        content = body.content
        encoded = content.encode("utf-8")
        if len(encoded) > container.settings.max_file_bytes:
            raise ApiRequestError(
                "write_too_large",
                f"内容超过写入上限（{container.settings.max_file_bytes} 字节）。",
            )

        lock = await _ws_path_locks.acquire(str(canonical))
        async with lock:
            before_exists = canonical.exists()
            if before_exists:
                before_stat = canonical.stat()
                before_raw = canonical.read_bytes()
                current_version = _file_version_of(before_raw, before_stat)
                before_content = _ws_read_text_or_none(canonical)
            else:
                before_raw = b""
                current_version = None
                before_content = None
            if body.version != current_version:
                raise ApiRequestError(
                    "file_version_conflict",
                    "文件已被其它改动更新，请先对比再决定是否覆盖。",
                    status_code=409,
                    details={
                        "path": resolved.original_raw,
                        "currentVersion": current_version,
                        "currentContent": before_content,
                        "beforeExists": before_exists,
                    },
                )
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text(content, encoding="utf-8")
            after_stat = canonical.stat()
            after_version = _file_version_of(encoded, after_stat)

        undo_entry = None
        if container.undo_service is not None:
            undo_entry = container.undo_service.record_file_write(
                conversation_id=conversation_id,
                target=resolved.original_raw,
                workspace_root=str(root),
                before_content=before_content,
                before_exists=before_exists,
                after_hash=_ws_sha256_text(content),
                workspace_id=workspace_id,
            )
        try:
            container.effect_log.append(
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                workspace_root=str(root),
                operation="human_write",
                detail=resolved.original_raw,
                receipt=EffectReceipt(
                    kind="file_write",
                    path=str(canonical),
                    sha256=_ws_sha256_text(content),
                    executed_at=datetime.now(timezone.utc).isoformat(),
                ),
                approver="user",
            )
        except _WsToolError as error:
            # 文件已写入但审计失败：如实告知，不假装成功。
            raise ApiRequestError(
                "unknown_outcome",
                "文件已保存，但本地审计日志写入失败；请到工作区设置检查日志目录后核对结果。",
                status_code=500,
            ) from error
        return {
            "workspaceId": workspace_id,
            "path": resolved.original_raw,
            "version": after_version,
            "size": after_stat.st_size,
            "totalLines": len(content.splitlines()) or 1,
            "undoEntryId": undo_entry.id if undo_entry is not None else None,
        }


    @app.get("/workspaces/{workspace_id}/shell-log")
    async def list_shell_log(
        workspace_id: str, limit: int = Query(100, ge=1, le=500)
    ) -> dict[str, object]:
        container.workspace_repository.get_workspace(workspace_id)
        entries = container.effect_log.list_for_workspace(
            workspace_id, limit=limit
        )
        return {"items": entries}
