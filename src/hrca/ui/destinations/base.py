"""The shared destination frame and the host protocol.

A :class:`Destination` is a titled page. It renders from the local
:class:`~hrca.ui.appmodel.session.Workspace` and asks the **host** — the
desktop window — to perform anything that reaches the backend.

Each destination has two regions:

* a **refreshed body** that :meth:`Destination.refresh` rebuilds from the
  workspace every time the state changes; and
* a **persistent hosted region**, at the foot of the page, where the host
  mounts widgets it built and keeps driving — the document library, the
  Memory reader, the Settings pages. A refresh never touches it, so those
  widgets keep their identity and the state already applied to them.

A destination that is entirely hosted (``hosted = True``) simply leaves its
refreshed body empty.

The host protocol is deliberately small. A destination may call:

``open_project()``
    Present the project picker and bind the chosen root.
``dispatch_job(key)``
    Ask the workspace to start a job; the host confirms a protected effect.
``confirm_plan()``
    Confirm the current plan.
``record_decision(decision)``
    Ask for and record a human review decision.
``pause_job(key)`` / ``resume_job(key)`` / ``cancel_job(key)``
    Ask for a supported lifecycle change.
``focus_destination(key)``
    Move the rail to another destination.
``show_details(heading, rows)``
    Show the temporary details drawer.

None of these is a backend call in itself; the host decides how each is
performed, so exactly one place talks to the boundary.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hrca.core import visual_tokens

from ..appmodel.session import Workspace
from ..components import StateView, make_button


class Destination(QWidget):
    """A titled page; refreshed from the workspace, plus a hosted region."""

    title = ""
    subtitle = ""
    #: When true the page is entirely host-built; the refreshed body stays empty.
    hosted = False
    #: When true the header carries the compact "New task" action. Agent Chat,
    #: which owns the composer, sets this to ``False``.
    show_new_task = True
    #: When true the page is shown *inside* another destination, so it renders
    #: no header of its own — its container already supplies the title and the
    #: one "New task" action, and repeating them would duplicate both.
    embedded = False

    def __init__(
        self,
        workspace: Workspace,
        host,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.workspace = workspace
        self.host = host
        self.setObjectName(type(self).__name__)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.new_task_button: Optional[QPushButton] = None
        self.heading: Optional[QLabel] = None
        self.subheading: Optional[QLabel] = None

        if not self.embedded:
            header = QWidget(self)
            header_row = QHBoxLayout(header)
            header_row.setContentsMargins(
                visual_tokens.GAP_GROUP,
                visual_tokens.INSET,
                visual_tokens.GAP_GROUP,
                visual_tokens.SPACE_4,
            )
            header_row.setSpacing(visual_tokens.GAP_TIGHT)

            titles = QWidget(header)
            header_layout = QVBoxLayout(titles)
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(visual_tokens.SPACE_4)
            self.heading = QLabel(self.title, titles)
            self.heading.setObjectName("destinationTitle")
            self.heading.setWordWrap(True)
            header_layout.addWidget(self.heading)
            self.subheading = QLabel(self.subtitle, titles)
            self.subheading.setObjectName("secondary")
            self.subheading.setWordWrap(True)
            self.subheading.setVisible(bool(self.subtitle))
            header_layout.addWidget(self.subheading)
            header_row.addWidget(titles, 1)

            # Only Agent Chat carries a composer. Every other page offers one
            # compact action that returns there, so a goal field is never shown
            # twice and never before there is a project to act on.
            if type(self).show_new_task:
                self.new_task_button = make_button(
                    "New task",
                    "ghost",
                    accessible="Start a new task in Agent Chat",
                    tooltip="Go to Agent Chat to state a goal.",
                    on_click=lambda: self.host.focus_destination("chat"),
                )
                header_row.addWidget(self.new_task_button, 0, Qt.AlignmentFlag.AlignTop)
            outer.addWidget(header)

        content = QWidget(self)
        self.body = QVBoxLayout(content)
        # An embedded page sits inside its container's padding, so it adds none
        # of its own horizontally. Otherwise the page surface carries the one
        # 24 px padding every screen shares; the gap between surfaces is 16 px.
        side = visual_tokens.SPACE_0 if self.embedded else visual_tokens.SPACE_24
        top = visual_tokens.SPACE_0 if self.embedded else visual_tokens.SPACE_24
        self.body.setContentsMargins(side, top, side, visual_tokens.SPACE_24)
        self.body.setSpacing(visual_tokens.GAP_GROUP)

        # The persistent region. It is a child widget so a refresh can lift it
        # out and re-append it last without destroying its contents.
        self.hosted_widget = QWidget(content)
        self.hosted_body = QVBoxLayout(self.hosted_widget)
        self.hosted_body.setContentsMargins(0, 0, 0, 0)
        self.hosted_body.setSpacing(visual_tokens.GAP_GROUP)
        self._hosted_mounted = False

        if self.hosted:
            outer.addWidget(content, 1)
        else:
            scroll = QScrollArea(self)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(content)
            outer.addWidget(scroll, 1)

    # -- helpers ------------------------------------------------------------
    def add_state_view(self, kind: str, title: str, message: str = "") -> StateView:
        """Add a state view to the body and return it."""
        view = StateView(kind, title, message, self)
        self.body.addWidget(view)
        return view

    def new_state_view(self) -> StateView:
        """Add an empty state view the caller configures itself."""
        view = StateView("empty", "", "", self)
        self.body.addWidget(view)
        return view

    def mount(self, widget: QWidget, stretch: int = 1) -> None:
        """Mount a host-built widget into the persistent hosted region.

        The region is placed at the foot of the page, after whatever the
        destination renders, so the rendered summary always reads first.
        """
        self.hosted_body.addWidget(widget, stretch)
        self._hosted_mounted = True
        if self.body.indexOf(self.hosted_widget) == -1:
            self.body.addWidget(self.hosted_widget)

    def clear_body(self) -> None:
        """Detach everything in the refreshed body, keeping the hosted region.

        The hosted widget is removed from the layout but not destroyed, so the
        host's widgets keep their identity; :meth:`refresh` re-appends the
        region after the destination has rendered.
        """
        for index in range(self.body.count() - 1, -1, -1):
            item = self.body.itemAt(index)
            widget = item.widget()
            self.body.takeAt(index)
            if widget is not None and widget is not self.hosted_widget:
                widget.setParent(None)
                widget.deleteLater()

    def refresh(self) -> None:
        """Re-render the body, keep the hosted region at the foot, fill below.

        The trailing stretch is what makes every card *natural height*: surplus
        vertical space belongs to the page canvas, below the content, so a
        heading, its explanation and its action stay one compact group at the
        top instead of being flung apart by stretch.
        """
        self.clear_body()
        self.render()
        if self._hosted_mounted and self.body.indexOf(self.hosted_widget) == -1:
            self.body.addWidget(self.hosted_widget)
        if not self.hosted:
            self.body.addStretch(1)

    def render(self) -> None:
        """Render this destination's refreshed body.

        Subclasses implement this, adding widgets with :meth:`add_state_view`,
        :meth:`new_state_view` or ``self.body``. ``self.body`` is empty when it
        runs, and the hosted region is appended after it returns.
        """


__all__ = ["Destination"]
