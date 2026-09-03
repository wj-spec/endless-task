-- Normalize any pre-existing duplicate running records before enforcing the
-- database-level single-run invariant. Keep the earliest running record for
-- each subject and fail later duplicates so the CHECK constraint remains true.
UPDATE task_runs
SET status = 'failed',
    error = COALESCE(error, '重复的运行中记录已在迁移时终止。'),
    finished_at = COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    retryable = 1
WHERE status = 'running'
  AND EXISTS (
      SELECT 1
      FROM task_runs AS survivor
      WHERE survivor.task_id = task_runs.task_id
        AND survivor.status = 'running'
        AND (
            survivor.started_at < task_runs.started_at
            OR (
                survivor.started_at = task_runs.started_at
                AND survivor.id < task_runs.id
            )
        )
  );

CREATE UNIQUE INDEX idx_task_runs_one_running_subject
ON task_runs(task_id)
WHERE status = 'running';