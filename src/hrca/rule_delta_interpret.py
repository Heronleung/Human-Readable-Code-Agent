"""Bounded provider-to-rule-delta interpretation domain (P4.8).

The first honest provider-backed document-interpretation path. It turns one
current saved synthetic quotation requirement into the existing, data-only
:mod:`hrca.rule_delta` 1.0.0 contract — never into code, commands, dependencies,
paths, environment, UI, runtime or verifier settings.

This module is the deterministic, Qt-free, network-free *domain* for that flow:

* it builds the **visible preflight disclosure manifest** from an exact set of
  code-owned instructions plus the one variable input (the selected saved
  requirement text);
* it defines the **bounded provider-output contract** — exactly one structured
  outcome (``delta`` / ``clarification_required`` / ``unsupported``) — and
  validates/normalizes it;
* it owns the **cost reservation arithmetic** (verified peak rates, worst-case
  reservation) and the one-attempt limit catalogue;
* it assembles the versioned interpretation result envelope.

It performs **no filesystem, network, provider, credential, command, Git or
repository-write** access and imports only the standard library plus the pure
:mod:`hrca.rule_delta` and :mod:`hrca.app_package` domains, so the desktop
client can never import it (enforced by :mod:`tests.test_architecture`).

Authority model (the whole point of P4.8): the provider output is untrusted
data. It can only name a code-owned ``result_kind``, code-owned
``set_parameter`` changes against the code-owned parameter registry, or a
bounded ``clarification_required`` / ``unsupported`` outcome. It can never
select policy, permission, an evaluator, a runtime, a verifier, an expected
result, an adoption decision or a retry. A delta only becomes a reviewable
Candidate after the code-owned validator, the isolated runner execution and the
independent oracle all pass — never merely on the provider's word.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import app_package, rule_delta

RULE_DELTA_INTERPRET_SCHEMA_VERSION = "1.0.0"
RULE_DELTA_INTERPRET_GENERATOR = "hrca-rule-delta-interpret"

# -- provider / model identity (verified against official pages 2026-09-11) --
#
# The recipient and allowlisted model are fixed code-owned constants, mirroring
# :mod:`hrca.deepseek`. They are never user-configurable and never taken from a
# document, a CLI flag or an environment variable.
PROVIDER_ID = "deepseek"
MODEL_ID = "deepseek-v4-flash"

# -- provider-output outcomes ---------------------------------------------

OUTCOME_DELTA = "delta"
OUTCOME_CLARIFICATION_REQUIRED = "clarification_required"
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOMES = frozenset(
    {OUTCOME_DELTA, OUTCOME_CLARIFICATION_REQUIRED, OUTCOME_UNSUPPORTED}
)

# -- bounded limits (code-owned; never user-configurable) ------------------

# Serialized request body cap, in UTF-8 bytes (12 KiB).
MAX_REQUEST_BYTES = 12 * 1024
# Input / output token caps. Input is enforced from a deterministic estimate
# before any network access; output is enforced as ``max_tokens`` on the wire.
MAX_INPUT_TOKENS = 4096
MAX_OUTPUT_TOKENS = 1024
# One-attempt provider deadline and the whole-workflow deadline, in seconds.
TIMEOUT_SECONDS = 45.0
WORKFLOW_TIMEOUT_SECONDS = 120.0
# At most eight synthetic, code-owned, user-confirmed examples in the prompt.
MAX_EXAMPLES = 8
# Bounded clarification / unsupported notes (mirrors rule_delta limits).
MAX_NOTES = 8
MAX_NOTE_CHARS = 256

# -- cost reservation (verified peak rates 2026-09-11) ----------------------
#
# Peak cache-miss input and peak output rates are used for a worst-case
# reservation, so a single request can never exceed the reserved amount even at
# peak pricing. Off-peak is half; the reservation deliberately assumes peak.
#
#   * input  (cache miss): US$0.44 per 1M tokens
#   * output              : US$1.32 per 1M tokens
#
# The atomic local reservation is US$0.01. With 4,096 input + 1,024 output
# tokens the worst-case cost is 4096/1e6*0.44 + 1024/1e6*1.32 ≈ US$0.00315,
# comfortably below the reservation. "Unknown pricing" (a model absent from the
# table) and "insufficient reservation" both fail closed.
PRICING = {
    MODEL_ID: {
        "input_usd_per_1m": Decimal("0.44"),
        "output_usd_per_1m": Decimal("1.32"),
    },
}
RESERVATION_USD = Decimal("0.01")

# Fixed egress and retention statements rendered into every disclosure so the
# data-egress and retention facts can never be missed. They are constants and
# never name a path, a secret or a host detail.
_EGRESS_STATEMENT = (
    "The selected requirement text and the code-owned interpretation "
    "instructions will leave this machine and be sent to the fixed provider "
    "endpoint."
)
_POLICY_WARNING = (
    "DeepSeek does not promise zero retention: inputs may be collected, service "
    "data may be used to improve or train technology, retention depends on "
    "purpose, account and legal needs, and data may be stored and processed in "
    "China. Only synthetic, non-sensitive text is permitted."
)
# Fixed, honest account-cap statement. The enforced limit is the atomic
# per-request reservation; the product does not enforce a US$8 account cap, so
# the disclosure must never imply one exists.
_ACCOUNT_CAP_STATEMENT = (
    "US$8 is not an enforced account cap; the enforced reservation for this "
    "single request is US$0.01."
)

# -- normalized terminal states ---------------------------------------------
#
# ``reviewable_candidate`` is the only success state (a strict delta plus
# isolated execution plus independent verification). Every other state is a
# bounded failure that preserves the current Accepted Version and never retries.

STATE_PREFLIGHT = "preflight"
STATE_CANCEL_REQUESTED = "cancel_requested"
STATE_STALE = "stale"
STATE_CLARIFICATION_REQUIRED = "clarification_required"
STATE_UNSUPPORTED = "unsupported"
STATE_INVALID_OUTPUT = "invalid_output"
STATE_USAGE_UNKNOWN = "usage_unknown"
STATE_PRICING_UNKNOWN = "pricing_unknown"
STATE_RESERVATION_FAILED = "reservation_failed"
STATE_RUNNER_UNAVAILABLE = "runner_unavailable"
STATE_VERIFICATION_FAILED = "verification_failed"
STATE_OVER_LIMIT = "over_limit"
STATE_REVIEWABLE_CANDIDATE = "reviewable_candidate"
# Transport-failure states (codes the transport raises map 1:1 here).
STATE_CREDENTIAL_MISSING = "credential_missing"
STATE_NETWORK_DENIED = "network_denied"
STATE_TIMEOUT = "timeout"
STATE_RATE_LIMITED = "rate_limited"
STATE_QUOTA_EXCEEDED = "quota_exceeded"
STATE_PROVIDER_UNAVAILABLE = "provider_unavailable"
STATE_CONTEXT_REJECTED = "context_rejected"
STATE_PROVIDER_FAILURE = "provider_failure"

STATES = frozenset(
    {
        STATE_PREFLIGHT,
        STATE_CANCEL_REQUESTED,
        STATE_STALE,
        STATE_CLARIFICATION_REQUIRED,
        STATE_UNSUPPORTED,
        STATE_INVALID_OUTPUT,
        STATE_USAGE_UNKNOWN,
        STATE_PRICING_UNKNOWN,
        STATE_RESERVATION_FAILED,
        STATE_RUNNER_UNAVAILABLE,
        STATE_VERIFICATION_FAILED,
        STATE_OVER_LIMIT,
        STATE_REVIEWABLE_CANDIDATE,
        STATE_CREDENTIAL_MISSING,
        STATE_NETWORK_DENIED,
        STATE_TIMEOUT,
        STATE_RATE_LIMITED,
        STATE_QUOTA_EXCEEDED,
        STATE_PROVIDER_UNAVAILABLE,
        STATE_CONTEXT_REJECTED,
        STATE_PROVIDER_FAILURE,
    }
)

# Bounded refusal reasons (never interpolate caller content).
REASON_TOO_LARGE = "request exceeds a bounded limit"
REASON_PRICING_UNKNOWN = "pricing for the allowlisted model is unknown"
REASON_RESERVATION_INSUFFICIENT = "the reservation does not cover the worst case"
REASON_NOT_MAPPING = "output is not a mapping"
REASON_UNSUPPORTED_SCHEMA = "unsupported schema_version"
REASON_UNKNOWN_OUTCOME = "unknown outcome"
REASON_MISSING_DELTA = "delta outcome is missing its delta"
REASON_MISSING_NOTES = "outcome is missing its notes"
REASON_INVALID_NOTES = "outcome notes are invalid"
REASON_UNEXPECTED_FIELD = "outcome carries an unexpected field"

# Allowlist of the exact top-level keys the provider output may carry.
_ALLOWED_OUTPUT_KEYS = frozenset(
    {
        "schema_version",
        "outcome",
        "delta",
        "clarification_questions",
        "unsupported_requirements",
    }
)


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def estimate_tokens(text: str) -> int:
    """Return a deterministic, conservative upper-bound token estimate.

    A common safe approximation for English text is ~4 bytes per token; using
    the byte length over 4 (rounded up) slightly over-estimates, which fails
    safe against the input-token cap.
    """
    if not isinstance(text, str):
        return 0
    return math.ceil(len(text.encode("utf-8")) / 4)


# -- code-owned synthetic examples (few-shot) --------------------------------
#
# At most :data:`MAX_EXAMPLES` hand-authored, synthetic, non-sensitive examples
# mapping a requirement to its rule delta. They are code-owned constants — never
# drawn from a repository, a document or a provider. Each is
# ``(requirement, delta)``.

_EXAMPLES: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    (
        "Members receive a 10% discount on quotations.",
        {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "result_kind": rule_delta.RESULT_KIND_QUOTATION,
            "changes": [
                {
                    "operation": rule_delta.OPERATION_SET_PARAMETER,
                    "rule_id": rule_delta.RESULT_KIND_QUOTATION,
                    "parameter_id": "member_discount_rate",
                    "value": "0.10",
                }
            ],
        },
    ),
    (
        "The member discount should be removed entirely.",
        {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "result_kind": rule_delta.RESULT_KIND_QUOTATION,
            "changes": [
                {
                    "operation": rule_delta.OPERATION_SET_PARAMETER,
                    "rule_id": rule_delta.RESULT_KIND_QUOTATION,
                    "parameter_id": "member_discount_rate",
                    "value": "0",
                }
            ],
        },
    ),
    (
        "Cap late return fees at 24.",
        {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "result_kind": rule_delta.RESULT_KIND_LATE_RETURN_FEE,
            "changes": [
                {
                    "operation": rule_delta.OPERATION_SET_PARAMETER,
                    "rule_id": rule_delta.RESULT_KIND_LATE_RETURN_FEE,
                    "parameter_id": "cap",
                    "value": "24",
                }
            ],
        },
    ),
)


def examples() -> List[Dict[str, Any]]:
    """Return the bounded code-owned synthetic examples as ``{requirement, delta}``."""
    return [{"requirement": r, "delta": d} for r, d in _EXAMPLES]


def _render_allowlist() -> str:
    """Render the code-owned rule/parameter allowlist as deterministic prose."""
    lines: List[str] = []
    for result_kind in sorted(rule_delta.RULE_PARAMETERS):
        lines.append(f"rule {result_kind}:")
        for parameter_id in sorted(rule_delta.RULE_PARAMETERS[result_kind]):
            spec = rule_delta.RULE_PARAMETERS[result_kind][parameter_id]
            lines.append(
                f"  set_parameter parameter_id={parameter_id} "
                f"(min {spec['min']}, max {spec['max']}, default {spec['default']})"
            )
    return "\n".join(lines)


def _render_baseline() -> str:
    """Render the current code-owned baseline (default) parameter values."""
    lines: List[str] = []
    for result_kind in sorted(rule_delta.RULE_PARAMETERS):
        for parameter_id in sorted(rule_delta.RULE_PARAMETERS[result_kind]):
            spec = rule_delta.RULE_PARAMETERS[result_kind][parameter_id]
            lines.append(
                f"{result_kind}.{parameter_id} = {spec['default']}"
            )
    return "\n".join(lines)


def _render_examples() -> str:
    """Render the code-owned synthetic examples as deterministic prose."""
    lines: List[str] = []
    for index, (requirement, delta) in enumerate(_EXAMPLES, start=1):
        lines.append(f"Example {index}:")
        lines.append(f"  requirement: {requirement}")
        lines.append(f"  delta: {dumps(delta)}")
    return "\n".join(lines)


def build_instruction() -> str:
    """Return the fixed, code-owned interpretation instruction (system prompt).

    It carries the rule_delta schema, the rule/parameter allowlist, the current
    baseline values and at most eight synthetic examples. It contains no source
    code, no repository or absolute path, no Code Map/Twin content, no chat
    history, no credential, no log and no expected output.
    """
    return (
        "You are a read-only rule interpreter for a bounded quotation engine. "
        "Given one short requirement, return a single JSON object (and only that "
        "object) with exactly one of three outcomes.\n"
        "\n"
        "The allowed change schema (version "
        + rule_delta.RULE_DELTA_SCHEMA_VERSION
        + ") is data-only: a delta names a \"result_kind\" and a list of "
        "\"changes\", where each change is {\"operation\": \"set_parameter\", "
        "\"rule_id\": <result_kind>, \"parameter_id\": <id>, \"value\": <decimal "
        "string>}. No code, command, import, dependency, path, mount, "
        "environment, network, UI, runtime or verifier field exists.\n"
        "\n"
        "Allowlisted rules and parameters:\n"
        + _render_allowlist()
        + "\n"
        "Current baseline values:\n"
        + _render_baseline()
        + "\n"
        "Synthetic examples:\n"
        + _render_examples()
        + "\n"
        "Return exactly one of:\n"
        ' 1. {\"schema_version\": \"'
        + RULE_DELTA_INTERPRET_SCHEMA_VERSION
        + '", \"outcome\": \"delta\", \"delta\": {<a valid delta>}} when the '
        "requirement maps to an allowlisted change;\n"
        ' 2. {\"schema_version\": \"'
        + RULE_DELTA_INTERPRET_SCHEMA_VERSION
        + '", \"outcome\": \"clarification_required\", '
        "\"clarification_questions\": [<strings>]} when the requirement is "
        "insufficient or ambiguous;\n"
        ' 3. {\"schema_version\": \"'
        + RULE_DELTA_INTERPRET_SCHEMA_VERSION
        + '", \"outcome\": \"unsupported\", "unsupported_requirements\": '
        "[<strings>]} when the requirement cannot be expressed by the "
        "allowlist.\n"
        "\n"
        "Never invent a change outside the allowlist; never emit prose, markdown "
        "fences, code, commands, or a delta you are not confident is exactly "
        "intended. If uncertain, ask for clarification rather than guessing."
    )


# -- interpretation token ---------------------------------------------------


def interpretation_token(
    *,
    document_id: str,
    revision_id: str,
    fingerprint: str,
    baseline_fingerprint: Optional[str],
    requirement_text: str,
) -> str:
    """Return a content-addressed token binding the full interpretation scope.

    The token binds the document identity, head revision, content fingerprint,
    accepted baseline and the exact requirement text, so a changed document or a
    changed accepted predecessor makes any prepared token stale.
    """
    canon = dumps(
        {
            "document_id": document_id,
            "revision_id": revision_id,
            "fingerprint": fingerprint,
            "baseline_fingerprint": baseline_fingerprint,
            "requirement": requirement_text,
        }
    )
    return "delta:" + sha256_hex(canon.encode("utf-8"))


# -- preflight / disclosure -------------------------------------------------


def pricing_for(model: str) -> Optional[Dict[str, Decimal]]:
    """Return the peak price rates for ``model``, or ``None`` when unknown."""
    return PRICING.get(model)


def worst_case_cost_usd() -> Optional[Decimal]:
    """Return the worst-case request cost, or ``None`` when pricing is unknown."""
    rates = pricing_for(MODEL_ID)
    if rates is None:
        return None
    input_cost = (Decimal(MAX_INPUT_TOKENS) / Decimal(1_000_000)) * rates["input_usd_per_1m"]
    output_cost = (Decimal(MAX_OUTPUT_TOKENS) / Decimal(1_000_000)) * rates["output_usd_per_1m"]
    return input_cost + output_cost


def usage_within_reservation(usage: Dict[str, Optional[int]]) -> bool:
    """Return True when reported usage is within the reserved amount.

    ``usage`` is the provider-reported token counts; the cost is recomputed from
    the code-owned peak rates. Unknown pricing, or a cost that exceeds the
    reservation, returns False (fail closed). This is the runtime "reservation
    / price-drift" guard: a reported usage beyond the caps would trip it.
    """
    rates = pricing_for(MODEL_ID)
    if rates is None:
        return False
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, bool) or isinstance(completion, bool):
        return False
    prompt_tokens = prompt if isinstance(prompt, int) else 0
    completion_tokens = completion if isinstance(completion, int) else 0
    input_cost = (Decimal(prompt_tokens) / Decimal(1_000_000)) * rates["input_usd_per_1m"]
    output_cost = (Decimal(completion_tokens) / Decimal(1_000_000)) * rates["output_usd_per_1m"]
    return (input_cost + output_cost) <= RESERVATION_USD


def reservation_record() -> Dict[str, Any]:
    """Return the itemized cost reservation record for the disclosure.

    ``sufficient`` is True only when pricing is known and the worst-case cost is
    at or below the reserved amount; ``worst_case_cost_usd`` is ``None`` when
    pricing is unknown (which fails closed downstream).
    """
    cost = worst_case_cost_usd()
    rates = pricing_for(MODEL_ID)
    return {
        "amount_usd": str(RESERVATION_USD),
        "worst_case_cost_usd": str(cost) if cost is not None else None,
        "input_rate_usd_per_1m": str(rates["input_usd_per_1m"]) if rates else None,
        "output_rate_usd_per_1m": str(rates["output_usd_per_1m"]) if rates else None,
        "sufficient": cost is not None and cost <= RESERVATION_USD,
    }


def request_too_large(instruction: str, requirement_text: str) -> bool:
    """Return True when the instruction plus requirement exceeds a bounded limit.

    Enforces both the serialized request-byte cap and the input-token cap using
    a deterministic estimate. The full system-plus-user payload is what the
    transport serializes, so both are bounded here before any network access.
    """
    total = instruction + "\n\n" + requirement_text
    if len(total.encode("utf-8")) > MAX_REQUEST_BYTES:
        return True
    if estimate_tokens(total) > MAX_INPUT_TOKENS:
        return True
    return False


def build_disclosure(*, requirement_text: str) -> Dict[str, Any]:
    """Build the visible preflight disclosure manifest for one requirement.

    The only variable content is the selected saved requirement text; every
    other item is code-owned. The manifest shows the exact recipient/model, the
    itemized outgoing content, the policy/retention warning, the worst-case
    reservation, the one-attempt limits and the data-egress statement.
    """
    instruction = build_instruction()
    total_bytes = len((instruction + "\n\n" + requirement_text).encode("utf-8"))
    reservation = reservation_record()
    allowlist_rules: List[Dict[str, Any]] = []
    for result_kind in sorted(rule_delta.RULE_PARAMETERS):
        params = []
        for parameter_id in sorted(rule_delta.RULE_PARAMETERS[result_kind]):
            spec = rule_delta.RULE_PARAMETERS[result_kind][parameter_id]
            params.append(
                {
                    "parameter_id": parameter_id,
                    "min": spec["min"],
                    "max": spec["max"],
                    "default": spec["default"],
                }
            )
        allowlist_rules.append({"result_kind": result_kind, "parameters": params})

    return {
        "schema_version": RULE_DELTA_INTERPRET_SCHEMA_VERSION,
        "provider_id": PROVIDER_ID,
        "model": MODEL_ID,
        "one_attempt": True,
        "no_retry": True,
        "no_paid_repair": True,
        "egress_statement": _EGRESS_STATEMENT,
        "policy_warning": _POLICY_WARNING,
        "account_cap_statement": _ACCOUNT_CAP_STATEMENT,
        "caps": {
            "request_bytes": MAX_REQUEST_BYTES,
            "input_tokens": MAX_INPUT_TOKENS,
            "output_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "workflow_timeout_seconds": WORKFLOW_TIMEOUT_SECONDS,
        },
        "reservation": reservation,
        "allowlist": {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "rules": allowlist_rules,
        },
        "items": [
            {
                "kind": "instruction",
                "label": "code-owned rule-delta schema, allowlist, baseline and examples",
                "bytes": len(instruction.encode("utf-8")),
            },
            {
                "kind": "requirement",
                "label": "selected requirement text",
                "bytes": len(requirement_text.encode("utf-8")),
            },
        ],
        "total_bytes": total_bytes,
    }


def build_provider_request(
    *, token: str, requirement_text: str
) -> Dict[str, Any]:
    """Return the P2.5 provider request payload for one requirement.

    The task id is the content-addressed token (correlation + staleness
    protection); the task is the fixed instruction; the single context item is
    the selected requirement text.
    """
    return {
        "task_id": token,
        "task": build_instruction(),
        "context": [requirement_text],
    }


# -- provider output validation ---------------------------------------------


def _valid_notes(value: Any) -> bool:
    """Return True when ``value`` is a bounded list of non-empty strings."""
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_NOTES
        and all(
            isinstance(item, str) and 0 < len(item) <= MAX_NOTE_CHARS
            for item in value
        )
    )


def validate_provider_output(value: Any) -> Optional[str]:
    """Validate a provider output against the bounded interpretation contract.

    Accepts exactly one structured outcome and rejects a non-mapping, a wrong
    schema version, an unknown outcome, a missing/mismatched payload, malformed
    notes, or any field outside the fixed allowlist (implicitly rejecting prose,
    code, script, commands, imports, paths, and extra fields).
    """
    if not isinstance(value, dict):
        return REASON_NOT_MAPPING
    if set(value) - _ALLOWED_OUTPUT_KEYS:
        return REASON_UNEXPECTED_FIELD
    if value.get("schema_version") != RULE_DELTA_INTERPRET_SCHEMA_VERSION:
        return REASON_UNSUPPORTED_SCHEMA

    outcome = value.get("outcome")
    if outcome not in OUTCOMES:
        return REASON_UNKNOWN_OUTCOME

    has_delta = value.get("delta") is not None
    has_clarification = value.get("clarification_questions") is not None
    has_unsupported = value.get("unsupported_requirements") is not None

    if outcome == OUTCOME_DELTA:
        if has_clarification or has_unsupported:
            return REASON_UNEXPECTED_FIELD
        if not isinstance(value.get("delta"), dict):
            return REASON_MISSING_DELTA
        if rule_delta.validate_delta(value["delta"]) is not None:
            return REASON_INVALID_NOTES
        return None

    if outcome == OUTCOME_CLARIFICATION_REQUIRED:
        if has_delta or has_unsupported:
            return REASON_UNEXPECTED_FIELD
        if not _valid_notes(value.get("clarification_questions")):
            return REASON_MISSING_NOTES
        return None

    # unsupported
    if has_delta or has_clarification:
        return REASON_UNEXPECTED_FIELD
    if not _valid_notes(value.get("unsupported_requirements")):
        return REASON_MISSING_NOTES
    return None


def normalize_output(value: Dict[str, Any]) -> Dict[str, Any]:
    """Return the normalized outcome from a validated provider output.

    Only the known fields are carried forward; an extra top-level key is already
    rejected by :func:`validate_provider_output`. For a delta outcome the delta
    is reduced to its canonical serialization round-trip.
    """
    outcome = value["outcome"]
    normalized: Dict[str, Any] = {"outcome": outcome}
    if outcome == OUTCOME_DELTA:
        normalized["delta"] = value["delta"]
    elif outcome == OUTCOME_CLARIFICATION_REQUIRED:
        normalized["clarification_questions"] = list(value["clarification_questions"])
    else:
        normalized["unsupported_requirements"] = list(value["unsupported_requirements"])
    return normalized


# -- result assembly ----------------------------------------------------------


def assemble_result(
    *,
    state: str,
    token: Optional[str],
    sent: bool,
    usage: Optional[Dict[str, Optional[int]]],
    candidate: Optional[Dict[str, Any]],
    limitations: List[str],
) -> Dict[str, Any]:
    """Assemble the versioned interpretation result envelope.

    ``candidate`` is present only in the ``reviewable_candidate`` state; it is
    the validated, runner-executed, independently-verified delta candidate and
    never an unverified provider payload. On every failure state the current
    Accepted Version is preserved and ``candidate`` is ``None``.
    """
    return {
        "schema_version": RULE_DELTA_INTERPRET_SCHEMA_VERSION,
        "generator": RULE_DELTA_INTERPRET_GENERATOR,
        "state": state,
        "provider_id": PROVIDER_ID,
        "model": MODEL_ID,
        "token": token,
        "sent": bool(sent),
        "usage": usage,
        "candidate": candidate,
        "limitations": list(limitations),
    }


__all__ = [
    "RULE_DELTA_INTERPRET_SCHEMA_VERSION",
    "RULE_DELTA_INTERPRET_GENERATOR",
    "PROVIDER_ID",
    "MODEL_ID",
    "OUTCOME_DELTA",
    "OUTCOME_CLARIFICATION_REQUIRED",
    "OUTCOME_UNSUPPORTED",
    "OUTCOMES",
    "MAX_REQUEST_BYTES",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "TIMEOUT_SECONDS",
    "WORKFLOW_TIMEOUT_SECONDS",
    "MAX_EXAMPLES",
    "MAX_NOTES",
    "MAX_NOTE_CHARS",
    "PRICING",
    "RESERVATION_USD",
    "STATE_PREFLIGHT",
    "STATE_CANCEL_REQUESTED",
    "STATE_STALE",
    "STATE_CLARIFICATION_REQUIRED",
    "STATE_UNSUPPORTED",
    "STATE_INVALID_OUTPUT",
    "STATE_USAGE_UNKNOWN",
    "STATE_PRICING_UNKNOWN",
    "STATE_RESERVATION_FAILED",
    "STATE_RUNNER_UNAVAILABLE",
    "STATE_VERIFICATION_FAILED",
    "STATE_OVER_LIMIT",
    "STATE_REVIEWABLE_CANDIDATE",
    "STATE_CREDENTIAL_MISSING",
    "STATE_NETWORK_DENIED",
    "STATE_TIMEOUT",
    "STATE_RATE_LIMITED",
    "STATE_QUOTA_EXCEEDED",
    "STATE_PROVIDER_UNAVAILABLE",
    "STATE_CONTEXT_REJECTED",
    "STATE_PROVIDER_FAILURE",
    "STATES",
    "REASON_TOO_LARGE",
    "REASON_PRICING_UNKNOWN",
    "REASON_RESERVATION_INSUFFICIENT",
    "REASON_NOT_MAPPING",
    "REASON_UNSUPPORTED_SCHEMA",
    "REASON_UNKNOWN_OUTCOME",
    "REASON_MISSING_DELTA",
    "REASON_MISSING_NOTES",
    "REASON_INVALID_NOTES",
    "REASON_UNEXPECTED_FIELD",
    "dumps",
    "sha256_hex",
    "estimate_tokens",
    "examples",
    "build_instruction",
    "interpretation_token",
    "pricing_for",
    "worst_case_cost_usd",
    "usage_within_reservation",
    "reservation_record",
    "request_too_large",
    "build_disclosure",
    "build_provider_request",
    "validate_provider_output",
    "normalize_output",
    "assemble_result",
]
