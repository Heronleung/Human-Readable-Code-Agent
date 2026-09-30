"""The Review projection: what changed, what proves it, and what is missing.

Review is presented as it exists, not as the work wishes it were. The
projection lists the artifacts a plan produced, the evidence each carries, the
proof that is absent, any conflict, and how far the stated acceptance criteria
are actually covered. It then offers four decisions — approve, request
changes, reject, escalate — with their consequences named.

The governing rule lives here in one place: **a job's completion is a claim, and
no claim advances acceptance.** :func:`advances_acceptance` returns ``False``
unconditionally, because recording a decision here never adopts a version,
writes a repository or changes a baseline. Adoption is the boundary's own
separate, explicit action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import states as _states
from .plan import Job, Plan

# Evidence state per artifact, reusing the claim vocabulary so there is one
# honest-state language across Review and Resume.
EVIDENCE_PRESENT = _states.CLAIM_VERIFIED
EVIDENCE_UNVERIFIED = _states.CLAIM_UNVERIFIED
EVIDENCE_MISSING = _states.CLAIM_MISSING
EVIDENCE_CONFLICTING = _states.CLAIM_CONFLICTING


@dataclass(frozen=True)
class ArtifactReview:
    """One job's contribution to a review."""

    key: str
    title: str
    capability_key: str
    capability_label: str
    job_state: str
    evidence_state: str
    evidence: str
    missing: Tuple[str, ...] = ()

    @property
    def state_label(self) -> str:
        """Return the human label of the job state."""
        return _states.job_state_label(self.job_state)

    @property
    def evidence_label(self) -> str:
        """Return the human label of the evidence state."""
        return _states.claim_state_label(self.evidence_state)


@dataclass(frozen=True)
class AcceptanceRow:
    """One acceptance criterion and whether a proof currently covers it."""

    job_key: str
    criterion: str
    covered: bool
    note: str = ""


@dataclass(frozen=True)
class ReviewBundle:
    """Everything a developer needs to decide, and nothing that decides for them."""

    goal: str
    artifacts: Tuple[ArtifactReview, ...] = ()
    conflicts: Tuple[str, ...] = ()
    missing_proof: Tuple[str, ...] = ()
    acceptance: Tuple[AcceptanceRow, ...] = ()
    decision: str = ""
    decision_actor: str = ""
    decision_note: str = ""

    @property
    def has_decision(self) -> bool:
        """Whether a decision has been recorded."""
        return bool(self.decision)

    @property
    def coverage(self) -> Tuple[int, int]:
        """Return ``(covered, total)`` acceptance criteria."""
        total = len(self.acceptance)
        covered = sum(1 for row in self.acceptance if row.covered)
        return covered, total

    @property
    def coverage_text(self) -> str:
        """Return a plain-language coverage phrase."""
        covered, total = self.coverage
        if total == 0:
            return "No acceptance criteria are recorded."
        return f"{covered} of {total} acceptance criteria have evidence."

    @property
    def is_reviewable(self) -> bool:
        """Whether there is anything to decide about yet."""
        return bool(self.artifacts)

    @property
    def blocking_reasons(self) -> Tuple[str, ...]:
        """Return why a plan's work is not yet ready to approve.

        Conflicts and missing proof block; an unverified artifact blocks. An
        empty tuple means the evidence is complete enough to approve — which
        still only records a decision.
        """
        reasons: List[str] = []
        reasons.extend(self.conflicts)
        reasons.extend(f"Missing proof: {item}" for item in self.missing_proof)
        for artifact in self.artifacts:
            if artifact.evidence_state == EVIDENCE_UNVERIFIED:
                reasons.append(f"{artifact.title}: {artifact.evidence or 'no evidence recorded'}")
        return tuple(reasons)

    @property
    def can_approve(self) -> bool:
        """Whether approval is currently supported by the evidence."""
        return self.is_reviewable and not self.blocking_reasons


def _evidence_for(job: Job) -> Tuple[str, str, Tuple[str, ...]]:
    """Return ``(evidence_state, evidence_text, missing)`` for one job."""
    capability = job.capability
    hint = capability.evidence_hint if capability is not None else ""

    if job.state == _states.STATE_COMPLETED:
        return EVIDENCE_PRESENT, hint or "The job reported completion.", ()
    if job.state in (_states.STATE_FAILED, _states.STATE_CANCELLED):
        return EVIDENCE_MISSING, hint or "No evidence was produced.", (
            f"{job.title} did not complete.",
        )
    if job.state == _states.STATE_BLOCKED:
        return EVIDENCE_MISSING, hint or "The job is blocked.", (
            job.blocker or f"{job.title} is blocked.",
        )
    if job.state == _states.STATE_STALE:
        return EVIDENCE_CONFLICTING, hint, (
            f"{job.title} was planned against a different baseline.",
        )
    if job.state == _states.STATE_UNKNOWN:
        return EVIDENCE_UNVERIFIED, hint, (
            f"{job.title}'s outcome could not be observed.",
        )
    return EVIDENCE_MISSING, hint or "The job has not run.", (
        f"{job.title} has not produced evidence yet.",
    )


def build_review(plan: Optional[Plan], decision: str = "", actor: str = "", note: str = "") -> ReviewBundle:
    """Return the review projection for ``plan``.

    ``plan`` may be ``None`` when nothing has been planned; the bundle is then
    empty and not reviewable, which the Review destination states plainly
    rather than showing an empty form.
    """
    if plan is None:
        return ReviewBundle(goal="", decision=decision, decision_actor=actor, decision_note=note)

    artifacts: List[ArtifactReview] = []
    missing: List[str] = []
    conflicts: List[str] = []
    acceptance: List[AcceptanceRow] = []

    for job in plan.jobs:
        capability = job.capability
        state, evidence, job_missing = _evidence_for(job)
        artifacts.append(
            ArtifactReview(
                key=job.key,
                title=job.title,
                capability_key=job.capability_key,
                capability_label=capability.label if capability else job.capability_key,
                job_state=job.state,
                evidence_state=state,
                evidence=evidence,
                missing=job_missing,
            )
        )
        for item in job_missing:
            if item not in missing:
                missing.append(item)
        if state == EVIDENCE_CONFLICTING:
            conflicts.append(
                f"{job.title}: the recorded baseline changed after this job was planned."
            )
        for criterion in job.acceptance:
            covered = job.state == _states.STATE_COMPLETED and state == EVIDENCE_PRESENT
            acceptance.append(
                AcceptanceRow(
                    job_key=job.key,
                    criterion=criterion,
                    covered=covered,
                    note="" if covered else "Waiting on " + job.title + ".",
                )
            )

    return ReviewBundle(
        goal=plan.goal,
        artifacts=tuple(artifacts),
        conflicts=tuple(conflicts),
        missing_proof=tuple(missing),
        acceptance=tuple(acceptance),
        decision=decision,
        decision_actor=actor,
        decision_note=note,
    )


def advances_acceptance(decision: str) -> bool:
    """Return whether a recorded decision advances an accepted baseline.

    Always ``False``. Approving is a human record of acceptance of the
    *evidence*; the accepted baseline moves only through the boundary's own
    explicit adoption action, which this model never calls.
    """
    return False


__all__ = [
    "EVIDENCE_PRESENT",
    "EVIDENCE_UNVERIFIED",
    "EVIDENCE_MISSING",
    "EVIDENCE_CONFLICTING",
    "ArtifactReview",
    "AcceptanceRow",
    "ReviewBundle",
    "build_review",
    "advances_acceptance",
]
