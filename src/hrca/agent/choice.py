"""The typed agent-choice payload contract (AGENT-LOOP-1).

This module owns the *closed* shape of one model-chosen next action. It is the
agent-loop sibling of :mod:`hrca.integrations.advisory_contract`: a stdlib-only
leaf that declares the schema version an answer must carry, the bounds an answer
is held to, and the two functions that decide whether an answer is acceptable
and what of it may be carried forward.

What a model may say is bounded here, never in the prompt alone. A model
returns one of five choices — ``clarify``, ``answer``, ``read_record``,
``propose_scan`` or ``stop`` — each with a fixed argument allowlist. Anything
outside that shape is refused with a bounded reason and produces no effect. The
validator never decides what an accepted choice may *do*: that is
:mod:`hrca.agent.policy`, and the effect itself is :mod:`hrca.agent.service`.

This module imports only the standard library and nothing else from ``hrca``. It
performs no I/O, reads no clock, opens no store or socket, reads no credential,
constructs no provider client, and defines no default a caller could widen.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The agent-choice payload schema version. A model answer declaring anything
#: else is refused rather than read.
AGENT_CHOICE_SCHEMA_VERSION = "1.0.0"

# -- the closed choice vocabulary ----------------------------------------

CHOICE_CLARIFY = "clarify"
CHOICE_ANSWER = "answer"
CHOICE_READ_RECORD = "read_record"
CHOICE_PROPOSE_SCAN = "propose_scan"
CHOICE_STOP = "stop"

CHOICES: Tuple[str, ...] = (
    CHOICE_CLARIFY,
    CHOICE_ANSWER,
    CHOICE_READ_RECORD,
    CHOICE_PROPOSE_SCAN,
    CHOICE_STOP,
)

# -- bounds (code-owned; never user-configurable) -------------------------

MAX_RATIONALE_CHARS = 600
MAX_TEXT_CHARS = 1200
MAX_REFERENCE_CHARS = 200
MAX_REFERENCES = 8
MAX_ARGUMENT_KEYS = 4
MAX_GOAL_CHARS = 400

#: The top-level keys an answer may carry. Nothing else is read.
TOP_LEVEL_KEYS: Tuple[str, ...] = ("schema_version", "choice", "rationale", "arguments")

#: Per-choice argument allowlist. A key outside the choice's tuple is refused
#: rather than ignored, so a model cannot smuggle an unmodelled instruction.
ARGUMENT_KEYS: Dict[str, Tuple[str, ...]] = {
    CHOICE_CLARIFY: ("question",),
    CHOICE_ANSWER: ("answer", "references"),
    CHOICE_READ_RECORD: ("record",),
    CHOICE_PROPOSE_SCAN: ("goal",),
    CHOICE_STOP: ("limitation",),
}

#: The closed set of records a ``read_record`` choice may name. The executor
#: still validates the name against the backend's own reader registry; this
#: tuple only states what the payload layer will carry at all.
ALLOWED_RECORDS: Tuple[str, ...] = ("workflow",)

REASON_NOT_MAPPING = "the agent answer is not a JSON object"
REASON_SCHEMA = "the agent answer declares an unsupported schema version"
REASON_UNKNOWN_CHOICE = "the agent answer names an unknown choice"
REASON_RATIONALE = "the agent answer's rationale is missing or too long"
REASON_ARGUMENTS = "the agent answer's arguments are missing or not an object"
REASON_ARGUMENT_KEYS = "the agent answer carries an argument this choice does not allow"
REASON_ARGUMENT_MISSING = "the agent answer omits a required argument"
REASON_ARGUMENT_TEXT = "an agent answer argument is not bounded text"
REASON_ARGUMENT_RECORD = "the agent answer names a record this slice does not read"
REASON_REFERENCES = "the agent answer's references are not a bounded list of strings"


def _bounded_text(value: Any, limit: int, *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if not allow_empty and not value.strip():
        return False
    return len(value) <= limit


def validate_choice_payload(payload: Any) -> Optional[str]:
    """Return a bounded reason the payload is unacceptable, or ``None``.

    Acceptance is deliberately strict: an unknown top-level key, an unknown
    choice, an unmodelled argument, an unbounded string or a record outside
    :data:`ALLOWED_RECORDS` is refused rather than coerced.
    """
    if not isinstance(payload, dict):
        return REASON_NOT_MAPPING
    if payload.get("schema_version") != AGENT_CHOICE_SCHEMA_VERSION:
        return REASON_SCHEMA
    choice = payload.get("choice")
    if choice not in CHOICES:
        return REASON_UNKNOWN_CHOICE
    if set(payload) - set(TOP_LEVEL_KEYS):
        return REASON_ARGUMENT_KEYS
    if not _bounded_text(payload.get("rationale", ""), MAX_RATIONALE_CHARS, allow_empty=True):
        return REASON_RATIONALE

    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        return REASON_ARGUMENTS
    allowed = ARGUMENT_KEYS[choice]
    if len(arguments) > MAX_ARGUMENT_KEYS or set(arguments) - set(allowed):
        return REASON_ARGUMENT_KEYS

    if choice == CHOICE_CLARIFY:
        if not _bounded_text(arguments.get("question"), MAX_TEXT_CHARS):
            return REASON_ARGUMENT_MISSING
    elif choice == CHOICE_ANSWER:
        if not _bounded_text(arguments.get("answer"), MAX_TEXT_CHARS):
            return REASON_ARGUMENT_MISSING
        references = arguments.get("references", [])
        if not isinstance(references, (list, tuple)):
            return REASON_REFERENCES
        if len(references) > MAX_REFERENCES:
            return REASON_REFERENCES
        for reference in references:
            if not _bounded_text(reference, MAX_REFERENCE_CHARS):
                return REASON_REFERENCES
    elif choice == CHOICE_READ_RECORD:
        record = arguments.get("record")
        if not _bounded_text(record, MAX_REFERENCE_CHARS):
            return REASON_ARGUMENT_MISSING
        if record not in ALLOWED_RECORDS:
            return REASON_ARGUMENT_RECORD
    elif choice == CHOICE_PROPOSE_SCAN:
        goal = arguments.get("goal", "")
        if not _bounded_text(goal, MAX_GOAL_CHARS, allow_empty=True):
            return REASON_ARGUMENT_TEXT
    elif choice == CHOICE_STOP:
        limitation = arguments.get("limitation", "")
        if not _bounded_text(limitation, MAX_TEXT_CHARS, allow_empty=True):
            return REASON_ARGUMENT_TEXT
    return None


def normalize_choice_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical accepted subset of an already-validated payload.

    Only the keys named here survive. A caller must call
    :func:`validate_choice_payload` first; this function assumes acceptance and
    never widens the shape.
    """
    choice = payload["choice"]
    arguments = payload.get("arguments") or {}
    normalized: Dict[str, Any] = {
        "schema_version": AGENT_CHOICE_SCHEMA_VERSION,
        "choice": choice,
        "rationale": str(payload.get("rationale") or ""),
        "arguments": {},
    }
    if choice == CHOICE_CLARIFY:
        normalized["arguments"] = {"question": str(arguments["question"])}
    elif choice == CHOICE_ANSWER:
        normalized["arguments"] = {
            "answer": str(arguments["answer"]),
            "references": [str(item) for item in (arguments.get("references") or [])],
        }
    elif choice == CHOICE_READ_RECORD:
        normalized["arguments"] = {"record": str(arguments["record"])}
    elif choice == CHOICE_PROPOSE_SCAN:
        normalized["arguments"] = {"goal": str(arguments.get("goal") or "")}
    elif choice == CHOICE_STOP:
        normalized["arguments"] = {"limitation": str(arguments.get("limitation") or "")}
    return normalized


__all__ = [
    "AGENT_CHOICE_SCHEMA_VERSION",
    "CHOICE_CLARIFY",
    "CHOICE_ANSWER",
    "CHOICE_READ_RECORD",
    "CHOICE_PROPOSE_SCAN",
    "CHOICE_STOP",
    "CHOICES",
    "MAX_RATIONALE_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_REFERENCE_CHARS",
    "MAX_REFERENCES",
    "MAX_GOAL_CHARS",
    "TOP_LEVEL_KEYS",
    "ARGUMENT_KEYS",
    "ALLOWED_RECORDS",
    "REASON_NOT_MAPPING",
    "REASON_SCHEMA",
    "REASON_UNKNOWN_CHOICE",
    "REASON_RATIONALE",
    "REASON_ARGUMENTS",
    "REASON_ARGUMENT_KEYS",
    "REASON_ARGUMENT_MISSING",
    "REASON_ARGUMENT_TEXT",
    "REASON_ARGUMENT_RECORD",
    "REASON_REFERENCES",
    "validate_choice_payload",
    "normalize_choice_payload",
]
