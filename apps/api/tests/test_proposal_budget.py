"""R5.7 全局提案预算：日上限、单会话冷却、静默时段。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.storage import Database
from endless_task.proposals.budget import ProposalBudget

TZ = timezone(timedelta(hours=8))


def make_clock(moment: datetime):
    return lambda: moment


class ProposalBudgetTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._dir.name) / "b.db")
        self.database.initialize()
        self.now = datetime(2026, 8, 27, 14, 0, tzinfo=TZ)

    def tearDown(self):
        self._dir.cleanup()

    def _budget(self, moment=None, **kwargs):
        defaults = dict(
            daily_limit=6,
            cooldown_minutes=30,
            quiet_start="23:00",
            quiet_end="07:00",
        )
        defaults.update(kwargs)
        return ProposalBudget(
            self.database, clock=make_clock(moment or self.now), **defaults
        )

    def _insert_proposal(self, table: str, conversation_id: str, created_at: str):
        row_id = f"p_{conversation_id}_{table}"
        with self.database.transaction() as connection:
            if table == "memory_proposals":
                connection.execute(
                    """
                    INSERT INTO memory_proposals (
                        id, conversation_id, turn_id, kind, content, reason,
                        status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'preference', '内容', '理由', 'pending', ?, ?)
                    """,
                    (row_id, conversation_id, "turn_x", created_at, created_at),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO knowledge_proposals (
                        id, proposal_type, payload, status,
                        conversation_id, turn_id, created_at, updated_at
                    )
                    VALUES (?, 'add_source', '{}', 'pending', ?, ?, ?, ?)
                    """,
                    (row_id, conversation_id, "turn_x", created_at, created_at),
                )

    def test_allows_when_empty(self):
        self.assertTrue(self._budget().allow("conv_1"))

    def test_daily_limit_counts_both_tables(self):
        budget = self._budget(daily_limit=2)
        self._insert_proposal(
            "memory_proposals", "conv_1", "2026-08-27T05:00:00.000Z"
        )
        self._insert_proposal(
            "knowledge_proposals", "conv_2", "2026-08-27T05:30:00.000Z"
        )
        self.assertFalse(budget.allow("conv_3"))

    def test_daily_limit_resets_next_local_day(self):
        budget = self._budget(daily_limit=1)
        # 本地 8 点 = UTC 0 点；昨天本地日的提案不影响今天。
        self._insert_proposal(
            "memory_proposals", "conv_1", "2026-08-26T02:00:00.000Z"
        )
        self.assertTrue(budget.allow("conv_2"))

    def test_cooldown_blocks_same_conversation(self):
        budget = self._budget(cooldown_minutes=30)
        recent = (self.now - timedelta(minutes=10)).astimezone(timezone.utc)
        self._insert_proposal(
            "memory_proposals",
            "conv_1",
            recent.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        self.assertFalse(budget.allow("conv_1"))
        self.assertTrue(budget.allow("conv_2"))

    def test_cooldown_expires(self):
        budget = self._budget(cooldown_minutes=30)
        old = (self.now - timedelta(minutes=31)).astimezone(timezone.utc)
        self._insert_proposal(
            "knowledge_proposals",
            "conv_1",
            old.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        self.assertTrue(budget.allow("conv_1"))

    def test_quiet_hours_block_overnight_window(self):
        quiet = datetime(2026, 8, 27, 23, 30, tzinfo=TZ)
        self.assertFalse(self._budget(moment=quiet).allow("conv_1"))
        early = datetime(2026, 8, 27, 6, 59, tzinfo=TZ)
        self.assertFalse(self._budget(moment=early).allow("conv_1"))
        after = datetime(2026, 8, 27, 7, 0, tzinfo=TZ)
        self.assertTrue(self._budget(moment=after).allow("conv_1"))

    def test_quiet_hours_disabled_when_blank(self):
        quiet = datetime(2026, 8, 27, 23, 30, tzinfo=TZ)
        budget = self._budget(moment=quiet, quiet_start="", quiet_end="")
        self.assertTrue(budget.allow("conv_1"))

    def test_zero_budget_disables(self):
        self.assertFalse(self._budget(daily_limit=0).allow("conv_1"))


class BudgetWiringTest(unittest.IsolatedAsyncioTestCase):
    """on_turn_completed 接线：预算耗尽时记忆/知识提取均被跳过。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self._dir.name) / "api.db"

    def tearDown(self):
        self._dir.cleanup()

    async def test_proposals_skipped_when_budget_exhausted(self):
        import httpx

        from endless_task.api import AppSettings, create_app
        from endless_task.runtime import ProviderCompleted, ProviderTextDelta
        from tests.fixtures.v2_client import send_message, wait_for_run_terminal

        class RecordingProvider:
            name = "recording"

            def __init__(self):
                self.requests = []

            async def stream(self, request, cancellation_token):
                self.requests.append(request)
                yield ProviderTextDelta(text='{"proposals":[]}')
                yield ProviderCompleted()

        provider = RecordingProvider()
        app = create_app(
            settings=AppSettings(
                database_path=self.database_path,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                proposal_daily_budget=0,  # 预算归零：记忆/知识提取全部静默跳过
            ),
            provider=provider,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        try:
            container = app.state.container
            conversation = await create_bound_conversation(client)
            handle = await send_message(
                client,
                conversation["id"],
                "记住，我喜欢喝绿茶，以后都按这个来。",
                idempotency_key="budget-1",
            )
            await wait_for_run_terminal(container, handle["runId"])
            # 预算归零：不应触发任何记忆/知识提取调用（主回合回答调用除外）。
            extraction_calls = [
                request
                for request in provider.requests
                if request.request_id.startswith(("memp_", "knwp_"))
            ]
            self.assertEqual(extraction_calls, [])
            proposals = (
                await client.get(
                    f"/conversations/{conversation['id']}/memory-proposals"
                )
            ).json()["items"]
            self.assertEqual(proposals, [])
        finally:
            await client.aclose()
            await lifespan.__aexit__(None, None, None)


import asyncio  # noqa: E402
from tests.fixtures.workspace_client import create_bound_conversation

if __name__ == "__main__":
    unittest.main()