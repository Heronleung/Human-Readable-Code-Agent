"""Typed Memory-to-Code-Twin links and an honest freshness comparison (M4.5/v2b).

A link binds **one Developer Memory record** to **one exact Code Twin entity**
plus the source revision that was current when the link was taken. It is a
*claim about identity and revision*, never a claim about what the source now
contains: only a comparison against authoritative Code Twin state may say
whether the link is still current, and that comparison is returned, never
persisted.

Identity is exact or it is nothing
----------------------------------

The entity identity is the Twin's own deterministic identifier —
``artifact:file:<root-relative-path>`` or ``artifact:<kind>:<locator>`` where
``<kind>`` is one of ``class``/``function``/``method`` and ``<locator>`` is the
scanner's dotted ``module.path.Class.method``. Resolution is a lookup **by that
exact identifier** in the authoritative store. There is no fallback of any kind:
no display label, no prose, no path substring, no "closest" or "similarly named"
entity, no row order, no digest and no caller-supplied repository path is ever
consulted. A miss is reported as a miss.

The recorded revision
---------------------

The recorded revision is the Twin's own workspace revision number
(``workspace_revision.scan_generation``), a monotone integer the Twin only
advances when a scan really changes the workspace. Using the revision *number*
rather than a content digest is deliberate: a fingerprint value never crosses
the boundary anywhere else in this program (a digest is exposed only as a
present/absent boolean, see :mod:`hrca.memory_docs`), and a revision identity is
what a link is defined to carry, so the number is the honest representation that
adds no new disclosure class.

Freshness is a returned comparison, never a stored assertion
-----------------------------------------------------------

:func:`resolve_freshness` is the only thing that may call a link current, and it
answers with exactly one of five states:

``current``
    the entity resolves as a *current* artifact of the authoritative store and
    the recorded revision equals the authoritative revision. Because the Twin
    advances its generation only for a real content change, equality means the
    workspace the link was taken against has not moved.
``historical``
    the entity resolves, but the Twin itself marks the record as a *retained,
    non-current* version (a last-valid symbol kept when its file stopped
    parsing, or a record held back from an ambiguous rename). The link points at
    history and is **never** reported as current, whatever its revision says.
``stale``
    the entity resolves as a current artifact, but the recorded revision is not
    the authoritative one: the source has moved on since the link was taken.
``missing``
    the entity identifier is absent from the authoritative store.
``unsupported``
    no comparison is possible: no readable authoritative store, a store written
    by an unsupported schema, a link recorded against a *different* workspace, an
    artifact state this contract does not classify, or an unusable revision.

``missing`` and ``unsupported`` are returned as visible, **non-actionable**
results — never as a silent success and never as an error, so a caller can show
them honestly without inventing an answer.

What this module deliberately is not
------------------------------------

It is pure: no filesystem, no store, no clock, no network, and no model. It
reads a Twin store document it is *given* and returns bounded values, so the
boundary stays the only place that roots storage and the only place that can
speak to a workspace. It never writes, so a link cannot accept a repository
change, alter run state, correction authority, generated evidence or source
truth, and it cannot claim a verified current baseline.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from . import twin

LINK_SCHEMA_VERSION = "1.0.0"

# -- freshness vocabulary ------------------------------------------------

FRESHNESS_CURRENT = "current"
FRESHNESS_HISTORICAL = "historical"
FRESHNESS_STALE = "stale"
FRESHNESS_MISSING = "missing"
FRESHNESS_UNSUPPORTED = "unsupported"
FRESHNESS_STATES = (
    FRESHNESS_CURRENT,
    FRESHNESS_HISTORICAL,
    FRESHNESS_STALE,
    FRESHNESS_MISSING,
    FRESHNESS_UNSUPPORTED,
)

# A link the Twin cannot act on. Missing and unsupported are deliberately in
# this set: they are honest answers, not invitations to guess.
NON_ACTIONABLE_STATES = frozenset({FRESHNESS_MISSING, FRESHNESS_UNSUPPORTED})

# Twin ``sync_state`` values as they appear on an artifact. A *current* state
# means the record is a live projection of the scanned source; a *retained*
# state means the Twin kept an earlier version because the current source could
# not be projected. Anything else is not classified by this contract and is
# refused as unsupported rather than assumed current.
CURRENT_ARTIFACT_STATES = frozenset({twin.SYNC_SYNCHRONIZED})
RETAINED_ARTIFACT_STATES = frozenset({twin.SYNC_STALE, twin.SYNC_NEEDS_REVIEW})

# Bounded reasons. They are fixed sentences that interpolate nothing, so an
# error can never carry store content, a path, a payload or a caller's text.
REASON_ENTITY_MALFORMED = "the entity identity is not a well-formed Twin identifier"
REASON_KIND_MISMATCH = "the entity kind does not match the entity identity"
REASON_STORE_UNSUPPORTED = "no comparable authoritative Twin state is available"
REASON_ENTITY_ABSENT = "the entity is not present in the authoritative store"
REASON_ENTITY_RETAINED = (
    "the entity is retained as a non-current version, so no new link may bind it"
)
REASON_WORKSPACE_MISMATCH = "the link was recorded against a different workspace"
REASON_ENTITY_STATE = "the Twin does not classify this artifact's state"
REASON_GENERATION = "the authoritative workspace revision is unusable"

# -- bounds --------------------------------------------------------------

MAX_ENTITY_ID_CHARS = 256
MAX_LOCATOR_CHARS = 512
# A scan generation is a bounded counter, never an arbitrary integer: a value
# outside this range is not something the Twin could have written.
MAX_REVISION = 1_000_000_000


# -- entity identity -----------------------------------------------------


def parse_entity_id(entity_id: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(kind, body, error)`` for a typed Twin artifact identifier.

    The identifier must be ``artifact:<kind>:<body>`` with ``kind`` one of the
    Twin's artifact kinds. A file body must be a plain root-relative path: no
    absolute name, no drive or colon, no backslash, no traversal segment and no
    empty segment, so a link can never name a location outside the workspace.
    A symbol body must be a dotted locator of Python identifiers, which is what
    the scanner's qualified names are. Nothing here consults content, a label or
    the store: an identifier is well-formed or it is refused.
    """
    if not isinstance(entity_id, str):
        return None, None, REASON_ENTITY_MALFORMED
    text = entity_id.strip()
    if not text or len(text) > MAX_ENTITY_ID_CHARS:
        return None, None, REASON_ENTITY_MALFORMED
    parts = text.split(":", 2)
    if len(parts) != 3 or parts[0] != "artifact":
        return None, None, REASON_ENTITY_MALFORMED
    kind, body = parts[1], parts[2]
    if kind not in twin.ARTIFACT_KINDS or not body:
        return None, None, REASON_ENTITY_MALFORMED
    if kind == twin.ARTIFACT_FILE:
        if len(body) > MAX_LOCATOR_CHARS or _unsafe_rel_path(body):
            return None, None, REASON_ENTITY_MALFORMED
    else:
        if len(body) > MAX_LOCATOR_CHARS or not _is_locator(body):
            return None, None, REASON_ENTITY_MALFORMED
    return kind, body, None


def _unsafe_rel_path(body: str) -> bool:
    """Return whether ``body`` is not a plain root-relative path."""
    if body.startswith("/") or "\\" in body or ":" in body:
        return True
    segments = body.split("/")
    return any(segment in ("", ".", "..") for segment in segments)


def _is_locator(body: str) -> bool:
    """Return whether ``body`` is a dotted locator of identifiers."""
    return all(segment.isidentifier() for segment in body.split("."))


# -- the link ------------------------------------------------------------


def build_link(
    workspace_id: Any,
    entity_id: Any,
    entity_kind: Any,
    memory_run_id: Any,
    memory_record_id: Any,
    store: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build the typed link for a Memory record against authoritative Twin state.

    Returns ``(link, error)``. A link is only ever built against an entity the
    authoritative store really holds **as a current artifact**, and the recorded
    revision is always read *from that store* rather than accepted from a
    caller: a caller cannot assert currentness, so a link can never carry a
    revision the Twin did not report. Nothing is written: the link is a value.
    """
    kind, _body, error = parse_entity_id(entity_id)
    if error is not None:
        return None, error
    if entity_kind != kind:
        # The declared kind must be the identity's own kind. A caller cannot
        # relabel an entity to cross a kind boundary.
        return None, REASON_KIND_MISMATCH
    for value in (workspace_id, memory_run_id, memory_record_id):
        if not isinstance(value, str) or not value.strip():
            return None, REASON_ENTITY_MALFORMED

    artifact, artifact_error = _artifact_by_id(store, entity_id)
    if artifact_error is not None:
        return None, artifact_error
    if artifact is None:
        return None, REASON_ENTITY_ABSENT
    if artifact.get("sync_state") not in CURRENT_ARTIFACT_STATES:
        # A retained (historical) or unclassified record is never a valid thing
        # to bind a new link against: a link taken against history would be
        # born out of date, and the Twin would say so immediately.
        return None, REASON_ENTITY_RETAINED

    revision, revision_error = current_revision(store)
    if revision_error is not None:
        return None, revision_error
    return {
        "link_schema_version": LINK_SCHEMA_VERSION,
        "workspace_id": workspace_id.strip(),
        "entity_id": entity_id.strip(),
        "entity_kind": kind,
        "memory_run_id": memory_run_id.strip(),
        "memory_record_id": memory_record_id.strip(),
        "recorded_revision": revision,
    }, None


def validate_link(link: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(link, error)`` after checking every field of a link document.

    The revision must be a bounded integer. That is what keeps a digest from
    ever travelling in the revision field: a fingerprint is not a revision this
    contract accepts, so it cannot be smuggled in and echoed back.
    """
    if not isinstance(link, dict):
        return None, REASON_ENTITY_MALFORMED
    if link.get("link_schema_version") != LINK_SCHEMA_VERSION:
        return None, REASON_ENTITY_MALFORMED
    kind, _body, error = parse_entity_id(link.get("entity_id"))
    if error is not None:
        return None, error
    if link.get("entity_kind") != kind:
        return None, REASON_KIND_MISMATCH
    revision = link.get("recorded_revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        return None, REASON_GENERATION
    if revision < 0 or revision > MAX_REVISION:
        return None, REASON_GENERATION
    for field in ("workspace_id", "memory_run_id", "memory_record_id"):
        value = link.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, REASON_ENTITY_MALFORMED
    return dict(link), None


# -- authoritative state -------------------------------------------------


def current_revision(store: Any) -> Tuple[Optional[int], Optional[str]]:
    """Return the authoritative workspace revision number of a Twin store."""
    if not isinstance(store, dict):
        return None, REASON_STORE_UNSUPPORTED
    if store.get("schema_version") != twin.TWIN_SCHEMA_VERSION:
        return None, REASON_STORE_UNSUPPORTED
    revision = store.get("workspace_revision")
    if not isinstance(revision, dict):
        return None, REASON_STORE_UNSUPPORTED
    generation = revision.get("scan_generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        return None, REASON_GENERATION
    if generation < 0 or generation > MAX_REVISION:
        return None, REASON_GENERATION
    return generation, None


def _artifact_by_id(
    store: Any, entity_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the artifact with exactly this identifier, or ``(None, None)``.

    The lookup is keyed by identifier alone. No other field is inspected, so a
    similarly named entity, a same-named entity in another module or a record
    that merely mentions this one can never be substituted for it.
    """
    if not isinstance(store, dict):
        return None, REASON_STORE_UNSUPPORTED
    artifacts = store.get("artifacts")
    if not isinstance(artifacts, list):
        return None, REASON_STORE_UNSUPPORTED
    for record in artifacts:
        if isinstance(record, dict) and record.get("id") == entity_id:
            return record, None
    return None, None


# -- the comparison ------------------------------------------------------


def resolve_freshness(
    link: Dict[str, Any], store: Any, workspace_id: str
) -> Dict[str, Any]:
    """Compare a link's recorded revision with authoritative Twin state.

    Always returns a bounded result — one of the five freshness states — and
    never reports ``current`` unless the authoritative store attests it. The
    order of the checks is part of the contract:

    1. a link recorded against another workspace is never compared at all, so a
       link can never be evaluated against the wrong repository;
    2. an unreadable, absent or unsupported store makes the comparison
       impossible, which is reported as unsupported rather than assumed;
    3. a retained (historical) record is decided *before* the revision is even
       looked at, so history can never appear current;
    4. only then are the recorded and authoritative revisions compared.
    """
    entity_id = link.get("entity_id")
    result = {
        "entity_id": entity_id,
        "entity_kind": link.get("entity_kind"),
        "workspace_id": link.get("workspace_id"),
        "memory_run_id": link.get("memory_run_id"),
        "memory_record_id": link.get("memory_record_id"),
        "recorded_revision": link.get("recorded_revision"),
        "current_revision": None,
        "freshness": FRESHNESS_UNSUPPORTED,
        "reason": REASON_STORE_UNSUPPORTED,
        "actionable": False,
    }

    if link.get("workspace_id") != workspace_id:
        result["reason"] = REASON_WORKSPACE_MISMATCH
        return result

    if not isinstance(store, dict) or store.get("schema_version") != twin.TWIN_SCHEMA_VERSION:
        return result
    revision_record = store.get("workspace_revision")
    if not isinstance(revision_record, dict):
        return result

    artifact, artifact_error = _artifact_by_id(store, entity_id)
    if artifact_error is not None:
        return result
    if artifact is None:
        # The identity is simply not in the authoritative store. This is a
        # visible, non-actionable answer: nothing is substituted for it.
        result["freshness"] = FRESHNESS_MISSING
        result["reason"] = None
        return result

    state = artifact.get("sync_state")
    if state in RETAINED_ARTIFACT_STATES:
        # The Twin kept this as an earlier version and says so. It is history,
        # and it is decided before any revision comparison can call it current.
        result["freshness"] = FRESHNESS_HISTORICAL
        result["reason"] = None
        if _usable_revision(revision_record):
            result["current_revision"] = revision_record.get("scan_generation")
        return result
    if state not in CURRENT_ARTIFACT_STATES:
        # An artifact state this contract does not classify is never guessed at.
        result["reason"] = REASON_ENTITY_STATE
        return result

    authoritative, revision_error = current_revision(store)
    if revision_error is not None:
        result["reason"] = revision_error
        return result
    result["current_revision"] = authoritative
    if authoritative == result["recorded_revision"]:
        result["freshness"] = FRESHNESS_CURRENT
        result["reason"] = None
        result["actionable"] = True
        return result
    result["freshness"] = FRESHNESS_STALE
    result["reason"] = None
    result["actionable"] = True
    return result


def _usable_revision(revision_record: Dict[str, Any]) -> bool:
    """Return whether a workspace revision record carries a usable counter."""
    generation = revision_record.get("scan_generation")
    return (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and 0 <= generation <= MAX_REVISION
    )


__all__ = [
    "LINK_SCHEMA_VERSION",
    "FRESHNESS_CURRENT",
    "FRESHNESS_HISTORICAL",
    "FRESHNESS_STALE",
    "FRESHNESS_MISSING",
    "FRESHNESS_UNSUPPORTED",
    "FRESHNESS_STATES",
    "NON_ACTIONABLE_STATES",
    "CURRENT_ARTIFACT_STATES",
    "RETAINED_ARTIFACT_STATES",
    "REASON_ENTITY_MALFORMED",
    "REASON_KIND_MISMATCH",
    "REASON_STORE_UNSUPPORTED",
    "REASON_ENTITY_ABSENT",
    "REASON_ENTITY_RETAINED",
    "REASON_WORKSPACE_MISMATCH",
    "REASON_ENTITY_STATE",
    "REASON_GENERATION",
    "MAX_ENTITY_ID_CHARS",
    "MAX_LOCATOR_CHARS",
    "MAX_REVISION",
    "parse_entity_id",
    "build_link",
    "validate_link",
    "current_revision",
    "resolve_freshness",
]
