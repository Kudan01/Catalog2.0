from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "catalog_app" / "static"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"


class FolderLinkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLE_CSS.read_text(encoding="utf-8")

    def test_links_keep_in_page_navigation_for_a_plain_left_click_only(self) -> None:
        bind = self._function_body("function bindFolderLink(link, open)")
        self.assertIn('link.addEventListener("click", (event) => {', bind)
        for condition in (
            "event.button !== 0",
            "event.ctrlKey",
            "event.metaKey",
            "event.shiftKey",
            "event.altKey",
        ):
            self.assertIn(condition, bind)
        self.assertLess(bind.index("return;"), bind.index("event.preventDefault()"))
        self.assertIn("open();", bind)

        url = self._function_body("function folderLinkUrl(relPath)")
        self.assertIn("return folderHistoryUrl({", url)
        self.assertIn('contentFilter: "all"', url)

    def test_tree_items_are_links_except_disk_candidates(self) -> None:
        node = self._function_body("function folderTreeNode(folder, depth)")
        self.assertIn('document.createElement(isDiskCandidate ? "button" : "a")', node)
        self.assertIn("main.href = folderLinkUrl(folder.rel_path)", node)
        self.assertIn("bindFolderLink(main, () => openFolder(folder.rel_path))", node)
        self.assertIn('text("jobs.diskRootCandidateOpen")', node)

        self.assertIn('main.href = folderLinkUrl("");', self.app)
        self.assertIn('bindFolderLink(main, () => openFolder(""))', self.app)

    def test_child_folder_card_link_keeps_the_return_anchor(self) -> None:
        render = self._function_body("function renderChildFolders(data)")
        self.assertIn('<a class="folder-card-link" href="${escapeHtml(folderLinkUrl(folder.rel_path))}"', render)
        self.assertIn('aria-label="${escapeHtml(folder.name)}"', render)
        self.assertIn('const openFromCard = () => openFolder(folder.rel_path, {', render)
        self.assertIn('bindFolderLink(card.querySelector(".folder-card-link"), openFromCard)', render)
        self.assertIn("sourceEntryAnchor: folder.rel_path", render)
        self.assertNotIn('card.addEventListener("click"', render)

    def test_breadcrumb_links_previous_items_and_keeps_the_current_one_disabled(self) -> None:
        render = self._function_body("function renderFolder(folder, breadcrumb)")
        self.assertIn('const link = document.createElement("a")', render)
        self.assertIn("link.href = folderLinkUrl(item.rel_path)", render)
        self.assertIn("bindFolderLink(link, () => openFolder(item.rel_path, {", render)
        self.assertIn("targetEntryAnchor,", render)
        self.assertIn("markBreadcrumbCurrent(current)", render)

    def test_link_styles_keep_the_previous_look(self) -> None:
        overlay = self._css_rule(".folder-card-link {")
        self.assertIn("position: absolute", overlay)
        self.assertIn("inset: 0", overlay)
        self.assertIn("position: relative", self._css_rule(".folder-card {"))
        raised = self._css_rule(".folder-card-actions,\n.folder-card .folder-rename-icon,\n.folder-card-summary {")
        self.assertIn("z-index: 1", raised)

        tree = self._css_rule(".tree-node-main {")
        for declaration in ("display: block", "color: var(--text)", "font: inherit", "text-decoration: none"):
            self.assertIn(declaration, tree)

        breadcrumb = self._css_rule(":root .content .breadcrumb button,\n:root .content .breadcrumb a {")
        self.assertIn("color: var(--muted)", breadcrumb)
        self.assertIn("text-decoration: none", breadcrumb)
        self.assertIn(":root .content .breadcrumb a:hover {", self.css)
        for rule in (overlay, tree, breadcrumb):
            self.assertNotIn("#", rule)
            self.assertNotIn("rgb", rule)

    def test_folder_not_found_message_exists_in_both_locales(self) -> None:
        for code in ("cs", "en"):
            locale = json.loads((STATIC / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
            self.assertIn("{path}", locale["navigation.folderNotFound"])

    def _css_rule(self, selector: str) -> str:
        start = self.css.index(selector)
        return self.css[start:self.css.index("}", start)]

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
