from __future__ import annotations

import asyncio
import contextvars
import unittest

from endless_task.runtime.background_tasks import BackgroundTaskSupervisor


class BackgroundTaskSupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_keeps_task_until_drain(self) -> None:
        supervisor = BackgroundTaskSupervisor(name="test")
        released = asyncio.Event()

        async def run() -> None:
            await released.wait()

        supervisor.spawn(run(), name="held-task")
        self.assertEqual(1, supervisor.running_count())

        released.set()
        await supervisor.drain()

        self.assertEqual(0, supervisor.running_count())
        self.assertEqual((), supervisor.tasks)

    async def test_spawn_copies_current_context(self) -> None:
        request_id = contextvars.ContextVar("request_id", default="missing")
        supervisor = BackgroundTaskSupervisor(name="test")
        observed: list[str] = []

        async def run() -> None:
            observed.append(request_id.get())

        request_id.set("req_123")
        supervisor.spawn(run(), name="context-task")
        await supervisor.drain()

        self.assertEqual(["req_123"], observed)

    async def test_failed_task_is_logged_and_discarded(self) -> None:
        supervisor = BackgroundTaskSupervisor(name="test")

        async def fail() -> None:
            raise RuntimeError("boom")

        with self.assertLogs(
            "endless_task.runtime.background_tasks",
            level="ERROR",
        ) as logs:
            supervisor.spawn(fail(), name="failing-task")
            await supervisor.drain()

        self.assertEqual((), supervisor.tasks)
        self.assertTrue(
            any("Background task failed" in message for message in logs.output)
        )

    async def test_shutdown_cancels_and_drains_tasks(self) -> None:
        supervisor = BackgroundTaskSupervisor(name="test")
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        supervisor.spawn(wait_forever(), name="blocked-task")
        await asyncio.wait_for(started.wait(), timeout=1)

        await supervisor.shutdown(cancel=True)

        self.assertTrue(cancelled.is_set())
        self.assertEqual((), supervisor.tasks)
        self.assertEqual(0, supervisor.running_count())


if __name__ == "__main__":
    unittest.main()