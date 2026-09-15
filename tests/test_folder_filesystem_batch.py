from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_app.api import (
    _FolderBrowseDiagnostics,
    _FolderFilesystemSnapshot,
    _attach_folder_filesystem_status_batch,
    _attach_folder_filesystem_status_targeted,
    _folder_filesystem_snapshot,
    _folder_filesystem_status,
    child_folders,
)
from catalog_app.config import load_config
from catalog_app.database import initialize_database
from catalog_app.scanner import source_root_status
from catalog_app.setup_instance import _instance_config_text
from catalog_app.sorting import catalog_path_key

ORIGINAL_SCANDIR = os.scandir


class FolderFilesystemBatchTests(unittest.TestCase):
    def test_non_root_targets_only_page_folders_without_parent_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))
            folders = [
                {"rel_path": "parent_folder/child_ok"},
                {"rel_path": "parent_folder/child_missing"},
                {"rel_path": "parent_folder/child_file"},
            ]
            diagnostics = _FolderBrowseDiagnostics()

            with patch("catalog_app.api.os.scandir", wraps=ORIGINAL_SCANDIR) as scandir, patch(
                "catalog_app.api._folder_filesystem_status",
                wraps=_folder_filesystem_status,
            ) as status_check:
                _attach_folder_filesystem_status_targeted(
                    config,
                    folders,
                    diagnostics=diagnostics,
                )

            self.assertEqual(1, diagnostics.source_root_status_checks)
            self.assertEqual(0, diagnostics.folder_fs_batch_enumerations)
            self.assertEqual(0.0, diagnostics.folder_fs_batch_ms)
            self.assertEqual(3, diagnostics.folder_fs_checks)
            self.assertEqual(0, diagnostics.folder_fs_fallback_checks)
            self.assertEqual([config.data_root], [call.args[0] for call in scandir.call_args_list])
            self.assertEqual(3, status_check.call_count)
            resolved_roots = {
                call.kwargs["resolved_root"] for call in status_check.call_args_list
            }
            self.assertEqual(1, len(resolved_roots))
            self.assertNotIn(None, resolved_roots)

    def test_non_root_listing_uses_database_page_without_filesystem_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))

            with patch("catalog_app.api.os.scandir", wraps=ORIGINAL_SCANDIR) as scandir, patch(
                "catalog_app.api._folder_filesystem_status",
                wraps=_folder_filesystem_status,
            ) as status_check, patch("catalog_app.api.source_root_status") as source_check:
                response = child_folders(
                    config,
                    "parent_folder",
                    raw_page="1",
                    raw_page_size="1",
                    raw_include_previews="0",
                )

            self.assertEqual(1, response["count"])
            self.assertGreater(response["total"], response["count"])
            self.assertEqual(1, response["page"])
            self.assertEqual(1, response["page_size"])
            self.assertNotIn("filesystem", response["folders"][0])
            scandir.assert_not_called()
            status_check.assert_not_called()
            source_check.assert_not_called()

    def test_empty_non_root_page_performs_no_child_filesystem_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))

            with patch("catalog_app.api.os.scandir", wraps=ORIGINAL_SCANDIR) as scandir, patch(
                "catalog_app.api._folder_filesystem_status",
                wraps=_folder_filesystem_status,
            ) as status_check, patch("catalog_app.api.source_root_status") as source_check:
                response = child_folders(
                    config,
                    "parent_folder/child_ok",
                    raw_page="1",
                    raw_page_size="20",
                    raw_include_previews="0",
                )

            self.assertEqual(0, response["count"])
            scandir.assert_not_called()
            status_check.assert_not_called()
            source_check.assert_not_called()

    def test_root_reuses_one_snapshot_for_active_and_disk_only_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))

            with patch(
                "catalog_app.api._folder_filesystem_snapshot",
                wraps=_folder_filesystem_snapshot,
            ) as snapshot:
                response = child_folders(
                    config,
                    "",
                    raw_page="1",
                    raw_page_size="20",
                    raw_include_previews="0",
                )

            self.assertEqual(1, snapshot.call_count)
            folders = {folder["name"]: folder for folder in response["folders"]}
            self.assertEqual("ok", folders["parent_folder"]["filesystem"]["reason"])
            self.assertTrue(folders["disk_only"]["is_disk_candidate"])

    def test_non_root_payload_matches_with_and_without_previews_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))
            database_before = config.db_path.read_bytes()

            without_previews = child_folders(
                config,
                "parent_folder",
                raw_page="1",
                raw_page_size="20",
                raw_include_previews="0",
            )
            with_previews = child_folders(
                config,
                "parent_folder",
                raw_page="1",
                raw_page_size="20",
                raw_include_previews="1",
            )

            self.assertEqual(
                [folder["rel_path"] for folder in without_previews["folders"]],
                [folder["rel_path"] for folder in with_previews["folders"]],
            )
            self.assertEqual(without_previews["total"], with_previews["total"])
            self.assertEqual(without_previews["pages"], with_previews["pages"])
            self.assertTrue(all("filesystem" not in folder for folder in without_previews["folders"]))
            self.assertTrue(all("filesystem" not in folder for folder in with_previews["folders"]))
            self.assertEqual(database_before, config.db_path.read_bytes())

    def test_non_root_listing_uses_active_snapshot_when_source_root_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self._make_catalog(root)
            config.data_root.rename(root / "data_root_unavailable")

            response = child_folders(
                config,
                "parent_folder",
                raw_page="1",
                raw_page_size="20",
                raw_include_previews="0",
            )

            self.assertGreater(response["count"], 0)
            self.assertTrue(all("filesystem" not in folder for folder in response["folders"]))

    def test_incomplete_batch_uses_individual_fallback_without_source_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self._make_catalog(Path(temp))
            source_status = source_root_status(config)
            snapshot = _FolderFilesystemSnapshot(
                source_status=source_status,
                resolved_root=config.data_root.resolve(strict=True),
                entries={},
                ambiguous_path_keys=frozenset(),
                complete=False,
            )
            diagnostics = _FolderBrowseDiagnostics()
            folders = [{"rel_path": "parent_folder/child_ok"}]

            with patch(
                "catalog_app.api._folder_filesystem_snapshot",
                return_value=snapshot,
            ), patch(
                "catalog_app.api._folder_filesystem_status",
                wraps=_folder_filesystem_status,
            ) as fallback, patch("catalog_app.api.source_root_status") as source_check:
                _attach_folder_filesystem_status_batch(
                    config,
                    "parent_folder",
                    folders,
                    diagnostics=diagnostics,
                )

            self.assertEqual("ok", folders[0]["filesystem"]["reason"])
            self.assertEqual(1, fallback.call_count)
            self.assertEqual(1, diagnostics.folder_fs_fallback_checks)
            source_check.assert_not_called()

    @staticmethod
    def _status_by_name(response: dict) -> dict[str, str]:
        return {
            str(folder["name"]): str(folder["filesystem"]["reason"])
            for folder in response["folders"]
        }

    @staticmethod
    def _make_catalog(root: Path):
        data_root = root / "data_root"
        parent_path = data_root / "parent_folder"
        output_root = root / "Catalog_Output"
        parent_path.mkdir(parents=True)
        (parent_path / "child_ok").mkdir()
        (parent_path / "child_case").mkdir()
        (parent_path / "child_file").write_text("test", encoding="utf-8")
        (data_root / "disk_only").mkdir()
        link_created = False
        try:
            (parent_path / "child_link").symlink_to(parent_path / "child_ok", target_is_directory=True)
            link_created = True
        except OSError:
            pass

        output_root.mkdir()
        config_path = output_root / "config.json"
        config_path.write_text(
            _instance_config_text(config_data_root=str(data_root)),
            encoding="utf-8",
        )
        initialize_database(output_root / "catalog.db")
        connection = sqlite3.connect(output_root / "catalog.db")
        try:
            scan_id = connection.execute(
                """
                INSERT INTO scan_sessions (
                    scan_type, scope_rel_path, scope_path_key, started_at,
                    finished_at, status, folder_count, media_count, error_count
                ) VALUES ('full', '', '', 1, 2, 'completed', 7, 0, 0)
                """
            ).lastrowid
            root_id = FolderFilesystemBatchTests._insert_folder(
                connection, scan_id, "", None
            )
            parent_id = FolderFilesystemBatchTests._insert_folder(
                connection, scan_id, "parent_folder", root_id
            )
            for name in ("child_ok", "child_missing", "child_file"):
                FolderFilesystemBatchTests._insert_folder(
                    connection,
                    scan_id,
                    f"parent_folder/{name}",
                    parent_id,
                )
            FolderFilesystemBatchTests._insert_folder(
                connection,
                scan_id,
                "parent_folder/CHILD_CASE",
                parent_id,
            )
            if link_created:
                FolderFilesystemBatchTests._insert_folder(
                    connection,
                    scan_id,
                    "parent_folder/child_link",
                    parent_id,
                )
            connection.commit()
        finally:
            connection.close()
        return load_config(config_path)

    @staticmethod
    def _insert_folder(
        connection: sqlite3.Connection,
        scan_id: int,
        rel_path: str,
        parent_id: int | None,
    ) -> int:
        name = Path(rel_path).name if rel_path else "Root"
        cursor = connection.execute(
            """
            INSERT INTO folders (
                rel_path, path_key, parent_id, name, depth, sort_key,
                last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                rel_path,
                catalog_path_key(rel_path),
                parent_id,
                name,
                0 if not rel_path else len(Path(rel_path).parts),
                name.casefold(),
                scan_id,
            ),
        )
        return int(cursor.lastrowid)


if __name__ == "__main__":
    unittest.main()
