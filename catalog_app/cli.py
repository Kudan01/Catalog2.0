from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Sequence

from .config import ConfigError, config_summary_lines, load_config
from .diagnostics import (
    diagnostics_requested,
    finish_diagnostics_session,
    start_diagnostics_session,
)
from .database import (
    DatabaseError,
    check_database,
    database_status_lines,
    database_upgrade_lines,
    initialize_database,
    upgrade_database,
    validate_database_runtime,
)
from .folder_preview_candidates import (
    FolderPreviewCandidateError,
    apply_branch_folder_preview_plan,
    apply_folder_preview_candidates,
    apply_folder_preview_parent_candidates,
    apply_parent_branch_folder_preview_plan,
    build_branch_folder_previews_with_thumbnails,
    build_branch_folder_preview_plan,
    build_folder_preview_tree,
    build_folder_preview_tree_plan,
    build_parent_branch_folder_preview_plan,
    clear_auto_folder_previews,
    folder_preview_apply_result_lines,
    folder_preview_branch_apply_result_lines,
    folder_preview_branch_plan_lines,
    folder_preview_build_branch_result_lines,
    folder_preview_candidate_report,
    folder_preview_candidate_report_lines,
    folder_preview_parent_apply_result_lines,
    folder_preview_parent_branch_apply_result_lines,
    folder_preview_parent_branch_plan_lines,
    folder_preview_parent_candidate_report,
    folder_preview_tree_build_result_lines,
    folder_preview_tree_plan_lines,
    folder_preview_parent_candidate_report_lines,
    folder_preview_clear_auto_result_lines,
)
from .scan_activate import (
    ScanActivationError,
    activate_scan_by_id,
    scan_activation_result_lines,
)
from .scanner import ScannerError, ensure_source_root_available, validate_scan_scope
from .paths import normalize_catalog_relative_path
from .media_preview_workflow import (
    MediaPreviewWorkflowError,
    build_media_previews_for_branch,
    media_preview_workflow_result_lines,
)
from .scan_lock import ScanLockError
from .scan_plan import build_scan_plan, print_scan_preview
from .server import ServerError, run_server
from .launcher import LauncherError, launch_catalog
from .setup_instance import (
    DEFAULT_LAUNCHER_NAME,
    DEFAULT_OUTPUT_DIR_NAME,
    SetupInstanceError,
    setup_installable_instance,
    setup_instance_result_lines,
    suggest_catalog_root,
)
from .update_instance import (
    UpdateInstanceError,
    build_update_instance_plan,
    execute_update_instance_plan,
    update_instance_result_lines,
)
from .scan_store import (
    ScanStageError,
    ScanStageInterrupted,
    discard_staged_scan_by_id,
    discard_staged_scan_lines,
    read_scan_status,
    scan_stage_result_lines,
    scan_status_lines,
    stage_scan_plan,
)
from .video_tools import video_tools_status_lines


def project_root() -> Path:
    """Return the directory containing the catalog2.py entry point."""
    return Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Catalog 2.0 development CLI.")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "config-check",
            "db-init",
            "db-check",
            "db-upgrade",
            "scan-preview",
            "scan-stage",
            "scan-activate",
            "scan-discard",
            "scan-update-branch",
            "media-preview-build-branch",
            "update-branch",
            "scan-status",
            "video-tools-check",
            "folder-preview-candidates",
            "folder-preview-apply",
            "folder-preview-apply-branch",
            "folder-preview-build-branch",
            "folder-preview-parent-candidates",
            "folder-preview-parent-apply",
            "folder-preview-parent-apply-branch",
            "folder-preview-build-tree",
            "folder-preview-clear-auto",
            "serve",
            "launch",
            "setup-instance",
            "update-instance",
        ),
        default="config-check",
        help="Command; default is config-check.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to an installed instance config.json. Runtime commands default to config.json next to catalog2.py only for development; public packages normally use setup-instance and Start Catalog.bat.",
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="Source data folder for setup-instance. If omitted, setup-instance asks interactively.",
    )
    parser.add_argument(
        "--catalog-root",
        default=None,
        help=(
            "New catalog instance folder for setup-instance. The folder itself must not exist. "
            "If omitted, setup-instance offers a recommended Catalog2_<source> location."
        ),
    )
    parser.add_argument(
        "--catalog-output",
        default=None,
        help="Existing Catalog_Output folder for update-instance. If omitted, update-instance asks interactively.",
    )
    parser.add_argument(
        "--output-name",
        default=DEFAULT_OUTPUT_DIR_NAME,
        help=f"Output folder name created inside the generated Catalog2_<source> instance folder; default is {DEFAULT_OUTPUT_DIR_NAME}.",
    )
    parser.add_argument(
        "--launcher-name",
        default=DEFAULT_LAUNCHER_NAME,
        help=f"Windows launcher filename created inside the generated Catalog2_<source> instance folder; default is {DEFAULT_LAUNCHER_NAME}.",
    )
    parser.add_argument(
        "--python-executable",
        default=None,
        help="Deprecated compatibility option; installed instances always use Catalog_Output\\.venv.",
    )
    parser.add_argument(
        "--branch",
        default="",
        help=(
            "Relative folder path inside data_root for scanning a single branch. "
            "Usable with scan-preview, scan-stage, scan-update-branch, "
            "media-preview-build-branch, update-branch and as a safety guard for scan-activate."
        ),
    )
    parser.add_argument(
        "--scan-id",
        type=int,
        default=None,
        help="Completed scan-stage ID for scan-activate or scan-discard.",
    )
    parser.add_argument(
        "--folder",
        default="",
        help=(
            "Relative catalog folder path for folder preview "
            "candidates. Usable with folder-preview-candidates, folder-preview-apply, "
            "folder-preview-apply-branch, folder-preview-build-branch, "
            "folder-preview-parent-candidates, folder-preview-parent-apply, "
            "folder-preview-parent-apply-branch, folder-preview-build-tree "
            "and folder-preview-clear-auto."
        ),
    )
    parser.add_argument(
        "--variant",
        type=int,
        default=0,
        help="Stable selection variant for folder preview commands; default is 0.",
    )
    parser.add_argument(
        "--preview-count",
        type=int,
        default=6,
        help="Candidate count for folder preview commands, range 1 to 12; default is 6.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Usable only with folder-preview-clear-auto; deletes all auto folder preview records from the DB.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Usable with bulk folder preview planning commands; prints the plan without writing.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Usable with folder-preview-build-branch and folder-preview-parent-apply-branch; processes all descendant folders in the branch instead of only direct subfolders. folder-preview-build-tree is always tree/recursive.",
    )
    return parser


def _prompt_text(prompt: str) -> str:
    """Read one CLI value and treat closed stdin as an empty answer."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def main(argv: Sequence[str] | None = None) -> int:
    cli_started = time.perf_counter()
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser() if args.config else project_root() / "config.json"

    try:
        if args.command == "setup-instance":
            if args.catalog_output is not None:
                raise ConfigError("The --catalog-output parameter can be used only with update-instance.")
            if args.dry_run:
                raise ConfigError("The --dry-run parameter can be used only with bulk folder preview commands.")
            raw_data_root = args.data_root
            if raw_data_root is None:
                raw_data_root = _prompt_text("Source data folder: ")
            if not str(raw_data_root).strip():
                raise SetupInstanceError("Source data folder must not be empty.")

            data_root = Path(raw_data_root)
            raw_catalog_root = args.catalog_root
            if raw_catalog_root is None:
                suggested_root = suggest_catalog_root(data_root)
                if suggested_root is None:
                    raw_catalog_root = _prompt_text(
                        "Enter a full catalog folder path outside the selected filesystem root: "
                    )
                    if not raw_catalog_root:
                        raise SetupInstanceError(
                            "Catalog folder must be specified when the source is a filesystem root."
                        )
                else:
                    print(f"Catalog folder [{suggested_root}]")
                    raw_catalog_root = _prompt_text(
                        "Press Enter to use the suggested path, or enter a different full path: "
                    )
                    if not raw_catalog_root:
                        raw_catalog_root = str(suggested_root)

            if not str(raw_catalog_root).strip():
                raise SetupInstanceError("Catalog folder must not be empty.")

            result = setup_installable_instance(
                source_project_root=project_root(),
                data_root=data_root,
                catalog_root=Path(raw_catalog_root),
                output_dir_name=args.output_name,
                launcher_name=args.launcher_name,
                python_executable=args.python_executable,
            )
            for line in setup_instance_result_lines(result):
                print(line)
            return 0

        if args.command == "update-instance":
            if args.data_root is not None:
                raise ConfigError("The --data-root parameter can be used only with setup-instance.")
            if args.catalog_root is not None:
                raise ConfigError("The --catalog-root parameter can be used only with setup-instance.")
            if args.output_name != DEFAULT_OUTPUT_DIR_NAME:
                raise ConfigError("The --output-name parameter can be used only with setup-instance.")
            if args.launcher_name != DEFAULT_LAUNCHER_NAME:
                raise ConfigError("The --launcher-name parameter can be used only with setup-instance.")
            if args.python_executable is not None:
                raise ConfigError("The --python-executable parameter can be used only with setup-instance.")
            raw_catalog_output = args.catalog_output
            if raw_catalog_output is None:
                raw_catalog_output = input("Catalog_Output folder: ").strip()
            if not str(raw_catalog_output).strip():
                raise UpdateInstanceError("Catalog_Output folder must not be empty.")

            if args.dry_run:
                raise ConfigError(
                    "The --dry-run parameter is not supported by update-instance. "
                    "Run update-instance directly; it validates the complete action plan before writing."
                )

            plan = build_update_instance_plan(
                source_project_root=project_root(),
                catalog_output=Path(raw_catalog_output),
            )
            result = execute_update_instance_plan(plan)
            for line in update_instance_result_lines(result):
                print(line)
            return 0

        if args.data_root is not None and args.command != "setup-instance":
            raise ConfigError("The --data-root parameter can be used only with setup-instance.")
        if args.catalog_root is not None and args.command != "setup-instance":
            raise ConfigError("The --catalog-root parameter can be used only with setup-instance.")
        if args.catalog_output is not None and args.command != "update-instance":
            raise ConfigError("The --catalog-output parameter can be used only with update-instance.")
        if args.output_name != DEFAULT_OUTPUT_DIR_NAME and args.command != "setup-instance":
            raise ConfigError("The --output-name parameter can be used only with setup-instance.")
        if args.launcher_name != DEFAULT_LAUNCHER_NAME and args.command != "setup-instance":
            raise ConfigError("The --launcher-name parameter can be used only with setup-instance.")
        if args.python_executable is not None and args.command != "setup-instance":
            raise ConfigError("The --python-executable parameter can be used only with setup-instance.")

        if args.config is None and not config_path.exists():
            raise ConfigError(
                f"Missing instance config: {config_path}. "
                "Run setup-instance first, start the generated launcher, "
                "or pass --config pointing to Catalog_Output/config.json."
            )

        config_load_started = time.perf_counter()
        config = load_config(config_path)
        config_load_ms = (time.perf_counter() - config_load_started) * 1000.0
        if args.command in {"serve", "launch"} and diagnostics_requested():
            script_start_ns_text = os.environ.get("CATALOG2_DIAGNOSTIC_SCRIPT_START_NS", "")
            try:
                script_start_ns = int(script_start_ns_text)
            except ValueError:
                script_start_ns = time.perf_counter_ns()
            start_diagnostics_session(
                config_path=config.config_path,
                output_root=config.output_root,
                command=args.command,
                cli_elapsed_ms=(time.perf_counter() - cli_started) * 1000.0,
                config_load_ms=config_load_ms,
                process_elapsed_ms=(time.perf_counter_ns() - script_start_ns) / 1_000_000.0,
            )
        if args.command in {"scan-preview", "scan-stage", "scan-update-branch", "media-preview-build-branch", "update-branch"}:
            branch_rel_path = validate_scan_scope(config, args.branch)
        elif args.command == "scan-activate":
            branch_rel_path = normalize_catalog_relative_path(args.branch, allow_root=True)
        else:
            branch_rel_path = normalize_catalog_relative_path(args.branch, allow_root=True)

        if branch_rel_path and args.command not in {"scan-preview", "scan-stage", "scan-activate", "scan-update-branch", "media-preview-build-branch", "update-branch"}:
            raise ConfigError("The --branch parameter can be used only with scan-preview, scan-stage, scan-activate, scan-update-branch, media-preview-build-branch or update-branch.")

        if args.scan_id is not None and args.command not in {"scan-activate", "scan-discard"}:
            raise ConfigError("The --scan-id parameter can be used only with scan-activate or scan-discard.")

        folder_preview_plan_commands = {"folder-preview-candidates", "folder-preview-apply", "folder-preview-apply-branch", "folder-preview-build-branch", "folder-preview-parent-candidates", "folder-preview-parent-apply", "folder-preview-parent-apply-branch", "folder-preview-build-tree"}
        folder_preview_folder_commands = folder_preview_plan_commands | {"folder-preview-clear-auto"}

        if args.folder and args.command not in folder_preview_folder_commands:
            raise ConfigError("The --folder parameter can be used only with folder preview commands.")

        if args.command not in folder_preview_plan_commands and args.variant != 0:
            raise ConfigError("The --variant parameter can be used only with folder preview planning commands.")

        if args.command not in folder_preview_plan_commands and args.preview_count != 6:
            raise ConfigError("The --preview-count parameter can be used only with folder preview planning commands.")

        if args.all and args.command != "folder-preview-clear-auto":
            raise ConfigError("The --all parameter can be used only with folder-preview-clear-auto.")

        if args.dry_run and args.command not in {"folder-preview-apply-branch", "folder-preview-build-branch", "folder-preview-parent-apply-branch", "folder-preview-build-tree"}:
            raise ConfigError("The --dry-run parameter can be used only with bulk folder preview commands.")

        if args.recursive and args.command not in {"folder-preview-build-branch", "folder-preview-parent-apply-branch", "folder-preview-build-tree"}:
            raise ConfigError("The --recursive parameter can be used only with folder-preview-build-branch, folder-preview-parent-apply-branch or folder-preview-build-tree.")

        if args.command == "config-check":
            for line in config_summary_lines(config):
                print(line)
            return 0

        if args.command == "db-init":
            if config.db_path.exists():
                result = upgrade_database(config.db_path)
                if result.upgraded:
                    for line in database_upgrade_lines(result):
                        print(line)
                else:
                    print(f"Database already exists and was left unchanged: {config.db_path}")
            else:
                initialize_database(config.db_path)
                print(f"Database was created: {config.db_path}")

            status = check_database(config.db_path)
            for line in database_status_lines(status):
                print(line)
            return 0

        if args.command == "db-check":
            status = check_database(config.db_path)
            for line in database_status_lines(status):
                print(line)
            return 0

        if args.command == "db-upgrade":
            result = upgrade_database(config.db_path)
            for line in database_upgrade_lines(result):
                print(line)
            status = check_database(config.db_path)
            for line in database_status_lines(status):
                print(line)
            return 0

        if args.command == "scan-preview":
            # Verify database identity read-only. The scan itself never writes.
            validate_database_runtime(config.db_path)
            print_scan_preview(
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                sample_limit=30,
            )
            return 0

        if args.command == "scan-stage":
            result = stage_scan_plan(
                config.db_path,
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                scope_rel_path=branch_rel_path,
            )
            for line in scan_stage_result_lines(result):
                print(line)
            return 0

        if args.command == "scan-activate":
            if args.scan_id is None:
                raise ConfigError(
                    "scan-activate requires an explicit --scan-id. "
                    "Use scan-status or the scan-stage output to find an available scan_id."
                )
            ensure_source_root_available(config)
            result = activate_scan_by_id(
                config.db_path,
                scan_id=args.scan_id,
                expected_scope_rel_path=branch_rel_path if args.branch != "" else None,
                favorites_json_path=config.favorites_json,
            )
            for line in scan_activation_result_lines(result):
                print(line)
            return 0

        if args.command == "scan-discard":
            if args.scan_id is None:
                raise ConfigError(
                    "scan-discard requires an explicit --scan-id. "
                    "Use scan-status or the scan-stage output to find an available scan_id."
                )
            result = discard_staged_scan_by_id(
                config.db_path,
                scan_id=args.scan_id,
            )
            for line in discard_staged_scan_lines(result):
                print(line)
            return 0

        if args.command == "scan-update-branch":
            if not branch_rel_path:
                raise ConfigError("scan-update-branch requires --branch with a concrete folder.")

            print("Catalog 2.0 – controlled branch update")
            print("=" * 70)
            print(f"branch: {branch_rel_path}")
            print("phases: 1/2 scan-stage, 2/2 explicit scan-activate")
            print("Source data is not changed.")
            print("")

            print("Phase 1/2 – scan-stage")
            stage_result = stage_scan_plan(
                config.db_path,
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                scope_rel_path=branch_rel_path,
            )
            for line in scan_stage_result_lines(stage_result):
                print(line)

            print("")
            print("Phase 2/2 – scan-activate the exact created scan_id")
            try:
                activation_result = activate_scan_by_id(
                    config.db_path,
                    scan_id=stage_result.scan_id,
                    expected_scope_rel_path=branch_rel_path,
                    favorites_json_path=config.favorites_json,
                )
            except ScanActivationError as exc:
                raise ScanActivationError(
                    f"Phase 2 activation failed after creating scan_id {stage_result.scan_id}. "
                    "The active catalog was left unchanged; the staged scan session remains available for inspection. "
                    f"Original error: {exc}"
                ) from exc

            for line in scan_activation_result_lines(activation_result):
                print(line)

            print("")
            print("Controlled branch update completed.")
            print(f"used scan_id: {stage_result.scan_id}")
            print("performed: scan-stage + scan-activate of the same scan_id with branch guard")
            print("not performed: media previews, folder previews, cache cleanup")
            return 0


        if args.command == "media-preview-build-branch":
            if not branch_rel_path:
                raise ConfigError("media-preview-build-branch requires --branch with a concrete folder.")

            result = build_media_previews_for_branch(
                config,
                branch_rel_path=branch_rel_path,
            )
            for line in media_preview_workflow_result_lines(result):
                print(line)
            return 0


        if args.command == "update-branch":
            if not branch_rel_path:
                raise ConfigError("update-branch requires --branch with a concrete folder.")

            print("Catalog 2.0 – update folder / branch")
            print("=" * 70)
            print(f"branch: {branch_rel_path}")
            print("phases: 1/2 scan-stage, 2/2 scan-activate")
            print("Source data is not changed.")
            print("Preview recalculation is separate; use Recalculate previews in the UI or the preview service commands.")
            print("")

            print("Phase 1/2 – scan-stage")
            stage_result = stage_scan_plan(
                config.db_path,
                build_scan_plan(config, branch_rel_path=branch_rel_path),
                scope_rel_path=branch_rel_path,
            )
            for line in scan_stage_result_lines(stage_result):
                print(line)

            print("")
            print("Phase 2/2 – scan-activate the exact created scan_id")
            try:
                activation_result = activate_scan_by_id(
                    config.db_path,
                    scan_id=stage_result.scan_id,
                    expected_scope_rel_path=branch_rel_path,
                    favorites_json_path=config.favorites_json,
                )
            except ScanActivationError as exc:
                raise ScanActivationError(
                    f"Phase 2 activation failed after creating scan_id {stage_result.scan_id}. "
                    "Further update-branch phases were not started. "
                    "The active catalog was left unchanged; the staged scan session remains available for inspection. "
                    f"Original error: {exc}"
                ) from exc

            for line in scan_activation_result_lines(activation_result):
                print(line)

            print("")
            print("Folder update completed.")
            print(f"branch: {branch_rel_path}")
            print(f"used scan_id: {stage_result.scan_id}")
            print(f"media in scan: {stage_result.media_total}")
            print("previews: not prepared")
            print("original media: unchanged")
            return 0

        if args.command == "scan-status":
            status = read_scan_status(config.db_path)
            for line in scan_status_lines(status):
                print(line)
            return 0

        if args.command == "video-tools-check":
            for line in video_tools_status_lines(config):
                print(line)
            return 0


        if args.command == "folder-preview-candidates":
            validate_database_runtime(config.db_path)
            report = folder_preview_candidate_report(
                config,
                folder_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
            )
            for line in folder_preview_candidate_report_lines(report):
                print(line)
            return 0

        if args.command == "folder-preview-apply":
            validate_database_runtime(config.db_path)
            result = apply_folder_preview_candidates(
                config,
                folder_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
            )
            for line in folder_preview_apply_result_lines(result):
                print(line)
            return 0

        if args.command == "folder-preview-apply-branch":
            validate_database_runtime(config.db_path)

            if args.dry_run:
                plan = build_branch_folder_preview_plan(
                    config,
                    branch_rel_path=args.folder,
                    variant=args.variant,
                    requested_count=args.preview_count,
                )
                for line in folder_preview_branch_plan_lines(plan):
                    print(line)
                return 0

            result = apply_branch_folder_preview_plan(
                config,
                branch_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
            )
            for line in folder_preview_branch_apply_result_lines(result):
                print(line)
            return 0

        if args.command == "folder-preview-build-branch":
            validate_database_runtime(config.db_path)

            if args.dry_run:
                plan = build_branch_folder_preview_plan(
                    config,
                    branch_rel_path=args.folder,
                    variant=args.variant,
                    requested_count=args.preview_count,
                    recursive=args.recursive,
                )
                for line in folder_preview_branch_plan_lines(plan):
                    print(line)
                print()
                print("Note: the real build also creates missing thumbnail cache only for selected candidates and stores auto records.")
                return 0

            result = build_branch_folder_previews_with_thumbnails(
                config,
                branch_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
                recursive=args.recursive,
                progress=lambda line: print(line, flush=True),
            )
            for line in folder_preview_build_branch_result_lines(result, include_folder_details=False):
                print(line)
            return 0

        if args.command == "folder-preview-parent-candidates":
            validate_database_runtime(config.db_path)
            report = folder_preview_parent_candidate_report(
                config,
                folder_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
            )
            for line in folder_preview_parent_candidate_report_lines(report):
                print(line)
            return 0

        if args.command == "folder-preview-parent-apply":
            validate_database_runtime(config.db_path)
            result = apply_folder_preview_parent_candidates(
                config,
                folder_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
            )
            for line in folder_preview_parent_apply_result_lines(result):
                print(line)
            return 0

        if args.command == "folder-preview-parent-apply-branch":
            validate_database_runtime(config.db_path)

            if args.dry_run:
                plan = build_parent_branch_folder_preview_plan(
                    config,
                    branch_rel_path=args.folder,
                    variant=args.variant,
                    requested_count=args.preview_count,
                    recursive=args.recursive,
                )
                for line in folder_preview_parent_branch_plan_lines(plan):
                    print(line)
                return 0

            result = apply_parent_branch_folder_preview_plan(
                config,
                branch_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
                recursive=args.recursive,
                progress=lambda line: print(line, flush=True),
            )
            for line in folder_preview_parent_branch_apply_result_lines(result, include_folder_details=False):
                print(line)
            return 0

        if args.command == "folder-preview-build-tree":
            validate_database_runtime(config.db_path)

            if args.dry_run:
                plan = build_folder_preview_tree_plan(
                    config,
                    branch_rel_path=args.folder,
                    variant=args.variant,
                    requested_count=args.preview_count,
                )
                for line in folder_preview_tree_plan_lines(plan):
                    print(line)
                return 0

            result = build_folder_preview_tree(
                config,
                branch_rel_path=args.folder,
                variant=args.variant,
                requested_count=args.preview_count,
                progress=lambda line: print(line, flush=True),
            )
            for line in folder_preview_tree_build_result_lines(result, include_folder_details=False):
                print(line)
            return 0

        if args.command == "folder-preview-clear-auto":
            validate_database_runtime(config.db_path)
            result = clear_auto_folder_previews(
                config,
                folder_rel_path=args.folder,
                all_folders=args.all,
            )
            for line in folder_preview_clear_auto_result_lines(result):
                print(line)
            return 0

        if args.command == "serve":
            run_server(config)
            return 0

        if args.command == "launch":
            launch_catalog(config)
            return 0

        raise RuntimeError(f"Unknown command: {args.command}")

    except ScanStageInterrupted as exc:
        finish_diagnostics_session("cli_interrupted")
        print(f"INTERRUPTED: {exc}", file=sys.stderr)
        return 130

    except (
        ConfigError,
        DatabaseError,
        ScannerError,
        ScanActivationError,
        ScanLockError,
        ScanStageError,
        ServerError,
        LauncherError,
        FolderPreviewCandidateError,
        MediaPreviewWorkflowError,
        SetupInstanceError,
        UpdateInstanceError,
) as exc:
        finish_diagnostics_session("cli_error")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
