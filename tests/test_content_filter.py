from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
INDEX_HTML = ROOT / "catalog_app" / "static" / "index.html"
I18N_ROOT = ROOT / "catalog_app" / "static" / "i18n"


class ContentFilterFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_tabs_expose_content_filters_in_requested_order(self) -> None:
        expected = ("all", "folders", "image", "gif", "video", "other")
        positions = [self.html.index(f'data-content-filter="{value}"') for value in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('data-aria-label="aria.mediaType"', self.html)
        self.assertIn('data-aria-label="aria.contentFilter"', self.html)

    def test_folder_filter_is_localized_in_czech_and_english(self) -> None:
        cs = json.loads((I18N_ROOT / "cs.json").read_text(encoding="utf-8"))
        en = json.loads((I18N_ROOT / "en.json").read_text(encoding="utf-8"))
        self.assertEqual("Složky", cs["filter.folders"])
        self.assertEqual("Folders", en["filter.folders"])
        self.assertEqual("Druh obsahu", cs["aria.contentFilter"])
        self.assertEqual("Content type", en["aria.contentFilter"])

    def test_folder_view_never_passes_folders_to_media_api(self) -> None:
        helper = self._function_body("function contentFilterMediaType(contentFilter)")
        loader = self._function_body("async function loadCurrentFolder(options = {})")
        self.assertIn('contentFilter === "folders" ? null : contentFilter', helper)
        self.assertIn("const loadMedia = snapshot.mediaType !== null", loader)
        self.assertIn("type: snapshot.mediaType", loader)
        self.assertNotIn("type: snapshot.contentFilter", loader)

    def test_folder_and_media_sections_follow_content_filter(self) -> None:
        loader = self._function_body("async function loadCurrentFolder(options = {})")
        self.assertIn(
            'snapshot.contentFilter === "all" || snapshot.contentFilter === "folders"',
            loader,
        )
        self.assertIn("resetAndHideMediaSection()", loader)
        self.assertIn("setChildFoldersSectionVisible(false)", loader)
        self.assertIn('snapshot.contentFilter === "folders"', loader)
        self.assertIn("resetChildFoldersCollapseUi()", loader)

    def test_search_folder_filter_uses_folder_pager_and_hides_media_section(self) -> None:
        search_loader = self._function_body("async function loadSearchView({ requestId, snapshot })")
        search_render = self._function_body("function renderSearchResults(data)")
        child_page = self._function_body("async function goToChildPage(page)")
        self.assertIn("filter: snapshot.contentFilter", search_loader)
        self.assertIn('snapshot.contentFilter === "folders" ? snapshot.childPage', search_loader)
        self.assertIn('const foldersOnly = data.type === "folders"', search_render)
        self.assertIn("updateChildPager(data)", search_render)
        self.assertIn("resetAndHideMediaSection()", search_render)
        self.assertIn('state.view === "search" && state.contentFilter === "folders"', child_page)

    def test_favorites_resets_and_hides_unsupported_folder_filter(self) -> None:
        favorites = self._function_body("async function openFavorites()")
        sync_tabs = self._function_body("function syncContentFilterTabs()")
        self.assertIn('state.contentFilter === "folders"', favorites)
        self.assertIn('setContentFilter("all")', favorites)
        self.assertIn('state.view === "favorites" && contentFilter === "folders"', sync_tabs)

    @classmethod
    def _function_body(cls, signature: str) -> str:
        start = cls.app.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := cls.app.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(cls.app)
        return cls.app[start:end]


if __name__ == "__main__":
    unittest.main()
