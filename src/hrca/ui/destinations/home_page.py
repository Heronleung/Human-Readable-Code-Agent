"""Home: where the work stands, and the one next action.

Home answers three questions and nothing else:

* **Continue** — the single recommended next action, with its reason;
* **Needs attention** — a blocking risk, a failed or blocked job, a pending
  approval or an unverified claim. This section is never collapsed away and is
  never hidden, whatever else is on the page: progressive disclosure reduces
  irrelevant controls, not safety information;
* **Recent changes** — what moved in the recorded documents and baseline.

Everything else — the raw Developer Memory reader with its Documents, Search,
Resume and Corrections views — is reached through one secondary **Project
history** entry rather than being shown as Home's content. The records stay
exactly as reachable as before, one deliberate step away.

With no project bound, Home is the first-use page: what the product is for,
any projects already opened this session, and exactly one primary action.
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

#: Resume sections that always belong in Needs attention. This is the safety
#: rule: a blocking risk, a failed or blocked job, a pending approval or an
#: unverified claim is never hidden by the progressive disclosure, whatever
#: else is on the page. It is keyed on the section rather than on the item's
#: tone because a pending approval is not a "bad" state — it is simply
#: something only a human can clear, and it must not be missed.
ATTENTION_SECTIONS = frozenset(
    {"blocked", "failed", "stale", "pending", "unverified"}
)

ATTENTION_TONES = frozenset(
    {
        states.STATE_BLOCKED,
        states.STATE_FAILED,
        states.STATE_CANCELLED,
        states.STATE_STALE,
        states.STATE_UNKNOWN,
        states.CLAIM_UNVERIFIED,
        states.CLAIM_MISSING,
        states.CLAIM_CONFLICTING,
    }
)


class HomeDestination(Destination):
    """Continue, needs attention, recent changes — and Project history behind one door."""

    title = "Home"
    subtitle = "Where the work stands, and the one next action."

    def render(self) -> None:
        """Render Home.

        When the backend holds a persisted workflow, Home renders *its* resume
        projection — so a desktop restart reconstructs the same records, ids,
        freshness and next action from the store rather than from an empty
        local model. Without one, Home shows the local first-run or workspace
        state as before.
        """
        persisted = self.host.persisted_resume()
        if isinstance(persisted, dict):
            self._render_persisted(persisted)
            return

        context = self.workspace.context
        if not context.has_project:
            self._render_first_run()
            return

        resume = self.workspace.resume()
        self._render_continue(resume.recommendation)
        self._render_attention(resume)
        self._render_recent_changes(resume)
        self._render_history_entry()

    # -- persisted projection ----------------------------------------------
    def _render_persisted(self, resume: dict) -> None:
        """Render the backend's resume projection exactly as it returned it."""
        action = str(resume.get("next_action") or "")
        card = Card("Continue", str(resume.get("next_action_reason") or ""), self)
        if action:
            card.body.addWidget(
                make_button(
                    action,
                    "primary",
                    accessible=action,
                    tooltip=str(resume.get("next_action_reason") or ""),
                    on_click=lambda: self._run_persisted_action(resume),
                )
            )
        self.body.addWidget(card)

        blockers = list(resume.get("blockers") or [])
        unverified = list(resume.get("unverified") or [])
        if blockers or unverified:
            attention = Card(
                "Needs attention",
                "Blocking risks, unresolved executions and uncovered requirements. "
                "These are never hidden.",
                self,
            )
            for reason in blockers:
                attention.body.addWidget(ListRow(str(reason), "Blocking", "blocked", "Blocked"))
            for item in unverified:
                attention.body.addWidget(
                    ListRow(
                        str(item.get("label") or ""),
                        str(item.get("detail") or ""),
                        "missing",
                        "Open",
                    )
                )
            self.body.addWidget(attention)

        changes = list(resume.get("changes") or [])
        if changes:
            card = Card(
                "Recorded facts",
                "Read from the persisted records; nothing is inferred.",
                self,
            )
            card.body.addWidget(
                ListRow(
                    "Accepted baseline",
                    "No accepted baseline is recorded for this project.",
                    "neutral",
                    "Unknown",
                )
                if not resume.get("accepted_baseline_ref")
                else ListRow(
                    "Accepted baseline",
                    str(resume.get("accepted_baseline_ref")),
                    "neutral",
                    "Recorded",
                )
            )
            for item in changes:
                card.body.addWidget(
                    ListRow(
                        str(item.get("label") or ""),
                        str(item.get("detail") or ""),
                        "neutral",
                        "Recorded",
                    )
                )
            card.body.addWidget(
                ListRow(
                    "Freshness",
                    f"The bound source is {resume.get('freshness')}.",
                    "neutral",
                    str(resume.get("freshness") or ""),
                )
            )
            self.body.addWidget(card)

        self._render_history_entry()

    def _run_persisted_action(self, resume: dict) -> None:
        """Send the developer to the view the persisted next action names."""
        action = str(resume.get("next_action") or "").lower()
        if "confirm" in action or "goal" in action:
            self.host.focus_destination("chat")
        elif "review" in action or "execution" in action:
            self.host.focus_destination("review")
        elif "scan" in action or "job" in action:
            self.host.focus_destination("jobs")
        else:
            self.host.focus_destination("review")

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

        recent = self.workspace.recent_projects
        if recent:
            card = Card("Recent projects", "Opened in this session.", self)
            for root in recent:
                row = ListRow(root, "", "ready", "Recent")
                row.add_action(
                    make_button(
                        "Open",
                        "secondary",
                        accessible=f"Open {root}",
                        tooltip=f"Bind the workspace to {root}.",
                        on_click=lambda _c=False, path=root: self.host.open_recent_project(path),
                    )
                )
                card.body.addWidget(row)
            self.body.addWidget(card)

    # -- sections -----------------------------------------------------------
    def _render_continue(self, recommendation) -> None:
        card = Card("Continue", recommendation.reason, self)
        card.body.addWidget(
            make_button(
                recommendation.action,
                "primary",
                accessible=recommendation.action,
                tooltip=recommendation.reason,
                on_click=lambda: self._run_recommendation(recommendation),
            )
        )
        self.body.addWidget(card)

    def _render_attention(self, resume) -> None:
        items = []
        for section in resume.sections:
            for item in section.items:
                if section.key in ATTENTION_SECTIONS or item.tone in ATTENTION_TONES:
                    items.append(item)
        if not items:
            return
        card = Card(
            "Needs attention",
            "Blocking risks, failed or blocked work, pending approvals and "
            "unverified claims. These are never hidden.",
            self,
        )
        for item in items:
            row = ListRow(item.label, item.detail, item.tone, self._tone_label(item.tone))
            if item.reference:
                row.add_action(
                    make_button(
                        "Open record",
                        "ghost",
                        accessible=f"Open the record behind {item.label}",
                        tooltip=f"Supporting record: {item.reference}",
                        on_click=lambda _c=False, entry=item: self._open_record(entry),
                    )
                )
            card.body.addWidget(row)
        self.body.addWidget(card)

    def _render_recent_changes(self, resume) -> None:
        changes = resume.section("changes")
        if changes is None or changes.is_empty:
            return
        card = Card(
            "Recent changes",
            "Read from the recorded documents and revisions; nothing is inferred.",
            self,
        )
        for item in changes.items:
            card.body.addWidget(ListRow(item.label, item.detail, item.tone, "Recorded"))
        self.body.addWidget(card)

    def _render_history_entry(self) -> None:
        # A compact secondary row, not a second panel: the recorded runs are
        # reference material, and giving them a full surface next to Continue
        # reads as a second, emptier call to action.
        row = ListRow(
            "Project history",
            "The recorded runs behind this resume: their documents, search, "
            "resume and human corrections.",
            "neutral",
            "Read-only",
        )
        row.add_action(
            make_button(
                "Open",
                "secondary",
                accessible="Open Project history",
                tooltip="Read the recorded runs and their human corrections.",
                on_click=lambda: self.host.focus_destination("history"),
            )
        )
        self.body.addWidget(row)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _tone_label(tone: str) -> str:
        if tone in states.JOB_STATES:
            return states.job_state_label(tone)
        return states.claim_state_label(tone)

    def _run_recommendation(self, recommendation) -> None:
        action = recommendation.action.lower()
        if "open a project" in action:
            self.host.open_project()
        elif "goal" in action or "plan" in action or "confirm" in action:
            self.host.focus_destination("chat")
        elif "review" in action or "adopt" in action or "decide" in action:
            self.host.focus_destination("review")
        elif "blocker" in action or "dispatch" in action or "baseline" in action:
            self.host.focus_destination("work")
        else:
            self.host.focus_destination("chat")

    def _open_record(self, item) -> None:
        self.host.show_details(
            item.label,
            (
                ("Detail", item.detail or "No further detail recorded."),
                ("Supporting record", item.reference),
            ),
        )


__all__ = ["HomeDestination", "PURPOSE"]
