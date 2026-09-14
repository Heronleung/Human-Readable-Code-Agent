"""Deterministic Proposal-Package domain (P4.1).

This module is the first, reviewable Phase 4 bridge: it turns a validated,
non-executable **Intent Delta** (produced by :mod:`hrca.codemap_draft`) plus the
source-grounded **Twin store** and **Code Map baseline** into a typed, bounded
**Proposal Package** — an explicit statement of what a future code change would
involve, *before* any provider, patch generation, command execution or
repository-write capability exists.

The module is deliberately pure in the same sense as :mod:`hrca.codemap` and
:mod:`hrca.codemap_draft`:

* it performs **no filesystem access** (the boundary supplies the baseline and
  store);
* it performs **no model, provider, network, credential, command, Git or
  repository-write** call and emits no model-generated text — every field is
  assembled deterministically from the Intent Delta, baseline blocks and Twin
  store only;
* it is **Qt-free** and imports only the standard library plus
  :mod:`hrca.twin` (fingerprints), :mod:`hrca.codemap` (block helpers) and
  :mod:`hrca.codemap_draft` (draft/delta constants), so the desktop client can
  never import it (enforced by :mod:`tests.test_architecture`).

A proposal is a **non-applied plan**. ``executable`` and ``applied`` are always
``False``: a result produced here is never a patch, diff, approval or execution
outcome, and it never claims that code exists or that repository state changed.

State machine (terminal ``state`` values):

* ``ready`` — the intent is fully source-grounded and yields a bounded, ordered
  plan;
* ``clarification_required`` — the intent is a behavior change to an entity
  that itself depends on or calls other code, so the deterministic scanner
  cannot bound the impact without a human answer (no plan is asserted);
* ``unsupported`` — the intent targets an entity not present in the baseline
  (unknown target scope); no facts are invented;
* ``no_change`` — the draft is a no-op (no proposal);
* ``blocked`` — the draft is stale against the current baseline (no proposal).

``no_change`` and ``blocked`` are represented as bounded reasons from
:func:`plan_proposal` (mapped by the boundary to the ``no_change`` result and
the ``draft_stale`` error respectively); the other three are full packages with
a ``state`` field.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import codemap, codemap_draft, twin

PROPOSAL_SCHEMA_VERSION = "1.0.0"
PROPOSAL_GENERATOR = "hrca-proposal"

# Terminal proposal states.
STATE_READY = "ready"
STATE_CLARIFICATION_REQUIRED = "clarification_required"
STATE_UNSUPPORTED = "unsupported"
STATE_NO_CHANGE = "no_change"
STATE_BLOCKED = "blocked"
PROPOSAL_STATES = frozenset(
    {
        STATE_READY,
        STATE_CLARIFICATION_REQUIRED,
        STATE_UNSUPPORTED,
        STATE_NO_CHANGE,
        STATE_BLOCKED,
    }
)

# Bounded reason strings (never interpolate caller content).
REASON_NO_CHANGE = "no_change"
REASON_STALE = "stale"
REASON_UNSUPPORTED_TARGET = "unsupported target scope"
REASON_AMBIGUOUS = "ambiguous behavior intent"

# The one role an affected artifact can carry in P4.1: the entity is the direct
# target of the intent. Later gated phases may add "dependent" / "caller".
ROLE_TARGET = "target"

# Plan-step verbs, keyed by the typed draft operation. Each is a fixed phrase;
# only bounded identifiers (target block id / entity locator) are interpolated.
_PLAN_VERBS = {
    codemap_draft.OP_REPLACE_DESCRIPTION: "replace the purpose description of",
    codemap_draft.OP_REPLACE_CONDITION_INTENT: "replace the recorded decision condition of",
    codemap_draft.OP_INSERT_BLOCK: "insert a draft block into",
    codemap_draft.OP_DELETE_DRAFT_BLOCK: "delete the draft block",
    codemap_draft.OP_MOVE_DRAFT_BLOCK: "move the draft block",
    codemap_draft.OP_MARK_UNRESOLVED: "mark unresolved the block",
    codemap_draft.OP_RESTORE_BLOCK: "restore to current state the block",
}

# Fixed, source-grounded assumptions included in every ready package.
_ASSUMPTIONS = (
    "the synchronized Code Map baseline is current and does not change while "
    "this proposal is reviewed",
    "this proposal is descriptive only; no provider, network, command, Git or "
    "repository write is used",
)

_ASSUMPTION_BEHAVIOR = (
    "the behavior intent is user-authored and has not been verified against a "
    "runtime",
)

# Fixed, source-grounded validation expectations included in every ready
# package (never executed — they describe what a later validation phase must
# confirm).
_VALIDATION_READ_ONLY = (
    {
        "check": "no source file is modified by planning",
        "expected_outcome": "no repository write; planning is read-only",
    },
    {
        "check": "the proposal is deterministic",
        "expected_outcome": "re-planning the same intent and baseline yields an "
        "identical package",
    },
)

# The global safety invariant carried into every package's preserved constraints.
_GLOBAL_INVARIANT = "verified source blocks are never rewritten, deleted or moved"


def dumps(obj: Any) -> str:
    """Serialize a proposal to a single-line, deterministic, ASCII-safe string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _proposal_id_for(package: Dict[str, Any]) -> str:
    canon = dumps({k: v for k, v in package.items() if k != "proposal_id"})
    return "proposal:" + twin.sha256_hex(canon.encode("utf-8"))


# -- source grounding helpers ---------------------------------------------


def _entity_locators(blocks: List[Dict[str, Any]]) -> Dict[str, None]:
    """Return the set (as a keyed dict) of entity locators in ``blocks``."""
    locators: Dict[str, None] = {}
    for block in blocks:
        if block.get("block_type") == codemap.BT_ENTITY:
            locator = (block.get("payload") or {}).get("locator")
            if isinstance(locator, str) and locator:
                locators[locator] = None
    return locators


def _artifact_refs_for_locator(
    store: Dict[str, Any], locator: str
) -> List[Dict[str, Any]]:
    """Return source-artifact references for an entity ``locator``.

    A symbol locator (``module.Class.method``) matches a symbol artifact's
    ``locator``; a module locator (``module``) matches a file artifact's
    ``module``. Each reference carries ``locator``, ``path`` and ``kind``.
    Returns ``[]`` when the locator has no artifact in the store.
    """
    refs: List[Dict[str, Any]] = []
    for art in store.get("artifacts", []):
        if not isinstance(art, dict):
            continue
        if art.get("locator") == locator:
            refs.append(
                {"locator": locator, "path": art.get("path"), "kind": art.get("kind")}
            )
    if not refs:
        for art in store.get("artifacts", []):
            if not isinstance(art, dict):
                continue
            if art.get("kind") == twin.ARTIFACT_FILE and art.get("module") == locator:
                refs.append(
                    {"locator": locator, "path": art.get("path"), "kind": art.get("kind")}
                )
    # De-duplicate and sort deterministically by (kind, path, locator).
    seen = set()
    unique: List[Dict[str, Any]] = []
    for ref in sorted(refs, key=lambda r: (str(r.get("kind")), str(r.get("path")), str(r.get("locator")))):
        key = (ref.get("locator"), ref.get("path"), ref.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _target_scope(entries: List[Dict[str, Any]], store: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{entities, artifacts}`` for the ordered, deduplicated targets."""
    entities: Dict[str, None] = {}
    paths: Dict[str, None] = {}
    for entry in entries:
        entity_id = entry.get("owning_entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        entities[entity_id] = None
        for ref in _artifact_refs_for_locator(store, entity_id):
            if ref.get("path"):
                paths[ref["path"]] = None
    return {
        "entities": sorted(entities),
        "artifacts": sorted(paths),
    }


def _affected_artifacts(
    entries: List[Dict[str, Any]], store: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Return deduplicated, deterministic affected-artifact references."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for entry in entries:
        entity_id = entry.get("owning_entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            continue
        for ref in _artifact_refs_for_locator(store, entity_id):
            ref = dict(ref)
            ref["role"] = ROLE_TARGET
            key = (ref.get("locator"), ref.get("path"), ref.get("kind"), ref.get("role"))
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
    out.sort(key=lambda r: (str(r.get("kind")), str(r.get("path")), str(r.get("locator"))))
    return out


def _preserved_constraints(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the global invariant plus each entry's preserved invariants."""
    constraints: List[Dict[str, Any]] = [{"entity_id": None, "invariant": _GLOBAL_INVARIANT}]
    seen = set()
    for entry in entries:
        entity_id = entry.get("owning_entity_id")
        for invariant in entry.get("preserved_invariants") or []:
            if not isinstance(invariant, str) or not invariant:
                continue
            key = (entity_id, invariant)
            if key in seen:
                continue
            seen.add(key)
            constraints.append({"entity_id": entity_id, "invariant": invariant})
    return constraints


def _assumptions(entries: List[Dict[str, Any]]) -> List[str]:
    assumptions = list(_ASSUMPTIONS)
    if any(e.get("intent_class") == codemap_draft.INTENT_BEHAVIOR for e in entries):
        assumptions.extend(_ASSUMPTION_BEHAVIOR)
    return assumptions


def _clarifications(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return non-blocking clarification questions for a ready package."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for entry in entries:
        entity_id = entry.get("owning_entity_id")
        if entry.get("intent_class") != codemap_draft.INTENT_BEHAVIOR:
            continue
        if entry.get("operation") == codemap_draft.OP_MARK_UNRESOLVED:
            question = (
                "confirm the unresolved construct that should be documented; no "
                "runtime evidence is available"
            )
        else:
            question = (
                "confirm the exact intended behavior change before any source edit"
            )
        key = (entity_id, question)
        if key in seen:
            continue
        seen.add(key)
        out.append({"entity_id": entity_id, "question": question})
    return out


def _plan_steps(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return bounded, ordered plan steps (one per entry, in entry order)."""
    steps: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        op = entry.get("operation", "unknown")
        verb = _PLAN_VERBS.get(op, "plan the operation for")
        target = entry.get("target_block_id", "unknown")
        entity_id = entry.get("owning_entity_id")
        description = f"Plan: {verb} {target}."
        if entity_id:
            description = f"Plan: {verb} {target} in {entity_id}."
        proposed = entry.get("proposed") or {}
        display = proposed.get("display_text")
        evidence = display if isinstance(display, str) and display else f"{target}: {op}"
        steps.append(
            {
                "step": index,
                "operation": op,
                "target_block_id": target,
                "entity_id": entity_id,
                "intent_class": entry.get("intent_class"),
                "description": description,
                "requires_approval": entry.get("required_approval_level") == "high",
                "expected_evidence": evidence,
            }
        )
    return steps


def _risks(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return deterministic risks derived from intent class and dependencies."""
    behavior = [e for e in entries if e.get("intent_class") == codemap_draft.INTENT_BEHAVIOR]
    risks: List[Dict[str, Any]] = []
    if not behavior:
        risks.append(
            {
                "level": "low",
                "description": "documentation-only intent; no behavior or call "
                "impact is planned",
            }
        )
    else:
        entities = sorted({e.get("owning_entity_id") for e in behavior if e.get("owning_entity_id")})
        risks.append(
            {
                "level": "high",
                "description": "behavior change to " + ", ".join(entities)
                + "; impact is not verified by a runtime or provider",
            }
        )
    for entry in entries:
        if entry.get("known_dependencies") or entry.get("known_callers"):
            entity_id = entry.get("owning_entity_id") or "the target entity"
            risks.append(
                {
                    "level": "medium",
                    "description": f"{entity_id} depends on or calls other code; "
                    "interaction with those targets is not statically verified",
                }
            )
    return risks


def _validation_plan(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the read-only validation expectations plus one per entry."""
    plan = list(_VALIDATION_READ_ONLY)
    for entry in entries:
        target = entry.get("target_block_id", "unknown")
        criteria = entry.get("acceptance_criteria") or []
        expected = criteria[0] if criteria else f"{target}: {entry.get('operation', 'unknown')}"
        plan.append(
            {
                "check": f"confirm {entry.get('operation', 'unknown')} on {target}",
                "expected_outcome": expected,
            }
        )
    return plan


# -- derivation -----------------------------------------------------------


def build_proposal(
    intent_delta: Dict[str, Any],
    baseline: Dict[str, Any],
    store: Dict[str, Any],
) -> Dict[str, Any]:
    """Derive a deterministic proposal package from a validated Intent Delta.

    ``intent_delta`` is the non-no-op, non-stale delta from
    :func:`hrca.codemap_draft.generate_intent_delta`. Returns a full package
    whose ``state`` is ``ready``, ``clarification_required`` or ``unsupported``.
    """
    blocks = baseline.get("blocks") or []
    entries = intent_delta.get("entries") or []
    locators = _entity_locators(blocks)

    # Classification. An entry whose owning entity is not in the baseline has
    # unknown target scope (unsupported); a behavior change to an entity that
    # depends on or calls other code cannot be bounded without clarification.
    unsupported = False
    ambiguous = False
    for entry in entries:
        entity_id = entry.get("owning_entity_id")
        if isinstance(entity_id, str) and entity_id and entity_id not in locators:
            unsupported = True
            break
        if (
            entry.get("intent_class") == codemap_draft.INTENT_BEHAVIOR
            and (entry.get("known_dependencies") or entry.get("known_callers"))
        ):
            ambiguous = True

    workspace_id = intent_delta.get("workspace_id")
    baseline_revision = baseline.get("baseline_revision")
    scan_generation = (
        (store.get("workspace_revision") or {}).get("scan_generation")
        if isinstance(store, dict)
        else None
    )

    if unsupported:
        package: Dict[str, Any] = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "generator": PROPOSAL_GENERATOR,
            "state": STATE_UNSUPPORTED,
            "reason": REASON_UNSUPPORTED_TARGET,
            "executable": False,
            "applied": False,
            "intent_delta_id": intent_delta.get("intent_delta_id"),
            "draft_id": intent_delta.get("draft_id"),
            "workspace_id": workspace_id,
            "baseline": {
                "baseline_revision": baseline_revision,
                "scan_generation": scan_generation,
            },
            "target_scope": {"entities": [], "artifacts": []},
            "affected_artifacts": [],
            "preserved_constraints": _preserved_constraints(entries),
            "assumptions": [],
            "clarifications": [],
            "plan_steps": [],
            "risks": [],
            "validation_plan": [],
            "confidence": codemap.CONF_LOW,
        }
    elif ambiguous:
        package = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "generator": PROPOSAL_GENERATOR,
            "state": STATE_CLARIFICATION_REQUIRED,
            "reason": REASON_AMBIGUOUS,
            "executable": False,
            "applied": False,
            "intent_delta_id": intent_delta.get("intent_delta_id"),
            "draft_id": intent_delta.get("draft_id"),
            "workspace_id": workspace_id,
            "baseline": {
                "baseline_revision": baseline_revision,
                "scan_generation": scan_generation,
            },
            "target_scope": _target_scope(entries, store),
            "affected_artifacts": _affected_artifacts(entries, store),
            "preserved_constraints": _preserved_constraints(entries),
            "assumptions": _assumptions(entries),
            "clarifications": [
                {
                    "entity_id": entry.get("owning_entity_id"),
                    "question": (
                        "this entity depends on or calls other code; confirm the "
                        "intended behavior impact on those targets before a plan "
                        "is asserted"
                    ),
                }
                for entry in entries
                if entry.get("intent_class") == codemap_draft.INTENT_BEHAVIOR
            ],
            "plan_steps": [],
            "risks": _risks(entries),
            "validation_plan": [],
            "confidence": codemap.CONF_LOW,
        }
    else:
        package = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "generator": PROPOSAL_GENERATOR,
            "state": STATE_READY,
            "reason": None,
            "executable": False,
            "applied": False,
            "intent_delta_id": intent_delta.get("intent_delta_id"),
            "draft_id": intent_delta.get("draft_id"),
            "workspace_id": workspace_id,
            "baseline": {
                "baseline_revision": baseline_revision,
                "scan_generation": scan_generation,
            },
            "target_scope": _target_scope(entries, store),
            "affected_artifacts": _affected_artifacts(entries, store),
            "preserved_constraints": _preserved_constraints(entries),
            "assumptions": _assumptions(entries),
            "clarifications": _clarifications(entries),
            "plan_steps": _plan_steps(entries),
            "risks": _risks(entries),
            "validation_plan": _validation_plan(entries),
            "confidence": codemap.CONF_HIGH,
        }

    package["proposal_id"] = _proposal_id_for(package)
    return package


def plan_proposal(
    draft: Dict[str, Any],
    baseline: Dict[str, Any],
    store: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(proposal, error)`` for a draft against a baseline and store.

    A no-op draft yields ``(None, "no_change")`` and a stale draft yields
    ``(None, "stale")`` — bounded refusals, never fabrications. Otherwise the
    Intent Delta is derived and a full proposal package is returned with a
    terminal ``state``.
    """
    if codemap_draft.is_noop(draft):
        return None, REASON_NO_CHANGE
    if codemap_draft.conflict_for(draft, baseline)["state"] != codemap_draft.CONFLICT_NONE:
        return None, REASON_STALE
    delta, err = codemap_draft.generate_intent_delta(draft, baseline)
    if err is not None:  # pragma: no cover - guarded by the checks above
        return None, err
    return build_proposal(delta, baseline, store), None


# -- validation -----------------------------------------------------------


def validate_proposal(proposal: Any) -> Optional[str]:
    """Validate a proposal package against the P4.1 schema; return a reason or ``None``.

    A valid package is a mapping with the current ``schema_version``, a known
    terminal ``state``, ``executable`` and ``applied`` both ``False`` (never a
    patch/diff/approval/execution), a ``proposal:``-prefixed id, and every
    structured field present. Returns a bounded reason on failure.
    """
    if not isinstance(proposal, dict):
        return "proposal is not a mapping"
    if proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
        return "unsupported schema_version"
    state = proposal.get("state")
    if state not in PROPOSAL_STATES:
        return "unknown proposal state"
    if proposal.get("executable") is not False:
        return "proposal must be non-executable"
    if proposal.get("applied") is not False:
        return "proposal must be non-applied"
    pid = proposal.get("proposal_id")
    if not isinstance(pid, str) or not pid.startswith("proposal:"):
        return "missing or malformed proposal_id"
    if not isinstance(proposal.get("target_scope"), dict):
        return "missing or malformed target_scope"
    for field in (
        "affected_artifacts",
        "preserved_constraints",
        "assumptions",
        "clarifications",
        "plan_steps",
        "risks",
        "validation_plan",
    ):
        if not isinstance(proposal.get(field), list):
            return f"missing or malformed {field}"
    steps = proposal.get("plan_steps", [])
    for index, step in enumerate(steps, start=1):
        if step.get("step") != index:
            return "plan steps are not ordered"
    return None


__all__ = [
    "PROPOSAL_SCHEMA_VERSION",
    "PROPOSAL_GENERATOR",
    "STATE_READY",
    "STATE_CLARIFICATION_REQUIRED",
    "STATE_UNSUPPORTED",
    "STATE_NO_CHANGE",
    "STATE_BLOCKED",
    "PROPOSAL_STATES",
    "REASON_NO_CHANGE",
    "REASON_STALE",
    "REASON_UNSUPPORTED_TARGET",
    "REASON_AMBIGUOUS",
    "ROLE_TARGET",
    "dumps",
    "build_proposal",
    "plan_proposal",
    "validate_proposal",
]
