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
        initial = self._function_body("async function openInitialFolderView()")
        self.assertIn(
            'await openFolder(initial.folder, { historyMode: "restore", historySnapshot: initial })',
            initial,
        )
        self.assertIn('await openFolder("", { historyMode: "replace" })', initial)
        self.assertIn("await openInitialFolderView()", self.source)

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
        tabs_end = self.source.index("\nfor (const pager of els.mediaPagers)", tabs_start)
        tabs = self.source[tabs_start:tabs_end]
        collapse_start = self.source.index('\nif (els.childFoldersToggle)') + 1
        collapse_end = self.source.index("\nfor (const pager", collapse_start)
        collapse = self.source[collapse_start:collapse_end]
        self.assertIn("syncCurrentCatalogHistorySnapshot()", tabs)
        self.assertIn("syncCurrentCatalogHistorySnapshot()", collapse)
        self.assertNotIn("pushFolderHistoryEntry", tabs)
        self.assertNotIn("pushFolderHistoryEntry", collapse)

    def test_folder_page_changes_push_a_history_step(self) -> None:
        helper = self._function_body("function pushFolderPageHistoryEntry()")
        self.assertIn("pushFolderHistoryEntry(state.folder)", helper)
        for signature, page_field in (
            ("async function goToMediaPage(page)", "state.mediaPage"),
            ("async function goToChildPage(page)", "state.childPage"),
        ):
            with self.subTest(navigation=signature):
                body = self._function_body(signature)
                flush = body.index("flushCatalogHistoryScrollSync()")
                assign = body.index(f"{page_field} = targetPage;", flush)
                self.assertLess(flush, assign)
                self.assertIn("folderPageNavigationsPending += 1", body)
                self.assertIn("folderPageNavigationsPending -= 1", body)
                self.assertIn("suppressCatalogHistoryScrollSync()", body)
                self.assertIn("pushFolderPageHistoryEntry()", body)
                self.assertIn('state.view === "folder"', body)
                self.assertIn("state.folder === folder", body)
                self.assertIn(f"{page_field} === targetPage", body)
                self.assertNotIn("pushSearchHistoryEntry", body)
                self.assertNotIn("pushFavoritesHistoryEntry", body)
                # Search and Favorites still only update their current entry.
                self.assertIn("syncCurrentCatalogHistorySnapshot()", body)

    def test_scroll_sync_is_suspended_while_a_page_loads(self) -> None:
        schedule = self._function_body("function scheduleCatalogHistoryScrollSync()")
        flush = self._function_body("function flushCatalogHistoryScrollSync()")
        self.assertIn("let folderPageNavigationsPending = 0;", self.source)
        self.assertEqual(2, schedule.count("if (folderPageNavigationsPending > 0) return;"))
        self.assertIn("if (folderPageNavigationsPending > 0) return;", flush)

    def test_folder_entries_carry_a_url_without_default_values(self) -> None:
        self.assertIn(
            'const FOLDER_URL_PARAMS = ["folder", "filter", "folder_page", "media_page"];',
            self.source,
        )
        base = self._function_body("function catalogBaseUrl()")
        self.assertIn("new URL(window.location.href)", base)
        self.assertIn("for (const name of FOLDER_URL_PARAMS)", base)
        self.assertIn("url.searchParams.delete(name)", base)

        url = self._function_body("function folderHistoryUrl(entry)")
        self.assertIn("const url = catalogBaseUrl()", url)
        self.assertIn('if (entry.folder) url.searchParams.set("folder", entry.folder)', url)
        self.assertIn('if (entry.contentFilter !== "all") url.searchParams.set("filter"', url)
        self.assertIn('if (entry.childPage > 1) url.searchParams.set("folder_page"', url)
        self.assertIn('if (entry.mediaPage > 1) url.searchParams.set("media_page"', url)

        string = self._function_body("function catalogUrlString(url)")
        self.assertIn("${url.pathname}${url.search}${url.hash}", string)

        replace = self._function_body("function replaceFolderHistoryEntry(folder, returnAnchor = null)")
        push = self._function_body("function pushFolderHistoryEntry(folder, returnAnchor = null)")
        self.assertIn(
            'window.history.replaceState(historyState, "", folderHistoryUrl(historyState.catalog))',
            replace,
        )
        self.assertIn(
            'window.history.pushState(historyState, "", folderHistoryUrl(historyState.catalog))',
            push,
        )

    def test_back_between_pages_of_the_same_folder_restores_in_place(self) -> None:
        popstate_start = self.source.index('window.addEventListener("popstate"')
        popstate_end = self.source.index("\n});", popstate_start) + len("\n});")
        popstate = self.source[popstate_start:popstate_end]
        in_place = popstate.index("canRestoreFolderPageInPlace(entry)")
        full = popstate.index('historyMode: "restore"')
        self.assertLess(in_place, full)
        self.assertIn("restoreFolderPageHistoryEntry(entry)", popstate)

        condition = self._function_body("function canRestoreFolderPageInPlace(entry)")
        self.assertIn('state.view === "folder"', condition)
        self.assertIn("state.folder === entry.folder", condition)
        self.assertIn("state.contentFilter === entry.contentFilter", condition)
        self.assertIn("!entry.returnAnchor", condition)

        restore = self._function_body("async function restoreFolderPageHistoryEntry(entry)")
        self.assertIn("? () => loadCurrentFolder()", restore)
        self.assertIn(": loadCurrentFolderMediaPage", restore)
        self.assertIn("correctRestoredPagedViewState()", restore)
        self.assertIn("restoreCatalogContentScroll(entry.scrollTop)", restore)
        self.assertIn("replaceFolderHistoryEntry(folder)", restore)
        self.assertIn("setChildFoldersCollapsed(entry.childFoldersCollapsed)", restore)
        self.assertIn("isCurrent(", restore)
        self.assertIn("folderPageNavigationsPending += 1", restore)
        self.assertIn("folderPageNavigationsPending -= 1", restore)
        for forbidden in ("loadRootFolders", "pushFolderHistoryEntry", "pushState"):
            self.assertNotIn(forbidden, restore)

    def test_startup_reads_the_folder_view_from_the_url(self) -> None:
        view = self._function_body("function folderViewFromUrl()")
        self.assertIn("new URLSearchParams(window.location.search)", view)
        self.assertIn('params.get("folder") || ""', view)
        self.assertIn("FOLDER_HISTORY_CONTENT_FILTERS.has(filter) ? filter : \"all\"", view)
        self.assertIn('positiveHistoryPage(params.get("folder_page"))', view)
        self.assertIn('positiveHistoryPage(params.get("media_page"))', view)
        self.assertIn("returnAnchor: null", view)
        self.assertIn("scrollTop: 0", view)
        # Scroll and collapse come only from an entry describing the same view.
        self.assertIn("const current = catalogFolderHistoryEntry()", view)
        for field in ("folder", "contentFilter", "childPage", "mediaPage"):
            self.assertIn(f"current.{field} === view.{field}", view)
        self.assertIn("view.childFoldersCollapsed = current.childFoldersCollapsed", view)
        self.assertIn("view.scrollTop = current.scrollTop", view)

    def test_startup_falls_back_to_root_for_a_missing_folder(self) -> None:
        initial = self._function_body("async function openInitialFolderView()")
        self.assertIn("const initial = folderViewFromUrl()", initial)
        self.assertIn('initial.folder !== ""', initial)
        self.assertIn("error?.status === 404 || error?.status === 400", initial)
        self.assertIn("if (!folderNotFound) throw error", initial)
        fallback = initial.index('await openFolder("", { historyMode: "replace" })')
        message = initial.index('text("navigation.folderNotFound", { path: initial.folder })')
        self.assertLess(fallback, message)

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
