"""Jobs: every unit of planned work, at its place in the hierarchy.

A job row states its owner (the assigned role), the baseline it was planned
against, what it depends on, its activity and progress, any blocker, and its
terminal state — and nothing it cannot honestly support.

Lifecycle controls appear **only where the capability says they can be
honoured**. A job whose capability cannot be paused shows no pause control at
all, rather than a button that does nothing. Dispatch on a job with a
protected authority routes through the host, which confirms that specific
effect first.
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtWidgets import QWidget

from ..appmodel import authority as _authority
from ..appmodel import states
from ..components import Card, ListRow, make_button
from .base import Destination


def _depths(plan) -> Dict[str, int]:
    """Return each job's depth in the dependency hierarchy (roots are 0)."""
    by_key = {job.key: job for job in plan.jobs}
    depths: Dict[str, int] = {}

    def depth(key: str, guard: frozenset) -> int:
        if key in depths:
            return depths[key]
        if key in guard or key not in by_key:
            return 0
        parents = by_key[key].depends_on
        value = 0 if not parents else 1 + max(depth(parent, guard | {key}) for parent in parents)
        depths[key] = value
        return value

    for key in by_key:
        depth(key, frozenset())
    return depths


class JobsDestination(Destination):
    """The plan's jobs, their states and the controls they actually support."""

    #: Shown inside Work, which supplies the title and the "New task" action.
    embedded = True
    title = "Jobs"
    subtitle = "Hierarchy, owner, baseline, dependencies, activity and honest terminal state."

    def render(self) -> None:
        """Re-render the job list."""
        plan = self.workspace.plan

        if plan is None or not plan.jobs:
            # Work owns the single "Start in Agent Chat" action for an empty
            # queue, so this view never duplicates it.
            view = self.new_state_view()
            view.set_state(
                "empty",
                "No work planned yet",
                "State a goal in Agent Chat to produce an editable plan.",
            )
            return

        self._render_summary(plan)
        depths = _depths(plan)
        card = Card("Jobs", f"Overall state: {states.job_state_label(plan.state_rollup)}.", self)
        for job in plan.jobs:
            card.body.addWidget(self._job_row(job, depths.get(job.key, 0)))
        self.body.addWidget(card)
        self.body.addStretch(1)

    def _render_summary(self, plan) -> None:
        card = Card("Plan", plan.goal or "No goal stated.", self)
        confirmed = "confirmed" if plan.confirmed else "not yet confirmed"
        card.body.addWidget(
            self._line(
                f"{len(plan.jobs)} job(s) · {confirmed} · "
                f"widest authority: {_authority.authority_label(plan.authority_ceiling)} · "
                f"risk: {plan.risk}"
            )
        )
        if plan.protection_effects:
            named = ", ".join(
                _authority.effect_label(effect).lower() for effect in plan.protection_effects
            )
            card.body.addWidget(
                self._line(f"Protected effects this plan would need approved: {named}.")
            )
        if not plan.confirmed:
            card.body.addWidget(
                make_button(
                    "Confirm plan",
                    "primary",
                    accessible="Confirm the plan",
                    tooltip="Confirmation enables dispatch. It runs nothing by itself.",
                    on_click=self.host.confirm_plan,
                )
            )
        self.body.addWidget(card)

    def _job_row(self, job, depth: int) -> QWidget:
        indent = "    " * depth
        marker = "└ " if depth else ""
        title = f"{indent}{marker}{job.title}"
        meta_bits = [
            f"Owner: {job.role_label}",
            f"Capability: {job.capability.label if job.capability else job.capability_key}",
            f"Authority: {_authority.authority_label(job.authority)}",
            f"Baseline: {job.baseline or 'none recorded yet'}",
            f"Risk: {job.risk}",
        ]
        if job.depends_on:
            meta_bits.append(f"Depends on: {', '.join(job.depends_on)}")
        meta_bits.append(job.progress_text)
        if job.blocker:
            meta_bits.append(f"Blocker: {job.blocker}")

        row = ListRow(title, " · ".join(meta_bits), job.state, job.state_label, self)

        capability = job.capability
        if capability is not None and not capability.available:
            row.add_action(
                make_button(
                    "Unavailable",
                    "secondary",
                    accessible=f"{job.title} is unavailable",
                    enabled=False,
                    disabled_reason=capability.disabled_reason,
                )
            )
        elif job.state in (states.STATE_DRAFT, states.STATE_READY) and self.workspace.plan.confirmed:
            row.add_action(
                make_button(
                    "Dispatch",
                    "primary",
                    accessible=f"Dispatch {job.title}",
                    tooltip=(
                        "Requires confirming "
                        + ", ".join(
                            _authority.effect_label(effect).lower()
                            for effect in _authority.effects_for(job.authority)
                        )
                        if job.is_protected
                        else "Start this job."
                    ),
                    on_click=lambda _checked=False, key=job.key: self.host.dispatch_job(key),
                )
            )

        if self.workspace.supports(job.key, "pause") and job.state == states.STATE_RUNNING:
            row.add_action(
                make_button("Pause", "secondary", on_click=lambda _c=False, k=job.key: self.host.pause_job(k))
            )
        if self.workspace.supports(job.key, "resume") and job.state == states.STATE_PAUSED:
            row.add_action(
                make_button("Resume", "secondary", on_click=lambda _c=False, k=job.key: self.host.resume_job(k))
            )
        if (
            self.workspace.supports(job.key, "cancel")
            and not states.is_terminal(job.state)
            and job.state != states.STATE_DRAFT
        ):
            row.add_action(
                make_button(
                    "Cancel",
                    "danger",
                    accessible=f"Cancel {job.title}",
                    tooltip="Abandon this job. Its recorded evidence is retained.",
                    on_click=lambda _c=False, k=job.key: self.host.cancel_job(k),
                )
            )
        return row

    def _line(self, text: str):
        from PySide6.QtWidgets import QLabel

        label = QLabel(text, self)
        label.setObjectName("secondary")
        label.setWordWrap(True)
        return label


__all__ = ["JobsDestination"]
