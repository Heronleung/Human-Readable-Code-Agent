"""Reusable presentation components for the chat-first workspace.

Every widget here is built from the shared token contract in
:mod:`hrca.core.visual_tokens` via :mod:`hrca.ui.style`; none hard-codes a
colour, radius or padding. Three rules are enforced by construction:

* **A control always explains itself.** :func:`make_button` requires a label
  and derives an accessible name, and a disabled control carries its reason as
  a tooltip — so a control that cannot act says why.
* **State never relies on colour.** :class:`StatusChip` always renders a glyph
  *and* a word alongside its tint, so the state survives a monochrome screen
  or a colour-blind reader.
* **Emptiness is stated, not implied.** :class:`StateView` renders the one of
  empty / loading / blocked / failed / stale / unknown an area is actually in,
  with the next action when there is one.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from hrca.core import visual_tokens

from . import style

# ---------------------------------------------------------------------------
# The one layout rule every surface follows.
#
# A surface is a card or a state view. It is *natural height*: it takes the
# height its content needs and never grows to fill the page. Surplus vertical
# space belongs to the page canvas, not to the inside of a card — that is what
# keeps a heading, its explanation and its action one visually coherent group
# instead of three items flung apart by stretch.
#
# The failure this replaces: a QFrame's default vertical size policy is
# Preferred, so a card in a scroll area grew to the viewport and its labels
# absorbed the slack. A one-line heading measured 93 px tall and its action sat
# 200 px below it.
# ---------------------------------------------------------------------------
SURFACE_PADDING = visual_tokens.SPACE_24          # padding inside a surface
SURFACE_GAP = visual_tokens.INSET                 # heading -> text -> action
SURFACE_SPACING = visual_tokens.GAP_GROUP         # between surfaces on a page


def natural_height(widget: QWidget) -> None:
    """Pin ``widget`` to its natural height so it never stretches."""
    widget.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
    )

# Object names the stylesheet knows. New names are added to style.build_stylesheet.
CARD = "card"
STATE_VIEW = "stateView"
STATUS_CHIP = "statusChip"
LIST_ROW = "listRow"
DETAILS_DRAWER = "detailsDrawer"
PLAN_CARD = "planCard"
MESSAGE_BUBBLE = "messageBubble"
CONTEXT_BAR = "contextBar"
RAIL_BUTTON = "railButton"
COMPOSER = "workspaceComposer"
SECTION_LABEL = "sectionLabel"

BUTTON_KINDS = ("primary", "secondary", "ghost", "danger")

# State tokens → (glyph, style palette token). The glyph is the non-colour cue.
_STATE_CUES = {
    "draft": ("○", "neutral"),
    "ready": ("●", "info"),
    "running": ("◔", "info"),
    "paused": ("‖", "warning"),
    "blocked": ("⚠", "warning"),
    "completed": ("✓", "success"),
    "failed": ("✗", "error"),
    "cancelled": ("⊘", "neutral"),
    "stale": ("↻", "warning"),
    "unknown": ("?", "warning"),
    "verified": ("✓", "success"),
    "unverified": ("?", "warning"),
    "missing": ("—", "error"),
    "conflicting": ("⚠", "error"),
    "info": ("ℹ", "info"),
    "success": ("✓", "success"),
    "warning": ("⚠", "warning"),
    "error": ("✗", "error"),
    "neutral": ("○", "neutral"),
}


def state_cue(state: str) -> str:
    """Return the non-colour glyph for a state token."""
    glyph, _ = _STATE_CUES.get(state, ("○", "neutral"))
    return glyph


def state_token(state: str) -> str:
    """Return the palette token a state maps to."""
    _, token = _STATE_CUES.get(state, ("○", "neutral"))
    return token


def make_button(
    label: str,
    kind: str = "secondary",
    *,
    accessible: str = "",
    tooltip: str = "",
    on_click: Optional[Callable[[], None]] = None,
    enabled: bool = True,
    disabled_reason: str = "",
    parent: Optional[QWidget] = None,
) -> QPushButton:
    """Build a styled button that always carries an accessible name.

    A disabled button's reason becomes its tooltip and its accessible
    description, so a control that cannot act never silently does nothing.
    """
    if kind not in BUTTON_KINDS:
        raise ValueError(f"unknown button kind: {kind!r}")
    button = QPushButton(label, parent)
    button.setObjectName(f"{kind}Button")
    button.setAccessibleName(accessible or label)
    if enabled:
        button.setEnabled(True)
        if tooltip:
            button.setToolTip(tooltip)
    else:
        button.setEnabled(False)
        reason = disabled_reason or tooltip or "This action is not available right now."
        button.setToolTip(reason)
        button.setAccessibleDescription(reason)
    if on_click is not None:
        button.clicked.connect(lambda _checked=False: on_click())
    return button


class StatusChip(QLabel):
    """A state chip that always shows a glyph and a word, never colour alone."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName(STATUS_CHIP)
        self._state = "neutral"
        self._label = ""

    def set_state(self, state: str, label: Optional[str] = None) -> None:
        """Set the chip's state token and optional display word."""
        self._state = state
        self._label = label if label is not None else state
        self.setText(f"{state_cue(state)}  {self._label}")
        self.setAccessibleName(f"State: {self._label}")
        if _style_parent(self) is not None:
            self.setStyleSheet(
                style.state_chip_style(_style_parent(self), state_token(state))
            )

    @property
    def state(self) -> str:
        """Return the current state token."""
        return self._state


def _style_parent(widget: QWidget) -> Optional[style.Palette]:
    """Return the palette in effect for ``widget``, if a window owns one."""
    window = widget.window()
    return getattr(window, "_palette", None)


class Card(QFrame):
    """A titled container. ``body`` is a vertical layout callers fill."""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(CARD)
        self.setFrameShape(QFrame.Shape.NoFrame)
        natural_height(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SURFACE_PADDING,
            SURFACE_PADDING,
            SURFACE_PADDING,
            SURFACE_PADDING,
        )
        outer.setSpacing(SURFACE_GAP)

        self._heading = QLabel(title, self)
        self._heading.setObjectName("panelHeader")
        self._heading.setWordWrap(True)
        self._heading.setVisible(bool(title))
        outer.addWidget(self._heading)

        self._subtitle = QLabel(subtitle, self)
        self._subtitle.setObjectName("secondary")
        self._subtitle.setWordWrap(True)
        self._subtitle.setVisible(bool(subtitle))
        outer.addWidget(self._subtitle)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SURFACE_GAP)
        outer.addLayout(self.body)
        # Surplus goes below the content, never between the heading, its text
        # and its action. A container that forces a larger rect on this card —
        # a stacked layout does — would otherwise spread that surplus across
        # the three of them.
        outer.addStretch(1)

    def set_heading(self, title: str, subtitle: str = "") -> None:
        """Update the card's heading and subtitle."""
        self._heading.setText(title)
        self._heading.setVisible(bool(title))
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))


class StateView(QFrame):
    """The one honest presentation of an area that has nothing to show.

    ``kind`` names which of the states the area is in — ``empty``, ``loading``,
    ``blocked``, ``failed``, ``stale``, ``unknown``, ``offline`` — so a caller
    cannot render "no data" as if it were success.
    """

    KINDS = ("empty", "loading", "blocked", "failed", "stale", "unknown", "offline")

    def __init__(
        self,
        kind: str = "empty",
        title: str = "",
        message: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        if kind not in self.KINDS:
            raise ValueError(f"unknown state view kind: {kind!r}")
        self.setObjectName(STATE_VIEW)
        natural_height(self)
        self._kind = kind
        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(SURFACE_GAP)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            SURFACE_PADDING,
            SURFACE_PADDING,
            SURFACE_PADDING,
            SURFACE_PADDING,
        )
        layout.setSpacing(SURFACE_GAP)

        self._heading = QLabel(title, self)
        self._heading.setObjectName("stateTitle")
        self._heading.setWordWrap(True)
        layout.addWidget(self._heading)

        self._message = QLabel(message, self)
        self._message.setObjectName("secondary")
        self._message.setWordWrap(True)
        layout.addWidget(self._message)

        layout.addLayout(self._actions)
        # After the action, so the group stays top-aligned as one unit.
        layout.addStretch(1)
        self.set_state(kind, title, message)

    def set_state(self, kind: str, title: str, message: str = "") -> None:
        """Update which state the area is in, and its copy."""
        if kind not in self.KINDS:
            raise ValueError(f"unknown state view kind: {kind!r}")
        self._kind = kind
        self._heading.setText(f"{self._cue(kind)}  {title}" if title else self._cue(kind))
        self._message.setText(message)
        self._message.setVisible(bool(message))
        self.setAccessibleName(f"{kind}: {title}")

    def add_action(self, button: QPushButton) -> None:
        """Add an action button beneath the message."""
        self._actions.addWidget(button)

    def clear_actions(self) -> None:
        """Remove every action button."""
        while self._actions.count():
            item = self._actions.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    @staticmethod
    def _cue(kind: str) -> str:
        return {
            "empty": "○",
            "loading": "◔",
            "blocked": "⚠",
            "failed": "✗",
            "stale": "↻",
            "unknown": "?",
            "offline": "⊘",
        }.get(kind, "○")

    @property
    def kind(self) -> str:
        """Return the current state kind."""
        return self._kind


class ListRow(QFrame):
    """One row in a list: a title, a meta line, a chip and optional actions."""

    def __init__(
        self,
        title: str,
        meta: str = "",
        state: str = "neutral",
        state_label: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(LIST_ROW)
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            visual_tokens.GAP_TIGHT,
            visual_tokens.SPACE_4,
            visual_tokens.GAP_TIGHT,
            visual_tokens.SPACE_4,
        )
        layout.setSpacing(visual_tokens.GAP_TIGHT)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(0)
        self.title_label = QLabel(title, self)
        self.title_label.setWordWrap(True)
        text_column.addWidget(self.title_label)
        self.meta_label = QLabel(meta, self)
        self.meta_label.setObjectName("secondary")
        self.meta_label.setWordWrap(True)
        self.meta_label.setVisible(bool(meta))
        text_column.addWidget(self.meta_label)
        layout.addLayout(text_column, 1)

        self.chip = StatusChip(self)
        self.chip.set_state(state, state_label or state)
        layout.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)

        self.actions = QWidget(self)
        self.actions_layout = QHBoxLayout(self.actions)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(visual_tokens.SPACE_4)
        self.actions.setVisible(False)
        layout.addWidget(self.actions, 0, Qt.AlignmentFlag.AlignTop)

    def add_action(self, button: QPushButton) -> None:
        """Add a trailing action button and reveal the action area."""
        self.actions_layout.addWidget(button)
        self.actions.setVisible(True)

    def set_meta(self, meta: str) -> None:
        """Update the row's meta line."""
        self.meta_label.setText(meta)
        self.meta_label.setVisible(bool(meta))


class DetailsDrawer(QFrame):
    """A temporary pane for detail that does not deserve permanent space."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName(DETAILS_DRAWER)
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            visual_tokens.INSET,
            visual_tokens.INSET,
            visual_tokens.INSET,
            visual_tokens.INSET,
        )
        layout.setSpacing(visual_tokens.GAP_TIGHT)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel("Details", self)
        self.heading.setObjectName("panelHeader")
        header.addWidget(self.heading, 1)
        self.close_button = make_button(
            "Close", "ghost", accessible="Close details", on_click=self.hide
        )
        header.addWidget(self.close_button)
        layout.addLayout(header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(visual_tokens.GAP_TIGHT)
        layout.addLayout(self.body)

    def set_content(self, heading: str, rows: Sequence[tuple]) -> None:
        """Replace the drawer body with labelled values."""
        self.heading.setText(heading)
        self.clear()
        for label, value in rows:
            row = QWidget(self)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(visual_tokens.GAP_TIGHT)
            name = QLabel(label, row)
            name.setObjectName("secondary")
            name.setMinimumWidth(120)
            value_label = QLabel(str(value), row)
            value_label.setWordWrap(True)
            row_layout.addWidget(name)
            row_layout.addWidget(value_label, 1)
            self.body.addWidget(row)
        self.body.addStretch(1)

    def clear(self) -> None:
        """Remove every content row."""
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()


class Composer(QFrame):
    """The goal field. Only Agent Chat carries one.

    Its summary line states the bound context, the action that will be taken
    and the authority it needs, so what a dispatch would do is legible before
    anything is sent. Every other destination reaches this one through a
    compact *New task* action rather than showing a second goal field.
    """

    submitted = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("composerArea")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(visual_tokens.SPACE_4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(visual_tokens.GAP_TIGHT)

        self.input = QTextEdit(self)
        self.input.setObjectName("workspaceComposer")
        self.input.setAccessibleName("Goal")
        self.input.setPlaceholderText(
            "Describe what you want done — this proposes a plan; it sends nothing."
        )
        self.input.setFixedHeight(44)
        row.addWidget(self.input, 1)

        self.send_button = make_button(
            "Propose a plan",
            "primary",
            accessible="Propose a plan from this goal",
            tooltip="Turn the goal into an editable plan. Nothing runs and nothing is sent.",
            on_click=self._submit,
        )
        row.addWidget(self.send_button, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(row)

        self.summary = QLabel("", self)
        self.summary.setObjectName("secondary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

    def _submit(self) -> None:
        text = self.input.toPlainText().strip()
        if text:
            self.submitted.emit(text)

    def set_summary(self, text: str) -> None:
        """Update the context/action/authority summary line."""
        self.summary.setText(text)

    def clear(self) -> None:
        """Clear the goal field."""
        self.input.clear()


__all__ = [
    "CARD",
    "STATE_VIEW",
    "STATUS_CHIP",
    "LIST_ROW",
    "DETAILS_DRAWER",
    "SECTION_LABEL",
    "SURFACE_PADDING",
    "SURFACE_GAP",
    "SURFACE_SPACING",
    "state_cue",
    "state_token",
    "natural_height",
    "make_button",
    "StatusChip",
    "Card",
    "StateView",
    "ListRow",
    "DetailsDrawer",
    "Composer",
]
