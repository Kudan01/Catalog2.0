from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "catalog_app" / "static" / "index.html"
APP_JS = ROOT / "catalog_app" / "static" / "app.js"
STYLE_CSS = ROOT / "catalog_app" / "static" / "style.css"


class ChildFolderPagerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLE_CSS.read_text(encoding="utf-8")

    def test_top_and_bottom_pagers_have_shared_data_contract_without_duplicate_ids(self) -> None:
        self.assertEqual(2, self.html.count("data-child-pager "))
        self.assertIn('data-child-pager-position="top"', self.html)
        self.assertIn('data-child-pager-position="bottom"', self.html)
        for action in ("first", "previous", "next", "last"):
            self.assertEqual(2, self.html.count(f'data-child-page-action="{action}"'))
        self.assertEqual(2, self.html.count("data-child-page-jump-form"))
        self.assertEqual(2, self.html.count("data-child-page-jump-input"))

        ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(len(ids), len(set(ids)))

    def test_bottom_pager_contains_controls_only(self) -> None:
        start = self.html.index('data-child-pager-position="bottom"')
        end = self.html.index("</section>", start)
        bottom = self.html[start:end]
        self.assertNotIn("childPageInfo", bottom)
        self.assertNotIn("pagination.folders", bottom)

    def test_both_pagers_share_state_update_and_navigation(self) -> None:
        self.assertIn(
            'childPagers: Array.from(document.querySelectorAll("[data-child-pager]"))',
            self.app,
        )
        sync = self._function_body("function syncChildPagerControls(page, pages)")
        self.assertIn("for (const pager of els.childPagers)", sync)
        self.assertIn('controls.jumpInput.value = hasPages ? String(page) : ""', sync)

        navigation = self._function_body("async function goToChildPage(page)")
        self.assertIn("state.childPages", navigation)
        self.assertIn("state.childPage = targetPage", navigation)
        self.assertIn("syncChildPagerControls(targetPage, state.childPages)", navigation)
        self.assertEqual(1, self.app.count("async function goToChildPage(page)"))

        self.assertIn("for (const pager of els.childPagers)", self.app)
        self.assertIn('controls.first.addEventListener("click", () => goToChildPage(1))', self.app)
        self.assertIn(
            'controls.jumpForm.addEventListener("submit", (event) => {',
            self.app,
        )

    def test_bottom_visibility_follows_cards_and_collapse_state(self) -> None:
        visibility = self._function_body("function setChildPagerVisible(visible)")
        self.assertIn('pager.dataset.childPagerPosition === "bottom"', visibility)
        self.assertIn("state.childPages <= 1", visibility)
        self.assertIn("areChildFoldersCollapsed()", visibility)
        self.assertIn("els.childFolders.children.length === 0", visibility)

        collapse = self._function_body("function setChildFoldersExpandedInDom(expanded)")
        self.assertIn("setChildPagerVisible(state.childPages > 0)", collapse)
        self.assertIn(".folder-pager-bottom", self.css)
        self.assertIn(".folder-pager[hidden]", self.css)

    def _function_body(self, signature: str) -> str:
        start = self.app.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := self.app.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(self.app)
        return self.app[start:end]


if __name__ == "__main__":
    unittest.main()
