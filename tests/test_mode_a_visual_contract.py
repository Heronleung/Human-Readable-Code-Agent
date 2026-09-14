"""Qt/token contract checks for the review-only Mode A visual pass."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from hrca import visual_tokens
    HAS_TOKENS = True
except ImportError:
    HAS_TOKENS = False

try:
    from hrca import style
    HAS_STYLE = True
except ImportError:
    HAS_STYLE = False


@unittest.skipUnless(HAS_TOKENS, "review snapshot does not include the full package")
class VisualTokenTests(unittest.TestCase):
    def test_interactive_tokens_remain_restrained(self):
        self.assertEqual(visual_tokens.RADIUS_INTERACTIVE, 4)
        self.assertEqual(visual_tokens.FOCUS_RING_WIDTH, 2)
        self.assertEqual(visual_tokens.RADIUS_INTERACTIVE % visual_tokens.SPACE_4, 0)


@unittest.skipUnless(HAS_STYLE, "PySide6/full package is unavailable")
class StylesheetContractTests(unittest.TestCase):
    def test_semantic_variants_and_subtle_nav_exist_in_both_palettes(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                qss = style.build_stylesheet(palette)
                self.assertIn("QPushButton#secondaryButton", qss)
                self.assertIn("QPushButton#ghostButton", qss)
                self.assertIn("QPushButton#dangerButton", qss)
                self.assertIn("QFrame#documentEmptyState", qss)
                self.assertIn("QPlainTextEdit#documentEditor", qss)
                self.assertIn("border-left: 2px solid", qss)
                self.assertNotIn("gradient", qss.lower())

    def test_primary_and_body_contrast_contract_is_preserved(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.text, palette.window),
                    style.CONTRAST_BODY,
                )
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.on_accent, palette.accent),
                    style.CONTRAST_BODY,
                )


if __name__ == "__main__":
    unittest.main()
