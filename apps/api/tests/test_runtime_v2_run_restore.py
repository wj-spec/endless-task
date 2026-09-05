"""M3B slice B: RunFailureAutoRestoreObserver decision tests.

The observer must restore ONLY terminal FAILED runs that actually have
workspace checkpoints, with the guarded semantics intact and everything
else a strict no-op:

- COMPLETED / CANCELLED (user stop) never restore.
- FAILED without checkpoints is a no-op.
- FAILED with excluded error codes is a no-op.
- FAILED with checkpoints restores every workspace of the run.
- Observer failures are swallowed (fail-open, executor contract).
"""

from __future__ import annotations

import asyncio
import unittest

from endless_task.runtime_v2.run_restore import (
    RunFailureAutoRestoreObserver,
    TerminalRunView,
)


class _FakeCoordinator:
    """Records restore_run calls; optional failure injection."""

    def __init__(self, *, workspaces: tuple[str, ...] = ()) -> None:
        self._workspaces = workspaces
        self.called: list[str] = []
        self.fail = False

    def workspace_roots_for(self, *, run_id: str) -> tuple[str, ...]:
        return self._workspaces

    def restore_run(self, *, run_id: str):
        if self.fail:
            raise RuntimeError("restore backend down")
        self.called.append(run_id)
        return tuple(
            _FakeOutcome(run_id, ws, ("a.txt",), ("user.txt",))
            for ws in self._workspaces
        )


class _FakeOutcome:
    def __init__(self, run_id: str, workspace_root: str, restored, skipped) -> None:
        self.run_id = run_id
        self.workspace_root = workspace_root
        self.restored = tuple(restored)
        self.skipped = tuple(skipped)


class RunFailureAutoRestoreObserverTest(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self, coordinator, *, status: str, error_code: str | None = None
    ) -> None:
        reader_calls: list[str] = []

        def reader(run_id: str) -> TerminalRunView:
            reader_calls.append(run_id)
            return TerminalRunView(run_id=run_id, status=status, error_code=error_code)

        observer = RunFailureAutoRestoreObserver(
            run_reader=reader,
            coordinator=coordinator,
        )
        await observer.on_run_terminal("run_1")
        return coordinator.called, reader_calls

    async def test_failed_run_with_checkpoints_restores_every_workspace(
        self,
    ) -> None:
        coordinator = _FakeCoordinator(workspaces=("/ws/one", "/ws/two"))
        called, _ = await self._run(coordinator, status="failed")
        self.assertEqual(["run_1"], called)

    async def test_completed_never_restores(self) -> None:
        coordinator = _FakeCoordinator(workspaces=("/ws/one",))
        called, _ = await self._run(coordinator, status="completed")
        self.assertEqual([], called)

    async def test_cancelled_user_stop_never_restores(self) -> None:
        coordinator = _FakeCoordinator(workspaces=("/ws/one",))
        called, _ = await self._run(coordinator, status="cancelled")
        self.assertEqual([], called)

    async def test_failed_without_checkpoints_is_noop(self) -> None:
        coordinator = _FakeCoordinator()
        called, _ = await self._run(coordinator, status="failed")
        self.assertEqual([], called)

    async def test_failed_with_excluded_code_is_noop(self) -> None:
        coordinator = _FakeCoordinator(workspaces=("/ws/one",))
        reader_calls: list[str] = []

        def reader(run_id: str) -> TerminalRunView:
            reader_calls.append(run_id)
            return TerminalRunView(
                run_id=run_id, status="failed", error_code="agent_timeout"
            )

        observer = RunFailureAutoRestoreObserver(
            run_reader=reader,
            coordinator=coordinator,
            excluded_codes=("agent_timeout",),
        )
        await observer.on_run_terminal("run_1")
        self.assertEqual([], coordinator.called)

    async def test_observer_failure_is_swallowed(self) -> None:
        coordinator = _FakeCoordinator(workspaces=("/ws/one",))
        coordinator.fail = True
        called, _ = await self._run(coordinator, status="failed")
        self.assertEqual([], called)  # no raise propagates


if __name__ == "__main__":
    unittest.main()
