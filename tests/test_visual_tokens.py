"""Tests for the shared visual-token contract (P4.2a v10).

Proves that :mod:`hrca.visual_tokens` is a pure, Qt-free/Win32-free single
source of truth, that :mod:`hrca.style` and :mod:`hrca.credential_sheet_win`
consume it instead of duplicating palette/spacing/control literals, and that
the pure scaling and colour helpers behave correctly.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

from hrca.core import visual_tokens

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))


def _read(name: str) -> str:
    with open(_hrca_module(name), "r", encoding="utf-8") as fh:
        return fh.read()


_HRCA_ROOT = _SRC

def _hrca_module(name):
    """Return the path of an ``hrca`` module wherever it now lives.

    The package is organised by responsibility, so a module is no longer a
    fixed number of directories below ``src``; it is resolved by name. A
    compatibility shim is skipped in favour of the implementation it aliases,
    because these tests are about what a module *does* and a shim does
    nothing but point at another module.
    """
    stem = name[:-3] if name.endswith(".py") else name
    matches = []
    for dirpath, dirnames, filenames in os.walk(_HRCA_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if stem + ".py" in filenames:
            matches.append(os.path.join(dirpath, stem + ".py"))
        # A responsibility package's front door is its ``__init__``, so a name
        # that used to be a module may now be a package.
        if os.path.basename(dirpath) == stem and "__init__.py" in filenames:
            matches.append(os.path.join(dirpath, "__init__.py"))
    for path in sorted(matches, key=len, reverse=True):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        alias = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not node.targets:
                continue
            t = node.targets[0]
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Attribute)
                    and t.value.attr == "modules"
                    and isinstance(t.value.value, ast.Name)
                    and t.value.value.id == "sys"
                    and isinstance(t.slice, ast.Name)
                    and t.slice.id == "__name__"):
                alias = True
        if not alias:
            return path
    if matches:
        return matches[0]
    raise AssertionError("no module named %r in the hrca package" % stem)


class TokenPurityTests(unittest.TestCase):
    def test_visual_tokens_imports_no_qt_or_win32(self):
        source = _read("visual_tokens.py")
        for token in ("PySide", "PyQt", "ctypes", "win32", "wincred"):
            self.assertNotIn(token, source)

    def test_color_tokens_are_hex(self):
        for scheme in ("light", "dark"):
            for key, value in visual_tokens.colors(scheme).items():
                self.assertRegex(value, r"^#[0-9a-f]{6}$", f"{scheme}:{key}")


class ScaleTests(unittest.TestCase):
    def test_scale_is_linear_and_rounds_once(self):
        self.assertEqual(visual_tokens.scale(24, 96), 24)
        self.assertEqual(visual_tokens.scale(24, 120), 30)   # 125%
        self.assertEqual(visual_tokens.scale(24, 144), 36)   # 150%
        self.assertEqual(visual_tokens.scale(24, 192), 48)   # 200%

    def test_scale_zero_stays_zero(self):
        self.assertEqual(visual_tokens.scale(0, 192), 0)

    def test_scale_invalid_dpi_treated_as_96(self):
        self.assertEqual(visual_tokens.scale(24, 0), 24)
        self.assertEqual(visual_tokens.scale(24, -5), 24)

    def test_scale_never_zero_for_nonzero_value(self):
        self.assertGreater(visual_tokens.scale(1, 96), 0)


class ColorRefTests(unittest.TestCase):
    def test_colorref_is_bgr(self):
        self.assertEqual(
            visual_tokens.colorref("#010203"), (3 << 16) | (2 << 8) | 1
        )

    def test_colorref_round_trips_palette(self):
        for scheme in ("light", "dark"):
            for value in visual_tokens.colors(scheme).values():
                self.assertGreater(visual_tokens.colorref(value), 0)


class AdapterParityTests(unittest.TestCase):
    """Both adapters consume the same token source — no duplicated literals."""

    def test_style_palette_equals_tokens(self):
        from hrca.ui import style

        for scheme, palette in (
            ("light", style.LIGHT_PALETTE),
            ("dark", style.DARK_PALETTE),
        ):
            colors = visual_tokens.colors(scheme)
            for key, value in colors.items():
                self.assertEqual(getattr(palette, key), value, f"{scheme}:{key}")

    def test_style_spacing_radii_typography_equal_tokens(self):
        from hrca.ui import style

        self.assertEqual(style.SPACE_8, visual_tokens.SPACE_8)
        self.assertEqual(style.INSET, visual_tokens.INSET)
        self.assertEqual(style.GAP_TIGHT, visual_tokens.GAP_TIGHT)
        self.assertEqual(style.RADIUS_CONTAINER, visual_tokens.RADIUS_CONTAINER)
        self.assertEqual(style.RADIUS_CHIP, visual_tokens.RADIUS_CHIP)
        self.assertEqual(style.UI_FONT_SIZE, visual_tokens.FONT_BODY)
        self.assertEqual(style.STATUS_FONT_SIZE, visual_tokens.FONT_STATUS)
        self.assertEqual(style.PANEL_HEADER_FONT_SIZE, visual_tokens.FONT_MICRO_HEADING)

    def test_native_sheet_has_no_duplicate_palette_literals(self):
        # The Win32 adapter must not carry its own hex palette literals.
        source = _read("credential_sheet_win.py")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}", source))

    def test_qt_factory_has_no_duplicate_palette_literals_outside_tokens(self):
        # The Qt adapter draws its palette from visual_tokens; the only hex
        # colour literals in style.py live in the token module, not style.py.
        source = _read("style.py")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}", source))


class SheetReferenceTests(unittest.TestCase):
    """The token contract encodes the pre-v10 sheet as the visual reference."""

    def test_dark_palette_matches_pre_v10_sheet(self):
        # These are the exact application-owned colors of the v9 sheet at 9285b90.
        self.assertEqual(visual_tokens.DARK_COLORS["window"], "#1e1f22")
        self.assertEqual(visual_tokens.DARK_COLORS["surface"], "#26272b")
        self.assertEqual(visual_tokens.DARK_COLORS["text"], "#dcddde")
        self.assertEqual(visual_tokens.DARK_COLORS["text_secondary"], "#9aa0a6")
        self.assertEqual(visual_tokens.DARK_COLORS["accent"], "#ffffff")
        self.assertEqual(visual_tokens.DARK_COLORS["on_accent"], "#1f2328")

    def test_native_sheet_uses_shared_body_font(self):
        from hrca.integrations import credential_sheet_win

        self.assertEqual(credential_sheet_win._SHEET_FONT_SIZE, visual_tokens.FONT_BODY)

    def test_sheet_text_is_one_step_above_pre_v10_default(self):
        # The pre-v10 sheet used the OS default GUI font (~9pt / 12px at 96 DPI);
        # the shared body size is one restrained semantic step larger (+~1px).
        self.assertEqual(visual_tokens.FONT_BODY, 13)
        self.assertGreater(visual_tokens.FONT_BODY, 12)
        # Secondary text stays subordinate (a step below the field/button size).
        self.assertLess(visual_tokens.FONT_STATUS, visual_tokens.FONT_BODY)


if __name__ == "__main__":
    unittest.main()
