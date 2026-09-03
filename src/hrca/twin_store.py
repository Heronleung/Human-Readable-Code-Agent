"""Deterministic Twin persistence (P3.3).

The Twin is persisted **outside** the selected repository, in a per-user
app-data directory keyed by the canonical workspace identifier. This module is
the *only* code allowed to read, write, parse or enumerate Twin storage; the
Qt client never imports it (enforced by :mod:`tests.test_architecture`).

Persistence rules implemented here:

* **Atomic write** — the complete store is written to a temporary file in the
  same directory, flushed and ``fsync``-ed, then atomically ``os.replace``-ed
  over the live store. A failed write leaves the previous valid store intact.
* **Fail-closed load** — a load that cannot be read, parsed or migrated returns
  ``(None, reason)`` and never overwrites the on-disk store. A future/unknown
  ``schema_version`` is rejected through :func:`hrca.twin.migrate_store`.
* **Per-workspace isolation** — each workspace's store lives under its own
  directory named by the deterministic ``workspace_id``, so two workspaces can
  never collide and the selected repository is never written to.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Optional, Tuple

from . import twin, codemap_draft

# App-data directory name (per user), independent of any selected repository.
_APP_DIR_NAME = "human-readable-code-agent"

# File name of the canonical Twin store within a workspace directory.
TWIN_STORE_FILENAME = "twin.json"

# File name of the editable Twin Draft (P3.4) within a workspace directory. The
# draft lives alongside ``twin.json`` under the same per-workspace directory and
# is never written into the selected repository.
DRAFT_STORE_FILENAME = "draft.json"

# Prefix for temporary files during the atomic write; kept in the same
# directory as the target so ``os.replace`` is atomic on the same filesystem.
_TMP_PREFIX = ".twin-"
_TMP_SUFFIX = ".tmp"


def app_data_dir() -> str:
    """Return the per-user app-data directory that owns all Twin storage.

    Uses the platform convention and never consults the selected repository:

    * Windows — ``%LOCALAPPDATA%`` (falling back to the home directory);
    * POSIX  — ``$XDG_DATA_HOME`` (falling back to ``~/.local/share``).
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "HumanReadableCodeAgent")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, _APP_DIR_NAME)


def _namespace(workspace_id: str) -> str:
    """Return a filesystem-safe directory name for ``workspace_id``.

    ``workspace_id`` is ``ws:<hex>``; the ``ws:`` prefix is a record value and
    is not a valid directory name on Windows, so it is folded to an underscore.
    """
    return workspace_id.replace(":", "_").replace("/", "_").replace("\\", "_")


def workspace_store_path(base_dir: str, workspace_id: str) -> str:
    """Return the absolute path of the canonical Twin store for a workspace."""
    return os.path.join(base_dir, _namespace(workspace_id), TWIN_STORE_FILENAME)


def workspace_draft_path(base_dir: str, workspace_id: str) -> str:
    """Return the absolute path of the editable Twin Draft for a workspace."""
    return os.path.join(base_dir, _namespace(workspace_id), DRAFT_STORE_FILENAME)


def load(base_dir: str, workspace_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fail-closed load of a workspace's Twin store.

    Returns ``(store, error)`` where exactly one of ``store`` / ``error`` is
    ``None`` (an absent store yields ``(None, None)``). Any read, parse or
    migration failure returns ``(None, reason)`` and leaves the on-disk store
    untouched.
    """
    path = workspace_store_path(base_dir, workspace_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read Twin store: {exc}"

    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return None, f"Twin store is not valid JSON: {exc}"

    store, err = twin.migrate_store(raw)
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


def save(base_dir: str, workspace_id: str, store: dict) -> Optional[str]:
    """Atomically persist ``store``; returns a reason on failure or ``None``.

    The complete store is serialized deterministically, written to a temporary
    file, flushed and ``fsync``-ed, then atomically replaced over the live
    store. On any failure the previous valid store is retained and the
    temporary file is removed.
    """
    path = workspace_store_path(base_dir, workspace_id)
    dirpath = os.path.dirname(path)
    err = _ensure_dir(dirpath)
    if err is not None:
        return err
    return _atomic_write(dirpath, path, twin.dumps(store).encode("utf-8"), "Twin store")


def load_draft(base_dir: str, workspace_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fail-closed load of a workspace's Twin Draft.

    Returns ``(draft, error)`` where exactly one of ``draft`` / ``error`` is
    ``None`` (an absent draft yields ``(None, None)``). Any read, parse or
    migration failure returns ``(None, reason)`` and leaves the on-disk draft
    untouched. The same future/unknown-version fail-closed rule as the Twin
    store applies via :func:`hrca.codemap_draft.migrate_draft`.
    """
    path = workspace_draft_path(base_dir, workspace_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read Twin Draft: {exc}"

    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return None, f"Twin Draft is not valid JSON: {exc}"

    draft, err = codemap_draft.migrate_draft(raw)
    if err is not None:
        return None, err
    return draft, None


def save_draft(base_dir: str, workspace_id: str, draft: dict) -> Optional[str]:
    """Atomically persist ``draft``; returns a reason on failure or ``None``.

    Serialization and atomic-write semantics match :func:`save` exactly; a
    failed write retains the previous valid draft.
    """
    path = workspace_draft_path(base_dir, workspace_id)
    dirpath = os.path.dirname(path)
    err = _ensure_dir(dirpath)
    if err is not None:
        return err
    return _atomic_write(dirpath, path, codemap_draft.dumps(draft).encode("utf-8"), "Twin Draft")


def discard_draft(base_dir: str, workspace_id: str) -> Optional[str]:
    """Remove the persisted Twin Draft; returns a reason on failure or ``None``.

    An absent draft is an idempotent success (``None``), so discarding a
    non-existent draft never errors.
    """
    path = workspace_draft_path(base_dir, workspace_id)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not discard Twin Draft: {exc}"
    return None


__all__ = [
    "TWIN_STORE_FILENAME",
    "DRAFT_STORE_FILENAME",
    "app_data_dir",
    "workspace_store_path",
    "workspace_draft_path",
    "load",
    "save",
    "load_draft",
    "save_draft",
    "discard_draft",
]
