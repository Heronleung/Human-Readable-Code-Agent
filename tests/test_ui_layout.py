"""The one layout rule every empty state follows.

These pin the D-UI2R regression: a card whose parent forces it to grow used to
spread the surplus *inside itself*, so a one-line heading measured 93 px and its
action sat 200 px below it. The rule is — a surface is natural height, its
content is one group aligned to the top, and the surplus belongs to the page
canvas.

The tests measure geometry rather than eyeballing styles, so a future change
that reintroduces a stretch between a heading, its text and its action fails
here rather than in a screenshot.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QStackedWidget, QVBoxLayout, QWidget

    from hrca.core import visual_tokens
    from hrca.ui import components
    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - environment without the desktop extra
    HAS_PYSIDE6 = False


#: A one-line label at the body font is ~19 px. Anything near three times that
#: means the label absorbed stretch rather than its text.
MAX_HEADING_HEIGHT = 40
#: How far an action may sit from its heading inside one surface: heading,
#: one line of explanation and the gaps between them.
MAX_HEADING_TO_ACTION = 120


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _host_with(widget, *, trailing_stretch: bool, height: int = 600):
    """Put ``widget`` in a 640x600 host, optionally with a trailing stretch.

    The caller must keep the returned host alive: it owns ``widget``, and a
    collected host takes its child with it.
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget)
    if trailing_stretch:
        layout.addStretch(1)
    host.resize(640, height)
    host.show()
    QApplication.processEvents()
    QApplication.processEvents()
    return host


def _offset(widget, ancestor) -> int:
    """Return the y offset of ``widget`` inside ``ancestor``."""
    return widget.mapTo(ancestor, widget.rect().topLeft()).y()


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class SurfaceRuleTests(unittest.TestCase):
    def setUp(self):
        _app()

    def _card(self) -> components.Card:
        card = components.Card("Continue", "No plan exists yet.")
        card.body.addWidget(components.make_button("State a goal in Agent Chat", "primary"))
        return card

    def test_a_surface_uses_the_shared_padding_and_gap(self):
        card = self._card()
        margins = card.layout().contentsMargins()
        self.assertEqual(margins.left(), visual_tokens.SPACE_24)
        self.assertEqual(margins.top(), visual_tokens.SPACE_24)
        self.assertEqual(margins.right(), visual_tokens.SPACE_24)
        self.assertEqual(margins.bottom(), visual_tokens.SPACE_24)
        self.assertEqual(card.layout().spacing(), visual_tokens.INSET)

    def test_a_surface_takes_its_natural_height(self):
        card = self._card()
        self._host = _host_with(card, trailing_stretch=True)
        self.assertLessEqual(card.height(), card.sizeHint().height() + 4)

    def _stacked(self, widget, height: int = 600):
        """Force ``widget`` to a full-height rect the way a stacked page is.

        A plain layout respects the Maximum size policy, so it cannot force
        growth. A ``QStackedLayout`` sets its page's geometry to the full rect
        regardless of policy — and that is exactly what a destination inside
        Work does, so it is the case that has to stay coherent.
        """
        stack = QStackedWidget()
        stack.addWidget(widget)
        stack.resize(640, height)
        stack.show()
        QApplication.processEvents()
        QApplication.processEvents()
        return stack

    def test_a_forced_surface_keeps_its_content_together(self):
        card = self._card()
        self._host = self._stacked(card)
        self.assertGreater(card.height(), card.sizeHint().height() + 40)
        heading_y = _offset(card._heading, card)
        action = card.body.itemAt(0).widget()
        action_y = _offset(action, card)
        self.assertLess(action_y - heading_y, MAX_HEADING_TO_ACTION)

    def test_a_surface_heading_never_absorbs_the_surplus(self):
        card = self._card()
        self._host = self._stacked(card)
        self.assertLess(card._heading.height(), MAX_HEADING_HEIGHT)

    def test_a_forced_state_view_keeps_its_content_together(self):
        view = components.StateView("empty", "Nothing in progress", "State a goal.")
        view.add_action(components.make_button("Start in Agent Chat", "primary"))
        self._host = self._stacked(view)
        self.assertLess(view._heading.height(), MAX_HEADING_HEIGHT)
        action = view._actions.itemAt(0).widget()
        self.assertLess(
            _offset(action, view) - _offset(view._heading, view), MAX_HEADING_TO_ACTION
        )

    def test_a_state_view_follows_the_same_rule(self):
        view = components.StateView("empty", "Nothing in progress", "State a goal.")
        view.add_action(components.make_button("Start in Agent Chat", "primary"))
        self._host = _host_with(view, trailing_stretch=False)
        margins = view.layout().contentsMargins()
        self.assertEqual(margins.left(), visual_tokens.SPACE_24)
        self.assertLess(view._heading.height(), MAX_HEADING_HEIGHT)
        action = view._actions.itemAt(0).widget()
        self.assertLess(
            _offset(action, view) - _offset(view._heading, view), MAX_HEADING_TO_ACTION
        )

    def test_a_state_view_carries_no_spacer_between_its_parts(self):
        # A spacer *between* the heading, the message and the action is what
        # splits the group; the only stretch is the one after the action.
        view = components.StateView("empty", "Title", "Message")
        stretch_positions = [
            index
            for index in range(view.layout().count())
            if view.layout().itemAt(index).spacerItem() is not None
        ]
        self.assertEqual(stretch_positions, [view.layout().count() - 1])

    def test_a_card_carries_no_spacer_before_its_body(self):
        card = self._card()
        stretch_positions = [
            index
            for index in range(card.layout().count())
            if card.layout().itemAt(index).spacerItem() is not None
        ]
        self.assertEqual(stretch_positions, [card.layout().count() - 1])


if __name__ == "__main__":
    unittest.main()
