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

    #: Shown inside Work, which supplies the title and the "New task" action.
    embedded = True
    title = "Review"
    subtitle = "Changed artifacts, evidence, missing proof, conflicts and acceptance coverage."

    def render(self) -> None:
        """Re-render the review.

        When the backend holds a persisted workflow, Review renders *that* — the
        projection it returns, with its own record ids, coverage and freshness.
        Only a capability the slice does not persist falls back to the local
        model, and the fallback says so.
        """
        persisted = self.host.persisted_review()
        if isinstance(persisted, dict):
            self._render_persisted(persisted)
            return

        bundle = self.workspace.review()

        if not bundle.is_reviewable:
            # Work owns the single "Start in Agent Chat" action for empty work,
            # so this view never duplicates it.
            view = self.new_state_view()
            view.set_state(
                "empty",
                "Nothing to review yet",
                "Review fills in once a plan's jobs report what they produced.",
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

    # -- persisted projection ----------------------------------------------
    def _render_persisted(self, review: dict) -> None:
        """Render the backend's review projection exactly as it returned it."""
        run_outcome = str(review.get("run_outcome") or "")
        if not run_outcome:
            view = self.new_state_view()
            view.set_state(
                "empty",
                "Nothing to review yet",
                "The plan is persisted. Review fills in once its scan job has run.",
            )
            return

        freshness = str(review.get("freshness") or "")
        summary = Card(
            "What is under review",
            str(review.get("goal") or "No goal recorded."),
            self,
        )
        summary.body.addWidget(
            self._line(
                f"Plan {review.get('plan_id')} · revision {review.get('plan_revision')}"
            )
        )
        summary.body.addWidget(self._line(f"Job {review.get('job_id')} · {review.get('job_state')}"))
        summary.body.addWidget(
            self._line(f"Run {review.get('run_id')} · {run_outcome} · freshness: {freshness}")
        )
        if review.get("run_reason"):
            summary.body.addWidget(self._line(str(review["run_reason"])))
        blocking = list(review.get("blocking") or [])
        for reason in blocking:
            summary.body.addWidget(self._line(f"• {reason}"))
        if not blocking:
            summary.body.addWidget(
                self._line(
                    "The evidence is current. Acknowledging records that you have seen "
                    "it; it does not adopt source and does not move a baseline."
                )
            )
        self.body.addWidget(summary)

        limitations = list(review.get("limitations") or [])
        if limitations:
            card = Card(
                "Limitations",
                "Reported by the scan, never converted into a clean bill of health.",
                self,
            )
            for item in limitations:
                card.body.addWidget(ListRow(str(item), "", "warning", "Preserved"))
            self.body.addWidget(card)

        evidence = list(review.get("evidence") or [])
        if evidence:
            card = Card("Evidence", "One row per code-owned check the scan recorded.", self)
            for item in evidence:
                tone = {
                    "satisfied": "verified",
                    "unsatisfied": "missing",
                    "unknown": "unverified",
                }.get(str(item.get("result")), "unverified")
                row = ListRow(
                    str(item.get("label") or item.get("predicate_id")),
                    str(item.get("limitation") or item.get("evidence_id") or ""),
                    tone,
                    str(item.get("result")),
                )
                card.body.addWidget(row)
            self.body.addWidget(card)

        coverage = list(review.get("coverage") or [])
        if coverage:
            covered = sum(1 for row in coverage if row.get("covered"))
            card = Card(
                "Acceptance coverage",
                f"{covered} of {len(coverage)} criteria have evidence.",
                self,
            )
            for row in coverage:
                tone = "verified" if row.get("covered") else "missing"
                detail = str(row.get("note") or "")
                if not row.get("supported"):
                    detail = detail or "No supported check establishes this requirement."
                card.body.addWidget(
                    ListRow(
                        str(row.get("label") or row.get("criterion_id")),
                        detail,
                        tone,
                        "Covered" if row.get("covered") else "Open",
                    )
                )
            self.body.addWidget(card)

        self._render_persisted_decision(review)

    def _render_persisted_decision(self, review: dict) -> None:
        """Offer exactly the decisions the persisted run supports."""
        decision = review.get("decision")
        card = Card("Decision", "Each decision records what it does, and nothing more.", self)
        if isinstance(decision, dict):
            card.body.addWidget(
                self._line(
                    f"{decision.get('outcome')} recorded by {decision.get('actor')} "
                    f"against run {decision.get('run_id')}."
                )
            )
            card.body.addWidget(
                self._line(
                    "Acknowledging the scan evidence adopts nothing and moves no baseline."
                )
            )
            self.body.addWidget(card)
            return

        blocking = list(review.get("blocking") or [])
        for choice in states.REVIEW_DECISIONS:
            enabled = choice != states.DECISION_APPROVE or not blocking
            card.body.addWidget(
                make_button(
                    states.decision_label(choice),
                    "primary" if choice == states.DECISION_APPROVE else "secondary",
                    accessible=states.decision_label(choice),
                    tooltip=states.decision_effect(choice),
                    enabled=enabled,
                    disabled_reason=blocking[0] if (blocking and not enabled) else "",
                    on_click=lambda _c=False, picked=choice: self.host.record_decision(picked),
                )
            )
        self.body.addWidget(card)

    def _line(self, text: str):
        from PySide6.QtWidgets import QLabel

        label = QLabel(text, self)
        label.setObjectName("secondary")
        label.setWordWrap(True)
        return label

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
