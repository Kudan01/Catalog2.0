from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_app.setup_instance import (
    RUNTIME_COPY_ITEMS,
    _instance_config_text,
    _windows_launcher_body,
    build_setup_plan,
)
from catalog_app.update_instance import (
    build_update_instance_plan,
    execute_update_instance_plan,
)


class InstanceRuntimeEnvironmentTests(unittest.TestCase):
    def test_launcher_uses_only_instance_virtual_environment(self) -> None:
        body = _windows_launcher_body(output_dir_name="Catalog_Output")

        self.assertIn(
            'set "PYTHON_EXE=%CATALOG_OUTPUT%\\.venv\\Scripts\\python.exe"',
            body,
        )
        self.assertNotIn(str(Path(__file__).resolve().parents[1] / ".venv"), body)

    def test_setup_plan_creates_local_venv_and_installs_runtime_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._make_runtime_source(root / "source")
            data_root = root / "media"
            data_root.mkdir()
            catalog_root = root / "Catalog2_media"

            plan = build_setup_plan(
                source_project_root=source,
                data_root=data_root,
                catalog_root=catalog_root,
                python_executable=r"C:\source-project\.venv\Scripts\python.exe",
            )

            create_action = next(action for action in plan.actions if action.kind == "create_venv")
            install_action = next(
                action for action in plan.actions if action.kind == "install_dependencies"
            )
            launcher_action = next(action for action in plan.actions if action.target == plan.launcher_path)

            self.assertEqual(plan.output_root / ".venv", plan.venv_root)
            self.assertEqual(plan.venv_root, create_action.target)
            self.assertEqual(plan.venv_root, install_action.target)
            self.assertEqual(plan.app_root / "requirements.txt", install_action.source)
            self.assertIn(str(plan.app_root / "requirements.txt"), install_action.command or ())
            self.assertNotIn(r"C:\source-project\.venv\Scripts\python.exe", launcher_action.content or "")
            self.assertIn(r"%CATALOG_OUTPUT%\.venv\Scripts\python.exe", launcher_action.content or "")

    def test_update_without_venv_plans_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, catalog_output = self._make_update_fixture(Path(temp))

            plan = build_update_instance_plan(
                source_project_root=source,
                catalog_output=catalog_output,
                timestamp="20260906_120000",
            )

            self.assertEqual(1, sum(a.kind == "create_venv" for a in plan.actions))
            self.assertEqual(1, sum(a.kind == "install_dependencies" for a in plan.actions))
            self.assertLess(
                next(i for i, a in enumerate(plan.actions) if a.kind == "create_venv"),
                next(i for i, a in enumerate(plan.actions) if a.kind == "install_dependencies"),
            )

            def run_process(command: tuple[str, ...], *, check: bool) -> None:
                self.assertTrue(check)
                if command[1:3] == ("-m", "venv"):
                    plan.venv_root.mkdir()

            with patch(
                "catalog_app.update_instance.subprocess.run",
                side_effect=run_process,
            ) as run:
                execute_update_instance_plan(plan)

            self.assertTrue(plan.venv_root.is_dir())
            self.assertEqual(2, run.call_count)
            launcher_body = plan.launcher_path.read_text(encoding="utf-8")
            self.assertIn(r"%CATALOG_OUTPUT%\.venv\Scripts\python.exe", launcher_body)

    def test_update_with_existing_venv_reuses_it_and_rewrites_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, catalog_output = self._make_update_fixture(Path(temp))
            venv_root = catalog_output / ".venv"
            venv_root.mkdir()
            marker = venv_root / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            launcher = catalog_output.parent / "Start Catalog.bat"
            old_python = r"C:\old-source\.venv\Scripts\python.exe"
            launcher.write_text(f'set "PYTHON_EXE={old_python}"\n', encoding="utf-8")

            plan = build_update_instance_plan(
                source_project_root=source,
                catalog_output=catalog_output,
                timestamp="20260906_120001",
            )

            self.assertFalse(any(a.kind == "create_venv" for a in plan.actions))
            self.assertTrue(any(a.kind == "install_dependencies" for a in plan.actions))

            with patch("catalog_app.update_instance.subprocess.run") as run:
                execute_update_instance_plan(plan)

            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            run.assert_called_once()
            launcher_body = launcher.read_text(encoding="utf-8")
            self.assertNotIn(old_python, launcher_body)
            self.assertIn(r"%CATALOG_OUTPUT%\.venv\Scripts\python.exe", launcher_body)

    @staticmethod
    def _make_runtime_source(path: Path) -> Path:
        path.mkdir()
        for name in RUNTIME_COPY_ITEMS:
            item = path / name
            if name in {"catalog_app", "tools"}:
                item.mkdir()
                (item / "placeholder.txt").write_text(name, encoding="utf-8")
            else:
                item.write_text("Pillow>=10.0\n" if name == "requirements.txt" else "", encoding="utf-8")
        return path

    @classmethod
    def _make_update_fixture(cls, root: Path) -> tuple[Path, Path]:
        source = cls._make_runtime_source(root / "source")
        catalog_output = root / "installed" / "Catalog_Output"
        app_root = catalog_output / "app"
        app_root.mkdir(parents=True)
        cls._make_runtime_source_contents(app_root)
        data_root = root / "media"
        data_root.mkdir()
        (catalog_output / "config.json").write_text(
            _instance_config_text(config_data_root=str(data_root)),
            encoding="utf-8",
        )
        return source, catalog_output

    @staticmethod
    def _make_runtime_source_contents(path: Path) -> None:
        for name in RUNTIME_COPY_ITEMS:
            item = path / name
            if name in {"catalog_app", "tools"}:
                item.mkdir()
                (item / "placeholder.txt").write_text(name, encoding="utf-8")
            else:
                item.write_text("Pillow>=10.0\n" if name == "requirements.txt" else "", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
