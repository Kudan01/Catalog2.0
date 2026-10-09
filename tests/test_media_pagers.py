from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "catalog_app" / "static" / "index.html"
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
STYLE_CSS = ROOT / "catalog_app" / "static" / "style.css"

OLD_MEDIA_PAGER_IDS = (
    "firstPage",
    "prevPage",
    "pageJumpForm",
    "pageJumpInput",
    "pageJumpButton",
    "nextPage",
    "lastPage",
)


class MediaPagerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLE_CSS.read_text(encoding="utf-8")

    def test_top_and_bottom_pagers_share_data_contract_without_duplicate_ids(self) -> None:
        self.assertEqual(2, self.html.count("data-media-pager "))
        self.assertIn('data-media-pager-position="top"', self.html)
        self.assertIn('data-media-pager-position="bottom"', self.html)
        for action in ("first", "previous", "next", "last"):
            self.assertEqual(2, self.html.count(f'data-media-page-action="{action}"'))
        self.assertEqual(2, self.html.count("data-media-page-jump-form"))
        self.assertEqual(2, self.html.count("data-media-page-jump-input"))

        ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)))

    def test_both_copies_use_the_same_aria_label(self) -> None:
        pager_tags = re.findall(r"<div [^>]*data-media-pager [^>]*>", self.html)
        self.assertEqual(2, len(pager_tags))
        for tag in pager_tags:
            self.assertIn('data-aria-label="sections.media"', tag)
        self.assertNotIn("data-media-page-label", self.html)

    def test_old_fixed_ids_are_removed(self) -> None:
        for old_id in OLD_MEDIA_PAGER_IDS:
            with self.subTest(old_id=old_id):
                self.assertNotIn(f'id="{old_id}"', self.html)
                self.assertNotIn(f"els.{old_id}", self.app)
                self.assertNotIn(f'getElementById("{old_id}")', self.app)

    def test_top_pager_sits_between_media_head_and_cards_with_controls_only(self) -> None:
        head = self.html.index('<div class="media-head">')
        top = self.html.index('data-media-pager-position="top"')
        media_list = self.html.index('id="mediaList"')
        bottom = self.html.index('data-media-pager-position="bottom"')
        self.assertLess(head, top)
        self.assertLess(top, media_list)
        self.assertLess(media_list, bottom)

        top_block = self.html[top:media_list]
        self.assertNotIn("pageInfo", top_block)
        self.assertNotIn("pagination.items", top_block)

    def test_both_pagers_share_state_update_and_navigation(self) -> None:
        self.assertIn(
            'mediaPagers: Array.from(document.querySelectorAll("[data-media-pager]"))',
            self.app,
        )
        sync = self._function_body("function syncMediaPagerControls(page, pages)")
        self.assertIn("for (const pager of els.mediaPagers)", sync)
        self.assertIn('controls.jumpInput.value = hasPages ? String(page) : ""', sync)

        update = self._function_body("function updateMediaPager(data)")
        self.assertIn("state.mediaPages = pages", update)
        self.assertIn("syncMediaPagerControls(page, pages)", update)
        self.assertIn("setMediaPagerVisible(pages)", update)

        navigation = self._function_body("async function goToMediaPage(page)")
        self.assertIn("state.mediaPage = targetPage", navigation)
        self.assertIn("syncMediaPagerControls(targetPage, state.mediaPages)", navigation)
        self.assertEqual(1, self.app.count("async function goToMediaPage(page)"))

        listeners_start = self.app.index("\nfor (const pager of els.mediaPagers) {")
        listeners = self.app[listeners_start:self.app.index("\n}\n", listeners_start)]
        self.assertIn('controls.first.addEventListener("click", () => goToMediaPage(1))', listeners)
        self.assertIn("goToMediaPage(controls.jumpInput.value)", listeners)
        self.assertIn("goToMediaPage(state.mediaPages)", listeners)

    def test_top_copy_looks_like_the_bottom_copy(self) -> None:
        pager_tags = re.findall(r"<div [^>]*data-media-pager [^>]*>", self.html)
        for tag in pager_tags:
            self.assertNotIn(" compact", tag)
        self.assertNotIn(".media-pager-top .page-jump input", self.css)

    def test_both_pagers_are_hidden_for_at_most_one_page(self) -> None:
        visibility = self._function_body("function setMediaPagerVisible(pages)")
        self.assertIn("for (const pager of els.mediaPagers)", visibility)
        self.assertIn("pager.hidden = pages <= 1", visibility)
        self.assertNotIn("mediaPagerPosition", visibility)
        self.assertIn("state.mediaPages = pages", self._function_body("function updateMediaPager(data)"))
        self.assertIn(".media-pager[hidden]", self.css)
        self.assertIn(".pager.media-pager-top", self.css)

    def _function_body(self, signature: str) -> str:
        start = self.app.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ", "\n// ")
            if (index := self.app.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(self.app)
        return self.app[start:end]


if __name__ == "__main__":
    unittest.main()
