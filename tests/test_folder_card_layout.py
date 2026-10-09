from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "catalog_app" / "static"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style.css"


class FolderCardLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLE_CSS.read_text(encoding="utf-8")

    def test_both_cards_use_the_header_summary_and_an_empty_preview_strip(self) -> None:
        regular = self._function_body("function renderChildFolders(data)")
        search = self._function_body("function folderResultCard(folder)")
        for card in (regular, search):
            self.assertIn('<div class="folder-card-header">', card)
            self.assertIn('<div class="folder-card-heading">', card)
            self.assertIn("folderCardSummaryMarkup(folder)", card)
            self.assertIn('<div class="folder-card-actions"></div>', card)
            self.assertIn("${previewHtml}", card)
            self.assertIn("card._folderPreviews =", card)
        self.assertIn('<div class="row-path">', search)
        self.assertIn('text("meta.unavailable")', search)

        self.assertIn('<a class="folder-card-link"', regular)
        self.assertIn("card.dataset.folderPath = folder.rel_path", regular)
        self.assertIn("sourceEntryAnchor: folder.rel_path", regular)
        self.assertIn('bindFolderLink(card.querySelector(".folder-card-summary"), openFromCard)', regular)

        strip = self._function_body("function folderPreviewMarkup(folder)")
        self.assertIn('<div class="folder-preview-strip"', strip)
        self.assertNotIn("<img", strip)

    def test_summary_shows_totals_ratio_and_exact_counts_in_a_tooltip(self) -> None:
        summary = self._function_body("function folderCardSummaryMarkup(folder)")
        self.assertIn('"count.media.one", "count.media.few", "count.media.many"', summary)
        self.assertIn("localizedFolderCount(r.folders)", summary)
        self.assertIn('text("folderCard.primaryEmpty")', summary)
        self.assertIn("localizedMediaCount(count, type)", summary)
        self.assertIn('title="${escapeHtml(tooltip)}"', summary)
        self.assertIn("(count / mediaTotal) * 100", summary)
        self.assertIn("folder-card-ratio-${type}", summary)

    def test_preview_count_follows_the_card_width(self) -> None:
        slots = self._function_body("function folderPreviewSlotCount(strip)")
        self.assertIn('style.getPropertyValue("--folder-preview-width")', slots)
        self.assertIn("Math.floor((strip.clientWidth + gap) / (thumbWidth + gap))", slots)

        update = self._function_body("function updateFolderPreviewStrips()")
        self.assertIn("strips.find(strip => strip.clientWidth > 0)", update)
        self.assertEqual(1, update.count("folderPreviewSlotCount("))
        self.assertIn('strip.closest(".folder-card")?._folderPreviews', update)
        self.assertIn("for (let index = strip.children.length; index < shown; index += 1)", update)
        self.assertIn("strip.appendChild(folderPreviewThumb(previews[index]))", update)
        self.assertIn("thumb.hidden = index >= shown", update)
        self.assertNotIn(".remove()", update)

        thumb = self._function_body("function folderPreviewThumb(preview)")
        self.assertLess(thumb.index('image.loading = "lazy"'), thumb.index("image.src ="))
        self.assertIn("bindFolderPreviewImageErrors(box)", thumb)

    def test_strips_update_after_render_and_on_resize(self) -> None:
        for signature in (
            "function renderChildFolders(data)",
            "function renderSearchResults(data)",
            "function renderFavoritesResults(data)",
        ):
            with self.subTest(renderer=signature):
                self.assertIn("updateFolderPreviewStrips();", self._function_body(signature))
        schedule = self._function_body("function scheduleFolderPreviewStripUpdate()")
        self.assertIn("window.requestAnimationFrame(", schedule)
        self.assertIn(
            "new ResizeObserver(scheduleFolderPreviewStripUpdate).observe(els.childFolders)",
            self.app,
        )

    def test_wide_compact_card_switching_and_statistics_panel_are_removed(self) -> None:
        for removed in (
            "folderCardWideRequiredWidth",
            "folderCardWideLayoutFits",
            "folderInsightMarkup",
            "folderInsightBar",
        ):
            self.assertNotIn(removed, self.app)
        mode = self._function_body("function updateResponsiveLayoutMode()")
        self.assertIn("if (contentAreaNeedsCompact()) {", mode)

    def test_breadcrumb_is_hidden_on_the_root_page_only(self) -> None:
        folder = self._function_body("function renderFolder(folder, breadcrumb)")
        self.assertIn("els.breadcrumb.hidden = breadcrumb.length <= 1", folder)
        for signature in ("function renderFavoritesHeader(data)", "function renderSearchHeader(data)"):
            with self.subTest(header=signature):
                self.assertIn("els.breadcrumb.hidden = false", self._function_body(signature))
        self.assertIn(".breadcrumb[hidden]", self.css)

    def test_card_styles_use_theme_variables_and_fixed_preview_size(self) -> None:
        strip = self._css_rule(".folder-preview-strip {")
        self.assertIn("--folder-preview-width: 160px", strip)
        self.assertIn("flex-wrap: nowrap", strip)
        self.assertIn("overflow: hidden", strip)
        thumb = self._css_rule(".folder-preview-thumb {")
        self.assertIn("aspect-ratio: 2 / 3", thumb)
        self.assertIn("width: var(--folder-preview-width)", thumb)

        raised = self._css_rule(".folder-card-actions,\n.folder-card .folder-rename-icon,\n.folder-card-summary {")
        self.assertIn("z-index: 1", raised)

        self.assertNotIn(".folder-insight", self.css)
        self.assertNotIn(".folder-card-insights", self.css)
        self.assertNotIn(".folder-card-layout", self.css)
        for selector in (
            ".folder-preview-strip {",
            ".folder-preview-thumb {",
            ".folder-card-header {",
            ".folder-card-summary {",
            ".folder-card-ratio {",
            ".folder-card-ratio-image {",
            ".folder-card-ratio-gif {",
            ".folder-card-ratio-video {",
            ".folder-card-ratio-other {",
        ):
            with self.subTest(selector=selector):
                # Fixed colors only; color-mix(in srgb, var(--accent) ...) is allowed.
                rule = self._css_rule(selector)
                self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", rule))
                self.assertIsNone(re.search(r"\b(?:rgba?|hsla?)\(", rule))
        self.assertIsNone(re.search(r"\[data-theme=\"[a-z-]+\"\] \.folder-card-ratio", self.css))

    def _css_rule(self, selector: str) -> str:
        start = self.css.index(selector)
        return self.css[start:self.css.index("}", start)]

    def _function_body(self, signature: str) -> str:
        start = self.app.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ", "\n// ", "\nlet ", "\nif (")
            if (index := self.app.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(self.app)
        return self.app[start:end]


if __name__ == "__main__":
    unittest.main()
