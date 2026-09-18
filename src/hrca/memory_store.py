"""Deterministic Developer Memory persistence (M4.1).

The Developer Memory store is persisted **outside** the selected repository, in
the same per-user app-data directory the Twin and the Working Document use, but
under a distinct ``memory/`` namespace keyed by the opaque run id. This module
is the *only* code allowed to read, write, parse or enumerate memory storage;
the Qt client never imports it (enforced by :mod:`tests.test_architecture`).

Persistence rules mirror :mod:`hrca.twin_store` and :mod:`hrca.version_store`
exactly:

* **Atomic write** — the complete store is written to a temporary file in the
  same directory, flushed and ``fsync``-ed, then atomically ``os.replace``-ed
  over the live store. A failed or interrupted write leaves the previous valid
  store intact and readable.
* **Fail-closed load** — a load that cannot be read, parsed or migrated returns
  ``(None, reason)`` and never overwrites the on-disk store. A future/unknown
  ``schema_version`` and a migration that would change replay meaning are both
  rejected through :func:`hrca.memory.migrate_memory`.
* **Per-run isolation** — each run's store lives under its own directory named
  by the opaque run id, so two runs never collide, the selected repository is
  never written to, and no other store namespace is ever touched.

Only normalized, already-redacted records reach this module: the privacy policy
in :mod:`hrca.memory` is applied during normalization, before any record exists
for this module to write.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import memory

# Directory name (under the store base) that owns all Developer Memory stores,
# kept distinct from the Twin and Document namespaces so no store is ever
# confused with, or transformed into, another.
_MEMORY_DIR = "memory"

# File name of the canonical run store within a run directory.
RUN_STORE_FILENAME = "run.json"

# Prefix/suffix for temporary files during the atomic write; kept in the same
# directory as the target so ``os.replace`` is atomic on the same filesystem.
_TMP_PREFIX = ".memory-"
_TMP_SUFFIX = ".tmp"


def memory_dir(base_dir: str) -> str:
    """Return the directory that owns every memory store under ``base_dir``."""
    return os.path.join(base_dir, _MEMORY_DIR)


def _namespace(run_id: str) -> str:
    """Return a filesystem-safe directory name for ``run_id``.

    ``run_id`` contains ``:`` separators, which are not valid in a Windows
    directory name, so they are folded to an underscore.
    """
    return run_id.replace(":", "_").replace("/", "_").replace("\\", "_")


def store_namespace(run_id: str) -> str:
    """Return the filesystem-safe namespace the store uses for ``run_id``.

    Public so a caller that has to name a store outside this module — an export
    or backup entry, for instance — uses exactly the same spelling rather than
    re-deriving one that could drift.
    """
    return _namespace(run_id)


def run_store_path(base_dir: str, run_id: str) -> str:
    """Return the absolute path of the memory store for ``run_id``."""
    return os.path.join(memory_dir(base_dir), _namespace(run_id), RUN_STORE_FILENAME)


def load(base_dir: str, run_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Fail-closed load of a run's memory store.

    Returns ``(store, error)`` where exactly one of ``store`` / ``error`` is
    ``None`` (an absent store yields ``(None, None)``). Any read, parse or
    migration failure returns ``(None, reason)`` and leaves the on-disk store
    untouched.
    """
    path = run_store_path(base_dir, run_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not read memory store: {exc}"

    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return None, f"memory store is not valid JSON: {exc}"

    store, err = memory.migrate_memory(raw)
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


def save(base_dir: str, run_id: str, store: dict) -> Optional[str]:
    """Atomically persist ``store``; returns a reason on failure or ``None``.

    Serialization and atomic-write semantics match :func:`hrca.twin_store.save`
    exactly; a failed write retains the previous valid store.
    """
    path = run_store_path(base_dir, run_id)
    dirpath = os.path.dirname(path)
    err = _ensure_dir(dirpath)
    if err is not None:
        return err
    return _atomic_write(dirpath, path, memory.dumps(store).encode("utf-8"), "memory store")


def list_runs(base_dir: str) -> List[Dict[str, Any]]:
    """Return a bounded, deterministic summary of every readable run store.

    Each summary carries the run identity, adapter, session, reported state,
    ingest sequence, event count and evidence count. A directory whose store
    cannot be read or migrated is skipped (never treated as a valid run), so a
    corrupt record can never masquerade as a run. Results are sorted by run id.
    """
    root = memory_dir(base_dir)
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []

    summaries: List[Dict[str, Any]] = []
    for entry in entries:
        dirpath = os.path.join(root, entry)
        if not os.path.isdir(dirpath):
            continue
        path = os.path.join(dirpath, RUN_STORE_FILENAME)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.loads(fh.read())
        except (OSError, ValueError):
            continue
        store, err = memory.migrate_memory(raw)
        if err is not None or store is None:
            continue
        run = store.get("agent_run")
        if not isinstance(run, dict):
            continue
        summaries.append(
            {
                "run_id": run.get("id"),
                "adapter": run.get("adapter"),
                "session_id": run.get("session_id"),
                "state": run.get("state"),
                "ingest_sequence": run.get("ingest_sequence"),
                "event_count": len(store.get("events", [])),
                "evidence_count": len(store.get("evidence", [])),
            }
        )

    summaries.sort(key=lambda s: str(s.get("run_id") or ""))
    return summaries


__all__ = [
    "RUN_STORE_FILENAME",
    "memory_dir",
    "run_store_path",
    "load",
    "save",
    "list_runs",
]
