"""Deterministic app-owned library persistence (P4.6).

The library (folder tree + document references) is persisted beside the Working
Document stores, in the same per-user app-data directory, under a distinct
``library/`` namespace. This module is the *only* code allowed to read, write,
parse or enumerate library storage; the Qt client never imports it (enforced by
:mod:`tests.test_architecture`).

Persistence rules mirror :mod:`hrca.version_store` exactly (atomic write,
fail-closed load, per-store isolation), plus one addition the library needs:
**idempotent, crash-safe migration** of the pre-P4.6 flat document set. On every
load the library is reconciled against the Working Document stores on disk; any
document that exists on disk but has no library reference is *adopted* into the
root without touching its id, content, revisions, fingerprints, candidates,
accepted versions or preview bindings. Legacy duplicate names are preserved as
they are — the migration never deletes, merges or renames anything, and it never
overwrites an existing reference.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import document, library, version_store

# Directory name (under the store base) that owns the library store, kept
# distinct from the ``documents/`` and workspace (Twin) namespaces.
_LIBRARY_DIR = "library"

# File name of the canonical library store.
LIBRARY_STORE_FILENAME = "library.json"

# Prefix/suffix for temporary files during the atomic write.
_TMP_PREFIX = ".library-"
_TMP_SUFFIX = ".tmp"


def library_dir(base_dir: str) -> str:
    """Return the directory that owns the library store under ``base_dir``."""
    return os.path.join(base_dir, _LIBRARY_DIR)


def library_store_path(base_dir: str) -> str:
    """Return the absolute path of the library store."""
    return os.path.join(library_dir(base_dir), LIBRARY_STORE_FILENAME)


def load(base_dir: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fail-closed load of the library store.

    Returns ``(store, error)`` where exactly one of ``store`` / ``error`` is
    ``None`` (an absent store yields ``(None, None)``). Any read, parse or
    migration failure returns ``(None, reason)`` and leaves the on-disk store
    untouched.
    """
    path = library_store_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read library store: {exc}"

    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return None, f"library store is not valid JSON: {exc}"

    store, err = library.migrate_library(raw)
    if err is not None:
        return None, err
    return store, None


def _ensure_dir(dirpath: str) -> Optional[str]:
    """Create ``dirpath`` if needed; return a reason on failure or ``None``."""
    try:
        os.makedirs(dirpath, exist_ok=True)
    except OSError as exc:
        return f"could not create storage directory: {exc}"
    return None


def _atomic_write(dirpath: str, path: str, data: bytes, label: str) -> Optional[str]:
    """Atomically write ``data`` to ``path``; return a reason on failure or ``None``.

    Mirrors :func:`hrca.version_store._atomic_write`: write to a temporary file
    in ``dirpath``, flush and ``fsync``, then atomically ``os.replace`` over
    ``path``. On failure the previous file is retained.
    """
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX)
    except OSError as exc:
        return f"could not create {label} temporary file: {exc}"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"could not write {label}: {exc}"
    return None


def save(base_dir: str, store: dict) -> Optional[str]:
    """Atomically persist ``store``; returns a reason on failure or ``None``."""
    path = library_store_path(base_dir)
    dirpath = os.path.dirname(path)
    err = _ensure_dir(dirpath)
    if err is not None:
        return err
    return _atomic_write(
        dirpath, path, library.dumps(store).encode("utf-8"), "library store"
    )


def adopt_documents(base_dir: str, store: dict, now: str) -> Tuple[dict, int]:
    """Adopt any Working Document on disk that has no library reference.

    Every document id already referenced is left exactly as it is; a document
    id present on disk but absent from the library is appended at the root with
    ``trashed: False``. Returns ``(store, adopted_count)``. This is idempotent
    (a re-run adds nothing) and never touches the Working Document stores, so it
    cannot change a document id, name, revision, fingerprint, candidate,
    accepted version or preview binding.
    """
    refs = {
        ref.get("document_id")
        for ref in store.get("documents", [])
        if isinstance(ref, dict) and ref.get("document_id")
    }
    adopted = 0
    for summary in version_store.list_documents(base_dir):
        document_id = summary.get("document_id")
        if not document_id or document_id in refs:
            continue
        store["documents"].append(library.new_document_record(document_id, None))
        adopted += 1
    if adopted and not store.get("created_at"):
        store["created_at"] = now
    return store, adopted


def ensure_library(base_dir: str, now: str) -> Tuple[dict, int]:
    """Load (or create) the library and reconcile it with on-disk documents.

    Returns ``(library, adopted_count)``. When the library is absent it is
    created fresh; any pre-P4.6 flat documents are then adopted at the root. If
    the reconciliation changed the store it is persisted atomically (so a crash
    before this point simply re-runs the same idempotent adoption next launch).
    A store that cannot be read/migrated is *not* overwritten: the caller gets a
    fresh empty library only when no store exists, otherwise the load error is
    surfaced by the caller via :func:`load`.
    """
    store, err = load(base_dir)
    if store is None and err is None:
        store = library.new_library_store(now)
    if store is None:
        # A corrupt/future store must never be silently replaced.
        return store, 0
    store, adopted = adopt_documents(base_dir, store, now)
    if adopted:
        save(base_dir, store)
    return store, adopted


def get_tree(base_dir: str, now: str) -> Dict[str, Any]:
    """Return the joined, deterministic app-owned tree for the client.

    The result is ``{"folders": [...], "documents": [...], "migration": {...}}``.
    Folders come straight from the library; document entries are the library's
    references joined with each Working Document's name/kind/revision summary
    from :func:`hrca.version_store.list_documents`. Every list is sorted
    (folders by name then id, documents by name then id) so identical rescans
    are byte-identical. Only documents that still exist on disk are listed; a
    dangling reference is dropped rather than shown.
    """
    store, adopted = ensure_library(base_dir, now)
    if store is None:
        return {"folders": [], "documents": [], "migration": {"adopted": 0}}

    summaries = {s["document_id"]: s for s in version_store.list_documents(base_dir)}

    folders = sorted(
        [
            {
                "folder_id": f.get("folder_id"),
                "name": f.get("name"),
                "parent_id": f.get("parent_id"),
                "trashed": bool(f.get("trashed")),
                "created_at": f.get("created_at"),
            }
            for f in store.get("folders", [])
            if isinstance(f, dict) and f.get("folder_id")
        ],
        key=lambda f: (str(f.get("name") or ""), str(f.get("folder_id") or "")),
    )

    documents: List[Dict[str, Any]] = []
    for ref in store.get("documents", []):
        if not isinstance(ref, dict):
            continue
        document_id = ref.get("document_id")
        summary = summaries.get(document_id)
        if summary is None:
            continue
        documents.append(
            {
                "document_id": document_id,
                "name": summary.get("name"),
                "kind": summary.get("kind"),
                "parent_id": ref.get("parent_id"),
                "trashed": bool(ref.get("trashed")),
                "head_revision_number": summary.get("head_revision_number", 0),
                "revision_count": summary.get("revision_count", 0),
                "has_candidate": summary.get("has_candidate", False),
                "current_accepted_version_id": summary.get("current_accepted_version_id"),
            }
        )
    documents.sort(
        key=lambda d: (str(d.get("name") or ""), str(d.get("document_id") or ""))
    )

    return {
        "folders": folders,
        "documents": documents,
        "migration": {"adopted": adopted},
    }


__all__ = [
    "LIBRARY_STORE_FILENAME",
    "library_dir",
    "library_store_path",
    "load",
    "save",
    "adopt_documents",
    "ensure_library",
    "get_tree",
]
