"""M3B slice E: crash-window startup reconciliation tests.

reconcile_failed_runs must restore exactly the FAILED runs whose terminal
auto-restore never ran (no run_auto_restored marker), newest first, guarded
and fail-open:

- marker present -> skip; no checkpoints -> skip; workspace dir gone -> skip;
- ordering newest first; one run's failure never stops the others;
- only FAILED runs are ever queried.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.runtime_v2 import RunStatus
from endless_task.runtime_v2.run_reconcile import reconcile_failed_runs


class _FakeRun:
    def __init__(
        self,
        run_id: str,
        *,
        status: RunStatus = RunStatus.FAILED,
        finished_at: str,
        created_at: str = "",
        error_code: str | None = None,
    ) -> None:
        self.id = run_id
        self.status = status
        self.finished_at = finished_at
        self.created_at = created_at or finished_at
        self.error_code = error_code


class _FakeEvent:
    def __init__(self, event_type: str) -> None:
        self.event_type = event_type


class _FakeRepository:
    def __init__(self, runs=(), events=None) -> None:
        self._runs = list(runs)
        self._events: dict[str, list] = {run.id: [] for run in runs}
        for run_id, event_types in (events or {}).items():
            self._events[run_id] = [_FakeEvent(t) for t in event_types]
        self.appended: list[tuple[str, str, dict]] = []
        self.queried_statuses: list[tuple] = []

    def list_runs(self, *, statuses=None):
        self.queried_statuses.append(tuple(statuses or ()))
        if not statuses:
            return tuple(self._runs)
        allowed = {status.value for status in statuses}
        return tuple(run for run in self._runs if run.status.value in allowed)

    def list_runtime_events(self, run_id: str):
        return tuple(self._events.get(run_id, []))

    def append_runtime_event(self, *, run_id: str, event_type: str, payload: dict) -> None:
        self.appended.append((run_id, event_type, payload))


class _FakeCoordinator:
    def __init__(self, roots_by_run, *, fail_on: str | None = None) -> None:
        self._roots_by_run = roots_by_run
        self._fail_on = fail_on
        self.restored: list[tuple[str, str]] = []

    def workspace_roots_for(self, *, run_id: str) -> tuple[str, ...]:
        return self._roots_by_run.get(run_id, ())

    def apply_restore(self, *, run_id: str, workspace_root: str):
        if self._fail_on == run_id:
            raise RuntimeError(f"restore down for {run_id}")
        self.restored.append((run_id, workspace_root))
        return (("a.txt",), ("user.txt",))


class ReconcileFailedRunsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _coordinator(self, roots_by_run=None, **kwargs) -> _FakeCoordinator:
        return _FakeCoordinator(roots_by_run or {}, **kwargs)

    async def test_failed_without_marker_and_with_cp_is_restored(self) -> None:
        runs = [_FakeRun("run_a", finished_at="2026-09-05T10:00:00Z")]
        repository = _FakeRepository(runs)
        coordinator = self._coordinator({"run_a": (str(self.ws),)})
        summary = await reconcile_failed_runs(
            repository=repository, coordinator=coordinator
        )
        self.assertEqual(1, summary.restored_runs)
        self.assertEqual(1, summary.restored_files)
        self.assertEqual([("run_a", str(self.ws))], coordinator.restored)
        self.assertEqual(
            [("run_a", "run_auto_restored")],
            [(rid, et) for rid, et, _ in repository.appended],
        )
        self.assertEqual("startup_reconcile", repository.appended[0][2]["trigger"])

    async def test_marker_present_skips(self) -> None:
        runs = [_FakeRun("run_a", finished_at="2026-09-05T10:00:00Z")]
        repository = _FakeRepository(runs, events={"run_a": ("run_auto_restored",)})
        coordinator = self._coordinator({"run_a": (str(self.ws),)})
        summary = await reconcile_failed_runs(
            repository=repository, coordinator=coordinator
        )
        self.assertEqual(0, summary.restored_runs)
        self.assertEqual([], coordinator.restored)

    async def test_no_checkpoints_skips(self) -> None:
        runs = [_FakeRun("run_a", finished_at="2026-09-05T10:00:00Z")]
        repository = _FakeRepository(runs)
        coordinator = self._coordinator({})
        summary = await reconcile_failed_runs(
            repository=repository, coordinator=coordinator
        )
        self.assertEqual(0, summary.restored_runs)

    async def test_missing_workspace_dir_skips(self) -> None:
        runs = [_FakeRun("run_a", finished_at="2026-09-05T10:00:00Z")]
        repository = _FakeRepository(runs)
        coordinator = self._coordinator({"run_a": ("/definitely/missing",)})
        summary = await reconcile_failed_runs(
            repository=repository, coordinator=coordinator
        )
        self.assertEqual(0, summary.restored_runs)
        self.assertEqual([], coordinator.restored)

    async def test_newest_failed_run_restored_first(self) -> None:
        runs = [
            _FakeRun("run_old", finished_at="2026-09-05T09:00:00Z"),
            _FakeRun("run_new", finished_at="2026-09-05T11:00:00Z"),
        ]
        repository = _FakeRepository(runs)
        coordinator = self._coordinator(
            {"run_old": (str(self.ws),), "run_new": (str(self.ws),)}
        )
        await reconcile_failed_runs(repository=repository, coordinator=coordinator)
        self.assertEqual(
            ["run_new", "run_old"],
            [run_id for run_id, _ in coordinator.restored],
        )

    async def test_one_failure_does_not_stop_others(self) -> None:
        runs = [
            _FakeRun("run_a", finished_at="2026-09-05T10:00:00Z"),
            _FakeRun("run_b", finished_at="2026-09-05T11:00:00Z"),
        ]
        repository = _FakeRepository(runs)
        coordinator = self._coordinator(
            {"run_a": (str(self.ws),), "run_b": (str(self.ws),)},
            fail_on="run_b",
        )
        summary = await reconcile_failed_runs(
            repository=repository, coordinator=coordinator
        )
        self.assertEqual(1, summary.restored_runs)
        self.assertEqual([("run_a", str(self.ws))], coordinator.restored)

    async def test_only_failed_runs_are_queried(self) -> None:
        runs = [
            _FakeRun("run_failed", finished_at="2026-09-05T10:00:00Z"),
            _FakeRun("run_cancelled", status=RunStatus.CANCELLED, finished_at="2026-09-05T09:00:00Z"),
            _FakeRun("run_completed", status=RunStatus.COMPLETED, finished_at="2026-09-05T08:00:00Z"),
        ]
        repository = _FakeRepository(runs)
        coordinator = self._coordinator(
            {"run_failed": (str(self.ws),), "run_cancelled": (str(self.ws),), "run_completed": (str(self.ws),)}
        )
        summary = await reconcile_failed_runs(
            repository=repository, coordinator=coordinator
        )
        self.assertEqual((RunStatus.FAILED,), repository.queried_statuses[0])
        self.assertEqual(1, summary.restored_runs)
        self.assertEqual([("run_failed", str(self.ws))], coordinator.restored)


if __name__ == "__main__":
    unittest.main()
