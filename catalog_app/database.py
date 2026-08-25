from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import diagnostic_sql_connection_factory
from .schema import (
    APPLICATION_ID,
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
)


class DatabaseError(RuntimeError):
    """Raised when the Catalog 2.0 database cannot be used safely."""


@dataclass(frozen=True)
class DatabaseRuntimeStatus:
    path: Path
    application_id: int
    schema_version: int
    journal_mode: str
    foreign_keys_enabled: bool


@dataclass(frozen=True)
class DatabaseStatus:
    path: Path
    application_id: int
    schema_version: int
    journal_mode: str
    foreign_keys_enabled: bool
    tables: frozenset[str]
    indexes: frozenset[str]
    quick_check: str


@dataclass(frozen=True)
class DatabaseUpgradeResult:
    path: Path
    from_version: int
    to_version: int
    upgraded: bool


def initialize_database(db_path: Path) -> bool:
    """
    Create a new Catalog 2.0 database.

    Returns True when a new database was created and False when an existing,
    already valid database was left unchanged.
    """
    db_path = db_path.expanduser()

    if db_path.exists():
        upgrade_database(db_path)
        check_database(db_path)
        return False

    db_path.parent.mkdir(parents=True, exist_ok=True)
    created_files = _database_companion_paths(db_path)

    try:
        connection = _connect_read_write(db_path)

        try:
            journal_mode = _enable_wal(connection)

            if journal_mode != "wal":
                raise DatabaseError(
                    f"SQLite did not switch to WAL mode: {journal_mode}"
                )

            connection.execute("BEGIN IMMEDIATE")

            try:
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)

                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA legacy_alter_table = OFF")
                connection.execute("PRAGMA foreign_keys = ON")

            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

        check_database(db_path)
        return True

    except (sqlite3.Error, DatabaseError) as exc:
        _remove_new_database_files(created_files)

        if isinstance(exc, DatabaseError):
            raise

        raise DatabaseError(f"Database initialization failed: {exc}") from exc


def upgrade_database(db_path: Path) -> DatabaseUpgradeResult:
    """Upgrade an existing Catalog 2.0 database with safe additive schema changes."""
    db_path = db_path.expanduser()

    if not db_path.exists():
        raise DatabaseError(f"Database does not exist: {db_path}")

    if not db_path.is_file():
        raise DatabaseError(f"Database path is not a file: {db_path}")

    try:
        connection = _connect_read_write(db_path)

        try:
            journal_mode = _enable_wal(connection)
            if journal_mode != "wal":
                raise DatabaseError(
                    f"SQLite did not switch to WAL mode: {journal_mode}"
                )

            application_id = int(
                connection.execute("PRAGMA application_id").fetchone()[0]
            )
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )

            if application_id != APPLICATION_ID:
                raise DatabaseError(
                    "Database does not have the Catalog 2.0 identity "
                    f"(application_id={application_id}, expected {APPLICATION_ID})."
                )

            if current_version > SCHEMA_VERSION:
                raise DatabaseError(
                    f"Database has a newer schema ({current_version}), "
                    f"than this code supports ({SCHEMA_VERSION})."
                )

            if current_version == SCHEMA_VERSION:
                return DatabaseUpgradeResult(
                    path=db_path,
                    from_version=current_version,
                    to_version=SCHEMA_VERSION,
                    upgraded=False,
                )

            original_version = current_version
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA legacy_alter_table = ON")
            connection.execute("BEGIN IMMEDIATE")

            try:
                if current_version == 1:
                    _migrate_v1_to_v2(connection)
                    current_version = 2

                if current_version == 2:
                    _migrate_v2_to_v3(connection)
                    current_version = 3

                if current_version == 3:
                    _migrate_v3_to_v4(connection)
                    current_version = 4

                if current_version == 4:
                    _migrate_v4_to_v5(connection)
                    current_version = 5

                if current_version != SCHEMA_VERSION:
                    raise DatabaseError(
                        f"Unsupported default schema version: {current_version}."
                    )

                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA legacy_alter_table = OFF")
                connection.execute("PRAGMA foreign_keys = ON")

            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

        check_database(db_path)
        return DatabaseUpgradeResult(
            path=db_path,
            from_version=original_version,
            to_version=SCHEMA_VERSION,
            upgraded=True,
        )

    except sqlite3.Error as exc:
        raise DatabaseError(f"Database upgrade failed: {exc}") from exc


def database_upgrade_lines(result: DatabaseUpgradeResult) -> list[str]:
    if result.upgraded:
        action = f"Database was upgraded from schema {result.from_version} to {result.to_version}."
    else:
        action = f"Database already uses the current schema {result.to_version}."

    return [
        "Catalog 2.0 – database upgrade",
        "=" * 70,
        f"database: {result.path}",
        action,
    ]


def validate_database_runtime(db_path: Path) -> DatabaseRuntimeStatus:
    """Validate only the database properties required for normal runtime use."""
    db_path = _validated_database_path(db_path)

    try:
        connection = _connect_read_only(db_path)
        try:
            return _runtime_status_from_connection(connection, db_path)
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Database cannot be read or is not a valid SQLite file: {exc}"
        ) from exc


def check_database(db_path: Path) -> DatabaseStatus:
    """Run the explicit full database structure and integrity check."""
    db_path = _validated_database_path(db_path)

    try:
        connection = _connect_read_only(db_path)

        try:
            runtime_status = _runtime_status_from_connection(connection, db_path)

            tables = frozenset(
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite_%'
                    """
                )
            )
            indexes = frozenset(
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'index'
                      AND name NOT LIKE 'sqlite_%'
                    """
                )
            )

            quick_check = str(
                connection.execute("PRAGMA quick_check").fetchone()[0]
            )
            foreign_key_violations = list(
                connection.execute("PRAGMA foreign_key_check")
            )
        finally:
            connection.close()

    except sqlite3.Error as exc:
        raise DatabaseError(
            f"Database cannot be read or is not a valid SQLite file: {exc}"
        ) from exc

    missing_tables = EXPECTED_TABLES - tables
    if missing_tables:
        raise DatabaseError(
            "Database is missing expected tables: "
            + ", ".join(sorted(missing_tables))
        )

    missing_indexes = EXPECTED_INDEXES - indexes
    if missing_indexes:
        raise DatabaseError(
            "Database is missing expected indexes: "
            + ", ".join(sorted(missing_indexes))
        )

    if quick_check.lower() != "ok":
        raise DatabaseError(f"SQLite quick_check failed: {quick_check}")

    if foreign_key_violations:
        raise DatabaseError(
            f"Database contains foreign key violations: "
            f"{len(foreign_key_violations)}"
        )

    return DatabaseStatus(
        path=db_path,
        application_id=runtime_status.application_id,
        schema_version=runtime_status.schema_version,
        journal_mode=runtime_status.journal_mode,
        foreign_keys_enabled=runtime_status.foreign_keys_enabled,
        tables=tables,
        indexes=indexes,
        quick_check=quick_check,
    )


@contextmanager
def open_database(
    db_path: Path,
    *,
    read_only: bool,
    validate: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Open one Catalog 2.0 database connection.

    Normal connections validate only the Catalog identity, supported schema
    version and operational SQLite settings on the connection being returned.
    Full integrity checks are reserved for the explicit ``db-check`` workflow.
    """
    db_path = db_path.expanduser()

    if validate:
        db_path = _validated_database_path(db_path)
    else:
        if not db_path.exists():
            raise DatabaseError(f"Database does not exist: {db_path}")

        if not db_path.is_file():
            raise DatabaseError(f"Database path is not a file: {db_path}")

    connection = (
        _connect_read_only(db_path)
        if read_only
        else _connect_read_write(db_path)
    )

    try:
        if validate:
            _runtime_status_from_connection(connection, db_path)
        yield connection
    finally:
        connection.close()


def database_status_lines(status: DatabaseStatus) -> list[str]:
    """Return a human-readable database validation summary."""
    return [
        "Catalog 2.0 – database check",
        "=" * 70,
        f"database: {status.path}",
        f"application_id: {status.application_id}",
        f"schema_version: {status.schema_version}",
        f"journal_mode: {status.journal_mode}",
        f"foreign_keys: {'ON' if status.foreign_keys_enabled else 'OFF'}",
        f"tables: {len(status.tables)}",
        f"indexes: {len(status.indexes)}",
        f"quick_check: {status.quick_check}",
        "Database is valid.",
    ]


def _validated_database_path(db_path: Path) -> Path:
    db_path = db_path.expanduser()

    if not db_path.exists():
        raise DatabaseError(f"Database does not exist: {db_path}")

    if not db_path.is_file():
        raise DatabaseError(f"Database path is not a file: {db_path}")

    return db_path


def _runtime_status_from_connection(
    connection: sqlite3.Connection,
    db_path: Path,
) -> DatabaseRuntimeStatus:
    connection.execute("PRAGMA foreign_keys = ON")

    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    foreign_keys_enabled = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])

    if application_id != APPLICATION_ID:
        raise DatabaseError(
            "Database does not have the Catalog 2.0 identity "
            f"(application_id={application_id}, expected {APPLICATION_ID})."
        )

    if schema_version != SCHEMA_VERSION:
        raise DatabaseError(
            f"Unsupported database schema version: {schema_version}. "
            f"Supported version is {SCHEMA_VERSION}."
        )

    if journal_mode != "wal":
        raise DatabaseError(f"Database does not use WAL mode: {journal_mode}")

    if not foreign_keys_enabled:
        raise DatabaseError("Foreign keys are not enabled for the database connection.")

    return DatabaseRuntimeStatus(
        path=db_path,
        application_id=application_id,
        schema_version=schema_version,
        journal_mode=journal_mode,
        foreign_keys_enabled=foreign_keys_enabled,
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Move thumbnail evidence from the early v1 shape to the v2 cache model."""
    _require_table(connection, "thumbnails")
    _require_table(connection, "folder_preview_items")

    connection.execute("DROP INDEX IF EXISTS idx_thumbnails_cleanup")
    connection.execute("DROP INDEX IF EXISTS idx_folder_preview_media")

    connection.execute(
        """
        CREATE TABLE thumbnails_v2 (
            id INTEGER PRIMARY KEY,
            media_id INTEGER NOT NULL
                REFERENCES media_files(id) ON DELETE CASCADE,
            thumbnail_type TEXT NOT NULL
                CHECK (thumbnail_type IN (
                    'photo_tile',
                    'gif_preview',
                    'video_poster',
                    'video_frame',
                    'folder_preview'
                )),
            cache_class TEXT NOT NULL
                CHECK (cache_class IN ('dynamic', 'protected')),
            variant_key TEXT NOT NULL DEFAULT '',
            output_rel_path TEXT NOT NULL UNIQUE,
            width INTEGER NOT NULL
                CHECK (width > 0),
            height INTEGER NOT NULL
                CHECK (height > 0),
            file_size_bytes INTEGER NOT NULL
                CHECK (file_size_bytes >= 0),
            source_size_bytes INTEGER NOT NULL
                CHECK (source_size_bytes >= 0),
            source_modified_time REAL NOT NULL,
            algorithm_version TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'ready', 'stale', 'missing', 'error')),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_used_at REAL,
            error_message TEXT,
            UNIQUE (media_id, thumbnail_type, variant_key)
        )
        """
    )

    connection.execute(
        """
        INSERT INTO thumbnails_v2 (
            id,
            media_id,
            thumbnail_type,
            cache_class,
            variant_key,
            output_rel_path,
            width,
            height,
            file_size_bytes,
            source_size_bytes,
            source_modified_time,
            algorithm_version,
            status,
            created_at,
            updated_at,
            last_used_at,
            error_message
        )
        SELECT
            id,
            media_id,
            CASE kind
                WHEN 'gif_static' THEN 'gif_preview'
                ELSE 'photo_tile'
            END AS thumbnail_type,
            CASE kind
                WHEN 'gif_static' THEN 'protected'
                ELSE 'dynamic'
            END AS cache_class,
            '' AS variant_key,
            output_rel_path,
            width,
            height,
            size_bytes AS file_size_bytes,
            source_size_bytes,
            source_modified_time,
            'legacy-v1' AS algorithm_version,
            'ready' AS status,
            created_at,
            created_at AS updated_at,
            last_accessed_at AS last_used_at,
            NULL AS error_message
        FROM thumbnails
        """
    )

    connection.execute("DROP TABLE thumbnails")
    connection.execute("ALTER TABLE thumbnails_v2 RENAME TO thumbnails")

    connection.execute(
        """
        CREATE TABLE folder_preview_items_v2 (
            folder_id INTEGER NOT NULL
                REFERENCES folders(id) ON DELETE CASCADE,
            selection_type TEXT NOT NULL
                CHECK (selection_type IN ('auto', 'manual')),
            position INTEGER NOT NULL
                CHECK (position BETWEEN 1 AND 12),
            media_id INTEGER NOT NULL
                REFERENCES media_files(id) ON DELETE CASCADE,
            PRIMARY KEY (folder_id, selection_type, position),
            UNIQUE (folder_id, selection_type, media_id)
        )
        """
    )

    connection.execute(
        """
        INSERT INTO folder_preview_items_v2 (
            folder_id,
            selection_type,
            position,
            media_id
        )
        SELECT folder_id, selection_type, position, media_id
        FROM folder_preview_items
        """
    )

    connection.execute("DROP TABLE folder_preview_items")
    connection.execute("ALTER TABLE folder_preview_items_v2 RENAME TO folder_preview_items")

    connection.execute(
        """
        CREATE INDEX idx_thumbnails_cleanup
            ON thumbnails(cache_class, status, last_used_at, id)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_thumbnails_media_kind
            ON thumbnails(media_id, thumbnail_type, variant_key)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_thumbnails_status
            ON thumbnails(status, cache_class, thumbnail_type)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_folder_preview_media
            ON folder_preview_items(media_id)
        """
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """Allow parent-derived automatic folder preview rows."""
    _require_table(connection, "folder_preview_items")

    connection.execute("DROP INDEX IF EXISTS idx_folder_preview_media")

    connection.execute(
        """
        CREATE TABLE folder_preview_items_v3 (
            folder_id INTEGER NOT NULL
                REFERENCES folders(id) ON DELETE CASCADE,
            selection_type TEXT NOT NULL
                CHECK (selection_type IN ('auto', 'auto_parent', 'manual')),
            position INTEGER NOT NULL
                CHECK (position BETWEEN 1 AND 12),
            media_id INTEGER NOT NULL
                REFERENCES media_files(id) ON DELETE CASCADE,
            PRIMARY KEY (folder_id, selection_type, position),
            UNIQUE (folder_id, selection_type, media_id)
        )
        """
    )

    connection.execute(
        """
        INSERT INTO folder_preview_items_v3 (
            folder_id,
            selection_type,
            position,
            media_id
        )
        SELECT folder_id, selection_type, position, media_id
        FROM folder_preview_items
        """
    )

    connection.execute("DROP TABLE folder_preview_items")
    connection.execute("ALTER TABLE folder_preview_items_v3 RENAME TO folder_preview_items")

    connection.execute(
        """
        CREATE INDEX idx_folder_preview_media
            ON folder_preview_items(media_id)
        """
    )



def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """Allow explicit cancellation of staged scans."""
    _require_table(connection, "scan_sessions")

    connection.execute("DROP INDEX IF EXISTS idx_scan_sessions_status_started")
    connection.execute("ALTER TABLE scan_sessions RENAME TO scan_sessions_v3")

    connection.execute(
        """
        CREATE TABLE scan_sessions (
            id INTEGER PRIMARY KEY,
            scan_type TEXT NOT NULL
                CHECK (scan_type IN ('full', 'branch')),
            scope_rel_path TEXT NOT NULL DEFAULT '',
            scope_path_key TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL
                CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'cancelled')),
            folder_count INTEGER NOT NULL DEFAULT 0
                CHECK (folder_count >= 0),
            media_count INTEGER NOT NULL DEFAULT 0
                CHECK (media_count >= 0),
            error_count INTEGER NOT NULL DEFAULT 0
                CHECK (error_count >= 0),
            CHECK (
                (status = 'running' AND finished_at IS NULL)
                OR
                (status IN ('completed', 'failed', 'interrupted', 'cancelled') AND finished_at IS NOT NULL)
            )
        )
        """
    )

    connection.execute(
        """
        INSERT INTO scan_sessions (
            id,
            scan_type,
            scope_rel_path,
            scope_path_key,
            started_at,
            finished_at,
            status,
            folder_count,
            media_count,
            error_count
        )
        SELECT
            id,
            scan_type,
            scope_rel_path,
            scope_path_key,
            started_at,
            finished_at,
            status,
            folder_count,
            media_count,
            error_count
        FROM scan_sessions_v3
        """
    )

    connection.execute("DROP TABLE scan_sessions_v3")
    connection.execute(
        """
        CREATE INDEX idx_scan_sessions_status_started
            ON scan_sessions(status, started_at)
        """
    )



def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
    """Store active catalog baseline for staged scans.

    Existing staged scans predate the baseline guard, so their baseline stays
    NULL. If an active catalog already exists, those old staged scans must be
    recreated before activation. That is intentional: the old scan cannot be
    proven to have been staged against the current active catalog.
    """
    _require_table(connection, "scan_sessions")

    existing_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(scan_sessions)")
    }
    if "active_scan_id_at_stage" in existing_columns:
        return

    connection.execute(
        """
        ALTER TABLE scan_sessions
        ADD COLUMN active_scan_id_at_stage INTEGER
            REFERENCES scan_sessions(id)
        """
    )


def _require_table(connection: sqlite3.Connection, table_name: str) -> None:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_schema
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    if row is None:
        raise DatabaseError(
            f"Database cannot be upgraded; missing table: {table_name}"
        )


def _connect_read_write(db_path: Path) -> sqlite3.Connection:
    factory = diagnostic_sql_connection_factory()
    connect_kwargs = {"timeout": 5.0}
    if factory is not None:
        connect_kwargs["factory"] = factory
    connection = sqlite3.connect(db_path, **connect_kwargs)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    factory = diagnostic_sql_connection_factory()
    connect_kwargs = {"uri": True, "timeout": 5.0}
    if factory is not None:
        connect_kwargs["factory"] = factory
    connection = sqlite3.connect(uri, **connect_kwargs)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _enable_wal(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()


def _database_companion_paths(db_path: Path) -> tuple[Path, Path, Path]:
    return (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )


def _remove_new_database_files(paths: tuple[Path, Path, Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
