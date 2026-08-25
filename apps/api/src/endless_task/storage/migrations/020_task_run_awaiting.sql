ALTER TABLE task_runs ADD COLUMN awaiting_user INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task_runs ADD COLUMN awaiting_note TEXT;
