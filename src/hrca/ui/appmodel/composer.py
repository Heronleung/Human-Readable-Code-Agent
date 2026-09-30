"""A deterministic local plan composer.

The composer turns a stated goal into an **editable scaffold**, never an
answer. It has no model behind it: it assembles a decomposition from the
document-based workflow the product actually has, adding the optional jobs
whose signals it can point at in the goal text or the workspace context. Every
scaffold it produces names, in ``Plan.notes``, exactly which signals produced
it, so nothing about the proposal is unexplained.

It reads only its arguments. It reaches no network, no provider, no store and
no boundary, so composing a plan costs nothing and can happen before any
authority is granted. The result is labelled a proposal until a human edits
and confirms it.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from . import agents as _agents
from . import authority as _authority
from . import capabilities as _capabilities
from .context import ProjectContext
from .plan import Job, Plan, RISK_HIGH, RISK_LOW, RISK_MEDIUM, Subtask

# Signals that a goal concerns rule interpretation. Held as lowercase
# substrings; the match is reported verbatim in the plan notes.
_INTERPRET_SIGNALS: Tuple[str, ...] = (
    "interpret",
    "rule",
    "policy",
    "discount",
    "quotation",
    "quote",
    "price",
    "condition",
    "eligib",
)

# Signals that a goal concerns recalling earlier work.
_RECALL_SIGNALS: Tuple[str, ...] = (
    "resume",
    "recall",
    "earlier",
    "previous",
    "history",
    "last time",
    "blocked",
    "continue",
)

_RISK_FOR_AUTHORITY = {
    _authority.AUTHORITY_READ_ONLY: RISK_LOW,
    _authority.AUTHORITY_LOCAL_WRITE: RISK_LOW,
    _authority.AUTHORITY_PROVIDER_CALL: RISK_MEDIUM,
    _authority.AUTHORITY_CREDENTIAL_USE: RISK_MEDIUM,
    _authority.AUTHORITY_REPOSITORY_WRITE: RISK_HIGH,
    _authority.AUTHORITY_DESTRUCTIVE: RISK_HIGH,
}


def _signals(goal: str, signals: Sequence[str]) -> Tuple[str, ...]:
    """Return the signals from ``signals`` that appear in ``goal``."""
    lowered = goal.lower()
    return tuple(signal for signal in signals if signal in lowered)


def _job(
    key: str,
    title: str,
    capability_key: str,
    acceptance: Sequence[str],
    subtasks: Sequence[Subtask] = (),
    depends_on: Sequence[str] = (),
    baseline: str = "",
) -> Job:
    """Build a job, deriving its role, authority and risk from its capability."""
    capability = _capabilities.require(capability_key)
    role = _agents.role_for_capability(capability_key)
    role_key = role.key if role is not None else _agents.ROLE_COORDINATOR
    return Job(
        key=key,
        title=title,
        capability_key=capability_key,
        role_key=role_key,
        subtasks=tuple(subtasks),
        depends_on=tuple(depends_on),
        acceptance=tuple(acceptance),
        risk=_RISK_FOR_AUTHORITY.get(capability.authority, RISK_LOW),
        baseline=baseline or None,
    )


def compose_plan(goal: str, context: ProjectContext) -> Plan:
    """Return an editable plan scaffold for ``goal`` in ``context``.

    The scaffold is deterministic: the same goal and context always produce
    the same plan. It contains only jobs whose capability the desktop can
    actually use.
    """
    goal_text = goal.strip()
    notes: List[str] = [
        "This is a proposal assembled from your goal and the recorded project "
        "state. Edit it before confirming; nothing runs until you do.",
    ]

    jobs: List[Job] = []
    baseline = context.baseline

    if context.has_project:
        jobs.append(
            _job(
                "scan",
                "Scan the project's Python",
                "source.scan",
                (
                    "Every Python file under the root is parsed or recorded as a parse error.",
                    "The scan completes without changing any file.",
                ),
                subtasks=(
                    Subtask("scan.run", "Run the deterministic read-only scan"),
                    Subtask("scan.record", "Record the report and source evidence"),
                ),
                baseline=baseline,
            )
        )
        notes.append("Included a read-only scan because a project root is bound.")
    else:
        notes.append(
            "No project root is bound, so the plan proposes no scan. "
            "Open a project to include one."
        )

    if context.has_document:
        jobs.append(
            _job(
                "save",
                f"Save the requirement revision for {context.document_name or 'the open document'}",
                "document.save",
                (
                    "A new immutable revision is appended to the open document.",
                    "The stored content fingerprint is reported back.",
                ),
                subtasks=(
                    Subtask("save.edit", "Bring the requirement text up to date"),
                    Subtask("save.append", "Append the revision"),
                ),
                depends_on=("scan",) if context.has_project else (),
                baseline=baseline,
            )
        )
        jobs.append(
            _job(
                "preview",
                "Preview the bound candidate",
                "document.preview",
                (
                    "The candidate and accepted state bound to the saved revision are shown.",
                    "No candidate is adopted by this step.",
                ),
                depends_on=("save",),
                baseline=baseline,
            )
        )
        notes.append(
            "Included a save and a preview because a document is open; both stay "
            "inside PrimaAgent and adopt nothing."
        )
    else:
        notes.append(
            "No document is open, so the plan proposes no document work. "
            "Create a document to include it."
        )

    matched_interpret = _signals(goal_text, _INTERPRET_SIGNALS)
    if matched_interpret and context.has_document:
        jobs.append(
            _job(
                "prepare",
                "Prepare the rule-interpretation disclosure (offline)",
                "rule_delta.prepare",
                (
                    "The exact text that would leave the machine is shown in full.",
                    "Nothing is sent by this step.",
                ),
                depends_on=("save",),
                baseline=baseline,
            )
        )
        jobs.append(
            _job(
                "interpret",
                "Interpret the rule change (one confirmed provider request)",
                "rule_delta.interpret",
                (
                    "Exactly one request is made, with no retry and no fallback.",
                    "The result is a reviewable candidate, never an automatic change.",
                    "The usage the provider reported is recorded.",
                ),
                subtasks=(
                    Subtask("interpret.confirm", "Confirm the disclosure and send once"),
                    Subtask("interpret.record", "Record the candidate and its limitations"),
                ),
                depends_on=("prepare",),
                baseline=baseline,
            )
        )
        jobs.append(
            _job(
                "candidate",
                "Materialize the reviewable candidate",
                "candidate.create",
                (
                    "A candidate bound to the saved revision exists.",
                    "Adopting it remains a separate human decision.",
                ),
                depends_on=("interpret",),
                baseline=baseline,
            )
        )
        notes.append(
            "Added the interpretation jobs because the goal mentions "
            + ", ".join(repr(signal) for signal in matched_interpret)
            + ". The provider request stays behind its own confirmation."
        )

    matched_recall = _signals(goal_text, _RECALL_SIGNALS)
    if matched_recall:
        jobs.append(
            _job(
                "recall",
                "Reconstruct earlier work from Memory",
                "memory.resume",
                (
                    "Completed work, blockers and unverified claims are listed from the records.",
                    "Each claim links to the exact record that supports it.",
                ),
                baseline=baseline,
            )
        )
        notes.append(
            "Added a Memory resume because the goal mentions "
            + ", ".join(repr(signal) for signal in matched_recall)
            + "."
        )

    return Plan(
        goal=goal_text,
        jobs=tuple(jobs),
        confirmed=False,
        provenance="local scaffold",
        notes=tuple(notes),
    )


__all__ = ["compose_plan"]
