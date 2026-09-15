from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_app.api import folder_preview_build_tree_plan_page
from catalog_app.cli import build_parser
from catalog_app.folder_preview_candidates import (
    FOLDER_PREVIEW_REQUESTED_COUNT,
    FOLDER_PREVIEW_SELECTION_VARIANT,
)


ROOT = Path(__file__).resolve().parents[1]


class FolderPreviewProductionContractTests(unittest.TestCase):
    def test_cli_does_not_offer_count_or_variant(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()

        self.assertNotIn("--preview-count", help_text)
        self.assertNotIn("--variant", help_text)
        for option in ("--preview-count", "--variant"):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["folder-preview-build-tree", option, "2"])

    def test_production_contract_constants_are_six_and_zero(self) -> None:
        self.assertEqual(6, FOLDER_PREVIEW_REQUESTED_COUNT)
        self.assertEqual(0, FOLDER_PREVIEW_SELECTION_VARIANT)

        cli_source = (ROOT / "catalog_app" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("args.preview_count", cli_source)
        self.assertNotIn("args.variant", cli_source)
        self.assertGreaterEqual(cli_source.count("requested_count=FOLDER_PREVIEW_REQUESTED_COUNT"), 8)
        self.assertGreaterEqual(cli_source.count("variant=FOLDER_PREVIEW_SELECTION_VARIANT"), 8)

    def test_browser_plan_uses_fixed_contract(self) -> None:
        config = object()
        with patch(
            "catalog_app.api.build_folder_preview_tree_plan",
            side_effect=RuntimeError("stop after argument capture"),
        ) as builder:
            with self.assertRaisesRegex(RuntimeError, "argument capture"):
                folder_preview_build_tree_plan_page(config, "parent_folder")

        builder.assert_called_once_with(
            config,
            branch_rel_path="parent_folder",
            variant=FOLDER_PREVIEW_SELECTION_VARIANT,
            requested_count=FOLDER_PREVIEW_REQUESTED_COUNT,
        )

    def test_browser_jobs_use_fixed_contract(self) -> None:
        jobs_source = (ROOT / "catalog_app" / "jobs.py").read_text(encoding="utf-8")
        self.assertIn("variant=FOLDER_PREVIEW_SELECTION_VARIANT", jobs_source)
        self.assertIn("requested_count=FOLDER_PREVIEW_REQUESTED_COUNT", jobs_source)


if __name__ == "__main__":
    unittest.main()
