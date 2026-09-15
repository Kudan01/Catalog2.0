from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import Config
from .database import open_database
from .paths import PathValidationError, normalize_catalog_relative_path
from .sorting import catalog_path_key
from .paths import safe_join_catalog_path
from .thumbnail_cache import (
    GIF_PREVIEW_VARIANT_KEY,
    PHOTO_TILE_VARIANT_KEY,
    VIDEO_POSTER_VARIANT_KEY,
    ThumbnailCacheError,
    gif_preview_resource,
    photo_tile_resource,
    reconcile_photo_tile_cache_lifecycle,
    video_poster_resource,
)


FOLDER_PREVIEW_REQUESTED_COUNT = 6
FOLDER_PREVIEW_SELECTION_VARIANT = 0


class FolderPreviewCandidateError(RuntimeError):
    """Raised when folder preview candidate diagnostics cannot be built."""


@dataclass(frozen=True)
class FolderPreviewCandidate:
    """One read-only candidate for a future folder preview slot."""

    position: int
    media_id: int
    rel_path: str
    file_name: str
    media_type: str
    extension: str
    thumbnail_type: str
    variant_key: str
    thumbnail_status: str
    thumbnail_output_rel_path: str
    thumbnail_file_exists: bool


@dataclass(frozen=True)
class FolderPreviewCandidateReport:
    """Read-only folder preview candidate diagnostics for one folder."""

    folder_id: int
    folder_rel_path: str
    folder_name: str
    variant: int
    requested_count: int
    visual_media_count: int
    selected_count: int
    ready_count: int
    missing_count: int
    candidates: tuple[FolderPreviewCandidate, ...]


@dataclass(frozen=True)
class FolderPreviewApplyResult:
    """Result of storing automatic folder preview candidates for one folder."""

    report: FolderPreviewCandidateReport
    deleted_auto_rows: int
    inserted_auto_rows: int
    stored_auto_rows: int


@dataclass(frozen=True)
class FolderPreviewClearAutoResult:
    """Result of clearing automatic folder preview metadata."""

    scope: str
    folder_rel_path: str
    folder_name: str
    deleted_auto_rows: int
    remaining_auto_rows: int


@dataclass(frozen=True)
class FolderPreviewBranchPlanItem:
    """One planned direct child folder in a branch preview operation."""

    folder_id: int
    folder_rel_path: str
    folder_name: str
    visual_media_count: int
    selected_count: int
    ready_count: int
    missing_count: int
    skip_reason: str
    report: FolderPreviewCandidateReport | None


@dataclass(frozen=True)
class FolderPreviewBranchPlan:
    """Shared dry-run/apply plan for branch folder previews.

    By default the plan targets only direct child folders of the selected
    branch. With recursive=True it targets all descendant folders in the
    selected branch. The branch folder itself is never included.
    """

    branch_folder_id: int
    branch_rel_path: str
    branch_name: str
    variant: int
    requested_count: int
    recursive: bool
    direct_child_count: int
    items: tuple[FolderPreviewBranchPlanItem, ...]


@dataclass(frozen=True)
class FolderPreviewBranchApplyResult:
    """Result of applying a branch folder preview plan."""

    plan: FolderPreviewBranchPlan
    applied_results: tuple[FolderPreviewApplyResult, ...]


@dataclass(frozen=True)
class FolderPreviewThumbnailBuildResult:
    """Thumbnail generation result for one folder preview candidate."""

    candidate: FolderPreviewCandidate
    action: str
    message: str


@dataclass(frozen=True)
class FolderPreviewBuildBranchResult:
    """Result of building folder previews and missing thumbnails for one branch."""

    plan: FolderPreviewBranchPlan
    applied_results: tuple[FolderPreviewApplyResult, ...]
    thumbnail_results: tuple[FolderPreviewThumbnailBuildResult, ...]


@dataclass(frozen=True)
class FolderPreviewParentCandidate:
    """One read-only candidate for representing a parent folder.

    The candidate is taken from an already stored automatic folder preview of
    one direct child folder. No original files are read and no thumbnails are
    generated while building this diagnostic object.
    """

    position: int
    source_folder_id: int
    source_folder_rel_path: str
    source_folder_name: str
    source_preview_position: int
    media_id: int
    rel_path: str
    file_name: str
    media_type: str
    extension: str
    thumbnail_type: str
    variant_key: str
    thumbnail_status: str
    thumbnail_output_rel_path: str
    thumbnail_file_exists: bool


@dataclass(frozen=True)
class FolderPreviewParentCandidateReport:
    """Read-only parent folder preview candidate diagnostics.

    The report is built for one selected folder. Its candidates are selected
    from already stored automatic folder previews of that folder's direct
    child folders. It is intentionally a dry-run-only structure.
    """

    folder_id: int
    folder_rel_path: str
    folder_name: str
    variant: int
    requested_count: int
    direct_child_count: int
    child_folders_with_stored_previews: int
    source_preview_item_count: int
    selected_count: int
    ready_count: int
    missing_count: int
    candidates: tuple[FolderPreviewParentCandidate, ...]


@dataclass(frozen=True)
class FolderPreviewParentApplyResult:
    """Result of storing a parent-derived folder preview for one folder."""

    report: FolderPreviewParentCandidateReport
    deleted_auto_parent_rows: int
    inserted_auto_parent_rows: int
    stored_auto_parent_rows: int


@dataclass(frozen=True)
class FolderPreviewParentBranchPlanItem:
    """One planned parent-preview target folder in a branch operation."""

    folder_id: int
    folder_rel_path: str
    folder_name: str
    direct_child_count: int
    child_folders_with_stored_previews: int
    source_preview_item_count: int
    selected_count: int
    ready_count: int
    missing_count: int
    skip_reason: str
    report: FolderPreviewParentCandidateReport | None


@dataclass(frozen=True)
class FolderPreviewParentBranchPlan:
    """Shared dry-run/apply plan for parent-derived previews in one branch."""

    branch_folder_id: int
    branch_rel_path: str
    branch_name: str
    variant: int
    requested_count: int
    recursive: bool
    candidate_folder_count: int
    items: tuple[FolderPreviewParentBranchPlanItem, ...]


@dataclass(frozen=True)
class FolderPreviewParentBranchApplyResult:
    """Result of applying parent-derived previews for one branch scope."""

    plan: FolderPreviewParentBranchPlan
    applied_results: tuple[FolderPreviewParentApplyResult, ...]


@dataclass(frozen=True)
class FolderPreviewTreePlan:
    """Unified plan for building previews for an entire folder tree.

    The plan is shared by dry-run and sharp run. It contains two composable
    layers: direct auto previews and parent-derived auto_parent previews. A
    mixed folder may have both; the selected branch folder itself is included.
    """

    branch_folder_id: int
    branch_rel_path: str
    branch_name: str
    variant: int
    requested_count: int
    candidate_folder_count: int
    scope_folder_ids: tuple[int, ...]
    existing_auto_rows_in_scope: int
    existing_auto_parent_rows_in_scope: int
    auto_items: tuple[FolderPreviewBranchPlanItem, ...]
    parent_items: tuple[FolderPreviewParentBranchPlanItem, ...]


@dataclass(frozen=True)
class FolderPreviewTreeBuildResult:
    """Result of executing a unified folder preview tree plan."""

    plan: FolderPreviewTreePlan
    deleted_auto_rows: int
    deleted_auto_parent_rows: int
    auto_applied_results: tuple[FolderPreviewApplyResult, ...]
    parent_applied_results: tuple[FolderPreviewParentApplyResult, ...]
    thumbnail_results: tuple[FolderPreviewThumbnailBuildResult, ...]


def folder_preview_candidate_report(
    config: Config,
    *,
    folder_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
) -> FolderPreviewCandidateReport:
    """
    Build read-only diagnostics for automatic folder preview candidates.

    This function does not write to the database, does not generate thumbnails,
    and does not touch original media files. It only reads active DB records and
    checks whether already recorded thumbnail cache files exist.
    """
    normalized_folder = _normalize_folder_rel_path(folder_rel_path)
    safe_variant = _non_negative_int(variant, "variant")
    safe_requested_count = _preview_count(requested_count)

    with open_database(config.db_path, read_only=True) as connection:
        return _folder_preview_candidate_report_from_connection(
            config,
            connection,
            folder_rel_path=normalized_folder,
            variant=safe_variant,
            requested_count=safe_requested_count,
        )


def _folder_preview_candidate_report_from_connection(
    config: Config,
    connection: sqlite3.Connection,
    *,
    folder_rel_path: str,
    variant: int,
    requested_count: int,
) -> FolderPreviewCandidateReport:
    """Build one candidate report using an already opened database connection."""
    folder = _available_folder(connection, folder_rel_path)
    media_rows = _visual_media_rows(connection, int(folder["id"]))
    selected_rows = _select_rows(media_rows, requested_count, variant)
    candidates = tuple(
        _candidate_from_row(
            config,
            connection,
            position=position,
            row=row,
        )
        for position, row in enumerate(selected_rows, start=1)
    )

    ready_count = sum(1 for item in candidates if item.thumbnail_status == "ready" and item.thumbnail_file_exists)
    missing_count = len(candidates) - ready_count

    return FolderPreviewCandidateReport(
        folder_id=int(folder["id"]),
        folder_rel_path=str(folder["rel_path"]),
        folder_name=str(folder["name"]),
        variant=variant,
        requested_count=requested_count,
        visual_media_count=len(media_rows),
        selected_count=len(candidates),
        ready_count=ready_count,
        missing_count=missing_count,
        candidates=candidates,
    )


def apply_folder_preview_candidates(
    config: Config,
    *,
    folder_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
) -> FolderPreviewApplyResult:
    """Store automatic folder preview candidates for one folder.

    The stored rows are based on the same candidate plan as the dry-run
    diagnostics. The function only writes rows with selection_type='auto'
    in folder_preview_items for the selected folder. It does not generate
    thumbnails and does not touch original media files.
    """
    report = folder_preview_candidate_report(
        config,
        folder_rel_path=folder_rel_path,
        variant=variant,
        requested_count=requested_count,
    )

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            affected_media_ids = _preview_media_ids_for_folder_ids(
                connection,
                folder_ids=(report.folder_id,),
                selection_type="auto",
            )
            affected_media_ids.update(candidate.media_id for candidate in report.candidates)
            result = _apply_folder_preview_report(connection, report)
            connection.commit()

        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    return result


def _insert_folder_preview_report(
    connection: sqlite3.Connection,
    report: FolderPreviewCandidateReport,
) -> FolderPreviewApplyResult:
    """Insert one already built auto preview report without deleting existing rows.

    This is used by tree rebuilds after the whole branch scope has been
    cleared once. It keeps dry-run and sharp run on one shared plan and avoids
    stale rows for skipped folders in the rebuilt branch.
    """
    for candidate in report.candidates:
        connection.execute(
            """
            INSERT INTO folder_preview_items (
                folder_id,
                selection_type,
                position,
                media_id
            )
            VALUES (?, 'auto', ?, ?)
            """,
            (report.folder_id, candidate.position, candidate.media_id),
        )

    stored_auto_rows = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM folder_preview_items
            WHERE folder_id = ?
              AND selection_type = 'auto'
            """,
            (report.folder_id,),
        ).fetchone()[0]
    )

    return FolderPreviewApplyResult(
        report=report,
        deleted_auto_rows=0,
        inserted_auto_rows=len(report.candidates),
        stored_auto_rows=stored_auto_rows,
    )


def _apply_folder_preview_report(
    connection: sqlite3.Connection,
    report: FolderPreviewCandidateReport,
) -> FolderPreviewApplyResult:
    """Write one already built folder preview report using an open transaction."""
    deleted_cursor = connection.execute(
        """
        DELETE FROM folder_preview_items
        WHERE folder_id = ?
          AND selection_type = 'auto'
        """,
        (report.folder_id,),
    )

    for candidate in report.candidates:
        connection.execute(
            """
            INSERT INTO folder_preview_items (
                folder_id,
                selection_type,
                position,
                media_id
            )
            VALUES (?, 'auto', ?, ?)
            """,
            (report.folder_id, candidate.position, candidate.media_id),
        )

    stored_auto_rows = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM folder_preview_items
            WHERE folder_id = ?
              AND selection_type = 'auto'
            """,
            (report.folder_id,),
        ).fetchone()[0]
    )

    return FolderPreviewApplyResult(
        report=report,
        deleted_auto_rows=max(0, int(deleted_cursor.rowcount or 0)),
        inserted_auto_rows=len(report.candidates),
        stored_auto_rows=stored_auto_rows,
    )


def build_branch_folder_preview_plan(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
    recursive: bool = False,
) -> FolderPreviewBranchPlan:
    """Build a shared dry-run/apply plan for branch folder previews.

    Default scope: direct child folders of the selected branch.
    Recursive scope: all descendant folders of the selected branch.

    The selected branch folder itself is never included. The plan does not
    generate thumbnails and does not write to the database.
    """
    normalized_branch = _normalize_folder_rel_path(branch_rel_path)
    safe_variant = _non_negative_int(variant, "variant")
    safe_requested_count = _preview_count(requested_count)

    with open_database(config.db_path, read_only=True) as connection:
        branch = _available_folder(connection, normalized_branch)
        child_rows = (
            _descendant_folder_rows(connection, int(branch["id"]), str(branch["rel_path"]))
            if recursive
            else _direct_child_folder_rows(connection, int(branch["id"]))
        )
        items: list[FolderPreviewBranchPlanItem] = []

        for child in child_rows:
            child_rel_path = str(child["rel_path"])
            report = _folder_preview_candidate_report_from_connection(
                config,
                connection,
                folder_rel_path=child_rel_path,
                variant=safe_variant,
                requested_count=safe_requested_count,
            )

            if report.visual_media_count <= 0:
                items.append(
                    FolderPreviewBranchPlanItem(
                        folder_id=report.folder_id,
                        folder_rel_path=report.folder_rel_path,
                        folder_name=report.folder_name,
                        visual_media_count=report.visual_media_count,
                        selected_count=report.selected_count,
                        ready_count=report.ready_count,
                        missing_count=report.missing_count,
                        skip_reason="no direct visual media",
                        report=None,
                    )
                )
                continue

            items.append(
                FolderPreviewBranchPlanItem(
                    folder_id=report.folder_id,
                    folder_rel_path=report.folder_rel_path,
                    folder_name=report.folder_name,
                    visual_media_count=report.visual_media_count,
                    selected_count=report.selected_count,
                    ready_count=report.ready_count,
                    missing_count=report.missing_count,
                    skip_reason="",
                    report=report,
                )
            )

    return FolderPreviewBranchPlan(
        branch_folder_id=int(branch["id"]),
        branch_rel_path=str(branch["rel_path"]),
        branch_name=str(branch["name"]),
        variant=safe_variant,
        requested_count=safe_requested_count,
        recursive=bool(recursive),
        direct_child_count=len(child_rows),
        items=tuple(items),
    )


def apply_branch_folder_preview_plan(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
    recursive: bool = False,
) -> FolderPreviewBranchApplyResult:
    """Apply automatic folder previews for one branch scope.

    The write phase executes the exact plan returned by
    build_branch_folder_preview_plan(). It only writes auto rows for planned
    folders with at least one direct visual medium. Skipped folders are left
    unchanged.
    """
    plan = build_branch_folder_preview_plan(
        config,
        branch_rel_path=branch_rel_path,
        variant=variant,
        requested_count=requested_count,
        recursive=recursive,
    )
    applied_results: list[FolderPreviewApplyResult] = []
    affected_media_ids: set[int] = set()

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed_folder_ids = tuple(
                item.report.folder_id for item in plan.items if item.report is not None
            )
            affected_media_ids.update(_preview_media_ids_for_folder_ids(
                connection,
                folder_ids=changed_folder_ids,
                selection_type="auto",
            ))

            for item in plan.items:
                if item.report is None:
                    continue

                result = _apply_folder_preview_report(connection, item.report)
                applied_results.append(result)
                affected_media_ids.update(candidate.media_id for candidate in item.report.candidates)

            connection.commit()

        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    return FolderPreviewBranchApplyResult(
        plan=plan,
        applied_results=tuple(applied_results),
    )


def build_branch_folder_previews_with_thumbnails(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
    recursive: bool = False,
    progress: Callable[[str], None] | None = None,
) -> FolderPreviewBuildBranchResult:
    """Build folder previews for one branch scope and create missing thumbnails.

    This function intentionally uses the same branch plan as the dry-run. It
    first stores the auto folder preview rows and then generates only missing
    thumbnails for selected candidates. The order matters: stored
    folder_preview_items make selected dynamic photo_tile thumbnails lower
    priority for automatic cleanup.
    """
    plan = build_branch_folder_preview_plan(
        config,
        branch_rel_path=branch_rel_path,
        variant=variant,
        requested_count=requested_count,
        recursive=recursive,
    )

    applied_results: list[FolderPreviewApplyResult] = []
    affected_media_ids: set[int] = set()
    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed_folder_ids = tuple(
                item.report.folder_id for item in plan.items if item.report is not None
            )
            affected_media_ids.update(_preview_media_ids_for_folder_ids(
                connection,
                folder_ids=changed_folder_ids,
                selection_type="auto",
            ))

            for item in plan.items:
                if item.report is None:
                    continue

                result = _apply_folder_preview_report(connection, item.report)
                applied_results.append(result)
                affected_media_ids.update(candidate.media_id for candidate in item.report.candidates)

            connection.commit()

        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]
    total_selected = sum(item.selected_count for item in planned_items)
    total_ready = sum(item.ready_count for item in planned_items)
    total_missing = sum(item.missing_count for item in planned_items)

    if progress is not None:
        progress(f"Catalog 2.0 – folder preview build progress ({_branch_scope_label(plan)})")
        progress("=" * 70)
        progress(f"branch: {plan.branch_rel_path or '[root]'}")
        progress(f"variant: {plan.variant}")
        progress(f"requested_count: {plan.requested_count}")
        progress(f"candidate_folder_count: {plan.direct_child_count}")
        progress(f"folders_to_build: {len(planned_items)}")
        progress(f"folders_skipped: {len(skipped_items)}")
        progress(f"selected_preview_items: {total_selected}")
        progress(f"ready_before: {total_ready}")
        progress(f"missing_before: {total_missing}")
        progress(_branch_scope_note(plan))
        progress("")

        if planned_items:
            progress("Processing folders:")
        else:
            progress("No folders to build.")

    applied_by_folder = {item.report.folder_id: item for item in applied_results}
    thumbnail_results: list[FolderPreviewThumbnailBuildResult] = []

    for index, item in enumerate(planned_items, start=1):
        assert item.report is not None
        folder_results: list[FolderPreviewThumbnailBuildResult] = []

        for candidate in item.report.candidates:
            thumb_result = _ensure_candidate_thumbnail(config, candidate)
            thumbnail_results.append(thumb_result)
            folder_results.append(thumb_result)

        if progress is not None:
            applied = applied_by_folder.get(item.folder_id)
            folder_created = sum(1 for result in folder_results if result.action == "created")
            folder_reused = sum(1 for result in folder_results if result.action == "reused")
            folder_errors = sum(1 for result in folder_results if result.action == "error")
            stored = applied.stored_auto_rows if applied is not None else 0
            progress(
                f"[{index}/{len(planned_items)}] BUILD {item.folder_rel_path} | "
                f"selected={item.selected_count} | reused={folder_reused} | "
                f"created={folder_created} | errors={folder_errors} | stored={stored}"
            )

    if progress is not None and planned_items:
        progress("")
        progress("Build completed. Summary follows.")

    return FolderPreviewBuildBranchResult(
        plan=plan,
        applied_results=tuple(applied_results),
        thumbnail_results=tuple(thumbnail_results),
    )


def _ensure_candidate_thumbnail(
    config: Config,
    candidate: FolderPreviewCandidate,
) -> FolderPreviewThumbnailBuildResult:
    """Create a missing thumbnail for one selected folder preview candidate."""
    if candidate.thumbnail_status == "ready" and candidate.thumbnail_file_exists:
        return FolderPreviewThumbnailBuildResult(
            candidate=candidate,
            action="reused",
            message="thumbnail already exists",
        )

    try:
        row = _media_source_row(config, candidate.media_id)
        rel_path = str(row["rel_path"])
        source_path = safe_join_catalog_path(config.data_root, rel_path, allow_root=False)

        if not source_path.exists() or not source_path.is_file():
            raise FolderPreviewCandidateError(
                f"Source media is not available on disk: {rel_path}"
            )

        stat_result = source_path.stat()
        source_size_bytes = int(stat_result.st_size)
        source_modified_time = float(stat_result.st_mtime)

        if candidate.media_type == "image":
            resource = photo_tile_resource(
                config,
                media_id=candidate.media_id,
                rel_path=rel_path,
                source_path=source_path,
                source_size_bytes=source_size_bytes,
                source_modified_time=source_modified_time,
            )
        elif candidate.media_type == "gif":
            resource = gif_preview_resource(
                config,
                media_id=candidate.media_id,
                rel_path=rel_path,
                source_path=source_path,
                source_size_bytes=source_size_bytes,
                source_modified_time=source_modified_time,
            )
        elif candidate.media_type == "video":
            resource = video_poster_resource(
                config,
                media_id=candidate.media_id,
                rel_path=rel_path,
                source_path=source_path,
                source_size_bytes=source_size_bytes,
                source_modified_time=source_modified_time,
            )
        else:
            raise FolderPreviewCandidateError(
                f"Unsupported media type for folder preview thumbnail generation: {candidate.media_type}"
            )

        return FolderPreviewThumbnailBuildResult(
            candidate=candidate,
            action="created" if resource.generated else "reused",
            message=resource.rel_path,
        )

    except (FolderPreviewCandidateError, ThumbnailCacheError, OSError) as exc:
        return FolderPreviewThumbnailBuildResult(
            candidate=candidate,
            action="error",
            message=str(exc),
        )


def _media_source_row(config: Config, media_id: int) -> sqlite3.Row:
    with open_database(config.db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT
                id,
                rel_path,
                media_type,
                size_bytes,
                modified_time
            FROM media_files
            WHERE id = ?
              AND is_available = 1
            """,
            (media_id,),
        ).fetchone()

    if row is None:
        raise FolderPreviewCandidateError(
            f"Media for folder preview is no longer available in DB: media_id={media_id}"
        )

    return row


def folder_preview_parent_candidate_report(
    config: Config,
    *,
    folder_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
) -> FolderPreviewParentCandidateReport:
    """Build read-only candidates for representing one parent folder.

    Candidates are selected from the effective stored representation of direct
    child folders, combining each child's direct and parent-derived rows. This
    function does not write to the database, generate thumbnails, read original
    media files, or change UI state.
    """
    normalized_folder = _normalize_folder_rel_path(folder_rel_path)
    safe_variant = _non_negative_int(variant, "variant")
    safe_requested_count = _preview_count(requested_count)

    with open_database(config.db_path, read_only=True) as connection:
        direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)
        return _folder_preview_parent_candidate_report_from_connection(
            config,
            connection,
            folder_rel_path=normalized_folder,
            variant=safe_variant,
            requested_count=safe_requested_count,
            planned_parent_reports={},
            direct_visual_counts=direct_visual_counts,
            recursive_visual_counts=recursive_visual_counts,
        )


def _folder_preview_parent_candidate_report_from_connection(
    config: Config,
    connection: sqlite3.Connection,
    *,
    folder_rel_path: str,
    variant: int,
    requested_count: int,
    planned_parent_reports: dict[int, FolderPreviewParentCandidateReport] | None = None,
    planned_auto_reports: dict[int, FolderPreviewCandidateReport] | None = None,
    use_stored_preview_rows: bool = True,
    direct_visual_counts: Mapping[int, int] | None = None,
    recursive_visual_counts: Mapping[int, int] | None = None,
) -> FolderPreviewParentCandidateReport:
    """Build one parent candidate report using an opened database connection.

    planned_parent_reports is an in-memory overlay used by recursive branch
    dry-runs and applies. It lets higher parent folders use the parent previews
    planned for lower child folders without requiring a separate user command.
    """
    folder = _available_folder(connection, folder_rel_path)
    child_rows = _direct_child_folder_rows(connection, int(folder["id"]))
    if direct_visual_counts is None or recursive_visual_counts is None:
        direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)

    source_rows = _stored_preview_rows_for_child_folders(
        connection,
        parent_folder_id=int(folder["id"]),
        planned_parent_reports=planned_parent_reports or {},
        planned_auto_reports=planned_auto_reports or {},
        use_stored_preview_rows=use_stored_preview_rows,
        requested_count=requested_count,
        variant=variant,
        direct_visual_counts=direct_visual_counts,
        recursive_visual_counts=recursive_visual_counts,
    )
    source_rows_by_child: dict[int, list[Mapping[str, object]]] = {}
    for row in source_rows:
        source_rows_by_child.setdefault(int(row["source_folder_id"]), []).append(row)

    source_groups: list[tuple[int, list[Mapping[str, object]], int]] = []
    for child in child_rows:
        child_id = int(child["id"])
        child_source_rows = source_rows_by_child.get(child_id, [])
        if not child_source_rows:
            continue
        source_groups.append((
            child_id,
            child_source_rows,
            int(recursive_visual_counts.get(child_id, len(child_source_rows))),
        ))

    selected_rows = _weighted_select_rows_by_source(
        source_groups,
        requested_count=requested_count,
        variant=variant,
    )
    candidates = tuple(
        _parent_candidate_from_row(
            config,
            position=position,
            row=row,
        )
        for position, row in enumerate(selected_rows, start=1)
    )

    child_folders_with_stored_previews = len({
        int(row["source_folder_id"]) for row in source_rows
    })
    ready_count = sum(
        1 for item in candidates
        if item.thumbnail_status == "ready" and item.thumbnail_file_exists
    )
    missing_count = len(candidates) - ready_count

    return FolderPreviewParentCandidateReport(
        folder_id=int(folder["id"]),
        folder_rel_path=str(folder["rel_path"]),
        folder_name=str(folder["name"]),
        variant=variant,
        requested_count=requested_count,
        direct_child_count=len(child_rows),
        child_folders_with_stored_previews=child_folders_with_stored_previews,
        source_preview_item_count=len(source_rows),
        selected_count=len(candidates),
        ready_count=ready_count,
        missing_count=missing_count,
        candidates=candidates,
    )


def apply_folder_preview_parent_candidates(
    config: Config,
    *,
    folder_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
) -> FolderPreviewParentApplyResult:
    """Store a parent-derived folder preview for one folder.

    The stored rows are selected by the same logic as
    folder_preview_parent_candidate_report(). Only selection_type='auto_parent'
    rows for the selected folder are replaced. The function does not generate
    thumbnails and does not touch original media files.
    """
    report = folder_preview_parent_candidate_report(
        config,
        folder_rel_path=folder_rel_path,
        variant=variant,
        requested_count=requested_count,
    )

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            affected_media_ids = _preview_media_ids_for_folder_ids(
                connection,
                folder_ids=(report.folder_id,),
                selection_type="auto_parent",
            )
            affected_media_ids.update(candidate.media_id for candidate in report.candidates)
            result = _apply_folder_preview_parent_report(connection, report)
            connection.commit()

        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    return result


def build_parent_branch_folder_preview_plan(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
    recursive: bool = False,
) -> FolderPreviewParentBranchPlan:
    """Build a shared dry-run/apply plan for parent previews in a branch.

    Default scope: direct child folders of the selected branch.
    Recursive scope: all descendant folders of the selected branch, evaluated
    bottom-up. The selected branch folder itself is never included. The plan
    does not write to the database, generate thumbnails, or read originals.
    """
    normalized_branch = _normalize_folder_rel_path(branch_rel_path)
    safe_variant = _non_negative_int(variant, "variant")
    safe_requested_count = _preview_count(requested_count)

    with open_database(config.db_path, read_only=True) as connection:
        branch = _available_folder(connection, normalized_branch)
        target_rows = (
            _descendant_folder_rows(connection, int(branch["id"]), str(branch["rel_path"]))
            if recursive
            else _direct_child_folder_rows(connection, int(branch["id"]))
        )

        if recursive:
            target_rows = sorted(
                target_rows,
                key=lambda row: (-_folder_depth(str(row["rel_path"])), catalog_path_key(str(row["rel_path"]))),
            )

        direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)
        planned_reports: dict[int, FolderPreviewParentCandidateReport] = {}
        items: list[FolderPreviewParentBranchPlanItem] = []

        for row in target_rows:
            report = _folder_preview_parent_candidate_report_from_connection(
                config,
                connection,
                folder_rel_path=str(row["rel_path"]),
                variant=safe_variant,
                requested_count=safe_requested_count,
                planned_parent_reports=planned_reports,
                direct_visual_counts=direct_visual_counts,
                recursive_visual_counts=recursive_visual_counts,
            )

            if report.source_preview_item_count <= 0 or report.selected_count <= 0:
                items.append(
                    FolderPreviewParentBranchPlanItem(
                        folder_id=report.folder_id,
                        folder_rel_path=report.folder_rel_path,
                        folder_name=report.folder_name,
                        direct_child_count=report.direct_child_count,
                        child_folders_with_stored_previews=report.child_folders_with_stored_previews,
                        source_preview_item_count=report.source_preview_item_count,
                        selected_count=report.selected_count,
                        ready_count=report.ready_count,
                        missing_count=report.missing_count,
                        skip_reason="no usable preview sources in direct child folders",
                        report=None,
                    )
                )
                continue

            planned_reports[report.folder_id] = report
            items.append(
                FolderPreviewParentBranchPlanItem(
                    folder_id=report.folder_id,
                    folder_rel_path=report.folder_rel_path,
                    folder_name=report.folder_name,
                    direct_child_count=report.direct_child_count,
                    child_folders_with_stored_previews=report.child_folders_with_stored_previews,
                    source_preview_item_count=report.source_preview_item_count,
                    selected_count=report.selected_count,
                    ready_count=report.ready_count,
                    missing_count=report.missing_count,
                    skip_reason="",
                    report=report,
                )
            )

    return FolderPreviewParentBranchPlan(
        branch_folder_id=int(branch["id"]),
        branch_rel_path=str(branch["rel_path"]),
        branch_name=str(branch["name"]),
        variant=safe_variant,
        requested_count=safe_requested_count,
        recursive=bool(recursive),
        candidate_folder_count=len(target_rows),
        items=tuple(items),
    )


def apply_parent_branch_folder_preview_plan(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
    recursive: bool = False,
    progress: Callable[[str], None] | None = None,
) -> FolderPreviewParentBranchApplyResult:
    """Apply parent-derived previews for one branch scope.

    The write phase executes the exact plan returned by
    build_parent_branch_folder_preview_plan(). It only replaces auto_parent rows
    for planned folders. Skipped folders are left unchanged.
    """
    plan = build_parent_branch_folder_preview_plan(
        config,
        branch_rel_path=branch_rel_path,
        variant=variant,
        requested_count=requested_count,
        recursive=recursive,
    )
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]

    if progress is not None:
        progress(f"Catalog 2.0 – bulk parent preview apply progress ({_parent_branch_scope_label(plan)})")
        progress("=" * 70)
        progress(f"branch: {plan.branch_rel_path or '[root]'}")
        progress(f"variant: {plan.variant}")
        progress(f"requested_count: {plan.requested_count}")
        progress(f"candidate_folder_count: {plan.candidate_folder_count}")
        progress(f"folders_to_apply: {len(planned_items)}")
        progress(f"folders_skipped: {len(skipped_items)}")
        progress(_parent_branch_scope_note(plan))
        progress(
            "Source: weighted effective previews from direct child folders, combining direct and descendant content."
        )
        progress("")
        if planned_items:
            progress("Processing folders:")
        else:
            progress("No folders to process for parent apply.")

    applied_results: list[FolderPreviewParentApplyResult] = []
    affected_media_ids: set[int] = set()
    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed_folder_ids = tuple(item.report.folder_id for item in planned_items if item.report is not None)
            affected_media_ids.update(_preview_media_ids_for_folder_ids(
                connection,
                folder_ids=changed_folder_ids,
                selection_type="auto_parent",
            ))

            for index, item in enumerate(planned_items, start=1):
                assert item.report is not None
                result = _apply_folder_preview_parent_report(connection, item.report)
                applied_results.append(result)
                affected_media_ids.update(candidate.media_id for candidate in item.report.candidates)

                if progress is not None:
                    progress(
                        f"[{index}/{len(planned_items)}] PARENT APPLY {item.folder_rel_path} | "
                        f"source_items={item.source_preview_item_count} | selected={item.selected_count} | "
                        f"ready={item.ready_count} | missing={item.missing_count} | "
                        f"stored={result.stored_auto_parent_rows}"
                    )

            connection.commit()

        except Exception:
            connection.rollback()
            raise

    if progress is not None and planned_items:
        progress("")
        progress("Parent apply completed. Summary follows.")

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    return FolderPreviewParentBranchApplyResult(
        plan=plan,
        applied_results=tuple(applied_results),
    )



def build_folder_preview_tree_plan(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
) -> FolderPreviewTreePlan:
    """Build one unified dry-run/apply plan for an entire preview tree.

    The selected branch folder is included. The plan first prepares direct auto
    previews, then prepares auto_parent rows bottom-up for every folder with
    usable child sources. Mixed folders keep both layers; their effective UI
    representation is selected deterministically from their combination.
    """
    normalized_branch = _normalize_folder_rel_path(branch_rel_path)
    safe_variant = _non_negative_int(variant, "variant")
    safe_requested_count = _preview_count(requested_count)

    with open_database(config.db_path, read_only=True) as connection:
        branch = _available_folder(connection, normalized_branch)
        branch_row = branch
        descendant_rows = _descendant_folder_rows(
            connection,
            int(branch["id"]),
            str(branch["rel_path"]),
        )
        target_rows = [branch_row] + descendant_rows
        target_rows = sorted(
            target_rows,
            key=lambda row: (catalog_path_key(str(row["rel_path"])), int(row["id"])),
        )
        scope_folder_ids = tuple(int(row["id"]) for row in target_rows)
        existing_auto_rows_in_scope = _count_preview_rows_for_folder_ids(
            connection,
            folder_ids=scope_folder_ids,
            selection_type="auto",
        )
        existing_auto_parent_rows_in_scope = _count_preview_rows_for_folder_ids(
            connection,
            folder_ids=scope_folder_ids,
            selection_type="auto_parent",
        )
        direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)

        auto_items: list[FolderPreviewBranchPlanItem] = []
        planned_auto_reports: dict[int, FolderPreviewCandidateReport] = {}

        for row in target_rows:
            report = _folder_preview_candidate_report_from_connection(
                config,
                connection,
                folder_rel_path=str(row["rel_path"]),
                variant=safe_variant,
                requested_count=safe_requested_count,
            )

            if report.visual_media_count <= 0 or report.selected_count <= 0:
                auto_items.append(
                    FolderPreviewBranchPlanItem(
                        folder_id=report.folder_id,
                        folder_rel_path=report.folder_rel_path,
                        folder_name=report.folder_name,
                        visual_media_count=report.visual_media_count,
                        selected_count=report.selected_count,
                        ready_count=report.ready_count,
                        missing_count=report.missing_count,
                        skip_reason="no direct visual media",
                        report=None,
                    )
                )
                continue

            planned_auto_reports[report.folder_id] = report
            auto_items.append(
                FolderPreviewBranchPlanItem(
                    folder_id=report.folder_id,
                    folder_rel_path=report.folder_rel_path,
                    folder_name=report.folder_name,
                    visual_media_count=report.visual_media_count,
                    selected_count=report.selected_count,
                    ready_count=report.ready_count,
                    missing_count=report.missing_count,
                    skip_reason="",
                    report=report,
                )
            )

        parent_rows = sorted(
            target_rows,
            key=lambda row: (-_folder_depth(str(row["rel_path"])), catalog_path_key(str(row["rel_path"])), int(row["id"])),
        )
        planned_parent_reports: dict[int, FolderPreviewParentCandidateReport] = {}
        parent_items: list[FolderPreviewParentBranchPlanItem] = []

        for row in parent_rows:
            report = _folder_preview_parent_candidate_report_from_connection(
                config,
                connection,
                folder_rel_path=str(row["rel_path"]),
                variant=safe_variant,
                requested_count=safe_requested_count,
                planned_parent_reports=planned_parent_reports,
                planned_auto_reports=planned_auto_reports,
                use_stored_preview_rows=False,
                direct_visual_counts=direct_visual_counts,
                recursive_visual_counts=recursive_visual_counts,
            )

            if report.source_preview_item_count <= 0 or report.selected_count <= 0:
                parent_items.append(
                    FolderPreviewParentBranchPlanItem(
                        folder_id=report.folder_id,
                        folder_rel_path=report.folder_rel_path,
                        folder_name=report.folder_name,
                        direct_child_count=report.direct_child_count,
                        child_folders_with_stored_previews=report.child_folders_with_stored_previews,
                        source_preview_item_count=report.source_preview_item_count,
                        selected_count=report.selected_count,
                        ready_count=report.ready_count,
                        missing_count=report.missing_count,
                        skip_reason="no usable preview sources in direct child folders",
                        report=None,
                    )
                )
                continue

            planned_parent_reports[report.folder_id] = report
            parent_items.append(
                FolderPreviewParentBranchPlanItem(
                    folder_id=report.folder_id,
                    folder_rel_path=report.folder_rel_path,
                    folder_name=report.folder_name,
                    direct_child_count=report.direct_child_count,
                    child_folders_with_stored_previews=report.child_folders_with_stored_previews,
                    source_preview_item_count=report.source_preview_item_count,
                    selected_count=report.selected_count,
                    ready_count=report.ready_count,
                    missing_count=report.missing_count,
                    skip_reason="",
                    report=report,
                )
            )

    return FolderPreviewTreePlan(
        branch_folder_id=int(branch["id"]),
        branch_rel_path=str(branch["rel_path"]),
        branch_name=str(branch["name"]),
        variant=safe_variant,
        requested_count=safe_requested_count,
        candidate_folder_count=len(target_rows),
        scope_folder_ids=scope_folder_ids,
        existing_auto_rows_in_scope=existing_auto_rows_in_scope,
        existing_auto_parent_rows_in_scope=existing_auto_parent_rows_in_scope,
        auto_items=tuple(auto_items),
        parent_items=tuple(parent_items),
    )


def maintain_folder_previews_for_scopes(
    config: Config,
    *,
    auto_folder_rels: Iterable[str],
    parent_folder_rels: Iterable[str],
    variant: int = 0,
    requested_count: int = 6,
) -> dict[str, Any]:
    """Update folder-preview rows only for explicitly supplied folders.

    This is the narrow maintenance path used by the full-catalog update
    orchestrator. It intentionally does not rebuild an entire branch tree:
    - auto rows are recalculated only for directly affected existing folders,
    - selected auto thumbnails are created/reused only for those folders,
    - auto_parent rows are recalculated bottom-up only for supplied candidate
      folders and ancestors.

    Original media files are never modified and protected cache is not cleaned.
    """
    started = time.monotonic()
    normalized_auto_rels = _normalize_rel_path_sequence(auto_folder_rels)
    normalized_parent_rels = _normalize_rel_path_sequence(parent_folder_rels)
    safe_variant = _non_negative_int(variant, "variant")
    safe_requested_count = _preview_count(requested_count)

    auto_reports: list[FolderPreviewCandidateReport] = []
    auto_skipped: list[dict[str, Any]] = []

    with open_database(config.db_path, read_only=True) as connection:
        for rel_path in normalized_auto_rels:
            try:
                report = _folder_preview_candidate_report_from_connection(
                    config,
                    connection,
                    folder_rel_path=rel_path,
                    variant=safe_variant,
                    requested_count=safe_requested_count,
                )
            except Exception as exc:  # noqa: BLE001 - one missing/unavailable folder must not stop maintenance
                auto_skipped.append({
                    "rel_path": rel_path,
                    "reason": str(exc),
                    "folder_id": None,
                    "deleted_auto_rows": 0,
                })
                continue

            if report.visual_media_count <= 0 or report.selected_count <= 0:
                auto_skipped.append({
                    "rel_path": report.folder_rel_path,
                    "reason": "no direct visual media",
                    "folder_id": report.folder_id,
                    "deleted_auto_rows": 0,
                })
                continue

            auto_reports.append(report)

    auto_applied_results: list[FolderPreviewApplyResult] = []
    deleted_auto_rows = 0
    affected_media_ids: set[int] = set()
    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            auto_folder_ids = tuple(
                int(folder_id)
                for folder_id in (
                    [report.folder_id for report in auto_reports]
                    + [item["folder_id"] for item in auto_skipped if item.get("folder_id") is not None]
                )
            )
            affected_media_ids.update(_preview_media_ids_for_folder_ids(
                connection,
                folder_ids=auto_folder_ids,
                selection_type="auto",
            ))

            for report in auto_reports:
                result = _apply_folder_preview_report(connection, report)
                deleted_auto_rows += result.deleted_auto_rows
                auto_applied_results.append(result)
                affected_media_ids.update(candidate.media_id for candidate in report.candidates)

            for item in auto_skipped:
                folder_id = item.get("folder_id")
                if folder_id is None:
                    continue
                deleted = _delete_preview_rows_for_folder_ids(
                    connection,
                    folder_ids=(int(folder_id),),
                    selection_type="auto",
                )
                item["deleted_auto_rows"] = deleted
                deleted_auto_rows += deleted

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    thumbnail_results: list[FolderPreviewThumbnailBuildResult] = []
    for report in auto_reports:
        for candidate in report.candidates:
            thumbnail_results.append(_ensure_candidate_thumbnail(config, candidate))

    parent_targets = sorted(
        normalized_parent_rels,
        key=lambda rel_path: (-_folder_depth(rel_path), catalog_path_key(rel_path)),
    )
    parent_applied_results: list[FolderPreviewParentApplyResult] = []
    parent_skipped: list[dict[str, Any]] = []
    deleted_auto_parent_rows = 0

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)

            for rel_path in parent_targets:
                try:
                    folder = _available_folder(connection, rel_path)
                except Exception as exc:  # noqa: BLE001 - purged/missing folders are skipped safely
                    parent_skipped.append({
                        "rel_path": rel_path,
                        "reason": str(exc),
                        "folder_id": None,
                        "deleted_auto_parent_rows": 0,
                    })
                    continue

                folder_id = int(folder["id"])
                affected_media_ids.update(_preview_media_ids_for_folder_ids(
                    connection,
                    folder_ids=(folder_id,),
                    selection_type="auto_parent",
                ))

                report = _folder_preview_parent_candidate_report_from_connection(
                    config,
                    connection,
                    folder_rel_path=rel_path,
                    variant=safe_variant,
                    requested_count=safe_requested_count,
                    planned_parent_reports={},
                    planned_auto_reports={},
                    use_stored_preview_rows=True,
                    direct_visual_counts=direct_visual_counts,
                    recursive_visual_counts=recursive_visual_counts,
                )

                if report.source_preview_item_count <= 0 or report.selected_count <= 0:
                    deleted = _delete_preview_rows_for_folder_ids(
                        connection,
                        folder_ids=(report.folder_id,),
                        selection_type="auto_parent",
                    )
                    deleted_auto_parent_rows += deleted
                    parent_skipped.append({
                        "rel_path": report.folder_rel_path,
                        "reason": "no usable preview sources in direct child folders",
                        "folder_id": report.folder_id,
                        "deleted_auto_parent_rows": deleted,
                    })
                    continue

                result = _apply_folder_preview_parent_report(connection, report)
                deleted_auto_parent_rows += result.deleted_auto_parent_rows
                parent_applied_results.append(result)
                affected_media_ids.update(candidate.media_id for candidate in report.candidates)

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    thumb_created = sum(1 for item in thumbnail_results if item.action == "created")
    thumb_reused = sum(1 for item in thumbnail_results if item.action == "reused")
    thumb_errors = sum(1 for item in thumbnail_results if item.action == "error")
    auto_inserted = sum(item.inserted_auto_rows for item in auto_applied_results)
    parent_inserted = sum(item.inserted_auto_parent_rows for item in parent_applied_results)

    return {
        "folder_preview_maintenance": True,
        "scope": "affected_scopes",
        "variant": safe_variant,
        "requested_count": safe_requested_count,
        "targets": {
            "auto_folder_count": len(normalized_auto_rels),
            "parent_folder_count": len(normalized_parent_rels),
            "auto_folder_samples": normalized_auto_rels[:20],
            "parent_folder_samples": normalized_parent_rels[:20],
            "auto_folder_rels": normalized_auto_rels,
            "parent_folder_rels": normalized_parent_rels,
        },
        "auto": {
            "folders_requested": len(normalized_auto_rels),
            "folders_applied": len(auto_applied_results),
            "folders_skipped": len(auto_skipped),
            "rows_deleted": deleted_auto_rows,
            "rows_inserted": auto_inserted,
            "skipped_samples": auto_skipped[:10],
        },
        "parent": {
            "folders_requested": len(normalized_parent_rels),
            "folders_applied": len(parent_applied_results),
            "folders_skipped": len(parent_skipped),
            "rows_deleted": deleted_auto_parent_rows,
            "rows_inserted": parent_inserted,
            "skipped_samples": parent_skipped[:10],
        },
        "thumbnails": {
            "reused": thumb_reused,
            "created": thumb_created,
            "errors": thumb_errors,
        },
        "duration_seconds": time.monotonic() - started,
        "writes": {
            "catalog_db": "folder_preview_items auto/auto_parent only for affected folders and ancestors",
            "thumbnail_cache": "missing selected auto candidates only",
            "source_media": False,
            "scan_data": False,
            "manual_preview_rows": False,
            "protected_cache_deleted": False,
        },
    }


def _normalize_rel_path_sequence(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        try:
            normalized = _normalize_folder_rel_path(str(value or ""))
        except FolderPreviewCandidateError:
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return sorted(result, key=catalog_path_key)


def build_folder_preview_tree(
    config: Config,
    *,
    branch_rel_path: str,
    variant: int = 0,
    requested_count: int = 6,
    progress: Callable[[str], None] | None = None,
) -> FolderPreviewTreeBuildResult:
    """Execute the unified preview-tree build using one shared plan.

    The sharp run uses the same FolderPreviewTreePlan as dry-run:
    1. store auto previews for folders with direct media,
    2. create missing thumbnails only for those selected auto candidates,
    3. store auto_parent previews bottom-up for every folder with child sources.
    """
    plan = build_folder_preview_tree_plan(
        config,
        branch_rel_path=branch_rel_path,
        variant=variant,
        requested_count=requested_count,
    )
    planned_auto_items = [item for item in plan.auto_items if item.report is not None]
    skipped_auto_items = [item for item in plan.auto_items if item.report is None]
    planned_parent_items = [item for item in plan.parent_items if item.report is not None]
    skipped_parent_items = [item for item in plan.parent_items if item.report is None]

    if progress is not None:
        progress("Catalog 2.0 – unified folder preview tree build progress")
        progress("=" * 70)
        progress(f"branch: {plan.branch_rel_path or '[root]'}")
        progress(f"variant: {plan.variant}")
        progress(f"requested_count: {plan.requested_count}")
        progress(f"candidate_folder_count: {plan.candidate_folder_count}")
        progress(f"auto_folders_to_build: {len(planned_auto_items)}")
        progress(f"auto_folders_skipped: {len(skipped_auto_items)}")
        progress(f"parent_folders_to_apply: {len(planned_parent_items)}")
        progress(f"parent_folders_skipped: {len(skipped_parent_items)}")
        progress("Scope: selected folder including all descendants. Plan: direct auto previews first, then bottom-up auto_parent previews.")
        progress("")

    auto_applied_results: list[FolderPreviewApplyResult] = []
    deleted_auto_rows = 0
    affected_auto_media_ids: set[int] = set()
    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_auto = _preview_row_signatures_for_folder_ids(
                connection,
                folder_ids=plan.scope_folder_ids,
                selection_type="auto",
            )
            planned_auto = {
                item.folder_id: _auto_report_signature(item.report)
                for item in planned_auto_items
                if item.report is not None
            }
            planned_auto_items_by_id = {item.folder_id: item for item in planned_auto_items}

            for folder_id in plan.scope_folder_ids:
                current_signature = existing_auto.get(folder_id, ())
                planned_signature = planned_auto.get(folder_id, ())
                if current_signature == planned_signature:
                    continue

                affected_auto_media_ids.update(media_id for _, media_id in current_signature)
                affected_auto_media_ids.update(media_id for _, media_id in planned_signature)

                deleted_auto_rows += _delete_preview_rows_for_folder_ids(
                    connection,
                    folder_ids=(folder_id,),
                    selection_type="auto",
                )

                item = planned_auto_items_by_id.get(folder_id)
                if item is not None and item.report is not None:
                    auto_applied_results.append(_insert_folder_preview_report(connection, item.report))

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_auto_media_ids)
    thumbnail_results: list[FolderPreviewThumbnailBuildResult] = []
    auto_result_by_folder = {item.report.folder_id: item for item in auto_applied_results}

    if progress is not None:
        if planned_auto_items:
            progress("Auto previews + thumbnails:")
        else:
            progress("Auto previews: no folders with direct media.")

    for index, item in enumerate(planned_auto_items, start=1):
        assert item.report is not None
        folder_results: list[FolderPreviewThumbnailBuildResult] = []

        for candidate in item.report.candidates:
            thumb_result = _ensure_candidate_thumbnail(config, candidate)
            thumbnail_results.append(thumb_result)
            folder_results.append(thumb_result)

        if progress is not None:
            applied = auto_result_by_folder.get(item.folder_id)
            folder_created = sum(1 for result in folder_results if result.action == "created")
            folder_reused = sum(1 for result in folder_results if result.action == "reused")
            folder_errors = sum(1 for result in folder_results if result.action == "error")
            stored = applied.stored_auto_rows if applied is not None else 0
            progress(
                f"[{index}/{len(planned_auto_items)}] AUTO BUILD {item.folder_rel_path or '[root]'} | "
                f"selected={item.selected_count} | reused={folder_reused} | "
                f"created={folder_created} | errors={folder_errors} | stored={stored}"
            )

    if progress is not None and planned_auto_items:
        progress("")

    parent_applied_results: list[FolderPreviewParentApplyResult] = []
    deleted_auto_parent_rows = 0
    affected_parent_media_ids: set[int] = set()
    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_parent = _preview_row_signatures_for_folder_ids(
                connection,
                folder_ids=plan.scope_folder_ids,
                selection_type="auto_parent",
            )
            planned_parent = {
                item.folder_id: _parent_report_signature(item.report)
                for item in planned_parent_items
                if item.report is not None
            }
            planned_parent_items_by_id = {item.folder_id: item for item in planned_parent_items}

            changed_parent_items: list[FolderPreviewParentBranchPlanItem] = []
            for folder_id in plan.scope_folder_ids:
                current_signature = existing_parent.get(folder_id, ())
                planned_signature = planned_parent.get(folder_id, ())
                if current_signature == planned_signature:
                    continue

                affected_parent_media_ids.update(media_id for _, media_id in current_signature)
                affected_parent_media_ids.update(media_id for _, media_id in planned_signature)

                deleted_auto_parent_rows += _delete_preview_rows_for_folder_ids(
                    connection,
                    folder_ids=(folder_id,),
                    selection_type="auto_parent",
                )
                item = planned_parent_items_by_id.get(folder_id)
                if item is not None and item.report is not None:
                    changed_parent_items.append(item)

            for index, item in enumerate(changed_parent_items, start=1):
                assert item.report is not None
                result = _insert_folder_preview_parent_report(connection, item.report)
                parent_applied_results.append(result)

                if progress is not None:
                    progress(
                        f"[{index}/{len(changed_parent_items)}] PARENT APPLY {item.folder_rel_path or '[root]'} | "
                        f"source_items={item.source_preview_item_count} | selected={item.selected_count} | "
                        f"ready_before={item.ready_count} | missing_before={item.missing_count} | "
                        f"stored={result.stored_auto_parent_rows}"
                    )

            connection.commit()
        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_parent_media_ids)
    if progress is not None and planned_parent_items:
        progress("")
        progress("Unified build completed. Summary follows.")

    return FolderPreviewTreeBuildResult(
        plan=plan,
        deleted_auto_rows=deleted_auto_rows,
        deleted_auto_parent_rows=deleted_auto_parent_rows,
        auto_applied_results=tuple(auto_applied_results),
        parent_applied_results=tuple(parent_applied_results),
        thumbnail_results=tuple(thumbnail_results),
    )


def _insert_folder_preview_parent_report(
    connection: sqlite3.Connection,
    report: FolderPreviewParentCandidateReport,
) -> FolderPreviewParentApplyResult:
    """Insert one already built auto_parent report without deleting existing rows."""
    for candidate in report.candidates:
        connection.execute(
            """
            INSERT INTO folder_preview_items (
                folder_id,
                selection_type,
                position,
                media_id
            )
            VALUES (?, 'auto_parent', ?, ?)
            """,
            (report.folder_id, candidate.position, candidate.media_id),
        )

    stored_auto_parent_rows = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM folder_preview_items
            WHERE folder_id = ?
              AND selection_type = 'auto_parent'
            """,
            (report.folder_id,),
        ).fetchone()[0]
    )

    return FolderPreviewParentApplyResult(
        report=report,
        deleted_auto_parent_rows=0,
        inserted_auto_parent_rows=len(report.candidates),
        stored_auto_parent_rows=stored_auto_parent_rows,
    )


def _apply_folder_preview_parent_report(
    connection: sqlite3.Connection,
    report: FolderPreviewParentCandidateReport,
) -> FolderPreviewParentApplyResult:
    """Write one parent preview report using an open transaction."""
    deleted_cursor = connection.execute(
        """
        DELETE FROM folder_preview_items
        WHERE folder_id = ?
          AND selection_type = 'auto_parent'
        """,
        (report.folder_id,),
    )

    for candidate in report.candidates:
        connection.execute(
            """
            INSERT INTO folder_preview_items (
                folder_id,
                selection_type,
                position,
                media_id
            )
            VALUES (?, 'auto_parent', ?, ?)
            """,
            (report.folder_id, candidate.position, candidate.media_id),
        )

    stored_auto_parent_rows = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM folder_preview_items
            WHERE folder_id = ?
              AND selection_type = 'auto_parent'
            """,
            (report.folder_id,),
        ).fetchone()[0]
    )

    return FolderPreviewParentApplyResult(
        report=report,
        deleted_auto_parent_rows=max(0, int(deleted_cursor.rowcount or 0)),
        inserted_auto_parent_rows=len(report.candidates),
        stored_auto_parent_rows=stored_auto_parent_rows,
    )


def clear_auto_folder_previews(
    config: Config,
    *,
    folder_rel_path: str = "",
    all_folders: bool = False,
) -> FolderPreviewClearAutoResult:
    """Clear automatic folder preview metadata without touching cache or media.

    This is a stabilization/service command. It only deletes
    folder_preview_items rows with selection_type='auto'. Manual rows,
    thumbnails, original media, scan data and favorites are not modified.
    """
    if all_folders and folder_rel_path:
        raise FolderPreviewCandidateError("Use either --all or --folder, not both at the same time.")

    if not all_folders and not folder_rel_path:
        raise FolderPreviewCandidateError("Provide --folder for one folder, or --all to delete all automatic preview rows.")

    with open_database(config.db_path, read_only=False) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")

            if all_folders:
                affected_media_ids = _preview_media_ids_for_folder_ids(
                    connection,
                    folder_ids=None,
                    selection_type="auto",
                )
                deleted_cursor = connection.execute(
                    """
                    DELETE FROM folder_preview_items
                    WHERE selection_type = 'auto'
                    """
                )
                scope = "all"
                folder_rel = ""
                folder_name = "[all folders]"
            else:
                normalized_folder = _normalize_folder_rel_path(folder_rel_path)
                folder = _available_folder(connection, normalized_folder)
                affected_media_ids = _preview_media_ids_for_folder_ids(
                    connection,
                    folder_ids=(int(folder["id"]),),
                    selection_type="auto",
                )
                deleted_cursor = connection.execute(
                    """
                    DELETE FROM folder_preview_items
                    WHERE folder_id = ?
                      AND selection_type = 'auto'
                    """,
                    (int(folder["id"]),),
                )
                scope = "folder"
                folder_rel = str(folder["rel_path"])
                folder_name = str(folder["name"])

            remaining_auto_rows = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM folder_preview_items
                    WHERE selection_type = 'auto'
                    """
                ).fetchone()[0]
            )

            connection.commit()

        except Exception:
            connection.rollback()
            raise

    reconcile_photo_tile_cache_lifecycle(config, media_ids=affected_media_ids)
    return FolderPreviewClearAutoResult(
        scope=scope,
        folder_rel_path=folder_rel,
        folder_name=folder_name,
        deleted_auto_rows=max(0, int(deleted_cursor.rowcount or 0)),
        remaining_auto_rows=remaining_auto_rows,
    )


def _auto_report_signature(report: FolderPreviewCandidateReport) -> tuple[tuple[int, int], ...]:
    """Return the exact stored row shape planned for one direct auto preview."""
    return tuple((candidate.position, candidate.media_id) for candidate in report.candidates)


def _parent_report_signature(report: FolderPreviewParentCandidateReport) -> tuple[tuple[int, int], ...]:
    """Return the exact stored row shape planned for one parent-derived preview."""
    return tuple((candidate.position, candidate.media_id) for candidate in report.candidates)


def _preview_row_signatures_for_folder_ids(
    connection: sqlite3.Connection,
    *,
    folder_ids: Iterable[int],
    selection_type: str,
) -> dict[int, tuple[tuple[int, int], ...]]:
    """Read current preview rows so execution can skip unchanged folders."""
    ids = tuple(dict.fromkeys(int(folder_id) for folder_id in folder_ids))
    if not ids:
        return {}
    if selection_type not in {"auto", "auto_parent"}:
        raise FolderPreviewCandidateError(f"Unsupported automatic selection type: {selection_type}")

    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT folder_id, position, media_id
        FROM folder_preview_items
        WHERE selection_type = ?
          AND folder_id IN ({placeholders})
        ORDER BY folder_id, position, media_id
        """,
        (selection_type, *ids),
    ).fetchall()

    grouped: dict[int, list[tuple[int, int]]] = {}
    for row in rows:
        grouped.setdefault(int(row["folder_id"]), []).append(
            (int(row["position"]), int(row["media_id"]))
        )
    return {folder_id: tuple(signature) for folder_id, signature in grouped.items()}


def _count_preview_rows_for_folder_ids(
    connection: sqlite3.Connection,
    *,
    folder_ids: tuple[int, ...],
    selection_type: str,
) -> int:
    """Count auto/auto_parent preview rows in a prepared folder scope."""
    if not folder_ids:
        return 0
    if selection_type not in {"auto", "auto_parent"}:
        raise FolderPreviewCandidateError(f"Unsupported selection_type for count: {selection_type}")

    placeholders = ",".join("?" for _ in folder_ids)
    row = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM folder_preview_items
        WHERE selection_type = ?
          AND folder_id IN ({placeholders})
        """,
        (selection_type, *folder_ids),
    ).fetchone()
    return int(row[0])


def _preview_media_ids_for_folder_ids(
    connection: sqlite3.Connection,
    *,
    folder_ids: tuple[int, ...] | None,
    selection_type: str,
) -> set[int]:
    """Return media identities affected by replacing one stored selection."""
    if selection_type not in {"auto", "auto_parent"}:
        raise FolderPreviewCandidateError(f"Unsupported selection_type for media lookup: {selection_type}")
    if folder_ids == ():
        return set()

    params: tuple[object, ...] = (selection_type,)
    folder_clause = ""
    if folder_ids is not None:
        placeholders = ",".join("?" for _ in folder_ids)
        folder_clause = f" AND folder_id IN ({placeholders})"
        params = (selection_type, *folder_ids)
    return {
        int(row["media_id"])
        for row in connection.execute(
            f"SELECT DISTINCT media_id FROM folder_preview_items WHERE selection_type = ?{folder_clause}",
            params,
        )
    }


def _delete_preview_rows_for_folder_ids(
    connection: sqlite3.Connection,
    *,
    folder_ids: tuple[int, ...],
    selection_type: str,
) -> int:
    """Delete auto/auto_parent preview rows in a prepared folder scope."""
    if not folder_ids:
        return 0
    if selection_type not in {"auto", "auto_parent"}:
        raise FolderPreviewCandidateError(f"Unsupported selection_type for delete: {selection_type}")

    placeholders = ",".join("?" for _ in folder_ids)
    cursor = connection.execute(
        f"""
        DELETE FROM folder_preview_items
        WHERE selection_type = ?
          AND folder_id IN ({placeholders})
        """,
        (selection_type, *folder_ids),
    )
    return max(0, int(cursor.rowcount or 0))


def _branch_scope_label(plan: FolderPreviewBranchPlan) -> str:
    return "entire branch recursively" if plan.recursive else "direct child folders"


def _branch_scope_note(plan: FolderPreviewBranchPlan) -> str:
    if plan.recursive:
        return "Scope: all descendant folders in the selected branch; folders without direct visual media are skipped."
    return "Scope: direct child folders of the selected branch only, without recursion."


def folder_preview_branch_plan_lines(plan: FolderPreviewBranchPlan) -> list[str]:
    """Return human-readable dry-run lines for a branch preview plan."""
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]
    total_selected = sum(item.selected_count for item in planned_items)
    total_ready = sum(item.ready_count for item in planned_items)
    total_missing = sum(item.missing_count for item in planned_items)

    lines = [
        f"Catalog 2.0 – folder preview plan ({_branch_scope_label(plan)})",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.direct_child_count}",
        f"folders_to_apply: {len(planned_items)}",
        f"folders_skipped: {len(skipped_items)}",
        f"selected_preview_items: {total_selected}",
        f"ready_thumbnail_files: {total_ready}",
        f"missing_or_not_ready: {total_missing}",
        "",
        "Mode: plan only. Nothing is written, generated, or deleted.",
        _branch_scope_note(plan),
        "",
    ]

    if not plan.items:
        lines.append("Folders in scope: none.")
        return lines

    lines.append("Planned folders:")

    for index, item in enumerate(plan.items, start=1):
        if item.report is None:
            lines.append(
                f"{index}. SKIP {item.folder_rel_path or '[root]'} | "
                f"visual_media={item.visual_media_count} | {item.skip_reason}"
            )
            continue

        lines.append(
            f"{index}. APPLY {item.folder_rel_path} | "
            f"visual_media={item.visual_media_count} | "
            f"selected={item.selected_count} | ready={item.ready_count} | missing={item.missing_count}"
        )

    return lines


def folder_preview_branch_apply_result_lines(result: FolderPreviewBranchApplyResult) -> list[str]:
    """Return human-readable result lines for applying a branch preview plan."""
    plan = result.plan
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]
    total_deleted = sum(item.deleted_auto_rows for item in result.applied_results)
    total_inserted = sum(item.inserted_auto_rows for item in result.applied_results)

    lines = [
        f"Catalog 2.0 – store folder previews ({_branch_scope_label(plan)})",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.direct_child_count}",
        f"folders_planned: {len(planned_items)}",
        f"folders_applied: {len(result.applied_results)}",
        f"folders_skipped: {len(skipped_items)}",
        "",
        "Writes: catalog.db / folder_preview_items / selection_type='auto' only.",
        _branch_scope_note(plan),
        "Does not generate thumbnails, delete cache files, or change source files.",
        "",
        f"previous auto rows deleted: {total_deleted}",
        f"auto rows inserted: {total_inserted}",
        "",
    ]

    if not plan.items:
        lines.append("Folders in scope: none.")
        return lines

    lines.append("Result by folder:")
    result_by_folder = {item.report.folder_id: item for item in result.applied_results}

    for index, item in enumerate(plan.items, start=1):
        if item.report is None:
            lines.append(
                f"{index}. SKIP {item.folder_rel_path or '[root]'} | "
                f"visual_media={item.visual_media_count} | {item.skip_reason}"
            )
            continue

        applied = result_by_folder.get(item.folder_id)
        if applied is None:
            lines.append(f"{index}. NOT STORED {item.folder_rel_path}")
            continue

        lines.append(
            f"{index}. APPLY {item.folder_rel_path} | "
            f"selected={item.selected_count} | ready={item.ready_count} | "
            f"deleted={applied.deleted_auto_rows} | inserted={applied.inserted_auto_rows} | stored={applied.stored_auto_rows}"
        )

    return lines


def folder_preview_build_branch_result_lines(
    result: FolderPreviewBuildBranchResult,
    *,
    include_folder_details: bool = True,
) -> list[str]:
    """Return human-readable result lines for building branch folder previews."""
    plan = result.plan
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]
    total_deleted = sum(item.deleted_auto_rows for item in result.applied_results)
    total_inserted = sum(item.inserted_auto_rows for item in result.applied_results)
    generated = sum(1 for item in result.thumbnail_results if item.action == "created")
    reused = sum(1 for item in result.thumbnail_results if item.action == "reused")
    errors = sum(1 for item in result.thumbnail_results if item.action == "error")

    lines = [
        f"Catalog 2.0 – build folder previews including thumbnails ({_branch_scope_label(plan)})",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.direct_child_count}",
        f"folders_planned: {len(planned_items)}",
        f"folders_applied: {len(result.applied_results)}",
        f"folders_skipped: {len(skipped_items)}",
        "",
        "Writes: catalog.db / folder_preview_items + missing thumbnail cache files for selected candidates only.",
        _branch_scope_note(plan),
        "Does not change source files and does not generate every thumbnail in the folders.",
        "photo_tile remains in dynamic cache; cleanup treats it as lower-priority for deletion when referenced from folder_preview_items.",
        "",
        f"previous auto rows deleted: {total_deleted}",
        f"auto rows inserted: {total_inserted}",
        f"thumbnail reused: {reused}",
        f"thumbnail created: {generated}",
        f"thumbnail errors: {errors}",
        "",
    ]

    if not plan.items:
        lines.append("Folders in scope: none.")
        return lines

    if not include_folder_details:
        error_results = [item for item in result.thumbnail_results if item.action == "error"]
        if error_results:
            lines.extend(["", "Thumbnail errors – first 10:"])
            for item in error_results[:10]:
                lines.append(f"- {item.candidate.rel_path}: {item.message}")
        return lines

    lines.append("Result by folder:")
    result_by_folder = {item.report.folder_id: item for item in result.applied_results}

    for index, item in enumerate(plan.items, start=1):
        if item.report is None:
            lines.append(
                f"{index}. SKIP {item.folder_rel_path or '[root]'} | "
                f"visual_media={item.visual_media_count} | {item.skip_reason}"
            )
            continue

        applied = result_by_folder.get(item.folder_id)
        if applied is None:
            lines.append(f"{index}. NOT STORED {item.folder_rel_path}")
            continue

        folder_results = [
            thumb_result
            for thumb_result in result.thumbnail_results
            if thumb_result.candidate.media_id in {candidate.media_id for candidate in item.report.candidates}
        ]
        folder_created = sum(1 for thumb_result in folder_results if thumb_result.action == "created")
        folder_reused = sum(1 for thumb_result in folder_results if thumb_result.action == "reused")
        folder_errors = sum(1 for thumb_result in folder_results if thumb_result.action == "error")

        lines.append(
            f"{index}. BUILD {item.folder_rel_path} | "
            f"selected={item.selected_count} | ready_before={item.ready_count} | "
            f"missing_before={item.missing_count} | reused={folder_reused} | "
            f"created={folder_created} | errors={folder_errors} | stored={applied.stored_auto_rows}"
        )

    error_results = [item for item in result.thumbnail_results if item.action == "error"]
    if error_results:
        lines.extend(["", "Thumbnail errors – first 10:"])
        for item in error_results[:10]:
            lines.append(f"- {item.candidate.rel_path}: {item.message}")

    return lines


def _parent_branch_scope_label(plan: FolderPreviewParentBranchPlan) -> str:
    return "entire branch recursively, bottom-up" if plan.recursive else "direct child folders"


def _parent_branch_scope_note(plan: FolderPreviewParentBranchPlan) -> str:
    if plan.recursive:
        return "Scope: all descendant folders in the selected branch; the plan is built bottom-up from the deepest folders."
    return "Scope: direct child folders of the selected branch only, without recursion."


def folder_preview_parent_branch_plan_lines(plan: FolderPreviewParentBranchPlan) -> list[str]:
    """Return human-readable dry-run lines for a parent-preview branch plan."""
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]
    total_source_items = sum(item.source_preview_item_count for item in planned_items)
    total_selected = sum(item.selected_count for item in planned_items)
    total_ready = sum(item.ready_count for item in planned_items)
    total_missing = sum(item.missing_count for item in planned_items)

    lines = [
        f"Catalog 2.0 – parent preview plan ({_parent_branch_scope_label(plan)})",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.candidate_folder_count}",
        f"folders_to_apply: {len(planned_items)}",
        f"folders_skipped: {len(skipped_items)}",
        f"source_preview_items: {total_source_items}",
        f"selected_preview_items: {total_selected}",
        f"ready_thumbnail_files: {total_ready}",
        f"missing_or_not_ready: {total_missing}",
        "",
        "Mode: plan only. Nothing is written, generated, or deleted.",
        _parent_branch_scope_note(plan),
        "Source: weighted effective previews from direct child folders, combining direct and descendant content.",
        "Source media are not read. The DB is not changed. The UI is not changed.",
        "",
    ]

    if not plan.items:
        lines.append("Folders in scope: none.")
        return lines

    lines.append("Planned parent folders:")

    for index, item in enumerate(plan.items, start=1):
        if item.report is None:
            lines.append(
                f"{index}. SKIP {item.folder_rel_path or '[root]'} | "
                f"direct_children={item.direct_child_count} | source_items={item.source_preview_item_count} | {item.skip_reason}"
            )
            continue

        lines.append(
            f"{index}. PARENT APPLY {item.folder_rel_path} | "
            f"direct_children={item.direct_child_count} | "
            f"children_with_previews={item.child_folders_with_stored_previews} | "
            f"source_items={item.source_preview_item_count} | selected={item.selected_count} | "
            f"ready={item.ready_count} | missing={item.missing_count}"
        )

    return lines


def folder_preview_parent_branch_apply_result_lines(
    result: FolderPreviewParentBranchApplyResult,
    *,
    include_folder_details: bool = True,
) -> list[str]:
    """Return human-readable result lines for applying parent previews in a branch."""
    plan = result.plan
    planned_items = [item for item in plan.items if item.report is not None]
    skipped_items = [item for item in plan.items if item.report is None]
    total_deleted = sum(item.deleted_auto_parent_rows for item in result.applied_results)
    total_inserted = sum(item.inserted_auto_parent_rows for item in result.applied_results)

    lines = [
        f"Catalog 2.0 – bulk store parent previews ({_parent_branch_scope_label(plan)})",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.candidate_folder_count}",
        f"folders_planned: {len(planned_items)}",
        f"folders_applied: {len(result.applied_results)}",
        f"folders_skipped: {len(skipped_items)}",
        "",
        "Writes: catalog.db / folder_preview_items / selection_type='auto_parent' only.",
        _parent_branch_scope_note(plan),
        "Does not generate thumbnails, read source media, delete cache files, or change source files.",
        "Source: weighted effective previews from direct child folders, combining direct and descendant content.",
        "",
        f"previous auto_parent rows deleted: {total_deleted}",
        f"auto_parent rows inserted: {total_inserted}",
        "",
    ]

    if not plan.items:
        lines.append("Folders in scope: none.")
        return lines

    if not include_folder_details:
        return lines

    lines.append("Result by folder:")
    result_by_folder = {item.report.folder_id: item for item in result.applied_results}

    for index, item in enumerate(plan.items, start=1):
        if item.report is None:
            lines.append(
                f"{index}. SKIP {item.folder_rel_path or '[root]'} | "
                f"direct_children={item.direct_child_count} | source_items={item.source_preview_item_count} | {item.skip_reason}"
            )
            continue

        applied = result_by_folder.get(item.folder_id)
        if applied is None:
            lines.append(f"{index}. NOT STORED {item.folder_rel_path}")
            continue

        lines.append(
            f"{index}. PARENT APPLY {item.folder_rel_path} | "
            f"source_items={item.source_preview_item_count} | selected={item.selected_count} | "
            f"ready={item.ready_count} | missing={item.missing_count} | "
            f"deleted={applied.deleted_auto_parent_rows} | inserted={applied.inserted_auto_parent_rows} | "
            f"stored={applied.stored_auto_parent_rows}"
        )

    return lines



def _skip_reason_counts(items: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        reason = getattr(item, "skip_reason", "") or "no reason"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def folder_preview_tree_plan_lines(plan: FolderPreviewTreePlan) -> list[str]:
    """Return human-readable dry-run lines for a unified preview-tree plan."""
    planned_auto_items = [item for item in plan.auto_items if item.report is not None]
    skipped_auto_items = [item for item in plan.auto_items if item.report is None]
    planned_parent_items = [item for item in plan.parent_items if item.report is not None]
    skipped_parent_items = [item for item in plan.parent_items if item.report is None]
    total_auto_selected = sum(item.selected_count for item in planned_auto_items)
    total_auto_ready = sum(item.ready_count for item in planned_auto_items)
    total_auto_missing = sum(item.missing_count for item in planned_auto_items)
    total_parent_source = sum(item.source_preview_item_count for item in planned_parent_items)
    total_parent_selected = sum(item.selected_count for item in planned_parent_items)

    lines = [
        "Catalog 2.0 – unified folder preview tree plan",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.candidate_folder_count}",
        f"auto_folders_to_build: {len(planned_auto_items)}",
        f"auto_folders_skipped: {len(skipped_auto_items)}",
        f"auto_selected_items: {total_auto_selected}",
        f"auto_ready_thumbnail_files: {total_auto_ready}",
        f"auto_missing_or_not_ready: {total_auto_missing}",
        f"parent_folders_to_apply: {len(planned_parent_items)}",
        f"parent_folders_skipped: {len(skipped_parent_items)}",
        f"parent_source_preview_items: {total_parent_source}",
        f"parent_selected_items: {total_parent_selected}",
        f"auto_rows_to_delete_in_scope: {plan.existing_auto_rows_in_scope}",
        f"auto_parent_rows_to_delete_in_scope: {plan.existing_auto_parent_rows_in_scope}",
        "",
        "Mode: plan only. Nothing is written, generated, or deleted.",
        "Scope: selected folder including all descendants.",
        "Plan: direct auto previews for content folders first, then bottom-up auto_parent previews.",
        "Execute first replaces old auto/auto_parent rows in this scope with the current plan.",
        "Execute creates missing thumbnails only for selected auto candidates.",
        "Manual rows, thumbnail cache files, scan data, and source files remain unchanged.",
        "",
    ]

    if skipped_auto_items:
        lines.append("Auto SKIP reasons:")
        for reason, count in _skip_reason_counts(skipped_auto_items).items():
            lines.append(f"- {reason}: {count}")
        lines.append("")

    if skipped_parent_items:
        lines.append("Parent SKIP reasons:")
        for reason, count in _skip_reason_counts(skipped_parent_items).items():
            lines.append(f"- {reason}: {count}")
        lines.append("")

    if planned_auto_items:
        lines.append("Auto preview planned folders:")
        for index, item in enumerate(planned_auto_items, start=1):
            lines.append(
                f"{index}. AUTO BUILD {item.folder_rel_path or '[root]'} | "
                f"visual_media={item.visual_media_count} | selected={item.selected_count} | "
                f"ready={item.ready_count} | missing={item.missing_count}"
            )
        lines.append("")

    if planned_parent_items:
        lines.append("Parent preview planned folders:")
        for index, item in enumerate(planned_parent_items, start=1):
            lines.append(
                f"{index}. PARENT APPLY {item.folder_rel_path or '[root]'} | "
                f"direct_children={item.direct_child_count} | "
                f"source_items={item.source_preview_item_count} | selected={item.selected_count} | "
                f"ready={item.ready_count} | missing={item.missing_count}"
            )

    return lines


def folder_preview_tree_build_result_lines(
    result: FolderPreviewTreeBuildResult,
    *,
    include_folder_details: bool = True,
) -> list[str]:
    """Return human-readable result lines for unified preview-tree build."""
    plan = result.plan
    planned_auto_items = [item for item in plan.auto_items if item.report is not None]
    skipped_auto_items = [item for item in plan.auto_items if item.report is None]
    planned_parent_items = [item for item in plan.parent_items if item.report is not None]
    skipped_parent_items = [item for item in plan.parent_items if item.report is None]
    auto_deleted = result.deleted_auto_rows
    auto_inserted = sum(item.inserted_auto_rows for item in result.auto_applied_results)
    parent_deleted = result.deleted_auto_parent_rows
    parent_inserted = sum(item.inserted_auto_parent_rows for item in result.parent_applied_results)
    thumb_created = sum(1 for item in result.thumbnail_results if item.action == "created")
    thumb_reused = sum(1 for item in result.thumbnail_results if item.action == "reused")
    thumb_errors = sum(1 for item in result.thumbnail_results if item.action == "error")

    lines = [
        "Catalog 2.0 – unified folder preview tree build",
        "=" * 70,
        f"branch: {plan.branch_rel_path or '[root]'}",
        f"name: {plan.branch_name}",
        f"variant: {plan.variant}",
        f"requested_count: {plan.requested_count}",
        f"candidate_folder_count: {plan.candidate_folder_count}",
        f"auto_folders_planned: {len(planned_auto_items)}",
        f"auto_folders_applied: {len(result.auto_applied_results)}",
        f"auto_folders_skipped: {len(skipped_auto_items)}",
        f"parent_folders_planned: {len(planned_parent_items)}",
        f"parent_folders_applied: {len(result.parent_applied_results)}",
        f"parent_folders_skipped: {len(skipped_parent_items)}",
        "",
        "Writes: catalog.db / folder_preview_items / selection_type='auto' and 'auto_parent'.",
        "Rebuild: old auto/auto_parent rows in the branch scope are replaced with the current plan.",
        "Thumbnails: only missing cache files for selected auto candidates are created.",
        "Does not change source files, scan data, UI layout, manual preview rows, or DB schema.",
        "",
        f"previous auto rows deleted: {auto_deleted}",
        f"auto rows inserted: {auto_inserted}",
        f"previous auto_parent rows deleted: {parent_deleted}",
        f"auto_parent rows inserted: {parent_inserted}",
        f"thumbnail reused: {thumb_reused}",
        f"thumbnail created: {thumb_created}",
        f"thumbnail errors: {thumb_errors}",
        "",
    ]

    if skipped_auto_items:
        lines.append("Auto SKIP reasons:")
        for reason, count in _skip_reason_counts(skipped_auto_items).items():
            lines.append(f"- {reason}: {count}")
        lines.append("")

    if skipped_parent_items:
        lines.append("Parent SKIP reasons:")
        for reason, count in _skip_reason_counts(skipped_parent_items).items():
            lines.append(f"- {reason}: {count}")
        lines.append("")

    error_results = [item for item in result.thumbnail_results if item.action == "error"]
    if error_results:
        lines.append("Thumbnail errors – first 10:")
        for item in error_results[:10]:
            lines.append(f"- {item.candidate.rel_path}: {item.message}")
        lines.append("")

    if not include_folder_details:
        return lines

    auto_result_by_folder = {item.report.folder_id: item for item in result.auto_applied_results}
    parent_result_by_folder = {item.report.folder_id: item for item in result.parent_applied_results}

    if planned_auto_items:
        lines.append("Auto result by folder:")
        for index, item in enumerate(planned_auto_items, start=1):
            applied = auto_result_by_folder.get(item.folder_id)
            if applied is None:
                lines.append(f"{index}. NOT STORED {item.folder_rel_path}")
                continue
            folder_results = [
                thumb_result
                for thumb_result in result.thumbnail_results
                if item.report is not None and thumb_result.candidate.media_id in {candidate.media_id for candidate in item.report.candidates}
            ]
            folder_created = sum(1 for thumb_result in folder_results if thumb_result.action == "created")
            folder_reused = sum(1 for thumb_result in folder_results if thumb_result.action == "reused")
            folder_errors = sum(1 for thumb_result in folder_results if thumb_result.action == "error")
            lines.append(
                f"{index}. AUTO BUILD {item.folder_rel_path or '[root]'} | "
                f"selected={item.selected_count} | reused={folder_reused} | created={folder_created} | "
                f"errors={folder_errors} | stored={applied.stored_auto_rows}"
            )
        lines.append("")

    if planned_parent_items:
        lines.append("Parent result by folder:")
        for index, item in enumerate(planned_parent_items, start=1):
            applied = parent_result_by_folder.get(item.folder_id)
            if applied is None:
                lines.append(f"{index}. NOT STORED {item.folder_rel_path}")
                continue
            lines.append(
                f"{index}. PARENT APPLY {item.folder_rel_path or '[root]'} | "
                f"source_items={item.source_preview_item_count} | selected={item.selected_count} | "
                f"stored={applied.stored_auto_parent_rows}"
            )

    return lines


def folder_preview_clear_auto_result_lines(result: FolderPreviewClearAutoResult) -> list[str]:
    """Return human-readable clear-auto result lines for the CLI."""
    lines = [
        "Catalog 2.0 – clear automatic folder preview metadata",
        "=" * 70,
        f"scope: {result.scope}",
        f"folder: {result.folder_rel_path or result.folder_name}",
        f"name: {result.folder_name}",
        "",
        "Writes: catalog.db / folder_preview_items / selection_type='auto' only.",
        "Does not delete manual rows, thumbnail cache files, or source files.",
        "",
        f"auto rows deleted: {result.deleted_auto_rows}",
        f"remaining auto rows total: {result.remaining_auto_rows}",
    ]
    return lines


def folder_preview_apply_result_lines(result: FolderPreviewApplyResult) -> list[str]:
    """Return human-readable apply result lines for the CLI."""
    report = result.report
    lines = [
        "Catalog 2.0 – store folder preview candidates",
        "=" * 70,
        f"folder: {report.folder_rel_path or '[root]'}",
        f"name: {report.folder_name}",
        f"variant: {report.variant}",
        f"requested_count: {report.requested_count}",
        f"visual_media_count: {report.visual_media_count}",
        f"selected_count: {report.selected_count}",
        f"ready_thumbnail_files: {report.ready_count}",
        f"missing_or_not_ready: {report.missing_count}",
        "",
        "Writes: catalog.db / folder_preview_items / selection_type='auto' only.",
        "Does not generate thumbnails, delete cache files, or change source files.",
        "",
        f"previous auto rows deleted for folder: {result.deleted_auto_rows}",
        f"auto rows inserted: {result.inserted_auto_rows}",
        f"stored auto rows after write: {result.stored_auto_rows}",
        "",
    ]

    if not report.candidates:
        lines.append("Stored: no candidates for this folder.")
        return lines

    lines.append("Stored items:")

    for candidate in report.candidates:
        if candidate.thumbnail_status == "ready" and candidate.thumbnail_file_exists:
            cache_state = "ready file"
        elif candidate.thumbnail_status == "ready":
            cache_state = "ready DB, file missing"
        elif candidate.thumbnail_status:
            cache_state = candidate.thumbnail_status
        else:
            cache_state = "thumbnail DB record missing"

        lines.append(
            "{position}. {media_type:<5} {file_name} | {thumb}:{variant} | {cache_state}".format(
                position=candidate.position,
                media_type=candidate.media_type,
                file_name=candidate.file_name,
                thumb=candidate.thumbnail_type,
                variant=candidate.variant_key,
                cache_state=cache_state,
            )
        )
        lines.append(f"   path: {candidate.rel_path}")

        if candidate.thumbnail_output_rel_path:
            lines.append(f"   cache: {candidate.thumbnail_output_rel_path}")

    return lines


def folder_preview_parent_candidate_report_lines(report: FolderPreviewParentCandidateReport) -> list[str]:
    """Return human-readable parent-preview diagnostic lines for the CLI."""
    lines = [
        "Catalog 2.0 – parent folder preview candidates dry run",
        "=" * 70,
        f"folder: {report.folder_rel_path or '[root]'}",
        f"name: {report.folder_name}",
        f"variant: {report.variant}",
        f"requested_count: {report.requested_count}",
        f"direct_child_folders: {report.direct_child_count}",
        f"child_folders_with_stored_previews: {report.child_folders_with_stored_previews}",
        f"source_preview_items: {report.source_preview_item_count}",
        f"selected_count: {report.selected_count}",
        f"ready_thumbnail_files: {report.ready_count}",
        f"missing_or_not_ready: {report.missing_count}",
        "",
        "Mode: read-only. Nothing is written, generated, or deleted.",
        "Source: weighted effective previews from direct child folders, combining direct and descendant content.",
        "Source media are not read. The DB is not changed. The UI is not changed.",
        "",
    ]

    if not report.candidates:
        lines.append(
            "Candidates: none. Direct child folders have no usable auto or auto_parent preview sources."
        )
        return lines

    lines.append("Candidates for possible future parent preview storage:")

    for candidate in report.candidates:
        if candidate.thumbnail_status == "ready" and candidate.thumbnail_file_exists:
            cache_state = "ready file"
        elif candidate.thumbnail_status == "ready":
            cache_state = "ready DB, file missing"
        elif candidate.thumbnail_status:
            cache_state = candidate.thumbnail_status
        else:
            cache_state = "thumbnail DB record missing"

        lines.append(
            "{position}. {media_type:<5} {file_name} | {thumb}:{variant} | {cache_state}".format(
                position=candidate.position,
                media_type=candidate.media_type,
                file_name=candidate.file_name,
                thumb=candidate.thumbnail_type,
                variant=candidate.variant_key,
                cache_state=cache_state,
            )
        )
        lines.append(
            f"   from folder: {candidate.source_folder_rel_path} "
            f"(preview position {candidate.source_preview_position})"
        )
        lines.append(f"   path: {candidate.rel_path}")

        if candidate.thumbnail_output_rel_path:
            lines.append(f"   cache: {candidate.thumbnail_output_rel_path}")

    return lines


def folder_preview_parent_apply_result_lines(result: FolderPreviewParentApplyResult) -> list[str]:
    """Return human-readable result lines for storing a parent-derived preview."""
    report = result.report
    lines = [
        "Catalog 2.0 – store parent folder preview",
        "=" * 70,
        f"folder: {report.folder_rel_path or '[root]'}",
        f"name: {report.folder_name}",
        f"variant: {report.variant}",
        f"requested_count: {report.requested_count}",
        f"direct_child_folders: {report.direct_child_count}",
        f"child_folders_with_stored_previews: {report.child_folders_with_stored_previews}",
        f"source_preview_items: {report.source_preview_item_count}",
        f"selected_count: {report.selected_count}",
        f"ready_thumbnail_files: {report.ready_count}",
        f"missing_or_not_ready: {report.missing_count}",
        "",
        "Writes: catalog.db / folder_preview_items / selection_type='auto_parent' only.",
        "Source: weighted effective previews from direct child folders, combining direct and descendant content.",
        "Does not generate thumbnails, read source media, delete cache files, or change source files.",
        "",
        f"previous auto_parent rows deleted for folder: {result.deleted_auto_parent_rows}",
        f"auto_parent rows inserted: {result.inserted_auto_parent_rows}",
        f"stored auto_parent rows after write: {result.stored_auto_parent_rows}",
        "",
    ]

    if not report.candidates:
        lines.append("Stored: no candidates for this parent folder.")
        return lines

    lines.append("Stored parent items:")

    for candidate in report.candidates:
        if candidate.thumbnail_status == "ready" and candidate.thumbnail_file_exists:
            cache_state = "ready file"
        elif candidate.thumbnail_status == "ready":
            cache_state = "ready DB, file missing"
        elif candidate.thumbnail_status:
            cache_state = candidate.thumbnail_status
        else:
            cache_state = "thumbnail DB record missing"

        lines.append(
            "{position}. {media_type:<5} {file_name} | {thumb}:{variant} | {cache_state}".format(
                position=candidate.position,
                media_type=candidate.media_type,
                file_name=candidate.file_name,
                thumb=candidate.thumbnail_type,
                variant=candidate.variant_key,
                cache_state=cache_state,
            )
        )
        lines.append(
            f"   from folder: {candidate.source_folder_rel_path} "
            f"(preview position {candidate.source_preview_position})"
        )
        lines.append(f"   path: {candidate.rel_path}")

        if candidate.thumbnail_output_rel_path:
            lines.append(f"   cache: {candidate.thumbnail_output_rel_path}")

    return lines


def folder_preview_candidate_report_lines(report: FolderPreviewCandidateReport) -> list[str]:
    """Return human-readable report lines for the CLI."""
    lines = [
        "Catalog 2.0 – folder preview candidates dry run",
        "=" * 70,
        f"folder: {report.folder_rel_path or '[root]'}",
        f"name: {report.folder_name}",
        f"variant: {report.variant}",
        f"requested_count: {report.requested_count}",
        f"visual_media_count: {report.visual_media_count}",
        f"selected_count: {report.selected_count}",
        f"ready_thumbnail_files: {report.ready_count}",
        f"missing_or_not_ready: {report.missing_count}",
        "",
        "Mode: read-only. Nothing is written, generated, or deleted.",
        "",
    ]

    if not report.candidates:
        lines.append("Candidates: no available candidates in the selected folder.")
        return lines

    lines.append("Candidates:")

    for candidate in report.candidates:
        if candidate.thumbnail_status == "ready" and candidate.thumbnail_file_exists:
            cache_state = "ready file"
        elif candidate.thumbnail_status == "ready":
            cache_state = "ready DB, file missing"
        elif candidate.thumbnail_status:
            cache_state = candidate.thumbnail_status
        else:
            cache_state = "thumbnail DB record missing"

        lines.append(
            "{position}. {media_type:<5} {file_name} | {thumb}:{variant} | {cache_state}".format(
                position=candidate.position,
                media_type=candidate.media_type,
                file_name=candidate.file_name,
                thumb=candidate.thumbnail_type,
                variant=candidate.variant_key,
                cache_state=cache_state,
            )
        )
        lines.append(f"   path: {candidate.rel_path}")

        if candidate.thumbnail_output_rel_path:
            lines.append(f"   cache: {candidate.thumbnail_output_rel_path}")

    return lines


def _normalize_folder_rel_path(value: str) -> str:
    try:
        return normalize_catalog_relative_path(value, allow_root=True)
    except PathValidationError as exc:
        raise FolderPreviewCandidateError(f"Invalid folder path: {exc}") from exc


def _non_negative_int(value: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise FolderPreviewCandidateError(f"{name} must be an integer.") from exc

    if number < 0:
        raise FolderPreviewCandidateError(f"{name} must not be negative.")

    return number


def _preview_count(value: int) -> int:
    number = _non_negative_int(value, "preview-count")

    if number < 1 or number > 12:
        raise FolderPreviewCandidateError("preview-count must be in the range 1 to 12.")

    return number


def _available_folder(connection: sqlite3.Connection, folder_rel_path: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, rel_path, name
        FROM folders
        WHERE path_key = ?
          AND is_available = 1
        """,
        (catalog_path_key(folder_rel_path),),
    ).fetchone()

    if row is None:
        raise FolderPreviewCandidateError(
            f"Folder does not exist or is not available: {folder_rel_path or '[root]'}"
        )

    return row


def _direct_child_folder_rows(connection: sqlite3.Connection, parent_folder_id: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT
                id,
                rel_path,
                name,
                sort_key
            FROM folders
            WHERE parent_id = ?
              AND is_available = 1
            ORDER BY sort_key, name, id
            """,
            (parent_folder_id,),
        )
    )


def _folder_depth(rel_path: str) -> int:
    if not rel_path:
        return 0
    return rel_path.count("/") + 1


def _descendant_folder_rows(
    connection: sqlite3.Connection,
    branch_folder_id: int,
    branch_rel_path: str,
) -> list[sqlite3.Row]:
    """Return all active descendant folders of the selected branch.

    The branch folder itself is intentionally excluded. Folders that contain
    only child folders are still returned here and later become SKIP plan items
    with reason "no direct visual media".
    """
    if branch_rel_path == "":
        return list(
            connection.execute(
                """
                SELECT
                    id,
                    rel_path,
                    name,
                    sort_key
                FROM folders
                WHERE id <> ?
                  AND is_available = 1
                ORDER BY rel_path, sort_key, name, id
                """,
                (branch_folder_id,),
            )
        )

    prefix = branch_rel_path + "/"
    return list(
        connection.execute(
            """
            SELECT
                id,
                rel_path,
                name,
                sort_key
            FROM folders
            WHERE is_available = 1
              AND substr(rel_path, 1, ?) = ?
            ORDER BY rel_path, sort_key, name, id
            """,
            (len(prefix), prefix),
        )
    )


def _stored_preview_rows_for_child_folders(
    connection: sqlite3.Connection,
    *,
    parent_folder_id: int,
    planned_parent_reports: dict[int, FolderPreviewParentCandidateReport] | None = None,
    planned_auto_reports: dict[int, FolderPreviewCandidateReport] | None = None,
    use_stored_preview_rows: bool = True,
    requested_count: int = 6,
    variant: int = 0,
    direct_visual_counts: Mapping[int, int] | None = None,
    recursive_visual_counts: Mapping[int, int] | None = None,
) -> list[Mapping[str, object]]:
    """Return preview source items stored/planned on direct child folders.

    Each child contributes one effective representation selected from the
    combination of its direct auto rows and its auto_parent rows. Planned rows
    from the current recursive tree plan override stored rows of the same type.
    Thumbnail rows are optional so the report can expose missing/stale cache
    state.
    """
    child_rows = _direct_child_folder_rows(connection, parent_folder_id)
    child_order = [int(row["id"]) for row in child_rows]
    grouped: dict[int, dict[str, list[Mapping[str, object]]]] = {
        child_id: {"auto": [], "auto_parent": []}
        for child_id in child_order
    }

    rows: list[sqlite3.Row] = []
    if use_stored_preview_rows:
        rows = list(
            connection.execute(
                """
                SELECT
                    child.id AS source_folder_id,
                    child.rel_path AS source_folder_rel_path,
                    child.name AS source_folder_name,
                    fpi.selection_type AS selection_type,
                    fpi.position AS source_preview_position,
                    mf.id AS media_id,
                    mf.rel_path AS rel_path,
                    mf.file_name AS file_name,
                    mf.extension AS extension,
                    mf.media_type AS media_type,
                    t.thumbnail_type AS thumbnail_type,
                    t.variant_key AS variant_key,
                    t.output_rel_path AS output_rel_path,
                    t.status AS status
                FROM folders AS child
                JOIN folder_preview_items AS fpi
                  ON fpi.folder_id = child.id
                 AND fpi.selection_type IN ('auto', 'auto_parent')
                JOIN media_files AS mf
                  ON mf.id = fpi.media_id
                 AND mf.is_available = 1
                 AND mf.media_type IN ('image', 'gif', 'video')
                LEFT JOIN thumbnails AS t
                  ON t.media_id = mf.id
                 AND (
                        (mf.media_type = 'image' AND t.thumbnail_type = 'photo_tile' AND t.variant_key = 'default')
                     OR (mf.media_type = 'gif'   AND t.thumbnail_type = 'gif_preview' AND t.variant_key = 'default')
                     OR (mf.media_type = 'video' AND t.thumbnail_type = 'video_poster' AND t.variant_key = 'default')
                 )
                WHERE child.parent_id = ?
                  AND child.is_available = 1
                ORDER BY child.sort_key, child.name, child.id, fpi.selection_type, fpi.position
                """,
                (parent_folder_id,),
            )
        )

    for row in rows:
        child_id = int(row["source_folder_id"])
        if child_id in grouped:
            grouped[child_id][str(row["selection_type"])].append(row)

    planned_auto_reports = planned_auto_reports or {}
    for child_id, report in planned_auto_reports.items():
        if child_id in grouped:
            grouped[child_id]["auto"] = _source_rows_from_auto_report(report)

    planned_parent_reports = planned_parent_reports or {}
    for child_id, report in planned_parent_reports.items():
        if child_id in grouped:
            grouped[child_id]["auto_parent"] = _source_rows_from_parent_report(report)

    if direct_visual_counts is None or recursive_visual_counts is None:
        direct_visual_counts, recursive_visual_counts = folder_preview_visual_media_count_maps(connection)

    result: list[Mapping[str, object]] = []
    for child_id in child_order:
        direct_count = int(direct_visual_counts.get(child_id, 0))
        recursive_count = int(recursive_visual_counts.get(child_id, direct_count))
        result.extend(
            _effective_preview_rows(
                grouped[child_id]["auto"],
                grouped[child_id]["auto_parent"],
                requested_count=requested_count,
                variant=variant,
                direct_visual_media_count=direct_count,
                descendant_visual_media_count=max(0, recursive_count - direct_count),
            )
        )

    return result


def _effective_preview_rows(
    auto_rows: list[Mapping[str, object]],
    auto_parent_rows: list[Mapping[str, object]],
    *,
    requested_count: int = 6,
    variant: int = 0,
    direct_visual_media_count: int | None = None,
    descendant_visual_media_count: int | None = None,
) -> list[Mapping[str, object]]:
    """Return one weighted deterministic representation from direct and child rows."""
    direct_count = (
        max(0, int(direct_visual_media_count))
        if direct_visual_media_count is not None
        else len(auto_rows)
    )
    descendant_count = (
        max(0, int(descendant_visual_media_count))
        if descendant_visual_media_count is not None
        else len(auto_parent_rows)
    )
    return _weighted_select_rows_by_source(
        [
            ("direct", list(auto_rows), direct_count),
            ("descendants", list(auto_parent_rows), descendant_count),
        ],
        requested_count=requested_count,
        variant=variant,
    )


def effective_folder_preview_rows(
    auto_rows: list[Mapping[str, object]],
    auto_parent_rows: list[Mapping[str, object]],
    *,
    requested_count: int = 6,
    variant: int = 0,
    direct_visual_media_count: int | None = None,
    descendant_visual_media_count: int | None = None,
) -> list[Mapping[str, object]]:
    """Public read helper shared by preview propagation and the browser API."""
    return _effective_preview_rows(
        auto_rows,
        auto_parent_rows,
        requested_count=requested_count,
        variant=variant,
        direct_visual_media_count=direct_visual_media_count,
        descendant_visual_media_count=descendant_visual_media_count,
    )


def _weighted_select_rows_by_source(
    sources: list[tuple[object, list[Mapping[str, object]], int]],
    *,
    requested_count: int,
    variant: int,
) -> list[Mapping[str, object]]:
    """Allocate preview slots by sqrt(media_count), then select evenly per source."""
    usable_sources = [
        (source_key, list(rows), max(1, int(media_count)))
        for source_key, rows, media_count in sources
        if rows
    ]
    if not usable_sources:
        return []

    capacities = [len(rows) for _, rows, _ in usable_sources]
    weights = [math.sqrt(media_count) for _, _, media_count in usable_sources]
    allocations = _weighted_slot_allocations(
        capacities=capacities,
        weights=weights,
        requested_count=requested_count,
    )

    selected: list[Mapping[str, object]] = []
    for (_, rows, _), slot_count in zip(usable_sources, allocations, strict=True):
        if slot_count <= 0:
            continue
        selected.extend(_select_rows(rows, slot_count, variant))
    return selected


def _weighted_slot_allocations(
    *,
    capacities: list[int],
    weights: list[float],
    requested_count: int,
) -> list[int]:
    """Return capped largest-remainder allocations in stable source order."""
    if len(capacities) != len(weights):
        raise FolderPreviewCandidateError("Weighted preview source metadata is inconsistent.")

    safe_capacities = [max(0, int(value)) for value in capacities]
    safe_weights = [max(0.0, float(value)) for value in weights]
    target = min(max(0, int(requested_count)), sum(safe_capacities))
    allocations = [0 for _ in safe_capacities]
    remaining = target

    while remaining > 0:
        active = [
            index for index, capacity in enumerate(safe_capacities)
            if allocations[index] < capacity
        ]
        if not active:
            break

        total_weight = sum(safe_weights[index] for index in active)
        if total_weight <= 0:
            for index in active:
                if remaining <= 0:
                    break
                allocations[index] += 1
                remaining -= 1
            continue

        quotas = {
            index: remaining * safe_weights[index] / total_weight
            for index in active
        }
        base_added = 0
        for index in active:
            available = safe_capacities[index] - allocations[index]
            base = min(available, math.floor(quotas[index]))
            if base <= 0:
                continue
            allocations[index] += base
            base_added += base

        remaining -= base_added
        if remaining <= 0:
            break

        ranked = sorted(
            (
                index for index in active
                if allocations[index] < safe_capacities[index]
            ),
            key=lambda index: (
                -(quotas[index] - math.floor(quotas[index])),
                index,
            ),
        )
        remainder_added = 0
        for index in ranked:
            if remaining <= 0:
                break
            allocations[index] += 1
            remaining -= 1
            remainder_added += 1

        if base_added <= 0 and remainder_added <= 0:
            break

    return allocations


def folder_preview_visual_media_count_maps(
    connection: sqlite3.Connection,
) -> tuple[dict[int, int], dict[int, int]]:
    """Return direct and recursive visual-media counts from stored folder statistics."""
    rows = connection.execute(
        """
        SELECT
            id,
            direct_image_count + direct_gif_count + direct_video_count AS direct_visual_count,
            recursive_image_count + recursive_gif_count + recursive_video_count AS recursive_visual_count
        FROM folders
        WHERE is_available = 1
        """
    ).fetchall()
    direct_counts = {int(row["id"]): int(row["direct_visual_count"]) for row in rows}
    recursive_counts = {int(row["id"]): int(row["recursive_visual_count"]) for row in rows}
    return direct_counts, recursive_counts


def _source_rows_from_auto_report(
    report: FolderPreviewCandidateReport,
) -> list[Mapping[str, object]]:
    """Convert an in-memory planned auto report to child-source rows."""
    return [
        {
            "source_folder_id": report.folder_id,
            "source_folder_rel_path": report.folder_rel_path,
            "source_folder_name": report.folder_name,
            "source_preview_position": candidate.position,
            "media_id": candidate.media_id,
            "rel_path": candidate.rel_path,
            "file_name": candidate.file_name,
            "extension": candidate.extension,
            "media_type": candidate.media_type,
            "thumbnail_type": candidate.thumbnail_type,
            "variant_key": candidate.variant_key,
            "output_rel_path": candidate.thumbnail_output_rel_path,
            "status": candidate.thumbnail_status,
        }
        for candidate in report.candidates
    ]


def _source_rows_from_parent_report(
    report: FolderPreviewParentCandidateReport,
) -> list[Mapping[str, object]]:
    """Convert an in-memory planned parent report to child-source rows."""
    return [
        {
            "source_folder_id": report.folder_id,
            "source_folder_rel_path": report.folder_rel_path,
            "source_folder_name": report.folder_name,
            "source_preview_position": candidate.position,
            "media_id": candidate.media_id,
            "rel_path": candidate.rel_path,
            "file_name": candidate.file_name,
            "extension": candidate.extension,
            "media_type": candidate.media_type,
            "thumbnail_type": candidate.thumbnail_type,
            "variant_key": candidate.variant_key,
            "output_rel_path": candidate.thumbnail_output_rel_path,
            "status": candidate.thumbnail_status,
        }
        for candidate in report.candidates
    ]


def _visual_media_rows(connection: sqlite3.Connection, folder_id: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT
                id,
                rel_path,
                file_name,
                extension,
                media_type,
                sort_key
            FROM media_files
            WHERE folder_id = ?
              AND is_available = 1
              AND media_type IN ('image', 'gif', 'video')
            ORDER BY sort_key, file_name, id
            """,
            (folder_id,),
        )
    )


def _select_rows(rows: list[Mapping[str, object]], requested_count: int, variant: int) -> list[Mapping[str, object]]:
    total = len(rows)

    if total <= requested_count:
        return rows

    indexes = _selection_indexes(total, requested_count, variant)
    return [rows[index] for index in indexes]


def _selection_indexes(total: int, requested_count: int, variant: int) -> list[int]:
    if total <= 0:
        return []

    if requested_count <= 1:
        return [min(total - 1, variant % total)]

    if total <= requested_count:
        return list(range(total))

    if variant == 0:
        raw_indexes = [
            round(index * (total - 1) / (requested_count - 1))
            for index in range(requested_count)
        ]
    else:
        raw_indexes = []
        bucket_size = total / requested_count

        for bucket_index in range(requested_count):
            start = math.floor(bucket_index * bucket_size)
            end = min(total - 1, math.floor((bucket_index + 1) * bucket_size) - 1)

            if end < start:
                end = start

            width = end - start + 1
            offset = (variant - 1) % width
            raw_indexes.append(start + offset)

    return _deduplicate_and_fill_indexes(raw_indexes, total, requested_count)


def _deduplicate_and_fill_indexes(indexes: list[int], total: int, requested_count: int) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()

    for index in indexes:
        safe_index = max(0, min(total - 1, int(index)))

        if safe_index not in seen:
            result.append(safe_index)
            seen.add(safe_index)

    if len(result) < requested_count:
        for index in range(total):
            if index not in seen:
                result.append(index)
                seen.add(index)

            if len(result) >= requested_count:
                break

    result = result[:requested_count]
    result.sort()
    return result


def _parent_candidate_from_row(
    config: Config,
    *,
    position: int,
    row: Mapping[str, object],
) -> FolderPreviewParentCandidate:
    media_type = str(row["media_type"])
    fallback_thumbnail_type, fallback_variant_key = _thumbnail_lookup_key(media_type)
    thumbnail_type = str(row["thumbnail_type"] or fallback_thumbnail_type)
    variant_key = str(row["variant_key"] or fallback_variant_key)
    output_rel_path = str(row["output_rel_path"] or "")
    status = str(row["status"] or "")
    file_exists = _thumbnail_file_exists(config.output_root, output_rel_path)

    return FolderPreviewParentCandidate(
        position=position,
        source_folder_id=int(row["source_folder_id"]),
        source_folder_rel_path=str(row["source_folder_rel_path"]),
        source_folder_name=str(row["source_folder_name"]),
        source_preview_position=int(row["source_preview_position"]),
        media_id=int(row["media_id"]),
        rel_path=str(row["rel_path"]),
        file_name=str(row["file_name"]),
        media_type=media_type,
        extension=str(row["extension"]),
        thumbnail_type=thumbnail_type,
        variant_key=variant_key,
        thumbnail_status=status,
        thumbnail_output_rel_path=output_rel_path,
        thumbnail_file_exists=file_exists,
    )


def _candidate_from_row(
    config: Config,
    connection: sqlite3.Connection,
    *,
    position: int,
    row: sqlite3.Row,
) -> FolderPreviewCandidate:
    thumbnail_type, variant_key = _thumbnail_lookup_key(str(row["media_type"]))
    thumbnail = _thumbnail_row(connection, int(row["id"]), thumbnail_type, variant_key)
    output_rel_path = str(thumbnail["output_rel_path"]) if thumbnail is not None else ""
    status = str(thumbnail["status"]) if thumbnail is not None else ""
    file_exists = _thumbnail_file_exists(config.output_root, output_rel_path)

    return FolderPreviewCandidate(
        position=position,
        media_id=int(row["id"]),
        rel_path=str(row["rel_path"]),
        file_name=str(row["file_name"]),
        media_type=str(row["media_type"]),
        extension=str(row["extension"]),
        thumbnail_type=thumbnail_type,
        variant_key=variant_key,
        thumbnail_status=status,
        thumbnail_output_rel_path=output_rel_path,
        thumbnail_file_exists=file_exists,
    )


def _thumbnail_lookup_key(media_type: str) -> tuple[str, str]:
    if media_type == "image":
        return "photo_tile", PHOTO_TILE_VARIANT_KEY

    if media_type == "gif":
        return "gif_preview", GIF_PREVIEW_VARIANT_KEY

    if media_type == "video":
        return "video_poster", VIDEO_POSTER_VARIANT_KEY

    raise FolderPreviewCandidateError(f"Unsupported media type for folder preview: {media_type}")


def _thumbnail_row(
    connection: sqlite3.Connection,
    media_id: int,
    thumbnail_type: str,
    variant_key: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT output_rel_path, status
        FROM thumbnails
        WHERE media_id = ?
          AND thumbnail_type = ?
          AND variant_key = ?
        """,
        (media_id, thumbnail_type, variant_key),
    ).fetchone()


def _thumbnail_file_exists(output_root: Path, output_rel_path: str) -> bool:
    if not output_rel_path:
        return False

    candidate = output_root / output_rel_path

    try:
        return candidate.exists() and candidate.is_file()
    except OSError:
        return False
