"""M6 W6-1: terminal-run trace observer bridging v2 journal into ledger."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
from endless_task.runtime_v2.trace_observer import (
    LedgerTraceObserver,
    ModelTurnUsageView,
    TerminalRunView,
    default_run_reader,
    default_usage_exists,
)
from endless_task.storage import Database


class _FakeLedger:
    """RuntimeLedger stub capturing usage rows (no persistence)."""

    def __init__(self) -> None:
        self.usage_rows: list[tuple] = []
        self.recorded: list[dict] = []

    async def record_usage(self, usage, *, cost=None) -> None:
        self.usage_rows.append(usage)
        self.recorded.append(
            {
                "provider": usage.provider,
                "model": usage.model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": cost.cost_usd if cost is not None else None,
                "price_revision": (
                    cost.price_revision if cost is not None else None
                ),
            }
        )

    def usage_for_run(self, run_id: str) -> tuple:
        return tuple(row for row in self.usage_rows if row.trace.run_id == run_id)


class _FakeRunRecord:
    def __init__(self, run_id: str, status: str, correlation_id: str) -> None:
        self.id = run_id
        self.status = _Status(status)
        self.correlation_id = correlation_id


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeTurn:
    def __init__(
        self,
        *,
        turn_id: str,
        status: str = "completed",
        provider: str = "deepseek",
        model: str = "deepseek-chat",
        input_tokens: int | None = 100,
        output_tokens: int | None = 50,
        finished_at: str | None = "2026-09-05T00:00:01Z",
        started_at: str | None = "2026-09-05T00:00:00Z",
    ) -> None:
        self.id = turn_id
        self.status = _Status(status)
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.finished_at = finished_at
        self.started_at = started_at


class _FakeRepository:
    def __init__(self, run: _FakeRunRecord, turns: list) -> None:
        self._run = run
        self._turns = turns

    def get_run(self, run_id: str) -> _FakeRunRecord:
        return self._run

    def list_model_turns(self, run_id: str) -> list:
        return self._turns


class ObserverUnitTest(unittest.TestCase):
    def test_mirrors_usage_with_catalog_cost(self) -> None:
        import asyncio

        ledger = _FakeLedger()
        observer = LedgerTraceObserver(
            ledger,
            catalog=make_default_catalog(),
            run_reader=default_run_reader(
                _FakeRepository(
                    _FakeRunRecord("run_1", "completed", "corr_1"),
                    [
                        _FakeTurn(turn_id="turn_1"),
                        _FakeTurn(turn_id="turn_2"),
                    ],
                )
            ),
            usage_exists=default_usage_exists(ledger),
        )
        asyncio.run(observer.on_run_terminal("run_1"))

        self.assertEqual(2, len(ledger.usage_rows))
        first = ledger.recorded[0]
        self.assertEqual("deepseek", first["provider"])
        self.assertEqual("deepseek-chat", first["model"])
        self.assertEqual(100, first["input_tokens"])
        self.assertEqual(50, first["output_tokens"])
        # Catalog pricing applied: known model yields a finite cost + revision.
        self.assertIsNotNone(first["cost_usd"])
        self.assertIsNotNone(first["price_revision"])
        # Usage carries run/turn trace linkage.
        self.assertEqual("run_1", ledger.usage_rows[0].trace.run_id)
        self.assertEqual("turn_1", ledger.usage_rows[0].trace.span_id)

    def test_idempotent_when_usage_already_mirrored(self) -> None:
        import asyncio

        ledger = _FakeLedger()
        observer = LedgerTraceObserver(
            ledger,
            run_reader=default_run_reader(
                _FakeRepository(
                    _FakeRunRecord("run_1", "completed", "corr_1"),
                    [_FakeTurn(turn_id="turn_1")],
                )
            ),
            usage_exists=default_usage_exists(ledger),
        )
        asyncio.run(observer.on_run_terminal("run_1"))
        asyncio.run(observer.on_run_terminal("run_1"))
        self.assertEqual(1, len(ledger.usage_rows))

    def test_skips_non_terminal_run(self) -> None:
        import asyncio

        ledger = _FakeLedger()
        observer = LedgerTraceObserver(
            ledger,
            run_reader=default_run_reader(
                _FakeRepository(
                    _FakeRunRecord("run_1", "running", "corr_1"),
                    [_FakeTurn(turn_id="turn_1")],
                )
            ),
            usage_exists=default_usage_exists(ledger),
        )
        asyncio.run(observer.on_run_terminal("run_1"))
        self.assertEqual(0, len(ledger.usage_rows))

    def test_skips_incomplete_turn_usage(self) -> None:
        import asyncio

        ledger = _FakeLedger()
        observer = LedgerTraceObserver(
            ledger,
            run_reader=default_run_reader(
                _FakeRepository(
                    _FakeRunRecord("run_1", "failed", "corr_1"),
                    [
                        _FakeTurn(turn_id="turn_ok"),
                        # failed turn with tokens still unset: no usage row
                        _FakeTurn(
                            turn_id="turn_failed",
                            status="failed",
                            input_tokens=None,
                            output_tokens=None,
                        ),
                    ],
                )
            ),
            usage_exists=default_usage_exists(ledger),
        )
        asyncio.run(observer.on_run_terminal("run_1"))
        self.assertEqual(1, len(ledger.usage_rows))
        self.assertEqual("turn_ok", ledger.usage_rows[0].trace.span_id)

    def test_observer_never_raises_into_caller(self) -> None:
        import asyncio

        class ExplodingLedger:
            async def record_usage(self, usage, *, cost=None) -> None:
                raise RuntimeError("sink down")

        observer = LedgerTraceObserver(
            ExplodingLedger(),
            run_reader=lambda run_id: TerminalRunView(
                run_id=run_id,
                correlation_id="corr_1",
                status="completed",
                turns=(ModelTurnUsageView("t1", "p", "m", 1, 1, "at"),),
            ),
            usage_exists=lambda run_id: False,
        )
        # on_run_terminal swallows the sink error and returns cleanly.
        asyncio.run(observer.on_run_terminal("run_1"))

    def test_invalid_construction_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LedgerTraceObserver(_FakeLedger(), catalog="not-a-catalog")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            LedgerTraceObserver(_FakeLedger(), run_reader="not-callable")  # type: ignore[arg-type]

    def test_default_run_reader_requires_completed_turns(self) -> None:
        reader = default_run_reader(
            _FakeRepository(
                _FakeRunRecord("run_1", "completed", "corr_1"),
                [
                    _FakeTurn(turn_id="a", status="completed", input_tokens=1, output_tokens=1),
                    _FakeTurn(turn_id="b", status="running", input_tokens=5, output_tokens=5),
                ],
            )
        )
        view = reader("run_1")
        self.assertEqual("completed", view.status)
        self.assertEqual(1, len(view.turns))
        self.assertEqual("a", view.turns[0].model_turn_id)


class ObserverSqliteTest(unittest.TestCase):
    """End-to-end: observer over a real sqlite ledger + v2 repository."""

    def setUp(self) -> None:
        import asyncio

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run_repository_run(self) -> None:
        # Reuse the ledger's own database to build a minimal fake repo whose
        # records mirror what a real v2 run would persist; the observer only
        # needs get_run/list_model_turns shapes (already unit-tested above).
        self.fake_repository = _FakeRepository(
            _FakeRunRecord("run_1", "completed", "corr_1"),
            [
                _FakeTurn(
                    turn_id="turn_1",
                    provider="deepseek",
                    model="deepseek-chat",
                    input_tokens=120,
                    output_tokens=60,
                ),
            ],
        )

    def test_observer_persists_usage_rows_into_sqlite_ledger(self) -> None:
        import asyncio

        self._run_repository_run()
        observer = LedgerTraceObserver(
            self.ledger,
            catalog=make_default_catalog(),
            run_reader=default_run_reader(self.fake_repository),
            usage_exists=default_usage_exists(self.ledger),
        )
        asyncio.run(observer.on_run_terminal("run_1"))

        rows = self.ledger.usage_for_run("run_1")
        self.assertEqual(1, len(rows))
        self.assertEqual("deepseek", rows[0].provider)
        self.assertEqual(120, rows[0].input_tokens)
        self.assertEqual(60, rows[0].output_tokens)
        self.assertIsNotNone(rows[0].price_revision)
        self.assertIsNotNone(rows[0].cost_usd)

        # Idempotency: second notification adds no rows.
        asyncio.run(observer.on_run_terminal("run_1"))
        self.assertEqual(1, len(self.ledger.usage_for_run("run_1")))


if __name__ == "__main__":
    unittest.main()


class ExecutorSeamIntegrationTest(unittest.TestCase):
    """A real AgentRunExecutor run with trace_observer mirrors usage."""

    def setUp(self) -> None:
        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime.provider import (
            ProviderCompleted,
            ProviderTextDelta,
        )
        from endless_task.runtime_v2 import (
            Actor,
            AgentRunExecutor,
            TranscriptEntryType,
        )
        from endless_task.storage import SqliteChatRepository, SqliteRuntimeV2Repository

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self._CancellationToken = CancellationToken
        self._ProviderCompleted = ProviderCompleted
        self._ProviderTextDelta = ProviderTextDelta
        self._Actor = Actor
        self._AgentRunExecutor = AgentRunExecutor
        self._TranscriptEntryType = TranscriptEntryType

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=self._TranscriptEntryType.USER_MESSAGE,
            actor=self._Actor.USER,
            payload={"content": "你好"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation, lane, trigger, run

    def _completed_provider(self):
        class OneShot:
            name = "scripted"

            def __init__(self, events) -> None:
                self._events = events
                self.requests = []

            async def stream(self, request, cancellation_token):
                self.requests.append(request)
                for event in self._events:
                    cancellation_token.raise_if_cancelled()
                    yield event

        return OneShot(
            [
                self._ProviderTextDelta("你好！"),
                self._ProviderCompleted(
                    finish_reason="stop",
                    input_tokens=42,
                    output_tokens=7,
                ),
            ]
        )

    def test_completed_run_mirrors_usage(self) -> None:
        import asyncio

        from endless_task.tooling import ToolRegistry

        _, lane, trigger, run = self._create_run()
        provider = self._completed_provider()
        observer = LedgerTraceObserver(
            self.ledger,
            catalog=make_default_catalog(),
            run_reader=default_run_reader(self.repository),
            usage_exists=default_usage_exists(self.ledger),
        )
        executor = self._AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            trace_observer=observer,
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=self._CancellationToken())
        )
        self.assertEqual("completed", result.status.value)
        rows = self.ledger.usage_for_run(run.id)
        self.assertEqual(1, len(rows))
        self.assertEqual(42, rows[0].input_tokens)
        self.assertEqual(7, rows[0].output_tokens)
        self.assertEqual("scripted", rows[0].provider)

    def test_executor_without_observer_unchanged(self) -> None:
        import asyncio

        from endless_task.tooling import ToolRegistry

        _, lane, trigger, run = self._create_run()
        provider = self._completed_provider()
        executor = self._AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=self._CancellationToken())
        )
        self.assertEqual("completed", result.status.value)
        self.assertEqual((), self.ledger.usage_for_run(run.id))


class SpanRecorderTest(unittest.TestCase):
    """M6 W6-7: executor records run + model-turn spans into a recorder."""

    def setUp(self) -> None:
        import tempfile

        from endless_task.storage import SqliteChatRepository, SqliteRuntimeV2Repository

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self):
        from endless_task.runtime_v2 import Actor, TranscriptEntryType

        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "你好"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation, lane, trigger, run

    def _provider(self):
        from endless_task.runtime.provider import ProviderCompleted, ProviderTextDelta

        class OneShot:
            name = "scripted"

            def __init__(self) -> None:
                self.requests = []

            async def stream(self, request, cancellation_token):
                self.requests.append(request)
                for event in (
                    ProviderTextDelta("你好！"),
                    ProviderCompleted(
                        finish_reason="stop", input_tokens=10, output_tokens=5
                    ),
                ):
                    cancellation_token.raise_if_cancelled()
                    yield event

        return OneShot()

    def test_executor_records_run_and_turn_spans(self) -> None:
        import asyncio

        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime_v2 import AgentRunExecutor
        from endless_task.tooling import ToolRegistry

        _, lane, trigger, run = self._create_run()
        ledger = SqliteRuntimeLedger(self.database)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=self._provider(),
            tool_registry=ToolRegistry(),
            model="scripted-model",
            span_recorder=ledger,
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        self.assertEqual("completed", result.status.value)
        spans = ledger.spans_for_run(run.id)
        # one run span + one model turn span
        self.assertGreaterEqual(len(spans), 2)
        kinds = [span.kind for span in spans]
        self.assertIn("internal", kinds)
        self.assertIn("model", kinds)
        for span in spans:
            self.assertEqual("completed", span.status)
            self.assertIsNotNone(span.ended_at)
        model_spans = [s for s in spans if s.kind == "model"]
        self.assertEqual(1, len(model_spans))
        self.assertEqual("scripted", model_spans[0].attributes.get("provider"))

    def test_executor_without_recorder_unchanged(self) -> None:
        import asyncio

        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime_v2 import AgentRunExecutor
        from endless_task.tooling import ToolRegistry

        _, lane, trigger, run = self._create_run()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=self._provider(),
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        self.assertEqual("completed", result.status.value)
        self.assertEqual((), SqliteRuntimeLedger(self.database).spans_for_run(run.id))

    def test_failed_turn_closes_span_failed(self) -> None:
        import asyncio

        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime.provider import (
            ProviderError,
            ProviderTextDelta,
        )
        from endless_task.runtime_v2 import AgentRunExecutor
        from endless_task.tooling import ToolRegistry

        class Failing:
            name = "failing"

            async def stream(self, request, cancellation_token):
                yield ProviderTextDelta("partial")
                raise ProviderError("provider_timeout", "超时", retryable=False)

        _, lane, trigger, run = self._create_run()
        ledger = SqliteRuntimeLedger(self.database)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=Failing(),
            tool_registry=ToolRegistry(),
            model="m",
            span_recorder=ledger,
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        self.assertEqual("failed", result.status.value)
        spans = ledger.spans_for_run(run.id)
        statuses = [span.status for span in spans]
        self.assertIn("failed", statuses)


class EffectReceiptLedgerTest(unittest.TestCase):
    """RS-6 slice 2b: completed tool effects land in the runtime ledger."""

    def setUp(self) -> None:
        import tempfile

        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime.provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
        from endless_task.runtime_v2 import Actor, AgentRunExecutor, TranscriptEntryType
        from endless_task.storage import SqliteChatRepository, SqliteRuntimeV2Repository

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self._temporary_directory.name) / "ws"
        self.workspace_root.mkdir()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self._CancellationToken = CancellationToken
        self._ProviderCompleted = ProviderCompleted
        self._ProviderTextDelta = ProviderTextDelta
        self._ProviderToolCall = ProviderToolCall
        self._Actor = Actor
        self._AgentRunExecutor = AgentRunExecutor
        self._TranscriptEntryType = TranscriptEntryType

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _run_with_provider(self, provider):
        from endless_task.runtime_v2 import Actor, TranscriptEntryType
        from endless_task.tooling import ToolRegistry
        from endless_task.workspace_runtime.effect_log import EffectLog
        from endless_task.workspace_runtime.fs_tools import WriteWorkspaceFileTool
        from endless_task.workspace_runtime.resolver import WorkspaceResolver

        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=self._TranscriptEntryType.USER_MESSAGE,
            actor=self._Actor.USER,
            payload={"content": "写文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        registry = ToolRegistry()

        class BindingResolver:
            def __init__(self, root):
                self._root = root

            def require_binding(self, conversation_id):
                from endless_task.workspace_runtime import WorkspaceBinding

                return WorkspaceBinding(
                    workspace_id="ws_1",
                    root=self._root,
                )

        effect_log_dir = Path(self._temporary_directory.name) / "logs"
        tool = WriteWorkspaceFileTool(
            BindingResolver(self.workspace_root),
            EffectLog(effect_log_dir),
        )
        registry.register(tool)
        executor = self._AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="m",
            span_recorder=self.ledger,
        )
        result = __import__("asyncio").run(
            executor.execute(run.id, cancellation_token=self._CancellationToken())
        )
        return run, result

    def test_completed_file_write_records_protocol_effect(self) -> None:
        run, result = self._run_with_provider(_WriteThenStopProvider())
        self.assertEqual("completed", result.status.value)
        # File actually written (tool side effect happened).
        self.assertTrue((self.workspace_root / "a.txt").exists())
        events = self.ledger.events_for_run(run.id)
        effect_events = [e for e in events if e.event_type == "effect_receipt"]
        self.assertGreaterEqual(len(effect_events), 1)
        data = effect_events[0].data
        self.assertEqual("call_write", data["tool_call_id"])
        self.assertEqual("committed", data["outcome"])
        self.assertTrue(effect_events[0].safety_critical)

    def test_no_effect_when_recorder_absent(self) -> None:
        # Executor WITHOUT span_recorder: no ledger effect events.
        from endless_task.tooling import ToolRegistry

        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=self._TranscriptEntryType.USER_MESSAGE,
            actor=self._Actor.USER,
            payload={"content": "写"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        executor = self._AgentRunExecutor(
            repository=self.repository,
            provider=_WriteThenStopProvider(),
            tool_registry=ToolRegistry(),
            model="m",
        )
        import asyncio

        asyncio.run(
            executor.execute(run.id, cancellation_token=self._CancellationToken())
        )
        effect_events = [
            e
            for e in self.ledger.events_for_run(run.id)
            if e.event_type == "effect_receipt"
        ]
        self.assertEqual([], effect_events)


class _WriteThenStopProvider:
    """Two-phase provider: tool-call turn then a stop turn."""

    name = "scripted"

    def __init__(self) -> None:
        self.turn = 0

    async def stream(self, request, cancellation_token):
        from endless_task.runtime.provider import (
            ProviderCompleted,
            ProviderTextDelta,
            ProviderToolCall,
        )

        self.turn += 1
        if self.turn == 1:
            yield ProviderTextDelta("写入文件。")
            yield ProviderToolCall(
                id="call_write",
                name="write_workspace_file",
                arguments={"path": "a.txt", "content": "hello"},
            )
            yield ProviderCompleted(
                finish_reason="tool_calls", input_tokens=5, output_tokens=2
            )
            return
        yield ProviderTextDelta("完成。")
        yield ProviderCompleted(
            finish_reason="stop", input_tokens=6, output_tokens=3
        )
