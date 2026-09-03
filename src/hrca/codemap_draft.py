"""Deterministic Code Map Draft and Intent Delta domain (P3.4).

This module is the Qt-free, dependency-free *domain* for the editable side of
the Code Map Procedural Language Standard 0.1. It replaces the field-based
``hrca.twin_draft`` model with **typed operations** (SCOPE G) and derives a
deterministic, non-executable **Intent Delta** (SCOPE H).

It is deliberately pure in the same sense as :mod:`hrca.codemap`:

* it performs **no filesystem access** — it receives the baseline blocks and the
  typed operations and returns records;
* it performs **no model, provider, network, credential or telemetry** call and
  emits no model-generated text — every record is assembled deterministically
  from the operations and the baseline blocks only;
* it is **Qt-free** and imports only the standard library plus
  :mod:`hrca.twin` (fingerprints) and :mod:`hrca.codemap` (block model), so the
  desktop client can never import it (enforced by
  :mod:`tests.test_architecture`).

Safety invariants (SCOPE G):

* **Verified source blocks are never rewritten, deleted or moved.** Only
  ``purpose`` blocks accept ``replace_description``, only ``decision`` blocks
  accept ``replace_condition_intent``, and ``mark_unresolved``/``restore_block``
  only toggle a block's *state* — never its verified payload.
* **Inserted blocks are draft-scoped.** ``insert_block``/``delete_draft_block``/
  ``move_draft_block`` operate on draft ids with **no verified source anchor**.
* **A draft never touches source.** It records only the human-authored intent;
  it never contains repository source, never writes anything, and never claims
  to be executable.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import codemap, twin

CODEMAP_DRAFT_SCHEMA_VERSION = "1.0.0"
DRAFT_GENERATOR = "hrca-codemap-draft"
INTENT_DELTA_GENERATOR = "hrca-intent-delta"

# Every operation a draft records is human-authored.
ORIGIN_USER_AUTHORED = "user_authored"

# Draft self-validation states.
DRAFT_VALID = "valid"
DRAFT_INVALID = "invalid"
DRAFT_STATES = frozenset({DRAFT_VALID, DRAFT_INVALID})

# Conflict/stale states between a draft and the current baseline.
CONFLICT_NONE = "none"
CONFLICT_STALE = "stale"
CONFLICT_STATES = frozenset({CONFLICT_NONE, CONFLICT_STALE})

# Intent classes (SCOPE G). A documentation edit changes only how the code is
# described; a behavior edit changes the recorded decision/step semantics.
INTENT_DOCUMENTATION = "documentation_intent"
INTENT_BEHAVIOR = "behavior_change_intent"
INTENT_CLASSES = frozenset({INTENT_DOCUMENTATION, INTENT_BEHAVIOR})

# Typed operations (SCOPE G).
OP_REPLACE_DESCRIPTION = "replace_description"
OP_INSERT_BLOCK = "insert_block"
OP_DELETE_DRAFT_BLOCK = "delete_draft_block"
OP_MOVE_DRAFT_BLOCK = "move_draft_block"
OP_REPLACE_CONDITION_INTENT = "replace_condition_intent"
OP_MARK_UNRESOLVED = "mark_unresolved"
OP_RESTORE_BLOCK = "restore_block"

OPERATIONS = frozenset(
    {
        OP_REPLACE_DESCRIPTION,
        OP_INSERT_BLOCK,
        OP_DELETE_DRAFT_BLOCK,
        OP_MOVE_DRAFT_BLOCK,
        OP_REPLACE_CONDITION_INTENT,
        OP_MARK_UNRESOLVED,
        OP_RESTORE_BLOCK,
    }
)

# Block types a draft may insert (SCOPE G): a step, a decision, an exception
# expectation, or a free-form note. Never a verified entity/return/dependency.
INSERTABLE_BLOCK_TYPES = frozenset(
    {codemap.BT_STEP, codemap.BT_DECISION, codemap.BT_EXCEPTION, codemap.BT_NOTE}
)

# Bounded input limits.
MAX_DRAFT_OPERATIONS = 200
MAX_PROPOSED_TEXT_CHARS = 4000

# Fixed, bounded reason strings (never interpolate caller content).
REASON_UNKNOWN_TARGET = "unknown target block"
REASON_READ_ONLY_BLOCK = "block is a verified source fact and cannot be edited"
REASON_UNSUPPORTED_OP = "operation is not allowed for this block"
REASON_INVALID_PROPOSED = "proposed value is invalid for this operation"
REASON_OVERSIZED = "value exceeds the bounded size"
REASON_DUPLICATE = "duplicate operation for the same target block"
REASON_NOT_A_DRAFT_BLOCK = "target is not a draft block"
REASON_NO_CHANGE = "no_change"
REASON_STALE = "stale"


def dumps(obj: Any) -> str:
    """Serialize a draft/delta to a single-line, deterministic, ASCII-safe string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


def migrate_draft(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a raw draft against the current schema version.

    Returns ``(draft, error)``; on a future/unknown version or a malformed
    mapping, ``draft`` is ``None`` and ``error`` is a bounded reason. There are
    no historical versions before 1.0.0; the fail-closed future-version rule is
    preserved for later phases.
    """
    if not isinstance(raw, dict):
        return None, "draft is not a mapping"
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, "missing schema_version"
    try:
        current = _version_tuple(CODEMAP_DRAFT_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, "invalid schema_version"
    if found == current:
        return raw, None
    if found > current:
        return None, "schema_version is newer than supported"
    return None, "schema_version is not migratable"


def draft_id_for(
    workspace_id: str,
    baseline_revision: Optional[str],
    operations: List[Dict[str, Any]],
) -> str:
    """Return a content-addressed draft identifier (never timestamp-derived)."""
    canon = dumps(
        {
            "workspace_id": workspace_id,
            "baseline_revision": baseline_revision,
            "operations": operations,
        }
    )
    return "draft:" + twin.sha256_hex(canon.encode("utf-8"))


# -- baseline helpers -----------------------------------------------------


def baseline_document(blocks: List[Dict[str, Any]], baseline_revision: Optional[str]) -> Dict[str, Any]:
    """Build the baseline envelope a draft validates against."""
    return {"baseline_revision": baseline_revision, "blocks": blocks}


def _index_blocks(blocks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for block in blocks:
        if isinstance(block, dict) and isinstance(block.get("block_id"), str):
            out[block["block_id"]] = block
    return out


def _locator_of_block_id(block_id: str) -> str:
    """Recover the owning entity locator from a ``codemap:{locator}:…`` id."""
    parts = block_id.split(":", 2)
    if len(parts) >= 2 and parts[0] == "codemap":
        return parts[1]
    return ""


def _locator_of(block: Dict[str, Any]) -> str:
    payload = block.get("payload") or {}
    if payload.get("locator"):
        return payload["locator"]
    return _locator_of_block_id(block.get("block_id", ""))


def _draft_block_id(workspace_id: str, op_index: int) -> str:
    """Return a draft-scoped, deterministic id for an inserted block."""
    return f"codemap:draft:{workspace_id}:{op_index}"


def _fingerprint_of_payload(payload: Dict[str, Any]) -> str:
    return twin.sha256_hex(dumps(payload).encode("utf-8"))


def _clean_text(value: Any, reason: str = REASON_INVALID_PROPOSED) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(reason)
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_PROPOSED_TEXT_CHARS:
        raise ValueError(REASON_OVERSIZED)
    return text


# -- operation normalization ---------------------------------------------


def _intent_class_for(op: str, block_type: Optional[str]) -> str:
    if op == OP_REPLACE_DESCRIPTION or block_type == codemap.BT_NOTE:
        return INTENT_DOCUMENTATION
    return INTENT_BEHAVIOR


def _normalize_operation(
    workspace_id: str,
    baseline_revision: Optional[str],
    op_index: int,
    op_input: Dict[str, Any],
    baseline_index: Dict[str, Dict[str, Any]],
    draft_index: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate and normalize one typed operation into a canonical record."""
    if not isinstance(op_input, dict):
        raise ValueError(REASON_INVALID_PROPOSED)
    op = op_input.get("op")
    if not isinstance(op, str) or op not in OPERATIONS:
        raise ValueError(REASON_UNSUPPORTED_OP)

    target_id = op_input.get("target_block_id")
    proposed_payload: Optional[Dict[str, Any]] = None
    proposed_text: Optional[str] = None

    if op == OP_REPLACE_DESCRIPTION:
        block = baseline_index.get(target_id)
        if block is None:
            raise ValueError(REASON_UNKNOWN_TARGET)
        if block.get("block_type") != codemap.BT_PURPOSE:
            raise ValueError(REASON_UNSUPPORTED_OP)
        new_text = _clean_text(op_input.get("proposed_text"))
        before = {"payload": dict(block.get("payload") or {}), "display_text": block.get("display_text")}
        proposed_payload = dict(block.get("payload") or {})
        proposed_payload["text"] = new_text
        proposed_text = new_text
        proposed = {"payload": proposed_payload, "display_text": new_text}
        return _record(
            op, target_id, baseline_revision, block, proposed,
            _intent_class_for(op, codemap.BT_PURPOSE),
        )

    if op == OP_REPLACE_CONDITION_INTENT:
        block = baseline_index.get(target_id)
        if block is None:
            raise ValueError(REASON_UNKNOWN_TARGET)
        if block.get("block_type") != codemap.BT_DECISION:
            raise ValueError(REASON_UNSUPPORTED_OP)
        new_condition = _clean_text(op_input.get("proposed_condition"))
        if new_condition is None:
            raise ValueError(REASON_INVALID_PROPOSED)
        before = {"payload": dict(block.get("payload") or {}), "display_text": block.get("display_text")}
        proposed_payload = dict(block.get("payload") or {})
        proposed_payload["condition"] = new_condition
        proposed = {
            "payload": proposed_payload,
            "display_text": f"If {new_condition} is true, the following runs:",
        }
        return _record(
            op, target_id, baseline_revision, block, proposed,
            _intent_class_for(op, codemap.BT_DECISION),
        )

    if op == OP_INSERT_BLOCK:
        owning_entity_id = op_input.get("owning_entity_id")
        if not isinstance(owning_entity_id, str) or not owning_entity_id:
            raise ValueError(REASON_INVALID_PROPOSED)
        block_type = op_input.get("block_type")
        if block_type not in INSERTABLE_BLOCK_TYPES:
            raise ValueError(REASON_UNSUPPORTED_OP)
        payload = op_input.get("proposed_payload")
        if not isinstance(payload, dict):
            payload = {}
        text = _clean_text(op_input.get("proposed_text"))
        if block_type == codemap.BT_NOTE:
            payload = {"text": text}
            if text is None:
                raise ValueError(REASON_INVALID_PROPOSED)
            text = f"Note: {text}"
        elif text is None:
            raise ValueError(REASON_INVALID_PROPOSED)
        draft_id = _draft_block_id(workspace_id, op_index)
        draft_index[draft_id] = {"payload": payload, "display_text": text}
        return {
            "op": op,
            "target_block_id": draft_id,
            "owning_entity_id": owning_entity_id,
            "baseline_revision": baseline_revision,
            "before_fingerprint": None,
            "proposed_fingerprint": _fingerprint_of_payload(payload),
            "before": None,
            "proposed": {"payload": payload, "display_text": text},
            "intent_class": _intent_class_for(op, block_type),
        }

    if op == OP_DELETE_DRAFT_BLOCK:
        block = draft_index.get(target_id)
        if block is None:
            raise ValueError(REASON_NOT_A_DRAFT_BLOCK)
        return {
            "op": op,
            "target_block_id": target_id,
            "owning_entity_id": block.get("owning_entity_id"),
            "baseline_revision": baseline_revision,
            "before_fingerprint": _fingerprint_of_payload(block["payload"]),
            "proposed_fingerprint": None,
            "before": {"payload": block["payload"], "display_text": block["display_text"]},
            "proposed": None,
            "intent_class": _intent_class_for(op, None),
        }

    if op == OP_MOVE_DRAFT_BLOCK:
        block = draft_index.get(target_id)
        if block is None:
            raise ValueError(REASON_NOT_A_DRAFT_BLOCK)
        position = op_input.get("position")
        if not isinstance(position, int):
            raise ValueError(REASON_INVALID_PROPOSED)
        return {
            "op": op,
            "target_block_id": target_id,
            "owning_entity_id": block.get("owning_entity_id"),
            "baseline_revision": baseline_revision,
            "before_fingerprint": None,
            "proposed_fingerprint": None,
            "before": {"position": block.get("position")},
            "proposed": {"position": position},
            "intent_class": _intent_class_for(op, None),
        }

    if op == OP_MARK_UNRESOLVED:
        block = baseline_index.get(target_id)
        if block is None:
            raise ValueError(REASON_UNKNOWN_TARGET)
        return _state_record(op, target_id, baseline_revision, block, codemap.STATE_UNSUPPORTED, op_input.get("reason"))

    if op == OP_RESTORE_BLOCK:
        block = baseline_index.get(target_id)
        if block is None:
            raise ValueError(REASON_UNKNOWN_TARGET)
        return _state_record(op, target_id, baseline_revision, block, codemap.STATE_CURRENT, None)

    raise ValueError(REASON_UNSUPPORTED_OP)


def _record(
    op: str,
    target_id: str,
    baseline_revision: Optional[str],
    block: Dict[str, Any],
    proposed: Dict[str, Any],
    intent_class: str,
) -> Dict[str, Any]:
    before = {"payload": dict(block.get("payload") or {}), "display_text": block.get("display_text")}
    return {
        "op": op,
        "target_block_id": target_id,
        "owning_entity_id": _locator_of(block),
        "baseline_revision": baseline_revision,
        "before_fingerprint": block.get("source_fingerprint"),
        "proposed_fingerprint": _fingerprint_of_payload(proposed.get("payload") or {}),
        "before": before,
        "proposed": proposed,
        "intent_class": intent_class,
    }


def _state_record(
    op: str,
    target_id: str,
    baseline_revision: Optional[str],
    block: Dict[str, Any],
    state: str,
    reason: Optional[str],
) -> Dict[str, Any]:
    before_state = block.get("state")
    proposed_state = state
    return {
        "op": op,
        "target_block_id": target_id,
        "owning_entity_id": _locator_of(block),
        "baseline_revision": baseline_revision,
        "before_fingerprint": block.get("source_fingerprint"),
        "proposed_fingerprint": None,
        "before": {"state": before_state},
        "proposed": {"state": proposed_state, "reason": reason if reason is not None else None},
        "intent_class": INTENT_BEHAVIOR,
    }


# -- draft building -------------------------------------------------------


def build_draft(
    workspace_id: str,
    baseline: Dict[str, Any],
    operations: List[Dict[str, Any]],
    created_at: str,
    updated_at: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate typed ``operations`` against ``baseline`` and build a canonical draft.

    ``baseline`` is ``{"baseline_revision": str|None, "blocks": [block, ...]}``.
    Returns ``(draft, error)``; on success ``error`` is ``None`` and the draft
    carries a deterministically sorted ``operations`` list. On failure ``draft``
    is ``None`` and ``error`` is a bounded reason (never caller content).
    """
    if not isinstance(operations, list):
        return None, REASON_INVALID_PROPOSED
    if len(operations) > MAX_DRAFT_OPERATIONS:
        return None, REASON_OVERSIZED

    baseline_revision = baseline.get("baseline_revision")
    blocks = baseline.get("blocks") or []
    if not isinstance(blocks, list):
        return None, REASON_INVALID_PROPOSED
    baseline_index = _index_blocks(blocks)
    draft_index: Dict[str, Dict[str, Any]] = {}

    seen: set = set()
    normalized: List[Dict[str, Any]] = []
    for i, op_input in enumerate(operations):
        try:
            record = _normalize_operation(
                workspace_id, baseline_revision, i, op_input, baseline_index, draft_index
            )
        except ValueError as exc:
            return None, str(exc)
        key = record["target_block_id"]
        if key in seen:
            return None, REASON_DUPLICATE
        seen.add(key)
        normalized.append(record)

    normalized.sort(key=lambda r: (r["op"], r["target_block_id"]))

    draft: Dict[str, Any] = {
        "schema_version": CODEMAP_DRAFT_SCHEMA_VERSION,
        "generator": DRAFT_GENERATOR,
        "draft_id": draft_id_for(workspace_id, baseline_revision, normalized),
        "workspace_id": workspace_id,
        "origin": ORIGIN_USER_AUTHORED,
        "baseline": {"baseline_revision": baseline_revision},
        "created_at": created_at,
        "updated_at": updated_at,
        "operations": normalized,
        "validation": {"state": DRAFT_VALID, "reason": None},
        "conflict": {"state": CONFLICT_NONE, "reason": None},
    }
    return draft, None


def is_noop(draft: Dict[str, Any]) -> bool:
    """Return True when the draft records no operations (an honest "no change")."""
    return not draft.get("operations")


# -- conflict/stale detection ---------------------------------------------


def conflict_for(draft: Dict[str, Any], baseline: Dict[str, Any]) -> Dict[str, Any]:
    """Return the conflict record between ``draft`` and the current ``baseline``.

    A draft is only valid against the baseline revision it recorded; when the
    current baseline revision differs, the result is ``stale`` with the old and
    current revisions plus safe (never auto-merging) actions.
    """
    old_base = (draft.get("baseline") or {}).get("baseline_revision")
    curr_base = baseline.get("baseline_revision")
    if old_base == curr_base:
        return {"state": CONFLICT_NONE, "reason": None}
    return {
        "state": CONFLICT_STALE,
        "reason": "the draft baseline no longer matches the current Code Map baseline",
        "old_baseline": old_base,
        "current_baseline": curr_base,
        "affected_targets": [o.get("target_block_id") for o in draft.get("operations", [])],
        "safe_actions": ["discard", "reset", "compare"],
    }


# -- Intent Delta ---------------------------------------------------------


def _entity_facts(baseline: Dict[str, Any], entity_id: Optional[str]) -> Tuple[List[str], List[str]]:
    """Return ``(known_dependencies, known_callers)`` for an owning entity."""
    blocks = baseline.get("blocks") or []
    if entity_id is None:
        return codemap.dependency_targets(blocks), codemap.call_targets(blocks)
    entity_blocks = codemap.blocks_for_entity(blocks, entity_id)
    return codemap.dependency_targets(entity_blocks), codemap.call_targets(entity_blocks)


def _intent_delta_id_for(delta: Dict[str, Any]) -> str:
    canon = dumps({k: v for k, v in delta.items() if k != "intent_delta_id"})
    return "delta:" + twin.sha256_hex(canon.encode("utf-8"))


def generate_intent_delta(
    draft: Dict[str, Any], baseline: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Derive a deterministic, non-executable Intent Delta from ``draft``.

    Returns ``(intent_delta, error)``. A no-op draft yields ``(None, "no_change")``
    and a stale draft yields ``(None, "stale")`` — honest refusals, never
    fabrications. Otherwise the delta normalizes every operation (SCOPE H) into
    a typed entry that never claims to be executable.
    """
    if is_noop(draft):
        return None, REASON_NO_CHANGE
    if conflict_for(draft, baseline)["state"] != CONFLICT_NONE:
        return None, REASON_STALE

    blocks = baseline.get("blocks") or []
    entries: List[Dict[str, Any]] = []
    for op in draft.get("operations", []):
        entity_id = op.get("owning_entity_id")
        dependencies, callers = _entity_facts(baseline, entity_id)
        is_behavior = op.get("intent_class") == INTENT_BEHAVIOR
        entry: Dict[str, Any] = {
            "operation": op.get("op"),
            "target_block_id": op.get("target_block_id"),
            "owning_entity_id": entity_id,
            "baseline_revision": op.get("baseline_revision"),
            "before_fingerprint": op.get("before_fingerprint"),
            "proposed_fingerprint": op.get("proposed_fingerprint"),
            "before": op.get("before"),
            "proposed": op.get("proposed"),
            "intent_class": op.get("intent_class"),
            "affected_source_artifacts": [entity_id] if entity_id else [],
            "known_dependencies": dependencies,
            "known_callers": callers,
            "constraints": _invariants_for(blocks, entity_id),
            "preserved_invariants": _invariants_for(blocks, entity_id),
            "acceptance_criteria": [_acceptance_criterion(op)],
            "unresolved_questions": (
                ["not executable by this tool (descriptive intent only)"] if is_behavior else []
            ),
            "ambiguity_flags": [],
            "required_approval_level": "high" if is_behavior else "low",
        }
        entries.append(entry)

    delta: Dict[str, Any] = {
        "schema_version": CODEMAP_DRAFT_SCHEMA_VERSION,
        "generator": INTENT_DELTA_GENERATOR,
        "intent_delta_id": "",
        "draft_id": draft.get("draft_id"),
        "workspace_id": draft.get("workspace_id"),
        "baseline": draft.get("baseline"),
        "intent": ORIGIN_USER_AUTHORED,
        "executable": False,
        "entries": entries,
        "conflict_state": CONFLICT_NONE,
    }
    delta["intent_delta_id"] = _intent_delta_id_for(delta)
    return delta, None


def _invariants_for(blocks: List[Dict[str, Any]], entity_id: Optional[str]) -> List[str]:
    subset = codemap.blocks_for_entity(blocks, entity_id) if entity_id else blocks
    return [
        (b.get("payload") or {}).get("assertion")
        for b in subset
        if b.get("block_type") == codemap.BT_INVARIANT
        and (b.get("payload") or {}).get("assertion")
    ]


def _acceptance_criterion(op: Dict[str, Any]) -> str:
    proposed = op.get("proposed") or {}
    display = proposed.get("display_text")
    if display:
        return f"{op.get('target_block_id')}: {display}"
    return f"{op.get('target_block_id')}: {op.get('op')}"


__all__ = [
    "CODEMAP_DRAFT_SCHEMA_VERSION",
    "DRAFT_GENERATOR",
    "INTENT_DELTA_GENERATOR",
    "ORIGIN_USER_AUTHORED",
    "DRAFT_VALID",
    "DRAFT_INVALID",
    "DRAFT_STATES",
    "CONFLICT_NONE",
    "CONFLICT_STALE",
    "CONFLICT_STATES",
    "INTENT_DOCUMENTATION",
    "INTENT_BEHAVIOR",
    "INTENT_CLASSES",
    "OP_REPLACE_DESCRIPTION",
    "OP_INSERT_BLOCK",
    "OP_DELETE_DRAFT_BLOCK",
    "OP_MOVE_DRAFT_BLOCK",
    "OP_REPLACE_CONDITION_INTENT",
    "OP_MARK_UNRESOLVED",
    "OP_RESTORE_BLOCK",
    "OPERATIONS",
    "INSERTABLE_BLOCK_TYPES",
    "MAX_DRAFT_OPERATIONS",
    "MAX_PROPOSED_TEXT_CHARS",
    "REASON_UNKNOWN_TARGET",
    "REASON_READ_ONLY_BLOCK",
    "REASON_UNSUPPORTED_OP",
    "REASON_INVALID_PROPOSED",
    "REASON_OVERSIZED",
    "REASON_DUPLICATE",
    "REASON_NOT_A_DRAFT_BLOCK",
    "REASON_NO_CHANGE",
    "REASON_STALE",
    "dumps",
    "migrate_draft",
    "draft_id_for",
    "baseline_document",
    "build_draft",
    "is_noop",
    "conflict_for",
    "generate_intent_delta",
]
