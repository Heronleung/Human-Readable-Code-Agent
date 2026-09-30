"""The workspace shell: one thin rail, one context bar, one composer.

The shell is the frame every destination is shown inside. It owns exactly four
things and nothing else:

* a **thin rail** of seven destinations, each with a glyph *and* a word so the
  destination reads without colour;
* a **context bar** stating the repository, its state and the accepted
  baseline, plus the single safest next action;
* a **composer** whose summary line names the bound context, the action that
  will be taken and the authority it needs — *before* anything is dispatched;
* a temporary **details drawer** that hosts detail on demand instead of a
  permanent pane.

The shell holds no domain logic. It reports intent through signals and renders
whatever the workspace tells it.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from hrca.core import visual_tokens

from . import style
from .components import DetailsDrawer, make_button
from .widgets import ElidedLabel

# The seven destinations, in order. The glyph is decorative: the word beside it
# is the label, and the button's accessible name repeats it.
RAIL_DESTINATIONS: Tuple[Tuple[str, str, str], ...] = (
    ("resume", "↻", "Resume"),
    ("chat", "◆", "Agent Chat"),
    ("jobs", "▤", "Jobs"),
    ("agents", "◇", "Agents"),
    ("review", "✓", "Review"),
    ("documents", "▭", "Documents"),
    ("settings", "⚙", "Settings"),
)

RAIL_WIDTH = 168
DRAWER_WIDTH = 340
CONTEXT_BAR_HEIGHT = 48
COMPOSER_HEIGHT = 88
#: Longest primary-action label the context bar renders before eliding.
PRIMARY_ACTION_CHARS = 30


class ContextBar(QFrame):
    """Repository, state and baseline, plus the one safest next action."""

    primary_clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("contextBar")
        self.setFixedHeight(CONTEXT_BAR_HEIGHT)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            visual_tokens.INSET, visual_tokens.SPACE_4, visual_tokens.INSET, visual_tokens.SPACE_4
        )
        layout.setSpacing(visual_tokens.GAP_TIGHT)

        self._primary = ElidedLabel("No project open", elide_mode=Qt.ElideRight, parent=self)
        self._primary.setObjectName("contextPrimary")
        layout.addWidget(self._primary, 3)

        self._secondary = ElidedLabel("", elide_mode=Qt.ElideRight, parent=self)
        self._secondary.setObjectName("secondary")
        layout.addWidget(self._secondary, 4)

        self.primary_button = make_button(
            "Open Project",
            "secondary",
            accessible="Open Project",
            tooltip="Choose the repository this workspace is bound to.",
            on_click=self.primary_clicked.emit,
        )
        layout.addWidget(self.primary_button)

    def add_widget(self, widget: QWidget) -> None:
        """Append a widget to the right-hand end of the context bar."""
        self.layout().addWidget(widget)

    def set_context(self, primary: str, secondary: str) -> None:
        """Update the two context lines."""
        self._primary.setText(primary)
        self._secondary.setText(secondary)

    def set_primary_action(self, label: str, tooltip: str = "", enabled: bool = True) -> None:
        """Update the context bar's single action.

        The button is kept to a bounded width so it never crowds the context
        lines at the smallest supported viewport; the full label and reason
        stay available in the tooltip and the accessible description.
        """
        display = label if len(label) <= PRIMARY_ACTION_CHARS else label[: PRIMARY_ACTION_CHARS - 1] + "…"
        full = tooltip or label
        self.primary_button.setText(display)
        self.primary_button.setAccessibleName(label)
        self.primary_button.setEnabled(enabled)
        self.primary_button.setToolTip(f"{label}\n\n{full}" if tooltip else label)
        self.primary_button.setAccessibleDescription(full)


class Composer(QFrame):
    """A goal field that states its context, action and authority up front."""

    submitted = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("composerArea")
        self.setFixedHeight(COMPOSER_HEIGHT)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            visual_tokens.INSET, visual_tokens.SPACE_4, visual_tokens.INSET, visual_tokens.SPACE_4
        )
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
        self.summary.setWordWrap(False)
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


class Shell(QWidget):
    """The whole workspace frame: rail, context bar, stack, composer, drawer."""

    destination_changed = Signal(str)
    goal_submitted = Signal(str)

    def __init__(self, palette: style.Palette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("shell")
        self._palette = palette
        self._buttons: Dict[str, QPushButton] = {}
        self._pages: Dict[str, QWidget] = {}
        self._current = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.context_bar = ContextBar(self)
        root.addWidget(self.context_bar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_rail())

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("contentStack")
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(self.stack, 1)

        self.drawer = DetailsDrawer(self)
        self.drawer.setFixedWidth(DRAWER_WIDTH)
        self.drawer.setVisible(False)
        body.addWidget(self.drawer)

        root.addLayout(body, 1)

        self.composer = Composer(self)
        self.composer.submitted.connect(self.goal_submitted.emit)
        root.addWidget(self.composer)

    def add_footer(self, widget: QWidget) -> None:
        """Append a widget beneath the composer (the transient status strip)."""
        self.layout().addWidget(widget)

    # -- rail ---------------------------------------------------------------
    def _build_rail(self) -> QWidget:
        rail = QWidget(self)
        rail.setObjectName("navRail")
        rail.setFixedWidth(RAIL_WIDTH)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, visual_tokens.SPACE_8, 0, visual_tokens.SPACE_8)
        layout.setSpacing(0)

        heading = QLabel("PrimaAgent", rail)
        heading.setObjectName("railHeading")
        heading.setContentsMargins(visual_tokens.INSET, 0, 0, visual_tokens.SPACE_4)
        layout.addWidget(heading)

        for key, glyph, label in RAIL_DESTINATIONS:
            button = QPushButton(f"{glyph}   {label}", rail)
            button.setObjectName("railButton")
            button.setCheckable(True)
            button.setAccessibleName(label)
            button.setToolTip(label)
            button.clicked.connect(lambda _checked=False, name=key: self.select(name))
            self._buttons[key] = button
            layout.addWidget(button)

        layout.addStretch(1)
        return rail

    # -- destinations -------------------------------------------------------
    def register(self, key: str, page: QWidget, label: str = "") -> None:
        """Add a destination page to the stack under ``key``."""
        self._pages[key] = page
        self.stack.addWidget(page)
        button = self._buttons.get(key)
        if button is not None and label:
            button.setAccessibleName(label)

    def page(self, key: str) -> Optional[QWidget]:
        """Return the registered page for ``key``, or ``None``."""
        return self._pages.get(key)

    def select(self, key: str) -> None:
        """Show the destination ``key``."""
        page = self._pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for name, button in self._buttons.items():
            button.setChecked(name == key)
        self._current = key
        self.destination_changed.emit(key)

    @property
    def current(self) -> str:
        """Return the current destination key."""
        return self._current

    # -- context ------------------------------------------------------------
    def set_context(self, context) -> None:
        """Render the bound context into the context bar."""
        if not context.has_project:
            self.context_bar.set_context("No project open", "Open a project to begin.")
        else:
            root_name = str(context.root).rstrip("/\\").split("/")[-1].split("\\")[-1]
            self.context_bar.set_context(
                f"{root_name}  ·  {context.repository_state}",
                f"Baseline: {context.baseline_text}  ·  {context.document_text}",
            )
        self.composer.set_summary(self._composer_summary(context))

    @staticmethod
    def _composer_summary(context) -> str:
        if not context.has_project:
            base = "Context: no project open"
        else:
            base = f"Context: {context.document_text}"
        return (
            f"{base}   ·   Action: propose an editable plan   ·   "
            "Authority: read only — nothing is sent"
        )

    # -- drawer -------------------------------------------------------------
    def show_details(self, heading: str, rows) -> None:
        """Show the details drawer with the given labelled rows."""
        self.drawer.set_content(heading, rows)
        self.drawer.setVisible(True)

    def hide_details(self) -> None:
        """Hide the details drawer."""
        self.drawer.setVisible(False)


__all__ = ["RAIL_DESTINATIONS", "ContextBar", "Composer", "Shell"]
