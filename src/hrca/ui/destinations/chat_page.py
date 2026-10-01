"""Agent Chat: the coordinating conversation, the Plan card and the composer.

Agent Chat is the **only** page with a composer. It is also where a plan is
confirmed, so it is the one place that shows a plan's full requested authority
— per job, and again as the plan's ceiling — immediately before the developer
confirms it. Every other surface shows the single concise authority indicator
in the context bar.

The composer is pinned to the foot of the page; the transcript scrolls above
it. Confirming a plan dispatches nothing: it only makes each job dispatchable,
still behind its own per-effect approval.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..appmodel import authority as _authority
from ..components import Card, Composer, make_button
from .base import Destination

NO_PLAN_TEXT = (
    "Describe a goal in the composer below. PrimaAgent proposes an editable "
    "plan — it sends nothing and runs nothing until you confirm."
)


class AgentChatDestination(Destination):
    """The transcript, the editable plan card, and the one composer."""

    hosted = True
    title = "Agent Chat"
    subtitle = "State a goal, shape the plan, and confirm what may run."
    #: This page owns the composer, so it carries no "New task" shortcut.
    show_new_task = False

    def __init__(self, workspace, host, parent: Optional[QWidget] = None) -> None:
        super().__init__(workspace, host, parent)
        self._included: dict = {}

        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scroll = QScrollArea(body)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transcript = QWidget(self._scroll)
        self._transcript_layout = QVBoxLayout(self._transcript)
        self._transcript_layout.setContentsMargins(0, 0, 0, 0)
        self._transcript_layout.setSpacing(12)
        self._scroll.setWidget(self._transcript)
        layout.addWidget(self._scroll, 1)

        self.composer = Composer(body)
        self.composer.submitted.connect(self.host.submit_goal)
        layout.addWidget(self.composer)

        self.mount(body)

    # -- rendering ----------------------------------------------------------
    def refresh(self) -> None:
        """Re-render the transcript and the plan card."""
        while self._transcript_layout.count():
            item = self._transcript_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        messages = self.workspace.messages
        if not messages:
            from ..components import StateView

            view = StateView("empty", "No conversation yet", NO_PLAN_TEXT, self._transcript)
            self._transcript_layout.addWidget(view)
        else:
            for message in messages:
                if message.is_plan:
                    self._transcript_layout.addWidget(self._plan_card())
                else:
                    self._transcript_layout.addWidget(self._message_bubble(message))

        self._transcript_layout.addStretch(1)
        self.composer.set_summary(self._composer_summary())
        self.composer.setVisible(self.workspace.context.has_project)

    def _composer_summary(self) -> str:
        context = self.workspace.context
        base = (
            f"Context: {context.document_text}"
            if context.has_project
            else "Context: no project open"
        )
        plan = self.workspace.plan
        if plan is not None:
            authority = _authority.describe_authority(plan.authority_ceiling)
            return (
                f"{base}   ·   Action: propose an editable plan   ·   "
                f"Authority if confirmed: {authority}"
            )
        return (
            f"{base}   ·   Action: propose an editable plan   ·   "
            "Authority: read only — nothing is sent"
        )

    # -- transcript ---------------------------------------------------------
    def _message_bubble(self, message) -> QWidget:
        bubble = QWidget(self._transcript)
        bubble.setObjectName("messageBubble")
        layout = QVBoxLayout(bubble)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        author = QLabel(f"{message.author_label}", bubble)
        author.setObjectName("messageAuthor")
        layout.addWidget(author)
        text = QLabel(message.text, bubble)
        text.setWordWrap(True)
        layout.addWidget(text)
        if message.detail:
            detail = QLabel(message.detail, bubble)
            detail.setObjectName("secondary")
            detail.setWordWrap(True)
            layout.addWidget(detail)
        return bubble

    # -- plan card ----------------------------------------------------------
    def _plan_card(self) -> QWidget:
        plan = self.workspace.plan
        if plan is None:
            return QWidget(self._transcript)

        card = Card("Plan", plan.goal or "No goal stated.", self._transcript)
        card.setObjectName("planCard")

        problems = self.workspace.plan_problems
        if problems:
            card.body.addWidget(self._note("This plan cannot be confirmed yet:"))
            for problem in problems:
                card.body.addWidget(self._note(f"• {problem}"))

        card.body.addWidget(
            self._note(
                "Widest authority this plan requests: "
                + _authority.describe_authority(plan.authority_ceiling)
                + ". Each job's authority is listed below."
            )
        )

        for job in plan.jobs:
            card.body.addWidget(self._job_editor(job, editable=not plan.confirmed))

        if plan.confirmed:
            card.body.addWidget(
                self._note(
                    "Confirmed. Each job is dispatched from Work, and a protected "
                    "effect is approved there, per job."
                )
            )
        else:
            card.body.addWidget(
                make_button(
                    "Confirm plan",
                    "primary",
                    accessible="Confirm the plan",
                    tooltip=(
                        "Confirming makes the jobs dispatchable. It runs nothing "
                        "and sends nothing by itself."
                    ),
                    enabled=not problems,
                    disabled_reason=problems[0] if problems else "",
                    on_click=self._confirm,
                )
            )
        return card

    def _job_editor(self, job, editable: bool) -> QWidget:
        container = QWidget(self._transcript)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        if editable:
            toggle = QCheckBox(job.title, container)
            toggle.setChecked(self._included.get(job.key, True))
            toggle.setAccessibleName(f"Include {job.title} in the plan")
            toggle.toggled.connect(
                lambda checked, key=job.key: self._included.__setitem__(key, checked)
            )
            layout.addWidget(toggle)
        else:
            layout.addWidget(QLabel(job.title, container))

        capability = job.capability
        meta = (
            f"  {job.role_label} · {capability.label if capability else job.capability_key} · "
            f"authority: {_authority.authority_label(job.authority)} · risk: {job.risk}"
        )
        meta_label = QLabel(meta, container)
        meta_label.setObjectName("secondary")
        meta_label.setWordWrap(True)
        layout.addWidget(meta_label)

        if job.acceptance:
            acceptance = QLabel("  Accepted when: " + "; ".join(job.acceptance), container)
            acceptance.setObjectName("secondary")
            acceptance.setWordWrap(True)
            layout.addWidget(acceptance)
        return container

    def _confirm(self) -> None:
        plan = self.workspace.plan
        if plan is None:
            return
        kept = tuple(job for job in plan.jobs if self._included.get(job.key, True))
        if len(kept) != len(plan.jobs):
            self.workspace.update_plan(replace(plan, jobs=kept))
        self.host.confirm_plan()

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("secondary")
        label.setWordWrap(True)
        return label


__all__ = ["AgentChatDestination", "NO_PLAN_TEXT"]
