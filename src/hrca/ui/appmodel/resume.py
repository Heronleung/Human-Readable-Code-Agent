"""The Resume projection: where the work stands, and one next action.

Resume answers, without reading a transcript: what the accepted baseline is,
what changed since work last happened, which jobs are active, failed or
blocked, what decisions are pending, which claims are unverified, and the one
action that moves things forward now.

Every item that makes a material claim carries a ``reference`` — a pointer to
the record that supports it — so a claim can be opened rather than believed.
The recommendation is derived from the workspace's actual state by a single
ordered rule table, so it is deterministic and its reason is always stated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import states as _states
from .context import ProjectContext
from .plan import Job, Plan, ready_jobs
from .review import ReviewBundle


@dataclass(frozen=True)
class ResumeItem:
    """One line of the resume, with the record that supports it."""

    key: str
    label: str
    detail: str = ""
    reference: str = ""
    tone: str = _states.STATE_READY


@dataclass(frozen=True)
class ResumeSection:
    """A titled group of resume items."""

    key: str
    title: str
    note: str
    items: Tuple[ResumeItem, ...]

    @property
    def is_empty(self) -> bool:
        """Whether the section holds nothing to show."""
        return not self.items


@dataclass(frozen=True)
class Recommendation:
    """The single next action, with its reason and its supporting record."""

    action: str
    reason: str
    reference: str = ""


@dataclass(frozen=True)
class Resume:
    """The whole resume: the sections and the one recommended next action."""

    sections: Tuple[ResumeSection, ...]
    recommendation: Recommendation

    def section(self, key: str) -> Optional[ResumeSection]:
        """Return the section with ``key``, or ``None``."""
        for candidate in self.sections:
            if candidate.key == key:
                return candidate
        return None


def _job_item(job: Job) -> ResumeItem:
    """Return a resume item for one job."""
    reference = job.capability.evidence_hint if job.capability else ""
    return ResumeItem(
        key=job.key,
        label=job.title,
        detail=job.blocker or job.progress_text,
        reference=reference,
        tone=job.state,
    )


def _recommend(
    plan: Optional[Plan],
    context: ProjectContext,
    review: ReviewBundle,
    problems: Tuple[str, ...],
) -> Recommendation:
    """Return the single next action, chosen by the first matching rule."""
    if not context.has_project:
        return Recommendation(
            action="Open a project",
            reason="No repository root is bound, so nothing can be read or planned yet.",
        )

    if plan is None or not plan.jobs:
        return Recommendation(
            action="State a goal in Agent Chat",
            reason="No plan exists yet. Describe what you want done to get an editable plan.",
        )

    if problems:
        return Recommendation(
            action="Fix the plan",
            reason=f"The plan cannot be confirmed: {problems[0]}",
            reference="plan",
        )

    if not plan.confirmed:
        return Recommendation(
            action="Review and confirm the plan",
            reason="The plan is a proposal. Nothing runs until you confirm it.",
            reference="plan",
        )

    blocked = [job for job in plan.jobs if job.state == _states.STATE_BLOCKED]
    if blocked:
        return Recommendation(
            action=f"Resolve the blocker on {blocked[0].title}",
            reason=blocked[0].blocker or "A job is blocked before it can proceed.",
            reference=blocked[0].capability.evidence_hint
            if blocked[0].capability
            else "jobs",
        )

    failed = [job for job in plan.jobs if job.state == _states.STATE_FAILED]
    if failed:
        return Recommendation(
            action=f"Decide what to do about {failed[0].title}",
            reason="A job failed. Its evidence is retained; decide whether to retry or change the plan.",
            reference="review",
        )

    stale = [job for job in plan.jobs if job.state == _states.STATE_STALE]
    if stale:
        return Recommendation(
            action="Re-plan against the new baseline",
            reason="The accepted baseline moved, so a job's binding is no longer valid.",
            reference="plan",
        )

    if review.is_reviewable and not review.has_decision and review.can_approve:
        return Recommendation(
            action="Review the evidence and record a decision",
            reason="Every job completed with evidence. Approving records your decision; it does not adopt.",
            reference="review",
        )

    if review.is_reviewable and not review.has_decision and not review.can_approve:
        return Recommendation(
            action="Review the outstanding evidence",
            reason=review.blocking_reasons[0]
            if review.blocking_reasons
            else "Some evidence is still outstanding before a decision.",
            reference="review",
        )

    if plan.confirmed and review.has_decision and not context.has_baseline:
        return Recommendation(
            action="Adopt the accepted version when you are ready",
            reason=(
                "Your decision is recorded. Moving the accepted baseline is the "
                "boundary's own separate, explicit step."
            ),
            reference="review",
        )

    ready = ready_jobs(plan, context.baseline or None)
    if ready:
        return Recommendation(
            action=f"Dispatch {ready[0].title}",
            reason=(
                "It is ready: its dependencies are done and its capability is available."
                + (
                    " It needs a protected effect confirmed first."
                    if ready[0].is_protected
                    else ""
                )
            ),
            reference=ready[0].capability.evidence_hint if ready[0].capability else "jobs",
        )

    return Recommendation(
        action="State the next goal",
        reason="Nothing is outstanding: every job is finished or terminal.",
    )


def build_resume(
    plan: Optional[Plan],
    context: ProjectContext,
    review: ReviewBundle,
    problems: Tuple[str, ...] = (),
    changes: Tuple[ResumeItem, ...] = (),
) -> Resume:
    """Return the resume projection for the current workspace state."""
    jobs: Tuple[Job, ...] = plan.jobs if plan is not None else ()

    active = tuple(
        _job_item(job)
        for job in jobs
        if job.state in (_states.STATE_RUNNING, _states.STATE_PAUSED, _states.STATE_READY)
    )
    blocked = tuple(_job_item(job) for job in jobs if job.state == _states.STATE_BLOCKED)
    failed = tuple(
        _job_item(job)
        for job in jobs
        if job.state in (_states.STATE_FAILED, _states.STATE_CANCELLED, _states.STATE_UNKNOWN)
    )
    stale = tuple(_job_item(job) for job in jobs if job.state == _states.STATE_STALE)

    pending = ()
    if review.is_reviewable and not review.has_decision:
        pending = (
            ResumeItem(
                key="review-decision",
                label="A review decision is waiting",
                detail="Approve, request changes, reject or escalate.",
                reference="review",
                tone=_states.STATE_READY,
            ),
        )

    unverified = tuple(
        ResumeItem(
            key=f"claim-{artifact.key}",
            label=artifact.title,
            detail=artifact.evidence or "No evidence recorded.",
            reference=artifact.capability_key,
            tone=artifact.evidence_state,
        )
        for artifact in review.artifacts
        if artifact.evidence_state
        in (_states.CLAIM_UNVERIFIED, _states.CLAIM_MISSING, _states.CLAIM_CONFLICTING)
    )

    sections = (
        ResumeSection(
            key="changes",
            title="Changes since last work",
            note="Read from the recorded documents and revisions; nothing is inferred.",
            items=changes,
        ),
        ResumeSection(
            key="active",
            title="Active work",
            note="Jobs that are ready, running or paused.",
            items=active,
        ),
        ResumeSection(
            key="blocked",
            title="Blocked",
            note="Recorded reasons a job cannot proceed.",
            items=blocked,
        ),
        ResumeSection(
            key="failed",
            title="Failed, cancelled or unknown",
            note="Terminal without success. The evidence is retained.",
            items=failed,
        ),
        ResumeSection(
            key="stale",
            title="Stale bindings",
            note="Planned against a baseline that has since moved.",
            items=stale,
        ),
        ResumeSection(
            key="pending",
            title="Pending decisions",
            note="Decisions only a human can make.",
            items=pending,
        ),
        ResumeSection(
            key="unverified",
            title="Unverified claims",
            note="Reported, never repaired: each claim says why it is unverified.",
            items=unverified,
        ),
    )

    return Resume(
        sections=sections,
        recommendation=_recommend(plan, context, review, problems),
    )


__all__ = [
    "ResumeItem",
    "ResumeSection",
    "Recommendation",
    "Resume",
    "build_resume",
]
