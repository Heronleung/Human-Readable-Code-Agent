"""The workspace shell: four primary groups, one context bar, one drawer.

The shell is the frame every destination is shown inside. It owns:

* a **thin rail** of four primary groups — **Home**, **Agent Chat**, **Work**
  and **Documents** — with **Settings** anchored separately at the foot. The
  seven approved functional destinations stay reachable (Work holds Jobs,
  Agents and Review as contextual views), but they are not seven simultaneous
  top-level choices;
* a **context bar** stating the repository, its state, the accepted baseline
  and the open document, the single safest next action, one concise authority
  indicator, and — only when relevant — a provider warning, the scan action and
  an activity entry;
* a temporary **details drawer** that hosts diagnostics and detail on demand.

There is **no global composer**. Only Agent Chat carries a composer; every
other surface reaches it through a compact *New task* action, so a
goal field is never presented before there is a project to act on.

The shell also knows the difference between **started** and **not started**.
With no project bound it hides every project-dependent rail entry and the
whole status footer, so first use shows one obvious path — open a project —
rather than an inactive orchestration surface. A blocking error is never
hidden: :meth:`Shell.set_footer` is what reveals the footer, and the client
keeps it visible whenever the state is a warning or a failure.

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
    QVBoxLayout,
    QWidget,
)

from hrca.core import visual_tokens

from . import style
from .components import DetailsDrawer, StatusChip, make_button
from .widgets import ElidedLabel

# The four primary groups, in order. The glyph is decorative: the word beside
# it is the label, and the button's accessible name repeats it.
RAIL_PRIMARY: Tuple[Tuple[str, str, str], ...] = (
    ("home", "↻", "Home"),
    ("chat", "◆", "Agent Chat"),
    ("work", "▤", "Work"),
    ("documents", "▭", "Documents"),
)

# Settings is reachable from first use, so it is anchored rather than grouped.
RAIL_SETTINGS: Tuple[str, str, str] = ("settings", "⚙", "Settings")

RAIL_DESTINATIONS: Tuple[Tuple[str, str, str], ...] = RAIL_PRIMARY + (RAIL_SETTINGS,)

#: Rail entries that only make sense once a project is bound.
PROJECT_DEPENDENT: Tuple[str, ...] = ("chat", "work", "documents")

#: The contextual views Work groups, in order.
WORK_VIEWS: Tuple[Tuple[str, str], ...] = (
    ("jobs", "Jobs"),
    ("agents", "Agents"),
    ("review", "Review"),
)

RAIL_WIDTH = 168
DRAWER_WIDTH = 340
CONTEXT_BAR_HEIGHT = 48
STATUS_BAR_HEIGHT = 24
#: Longest primary-action label the context bar renders before eliding.
PRIMARY_ACTION_CHARS = 30


class ContextBar(QFrame):
    """Repository, state, baseline, authority and the one safest next action."""

    primary_clicked = Signal()
    activity_clicked = Signal()

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

        self.authority_chip = StatusChip(self)
        self.authority_chip.set_state("neutral", "Read only")
        self.authority_chip.setToolTip(
            "The widest authority the current plan requests. Details are shown "
            "when you confirm a plan."
        )
        layout.addWidget(self.authority_chip)

        self.activity_button = make_button(
            "Activity",
            "ghost",
            accessible="Show activity and diagnostics",
            tooltip="Show the recorded status and diagnostics.",
            on_click=self.activity_clicked.emit,
        )
        self.activity_button.setVisible(False)
        layout.addWidget(self.activity_button)

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

    def set_authority(self, label: str, detail: str = "") -> None:
        """Update the single concise authority indicator."""
        self.authority_chip.set_state("neutral", label)
        self.authority_chip.setToolTip(detail or label)


class Shell(QWidget):
    """The whole workspace frame: rail, context bar, stack and drawer."""

    destination_changed = Signal(str)

    def __init__(self, palette: style.Palette, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("shell")
        self._palette = palette
        self._buttons: Dict[str, QPushButton] = {}
        self._pages: Dict[str, QWidget] = {}
        self._current = ""
        self._started = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.context_bar = ContextBar(self)
        self.context_bar.activity_clicked.connect(self._on_activity)
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
        self._footer: Optional[QWidget] = None

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

        for key, glyph, label in RAIL_PRIMARY:
            layout.addWidget(self._rail_button(rail, key, glyph, label))

        layout.addStretch(1)
        key, glyph, label = RAIL_SETTINGS
        layout.addWidget(self._rail_button(rail, key, glyph, label))
        return rail

    def _rail_button(self, rail: QWidget, key: str, glyph: str, label: str) -> QPushButton:
        button = QPushButton(f"{glyph}   {label}", rail)
        button.setObjectName("railButton")
        button.setCheckable(True)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.clicked.connect(lambda _checked=False, name=key: self.select(name))
        self._buttons[key] = button
        return button

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
            button.setEnabled(True)
        self._current = key
        self.destination_changed.emit(key)

    @property
    def current(self) -> str:
        """Return the current destination key."""
        return self._current

    # -- start state --------------------------------------------------------
    @property
    def started(self) -> bool:
        """Whether a project is bound."""
        return self._started

    def set_started(self, started: bool) -> None:
        """Switch between the first-use frame and the bound-project frame.

        With no project, the project-dependent rail entries are hidden rather
        than disabled: there is nothing behind them yet, and presenting four
        inactive choices is the density this exists to remove. Home and
        Settings stay, because first use still needs a place to open a project
        and a place to configure the provider.
        """
        self._started = started
        for key in PROJECT_DEPENDENT:
            button = self._buttons.get(key)
            if button is not None:
                button.setVisible(started)
        if not started and self._current in PROJECT_DEPENDENT:
            self.select("home")

    # -- context ------------------------------------------------------------
    def set_context(self, context) -> None:
        """Render the bound context into the context bar."""
        if not context.has_project:
            self.context_bar.set_context("No project open", "")
            return
        root_name = str(context.root).rstrip("/\\").split("/")[-1].split("\\")[-1]
        self.context_bar.set_context(
            f"{root_name}  ·  {context.repository_state}",
            f"Baseline: {context.baseline_text}  ·  {context.document_text}",
        )

    def set_authority(self, label: str, detail: str = "") -> None:
        """Update the concise authority indicator."""
        self.context_bar.set_authority(label, detail)

    def show_activity(self, visible: bool) -> None:
        """Show the Activity entry only when there is activity to inspect."""
        self.context_bar.activity_button.setVisible(visible)

    # -- footer -------------------------------------------------------------
    def add_footer(self, widget: QWidget) -> None:
        """Register the status footer; it stays hidden until it has news."""
        self._footer = widget
        widget.setVisible(False)
        self.layout().addWidget(widget)

    def set_footer(self, visible: bool) -> None:
        """Reveal the status footer for a warning or a failure."""
        if self._footer is not None:
            self._footer.setVisible(visible)

    # -- drawer -------------------------------------------------------------
    def _on_activity(self) -> None:
        if self.drawer.isHidden():
            self.show_details("Activity", ())
        else:
            self.hide_details()

    def show_details(self, heading: str, rows) -> None:
        """Show the details drawer with the given labelled rows."""
        self.drawer.set_content(heading, rows)
        self.drawer.setVisible(True)

    def hide_details(self) -> None:
        """Hide the details drawer."""
        self.drawer.setVisible(False)


__all__ = [
    "RAIL_PRIMARY",
    "RAIL_SETTINGS",
    "RAIL_DESTINATIONS",
    "PROJECT_DEPENDENT",
    "WORK_VIEWS",
    "ContextBar",
    "Shell",
]
