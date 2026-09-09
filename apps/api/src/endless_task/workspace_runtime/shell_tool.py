"""run_shell 工具（R5.12c / S8）：工作区根内执行 bash 命令。

- 无状态 spawn、硬超时 + 软超时、输出上限截断、终端净化、凭据剔除。
- 危险命令恒显式确认（提权模式也不放行）。
- **S8 只读信任**：`ShellTrustPolicy` 开启时，确定只读的命令免逐条确认；
  默认关闭时行为与今天完全一致（全部确认）。
- **S8 变更对账**：执行前扫描工作区、执行后再次扫描，把 shell 造成的文件
  改动写进 undo journal（可撤销）与 effect log（可按路径审计）。
- 执行成功落副作用日志并返回 EffectReceipt；日志失败标记 unknown_outcome。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolActivityCopy,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolResult,
)

from .dangerous_commands import is_dangerous
from .shell_runner import _sanitized_env
from .effect_log import EffectLog, EffectReceipt, sha256_text
from .resolver import WorkspaceResolver
from .shell_reconcile import (
    DEFAULT_MAX_FILES,
    ReconciledChange,
    changed_paths,
    read_text_bounded,
    scan_workspace,
)
from .shell_runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_NO_CHANGE_TIMEOUT_SECONDS,
    DEFAULT_SHELL_TIMEOUT_SECONDS,
    ShellResult,
    run_shell_command,
)
logger = logging.getLogger(__name__)

_TRUNCATION_NOTICE = "\n[输出已截断，完整输出见工作区设置 → 命令历史]"

#: 一次命令最多为多少个改动文件落撤销/审计记录（超出只记摘要）。
MAX_RECONCILE_ENTRIES = 50


@dataclass(frozen=True)
class ReconcileOutcome:
    """一次 shell 变更对账的结果。"""

    changed: tuple[ReconciledChange, ...] = ()
    recorded: tuple[str, ...] = ()
    skipped: Optional[str] = None
    truncated: bool = False


class RunShellTool:
    definition = ToolDefinition(
        name="run_shell",
        description=(
            "在当前工作区根目录执行一条 bash 命令（非交互、无持久 shell 状态）。"
            "输出有上限，超时或输出无变化会被中止。部分危险命令始终需要用户确认。"
            "执行外部网络请求、删除、git push 等操作前，应说明意图并等待用户确认。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 16_384},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_ACTION,
        # 外部动作工具恒为 REQUIRED；只读免确认由 ToolTrustPolicy 在
        # 执行协调器层裁决（危险命令 force_confirm 优先，永不免确认）。
        approval_mode=ToolApprovalMode.REQUIRED,
        timeout_seconds=DEFAULT_SHELL_TIMEOUT_SECONDS,
        max_output_characters=32_000,
    )

    def __init__(
        self,
        resolver: WorkspaceResolver,
        effect_log: EffectLog,
        *,
        timeout_seconds: float = DEFAULT_SHELL_TIMEOUT_SECONDS,
        no_change_timeout_seconds: float = DEFAULT_NO_CHANGE_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        checkpoint_coordinator=None,
        undo_service=None,
        reconcile_max_files: int = DEFAULT_MAX_FILES,
        execution_backend=None,
        sandbox_network_mode=None,
    ) -> None:
        self._resolver = resolver
        self._effect_log = effect_log
        self._timeout_seconds = timeout_seconds
        self._no_change_timeout_seconds = no_change_timeout_seconds
        self._max_output_bytes = max_output_bytes
        # M3B run-level (方案 A): optional per-run workspace checkpoint
        # coordinator; snapshot before executing so a failed run can roll
        # back its shell/file side effects. None = current behavior.
        self._checkpoint_coordinator = checkpoint_coordinator
        # S8: shell 造成的文件改动也进撤销日志与审计日志。
        self._undo_service = undo_service
        self._reconcile_max_files = reconcile_max_files
        # S9: 可选 ExecutionEnvironment 后端（seatbelt/container）；None = 直接
        # 在本机 spawn（历史行为）。
        self._execution_backend = execution_backend
        self._sandbox_network_mode = sandbox_network_mode

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在执行命令",
            completed="命令执行完成",
            failed="命令执行失败",
            cancelled="命令已停止",
        )

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        return is_dangerous(call.require_argument("command", str))

    def approval_prompt(self, call: ToolCall) -> ToolApprovalPrompt:
        command = call.require_argument("command", str)
        dangerous = is_dangerous(command)
        return ToolApprovalPrompt(
            summary=(
                "允许执行 run_shell 吗？（危险命令）"
                if dangerous
                else "允许执行 run_shell 吗？"
            ),
            reason=(
                f"将在工作区根目录执行：{command}\n"
                + (
                    "该命令属于危险名单（删除/外发/提权等），即使已开启提权模式也需要确认。"
                    if dangerous
                    else "命令在工作区目录内执行，有超时与输出上限保护。"
                )
            ),
            metadata={"toolName": "run_shell", "command": command},
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        command = call.require_argument("command", str)
        self._ensure_checkpoint(call, binding)
        before = scan_workspace(
            binding.root, max_files=self._reconcile_max_files
        )
        result = await self._run(call, binding, command)
        outcome = self._reconcile(call, binding, before)
        return await self._finish(call, result, command, binding, token, outcome)

    async def execute_with_progress(
        self,
        call: ToolCall,
        token: CancellationToken,
        *,
        on_progress,
    ) -> ToolResult:
        """长命令执行中按间隔上报进度(可选能力,协调器检测到即启用)。"""
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        command = call.require_argument("command", str)
        self._ensure_checkpoint(call, binding)
        before = scan_workspace(
            binding.root, max_files=self._reconcile_max_files
        )
        result = await self._run(call, binding, command, on_progress=on_progress)
        outcome = self._reconcile(call, binding, before)
        return await self._finish(call, result, command, binding, token, outcome)

    async def _finish(
        self,
        call: ToolCall,
        result: ShellResult,
        command: str,
        binding,
        token: CancellationToken,
        outcome: ReconcileOutcome = ReconcileOutcome(),
    ) -> ToolResult:
        combined = result.stdout
        if result.stderr:
            combined += ("\n" if combined else "") + f"[stderr]\n{result.stderr}"
        if len(combined) > self.definition.max_output_characters:
            combined = (
                combined[: self.definition.max_output_characters]
                + _TRUNCATION_NOTICE
            )
            result = ShellResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                no_change_timeout=result.no_change_timeout,
                truncated=True,
                duration_seconds=result.duration_seconds,
            )
        if not combined.strip():
            combined = "（命令无输出）"
        if result.exit_code is not None and result.exit_code != 0:
            combined += (
                f"\n[退出码 {result.exit_code}"
                + ("，超时中止" if result.timed_out else "")
                + "]"
            )
        elif result.timed_out:
            combined += (
                "\n[命令超时"
                + ("：输出无变化" if result.no_change_timeout else "")
                + "，已中止；可向用户说明后调整重试]"
            )
        combined += self._reconcile_notice(outcome)
        now_iso = _now_iso()
        receipt = EffectReceipt(
            kind="shell",
            path=command,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            truncated=result.truncated,
            executed_at=now_iso,
        )
        try:
            self._effect_log.append(
                conversation_id=call.conversation_id,
                workspace_id=binding.workspace_id,
                workspace_root=str(binding.root),
                operation="run_shell",
                detail=command,
                receipt=receipt,
                duration_ms=int(result.duration_seconds * 1000),
            )
        except ToolError:
            receipt = EffectReceipt(
                kind="shell",
                path=command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                truncated=result.truncated,
                executed_at=now_iso,
                unknown_outcome=True,
            )
            combined += "\n[副作用已发生，但本地审计日志写入失败；请到工作区设置核对命令历史]"
        token.raise_if_cancelled()
        return ToolResult(
            tool_call_id=call.id,
            content=combined,
            structured_content={
                "exitCode": result.exit_code,
                "timedOut": result.timed_out,
                "noChangeTimeout": result.no_change_timeout,
                "truncated": result.truncated,
                "durationMs": int(result.duration_seconds * 1000),
                "effect": receipt.as_dict(),
                "changedPaths": [change.relative for change in outcome.changed],
                "reconcileSkipped": outcome.skipped,
                "reconcileTruncated": outcome.truncated,
            },
        )

    # ---------- S9 沙箱执行 ----------

    async def _run(
        self,
        call: ToolCall,
        binding,
        command: str,
        *,
        on_progress=None,
    ) -> ShellResult:
        """执行命令：配置了 ExecutionEnvironment 后端则走沙箱，否则本机直跑。"""
        backend = self._execution_backend
        if backend is None:
            kwargs = {}
            if on_progress is not None:
                kwargs["on_progress"] = on_progress
            return await run_shell_command(
                command=command,
                cwd=binding.root,
                timeout_seconds=self._timeout_seconds,
                no_change_timeout_seconds=self._no_change_timeout_seconds,
                max_output_bytes=self._max_output_bytes,
                **kwargs,
            )
        from endless_task.agent_platform import AgentPlatformError
        from endless_task.execution_env import (
            ExecutionPolicy,
            ProcessRequest,
        )
        from endless_task.runtime_ledger import TraceContext

        run_id = call.response_variant_id or call.id
        network_mode = self._sandbox_network_mode
        if network_mode is None:
            from endless_task.execution_env import NetworkMode

            network_mode = NetworkMode.DENY
        request = ProcessRequest(
            effect_id=f"shell:{call.id}",
            tool_call_id=call.id,
            argv=("/bin/bash", "-lc", command),
            cwd=str(Path(binding.root).expanduser().resolve()),
            policy=ExecutionPolicy(
                workspace_root=str(Path(binding.root).expanduser().resolve()),
                read_allow_paths=(),
                write_allow_paths=(),
                network_mode=network_mode,
                timeout_seconds=self._timeout_seconds,
            ),
            trace=TraceContext(
                trace_id=run_id, run_id=run_id, correlation_id=run_id
            ),
            requested_at=_now_iso(),
            environment=_sanitized_env(),
        )
        started = _monotonic()
        try:
            result = await backend.run_process(request)
        except AgentPlatformError as error:
            raise ToolError(
                error.code,
                "命令未执行：当前沙箱后端不可用（已按失败关闭处理）。",
                retryable=False,
            ) from error
        duration = _monotonic() - started
        return ShellResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            timed_out=result.exit_code is None,
            no_change_timeout=False,
            truncated=result.truncated,
            duration_seconds=duration,
        )

    # ---------- S8 变更对账 ----------

    def _reconcile_notice(self, outcome: ReconcileOutcome) -> str:
        if outcome.skipped == "workspace_too_large":
            return "\n[工作区文件过多，本次未做文件变更对账]"
        if not outcome.changed:
            return ""
        listed = "、".join(outcome.recorded[:10])
        more = (
            f" 等 {len(outcome.changed)} 个"
            if len(outcome.changed) > len(outcome.recorded[:10])
            else ""
        )
        suffix = "（已记录，可在撤销日志回滚）" if outcome.recorded else "（未记录）"
        return f"\n[本次命令改动了工作区文件：{listed}{more}{suffix}]"

    def _reconcile(
        self,
        call: ToolCall,
        binding,
        before,
    ) -> ReconcileOutcome:
        if before is None:
            return ReconcileOutcome(skipped="workspace_too_large")
        after = scan_workspace(
            binding.root, max_files=self._reconcile_max_files
        )
        if after is None:
            return ReconcileOutcome(skipped="workspace_too_large")
        changes = changed_paths(before, after)
        if not changes:
            return ReconcileOutcome()
        recorded: list[str] = []
        truncated = len(changes) > MAX_RECONCILE_ENTRIES
        for change in changes[:MAX_RECONCILE_ENTRIES]:
            if self._record_change(call, binding, change):
                recorded.append(change.relative)
        return ReconcileOutcome(
            changed=tuple(changes),
            recorded=tuple(recorded),
            truncated=truncated,
        )

    def _record_change(
        self,
        call: ToolCall,
        binding,
        change: ReconciledChange,
    ) -> bool:
        """为单个变更落 undo / effect / ledger 记录；返回是否成功记录撤销。"""
        absolute = Path(binding.root).expanduser() / change.relative
        run_id = call.response_variant_id or call.id
        workspace_id = getattr(binding, "workspace_id", None)
        if change.kind == "deleted":
            before_text = self._checkpoint_text(call, binding, change.relative)
            if before_text is not None and self._undo_service is not None:
                try:
                    self._undo_service.record_file_delete(
                        conversation_id=call.conversation_id,
                        target=change.relative,
                        workspace_root=str(binding.root),
                        before_content=before_text,
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                except Exception:  # noqa: BLE001 撤销记录失败不影响命令结果
                    logger.debug("Shell undo delete record failed", exc_info=True)
            self._log_change(call, binding, "shell_file_delete", change.relative, None)
            self._record_ledger(
                run_id, change.relative, "file_delete",
                sha256_text(before_text) if before_text is not None else None,
                None,
            )
            return before_text is not None

        after_text = read_text_bounded(absolute)
        after_hash = sha256_text(after_text) if after_text is not None else None
        before_text = (
            None
            if change.kind == "created"
            else self._checkpoint_text(call, binding, change.relative)
        )
        undo_recorded = False
        if self._undo_service is not None and (
            change.kind == "created" or before_text is not None
        ):
            # modified 但拿不到 before 内容时不记录：宁可不可撤销，也不留一条
            # before_exists=1 却无快照的"假撤销"条目。
            try:
                self._undo_service.record_file_write(
                    conversation_id=call.conversation_id,
                    target=change.relative,
                    workspace_root=str(binding.root),
                    before_content=before_text,
                    before_exists=change.kind == "modified",
                    after_hash=after_hash,
                    workspace_id=workspace_id,
                    run_id=run_id,
                )
                undo_recorded = True
            except Exception:  # noqa: BLE001
                logger.debug("Shell undo write record failed", exc_info=True)
        self._log_change(call, binding, "shell_file_write", change.relative, after_hash)
        self._record_ledger(
            run_id,
            change.relative,
            "file_write",
            sha256_text(before_text) if before_text is not None else None,
            after_hash,
        )
        return undo_recorded

    def _checkpoint_text(self, call: ToolCall, binding, relative: str) -> Optional[str]:
        coordinator = self._checkpoint_coordinator
        if coordinator is None:
            return None
        reader = getattr(coordinator, "read_checkpoint_text", None)
        if reader is None:
            return None
        run_id = call.response_variant_id or call.id
        try:
            return reader(
                run_id=run_id,
                workspace_root=str(binding.root),
                relative=relative,
            )
        except Exception:  # noqa: BLE001
            return None

    def _log_change(
        self,
        call: ToolCall,
        binding,
        operation: str,
        relative: str,
        after_hash: Optional[str],
    ) -> None:
        try:
            self._effect_log.append(
                conversation_id=call.conversation_id,
                workspace_id=binding.workspace_id,
                workspace_root=str(binding.root),
                operation=operation,
                detail=relative,
                receipt=EffectReceipt(
                    kind="file_write" if operation == "shell_file_write" else "file_delete",
                    path=str(Path(binding.root).expanduser() / relative),
                    sha256=after_hash or "",
                    executed_at=_now_iso(),
                ),
            )
        except ToolError:
            # 审计失败已在 _finish 的 receipt 里标记 unknown_outcome；这里不抛。
            logger.debug("Shell reconcile effect log failed", exc_info=True)

    def _record_ledger(
        self,
        run_id: str,
        relative: str,
        operation: str,
        before_hash: Optional[str],
        after_hash: Optional[str],
    ) -> None:
        coordinator = self._checkpoint_coordinator
        if coordinator is None:
            return
        try:
            coordinator.record_effect(
                run_id=run_id,
                effect_id=(
                    f"shell_{operation}:{relative}:{run_id}:"
                    f"{(after_hash or before_hash or 'x')[:16]}"
                ),
                path=relative,
                operation=operation,
                before_hash=before_hash,
                after_hash=after_hash,
            )
        except Exception:  # noqa: BLE001
            logger.debug("Shell reconcile ledger record failed", exc_info=True)

    def _ensure_checkpoint(self, call: ToolCall, binding) -> None:
        coordinator = self._checkpoint_coordinator
        if coordinator is None:
            return
        run_id = call.response_variant_id or call.id
        try:
            coordinator.ensure_checkpoint(
                run_id=run_id,
                workspace_root=str(binding.root),
            )
        except Exception:
            # Snapshot is best-effort; never block the user's command.
            return


def _monotonic() -> float:
    import time

    return time.monotonic()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
