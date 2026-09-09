"""S4 内嵌终端端点：REST 生命周期 + WebSocket 双向通道（真实 PTY）。"""

from __future__ import annotations

import os
import pty
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider


def _pty_available() -> bool:
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        return False
    os.close(master_fd)
    os.close(slave_fd)
    return True


PTY_AVAILABLE = _pty_available()


@unittest.skipUnless(PTY_AVAILABLE, "host cannot allocate a PTY (/dev/ptmx denied)")
class TerminalApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._tmp.name) / "terminal-api.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
            ),
            provider=FakeProvider(chunks=("ok",)),
        )
        self._client = TestClient(app)
        self._client.__enter__()
        created = self._client.post(
            "/workspaces", json={"name": "终端区", "rootPath": str(self.root)}
        )
        self.workspace_id = created.json()["workspace"]["id"]

    def tearDown(self) -> None:
        self._client.__exit__(None, None, None)
        self._tmp.cleanup()

    def _create(self, **body) -> dict:
        response = self._client.post(
            f"/workspaces/{self.workspace_id}/terminals", json=body or None
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()["terminal"]

    def test_create_list_close_lifecycle(self) -> None:
        terminal = self._create(name="main", rows=30, cols=100)

        self.assertEqual("running", terminal["status"]["kind"])
        self.assertEqual(30, terminal["rows"])
        self.assertGreater(terminal["pid"], 0)

        listed = self._client.get(f"/workspaces/{self.workspace_id}/terminals")
        self.assertEqual(1, len(listed.json()["items"]))
        self.assertEqual(terminal["sessionId"], listed.json()["items"][0]["sessionId"])

        closed = self._client.delete(
            f"/workspaces/{self.workspace_id}/terminals/{terminal['sessionId']}"
        )
        self.assertTrue(closed.json()["closed"])
        again = self._client.delete(
            f"/workspaces/{self.workspace_id}/terminals/{terminal['sessionId']}"
        )
        self.assertFalse(again.json()["closed"])
        self.assertEqual(
            [], self._client.get(f"/workspaces/{self.workspace_id}/terminals").json()["items"]
        )

    def test_unknown_workspace_is_rejected(self) -> None:
        response = self._client.post("/workspaces/missing/terminals", json=None)
        self.assertEqual(404, response.status_code)

    def test_per_workspace_limit_is_reported(self) -> None:
        for _ in range(3):
            self._create()

        response = self._client.post(
            f"/workspaces/{self.workspace_id}/terminals", json=None
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual("terminal_create_failed", response.json()["error"]["code"])

    def test_websocket_streams_output_and_accepts_input(self) -> None:
        terminal = self._create()
        session_id = terminal["sessionId"]

        with self._client.websocket_connect(
            f"/workspaces/{self.workspace_id}/terminals/{session_id}"
        ) as socket:
            ready = socket.receive_json()
            self.assertEqual("ready", ready["type"])
            self.assertEqual(session_id, ready["terminal"]["sessionId"])

            socket.send_json({"type": "input", "data": "for f in ws-one ws-two; do echo socket-$f; done\n"})
            output = ""
            for _ in range(40):
                frame = socket.receive_json()
                if frame["type"] == "output":
                    output += frame["data"]
                if "socket-ws-two" in output:
                    break
            self.assertIn("socket-ws-two", output)

            socket.send_json({"type": "resize", "rows": 30, "cols": 90})
            socket.send_json({"type": "ping"})
            seen_pong = False
            for _ in range(10):
                frame = socket.receive_json()
                if frame["type"] == "pong":
                    seen_pong = True
                    break
            self.assertTrue(seen_pong)

    def test_websocket_unknown_session_closes_with_error(self) -> None:
        with self._client.websocket_connect(
            f"/workspaces/{self.workspace_id}/terminals/term_missing"
        ) as socket:
            frame = socket.receive_json()
            self.assertEqual("error", frame["type"])


if __name__ == "__main__":
    unittest.main()
