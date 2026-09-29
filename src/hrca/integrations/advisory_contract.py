"""Advisory provider-payload contract (B-R2).

This module owns the **provider payload contract** for the advisory flow: the
schema version a provider answer must declare, the bounds a request and an
answer are held to, and the two functions that decide whether an answer is
acceptable and what of it may be carried forward. It is the artifact the
provider interface is expressed in, which is why it lives with the rest of the
provider seam (:mod:`hrca.integrations.provider` for the neutral protocol,
:mod:`hrca.integrations.deepseek` for the fixed provider identity) rather than
inside the capability that consumes an answer.

Why it is here and not in the Twin package
------------------------------------------

It was written inside :mod:`hrca.twin.advisory`, which is where its first
consumer needed it. But a *transport* also needs it — it must bound its request
and validate and normalize what comes back — and the advisory transport lives in
:mod:`hrca.integrations.deepseek_transport`. That made the provider seam depend
on a capability, so the one package that opens a socket and reads a credential
dragged in the Code Map domain. The rule this module restores is the ordinary
one: a seam does not depend on a capability. The rule-delta path already has
this shape — :mod:`hrca.execution.delta_transport` takes its payload contract
from :mod:`hrca.execution.rule_delta_interpret`, its own package.

:mod:`hrca.twin.advisory` re-exports every public name below, so
``twin.advisory.validate_advisory_payload`` is the *same object* as
``integrations.advisory_contract.validate_advisory_payload`` — not a copy, and
not a wrapper. Nothing that already resolved has stopped resolving, and no
verdict, reason string, bound or normalized field changed.

A stdlib-only leaf
------------------

This module imports only the standard library and nothing else from ``hrca``. It
performs no I/O, reads no clock, opens no store or socket, reads no credential,
constructs no provider client, reaches no Twin, runner, container or network, and
defines no default that a caller could tune. The bounds below are code-owned
constants, never configuration: a caller may pass a tighter one to a transport,
never a wider one.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# The advisory payload schema version. A provider answer that declares anything
# else is refused rather than read, and the value appears verbatim in the
# assembled result document.
ADVISORY_SCHEMA_VERSION = "1.0.0"

# -- limits (code-controlled; never user-configurable) --------------------

# Maximum total context bytes and context-item count sent in one request.
MAX_REQUEST_BYTES = 64 * 1024  # 64 KiB
MAX_CONTEXT_ITEMS = 32

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


def valid_string_list(value: Any, *, max_items: int, max_chars: int) -> bool:
    """Return whether ``value`` is a bounded list of non-empty bounded strings."""
    if not isinstance(value, list) or len(value) > max_items:
        return False
    return all(
        isinstance(item, str) and 0 < len(item) <= max_chars for item in value
    )


def valid_plan_suggestions(value: Any) -> bool:
    """Return whether ``value`` is an ordered, 1-based, bounded suggestion list.

    The steps must be sequential and start at 1, because the order is what the
    provider is asserting about the plan; a gap or a repeat is a malformed
    answer rather than a reordering to be repaired.
    """
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
    if not valid_string_list(
        payload.get("clarification_needs"),
        max_items=MAX_PROVIDER_LIST_ITEMS,
        max_chars=MAX_PROVIDER_FIELD_CHARS,
    ):
        return "invalid clarification_needs"
    if not isinstance(payload.get("impact"), str) or not payload.get("impact").strip():
        return "invalid impact"
    if len(payload["impact"]) > MAX_PROVIDER_FIELD_CHARS:
        return "impact too large"
    if not valid_string_list(
        payload.get("assumptions"),
        max_items=MAX_PROVIDER_LIST_ITEMS,
        max_chars=MAX_PROVIDER_FIELD_CHARS,
    ):
        return "invalid assumptions"
    if not valid_string_list(
        payload.get("risks"),
        max_items=MAX_PROVIDER_LIST_ITEMS,
        max_chars=MAX_PROVIDER_FIELD_CHARS,
    ):
        return "invalid risks"
    if not valid_plan_suggestions(payload.get("plan_suggestions")):
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


__all__ = [
    "ADVISORY_SCHEMA_VERSION",
    "MAX_REQUEST_BYTES",
    "MAX_CONTEXT_ITEMS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PROVIDER_FIELD_CHARS",
    "MAX_PROVIDER_LIST_ITEMS",
    "MAX_PLAN_SUGGESTIONS",
    "TIMEOUT_SECONDS",
    "valid_string_list",
    "valid_plan_suggestions",
    "validate_advisory_payload",
    "normalize_payload",
]
