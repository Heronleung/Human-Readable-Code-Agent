"""Minimum structured context for one bounded agent turn (AGENT-LOOP-1).

The context handed to a model adapter is assembled here from a **field
allowlist** and nothing else. It carries useful *facts plus refs* — the current
project's goal, typed Plan/confirmation metadata, bounded structured scanner
facts with exact identities, freshness and limitations — never raw source files,
diffs, sensitive logs, secrets, arbitrary Memory documents or external captures.

The module is pure: no I/O, no store, no clock, no provider. It renders the
allowlisted facts into a bounded tuple of strings (the shape
:class:`hrca.integrations.provider.ProviderRequest` carries) and refuses to
exceed its own limits. A fact the caller does not hold is simply absent; the
executor treats a missing fact as a reason to clarify or stop, never as licence
to retrieve wider.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: Context bounds, stricter than the transport's own (64 KiB / 32 items) so the
#: agent never leans on the transport's ceiling.
MAX_CONTEXT_ITEMS = 16
MAX_CONTEXT_BYTES = 16 * 1024
MAX_FACT_CHARS = 400
MAX_LIMITATIONS = 6
MAX_EVIDENCE_REFS = 12

#: The exact workflow fields this slice may disclose. Everything else in a
#: read projection stays behind the boundary.
ALLOWED_WORKFLOW_FIELDS: Tuple[str, ...] = (
    "plan_id",
    "plan_revision",
    "plan_phase",
    "plan_digest",
    "goal",
    "job_id",
    "job_state",
    "run_id",
    "run_outcome",
    "freshness",
)

_ALLOWED_CONTROL = {"\t": " "}


def sanitize_text(value: Any, limit: int = MAX_FACT_CHARS) -> str:
    """Return bounded, control-character-free text for one context fact.

    Newlines and tabs collapse to spaces, other control characters are dropped,
    and the result is truncated to ``limit``. The function is deliberately
    lossy: context carries a *fact*, never a document.
    """
    if value is None:
        return ""
    text = str(value)
    cleaned = []
    for char in text:
        if char in _ALLOWED_CONTROL:
            cleaned.append(_ALLOWED_CONTROL[char])
        elif ord(char) < 32 or ord(char) == 127:
            continue
        else:
            cleaned.append(char)
    return "".join(cleaned).strip()[:limit]


def summarize_workflow(workflow: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the allowlisted summary of a scoped workflow read, or ``None``.

    ``None`` means no plan is recorded for the project — the executor must not
    manufacture one. Every returned value is bounded text or a bounded list, so
    the summary can be rendered into context without further sanitisation
    decisions.
    """
    if not isinstance(workflow, Mapping) or not workflow.get("has_plan"):
        return None
    revision = workflow.get("revision")
    revision = revision if isinstance(revision, Mapping) else {}
    review = workflow.get("review")
    review = review if isinstance(review, Mapping) else {}
    summary: Dict[str, Any] = {
        "plan_id": sanitize_text(workflow.get("plan_id"), 120),
        "plan_revision": workflow.get("plan_revision"),
        "plan_phase": sanitize_text(workflow.get("plan_phase"), 40),
        "plan_digest": sanitize_text(workflow.get("plan_digest"), 80),
        "goal": sanitize_text(revision.get("goal"), MAX_FACT_CHARS),
        "job_id": sanitize_text(workflow.get("job_id"), 120),
        "job_state": sanitize_text(workflow.get("job_state"), 40),
        "run_id": sanitize_text(workflow.get("run_id"), 120),
        "run_outcome": sanitize_text(review.get("run_outcome"), 40),
        "freshness": sanitize_text(review.get("freshness"), 40),
    }
    limitations = []
    for item in (review.get("limitations") or [])[:MAX_LIMITATIONS]:
        text = sanitize_text(item)
        if text:
            limitations.append(text)
    summary["limitations"] = limitations
    references = []
    for row in (review.get("evidence") or [])[:MAX_EVIDENCE_REFS]:
        if isinstance(row, Mapping):
            evidence_id = sanitize_text(row.get("evidence_id"), 120)
            predicate = sanitize_text(row.get("predicate_id"), 80)
            result = sanitize_text(row.get("result"), 40)
            if evidence_id:
                references.append({"evidence_id": evidence_id, "predicate": predicate, "result": result})
    summary["evidence"] = references
    return summary


def render_fact(label: str, value: Any) -> str:
    """Render one labelled, bounded fact line."""
    return f"{label}: {sanitize_text(value)}"


def build_context(
    *,
    goal: str,
    plan: Optional[Mapping[str, Any]],
    observation: Optional[str] = None,
) -> Tuple[Tuple[str, ...], int]:
    """Return ``(context_items, byte_count)`` for one agent turn.

    ``plan`` is a :func:`summarize_workflow` result (or ``None``). ``observation``
    is the bounded summary of the previous record read, when the loop continued
    after one. The caller supplies facts it actually holds; this function never
    retrieves anything.
    """
    items = [render_fact("GOAL", goal)]
    if plan is None:
        items.append("PLAN: none recorded for this project")
    else:
        items.append(
            render_fact(
                "PLAN",
                "phase={phase} revision={revision} digest={digest} plan={plan} job={job}".format(
                    phase=plan.get("plan_phase") or "unknown",
                    revision=plan.get("plan_revision"),
                    digest=(plan.get("plan_digest") or "")[:16],
                    plan=plan.get("plan_id") or "none",
                    job=plan.get("job_id") or "none",
                ),
            )
        )
        plan_goal = plan.get("goal")
        if plan_goal:
            items.append(render_fact("PLAN_GOAL", plan_goal))
        items.append(
            render_fact(
                "SCAN",
                "state={state} outcome={outcome} freshness={freshness}".format(
                    state=plan.get("job_state") or "none",
                    outcome=plan.get("run_outcome") or "none",
                    freshness=plan.get("freshness") or "none",
                ),
            )
        )
        for limitation in plan.get("limitations") or []:
            items.append(render_fact("LIMITATION", limitation))
        for reference in plan.get("evidence") or []:
            items.append(
                render_fact(
                    "EVIDENCE",
                    "{id} predicate={predicate} result={result}".format(
                        id=reference.get("evidence_id"),
                        predicate=reference.get("predicate"),
                        result=reference.get("result"),
                    ),
                )
            )
    if observation:
        items.append(render_fact("OBSERVED", observation))

    items = tuple(items[:MAX_CONTEXT_ITEMS])
    total = sum(len(item.encode("utf-8")) for item in items)
    while total > MAX_CONTEXT_BYTES and len(items) > 1:
        items = items[:-1]
        total = sum(len(item.encode("utf-8")) for item in items)
    return items, total


__all__ = [
    "MAX_CONTEXT_ITEMS",
    "MAX_CONTEXT_BYTES",
    "MAX_FACT_CHARS",
    "ALLOWED_WORKFLOW_FIELDS",
    "sanitize_text",
    "summarize_workflow",
    "render_fact",
    "build_context",
]
