"""Review: the changed artifacts, the evidence, and the four decisions.

Review presents what a plan produced and what proves it: each artifact with
its evidence state, the proof that is missing, any conflict, and how far the
stated acceptance criteria are covered. Then it separates the four decisions —
approve, request changes, reject, escalate — each with its consequence named.

Approval is disabled, with the reason stated, while the evidence is
incomplete. Even when it is enabled, approving records a named human's
decision about the evidence; it adopts nothing, writes nothing and moves no
accepted baseline. That is the boundary's own separate action.
"""

from __future__ import annotations

from ..appmodel import states
from ..components import Card, ListRow, make_button
from .base import Destination


class ReviewDestination(Destination):
    """Evidence, coverage and the four review decisions."""

    title = "Review"
    subtitle = "Changed artifacts, evidence, missing proof, conflicts and acceptance coverage."

    def render(self) -> None:
        """Re-render the review from the workspace."""
        bundle = self.workspace.review()

        if not bundle.is_reviewable:
            view = self.new_state_view()
            view.set_state(
                "empty",
                "Nothing to review yet",
                "Review fills in once a plan's jobs report what they produced.",
            )
            view.add_action(
                make_button(
                    "Go to Agent Chat",
                    "primary",
                    accessible="Go to Agent Chat",
                    on_click=lambda: self.host.focus_destination("chat"),
                )
            )
            return

        self._render_summary(bundle)
        self._render_artifacts(bundle)
        if bundle.missing_proof:
            self._render_list_card(
                "Missing proof",
                "Reported, never repaired: each item says what is absent and why.",
                [(item, "missing") for item in bundle.missing_proof],
            )
        if bundle.conflicts:
            self._render_list_card(
                "Conflicts",
                "A conflict blocks approval until it is resolved.",
                [(item, "conflicting") for item in bundle.conflicts],
            )
        self._render_acceptance(bundle)
        self._render_decisions(bundle)
        self.body.addStretch(1)

    # -- sections -----------------------------------------------------------
    def _render_summary(self, bundle) -> None:
        card = Card("What is under review", bundle.goal or "No goal recorded.", self)
        card.body.addWidget(self._note(bundle.coverage_text))
        if bundle.blocking_reasons:
            card.body.addWidget(self._note("Approval is blocked by:"))
            for reason in bundle.blocking_reasons:
                card.body.addWidget(self._note(f"• {reason}"))
        else:
            card.body.addWidget(
                self._note(
                    "The evidence is complete. Approving records your decision; it "
                    "does not adopt, merge or deploy."
                )
            )
        self.body.addWidget(card)

    def _render_artifacts(self, bundle) -> None:
        card = Card("Changed artifacts and evidence", "One row per job's contribution.", self)
        for artifact in bundle.artifacts:
            row = ListRow(
                artifact.title,
                f"{artifact.capability_label} · {artifact.state_label} · {artifact.evidence}",
                artifact.job_state,
                artifact.state_label,
            )
            row.add_action(
                make_button(
                    "Evidence",
                    "ghost",
                    accessible=f"Open the evidence for {artifact.title}",
                    on_click=lambda _c=False, item=artifact: self._open_evidence(item),
                )
            )
            card.body.addWidget(row)
        self.body.addWidget(card)

    def _render_list_card(self, title: str, note: str, items) -> None:
        card = Card(title, note, self)
        for text, tone in items:
            card.body.addWidget(ListRow(text, "", tone, states.claim_state_label(tone)))
        self.body.addWidget(card)

    def _render_acceptance(self, bundle) -> None:
        card = Card("Acceptance coverage", bundle.coverage_text, self)
        for row in bundle.acceptance:
            tone = "verified" if row.covered else "missing"
            card.body.addWidget(ListRow(row.criterion, row.note, tone, "Covered" if row.covered else "Not covered"))
        self.body.addWidget(card)

    def _render_decisions(self, bundle) -> None:
        card = Card("Decision", "Each decision records what it does, and nothing more.", self)
        if bundle.has_decision:
            card.body.addWidget(
                self._note(
                    f"{states.decision_label(bundle.decision)} recorded by "
                    f"{bundle.decision_actor}. {states.decision_effect(bundle.decision)}"
                )
            )
            if bundle.decision_note:
                card.body.addWidget(self._note(f"Note: {bundle.decision_note}"))
            self.body.addWidget(card)
            return

        blocking = bundle.blocking_reasons
        approve_reason = blocking[0] if blocking else ""
        for decision in states.REVIEW_DECISIONS:
            enabled = decision != states.DECISION_APPROVE or bundle.can_approve
            card.body.addWidget(
                make_button(
                    states.decision_label(decision),
                    "primary" if decision == states.DECISION_APPROVE else "secondary",
                    accessible=states.decision_label(decision),
                    tooltip=states.decision_effect(decision),
                    enabled=enabled,
                    disabled_reason=(
                        f"Cannot approve yet: {approve_reason}" if not enabled else ""
                    ),
                    on_click=lambda _c=False, choice=decision: self.host.record_decision(choice),
                )
            )
        self.body.addWidget(card)

    # -- helpers ------------------------------------------------------------
    def _open_evidence(self, artifact) -> None:
        self.host.show_details(
            artifact.title,
            (
                ("Capability", artifact.capability_label),
                ("Job state", artifact.state_label),
                ("Evidence state", artifact.evidence_label),
                ("Evidence", artifact.evidence or "None recorded."),
            ),
        )

    @staticmethod
    def _note(text: str):
        from PySide6.QtWidgets import QLabel

        label = QLabel(text)
        label.setObjectName("secondary")
        label.setWordWrap(True)
        return label


__all__ = ["ReviewDestination"]
