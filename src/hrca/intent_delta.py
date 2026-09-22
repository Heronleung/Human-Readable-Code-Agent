"""Typed developer Intent Delta (P5.3).

This module records **what a developer asked for**, as a versioned, canonical,
non-executable document. It is the first half of the P5.3 contract; the second
half is :mod:`hrca.impact_proposal`, which turns a delta plus bound read-side
evidence into a deterministic advisory impact proposal.

Relation to the P3.4/P4.1 Intent Delta
--------------------------------------

:mod:`hrca.codemap_draft` already derives an "Intent Delta" — but that one is
*drafted from typed Code Map block operations* against a Code Map baseline.
Every field it carries is computed by the tool from a block edit, including its
acceptance criterion. This module is the other thing: a record of developer
intent whose required facts are **supplied, never derived**. Sharing the name
would be misleading, so this contract names its own generator
(``hrca-developer-intent``) and its own schema, and :mod:`hrca.impact_proposal`
imports neither :mod:`hrca.codemap_draft` nor :mod:`hrca.proposal`.

Explicit facts, never inferred ones
-----------------------------------

Requirement 3 of P5.3 is that *required facts are supplied explicitly*: scope,
acceptance criteria, authority and baseline are never inferred from prose. So:

* every required section must be **present** in the input. An absent section is
  refused, even when a request is obviously implied by the surrounding text;
* an **explicitly empty** list is a supplied answer ("there are no constraints
  I know of") and is accepted for ``constraints``, ``assumptions`` and
  ``unresolved_questions``;
* ``requested_outcome``, the scope, ``acceptance_criteria`` and
  ``origin.evidence`` must name **at least one** entry. An intent with no
  outcome, no target, no acceptance criterion or no evidence reference is not a
  bounded intent, and refusing it is the honest answer.

Prose is input data
-------------------

``prose`` is carried as-is at the caller's option, marked
``authority: "input_data_only"``. It is never parsed, never searched, and never
substituted for a missing required fact. It is not echoed into an impact
proposal, because free-form text the developer pasted is exactly the kind of
thing that must not be re-published by a downstream document.

Purity
------

Standard library plus :func:`hrca.identity.sha256_hex` for content addressing. No
filesystem access, no network, no model, no provider, no credential, no
command, no Git, no repository write. Nothing here can create a candidate, a
diff, a branch or a commit: ``executable`` and ``applied`` are always ``False``.

Determinism
-----------

``dumps`` is the canonical serialization (sorted keys, compact separators,
ASCII-safe). ``scope`` and ``origin.evidence`` are sets, so they are sorted and
de-duplicated; the authored statement lists keep their authored order, because
in a checklist the order is meaning, and de-duplicate on first occurrence.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from .identity import sha256_hex

INTENT_DELTA_SCHEMA_VERSION = "1.0.0"
INTENT_DELTA_GENERATOR = "hrca-developer-intent"
INTENT_ID_PREFIX = "intent:"

# The only origin an intent may declare: a human wrote it. A provider- or
# model-authored intent is out of scope for this contract entirely.
ORIGIN_DEVELOPER_AUTHORED = "developer_authored"

# The bounded vocabulary of exact read-side identity spaces an evidence
# reference may name. Every one of them is an identity the accepted
# architecture already publishes: a Twin artifact id, a scanner symbol id, or a
# scanner file path (workspace-relative, exactly as the scanner records it).
REF_TWIN_ARTIFACT = "twin_artifact"
REF_SCANNER_SYMBOL = "scanner_symbol"
REF_SCANNER_FILE = "scanner_file"
EVIDENCE_REF_KINDS = frozenset(
    {REF_TWIN_ARTIFACT, REF_SCANNER_SYMBOL, REF_SCANNER_FILE}
)

PROSE_AUTHORITY_INPUT_ONLY = "input_data_only"

# Bounded input limits. Every one is a refusal, never a truncation: a silently
# shortened value is a value the developer did not write.
MAX_OUTCOME_CHARS = 2000
MAX_ITEM_CHARS = 500
MAX_LIST_ITEMS = 100
MAX_PROSE_CHARS = 8000
MAX_SCOPE_REFS = 200
MAX_EVIDENCE_REFS = 200

# Bounded reasons. Field names are this module's own vocabulary; a caller value
# is never interpolated into a reason.
REASON_NOT_MAPPING = "intent is not a mapping"
REASON_MISSING_VERSION = "missing schema_version"
REASON_INVALID_VERSION = "invalid schema_version"
REASON_NEWER_VERSION = "schema_version is newer than supported"
REASON_NOT_MIGRATABLE = "schema_version is not migratable"
REASON_INVALID_DELTA = "intent delta is not valid"

# Sections every intent must supply. Naming the missing section is safe: the
# name comes from this tuple, never from the caller.
REQUIRED_SECTIONS = (
    "origin",
    "baseline",
    "requested_outcome",
    "scope",
    "constraints",
    "acceptance_criteria",
    "assumptions",
    "unresolved_questions",
)

_VERSION_KEYS = ("workspace_id", "scan_generation", "baseline_fingerprint",
                 "scanner_schema_version", "grammar")

# Pure migrations map an older ``schema_version`` to an upgrade function. There
# are no historical versions before 1.0.0, so the registry is empty; it exists
# so a later phase can add one without changing the read path. A document with a
# *future* (or unknown) version is never migrated and never half-read.
MIGRATIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def dumps(obj: Any) -> str:
    """Serialize an intent delta canonically (sorted keys, compact, ASCII-safe)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


# -- bounded field helpers -------------------------------------------------


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Refusal(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise _Refusal(f"{field} is oversized")
    return text


def _statement_list(value: Any, field: str, *, required: bool) -> List[str]:
    """Normalize an authored statement list, keeping authored order.

    ``required`` distinguishes "at least one statement" (an acceptance-criteria
    list with nothing in it is not an acceptance criterion) from "explicitly
    none" (a developer may legitimately have no known constraints).
    """
    if not isinstance(value, list):
        raise _Refusal(f"{field} must be a list of statements")
    if len(value) > MAX_LIST_ITEMS:
        raise _Refusal(f"{field} is oversized")
    out: List[str] = []
    for item in value:
        out.append(_text(item, field, MAX_ITEM_CHARS))
    if required and not out:
        raise _Refusal(f"{field} must name at least one entry")
    # De-duplicate on first occurrence; a repeated statement is the same
    # statement, and dropping the repeat invents nothing.
    seen: set = set()
    unique: List[str] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _sorted_refs(value: Any, field: str) -> List[str]:
    """Normalize a set-valued reference list: sorted, de-duplicated, non-empty."""
    if not isinstance(value, list):
        raise _Refusal(f"{field} must be a list of identifiers")
    if len(value) > MAX_SCOPE_REFS:
        raise _Refusal(f"{field} is oversized")
    seen: set = set()
    for item in value:
        seen.add(_text(item, field, MAX_ITEM_CHARS))
    return sorted(seen)


class _Refusal(ValueError):
    """Internal: a bounded refusal carrying a safe reason string."""


# -- section normalization -------------------------------------------------


def _normalize_baseline(value: Any) -> Dict[str, Any]:
    """Validate the exact baseline identity an intent is authored against.

    Every field here is an identity the read side already publishes, so the
    binding in :mod:`hrca.impact_proposal` compares like with like. No path is
    accepted or recorded: ``workspace_id`` is the Twin's canonical workspace
    identity, and the root it was derived from never enters this contract.
    """
    if not isinstance(value, dict):
        raise _Refusal("baseline must be a mapping")
    missing = [key for key in _VERSION_KEYS if key not in value]
    if missing:
        raise _Refusal(f"baseline is missing {missing[0]}")

    workspace_id = _text(value["workspace_id"], "baseline.workspace_id", MAX_ITEM_CHARS)
    fingerprint = _text(
        value["baseline_fingerprint"], "baseline.baseline_fingerprint", MAX_ITEM_CHARS
    )
    schema_version = _text(
        value["scanner_schema_version"], "baseline.scanner_schema_version", 32
    )

    generation = value["scan_generation"]
    # ``bool`` is an ``int`` subclass; a boolean generation is a caller error.
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise _Refusal("baseline.scan_generation must be an integer")
    if generation < 0:
        raise _Refusal("baseline.scan_generation must not be negative")

    grammar = value["grammar"]
    if not isinstance(grammar, dict):
        raise _Refusal("baseline.grammar must be a mapping")
    for key in ("implementation", "version"):
        if key not in grammar:
            raise _Refusal(f"baseline.grammar is missing {key}")

    return {
        "workspace_id": workspace_id,
        "scan_generation": generation,
        "baseline_fingerprint": fingerprint,
        "scanner_schema_version": schema_version,
        "grammar": {
            "implementation": _text(
                grammar["implementation"], "baseline.grammar.implementation", 32
            ),
            "version": _text(grammar["version"], "baseline.grammar.version", 32),
        },
    }


def _normalize_scope(value: Any) -> Dict[str, List[str]]:
    """Validate the bounded scope: exact identity references, never patterns.

    A scope must name at least one target, and it may name targets as entity
    references, as exact artifact ids, or as both. Either route alone is a
    bounded scope: an artifact id is the precise route an author uses when an
    entity reference would be ambiguous, so refusing an artifact-only scope
    would remove the only way to say exactly which artifact is meant.
    """
    if not isinstance(value, dict):
        raise _Refusal("scope must be a mapping")
    for key in ("entities", "artifacts"):
        if key not in value:
            raise _Refusal(f"scope is missing {key}")
    entities = _sorted_refs(value["entities"], "scope.entities")
    artifacts = _sorted_refs(value["artifacts"], "scope.artifacts")
    if not entities and not artifacts:
        raise _Refusal("scope must name at least one entity or artifact")
    return {"entities": entities, "artifacts": artifacts}


def _normalize_origin(value: Any) -> Dict[str, Any]:
    """Validate the origin and its exact evidence references."""
    if not isinstance(value, dict):
        raise _Refusal("origin must be a mapping")
    for key in ("kind", "evidence"):
        if key not in value:
            raise _Refusal(f"origin is missing {key}")
    kind = value["kind"]
    if kind != ORIGIN_DEVELOPER_AUTHORED:
        raise _Refusal("origin.kind must be developer_authored")

    raw = value["evidence"]
    if not isinstance(raw, list):
        raise _Refusal("origin.evidence must be a list of references")
    if len(raw) > MAX_EVIDENCE_REFS:
        raise _Refusal("origin.evidence is oversized")
    refs: List[Tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _Refusal("origin.evidence must be a list of references")
        for key in ("kind", "id"):
            if key not in item:
                raise _Refusal(f"an origin.evidence entry is missing {key}")
        ref_kind = item["kind"]
        if not isinstance(ref_kind, str) or ref_kind not in EVIDENCE_REF_KINDS:
            raise _Refusal("an origin.evidence entry names an unknown kind")
        refs.append((ref_kind, _text(item["id"], "origin.evidence.id", MAX_ITEM_CHARS)))
    if not refs:
        raise _Refusal("origin.evidence must name at least one entry")
    # References are a set: sorted by (kind, id) and de-duplicated.
    return {
        "kind": ORIGIN_DEVELOPER_AUTHORED,
        "evidence": [
            {"kind": kind_, "id": id_} for kind_, id_ in sorted(set(refs))
        ],
    }


def _normalize_prose(value: Any) -> Dict[str, Any]:
    """Carry optional free-form prose as data, with its authority fixed."""
    text: Optional[str] = None
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise _Refusal("prose must be a non-empty string when supplied")
        stripped = value.strip()
        if len(stripped) > MAX_PROSE_CHARS:
            raise _Refusal("prose is oversized")
        text = stripped
    return {"text": text, "authority": PROSE_AUTHORITY_INPUT_ONLY}


# -- building --------------------------------------------------------------


def intent_delta_id_for(delta: Dict[str, Any]) -> str:
    """Return the content-addressed identity of a delta (never time-derived)."""
    canon = dumps({k: v for k, v in delta.items() if k != "intent_delta_id"})
    return INTENT_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def build_intent_delta(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate explicitly supplied developer facts and build a canonical delta.

    Returns ``(delta, error)``. On success ``error`` is ``None``. On a missing
    or invalid required fact, ``delta`` is ``None`` and ``error`` is a bounded
    reason naming this module's own field vocabulary — never a caller value,
    never a path, never a fragment of prose.
    """
    try:
        if not isinstance(raw, dict):
            raise _Refusal(REASON_NOT_MAPPING)
        for section in REQUIRED_SECTIONS:
            if section not in raw:
                raise _Refusal(f"missing required section {section}")

        delta: Dict[str, Any] = {
            "schema_version": INTENT_DELTA_SCHEMA_VERSION,
            "generator": INTENT_DELTA_GENERATOR,
            "intent_delta_id": "",
            "origin": _normalize_origin(raw["origin"]),
            "baseline": _normalize_baseline(raw["baseline"]),
            "requested_outcome": _text(
                raw["requested_outcome"], "requested_outcome", MAX_OUTCOME_CHARS
            ),
            "scope": _normalize_scope(raw["scope"]),
            "constraints": _statement_list(
                raw["constraints"], "constraints", required=False
            ),
            "acceptance_criteria": _statement_list(
                raw["acceptance_criteria"], "acceptance_criteria", required=True
            ),
            "assumptions": _statement_list(
                raw["assumptions"], "assumptions", required=False
            ),
            "unresolved_questions": _statement_list(
                raw["unresolved_questions"], "unresolved_questions", required=False
            ),
            "prose": _normalize_prose(raw.get("prose")),
            # The same two invariants :mod:`hrca.proposal` holds. An intent is a
            # description; it is never a patch, an approval or an execution.
            "executable": False,
            "applied": False,
        }
        delta["intent_delta_id"] = intent_delta_id_for(delta)
        return delta, None
    except _Refusal as exc:
        return None, str(exc)


def migrate_intent_delta(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a serialized delta's declared version, fail-closed.

    Returns ``(raw, None)`` when the document declares the current version, and
    ``(None, reason)`` for a malformed document, a missing version, a version
    newer than this build, or an older version with no registered migration.
    """
    if not isinstance(raw, dict):
        return None, REASON_NOT_MAPPING
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, REASON_MISSING_VERSION
    try:
        current = _version_tuple(INTENT_DELTA_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, REASON_INVALID_VERSION
    if found == current:
        return raw, None
    if found > current:
        return None, REASON_NEWER_VERSION
    if version not in MIGRATIONS:
        return None, REASON_NOT_MIGRATABLE
    return MIGRATIONS[version](dict(raw)), None


# -- validation ------------------------------------------------------------


def _validate_built(delta: Dict[str, Any]) -> Optional[str]:
    """Re-derive every section from the delta's own fields; return a reason or None."""
    try:
        if delta.get("origin") != _normalize_origin(delta.get("origin")):
            return "origin is not canonical"
        if delta.get("baseline") != _normalize_baseline(delta.get("baseline")):
            return "baseline is not canonical"
        _text(delta.get("requested_outcome"), "requested_outcome", MAX_OUTCOME_CHARS)
        if delta.get("scope") != _normalize_scope(delta.get("scope")):
            return "scope is not canonical"
        for field, required in (
            ("constraints", False),
            ("acceptance_criteria", True),
            ("assumptions", False),
            ("unresolved_questions", False),
        ):
            if delta.get(field) != _statement_list(
                delta.get(field), field, required=required
            ):
                return f"{field} is not canonical"
        prose = delta.get("prose")
        if not isinstance(prose, dict) or set(prose) != {"text", "authority"}:
            return "prose is not canonical"
        if prose.get("authority") != PROSE_AUTHORITY_INPUT_ONLY:
            return "prose authority is not input_data_only"
        if prose != _normalize_prose(prose.get("text")):
            return "prose is not canonical"
    except _Refusal as exc:
        return str(exc)
    return None


def validate_intent_delta(delta: Any) -> Optional[str]:
    """Validate a built delta against the P5.3 schema; return a reason or ``None``.

    A valid delta declares the current schema and generator, carries an
    ``intent:``-prefixed identity that matches its own content, is canonically
    normalized section by section, and is neither executable nor applied.
    """
    if not isinstance(delta, dict):
        return REASON_INVALID_DELTA
    migrated, error = migrate_intent_delta(delta)
    if error is not None:
        return error
    if migrated is not delta:  # pragma: no cover - no migration is registered
        return REASON_INVALID_DELTA
    if delta.get("generator") != INTENT_DELTA_GENERATOR:
        return "unknown intent delta generator"
    if delta.get("executable") is not False:
        return "intent delta must be non-executable"
    if delta.get("applied") is not False:
        return "intent delta must be non-applied"
    for field in REQUIRED_SECTIONS:
        if field not in delta:
            return f"missing required section {field}"
    if "prose" not in delta:
        return "missing required section prose"
    reason = _validate_built(delta)
    if reason is not None:
        return reason
    identity = delta.get("intent_delta_id")
    if not isinstance(identity, str) or not identity.startswith(INTENT_ID_PREFIX):
        return "missing or malformed intent_delta_id"
    if identity != intent_delta_id_for(delta):
        return "intent_delta_id does not match the delta content"
    return None


__all__ = [
    "INTENT_DELTA_SCHEMA_VERSION",
    "INTENT_DELTA_GENERATOR",
    "INTENT_ID_PREFIX",
    "ORIGIN_DEVELOPER_AUTHORED",
    "REF_TWIN_ARTIFACT",
    "REF_SCANNER_SYMBOL",
    "REF_SCANNER_FILE",
    "EVIDENCE_REF_KINDS",
    "PROSE_AUTHORITY_INPUT_ONLY",
    "MAX_OUTCOME_CHARS",
    "MAX_ITEM_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_PROSE_CHARS",
    "MAX_SCOPE_REFS",
    "MAX_EVIDENCE_REFS",
    "REQUIRED_SECTIONS",
    "MIGRATIONS",
    "dumps",
    "build_intent_delta",
    "migrate_intent_delta",
    "validate_intent_delta",
    "intent_delta_id_for",
]
