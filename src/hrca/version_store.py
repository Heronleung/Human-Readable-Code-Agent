"""Deterministic document/version persistence (P4.4).

The document/version store is persisted **outside** the selected repository, in
the same per-user app-data directory the Twin uses, but under a distinct
``documents/`` namespace keyed by the opaque document id. This module is the
*only* code allowed to read, write, parse or enumerate document storage; the Qt
client never imports it (enforced by :mod:`tests.test_architecture`).

Persistence rules mirror :mod:`hrca.twin_store` exactly:

* **Atomic write** — the complete store is written to a temporary file in the
  same directory, flushed and ``fsync``-ed, then atomically ``os.replace``-ed
  over the live store. A failed write leaves the previous valid store intact.
* **Fail-closed load** — a load that cannot be read, parsed or migrated returns
  ``(None, reason)`` and never overwrites the on-disk store. A future/unknown
  ``schema_version`` is rejected through :func:`hrca.document.migrate_document`.
* **Per-document isolation** — each document's store lives under its own
  directory named by the opaque ``document_id``, so two documents never collide,
  the selected repository is never written to, and the legacy Twin/Draft stores
  (``twin.json`` / ``draft.json``) are never touched or transformed.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import document

# Directory name (under the store base) that owns all document stores, kept
# distinct from the Twin store namespace so legacy Twin/Draft data is never
# confused with, or transformed into, a document.
_DOCUMENTS_DIR = "documents"

# File name of the canonical document store within a document directory.
DOCUMENT_STORE_FILENAME = "document.json"

# Prefix/suffix for temporary files during the atomic write; kept in the same
# directory as the target so ``os.replace`` is atomic on the same filesystem.
_TMP_PREFIX = ".document-"
_TMP_SUFFIX = ".tmp"


def documents_dir(base_dir: str) -> str:
    """Return the directory that owns every document store under ``base_dir``."""
    return os.path.join(base_dir, _DOCUMENTS_DIR)


def _namespace(document_id: str) -> str:
    """Return a filesystem-safe directory name for ``document_id``.

    ``document_id`` is ``doc:<hex>``; the ``doc:`` prefix is a record value and
    is not a valid directory name on Windows, so it is folded to an underscore.
    """
    return document_id.replace(":", "_").replace("/", "_").replace("\\", "_")


def document_store_path(base_dir: str, document_id: str) -> str:
    """Return the absolute path of the document store for ``document_id``."""
    return os.path.join(
        documents_dir(base_dir), _namespace(document_id), DOCUMENT_STORE_FILENAME
    )


def load(base_dir: str, document_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fail-closed load of a document's store.

    Returns ``(store, error)`` where exactly one of ``store`` / ``error`` is
    ``None`` (an absent store yields ``(None, None)``). Any read, parse or
    migration failure returns ``(None, reason)`` and leaves the on-disk store
    untouched.
    """
    path = document_store_path(base_dir, document_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read document store: {exc}"

    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return None, f"document store is not valid JSON: {exc}"

    store, err = document.migrate_document(raw)
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

    The complete payload is written to a temporary file in ``dirpath``, flushed
    and ``fsync``-ed, then atomically ``os.replace``-ed over ``path``. On any
    failure the previous file is retained and the temporary file is removed.
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


def save(base_dir: str, document_id: str, store: dict) -> Optional[str]:
    """Atomically persist ``store``; returns a reason on failure or ``None``.

    Serialization and atomic-write semantics match :func:`hrca.twin_store.save`
    exactly; a failed write retains the previous valid store.
    """
    path = document_store_path(base_dir, document_id)
    dirpath = os.path.dirname(path)
    err = _ensure_dir(dirpath)
    if err is not None:
        return err
    return _atomic_write(
        dirpath, path, document.dumps(store).encode("utf-8"), "document store"
    )


def list_documents(base_dir: str) -> List[Dict[str, Any]]:
    """Return a bounded summary of every readable document store under ``base_dir``.

    Each summary carries the document id, name, kind, head revision number,
    revision count, head fingerprint, candidate presence and current accepted
    version id. A directory whose store cannot be read/migrated is skipped (never
    treated as a valid document), so a corrupt record can never masquerade as a
    document. Results are sorted by name then id for determinism.
    """
    root = documents_dir(base_dir)
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []

    summaries: List[Dict[str, Any]] = []
    for entry in entries:
        dirpath = os.path.join(root, entry)
        if not os.path.isdir(dirpath):
            continue
        path = os.path.join(dirpath, DOCUMENT_STORE_FILENAME)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.loads(fh.read())
        except (OSError, ValueError):
            continue
        store, err = document.migrate_document(raw)
        if err is not None or store is None:
            continue
        head_rev = next(
            (
                r
                for r in store.get("revisions", [])
                if isinstance(r, dict) and r.get("revision_id") == store.get("head_revision_id")
            ),
            None,
        )
        summaries.append(
            {
                "document_id": store.get("document_id"),
                "name": store.get("name"),
                "kind": store.get("kind"),
                "head_revision_number": (
                    head_rev.get("revision_number") if head_rev is not None else 0
                ),
                "revision_count": len(store.get("revisions", [])),
                "head_fingerprint": (
                    head_rev.get("content_fingerprint") if head_rev is not None else None
                ),
                "has_candidate": bool(store.get("candidates")),
                "current_accepted_version_id": store.get("current_accepted_version_id"),
            }
        )

    summaries.sort(key=lambda s: (str(s.get("name") or ""), str(s.get("document_id") or "")))
    return summaries


__all__ = [
    "DOCUMENT_STORE_FILENAME",
    "documents_dir",
    "document_store_path",
    "load",
    "save",
    "list_documents",
]
