from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator


class Database:
    """Owns SQLite connections and applies bundled migrations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def integrity_check(self) -> str:
        with self.connect() as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0])

    def backup(self, destination: Path) -> Path:
        target = Path(destination).expanduser().resolve()
        source = self.path.expanduser().resolve()
        if target == source:
            raise ValueError("Backup destination must differ from the live database")
        if target.exists():
            raise FileExistsError(f"Backup already exists: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        source_uri = f"{source.as_uri()}?mode=ro"
        try:
            with closing(
                sqlite3.connect(source_uri, uri=True, timeout=5.0)
            ) as source_connection:
                source_connection.execute("PRAGMA busy_timeout = 5000")
                with closing(sqlite3.connect(str(temporary))) as destination_connection:
                    source_connection.backup(destination_connection)
                    result = destination_connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()
                    if result is None or result[0] != "ok":
                        raise sqlite3.DatabaseError("Backup integrity check failed")
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def restore(self, source: Path) -> Path:
        backup = Path(source).expanduser().resolve()
        target = self.path.expanduser().resolve()
        if backup == target:
            raise ValueError("Restore source must differ from the live database")
        if not backup.is_file():
            raise FileNotFoundError(f"Restore source does not exist: {backup}")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.restore.tmp"
        source_uri = f"{backup.as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
                result = source_connection.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise sqlite3.DatabaseError("Restore source integrity check failed")
                with closing(sqlite3.connect(str(temporary))) as target_connection:
                    source_connection.backup(target_connection)
                    restored = target_connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()
                    if restored is None or restored[0] != "ok":
                        raise sqlite3.DatabaseError("Restored database integrity check failed")
            temporary.chmod(0o600)
            os.replace(temporary, target)
            Path(f"{target}-wal").unlink(missing_ok=True)
            Path(f"{target}-shm").unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def applied_migrations(self) -> tuple[str, ...]:
        with self.connect() as connection:
            self._ensure_migration_table(connection)
            rows = connection.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            ).fetchall()
        return tuple(row["name"] for row in rows)

    def _migrate(self) -> None:
        with self.connect() as connection:
            self._ensure_migration_table(connection)
            applied = {
                row["name"]
                for row in connection.execute("SELECT name FROM schema_migrations")
            }

            migration_root = resources.files("endless_task.storage.migrations")
            migrations = sorted(
                item
                for item in migration_root.iterdir()
                if item.name.endswith(".sql")
            )

            for migration in migrations:
                if migration.name in applied:
                    continue

                sql = migration.read_text(encoding="utf-8")
                escaped_name = migration.name.replace("'", "''")
                script = (
                    "BEGIN IMMEDIATE;\n"
                    f"{sql}\n"
                    "INSERT INTO schema_migrations(name, applied_at) "
                    f"VALUES ('{escaped_name}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n"
                    "COMMIT;"
                )
                try:
                    connection.executescript(script)
                except BaseException:
                    connection.rollback()
                    raise

    @staticmethod
    def _ensure_migration_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
