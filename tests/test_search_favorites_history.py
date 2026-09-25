from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "catalog_app" / "static" / "app.js"


class SearchFavoritesHistoryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS.read_text(encoding="utf-8")

    def test_search_entry_contract_and_defaults(self) -> None:
        builder = self._function_body("function searchHistoryState(snapshot = null)")
        reader = self._function_body(
            "function catalogSearchHistoryEntry(value = window.history.state)"
        )
        self.assertIn('"catalog2.search-view"', self.source)
        for field in (
            "folder",
            "searchQuery",
            "searchFolder",
            "searchInCurrentFolder",
            "contentFilter",
            "childPage",
            "mediaPage",
            "searchFoldersCollapsed",
            "scrollTop",
        ):
            self.assertIn(field, builder)
        self.assertIn("searchHistoryState(entry).catalog", reader)
        self.assertIn('typeof viewSnapshot.searchQuery === "string"', builder)
        self.assertIn("positiveHistoryPage(viewSnapshot.childPage)", builder)
        self.assertIn("nonnegativeHistoryScroll(viewSnapshot.scrollTop)", builder)

    def test_favorites_entry_contract_and_defaults(self) -> None:
        builder = self._function_body("function favoritesHistoryState(snapshot = null)")
        reader = self._function_body(
            "function catalogFavoritesHistoryEntry(value = window.history.state)"
        )
        self.assertIn('"catalog2.favorites-view"', self.source)
        for field in (
            "folder",
            "contentFilter",
            "childPage",
            "mediaPage",
            "favoritesFoldersCollapsed",
            "scrollTop",
        ):
            self.assertIn(field, builder)
        self.assertIn("favoritesHistoryState(entry).catalog", reader)
        self.assertIn("positiveHistoryPage(viewSnapshot.mediaPage)", builder)
        self.assertIn("nonnegativeHistoryScroll(viewSnapshot.scrollTop)", builder)

    def test_favorites_navigation_pushes_once_and_repeat_replaces(self) -> None:
        body = self._function_body("async function openFavorites()")
        self.assertEqual(1, body.count("pushFavoritesHistoryEntry()"))
        self.assertIn('state.view === "favorites"', body)
        self.assertIn("replaceFavoritesHistoryEntry()", body)
        self.assertLess(body.index("await loadCurrentFolder"), body.index("pushFavoritesHistoryEntry"))

    def test_each_successful_search_pushes_but_empty_search_does_not(self) -> None:
        body = self._function_body("async function startSearch()")
        self.assertEqual(1, body.count("pushSearchHistoryEntry()"))
        self.assertLess(body.index("if (!query)"), body.index("pushSearchHistoryEntry()"))
        self.assertLess(body.index("await loadCurrentFolder"), body.index("pushSearchHistoryEntry()"))
        self.assertIn("if (rendered !== false) pushSearchHistoryEntry()", body)

    def test_search_restore_applies_context_controls_pages_collapse_and_scroll(self) -> None:
        body = self._function_body("async function restoreSearchHistoryEntry(entry)")
        for statement in (
            "state.folder = entry.folder",
            "state.searchQuery = entry.searchQuery",
            "state.searchFolder = entry.searchFolder",
            "state.searchInCurrentFolder = entry.searchInCurrentFolder",
            "state.searchFoldersCollapsed = entry.searchFoldersCollapsed",
            "state.childPage = entry.childPage",
            "state.mediaPage = entry.mediaPage",
            "els.searchInput.value = entry.searchQuery",
            "els.searchInCurrentFolder.checked = entry.searchInCurrentFolder",
            "restoreCatalogContentScroll(entry.scrollTop)",
            "correctRestoredPagedViewState()",
            "replaceSearchHistoryEntry()",
        ):
            self.assertIn(statement, body)
        self.assertNotIn("pushState", body)

    def test_favorites_restore_applies_context_pages_collapse_and_scroll(self) -> None:
        body = self._function_body("async function restoreFavoritesHistoryEntry(entry)")
        for statement in (
            "state.folder = entry.folder",
            "state.favoritesFoldersCollapsed = entry.favoritesFoldersCollapsed",
            "state.childPage = entry.childPage",
            "state.mediaPage = entry.mediaPage",
            "restoreCatalogContentScroll(entry.scrollTop)",
            "correctRestoredPagedViewState()",
            "replaceFavoritesHistoryEntry()",
        ):
            self.assertIn(statement, body)
        self.assertNotIn("pushState", body)

    def test_restore_repairs_out_of_range_pages_before_replacing_snapshot(self) -> None:
        correction = self._function_body("function correctRestoredPagedViewState()")
        search = self._function_body("async function restoreSearchHistoryEntry(entry)")
        favorites = self._function_body("async function restoreFavoritesHistoryEntry(entry)")
        self.assertIn("clampPageNumber(state.childPage, state.childPages)", correction)
        self.assertIn("clampPageNumber(state.mediaPage, state.mediaPages)", correction)
        for body, replacement in (
            (search, "replaceSearchHistoryEntry()"),
            (favorites, "replaceFavoritesHistoryEntry()"),
        ):
            self.assertIn("correctRestoredPagedViewState()", body)
            self.assertGreaterEqual(body.count("loadCurrentFolder({ requestId })"), 2)
            self.assertLess(body.index("correctRestoredPagedViewState()"), body.index(replacement))

    def test_opening_folder_from_result_flushes_view_snapshot_without_parent_anchor(self) -> None:
        card = self._function_body("function folderResultCard(folder)")
        open_folder = self._function_body("async function openFolder(path, options = {})")
        self.assertIn("openFolder(folder.rel_path)", card)
        self.assertNotIn("sourceEntryAnchor", card)
        self.assertIn("flushCatalogHistoryScrollSync()", open_folder)
        self.assertIn('state.view = "folder"', open_folder)
        self.assertIn("pushFolderHistoryEntry(nextFolder, targetEntryAnchor)", open_folder)

    def test_snapshot_sync_dispatches_by_matching_view_without_push(self) -> None:
        body = self._function_body("function syncCurrentCatalogHistorySnapshot()")
        self.assertIn('state.view === "folder"', body)
        self.assertIn('state.view === "search"', body)
        self.assertIn('state.view === "favorites"', body)
        self.assertIn("replaceSearchHistoryEntry()", body)
        self.assertIn("replaceFavoritesHistoryEntry()", body)
        self.assertNotIn("push", body)

    def test_search_and_favorites_interactions_replace_their_current_entry(self) -> None:
        media_page = self._function_body("async function goToMediaPage(page)")
        child_page = self._function_body("async function goToChildPage(page)")
        tabs_start = self.source.index('for (const button of document.querySelectorAll(".tab"))')
        tabs_end = self.source.index("\nels.firstPage", tabs_start)
        tabs = self.source[tabs_start:tabs_end]
        collapse_start = self.source.index("\nif (els.childFoldersToggle)") + 1
        collapse_end = self.source.index("\nfor (const pager", collapse_start)
        collapse = self.source[collapse_start:collapse_end]
        for body in (media_page, child_page, tabs, collapse):
            self.assertIn("syncCurrentCatalogHistorySnapshot()", body)
            self.assertNotIn("pushSearchHistoryEntry", body)
            self.assertNotIn("pushFavoritesHistoryEntry", body)

    def test_popstate_dispatches_all_views_without_push(self) -> None:
        start = self.source.index('window.addEventListener("popstate"')
        end = self.source.index("\n});", start) + len("\n});")
        body = self.source[start:end]
        self.assertIn("catalogHistoryEntry(event.state)", body)
        self.assertIn('entry.view === "folder"', body)
        self.assertIn('entry.view === "search"', body)
        self.assertIn('entry.view === "favorites"', body)
        self.assertIn("restoreSearchHistoryEntry(entry)", body)
        self.assertIn("restoreFavoritesHistoryEntry(entry)", body)
        self.assertNotIn("pushState", body)

    def test_scroll_uses_one_shared_debounced_history_handler(self) -> None:
        schedule = self._function_body("function scheduleCatalogHistoryScrollSync()")
        self.assertIn("syncCurrentCatalogHistorySnapshot()", schedule)
        self.assertIn("window.setTimeout", schedule)
        self.assertEqual(
            1,
            self.source.count(
                'window.addEventListener("scroll", scheduleCatalogHistoryScrollSync'
            ),
        )

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
