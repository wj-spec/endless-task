CREATE TABLE preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    permission_mode TEXT NOT NULL DEFAULT 'confirm_every_time'
        CHECK (permission_mode IN (
            'confirm_every_time', 'trust_local_writes', 'trust_all'
        )),
    updated_at TEXT NOT NULL
);

INSERT INTO preferences (id, permission_mode, updated_at)
VALUES (1, 'confirm_every_time', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
