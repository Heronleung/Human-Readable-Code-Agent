"""App-owned folder/document tree domain (P4.6).

The smallest versioned metadata model needed for an application-owned document
tree. It is deliberately pure in the same sense as :mod:`hrca.document` and
:mod:`hrca.twin`: Qt-free, stdlib-only, no filesystem, network, credential,
command, Git, repository-write, provider or runner access. Persistence belongs
to :mod:`hrca.library_store`; the boundary supplies timestamps and orchestrates
the cross-store name join.

The library records *tree structure only*:

* a **folder** carries an opaque stable id (``dir:<hex>``), a normalized display
  name, a parent folder id (``None`` for root), a trashed flag and a creation
  timestamp;
* a **document reference** carries an opaque stable id (``doc:<hex>``) that
  points at the Working Document store, a parent folder id and a trashed flag.

The document's *name* lives in its own Working Document store (never here), so a
rename changes exactly one place and a move changes exactly one place. Identity
is never derived from a name, list order, path or tree position: every node is
keyed by its opaque id, and a rename/move/trash/restore never rewrites a
document id, revision id, candidate/accepted record, package identity or
historic evidence binding.

Sibling-name uniqueness is *folders-and-documents together*, compared
case-insensitively after trimming, and is enforced at the boundary by joining
this structure with the document names. Because document names are external, the
mutation functions that need a sibling collision check take an explicit
``sibling_keys`` set (the normalized name-keys of the target parent's live
children, excluding the node being changed) — the caller computes it and the
domain only enforces it, so every invariant remains unit-testable without a
filesystem.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from . import document

LIBRARY_SCHEMA_VERSION = "1.0.0"
LIBRARY_GENERATOR = "hrca-library"

# Bounded input limits (code-owned; never from user text).
MAX_FOLDER_NAME_CHARS = 120

# Record-type tags carried by every stored record (mirrors hrca.document).
RECORD_TYPE_FOLDER = "folder"
RECORD_TYPE_DOCUMENT_REF = "document_ref"

# Historical migrations (none before 1.0.0). Kept as a registry so later phases
# can add migrations without changing the load path.
MIGRATIONS: Dict[str, Any] = {}

# Bounded load reasons (mirror hrca.document.migrate_document).
REASON_NOT_MAPPING = "store is not a mapping"
REASON_MISSING_VERSION = "missing schema_version"
REASON_INVALID_VERSION = "invalid schema_version"
REASON_FUTURE_VERSION = "schema_version is newer than supported"
REASON_NOT_MIGRATABLE = "schema_version is not migratable"

# Bounded operation reasons. The boundary maps these to fixed contract codes;
# none interpolates caller text.
REASON_FOLDER_NOT_FOUND = "folder not found"
REASON_ITEM_NOT_FOUND = "item not found"
REASON_NAME_INVALID = "name invalid"
REASON_NAME_IN_USE = "name in use"
REASON_INVALID_PARENT = "invalid parent"
REASON_CYCLIC_MOVE = "cyclic move"
REASON_PARENT_TRASHED = "parent trashed"
REASON_ITEM_TRASHED = "item trashed"
REASON_NOT_TRASHED = "not trashed"
REASON_RESTORE_COLLISION = "restore collision"


def new_folder_id() -> str:
    """Return a fresh opaque folder id (``dir:<32 hex>``)."""
    return "dir:" + uuid.uuid4().hex


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return document.dumps(obj)


def _copy(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of ``store`` (so callers never mutate in place)."""
    return json.loads(dumps(store))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


def migrate_library(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and migrate a raw library store to the current schema version.

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
        current = _version_tuple(LIBRARY_SCHEMA_VERSION)
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


def new_library_store(now: str) -> Dict[str, Any]:
    """Return a fresh, empty library store (no folders, no document refs)."""
    return {
        "schema_version": LIBRARY_SCHEMA_VERSION,
        "generator": LIBRARY_GENERATOR,
        "folders": [],
        "documents": [],
        "created_at": now,
    }


# -- folder names ---------------------------------------------------------


def normalize_folder_name(name: Any) -> Optional[str]:
    """Return the trimmed, valid folder name, or ``None`` when invalid.

    A valid folder name is non-blank, at most :data:`MAX_FOLDER_NAME_CHARS`
    after trimming, contains no path separator or traversal component, and has a
    base name that is not a reserved Windows device name. Unlike a document name
    it requires no ``.md``/``.txt`` suffix. The original spelling/casing is
    preserved; normalization only trims permitted surrounding whitespace.
    """
    if not isinstance(name, str):
        return None
    trimmed = name.strip()
    if not trimmed:
        return None
    if len(trimmed) > MAX_FOLDER_NAME_CHARS:
        return None
    if "/" in trimmed or "\\" in trimmed:
        return None
    if trimmed in (".", ".."):
        return None
    if trimmed.rsplit(".", 1)[0].lower() in document._WINDOWS_RESERVED_BASE_NAMES:
        return None
    return trimmed


def folder_name_key(name: Any) -> Optional[str]:
    """Return the case-insensitive comparison key for a folder name, or ``None``."""
    return document.name_key(name)


# -- store construction / lookups ------------------------------------------


def new_folder_record(folder_id: str, name: str, parent_id: Optional[str], now: str) -> Dict[str, Any]:
    """Return one folder record."""
    return {
        "record_type": RECORD_TYPE_FOLDER,
        "folder_id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "trashed": False,
        "created_at": now,
    }


def new_document_record(document_id: str, parent_id: Optional[str]) -> Dict[str, Any]:
    """Return one document reference record (no name — the name lives in the
    Working Document store)."""
    return {
        "record_type": RECORD_TYPE_DOCUMENT_REF,
        "document_id": document_id,
        "parent_id": parent_id,
        "trashed": False,
    }


def _folder(store: Dict[str, Any], folder_id: str) -> Optional[Dict[str, Any]]:
    """Return the folder record with ``folder_id``, or ``None``."""
    for folder in store.get("folders", []):
        if isinstance(folder, dict) and folder.get("folder_id") == folder_id:
            return folder
    return None


def _document_ref(store: Dict[str, Any], document_id: str) -> Optional[Dict[str, Any]]:
    """Return the document reference with ``document_id``, or ``None``."""
    for ref in store.get("documents", []):
        if isinstance(ref, dict) and ref.get("document_id") == document_id:
            return ref
    return None


# -- parent / trash / cycle helpers ----------------------------------------


def parent_error(store: Dict[str, Any], parent_id: Optional[str]) -> Optional[str]:
    """Return a bounded reason when ``parent_id`` is not a valid live parent.

    ``None`` is the root and is always valid; any other parent must be an
    existing folder that is not itself (or via any ancestor) trashed.
    """
    if parent_id is None:
        return None
    if not isinstance(parent_id, str) or not parent_id:
        return REASON_INVALID_PARENT
    if _folder(store, parent_id) is None:
        return REASON_FOLDER_NOT_FOUND
    if folder_effectively_trashed(store, parent_id):
        return REASON_PARENT_TRASHED
    return None


# Internal alias kept for the module's own mutation functions.
_parent_error = parent_error


def folder_effectively_trashed(store: Dict[str, Any], folder_id: str) -> bool:
    """True when ``folder_id`` or any ancestor folder is trashed."""
    seen: Set[str] = set()
    current: Optional[str] = folder_id
    while current is not None and current not in seen:
        seen.add(current)
        folder = _folder(store, current)
        if folder is None:
            return False
        if folder.get("trashed"):
            return True
        current = folder.get("parent_id")
    return False


def is_descendant(store: Dict[str, Any], ancestor_id: str, node_id: Optional[str]) -> bool:
    """True when ``node_id`` is ``ancestor_id`` or any folder below it."""
    if node_id is None:
        return False
    seen: Set[str] = set()
    current: Optional[str] = node_id
    while current is not None and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        folder = _folder(store, current)
        current = folder.get("parent_id") if folder is not None else None
    return False


def restore_parent(store: Dict[str, Any], item: Dict[str, Any]) -> Optional[str]:
    """Return the parent an item should be restored to.

    An item whose recorded parent is missing or trashed is restored to the root
    (``None``) instead — a trashed/missing parent is not a safe destination.
    """
    parent_id = item.get("parent_id")
    if parent_id is None:
        return None
    parent = _folder(store, parent_id)
    if parent is None or parent.get("trashed"):
        return None
    return parent_id


def sibling_name_keys(
    folders: List[Dict[str, Any]],
    documents: List[Dict[str, Any]],
    parent_id: Optional[str],
    exclude_id: Optional[str] = None,
) -> Set[str]:
    """Return the normalized name-keys of a parent's live children.

    ``folders`` are library folder records; ``documents`` are document-reference
    records enriched with a transient ``name`` (the caller joins the Working
    Document name). Trashed children are excluded, and ``exclude_id`` (a folder
    id or document id) is excluded so a rename/move of the node itself never
    collides with its own current name.
    """
    keys: Set[str] = set()
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        if folder.get("trashed"):
            continue
        if folder.get("parent_id") != parent_id:
            continue
        if exclude_id is not None and folder.get("folder_id") == exclude_id:
            continue
        key = folder_name_key(folder.get("name"))
        if key:
            keys.add(key)
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        if doc.get("trashed"):
            continue
        if doc.get("parent_id") != parent_id:
            continue
        if exclude_id is not None and doc.get("document_id") == exclude_id:
            continue
        key = document.name_key(doc.get("name"))
        if key:
            keys.add(key)
    return keys


# -- folder mutations ------------------------------------------------------


def add_folder(
    store: Dict[str, Any],
    folder_id: str,
    name: str,
    parent_id: Optional[str],
    now: str,
    sibling_keys: Set[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Append a folder, enforcing a valid name, parent and sibling uniqueness.

    Returns ``(new_store, None)`` on success, ``(None, reason)`` on refusal.
    """
    normalized = normalize_folder_name(name)
    if normalized is None:
        return None, REASON_NAME_INVALID
    parent_err = _parent_error(store, parent_id)
    if parent_err is not None:
        return None, parent_err
    key = folder_name_key(normalized)
    if key in sibling_keys:
        return None, REASON_NAME_IN_USE
    new_store = _copy(store)
    new_store["folders"].append(new_folder_record(folder_id, normalized, parent_id, now))
    return new_store, None


def rename_folder(
    store: Dict[str, Any], folder_id: str, name: str, sibling_keys: Set[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Change a folder's display name only; identity and children are untouched."""
    folder = _folder(store, folder_id)
    if folder is None:
        return None, REASON_ITEM_NOT_FOUND
    if folder.get("trashed"):
        return None, REASON_ITEM_TRASHED
    normalized = normalize_folder_name(name)
    if normalized is None:
        return None, REASON_NAME_INVALID
    key = folder_name_key(normalized)
    if key in sibling_keys:
        return None, REASON_NAME_IN_USE
    new_store = _copy(store)
    _folder(new_store, folder_id)["name"] = normalized
    return new_store, None


def move_folder(
    store: Dict[str, Any], folder_id: str, new_parent_id: Optional[str], sibling_keys: Set[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Move a folder under a new parent, rejecting cycles and name collisions.

    A move never changes the folder's name, id or children; only the parent
    relationship changes.
    """
    folder = _folder(store, folder_id)
    if folder is None:
        return None, REASON_ITEM_NOT_FOUND
    if folder.get("trashed"):
        return None, REASON_ITEM_TRASHED
    parent_err = _parent_error(store, new_parent_id)
    if parent_err is not None:
        return None, parent_err
    if new_parent_id == folder_id or is_descendant(store, folder_id, new_parent_id):
        return None, REASON_CYCLIC_MOVE
    key = folder_name_key(folder.get("name"))
    if key in sibling_keys:
        return None, REASON_NAME_IN_USE
    new_store = _copy(store)
    _folder(new_store, folder_id)["parent_id"] = new_parent_id
    return new_store, None


def trash_folder(
    store: Dict[str, Any], folder_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Mark a folder trashed. Idempotent; children keep their own flags and are
    hidden only because their ancestor is trashed, so restoring the folder
    restores the whole subtree."""
    folder = _folder(store, folder_id)
    if folder is None:
        return None, REASON_ITEM_NOT_FOUND
    if folder.get("trashed"):
        return _copy(store), None
    new_store = _copy(store)
    _folder(new_store, folder_id)["trashed"] = True
    return new_store, None


def restore_folder(
    store: Dict[str, Any],
    folder_id: str,
    restore_parent: Optional[str],
    sibling_keys: Set[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Un-trash a folder, reattaching it to ``restore_parent`` (precomputed).

    A name collision at the restore destination is a bounded refusal — live data
    is never overwritten.
    """
    folder = _folder(store, folder_id)
    if folder is None:
        return None, REASON_ITEM_NOT_FOUND
    if not folder.get("trashed"):
        return None, REASON_NOT_TRASHED
    key = folder_name_key(folder.get("name"))
    if key in sibling_keys:
        return None, REASON_RESTORE_COLLISION
    new_store = _copy(store)
    target = _folder(new_store, folder_id)
    target["trashed"] = False
    target["parent_id"] = restore_parent
    return new_store, None


# -- document-reference mutations ------------------------------------------


def add_document(
    store: Dict[str, Any], document_id: str, parent_id: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Append a document reference, enforcing a valid parent (name is external)."""
    parent_err = _parent_error(store, parent_id)
    if parent_err is not None:
        return None, parent_err
    if _document_ref(store, document_id) is not None:
        # Idempotent adoption guard (migration re-runs must not duplicate).
        return _copy(store), None
    new_store = _copy(store)
    new_store["documents"].append(new_document_record(document_id, parent_id))
    return new_store, None


def move_document(
    store: Dict[str, Any],
    document_id: str,
    new_parent_id: Optional[str],
    sibling_keys: Set[str],
    own_name_key: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Move a document under a new parent, rejecting collisions.

    ``own_name_key`` is the document's normalized name-key (the caller joins the
    Working Document name); the sibling set already excludes this document.
    """
    ref = _document_ref(store, document_id)
    if ref is None:
        return None, REASON_ITEM_NOT_FOUND
    if ref.get("trashed"):
        return None, REASON_ITEM_TRASHED
    parent_err = _parent_error(store, new_parent_id)
    if parent_err is not None:
        return None, parent_err
    if own_name_key and own_name_key in sibling_keys:
        return None, REASON_NAME_IN_USE
    new_store = _copy(store)
    _document_ref(new_store, document_id)["parent_id"] = new_parent_id
    return new_store, None


def trash_document(
    store: Dict[str, Any], document_id: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Mark a document reference trashed. Idempotent."""
    ref = _document_ref(store, document_id)
    if ref is None:
        return None, REASON_ITEM_NOT_FOUND
    if ref.get("trashed"):
        return _copy(store), None
    new_store = _copy(store)
    _document_ref(new_store, document_id)["trashed"] = True
    return new_store, None


def restore_document(
    store: Dict[str, Any],
    document_id: str,
    restore_parent: Optional[str],
    sibling_keys: Set[str],
    own_name_key: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Un-trash a document reference, reattaching it to ``restore_parent``.

    A name collision at the destination is a bounded refusal.
    """
    ref = _document_ref(store, document_id)
    if ref is None:
        return None, REASON_ITEM_NOT_FOUND
    if not ref.get("trashed"):
        return None, REASON_NOT_TRASHED
    if own_name_key and own_name_key in sibling_keys:
        return None, REASON_RESTORE_COLLISION
    new_store = _copy(store)
    target = _document_ref(new_store, document_id)
    target["trashed"] = False
    target["parent_id"] = restore_parent
    return new_store, None


__all__ = [
    "LIBRARY_SCHEMA_VERSION",
    "LIBRARY_GENERATOR",
    "MAX_FOLDER_NAME_CHARS",
    "RECORD_TYPE_FOLDER",
    "RECORD_TYPE_DOCUMENT_REF",
    "MIGRATIONS",
    "REASON_NOT_MAPPING",
    "REASON_MISSING_VERSION",
    "REASON_INVALID_VERSION",
    "REASON_FUTURE_VERSION",
    "REASON_NOT_MIGRATABLE",
    "REASON_FOLDER_NOT_FOUND",
    "REASON_ITEM_NOT_FOUND",
    "REASON_NAME_INVALID",
    "REASON_NAME_IN_USE",
    "REASON_INVALID_PARENT",
    "REASON_CYCLIC_MOVE",
    "REASON_PARENT_TRASHED",
    "REASON_ITEM_TRASHED",
    "REASON_NOT_TRASHED",
    "REASON_RESTORE_COLLISION",
    "new_folder_id",
    "dumps",
    "migrate_library",
    "new_library_store",
    "normalize_folder_name",
    "folder_name_key",
    "new_folder_record",
    "new_document_record",
    "parent_error",
    "folder_effectively_trashed",
    "is_descendant",
    "restore_parent",
    "sibling_name_keys",
    "add_folder",
    "rename_folder",
    "move_folder",
    "trash_folder",
    "restore_folder",
    "add_document",
    "move_document",
    "trash_document",
    "restore_document",
]
