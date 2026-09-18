from __future__ import annotations

import unittest
from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "catalog_app" / "static" / "app.js"


class SearchRenderDiagnosticsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS.read_text(encoding="utf-8")
        cls.render = cls._function_body("function renderSearchResults(data)")

    def test_search_render_starts_and_ends_its_own_diagnostic_operation(self) -> None:
        start = self.render.index(
            'const operation = diagnosticOperationStart("frontend.render.media"'
        )
        first_end = self.render.index(
            'diagnosticOperationEnd("frontend.render.media", operation'
        )
        self.assertLess(start, first_end)
        self.assertEqual(3, self.render.count(
            'diagnosticOperationEnd("frontend.render.media", operation'
        ))

    def test_search_render_uses_filtered_media_result_count(self) -> None:
        self.assertIn("count: mediaResults.length", self.render)
        self.assertIn("rendered: mediaResults.length", self.render)
        self.assertNotIn("data.media", self.render)

    def test_empty_media_branch_ends_diagnostics_before_return(self) -> None:
        branch_start = self.render.index("if (mediaResults.length === 0)")
        branch_end = self.render.index("\n  }", branch_start)
        empty_branch = self.render[branch_start:branch_end]
        self.assertIn(
            'diagnosticOperationEnd("frontend.render.media", operation',
            empty_branch,
        )
        self.assertLess(empty_branch.index("diagnosticOperationEnd"), empty_branch.index("return;"))

    @classmethod
    def _function_body(cls, signature: str) -> str:
        start = cls.source.index(signature)
        boundaries = [
            index
            for marker in ("\nfunction ", "\nasync function ")
            if (index := cls.source.find(marker, start + len(signature))) >= 0
        ]
        end = min(boundaries) if boundaries else len(cls.source)
        return cls.source[start:end]


if __name__ == "__main__":
    unittest.main()
