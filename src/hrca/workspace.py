"""Read-only workspace filesystem policy (P3.2).

This module is the *boundary-side* authority for the P3.2 workspace/document
surface. It never performs a write, Git operation, command execution, network
access or provider call, and it never resolves a path outside the root the
boundary has accepted. It imports only the standard library and
:mod:`hrca.contract`, so it can never leak the deterministic core into a
client.

It provides:

* the bounded Python-project file filter (``.py`` / ``.pyi`` / ``pyproject.toml`` /
  ``README.md`` / ``README.rst``) and the excluded directory names,
* :func:`resolve_root` — canonicalize and validate a project root,
* :func:`build_tree` — a deterministic, filtered, size-bounded directory tree,
* :func:`read_document` — read one permitted document below the root, with
  traversal / symlink / extension / size checks.

Every failure is raised as a bounded :class:`hrca.contract.ContractError` whose
message is drawn from the fixed catalogue, so no requested path or file content
ever leaks into an error.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from . import contract

# Bounded Python-project filter: a file is included when its basename is one of
# these exact names or its extension is one of these suffixes.
INCLUDED_FILENAMES = frozenset({"pyproject.toml", "README.md", "README.rst"})
INCLUDED_SUFFIXES = frozenset({".py", ".pyi"})

# Directory names the tree walk and document reader never descend into or
# resolve through. These are workspace-level exclusions, not a security claim.
EXCLUDED_DIR_NAMES = frozenset(
    {".git", ".venv", "__pycache__", "node_modules", "build", "dist"}
)


def is_included_file(name: str) -> bool:
    """Return True when ``name`` is a permitted workspace file."""
    return name in INCLUDED_FILENAMES or _suffix(name) in INCLUDED_SUFFIXES


def _suffix(name: str) -> str:
    """Return the lowercase suffix of a filename, or ``""`` when absent."""
    base = os.path.basename(name)
    dot = base.rfind(".")
    if dot <= 0:  # hidden files such as ".gitignore" have no usable suffix here
        return ""
    return base[dot:].lower()


def _join(prefix: str, name: str) -> str:
    """Join a relative path prefix and a name with a portable ``/`` separator."""
    return f"{prefix}/{name}" if prefix else name


def resolve_root(path: str) -> str:
    """Canonicalize and validate a project root ``path``.

    Returns the symlink-resolved absolute directory. Raises
    ``path_not_found`` when the path does not exist or is not a directory.
    """
    if not isinstance(path, str) or not path.strip():
        raise contract.ContractError("invalid_request")
    canonical = os.path.realpath(os.path.abspath(path))
    if not os.path.isdir(canonical):
        raise contract.ContractError("path_not_found")
    return canonical


def build_tree(root: str) -> Dict[str, Any]:
    """Return a deterministic, filtered, bounded tree for ``root``.

    The result is ``{"root": root, "truncated": bool, "children": [...]}``.
    Entries are dictionaries of ``name``, ``type`` (``dir`` or ``file``),
    ``path`` (portable, root-relative) and, for files, ``size``; directory
    entries carry a ``children`` list. Within each directory, entries are
    ordered directories-first then by name, so identical rescans are
    byte-identical.
    """
    state = {"count": 0, "truncated": False}
    children = _collect_children(root, "", 0, state)
    return {"root": root, "truncated": state["truncated"], "children": children}


def _collect_children(
    dir_path: str, rel_prefix: str, depth: int, state: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Collect the filtered children of ``dir_path`` (at ``depth`` components).

    Symlinks are skipped (never followed); excluded directories are skipped;
    files outside the filter or above ``contract.MAX_FILE_BYTES`` are skipped;
    the walk stops once ``contract.MAX_TREE_ENTRIES`` entries have been emitted
    (marking ``truncated``). A directory is expanded only while its children
    remain within ``contract.MAX_TREE_DEPTH`` components of the root.
    """
    children: List[Dict[str, Any]] = []
    try:
        with os.scandir(dir_path) as it:
            entries = sorted(
                list(it),
                key=lambda e: (0 if e.is_dir(follow_symlinks=False) else 1, e.name),
            )
    except OSError:
        return children

    for entry in entries:
        if state["count"] >= contract.MAX_TREE_ENTRIES:
            state["truncated"] = True
            break
        name = entry.name
        if entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            if name in EXCLUDED_DIR_NAMES:
                continue
            rel = _join(rel_prefix, name)
            state["count"] += 1
            node: Dict[str, Any] = {"name": name, "type": "dir", "path": rel}
            if depth + 1 < contract.MAX_TREE_DEPTH:
                node["children"] = _collect_children(
                    entry.path, rel, depth + 1, state
                )
            else:
                node["children"] = []
            children.append(node)
        elif entry.is_file(follow_symlinks=False):
            if not is_included_file(name):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > contract.MAX_FILE_BYTES:
                continue
            rel = _join(rel_prefix, name)
            state["count"] += 1
            children.append(
                {"name": name, "type": "file", "path": rel, "size": size}
            )
    return children


def read_document(root: str, rel_path: str) -> Dict[str, Any]:
    """Read one permitted document ``rel_path`` below the accepted ``root``.

    Rejects, with bounded errors, absolute paths, ``..`` traversal, symlink
    escape outside the root, excluded directories, missing paths, non-regular
    or unreadable files, unsupported extensions and oversized files. Returns
    ``{"path", "name", "size", "content"}`` with the root-relative ``path``.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise contract.ContractError("invalid_request")

    normalized = rel_path.replace("\\", "/")
    if os.path.isabs(normalized) or normalized.startswith("/"):
        raise contract.ContractError("path_not_allowed")

    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise contract.ContractError("path_not_allowed")

    # Any intermediate component that is an excluded directory name makes the
    # document unreachable regardless of what actually exists on disk.
    if any(p in EXCLUDED_DIR_NAMES for p in parts[:-1]):
        raise contract.ContractError("path_not_allowed")

    # Containment guard: the symlink-resolved target must stay within the
    # symlink-resolved root (blocks ``..`` and symlink escape).
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root, *parts))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise contract.ContractError("path_not_allowed")

    if not os.path.exists(target):
        raise contract.ContractError("path_not_found")
    if os.path.islink(target) or not os.path.isfile(target):
        raise contract.ContractError("path_not_readable")
    if not os.access(target, os.R_OK):
        raise contract.ContractError("path_not_readable")
    if not is_included_file(os.path.basename(target)):
        raise contract.ContractError("unsupported_type")

    try:
        size = os.path.getsize(target)
    except OSError:
        raise contract.ContractError("path_not_readable")
    if size > contract.MAX_DOCUMENT_BYTES:
        raise contract.ContractError("file_too_large")

    try:
        with open(target, "rb") as fh:
            raw = fh.read(contract.MAX_DOCUMENT_BYTES + 1)
    except OSError:
        raise contract.ContractError("path_not_readable")

    # UTF-8 with replacement keeps the surface deterministic even for stray
    # bytes; the filter already restricts us to text-ish source documents.
    content = raw.decode("utf-8", errors="replace")

    return {
        "path": "/".join(parts),
        "name": os.path.basename(target),
        "size": size,
        "content": content,
    }


__all__ = [
    "INCLUDED_FILENAMES",
    "INCLUDED_SUFFIXES",
    "EXCLUDED_DIR_NAMES",
    "is_included_file",
    "resolve_root",
    "build_tree",
    "read_document",
]
