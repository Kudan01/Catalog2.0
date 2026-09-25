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
        self.assertIn("pushFolderHistoryEntry(nextFolder, targetEntryAnchor)", open_folder)
        self.assertIn('await openFolder("", { historyMode: "replace" })', self.source)

    def test_history_state_contains_normalized_folder_snapshot_and_legacy_defaults(self) -> None:
        state_builder = self._function_body(
            "function folderHistoryState(folder, returnAnchor = null, snapshot = null)"
        )
        reader = self._function_body(
            "function catalogFolderHistoryEntry(value = window.history.state)"
        )
        for field in (
            "contentFilter",
            "mediaPage",
            "childPage",
            "childFoldersCollapsed",
            "scrollTop",
        ):
            self.assertIn(field, state_builder)
            self.assertIn(field, reader)
        self.assertIn('? entry.contentFilter\n      : "all"', reader)
        self.assertIn("positiveHistoryPage(entry.mediaPage)", reader)
        self.assertIn("positiveHistoryPage(entry.childPage)", reader)
        self.assertIn("entry.childFoldersCollapsed === true", reader)
        self.assertIn("nonnegativeHistoryScroll(entry.scrollTop)", reader)

    def test_child_card_records_parent_return_anchor(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        render = self._function_body("function renderChildFolders(data)")
        self.assertIn("replaceFolderHistoryEntry(state.folder, sourceEntryAnchor)", open_folder)
        self.assertIn("card.dataset.folderPath = folder.rel_path", render)
        self.assertIn("sourceEntryAnchor: folder.rel_path", render)

    def test_breadcrumb_uses_next_item_as_target_entry_anchor(self) -> None:
        render = self._function_body("function renderFolder(folder, breadcrumb)")
        open_folder = self._function_body("async function openFolder(path, options = {})")
        push_entry = self._function_body(
            "function pushFolderHistoryEntry(folder, returnAnchor = null)"
        )
        self.assertIn("breadcrumb[index + 1]?.rel_path || null", render)
        self.assertIn("openFolder(item.rel_path, {", render)
        self.assertIn("targetEntryAnchor", render)
        self.assertIn(
            'resolveChildFolderAnchorPage(nextFolder, targetEntryAnchor)',
            open_folder,
        )
        self.assertIn("pushFolderHistoryEntry(nextFolder, targetEntryAnchor)", open_folder)
        self.assertIn("folderHistoryState(folder, returnAnchor)", push_entry)
        self.assertIn(
            "state.childPage = anchorResolution.found ? anchorResolution.page : 1",
            open_folder,
        )
        self.assertNotIn(
            "replaceFolderHistoryEntry(state.folder, targetEntryAnchor)",
            open_folder,
        )

    def test_popstate_restores_without_pushing_and_centers_anchor(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        restore = self._function_body("function restoreChildFolderAnchor(anchor)")
        popstate_start = self.source.index('window.addEventListener("popstate"')
        popstate_end = self.source.index("\n});", popstate_start) + len("\n});")
        popstate = self.source[popstate_start:popstate_end]
        self.assertIn('historyMode: "restore"', popstate)
        self.assertIn("targetEntryAnchor: entry.returnAnchor", popstate)
        self.assertIn("historySnapshot: entry", popstate)
        self.assertNotIn("pushState", popstate)
        self.assertEqual(1, open_folder.count("pushFolderHistoryEntry(nextFolder, targetEntryAnchor)"))
        self.assertIn('else if (historyMode === "push")', open_folder)
        self.assertIn("setChildFoldersCollapsed(false)", restore)
        self.assertIn('candidate.dataset.folderPath === anchor', restore)
        self.assertIn('scrollIntoView({ block: "center", inline: "nearest" })', restore)

    def test_restore_without_anchor_applies_snapshot_and_repairs_invalid_pages(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        self.assertIn("setContentFilter(targetEntryAnchor ? \"all\"", open_folder)
        self.assertIn("state.mediaPage = restoredSnapshot.mediaPage", open_folder)
        self.assertIn("restoredSnapshot.childPage", open_folder)
        self.assertIn("restoredSnapshot.childFoldersCollapsed", open_folder)
        self.assertIn("clampPageNumber(state.childPage, state.childPages)", open_folder)
        self.assertIn("clampPageNumber(state.mediaPage, state.mediaPages)", open_folder)
        self.assertIn("restoreCatalogContentScroll(restoredSnapshot.scrollTop)", open_folder)
        self.assertIn("replaceFolderHistoryEntry(nextFolder)", open_folder)

    def test_active_anchor_precedes_snapshot_and_programmatic_center_does_not_clear_it(self) -> None:
        open_folder = self._function_body("async function openFolder(path, options = {})")
        restore = self._function_body("function restoreChildFolderAnchor(anchor)")
        self.assertIn("state.childPage = targetEntryAnchor ? 1", open_folder)
        self.assertIn("if (targetEntryAnchor)", open_folder)
        self.assertIn("state.childPage = anchorResolution.found ? anchorResolution.page : 1", open_folder)
        self.assertIn("suppressCatalogHistoryScrollSync()", restore)
        self.assertNotIn("syncCurrentFolderHistorySnapshot", restore)

    def test_user_state_changes_replace_snapshot_and_clear_return_anchor(self) -> None:
        sync = self._function_body(
            "function syncCurrentFolderHistorySnapshot({ clearReturnAnchor = true } = {})"
        )
        media_page = self._function_body("async function goToMediaPage(page)")
        child_page = self._function_body("async function goToChildPage(page)")
        self.assertIn('state.view !== "folder"', sync)
        self.assertIn("entry.folder !== state.folder", sync)
        self.assertIn("clearReturnAnchor ? null : entry.returnAnchor", sync)
        self.assertIn("replaceFolderHistoryEntry", sync)
        self.assertNotIn("pushFolderHistoryEntry", sync)
        self.assertIn("syncCurrentCatalogHistorySnapshot()", media_page)
        self.assertIn("syncCurrentCatalogHistorySnapshot()", child_page)
        tabs_start = self.source.index('for (const button of document.querySelectorAll(".tab"))')
        tabs_end = self.source.index("\nels.firstPage", tabs_start)
        tabs = self.source[tabs_start:tabs_end]
        collapse_start = self.source.index('\nif (els.childFoldersToggle)') + 1
        collapse_end = self.source.index("\nfor (const pager", collapse_start)
        collapse = self.source[collapse_start:collapse_end]
        self.assertIn("syncCurrentCatalogHistorySnapshot()", tabs)
        self.assertIn("syncCurrentCatalogHistorySnapshot()", collapse)
        self.assertNotIn("pushFolderHistoryEntry", tabs)
        self.assertNotIn("pushFolderHistoryEntry", collapse)

    def test_scroll_snapshot_is_debounced(self) -> None:
        schedule = self._function_body("function scheduleCatalogHistoryScrollSync()")
        self.assertIn("catalogHistoryScrollSuppressedUntil", schedule)
        self.assertIn("window.clearTimeout(catalogHistoryScrollTimer)", schedule)
        self.assertIn("window.setTimeout", schedule)
        self.assertIn("CATALOG_HISTORY_SCROLL_DEBOUNCE_MS", schedule)
        self.assertNotIn("replaceState", schedule)

    def test_search_and_favorites_do_not_use_folder_history_writers(self) -> None:
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
