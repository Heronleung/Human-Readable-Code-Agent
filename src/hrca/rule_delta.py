"""Bounded declarative rule-delta contract (P4.7a).

The smallest offline, parameterized, data-only shape for expressing a genuinely
new bounded business-rule change, in place of P4.7's byte-match selection of
prewritten variants. A **rule delta** is pure data: it names a supported
``result_kind`` (rule family) and a list of ``set_parameter`` changes against a
code-owned parameter registry. It never carries Python, code, commands, imports,
dependencies, paths, mounts, environment, network, UI, runtime or verifier
instructions.

The trusted side is only the code-owned parameter registry (:data:`RULE_PARAMETERS`)
and the code-owned evaluators the isolated runner resolves. ``resolve_delta``
turns a validated delta into a bounded ``{rule_id, parameters}`` mapping that
the runner hands to a code-owned handler — raw code never enters the path.

This module is pure (Qt-free, stdlib-only): no filesystem, network, credential,
command, Git, provider, runner or package-execution access.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

RULE_DELTA_SCHEMA_VERSION = "1.0.0"

# Supported rule families (code-owned allowlist).
RESULT_KIND_QUOTATION = "quotation"
RESULT_KIND_LATE_RETURN_FEE = "late_return_fee"
RESULT_KINDS = frozenset({RESULT_KIND_QUOTATION, RESULT_KIND_LATE_RETURN_FEE})

# The only supported operation.
OPERATION_SET_PARAMETER = "set_parameter"
OPERATIONS = frozenset({OPERATION_SET_PARAMETER})

# Provenance tokens. ``provider_delta`` is reserved for the later, separately
# authorized live flow; no provider request is made here.
PROVENANCE_DETERMINISTIC_FIXTURE = "deterministic_fixture"
PROVENANCE_MANUAL_DELTA = "manual_delta"
PROVENANCE_PROVIDER_DELTA = "provider_delta"
PROVENANCES = frozenset(
    {PROVENANCE_DETERMINISTIC_FIXTURE, PROVENANCE_MANUAL_DELTA, PROVENANCE_PROVIDER_DELTA}
)

# Bounded limits (code-owned; never from delta input).
MAX_DELTA_BYTES = 8 * 1024
MAX_CHANGES = 8
MAX_NOTES = 8
MAX_NOTE_CHARS = 256
MAX_DECIMAL_PLACES = 4

# Code-owned parameter registry: rule family -> parameter_id -> spec. The value
# of a ``set_parameter`` change must be a decimal string within ``min``/``max``.
RULE_PARAMETERS = {
    RESULT_KIND_QUOTATION: {
        "member_discount_rate": {"min": "0", "max": "1", "default": "0.05"},
    },
    RESULT_KIND_LATE_RETURN_FEE: {
        "cap": {"min": "0", "max": "100", "default": "30"},
    },
}

# The exact top-level keys a delta may carry.
_ALLOWED_DELTA_KEYS = frozenset(
    {"schema_version", "result_kind", "changes", "clarification_questions", "unsupported_requirements"}
)
# The exact keys a change may carry. Anything else (code/expr/command/...) is rejected.
_ALLOWED_CHANGE_KEYS = frozenset({"operation", "rule_id", "parameter_id", "value"})

_DECIMAL_STR_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")

# Bounded refusal reasons (never interpolate delta content).
REASON_NOT_MAPPING = "delta is not a mapping"
REASON_TOO_LARGE = "delta is too large"
REASON_UNKNOWN_KEYS = "delta has unsupported fields"
REASON_UNSUPPORTED_SCHEMA = "unsupported schema_version"
REASON_UNKNOWN_RESULT_KIND = "unknown result_kind"
REASON_NO_CHANGES = "delta has no changes"
REASON_TOO_MANY_CHANGES = "delta has too many changes"
REASON_INVALID_CHANGE = "change is invalid"
REASON_UNKNOWN_OPERATION = "unsupported operation"
REASON_RULE_MISMATCH = "change rule_id does not match result_kind"
REASON_UNKNOWN_PARAMETER = "unknown parameter_id"
REASON_INVALID_VALUE = "value is not a valid decimal"
REASON_OUT_OF_RANGE = "value is out of range"
REASON_PRECISION = "value has too many decimal places"
REASON_DUPLICATE = "duplicate parameter change"
REASON_INVALID_NOTES = "clarification or unsupported notes are invalid"


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    import json

    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _bounded_notes(value: Any, limit: int) -> bool:
    return isinstance(value, list) and len(value) <= limit and all(
        isinstance(item, str) and 0 < len(item) <= MAX_NOTE_CHARS for item in value
    )


def _parse_decimal(value: Any) -> Optional[Decimal]:
    if not isinstance(value, str) or not _DECIMAL_STR_RE.match(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _decimal_places(value: str) -> int:
    if "." not in value:
        return 0
    return len(value.rsplit(".", 1)[1])


def validate_delta(value: Any) -> Optional[str]:
    """Return a bounded reason when ``value`` is not a valid rule delta.

    Rejects a non-mapping, an oversized delta, any top-level or change key
    outside the fixed allowlists (implicitly rejecting code/expr/command/import/
    dependency/path/mount/env/network/UI/runtime/verifier fields), a wrong
    schema version, an unknown result kind, a non-``set_parameter`` operation, an
    unknown rule/parameter id, a malformed/out-of-range/over-precise value, a
    duplicate change, or malformed clarification/unsupported notes.
    """
    if not isinstance(value, dict):
        return REASON_NOT_MAPPING
    if len(dumps(value).encode("utf-8")) > MAX_DELTA_BYTES:
        return REASON_TOO_LARGE
    if set(value) - _ALLOWED_DELTA_KEYS:
        return REASON_UNKNOWN_KEYS
    if value.get("schema_version") != RULE_DELTA_SCHEMA_VERSION:
        return REASON_UNSUPPORTED_SCHEMA

    result_kind = value.get("result_kind")
    if result_kind not in RESULT_KINDS:
        return REASON_UNKNOWN_RESULT_KIND

    changes = value.get("changes")
    if not isinstance(changes, list) or not changes:
        return REASON_NO_CHANGES
    if len(changes) > MAX_CHANGES:
        return REASON_TOO_MANY_CHANGES

    params = RULE_PARAMETERS[result_kind]
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - _ALLOWED_CHANGE_KEYS:
            return REASON_INVALID_CHANGE
        if change.get("operation") != OPERATION_SET_PARAMETER:
            return REASON_UNKNOWN_OPERATION
        if change.get("rule_id") != result_kind:
            return REASON_RULE_MISMATCH
        parameter_id = change.get("parameter_id")
        if parameter_id not in params:
            return REASON_UNKNOWN_PARAMETER
        if parameter_id in seen:
            return REASON_DUPLICATE
        seen.add(parameter_id)

        raw = change.get("value")
        dec = _parse_decimal(raw)
        if dec is None:
            return REASON_INVALID_VALUE
        if _decimal_places(raw) > MAX_DECIMAL_PLACES:
            return REASON_PRECISION
        spec = params[parameter_id]
        if dec < Decimal(spec["min"]) or dec > Decimal(spec["max"]):
            return REASON_OUT_OF_RANGE

    for key in ("clarification_questions", "unsupported_requirements"):
        note = value.get(key)
        if note is not None and not _bounded_notes(note, MAX_NOTES):
            return REASON_INVALID_NOTES

    return None


def resolve_delta(delta: Any) -> Optional[Dict[str, Any]]:
    """Resolve a validated delta to a bounded ``{rule_id, parameters}`` mapping.

    Returns ``None`` when the delta is invalid. On success the parameters map
    each changed ``parameter_id`` to its exact typed value string (the original
    spelling is preserved). This is the only data that flows to the runner.
    """
    if validate_delta(delta) is not None:
        return None
    parameters = {
        change["parameter_id"]: change["value"] for change in delta["changes"]
    }
    return {"rule_id": delta["result_kind"], "parameters": parameters}


__all__ = [
    "RULE_DELTA_SCHEMA_VERSION",
    "RESULT_KIND_QUOTATION",
    "RESULT_KIND_LATE_RETURN_FEE",
    "RESULT_KINDS",
    "OPERATION_SET_PARAMETER",
    "OPERATIONS",
    "PROVENANCE_DETERMINISTIC_FIXTURE",
    "PROVENANCE_MANUAL_DELTA",
    "PROVENANCE_PROVIDER_DELTA",
    "PROVENANCES",
    "MAX_DELTA_BYTES",
    "MAX_CHANGES",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "MAX_DECIMAL_PLACES",
    "RULE_PARAMETERS",
    "REASON_NOT_MAPPING",
    "REASON_TOO_LARGE",
    "REASON_UNKNOWN_KEYS",
    "REASON_UNSUPPORTED_SCHEMA",
    "REASON_UNKNOWN_RESULT_KIND",
    "REASON_NO_CHANGES",
    "REASON_TOO_MANY_CHANGES",
    "REASON_INVALID_CHANGE",
    "REASON_UNKNOWN_OPERATION",
    "REASON_RULE_MISMATCH",
    "REASON_UNKNOWN_PARAMETER",
    "REASON_INVALID_VALUE",
    "REASON_OUT_OF_RANGE",
    "REASON_PRECISION",
    "REASON_DUPLICATE",
    "REASON_INVALID_NOTES",
    "dumps",
    "validate_delta",
    "resolve_delta",
]
