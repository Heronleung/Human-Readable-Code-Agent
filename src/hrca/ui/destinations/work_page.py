"""Work: the contextual grouping of Jobs, Agents and Review.

Work keeps the three oversight destinations reachable without presenting them
as three more top-level choices. It opens on **Jobs** — the active job or the
queue — and reveals its siblings only when they have something to say:

* **Agents** appears once a plan assigns work to a role. Before that, a
  capability catalogue is background reading, not a decision;
* **Review** appears once there is an artifact or evidence to review. Before
  that, its tabs are empty internals.

With no plan at all, Work is a single calm state with one action: start in
Agent Chat. Nothing here is deleted — the views are the same ones the rail
used to present, deferred until they are relevant.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from hrca.core import visual_tokens

from ..appmodel import states
from ..components import make_button
from .base import Destination


class WorkDestination(Destination):
    """Jobs, Agents and Review, revealed by relevance."""

    hosted = True
    title = "Work"
    subtitle = "The plan's jobs, the roles it assigns, and the evidence awaiting a decision."

    def __init__(self, workspace, host, parent: Optional[QWidget] = None) -> None:
        super().__init__(workspace, host, parent)
        self._views: List[Tuple[str, str, Destination]] = []
        self._buttons = {}
        self._stack = QStackedWidget(self)

        self._nav = QWidget(self)
        nav_layout = QHBoxLayout(self._nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(visual_tokens.SPACE_4)
        self._nav_layout = nav_layout

        # The empty state sits beside the stack, not inside it: a stacked
        # layout forces its page to fill, which would draw an oversized
        # outlined panel around three compact lines. Here its surface is
        # natural height and the surplus is page canvas.
        self._empty = self._build_empty_state()

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(visual_tokens.GAP_TIGHT)
        layout.addWidget(self._nav)
        layout.addWidget(self._empty)
        # Exactly one of these two consumes the surplus, and which one it is
        # depends on the state: the spacer below the empty panel, or the stack
        # when there is work to show. Both greedy at once would split the page.
        self._tail = QWidget(container)
        self._tail.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self._tail, 1)
        layout.addWidget(self._stack, 1)
        self.mount(container)

    # -- construction -------------------------------------------------------
    def _build_empty_state(self) -> QWidget:
        from ..components import StateView

        view = StateView(
            "empty",
            "Nothing in progress",
            "State a goal in Agent Chat to produce an editable plan. Jobs, agents "
            "and review appear here once there is work to oversee.",
            self,
        )
        view.add_action(
            make_button(
                "Start in Agent Chat",
                "primary",
                accessible="Start in Agent Chat",
                tooltip="Describe what you want done to get an editable plan.",
                on_click=lambda: self.host.focus_destination("chat"),
            )
        )
        return view

    def register_view(self, key: str, label: str, page: Destination) -> None:
        """Add one contextual view to Work."""
        button = QPushButton(label, self._nav)
        button.setObjectName("workViewButton")
        button.setCheckable(True)
        button.setAccessibleName(label)
        button.setToolTip(f"Show {label}")
        button.clicked.connect(lambda _checked=False, name=key: self.select_view(name))
        self._nav_layout.addWidget(button)
        self._buttons[key] = button
        self._views.append((key, label, page))
        self._stack.addWidget(page)

    # -- rendering ----------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        """Reveal the views that have something to show, and refresh them."""
        plan = self.workspace.plan

        has_plan = plan is not None and bool(plan.jobs)
        assigns_roles = has_plan and any(job.role_key for job in plan.jobs)
        # Review is about *evidence*, not intent: a job that has not left the
        # queue has produced nothing to review, so its row would be a row of
        # "nothing yet". It appears once a job has an outcome to look at.
        has_evidence = has_plan and any(
            job.state not in (states.STATE_DRAFT, states.STATE_READY)
            for job in plan.jobs
        )

        # A persisted workflow is a plan too, and after a desktop restart it is
        # the *only* one: the local model is empty while the backend still holds
        # the plan, its job and its evidence. Without this, reopening the app
        # would hide Work's views on a project that plainly has work in it.
        persisted = self.host.persisted_plan()
        if isinstance(persisted, dict):
            review = self.host.persisted_review() or {}
            has_plan = True
            assigns_roles = True
            has_evidence = bool(review.get("run_outcome"))

        relevant = {
            "jobs": has_plan,
            "agents": assigns_roles,
            "review": has_evidence,
        }

        for key, _label, page in self._views:
            visible = relevant.get(key, False)
            self._buttons[key].setVisible(visible)
            if visible:
                page.refresh()

        if not has_plan:
            self._empty.setVisible(True)
            self._tail.setVisible(True)
            self._stack.setVisible(False)
            self._nav.setVisible(False)
            self._subtitle_for(None)
            return

        self._empty.setVisible(False)
        self._tail.setVisible(False)
        self._stack.setVisible(True)
        self._nav.setVisible(True)
        current = self._current_key()
        if current is None or not relevant.get(current, False):
            current = "jobs"
        self.select_view(current, refresh=False)

    def _current_key(self) -> Optional[str]:
        widget = self._stack.currentWidget()
        for key, _label, page in self._views:
            if page is widget:
                return key
        return None

    def select_view(self, key: str, refresh: bool = True) -> None:
        """Show one contextual view."""
        for name, _label, page in self._views:
            if name != key:
                continue
            if refresh:
                page.refresh()
            self._stack.setCurrentWidget(page)
            for button_key, button in self._buttons.items():
                button.setChecked(button_key == key)
            self._subtitle_for(key)
            return

    def _subtitle_for(self, key: Optional[str]) -> None:
        if key is None:
            self.subheading.setText(
                "Nothing in progress. State a goal in Agent Chat to begin."
            )
            return
        if key == "review":
            recorded = self.workspace.review().has_decision
            self.subheading.setText(
                "Evidence, missing proof and the decision you recorded."
                if recorded
                else "Evidence, missing proof and the decision awaiting you."
            )
            return
        self.subheading.setText(
            {
                "jobs": "The plan's jobs, their state and the controls they support.",
                "agents": "The roles this plan assigns work to, and what each may touch.",
            }.get(key, self.subtitle)
        )

    # -- routing ------------------------------------------------------------
    def select_on_open(self) -> None:
        """Choose the most useful view for the current state."""
        review = self.workspace.review()
        if review.is_reviewable and not review.has_decision and review.can_approve:
            self.select_view("review")
            return
        plan = self.workspace.plan
        if plan is not None:
            rollup = plan.state_rollup
            if rollup in (states.STATE_BLOCKED, states.STATE_FAILED, states.STATE_STALE):
                self.select_view("jobs")
                return
        self.select_view("jobs")


__all__ = ["WorkDestination"]
