"""Deterministic advisory impact proposal (P5.3).

Takes a typed developer Intent Delta (:mod:`hrca.intent_delta`) plus the exact
read-side evidence the accepted architecture already publishes — one scanner
document and one Structured Code Twin store — and returns a versioned,
canonical, **advisory** statement of what the intent would affect.

This module creates nothing. It has no provider, no network, no credential, no
command, no Git, no filesystem and no store of its own: it is a pure function
from two supplied documents to one derived document. ``executable`` and
``applied`` are always ``False``, and ``mutation_surface`` declares, per
boundary the contract names, that nothing is written. The proposal is not a
candidate, not a diff, not a patch, not an approval and not an execution
outcome; it never claims that code exists or that repository state changed.

Exact binding, never a fallback
-------------------------------

Every identity in the output is an identity the evidence publishes verbatim: a
Twin artifact ``id``, a scanner symbol ``id``, a scanner relation ``id``, a
workspace-relative scanner file ``path``. The scope resolves by **exact**
identity only — a Twin artifact ``locator`` or file ``module`` equal to the
named entity, or a Twin artifact ``id`` equal to the named artifact. There is no
name matching, no path matching, no prefix matching, no positional matching and
no prose matching anywhere in this module, and a scope reference that does not
bind to exactly one artifact is refused rather than guessed at.

Relation ``target`` values are the scanner's literal source names, which the
Phase 1 contract explicitly never resolves. They are reported as
``resolved: false`` evidence entries — a statement about the document, never an
identity claim about a definition.

Revisions, and what invalidates a binding
-----------------------------------------

A binding is exact or it does not exist. The proposal refuses, rather than
silently re-binding, when:

* ``workspace_id`` differs — the evidence is another workspace's;
* ``scan_generation`` or ``baseline_fingerprint`` differs — the baseline moved;
* the scanner ``schema_version`` or ``grammar`` differs from the baseline the
  intent was authored against — the grammar that read the source changed, so a
  record this proposal would have read may never have existed;
* a scope reference binds to more than one artifact, or an artifact id occurs
  twice — which of the two the intent means is not knowable;
* an ``origin.evidence`` reference does not resolve to exactly one identity.

Refusal versus state
--------------------

They are different questions and this module keeps them apart.

A **refusal** is returned as ``(None, reason)``: the binding could not be
established at all, so nothing is proposed. Nothing is guessed, nothing is
partially read, and nothing is downgraded to a warning.

When the binding *does* hold, the impact itself is reported as a terminal
``state``, because "we could not determine the impact" is a result a reviewer
must see, not an error that hides it. The five determinations the P5.3 contract
asks to be distinguished are:

``unsupported``
    a scope reference occurs in neither the Twin store nor the scanner
    document: the accepted architecture does not hold that identity, so it
    cannot represent what the intent names.
``unavailable``
    the target is known to the Twin, but the scanner evidence that would
    describe it is absent from the supplied document.
``unknown``
    the source that declares the target is present but was not readable — the
    scanner recorded a parse error or a non-``ok`` syntax status for it.
``ambiguous``
    every identity bound, but the supplied evidence contradicts itself about a
    scoped fact (two records claim one id with different content), so *which*
    fact would be affected is not unique.
``no_impact`` / ``bound``
    everything read: the evidence records no source fact inside the bound
    scope (``no_impact``), or it records at least one (``bound``).

Precedence is fixed and documented: ``unsupported``, then ``unavailable``, then
``unknown``, then ``ambiguous``, then ``no_impact`` or ``bound``. ``no_impact``
is a positive finding about the bound evidence — never a place to put unknown
impact, and never rendered as empty success: it carries its own risk entry
saying so.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from . import intent_delta, scanner, twin

IMPACT_SCHEMA_VERSION = "1.0.0"
IMPACT_GENERATOR = "hrca-impact-proposal"
IMPACT_ID_PREFIX = "impact:"
BINDING_ID_PREFIX = "bind:"

# Terminal impact-precision states (see the module docstring for precedence).
STATE_BOUND = "bound"
STATE_NO_IMPACT = "no_impact"
STATE_UNKNOWN = "unknown"
STATE_UNAVAILABLE = "unavailable"
STATE_UNSUPPORTED = "unsupported"
STATE_AMBIGUOUS = "ambiguous"
IMPACT_STATES = frozenset(
    {
        STATE_BOUND,
        STATE_NO_IMPACT,
        STATE_UNKNOWN,
        STATE_UNAVAILABLE,
        STATE_UNSUPPORTED,
        STATE_AMBIGUOUS,
    }
)

# Bounded state reasons. Never caller content.
REASON_IMPACT_FOUND = "impact_found"
REASON_NO_SOURCE_FACT = "no_source_fact_inside_bound_scope"
REASON_UNREADABLE_SOURCE = "source_facts_are_not_readable"
REASON_SOURCE_ABSENT = "scanner_evidence_is_absent_for_the_target"
REASON_IDENTITY_UNKNOWN = "scope_identity_is_not_held_by_the_evidence"
REASON_EVIDENCE_CONTRADICTS = "evidence_contradicts_itself_about_a_scoped_fact"

_STATE_REASONS = {
    STATE_BOUND: REASON_IMPACT_FOUND,
    STATE_NO_IMPACT: REASON_NO_SOURCE_FACT,
    STATE_UNKNOWN: REASON_UNREADABLE_SOURCE,
    STATE_UNAVAILABLE: REASON_SOURCE_ABSENT,
    STATE_UNSUPPORTED: REASON_IDENTITY_UNKNOWN,
    STATE_AMBIGUOUS: REASON_EVIDENCE_CONTRADICTS,
}

_STATE_SENTENCES = {
    STATE_BOUND: (
        "the bound evidence records at least one source fact inside the intent's "
        "scope; the affected facts below are those facts, and nothing beyond them "
        "was inferred"
    ),
    STATE_NO_IMPACT: (
        "every scope reference bound exactly, and the bound evidence records no "
        "source fact inside the bound scope: no affected fact was found. This is "
        "a finding about the bound evidence only, not a statement that no impact "
        "exists"
    ),
    STATE_UNKNOWN: (
        "the source that declares a scoped fact is present in the bound evidence "
        "but was not readable, so the facts inside the scope are not known"
    ),
    STATE_UNAVAILABLE: (
        "a scoped fact is known to the Twin evidence, but the scanner evidence "
        "that would describe its source is absent from the supplied document"
    ),
    STATE_UNSUPPORTED: (
        "a scope reference is held by neither the Twin evidence nor the scanner "
        "evidence, so the accepted architecture cannot represent what the intent "
        "names"
    ),
    STATE_AMBIGUOUS: (
        "every scope reference bound, but the supplied evidence carries "
        "contradictory records for a scoped id, so which fact would be affected "
        "is not unique"
    ),
}

# Bounded refusal reasons: the binding could not be established.
REASON_INVALID_INTENT = "intent delta is not valid"
REASON_EVIDENCE_NOT_MAPPING = "evidence is not a mapping"
REASON_MISSING_SCANNER = "scanner evidence is missing"
REASON_MISSING_TWIN = "twin evidence is missing"
REASON_UNSUPPORTED_EVIDENCE = "an evidence document is not a supported document"
REASON_CROSS_WORKSPACE = "evidence belongs to a different workspace"
REASON_STALE_BASELINE = "evidence baseline does not match the intent baseline"
REASON_SCHEMA_CHANGED = "the scanner schema context does not match the intent baseline"
REASON_GRAMMAR_CHANGED = "the scanner grammar context does not match the intent baseline"
REASON_AMBIGUOUS_REFERENCE = "a scope reference does not bind to exactly one artifact"
REASON_UNBOUND_EVIDENCE = "an origin evidence reference does not resolve exactly"

# How a scope reference bound, and the facts it produced.
BINDING_ARTIFACT_ID = "named_by_exact_artifact_id"
BINDING_ENTITY_LOCATOR = "entity_locator_is_artifact_locator"
BINDING_ENTITY_MODULE = "entity_module_is_file_module"

ROLE_CONTAINED = "contained_symbol"
ROLE_RELATION = "relation_from_scope"

# Bounded evidence-binding kinds.
BIND_TWIN_ARTIFACT = "twin_artifact"
BIND_SCANNER_SYMBOL = "scanner_symbol"
BIND_SCANNER_RELATION = "scanner_relation"
BIND_SCANNER_FILE = "scanner_file"
BIND_ORIGIN_EVIDENCE = "origin_evidence"

# Fixed, source-grounded statements. Each is a fixed phrase; only exact
# identities taken from the evidence are interpolated.
_CONSTRAINT_SOURCE_IMMUTABLE = (
    "source facts bound by this proposal are read-only evidence; nothing here "
    "rewrites, deletes or moves them"
)
_CONSTRAINT_ADVISORY_ONLY = (
    "this proposal is advisory: it creates no candidate, diff, branch, commit, "
    "runner job, provider request, package state or stored fact"
)
_ASSUMPTION_EVIDENCE_STATIC = (
    "the bound evidence is the accepted read-side state and is not re-read while "
    "this proposal is reviewed"
)
_ASSUMPTION_NO_RUNTIME = (
    "no runtime, provider or credential verification of this intent exists"
)
_RISK_NOT_VERIFIED = (
    "the affected set is derived statically from bound identities; no runtime, "
    "provider or credential evidence verifies it"
)
_RISK_NO_IMPACT_IS_NOT_SAFETY = (
    "no affected fact was found in the bound evidence. That is a finding about "
    "the evidence supplied, not evidence that no impact exists beyond it"
)
_RISK_MODULE_SCOPE = (
    "the scope names a whole file artifact, so every fact inside it is in scope "
    "whether or not the intent concerns it"
)
_QUESTION_REVERSE_IMPACT = (
    "reverse impact is not determinable here: the scanner records relation "
    "targets as literal source names and this contract never resolves them to "
    "identities, so facts that depend on a scoped entity cannot be bound exactly"
)
_QUESTION_AUTHORITY = (
    "what authority would apply the requested outcome is not determined here; "
    "this proposal grants none and performs none"
)

# The complete mutation surface the P5.3 contract names. Every value is always
# ``False``; the block exists so a reviewer (and a test) can assert the whole
# surface at once instead of trusting a docstring.
_MUTATION_SURFACE_KEYS = (
    "candidate",
    "diff",
    "branch",
    "commit",
    "source_file",
    "git_index",
    "runner_job",
    "provider_request",
    "credential",
    "package_state",
    "recovery_state",
    "twin_state",
    "memory",
)


def dumps(obj: Any) -> str:
    """Serialize a proposal canonically (sorted keys, compact, ASCII-safe)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _mutation_surface() -> Dict[str, bool]:
    return {key: False for key in _MUTATION_SURFACE_KEYS}


# -- evidence indexes ------------------------------------------------------


def _artifacts(store: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in store.get("artifacts") or []:
        if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]:
            out.append(rec)
    return out


def _symbols(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in document.get("symbols") or []:
        if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]:
            out.append(rec)
    return out


def _relations(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in document.get("relations") or []:
        if isinstance(rec, dict) and isinstance(rec.get("id"), str) and rec["id"]:
            out.append(rec)
    return out


def _files(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for rec in document.get("files") or []:
        if isinstance(rec, dict) and isinstance(rec.get("path"), str) and rec["path"]:
            out[rec["path"]] = rec
    return out


def _parse_error_paths(document: Dict[str, Any]) -> set:
    out = set()
    for rec in document.get("parse_errors") or []:
        if isinstance(rec, dict) and isinstance(rec.get("file"), str) and rec["file"]:
            out.add(rec["file"])
    return out


def _duplicate_ids(records: List[Dict[str, Any]]) -> bool:
    """Return True when one id is claimed by two records that disagree."""
    seen: Dict[str, str] = {}
    for rec in records:
        identity = rec["id"]
        canon = dumps(rec)
        if identity in seen and seen[identity] != canon:
            return True
        seen[identity] = canon
    return False


# -- binding ---------------------------------------------------------------


def _bind_revision(
    intent: Dict[str, Any], store: Dict[str, Any], document: Dict[str, Any]
) -> Optional[str]:
    """Compare the intent baseline against the evidence; return a reason or None.

    Every comparison is equality between two published identities. A missing
    Twin revision is a mismatch, never a wildcard: an evidence document that
    cannot state which revision it is is not evidence for this intent.
    """
    baseline = intent["baseline"]
    revision = store.get("workspace_revision")
    if not isinstance(revision, dict):
        return REASON_UNSUPPORTED_EVIDENCE
    if revision.get("workspace_id") != baseline["workspace_id"]:
        return REASON_CROSS_WORKSPACE
    if revision.get("scan_generation") != baseline["scan_generation"]:
        return REASON_STALE_BASELINE
    if revision.get("baseline_fingerprint") != baseline["baseline_fingerprint"]:
        return REASON_STALE_BASELINE
    if document.get("schema_version") != baseline["scanner_schema_version"]:
        return REASON_SCHEMA_CHANGED
    if document.get("grammar") != baseline["grammar"]:
        return REASON_GRAMMAR_CHANGED
    return None


def _origin_evidence_is_bound(intent: Dict[str, Any], indexes: Dict[str, Any]) -> bool:
    """Return whether every origin evidence reference resolves to exactly one identity."""
    for ref in intent["origin"]["evidence"]:
        kind = ref["kind"]
        identity = ref["id"]
        if kind == intent_delta.REF_TWIN_ARTIFACT:
            if len(indexes["by_id"].get(identity, [])) != 1:
                return False
        elif kind == intent_delta.REF_SCANNER_SYMBOL:
            if identity not in indexes["symbol_ids"]:
                return False
        elif kind == intent_delta.REF_SCANNER_FILE:
            if identity not in indexes["files"]:
                return False
        else:  # pragma: no cover - the delta schema bounds this vocabulary
            return False
    return True


def _resolve_scope(
    intent: Dict[str, Any], indexes: Dict[str, Any]
) -> Tuple[Dict[str, Dict[str, Any]], List[str], Optional[str]]:
    """Resolve every scope reference to exactly one artifact.

    Returns ``(bound, unresolved, refusal)``. ``bound`` maps an artifact id to
    the artifact, the scope reference that named it, and how it bound.
    ``unresolved`` lists the references the evidence does not hold. A reference
    that binds to more than one artifact is a refusal: which one the intent
    means is not knowable from the evidence, and the exact artifact id is the
    route the intent has to disambiguate it.
    """
    bound: Dict[str, Dict[str, Any]] = {}
    unresolved: List[str] = []

    def take(artifact: Dict[str, Any], reference: str, binding: str) -> None:
        if artifact["id"] in bound:
            return
        bound[artifact["id"]] = {
            "artifact": artifact,
            "reference": reference,
            "binding": binding,
        }

    for entity in intent["scope"]["entities"]:
        candidates: Dict[str, Dict[str, Any]] = {}
        for artifact in indexes["by_locator"].get(entity, []):
            candidates[artifact["id"]] = artifact
        for artifact in indexes["by_module"].get(entity, []):
            candidates[artifact["id"]] = artifact
        if not candidates:
            unresolved.append(entity)
            continue
        if len(candidates) > 1:
            return bound, unresolved, REASON_AMBIGUOUS_REFERENCE
        artifact = next(iter(candidates.values()))
        binding = (
            BINDING_ENTITY_MODULE
            if artifact.get("kind") == twin.ARTIFACT_FILE
            else BINDING_ENTITY_LOCATOR
        )
        take(artifact, entity, binding)

    for artifact_id in intent["scope"]["artifacts"]:
        matches = indexes["by_id"].get(artifact_id, [])
        if not matches:
            unresolved.append(artifact_id)
            continue
        if len(matches) > 1:
            return bound, unresolved, REASON_AMBIGUOUS_REFERENCE
        take(matches[0], artifact_id, BINDING_ARTIFACT_ID)

    return bound, unresolved, None


# -- derived sections ------------------------------------------------------


def _contained_facts(
    bound: Dict[str, Dict[str, Any]], indexes: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return ``(facts, contradicted)`` for the facts inside the bound scope.

    A fact is included only when the evidence publishes its identity: a scanner
    symbol declared inside a bound artifact (by its declaring ``file`` for a file
    artifact, or by ``parent_id`` for a symbol artifact), or a scanner relation
    recorded against a bound file. Nothing is reached by name.

    ``target_scope`` already carries the artifacts the intent names. What this
    function reports is what the evidence records *inside* them, so an empty
    result is a real finding about the evidence rather than a bookkeeping zero.

    A fact is only established when the source that declares it is readable in
    the bound evidence. When the scanner has no file record for a target's path,
    or recorded that it did not parse, the facts inside it are not known — and
    reporting them anyway would contradict the state that says so. They are
    omitted, and the terminal state names the reason.
    """
    facts: List[Dict[str, Any]] = []
    contradicted = False

    def readable(path: Any) -> bool:
        if not isinstance(path, str):
            return False
        record = indexes["files"].get(path)
        if record is None:
            return False
        return record.get("syntax_status") == "ok" and path not in indexes["parse_error_paths"]

    for artifact_id in sorted(bound):
        entry = bound[artifact_id]
        artifact = entry["artifact"]
        if not readable(artifact.get("path")):
            continue
        if artifact.get("kind") == twin.ARTIFACT_FILE:
            path = artifact["path"]
            symbols = [s for s in indexes["symbols"] if s.get("file") == path]
            relations = [r for r in indexes["relations"] if r.get("file") == path]
            if _duplicate_ids(symbols) or _duplicate_ids(relations):
                contradicted = True
            for symbol in sorted(symbols, key=lambda s: s["id"]):
                facts.append(_symbol_fact(symbol, path, indexes))
            for relation in sorted(relations, key=lambda r: r["id"]):
                facts.append(_relation_fact(relation, path))
        else:
            locator = artifact.get("locator")
            if not isinstance(locator, str):
                continue
            path = artifact["path"]
            declared = [
                s
                for s in indexes["symbols"]
                if s.get("parent_id") == locator and s.get("file") == path
            ]
            if _duplicate_ids(declared):
                contradicted = True
            for symbol in sorted(declared, key=lambda s: s["id"]):
                facts.append(_symbol_fact(symbol, path, indexes))

    facts.sort(key=lambda f: (f["fact_kind"], f["id"]))
    deduped: List[Dict[str, Any]] = []
    seen: set = set()
    for fact in facts:
        key = (fact["fact_kind"], fact["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped, contradicted


def _symbol_fact(
    symbol: Dict[str, Any], path: Any, indexes: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "fact_kind": BIND_SCANNER_SYMBOL,
        "id": symbol["id"],
        "file": path if isinstance(path, str) else symbol.get("file"),
        "role": ROLE_CONTAINED,
        "twin_artifact_id": indexes["symbol_artifacts"].get(symbol["id"]),
        "source_range": symbol.get("source_range"),
    }


def _relation_fact(relation: Dict[str, Any], path: str) -> Dict[str, Any]:
    return {
        "fact_kind": BIND_SCANNER_RELATION,
        "id": relation["id"],
        "file": path,
        "role": ROLE_RELATION,
        "relation_kind": relation.get("kind"),
        "status": relation.get("status"),
        # The scanner records a relation target as the literal name in source.
        # It is never resolved to a definition here, and this field says so
        # rather than leaving it to be assumed.
        "resolved": False,
        "target": relation.get("target"),
    }


def _state_for(
    unresolved: List[str],
    bound: Dict[str, Dict[str, Any]],
    facts: List[Dict[str, Any]],
    contradicted: bool,
    indexes: Dict[str, Any],
) -> str:
    """Return the terminal state, in the documented precedence order.

    The document order of these checks is the precedence: an identity the
    architecture does not hold outranks a source it cannot read, which outranks
    evidence that contradicts itself, which outranks the positive findings.
    """
    if unresolved:
        return STATE_UNSUPPORTED
    for artifact_id in sorted(bound):
        path = bound[artifact_id]["artifact"].get("path")
        if not isinstance(path, str) or path not in indexes["files"]:
            return STATE_UNAVAILABLE
    for artifact_id in sorted(bound):
        path = bound[artifact_id]["artifact"]["path"]
        record = indexes["files"][path]
        if record.get("syntax_status") != "ok" or path in indexes["parse_error_paths"]:
            return STATE_UNKNOWN
    if contradicted:
        return STATE_AMBIGUOUS
    if not facts:
        return STATE_NO_IMPACT
    return STATE_BOUND


def _target_scope(
    bound: Dict[str, Dict[str, Any]],
    unresolved: List[str],
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the exact artifacts the intent names, plus what did not resolve."""
    return {
        "entities": list(intent["scope"]["entities"]),
        "artifacts": list(intent["scope"]["artifacts"]),
        "targets": [
            {
                "artifact_id": artifact_id,
                "kind": bound[artifact_id]["artifact"].get("kind"),
                "path": bound[artifact_id]["artifact"].get("path"),
                "reference": bound[artifact_id]["reference"],
                "binding": bound[artifact_id]["binding"],
            }
            for artifact_id in sorted(bound)
        ],
        "unresolved_references": sorted(unresolved),
    }


def _unchanged_constraints(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [
        {
            "constraint_id": "constraint:fixed:source_immutable",
            "statement": _CONSTRAINT_SOURCE_IMMUTABLE,
            "origin": "fixed",
        },
        {
            "constraint_id": "constraint:fixed:advisory_only",
            "statement": _CONSTRAINT_ADVISORY_ONLY,
            "origin": "fixed",
        },
    ]
    for index, statement in enumerate(intent["constraints"], start=1):
        out.append(
            {
                "constraint_id": f"constraint:declared:{index}",
                "statement": statement,
                "origin": "intent_delta",
            }
        )
    return out


def _assumptions(intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [
        {
            "assumption_id": "assumption:fixed:evidence_is_static",
            "statement": _ASSUMPTION_EVIDENCE_STATIC,
            "origin": "fixed",
        },
        {
            "assumption_id": "assumption:fixed:no_runtime_verification",
            "statement": _ASSUMPTION_NO_RUNTIME,
            "origin": "fixed",
        },
    ]
    for index, statement in enumerate(intent["assumptions"], start=1):
        out.append(
            {
                "assumption_id": f"assumption:declared:{index}",
                "statement": statement,
                "origin": "intent_delta",
            }
        )
    return out


def _risks(
    state: str,
    bound: Dict[str, Dict[str, Any]],
    facts: List[Dict[str, Any]],
    indexes: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return bounded risks, each grounded in a bound identity."""
    out: List[Dict[str, Any]] = [
        {
            "risk_id": "risk:impact_not_verified",
            "level": "medium",
            "statement": _RISK_NOT_VERIFIED,
            "evidence_ref": None,
        }
    ]
    if state == STATE_NO_IMPACT:
        out.append(
            {
                "risk_id": "risk:no_impact_is_not_absence_of_impact",
                "level": "medium",
                "statement": _RISK_NO_IMPACT_IS_NOT_SAFETY,
                "evidence_ref": None,
            }
        )
    unresolved_status = sorted(
        {
            fact["id"]
            for fact in facts
            if fact.get("relation_kind") == "imports" and fact.get("status") == "unresolved"
        }
    )
    if unresolved_status:
        out.append(
            {
                "risk_id": "risk:unresolved_literal_relations",
                "level": "medium",
                "statement": (
                    "the bound scope declares imports the scanner recorded as "
                    "unresolved; their targets are literal source names and no "
                    "definition was inferred from them"
                ),
                "evidence_ref": unresolved_status[0],
            }
        )
    for artifact_id in sorted(bound):
        path = bound[artifact_id]["artifact"].get("path")
        if not isinstance(path, str):
            continue
        record = indexes["files"].get(path)
        if record is not None and (
            record.get("syntax_status") != "ok" or path in indexes["parse_error_paths"]
        ):
            out.append(
                {
                    "risk_id": f"risk:unreadable_source:{path}",
                    "level": "high",
                    "statement": "a scoped file did not parse, so its facts are not known",
                    "evidence_ref": path,
                }
            )
    if any(
        entry["artifact"].get("kind") == twin.ARTIFACT_FILE for entry in bound.values()
    ):
        out.append(
            {
                "risk_id": "risk:module_scope_breadth",
                "level": "medium",
                "statement": _RISK_MODULE_SCOPE,
                "evidence_ref": None,
            }
        )
    out.sort(key=lambda r: r["risk_id"])
    return out


def _suggested_tests(
    state: str,
    bound: Dict[str, Dict[str, Any]],
    facts: List[Dict[str, Any]],
    indexes: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return bounded, advisory test suggestions, each with its reason.

    A suggestion is a statement of what a reviewer or a later phase should
    confirm. Nothing here is executed, scheduled or dispatched, and no test
    runner is reached.
    """
    out: List[Dict[str, Any]] = [
        {
            "test_id": "test:rescan_is_byte_identical",
            "statement": (
                "re-run the scanner over the workspace and confirm the document "
                "matches the bound evidence byte for byte"
            ),
            "reason": (
                "the binding is to a scanner schema and grammar context; a "
                "different grammar can change which records exist"
            ),
            "evidence_ref": None,
        },
        {
            "test_id": "test:baseline_fingerprint_unchanged",
            "statement": (
                "confirm the Twin baseline fingerprint still equals the one this "
                "proposal is bound to"
            ),
            "reason": (
                "a changed baseline fingerprint invalidates this binding entirely; "
                "the proposal would have to be rebuilt against the new evidence"
            ),
            "evidence_ref": None,
        },
    ]
    for artifact_id in sorted(bound):
        out.append(
            {
                "test_id": f"test:identity_still_resolves:{artifact_id}",
                "statement": (
                    f"confirm {artifact_id} still resolves at the same source "
                    "range in the current evidence"
                ),
                "reason": (
                    "this artifact is in scope by exact identity; the proposal "
                    "asserts nothing about it once that identity moves"
                ),
                "evidence_ref": artifact_id,
            }
        )
    for entry in sorted(bound.values(), key=lambda e: e["artifact"]["id"]):
        artifact = entry["artifact"]
        path = artifact.get("path")
        record = indexes["files"].get(path) if isinstance(path, str) else None
        if record is not None and (
            record.get("syntax_status") != "ok" or path in indexes["parse_error_paths"]
        ):
            out.append(
                {
                    "test_id": f"test:parse_error_cleared:{path}",
                    "statement": f"confirm {path} parses under the bound grammar",
                    "reason": (
                        "the scope names a file that did not parse, so the impact "
                        "of this intent inside it is unknown"
                    ),
                    "evidence_ref": path,
                }
            )
    if any(
        fact.get("relation_kind") == "imports" and fact.get("status") == "unresolved"
        for fact in facts
    ):
        out.append(
            {
                "test_id": "test:unresolved_relations_unchanged",
                "statement": (
                    "confirm no additional unresolved import is introduced inside "
                    "the bound scope"
                ),
                "reason": (
                    "an unresolved import is a place where a static impact claim "
                    "cannot be made, so a new one widens the blind spot"
                ),
                "evidence_ref": None,
            }
        )
    out.sort(key=lambda t: t["test_id"])
    return out


def _unresolved_questions(state: str, intent: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [
        {
            "question_id": "question:fixed:reverse_impact",
            "question": _QUESTION_REVERSE_IMPACT,
            "origin": "fixed",
        },
        {
            "question_id": "question:fixed:authority",
            "question": _QUESTION_AUTHORITY,
            "origin": "fixed",
        },
    ]
    if state in (STATE_UNSUPPORTED, STATE_UNAVAILABLE, STATE_UNKNOWN):
        out.append(
            {
                "question_id": "question:fixed:scope_not_bound",
                "question": (
                    "which exact identity the intent names, and what source fact "
                    "inside it the requested outcome concerns"
                ),
                "origin": "fixed",
            }
        )
    for index, question in enumerate(intent["unresolved_questions"], start=1):
        out.append(
            {
                "question_id": f"question:declared:{index}",
                "question": question,
                "origin": "intent_delta",
            }
        )
    out.sort(key=lambda q: q["question_id"])
    return out


def _evidence_bindings(
    intent: Dict[str, Any],
    bound: Dict[str, Dict[str, Any]],
    facts: List[Dict[str, Any]],
    indexes: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return every exact identity this proposal is bound to, sorted.

    A file binding is recorded only when the scanner document actually holds
    that file record, so the list is a statement about the supplied evidence
    rather than about what the Twin would have expected to find in it.
    """
    out: List[Dict[str, Any]] = []
    for ref in intent["origin"]["evidence"]:
        out.append(
            {
                "kind": BIND_ORIGIN_EVIDENCE,
                "id": f"{ref['kind']}:{ref['id']}",
            }
        )
    for artifact_id in sorted(bound):
        out.append({"kind": BIND_TWIN_ARTIFACT, "id": artifact_id})
        path = bound[artifact_id]["artifact"].get("path")
        if isinstance(path, str) and path in indexes["files"]:
            out.append({"kind": BIND_SCANNER_FILE, "id": path})
    for fact in facts:
        out.append({"kind": fact["fact_kind"], "id": fact["id"]})
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for entry in sorted(out, key=lambda e: (e["kind"], e["id"])):
        key = (entry["kind"], entry["id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _binding_record(
    intent: Dict[str, Any], store: Dict[str, Any], document: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the workspace/schema/grammar identity the proposal is bound to.

    No filesystem path appears here — not the scan root, not a home directory,
    not an interpreter path. The identities are the ones the read side already
    publishes as identities.
    """
    baseline = intent["baseline"]
    record: Dict[str, Any] = {
        "workspace_id": baseline["workspace_id"],
        "scan_generation": baseline["scan_generation"],
        "baseline_fingerprint": baseline["baseline_fingerprint"],
        "scanner_schema_version": baseline["scanner_schema_version"],
        "scanner_grammar": {
            "implementation": baseline["grammar"]["implementation"],
            "version": baseline["grammar"]["version"],
        },
        "twin_schema_version": store.get("schema_version"),
    }
    record["binding_fingerprint"] = BINDING_ID_PREFIX + twin.sha256_hex(
        dumps(record).encode("utf-8")
    )
    return record


def impact_proposal_id_for(proposal: Dict[str, Any]) -> str:
    """Return the content-addressed identity of a proposal (never time-derived)."""
    canon = dumps({k: v for k, v in proposal.items() if k != "proposal_id"})
    return IMPACT_ID_PREFIX + twin.sha256_hex(canon.encode("utf-8"))


# -- derivation ------------------------------------------------------------


def build_impact_proposal(
    intent: Dict[str, Any], evidence: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Derive the deterministic advisory impact proposal, or refuse a binding.

    ``intent`` is a delta from :func:`hrca.intent_delta.build_intent_delta`;
    ``evidence`` is ``{"scanner": <scan document>, "twin": <Twin store>}``.
    Returns ``(proposal, error)``: a refusal is ``(None, bounded reason)``, and
    a bound analysis is ``(proposal, None)`` with a terminal ``state``.
    """
    if intent_delta.validate_intent_delta(intent) is not None:
        return None, REASON_INVALID_INTENT
    if not isinstance(evidence, dict):
        return None, REASON_EVIDENCE_NOT_MAPPING
    if "scanner" not in evidence:
        return None, REASON_MISSING_SCANNER
    if "twin" not in evidence:
        return None, REASON_MISSING_TWIN

    # Both evidence documents go through the accepted migration entry points.
    # A malformed or newer document is refused by the module that owns the
    # schema; this contract never reads a shape it does not understand.
    document, error = scanner.migrate_document(evidence["scanner"])
    if error is not None:
        return None, REASON_UNSUPPORTED_EVIDENCE
    raw_store = evidence["twin"]
    if not isinstance(raw_store, dict):
        return None, REASON_UNSUPPORTED_EVIDENCE
    store, error = twin.migrate_store(raw_store)
    if error is not None:
        return None, REASON_UNSUPPORTED_EVIDENCE

    refusal = _bind_revision(intent, store, document)
    if refusal is not None:
        return None, refusal

    artifacts = _artifacts(store)
    symbols = _symbols(document)
    relations = _relations(document)
    indexes: Dict[str, Any] = {
        "artifacts": artifacts,
        "symbols": symbols,
        "relations": relations,
        "files": _files(document),
        "parse_error_paths": _parse_error_paths(document),
        "symbol_ids": {s["id"] for s in symbols},
        "by_id": {},
        "by_locator": {},
        "by_module": {},
        "symbol_artifacts": {},
    }
    for artifact in artifacts:
        indexes["by_id"].setdefault(artifact["id"], []).append(artifact)
        locator = artifact.get("locator")
        if isinstance(locator, str) and locator:
            indexes["by_locator"].setdefault(locator, []).append(artifact)
            indexes["symbol_artifacts"].setdefault(locator, artifact["id"])
        if artifact.get("kind") == twin.ARTIFACT_FILE:
            module = artifact.get("module")
            if isinstance(module, str) and module:
                indexes["by_module"].setdefault(module, []).append(artifact)

    if not _origin_evidence_is_bound(intent, indexes):
        return None, REASON_UNBOUND_EVIDENCE

    bound, unresolved, refusal = _resolve_scope(intent, indexes)
    if refusal is not None:
        return None, refusal

    facts, contradicted = _contained_facts(bound, indexes)
    state = _state_for(unresolved, bound, facts, contradicted, indexes)

    proposal: Dict[str, Any] = {
        "schema_version": IMPACT_SCHEMA_VERSION,
        "generator": IMPACT_GENERATOR,
        "proposal_id": "",
        "intent_delta_id": intent["intent_delta_id"],
        "state": state,
        "reason_code": _STATE_REASONS[state],
        "reason": _STATE_SENTENCES[state],
        "advisory": True,
        "executable": False,
        "applied": False,
        # The delta may have carried free-form prose. This proposal records that
        # it was supplied and never re-publishes it: a downstream document must
        # not become the place pasted text comes back out.
        "prose_present": bool((intent.get("prose") or {}).get("text")),
        "binding": _binding_record(intent, store, document),
        "target_scope": _target_scope(bound, unresolved, intent),
        "affected_facts": facts,
        "unchanged_constraints": _unchanged_constraints(intent),
        "assumptions": _assumptions(intent),
        "risks": _risks(state, bound, facts, indexes),
        "suggested_tests": _suggested_tests(state, bound, facts, indexes),
        "unresolved_questions": _unresolved_questions(state, intent),
        "evidence_bindings": _evidence_bindings(intent, bound, facts, indexes),
        "mutation_surface": _mutation_surface(),
        "confidence": twin.CONF_HIGH if state in (STATE_BOUND, STATE_NO_IMPACT) else twin.CONF_LOW,
    }
    proposal["proposal_id"] = impact_proposal_id_for(proposal)
    return proposal, None


# -- validation ------------------------------------------------------------


def validate_impact_proposal(proposal: Any) -> Optional[str]:
    """Validate a proposal against the P5.3 schema; return a reason or ``None``.

    A valid proposal declares the current schema and generator, carries an
    ``impact:``-prefixed id that matches its own content, holds a known state, is
    advisory and neither executable nor applied, and declares every mutation
    surface as ``False``.
    """
    if not isinstance(proposal, dict):
        return "proposal is not a mapping"
    if proposal.get("schema_version") != IMPACT_SCHEMA_VERSION:
        return "unsupported schema_version"
    if proposal.get("generator") != IMPACT_GENERATOR:
        return "unknown proposal generator"
    if proposal.get("state") not in IMPACT_STATES:
        return "unknown impact state"
    if proposal.get("advisory") is not True:
        return "proposal must be advisory"
    if proposal.get("executable") is not False:
        return "proposal must be non-executable"
    if proposal.get("applied") is not False:
        return "proposal must be non-applied"
    if not isinstance(proposal.get("intent_delta_id"), str):
        return "missing or malformed intent_delta_id"
    identity = proposal.get("proposal_id")
    if not isinstance(identity, str) or not identity.startswith(IMPACT_ID_PREFIX):
        return "missing or malformed proposal_id"
    if identity != impact_proposal_id_for(proposal):
        return "proposal_id does not match the proposal content"
    for field in (
        "binding",
        "target_scope",
        "mutation_surface",
    ):
        if not isinstance(proposal.get(field), dict):
            return f"missing or malformed {field}"
    for field in (
        "affected_facts",
        "unchanged_constraints",
        "assumptions",
        "risks",
        "suggested_tests",
        "unresolved_questions",
        "evidence_bindings",
    ):
        if not isinstance(proposal.get(field), list):
            return f"missing or malformed {field}"
    surface = proposal.get("mutation_surface")
    if set(surface) != set(_MUTATION_SURFACE_KEYS):
        return "mutation_surface does not declare every named boundary"
    for key, value in sorted(surface.items()):
        if value is not False:
            return f"mutation_surface declares a non-false {key}"
    binding = proposal.get("binding")
    fingerprint = binding.get("binding_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith(BINDING_ID_PREFIX):
        return "missing or malformed binding_fingerprint"
    return None


__all__ = [
    "IMPACT_SCHEMA_VERSION",
    "IMPACT_GENERATOR",
    "IMPACT_ID_PREFIX",
    "BINDING_ID_PREFIX",
    "STATE_BOUND",
    "STATE_NO_IMPACT",
    "STATE_UNKNOWN",
    "STATE_UNAVAILABLE",
    "STATE_UNSUPPORTED",
    "STATE_AMBIGUOUS",
    "IMPACT_STATES",
    "ROLE_CONTAINED",
    "ROLE_RELATION",
    "REASON_INVALID_INTENT",
    "REASON_EVIDENCE_NOT_MAPPING",
    "REASON_MISSING_SCANNER",
    "REASON_MISSING_TWIN",
    "REASON_UNSUPPORTED_EVIDENCE",
    "REASON_CROSS_WORKSPACE",
    "REASON_STALE_BASELINE",
    "REASON_SCHEMA_CHANGED",
    "REASON_GRAMMAR_CHANGED",
    "REASON_AMBIGUOUS_REFERENCE",
    "REASON_UNBOUND_EVIDENCE",
    "dumps",
    "build_impact_proposal",
    "validate_impact_proposal",
    "impact_proposal_id_for",
]
