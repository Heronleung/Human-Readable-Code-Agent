"""Append-only human revisions, immutable generated history and effective resolution.

M4.5/v1a. Schema 1.1.0 adds two human-facing record kinds to the Developer
Memory contract:

``generated_document``
    an immutable snapshot of one projected document, at one revision, with the
    content fingerprint that identifies exactly what was generated.
``correction``
    an append-only, human-owned revision over one *exact typed target*.

Authority rules
---------------
* **A correction changes what a reader is shown, never what was recorded.** It
  cannot alter a normalized record, cannot change a run's terminal state, cannot
  imply repository adoption and cannot claim the recorded baseline is current.
* **History is append-only.** No operation deletes or rewrites a generated
  version, a correction, or a normalized record. Superseding adds a successor;
  it never removes the thing it supersedes.
* **Resolution is by stable typed identity, never prose.** An overlay binds to
  the claim it names, and it is compatible only while that claim's fingerprint is
  unchanged. A missing, changed or ambiguous target becomes an explicit
  unresolved conflict — never a guessed replacement, and never a match by
  similarity or order.
* **Only confirmed overlays are authority.** A draft is retained and reviewable
  and changes nothing; a rejected, superseded or archived revision stays in
  history and changes nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import memory

# -- operations ----------------------------------------------------------

OP_KEEP = "keep"
OP_MERGE = "merge"
OP_SUPERSEDE = "supersede"
OP_REJECT = "reject"

OPERATIONS = (OP_KEEP, OP_MERGE, OP_SUPERSEDE, OP_REJECT)

# -- states --------------------------------------------------------------

STATE_DRAFT = "draft"
STATE_CONFIRMED = "confirmed"
STATE_REJECTED = "rejected"
STATE_SUPERSEDED = "superseded"
STATE_ARCHIVED = "archived"

CORRECTION_STATES = (
    STATE_DRAFT,
    STATE_CONFIRMED,
    STATE_REJECTED,
    STATE_SUPERSEDED,
    STATE_ARCHIVED,
)

# ``unresolved_conflict`` is deliberately *not* a stored state: a stored state
# would have to be written, and writing a resolution outcome into append-only
# history would be a rewrite. It is a resolution result instead, and it is
# distinct from every stored state.
OUTCOME_UNRESOLVED_CONFLICT = "unresolved_conflict"

# The states whose overlays the resolver treats as authority. A confirmed
# revision and a rejection both bind: confirmation affirms the claim, rejection
# marks it — and neither is allowed to delete it from the generated document.
AUTHORITY_STATES = (STATE_CONFIRMED, STATE_REJECTED)

# The states that are retained in history and change nothing.
INERT_STATES = (STATE_DRAFT, STATE_ARCHIVED, STATE_SUPERSEDED)

# The operations that carry replacement text.
TEXT_OPERATIONS = (OP_MERGE, OP_SUPERSEDE)

# -- bounds --------------------------------------------------------------

MAX_CORRECTION_TEXT_CHARS = 2048
MAX_CORRECTION_ACTOR_CHARS = 128
MAX_SUPERSEDES = 16
MAX_CORRECTIONS_PER_RUN = 512
MAX_GENERATED_VERSIONS = 64
MAX_EFFECTIVE_CLAIMS = 512
MAX_CONFLICTS = 256

# -- bounded reasons -----------------------------------------------------

REASON_NOT_A_STORE = "revision input is not a store mapping"
REASON_OPERATION_UNSUPPORTED = "operation is not supported by the revision model"
REASON_STATE_UNSUPPORTED = "state is not supported by the revision model"
REASON_TARGET_UNUSABLE = "correction target is not a usable typed identity"
REASON_DOCUMENT_UNSUPPORTED = "document type is not supported by the projector"
REASON_TEXT_UNUSABLE = "correction text is empty or exceeds the bounded length"
REASON_TEXT_NOT_ALLOWED = "this operation does not accept replacement text"
REASON_ACTOR_UNUSABLE = "actor is empty or exceeds the bounded length"
REASON_SUPERSEDES_UNUSABLE = "supersedes list is unusable"
REASON_SUPERSEDES_UNKNOWN = "a named revision to supersede does not exist in this run"
REASON_CORRECTION_LIMIT = "the run already holds the bounded number of corrections"
REASON_VERSION_LIMIT = "the document already holds the bounded number of versions"
REASON_SOURCE_ID_UNUSABLE = "correction source id is empty or exceeds the bounded length"
REASON_IDENTITY_CONFLICT = (
    "a revision with this identity already exists with different content"
)

# Resolution reasons.
REASON_TARGET_NOT_PRESENT = "the correction names a claim that is not in this document"
REASON_TARGET_CHANGED = (
    "the claim changed since the correction was made, so the correction no longer binds"
)
REASON_BASE_UNKNOWN = "the correction records no usable base fingerprint"


# -- identity ------------------------------------------------------------


def _identity_key(basis: Any) -> str:
    """Return the bounded 32-hex identity of ``basis``."""
    return memory.sha256_hex(memory.dumps(basis).encode("utf-8"))[:32]


def fingerprint(value: Any) -> str:
    """Return a canonical content fingerprint for ``value``."""
    return "sha256:" + memory.sha256_hex(memory.dumps(value).encode("utf-8"))


def claim_fingerprint(claim: Any) -> str:
    """Return the fingerprint of one projected claim."""
    return fingerprint(claim)


def document_fingerprint(document: Any) -> str:
    """Return the fingerprint of one projected document's content."""
    return fingerprint(document)


def generated_document_id(run_id: str, document_type: str, revision: int) -> str:
    """Return the stable id of one generated-document version."""
    return "gendoc:%s:%s:%d" % (run_id, document_type, revision)


def correction_id_for(run_id: str, raw: Dict[str, Any], normalized: Dict[str, Any]) -> str:
    """Return the identity of one correction.

    A caller-supplied ``source_id`` is preferred, so an identical retry keeps its
    identity while a *different* command reusing that identity is recognisable
    as a conflict. Without one, identity falls back to the normalized content,
    which makes an identical retry idempotent by construction.
    """
    source_id = raw.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        basis: Any = {"source_id": source_id.strip()}
    else:
        basis = {"content": normalized}
    return "correction:%s:%s" % (run_id, _identity_key(basis))


# -- normalization -------------------------------------------------------


def _bounded_text(value: Any, limit: int) -> Optional[str]:
    """Return a redacted copy of ``value``, or ``None`` when unusable.

    A human's own words are redacted but never silently truncated: text over the
    bound is refused so the author learns the limit rather than discovering later
    that half of what they wrote was discarded. The redaction still runs first,
    so nothing this module refuses or keeps can carry a secret.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    redacted = memory.redact_text(value)
    if len(redacted) > limit:
        return None
    return redacted


def normalize_correction(
    run_id: str, raw: Any, base_fingerprint: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(normalized, reason)`` for one correction command.

    Everything a caller supplies is bounded and redacted here, before anything
    is fingerprinted or written, so a durable revision cannot carry content the
    contract would refuse elsewhere.
    """
    if not isinstance(raw, dict):
        return None, REASON_OPERATION_UNSUPPORTED

    operation = raw.get("operation")
    if operation not in OPERATIONS:
        return None, REASON_OPERATION_UNSUPPORTED

    state = raw.get("state")
    if state is None:
        state = STATE_REJECTED if operation == OP_REJECT else STATE_CONFIRMED
    if state not in CORRECTION_STATES:
        return None, REASON_STATE_UNSUPPORTED
    # A rejected operation states itself; a caller cannot confirm by rejecting,
    # nor quietly mark a rejection as a draft that later reads as authority.
    if operation == OP_REJECT and state == STATE_CONFIRMED:
        state = STATE_REJECTED

    document_type = raw.get("document_type")
    if not isinstance(document_type, str) or not document_type.strip():
        return None, REASON_DOCUMENT_UNSUPPORTED
    document_type = document_type.strip()

    # The target is a *claim*, named by its stable projected identity — the same
    # identity a document renders. The typed record the claim points at is
    # optional context, never the thing that is corrected.
    target = raw.get("target")
    if not isinstance(target, dict):
        return None, REASON_TARGET_UNUSABLE
    claim_id = target.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id.strip():
        return None, REASON_TARGET_UNUSABLE
    for optional in (target.get("kind"), target.get("record_id")):
        if optional is not None and (
            not isinstance(optional, str) or not optional.strip()
        ):
            return None, REASON_TARGET_UNUSABLE

    text = _bounded_text(raw.get("text"), MAX_CORRECTION_TEXT_CHARS)
    if raw.get("text") not in (None, "") and text is None:
        return None, REASON_TEXT_UNUSABLE
    if operation in TEXT_OPERATIONS and text is None:
        return None, REASON_TEXT_UNUSABLE
    if operation not in TEXT_OPERATIONS and text is not None:
        return None, REASON_TEXT_NOT_ALLOWED

    actor = _bounded_text(raw.get("actor"), MAX_CORRECTION_ACTOR_CHARS)
    if raw.get("actor") not in (None, "") and actor is None:
        return None, REASON_ACTOR_UNUSABLE

    supersedes = raw.get("supersedes")
    if supersedes is None:
        supersedes = []
    if not isinstance(supersedes, list) or len(supersedes) > MAX_SUPERSEDES:
        return None, REASON_SUPERSEDES_UNUSABLE
    parents: List[str] = []
    for parent in supersedes:
        if not isinstance(parent, str) or not parent.strip():
            return None, REASON_SUPERSEDES_UNUSABLE
        if parent.strip() not in parents:
            parents.append(parent.strip())

    source_id = raw.get("source_id")
    if source_id is not None and (
        not isinstance(source_id, str) or not source_id.strip()
        or len(source_id.strip()) > memory.MAX_SOURCE_ID_CHARS
    ):
        return None, REASON_SOURCE_ID_UNUSABLE

    created_at = _bounded_text(raw.get("created_at"), memory.MAX_TIMESTAMP_CHARS)

    normalized = {
        "run_id": run_id,
        "document_type": document_type,
        "operation": operation,
        "state": state,
        "target": {
            "claim_id": claim_id.strip(),
            "kind": target["kind"].strip() if isinstance(target.get("kind"), str) else None,
            "record_id": (
                target["record_id"].strip()
                if isinstance(target.get("record_id"), str)
                else None
            ),
        },
        "text": text,
        "actor": actor,
        "created_at": created_at,
        "supersedes": sorted(parents),
        "base_content_fingerprint": base_fingerprint,
        "source_id": source_id.strip() if isinstance(source_id, str) else None,
    }
    return normalized, None


def _correction_record(normalized: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    return {
        "id": correction_id_for(run_id, normalized, normalized),
        "record_kind": memory.RECORD_CORRECTION,
        "run_id": run_id,
        "document_type": normalized["document_type"],
        "operation": normalized["operation"],
        "state": normalized["state"],
        "target": dict(normalized["target"]),
        "base_content_fingerprint": normalized["base_content_fingerprint"],
        "supersedes": list(normalized["supersedes"]),
        "text": normalized["text"],
        "actor": normalized["actor"],
        "created_at": normalized["created_at"],
        "source_id": normalized["source_id"],
        # A human revision is the human's word; nothing else verifies it.
        "provenance": memory.PROVENANCE_SOURCE,
        "privacy": memory.privacy_counts(),
    }


# -- appending -----------------------------------------------------------


def _content_of(record: Dict[str, Any]) -> str:
    """Return the canonical content of a revision, excluding its identity."""
    return memory.dumps({k: v for k, v in record.items() if k != "id"})


def _append_unique(store: Dict[str, Any], array: str, record: Dict[str, Any]) -> bool:
    """Append ``record`` unless its id is present; return whether it was added."""
    target = store.setdefault(array, [])
    for existing in target:
        if isinstance(existing, dict) and existing.get("id") == record.get("id"):
            return False
    target.append(record)
    return True


def _sort_store(store: Dict[str, Any]) -> None:
    """Sort every array deterministically, keeping events in ingest order.

    A store written by the append path never passes through ``finalize``, so it
    must be canonicalised here or two identical command sequences could produce
    two different byte sequences.
    """
    for array in memory.STORE_ARRAYS:
        records = store.get(array)
        if not isinstance(records, list):
            continue
        if array == "events":
            store[array] = sorted(
                records,
                key=lambda r: (r.get("ingest_ordinal", 0), str(r.get("id", ""))),
            )
        else:
            store[array] = sorted(records, key=lambda r: str(r.get("id", "")))


def record_generated_version(
    store: Dict[str, Any], run_id: str, document_type: str, document: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Record one immutable generated-document version, or reuse the last.

    Regenerating identical content does not create a new revision: the content
    fingerprint is compared first, so a retry is a no-op and the history stays a
    record of *changes*, not of how often a reader looked.
    """
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE
    content_fingerprint = document_fingerprint(document)
    existing = [
        record
        for record in store.get("generated_documents", [])
        if isinstance(record, dict)
        and record.get("run_id") == run_id
        and record.get("document_type") == document_type
    ]
    existing.sort(key=lambda r: r.get("revision", 0))
    if existing and existing[-1].get("content_fingerprint") == content_fingerprint:
        return existing[-1], None
    if len(existing) >= MAX_GENERATED_VERSIONS:
        return None, REASON_VERSION_LIMIT

    revision = (existing[-1].get("revision", 0) + 1) if existing else 1
    record = {
        "id": generated_document_id(run_id, document_type, revision),
        "record_kind": memory.RECORD_GENERATED_DOCUMENT,
        "run_id": run_id,
        "document_type": document_type,
        "revision": revision,
        "content": document,
        "content_fingerprint": content_fingerprint,
        "provenance": memory.PROVENANCE_DERIVED,
        "privacy": memory.privacy_counts(),
    }
    _append_unique(store, "generated_documents", record)
    return record, None


def append_correction(
    store: Dict[str, Any],
    run_id: str,
    raw: Any,
    base_fingerprint: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Append one correction. Returns ``(record, reason, created)``.

    ``base_fingerprint`` is the fingerprint of the target claim *as the caller
    observed it*, computed by the caller from the projected document rather than
    trusted from the request: a durable revision records the baseline it actually
    bound to, so a later regeneration can tell whether it still applies.

    ``created`` is ``False`` when an identical command was already appended, so a
    retry is observably idempotent rather than silently duplicated.
    """
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE, False
    # A correction that records no baseline could never bind later, and would sit
    # in history looking like authority while changing nothing. It is refused.
    if not isinstance(base_fingerprint, str) or not base_fingerprint:
        return None, REASON_BASE_UNKNOWN, False
    normalized, reason = normalize_correction(run_id, raw, base_fingerprint)
    if normalized is None:
        return None, reason, False

    run = store.get("agent_run")
    if not isinstance(run, dict) or run.get("id") != run_id:
        return None, REASON_NOT_A_STORE, False

    corrections = store.get("corrections")
    if isinstance(corrections, list) and len(corrections) >= MAX_CORRECTIONS_PER_RUN:
        return None, REASON_CORRECTION_LIMIT, False

    # Every named parent must exist: a supersede chain that points at nothing
    # would silently change what history means.
    known = {
        record.get("id")
        for record in corrections or []
        if isinstance(record, dict)
    }
    for parent in normalized["supersedes"]:
        if parent not in known:
            return None, REASON_SUPERSEDES_UNKNOWN, False

    record = _correction_record(normalized, run_id)
    for existing in corrections or []:
        if not isinstance(existing, dict) or existing.get("id") != record["id"]:
            continue
        # Identity is stable across retries by design, so the same identity can
        # arrive with different content. An identical retry is a no-op; anything
        # else is refused rather than silently overwriting a durable revision.
        if _content_of(existing) == _content_of(record):
            return existing, None, False
        return None, REASON_IDENTITY_CONFLICT, False
    _append_unique(store, "corrections", record)
    _sort_store(store)
    return record, None, True


# -- resolution ----------------------------------------------------------


def _claims_of(document: Any) -> Dict[str, Dict[str, Any]]:
    claims: Dict[str, Dict[str, Any]] = {}
    if not isinstance(document, dict):
        return claims
    for claim in document.get("claims") or []:
        if isinstance(claim, dict) and isinstance(claim.get("id"), str):
            claims[claim["id"]] = claim
    return claims


def _corrections_for(
    store: Dict[str, Any], run_id: str, document_type: str
) -> List[Dict[str, Any]]:
    records = [
        record
        for record in store.get("corrections", [])
        if isinstance(record, dict)
        and record.get("run_id") == run_id
        and record.get("document_type") == document_type
    ]
    return sorted(records, key=lambda r: str(r.get("id", "")))


def correction_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return the bounded projection of one correction for a reader.

    The stored baseline fingerprint is deliberately absent: it is the internal
    binding key, and no fingerprint or digest value crosses this boundary — only
    the fact that a baseline was recorded.
    """
    return {
        "id": record.get("id"),
        "record_kind": record.get("record_kind"),
        "run_id": record.get("run_id"),
        "document_type": record.get("document_type"),
        "operation": record.get("operation"),
        "state": record.get("state"),
        "target": record.get("target"),
        "supersedes": list(record.get("supersedes") or []),
        "text": record.get("text"),
        "actor": record.get("actor"),
        "created_at": record.get("created_at"),
        "source_id": record.get("source_id"),
        "base_recorded": bool(record.get("base_content_fingerprint")),
        "provenance": record.get("provenance"),
        "privacy": record.get("privacy"),
    }


def version_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return the bounded projection of one generated-document version."""
    return {
        "id": record.get("id"),
        "record_kind": record.get("record_kind"),
        "run_id": record.get("run_id"),
        "document_type": record.get("document_type"),
        "revision": record.get("revision"),
        "content": record.get("content"),
        "provenance": record.get("provenance"),
    }


def history_for(
    store: Any, run_id: str, document_type: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the append-only history of one run, optionally one document type."""
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE
    run = store.get("agent_run")
    if not isinstance(run, dict) or run.get("id") != run_id:
        return None, REASON_NOT_A_STORE

    def _for_type(records: Sequence[Any]) -> List[Dict[str, Any]]:
        return [
            record
            for record in records
            if isinstance(record, dict)
            and record.get("run_id") == run_id
            and (document_type is None or record.get("document_type") == document_type)
        ]

    versions = sorted(
        _for_type(store.get("generated_documents", [])),
        key=lambda r: (str(r.get("document_type")), r.get("revision", 0)),
    )
    corrections = sorted(
        _for_type(store.get("corrections", [])), key=lambda r: str(r.get("id", ""))
    )
    superseded = {
        parent
        for record in corrections
        for parent in record.get("supersedes", [])
        if isinstance(parent, str)
    }
    return {
        "run_id": run_id,
        "document_type": document_type,
        "generated_versions": [version_view(record) for record in versions],
        "corrections": [correction_view(record) for record in corrections],
        "superseded_ids": sorted(superseded),
        "limitations": [
            "history is append-only: no entry here is ever rewritten or removed",
            "a correction is the human's word; nothing else verifies it",
            "a correction never changes a normalized record or a run state",
        ],
    }, None


def resolve_effective(
    store: Any,
    run_id: str,
    document_type: str,
    document: Any,
    generated_version: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve the effective document: the generated projection plus overlays.

    Returns ``(effective, reason)``. Only confirmed, non-superseded, non-archived
    overlays apply, and only while their exact target is unchanged. Everything
    else is reported — never dropped, never guessed, never deleted.
    """
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE
    if not isinstance(document, dict):
        return None, REASON_DOCUMENT_UNSUPPORTED

    claims = _claims_of(document)
    corrections = _corrections_for(store, run_id, document_type)
    superseded = {
        parent
        for record in corrections
        for parent in record.get("supersedes", [])
        if isinstance(parent, str)
    }

    overlays: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    applied: List[str] = []
    for record in corrections:
        correction_id = record.get("id")
        state = record.get("state")
        if correction_id in superseded:
            # Superseded by a later revision: retained in history, not authority.
            continue
        if state in INERT_STATES:
            continue
        if state not in AUTHORITY_STATES:
            continue

        target = record.get("target") if isinstance(record.get("target"), dict) else {}
        claim_id = target.get("claim_id") or target.get("record_id")
        base = record.get("base_content_fingerprint")
        if not isinstance(base, str) or not base:
            conflicts.append(
                _conflict(record, REASON_BASE_UNKNOWN)
            )
            continue
        claim = claims.get(claim_id) if isinstance(claim_id, str) else None
        if claim is None:
            conflicts.append(_conflict(record, REASON_TARGET_NOT_PRESENT))
            continue
        if claim_fingerprint(claim) != base:
            conflicts.append(_conflict(record, REASON_TARGET_CHANGED))
            continue
        overlays[claim_id] = record
        applied.append(correction_id)

    effective_claims: List[Dict[str, Any]] = []
    for claim_id, claim in sorted(claims.items()):
        overlay = overlays.get(claim_id)
        entry: Dict[str, Any] = {
            "claim_id": claim_id,
            "generated_statement": claim.get("statement"),
            "effective_statement": claim.get("statement"),
            "generated_provenance": claim.get("provenance"),
            "effective_provenance": claim.get("provenance"),
            "rejected": False,
            "overlay": None,
        }
        if overlay is not None:
            operation = overlay.get("operation")
            text = overlay.get("text")
            entry["overlay"] = {
                "correction_id": overlay.get("id"),
                "operation": operation,
                "state": overlay.get("state"),
                "actor": overlay.get("actor"),
                "created_at": overlay.get("created_at"),
                "text": text,
                "supersedes": list(overlay.get("supersedes") or []),
            }
            entry["effective_provenance"] = memory.PROVENANCE_SOURCE
            if operation == OP_REJECT:
                entry["rejected"] = True
            elif operation in TEXT_OPERATIONS and isinstance(text, str):
                entry["effective_statement"] = text
        effective_claims.append(entry)

    result = {
        "schema_version": memory.MEMORY_SCHEMA_VERSION,
        "generator": memory.MEMORY_GENERATOR,
        "run_id": run_id,
        "document_type": document_type,
        # Only the version identity crosses; the content fingerprint stays the
        # resolver's internal binding key.
        "generated": {
            "revision": (generated_version or {}).get("revision"),
            "version_id": (generated_version or {}).get("id"),
        },
        "applied_correction_ids": sorted(applied),
        "claims": effective_claims[:MAX_EFFECTIVE_CLAIMS],
        "conflicts": conflicts[:MAX_CONFLICTS],
        "limitations": [
            "a confirmed correction changes what a reader is shown; it never "
            "changes a normalized record, a run state, or the repository",
            "the recorded baseline is not verified by any correction, and no "
            "correction claims it is current",
            "completion is not acceptance, and no correction implies adoption",
            "resolution binds by exact typed target and unchanged content only; "
            "a missing, changed or ambiguous target is reported, never guessed",
        ],
    }
    return result, None


def _conflict(record: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "outcome": OUTCOME_UNRESOLVED_CONFLICT,
        "correction_id": record.get("id"),
        "state": record.get("state"),
        "operation": record.get("operation"),
        "reason": reason,
        "target": record.get("target"),
        "base_recorded": bool(record.get("base_content_fingerprint")),
        "actor": record.get("actor"),
        "created_at": record.get("created_at"),
    }


def render(payload: Dict[str, Any]) -> str:
    """Return the canonical serialization of a history or effective result."""
    return memory.dumps(payload)
