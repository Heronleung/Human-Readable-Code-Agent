"""Accessibility and honesty contracts for the workspace component system.

Every control the redesign builds must explain itself, and no state may rely on
colour alone. These tests pin that at the component layer so a destination
cannot accidentally ship a button with no accessible name or a "no data" panel
that reads as success.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton

    from hrca.ui import components, style
    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - environment without the desktop extra
    HAS_PYSIDE6 = False


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ButtonContractTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_every_button_kind_carries_an_accessible_name(self):
        for kind in components.BUTTON_KINDS:
            with self.subTest(kind=kind):
                button = components.make_button("Do the thing", kind)
                self.assertIsInstance(button, QPushButton)
                self.assertEqual(button.accessibleName(), "Do the thing")
                self.assertTrue(button.isEnabled())

    def test_a_disabled_button_states_why(self):
        button = components.make_button(
            "Cancel", "danger", enabled=False, disabled_reason="This job cannot be cancelled."
        )
        self.assertFalse(button.isEnabled())
        self.assertEqual(button.toolTip(), "This job cannot be cancelled.")
        self.assertEqual(button.accessibleDescription(), "This job cannot be cancelled.")

    def test_a_disabled_button_without_a_reason_still_explains_itself(self):
        button = components.make_button("Cancel", "danger", enabled=False)
        self.assertTrue(button.toolTip())
        self.assertTrue(button.accessibleDescription())

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            components.make_button("Nope", "fancy")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class StateChipTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_a_chip_always_shows_a_glyph_and_a_word(self):
        chip = components.StatusChip()
        chip.set_state("blocked", "Blocked")
        text = chip.text()
        self.assertIn(components.state_cue("blocked"), text)
        self.assertIn("Blocked", text)

    def test_every_state_token_has_a_non_colour_cue(self):
        for token in (
            "draft", "ready", "running", "paused", "blocked", "completed",
            "failed", "cancelled", "stale", "unknown",
        ):
            with self.subTest(token=token):
                self.assertTrue(components.state_cue(token))
                self.assertTrue(components.state_token(token))

    def test_an_unknown_token_still_renders_a_cue(self):
        self.assertTrue(components.state_cue("no_such_state"))


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class StateViewTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_every_named_state_renders_its_own_cue(self):
        for kind in components.StateView.KINDS:
            with self.subTest(kind=kind):
                view = components.StateView(kind, "Title", "Message")
                self.assertEqual(view.kind, kind)
                self.assertIn("Title", view.accessibleName())

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(ValueError):
            components.StateView("triumphant", "Title", "Message")

    def test_actions_can_be_added_and_cleared(self):
        view = components.StateView("empty", "Nothing here", "Try again")
        view.add_action(components.make_button("Open project", "primary"))
        self.assertEqual(len(view.findChildren(QPushButton)), 1)
        view.clear_actions()
        self.assertEqual(len(view.findChildren(QPushButton)), 0)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ListRowTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_a_row_shows_a_chip_that_carries_its_state(self):
        row = components.ListRow("Job one", "owner: Author", "running", "Running")
        self.assertEqual(row.chip.state, "running")
        self.assertIn("Running", row.chip.text())

    def test_actions_reveal_the_action_area(self):
        row = components.ListRow("Job one", "", "ready", "Ready")
        self.assertTrue(row.actions.isHidden())
        row.add_action(components.make_button("Dispatch", "primary"))
        self.assertFalse(row.actions.isHidden())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class StylesheetContractTests(unittest.TestCase):
    def test_the_workspace_components_are_styled_in_both_palettes(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                qss = style.build_stylesheet(palette)
                for selector in (
                    "QFrame#card",
                    "QFrame#planCard",
                    "QFrame#stateView",
                    "QLabel#statusChip",
                    "QFrame#listRow",
                    "QFrame#detailsDrawer",
                    "QWidget#contextBar",
                    "QPushButton#railButton",
                    "QTextEdit#workspaceComposer",
                ):
                    self.assertIn(selector, qss)
                self.assertNotIn("gradient", qss.lower())

    def test_the_style_sheet_is_not_duplicated_by_component_literals(self):
        # Components name a role (an objectName); colour stays in the stylesheet.
        path = os.path.join("src", "hrca", "ui", "components.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("#ffffff", source)
        self.assertNotIn("rgb(", source)


if __name__ == "__main__":
    unittest.main()
