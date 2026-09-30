"""Bounded state vocabularies for the chat-first workspace.

Every state a job, a plan, an agent or a review can be in is named here, with
four things attached to each name:

* a **label** a developer reads;
* a **glyph** so the state is legible without colour (the accessibility rule:
  colour is never the only cue);
* a **severity**, so a hierarchy can roll up to the most severe state it
  contains instead of inventing a summary; and
* a **terminal** flag, so "can this advance?" is answered by the vocabulary
  rather than by each caller.

An outcome is never inferred. ``unknown`` exists precisely so that "we could
not observe it" is a first-class answer instead of a silent success, and
``stale`` exists so that a job whose baseline moved is shown rather than
quietly re-bound.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple

# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------
STATE_DRAFT = "draft"
STATE_READY = "ready"
STATE_BLOCKED = "blocked"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_STALE = "stale"
STATE_UNKNOWN = "unknown"

JOB_STATES: Tuple[str, ...] = (
    STATE_DRAFT,
    STATE_READY,
    STATE_RUNNING,
    STATE_PAUSED,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_STALE,
    STATE_UNKNOWN,
)

JOB_STATE_LABELS: Dict[str, str] = {
    STATE_DRAFT: "Draft",
    STATE_READY: "Ready",
    STATE_RUNNING: "Running",
    STATE_PAUSED: "Paused",
    STATE_BLOCKED: "Blocked",
    STATE_COMPLETED: "Completed",
    STATE_FAILED: "Failed",
    STATE_CANCELLED: "Cancelled",
    STATE_STALE: "Stale",
    STATE_UNKNOWN: "Unknown",
}

# A short text cue pairs with every state so meaning survives without colour.
JOB_STATE_GLYPHS: Dict[str, str] = {
    STATE_DRAFT: "○",       # ○ empty circle
    STATE_READY: "●",       # ● filled circle
    STATE_RUNNING: "◔",     # ◔ partial
    STATE_PAUSED: "‖",      # ‖ pause bars
    STATE_BLOCKED: "⚠",     # ⚠ warning
    STATE_COMPLETED: "✓",   # ✓
    STATE_FAILED: "✗",      # ✗
    STATE_CANCELLED: "⊘",   # ⊘
    STATE_STALE: "↻",       # ↻ moved
    STATE_UNKNOWN: "?",          # ?
}

# A state that cannot advance on its own. ``stale`` is terminal until the job
# is explicitly re-bound to the new baseline; ``unknown`` never advances
# without a fresh observation.
TERMINAL_JOB_STATES = frozenset(
    {STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED, STATE_STALE, STATE_UNKNOWN}
)

# Severity 0 is the calmest. A rollup returns the highest severity present, so
# an "overall" state is never a new invention — it is one of the real states.
_JOB_SEVERITY: Dict[str, int] = {
    STATE_DRAFT: 0,
    STATE_READY: 1,
    STATE_RUNNING: 2,
    STATE_PAUSED: 3,
    STATE_COMPLETED: 1,
    STATE_STALE: 4,
    STATE_BLOCKED: 5,
    STATE_UNKNOWN: 6,
    STATE_CANCELLED: 7,
    STATE_FAILED: 8,
}

# When several states share the top severity, the earliest of these wins, so a
# rollup is deterministic rather than dependent on iteration order.
_SEVERITY_TIEBREAK: Tuple[str, ...] = (
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_UNKNOWN,
    STATE_BLOCKED,
    STATE_STALE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_READY,
    STATE_COMPLETED,
    STATE_DRAFT,
)


def job_state_label(state: str) -> str:
    """Return the human label for ``state`` (or the raw state if unknown)."""
    return JOB_STATE_LABELS.get(state, state)


def job_state_glyph(state: str) -> str:
    """Return the non-colour cue for ``state``."""
    return JOB_STATE_GLYPHS.get(state, "?")


def is_terminal(state: str) -> bool:
    """Return whether ``state`` cannot advance without an explicit re-bind."""
    return state in TERMINAL_JOB_STATES


def job_state_severity(state: str) -> int:
    """Return the severity rank of ``state`` (unknown states rank highest)."""
    return _JOB_SEVERITY.get(state, 99)


def rollup_job_state(states: Iterable[str]) -> str:
    """Return the most severe state in ``states``.

    An empty input rolls up to ``draft`` — nothing has started yet. This
    mirrors the accepted validation contract: the overall state is the most
    severe any member reached, never a separate summary value.
    """
    materialized = list(states)
    if not materialized:
        return STATE_DRAFT
    worst = max(job_state_severity(state) for state in materialized)
    candidates = [state for state in materialized if job_state_severity(state) == worst]
    if len(set(candidates)) == 1:
        return candidates[0]
    for preferred in _SEVERITY_TIEBREAK:
        if preferred in candidates:
            return preferred
    return candidates[0]


# ---------------------------------------------------------------------------
# Evidence and claim states (used by Review and Resume)
# ---------------------------------------------------------------------------
CLAIM_VERIFIED = "verified"
CLAIM_UNVERIFIED = "unverified"
CLAIM_MISSING = "missing"
CLAIM_CONFLICTING = "conflicting"

CLAIM_STATES: Tuple[str, ...] = (
    CLAIM_VERIFIED,
    CLAIM_UNVERIFIED,
    CLAIM_MISSING,
    CLAIM_CONFLICTING,
)

CLAIM_STATE_LABELS: Dict[str, str] = {
    CLAIM_VERIFIED: "Verified",
    CLAIM_UNVERIFIED: "Unverified",
    CLAIM_MISSING: "Missing",
    CLAIM_CONFLICTING: "Conflicting",
}

CLAIM_STATE_GLYPHS: Dict[str, str] = {
    CLAIM_VERIFIED: "✓",
    CLAIM_UNVERIFIED: "?",
    CLAIM_MISSING: "—",
    CLAIM_CONFLICTING: "⚠",
}


def claim_state_label(state: str) -> str:
    """Return the human label for a claim state."""
    return CLAIM_STATE_LABELS.get(state, state)


# ---------------------------------------------------------------------------
# Review decisions
# ---------------------------------------------------------------------------
DECISION_APPROVE = "approve"
DECISION_REQUEST_CHANGES = "request_changes"
DECISION_REJECT = "reject"
DECISION_ESCALATE = "escalate"

REVIEW_DECISIONS: Tuple[str, ...] = (
    DECISION_APPROVE,
    DECISION_REQUEST_CHANGES,
    DECISION_REJECT,
    DECISION_ESCALATE,
)

REVIEW_DECISION_LABELS: Dict[str, str] = {
    DECISION_APPROVE: "Approve",
    DECISION_REQUEST_CHANGES: "Request changes",
    DECISION_REJECT: "Reject",
    DECISION_ESCALATE: "Escalate",
}

REVIEW_DECISION_EFFECTS: Dict[str, str] = {
    DECISION_APPROVE: (
        "Records that a named human accepts this evidence. It does not adopt, "
        "merge, deploy or write anything."
    ),
    DECISION_REQUEST_CHANGES: (
        "Returns the work for revision and records why. Nothing is accepted."
    ),
    DECISION_REJECT: (
        "Declines the work as presented. The evidence is retained, not deleted."
    ),
    DECISION_ESCALATE: (
        "Hands the decision to someone else and records the open question."
    ),
}


def decision_label(decision: str) -> str:
    """Return the human label for a review decision."""
    return REVIEW_DECISION_LABELS.get(decision, decision)


def decision_effect(decision: str) -> str:
    """Return the stated consequence of a review decision."""
    return REVIEW_DECISION_EFFECTS.get(decision, "")


__all__ = [
    "STATE_DRAFT",
    "STATE_READY",
    "STATE_BLOCKED",
    "STATE_RUNNING",
    "STATE_PAUSED",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_CANCELLED",
    "STATE_STALE",
    "STATE_UNKNOWN",
    "JOB_STATES",
    "JOB_STATE_LABELS",
    "JOB_STATE_GLYPHS",
    "TERMINAL_JOB_STATES",
    "job_state_label",
    "job_state_glyph",
    "is_terminal",
    "job_state_severity",
    "rollup_job_state",
    "CLAIM_VERIFIED",
    "CLAIM_UNVERIFIED",
    "CLAIM_MISSING",
    "CLAIM_CONFLICTING",
    "CLAIM_STATES",
    "CLAIM_STATE_LABELS",
    "CLAIM_STATE_GLYPHS",
    "claim_state_label",
    "DECISION_APPROVE",
    "DECISION_REQUEST_CHANGES",
    "DECISION_REJECT",
    "DECISION_ESCALATE",
    "REVIEW_DECISIONS",
    "REVIEW_DECISION_LABELS",
    "REVIEW_DECISION_EFFECTS",
    "decision_label",
    "decision_effect",
]
