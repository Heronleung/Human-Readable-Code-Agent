"""Tests for the P3.2 visual design system (:mod:`hrca.style`).

Covered here: deterministic colour-scheme detection, the WCAG 4.5:1 contrast
contract for body / syntax / chip text in both palettes, the chip background
compositing helper, the component style-sheet factories, and the Twin body
selector. These tests import PySide6 and are skipped when it is not installed.
"""

from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleOption

    from hrca import style

    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - exercised in the no-Qt environment
    HAS_PYSIDE6 = False


def _app() -> "QApplication":
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------------------------------------------------------------------------
# Stand-ins for QGuiApplication so colour-scheme detection can be driven
# deterministically without depending on the operating-system appearance.
# ---------------------------------------------------------------------------
class _FakeColor:
    def __init__(self, lightness: int) -> None:
        self._lightness = lightness

    def lightness(self) -> int:
        return self._lightness


class _FakeWindow:
    def __init__(self, lightness: int) -> None:
        self._lightness = lightness

    def color(self) -> _FakeColor:
        return _FakeColor(self._lightness)


class _FakePalette:
    def __init__(self, lightness: int) -> None:
        self._lightness = lightness

    def window(self) -> _FakeWindow:
        return _FakeWindow(self._lightness)


class _FakeStyleHints:
    def __init__(self, scheme) -> None:
        self._scheme = scheme

    def colorScheme(self):
        return self._scheme


class _FakeApp:
    """Expose ``styleHints().colorScheme()`` and a fixed palette lightness."""

    def __init__(self, scheme, lightness: int = 255) -> None:
        self._scheme = scheme
        self._lightness = lightness

    def styleHints(self) -> _FakeStyleHints:
        return _FakeStyleHints(self._scheme)

    def palette(self) -> _FakePalette:
        return _FakePalette(self._lightness)


class _FakeAppNoHints:
    """A Qt-like app with no ``styleHints`` attribute at all (Qt < 6.5)."""

    def __init__(self, lightness: int = 255) -> None:
        self._lightness = lightness

    def palette(self) -> _FakePalette:
        return _FakePalette(self._lightness)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ThemeDetectionTests(unittest.TestCase):
    def test_dark_scheme_hint(self):
        self.assertEqual(
            style.detect_color_scheme(_FakeApp(Qt.ColorScheme.Dark)), "dark"
        )

    def test_light_scheme_hint(self):
        self.assertEqual(
            style.detect_color_scheme(_FakeApp(Qt.ColorScheme.Light)), "light"
        )

    def test_unknown_scheme_falls_back_to_light(self):
        self.assertEqual(
            style.detect_color_scheme(_FakeApp(Qt.ColorScheme.Unknown, lightness=240)),
            "light",
        )

    def test_unknown_scheme_falls_back_to_dark(self):
        self.assertEqual(
            style.detect_color_scheme(_FakeApp(Qt.ColorScheme.Unknown, lightness=32)),
            "dark",
        )

    def test_missing_hints_falls_back_to_palette(self):
        self.assertEqual(style.detect_color_scheme(_FakeAppNoHints(lightness=32)), "dark")

    def test_detection_is_deterministic_with_real_app(self):
        _app()
        self.assertEqual(style.detect_color_scheme(), style.detect_color_scheme())
        self.assertIn(style.detect_color_scheme(), ("dark", "light"))


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ContrastTests(unittest.TestCase):
    def test_body_text_meets_wcag(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.text, palette.window),
                    style.CONTRAST_BODY,
                )
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.text, palette.surface),
                    style.CONTRAST_BODY,
                )

    def test_syntax_text_meets_wcag_on_code_background(self):
        syntax = ("syntax_keyword", "syntax_string", "syntax_comment", "syntax_number")
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for token in syntax:
                with self.subTest(palette=palette.name, token=token):
                    self.assertGreaterEqual(
                        style.contrast_ratio(getattr(palette, token), palette.sunken),
                        style.CONTRAST_BODY,
                    )

    def test_chip_text_meets_wcag(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for state, token in style.TWIN_STATE_TOKEN.items():
                with self.subTest(palette=palette.name, state=state):
                    fg = palette.state_fg(token)
                    bg = style.chip_rendered_background(palette, token)
                    self.assertGreaterEqual(
                        style.contrast_ratio(fg, bg), style.CONTRAST_BODY
                    )


# ---------------------------------------------------------------------------
# Monochrome audit: the rework removed blue as the default accent, so no
# ordinary UI token (buttons / tabs / selection / focus / borders / hover)
# may be blue-dominant. Semantic success/warning/error hues and the syntax
# palette are code/state colours and are deliberately excluded.
# ---------------------------------------------------------------------------
_BLUE_DOMINANCE_LIMIT = 24
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\b")

# Palette fields that are ordinary UI chrome and must stay non-blue.
_MONOCHROME_TOKENS = (
    "window",
    "surface",
    "sunken",
    "border",
    "text",
    "text_secondary",
    "text_disabled",
    "accent",
    "accent_hover",
    "accent_pressed",
    "focus",
    "on_accent",
    "info",
    "neutral",
)


def _blue_dominance(hex_color: str) -> int:
    """Return blue minus max(red, green); positive means blue dominates."""
    color = QColor(hex_color)
    return color.blue() - max(color.red(), color.green())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class MonochromeAuditTests(unittest.TestCase):
    def test_ordinary_ui_tokens_are_not_blue_dominant(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for token in _MONOCHROME_TOKENS:
                with self.subTest(palette=palette.name, token=token):
                    value = getattr(palette, token)
                    self.assertLess(
                        _blue_dominance(value),
                        _BLUE_DOMINANCE_LIMIT,
                        f"{token}={value} is blue-dominant",
                    )

    def test_stylesheet_has_no_blue_dominant_color(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                qss = style.build_stylesheet(palette)
                for match in _HEX_COLOR_RE.finditer(qss):
                    color = match.group(0)
                    self.assertLess(
                        _blue_dominance(color),
                        _BLUE_DOMINANCE_LIMIT,
                        f"{color} in the {palette.name} stylesheet is blue-dominant",
                    )

    def test_semantic_state_colors_preserve_their_hue(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                success = QColor(palette.success)
                warning = QColor(palette.warning)
                error = QColor(palette.error)
                # success is green, warning is orange/amber, error is red.
                self.assertGreater(success.green(), success.red())
                self.assertGreater(success.green(), success.blue())
                self.assertGreater(warning.red(), warning.blue())
                self.assertGreater(error.red(), error.green())
                self.assertGreater(error.red(), error.blue())

    def test_focus_indicator_meets_wcag_non_text_contrast(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.focus, palette.surface),
                    style.CONTRAST_LARGE,
                )

    def test_accent_meets_wcag_against_surface(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.accent, palette.surface),
                    style.CONTRAST_LARGE,
                )

    def test_primary_button_text_meets_wcag(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                self.assertGreaterEqual(
                    style.contrast_ratio(palette.on_accent, palette.accent),
                    style.CONTRAST_BODY,
                )


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class StyleFactoryTests(unittest.TestCase):
    def test_secondary_text_style(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            self.assertEqual(
                style.secondary_text_style(palette),
                f"color: {palette.text_secondary};",
            )

    def test_status_label_style(self):
        palette = style.LIGHT_PALETTE
        self.assertEqual(
            style.status_label_style(palette),
            f"color: {palette.text}; font-size: {style.STATUS_FONT_SIZE}px;",
        )

    def test_status_field_style(self):
        palette = style.LIGHT_PALETTE
        self.assertEqual(
            style.status_field_style(palette),
            f"color: {palette.text_secondary}; font-size: {style.STATUS_FONT_SIZE}px;",
        )

    def test_project_root_label_style(self):
        palette = style.LIGHT_PALETTE
        self.assertEqual(
            style.project_root_label_style(palette),
            f"color: {palette.text_secondary}; "
            f"padding: {style.GAP_TIGHT}px {style.INSET}px;",
        )

    def test_twin_chip_style_uses_state_token(self):
        palette = style.LIGHT_PALETTE
        for state, token in style.TWIN_STATE_TOKEN.items():
            qss = style.twin_chip_style(palette, state)
            self.assertIn(palette.state_fg(token), qss)
            self.assertIn(palette.state_bg(token), qss)
            self.assertIn(f"border-radius: {style.RADIUS_CHIP}px", qss)
            self.assertIn(f"font-size: {style.STATUS_FONT_SIZE}px", qss)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class StylesheetTests(unittest.TestCase):
    def test_twin_body_selector_targets_label(self):
        qss = style.build_stylesheet(style.LIGHT_PALETTE)
        self.assertIn("QLabel#twinBody", qss)
        self.assertNotIn("QPlainTextEdit#twinBody", qss)

    def test_stylesheet_has_no_unresolved_tokens(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                self.assertNotIn("$", style.build_stylesheet(palette))

    def test_stylesheet_is_deterministic(self):
        self.assertEqual(
            style.build_stylesheet(style.LIGHT_PALETTE),
            style.build_stylesheet(style.LIGHT_PALETTE),
        )

    def test_chip_rendered_background_is_opaque_hex(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for token in (
                style.STATE_INFO,
                style.STATE_SUCCESS,
                style.STATE_WARNING,
                style.STATE_ERROR,
                style.STATE_NEUTRAL,
            ):
                with self.subTest(palette=palette.name, token=token):
                    self.assertRegex(
                        style.chip_rendered_background(palette, token),
                        r"^#[0-9a-f]{6}$",
                    )


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class TreeStyleTests(unittest.TestCase):
    def test_disclosure_slot_and_chevron_are_fixed(self):
        self.assertEqual(style.TREE_DISCLOSURE_SLOT_WIDTH, 20)
        self.assertEqual(style.TREE_INDENT, style.TREE_DISCLOSURE_SLOT_WIDTH)
        self.assertEqual(style.TREE_CHEVRON_SIZE, 6)
        self.assertGreater(style.TREE_CHEVRON_STROKE, 0)
        self.assertLessEqual(style.TREE_CHEVRON_STROKE, 3)
        self.assertGreaterEqual(style.TREE_DISCLOSURE_HIT_SIZE, 20)

    def test_tree_branch_style_is_a_proxy_style(self):
        self.assertTrue(issubclass(style.TreeBranchStyle, QProxyStyle))

    def test_chevron_vertices_differ_and_span_fixed_square(self):
        collapsed = style.tree_chevron_vertices(False)
        expanded = style.tree_chevron_vertices(True)
        self.assertNotEqual(collapsed, expanded)
        half = style.TREE_CHEVRON_SIZE / 2.0
        for vertices in (collapsed, expanded):
            self.assertEqual(len(vertices), 3)
            xs = [p.x() for p in vertices]
            ys = [p.y() for p in vertices]
            # Both chevrons fill the same centred TREE_CHEVRON_SIZE square, so
            # toggling state never changes the indicator rectangle.
            self.assertEqual(max(xs) - min(xs), style.TREE_CHEVRON_SIZE)
            self.assertEqual(max(ys) - min(ys), style.TREE_CHEVRON_SIZE)
            self.assertEqual(min(xs), -half)
            self.assertEqual(max(xs), half)
            self.assertEqual(min(ys), -half)
            self.assertEqual(max(ys), half)

    def test_stylesheet_has_no_branch_background_rule(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            self.assertNotIn("::branch", style.build_stylesheet(palette))

    def test_branch_chevron_renders_different_directions(self):
        _app()
        style_obj = style.TreeBranchStyle(style.LIGHT_PALETTE)

        def drawn_pixels(open_state: bool):
            img = QImage(40, 40, QImage.Format_ARGB32)
            img.fill(0)
            painter = QPainter(img)
            option = QStyleOption()
            option.state = QStyle.State_Children
            if open_state:
                option.state |= QStyle.State_Open
            option.rect = QRect(0, 0, 20, 22)
            style_obj.drawPrimitive(QStyle.PE_IndicatorBranch, option, painter, None)
            painter.end()
            return {
                (x, y)
                for x in range(img.width())
                for y in range(img.height())
                if img.pixelColor(x, y).alpha() != 0
            }

        collapsed = drawn_pixels(False)
        expanded = drawn_pixels(True)
        # Both states actually paint a chevron, and the two directions differ.
        self.assertTrue(collapsed)
        self.assertTrue(expanded)
        self.assertNotEqual(collapsed, expanded)

    def test_tree_folder_font_is_bold_copy(self):
        font = style.tree_folder_font()
        self.assertTrue(font.bold())
        base = style.ui_font()
        self.assertFalse(base.bold())
        copy = style.tree_folder_font(base)
        self.assertTrue(copy.bold())
        self.assertFalse(base.bold())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class BannerStyleTests(unittest.TestCase):
    def test_preview_banner_uses_secondary_text(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            qss = style.preview_banner_style(palette)
            self.assertIn(palette.text_secondary, qss)

    def test_unavailable_banner_uses_warning(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            qss = style.unavailable_banner_style(palette)
            self.assertIn(palette.warning, qss)

    def test_banner_styles_are_distinct(self):
        palette = style.LIGHT_PALETTE
        self.assertNotEqual(
            style.preview_banner_style(palette),
            style.unavailable_banner_style(palette),
        )


if __name__ == "__main__":
    unittest.main()
