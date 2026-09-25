from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from catalog_app.api import child_folder_page_anchor
from catalog_app.database import initialize_database


class FolderHistoryAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.config = SimpleNamespace(
            db_path=root / "catalog.db",
            data_root=root / "data",
            favorites_json=root / "favorites.json",
            folder_page_size=2,
        )
        self.config.data_root.mkdir()
        initialize_database(self.config.db_path)
        connection = sqlite3.connect(self.config.db_path)
        try:
            scan_id = int(connection.execute(
                """
                INSERT INTO scan_sessions (
                    scan_type, scope_rel_path, scope_path_key, started_at,
                    finished_at, status, folder_count, media_count, error_count
                ) VALUES ('full', '', '', 1, 2, 'completed', 5, 0, 0)
                """
            ).lastrowid)
            root_id = self._insert_folder(connection, scan_id, None, "", "", "", 0)
            parent_id = self._insert_folder(
                connection, scan_id, root_id, "parent_folder", "parent_folder", "parent_folder", 1,
            )
            for name in ("child_a", "child_b", "child_c"):
                self._insert_folder(
                    connection,
                    scan_id,
                    parent_id,
                    f"parent_folder/{name}",
                    name,
                    name,
                    2,
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
        sort_key: str,
        depth: int,
    ) -> int:
        return int(connection.execute(
            """
            INSERT INTO folders (
                rel_path, path_key, parent_id, name, depth, sort_key,
                last_successful_scan_id, is_available
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (rel_path, rel_path, parent_id, name, depth, sort_key, scan_id),
        ).lastrowid)

    def test_anchor_page_uses_current_order_and_page_size(self) -> None:
        result = child_folder_page_anchor(
            self.config,
            raw_parent="parent_folder",
            raw_anchor="parent_folder/child_c",
        )
        self.assertTrue(result["found"])
        self.assertEqual(2, result["page"])

        self.config.folder_page_size = 3
        resized = child_folder_page_anchor(
            self.config,
            raw_parent="parent_folder",
            raw_anchor="parent_folder/child_c",
        )
        self.assertEqual(1, resized["page"])

        connection = sqlite3.connect(self.config.db_path)
        try:
            connection.execute(
                "UPDATE folders SET sort_key = ? WHERE rel_path = ?",
                ("00_child_c", "parent_folder/child_c"),
            )
            connection.commit()
        finally:
            connection.close()
        self.config.folder_page_size = 2
        reordered = child_folder_page_anchor(
            self.config,
            raw_parent="parent_folder",
            raw_anchor="parent_folder/child_c",
        )
        self.assertEqual(1, reordered["page"])

    def test_missing_anchor_falls_back_to_first_page(self) -> None:
        result = child_folder_page_anchor(
            self.config,
            raw_parent="parent_folder",
            raw_anchor="parent_folder/missing_folder",
        )
        self.assertFalse(result["found"])
        self.assertEqual(1, result["page"])

    def test_root_anchor_uses_combined_active_and_disk_candidate_order(self) -> None:
        (self.config.data_root / "disk_folder").mkdir()
        self.config.folder_page_size = 1
        result = child_folder_page_anchor(
            self.config,
            raw_parent="",
            raw_anchor="parent_folder",
        )
        self.assertTrue(result["found"])
        self.assertEqual(2, result["page"])


class FolderHistoryFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (
            Path(__file__).resolve().parents[1] / "catalog_app" / "static" / "app.js"
        ).read_text(encoding="utf-8")

    def test_initial_entry_replaces_and_normal_navigation_pushes(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        self.assertIn('historyMode === "replace"', open_folder)
        self.assertIn("replaceFolderHistoryEntry(nextFolder)", open_folder)
        self.assertIn('historyMode === "push"', open_folder)
        self.assertIn("pushFolderHistoryEntry(nextFolder)", open_folder)
        self.assertIn('await openFolder("", { historyMode: "replace" })', self.source)

    def test_child_card_records_parent_return_anchor(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        render = self._function_body("function renderChildFolders(data)")
        self.assertIn("replaceFolderHistoryEntry(state.folder, returnAnchor)", open_folder)
        self.assertIn("card.dataset.folderPath = folder.rel_path", render)
        self.assertIn("returnAnchor: folder.rel_path", render)

    def test_popstate_restores_without_pushing_and_centers_anchor(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        restore = self._function_body("function restoreChildFolderAnchor(anchor)")
        popstate_start = self.source.index('window.addEventListener("popstate"')
        popstate_end = self.source.index("\n});", popstate_start) + len("\n});")
        popstate = self.source[popstate_start:popstate_end]
        self.assertIn('historyMode: "restore"', popstate)
        self.assertNotIn("pushState", popstate)
        self.assertEqual(1, open_folder.count("pushFolderHistoryEntry(nextFolder)"))
        self.assertIn('else if (historyMode === "push")', open_folder)
        self.assertIn("setChildFoldersCollapsed(false)", restore)
        self.assertIn('candidate.dataset.folderPath === anchor', restore)
        self.assertIn('scrollIntoView({ block: "center", inline: "nearest" })', restore)

    def test_history_contract_does_not_capture_search_or_favorites(self) -> None:
        search = self._function_body("async function startSearch()")
        favorites = self._function_body("async function openFavorites()")
        for body in (search, favorites):
            self.assertNotIn("pushFolderHistoryEntry", body)
            self.assertNotIn("replaceFolderHistoryEntry", body)

    @classmethod
    def _function_body(cls, signature: str) -> str:
        start = cls.source.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := cls.source.find(marker, start + len(signature))) >= 0
        ]
        return cls.source[start:min(boundaries) if boundaries else len(cls.source)]


if __name__ == "__main__":
    unittest.main()
