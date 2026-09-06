from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .config import ConfigError, load_config
from .paths import is_path_within, same_path
from .setup_instance import (
    DEFAULT_LAUNCHER_NAME,
    INSTANCE_VENV_DIR_NAME,
    RUNTIME_COPY_ITEMS,
    _venv_python_path,
    _write_windows_launcher,
)


class UpdateInstanceError(RuntimeError):
    """Raised when an installed catalog instance cannot be updated safely."""


ActionKind = Literal[
    "rename",
    "copy",
    "validate_config",
    "create_venv",
    "install_dependencies",
    "write_launcher",
]


@dataclass(frozen=True)
class UpdateInstanceAction:
    kind: ActionKind
    source: Path | None
    target: Path
    label: str
    command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class UpdateInstancePlan:
    source_project_root: Path
    catalog_output: Path
    app_root: Path
    venv_root: Path
    backup_root: Path
    config_path: Path
    launcher_path: Path
    launcher_backup_path: Path | None
    actions: tuple[UpdateInstanceAction, ...]
    copied_items: tuple[str, ...]


@dataclass(frozen=True)
class UpdateInstanceResult:
    source_project_root: Path
    catalog_output: Path
    app_root: Path
    venv_root: Path
    backup_root: Path
    config_path: Path
    launcher_path: Path
    launcher_backup_path: Path | None
    copied_items: tuple[str, ...]
    config_checked: bool


def build_update_instance_plan(
    *,
    source_project_root: Path,
    catalog_output: Path,
    timestamp: str | None = None,
) -> UpdateInstancePlan:
    """Build and validate the action plan used by update execution."""
    source_project_root = source_project_root.expanduser().resolve()
    catalog_output = catalog_output.expanduser().resolve()
    app_root = catalog_output / "app"
    venv_root = catalog_output / INSTANCE_VENV_DIR_NAME
    config_path = catalog_output / "config.json"

    _ensure_runtime_source(source_project_root)
    _ensure_catalog_output(catalog_output, app_root, config_path)
    _ensure_source_is_not_target(source_project_root, catalog_output, app_root)

    load_config(config_path)

    timestamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = catalog_output / f"app_backup_{timestamp}"
    if backup_root.exists():
        raise UpdateInstanceError(f"Runtime app backup path already exists: {backup_root}")

    launcher_path = catalog_output.parent / DEFAULT_LAUNCHER_NAME
    launcher_backup_path = launcher_path.with_name(f"{launcher_path.name}.backup_{timestamp}") if launcher_path.exists() else None
    if launcher_backup_path is not None and launcher_backup_path.exists():
        raise UpdateInstanceError(f"Launcher backup path already exists: {launcher_backup_path}")
    if venv_root.exists() and not venv_root.is_dir():
        raise UpdateInstanceError(f"Instance virtual environment path is not a folder: {venv_root}")

    actions = [
        UpdateInstanceAction(
            kind="rename",
            source=app_root,
            target=backup_root,
            label="Back up current runtime app",
        )
    ]

    for name in RUNTIME_COPY_ITEMS:
        actions.append(
            UpdateInstanceAction(
                kind="copy",
                source=source_project_root / name,
                target=app_root / name,
                label=f"Copy runtime item: {name}",
            )
        )

    actions.append(
        UpdateInstanceAction(
            kind="validate_config",
            source=None,
            target=config_path,
            label="Validate existing instance config with the updated runtime",
        )
    )
    if not venv_root.exists():
        actions.append(
            UpdateInstanceAction(
                kind="create_venv",
                source=None,
                target=venv_root,
                label="Create instance virtual environment",
                command=(sys.executable, "-m", "venv", str(venv_root)),
            )
        )
    actions.append(
        UpdateInstanceAction(
            kind="install_dependencies",
            source=app_root / "requirements.txt",
            target=venv_root,
            label="Install current runtime dependencies",
            command=(
                str(_venv_python_path(venv_root)),
                "-m",
                "pip",
                "install",
                "-r",
                str(app_root / "requirements.txt"),
            ),
        )
    )
    if launcher_backup_path is not None:
        actions.append(
            UpdateInstanceAction(
                kind="copy",
                source=launcher_path,
                target=launcher_backup_path,
                label="Back up current launcher",
            )
        )
    actions.append(
        UpdateInstanceAction(
            kind="write_launcher",
            source=None,
            target=launcher_path,
            label="Write updated launcher",
        )
    )

    return UpdateInstancePlan(
        source_project_root=source_project_root,
        catalog_output=catalog_output,
        app_root=app_root,
        venv_root=venv_root,
        backup_root=backup_root,
        config_path=config_path,
        launcher_path=launcher_path,
        launcher_backup_path=launcher_backup_path,
        actions=tuple(actions),
        copied_items=tuple(RUNTIME_COPY_ITEMS),
    )


def execute_update_instance_plan(plan: UpdateInstancePlan) -> UpdateInstanceResult:
    """Execute the exact plan created by build_update_instance_plan()."""
    renamed_backup = False

    try:
        for action in plan.actions:
            if action.kind == "rename":
                if action.source is None:
                    raise UpdateInstanceError("Invalid rename action without source path.")
                action.source.rename(action.target)
                renamed_backup = True
                plan.app_root.mkdir(parents=True, exist_ok=False)

            elif action.kind == "copy":
                if action.source is None:
                    raise UpdateInstanceError("Invalid copy action without source path.")
                if action.source.is_dir():
                    shutil.copytree(action.source, action.target, ignore=_runtime_ignore)
                else:
                    shutil.copy2(action.source, action.target)

            elif action.kind == "validate_config":
                load_config(action.target)

            elif action.kind in {"create_venv", "install_dependencies"}:
                if action.command is None:
                    raise UpdateInstanceError(f"Invalid process action without command: {action.kind}")
                subprocess.run(action.command, check=True)

            elif action.kind == "write_launcher":
                _write_windows_launcher(
                    launcher_path=action.target,
                    output_dir_name=plan.catalog_output.name,
                )

            else:
                raise UpdateInstanceError(f"Unknown update action kind: {action.kind}")

    except PermissionError as exc:
        _restore_previous_app_after_failure(plan, renamed_backup=renamed_backup)
        raise UpdateInstanceError(
            "Runtime update failed because a file is locked. "
            "Close the running catalog instance and run update-instance again. "
            f"Technical detail: {exc}"
        ) from exc

    except (OSError, ConfigError, UpdateInstanceError, subprocess.CalledProcessError) as exc:
        _restore_previous_app_after_failure(plan, renamed_backup=renamed_backup)
        raise UpdateInstanceError(f"Runtime update failed. Previous app was restored if possible. Original error: {exc}") from exc

    return UpdateInstanceResult(
        source_project_root=plan.source_project_root,
        catalog_output=plan.catalog_output,
        app_root=plan.app_root,
        venv_root=plan.venv_root,
        backup_root=plan.backup_root,
        config_path=plan.config_path,
        launcher_path=plan.launcher_path,
        launcher_backup_path=plan.launcher_backup_path,
        copied_items=plan.copied_items,
        config_checked=True,
    )



def update_instance_result_lines(result: UpdateInstanceResult) -> list[str]:
    lines = [
        "Catalog 2.0 – update existing instance",
        "=" * 70,
        f"source project:  {result.source_project_root}",
        f"catalog output:  {result.catalog_output}",
        f"runtime app:     {result.app_root}",
        f"virtual env:     {result.venv_root}",
        f"backup:          {result.backup_root}",
        f"config:          {result.config_path} (checked)",
        f"launcher:        {result.launcher_path}",
        "",
        "Updated runtime items:",
    ]
    lines.extend(f"- {item}" for item in result.copied_items)
    if result.launcher_backup_path is not None:
        lines.append(f"Launcher backup: {result.launcher_backup_path}")
    lines.extend(
        [
            "",
            "User data was not modified.",
            "Start the catalog with Start Catalog.bat.",
        ]
    )
    return lines


def _ensure_runtime_source(source_project_root: Path) -> None:
    if not source_project_root.exists() or not source_project_root.is_dir():
        raise UpdateInstanceError(f"Source project folder does not exist: {source_project_root}")
    missing = [name for name in RUNTIME_COPY_ITEMS if not (source_project_root / name).exists()]
    if missing:
        raise UpdateInstanceError(
            "The source project root does not contain required runtime files: " + ", ".join(missing)
        )


def _ensure_catalog_output(catalog_output: Path, app_root: Path, config_path: Path) -> None:
    if not catalog_output.exists():
        raise UpdateInstanceError(f"Catalog output folder does not exist: {catalog_output}")
    if not catalog_output.is_dir():
        raise UpdateInstanceError(f"Catalog output path is not a folder: {catalog_output}")
    if not config_path.exists() or not config_path.is_file():
        raise UpdateInstanceError(f"Catalog output does not contain config.json: {config_path}")
    if not app_root.exists() or not app_root.is_dir():
        raise UpdateInstanceError(f"Catalog output does not contain runtime app folder: {app_root}")

    missing_in_app = [name for name in RUNTIME_COPY_ITEMS if not (app_root / name).exists()]
    if missing_in_app:
        raise UpdateInstanceError(
            "The existing runtime app folder is incomplete. Missing: " + ", ".join(missing_in_app)
        )


def _ensure_source_is_not_target(source_project_root: Path, catalog_output: Path, app_root: Path) -> None:
    if same_path(source_project_root, app_root):
        raise UpdateInstanceError("Source project root is the same as the target runtime app folder.")
    if is_path_within(source_project_root, app_root):
        raise UpdateInstanceError("Source project root is inside the target runtime app folder.")
    if is_path_within(app_root, source_project_root):
        raise UpdateInstanceError(
            "Target runtime app is inside the source project root. "
            "Run update-instance from the newly downloaded project, not from the installed instance."
        )
    if same_path(source_project_root, catalog_output):
        raise UpdateInstanceError("Source project root is the same as Catalog_Output.")


def _runtime_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in {"__pycache__", ".pytest_cache", ".mypy_cache"}:
            ignored.add(name)
        elif name.endswith((".pyc", ".pyo")):
            ignored.add(name)
    return ignored


def _restore_previous_app_after_failure(plan: UpdateInstancePlan, *, renamed_backup: bool) -> None:
    if not renamed_backup:
        return

    try:
        if plan.app_root.exists():
            shutil.rmtree(plan.app_root)
        if plan.backup_root.exists() and not plan.app_root.exists():
            plan.backup_root.rename(plan.app_root)
    except OSError:
        # The original error is more important for the user-facing message.
        # A failed restore leaves the backup path in the printed error context.
        return
