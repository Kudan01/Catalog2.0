from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "catalog_app" / "static"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"
LOCALES = {code: STATIC / "i18n" / f"{code}.json" for code in ("cs", "en")}

PAGER_SYMBOLS = {"first": "«", "previous": "‹", "next": "›", "last": "»"}


class FolderPageSimplificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX_HTML.read_text(encoding="utf-8")
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLE_CSS.read_text(encoding="utf-8")
        cls.locales = {
            code: json.loads(path.read_text(encoding="utf-8"))
            for code, path in LOCALES.items()
        }

    def _pager_blocks(self) -> list[tuple[str, str]]:
        blocks = []
        for match in re.finditer(r"<div [^>]*data-(child|media)-pager [^>]*>", self.html):
            end = self.html.index("</div>", match.end())
            blocks.append((match.group(1), self.html[match.start():end]))
        return blocks

    def test_every_pager_uses_symbols_and_a_jump_field_without_label_or_button(self) -> None:
        blocks = self._pager_blocks()
        self.assertEqual(4, len(blocks))
        for kind, block in blocks:
            with self.subTest(kind=kind, block=block[:80]):
                for action, symbol in PAGER_SYMBOLS.items():
                    button = re.search(
                        rf'<button [^>]*data-{kind}-page-action="{action}"[^>]*>([^<]*)</button>',
                        block,
                    )
                    self.assertIsNotNone(button)
                    self.assertEqual(symbol, button.group(1))
                    self.assertIn(f'data-title="actions.{action}"', button.group(0))
                    self.assertIn(f'data-aria-label="actions.{action}"', button.group(0))
                    self.assertNotIn("data-text=", button.group(0))
                self.assertNotIn("<label", block)
                self.assertNotIn("actions.go", block)
                self.assertNotIn('type="submit"', block)
                self.assertEqual(1, block.count(f"data-{kind}-page-jump-input"))
                self.assertIn('data-aria-label="pagination.jumpLabel"', block)
                self.assertEqual(1, block.count(f"data-{kind}-page-total"))

    def test_pager_sync_shows_the_total_page_count(self) -> None:
        for kind in ("Child", "Media"):
            sync = self._function_body(f"function sync{kind}PagerControls(page, pages)")
            self.assertIn('controls.total.textContent = hasPages ? `/ ${pages}` : ""', sync)
        self.assertIn('total: pager.querySelector("[data-child-page-total]")', self.app)
        self.assertIn('total: pager.querySelector("[data-media-page-total]")', self.app)

    def test_count_texts_contain_only_the_total(self) -> None:
        for code, locale in self.locales.items():
            with self.subTest(locale=code):
                for key in ("pagination.folders", "pagination.items"):
                    self.assertEqual({"total"}, set(re.findall(r"\{(\w+)\}", locale[key])))
                self.assertNotIn("count.directLabel", locale)
                self.assertNotIn("count.directSuffix", locale)
                self.assertIn("count.recursiveLabel", locale)
                self.assertIn("count.recursiveSuffix", locale)

    def test_folder_header_uses_one_summary_line(self) -> None:
        render = self._function_body("function renderFolder(folder, breadcrumb)")
        self.assertNotIn("count.directLabel", render)
        self.assertNotIn("count.recursiveLabel", render)
        self.assertIn("folderPrimarySummary(folder)", render)
        self.assertIn("folder-runtime-status", render)

        summary = self._function_body("function folderPrimarySummary(folder)")
        self.assertIn("localizedFolderCount(r.folders)", summary)
        self.assertIn('text("folderCard.primaryEmpty")', summary)

        self.assertNotIn("directCountText", self.app)
        self.assertIn("function recursiveCountText(folder)", self.app)

    def test_folder_cards_keep_only_the_insight_graphic(self) -> None:
        regular = self._function_body("function renderChildFolders(data)")
        search = self._function_body("function folderResultCard(folder)")
        for card in (regular, search):
            self.assertNotIn("folder-card-primary-meta", card)
            self.assertNotIn("folder-card-detail-meta", card)
            self.assertNotIn("count.directLabel", card)
            self.assertNotIn("count.recursiveLabel", card)
            self.assertIn("folderInsightMarkup(folder)", card)
        self.assertIn('text("meta.unavailable")', search)
        self.assertNotIn(".folder-card-primary-meta", self.css)
        self.assertNotIn(".folder-card-detail-meta", self.css)

    def test_breadcrumb_is_separated_text_with_a_current_item(self) -> None:
        append = self._function_body("function appendBreadcrumbItem(button)")
        self.assertIn('separator.className = "breadcrumb-separator"', append)
        self.assertIn('separator.setAttribute("aria-hidden", "true")', append)
        self.assertIn('separator.textContent = "›"', append)

        current = self._function_body("function markBreadcrumbCurrent(button)")
        self.assertIn("button.disabled = true", current)
        self.assertIn('button.setAttribute("aria-current", "page")', current)

        for signature in (
            "function renderFolder(folder, breadcrumb)",
            "function renderFavoritesHeader(data)",
            "function renderSearchHeader(data)",
        ):
            with self.subTest(renderer=signature):
                body = self._function_body(signature)
                self.assertIn("appendBreadcrumbItem(", body)
                self.assertIn("markBreadcrumbCurrent(", body)
        self.assertEqual(2, self.app.count("els.breadcrumb.appendChild("))

    def test_section_heads_and_media_separator(self) -> None:
        self.assertIn('<section class="media-section">', self.html)
        media_head = self.html[
            self.html.index('<div class="media-head">'):self.html.index('data-media-pager-position="top"')
        ]
        self.assertIn('id="mediaTitle"', media_head)
        self.assertIn('id="pageInfo"', media_head)
        self.assertIn("section:not([hidden]) + .media-section", self.css)

    def test_tabs_and_breadcrumb_use_theme_variables_only(self) -> None:
        self.assertIsNone(re.search(r'\[data-theme="[a-z-]+"\] \.tab\.active', self.css))
        for selector in (
            ":root .tabs .tab {",
            ":root .tabs .tab:hover {",
            ":root .tabs .tab.active {",
            ":root .content .breadcrumb button {",
            ":root .content .breadcrumb button:hover {",
            ":root .content .breadcrumb button:disabled {",
            ".breadcrumb-separator {",
            "section:not([hidden]) + .media-section {",
            ".page-jump-total {",
        ):
            with self.subTest(selector=selector):
                start = self.css.index(selector)
                rule = self.css[start:self.css.index("}", start)]
                self.assertNotIn("#", rule)
                self.assertNotIn("rgb", rule)
        active = self.css[self.css.index(":root .tabs .tab.active {"):]
        self.assertIn("border-bottom-color: var(--accent)", active[:active.index("}")])

    def test_jump_field_hides_number_spinners(self) -> None:
        self.assertIn(".page-jump input::-webkit-inner-spin-button", self.css)
        self.assertIn(".page-jump input::-webkit-outer-spin-button", self.css)
        self.assertIn("appearance: textfield", self.css)

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
