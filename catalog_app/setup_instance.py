from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_TRANSITION_UI_LOCALE,
    DEFAULT_UI_THEME,
    DEFAULT_CATALOG_TITLE,
    SUPPORTED_CONFIG_VERSION,
    SUPPORTED_RUNTIME_SETTINGS_VERSION,
    load_config,
)
from .database import initialize_database
from .paths import is_path_within, same_path


DEFAULT_INSTANCE_DIR_PREFIX = "Catalog2_"
DEFAULT_OUTPUT_DIR_NAME = "Catalog_Output"
DEFAULT_LAUNCHER_NAME = "Start Catalog.bat"
RUNTIME_COPY_ITEMS = (
    "catalog2.py",
    "catalog_app",
    "tools",
    "requirements.txt",
)

DEFAULT_INSTANCE_CONFIG: dict[str, Any] = {
    "config_version": SUPPORTED_CONFIG_VERSION,
    "data_root": "",
    "output_root": ".",
    "server_port": 8765,
    "thumbnail_cache_limit_gb": 20,
    "photo_page_size": 24,
    "video_page_size": 48,
    "gif_page_size": 48,
    "other_page_size": 100,
    "folder_page_size": 60,
    "image_thumb_size": [600, 800],
    "gif_thumb_size": [300, 300],
    "video_preview_width": 1024,
    "ffmpeg_timeout_seconds": 180,
    "ffmpeg_threads_per_job": 1,
}


class SetupInstanceError(RuntimeError):
    """Raised when an installable catalog instance cannot be created safely."""


@dataclass(frozen=True)
class SetupWriteAction:
    """One write operation prepared by the setup preflight."""

    kind: str
    target: Path
    source: Path | None = None
    content: str | None = None
    newline: str | None = None


@dataclass(frozen=True)
class SetupInstancePlan:
    """Validated, write-free plan for creating one installable catalog instance."""

    source_project_root: Path
    data_root: Path
    catalog_root: Path
    output_root: Path
    app_root: Path
    state_root: Path
    cache_root: Path
    config_path: Path
    config_data_root: str
    settings_path: Path
    launcher_path: Path
    db_path: Path
    copied_items: tuple[str, ...]
    actions: tuple[SetupWriteAction, ...]


@dataclass(frozen=True)
class SetupInstanceResult:
    data_root: Path
    catalog_root: Path
    output_root: Path
    app_root: Path
    config_path: Path
    config_data_root: str
    launcher_path: Path
    db_path: Path
    copied_items: tuple[str, ...]
    database_created: bool


def setup_installable_instance(
    *,
    source_project_root: Path,
    data_root: Path,
    catalog_root: Path | None = None,
    output_dir_name: str = DEFAULT_OUTPUT_DIR_NAME,
    launcher_name: str = DEFAULT_LAUNCHER_NAME,
    python_executable: str | None = None,
) -> SetupInstanceResult:
    """Build and execute one safe setup plan.

    This compatibility entry point keeps the existing CLI behavior while all
    validation now happens before the first write operation.
    """
    plan = build_setup_plan(
        source_project_root=source_project_root,
        data_root=data_root,
        catalog_root=catalog_root,
        output_dir_name=output_dir_name,
        launcher_name=launcher_name,
        python_executable=python_executable,
    )
    return execute_setup_plan(plan)


def build_setup_plan(
    *,
    source_project_root: Path,
    data_root: Path,
    catalog_root: Path | None = None,
    output_dir_name: str = DEFAULT_OUTPUT_DIR_NAME,
    launcher_name: str = DEFAULT_LAUNCHER_NAME,
    python_executable: str | None = None,
) -> SetupInstancePlan:
    """Validate setup and return the exact write plan without changing disk."""
    source_project_root = source_project_root.resolve()
    data_root = data_root.expanduser().resolve()

    _validate_data_root(data_root)

    output_dir_name = _safe_relative_name(output_dir_name, "output directory name")
    launcher_name = _safe_launcher_name(launcher_name)

    if catalog_root is None:
        catalog_root = suggest_catalog_root(data_root)
        if catalog_root is None:
            raise SetupInstanceError(
                "A filesystem root requires an explicit catalog folder outside the source data. "
                "Pass --catalog-root or enter a different catalog folder interactively."
            )
    else:
        expanded_catalog_root = catalog_root.expanduser()
        if not expanded_catalog_root.is_absolute():
            raise SetupInstanceError(
                f"Catalog root must be an absolute path: {catalog_root}"
            )
        catalog_root = expanded_catalog_root.resolve()

    if same_path(catalog_root, data_root):
        raise SetupInstanceError(
            f"Catalog root must not match the source data folder: {catalog_root}"
        )
    if is_path_within(catalog_root, data_root):
        raise SetupInstanceError(
            f"Catalog root must not be inside the source data folder: {catalog_root}"
        )

    output_root = catalog_root / output_dir_name
    app_root = output_root / "app"
    state_root = output_root / "_state"
    cache_root = output_root / "_cache"
    config_path = output_root / "config.json"
    settings_path = state_root / "settings.json"
    launcher_path = catalog_root / launcher_name
    db_path = output_root / "catalog.db"

    if same_path(output_root, data_root) or is_path_within(output_root, data_root):
        raise SetupInstanceError("Catalog output must not be the same as, or inside, the source data folder.")

    _ensure_runtime_source(source_project_root)
    _validate_new_catalog_root(catalog_root)

    config_data_root = _config_data_root_value(data_root=data_root, output_root=output_root)
    config_text = _instance_config_text(config_data_root=config_data_root)
    settings_text = _default_runtime_settings_text()
    launcher_text = _windows_launcher_body(
        output_dir_name=output_dir_name,
        python_executable=python_executable or sys.executable,
    )

    actions = _build_setup_actions(
        source_project_root=source_project_root,
        catalog_root=catalog_root,
        output_root=output_root,
        app_root=app_root,
        state_root=state_root,
        cache_root=cache_root,
        config_path=config_path,
        config_text=config_text,
        settings_path=settings_path,
        settings_text=settings_text,
        launcher_path=launcher_path,
        launcher_text=launcher_text,
        db_path=db_path,
    )

    return SetupInstancePlan(
        source_project_root=source_project_root,
        data_root=data_root,
        catalog_root=catalog_root,
        output_root=output_root,
        app_root=app_root,
        state_root=state_root,
        cache_root=cache_root,
        config_path=config_path,
        config_data_root=config_data_root,
        settings_path=settings_path,
        launcher_path=launcher_path,
        db_path=db_path,
        copied_items=tuple(RUNTIME_COPY_ITEMS),
        actions=actions,
    )


def execute_setup_plan(plan: SetupInstancePlan) -> SetupInstanceResult:
    """Execute a previously validated setup plan without recalculating targets."""
    _validate_new_catalog_root(plan.catalog_root)

    database_created: bool | None = None

    for action in plan.actions:
        try:
            if action.kind == "create_dir":
                action.target.mkdir(parents=False, exist_ok=False)
            elif action.kind == "copy_tree":
                if action.source is None:
                    raise SetupInstanceError(f"Missing source for copy action: {action.target}")
                shutil.copytree(action.source, action.target, ignore=_runtime_ignore)
            elif action.kind == "copy_file":
                if action.source is None:
                    raise SetupInstanceError(f"Missing source for copy action: {action.target}")
                shutil.copy2(action.source, action.target)
            elif action.kind == "write_text":
                if action.content is None:
                    raise SetupInstanceError(f"Missing content for write action: {action.target}")
                _atomic_write_text(action.target, action.content, newline=action.newline)
            elif action.kind == "validate_config":
                config = load_config(action.target)
                if not same_path(config.db_path, plan.db_path):
                    raise SetupInstanceError(
                        f"Prepared database path does not match loaded config: {config.db_path}"
                    )
            elif action.kind == "initialize_database":
                database_created = initialize_database(action.target)
            else:
                raise SetupInstanceError(f"Unknown setup action: {action.kind}")
        except SetupInstanceError:
            raise
        except (OSError, shutil.Error) as exc:
            raise SetupInstanceError(
                f"Setup failed during {action.kind} for {action.target}: {exc}"
            ) from exc

    if database_created is None:
        raise SetupInstanceError("The prepared setup plan did not initialize or validate the database.")

    return SetupInstanceResult(
        data_root=plan.data_root,
        catalog_root=plan.catalog_root,
        output_root=plan.output_root,
        app_root=plan.app_root,
        config_path=plan.config_path,
        config_data_root=plan.config_data_root,
        launcher_path=plan.launcher_path,
        db_path=plan.db_path,
        copied_items=plan.copied_items,
        database_created=database_created,
    )


def setup_instance_result_lines(result: SetupInstanceResult) -> list[str]:
    db_action = "created" if result.database_created else "already existed and was checked/upgraded"
    lines = [
        "Catalog 2.0 – setup installable catalog instance",
        "=" * 70,
        f"source data:     {result.data_root}",
        f"catalog root:    {result.catalog_root}",
        f"output folder:   {result.output_root}",
        f"runtime app:     {result.app_root}",
        f"config:          {result.config_path}",
        f"config data_root: {result.config_data_root}",
        f"database:        {result.db_path} ({db_action})",
        f"launcher:        {result.launcher_path}",
        "",
        "Copied runtime items:",
    ]
    lines.extend(f"- {item}" for item in result.copied_items)
    lines.extend(
        [
            "",
            "Created instance layout:",
            f"- source data remains at: {result.data_root}",
            f"- catalog folder: {result.catalog_root}",
            f"- output folder: {result.output_root}",
            f"- launcher: {result.launcher_path}",
            "",
            "Original source media was not modified.",
            "No scan was started. Start the catalog and run catalog update from the UI.",
        ]
    )
    return lines


def _validate_data_root(data_root: Path) -> None:
    if not data_root.exists():
        raise SetupInstanceError(f"Source data folder does not exist: {data_root}")
    if not data_root.is_dir():
        raise SetupInstanceError(f"Source data path is not a folder: {data_root}")


def suggest_catalog_root(data_root: Path) -> Path | None:
    """Return the default sibling instance path, or None for a filesystem root.

    A filesystem root cannot safely host its own catalog as a child because
    every child folder belongs to the selected source tree.
    """
    resolved_data_root = data_root.expanduser().resolve()
    _validate_data_root(resolved_data_root)
    if resolved_data_root.parent == resolved_data_root:
        return None
    return resolved_data_root.parent / _default_instance_dir_name(resolved_data_root)


def _default_instance_dir_name(data_root: Path) -> str:
    """Return a stable Windows-safe default instance folder name for one source folder."""
    source_name = data_root.name
    sanitized = re.sub(r'[\s<>:"/\\|?*\x00-\x1f]+', '_', source_name)
    sanitized = re.sub(r'_+', '_', sanitized).strip(' ._')
    if not sanitized:
        raise SetupInstanceError(
            f"Cannot derive a usable catalog folder name from source folder: {data_root}"
        )
    return f"{DEFAULT_INSTANCE_DIR_PREFIX}{sanitized}"



def _validate_new_catalog_root(catalog_root: Path) -> None:
    """Require a completely new destination for every fresh setup."""
    if os.path.lexists(catalog_root):
        raise SetupInstanceError(
            f"Catalog root already exists and was left unchanged: {catalog_root}. "
            "Choose a different catalog root or use update-instance for an existing catalog."
        )

    parent = catalog_root.parent
    if not parent.exists():
        raise SetupInstanceError(f"Catalog root parent folder does not exist: {parent}")
    if not parent.is_dir():
        raise SetupInstanceError(f"Catalog root parent path is not a folder: {parent}")


def _build_setup_actions(
    *,
    source_project_root: Path,
    catalog_root: Path,
    output_root: Path,
    app_root: Path,
    state_root: Path,
    cache_root: Path,
    config_path: Path,
    config_text: str,
    settings_path: Path,
    settings_text: str,
    launcher_path: Path,
    launcher_text: str,
    db_path: Path,
) -> tuple[SetupWriteAction, ...]:
    actions: list[SetupWriteAction] = [
        SetupWriteAction("create_dir", catalog_root),
        SetupWriteAction("create_dir", output_root),
        SetupWriteAction("create_dir", state_root),
        SetupWriteAction("create_dir", cache_root),
        SetupWriteAction("create_dir", app_root),
    ]

    for name in RUNTIME_COPY_ITEMS:
        source = source_project_root / name
        target = app_root / name
        kind = "copy_tree" if source.is_dir() else "copy_file"
        actions.append(SetupWriteAction(kind, target, source=source))

    actions.append(SetupWriteAction("write_text", config_path, content=config_text))
    actions.append(SetupWriteAction("write_text", settings_path, content=settings_text))
    actions.append(SetupWriteAction("validate_config", config_path))
    actions.append(SetupWriteAction("initialize_database", db_path))
    actions.append(SetupWriteAction("write_text", launcher_path, content=launcher_text, newline=""))
    return tuple(actions)


def _safe_relative_name(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise SetupInstanceError(f"The {label} must not be empty.")
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise SetupInstanceError(f"The {label} must be a simple relative name, not a path: {value}")
    return value


def _safe_launcher_name(value: str) -> str:
    value = _safe_relative_name(value, "launcher name")
    if not value.lower().endswith((".bat", ".cmd")):
        raise SetupInstanceError("The Windows launcher name must end with .bat or .cmd.")
    return value


def _ensure_runtime_source(source_project_root: Path) -> None:
    missing = [name for name in RUNTIME_COPY_ITEMS if not (source_project_root / name).exists()]
    if missing:
        raise SetupInstanceError(
            "The source project root does not contain required runtime files: " + ", ".join(missing)
        )


def _runtime_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in {"__pycache__", ".pytest_cache", ".mypy_cache"}:
            ignored.add(name)
        elif name.endswith((".pyc", ".pyo")):
            ignored.add(name)
    return ignored


def _instance_config_text(*, config_data_root: str) -> str:
    raw = dict(DEFAULT_INSTANCE_CONFIG)
    raw["data_root"] = config_data_root
    raw["output_root"] = "."
    return json.dumps(raw, ensure_ascii=False, indent=2) + "\n"


def _default_runtime_settings_text() -> str:
    settings = {
        "settings_version": SUPPORTED_RUNTIME_SETTINGS_VERSION,
        "ui_locale": DEFAULT_TRANSITION_UI_LOCALE,
        "ui_theme": DEFAULT_UI_THEME,
        "catalog_title": DEFAULT_CATALOG_TITLE,
    }
    return json.dumps(settings, ensure_ascii=False, indent=2) + "\n"


def _config_data_root_value(*, data_root: Path, output_root: Path) -> str:
    """Return a relative path when possible, otherwise an absolute path.

    On Windows, ``os.path.relpath`` raises ``ValueError`` for different drive
    letters or different UNC shares. An absolute source path is required in
    that case.
    """
    try:
        return os.path.relpath(data_root, output_root)
    except ValueError:
        return str(data_root)


def _windows_launcher_body(
    *,
    output_dir_name: str,
    python_executable: str,
) -> str:
    return fr'''@echo off
setlocal

rem Catalog 2.0 launcher generated by setup-instance.
rem This file is safe to edit manually if your Python path changes.

set "INSTANCE_ROOT=%~dp0"
set "CATALOG_OUTPUT=%INSTANCE_ROOT%{output_dir_name}"
set "CATALOG_APP=%CATALOG_OUTPUT%\app"
set "CATALOG_CONFIG=%CATALOG_OUTPUT%\config.json"
set "PYTHON_EXE={python_executable}"

echo Catalog 2.0 - starting or opening this catalog instance...
echo Catalog output: "%CATALOG_OUTPUT%"
echo.

cd /d "%CATALOG_APP%"
"%PYTHON_EXE%" catalog2.py launch --config "%CATALOG_CONFIG%"

if errorlevel 1 (
    echo.
    echo Catalog 2.0 stopped with an error.
    pause
)

endlocal
'''


def _write_windows_launcher(
    *,
    launcher_path: Path,
    output_dir_name: str,
    python_executable: str,
) -> None:
    """Write a generated launcher; retained for update-instance compatibility."""
    _atomic_write_text(
        launcher_path,
        _windows_launcher_body(
            output_dir_name=output_dir_name,
            python_executable=python_executable,
        ),
        newline="",
    )


def _atomic_write_text(path: Path, content: str, *, newline: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(content, encoding="utf-8", newline=newline)
    os.replace(temp_path, path)
