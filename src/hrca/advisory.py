"""Advisory hosted-planning domain (P4.2b).

This module is the deterministic, Qt-free, network-free *domain* for the first
hosted-provider interaction: turning a current, non-executable Intent Delta and
its deterministic P4.1 Proposal Package into a **bounded disclosure context**
that a *future* provider transport may send, and validating/normalizing the
advisory answer that comes back.

It is deliberately pure in the same sense as :mod:`hrca.proposal`:

* it performs **no filesystem access** — the caller supplies the source
  excerpts and the Twin store;
* it performs **no network, provider, credential, command, Git or
  repository-write** call and emits no model-generated text — every field here
  is assembled deterministically from the supplied inputs only;
* it is **Qt-free** and imports only the standard library plus
  :mod:`hrca.twin` (fingerprints), :mod:`hrca.codemap` (block model),
  :mod:`hrca.codemap_draft` (intent/delta constants) and :mod:`hrca.proposal`
  (proposal constants), so the desktop client can never import it (enforced by
  :mod:`tests.test_architecture`).

Authority model (the whole point of P4.2b):

* **Deterministic** fields — the P4.1 proposal, target scope, source anchors,
  preserved constraints, baseline and stale/conflict decisions — are the
  authority and are never overwritten by the provider.
* **Provider-suggested** fields — clarification needs, impact, assumptions,
  risks and ordered plan suggestions — are the only fields a provider may
  contribute, and they are always carried under ``provider_suggested``, never
  merged into the deterministic proposal.

The module never decides to send anything: it only *prepares* the bounded
context and the disclosure manifest. The boundary owns the confirmation gate
and the single network attempt.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import codemap, twin

ADVISORY_SCHEMA_VERSION = "1.0.0"
ADVISORY_GENERATOR = "hrca-advisory"

# -- normalized terminal states ------------------------------------------
#
# ``ready`` is a schema-validated advisory. Every other value is a normalized
# failure state that the boundary must be able to produce while preserving the
# deterministic P4.1 proposal. ``cancel_requested`` and ``stale_response`` and
# ``over_limit`` / ``context_rejected`` are produced *before* any network access.

STATE_READY = "ready"
STATE_CREDENTIAL_MISSING = "credential_missing"
STATE_PROVIDER_UNAVAILABLE = "provider_unavailable"
STATE_NETWORK_DENIED = "network_denied"
STATE_TIMEOUT = "timeout"
STATE_RATE_LIMITED = "rate_limited"
STATE_QUOTA_EXCEEDED = "quota_exceeded"
STATE_INVALID_OUTPUT = "invalid_output"
STATE_CONTEXT_REJECTED = "context_rejected"
STATE_OVER_LIMIT = "over_limit"
STATE_CANCEL_REQUESTED = "cancel_requested"
STATE_STALE_RESPONSE = "stale_response"
STATE_PROVIDER_FAILURE = "provider_failure"

ADVISORY_STATES = frozenset(
    {
        STATE_READY,
        STATE_CREDENTIAL_MISSING,
        STATE_PROVIDER_UNAVAILABLE,
        STATE_NETWORK_DENIED,
        STATE_TIMEOUT,
        STATE_RATE_LIMITED,
        STATE_QUOTA_EXCEEDED,
        STATE_INVALID_OUTPUT,
        STATE_CONTEXT_REJECTED,
        STATE_OVER_LIMIT,
        STATE_CANCEL_REQUESTED,
        STATE_STALE_RESPONSE,
        STATE_PROVIDER_FAILURE,
    }
)

# -- bounded denial reasons (never interpolate caller content) ------------

DENY_SECRET_LIKE = "secret_like"
DENY_BINARY = "binary"
DENY_MISSING_ANCHOR = "missing_anchor"
DENY_OUTSIDE_ROOT = "outside_root"
DENY_UNSUPPORTED_PATH = "unsupported_path"
DENY_IGNORED_PATH = "ignored_path"
DENY_OVER_LIMIT = "over_limit"

# The deterministic proposal can be in one of these terminal states when the
# advisory flow is entered. Only ``ready`` is eligible for an advisory request;
# every other state short-circuits to a bounded, offline result.
_ADVISORY_ELIGIBLE_PROPOSAL_STATES = frozenset({"ready"})

# Fixed statement rendered into every disclosure so a user can never miss the
# data-egress fact. It is a constant; it never names a path or a secret.
_EGRESS_STATEMENT = (
    "Source-derived context from the current Intent Delta and proposal will "
    "leave this machine and be sent to the fixed provider endpoint."
)

# The fixed provider instruction (system prompt) sent with every advisory
# request. It is a constant and contains no source content; the bounded context
# items are appended as the user message by the transport.
_ADVISORY_TASK = (
    "You are a read-only planning advisor. Given a deterministic proposal and "
    "narrowly anchored source context, return a JSON object (and only that "
    "object) with exactly these keys: "
    '"schema_version" (the string "1.0.0"), '
    '"clarification_needs" (array of strings), '
    '"impact" (string), '
    '"assumptions" (array of strings), '
    '"risks" (array of strings), and '
    '"plan_suggestions" (array of objects, each with an integer "step" starting '
    "at 1 and a string \"description\"). "
    "Do not propose or generate source code, patches, diffs, commands or Git "
    "actions; advise only. Never invent facts: if the context is insufficient, "
    'say so in "clarification_needs" rather than guessing.'
)

# -- limits (code-controlled; never user-configurable) --------------------

# Maximum total context bytes and context-item count sent in one request.
MAX_REQUEST_BYTES = 64 * 1024  # 64 KiB
MAX_CONTEXT_ITEMS = 32

# Maximum source excerpt size: line count and character count per excerpt.
MAX_SOURCE_EXCERPT_LINES = 400
MAX_SOURCE_EXCERPT_CHARS = 8 * 1024

# Output cap enforced on the provider request (max_tokens) and used to bound
# the accepted provider payload.
MAX_OUTPUT_TOKENS = 2048

# Bounds on individual provider-suggested fields, so a hostile or runaway
# provider payload can never produce an unbounded result.
MAX_PROVIDER_FIELD_CHARS = 4000
MAX_PROVIDER_LIST_ITEMS = 20
MAX_PLAN_SUGGESTIONS = 50

# One-attempt request timeout (seconds). Fixed; no retry, no fallback.
TIMEOUT_SECONDS = 30.0

# Allowlist of the top-level keys the provider payload may contribute. Anything
# outside this set is ignored (never copied) by the validator, so a provider can
# never inject an authoritative deterministic field.
_PROVIDER_PAYLOAD_KEYS = frozenset(
    {"schema_version", "clarification_needs", "impact", "assumptions", "risks", "plan_suggestions"}
)

# A plan-suggestion entry may contribute exactly these keys; anything else is
# dropped by the validator.
_SUGGESTION_KEYS = frozenset({"step", "description", "rationale"})


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# -- secret-like heuristic -------------------------------------------------
#
# This is a conservative, deterministic *heuristic*, not a security boundary.
# It flags source excerpts that look like embedded secrets so the advisory flow
# can refuse to send them rather than silently exfiltrating a key. The list of
# recognized patterns is intentionally narrow and documented; anything it
# misses is a known limitation surfaced to the user, never a silently passed
# secret.

_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|pwd|credential|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret)\b"
    r"\s*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9+/_\-=]{16,}"
)
_PRIVATE_KEY_MARKER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# Opaque assignment to a generic secret-ish name (``key`` included), where the
# name and/or the value may be quoted (JSON/YAML mapping literal).
_GENERIC_ASSIGN_RE = re.compile(
    r"(?i)\b(secret|token|key|credential)\b\s*[\"']?\s*[:=]\s*[\"']"
    r"[A-Za-z0-9+/_\-=]{16,}[\"']"
)


def is_secret_like(text: str) -> bool:
    """Return True when ``text`` carries an obvious embedded-secret signal.

    The check is conservative and purely textual: it flags private-key markers
    and long opaque values assigned to secret-ish names. It is a best-effort
    refusal, not a guarantee; the caller reports any rejection truthfully as a
    bounded limitation rather than fabricating a replacement fact.
    """
    if not isinstance(text, str):
        return True
    if _PRIVATE_KEY_MARKER_RE.search(text):
        return True
    if _SECRET_ASSIGN_RE.search(text):
        return True
    return bool(_GENERIC_ASSIGN_RE.search(text))


def _is_binary(text: str) -> bool:
    """Return True when ``text`` carries a NUL byte (a strong binary signal)."""
    return "\x00" in text


def _bounded(text: Any, limit: int) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    return text if len(text) <= limit else text[:limit]


# -- context item rendering ------------------------------------------------

_CONTEXT_INTENT = "intent"
_CONTEXT_PROPOSAL = "proposal"
_CONTEXT_ENTITIES = "entities"
_CONTEXT_CONSTRAINTS = "constraints"
_CONTEXT_SOURCE_EXCERPT = "source_excerpt"
_CONTEXT_KINDS = frozenset(
    {
        _CONTEXT_INTENT,
        _CONTEXT_PROPOSAL,
        _CONTEXT_ENTITIES,
        _CONTEXT_CONSTRAINTS,
        _CONTEXT_SOURCE_EXCERPT,
    }
)


def _render_intent(delta: Dict[str, Any]) -> str:
    lines = [
        f"Intent: {delta.get('intent', 'unknown')}",
        f"Entries: {len(delta.get('entries', []))}",
    ]
    for entry in delta.get("entries", []):
        op = entry.get("operation", "unknown")
        entity = entry.get("owning_entity_id") or "?"
        cls = entry.get("intent_class", "unknown")
        deps = ", ".join(entry.get("known_dependencies") or [])
        callers = ", ".join(entry.get("known_callers") or [])
        row = f"- {op} on {entity} ({cls})"
        if deps:
            row += f"; dependencies: {deps}"
        if callers:
            row += f"; callers: {callers}"
        lines.append(row)
    return "\n".join(lines)


def _render_proposal(proposal: Dict[str, Any]) -> str:
    lines = [
        f"State: {proposal.get('state', 'unknown')}",
        f"Executable: {proposal.get('executable', False)}",
        f"Applied: {proposal.get('applied', False)}",
    ]
    scope = proposal.get("target_scope") or {}
    entities = scope.get("entities") or []
    artifacts = scope.get("artifacts") or []
    lines.append("Target entities: " + (", ".join(entities) if entities else "none"))
    lines.append("Target artifacts: " + (", ".join(artifacts) if artifacts else "none"))
    for step in proposal.get("plan_steps") or []:
        lines.append(f"Plan {step.get('step', '?')}: {step.get('description', '')}")
    for risk in proposal.get("risks") or []:
        lines.append(f"Risk [{risk.get('level', 'unknown')}]: {risk.get('description', '')}")
    for q in proposal.get("clarifications") or []:
        lines.append(f"Clarification: {q.get('question', '')}")
    return "\n".join(lines)


def _render_entities(store: Dict[str, Any], entities: List[str]) -> str:
    entity_set = set(entities)
    rows: List[str] = []
    for art in store.get("artifacts", []):
        if not isinstance(art, dict):
            continue
        locator = art.get("locator")
        if locator not in entity_set:
            continue
        rows.append(
            f"{art.get('kind', 'unknown')} {locator} -> {art.get('path', '?')}"
        )
    return "\n".join(rows) if rows else "No typed entities."


def _render_constraints(proposal: Dict[str, Any]) -> str:
    constraints = proposal.get("preserved_constraints") or []
    rows = [
        f"{c.get('entity_id') or 'global'}: {c.get('invariant', '')}"
        for c in constraints
        if isinstance(c, dict)
    ]
    return "\n".join(rows) if rows else "No preserved constraints."


# -- source excerpt extraction --------------------------------------------


def entity_anchors(
    blocks: List[Dict[str, Any]], entity_locators: List[str]
) -> List[Dict[str, Any]]:
    """Return the deduplicated, deterministic source anchors for the entities.

    Each anchor carries its owning ``locator``, the repository-relative
    ``file`` and the ``lineno``/``end_lineno`` range, so the caller can both
    slice a bounded excerpt and detect a target entity with no source anchor.
    """
    wanted = set(entity_locators)
    anchors: List[Dict[str, Any]] = []
    seen = set()
    for block in blocks:
        if block.get("block_type") != codemap.BT_ENTITY:
            continue
        locator = (block.get("payload") or {}).get("locator")
        if locator not in wanted:
            continue
        for anchor in block.get("source_anchors") or []:
            file = anchor.get("file")
            if not isinstance(file, str):
                continue
            key = (locator, file, anchor.get("lineno"), anchor.get("end_lineno"))
            if key in seen:
                continue
            seen.add(key)
            anchors.append(
                {
                    "locator": locator,
                    "file": file,
                    "lineno": anchor.get("lineno"),
                    "end_lineno": anchor.get("end_lineno"),
                }
            )
    anchors.sort(
        key=lambda a: (a["locator"], a["file"], a["lineno"] or 0, a["end_lineno"] or 0)
    )
    return anchors


def slice_excerpt(*, file: str, lineno: int, end_lineno: int, content: str) -> str:
    """Return a bounded, labelled source excerpt for one anchor range.

    The excerpt is a single-line header (repository-relative path and inclusive
    line range) followed by the source lines in that range, bounded to
    :data:`MAX_SOURCE_EXCERPT_LINES` lines and
    :data:`MAX_SOURCE_EXCERPT_CHARS` characters.
    """
    lines = content.splitlines() or [""]
    start = max(1, lineno)
    end = min(len(lines), max(start, end_lineno))
    window = lines[start - 1 : end]
    if len(window) > MAX_SOURCE_EXCERPT_LINES:
        window = window[:MAX_SOURCE_EXCERPT_LINES]
    label = f"{file}:{start}-{end}"
    excerpt = label + "\n" + "\n".join(window)
    if len(excerpt) > MAX_SOURCE_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_SOURCE_EXCERPT_CHARS]
    return excerpt


# -- context assembly ------------------------------------------------------


def _context_items(
    *,
    delta: Dict[str, Any],
    proposal: Dict[str, Any],
    store: Dict[str, Any],
    entities: List[str],
    excerpts: Dict[str, List[str]],
) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = [
        {"kind": _CONTEXT_INTENT, "label": "Intent Delta", "text": _render_intent(delta)},
        {"kind": _CONTEXT_PROPOSAL, "label": "Deterministic proposal", "text": _render_proposal(proposal)},
        {"kind": _CONTEXT_ENTITIES, "label": "Typed Twin entities", "text": _render_entities(store, entities)},
        {"kind": _CONTEXT_CONSTRAINTS, "label": "Preserved constraints", "text": _render_constraints(proposal)},
    ]
    for path in sorted(excerpts):
        for excerpt in excerpts[path]:
            items.append(
                {"kind": _CONTEXT_SOURCE_EXCERPT, "label": path, "text": excerpt}
            )
    return items


def advisory_token_for(items: List[Dict[str, str]]) -> str:
    """Return a content-addressed token binding the prepared context items."""
    canon = dumps([(i["kind"], i["label"], i["text"]) for i in items])
    return "advisory:" + twin.sha256_hex(canon.encode("utf-8"))


def build_context(
    *,
    delta: Dict[str, Any],
    proposal: Dict[str, Any],
    store: Dict[str, Any],
    excerpts: Dict[str, List[str]],
    denials: List[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Assemble a bounded advisory context, or a bounded rejection reason.

    Returns ``(context, error)`` where exactly one is ``None``. A rejection is
    produced *before* any network access for a denied source excerpt, a
    secret-like or binary excerpt, a missing anchor, or an over-limit context.
    ``denials`` is a sorted, deduplicated list of bounded reasons already
    collected by the caller; ``context`` carries the ordered items, the
    disclosure manifest, the request-byte total and the content-addressed
    advisory token.
    """
    entities = (proposal.get("target_scope") or {}).get("entities") or []

    if denials:
        return None, denials[0]
    for path in sorted(excerpts):
        for excerpt in excerpts[path]:
            if _is_binary(excerpt):
                return None, DENY_BINARY
            if is_secret_like(excerpt):
                return None, DENY_SECRET_LIKE

    # Required anchors: a ready proposal targets entities, so there must be at
    # least one source excerpt; an empty set would silently fabricate anchors.
    if entities and not excerpts:
        return None, DENY_MISSING_ANCHOR

    items = _context_items(
        delta=delta, proposal=proposal, store=store, entities=entities, excerpts=excerpts
    )
    if len(items) > MAX_CONTEXT_ITEMS:
        return None, DENY_OVER_LIMIT

    total_bytes = len(dumps(items).encode("utf-8"))
    if total_bytes > MAX_REQUEST_BYTES:
        return None, DENY_OVER_LIMIT

    disclosure_items = [
        {
            "kind": item["kind"],
            "label": item["label"],
            "bytes": len(item["text"].encode("utf-8")),
        }
        for item in items
    ]
    disclosure = {
        "provider_id": "deepseek",
        "model": "deepseek-flash",
        "one_attempt": True,
        "egress_statement": _EGRESS_STATEMENT,
        "caps": {
            "request_bytes": MAX_REQUEST_BYTES,
            "context_items": MAX_CONTEXT_ITEMS,
            "output_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
        },
        "items": disclosure_items,
        "total_bytes": total_bytes,
        "context_count": len(items),
    }
    token = advisory_token_for(items)
    return {
        "advisory_token": token,
        "items": items,
        "disclosure": disclosure,
        "request_bytes": total_bytes,
        "context_count": len(items),
    }, None


# -- provider request ------------------------------------------------------


def build_provider_request(context: Dict[str, Any]) -> Dict[str, Any]:
    """Return the P2.5 provider request payload for a prepared ``context``.

    The task id is the content-addressed advisory token (giving correlation and
    staleness protection); the task is the fixed instruction; the context is the
    ordered, already-bounded item texts.
    """
    return {
        "task_id": context["advisory_token"],
        "task": _ADVISORY_TASK,
        "context": [item["text"] for item in context["items"]],
    }


# -- provider payload validation -------------------------------------------


def _valid_string_list(value: Any, *, max_items: int, max_chars: int) -> bool:
    if not isinstance(value, list) or len(value) > max_items:
        return False
    return all(
        isinstance(item, str) and 0 < len(item) <= max_chars for item in value
    )


def _valid_plan_suggestions(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > MAX_PLAN_SUGGESTIONS:
        return False
    expected = 1
    for item in value:
        if not isinstance(item, dict):
            return False
        step = item.get("step")
        if not isinstance(step, int) or step != expected:
            return False
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            return False
        if len(description) > MAX_PROVIDER_FIELD_CHARS:
            return False
        expected += 1
    return True


def validate_advisory_payload(payload: Any) -> Optional[str]:
    """Validate a provider payload against the versioned advisory schema.

    Returns a bounded reason on failure, or ``None`` when the payload is a valid
    ``{schema_version, clarification_needs, impact, assumptions, risks,
    plan_suggestions}`` object. Only the known keys are ever accepted.
    """
    if not isinstance(payload, dict):
        return "payload is not a mapping"
    if payload.get("schema_version") != ADVISORY_SCHEMA_VERSION:
        return "unsupported schema_version"
    for key in ("clarification_needs", "impact", "assumptions", "risks", "plan_suggestions"):
        if key not in payload:
            return f"missing {key}"
    if not _valid_string_list(
        payload.get("clarification_needs"),
        max_items=MAX_PROVIDER_LIST_ITEMS,
        max_chars=MAX_PROVIDER_FIELD_CHARS,
    ):
        return "invalid clarification_needs"
    if not isinstance(payload.get("impact"), str) or not payload.get("impact").strip():
        return "invalid impact"
    if len(payload["impact"]) > MAX_PROVIDER_FIELD_CHARS:
        return "impact too large"
    if not _valid_string_list(
        payload.get("assumptions"),
        max_items=MAX_PROVIDER_LIST_ITEMS,
        max_chars=MAX_PROVIDER_FIELD_CHARS,
    ):
        return "invalid assumptions"
    if not _valid_string_list(
        payload.get("risks"),
        max_items=MAX_PROVIDER_LIST_ITEMS,
        max_chars=MAX_PROVIDER_FIELD_CHARS,
    ):
        return "invalid risks"
    if not _valid_plan_suggestions(payload.get("plan_suggestions")):
        return "invalid plan_suggestions"
    return None


def normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the normalized provider-suggested fields from a validated payload.

    Only the five advisory fields are carried forward; any extra top-level key a
    provider emitted is dropped, and each suggestion is reduced to ``step``,
    ``description`` and an optional ``rationale``. This is what marks the fields
    as provider-suggested rather than authoritative.
    """
    suggestions = []
    for item in payload.get("plan_suggestions") or []:
        entry = {"step": item.get("step"), "description": item.get("description")}
        rationale = item.get("rationale")
        if isinstance(rationale, str) and rationale.strip():
            entry["rationale"] = rationale[:MAX_PROVIDER_FIELD_CHARS]
        suggestions.append(entry)
    return {
        "clarification_needs": list(payload.get("clarification_needs") or []),
        "impact": payload.get("impact", ""),
        "assumptions": list(payload.get("assumptions") or []),
        "risks": list(payload.get("risks") or []),
        "plan_suggestions": suggestions,
    }


# -- result assembly -------------------------------------------------------


def assemble_result(
    *,
    state: str,
    provider_id: str,
    model: str,
    advisory_token: Optional[str],
    sent: bool,
    deterministic: Dict[str, Any],
    provider_suggested: Optional[Dict[str, Any]],
    usage: Optional[Dict[str, Optional[int]]],
    limitations: List[str],
) -> Dict[str, Any]:
    """Assemble the versioned advisory result envelope.

    ``deterministic`` carries the authoritative proposal (and its preserved
    scope/constraints/baseline). ``provider_suggested`` is present only in the
    ``ready`` state and never overwrites the deterministic fields. On a failure
    state the deterministic proposal is preserved and ``provider_suggested`` is
    ``None``.
    """
    result: Dict[str, Any] = {
        "schema_version": ADVISORY_SCHEMA_VERSION,
        "generator": ADVISORY_GENERATOR,
        "state": state,
        "provider_id": provider_id,
        "model": model,
        "advisory_token": advisory_token,
        "sent": bool(sent),
        "deterministic": deterministic,
        "provider_suggested": provider_suggested,
        "usage": usage,
        "limitations": list(limitations),
    }
    return result


def deterministic_authority(
    proposal: Dict[str, Any], store: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the authoritative deterministic slice carried into every result.

    The P4.1 proposal is preserved verbatim; the target scope, preserved
    constraints, baseline and stale/conflict decision are surfaced explicitly
    so a provider can never silently change them.
    """
    return {
        "proposal": proposal,
        "target_scope": proposal.get("target_scope") or {},
        "preserved_constraints": proposal.get("preserved_constraints") or [],
        "baseline": proposal.get("baseline") or {},
        "stale_or_conflict": "none",
    }


__all__ = [
    "ADVISORY_SCHEMA_VERSION",
    "ADVISORY_GENERATOR",
    "STATE_READY",
    "STATE_CREDENTIAL_MISSING",
    "STATE_PROVIDER_UNAVAILABLE",
    "STATE_NETWORK_DENIED",
    "STATE_TIMEOUT",
    "STATE_RATE_LIMITED",
    "STATE_QUOTA_EXCEEDED",
    "STATE_INVALID_OUTPUT",
    "STATE_CONTEXT_REJECTED",
    "STATE_OVER_LIMIT",
    "STATE_CANCEL_REQUESTED",
    "STATE_STALE_RESPONSE",
    "STATE_PROVIDER_FAILURE",
    "ADVISORY_STATES",
    "DENY_SECRET_LIKE",
    "DENY_BINARY",
    "DENY_MISSING_ANCHOR",
    "DENY_OUTSIDE_ROOT",
    "DENY_UNSUPPORTED_PATH",
    "DENY_IGNORED_PATH",
    "DENY_OVER_LIMIT",
    "MAX_REQUEST_BYTES",
    "MAX_CONTEXT_ITEMS",
    "MAX_SOURCE_EXCERPT_LINES",
    "MAX_SOURCE_EXCERPT_CHARS",
    "MAX_OUTPUT_TOKENS",
    "TIMEOUT_SECONDS",
    "dumps",
    "is_secret_like",
    "entity_anchors",
    "slice_excerpt",
    "build_context",
    "advisory_token_for",
    "build_provider_request",
    "validate_advisory_payload",
    "normalize_payload",
    "assemble_result",
    "deterministic_authority",
]
