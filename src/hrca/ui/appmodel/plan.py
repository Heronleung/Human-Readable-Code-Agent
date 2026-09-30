"""Goal, Plan, Job and Subtask records with their validation.

A :class:`Plan` is what Agent Chat produces from a stated goal: an ordered set
of jobs, each with subtasks, dependencies, acceptance criteria, a risk level,
an assigned role and a requested authority. It is editable until it is
confirmed, and nothing it contains dispatches anything.

:func:`validate_plan` is the honest gate. It refuses a plan whose jobs name an
unknown capability or role, whose dependencies are missing or cyclic, whose
role does not own the capability it is assigned, or whose job carries no
acceptance criteria — because a job with nothing to accept cannot be reviewed.
A plan that fails validation cannot be confirmed, so a malformed plan is caught
before it can reach a dispatch decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import agents as _agents
from . import authority as _authority
from . import capabilities as _capabilities
from . import states as _states

# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

RISK_LEVELS: Tuple[str, ...] = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

RISK_LABELS: Dict[str, str] = {
    RISK_LOW: "Low",
    RISK_MEDIUM: "Medium",
    RISK_HIGH: "High",
}

RISK_RANK: Dict[str, int] = {name: index for index, name in enumerate(RISK_LEVELS)}


def risk_label(risk: str) -> str:
    """Return the human label for a risk level."""
    return RISK_LABELS.get(risk, risk)


def max_risk(levels: Iterable[str]) -> str:
    """Return the highest risk in ``levels`` (``low`` when empty)."""
    materialized = list(levels)
    if not materialized:
        return RISK_LOW
    return max(materialized, key=lambda item: RISK_RANK.get(item, 99))


@dataclass
class Subtask:
    """One step inside a job, with its own honest state."""

    key: str
    title: str
    state: str = _states.STATE_DRAFT
    detail: str = ""

    def with_state(self, state: str, detail: str = "") -> "Subtask":
        """Return a copy in ``state``, optionally with a new detail line."""
        return replace(self, state=state, detail=detail)

    @property
    def is_terminal(self) -> bool:
        """Whether this subtask can no longer advance on its own."""
        return _states.is_terminal(self.state)


@dataclass
class Job:
    """One unit of planned work, bound to a capability and a role.

    ``baseline`` records the accepted baseline the job was planned against.
    When the workspace's baseline no longer matches it the job is shown
    ``stale`` rather than silently re-bound — a moved baseline invalidates the
    binding instead of quietly repairing it.
    """

    key: str
    title: str
    capability_key: str
    role_key: str
    subtasks: Tuple[Subtask, ...] = ()
    depends_on: Tuple[str, ...] = ()
    acceptance: Tuple[str, ...] = ()
    risk: str = RISK_LOW
    state: str = _states.STATE_DRAFT
    blocker: str = ""
    baseline: Optional[str] = None

    # -- derived views ------------------------------------------------------
    @property
    def capability(self) -> Optional[_capabilities.Capability]:
        """Return this job's capability, or ``None`` if the key is unknown."""
        return _capabilities.get(self.capability_key)

    @property
    def authority(self) -> str:
        """Return the authority this job requests (read-only if unknown)."""
        capability = self.capability
        return capability.authority if capability is not None else _authority.AUTHORITY_READ_ONLY

    @property
    def is_protected(self) -> bool:
        """Whether dispatching this job needs its own explicit confirmation."""
        return _authority.is_protected(self.authority)

    @property
    def role_label(self) -> str:
        """Return the human label of the assigned role."""
        return _agents.role_label(self.role_key)

    @property
    def is_terminal(self) -> bool:
        """Whether this job can no longer advance on its own."""
        return _states.is_terminal(self.state)

    @property
    def state_label(self) -> str:
        """Return the human label of this job's state."""
        return _states.job_state_label(self.state)

    @property
    def progress(self) -> Tuple[int, int]:
        """Return ``(finished, total)`` subtasks for a simple progress read."""
        total = len(self.subtasks)
        finished = sum(1 for subtask in self.subtasks if subtask.is_terminal)
        return finished, total

    @property
    def progress_text(self) -> str:
        """Return a plain-language progress phrase."""
        finished, total = self.progress
        if total == 0:
            return "No subtasks recorded."
        return f"{finished} of {total} subtasks finished."

    def with_state(self, state: str, blocker: str = "") -> "Job":
        """Return a copy in ``state``, carrying ``blocker`` when blocked."""
        return replace(self, state=state, blocker=blocker)


@dataclass
class Plan:
    """An editable goal decomposition. Confirming it dispatches nothing."""

    goal: str
    jobs: Tuple[Job, ...] = ()
    confirmed: bool = False
    confirmed_authority: str = _authority.AUTHORITY_READ_ONLY
    provenance: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)

    # -- derived views ------------------------------------------------------
    def job(self, key: str) -> Optional[Job]:
        """Return the job with ``key``, or ``None``."""
        for candidate in self.jobs:
            if candidate.key == key:
                return candidate
        return None

    @property
    def job_keys(self) -> Tuple[str, ...]:
        """Return every job key, in plan order."""
        return tuple(job.key for job in self.jobs)

    @property
    def authority_ceiling(self) -> str:
        """Return the widest authority any job in the plan requests."""
        widest = _authority.AUTHORITY_READ_ONLY
        for job in self.jobs:
            if _authority.authority_rank(job.authority) > _authority.authority_rank(widest):
                widest = job.authority
        return widest

    @property
    def protection_effects(self) -> Tuple[str, ...]:
        """Return each protected effect the plan would need confirmed."""
        effects: List[str] = []
        for job in self.jobs:
            for effect in _authority.effects_for(job.authority):
                if effect not in effects:
                    effects.append(effect)
        return tuple(effects)

    @property
    def risk(self) -> str:
        """Return the highest risk any job carries."""
        return max_risk(job.risk for job in self.jobs)

    @property
    def state_rollup(self) -> str:
        """Return the most severe state any job is in."""
        return _states.rollup_job_state(job.state for job in self.jobs)

    def dependents(self, key: str) -> Tuple[Job, ...]:
        """Return the jobs that declare a dependency on ``key``."""
        return tuple(job for job in self.jobs if key in job.depends_on)

    def with_confirmation(self, authority: str) -> "Plan":
        """Return a confirmed copy carrying the authority it was confirmed at."""
        return replace(self, confirmed=True, confirmed_authority=authority)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _cycle_jobs(jobs: Sequence[Job]) -> Tuple[str, ...]:
    """Return the keys that take part in a dependency cycle."""
    by_key = {job.key: job for job in jobs}
    visiting: set = set()
    visited: set = set()
    cyclic: List[str] = []

    def walk(key: str) -> None:
        if key in visited or key not in by_key:
            return
        if key in visiting:
            cyclic.append(key)
            return
        visiting.add(key)
        for dependency in by_key[key].depends_on:
            walk(dependency)
        visiting.discard(key)
        visited.add(key)

    for job in jobs:
        walk(job.key)
    return tuple(dict.fromkeys(cyclic))


def validate_plan(plan: Plan) -> Tuple[str, ...]:
    """Return every problem that makes ``plan`` unfit to confirm.

    An empty tuple means the plan is coherent: every job names a real,
    available capability whose owning role is the one assigned, every
    dependency resolves without a cycle, and every job states what would be
    accepted.
    """
    problems: List[str] = []

    if not plan.goal.strip():
        problems.append("The plan has no stated goal.")

    if not plan.jobs:
        problems.append("The plan contains no jobs.")

    seen: set = set()
    for job in plan.jobs:
        if job.key in seen:
            problems.append(f"Two jobs share the key {job.key!r}.")
        seen.add(job.key)

    known_keys = {job.key for job in plan.jobs}

    for job in plan.jobs:
        capability = job.capability
        if capability is None:
            problems.append(
                f"Job {job.key!r} names unknown capability {job.capability_key!r}."
            )
        else:
            if not capability.available:
                problems.append(
                    f"Job {job.key!r} needs {capability.label!r}, which the desktop "
                    f"cannot use: {capability.disabled_reason}"
                )
            owner = _agents.role_for_capability(capability.key)
            if owner is None:
                problems.append(
                    f"Job {job.key!r} names capability {capability.key!r}, which no "
                    "role owns; it cannot be assigned."
                )
            elif owner.key != job.role_key:
                problems.append(
                    f"Job {job.key!r} is assigned to {_agents.role_label(job.role_key)!r} "
                    f"but {capability.key!r} belongs to {owner.label!r}."
                )

        if _agents.get(job.role_key) is None:
            problems.append(f"Job {job.key!r} names unknown role {job.role_key!r}.")
        elif not _agents.require(job.role_key).available:
            problems.append(
                f"Job {job.key!r} is assigned to {_agents.role_label(job.role_key)!r}, "
                f"which is unavailable: {_agents.require(job.role_key).unavailable_reason}"
            )

        if not job.acceptance:
            problems.append(f"Job {job.key!r} states no acceptance criteria.")

        if job.risk not in RISK_LEVELS:
            problems.append(f"Job {job.key!r} carries unknown risk {job.risk!r}.")

        for dependency in job.depends_on:
            if dependency == job.key:
                problems.append(f"Job {job.key!r} depends on itself.")
            elif dependency not in known_keys:
                problems.append(
                    f"Job {job.key!r} depends on {dependency!r}, which is not in the plan."
                )

    for key in _cycle_jobs(plan.jobs):
        problems.append(f"Job {key!r} takes part in a dependency cycle.")

    return tuple(dict.fromkeys(problems))


def plan_is_dispatchable(plan: Plan) -> Tuple[bool, str]:
    """Return whether ``plan`` may be confirmed, and the reason when it may not.

    Confirmation is refused while any validation problem stands, so an
    incoherent plan can never reach a dispatch decision.
    """
    problems = validate_plan(plan)
    if problems:
        return False, problems[0]
    if plan.confirmed:
        return False, "This plan is already confirmed."
    return True, ""


def ready_jobs(plan: Plan, baseline: Optional[str] = None) -> Tuple[Job, ...]:
    """Return the jobs that could be dispatched now.

    A job is ready when its capability is available, every dependency has
    completed, and it is not already running or terminal. ``baseline`` is
    checked when the caller knows the workspace's current baseline: a job
    planned against a different one is stale, not ready.
    """
    completed = {
        job.key for job in plan.jobs if job.state == _states.STATE_COMPLETED
    }
    ready: List[Job] = []
    for job in plan.jobs:
        if job.state not in (_states.STATE_DRAFT, _states.STATE_READY):
            continue
        capability = job.capability
        if capability is None or not capability.available:
            continue
        if not set(job.depends_on).issubset(completed):
            continue
        if baseline is not None and job.baseline not in (None, baseline):
            continue
        ready.append(job)
    return tuple(ready)


__all__ = [
    "RISK_LOW",
    "RISK_MEDIUM",
    "RISK_HIGH",
    "RISK_LEVELS",
    "RISK_LABELS",
    "RISK_RANK",
    "risk_label",
    "max_risk",
    "Subtask",
    "Job",
    "Plan",
    "validate_plan",
    "plan_is_dispatchable",
    "ready_jobs",
]
