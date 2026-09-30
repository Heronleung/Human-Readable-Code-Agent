"""The workspace: local presentation state and the transitions allowed on it.

:class:`Workspace` is the single place the chat-first surfaces read from and
write to. It holds the bound context, the transcript, the plan, the job
states and the recorded decision — and it is where every honest rule is
enforced rather than restated by each screen.

The rules it enforces, all as refusals with a stated reason:

* a plan cannot be confirmed while it fails validation;
* a job cannot be dispatched until the plan is confirmed, its dependencies
  have completed and its capability is available;
* a job whose authority is protected cannot be dispatched until that specific
  protected effect is confirmed for it;
* pause, resume, cancel and reassign are refused unless the job's capability
  actually supports them, so no control is offered that cannot be honoured;
* a job never completes itself — a completion arrives as a *reported claim*,
  and even then it only records the job's state; it changes no accepted
  baseline;
* when the accepted baseline moves, every non-terminal job planned against a
  different one becomes ``stale`` rather than being silently re-bound.

Nothing here writes to a store the boundary owns, and nothing here can advance
an accepted version.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Tuple

from . import agents as _agents
from . import authority as _authority
from . import capabilities as _capabilities
from . import conversation as _conversation
from . import review as _review
from . import states as _states
from .composer import compose_plan
from .context import ProjectContext
from .plan import Job, Plan, ready_jobs, validate_plan
from .resume import Resume, ResumeItem, build_resume


@dataclass(frozen=True)
class Outcome:
    """The result of a transition: whether it happened, and why it did not."""

    ok: bool
    reason: str = ""

    @property
    def refused(self) -> bool:
        """Whether the transition was refused."""
        return not self.ok


_OK = Outcome(True, "")


class Workspace:
    """The local presentation state for one desktop session."""

    def __init__(self, context: Optional[ProjectContext] = None) -> None:
        self._context = context or ProjectContext()
        self._messages: List[_conversation.ChatMessage] = []
        self._plan: Optional[Plan] = None
        self._decision = ""
        self._decision_actor = ""
        self._decision_note = ""
        self._message_seq = 0
        self._baseline_at_plan = ""

    # -- context ------------------------------------------------------------
    @property
    def context(self) -> ProjectContext:
        """Return the bound workspace context."""
        return self._context

    def set_context(self, context: ProjectContext) -> None:
        """Replace the bound context, staleness-checking any plan against it."""
        self._context = context
        self._apply_baseline_staleness()

    # -- transcript ---------------------------------------------------------
    @property
    def messages(self) -> Tuple[_conversation.ChatMessage, ...]:
        """Return the transcript, oldest first."""
        return tuple(self._messages)

    def _next_key(self) -> str:
        self._message_seq += 1
        return f"msg-{self._message_seq}"

    def post(
        self,
        author: str,
        kind: str,
        text: str,
        reference: str = "",
        detail: str = "",
    ) -> _conversation.ChatMessage:
        """Append a message to the transcript and return it."""
        message = _conversation.ChatMessage(
            key=self._next_key(),
            author=author,
            kind=kind,
            text=text,
            reference=reference,
            detail=detail,
        )
        self._messages.append(message)
        return message

    def notice(self, text: str, reference: str = "") -> _conversation.ChatMessage:
        """Append a workspace notice."""
        return self.post(
            _conversation.AUTHOR_SYSTEM, _conversation.KIND_NOTICE, text, reference
        )

    # -- plan ---------------------------------------------------------------
    @property
    def plan(self) -> Optional[Plan]:
        """Return the current plan, or ``None``."""
        return self._plan

    @property
    def plan_problems(self) -> Tuple[str, ...]:
        """Return the problems that currently block confirmation."""
        if self._plan is None:
            return ()
        return validate_plan(self._plan)

    def state_goal(self, goal: str) -> Outcome:
        """Record a stated goal and propose an editable plan scaffold."""
        text = goal.strip()
        if not text:
            return Outcome(False, "Enter a goal before proposing a plan.")
        self.post(_conversation.AUTHOR_DEVELOPER, _conversation.KIND_MESSAGE, text)
        return self.propose_plan(text)

    def propose_plan(self, goal: str) -> Outcome:
        """Compose a scaffold for ``goal`` and add its plan card to the chat."""
        text = goal.strip()
        if not text:
            return Outcome(False, "Enter a goal before proposing a plan.")
        plan = compose_plan(text, self._context)
        self._plan = plan
        self._baseline_at_plan = self._context.baseline
        self.post(
            _conversation.AUTHOR_COORDINATOR,
            _conversation.KIND_PLAN,
            "Here is a proposed plan. Edit it, then confirm it — nothing runs until you do.",
            reference="plan",
        )
        return _OK

    def update_plan(self, plan: Plan) -> Outcome:
        """Replace the current plan with an edited one."""
        if self._plan is None:
            return Outcome(False, "There is no plan to edit.")
        if plan.confirmed:
            return Outcome(False, "A confirmed plan cannot be edited; start a new goal instead.")
        self._plan = plan
        return _OK

    def confirm_plan(self) -> Outcome:
        """Confirm the plan, refusing while it fails validation."""
        plan = self._plan
        if plan is None:
            return Outcome(False, "There is no plan to confirm.")
        if plan.confirmed:
            return Outcome(False, "This plan is already confirmed.")
        problems = validate_plan(plan)
        if problems:
            return Outcome(False, problems[0])

        confirmed = plan.with_confirmation(plan.authority_ceiling)
        ready_keys = {job.key for job in ready_jobs(confirmed, self._context.baseline or None)}
        confirmed = replace(
            confirmed,
            jobs=tuple(
                job.with_state(_states.STATE_READY) if job.key in ready_keys else job
                for job in confirmed.jobs
            ),
        )
        self._plan = confirmed
        self.post(
            _conversation.AUTHOR_SYSTEM,
            _conversation.KIND_NOTICE,
            "Plan confirmed. Each job still needs the developer to dispatch it; a "
            "protected effect is confirmed separately, per job.",
            reference="plan",
        )
        return _OK

    # -- jobs ---------------------------------------------------------------
    def job(self, key: str) -> Optional[Job]:
        """Return the job with ``key``, or ``None``."""
        return self._plan.job(key) if self._plan is not None else None

    def _replace_job(self, key: str, job: Job) -> None:
        assert self._plan is not None
        self._plan = replace(
            self._plan,
            jobs=tuple(job if existing.key == key else existing for existing in self._plan.jobs),
        )

    def dispatch(self, key: str, confirmed_effects: Sequence[str] = ()) -> Outcome:
        """Start ``key``, refusing every condition the product cannot honour."""
        plan = self._plan
        if plan is None:
            return Outcome(False, "There is no plan.")
        if not plan.confirmed:
            return Outcome(False, "Confirm the plan before dispatching any job.")
        job = plan.job(key)
        if job is None:
            return Outcome(False, f"No job {key!r} exists in the plan.")
        if _states.is_terminal(job.state):
            return Outcome(False, f"{job.title} is {job.state_label.lower()} and cannot be dispatched.")
        if job.state == _states.STATE_RUNNING:
            return Outcome(False, f"{job.title} is already running.")

        capability = job.capability
        if capability is None or not capability.available:
            reason = capability.disabled_reason if capability else "Unknown capability."
            return Outcome(False, reason)

        completed = {item.key for item in plan.jobs if item.state == _states.STATE_COMPLETED}
        unmet = [dep for dep in job.depends_on if dep not in completed]
        if unmet:
            return Outcome(False, f"{job.title} is waiting on {', '.join(unmet)}.")

        if job.baseline and self._context.baseline and job.baseline != self._context.baseline:
            self._replace_job(key, job.with_state(_states.STATE_STALE))
            return Outcome(False, "The accepted baseline moved; re-plan this job.")

        if job.is_protected:
            required = _authority.effects_for(job.authority)
            missing = [effect for effect in required if effect not in confirmed_effects]
            if missing:
                named = ", ".join(_authority.effect_label(effect).lower() for effect in missing)
                return Outcome(
                    False,
                    f"{job.title} needs approval for {named} first.",
                )

        self._replace_job(key, job.with_state(_states.STATE_RUNNING))
        self.post(
            _conversation.AUTHOR_SYSTEM,
            _conversation.KIND_NOTICE,
            f"Dispatched {job.title} to {job.role_label}.",
            reference=capability.evidence_hint,
        )
        return _OK

    def report_job(self, key: str, state: str, blocker: str = "", detail: str = "") -> Outcome:
        """Record an observed job outcome. A reported completion is only a claim."""
        job = self.job(key)
        if job is None:
            return Outcome(False, f"No job {key!r} exists in the plan.")
        if state not in _states.JOB_STATES:
            return Outcome(False, f"{state!r} is not a known job state.")

        self._replace_job(key, job.with_state(state, blocker))
        capability = job.capability
        reference = capability.evidence_hint if capability else ""
        if state == _states.STATE_COMPLETED:
            text = (
                f"{job.title} reported completion. That is a claim: it changes no "
                "accepted state. Review the evidence to decide."
            )
        elif state == _states.STATE_BLOCKED:
            text = f"{job.title} is blocked. {blocker or 'No reason was recorded.'}"
        elif state == _states.STATE_UNKNOWN:
            text = (
                f"The outcome of {job.title} could not be observed. It is recorded as "
                "unknown rather than assumed to have succeeded."
            )
        else:
            text = f"{job.title} is now {_states.job_state_label(state).lower()}."
        self.post(_conversation.AUTHOR_SYSTEM, _conversation.KIND_EVIDENCE, text, reference, detail)
        return _OK

    def _control(self, key: str, attribute: str, phrase: str) -> Outcome:
        job = self.job(key)
        if job is None:
            return Outcome(False, f"No job {key!r} exists in the plan.")
        capability = job.capability
        if capability is None or not getattr(capability.control, attribute, False):
            return Outcome(
                False,
                f"{job.title} cannot be {phrase}: its capability does not support it.",
            )
        return _OK

    def supports(self, key: str, attribute: str) -> bool:
        """Return whether ``key``'s capability supports a lifecycle control.

        The Jobs surface asks this before rendering a control, so no button is
        shown that cannot be honoured.
        """
        job = self.job(key)
        if job is None or job.capability is None:
            return False
        return bool(getattr(job.capability.control, attribute, False))

    def pause(self, key: str) -> Outcome:
        """Pause ``key`` when the capability supports it."""
        allowed = self._control(key, "pause", "paused")
        if allowed.refused:
            return allowed
        job = self.job(key)
        assert job is not None
        if job.state != _states.STATE_RUNNING:
            return Outcome(False, "Only a running job can be paused.")
        self._replace_job(key, job.with_state(_states.STATE_PAUSED))
        self.notice(f"Paused {job.title}.")
        return _OK

    def resume_job(self, key: str) -> Outcome:
        """Resume ``key`` when the capability supports it."""
        allowed = self._control(key, "resume", "resumed")
        if allowed.refused:
            return allowed
        job = self.job(key)
        assert job is not None
        if job.state != _states.STATE_PAUSED:
            return Outcome(False, "Only a paused job can be resumed.")
        self._replace_job(key, job.with_state(_states.STATE_RUNNING))
        self.notice(f"Resumed {job.title}.")
        return _OK

    def cancel(self, key: str) -> Outcome:
        """Cancel ``key`` when the capability supports it."""
        allowed = self._control(key, "cancel", "cancelled")
        if allowed.refused:
            return allowed
        job = self.job(key)
        assert job is not None
        if _states.is_terminal(job.state):
            return Outcome(False, f"{job.title} is already {job.state_label.lower()}.")
        self._replace_job(key, job.with_state(_states.STATE_CANCELLED))
        self.notice(f"Cancelled {job.title}. Its recorded evidence is retained.")
        return _OK

    def reassign(self, key: str, role_key: str, confirmed_effects: Sequence[str] = ()) -> Outcome:
        """Reassign ``key`` to another role that owns the same capability."""
        allowed = self._control(key, "reassign", "reassigned")
        if allowed.refused:
            return allowed
        job = self.job(key)
        assert job is not None
        role = _agents.get(role_key)
        if role is None:
            return Outcome(False, f"No role {role_key!r} exists.")
        if not role.available:
            return Outcome(False, f"{role.label} is unavailable: {role.unavailable_reason}")
        if not _role_owns(role_key, job.capability_key):
            return Outcome(
                False,
                f"{role.label} does not own {job.capability_key!r}, so it cannot take this job.",
            )
        self._replace_job(key, replace(job, role_key=role_key))
        self.notice(f"Reassigned {job.title} to {role.label}.")
        return _OK

    # -- decisions ----------------------------------------------------------
    @property
    def decision(self) -> str:
        """Return the recorded review decision, or ``""``."""
        return self._decision

    def record_decision(self, decision: str, actor: str, note: str = "") -> Outcome:
        """Record a human decision. It never advances an accepted baseline."""
        if self._plan is None:
            return Outcome(False, "There is nothing to decide about yet.")
        if decision not in _states.REVIEW_DECISIONS:
            return Outcome(False, f"{decision!r} is not a known decision.")
        if not actor.strip():
            return Outcome(False, "A decision must name who made it.")
        if self._decision:
            return Outcome(False, "A decision is already recorded for this plan.")
        if decision == _states.DECISION_APPROVE:
            bundle = self.review()
            if not bundle.can_approve:
                reason = bundle.blocking_reasons[0] if bundle.blocking_reasons else "Evidence is incomplete."
                return Outcome(False, f"Cannot approve yet: {reason}")
        self._decision = decision
        self._decision_actor = actor.strip()
        self._decision_note = note.strip()
        self.post(
            _conversation.AUTHOR_SYSTEM,
            _conversation.KIND_DECISION,
            f"{actor.strip()} recorded “{_states.decision_label(decision)}”. {_states.decision_effect(decision)}",
            reference="review",
            detail=self._decision_note,
        )
        return _OK

    # -- projections --------------------------------------------------------
    def review(self) -> _review.ReviewBundle:
        """Return the review projection for the current plan."""
        return _review.build_review(
            self._plan, self._decision, self._decision_actor, self._decision_note
        )

    def resume(self, changes: Tuple[ResumeItem, ...] = ()) -> Resume:
        """Return the resume projection for the current state."""
        return build_resume(
            self._plan,
            self._context,
            self.review(),
            self.plan_problems,
            changes,
        )

    # -- baseline -----------------------------------------------------------
    def _apply_baseline_staleness(self) -> None:
        """Mark jobs stale when the accepted baseline no longer matches the plan's premise.

        The comparison is against the baseline the plan was *proposed* under —
        including the case where there was no baseline then and there is one
        now. A job in flight is left alone (it is already running under the
        premise it started with); a draft job is untouched because nothing has
        been promised about it yet.
        """
        plan = self._plan
        if plan is None:
            return
        current = self._context.baseline
        if current == self._baseline_at_plan:
            return
        self._baseline_at_plan = current

        moved: List[Job] = []
        changed = False
        for job in plan.jobs:
            if (
                not _states.is_terminal(job.state)
                and job.state not in (_states.STATE_DRAFT, _states.STATE_RUNNING)
            ):
                moved.append(job.with_state(_states.STATE_STALE))
                changed = True
            else:
                moved.append(job)
        if changed:
            self._plan = replace(plan, jobs=tuple(moved))
            self.notice(
                "The accepted baseline moved. Jobs planned against the previous "
                "baseline are marked stale rather than re-bound.",
                reference="resume",
            )


def _role_owns(role_key: str, capability_key: str) -> bool:
    """Return whether ``role_key`` owns ``capability_key``."""
    role = _agents.get(role_key)
    return role is not None and capability_key in role.capability_keys


__all__ = ["Outcome", "Workspace"]
