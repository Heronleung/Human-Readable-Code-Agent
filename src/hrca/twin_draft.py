"""Deterministic Twin Draft and Intent Delta domain (P3.4).

This module is the Qt-free, dependency-free *domain* for the editable side of
the Structured Code Twin. It turns a set of user-authored *interpretation*
edits into a versioned Twin Draft, validates those edits against a baseline
Twin store, and derives a deterministic, non-executable Intent Delta from a
valid, non-no-op draft.

It is deliberately pure in the same sense as :mod:`hrca.twin`:

* it performs **no filesystem access** and can never read or write the selected
  repository — it receives the baseline store and edits, and returns records;
* it performs **no model, provider, network, credential or telemetry** call and
  emits no model-generated explanation — every field, reason and projection is
  assembled deterministically from the edits and the baseline store only;
* it is **Qt-free** and imports only the standard library plus
  :mod:`hrca.twin`, so the desktop client can never import it (enforced by
  :mod:`tests.test_architecture`).

Two safety invariants are central:

* **Read-only facts are immutable.** The deterministic IDs, source anchors and
  ranges, scanner-verified signatures, baseline revision/fingerprint, sync
  state, provenance and confidence are *never* editable. An edit that names one
  of them (or any other non-editable field) is rejected with a bounded reason.
* **A draft never touches source.** It records only the human-authored
  interpretation fields and their before/after values; it never contains source
  content, never writes to the repository, and never claims to be executable.

The draft and the Intent Delta are both single canonical JSON documents whose
serialization is deterministic (sorted keys, ``ensure_ascii``, compact
separators) so identical inputs always yield byte-identical outputs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import twin

DRAFT_SCHEMA_VERSION = "1.0.0"
DRAFT_GENERATOR = "hrca-twin-draft"
INTENT_DELTA_GENERATOR = "hrca-intent-delta"

# The only provenance a draft can carry: every edit is human-authored.
ORIGIN_USER_AUTHORED = "user_authored"

# Validation states a draft records about itself.
DRAFT_VALID = "valid"
DRAFT_INVALID = "invalid"
DRAFT_STATES = frozenset({DRAFT_VALID, DRAFT_INVALID})

# Conflict/stale states between a draft and the current baseline.
CONFLICT_NONE = "none"
CONFLICT_STALE = "stale"
CONFLICT_STATES = frozenset({CONFLICT_NONE, CONFLICT_STALE})

# Target kinds a draft can edit. A ``artifact`` target is a SourceArtifact (a
# file, class, function or method); a ``behavior`` target is a BehaviorNode.
TARGET_ARTIFACT = "artifact"
TARGET_BEHAVIOR = "behavior"
TARGET_KINDS = frozenset({TARGET_ARTIFACT, TARGET_BEHAVIOR})

# -- editable field vocabulary -------------------------------------------

# The fixed set of human-authored interpretation fields. Every other field on a
# Twin record is a read-only source fact (or envelope metadata) and is rejected.
FIELD_PURPOSE = "purpose"
FIELD_WORKFLOW_STEPS = "workflow_steps"
FIELD_CONDITIONS = "conditions"
FIELD_INPUTS_OUTPUTS = "inputs_outputs"
FIELD_EXCEPTION_HANDLING = "exception_handling"
FIELD_SIDE_EFFECTS = "side_effects"
FIELD_DEPENDENCIES = "dependencies"
FIELD_INVARIANTS = "invariants"
FIELD_LIMITATIONS = "limitations"

EDITABLE_FIELDS = frozenset(
    {
        FIELD_PURPOSE,
        FIELD_WORKFLOW_STEPS,
        FIELD_CONDITIONS,
        FIELD_INPUTS_OUTPUTS,
        FIELD_EXCEPTION_HANDLING,
        FIELD_SIDE_EFFECTS,
        FIELD_DEPENDENCIES,
        FIELD_INVARIANTS,
        FIELD_LIMITATIONS,
    }
)

# Cardinality: a ``single`` field is one line of prose; a ``list`` field is an
# ordered list of steps/items (never a free-form rich-text blob).
SINGLE = "single"
LIST = "list"

FIELD_CARDINALITY = {
    FIELD_PURPOSE: SINGLE,
    FIELD_WORKFLOW_STEPS: LIST,
    FIELD_CONDITIONS: LIST,
    FIELD_INPUTS_OUTPUTS: LIST,
    FIELD_EXCEPTION_HANDLING: LIST,
    FIELD_SIDE_EFFECTS: LIST,
    FIELD_DEPENDENCIES: LIST,
    FIELD_INVARIANTS: LIST,
    FIELD_LIMITATIONS: LIST,
}

# Which fields each target kind supports. Artifact targets own the file/module
# and symbol-level interpretation; behavior targets own the workflow-level
# interpretation.
ARTIFACT_FIELDS = (
    FIELD_PURPOSE,
    FIELD_DEPENDENCIES,
    FIELD_INVARIANTS,
    FIELD_LIMITATIONS,
)
BEHAVIOR_FIELDS = (
    FIELD_PURPOSE,
    FIELD_WORKFLOW_STEPS,
    FIELD_CONDITIONS,
    FIELD_INPUTS_OUTPUTS,
    FIELD_EXCEPTION_HANDLING,
    FIELD_SIDE_EFFECTS,
    FIELD_DEPENDENCIES,
    FIELD_INVARIANTS,
    FIELD_LIMITATIONS,
)
_FIELDS_FOR_KIND = {
    TARGET_ARTIFACT: frozenset(ARTIFACT_FIELDS),
    TARGET_BEHAVIOR: frozenset(BEHAVIOR_FIELDS),
}

# Read-only source facts that must never be edited. The domain rejects any edit
# whose field name is in this set (or is not in :data:`EDITABLE_FIELDS`).
READ_ONLY_FIELDS = frozenset(
    {
        "id",
        "record_type",
        "kind",
        "path",
        "locator",
        "module",
        "name",
        "source_range",
        "source_anchor",
        "fingerprint",
        "baseline_fingerprint",
        "sync_state",
        "provenance",
        "confidence",
        "syntax_status",
        "summary",
        "details",
        "items",
        "reason",
        "schema_version",
        "generator",
        "workspace_revision",
        "scan_generation",
        "scan_timestamp",
        "moved_from",
    }
)

# -- bounded input limits ------------------------------------------------

# A draft is a bounded document. These limits are enforced during validation so
# an oversized or pathological edit set is rejected with a bounded reason rather
# than persisted.
MAX_DRAFT_EDITS = 200
MAX_FIELD_VALUE_CHARS = 4000
MAX_FIELD_ITEMS = 200
MAX_FIELD_ITEM_CHARS = 4000

# Fixed bounded reason strings (never interpolate caller content).
REASON_UNKNOWN_TARGET = "unknown target"
REASON_UNSUPPORTED_FIELD = "field is not editable for this target"
REASON_READ_ONLY_FIELD = "field is a read-only source fact"
REASON_INVALID_VALUE = "value does not match the field's cardinality"
REASON_OVERSIZED = "value exceeds the bounded size"
REASON_DUPLICATE_FIELD = "duplicate edit for the same target field"
REASON_NO_CHANGE = "no_change"
REASON_STALE = "stale"


# -- deterministic serialization -----------------------------------------


def dumps(obj: Any) -> str:
    """Serialize a draft/delta to a single-line, deterministic, ASCII-safe string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


def migrate_draft(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a raw draft/delta against the current schema version.

    Returns ``(draft, error)``; on a future/unknown version or a malformed
    mapping, ``draft`` is ``None`` and ``error`` is a bounded reason. There are
    no historical versions before 1.0.0, so no migrations are defined; the
    fail-closed future-version rule is preserved for later phases.
    """
    if not isinstance(raw, dict):
        return None, "draft is not a mapping"
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, "missing schema_version"
    try:
        current = _version_tuple(DRAFT_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, "invalid schema_version"
    if found == current:
        return raw, None
    if found > current:
        return None, "schema_version is newer than supported"
    return None, "schema_version is not migratable"


# -- value normalization -------------------------------------------------


def normalize_value(field: str, value: Any) -> Optional[Any]:
    """Normalize a proposed edit value for ``field``.

    Returns a canonical value (a stripped ``str`` for a single field, a list of
    stripped non-empty strings for a list field, or ``None`` when the edit
    clears the field). Raises :class:`ValueError` with a bounded reason when the
    value does not match the field's cardinality or exceeds a size limit.
    """
    if value is None:
        return None
    cardinality = FIELD_CARDINALITY[field]
    if cardinality == SINGLE:
        if not isinstance(value, str):
            raise ValueError(REASON_INVALID_VALUE)
        text = value.strip()
        if not text:
            return None
        if len(text) > MAX_FIELD_VALUE_CHARS:
            raise ValueError(REASON_OVERSIZED)
        return text

    # list field: an ordered list of plain strings, never a rich-text blob.
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValueError(REASON_INVALID_VALUE)
    if len(value) > MAX_FIELD_ITEMS:
        raise ValueError(REASON_OVERSIZED)
    items = []
    for item in value:
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_FIELD_ITEM_CHARS:
            raise ValueError(REASON_OVERSIZED)
        items.append(text)
    return items if items else None


# -- target resolution ---------------------------------------------------


def _records_by_id(store: Dict[str, Any], key: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for rec in store.get(key, []):
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out[rec["id"]] = rec
    return out


def _target_kind_for(store: Dict[str, Any], target_id: str) -> Optional[str]:
    """Return the target kind (``artifact``/``behavior``) for ``target_id``.

    Artifact and behavior-node identifiers occupy disjoint namespaces
    (``artifact:…`` vs ``behavior:…``), so the kind is unambiguous.
    """
    for key, kind in (("artifacts", TARGET_ARTIFACT), ("behavior_nodes", TARGET_BEHAVIOR)):
        for rec in store.get(key, []):
            if isinstance(rec, dict) and rec.get("id") == target_id:
                return kind
    return None


def _source_artifact_for(
    store: Dict[str, Any], target_id: str, target_kind: str
) -> Dict[str, Any]:
    """Return the linked SourceArtifact identity for a target.

    An artifact target links to itself; a behavior target links to its owning
    symbol artifact (matched by the behavior node's ``symbol`` locator), falling
    back to the file artifact when the symbol is not a standalone artifact.
    """
    artifacts = _records_by_id(store, "artifacts")
    if target_kind == TARGET_ARTIFACT:
        rec = artifacts.get(target_id, {})
        return {
            "id": target_id,
            "path": rec.get("path"),
            "locator": rec.get("locator"),
        }
    nodes = _records_by_id(store, "behavior_nodes")
    node = nodes.get(target_id, {})
    symbol = node.get("symbol")
    for aid, rec in artifacts.items():
        if rec.get("locator") == symbol:
            return {"id": aid, "path": rec.get("path"), "locator": rec.get("locator")}
    # Fall back to the file artifact sharing the behavior node's path.
    path = node.get("path")
    for aid, rec in artifacts.items():
        if rec.get("kind") == twin.ARTIFACT_FILE and rec.get("path") == path:
            return {"id": aid, "path": rec.get("path"), "locator": None}
    return {"id": None, "path": node.get("path"), "locator": None}


def _target_record(
    store: Dict[str, Any], target_id: str, target_kind: str
) -> Dict[str, Any]:
    return {
        "target_id": target_id,
        "target_kind": target_kind,
        "source_artifact": _source_artifact_for(store, target_id, target_kind),
    }


# -- draft building ------------------------------------------------------


def draft_id_for(
    workspace_id: str, baseline_revision: Optional[str], changes: List[Dict[str, Any]]
) -> str:
    """Return a content-addressed draft identifier.

    The identifier is derived from the workspace identity, the baseline revision
    and the canonical change list — never from timestamps — so two drafts with
    identical content carry identical identifiers.
    """
    canon = json.dumps(
        {
            "workspace_id": workspace_id,
            "baseline_revision": baseline_revision,
            "changes": changes,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "draft:" + twin.sha256_hex(canon.encode("utf-8"))


def _baseline_meta(store: Dict[str, Any]) -> Dict[str, Any]:
    rev = (store.get("workspace_revision") or {})
    return {
        "baseline_revision": rev.get("baseline_fingerprint"),
        "scan_generation": rev.get("scan_generation"),
        "scan_timestamp": rev.get("scan_timestamp"),
    }


def build_draft(
    workspace_id: str,
    baseline_store: Dict[str, Any],
    edits: List[Dict[str, Any]],
    created_at: str,
    updated_at: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate ``edits`` against ``baseline_store`` and build a canonical draft.

    ``edits`` is a list of ``{target_id, field, proposed}`` mappings. Returns
    ``(draft, error)``: on success ``error`` is ``None`` and the draft is a
    canonical document whose ``changes`` are sorted by ``(target_id, field)`` and
    whose ``targets`` are sorted by ``target_id``. On failure ``draft`` is
    ``None`` and ``error`` is a bounded reason (never caller content).

    A draft always records ``original`` as the baseline value, which is ``None``
    for every user-authored interpretation field (the read-only Twin carries no
    such field).
    """
    if not isinstance(edits, list):
        return None, REASON_INVALID_VALUE
    if len(edits) > MAX_DRAFT_EDITS:
        return None, REASON_OVERSIZED

    # Reject duplicate (target_id, field) edits up front for determinism.
    seen: set = set()
    changes: List[Dict[str, Any]] = []
    targets: Dict[str, Dict[str, Any]] = {}
    for edit in edits:
        if not isinstance(edit, dict):
            return None, REASON_INVALID_VALUE
        target_id = edit.get("target_id")
        field = edit.get("field")
        if not isinstance(target_id, str) or not target_id:
            return None, REASON_UNKNOWN_TARGET
        if not isinstance(field, str):
            return None, REASON_UNSUPPORTED_FIELD

        key = (target_id, field)
        if key in seen:
            return None, REASON_DUPLICATE_FIELD
        seen.add(key)

        if field in READ_ONLY_FIELDS:
            return None, REASON_READ_ONLY_FIELD
        if field not in EDITABLE_FIELDS:
            return None, REASON_UNSUPPORTED_FIELD

        target_kind = _target_kind_for(baseline_store, target_id)
        if target_kind is None:
            return None, REASON_UNKNOWN_TARGET
        if field not in _FIELDS_FOR_KIND[target_kind]:
            return None, REASON_UNSUPPORTED_FIELD

        try:
            proposed = normalize_value(field, edit.get("proposed"))
        except ValueError as exc:
            return None, str(exc)

        changes.append(
            {"target_id": target_id, "field": field, "original": None, "proposed": proposed}
        )
        targets[target_id] = _target_record(baseline_store, target_id, target_kind)

    changes.sort(key=lambda c: (c["target_id"], c["field"]))
    ordered_targets = [targets[t] for t in sorted(targets)]

    baseline_revision = _baseline_meta(baseline_store).get("baseline_revision")
    draft: Dict[str, Any] = {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "generator": DRAFT_GENERATOR,
        "draft_id": draft_id_for(workspace_id, baseline_revision, changes),
        "workspace_id": workspace_id,
        "origin": ORIGIN_USER_AUTHORED,
        "baseline": _baseline_meta(baseline_store),
        "created_at": created_at,
        "updated_at": updated_at,
        "targets": ordered_targets,
        "changes": changes,
        "validation": {"state": DRAFT_VALID, "reason": None},
        "conflict": {"state": CONFLICT_NONE, "reason": None},
    }
    return draft, None


def is_noop(draft: Dict[str, Any]) -> bool:
    """Return True when the draft records no edits (an honest "no change")."""
    return not draft.get("changes")


# -- conflict/stale detection --------------------------------------------


def conflict_for(draft: Dict[str, Any], current_store: Dict[str, Any]) -> Dict[str, Any]:
    """Return the conflict record between ``draft`` and ``current_store``.

    A draft is only valid against the baseline revision it recorded; when the
    current store's baseline fingerprint differs, the result is ``stale`` and
    carries the old/current baseline plus the affected targets and the safe
    (never auto-merging) actions.
    """
    old_base = (draft.get("baseline") or {}).get("baseline_revision")
    curr_base = (current_store.get("workspace_revision") or {}).get("baseline_fingerprint")
    if old_base == curr_base:
        return {"state": CONFLICT_NONE, "reason": None}
    return {
        "state": CONFLICT_STALE,
        "reason": "the draft baseline no longer matches the current Twin baseline",
        "old_baseline": old_base,
        "current_baseline": curr_base,
        "affected_targets": [t.get("target_id") for t in draft.get("targets", [])],
        "safe_actions": ["discard", "reset", "compare"],
    }


# -- Intent Delta --------------------------------------------------------


def _criterion(change: Dict[str, Any]) -> str:
    field = change["field"]
    if FIELD_CARDINALITY[field] == SINGLE:
        return f"{change['target_id']}: {field} authored"
    n = len(change["proposed"]) if isinstance(change["proposed"], list) else 0
    return f"{change['target_id']}: {field} authored ({n} step(s))"


def _flatten_field(changes: List[Dict[str, Any]], field: str) -> List[str]:
    out: List[str] = []
    for change in changes:
        if change["field"] != field:
            continue
        proposed = change["proposed"]
        if isinstance(proposed, list):
            out.extend(str(item) for item in proposed)
    return out


def _intent_delta_id_for(delta: Dict[str, Any]) -> str:
    canon = dumps({k: v for k, v in delta.items() if k != "intent_delta_id"})
    return "delta:" + twin.sha256_hex(canon.encode("utf-8"))


def generate_intent_delta(
    draft: Dict[str, Any], current_store: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Derive a deterministic, non-executable Intent Delta from ``draft``.

    Returns ``(intent_delta, error)``. A no-op draft yields ``(None, "no_change")``
    and a stale draft yields ``(None, "stale")`` — both are honest refusals, not
    fabrications. Otherwise the delta is a canonical document that never claims
    to be executable and never contains source content.
    """
    if is_noop(draft):
        return None, REASON_NO_CHANGE
    if conflict_for(draft, current_store)["state"] != CONFLICT_NONE:
        return None, REASON_STALE

    changes = draft.get("changes", [])
    targets = draft.get("targets", [])
    delta: Dict[str, Any] = {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "generator": INTENT_DELTA_GENERATOR,
        "intent_delta_id": "",  # filled deterministically below
        "draft_id": draft.get("draft_id"),
        "workspace_id": draft.get("workspace_id"),
        "baseline": draft.get("baseline"),
        "intent": ORIGIN_USER_AUTHORED,
        "executable": False,
        "targets": targets,
        "affected_behavior_nodes": sorted(
            t["target_id"] for t in targets if t.get("target_kind") == TARGET_BEHAVIOR
        ),
        "affected_sources": sorted(
            {
                t["source_artifact"]["id"]
                for t in targets
                if t.get("source_artifact", {}).get("id") is not None
            }
        ),
        "changes": [
            {
                "target_id": c["target_id"],
                "field": c["field"],
                "before": c["original"],
                "after": c["proposed"],
            }
            for c in changes
        ],
        "constraints": _flatten_field(changes, FIELD_INVARIANTS),
        "acceptance_criteria": [_criterion(c) for c in changes],
        "unresolved": ["not executable by this tool (descriptive intent only)"]
        + _flatten_field(changes, FIELD_LIMITATIONS),
        "conflict_state": CONFLICT_NONE,
    }
    delta["intent_delta_id"] = _intent_delta_id_for(delta)
    return delta, None


__all__ = [
    "DRAFT_SCHEMA_VERSION",
    "DRAFT_GENERATOR",
    "INTENT_DELTA_GENERATOR",
    "ORIGIN_USER_AUTHORED",
    "DRAFT_VALID",
    "DRAFT_INVALID",
    "DRAFT_STATES",
    "CONFLICT_NONE",
    "CONFLICT_STALE",
    "CONFLICT_STATES",
    "TARGET_ARTIFACT",
    "TARGET_BEHAVIOR",
    "TARGET_KINDS",
    "FIELD_PURPOSE",
    "FIELD_WORKFLOW_STEPS",
    "FIELD_CONDITIONS",
    "FIELD_INPUTS_OUTPUTS",
    "FIELD_EXCEPTION_HANDLING",
    "FIELD_SIDE_EFFECTS",
    "FIELD_DEPENDENCIES",
    "FIELD_INVARIANTS",
    "FIELD_LIMITATIONS",
    "EDITABLE_FIELDS",
    "SINGLE",
    "LIST",
    "FIELD_CARDINALITY",
    "ARTIFACT_FIELDS",
    "BEHAVIOR_FIELDS",
    "READ_ONLY_FIELDS",
    "MAX_DRAFT_EDITS",
    "MAX_FIELD_VALUE_CHARS",
    "MAX_FIELD_ITEMS",
    "MAX_FIELD_ITEM_CHARS",
    "dumps",
    "migrate_draft",
    "normalize_value",
    "draft_id_for",
    "build_draft",
    "is_noop",
    "conflict_for",
    "generate_intent_delta",
]
