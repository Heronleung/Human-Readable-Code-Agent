"""Agent Chat: the coordinating conversation and the editable Plan card.

The destination shows the transcript — what the developer said, what the
coordinator proposed, what the workspace observed — and, at the position the
proposal was made, the live **Plan card**.

The Plan card is editable and explicit. Each job can be included or left out
before confirmation; the card states every job's owner, capability, requested
authority, risk and acceptance criteria, so the developer can see exactly what
they are about to authorise. Confirming the plan dispatches nothing: it only
makes each job dispatchable, still behind its own per-effect approval.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..appmodel import authority as _authority
from ..components import Card, make_button
from .base import Destination

NO_PLAN_TEXT = (
    "Describe a goal in the composer below. PrimaAgent proposes an editable "
    "plan — it sends nothing and runs nothing until you confirm."
)


class AgentChatDestination(Destination):
    """The transcript and the editable plan card."""

    title = "Agent Chat"
    subtitle = "State a goal, shape the plan, and confirm what may run."

    def __init__(self, workspace, host, parent=None) -> None:
        super().__init__(workspace, host, parent)
        self._included: dict = {}

    def render(self) -> None:
        """Re-render the transcript and the plan card."""
        messages = self.workspace.messages

        if not messages:
            self.add_state_view("empty", "No conversation yet", NO_PLAN_TEXT)
            self.body.addStretch(1)
            return

        for message in messages:
            if message.is_plan:
                self._render_plan_card()
            else:
                self.body.addWidget(self._message_bubble(message))
        self.body.addStretch(1)

    # -- transcript ---------------------------------------------------------
    def _message_bubble(self, message) -> QWidget:
        bubble = QWidget(self)
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
    def _render_plan_card(self) -> None:
        plan = self.workspace.plan
        if plan is None:
            return

        card = Card("Plan", plan.goal or "No goal stated.", self)
        card.setObjectName("planCard")

        problems = self.workspace.plan_problems
        if problems:
            card.body.addWidget(self._note("This plan cannot be confirmed yet:"))
            for problem in problems:
                card.body.addWidget(self._note(f"• {problem}"))

        for job in plan.jobs:
            card.body.addWidget(self._job_editor(job, editable=not plan.confirmed))

        if plan.notes:
            for note in plan.notes:
                card.body.addWidget(self._note(note))

        if plan.confirmed:
            card.body.addWidget(
                self._note(
                    "Confirmed. Each job is dispatched from Jobs, and a protected "
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
        self.body.addWidget(card)

    def _job_editor(self, job, editable: bool) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(container)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        if editable:
            toggle = QCheckBox(job.title, header)
            toggle.setChecked(self._included.get(job.key, True))
            toggle.setAccessibleName(f"Include {job.title} in the plan")
            toggle.toggled.connect(
                lambda checked, key=job.key: self._included.__setitem__(key, checked)
            )
            header_layout.addWidget(toggle)
        else:
            title = QLabel(job.title, header)
            header_layout.addWidget(title)

        capability = job.capability
        meta = (
            f"  {job.role_label} · {capability.label if capability else job.capability_key} · "
            f"authority: {_authority.authority_label(job.authority)} · risk: {job.risk}"
        )
        meta_label = QLabel(meta, header)
        meta_label.setObjectName("secondary")
        meta_label.setWordWrap(True)
        header_layout.addWidget(meta_label)

        if job.acceptance:
            acceptance = QLabel(
                "  Accepted when: " + "; ".join(job.acceptance), header
            )
            acceptance.setObjectName("secondary")
            acceptance.setWordWrap(True)
            header_layout.addWidget(acceptance)

        layout.addWidget(header)
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
