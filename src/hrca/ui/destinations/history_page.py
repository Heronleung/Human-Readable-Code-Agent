"""Project history: the recorded runs behind the resume.

The raw Developer Memory reader — its Documents, Search, Resume and
Corrections views — used to sit on the landing page, where it presented an
operator surface to someone who had not yet opened a project. It now lives
here, one deliberate step behind Home's **Project history** entry.

Nothing is removed or made read-only that was not already: the reader is the
same read-only projection over stored runs, and a correction is still
append-only and still changes what is shown, never what was recorded.
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..components import make_button
from .base import Destination


class HistoryDestination(Destination):
    """The read-only Developer Memory reader."""

    hosted = True
    title = "Project history"
    subtitle = (
        "Read-only views over the stored runs: their documents, the bounded "
        "search, the composed resume and the append-only human corrections."
    )

    def __init__(self, workspace, host, parent: QWidget | None = None) -> None:
        super().__init__(workspace, host, parent)
        self.body.addWidget(
            make_button(
                "Back to Home",
                "ghost",
                accessible="Back to Home",
                tooltip="Return to the workspace summary.",
                on_click=lambda: self.host.focus_destination("home"),
            )
        )


__all__ = ["HistoryDestination"]
