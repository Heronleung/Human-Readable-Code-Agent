"""Tests for the P3.2 visual design system (:mod:`hrca.style`).

Covered here: deterministic colour-scheme detection, the WCAG 4.5:1 contrast
contract for body / syntax / chip text in both palettes, the chip background
compositing helper, the component style-sheet factories, and the Twin body
selector. These tests import PySide6 and are skipped when it is not installed.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

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


if __name__ == "__main__":
    unittest.main()
