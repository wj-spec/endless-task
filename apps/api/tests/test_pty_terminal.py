"""S4 内嵌终端：PTY 会话与终端服务（真实进程，不 mock）。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

def _pty_available() -> bool:
    """宿主是否允许分配 PTY（受限沙箱/容器里可能被拒）。"""
    import pty as _pty

    try:
        master_fd, slave_fd = _pty.openpty()
    except OSError:
        return False
    os.close(master_fd)
    os.close(slave_fd)
    return True


PTY_AVAILABLE = _pty_available()


@unittest.skipUnless(PTY_AVAILABLE, "host cannot allocate a PTY (/dev/ptmx denied)")
class _PtyBoundTestCase(unittest.IsolatedAsyncioTestCase):
    pass


from endless_task.workspace_runtime.terminal import (
    PtySession,
    TerminalService,
    _IncrementalUtf8,
    _utf8_tail,
)


class Utf8HelpersTest(unittest.TestCase):
    def test_incremental_decode_keeps_split_characters(self) -> None:
        decoder = _IncrementalUtf8()
        raw = "中文".encode("utf-8")
        self.assertEqual("", decoder.decode(raw[:2]))
        self.assertEqual("中文", decoder.decode(raw[2:]))

    def test_utf8_tail_never_splits_characters(self) -> None:
        text = "abc中文"
        tail = _utf8_tail(text, 6)
        self.assertEqual("中文", tail)
        self.assertEqual("abc中文", _utf8_tail(text, 100))


class PtySessionTest(_PtyBoundTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _session(self, **kwargs) -> PtySession:
        session = PtySession(workspace_id="ws_1", cwd=self.root, **kwargs)
        await session.start()
        self.addAsyncCleanup(session.close)
        return session

    async def _wait_for(self, predicate, *, timeout: float = 20.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition not reached in time")

    async def test_command_output_is_captured_and_readable(self) -> None:
        session = await self._session()

        # 输出串不出现在命令行里，避免把 PTY 回显当成命令结果。
        session.write("for f in alpha beta; do echo result-$f; done\n")

        await self._wait_for(lambda: "result-beta" in session.read(count=200).text)
        self.assertEqual("running", session.status.kind)
        self.assertGreater(session.snapshot().pid, 0)
        # 启动后应出现提示符（含 PROMPT_COMMAND marker）。
        self.assertIn("]133;D;", session.read(count=200).text)

    async def test_read_pages_from_newest_with_bounds(self) -> None:
        session = await self._session()

        session.write("for i in 1 2 3 4; do echo page-$i; done\n")
        await self._wait_for(lambda: "page-4" in session.read(count=200).text)

        # 末尾还有一行提示符，所以取最近 3 行正好覆盖 page-3/page-4。
        page = session.read(offset=0, count=3)
        self.assertIn("page-4", page.text)
        self.assertIn("page-3", page.text)
        self.assertNotIn("page-2", page.text)
        self.assertGreaterEqual(page.total_lines, 4)
        self.assertFalse(page.truncated)

    async def test_scrollback_is_bounded(self) -> None:
        session = await self._session(scrollback_lines=3, scrollback_bytes=10_000)

        session.write("for i in {1..40}; do echo bound-$i; done\n")
        await self._wait_for(lambda: "bound-40" in session.read(count=200).text)

        text = session.read(offset=0, count=100).text
        self.assertLessEqual(len(text.split("\n")), 5)
        self.assertIn("bound-40", text)

    async def test_resize_and_signal_reach_the_foreground_process(self) -> None:
        session = await self._session()

        session.resize(24, 80)
        session.write("stty size\n")
        await self._wait_for(lambda: "24 80" in session.read(count=200).text)

        session.write("sleep 30\n")
        await asyncio.sleep(0.5)
        self.assertTrue(session.signal("SIGINT"))
        await self._wait_for(
            lambda: "]133;D;" in session.read(count=20).text
        )
        self.assertEqual("running", session.status.kind)

    async def test_exit_is_reported_and_listeners_notified(self) -> None:
        session = await self._session()
        exits: list[object] = []
        session.add_exit_listener(exits.append)

        session.write("exit 7\n")
        status = await asyncio.wait_for(session.wait_closed(), timeout=20.0)

        self.assertEqual("exited", status.kind)
        self.assertEqual(7, status.exit_code)
        self.assertTrue(exits)
        self.assertFalse(session.alive)

    async def test_output_listener_receives_raw_text(self) -> None:
        session = await self._session()
        chunks: list[str] = []
        session.add_listener(chunks.append)

        session.write("printf 'streamed\\n'\n")
        await self._wait_for(lambda: any("streamed" in chunk for chunk in chunks))

    async def test_missing_cwd_is_rejected(self) -> None:
        session = PtySession(workspace_id="ws_1", cwd=self.root / "nope")
        with self.assertRaises(ValueError):
            await session.start()


class TerminalServiceTest(_PtyBoundTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.service = TerminalService(
            max_sessions_per_workspace=2,
            idle_timeout_seconds=60.0,
            reap_interval_seconds=0.05,
        )
        self.addAsyncCleanup(self.service.close_all)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_create_list_get_and_close(self) -> None:
        session = await self.service.create(workspace_id="ws_1", cwd=self.root)

        self.assertEqual(1, len(self.service.list_for_workspace("ws_1")))
        self.assertIs(
            session, self.service.get(workspace_id="ws_1", session_id=session.session_id)
        )
        with self.assertRaises(KeyError):
            self.service.get(workspace_id="ws_2", session_id=session.session_id)

        self.assertTrue(
            await self.service.close(
                workspace_id="ws_1", session_id=session.session_id
            )
        )
        self.assertEqual((), self.service.list_for_workspace("ws_1"))
        self.assertFalse(
            await self.service.close(
                workspace_id="ws_1", session_id=session.session_id
            )
        )

    async def test_per_workspace_limit(self) -> None:
        await self.service.create(workspace_id="ws_1", cwd=self.root)
        await self.service.create(workspace_id="ws_1", cwd=self.root)

        with self.assertRaises(ValueError):
            await self.service.create(workspace_id="ws_1", cwd=self.root)

        other = await self.service.create(workspace_id="ws_2", cwd=self.root)
        self.assertEqual(1, len(self.service.list_for_workspace("ws_2")))
        await self.service.close(
            workspace_id="ws_2", session_id=other.session_id
        )

    async def test_idle_sessions_are_reaped(self) -> None:
        service = TerminalService(
            max_sessions_per_workspace=2,
            idle_timeout_seconds=0.1,
            reap_interval_seconds=0.05,
        )
        session = await service.create(workspace_id="ws_idle", cwd=self.root)

        deadline = asyncio.get_running_loop().time() + 20.0
        while asyncio.get_running_loop().time() < deadline:
            if not service.list_for_workspace("ws_idle"):
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("idle session was not reaped")
        self.assertFalse(session.alive)
        await service.close_all()

    async def test_close_all_terminates_sessions(self) -> None:
        first = await self.service.create(workspace_id="ws_1", cwd=self.root)
        second = await self.service.create(workspace_id="ws_2", cwd=self.root)

        await self.service.close_all()

        self.assertFalse(first.alive)
        self.assertFalse(second.alive)
        self.assertEqual((), self.service.list_for_workspace("ws_1"))


if __name__ == "__main__":
    unittest.main()
