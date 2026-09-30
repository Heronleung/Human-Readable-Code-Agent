"""Resume: where the work stands, and the one action that moves it forward.

Resume is the landing destination. With no project bound it states the
product's purpose in one line and offers the single primary action — open a
project. With a project bound it reconstructs the accepted baseline, the
changes since work last happened, active, blocked and failed jobs, pending
decisions and unverified claims, and names one recommended next action.

Every item that makes a material claim carries a supporting record, and the
row's action opens it in the details drawer rather than asking the reader to
believe it.
"""

from __future__ import annotations



from ..appmodel import states
from ..components import Card, ListRow, make_button
from .base import Destination

PURPOSE = (
    "PrimaAgent helps you manage coding agents: state a goal, get an editable "
    "plan, watch the work honestly, review the evidence, and decide. It reads "
    "your repository; it changes nothing without your explicit confirmation."
)


class ResumeDestination(Destination):
    """The resume projection, and first-run guidance."""

    title = "Resume"
    subtitle = "Where the work stands, and the one next action."

    def render(self) -> None:
        """Re-render the resume from the workspace."""
        context = self.workspace.context

        if not context.has_project:
            self._render_first_run()
            return

        resume = self.workspace.resume()
        self._render_recommendation(resume.recommendation)
        for section in resume.sections:
            if section.is_empty:
                continue
            self._render_section(section)

        if all(section.is_empty for section in resume.sections):
            self.add_state_view(
                "empty",
                "Nothing recorded yet",
                "State a goal in Agent Chat to produce an editable plan.",
            )

    # -- first run ----------------------------------------------------------
    def _render_first_run(self) -> None:
        view = self.new_state_view()
        view.set_state("empty", "Start by opening a project", PURPOSE)
        view.add_action(
            make_button(
                "Open project",
                "primary",
                accessible="Open project",
                tooltip="Choose the repository this workspace will read.",
                on_click=self.host.open_project,
            )
        )
        self.body.addStretch(1)

    # -- recommendation -----------------------------------------------------
    def _render_recommendation(self, recommendation) -> None:
        card = Card("Recommended next action", recommendation.reason, self)
        action = make_button(
            recommendation.action,
            "primary",
            accessible=recommendation.action,
            tooltip=recommendation.reason,
            on_click=lambda: self._run_recommendation(recommendation),
        )
        card.body.addWidget(action)
        self.body.addWidget(card)

    def _run_recommendation(self, recommendation) -> None:
        action = recommendation.action.lower()
        if "open a project" in action:
            self.host.open_project()
        elif "goal" in action or "plan" in action or "confirm" in action:
            self.host.focus_destination("chat")
        elif "review" in action or "adopt" in action or "decide" in action:
            self.host.focus_destination("review")
        elif "blocker" in action or "dispatch" in action or "baseline" in action:
            self.host.focus_destination("jobs")
        else:
            self.host.focus_destination("chat")

    # -- sections -----------------------------------------------------------
    def _render_section(self, section) -> None:
        card = Card(section.title, section.note, self)
        for item in section.items:
            row = ListRow(item.label, item.detail, item.tone, self._tone_label(item.tone))
            if item.reference:
                row.add_action(
                    make_button(
                        "Open record",
                        "ghost",
                        accessible=f"Open the record behind {item.label}",
                        tooltip=f"Supporting record: {item.reference}",
                        on_click=lambda _checked=False, entry=item: self._open_record(entry),
                    )
                )
            card.body.addWidget(row)
        self.body.addWidget(card)

    @staticmethod
    def _tone_label(tone: str) -> str:
        return states.job_state_label(tone) if tone in states.JOB_STATES else states.claim_state_label(tone)

    def _open_record(self, item) -> None:
        self.host.show_details(
            item.label,
            (
                ("Detail", item.detail or "No further detail recorded."),
                ("Supporting record", item.reference),
            ),
        )


__all__ = ["ResumeDestination", "PURPOSE"]
