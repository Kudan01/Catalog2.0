from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog_app.api import (
    _favorite_entries_with_folder_branch_renamed,
    _favorite_entries_with_media_path_renamed,
    _favorite_entries_without_branch_path_key,
    _read_favorite_entries,
    _write_favorite_entries,
    child_folders,
    folder_detail,
    favorite_add_action,
    favorite_remove_action,
    favorites_page,
    search_page,
)
from catalog_app.database import initialize_database
from catalog_app.scan_activate import _favorite_entries_without_media_path_keys


class FolderFavoritesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.config = SimpleNamespace(
            db_path=root / "catalog.db",
            favorites_json=root / "favorites.json",
            folder_page_size=60,
        )
        initialize_database(self.config.db_path)
        connection = sqlite3.connect(self.config.db_path)
        try:
            scan_id = int(connection.execute(
                """
                INSERT INTO scan_sessions (
                    scan_type, scope_rel_path, scope_path_key, started_at,
                    finished_at, status, folder_count, media_count, error_count
                ) VALUES ('full', '', '', 1, 2, 'completed', 4, 1, 0)
                """
            ).lastrowid)
            root_id = self._insert_folder(connection, scan_id, None, "", "", 0)
            parent_id = self._insert_folder(
                connection, scan_id, root_id, "parent_folder", "parent_folder", 1,
            )
            self.child_id = self._insert_folder(
                connection, scan_id, parent_id, "parent_folder/child_folder", "child_folder", 2,
            )
            self._insert_folder(
                connection, scan_id, parent_id, "parent_folder/inactive_folder", "inactive_folder", 2,
                available=0,
            )
            connection.execute(
                """
                INSERT INTO media_files (
                    rel_path, path_key, folder_id, file_name, extension, media_type,
                    size_bytes, modified_time, sort_key, last_successful_scan_id,
                    is_available
                ) VALUES (?, ?, ?, ?, 'jpg', 'image', 1, 1, ?, ?, 1)
                """,
                (
                    "parent_folder/example.jpg",
                    "parent_folder/example.jpg",
                    parent_id,
                    "example.jpg",
                    "example.jpg",
                    scan_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _insert_folder(
        connection: sqlite3.Connection,
        scan_id: int,
        parent_id: int | None,
        rel_path: str,
        name: str,
        depth: int,
        *,
        available: int = 1,
    ) -> int:
        return int(connection.execute(
            """
            INSERT INTO folders (
                rel_path, path_key, parent_id, name, depth, sort_key,
                last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rel_path, rel_path, parent_id, name, depth, name, scan_id, available),
        ).lastrowid)

    def test_legacy_v1_is_media_and_next_write_normalizes_to_v2(self) -> None:
        self.config.favorites_json.write_text(json.dumps({
            "version": 1,
            "favorites": [{"path": "parent_folder/example.jpg", "added_at": "kept"}],
        }), encoding="utf-8")

        entries = _read_favorite_entries(self.config)
        self.assertEqual("media", entries[0]["kind"])
        self.assertEqual("kept", entries[0]["added_at"])
        _write_favorite_entries(self.config, entries)

        payload = json.loads(self.config.favorites_json.read_text(encoding="utf-8"))
        self.assertEqual(2, payload["version"])
        self.assertEqual("media", payload["favorites"][0]["kind"])
        self.assertEqual("kept", payload["favorites"][0]["added_at"])

    def test_folder_add_remove_uses_kind_aware_identity(self) -> None:
        favorite_add_action(self.config, "parent_folder/example.jpg")
        favorite_add_action(self.config, "parent_folder/child_folder", "folder")
        entries = _read_favorite_entries(self.config)
        self.assertEqual({"media", "folder"}, {entry["kind"] for entry in entries})

        result = favorite_remove_action(self.config, "parent_folder/child_folder", "folder")
        self.assertTrue(result["removed"])
        self.assertEqual(["media"], [entry["kind"] for entry in _read_favorite_entries(self.config)])

    def test_version_2_identity_is_kind_plus_path_key(self) -> None:
        self.config.favorites_json.write_text(json.dumps({
            "version": 2,
            "favorites": [
                {"kind": "media", "path": "same_path", "added_at": "media-time"},
                {"kind": "folder", "path": "same_path", "added_at": "folder-time"},
            ],
        }), encoding="utf-8")
        entries = _read_favorite_entries(self.config)
        self.assertEqual(["media", "folder"], [entry["kind"] for entry in entries])

    def test_folder_add_rejects_inactive_folder_root_and_invalid_kind(self) -> None:
        with self.assertRaises(Exception):
            favorite_add_action(self.config, "parent_folder/inactive_folder", "folder")
        with self.assertRaises(Exception):
            favorite_add_action(self.config, "", "folder")
        with self.assertRaises(Exception):
            favorite_add_action(self.config, "parent_folder/child_folder", "unknown")

    def test_browse_and_search_annotate_folder_favorite(self) -> None:
        favorite_add_action(self.config, "parent_folder/child_folder", "folder")
        browse = child_folders(
            self.config,
            "parent_folder",
            raw_page="1",
            raw_page_size="60",
            raw_include_previews="0",
        )
        search = search_page(
            self.config,
            raw_query="child_folder",
            raw_folder="",
            raw_content_filter="folders",
            raw_page="1",
            raw_page_size=None,
        )
        self.assertTrue(browse["folders"][0]["is_favorite"])
        self.assertTrue(search["results"][0]["is_favorite"])
        with patch("catalog_app.api._attach_folder_filesystem_status"):
            detail = folder_detail(self.config, "parent_folder/child_folder")
        self.assertTrue(detail["folder"]["is_favorite"])

    def test_media_favorites_view_ignores_folder_entries(self) -> None:
        favorite_add_action(self.config, "parent_folder/example.jpg", "media")
        favorite_add_action(self.config, "parent_folder/child_folder", "folder")
        result = favorites_page(
            self.config,
            raw_media_type="all",
            raw_page="1",
            raw_page_size="50",
        )
        self.assertEqual(1, result["total"])
        self.assertEqual("parent_folder/example.jpg", result["media"][0]["rel_path"])

    def test_rename_and_delete_preserve_kind_added_at_and_branch_scope(self) -> None:
        entries = [
            {"kind": "folder", "path": "branch", "added_at": "folder-time"},
            {"kind": "folder", "path": "branch/child", "added_at": "child-time"},
            {"kind": "media", "path": "branch/example.jpg", "added_at": "media-time"},
            {"kind": "folder", "path": "outside", "added_at": "outside-time"},
        ]
        renamed = _favorite_entries_with_folder_branch_renamed(
            entries,
            old_rel_path="branch",
            old_path_key="branch",
            new_rel_path="renamed",
        )
        self.assertEqual(
            ["renamed", "renamed/child", "renamed/example.jpg", "outside"],
            [entry["path"] for entry in renamed],
        )
        self.assertEqual(
            ["folder-time", "child-time", "media-time", "outside-time"],
            [entry["added_at"] for entry in renamed],
        )
        self.assertEqual([entries[-1]], _favorite_entries_without_branch_path_key(entries, "branch"))

    def test_media_rename_does_not_change_folder_favorite(self) -> None:
        entries = [
            {"kind": "media", "path": "same_path", "added_at": "media-time"},
            {"kind": "folder", "path": "same_path", "added_at": "folder-time"},
        ]
        renamed = _favorite_entries_with_media_path_renamed(
            entries,
            old_path_key="same_path",
            new_rel_path="new_path",
        )
        self.assertEqual("new_path", renamed[0]["path"])
        self.assertEqual("same_path", renamed[1]["path"])

    def test_media_purge_does_not_remove_folder_favorite_with_same_path(self) -> None:
        entries = [
            {"kind": "media", "path": "same_path", "added_at": "media-time"},
            {"kind": "folder", "path": "same_path", "added_at": "folder-time"},
        ]
        self.assertEqual(
            [entries[1]],
            _favorite_entries_without_media_path_keys(entries, {"same_path"}),
        )


class FolderFavoriteFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        static_root = Path(__file__).resolve().parents[1] / "catalog_app" / "static"
        cls.app = (static_root / "app.js").read_text(encoding="utf-8")
        cls.html = (static_root / "index.html").read_text(encoding="utf-8")
        cls.css = (static_root / "style.css").read_text(encoding="utf-8")

    def test_regular_and_search_folder_cards_use_shared_favorite_action(self) -> None:
        regular = self._function_body("function renderChildFolders(data)")
        search = self._function_body("function folderResultCard(folder)")
        helper = self._function_body("function folderFavoriteButton(folder)")
        toggle = self._function_body("async function toggleFolderFavorite(folder, button)")
        self.assertIn("folderFavoriteButton(folder)", regular)
        self.assertIn("folderFavoriteButton(folder)", search)
        self.assertIn("toggleFolderFavorite(folder, button)", helper)
        self.assertIn('setFavoritePath(folder.rel_path, shouldBeFavorite, "folder")', toggle)
        self.assertIn('dataset.action = "favorite-folder"', helper)

    def test_regular_card_moves_system_open_to_more_menu(self) -> None:
        regular = self._function_body("function renderChildFolders(data)")
        menu = self._function_body("function folderMoreMenu(branch, folder = null, options = {})")
        self.assertNotIn("actions.appendChild(folderSystemOpenButton", regular)
        self.assertIn("includeOpen: true", regular)
        self.assertIn("folderSystemOpenButton(branch, folder)", menu)

    def test_current_folder_uses_shared_favorite_toggle_and_more_menu(self) -> None:
        update = self._function_body("function updateCurrentFolderAction(isRunning = state.jobRunning)")
        toggle = self._function_body("async function toggleFolderFavorite(folder, button)")
        self.assertIn('id="currentFolderFavorite"', self.html)
        more_start = self.html.index('id="currentFolderMore"')
        more_end = self.html.index("</details>", more_start)
        self.assertIn('id="currentFolderOpen"', self.html[more_start:more_end])
        self.assertIn("state.currentFolder.is_favorite", update)
        self.assertIn('setFavoritePath(folder.rel_path, shouldBeFavorite, "folder")', toggle)

    def test_media_modal_favorite_width_is_stable_without_changing_other_controls(self) -> None:
        modal = self._function_body("function ensureMediaModal()")
        controls = self._function_body("function updateModalControls(modal, mode)")
        self.assertNotIn("#mediaModalFavorite {\n  width:", self.css)
        self.assertIn("display: inline-grid", self.css)
        self.assertIn('data-favorite-label="add"', modal)
        self.assertIn('data-favorite-label="remove"', modal)
        self.assertIn("setAttribute(\"aria-hidden\"", controls)

    @classmethod
    def _function_body(cls, signature: str) -> str:
        start = cls.app.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := cls.app.find(marker, start + len(signature))) >= 0
        ]
        return cls.app[start:min(boundaries) if boundaries else len(cls.app)]


if __name__ == "__main__":
    unittest.main()
