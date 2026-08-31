from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from endless_task.domain.repositories import NotFoundError

from .models import Aggregation, RunEvaluation

from endless_task.storage.database import Database


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _new_id() -> str:
    return f"eval_{uuid.uuid4().hex}"


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class EvalBatchRow:
    id: str
    created_at: str
    mode: str
    filters: dict[str, Any]
    judge_provider: Optional[str]
    judge_model: Optional[str]
    run_count: int
    status: str
    aggregate: Optional[dict[str, Any]]


class SqliteEvalRepository:
    """Persists offline evaluation batches and their per-run results."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def create_batch(
        self,
        *,
        mode: str,
        filters: Mapping[str, Any],
        judge_provider: Optional[str] = None,
        judge_model: Optional[str] = None,
        run_count: int = 0,
    ) -> str:
        batch_id = _new_id()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO eval_batches(
                    id, created_at, mode, filters_json,
                    judge_provider, judge_model, run_count, status, aggregate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', NULL)
                """,
                (
                    batch_id,
                    _utc_now(),
                    mode,
                    _dump(dict(filters)),
                    judge_provider,
                    judge_model,
                    run_count,
                ),
            )
        return batch_id

    def append_run_result(
        self,
        batch_id: str,
        run: RunEvaluation,
    ) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO eval_run_results(
                    id, batch_id, run_id, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, run_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    created_at = excluded.created_at
                """,
                (
                    _new_id(),
                    batch_id,
                    run.run_id,
                    _dump(run.to_dict()),
                    _utc_now(),
                ),
            )

    def finish_batch(
        self,
        batch_id: str,
        aggregation: Aggregation,
    ) -> None:
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE eval_batches
                SET status = 'complete', aggregate_json = ?, run_count = ?
                WHERE id = ?
                """,
                (_dump(aggregation.to_dict()), aggregation.run_count, batch_id),
            )

    def get_batch(self, batch_id: str) -> EvalBatchRow:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM eval_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Evaluation batch not found: {batch_id}")
        return self._batch_from_row(row)

    def list_batches(self) -> tuple[EvalBatchRow, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM eval_batches ORDER BY created_at, id"
            ).fetchall()
        return tuple(self._batch_from_row(row) for row in rows)

    def load_run_results(self, batch_id: str) -> tuple[RunEvaluation, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM eval_run_results
                WHERE batch_id = ?
                ORDER BY created_at, run_id
                """,
                (batch_id,),
            ).fetchall()
        results = []
        for row in rows:
            result = json.loads(row["result_json"])
            results.append(RunEvaluation.from_dict(result))
        return tuple(results)

    @staticmethod
    def _batch_from_row(row: sqlite3.Row) -> EvalBatchRow:
        return EvalBatchRow(
            id=row["id"],
            created_at=row["created_at"],
            mode=row["mode"],
            filters=json.loads(row["filters_json"]),
            judge_provider=row["judge_provider"],
            judge_model=row["judge_model"],
            run_count=row["run_count"],
            status=row["status"],
            aggregate=(
                json.loads(row["aggregate_json"]) if row["aggregate_json"] else None
            ),
        )
