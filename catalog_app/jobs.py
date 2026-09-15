from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .database import open_database, validate_database_runtime
from .models import FolderRecord, MediaRecord, ScanErrorRecord, ScanSkipRecord
from .message_contract import build_backend_message
from .scan_lock import scan_lock_path
from .scan_activate import (
    ScanActivationDecisionRequired,
    ScanActivationResult,
    activate_scan_by_id,
)
from .scan_plan import ScanAction, build_scan_plan
from .scan_store import ScanStageResult, discard_staged_scan_by_id, read_scan_status, stage_scan_plan
from .scanner import ensure_source_root_available, validate_scan_scope
from .folder_preview_candidates import (
    FOLDER_PREVIEW_REQUESTED_COUNT,
    FOLDER_PREVIEW_SELECTION_VARIANT,
    FolderPreviewTreeBuildResult,
    build_folder_preview_tree,
    maintain_folder_previews_for_scopes,
)
from .media_preview_workflow import build_media_previews_for_branch, build_media_previews_for_scope
from .thumbnail_cache import (
    execute_dynamic_thumbnail_cache_cleanup,
    execute_protected_thumbnail_cache_orphan_cleanup,
    generate_gif_previews_for_scope,
    generate_video_frames_for_scope,
    generate_video_posters_for_scope,
    thumbnail_cache_protected_orphan_cleanup_plan_bundle,
)
from .update_plan import (
    build_branch_update_plan_dict,
    build_catalog_update_plan_dict,
)




def _scan_activation_timing_payload(result: ScanActivationResult) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in result.activation_timing:
        params = step.get("params")
        phase = build_backend_message(
            str(step.get("code") or "scan.update.timing.unknown"),
            severity="info",
            params=params if isinstance(params, dict) else {},
        )
        steps.append({
            "phase": phase,
            "seconds": float(step.get("seconds") or 0.0),
            "elapsed_seconds": float(step.get("elapsed_seconds") or 0.0),
        })

    return {
        "total_seconds": result.duration_seconds,
        "steps": steps,
    }


def _scan_stage_result_message(result: ScanStageResult) -> dict[str, Any]:
    return build_backend_message(
        "scan.update.stage.completed" if result.status == "completed" else "scan.update.stage.status",
        severity="success" if result.status == "completed" else "info",
        params={"scan_id": result.scan_id, "status": result.status},
    )


def _scan_activation_result_message(result: ScanActivationResult) -> dict[str, Any]:
    return build_backend_message(
        "scan.update.activation.completed",
        severity="success",
        params={"scan_id": result.scan_id, "mode": result.mode},
    )

class JobBusyError(RuntimeError):
    """Language-neutral reason why a background job could not start."""

    def __init__(self, code: str, *, params: dict[str, Any] | None = None) -> None:
        self.code = code
        self.params = dict(params or {})
        super().__init__(code)

    @classmethod
    def another_job_running(
        cls,
        *,
        requested_job_kind: str,
        active_job_kind: str,
    ) -> "JobBusyError":
        return cls(
            "jobs.start.another_job_running",
            params={
                "requested_job_kind": requested_job_kind,
                "active_job_kind": active_job_kind,
            },
        )

    @classmethod
    def write_scan_active(cls, *, requested_job_kind: str) -> "JobBusyError":
        return cls(
            "jobs.start.write_scan_active",
            params={"requested_job_kind": requested_job_kind},
        )


@dataclass
class RuntimeJob:
    job_id: str
    kind: str
    state: str
    scan_type: str
    branch: str
    started_at: float
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self.state == "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state,
            "is_running": self.is_running,
            "scan_type": self.scan_type,
            "branch": self.branch,
            "started_at": self.started_at,
            "started_at_iso": _unix_time_iso(self.started_at),
            "finished_at": self.finished_at,
            "finished_at_iso": _unix_time_iso(self.finished_at),
            "error": self.error,
            "result": self.result,
        }


@dataclass
class JobManager:
    """In-memory coordinator for server-started long-running jobs."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _job: RuntimeJob | None = None
    _protected_cache_cleanup_candidate_plan: dict[str, object] | None = None

    def _ensure_start_allowed(self, config: Config, *, requested_job_kind: str) -> None:
        """Reject a new job without embedding localized UI text in job logic.

        The caller holds ``self._lock``. The checks intentionally preserve the
        original order: an in-memory running job takes precedence over the
        cross-process write-scan lock.
        """
        active_job = self._job
        if active_job is not None and active_job.is_running:
            raise JobBusyError.another_job_running(
                requested_job_kind=requested_job_kind,
                active_job_kind=active_job.kind,
            )

        if _scan_lock_is_held(scan_lock_path(config.db_path)):
            raise JobBusyError.write_scan_active(
                requested_job_kind=requested_job_kind,
            )

    def start_scan_preview(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start one read-only scan-preview job in a background thread."""
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="scan-preview",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="scan-preview",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_scan_preview,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-scan-preview-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_scan_stage(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start one scan-stage job in a background thread.

        This writes only to staging scan tables. It never activates the scan into
        the active catalog tables and never modifies source media files.
        """
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="scan-stage",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="scan-stage",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_scan_stage,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-scan-stage-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_scan_activate(
        self,
        config: Config,
        *,
        scan_id: int,
        expected_branch: str | None = None,
        missing_action: str = "require_decision",
    ) -> RuntimeJob:
        """Start activation of one explicit completed staged scan in a background thread.

        This may update active catalog tables. If activation needs a missing-items
        decision and no explicit decision was supplied, the job stops with state
        'requires_decision' before active data is changed.
        """
        if scan_id <= 0:
            raise ValueError("scan_id musí být kladné celé číslo.")

        normalized_missing_action = _normalize_missing_action(missing_action)
        if normalized_missing_action != "cancel":
            ensure_source_root_available(config)
        validate_database_runtime(config.db_path)
        metadata = _scan_session_metadata(config.db_path, scan_id)
        branch_for_job = expected_branch if expected_branch is not None else metadata["scope_rel_path"]

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="scan-activate",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="scan-activate",
                state="running",
                scan_type=metadata["scan_type"],
                branch=branch_for_job,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_scan_activate,
                args=(config, job.job_id, scan_id, expected_branch, normalized_missing_action),
                name=f"catalog2-scan-activate-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_catalog_update(self, config: Config) -> RuntimeJob:
        """Start full catalog update as one background workflow.

        The job stages a full scan and immediately activates it with purge
        semantics. Missing items are removed from the catalog and matching
        favorites are cleaned; source media files are never modified.
        """
        ensure_source_root_available(config)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="catalog-update",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="catalog-update",
                state="running",
                scan_type="full",
                branch="",
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_catalog_update,
                args=(config, job.job_id),
                name=f"catalog2-catalog-update-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_gif_preview(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start protected GIF preview generation in a background thread."""
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="gif-preview",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="gif-preview",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_gif_preview,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-gif-preview-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_video_poster(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start protected video poster generation in a background thread."""
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="video-poster",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="video-poster",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_video_poster,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-video-poster-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_video_frames(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start protected video frame generation in a background thread."""
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="video-frames",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="video-frames",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_video_frames,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-video-frames-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self._job is None:
                return None
            return self._job.to_dict()

    def start_dynamic_cache_cleanup(self, config: Config) -> RuntimeJob:
        """Start safe cleanup of unreferenced dynamic thumbnail cache."""
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="dynamic-cache-cleanup",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="dynamic-cache-cleanup",
                state="running",
                scan_type="cache",
                branch="",
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_dynamic_cache_cleanup,
                args=(config, job.job_id),
                name=f"catalog2-dynamic-cache-cleanup-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_protected_cache_orphan_check(self, config: Config) -> RuntimeJob:
        """Start persistent protected-cache orphan check as a standard long job.

        The job creates the audit and the executable cleanup plan in one pass and
        stores only the private candidate plan in server memory for a later clean
        step. Source media, dynamic cache, and settings are not changed.
        """
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="protected-cache-orphan-check",
            )

            self._protected_cache_cleanup_candidate_plan = None
            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="protected-cache-orphan-check",
                state="running",
                scan_type="cache",
                branch="",
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_protected_cache_orphan_check,
                args=(config, job.job_id),
                name=f"catalog2-protected-cache-check-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_protected_cache_orphan_clean(self, config: Config) -> RuntimeJob:
        """Start persistent cleanup for the last protected-cache orphan plan."""
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="protected-cache-orphan-clean",
            )

            candidate_plan = self._protected_cache_cleanup_candidate_plan
            if candidate_plan is None:
                raise ValueError("Run the protected cache check before starting cleanup.")

            self._protected_cache_cleanup_candidate_plan = None
            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="protected-cache-orphan-clean",
                state="running",
                scan_type="cache",
                branch="",
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_protected_cache_orphan_clean,
                args=(config, candidate_plan, job.job_id),
                name=f"catalog2-protected-cache-clean-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_folder_preview_build_tree(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start unified folder-preview tree build in a background thread."""
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="folder-preview-build-tree",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="folder-preview-build-tree",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_folder_preview_build_tree,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-folder-preview-tree-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def start_prepare_previews(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start protected preview preparation for one branch or the whole catalog."""
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="prepare-previews",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="prepare-previews",
                state="running",
                scan_type="branch" if normalized_branch else "full",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_prepare_previews,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-prepare-previews-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job


    def start_update_branch(self, config: Config, *, branch_rel_path: str = "") -> RuntimeJob:
        """Start controlled branch update workflow in a background thread.

        This is the server/API counterpart of CLI update-branch. It runs scan-stage
        and explicit scan-activate for the created scan_id for one concrete branch.
        Preview preparation is intentionally handled by the separate prepare-previews job.
        """
        normalized_branch = validate_scan_scope(config, branch_rel_path)
        if not normalized_branch:
            raise ValueError("update-branch vyžaduje parametr branch s konkrétní složkou.")
        validate_database_runtime(config.db_path)

        with self._lock:
            self._ensure_start_allowed(
                config,
                requested_job_kind="update-branch",
            )

            job = RuntimeJob(
                job_id=uuid.uuid4().hex,
                kind="update-branch",
                state="running",
                scan_type="branch",
                branch=normalized_branch,
                started_at=time.time(),
            )
            self._job = job

            thread = threading.Thread(
                target=self._run_update_branch,
                args=(config, normalized_branch, job.job_id),
                name=f"catalog2-update-branch-{job.job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return job

    def _run_update_branch(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            started = time.monotonic()

            stage_result = stage_scan_plan(
                config.db_path,
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                scope_rel_path=branch_rel_path,
            )

            try:
                activation_result = activate_scan_by_id(
                    config.db_path,
                    scan_id=stage_result.scan_id,
                    expected_scope_rel_path=branch_rel_path,
                    missing_action="require_decision",
                    input_func=_no_input,
                    output_func=_discard_output,
                    favorites_json_path=config.favorites_json,
                )
            except ScanActivationDecisionRequired as exc:
                self._finish(
                    job_id,
                    state="requires_decision",
                    result={
                        "update_branch": True,
                        "branch": branch_rel_path,
                        "stage": _scan_stage_result_dict(config.db_path, stage_result),
                        "activation_decision_required": exc.decision_summary,
                        "stopped_after_phase": "scan-activate",
                        "next_phases_started": False,
                        "writes": {
                            "source_media": False,
                            "media_previews_started": False,
                            "folder_previews_started": False,
                        },
                    },
                    error=str(exc),
                )
                return
            except Exception as exc:
                raise RuntimeError(
                    f"Aktivace fáze 2 selhala po vytvoření scan_id {stage_result.scan_id}. "
                    "Další fáze update-branch nebyly spuštěny. "
                    "Aktivní katalog zůstal beze změny; staged scan session zůstává k dispozici pro kontrolu. "
                    f"Původní chyba: {exc}"
                ) from exc

            payload = _update_branch_result_dict(
                config.db_path,
                branch_rel_path=branch_rel_path,
                started=started,
                stage_result=stage_result,
                activation_result=activation_result,
            )
            self._finish(job_id, state="completed", result=payload, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_scan_preview(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            result = _summarize_scan_preview(
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                branch_rel_path=branch_rel_path,
                db_path=config.db_path,
            )
            self._finish(job_id, state="completed", result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_scan_stage(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            result = stage_scan_plan(
                config.db_path,
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                scope_rel_path=branch_rel_path,
            )
            self._finish(
                job_id,
                state="completed",
                result=_scan_stage_result_dict(config.db_path, result),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_scan_activate(
        self,
        config: Config,
        job_id: str,
        scan_id: int,
        expected_branch: str | None,
        missing_action: str,
    ) -> None:
        try:
            if missing_action == "cancel":
                discard_result = discard_staged_scan_by_id(
                    config.db_path,
                    scan_id=scan_id,
                )
                self._finish(
                    job_id,
                    state="cancelled",
                    result={
                        "activation_cancelled": True,
                        "scan_discarded": True,
                        "scan_id": scan_id,
                        "previous_scan_status": discard_result.previous_status,
                        "deleted_staging": {
                            "folders": discard_result.deleted_folders,
                            "media": discard_result.deleted_media,
                            "errors": discard_result.deleted_errors,
                        },
                        "result_messages": [build_backend_message(
                            "scan.update.activation.cancelled",
                            severity="info",
                            params={"scan_id": scan_id},
                        )],
                        "writes": {
                            "catalog_db": "staging_scan_cancelled",
                            "active_catalog": False,
                            "source_media": False,
                        },
                    },
                    error=None,
                )
                return

            result = activate_scan_by_id(
                config.db_path,
                scan_id=scan_id,
                expected_scope_rel_path=expected_branch,
                missing_action=missing_action,
                input_func=_no_input,
                output_func=_discard_output,
                favorites_json_path=config.favorites_json,
            )

            if _should_return_catalog_update_payload(
                result,
                expected_branch=expected_branch,
                missing_action=missing_action,
            ):
                started = time.monotonic() - result.duration_seconds
                preview_result = _empty_catalog_preview_maintenance_result(
                    result.affected_scopes,
                    reason="Preview preparation is not automatic.",
                )
                payload = _catalog_update_result_dict(
                    config.db_path,
                    started=started,
                    stage_result=None,
                    activation_result=result,
                    preview_result=preview_result,
                    source_action="scan-activate",
                )
            else:
                payload = _scan_activation_result_dict(result)

            self._finish(
                job_id,
                state="completed",
                result=payload,
                error=None,
            )
        except ScanActivationDecisionRequired as exc:
            self._finish(
                job_id,
                state="requires_decision",
                result=exc.decision_summary,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_catalog_update(self, config: Config, job_id: str) -> None:
        try:
            started = time.monotonic()
            branch_rel_path = ""
            stage_result = stage_scan_plan(
                config.db_path,
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                scope_rel_path=branch_rel_path,
            )
            activation_result = activate_scan_by_id(
                config.db_path,
                scan_id=stage_result.scan_id,
                expected_scope_rel_path=branch_rel_path,
                missing_action="purge",
                input_func=_no_input,
                output_func=_discard_output,
                favorites_json_path=config.favorites_json,
            )
            preview_result = _empty_catalog_preview_maintenance_result(
                activation_result.affected_scopes,
                reason="Preview preparation is not automatic.",
            )
            self._finish(
                job_id,
                state="completed",
                result=_catalog_update_result_dict(
                    config.db_path,
                    started=started,
                    stage_result=stage_result,
                    activation_result=activation_result,
                    preview_result=preview_result,
                    source_action="catalog-update",
                ),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_dynamic_cache_cleanup(self, config: Config, job_id: str) -> None:
        try:
            result = execute_dynamic_thumbnail_cache_cleanup(config).to_dict()
            self._finish(job_id, state="completed", result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_protected_cache_orphan_check(self, config: Config, job_id: str) -> None:
        try:
            started = time.monotonic()
            public_plan, candidate_plan = thumbnail_cache_protected_orphan_cleanup_plan_bundle(config)
            audit = public_plan.get("audit") if isinstance(public_plan, dict) else None
            result = {
                "protected_cache_maintenance": True,
                "operation": "check",
                "protected_orphan_audit": audit,
                "protected_orphan_cleanup_plan": public_plan,
                "duration_seconds": time.monotonic() - started,
                "writes": {
                    "source_media": False,
                    "dynamic_cache": False,
                    "protected_cache": False,
                    "catalog_db": False,
                    "settings_json": False,
                    "config_json": False,
                },
            }
            with self._lock:
                if self._job is not None and self._job.job_id == job_id:
                    self._protected_cache_cleanup_candidate_plan = candidate_plan
            self._finish(job_id, state="completed", result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_protected_cache_orphan_clean(
        self,
        config: Config,
        candidate_plan: dict[str, object],
        job_id: str,
    ) -> None:
        try:
            result = execute_protected_thumbnail_cache_orphan_cleanup(
                config,
                candidate_plan=candidate_plan,
            )
            payload = {
                "protected_cache_maintenance": True,
                "operation": "clean",
                "protected_orphan_cleanup_execute": result,
                "duration_seconds": float(result.get("duration_seconds") or 0.0),
                "writes": result.get("writes", {}),
            }
            self._finish(job_id, state="completed", result=payload, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_gif_preview(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            result = generate_gif_previews_for_scope(
                config,
                branch_rel_path=branch_rel_path,
            )
            self._finish(job_id, state="completed", result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_video_poster(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            result = generate_video_posters_for_scope(
                config,
                branch_rel_path=branch_rel_path,
            )
            self._finish(job_id, state="completed", result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_video_frames(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            result = generate_video_frames_for_scope(
                config,
                branch_rel_path=branch_rel_path,
            )
            self._finish(job_id, state="completed", result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _run_prepare_previews(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            started = time.monotonic()
            media_result = build_media_previews_for_scope(
                config,
                branch_rel_path=branch_rel_path,
            )
            folder_result = build_folder_preview_tree(
                config,
                branch_rel_path=branch_rel_path,
                variant=FOLDER_PREVIEW_SELECTION_VARIANT,
                requested_count=FOLDER_PREVIEW_REQUESTED_COUNT,
            )
            payload = _prepare_previews_result_dict(
                branch_rel_path=branch_rel_path,
                started=started,
                media_result=media_result,
                folder_result=folder_result,
            )
            self._finish(job_id, state="completed", result=payload, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))


    def _run_folder_preview_build_tree(self, config: Config, branch_rel_path: str, job_id: str) -> None:
        try:
            started = time.monotonic()
            result = build_folder_preview_tree(
                config,
                branch_rel_path=branch_rel_path,
                variant=FOLDER_PREVIEW_SELECTION_VARIANT,
                requested_count=FOLDER_PREVIEW_REQUESTED_COUNT,
            )
            payload = _folder_preview_tree_build_result_dict(result)
            payload["duration_seconds"] = time.monotonic() - started
            self._finish(job_id, state="completed", result=payload, error=None)
        except Exception as exc:  # noqa: BLE001 - background job must record all failures
            self._finish(job_id, state="failed", result=None, error=str(exc))

    def _finish(
        self,
        job_id: str,
        *,
        state: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        with self._lock:
            if self._job is None or self._job.job_id != job_id:
                return
            self._job.state = state
            self._job.finished_at = time.time()
            self._job.result = result
            self._job.error = error


def _summarize_scan_preview(
    actions: Iterable[ScanAction],
    *,
    branch_rel_path: str,
    db_path,
) -> dict[str, Any]:
    """Consume a scan plan and return a read-only summary without printing."""
    started = time.monotonic()
    counts = {
        "folders": 0,
        "images": 0,
        "gifs": 0,
        "videos": 0,
        "other": 0,
        "errors": 0,
        "skipped_links": 0,
        "skipped_ignored_directories": 0,
        "skipped_other": 0,
    }
    completed = False
    scanned_folder_keys: set[str] = set()
    scanned_media_keys: set[str] = set()
    diff = _empty_change_summary()
    samples = _empty_change_samples()

    with open_database(db_path, read_only=True) as connection:
        for action in actions:
            if action.kind == "folder":
                record = _expect_payload(action, FolderRecord)
                counts["folders"] += 1
                scanned_folder_keys.add(record.path_key)
                _count_preview_folder_change(connection, record, diff, samples, record.rel_path)
            elif action.kind == "media":
                record = _expect_payload(action, MediaRecord)
                count_key = {
                    "image": "images",
                    "gif": "gifs",
                    "video": "videos",
                    "other": "other",
                }[record.media_type]
                counts[count_key] += 1
                scanned_media_keys.add(record.path_key)
                _count_preview_media_change(connection, record, diff)
            elif action.kind == "error":
                _expect_payload(action, ScanErrorRecord)
                counts["errors"] += 1
            elif action.kind == "skip":
                record = _expect_payload(action, ScanSkipRecord)
                if record.reason == "link_or_junction":
                    counts["skipped_links"] += 1
                elif record.reason == "ignored_directory":
                    counts["skipped_ignored_directories"] += 1
                else:
                    counts["skipped_other"] += 1
            elif action.kind == "complete_scan":
                completed = True

        _count_preview_missing_items(
            connection,
            branch_rel_path=branch_rel_path,
            scanned_folder_keys=scanned_folder_keys,
            scanned_media_keys=scanned_media_keys,
            diff=diff,
            samples=samples,
        )

    duration = time.monotonic() - started
    _finalize_change_summary(diff)
    return {
        "scan_type": "branch" if branch_rel_path else "full",
        "branch": branch_rel_path,
        "folders": counts["folders"],
        "media": {
            "total": counts["images"] + counts["gifs"] + counts["videos"] + counts["other"],
            "images": counts["images"],
            "gifs": counts["gifs"],
            "videos": counts["videos"],
            "other": counts["other"],
        },
        "errors": counts["errors"],
        "skipped": {
            "links": counts["skipped_links"],
            "ignored_directories": counts["skipped_ignored_directories"],
            "other": counts["skipped_other"],
        },
        "changes": diff,
        "change_samples": samples,
        "duration_seconds": duration,
        "completed": completed,
        "read_only": True,
    }



def _should_return_catalog_update_payload(
    result: ScanActivationResult,
    *,
    expected_branch: str | None,
    missing_action: str,
) -> bool:
    """Return True only for the normal confirmed full-catalog update path.

    The payload is still rendered as a catalog-update result in the UI, but
    preview preparation is not started automatically.
    """
    normalized_branch = (expected_branch if expected_branch is not None else result.scope_rel_path) or ""
    return (
        result.scan_type == "full"
        and normalized_branch == ""
        and missing_action == "purge"
    )


def _run_catalog_update_preview_maintenance(
    config: Config,
    *,
    affected_scopes: dict[str, Any],
) -> dict[str, Any]:
    """Run post-activation preview maintenance only for affected scopes."""
    started = time.monotonic()
    targets = _catalog_preview_maintenance_targets(affected_scopes)

    if not targets["has_targets"]:
        return _empty_catalog_preview_maintenance_result(
            affected_scopes,
            reason="žádné dotčené rozsahy pro náhledové fáze",
            duration_seconds=time.monotonic() - started,
            targets=targets,
        )

    media_result = _run_media_preview_maintenance_for_branches(
        config,
        branch_rels=targets["media_preview_branch_rels"],
    )
    folder_result = maintain_folder_previews_for_scopes(
        config,
        auto_folder_rels=targets["folder_preview_auto_rels"],
        parent_folder_rels=targets["folder_preview_parent_rels"],
        variant=FOLDER_PREVIEW_SELECTION_VARIANT,
        requested_count=FOLDER_PREVIEW_REQUESTED_COUNT,
    )

    return {
        "catalog_update_orchestrator": True,
        "version": 1,
        "scope": "affected_scopes",
        "preview_maintenance_started": True,
        "targets": targets,
        "media_previews": media_result,
        "folder_previews": folder_result,
        "duration_seconds": time.monotonic() - started,
        "writes": {
            "catalog_db": "thumbnails and affected folder_preview_items",
            "cache_files": "protected previews only for affected media/folders",
            "source_media": False,
            "dynamic_photo_tiles_bulk": False,
            "protected_cache_deleted": False,
        },
    }


def _empty_catalog_preview_maintenance_result(
    affected_scopes: dict[str, Any] | None,
    *,
    reason: str,
    duration_seconds: float = 0.0,
    targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    empty_media = _empty_media_preview_maintenance_result()
    empty_folder = {
        "folder_preview_maintenance": True,
        "scope": "affected_scopes",
        "targets": {
            "auto_folder_count": 0,
            "parent_folder_count": 0,
            "auto_folder_samples": [],
            "parent_folder_samples": [],
            "auto_folder_rels": [],
            "parent_folder_rels": [],
        },
        "auto": {
            "folders_requested": 0,
            "folders_applied": 0,
            "folders_skipped": 0,
            "rows_deleted": 0,
            "rows_inserted": 0,
            "skipped_samples": [],
        },
        "parent": {
            "folders_requested": 0,
            "folders_applied": 0,
            "folders_skipped": 0,
            "rows_deleted": 0,
            "rows_inserted": 0,
            "skipped_samples": [],
        },
        "thumbnails": {"reused": 0, "created": 0, "errors": 0},
        "duration_seconds": 0.0,
        "writes": {
            "catalog_db": False,
            "thumbnail_cache": False,
            "source_media": False,
        },
    }
    return {
        "catalog_update_orchestrator": True,
        "version": 1,
        "scope": "affected_scopes",
        "preview_maintenance_started": False,
        "reason": reason,
        "targets": targets or _catalog_preview_maintenance_targets(affected_scopes or {}),
        "media_previews": empty_media,
        "folder_previews": empty_folder,
        "duration_seconds": duration_seconds,
        "writes": {
            "catalog_db": False,
            "cache_files": False,
            "source_media": False,
            "dynamic_photo_tiles_bulk": False,
            "protected_cache_deleted": False,
        },
    }


def _catalog_preview_maintenance_targets(affected_scopes: dict[str, Any] | None) -> dict[str, Any]:
    scopes = affected_scopes or {}
    media_branch_rels = _sorted_unique_rel_strings([
        *_affected_group_rels(scopes, "media_new_parent_folders"),
        *_affected_group_rels(scopes, "media_restored_parent_folders"),
        *_affected_group_rels(scopes, "media_changed_parent_folders"),
    ])
    direct_rels = _affected_derived_rels(scopes, "direct_folder_rels")
    removed_rels = set(_affected_derived_rels(scopes, "removed_folder_rels"))
    preview_candidate_rels = _affected_derived_rels(scopes, "preview_candidate_folder_rels")
    folder_auto_rels = _sorted_unique_rel_strings(
        rel for rel in direct_rels if rel not in removed_rels
    )
    folder_parent_rels = _sorted_unique_rel_strings(
        rel for rel in preview_candidate_rels if rel not in removed_rels
    )
    media_branch_rels = [rel for rel in media_branch_rels if rel]

    return {
        "has_targets": bool(media_branch_rels or folder_auto_rels or folder_parent_rels),
        "media_preview_branch_count": len(media_branch_rels),
        "folder_preview_auto_count": len(folder_auto_rels),
        "folder_preview_parent_count": len(folder_parent_rels),
        "media_preview_branch_rels": media_branch_rels,
        "folder_preview_auto_rels": folder_auto_rels,
        "folder_preview_parent_rels": folder_parent_rels,
        "media_preview_branch_samples": media_branch_rels[:20],
        "folder_preview_auto_samples": folder_auto_rels[:20],
        "folder_preview_parent_samples": folder_parent_rels[:20],
    }


def _run_media_preview_maintenance_for_branches(
    config: Config,
    *,
    branch_rels: list[str],
) -> dict[str, Any]:
    started = time.monotonic()
    branches: list[dict[str, Any]] = []
    branch_errors: list[dict[str, str]] = []

    for branch_rel in branch_rels:
        try:
            branches.append(build_media_previews_for_branch(config, branch_rel_path=branch_rel))
        except Exception as exc:  # noqa: BLE001 - a preview branch failure must be visible but must not undo DB activation
            branch_errors.append({"branch": branch_rel, "error": str(exc)})

    phase_results = [phase for branch in branches for phase in branch.get("phases", [])]
    totals = {
        "processed_media": sum(int(item.get("processed", 0)) for item in phase_results),
        "processed_frames": sum(int(item.get("frames_processed", 0)) for item in phase_results),
        "created": sum(int(item.get("created", 0)) for item in phase_results),
        "reused": sum(int(item.get("reused", 0)) for item in phase_results),
        "errors": sum(int(item.get("errors", 0)) for item in phase_results) + len(branch_errors),
        "duration_seconds": sum(float(item.get("duration_seconds", 0.0)) for item in phase_results),
    }

    return {
        "media_preview_workflow": True,
        "scope": "affected_scopes",
        "branch_count": len(branch_rels),
        "branches_completed": len(branches),
        "branch_errors": len(branch_errors),
        "branch_error_samples": branch_errors[:10],
        "branch_samples": branch_rels[:20],
        "branch_rels": branch_rels,
        "branches": branches,
        "totals": totals,
        "duration_seconds": time.monotonic() - started,
        "writes": {
            "catalog_db": "thumbnails for affected media branches",
            "cache_files": "protected GIF/video preview cache for affected media branches",
            "source_media": False,
            "folder_preview_items": False,
            "dynamic_photo_tiles": False,
            "protected_cache_deleted": False,
        },
    }


def _empty_media_preview_maintenance_result() -> dict[str, Any]:
    return {
        "media_preview_workflow": True,
        "scope": "affected_scopes",
        "branch_count": 0,
        "branches_completed": 0,
        "branch_errors": 0,
        "branch_error_samples": [],
        "branch_samples": [],
        "branch_rels": [],
        "branches": [],
        "totals": {
            "processed_media": 0,
            "processed_frames": 0,
            "created": 0,
            "reused": 0,
            "errors": 0,
            "duration_seconds": 0.0,
        },
        "duration_seconds": 0.0,
        "writes": {
            "catalog_db": False,
            "cache_files": False,
            "source_media": False,
            "folder_preview_items": False,
            "dynamic_photo_tiles": False,
            "protected_cache_deleted": False,
        },
    }


def _affected_group_rels(affected_scopes: dict[str, Any], group_key: str) -> list[str]:
    for group in affected_scopes.get("groups", []):
        group_dict = dict(group)
        if group_dict.get("key") == group_key:
            return _sorted_unique_rel_strings(group_dict.get("rels") or group_dict.get("samples") or [])
    return []


def _affected_derived_rels(affected_scopes: dict[str, Any], derived_key: str) -> list[str]:
    derived = affected_scopes.get("derived", {})
    if not isinstance(derived, dict):
        return []
    group = derived.get(derived_key, {})
    if not isinstance(group, dict):
        return []
    return _sorted_unique_rel_strings(group.get("rels") or group.get("samples") or [])


def _sorted_unique_rel_strings(values: Iterable[Any]) -> list[str]:
    return sorted({str(value or "").strip("/") for value in values}, key=lambda item: item.lower())


def _update_branch_result_dict(
    db_path: Any,
    *,
    branch_rel_path: str,
    started: float,
    stage_result: ScanStageResult,
    activation_result: ScanActivationResult,
) -> dict[str, Any]:
    """Return browser/API summary for the controlled DB-only update-branch workflow."""
    return {
        "update_branch": True,
        "branch": branch_rel_path,
        "scan_id": stage_result.scan_id,
        "update_plan": build_branch_update_plan_dict(
            branch_rel_path=branch_rel_path,
            affected_scopes=activation_result.affected_scopes,
        ),
        "affected_scopes": activation_result.affected_scopes,
        "phases": {
            "scan_stage": _scan_stage_result_dict(db_path, stage_result),
            "scan_activate": _scan_activation_result_dict(activation_result),
        },
        "summary": {
            "scan_id": stage_result.scan_id,
            "scanned_media": stage_result.media_total,
        },
        "duration_seconds": time.monotonic() - started,
        "writes": {
            "catalog_db": "staging scan tables and active catalog tables",
            "cache_files": False,
            "source_media": False,
            "dynamic_photo_tiles_bulk": False,
            "preview_preparation": False,
        },
    }


def _catalog_update_result_dict(
    db_path: Any,
    *,
    started: float,
    stage_result: ScanStageResult | None,
    activation_result: ScanActivationResult,
    preview_result: dict[str, Any] | None = None,
    source_action: str = "catalog-update",
) -> dict[str, Any]:
    activation_payload = _scan_activation_result_dict(activation_result)
    preview_payload = preview_result or _empty_catalog_preview_maintenance_result(
        activation_result.affected_scopes,
        reason="Preview preparation is not automatic.",
    )
    media_payload = dict(preview_payload.get("media_previews", {}))
    folder_payload = dict(preview_payload.get("folder_previews", {}))
    media_totals = dict(media_payload.get("totals", {}))
    folder_auto = dict(folder_payload.get("auto", {}))
    folder_parent = dict(folder_payload.get("parent", {}))
    folder_thumbnails = dict(folder_payload.get("thumbnails", {}))
    phases = {
        "scan_activate": activation_payload,
        "preview_maintenance": preview_payload,
        "media_previews": media_payload,
        "folder_previews": folder_payload,
    }
    if stage_result is not None:
        phases = {"scan_stage": _scan_stage_result_dict(db_path, stage_result), **phases}

    return {
        "catalog_update": True,
        "scan_id": activation_result.scan_id,
        "branch": "",
        "update_plan": build_catalog_update_plan_dict(
            source_action=source_action,
            affected_scopes=activation_result.affected_scopes,
        ),
        "affected_scopes": activation_result.affected_scopes,
        "preview_maintenance": preview_payload,
        "phases": phases,
        "summary": {
            "scan_id": activation_result.scan_id,
            "media_preview_branches": int(media_payload.get("branch_count", 0)),
            "media_preview_created": int(media_totals.get("created", 0)),
            "media_preview_reused": int(media_totals.get("reused", 0)),
            "media_preview_errors": int(media_totals.get("errors", 0)),
            "folder_preview_auto_inserted": int(folder_auto.get("rows_inserted", 0)),
            "folder_preview_auto_deleted": int(folder_auto.get("rows_deleted", 0)),
            "folder_preview_auto_parent_inserted": int(folder_parent.get("rows_inserted", 0)),
            "folder_preview_auto_parent_deleted": int(folder_parent.get("rows_deleted", 0)),
            "folder_preview_thumbnail_created": int(folder_thumbnails.get("created", 0)),
            "folder_preview_thumbnail_reused": int(folder_thumbnails.get("reused", 0)),
            "folder_preview_thumbnail_errors": int(folder_thumbnails.get("errors", 0)),
        },
        "changes": activation_payload.get("changes", {}),
        "folders": activation_result.folder_count,
        "media": {"total": activation_result.media_count},
        "duration_seconds": time.monotonic() - started,
        "writes": {
            "catalog_db": "active catalog tables",
            "favorites_json": "removed favorites for purged missing items",
            "cache_files": False,
            "source_media": False,
            "dynamic_photo_tiles_bulk": False,
            "preview_preparation": False,
            "protected_cache_deleted": False,
        },
    }


def _prepare_previews_result_dict(
    *,
    branch_rel_path: str,
    started: float,
    media_result: dict[str, Any],
    folder_result: FolderPreviewTreeBuildResult,
) -> dict[str, Any]:
    """Return browser/API summary for the combined prepare-previews workflow."""
    media_totals = dict(media_result.get("totals", {}))
    folder_payload = _folder_preview_tree_build_result_dict(folder_result)

    return {
        "prepare_previews": True,
        "branch": branch_rel_path,
        "scope": "branch" if branch_rel_path else "full",
        "phases": {
            "media_previews": media_result,
            "folder_previews": folder_payload,
        },
        "summary": {
            "media_preview_created": int(media_totals.get("created", 0)),
            "media_preview_reused": int(media_totals.get("reused", 0)),
            "media_preview_errors": int(media_totals.get("errors", 0)),
            "processed_media": int(media_totals.get("processed_media", 0)),
            "processed_frames": int(media_totals.get("processed_frames", 0)),
            "folder_preview_auto_inserted": int(folder_payload["auto"]["rows_inserted"]),
            "folder_preview_auto_deleted": int(folder_payload["auto"]["rows_deleted"]),
            "folder_preview_auto_parent_inserted": int(folder_payload["parent"]["rows_inserted"]),
            "folder_preview_auto_parent_deleted": int(folder_payload["parent"]["rows_deleted"]),
            "folder_preview_thumbnail_created": int(folder_payload["thumbnails"]["created"]),
            "folder_preview_thumbnail_reused": int(folder_payload["thumbnails"]["reused"]),
            "folder_preview_thumbnail_errors": int(folder_payload["thumbnails"]["errors"]),
        },
        "duration_seconds": time.monotonic() - started,
        "writes": {
            "catalog_db": "thumbnails and folder_preview_items",
            "cache_files": "protected GIF/video previews and missing selected folder-preview thumbnails",
            "source_media": False,
            "dynamic_photo_tiles_bulk": False,
            "scan_data": False,
        },
    }


def _folder_preview_tree_build_result_dict(result: FolderPreviewTreeBuildResult) -> dict[str, Any]:
    """Return a compact browser-friendly summary of folder-preview tree build."""
    plan = result.plan
    planned_auto_items = [item for item in plan.auto_items if item.report is not None]
    skipped_auto_items = [item for item in plan.auto_items if item.report is None]
    planned_parent_items = [item for item in plan.parent_items if item.report is not None]
    skipped_parent_items = [item for item in plan.parent_items if item.report is None]
    auto_inserted = sum(item.inserted_auto_rows for item in result.auto_applied_results)
    parent_inserted = sum(item.inserted_auto_parent_rows for item in result.parent_applied_results)
    thumb_created = sum(1 for item in result.thumbnail_results if item.action == "created")
    thumb_reused = sum(1 for item in result.thumbnail_results if item.action == "reused")
    thumb_errors = sum(1 for item in result.thumbnail_results if item.action == "error")

    return {
        "folder_preview_build_tree": True,
        "branch": plan.branch_rel_path,
        "name": plan.branch_name,
        "variant": plan.variant,
        "requested_count": plan.requested_count,
        "candidate_folder_count": plan.candidate_folder_count,
        "auto": {
            "folders_planned": len(planned_auto_items),
            "folders_applied": len(result.auto_applied_results),
            "folders_skipped": len(skipped_auto_items),
            "rows_deleted": result.deleted_auto_rows,
            "rows_inserted": auto_inserted,
        },
        "parent": {
            "folders_planned": len(planned_parent_items),
            "folders_applied": len(result.parent_applied_results),
            "folders_skipped": len(skipped_parent_items),
            "rows_deleted": result.deleted_auto_parent_rows,
            "rows_inserted": parent_inserted,
        },
        "thumbnails": {
            "reused": thumb_reused,
            "created": thumb_created,
            "errors": thumb_errors,
        },
        "writes": {
            "catalog_db": "folder_preview_items auto/auto_parent in selected branch",
            "thumbnail_cache": "missing selected auto candidates only",
            "source_media": False,
            "scan_data": False,
            "manual_preview_rows": False,
        },
    }


def _scan_stage_result_dict(db_path, result: ScanStageResult) -> dict[str, Any]:
    result_message = _scan_stage_result_message(result)
    return {
        "result_messages": [result_message],
        "scan_id": result.scan_id,
        "scan_type": result.scan_type,
        "branch": result.scope_rel_path,
        "status": result.status,
        "folders": result.folders,
        "media": {
            "total": result.media_total,
            "images": result.images,
            "gifs": result.gifs,
            "videos": result.videos,
            "other": result.other,
        },
        "errors": result.errors,
        "skipped": {
            "links": result.skipped_links,
            "ignored_directories": result.skipped_ignored_directories,
            "other": result.skipped_other,
        },
        "changes": _scan_stage_change_summary(
            db_path,
            scan_id=result.scan_id,
            scan_type=result.scan_type,
            branch_rel_path=result.scope_rel_path,
        ),
        "duration_seconds": result.duration_seconds,
        "completed": result.status == "completed",
        "read_only": False,
        "writes": {
            "catalog_db": "staging_scan_tables",
            "active_catalog": False,
            "source_media": False,
        },
    }


def _empty_change_summary() -> dict[str, Any]:
    return {
        "folders_new": 0,
        "folders_restored": 0,
        "folders_missing": 0,
        "folders_already_unavailable": 0,
        "media_new": 0,
        "media_restored": 0,
        "media_changed": 0,
        "media_unchanged": 0,
        "media_missing": 0,
        "media_already_unavailable": 0,
        "has_changes": False,
    }


def _empty_change_samples() -> dict[str, list[str]]:
    return {
        "folders_new": [],
        "folders_restored": [],
        "folders_missing": [],
    }


def _append_change_sample(samples: dict[str, list[str]], key: str, value: str, *, limit: int = 30) -> None:
    if not value:
        value = "Hlavní stránka"
    bucket = samples.setdefault(key, [])
    if len(bucket) < limit:
        bucket.append(value)


def _finalize_change_summary(summary: dict[str, Any]) -> dict[str, Any]:
    summary["has_changes"] = any(
        int(summary[key]) > 0
        for key in (
            "folders_new",
            "folders_restored",
            "folders_missing",
            "media_new",
            "media_restored",
            "media_changed",
            "media_missing",
        )
    )
    return summary


def _count_preview_folder_change(
    connection,
    record: FolderRecord,
    summary: dict[str, Any],
    samples: dict[str, list[str]],
    rel_path: str,
) -> None:
    row = connection.execute(
        """
        SELECT is_available
        FROM folders
        WHERE path_key = ?
        """,
        (record.path_key,),
    ).fetchone()
    if row is None:
        summary["folders_new"] += 1
        _append_change_sample(samples, "folders_new", rel_path)
    elif int(row["is_available"]) == 0:
        summary["folders_restored"] += 1
        _append_change_sample(samples, "folders_restored", rel_path)


def _count_preview_media_change(connection, record: MediaRecord, summary: dict[str, Any]) -> None:
    row = connection.execute(
        """
        SELECT
            active.file_name,
            active.extension,
            active.media_type,
            active.size_bytes,
            active.modified_time,
            active.sort_key,
            active.is_available,
            folder.path_key AS folder_path_key
        FROM media_files AS active
        JOIN folders AS folder
          ON folder.id = active.folder_id
        WHERE active.path_key = ?
        """,
        (record.path_key,),
    ).fetchone()
    if row is None:
        summary["media_new"] += 1
        return
    if int(row["is_available"]) == 0:
        summary["media_restored"] += 1
        return

    changed = (
        str(row["folder_path_key"]) != record.folder_path_key
        or str(row["file_name"]) != record.file_name
        or str(row["extension"]) != record.extension
        or str(row["media_type"]) != record.media_type
        or int(row["size_bytes"]) != int(record.size_bytes)
        or float(row["modified_time"]) != float(record.modified_time)
        or str(row["sort_key"]) != record.sort_key
    )
    if changed:
        summary["media_changed"] += 1
    else:
        summary["media_unchanged"] += 1


def _count_preview_missing_items(
    connection,
    *,
    branch_rel_path: str,
    scanned_folder_keys: set[str],
    scanned_media_keys: set[str],
    diff: dict[str, Any],
    samples: dict[str, list[str]],
) -> None:
    folder_rows = _active_rows_in_scope(connection, "folders", branch_rel_path)
    for row in folder_rows:
        if str(row["path_key"]) in scanned_folder_keys:
            continue
        if int(row["is_available"]) == 1:
            diff["folders_missing"] += 1
            _append_change_sample(samples, "folders_missing", str(row["rel_path"]))
        else:
            diff["folders_already_unavailable"] += 1

    media_rows = _active_rows_in_scope(connection, "media_files", branch_rel_path)
    for row in media_rows:
        if str(row["path_key"]) in scanned_media_keys:
            continue
        if int(row["is_available"]) == 1:
            diff["media_missing"] += 1
        else:
            diff["media_already_unavailable"] += 1


def _active_rows_in_scope(connection, table_name: str, branch_rel_path: str):
    if not branch_rel_path:
        return connection.execute(
            f"""
            SELECT path_key, rel_path, is_available
            FROM {table_name}
            """
        ).fetchall()

    return connection.execute(
        f"""
        SELECT path_key, rel_path, is_available
        FROM {table_name}
        WHERE rel_path = ? OR rel_path LIKE ?
        """,
        (branch_rel_path, f"{branch_rel_path}/%"),
    ).fetchall()


def _scan_stage_change_summary(
    db_path,
    *,
    scan_id: int,
    scan_type: str,
    branch_rel_path: str,
) -> dict[str, Any]:
    with open_database(db_path, read_only=True) as connection:
        summary = _empty_change_summary()
        summary["folders_new"] = _count_sql(
            connection,
            """
            SELECT COUNT(*)
            FROM scan_folders AS staged
            LEFT JOIN folders AS active
              ON active.path_key = staged.path_key
            WHERE staged.scan_id = ?
              AND active.id IS NULL
            """,
            (scan_id,),
        )
        summary["folders_restored"] = _count_sql(
            connection,
            """
            SELECT COUNT(*)
            FROM scan_folders AS staged
            JOIN folders AS active
              ON active.path_key = staged.path_key
            WHERE staged.scan_id = ?
              AND active.is_available = 0
            """,
            (scan_id,),
        )
        summary["media_new"] = _count_sql(
            connection,
            """
            SELECT COUNT(*)
            FROM scan_media_files AS staged
            LEFT JOIN media_files AS active
              ON active.path_key = staged.path_key
            WHERE staged.scan_id = ?
              AND active.id IS NULL
            """,
            (scan_id,),
        )
        summary["media_restored"] = _count_sql(
            connection,
            """
            SELECT COUNT(*)
            FROM scan_media_files AS staged
            JOIN media_files AS active
              ON active.path_key = staged.path_key
            WHERE staged.scan_id = ?
              AND active.is_available = 0
            """,
            (scan_id,),
        )
        summary["media_changed"] = _count_sql(
            connection,
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
        )
        summary["media_unchanged"] = _count_sql(
            connection,
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
        )

        scope_sql, scope_params = _scope_filter_sql("active", scan_type, branch_rel_path)
        summary["folders_missing"] = _count_sql(
            connection,
            f"""
            SELECT COUNT(*)
            FROM folders AS active
            LEFT JOIN scan_folders AS staged
              ON staged.scan_id = ?
             AND staged.path_key = active.path_key
            WHERE {scope_sql}
              AND staged.path_key IS NULL
              AND active.is_available = 1
            """,
            (scan_id, *scope_params),
        )
        summary["folders_already_unavailable"] = _count_sql(
            connection,
            f"""
            SELECT COUNT(*)
            FROM folders AS active
            LEFT JOIN scan_folders AS staged
              ON staged.scan_id = ?
             AND staged.path_key = active.path_key
            WHERE {scope_sql}
              AND staged.path_key IS NULL
              AND active.is_available = 0
            """,
            (scan_id, *scope_params),
        )
        summary["media_missing"] = _count_sql(
            connection,
            f"""
            SELECT COUNT(*)
            FROM media_files AS active
            LEFT JOIN scan_media_files AS staged
              ON staged.scan_id = ?
             AND staged.path_key = active.path_key
            WHERE {scope_sql}
              AND staged.path_key IS NULL
              AND active.is_available = 1
            """,
            (scan_id, *scope_params),
        )
        summary["media_already_unavailable"] = _count_sql(
            connection,
            f"""
            SELECT COUNT(*)
            FROM media_files AS active
            LEFT JOIN scan_media_files AS staged
              ON staged.scan_id = ?
             AND staged.path_key = active.path_key
            WHERE {scope_sql}
              AND staged.path_key IS NULL
              AND active.is_available = 0
            """,
            (scan_id, *scope_params),
        )
        return _finalize_change_summary(summary)


def _scope_filter_sql(alias: str, scan_type: str, branch_rel_path: str) -> tuple[str, tuple[str, ...]]:
    if scan_type == "full":
        return "1 = 1", ()
    return (
        f"({alias}.rel_path = ? OR {alias}.rel_path LIKE ?)",
        (branch_rel_path, f"{branch_rel_path}/%"),
    )


def _count_sql(connection, sql: str, params: tuple = ()) -> int:
    return int(connection.execute(sql, params).fetchone()[0])


def _scan_activation_result_dict(result: ScanActivationResult) -> dict[str, Any]:
    result_message = _scan_activation_result_message(result)
    return {
        "result_messages": [result_message],
        "activation": True,
        "scan_id": result.scan_id,
        "scan_type": result.scan_type,
        "branch": result.scope_rel_path,
        "mode": result.mode,
        "folders": result.folder_count,
        "media": {
            "total": result.media_count,
            "images": None,
            "gifs": None,
            "videos": None,
            "other": None,
        },
        "changes": {
            "folders_new": result.folders_new,
            "folders_restored": result.folders_restored,
            "folders_missing_new": result.folders_missing_new,
            "folders_unavailable_total": result.folders_unavailable_total,
            "folders_purged": result.folders_purged,
            "media_new": result.media_new,
            "media_restored": result.media_restored,
            "media_changed": result.media_changed,
            "media_unchanged": result.media_unchanged,
            "media_missing_new": result.media_missing_new,
            "media_unavailable_total": result.media_unavailable_total,
            "media_purged": result.media_purged,
            "favorites_purged": result.favorites_purged,
            "missing_action": result.missing_action,
        },
        "errors": 0,
        "skipped": {
            "links": 0,
            "ignored_directories": 0,
            "other": 0,
        },
        "duration_seconds": result.duration_seconds,
        "timing": _scan_activation_timing_payload(result),
        "affected_scopes": result.affected_scopes,
        "completed": True,
        "read_only": False,
        "writes": {
            "catalog_db": "active_catalog_tables",
            "active_catalog": True,
            "source_media": False,
        },
    }


def _scan_session_metadata(db_path: Any, scan_id: int) -> dict[str, str]:
    with open_database(db_path, read_only=True) as connection:
        row = connection.execute(
            """
            SELECT scan_type, scope_rel_path
            FROM scan_sessions
            WHERE id = ?
            """,
            (scan_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"Scan {scan_id} neexistuje.")

    return {
        "scan_type": str(row["scan_type"]),
        "scope_rel_path": str(row["scope_rel_path"]),
    }


def _normalize_missing_action(value: str | None) -> str:
    """Normalize UI/API missing-item decisions to scan activation actions."""
    normalized = (value or "require_decision").strip().lower()
    mapping = {
        "": "require_decision",
        "require_decision": "require_decision",
        "keep": "keep",
        "keep_unavailable": "keep",
        "ponechat": "keep",
        "purge": "purge",
        "delete_from_db": "purge",
        "odstranit": "purge",
        "cancel": "cancel",
        "zrusit": "cancel",
        "zrušit": "cancel",
    }
    if normalized not in mapping:
        raise ValueError(
            "Neplatná volba missing_decision. "
            "Použij require_decision, keep_unavailable, delete_from_db nebo cancel."
        )
    return mapping[normalized]


def _no_input(prompt: str) -> str:
    raise RuntimeError("Serverová aktivace nesmí čekat na vstup z konzole.")


def _discard_output(line: str) -> None:
    return None


def _expect_payload(action: ScanAction, expected_type: type):
    if not isinstance(action.payload, expected_type):
        raise TypeError(
            f"Akce {action.kind} nemá očekávaný payload {expected_type.__name__}."
        )
    return action.payload


def _scan_lock_is_held(lock_path) -> bool:
    if not lock_path.exists() or not lock_path.is_file():
        return False

    try:
        with lock_path.open("r+b") as lock_file:
            if __import__("os").name == "nt":
                import msvcrt

                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
                return False

            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            return False
    except OSError:
        return True


def _unix_time_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
