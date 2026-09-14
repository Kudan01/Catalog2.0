from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .database import open_database, validate_database_runtime
from .models import (
    FolderRecord,
    MediaRecord,
    ScanErrorRecord,
    ScanSkipRecord,
)
from .scan_lock import ScanLockError, ScanProcessLock, scan_lock_path
from .scan_plan import ScanAction
from .paths import normalize_catalog_relative_path
from .sorting import catalog_path_key


SCAN_BATCH_SIZE = 1000


class ScanStageError(RuntimeError):
    """Raised when a staging scan cannot be completed safely."""


class ScanStageInterrupted(RuntimeError):
    """Raised after a staging scan was interrupted and recorded as such."""


@dataclass(frozen=True)
class ScanStageResult:
    scan_id: int
    scan_type: str
    scope_rel_path: str
    status: str
    active_scan_id_at_stage: int | None
    folders: int
    images: int
    gifs: int
    videos: int
    other: int
    errors: int
    skipped_links: int
    skipped_ignored_directories: int
    skipped_other: int
    duration_seconds: float

    @property
    def media_total(self) -> int:
        return self.images + self.gifs + self.videos + self.other


@dataclass(frozen=True)
class ScanStatus:
    database_path: Path
    scan_id: int | None
    scan_type: str | None
    scope_rel_path: str | None
    status: str | None
    started_at: float | None
    finished_at: float | None
    declared_folder_count: int
    declared_media_count: int
    declared_error_count: int
    active_scan_id_at_stage: int | None
    staged_folder_count: int
    staged_media_count: int
    staged_error_count: int
    active_scan_id: int | None
    active_folder_count: int
    active_media_count: int
    available_folder_count: int
    available_media_count: int
    unavailable_folder_count: int
    unavailable_media_count: int



@dataclass(frozen=True)
class DiscardStagedScanResult:
    scan_id: int
    previous_status: str
    status: str
    deleted_folders: int
    deleted_media: int
    deleted_errors: int
    active_folder_references: int
    active_media_references: int



def stage_scan_plan(
    db_path: Path,
    actions: Iterable[ScanAction],
    *,
    scope_rel_path: str = "",
    batch_size: int = SCAN_BATCH_SIZE,
) -> ScanStageResult:
    """
    Store the shared scan plan in staging tables without activating it.

    Committed batches remain associated with their scan_id. The active catalog
    tables are never modified by this function.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than 0.")

    scope_rel_path = normalize_catalog_relative_path(
        scope_rel_path,
        allow_root=True,
    )
    scope_path_key = catalog_path_key(scope_rel_path)
    scan_type = "branch" if scope_rel_path else "full"

    started_monotonic = time.monotonic()

    # Validate first so a missing database does not create even a lock directory.
    validate_database_runtime(db_path)
    lock_path = scan_lock_path(db_path)

    try:
        process_lock = ScanProcessLock(lock_path)
        process_lock.__enter__()
    except ScanLockError:
        raise

    scan_id: int | None = None

    try:
        with open_database(db_path, read_only=False) as connection:
            _mark_stale_running_scans_interrupted(connection)
            scan_id = _create_scan_session(
                connection,
                scan_type=scan_type,
                scope_rel_path=scope_rel_path,
                scope_path_key=scope_path_key,
            )

        counters = _new_counters()
        folder_rows: list[tuple[object, ...]] = []
        media_rows: list[tuple[object, ...]] = []
        error_rows: list[tuple[object, ...]] = []
        saw_begin = False
        saw_complete = False

        try:
            with open_database(db_path, read_only=False) as connection:
                for action in actions:
                    if saw_complete:
                        raise ScanStageError(
                            "Plan contains an action after complete_scan."
                        )

                    if action.kind == "begin_scan":
                        if saw_begin:
                            raise ScanStageError(
                                "Plan contains more than one begin_scan action."
                            )
                        saw_begin = True
                        continue

                    if not saw_begin:
                        raise ScanStageError(
                            "The first data action precedes begin_scan."
                        )

                    if action.kind == "folder":
                        record = _expect_payload(action, FolderRecord)
                        folder_rows.append(_folder_row(scan_id, record))
                        counters["folders"] += 1

                    elif action.kind == "media":
                        record = _expect_payload(action, MediaRecord)
                        media_rows.append(_media_row(scan_id, record))
                        counters[record.media_type] += 1

                    elif action.kind == "error":
                        record = _expect_payload(action, ScanErrorRecord)
                        error_rows.append(_error_row(scan_id, record))
                        counters["errors"] += 1

                    elif action.kind == "skip":
                        record = _expect_payload(action, ScanSkipRecord)
                        _count_skip(counters, record)

                    elif action.kind == "complete_scan":
                        saw_complete = True

                    else:
                        raise ScanStageError(
                            f"Unknown scan action kind: {action.kind}"
                        )

                    if _pending_count(folder_rows, media_rows, error_rows) >= batch_size:
                        _flush_batch(
                            connection,
                            folder_rows,
                            media_rows,
                            error_rows,
                        )

                if not saw_begin:
                    raise ScanStageError("Plan does not contain begin_scan.")

                if not saw_complete:
                    raise ScanStageError("Plan was not completed by complete_scan.")

                _flush_batch(
                    connection,
                    folder_rows,
                    media_rows,
                    error_rows,
                )

            _finish_scan_session(
                db_path,
                scan_id,
                status="completed",
            )
            _checkpoint_wal(db_path)

        except KeyboardInterrupt as exc:
            _finish_scan_session(
                db_path,
                scan_id,
                status="interrupted",
            )
            raise ScanStageInterrupted(
                f"Scan {scan_id} was interrupted by the user."
            ) from exc

        except Exception as exc:
            _finish_scan_session(
                db_path,
                scan_id,
                status="failed",
            )

            if isinstance(exc, ScanStageError):
                raise

            if isinstance(exc, sqlite3.Error):
                raise ScanStageError(
                    f"Database write for scan {scan_id} failed: {exc}"
                ) from exc

            raise ScanStageError(
                f"Scan {scan_id} selhal: {exc}"
            ) from exc

        return ScanStageResult(
            scan_id=scan_id,
            scan_type=scan_type,
            scope_rel_path=scope_rel_path,
            status="completed",
            active_scan_id_at_stage=_scan_session_active_baseline(db_path, scan_id),
            folders=counters["folders"],
            images=counters["image"],
            gifs=counters["gif"],
            videos=counters["video"],
            other=counters["other"],
            errors=counters["errors"],
            skipped_links=counters["skipped_links"],
            skipped_ignored_directories=counters[
                "skipped_ignored_directories"
            ],
            skipped_other=counters["skipped_other"],
            duration_seconds=time.monotonic() - started_monotonic,
        )

    finally:
        process_lock.__exit__(None, None, None)



def discard_staged_scan_by_id(db_path: Path, *, scan_id: int) -> DiscardStagedScanResult:
    """Discard one staged scan session without touching active catalog rows.

    The function invalidates only a non-running, non-activated scan_id. It deletes
    working rows from scan_folders / scan_media_files / scan_errors and marks the
    scan session as cancelled. Source media files and active catalog tables are
    never modified.
    """
    if scan_id <= 0:
        raise ScanStageError("scan_id must be a positive integer.")

    validate_database_runtime(db_path)

    try:
        with ScanProcessLock(scan_lock_path(db_path)):
            with open_database(db_path, read_only=False) as connection:
                try:
                    connection.execute("BEGIN IMMEDIATE")

                    row = connection.execute(
                        """
                        SELECT id, status
                        FROM scan_sessions
                        WHERE id = ?
                        """,
                        (scan_id,),
                    ).fetchone()
                    if row is None:
                        raise ScanStageError(f"Scan {scan_id} neexistuje.")

                    previous_status = str(row["status"])
                    if previous_status == "running":
                        raise ScanStageError(
                            f"Scan {scan_id} cannot be discarded: it is still running."
                        )

                    active_folder_references = _count_active_scan_references(
                        connection,
                        "folders",
                        scan_id,
                    )
                    active_media_references = _count_active_scan_references(
                        connection,
                        "media_files",
                        scan_id,
                    )
                    if active_folder_references or active_media_references:
                        raise ScanStageError(
                            f"Scan {scan_id} cannot be discarded: it is already used in the active catalog."
                        )

                    deleted_media = _count_for_scan(connection, "scan_media_files", scan_id)
                    deleted_folders = _count_for_scan(connection, "scan_folders", scan_id)
                    deleted_errors = _count_for_scan(connection, "scan_errors", scan_id)

                    connection.execute(
                        "DELETE FROM scan_media_files WHERE scan_id = ?",
                        (scan_id,),
                    )
                    connection.execute(
                        "DELETE FROM scan_errors WHERE scan_id = ?",
                        (scan_id,),
                    )
                    connection.execute(
                        "DELETE FROM scan_folders WHERE scan_id = ?",
                        (scan_id,),
                    )
                    connection.execute(
                        """
                        UPDATE scan_sessions
                        SET
                            status = 'cancelled',
                            finished_at = COALESCE(finished_at, ?),
                            folder_count = 0,
                            media_count = 0,
                            error_count = 0
                        WHERE id = ?
                        """,
                        (time.time(), scan_id),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
    except ScanLockError as exc:
        raise ScanStageError(
            "Staged scan cannot be discarded because another scan operation is running."
        ) from exc

    return DiscardStagedScanResult(
        scan_id=scan_id,
        previous_status=previous_status,
        status="cancelled",
        deleted_folders=deleted_folders,
        deleted_media=deleted_media,
        deleted_errors=deleted_errors,
        active_folder_references=active_folder_references,
        active_media_references=active_media_references,
    )


def discard_staged_scan_lines(result: DiscardStagedScanResult) -> list[str]:
    return [
        "Catalog 2.0 – staged scan discard",
        "=" * 70,
        f"scan_id: {result.scan_id}",
        f"previous status: {result.previous_status}",
        f"new status: {result.status}",
        f"deleted staged folders: {result.deleted_folders}",
        f"deleted staged media: {result.deleted_media}",
        f"deleted staged errors: {result.deleted_errors}",
        "The active catalog was not changed.",
        "Source data was not changed.",
    ]


def _count_active_scan_references(
    connection: sqlite3.Connection,
    table_name: str,
    scan_id: int,
) -> int:
    if table_name not in {"folders", "media_files"}:
        raise ValueError(f"Disallowed table for active reference check: {table_name}")
    return int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM {table_name}
            WHERE last_successful_scan_id = ?
            """,
            (scan_id,),
        ).fetchone()[0]
    )

def read_scan_status(db_path: Path) -> ScanStatus:
    """Read the latest staging scan and active-table counts without writing."""
    with open_database(db_path, read_only=True) as connection:
        latest = connection.execute(
            """
            SELECT
                id,
                scan_type,
                scope_rel_path,
                status,
                started_at,
                finished_at,
                folder_count,
                media_count,
                error_count,
                active_scan_id_at_stage
            FROM scan_sessions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        active_folder_count, available_folder_count = active_table_counts(
            connection,
            "folders",
        )
        active_media_count, available_media_count = active_table_counts(
            connection,
            "media_files",
        )
        unavailable_folder_count = active_folder_count - available_folder_count
        unavailable_media_count = active_media_count - available_media_count
        active_scan_row = connection.execute(
            """
            SELECT last_successful_scan_id
            FROM folders
            WHERE path_key = ''
            LIMIT 1
            """
        ).fetchone()
        active_scan_id = (
            int(active_scan_row["last_successful_scan_id"])
            if active_scan_row is not None
            else None
        )

        if latest is None:
            return ScanStatus(
                database_path=db_path,
                scan_id=None,
                scan_type=None,
                scope_rel_path=None,
                status=None,
                started_at=None,
                finished_at=None,
                declared_folder_count=0,
                declared_media_count=0,
                declared_error_count=0,
                active_scan_id_at_stage=None,
                staged_folder_count=0,
                staged_media_count=0,
                staged_error_count=0,
                active_scan_id=active_scan_id,
                active_folder_count=active_folder_count,
                active_media_count=active_media_count,
                available_folder_count=available_folder_count,
                available_media_count=available_media_count,
                unavailable_folder_count=unavailable_folder_count,
                unavailable_media_count=unavailable_media_count,
            )

        scan_id = int(latest["id"])

        return ScanStatus(
            database_path=db_path,
            scan_id=scan_id,
            scan_type=str(latest["scan_type"]),
            scope_rel_path=str(latest["scope_rel_path"]),
            status=str(latest["status"]),
            started_at=float(latest["started_at"]),
            finished_at=(
                float(latest["finished_at"])
                if latest["finished_at"] is not None
                else None
            ),
            declared_folder_count=int(latest["folder_count"]),
            declared_media_count=int(latest["media_count"]),
            declared_error_count=int(latest["error_count"]),
            active_scan_id_at_stage=(
                int(latest["active_scan_id_at_stage"])
                if latest["active_scan_id_at_stage"] is not None
                else None
            ),
            staged_folder_count=_count_for_scan(
                connection,
                "scan_folders",
                scan_id,
            ),
            staged_media_count=_count_for_scan(
                connection,
                "scan_media_files",
                scan_id,
            ),
            staged_error_count=_count_for_scan(
                connection,
                "scan_errors",
                scan_id,
            ),
            active_scan_id=active_scan_id,
            active_folder_count=active_folder_count,
            active_media_count=active_media_count,
            available_folder_count=available_folder_count,
            available_media_count=available_media_count,
            unavailable_folder_count=unavailable_folder_count,
            unavailable_media_count=unavailable_media_count,
        )


def scan_stage_result_lines(result: ScanStageResult) -> list[str]:
    """Return a human-readable staging-scan summary."""
    return [
        "Catalog 2.0 – batch scan write to staging tables",
        "=" * 70,
        f"scan_id: {result.scan_id}",
        f"type: {result.scan_type}",
        f"branch: {result.scope_rel_path or '[root]'}",
        f"status: {result.status}",
        f"active scan at stage time: {result.active_scan_id_at_stage or 'none'}",
        f"Folders: {result.folders}",
        f"Photos: {result.images}",
        f"GIFy: {result.gifs}",
        f"Videos: {result.videos}",
        f"Other: {result.other}",
        f"Media total: {result.media_total}",
        f"Errors: {result.errors}",
        f"Skipped links/junctions: {result.skipped_links}",
        (
            "Skipped technical directories: "
            f"{result.skipped_ignored_directories}"
        ),
        f"Other skipped items: {result.skipped_other}",
        f"Time: {result.duration_seconds:.3f} s",
        "",
        "The result exists only in staging tables.",
        "The active catalog was not changed.",
        (
            "Aktivace: python catalog2.py scan-activate "
            f"--scan-id {result.scan_id}"
            f" --branch \"{result.scope_rel_path}\""
            if result.scope_rel_path
            else f"Aktivace: python catalog2.py scan-activate --scan-id {result.scan_id}"
        ),
    ]


def scan_status_lines(status: ScanStatus) -> list[str]:
    """Return a human-readable staging status summary."""
    lines = [
        "Catalog 2.0 – scan status",
        "=" * 70,
        f"database: {status.database_path}",
    ]

    if status.scan_id is None:
        lines.extend([
            "Latest scan: none",
            f"Active scan_id: {status.active_scan_id or 'none'}",
            f"Available active folders: {status.available_folder_count}",
            f"Available active media: {status.available_media_count}",
            f"Unavailable active folders: {status.unavailable_folder_count}",
            f"Unavailable active media: {status.unavailable_media_count}",
            f"Active folders total: {status.active_folder_count}",
            f"Active media total: {status.active_media_count}",
        ])
        return lines

    scope = status.scope_rel_path or "[root]"
    lines.extend([
        f"Latest scan_id: {status.scan_id}",
        f"type: {status.scan_type}",
        f"branch: {scope}",
        f"status: {status.status}",
        f"declared folders: {status.declared_folder_count}",
        f"declared media: {status.declared_media_count}",
        f"declared errors: {status.declared_error_count}",
        f"active scan at stage time: {status.active_scan_id_at_stage or 'none'}",
        f"staged folders: {status.staged_folder_count}",
        f"staged media: {status.staged_media_count}",
        f"staged errors: {status.staged_error_count}",
        f"active scan_id: {status.active_scan_id or 'none'}",
        f"available active folders: {status.available_folder_count}",
        f"available active media: {status.available_media_count}",
        f"unavailable active folders: {status.unavailable_folder_count}",
        f"unavailable active media: {status.unavailable_media_count}",
        f"active folders total: {status.active_folder_count}",
        f"active media total: {status.active_media_count}",
    ])

    if status.status == "completed" and status.staged_folder_count > 0:
        activate_command = f"python catalog2.py scan-activate --scan-id {status.scan_id}"
        if status.scope_rel_path:
            activate_command += f" --branch \"{status.scope_rel_path}\""
        lines.extend([
            "",
            f"Explicit activation: {activate_command}",
        ])

    return lines


def _current_active_scan_id(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        """
        SELECT last_successful_scan_id
        FROM folders
        WHERE path_key = ''
        LIMIT 1
        """
    ).fetchone()
    return int(row["last_successful_scan_id"]) if row is not None else None


def _scan_session_active_baseline(db_path: Path, scan_id: int) -> int | None:
    with open_database(db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT active_scan_id_at_stage
            FROM scan_sessions
            WHERE id = ?
            """,
            (scan_id,),
        ).fetchone()
    if row is None or row["active_scan_id_at_stage"] is None:
        return None
    return int(row["active_scan_id_at_stage"])


def _create_scan_session(
    connection: sqlite3.Connection,
    *,
    scan_type: str,
    scope_rel_path: str,
    scope_path_key: str,
) -> int:
    if scan_type not in {"full", "branch"}:
        raise ScanStageError(f"Invalid scan type: {scan_type}")

    started_at = time.time()

    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO scan_sessions (
                scan_type,
                scope_rel_path,
                scope_path_key,
                started_at,
                finished_at,
                status,
                folder_count,
                media_count,
                error_count,
                active_scan_id_at_stage
            )
            VALUES (?, ?, ?, ?, NULL, 'running', 0, 0, 0, ?)
            """,
            (
                scan_type,
                scope_rel_path,
                scope_path_key,
                started_at,
                _current_active_scan_id(connection),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return int(cursor.lastrowid)


def _mark_stale_running_scans_interrupted(
    connection: sqlite3.Connection,
) -> None:
    finished_at = time.time()

    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE scan_sessions
            SET
                status = 'interrupted',
                finished_at = ?,
                folder_count = (
                    SELECT COUNT(*)
                    FROM scan_folders
                    WHERE scan_folders.scan_id = scan_sessions.id
                ),
                media_count = (
                    SELECT COUNT(*)
                    FROM scan_media_files
                    WHERE scan_media_files.scan_id = scan_sessions.id
                ),
                error_count = (
                    SELECT COUNT(*)
                    FROM scan_errors
                    WHERE scan_errors.scan_id = scan_sessions.id
                )
            WHERE status = 'running'
            """,
            (finished_at,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _finish_scan_session(
    db_path: Path,
    scan_id: int,
    *,
    status: str,
) -> None:
    if status not in {"completed", "failed", "interrupted", "cancelled"}:
        raise ValueError(f"Invalid final scan status: {status}")

    with open_database(db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE scan_sessions
                SET
                    status = ?,
                    finished_at = ?,
                    folder_count = (
                        SELECT COUNT(*)
                        FROM scan_folders
                        WHERE scan_id = ?
                    ),
                    media_count = (
                        SELECT COUNT(*)
                        FROM scan_media_files
                        WHERE scan_id = ?
                    ),
                    error_count = (
                        SELECT COUNT(*)
                        FROM scan_errors
                        WHERE scan_id = ?
                    )
                WHERE id = ?
                  AND status = 'running'
                """,
                (
                    status,
                    time.time(),
                    scan_id,
                    scan_id,
                    scan_id,
                    scan_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _flush_batch(
    connection: sqlite3.Connection,
    folder_rows: list[tuple[object, ...]],
    media_rows: list[tuple[object, ...]],
    error_rows: list[tuple[object, ...]],
) -> None:
    if not folder_rows and not media_rows and not error_rows:
        return

    try:
        connection.execute("BEGIN IMMEDIATE")

        if folder_rows:
            connection.executemany(
                """
                INSERT INTO scan_folders (
                    scan_id,
                    rel_path,
                    path_key,
                    parent_path_key,
                    name,
                    depth,
                    sort_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                folder_rows,
            )

        if media_rows:
            connection.executemany(
                """
                INSERT INTO scan_media_files (
                    scan_id,
                    rel_path,
                    path_key,
                    folder_path_key,
                    file_name,
                    extension,
                    media_type,
                    size_bytes,
                    modified_time,
                    sort_key
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                media_rows,
            )

        if error_rows:
            connection.executemany(
                """
                INSERT INTO scan_errors (
                    scan_id,
                    rel_path,
                    operation,
                    error_type,
                    message,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                error_rows,
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise

    folder_rows.clear()
    media_rows.clear()
    error_rows.clear()


def _checkpoint_wal(db_path: Path) -> None:
    with open_database(db_path, read_only=False) as connection:
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")


def _folder_row(
    scan_id: int,
    record: FolderRecord,
) -> tuple[object, ...]:
    return (
        scan_id,
        record.rel_path,
        record.path_key,
        record.parent_path_key,
        record.name,
        record.depth,
        record.sort_key,
    )


def _media_row(
    scan_id: int,
    record: MediaRecord,
) -> tuple[object, ...]:
    return (
        scan_id,
        record.rel_path,
        record.path_key,
        record.folder_path_key,
        record.file_name,
        record.extension,
        record.media_type,
        record.size_bytes,
        record.modified_time,
        record.sort_key,
    )


def _error_row(
    scan_id: int,
    record: ScanErrorRecord,
) -> tuple[object, ...]:
    return (
        scan_id,
        record.rel_path,
        record.operation,
        record.error_type,
        record.message,
        time.time(),
    )


def _new_counters() -> dict[str, int]:
    return {
        "folders": 0,
        "image": 0,
        "gif": 0,
        "video": 0,
        "other": 0,
        "errors": 0,
        "skipped_links": 0,
        "skipped_ignored_directories": 0,
        "skipped_other": 0,
    }


def _count_skip(counters: dict[str, int], record: ScanSkipRecord) -> None:
    if record.reason == "link_or_junction":
        counters["skipped_links"] += 1
    elif record.reason == "ignored_directory":
        counters["skipped_ignored_directories"] += 1
    else:
        counters["skipped_other"] += 1


def _pending_count(
    folder_rows: list[tuple[object, ...]],
    media_rows: list[tuple[object, ...]],
    error_rows: list[tuple[object, ...]],
) -> int:
    return len(folder_rows) + len(media_rows) + len(error_rows)


def _expect_payload(action: ScanAction, expected_type: type):
    if not isinstance(action.payload, expected_type):
        raise ScanStageError(
            f"Action {action.kind} does not have expected payload "
            f"{expected_type.__name__}."
        )
    return action.payload


def active_table_counts(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[int, int]:
    """Return total and available rows with one table scan."""
    if table_name not in {"folders", "media_files"}:
        raise ValueError(f"Disallowed active catalog table: {table_name}")

    # Combining these aggregates avoids two cold full-table passes; is_available
    # is not the leading column of any index used for global catalog counts.
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS total_count,
            COALESCE(SUM(is_available), 0) AS available_count
        FROM {table_name}
        """
    ).fetchone()
    return int(row[0]), int(row[1])

def _count_for_scan(
    connection: sqlite3.Connection,
    table: str,
    scan_id: int,
) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()[0]
    )
