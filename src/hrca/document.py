"""Document / version-authority domain (P4.4).

The first complete version-authority slice for Specification 2.0. This module is
the Qt-free, dependency-free *domain* for three distinct, never-conflated
records that live together under one per-document store:

* **Working Document** — user-authored UTF-8 md/txt prose plus an immutable,
  append-only revision history (revision identity, content fingerprint, byte
  size, timestamp and provenance). A saved document is *user intent*.
* **Candidate** — a deterministic, version-bound record that binds one exact
  document revision plus the app-package snapshot, the runner/runtime identity,
  the validation identity and the accepted predecessor. A candidate is *not*
  accepted behavior, and it is *never* generated from the prose: it is bound to
  the hand-written quotation fixture only.
* **Accepted Version** — a version-bound record produced only by explicit
  adoption. It is an assertion about *which* candidate was accepted at a point
  in time, never an assertion of universal correctness.

The module is deliberately pure in the same sense as :mod:`hrca.twin` and
:mod:`hrca.codemap_draft`:

* no filesystem, network, credential, command, Git, repository-write, provider
  or runner access — persistence belongs to :mod:`hrca.version_store` and the
  boundary supplies the package snapshot, runtime identity and validation
  identity;
* Qt-free, stdlib-only plus the pure :mod:`hrca.app_package` domain;
* every record is assembled deterministically from its inputs only, and every
  rejection is a bounded reason that never interpolates user prose.

The adoption path is the authority: before the Accepted Version pointer moves,
the exact candidate is revalidated against the *current* document head, package
fingerprint, runtime identity, validation identity and accepted predecessor.
A stale candidate, a changed document, missing/failed evidence, a corrupt
manifest, an unexpected baseline, or a repeated/concurrent adoption is a
bounded refusal that leaves the previous Accepted Version intact.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import app_package

DOCUMENT_SCHEMA_VERSION = "1.0.0"
DOCUMENT_GENERATOR = "hrca-document"

# Document kinds, derived from the user-authored name's suffix.
KIND_MARKDOWN = "md"
KIND_TEXT = "txt"
KINDS = frozenset({KIND_MARKDOWN, KIND_TEXT})

# Bounded input limits (code-owned; never from document text).
MAX_DOCUMENT_NAME_CHARS = 120

# Provenance values a revision can carry. Only ``user_saved`` is produced today;
# the field is the explicit provenance slot future origins may extend without
# changing the load path.
ORIGIN_USER_SAVED = "user_saved"

# Candidate generation source. It is *always* the deterministic fixture; a
# candidate is never claimed to be derived from, or an interpretation of, the
# document prose.
SOURCE_DETERMINISTIC_FIXTURE = "deterministic_fixture"

# Fixed limitation attached to every candidate: it makes the non-interpretation
# visible in the record itself, never merely in a UI label.
CANDIDATE_LIMITATION = (
    "Bound to the hand-written quotation fixture only; no document-to-code "
    "interpretation occurred."
)

# Fixed validation identity. It names the code-owned validator and its schema
# version, so a candidate whose evidence came from a different validator is
# rejected at adoption rather than silently trusted.
VALIDATION_IDENTITY = "app_package.validate_package:" + app_package.APP_PACKAGE_SCHEMA_VERSION

# Bounded reasons (never interpolate caller content).
REASON_NOT_MAPPING = "store is not a mapping"
REASON_MISSING_VERSION = "missing schema_version"
REASON_INVALID_VERSION = "invalid schema_version"
REASON_FUTURE_VERSION = "schema_version is newer than supported"
REASON_NOT_MIGRATABLE = "schema_version is not migratable"

REASON_CONTENT_INVALID = "content is not valid text"
REASON_STALE = "stale"
REASON_NO_REVISION = "no saved revision"

REASON_CANDIDATE_NOT_FOUND = "candidate not found"
REASON_CANDIDATE_STALE = "candidate is stale"
REASON_ALREADY_ADOPTED = "candidate already adopted"
REASON_CORRUPT = "candidate manifest is corrupt"
REASON_EVIDENCE_FAILED = "required validation evidence failed"
REASON_BASELINE_MISMATCH = "accepted baseline does not match"

REASON_VERSION_NOT_FOUND = "accepted version not found"

# The candidate fields whose canonical serialization produces the deterministic
# candidate id. ``adopted``, ``created_at`` and the id itself are deliberately
# excluded so an identical binding always yields an identical id.
_CANDIDATE_BINDING_KEYS = (
    "document_revision_id",
    "document_fingerprint",
    "package_id",
    "package_fingerprint",
    "runtime_identity",
    "validation_identity",
    "accepted_predecessor_id",
)

# Historical migrations (none before 1.0.0). Kept as a registry so later phases
# can add migrations without changing the load path; a future/unknown version is
# never migrated and never overwritten.
MIGRATIONS: Dict[str, Any] = {}


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _copy(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of ``store`` (so callers never mutate in place)."""
    return json.loads(dumps(store))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


def migrate_document(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and migrate a raw store to the current schema version.

    Returns ``(store, error)``; on success ``error`` is ``None``, on a
    future/unknown version or a malformed mapping ``store`` is ``None`` and
    ``error`` is a bounded reason. The caller must retain the last valid store
    whenever this returns an error.
    """
    if not isinstance(raw, dict):
        return None, REASON_NOT_MAPPING
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, REASON_MISSING_VERSION
    try:
        current = _version_tuple(DOCUMENT_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, REASON_INVALID_VERSION
    if found == current:
        return raw, None
    if found > current:
        return None, REASON_FUTURE_VERSION
    if version not in MIGRATIONS:
        return None, REASON_NOT_MIGRATABLE
    return MIGRATIONS[version](dict(raw)), None


# -- identifiers ---------------------------------------------------------


def new_document_id() -> str:
    """Return a fresh opaque document id (``doc:<32 hex>``)."""
    return "doc:" + uuid.uuid4().hex


def new_revision_id() -> str:
    """Return a fresh opaque revision id (``rev:<32 hex>``)."""
    return "rev:" + uuid.uuid4().hex


def new_version_id() -> str:
    """Return a fresh opaque accepted-version id (``ver:<32 hex>``)."""
    return "ver:" + uuid.uuid4().hex


# -- document name/kind ---------------------------------------------------


def kind_for_name(name: str) -> Optional[str]:
    """Return the document kind (``md``/``txt``) for ``name``, or ``None``."""
    if not isinstance(name, str):
        return None
    lowered = name.strip().lower()
    if lowered.endswith(".md"):
        return KIND_MARKDOWN
    if lowered.endswith(".txt"):
        return KIND_TEXT
    return None


def valid_name(name: Any) -> bool:
    """Return True when ``name`` is a valid, bounded md/txt document name."""
    return (
        isinstance(name, str)
        and bool(name.strip())
        and len(name) <= MAX_DOCUMENT_NAME_CHARS
        and kind_for_name(name) is not None
    )


# -- store construction ---------------------------------------------------


def new_document_store(document_id: str, name: str, kind: str, now: str) -> Dict[str, Any]:
    """Return a fresh, empty document store (no revisions, no candidate)."""
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "generator": DOCUMENT_GENERATOR,
        "document_id": document_id,
        "name": name,
        "kind": kind,
        "created_at": now,
        "revisions": [],
        "head_revision_id": None,
        "candidates": [],
        "accepted_versions": [],
        "current_accepted_version_id": None,
    }


def _revision(store: Dict[str, Any], revision_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the revision with ``revision_id``, or ``None``."""
    if not revision_id:
        return None
    for rev in store.get("revisions", []):
        if isinstance(rev, dict) and rev.get("revision_id") == revision_id:
            return rev
    return None


def _candidate(store: Dict[str, Any], candidate_id: str) -> Optional[Dict[str, Any]]:
    """Return the candidate with ``candidate_id``, or ``None``."""
    for cand in store.get("candidates", []):
        if isinstance(cand, dict) and cand.get("candidate_id") == candidate_id:
            return cand
    return None


def _accepted(store: Dict[str, Any], version_id: str) -> Optional[Dict[str, Any]]:
    """Return the accepted version with ``version_id``, or ``None``."""
    for ver in store.get("accepted_versions", []):
        if isinstance(ver, dict) and ver.get("version_id") == version_id:
            return ver
    return None


def _candidate_id(candidate: Dict[str, Any]) -> str:
    """Return the deterministic candidate id from its binding fields."""
    binding = {k: candidate[k] for k in _CANDIDATE_BINDING_KEYS}
    return "cand:" + sha256_hex(dumps(binding).encode("utf-8"))


# -- save (append an immutable revision) ----------------------------------


def save_revision(
    store: Dict[str, Any],
    content: str,
    base_revision_id: Optional[str],
    now: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Append an immutable revision for ``content`` and return ``(store, result)``.

    ``base_revision_id`` is the head the caller loaded; it must match the current
    head or the save is refused with ``stale`` (an external change happened
    between load and save). The exact ``content`` bytes — never trimmed,
    normalized or rewritten — are stored and fingerprinted. Only the Working
    Document changes; candidate and accepted state are untouched.

    Returns ``(new_store, revision)`` on success, ``(None, reason)`` on refusal.
    """
    if not isinstance(content, str):
        return None, REASON_CONTENT_INVALID

    head = store.get("head_revision_id")
    if base_revision_id != head:
        return None, REASON_STALE

    revisions = [r for r in store.get("revisions", []) if isinstance(r, dict)]
    last_number = max((r.get("revision_number") for r in revisions if isinstance(r.get("revision_number"), int)), default=0)

    revision = {
        "record_type": "revision",
        "revision_id": new_revision_id(),
        "revision_number": last_number + 1,
        "parent_revision_id": head,
        "content": content,
        "content_fingerprint": sha256_hex(content.encode("utf-8")),
        "byte_size": len(content.encode("utf-8")),
        "created_at": now,
        "origin": ORIGIN_USER_SAVED,
    }

    new_store = _copy(store)
    new_store["revisions"].append(revision)
    new_store["head_revision_id"] = revision["revision_id"]
    return new_store, revision


# -- candidate ------------------------------------------------------------


def build_candidate(
    store: Dict[str, Any],
    package_snapshot: Dict[str, Any],
    runtime_identity: str,
    validation_identity: str,
    now: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build a deterministic candidate bound to the current head revision.

    Returns ``(candidate, None)`` on success, ``(None, reason)`` on refusal (no
    saved revision). The candidate binds the head document revision, the package
    snapshot fingerprint, the runtime identity, the validation identity and the
    current accepted predecessor — and carries the explicit non-interpretation
    limitation.
    """
    head = store.get("head_revision_id")
    head_rev = _revision(store, head)
    if head is None or head_rev is None:
        return None, REASON_NO_REVISION

    package_id = package_snapshot.get("package_id") if isinstance(package_snapshot, dict) else None
    package_fingerprint = sha256_hex(dumps(package_snapshot).encode("utf-8"))

    candidate: Dict[str, Any] = {
        "record_type": "candidate",
        "candidate_id": "",
        "document_revision_id": head,
        "document_fingerprint": head_rev.get("content_fingerprint"),
        "package_id": package_id,
        "package_fingerprint": package_fingerprint,
        "runtime_identity": runtime_identity,
        "validation_identity": validation_identity,
        "accepted_predecessor_id": store.get("current_accepted_version_id"),
        "generation_source": SOURCE_DETERMINISTIC_FIXTURE,
        "limitation": CANDIDATE_LIMITATION,
        "adopted": False,
        "created_at": now,
    }
    candidate["candidate_id"] = _candidate_id(candidate)
    return candidate, None


# -- adoption (explicit, revalidated) -------------------------------------


def adopt_candidate(
    store: Dict[str, Any],
    candidate_id: str,
    package_snapshot: Dict[str, Any],
    runtime_identity: str,
    validation_identity: str,
    now: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Revalidate and atomically adopt ``candidate_id`` into an Accepted Version.

    Before the accepted pointer moves, every condition is re-checked against the
    *current* store:

    * the candidate exists and its manifest is intact (its deterministic id
      matches its binding fields);
    * it has not already been adopted (repeated/concurrent adoption);
    * its document revision is still the current head, with an unchanged content
      fingerprint (changed document / stale candidate);
    * its accepted predecessor is still the current accepted version (unexpected
      baseline);
    * its package, runtime and validation identities match the current, code-owned
      values, and the package still validates (missing/failed required evidence).

    On success a new Accepted Version is appended and the pointer is moved; on any
    refusal ``(None, reason)`` is returned and the previous Accepted Version is
    left intact.
    """
    candidate = _candidate(store, candidate_id)
    if candidate is None:
        return None, REASON_CANDIDATE_NOT_FOUND
    if _candidate_id(candidate) != candidate_id:
        return None, REASON_CORRUPT
    if candidate.get("adopted"):
        return None, REASON_ALREADY_ADOPTED

    head = store.get("head_revision_id")
    head_rev = _revision(store, head)
    if candidate.get("document_revision_id") != head:
        return None, REASON_CANDIDATE_STALE
    if head_rev is None or candidate.get("document_fingerprint") != head_rev.get("content_fingerprint"):
        return None, REASON_CANDIDATE_STALE

    if candidate.get("accepted_predecessor_id") != store.get("current_accepted_version_id"):
        return None, REASON_BASELINE_MISMATCH

    package_id = package_snapshot.get("package_id") if isinstance(package_snapshot, dict) else None
    package_fingerprint = sha256_hex(dumps(package_snapshot).encode("utf-8"))
    if (
        candidate.get("package_id") != package_id
        or candidate.get("package_fingerprint") != package_fingerprint
        or candidate.get("runtime_identity") != runtime_identity
        or candidate.get("validation_identity") != validation_identity
    ):
        return None, REASON_EVIDENCE_FAILED

    if app_package.validate_package(package_snapshot) is not None:
        return None, REASON_EVIDENCE_FAILED

    version: Dict[str, Any] = {
        "record_type": "accepted_version",
        "version_id": new_version_id(),
        "candidate_id": candidate_id,
        "document_revision_id": candidate["document_revision_id"],
        "document_fingerprint": candidate["document_fingerprint"],
        "package_id": candidate["package_id"],
        "package_fingerprint": candidate["package_fingerprint"],
        "runtime_identity": candidate["runtime_identity"],
        "validation_identity": candidate["validation_identity"],
        "predecessor_id": candidate["accepted_predecessor_id"],
        "accepted_at": now,
        "restore_of": None,
    }

    new_store = _copy(store)
    for cand in new_store["candidates"]:
        if cand.get("candidate_id") == candidate_id:
            cand["adopted"] = True
    new_store["accepted_versions"].append(version)
    new_store["current_accepted_version_id"] = version["version_id"]
    return new_store, version


# -- restore --------------------------------------------------------------


def restore_version(
    store: Dict[str, Any],
    version_id: str,
    now: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Re-point the Accepted Version to a prior accepted snapshot.

    The Working Document (its head revision and history) and every non-adopted
    candidate are never touched, so newer unsaved or unadopted work is preserved.
    Restoring the already-current version is a no-op; otherwise a new restore
    record (whose ``restore_of`` names the target) is appended so the accepted
    history stays monotonic and the pointer never points at a half-written record.

    Returns ``(new_store, version)`` on success (the current version on a no-op),
    ``(None, reason)`` when the target does not exist.
    """
    version = _accepted(store, version_id)
    if version is None:
        return None, REASON_VERSION_NOT_FOUND

    current = store.get("current_accepted_version_id")
    if version_id == current:
        return _copy(store), version

    restored: Dict[str, Any] = {
        "record_type": "accepted_version",
        "version_id": new_version_id(),
        "candidate_id": version.get("candidate_id"),
        "document_revision_id": version.get("document_revision_id"),
        "document_fingerprint": version.get("document_fingerprint"),
        "package_id": version.get("package_id"),
        "package_fingerprint": version.get("package_fingerprint"),
        "runtime_identity": version.get("runtime_identity"),
        "validation_identity": version.get("validation_identity"),
        "predecessor_id": current,
        "accepted_at": now,
        "restore_of": version_id,
    }

    new_store = _copy(store)
    new_store["accepted_versions"].append(restored)
    new_store["current_accepted_version_id"] = restored["version_id"]
    return new_store, restored


# -- state assembly -------------------------------------------------------


def document_state(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return the reopenable document state: head revision, candidate, accepted.

    The head revision carries its full ``content`` so a client can repopulate the
    editor. The most recent candidate and the current accepted version are
    surfaced with their identities distinct — never merged or conflated.
    """
    head_rev = _revision(store, store.get("head_revision_id"))
    head_number = (
        head_rev.get("revision_number") if head_rev is not None else 0
    )
    candidates = [c for c in store.get("candidates", []) if isinstance(c, dict)]
    latest_candidate = candidates[-1] if candidates else None
    current = _accepted(store, store.get("current_accepted_version_id"))
    return {
        "document": {
            "document_id": store.get("document_id"),
            "name": store.get("name"),
            "kind": store.get("kind"),
            "created_at": store.get("created_at"),
            "head_revision_number": head_number,
            "revision_count": len(store.get("revisions", [])),
        },
        "head_revision": head_rev,
        "candidate": latest_candidate,
        "accepted": current,
        "current_accepted_version_id": store.get("current_accepted_version_id"),
        "versions": [v for v in store.get("accepted_versions", []) if isinstance(v, dict)],
    }


def candidate_state(store: Dict[str, Any], candidate_id: str) -> Optional[Dict[str, Any]]:
    """Return the candidate plus its current/adopted status, or ``None``.

    ``current`` is True only while the candidate's document revision is still the
    head and its fingerprint matches the head's content; ``adopted`` is the
    recorded adoption flag.
    """
    candidate = _candidate(store, candidate_id)
    if candidate is None:
        return None
    head = store.get("head_revision_id")
    head_rev = _revision(store, head)
    current = (
        candidate.get("document_revision_id") == head
        and head_rev is not None
        and candidate.get("document_fingerprint") == head_rev.get("content_fingerprint")
        and candidate.get("accepted_predecessor_id") == store.get("current_accepted_version_id")
    )
    result = dict(candidate)
    result["current"] = current
    return result


__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "DOCUMENT_GENERATOR",
    "KIND_MARKDOWN",
    "KIND_TEXT",
    "KINDS",
    "MAX_DOCUMENT_NAME_CHARS",
    "ORIGIN_USER_SAVED",
    "SOURCE_DETERMINISTIC_FIXTURE",
    "CANDIDATE_LIMITATION",
    "VALIDATION_IDENTITY",
    "REASON_NOT_MAPPING",
    "REASON_MISSING_VERSION",
    "REASON_INVALID_VERSION",
    "REASON_FUTURE_VERSION",
    "REASON_NOT_MIGRATABLE",
    "REASON_CONTENT_INVALID",
    "REASON_STALE",
    "REASON_NO_REVISION",
    "REASON_CANDIDATE_NOT_FOUND",
    "REASON_CANDIDATE_STALE",
    "REASON_ALREADY_ADOPTED",
    "REASON_CORRUPT",
    "REASON_EVIDENCE_FAILED",
    "REASON_BASELINE_MISMATCH",
    "REASON_VERSION_NOT_FOUND",
    "MIGRATIONS",
    "sha256_hex",
    "dumps",
    "migrate_document",
    "new_document_id",
    "new_revision_id",
    "new_version_id",
    "kind_for_name",
    "valid_name",
    "new_document_store",
    "save_revision",
    "build_candidate",
    "adopt_candidate",
    "restore_version",
    "document_state",
    "candidate_state",
]
