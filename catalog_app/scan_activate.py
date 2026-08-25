from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Callable, Iterable, Literal
from pathlib import Path

from .database import open_database, validate_database_runtime
from .paths import normalize_catalog_relative_path
from .sorting import catalog_path_key
from .scan_lock import ScanProcessLock, scan_lock_path


class ScanActivationError(RuntimeError):
    """Raised when a completed scan cannot be activated safely."""


class ScanActivationDecisionRequired(ScanActivationError):
    """Raised when activation needs an explicit missing-items decision."""

    def __init__(self, decision_summary: dict) -> None:
        super().__init__("Aktivace vyžaduje rozhodnutí o chybějících položkách.")
        self.decision_summary = decision_summary


MissingAction = Literal["keep", "purge", "cancel", "require_decision"]
ScanActivationMode = Literal["first_activation", "incremental_update", "branch_update"]


@dataclass(frozen=True)
class ScanActivationResult:
    scan_id: int
    mode: ScanActivationMode
    scan_type: str
    scope_rel_path: str
    folder_count: int
    media_count: int
    cleaned_staged_folders: int
    cleaned_staged_media: int
    duration_seconds: float

    folders_new: int = 0
    folders_restored: int = 0
    folders_missing_new: int = 0
    folders_unavailable_total: int = 0
    folders_purged: int = 0

    media_new: int = 0
    media_restored: int = 0
    media_changed: int = 0
    media_unchanged: int = 0
    media_missing_new: int = 0
    media_unavailable_total: int = 0
    media_purged: int = 0
    favorites_purged: int = 0

    missing_action: str = "none"
    purged_media_path_keys: tuple[str, ...] = ()
    activation_timing: tuple[dict[str, object], ...] = ()
    affected_scopes: dict[str, object] = field(default_factory=dict)


class _ActivationTiming:
    """Collect diagnostic timing as language-neutral phase codes and parameters."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.previous = started
        self.steps: list[dict[str, object]] = []

    def mark(self, code: str, params: dict[str, object] | None = None) -> None:
        now = time.monotonic()
        self.steps.append({
            "code": code,
            "params": dict(params or {}),
            "seconds": now - self.previous,
            "elapsed_seconds": now - self.started,
        })
        self.previous = now

    def add_measure(
        self,
        code: str,
        seconds: float,
        params: dict[str, object] | None = None,
    ) -> None:
        # Diagnostic summary row. It does not move the incremental timer, so the
        # next mark() still measures from the previous real activation phase.
        self.steps.append({
            "code": code,
            "params": dict(params or {}),
            "seconds": seconds,
            "elapsed_seconds": time.monotonic() - self.started,
        })

    def snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(step) for step in self.steps)


def _mark_timing(
    timing: _ActivationTiming | None,
    code: str,
    params: dict[str, object] | None = None,
) -> None:
    if timing is not None:
        timing.mark(code, params)


def _add_timing_measure(
    timing: _ActivationTiming | None,
    code: str,
    seconds: float,
    params: dict[str, object] | None = None,
) -> None:
    if timing is not None:
        timing.add_measure(code, seconds, params)


@dataclass(frozen=True)
class _ChangeSummary:
    folders_new: int
    folders_restored: int
    folders_missing_new: int
    folders_unavailable_total: int
    media_new: int
    media_restored: int
    media_changed: int
    media_unchanged: int
    media_missing_new: int
    media_unavailable_total: int


def activate_scan_by_id(
    db_path: Path,
    *,
    scan_id: int,
    expected_scope_rel_path: str | None = None,
    missing_action: MissingAction | None = None,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    favorites_json_path: Path | None = None,
) -> ScanActivationResult:
    """
    Activate one explicit completed staged scan session.

    The function never chooses a scan implicitly. Callers must pass scan_id.
    When expected_scope_rel_path is supplied, the stored scan scope must match
    exactly; this is the guard used by future branch-level workflows.
    """
    if scan_id <= 0:
        raise ScanActivationError("scan_id musí být kladné celé číslo.")

    expected_scope = (
        normalize_catalog_relative_path(expected_scope_rel_path, allow_root=True)
        if expected_scope_rel_path is not None
        else None
    )
    started = time.monotonic()
    timing = _ActivationTiming(started)

    validate_database_runtime(db_path)
    timing.mark("scan.update.timing.database_check")

    original_favorites: list[dict[str, str]] | None = None
    favorites_written = False

    with ScanProcessLock(scan_lock_path(db_path)):
        timing.mark("scan.update.timing.acquire_scan_lock")

        with open_database(db_path, read_only=False) as connection:
            timing.mark("scan.update.timing.open_db_connection")

            try:
                connection.execute("BEGIN IMMEDIATE")
                timing.mark("scan.update.timing.begin_transaction")

                scan = _scan_by_id(connection, scan_id)
                timing.mark("scan.update.timing.load_scan_session")

                _validate_expected_scope(scan, expected_scope)
                timing.mark("scan.update.timing.validate_scan_scope")

                _validate_completed_scan(connection, scan)
                timing.mark("scan.update.timing.validate_staged_scan")

                _validate_active_catalog_baseline(connection, scan)
                timing.mark("scan.update.timing.baseline_stale_check")

                scan_type = str(scan["scan_type"])
                scope_rel_path = str(scan["scope_rel_path"])
                declared_folder_count = int(scan["folder_count"])
                declared_media_count = int(scan["media_count"])

                active_folders = _scalar_count(connection, "folders")
                active_media = _scalar_count(connection, "media_files")
                timing.mark("scan.update.timing.active_catalog_counts")

                if active_folders == 0 and active_media == 0 and scan_type == "full":
                    result = _activate_first_catalog(
                        connection=connection,
                        scan_id=scan_id,
                        scan_type=scan_type,
                        scope_rel_path=scope_rel_path,
                        declared_folder_count=declared_folder_count,
                        declared_media_count=declared_media_count,
                        started=started,
                        timing=timing,
                    )
                elif active_folders == 0 and active_media == 0 and scan_type == "branch":
                    if not _is_top_level_branch_scope(scope_rel_path):
                        raise ScanActivationError(
                            "První branch aktivace prázdného katalogu je povolena "
                            "jen pro novou hlavní větev. Pro vnořenou větev nejdřív "
                            "aktivuj její rodičovskou větev nebo spusť úplný scan."
                        )
                    result = _activate_first_top_level_branch(
                        connection=connection,
                        scan_id=scan_id,
                        scan_type=scan_type,
                        scope_rel_path=scope_rel_path,
                        declared_folder_count=declared_folder_count,
                        declared_media_count=declared_media_count,
                        started=started,
                        timing=timing,
                        missing_action=missing_action,
                        input_func=input_func,
                        output_func=output_func,
                    )
                else:
                    result = _activate_incremental_catalog(
                        connection=connection,
                        scan_id=scan_id,
                        scan_type=scan_type,
                        scope_rel_path=scope_rel_path,
                        declared_folder_count=declared_folder_count,
                        declared_media_count=declared_media_count,
                        started=started,
                        timing=timing,
                        missing_action=missing_action,
                        input_func=input_func,
                        output_func=output_func,
                    )

                if (
                    favorites_json_path is not None
                    and result.missing_action == "purge"
                    and result.purged_media_path_keys
                ):
                    original_favorites = _read_favorite_entries_from_path(favorites_json_path)
                    kept_favorites = _favorite_entries_without_media_path_keys(
                        original_favorites,
                        set(result.purged_media_path_keys),
                    )
                    favorites_removed = len(original_favorites) - len(kept_favorites)
                    if favorites_removed:
                        _write_favorite_entries_to_path(favorites_json_path, kept_favorites)
                        favorites_written = True
                        result = replace(result, favorites_purged=favorites_removed)
                timing.mark("scan.update.timing.clean_favorites_json")

                connection.commit()
                timing.mark("scan.update.timing.commit_transaction")

            except ScanActivationError:
                connection.rollback()
                if favorites_written and original_favorites is not None and favorites_json_path is not None:
                    _restore_favorite_entries_after_failed_activation(favorites_json_path, original_favorites)
                raise
            except sqlite3.Error as exc:
                connection.rollback()
                if favorites_written and original_favorites is not None and favorites_json_path is not None:
                    _restore_favorite_entries_after_failed_activation(favorites_json_path, original_favorites)
                raise ScanActivationError(
                    f"Databázová aktivace scanu selhala: {exc}"
                ) from exc
            except Exception as exc:
                connection.rollback()
                if favorites_written and original_favorites is not None and favorites_json_path is not None:
                    _restore_favorite_entries_after_failed_activation(favorites_json_path, original_favorites)
                raise ScanActivationError(
                    f"Aktivace scanu selhala: {exc}"
                ) from exc

        _checkpoint_wal(db_path)
        timing.mark("scan.update.timing.wal_checkpoint")

    return replace(
        result,
        duration_seconds=time.monotonic() - started,
        activation_timing=timing.snapshot(),
    )


def _cli_scan_mode_label(mode: ScanActivationMode) -> str:
    return str(mode).replace("_", " ")


def _cli_timing_label(step: dict[str, object]) -> str:
    code = str(step.get("code") or "")
    phase = code.removeprefix("scan.update.timing.").replace("_", " ")
    params = step.get("params")
    values = params if isinstance(params, dict) else {}
    count = values.get("count")
    return f"{phase} ({count})" if count is not None else (phase or "unknown phase")


def scan_activation_result_lines(result: ScanActivationResult) -> list[str]:
    """Return a human-readable activation summary."""
    mode_label = _cli_scan_mode_label(result.mode)
    lines = [
        "Catalog 2.0 – completed scan activation",
        "=" * 70,
        f"activated scan_id: {result.scan_id}",
        f"scan type: {result.scan_type}",
        f"branch: {result.scope_rel_path or '[root]'}",
        f"mode: {mode_label}",
        f"available active folders in scan: {result.folder_count}",
        f"available active media in scan: {result.media_count}",
    ]

    if result.mode in {"incremental_update", "branch_update"}:
        lines.extend([
            "",
            "Folder changes:",
            f"  new: {result.folders_new}",
            f"  restored: {result.folders_restored}",
            f"  newly unavailable: {result.folders_missing_new}",
            f"  unavailable in scan scope total: {result.folders_unavailable_total}",
            f"  removed from database: {result.folders_purged}",
            "",
            "Media changes:",
            f"  new: {result.media_new}",
            f"  changed: {result.media_changed}",
            f"  unchanged: {result.media_unchanged}",
            f"  restored: {result.media_restored}",
            f"  newly unavailable: {result.media_missing_new}",
            f"  unavailable in scan scope total: {result.media_unavailable_total}",
            f"  removed from database: {result.media_purged}",
            f"  removed from favorites: {result.favorites_purged}",
            f"  decision for unavailable items: {result.missing_action}",
        ])

    lines.extend([
        "",
        f"cleaned staged folders: {result.cleaned_staged_folders}",
        f"cleaned staged media: {result.cleaned_staged_media}",
        f"activation time: {result.duration_seconds:.3f} s",
    ])

    if result.activation_timing:
        lines.extend(["", "Timing scan-activate:"])
        for step in result.activation_timing:
            lines.append(f"  {_cli_timing_label(step)}: {float(step['seconds']):.3f} s")

    lines.extend([
        "",
        "Source data was not changed.",
    ])

    return lines


def _activate_first_catalog(
    *,
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
    declared_folder_count: int,
    declared_media_count: int,
    started: float,
    timing: _ActivationTiming | None = None,
) -> ScanActivationResult:
    _insert_active_folders(connection, scan_id)
    _mark_timing(timing, "scan.update.timing.insert_folders")

    _insert_active_media(connection, scan_id)
    _mark_timing(timing, "scan.update.timing.insert_media")

    _calculate_folder_statistics(connection)
    _mark_timing(timing, "scan.update.timing.recalculate_folder_statistics")

    _validate_full_active_catalog(
        connection,
        scan_id=scan_id,
        expected_available_folders=declared_folder_count,
        expected_available_media=declared_media_count,
    )
    _mark_timing(timing, "scan.update.timing.validate_active_catalog")

    cleaned_staged_folders, cleaned_staged_media = _clean_staging_tables(connection, scan_id)
    _mark_timing(timing, "scan.update.timing.clean_staged_tables")

    return ScanActivationResult(
        scan_id=scan_id,
        mode="first_activation",
        scan_type=scan_type,
        scope_rel_path=scope_rel_path,
        folder_count=declared_folder_count,
        media_count=declared_media_count,
        cleaned_staged_folders=cleaned_staged_folders,
        cleaned_staged_media=cleaned_staged_media,
        duration_seconds=time.monotonic() - started,
    )


def _activate_first_top_level_branch(
    *,
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
    declared_folder_count: int,
    declared_media_count: int,
    started: float,
    timing: _ActivationTiming | None = None,
    missing_action: MissingAction | None = None,
    input_func: Callable[[str], str],
    output_func: Callable[[str], None],
) -> ScanActivationResult:
    """Activate a top-level branch into an empty catalog.

    A branch scan does not contain the catalog virtual root row. For a clean
    database this root must be created explicitly so the top-level branch can
    have a valid parent and global statistics remain consistent.
    """
    _ensure_active_catalog_root(connection, scan_id)
    _mark_timing(timing, "scan.update.timing.ensure_catalog_root")

    return _activate_incremental_catalog(
        connection=connection,
        scan_id=scan_id,
        scan_type=scan_type,
        scope_rel_path=scope_rel_path,
        declared_folder_count=declared_folder_count,
        declared_media_count=declared_media_count,
        started=started,
        timing=timing,
        missing_action=missing_action,
        input_func=input_func,
        output_func=output_func,
    )


def _activate_incremental_catalog(
    *,
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
    declared_folder_count: int,
    declared_media_count: int,
    started: float,
    timing: _ActivationTiming | None = None,
    missing_action: MissingAction | None = None,
    input_func: Callable[[str], str],
    output_func: Callable[[str], None],
) -> ScanActivationResult:
    summary = _change_summary(connection, scan_id, scan_type, scope_rel_path)
    _mark_timing(timing, "scan.update.timing.compute_changes")

    affected_scopes = _calculate_affected_scopes(
        connection,
        scan_id=scan_id,
        scan_type=scan_type,
        scope_rel_path=scope_rel_path,
    )
    _mark_timing(timing, "scan.update.timing.compute_affected_scopes")

    action = _resolve_missing_action(
        connection=connection,
        scan_id=scan_id,
        scan_type=scan_type,
        scope_rel_path=scope_rel_path,
        summary=summary,
        missing_action=missing_action,
        input_func=input_func,
        output_func=output_func,
    )
    _mark_timing(timing, "scan.update.timing.missing_items_decision")

    if action == "cancel":
        raise ScanActivationError(
            "Aktivace byla zrušena. Aktivní katalog zůstal beze změny."
        )

    _upsert_active_folders(connection, scan_id)
    _mark_timing(timing, "scan.update.timing.upsert_folders")

    _upsert_active_media(connection, scan_id, timing=timing)

    _touch_scope_ancestors(connection, scan_id, scan_type, scope_rel_path)
    _mark_timing(timing, "scan.update.timing.update_branch_ancestors")

    purged_folders = 0
    purged_media = 0

    purged_media_path_keys: tuple[str, ...] = ()

    if action == "purge":
        purged_media, purged_folders, purged_media_path_keys = _purge_missing_from_database(
            connection,
            scan_id,
            scan_type,
            scope_rel_path,
        )
    else:
        _mark_missing_unavailable(connection, scan_id, scan_type, scope_rel_path)
    _mark_timing(timing, "scan.update.timing.remove_or_mark_missing")

    _calculate_folder_statistics(connection)
    _mark_timing(timing, "scan.update.timing.recalculate_folder_statistics")

    if scan_type == "full":
        _validate_full_active_catalog(
            connection,
            scan_id=scan_id,
            expected_available_folders=declared_folder_count,
            expected_available_media=declared_media_count,
        )
    else:
        _validate_branch_activation(
            connection,
            scan_id=scan_id,
            scope_rel_path=scope_rel_path,
            expected_available_folders=declared_folder_count,
            expected_available_media=declared_media_count,
        )
    _mark_timing(timing, "scan.update.timing.validate_active_catalog")

    unavailable_folders = _scope_unavailable_count(
        connection,
        "folders",
        scan_type,
        scope_rel_path,
    )
    unavailable_media = _scope_unavailable_count(
        connection,
        "media_files",
        scan_type,
        scope_rel_path,
    )
    _mark_timing(timing, "scan.update.timing.unavailable_item_counts")

    cleaned_staged_folders, cleaned_staged_media = _clean_staging_tables(connection, scan_id)
    _mark_timing(timing, "scan.update.timing.clean_staged_tables")

    return ScanActivationResult(
        scan_id=scan_id,
        mode="branch_update" if scan_type == "branch" else "incremental_update",
        scan_type=scan_type,
        scope_rel_path=scope_rel_path,
        folder_count=declared_folder_count,
        media_count=declared_media_count,
        cleaned_staged_folders=cleaned_staged_folders,
        cleaned_staged_media=cleaned_staged_media,
        duration_seconds=time.monotonic() - started,
        folders_new=summary.folders_new,
        folders_restored=summary.folders_restored,
        folders_missing_new=summary.folders_missing_new,
        folders_unavailable_total=unavailable_folders,
        folders_purged=purged_folders,
        media_new=summary.media_new,
        media_restored=summary.media_restored,
        media_changed=summary.media_changed,
        media_unchanged=summary.media_unchanged,
        media_missing_new=summary.media_missing_new,
        media_unavailable_total=unavailable_media,
        media_purged=purged_media,
        missing_action=action if (summary.folders_unavailable_total or summary.media_unavailable_total) else "none",
        purged_media_path_keys=purged_media_path_keys,
        affected_scopes=affected_scopes,
    )


def _scan_by_id(connection: sqlite3.Connection, scan_id: int) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
            id,
            scan_type,
            scope_rel_path,
            scope_path_key,
            status,
            folder_count,
            media_count,
            error_count,
            active_scan_id_at_stage
        FROM scan_sessions
        WHERE id = ?
        """,
        (scan_id,),
    ).fetchone()

    if row is None:
        raise ScanActivationError(f"Scan {scan_id} neexistuje.")

    return row


def _validate_expected_scope(
    scan: sqlite3.Row,
    expected_scope_rel_path: str | None,
) -> None:
    if expected_scope_rel_path is None:
        return

    scan_id = int(scan["id"])
    actual_scope = str(scan["scope_rel_path"])

    if actual_scope != expected_scope_rel_path:
        raise ScanActivationError(
            f"Scan {scan_id} má jiný rozsah: "
            f"uloženo {actual_scope or '[kořen]'}, "
            f"očekáváno {expected_scope_rel_path or '[kořen]'}."
        )


def _validate_completed_scan(
    connection: sqlite3.Connection,
    scan: sqlite3.Row,
) -> None:
    scan_id = int(scan["id"])
    status = str(scan["status"])
    scan_type = str(scan["scan_type"])
    scope_rel_path = str(scan["scope_rel_path"])
    scope_path_key = str(scan["scope_path_key"])

    if status != "completed":
        raise ScanActivationError(
            f"Scan {scan_id} nelze aktivovat: stav je {status!r}, "
            "očekáváno 'completed'."
        )

    if scan_type not in {"full", "branch"}:
        raise ScanActivationError(f"Scan {scan_id} má neplatný typ: {scan_type!r}.")

    if scan_type == "full" and (scope_rel_path or scope_path_key):
        raise ScanActivationError(f"Úplný scan {scan_id} nemá rozsah kořene.")

    if scan_type == "branch" and (not scope_rel_path or not scope_path_key):
        raise ScanActivationError(f"Branch scan {scan_id} nemá platně uloženou větev.")

    declared_errors = int(scan["error_count"])
    staged_errors = _count_for_scan(connection, "scan_errors", scan_id)

    if declared_errors != staged_errors:
        raise ScanActivationError(
            f"Scan {scan_id} má nekonzistentní počet chyb: "
            f"session={declared_errors}, tabulka={staged_errors}."
        )

    if declared_errors != 0:
        raise ScanActivationError(
            f"Scan {scan_id} obsahuje {declared_errors} chyb a nelze jej aktivovat."
        )

    declared_folders = int(scan["folder_count"])
    declared_media = int(scan["media_count"])
    staged_folders = _count_for_scan(connection, "scan_folders", scan_id)
    staged_media = _count_for_scan(connection, "scan_media_files", scan_id)

    if declared_folders != staged_folders:
        raise ScanActivationError(
            f"Scan {scan_id} má nekonzistentní počet složek: "
            f"session={declared_folders}, tabulka={staged_folders}."
        )

    if declared_media != staged_media:
        raise ScanActivationError(
            f"Scan {scan_id} má nekonzistentní počet médií: "
            f"session={declared_media}, tabulka={staged_media}."
        )

    if declared_folders < 1:
        raise ScanActivationError(f"Scan {scan_id} neobsahuje žádnou složku.")

    if scan_type == "full":
        _validate_full_scan_rows(connection, scan_id)
    else:
        _validate_branch_scan_rows(
            connection,
            scan_id=scan_id,
            scope_rel_path=scope_rel_path,
            scope_path_key=scope_path_key,
        )


def _validate_active_catalog_baseline(
    connection: sqlite3.Connection,
    scan: sqlite3.Row,
) -> None:
    scan_id = int(scan["id"])
    expected = scan["active_scan_id_at_stage"]
    current = _current_active_scan_id(connection)

    if expected is None:
        if current is None:
            return
        raise ScanActivationError(
            f"Scan {scan_id} nemá uložený aktivní základ katalogu. "
            "Vznikl před zavedením bezpečnostní kontroly nebo proti prázdnému "
            "katalogu. Spusť nový scan-stage a aktivuj nový scan_id."
        )

    expected_id = int(expected)
    if current != expected_id:
        current_text = str(current) if current is not None else "žádný"
        raise ScanActivationError(
            f"Scan {scan_id} nelze aktivovat: aktivní katalog se od stage změnil. "
            f"Očekávaný aktivní scan_id při stage: {expected_id}, "
            f"aktuální aktivní scan_id: {current_text}. "
            "Spusť novou kontrolu katalogu."
        )


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


def _validate_full_scan_rows(connection: sqlite3.Connection, scan_id: int) -> None:
    root_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM scan_folders
            WHERE scan_id = ?
              AND rel_path = ''
              AND path_key = ''
              AND parent_path_key IS NULL
              AND depth = 0
            """,
            (scan_id,),
        ).fetchone()[0]
    )

    if root_count != 1:
        raise ScanActivationError(
            f"Scan {scan_id} musí obsahovat právě jednu platnou kořenovou složku; "
            f"nalezeno {root_count}."
        )

    invalid_roots = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM scan_folders
            WHERE scan_id = ?
              AND depth = 0
              AND NOT (
                  rel_path = ''
                  AND path_key = ''
                  AND parent_path_key IS NULL
              )
            """,
            (scan_id,),
        ).fetchone()[0]
    )

    if invalid_roots:
        raise ScanActivationError(
            f"Scan {scan_id} obsahuje další neplatné složky s hloubkou 0."
        )

    _validate_staged_parent_links(connection, scan_id, allow_external_scope_parent=False)
    _validate_staged_media_links(connection, scan_id)


def _validate_branch_scan_rows(
    connection: sqlite3.Connection,
    *,
    scan_id: int,
    scope_rel_path: str,
    scope_path_key: str,
) -> None:
    scope_row = connection.execute(
        """
        SELECT rel_path, path_key, parent_path_key, depth
        FROM scan_folders
        WHERE scan_id = ?
          AND path_key = ?
        """,
        (scan_id, scope_path_key),
    ).fetchone()

    if scope_row is None:
        raise ScanActivationError(
            f"Branch scan {scan_id} neobsahuje kořen skenované větve."
        )

    if str(scope_row["rel_path"]) != scope_rel_path or int(scope_row["depth"]) <= 0:
        raise ScanActivationError(f"Branch scan {scan_id} má neplatný kořen větve.")

    parent_key = scope_row["parent_path_key"]
    if parent_key is None:
        raise ScanActivationError(f"Branch scan {scan_id} nemá rodiče větve.")

    if not (_is_top_level_branch_scope(scope_rel_path) and str(parent_key) == ""):
        active_parent = connection.execute(
            """
            SELECT id, is_available
            FROM folders
            WHERE path_key = ?
            """,
            (parent_key,),
        ).fetchone()
        if active_parent is None or int(active_parent["is_available"]) != 1:
            raise ScanActivationError(
                "Rodič skenované větve není dostupný v aktivním katalogu. "
                "Pro vnořenou větev nejdřív aktivuj rodičovskou větev nebo spusť úplný scan."
            )

    outside_scope = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM scan_folders
            WHERE scan_id = ?
              AND NOT (rel_path = ? OR rel_path LIKE ?)
            """,
            (scan_id, scope_rel_path, f"{scope_rel_path}/%"),
        ).fetchone()[0]
    )
    if outside_scope:
        raise ScanActivationError(
            f"Branch scan {scan_id} obsahuje {outside_scope} složek mimo skenovanou větev."
        )

    _validate_staged_parent_links(connection, scan_id, allow_external_scope_parent=True)
    _validate_staged_media_links(connection, scan_id)


def _validate_staged_parent_links(
    connection: sqlite3.Connection,
    scan_id: int,
    *,
    allow_external_scope_parent: bool,
) -> None:
    if allow_external_scope_parent:
        missing_parents = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM scan_folders AS child
                LEFT JOIN scan_folders AS parent
                  ON parent.scan_id = child.scan_id
                 AND parent.path_key = child.parent_path_key
                LEFT JOIN folders AS active_parent
                  ON active_parent.path_key = child.parent_path_key
                WHERE child.scan_id = ?
                  AND child.depth > 0
                  AND child.parent_path_key <> ''
                  AND (
                      (parent.path_key IS NULL AND active_parent.path_key IS NULL)
                      OR
                      (parent.path_key IS NOT NULL AND parent.depth <> child.depth - 1)
                  )
                """,
                (scan_id,),
            ).fetchone()[0]
        )
    else:
        missing_parents = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM scan_folders AS child
                LEFT JOIN scan_folders AS parent
                  ON parent.scan_id = child.scan_id
                 AND parent.path_key = child.parent_path_key
                WHERE child.scan_id = ?
                  AND child.depth > 0
                  AND (
                      parent.path_key IS NULL
                      OR parent.depth <> child.depth - 1
                  )
                """,
                (scan_id,),
            ).fetchone()[0]
        )

    if missing_parents:
        raise ScanActivationError(
            f"Scan {scan_id} obsahuje {missing_parents} složek bez platného rodiče."
        )


def _validate_staged_media_links(connection: sqlite3.Connection, scan_id: int) -> None:
    missing_media_folders = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM scan_media_files AS media
            LEFT JOIN scan_folders AS folder
              ON folder.scan_id = media.scan_id
             AND folder.path_key = media.folder_path_key
            WHERE media.scan_id = ?
              AND folder.path_key IS NULL
            """,
            (scan_id,),
        ).fetchone()[0]
    )

    if missing_media_folders:
        raise ScanActivationError(
            f"Scan {scan_id} obsahuje {missing_media_folders} médií bez platné složky."
        )


def _change_summary(
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
) -> _ChangeSummary:
    folders_new = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM scan_folders AS staged
        LEFT JOIN folders AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.id IS NULL
        """,
        (scan_id,),
    ).fetchone()[0])

    folders_restored = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM scan_folders AS staged
        JOIN folders AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 0
        """,
        (scan_id,),
    ).fetchone()[0])

    folder_scope_sql, folder_scope_params = _scope_filter_sql(
        "active",
        scan_type,
        scope_rel_path,
    )
    media_scope_sql, media_scope_params = _scope_filter_sql(
        "active",
        scan_type,
        scope_rel_path,
    )

    folders_missing_new = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM folders AS active
        LEFT JOIN scan_folders AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {folder_scope_sql}
          AND staged.path_key IS NULL
          AND active.is_available = 1
        """,
        (scan_id, *folder_scope_params),
    ).fetchone()[0])

    folders_unavailable_total = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM folders AS active
        LEFT JOIN scan_folders AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {folder_scope_sql}
          AND staged.path_key IS NULL
        """,
        (scan_id, *folder_scope_params),
    ).fetchone()[0])

    media_new = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM scan_media_files AS staged
        LEFT JOIN media_files AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.id IS NULL
        """,
        (scan_id,),
    ).fetchone()[0])

    media_restored = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM scan_media_files AS staged
        JOIN media_files AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 0
        """,
        (scan_id,),
    ).fetchone()[0])

    media_changed = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM scan_media_files AS staged
        JOIN media_files AS active
          ON active.path_key = staged.path_key
        JOIN folders AS folder
          ON folder.path_key = staged.folder_path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 1
          AND (
              active.folder_id <> folder.id
              OR active.file_name <> staged.file_name
              OR active.extension <> staged.extension
              OR active.media_type <> staged.media_type
              OR active.size_bytes <> staged.size_bytes
              OR active.modified_time <> staged.modified_time
              OR active.sort_key <> staged.sort_key
          )
        """,
        (scan_id,),
    ).fetchone()[0])

    media_unchanged = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM scan_media_files AS staged
        JOIN media_files AS active
          ON active.path_key = staged.path_key
        JOIN folders AS folder
          ON folder.path_key = staged.folder_path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 1
          AND active.folder_id = folder.id
          AND active.file_name = staged.file_name
          AND active.extension = staged.extension
          AND active.media_type = staged.media_type
          AND active.size_bytes = staged.size_bytes
          AND active.modified_time = staged.modified_time
          AND active.sort_key = staged.sort_key
        """,
        (scan_id,),
    ).fetchone()[0])

    media_missing_new = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM media_files AS active
        LEFT JOIN scan_media_files AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {media_scope_sql}
          AND staged.path_key IS NULL
          AND active.is_available = 1
        """,
        (scan_id, *media_scope_params),
    ).fetchone()[0])

    media_unavailable_total = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM media_files AS active
        LEFT JOIN scan_media_files AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {media_scope_sql}
          AND staged.path_key IS NULL
        """,
        (scan_id, *media_scope_params),
    ).fetchone()[0])

    return _ChangeSummary(
        folders_new=folders_new,
        folders_restored=folders_restored,
        folders_missing_new=folders_missing_new,
        folders_unavailable_total=folders_unavailable_total,
        media_new=media_new,
        media_restored=media_restored,
        media_changed=media_changed,
        media_unchanged=media_unchanged,
        media_missing_new=media_missing_new,
        media_unavailable_total=media_unavailable_total,
    )


_AFFECTED_SCOPE_SAMPLE_LIMIT = 20


def _calculate_affected_scopes(
    connection: sqlite3.Connection,
    *,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
) -> dict[str, object]:
    """Return diagnostic affected-scope groups for future update orchestration.

    This function does not execute preview/cache work. It only describes which
    folder scopes were touched by the staged delta so a later orchestrator can
    decide where media and folder-preview maintenance should run.
    """
    folder_scope_sql, folder_scope_params = _scope_filter_sql(
        "active",
        scan_type,
        scope_rel_path,
    )
    media_scope_sql, media_scope_params = _scope_filter_sql(
        "active",
        scan_type,
        scope_rel_path,
    )

    folders_new = _distinct_rel_paths(
        connection,
        """
        SELECT staged.rel_path
        FROM scan_folders AS staged
        LEFT JOIN folders AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.id IS NULL
        """,
        (scan_id,),
    )
    folders_restored = _distinct_rel_paths(
        connection,
        """
        SELECT staged.rel_path
        FROM scan_folders AS staged
        JOIN folders AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 0
        """,
        (scan_id,),
    )
    folders_missing = _distinct_rel_paths(
        connection,
        f"""
        SELECT active.rel_path
        FROM folders AS active
        LEFT JOIN scan_folders AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {folder_scope_sql}
          AND staged.path_key IS NULL
          AND active.is_available = 1
        """,
        (scan_id, *folder_scope_params),
    )

    media_new_folders = _distinct_rel_paths(
        connection,
        """
        SELECT folder.rel_path
        FROM scan_media_files AS staged
        JOIN scan_folders AS folder
          ON folder.scan_id = staged.scan_id
         AND folder.path_key = staged.folder_path_key
        LEFT JOIN media_files AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.id IS NULL
        """,
        (scan_id,),
    )
    media_restored_folders = _distinct_rel_paths(
        connection,
        """
        SELECT folder.rel_path
        FROM scan_media_files AS staged
        JOIN scan_folders AS folder
          ON folder.scan_id = staged.scan_id
         AND folder.path_key = staged.folder_path_key
        JOIN media_files AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 0
        """,
        (scan_id,),
    )
    media_changed_folders = _distinct_rel_paths(
        connection,
        """
        SELECT folder.rel_path
        FROM scan_media_files AS staged
        JOIN scan_folders AS staged_folder
          ON staged_folder.scan_id = staged.scan_id
         AND staged_folder.path_key = staged.folder_path_key
        JOIN media_files AS active
          ON active.path_key = staged.path_key
        JOIN folders AS folder
          ON folder.path_key = staged.folder_path_key
        WHERE staged.scan_id = ?
          AND active.is_available = 1
          AND (
              active.folder_id <> folder.id
              OR active.file_name <> staged.file_name
              OR active.extension <> staged.extension
              OR active.media_type <> staged.media_type
              OR active.size_bytes <> staged.size_bytes
              OR active.modified_time <> staged.modified_time
              OR active.sort_key <> staged.sort_key
          )
        """,
        (scan_id,),
    )
    media_missing_folders = _distinct_rel_paths(
        connection,
        f"""
        SELECT folder.rel_path
        FROM media_files AS active
        JOIN folders AS folder
          ON folder.id = active.folder_id
        LEFT JOIN scan_media_files AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {media_scope_sql}
          AND staged.path_key IS NULL
          AND active.is_available = 1
        """,
        (scan_id, *media_scope_params),
    )

    groups = [
        _affected_group("folders_new", folders_new),
        _affected_group("folders_restored", folders_restored),
        _affected_group("folders_missing", folders_missing),
        _affected_group("media_new_parent_folders", media_new_folders),
        _affected_group("media_restored_parent_folders", media_restored_folders),
        _affected_group("media_changed_parent_folders", media_changed_folders),
        _affected_group("media_missing_parent_folders", media_missing_folders),
    ]

    direct_folder_rels = _sorted_unique(
        rel
        for group in (
            folders_new,
            folders_restored,
            folders_missing,
            media_new_folders,
            media_restored_folders,
            media_changed_folders,
            media_missing_folders,
        )
        for rel in group
    )
    removed_folder_rels = _sorted_unique(folders_missing)

    ancestor_rels = _sorted_unique(
        ancestor
        for rel in direct_folder_rels
        for ancestor in _path_self_and_ancestors(rel)
    )
    existing_direct_rels = [
        rel for rel in direct_folder_rels
        if rel not in set(removed_folder_rels)
    ]
    preview_candidate_rels = _sorted_unique([*existing_direct_rels, *ancestor_rels])

    return {
        "affected_scopes": True,
        "version": 1,
        "scope": {
            "type": "branch" if scan_type == "branch" else "full_catalog",
            "rel_path": scope_rel_path,
        },
        "has_affected_scopes": bool(direct_folder_rels),
        "summary": {
            "direct_folder_count": len(direct_folder_rels),
            "ancestor_folder_count": len(ancestor_rels),
            "preview_candidate_folder_count": len(preview_candidate_rels),
            "removed_folder_count": len(removed_folder_rels),
        },
        "groups": groups,
        "derived": {
            "direct_folder_rels": _affected_group(
                "direct_folder_rels",
                direct_folder_rels,
            ),
            "ancestor_folder_rels": _affected_group(
                "ancestor_folder_rels",
                ancestor_rels,
            ),
            "preview_candidate_folder_rels": _affected_group(
                "preview_candidate_folder_rels",
                preview_candidate_rels,
            ),
            "removed_folder_rels": _affected_group(
                "removed_folder_rels",
                removed_folder_rels,
            ),
        },
    }


def _distinct_rel_paths(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> list[str]:
    return _sorted_unique(str(row[0] or "") for row in connection.execute(sql, params))


def _affected_group(key: str, rels: list[str]) -> dict[str, object]:
    unique = _sorted_unique(rels)
    return {
        "key": key,
        "count": len(unique),
        "samples": unique[:_AFFECTED_SCOPE_SAMPLE_LIMIT],
        "rels": unique,
        "sample_limit": _AFFECTED_SCOPE_SAMPLE_LIMIT,
        "truncated": len(unique) > _AFFECTED_SCOPE_SAMPLE_LIMIT,
    }


def _sorted_unique(rels: Iterable[str]) -> list[str]:
    return sorted(set(rels), key=catalog_path_key)


def _path_self_and_ancestors(rel_path: str) -> list[str]:
    if not rel_path:
        return [""]

    parts = list(PurePosixPath(rel_path).parts)
    result = [""]
    for index in range(1, len(parts) + 1):
        result.append("/".join(parts[:index]))
    return result


def _resolve_missing_action(
    *,
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
    summary: _ChangeSummary,
    missing_action: MissingAction | None,
    input_func: Callable[[str], str],
    output_func: Callable[[str], None],
) -> MissingAction:
    if summary.folders_unavailable_total == 0 and summary.media_unavailable_total == 0:
        return "keep"

    if missing_action == "require_decision":
        raise ScanActivationDecisionRequired(
            _missing_decision_summary(
                connection=connection,
                scan_id=scan_id,
                scan_type=scan_type,
                scope_rel_path=scope_rel_path,
                summary=summary,
            )
        )

    _print_missing_summary(
        connection,
        scan_id,
        scan_type,
        scope_rel_path,
        summary,
        output_func,
    )

    if missing_action is not None:
        if missing_action not in {"keep", "purge", "cancel"}:
            raise ScanActivationError(
                f"Neplatná volba pro nedostupné položky: {missing_action}"
            )
        return missing_action

    while True:
        choice = input_func(
            "Volba pro nedostupné položky "
            "[1 ponechat / 2 odstranit z databáze / 3 zrušit aktivaci]: "
        ).strip().lower()

        if choice in {"1", "p", "ponechat", "keep"}:
            return "keep"
        if choice in {"2", "o", "odstranit", "smazat", "purge"}:
            return "purge"
        if choice in {"3", "z", "zrusit", "zrušit", "cancel"}:
            return "cancel"

        output_func("Neplatná volba. Zadej 1, 2 nebo 3.")


def _missing_decision_summary(
    *,
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
    summary: _ChangeSummary,
) -> dict:
    """Return a serializable summary for UI decision handling."""
    folder_scope_sql, folder_scope_params = _scope_filter_sql("active", scan_type, scope_rel_path)
    missing_folders = connection.execute(
        f"""
        SELECT active.rel_path
        FROM folders AS active
        LEFT JOIN scan_folders AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {folder_scope_sql}
          AND staged.path_key IS NULL
        ORDER BY active.path_key
        LIMIT 30
        """,
        (scan_id, *folder_scope_params),
    ).fetchall()

    media_scope_sql, media_scope_params = _scope_filter_sql("active", scan_type, scope_rel_path)
    missing_media = connection.execute(
        f"""
        SELECT active.rel_path
        FROM media_files AS active
        LEFT JOIN scan_media_files AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {media_scope_sql}
          AND staged.path_key IS NULL
        ORDER BY active.path_key
        LIMIT 30
        """,
        (scan_id, *media_scope_params),
    ).fetchall()

    return {
        "requires_decision": True,
        "scan_id": scan_id,
        "scan_type": scan_type,
        "branch": scope_rel_path,
        "missing": {
            "folders_new": summary.folders_missing_new,
            "folders_total": summary.folders_unavailable_total,
            "media_new": summary.media_missing_new,
            "media_total": summary.media_unavailable_total,
        },
        "sample": {
            "folders": [str(row["rel_path"]) for row in missing_folders],
            "media": [str(row["rel_path"]) for row in missing_media],
            "limit": 30,
        },
        "options": ["keep", "purge", "cancel"],
        "writes": {
            "catalog_db": "none",
            "active_catalog": False,
            "source_media": False,
        },
    }


def _print_missing_summary(
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
    summary: _ChangeSummary,
    output_func: Callable[[str], None],
) -> None:
    output_func("")
    output_func("Nalezeny položky, které nejsou v aktuálním scanu.")
    output_func(f"Rozsah: {scope_rel_path or '[kořen]'}")
    output_func(f"Nově nedostupné složky: {summary.folders_missing_new}")
    output_func(f"Nově nedostupná média: {summary.media_missing_new}")
    output_func(f"Nedostupné složky v rozsahu scanu celkem: {summary.folders_unavailable_total}")
    output_func(f"Nedostupná média v rozsahu scanu celkem: {summary.media_unavailable_total}")

    scope_sql, scope_params = _scope_filter_sql("active", scan_type, scope_rel_path)
    rows = connection.execute(
        f"""
        SELECT active.rel_path
        FROM media_files AS active
        LEFT JOIN scan_media_files AS staged
          ON staged.scan_id = ?
         AND staged.path_key = active.path_key
        WHERE {scope_sql}
          AND staged.path_key IS NULL
        ORDER BY active.path_key
        LIMIT 30
        """,
        (scan_id, *scope_params),
    ).fetchall()

    if rows:
        output_func("")
        output_func("Prvních 30 nedostupných médií v rozsahu scanu:")
        for row in rows:
            output_func(f"  {row['rel_path']}")

    output_func("")
    output_func("Zdrojová data se nebudou mazat. Volba 2 odstraní pouze záznamy z databáze.")


def _is_top_level_branch_scope(scope_rel_path: str) -> bool:
    return bool(scope_rel_path) and "/" not in scope_rel_path


def _ensure_active_catalog_root(connection: sqlite3.Connection, scan_id: int) -> None:
    root = connection.execute(
        """
        SELECT id, is_available
        FROM folders
        WHERE path_key = ''
        """
    ).fetchone()

    if root is None:
        connection.execute(
            """
            INSERT INTO folders (
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                sort_key,
                last_successful_scan_id,
                is_available
            )
            VALUES ('', '', NULL, '[kořen]', 0, '', ?, 1)
            """,
            (scan_id,),
        )
        return

    if int(root["is_available"]) != 1:
        connection.execute(
            """
            UPDATE folders
            SET
                parent_id = NULL,
                name = '[kořen]',
                depth = 0,
                sort_key = '',
                last_successful_scan_id = ?,
                is_available = 1
            WHERE path_key = ''
            """,
            (scan_id,),
        )


def _insert_active_folders(connection: sqlite3.Connection, scan_id: int) -> None:
    connection.execute(
        """
        INSERT INTO folders (
            rel_path,
            path_key,
            parent_id,
            name,
            depth,
            sort_key,
            last_successful_scan_id,
            is_available
        )
        SELECT
            rel_path,
            path_key,
            NULL,
            name,
            depth,
            sort_key,
            ?,
            1
        FROM scan_folders
        WHERE scan_id = ?
          AND depth = 0
        """,
        (scan_id, scan_id),
    )

    max_depth = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(depth), 0)
            FROM scan_folders
            WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()[0]
    )

    for depth in range(1, max_depth + 1):
        connection.execute(
            """
            INSERT INTO folders (
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                sort_key,
                last_successful_scan_id,
                is_available
            )
            SELECT
                child.rel_path,
                child.path_key,
                parent.id,
                child.name,
                child.depth,
                child.sort_key,
                ?,
                1
            FROM scan_folders AS child
            JOIN folders AS parent
              ON parent.path_key = child.parent_path_key
            WHERE child.scan_id = ?
              AND child.depth = ?
            ORDER BY child.sort_key, child.path_key
            """,
            (scan_id, scan_id, depth),
        )


def _upsert_active_folders(connection: sqlite3.Connection, scan_id: int) -> None:
    max_depth = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(depth), 0)
            FROM scan_folders
            WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()[0]
    )

    for depth in range(0, max_depth + 1):
        connection.execute(
            """
            INSERT INTO folders (
                rel_path,
                path_key,
                parent_id,
                name,
                depth,
                sort_key,
                last_successful_scan_id,
                is_available
            )
            SELECT
                child.rel_path,
                child.path_key,
                parent.id,
                child.name,
                child.depth,
                child.sort_key,
                ?,
                1
            FROM scan_folders AS child
            LEFT JOIN folders AS parent
              ON parent.path_key = child.parent_path_key
            LEFT JOIN folders AS active
              ON active.path_key = child.path_key
            WHERE child.scan_id = ?
              AND child.depth = ?
              AND active.id IS NULL
              AND (
                  child.depth = 0
                  OR parent.id IS NOT NULL
              )
            ORDER BY child.sort_key, child.path_key
            """,
            (scan_id, scan_id, depth),
        )

    connection.execute(
        """
        UPDATE folders
        SET
            rel_path = (
                SELECT staged.rel_path
                FROM scan_folders AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = folders.path_key
            ),
            name = (
                SELECT staged.name
                FROM scan_folders AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = folders.path_key
            ),
            depth = (
                SELECT staged.depth
                FROM scan_folders AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = folders.path_key
            ),
            sort_key = (
                SELECT staged.sort_key
                FROM scan_folders AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = folders.path_key
            ),
            last_successful_scan_id = ?,
            is_available = 1
        WHERE path_key IN (
            SELECT path_key
            FROM scan_folders
            WHERE scan_id = ?
        )
        """,
        (scan_id, scan_id, scan_id, scan_id, scan_id, scan_id),
    )

    connection.execute(
        """
        UPDATE folders
        SET parent_id = NULL
        WHERE path_key = ''
        """
    )

    connection.execute(
        """
        UPDATE folders
        SET parent_id = (
            SELECT parent.id
            FROM scan_folders AS staged
            JOIN folders AS parent
              ON parent.path_key = staged.parent_path_key
            WHERE staged.scan_id = ?
              AND staged.path_key = folders.path_key
        )
        WHERE path_key IN (
            SELECT path_key
            FROM scan_folders
            WHERE scan_id = ?
              AND depth > 0
        )
        """,
        (scan_id, scan_id),
    )


def _insert_active_media(connection: sqlite3.Connection, scan_id: int) -> None:
    connection.execute(
        """
        INSERT INTO media_files (
            rel_path,
            path_key,
            folder_id,
            file_name,
            extension,
            media_type,
            size_bytes,
            modified_time,
            sort_key,
            last_successful_scan_id,
            is_available
        )
        SELECT
            media.rel_path,
            media.path_key,
            folder.id,
            media.file_name,
            media.extension,
            media.media_type,
            media.size_bytes,
            media.modified_time,
            media.sort_key,
            ?,
            1
        FROM scan_media_files AS media
        JOIN folders AS folder
          ON folder.path_key = media.folder_path_key
        WHERE media.scan_id = ?
        ORDER BY folder.id, media.sort_key, media.path_key
        """,
        (scan_id, scan_id),
    )


def _upsert_active_media(
    connection: sqlite3.Connection,
    scan_id: int,
    timing: _ActivationTiming | None = None,
) -> None:
    group_started = time.monotonic()

    _invalidate_changed_derived_rows(connection, scan_id, timing=timing)

    changed_before = connection.total_changes
    connection.execute(
        """
        INSERT INTO media_files (
            rel_path,
            path_key,
            folder_id,
            file_name,
            extension,
            media_type,
            size_bytes,
            modified_time,
            sort_key,
            last_successful_scan_id,
            is_available
        )
        SELECT
            staged.rel_path,
            staged.path_key,
            folder.id,
            staged.file_name,
            staged.extension,
            staged.media_type,
            staged.size_bytes,
            staged.modified_time,
            staged.sort_key,
            ?,
            1
        FROM scan_media_files AS staged
        JOIN folders AS folder
          ON folder.path_key = staged.folder_path_key
        LEFT JOIN media_files AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND active.id IS NULL
        ORDER BY folder.id, staged.sort_key, staged.path_key
        """,
        (scan_id, scan_id),
    )
    inserted_rows = connection.total_changes - changed_before
    _mark_timing(
        timing,
        "scan.update.timing.media_upsert_insert_new_rows",
        {"count": inserted_rows},
    )

    changed_before = connection.total_changes
    connection.execute(
        """
        UPDATE media_files
        SET
            rel_path = (
                SELECT staged.rel_path
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            folder_id = (
                SELECT folder.id
                FROM scan_media_files AS staged
                JOIN folders AS folder
                  ON folder.path_key = staged.folder_path_key
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            file_name = (
                SELECT staged.file_name
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            extension = (
                SELECT staged.extension
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            media_type = (
                SELECT staged.media_type
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            size_bytes = (
                SELECT staged.size_bytes
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            modified_time = (
                SELECT staged.modified_time
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            sort_key = (
                SELECT staged.sort_key
                FROM scan_media_files AS staged
                WHERE staged.scan_id = ?
                  AND staged.path_key = media_files.path_key
            ),
            last_successful_scan_id = ?,
            is_available = 1
        WHERE path_key IN (
            SELECT staged.path_key
            FROM scan_media_files AS staged
            JOIN media_files AS active
              ON active.path_key = staged.path_key
            JOIN folders AS folder
              ON folder.path_key = staged.folder_path_key
            WHERE staged.scan_id = ?
              AND (
                  active.is_available = 0
                  OR active.rel_path <> staged.rel_path
                  OR active.folder_id <> folder.id
                  OR active.file_name <> staged.file_name
                  OR active.extension <> staged.extension
                  OR active.media_type <> staged.media_type
                  OR active.size_bytes <> staged.size_bytes
                  OR active.modified_time <> staged.modified_time
                  OR active.sort_key <> staged.sort_key
              )
        )
        """,
        (
            scan_id,
            scan_id,
            scan_id,
            scan_id,
            scan_id,
            scan_id,
            scan_id,
            scan_id,
            scan_id,
            scan_id,
        ),
    )
    updated_rows = connection.total_changes - changed_before
    _mark_timing(
        timing,
        "scan.update.timing.media_upsert_update_changed_or_restored_rows",
        {"count": updated_rows},
    )
    _add_timing_measure(
        timing,
        "scan.update.timing.media_upsert_total",
        time.monotonic() - group_started,
    )


def _touch_scope_ancestors(
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
) -> None:
    if scan_type == "full":
        return

    ancestors = _ancestor_paths(scope_rel_path)
    if not ancestors:
        return

    placeholders = ",".join("?" for _ in ancestors)
    connection.execute(
        f"""
        UPDATE folders
        SET last_successful_scan_id = ?
        WHERE path_key IN ({placeholders})
          AND is_available = 1
        """,
        (scan_id, *ancestors),
    )


def _invalidate_changed_derived_rows(
    connection: sqlite3.Connection,
    scan_id: int,
    timing: _ActivationTiming | None = None,
) -> None:
    """Invalidate derived media rows only for media whose source actually changed.

    Před 8.1u běžel UPDATE nad tabulkou thumbnails i tehdy, když scan neobsahoval
    žádná změněná / obnovená média. U větší cache to znamenalo drahý průchod bez
    změněných řádků. Tady se nejdřív vytvoří malý delta seznam médií, jejichž
    velikost nebo modified_time se proti aktivní DB opravdu liší. Pokud je prázdný,
    thumbnail/video-preview invalidace se úplně přeskočí.
    """
    connection.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS catalog2_changed_derived_media (
            media_id INTEGER PRIMARY KEY,
            source_size_bytes INTEGER NOT NULL,
            source_modified_time REAL NOT NULL
        )
        """
    )
    connection.execute("DELETE FROM catalog2_changed_derived_media")

    changed_before = connection.total_changes
    connection.execute(
        """
        INSERT INTO catalog2_changed_derived_media (
            media_id,
            source_size_bytes,
            source_modified_time
        )
        SELECT
            active.id,
            staged.size_bytes,
            staged.modified_time
        FROM scan_media_files AS staged
        JOIN media_files AS active
          ON active.path_key = staged.path_key
        WHERE staged.scan_id = ?
          AND (
              active.size_bytes <> staged.size_bytes
              OR active.modified_time <> staged.modified_time
          )
        """,
        (scan_id,),
    )
    changed_media_rows = connection.total_changes - changed_before
    _mark_timing(
        timing,
        "scan.update.timing.media_upsert_changed_for_derived_rows",
        {"count": changed_media_rows},
    )

    if changed_media_rows <= 0:
        _mark_timing(
            timing,
            "scan.update.timing.media_upsert_invalidate_thumbnail_rows",
            {"count": 0},
        )
        _mark_timing(
            timing,
            "scan.update.timing.media_upsert_delete_video_preview_rows",
            {"count": 0},
        )
        return

    now = time.time()

    changed_before = connection.total_changes
    connection.execute(
        """
        UPDATE thumbnails
        SET status = 'stale',
            updated_at = ?,
            error_message = NULL
        WHERE EXISTS (
            SELECT 1
            FROM catalog2_changed_derived_media AS changed
            WHERE changed.media_id = thumbnails.media_id
              AND (
                  thumbnails.source_size_bytes <> changed.source_size_bytes
                  OR thumbnails.source_modified_time <> changed.source_modified_time
              )
        )
        """,
        (now,),
    )
    stale_thumbnail_rows = connection.total_changes - changed_before
    _mark_timing(
        timing,
        "scan.update.timing.media_upsert_invalidate_thumbnail_rows",
        {"count": stale_thumbnail_rows},
    )

    changed_before = connection.total_changes
    connection.execute(
        """
        DELETE FROM video_previews
        WHERE EXISTS (
            SELECT 1
            FROM catalog2_changed_derived_media AS changed
            WHERE changed.media_id = video_previews.media_id
              AND (
                  video_previews.source_size_bytes <> changed.source_size_bytes
                  OR video_previews.source_modified_time <> changed.source_modified_time
              )
        )
        """,
    )
    deleted_video_preview_rows = connection.total_changes - changed_before
    _mark_timing(
        timing,
        "scan.update.timing.media_upsert_delete_video_preview_rows",
        {"count": deleted_video_preview_rows},
    )


def _mark_missing_unavailable(
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
) -> None:
    media_scope_sql, media_scope_params = _scope_filter_sql(
        "media_files",
        scan_type,
        scope_rel_path,
    )
    now = time.time()
    connection.execute(
        f"""
        UPDATE thumbnails
        SET status = 'missing',
            updated_at = ?,
            error_message = NULL
        WHERE media_id IN (
            SELECT id
            FROM media_files
            WHERE {media_scope_sql}
              AND path_key NOT IN (
                  SELECT path_key
                  FROM scan_media_files
                  WHERE scan_id = ?
              )
        )
        """,
        (now, *media_scope_params, scan_id),
    )
    connection.execute(
        f"""
        UPDATE media_files
        SET is_available = 0
        WHERE {media_scope_sql}
          AND path_key NOT IN (
              SELECT path_key
              FROM scan_media_files
              WHERE scan_id = ?
          )
        """,
        (*media_scope_params, scan_id),
    )

    folder_scope_sql, folder_scope_params = _scope_filter_sql(
        "folders",
        scan_type,
        scope_rel_path,
    )
    connection.execute(
        f"""
        UPDATE folders
        SET is_available = 0
        WHERE {folder_scope_sql}
          AND path_key NOT IN (
              SELECT path_key
              FROM scan_folders
              WHERE scan_id = ?
          )
        """,
        (*folder_scope_params, scan_id),
    )


def _purge_missing_from_database(
    connection: sqlite3.Connection,
    scan_id: int,
    scan_type: str,
    scope_rel_path: str,
) -> tuple[int, int, tuple[str, ...]]:
    media_scope_sql, media_scope_params = _scope_filter_sql(
        "media_files",
        scan_type,
        scope_rel_path,
    )
    folder_scope_sql, folder_scope_params = _scope_filter_sql(
        "folders",
        scan_type,
        scope_rel_path,
    )

    media_rows_to_delete = connection.execute(
        f"""
        SELECT path_key
        FROM media_files
        WHERE {media_scope_sql}
          AND path_key NOT IN (
              SELECT path_key
              FROM scan_media_files
              WHERE scan_id = ?
          )
        ORDER BY path_key
        """,
        (*media_scope_params, scan_id),
    ).fetchall()
    media_path_keys_to_delete = tuple(str(row["path_key"]) for row in media_rows_to_delete)
    media_to_delete = len(media_path_keys_to_delete)

    folder_to_delete = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM folders
        WHERE {folder_scope_sql}
          AND path_key NOT IN (
              SELECT path_key
              FROM scan_folders
              WHERE scan_id = ?
          )
        """,
        (*folder_scope_params, scan_id),
    ).fetchone()[0])

    connection.execute(
        f"""
        DELETE FROM media_files
        WHERE {media_scope_sql}
          AND path_key NOT IN (
              SELECT path_key
              FROM scan_media_files
              WHERE scan_id = ?
          )
        """,
        (*media_scope_params, scan_id),
    )
    connection.execute(
        f"""
        DELETE FROM folders
        WHERE {folder_scope_sql}
          AND path_key NOT IN (
              SELECT path_key
              FROM scan_folders
              WHERE scan_id = ?
          )
        """,
        (*folder_scope_params, scan_id),
    )
    return media_to_delete, folder_to_delete, media_path_keys_to_delete


def _read_favorite_entries_from_path(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if not path.is_file():
        raise ScanActivationError(f"favorites.json není soubor: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScanActivationError(f"favorites.json nelze přečíst jako platný JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ScanActivationError("favorites.json musí být JSON objekt.")

    raw_entries = payload.get("favorites", [])
    if not isinstance(raw_entries, list):
        raise ScanActivationError("favorites.json položka favorites musí být seznam.")

    entries: list[dict[str, str]] = []
    seen_path_keys: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ScanActivationError("favorites.json obsahuje neplatnou položku.")

        raw_path = raw_entry.get("path")
        if not isinstance(raw_path, str):
            raise ScanActivationError("favorites.json položka path musí být text.")

        rel_path = normalize_catalog_relative_path(raw_path, allow_root=False)
        path_key = catalog_path_key(rel_path)
        if path_key in seen_path_keys:
            continue

        added_at = raw_entry.get("added_at")
        if not isinstance(added_at, str) or not added_at:
            added_at = ""

        entries.append({"path": rel_path, "added_at": added_at})
        seen_path_keys.add(path_key)

    return entries


def _favorite_entries_without_media_path_keys(
    entries: list[dict[str, str]],
    media_path_keys: set[str],
) -> list[dict[str, str]]:
    if not media_path_keys:
        return entries
    return [
        entry
        for entry in entries
        if catalog_path_key(entry["path"]) not in media_path_keys
    ]


def _write_favorite_entries_to_path(path: Path, entries: list[dict[str, str]]) -> None:
    payload = {
        "version": 1,
        "favorites": entries,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ScanActivationError(f"favorites.json se nepodařilo zapsat: {exc}") from exc


def _restore_favorite_entries_after_failed_activation(
    path: Path,
    entries: list[dict[str, str]],
) -> None:
    try:
        _write_favorite_entries_to_path(path, entries)
    except Exception:
        pass


def _calculate_folder_statistics(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS activation_child_counts")
    connection.execute("DROP TABLE IF EXISTS activation_media_counts")

    connection.execute(
        """
        CREATE TEMP TABLE activation_child_counts (
            folder_id INTEGER PRIMARY KEY,
            child_count INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO activation_child_counts (folder_id, child_count)
        SELECT parent_id, COUNT(*)
        FROM folders
        WHERE parent_id IS NOT NULL
          AND is_available = 1
        GROUP BY parent_id
        """
    )

    connection.execute(
        """
        CREATE TEMP TABLE activation_media_counts (
            folder_id INTEGER PRIMARY KEY,
            image_count INTEGER NOT NULL,
            gif_count INTEGER NOT NULL,
            video_count INTEGER NOT NULL,
            other_count INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO activation_media_counts (
            folder_id,
            image_count,
            gif_count,
            video_count,
            other_count
        )
        SELECT
            folder_id,
            SUM(CASE WHEN media_type = 'image' THEN 1 ELSE 0 END),
            SUM(CASE WHEN media_type = 'gif' THEN 1 ELSE 0 END),
            SUM(CASE WHEN media_type = 'video' THEN 1 ELSE 0 END),
            SUM(CASE WHEN media_type = 'other' THEN 1 ELSE 0 END)
        FROM media_files
        WHERE is_available = 1
        GROUP BY folder_id
        """
    )

    connection.execute(
        """
        UPDATE folders
        SET
            direct_child_count = COALESCE((
                SELECT child_count
                FROM activation_child_counts
                WHERE folder_id = folders.id
            ), 0),
            direct_image_count = COALESCE((
                SELECT image_count
                FROM activation_media_counts
                WHERE folder_id = folders.id
            ), 0),
            direct_gif_count = COALESCE((
                SELECT gif_count
                FROM activation_media_counts
                WHERE folder_id = folders.id
            ), 0),
            direct_video_count = COALESCE((
                SELECT video_count
                FROM activation_media_counts
                WHERE folder_id = folders.id
            ), 0),
            direct_other_count = COALESCE((
                SELECT other_count
                FROM activation_media_counts
                WHERE folder_id = folders.id
            ), 0)
        """
    )

    connection.execute(
        """
        UPDATE folders
        SET
            recursive_folder_count = direct_child_count,
            recursive_image_count = direct_image_count,
            recursive_gif_count = direct_gif_count,
            recursive_video_count = direct_video_count,
            recursive_other_count = direct_other_count
        """
    )

    max_depth = int(
        connection.execute("SELECT COALESCE(MAX(depth), 0) FROM folders").fetchone()[0]
    )

    for child_depth in range(max_depth, 0, -1):
        parent_depth = child_depth - 1
        connection.execute(
            """
            UPDATE folders AS parent
            SET
                recursive_folder_count = recursive_folder_count + COALESCE((
                    SELECT SUM(child.recursive_folder_count)
                    FROM folders AS child
                    WHERE child.parent_id = parent.id
                      AND child.depth = ?
                      AND child.is_available = 1
                ), 0),
                recursive_image_count = recursive_image_count + COALESCE((
                    SELECT SUM(child.recursive_image_count)
                    FROM folders AS child
                    WHERE child.parent_id = parent.id
                      AND child.depth = ?
                      AND child.is_available = 1
                ), 0),
                recursive_gif_count = recursive_gif_count + COALESCE((
                    SELECT SUM(child.recursive_gif_count)
                    FROM folders AS child
                    WHERE child.parent_id = parent.id
                      AND child.depth = ?
                      AND child.is_available = 1
                ), 0),
                recursive_video_count = recursive_video_count + COALESCE((
                    SELECT SUM(child.recursive_video_count)
                    FROM folders AS child
                    WHERE child.parent_id = parent.id
                      AND child.depth = ?
                      AND child.is_available = 1
                ), 0),
                recursive_other_count = recursive_other_count + COALESCE((
                    SELECT SUM(child.recursive_other_count)
                    FROM folders AS child
                    WHERE child.parent_id = parent.id
                      AND child.depth = ?
                      AND child.is_available = 1
                ), 0)
            WHERE parent.depth = ?
            """,
            (
                child_depth,
                child_depth,
                child_depth,
                child_depth,
                child_depth,
                parent_depth,
            ),
        )

    connection.execute("DROP TABLE activation_child_counts")
    connection.execute("DROP TABLE activation_media_counts")


def _validate_full_active_catalog(
    connection: sqlite3.Connection,
    *,
    scan_id: int,
    expected_available_folders: int,
    expected_available_media: int,
) -> None:
    active_folders = _active_available_count(connection, "folders")
    active_media = _active_available_count(connection, "media_files")

    if active_folders != expected_available_folders:
        raise ScanActivationError(
            "Po aktivaci nesouhlasí počet dostupných složek: "
            f"očekáváno {expected_available_folders}, aktivních {active_folders}."
        )

    if active_media != expected_available_media:
        raise ScanActivationError(
            "Po aktivaci nesouhlasí počet dostupných médií: "
            f"očekáváno {expected_available_media}, aktivních {active_media}."
        )

    _validate_global_root_statistics(connection, scan_id=scan_id)


def _validate_branch_activation(
    connection: sqlite3.Connection,
    *,
    scan_id: int,
    scope_rel_path: str,
    expected_available_folders: int,
    expected_available_media: int,
) -> None:
    folder_scope_sql, folder_scope_params = _scope_filter_sql(
        "folders",
        "branch",
        scope_rel_path,
    )
    active_branch_folders = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM folders
        WHERE {folder_scope_sql}
          AND is_available = 1
        """,
        folder_scope_params,
    ).fetchone()[0])
    if active_branch_folders != expected_available_folders:
        raise ScanActivationError(
            "Po aktivaci větve nesouhlasí počet dostupných složek ve větvi: "
            f"očekáváno {expected_available_folders}, aktivních {active_branch_folders}."
        )

    media_scope_sql, media_scope_params = _scope_filter_sql(
        "media_files",
        "branch",
        scope_rel_path,
    )
    active_branch_media = int(connection.execute(
        f"""
        SELECT COUNT(*)
        FROM media_files
        WHERE {media_scope_sql}
          AND is_available = 1
        """,
        media_scope_params,
    ).fetchone()[0])
    if active_branch_media != expected_available_media:
        raise ScanActivationError(
            "Po aktivaci větve nesouhlasí počet dostupných médií ve větvi: "
            f"očekáváno {expected_available_media}, aktivních {active_branch_media}."
        )

    _validate_global_root_statistics(connection, scan_id=None)


def _validate_global_root_statistics(
    connection: sqlite3.Connection,
    *,
    scan_id: int | None,
) -> None:
    root = connection.execute(
        """
        SELECT
            id,
            last_successful_scan_id,
            recursive_folder_count,
            recursive_image_count,
            recursive_gif_count,
            recursive_video_count,
            recursive_other_count,
            is_available
        FROM folders
        WHERE path_key = ''
        """
    ).fetchone()

    if root is None:
        raise ScanActivationError("Po aktivaci neexistuje kořenová složka.")

    if scan_id is not None and int(root["last_successful_scan_id"]) != scan_id:
        raise ScanActivationError(
            "Kořenová složka neodkazuje na aktivovaný scan."
        )

    if int(root["is_available"]) != 1:
        raise ScanActivationError("Kořenová složka po aktivaci není dostupná.")

    expected_root_folders = _active_available_count(connection, "folders") - 1
    expected_root_media = _available_media_type_counts(connection)

    if int(root["recursive_folder_count"]) != expected_root_folders:
        raise ScanActivationError(
            "Rekurzivní počet složek v kořeni nesouhlasí: "
            f"očekáváno {expected_root_folders}, "
            f"nalezeno {root['recursive_folder_count']}."
        )

    for media_type, column in (
        ("image", "recursive_image_count"),
        ("gif", "recursive_gif_count"),
        ("video", "recursive_video_count"),
        ("other", "recursive_other_count"),
    ):
        expected = expected_root_media[media_type]
        actual = int(root[column])
        if actual != expected:
            raise ScanActivationError(
                f"Rekurzivní počet pro typ {media_type!r} v kořeni nesouhlasí: "
                f"očekáváno {expected}, nalezeno {actual}."
            )


def _available_media_type_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {"image": 0, "gif": 0, "video": 0, "other": 0}
    for row in connection.execute(
        """
        SELECT media_type, COUNT(*) AS count
        FROM media_files
        WHERE is_available = 1
        GROUP BY media_type
        """
    ):
        counts[str(row["media_type"])] = int(row["count"])
    return counts


def _clean_staging_tables(connection: sqlite3.Connection, scan_id: int | None = None) -> tuple[int, int]:
    if scan_id is None:
        cleaned_staged_folders = _scalar_count(connection, "scan_folders")
        cleaned_staged_media = _scalar_count(connection, "scan_media_files")
        connection.execute("DELETE FROM scan_media_files")
        connection.execute("DELETE FROM scan_folders")
        return cleaned_staged_folders, cleaned_staged_media

    cleaned_staged_folders = _count_for_scan(connection, "scan_folders", scan_id)
    cleaned_staged_media = _count_for_scan(connection, "scan_media_files", scan_id)
    connection.execute("DELETE FROM scan_media_files WHERE scan_id = ?", (scan_id,))
    connection.execute("DELETE FROM scan_folders WHERE scan_id = ?", (scan_id,))
    return cleaned_staged_folders, cleaned_staged_media


def _scope_filter_sql(
    alias: str,
    scan_type: str,
    scope_rel_path: str,
) -> tuple[str, tuple[str, ...]]:
    if scan_type == "full":
        return "1 = 1", ()

    return (
        f"({alias}.rel_path = ? OR {alias}.rel_path LIKE ?)",
        (scope_rel_path, f"{scope_rel_path}/%"),
    )


def _scope_unavailable_count(
    connection: sqlite3.Connection,
    table_name: str,
    scan_type: str,
    scope_rel_path: str,
) -> int:
    sql, params = _scope_filter_sql(table_name, scan_type, scope_rel_path)
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE {sql} AND is_available = 0",
            params,
        ).fetchone()[0]
    )


def _ancestor_paths(scope_rel_path: str) -> list[str]:
    parts = list(PurePosixPath(scope_rel_path).parts)
    ancestors = [""]
    for i in range(1, len(parts)):
        ancestors.append("/".join(parts[:i]))
    return ancestors


def _scalar_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _active_available_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE is_available = 1"
        ).fetchone()[0]
    )


def _count_for_scan(connection: sqlite3.Connection, table_name: str, scan_id: int) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()[0]
    )


def _checkpoint_wal(db_path: Path) -> None:
    with open_database(db_path, read_only=False) as connection:
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
