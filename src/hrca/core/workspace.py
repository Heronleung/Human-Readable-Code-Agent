"""Read-only workspace filesystem policy (P3.2).

This module is the *boundary-side* authority for the P3.2 workspace/document
surface. It never performs a write, Git operation, command execution, network
access or provider call, and it never resolves a path outside the root the
boundary has accepted. It imports only the standard library and
:mod:`hrca.contract`, so it can never leak the deterministic core into a
client.

It provides:

* :func:`classify_file` — the deterministic render kind for a filename
  (``source`` / ``preview`` / ``binary`` / ``unsupported``) and the excluded
  directory names,
* :func:`resolve_root` — canonicalize and validate a project root,
* :func:`build_tree` — a deterministic, filtered, size-bounded directory tree
  that lists every ordinary file and folder (with each file's ``size`` and
  ``kind``) while still excluding the excluded directories and symlinks,
* :func:`read_document` — read one document below the root, returning a
  ``source`` / ``preview`` result for readable text and a bounded
  ``unavailable`` result for binary, unsupported, missing, unreadable or
  oversized files.

A malformed request or a path that escapes the accepted root raises a bounded
:class:`hrca.contract.ContractError` whose message is drawn from the fixed
catalogue; availability failures are reported as an ``unavailable`` result, so
no requested path or file content ever leaks into an error.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import contract

# ---------------------------------------------------------------------------
# File classification.
#
# Every ordinary file is one of four kinds; the kind decides how the client
# renders it:
#
# * ``source``       — Python project files (``.py`` / ``.pyi`` /
#   ``pyproject.toml`` / ``README.md`` / ``README.rst``): full syntax-highlighted
#   source view.
# * ``preview``      — common text and configuration formats: a clearly-labelled
#   read-only text preview.
# * ``binary``       — known binary formats: listed in the tree but never
#   decoded.
# * ``unsupported``  — unrecognised/other extensions: listed in the tree but
#   never decoded.
# ---------------------------------------------------------------------------
SOURCE_FILENAMES = frozenset({"pyproject.toml", "README.md", "README.rst"})
SOURCE_SUFFIXES = frozenset({".py", ".pyi"})

PREVIEW_FILENAMES = frozenset(
    {
        "Makefile",
        "makefile",
        "GNUmakefile",
        "Dockerfile",
        "dockerfile",
        "LICENSE",
        "LICENSE.txt",
        "LICENSE.md",
        "NOTICE",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".editorconfig",
        ".env",
    }
)
PREVIEW_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst", ".json", ".jsonc", ".toml",
        ".yaml", ".yml", ".ini", ".cfg", ".conf", ".config", ".csv", ".tsv",
        ".xml", ".log", ".sh", ".bash", ".zsh", ".properties", ".lock",
        ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
        ".vue", ".sql", ".c", ".h", ".cpp", ".hpp", ".java", ".rb", ".php",
        ".swift", ".kt", ".kts", ".rs", ".go",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav",
        ".avi", ".mov", ".mkv", ".db", ".sqlite", ".class", ".o", ".a",
        ".jar", ".wasm", ".whl",
    }
)

FILE_KINDS = ("source", "preview", "binary", "unsupported")

# Bounded reasons an ``unavailable`` document result can carry. These are the
# only values ``read_document`` places in ``reason``; none echoes a path or
# file content.
UNAVAILABLE_UNSUPPORTED = "unsupported_type"
UNAVAILABLE_BINARY = "binary"
UNAVAILABLE_TOO_LARGE = "file_too_large"
UNAVAILABLE_NOT_FOUND = "path_not_found"
UNAVAILABLE_NOT_READABLE = "path_not_readable"
UNAVAILABLE_REASONS = frozenset(
    {
        UNAVAILABLE_UNSUPPORTED,
        UNAVAILABLE_BINARY,
        UNAVAILABLE_TOO_LARGE,
        UNAVAILABLE_NOT_FOUND,
        UNAVAILABLE_NOT_READABLE,
    }
)

# Directory names the tree walk and document reader never descend into or
# resolve through. These are workspace-level exclusions, not a security claim.
EXCLUDED_DIR_NAMES = frozenset(
    {".git", ".venv", "__pycache__", "node_modules", "build", "dist"}
)


def classify_file(name: str) -> str:
    """Return the render kind for ``name`` (source / preview / binary / unsupported)."""
    if name in SOURCE_FILENAMES or _suffix(name) in SOURCE_SUFFIXES:
        return "source"
    if name in PREVIEW_FILENAMES or _suffix(name) in PREVIEW_SUFFIXES:
        return "preview"
    if _suffix(name) in BINARY_SUFFIXES:
        return "binary"
    return "unsupported"


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
    Every ordinary file and folder below the root — except the excluded
    directories and symlinks — is listed. Entries are dictionaries of ``name``,
    ``type`` (``dir`` or ``file``), ``path`` (portable, root-relative); file
    entries add ``size`` and ``kind``, directory entries a ``children`` list.
    Within each directory, entries are ordered directories-first then by name,
    so identical rescans are byte-identical.
    """
    state = {"count": 0, "truncated": False}
    children = _collect_children(root, "", 0, state)
    return {"root": root, "truncated": state["truncated"], "children": children}


def _collect_children(
    dir_path: str, rel_prefix: str, depth: int, state: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Collect the children of ``dir_path`` (at ``depth`` components).

    Symlinks are skipped (never followed); excluded directories are skipped; the
    walk stops once ``contract.MAX_TREE_ENTRIES`` entries have been emitted
    (marking ``truncated``). A directory is expanded only while its children
    remain within ``contract.MAX_TREE_DEPTH`` components of the root. Every file
    is included with its ``size`` and render ``kind``.
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
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            rel = _join(rel_prefix, name)
            state["count"] += 1
            children.append(
                {
                    "name": name,
                    "type": "file",
                    "path": rel,
                    "size": size,
                    "kind": classify_file(name),
                }
            )
    return children


def read_document(root: str, rel_path: str) -> Dict[str, Any]:
    """Return the read-only state of one document ``rel_path`` below ``root``.

    The result is always one of:

    * ``{"path", "name", "size", "kind": "source"|"preview", "content"}`` for a
      readable text file (Python source, or a common text/config preview);
    * ``{"path", "name", "size", "kind": "unavailable", "reason"}`` for a file
      that cannot be shown. ``reason`` is one of :data:`UNAVAILABLE_REASONS`.

    Only a malformed request or a path that escapes the accepted root (absolute,
    ``..`` traversal, symlink escape, or an excluded directory) raises a bounded
    :class:`~hrca.contract.ContractError`; availability failures are reported as
    an ``unavailable`` result, never an exception.
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

    name = os.path.basename(target)
    size = _size(target)

    if not os.path.exists(target):
        return _unavailable(parts, name, None, UNAVAILABLE_NOT_FOUND)
    if os.path.islink(target) or not os.path.isfile(target):
        return _unavailable(parts, name, None, UNAVAILABLE_NOT_READABLE)
    if not os.access(target, os.R_OK):
        return _unavailable(parts, name, size, UNAVAILABLE_NOT_READABLE)

    kind = classify_file(name)
    if kind == "binary":
        # Never decoded: an arbitrarily large binary is still bounded.
        return _unavailable(parts, name, size, UNAVAILABLE_BINARY)
    if kind == "unsupported":
        return _unavailable(parts, name, size, UNAVAILABLE_UNSUPPORTED)

    if size is None or size > contract.MAX_DOCUMENT_BYTES:
        return _unavailable(parts, name, size, UNAVAILABLE_TOO_LARGE)

    try:
        with open(target, "rb") as fh:
            raw = fh.read(contract.MAX_DOCUMENT_BYTES + 1)
    except OSError:
        return _unavailable(parts, name, size, UNAVAILABLE_NOT_READABLE)

    if len(raw) > contract.MAX_DOCUMENT_BYTES:
        return _unavailable(parts, name, size, UNAVAILABLE_TOO_LARGE)
    if _is_binary(raw):
        return _unavailable(parts, name, size, UNAVAILABLE_BINARY)

    # UTF-8 with replacement keeps the surface deterministic even for stray
    # bytes; the classification already restricts us to text-ish documents.
    content = raw.decode("utf-8", errors="replace")

    return {
        "path": "/".join(parts),
        "name": name,
        "size": size,
        "kind": kind,
        "content": content,
    }


def _size(path: str) -> Optional[int]:
    """Return the size of ``path``, or ``None`` when it cannot be read."""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def _is_binary(raw: bytes) -> bool:
    """Return True when ``raw`` carries a NUL byte (a strong binary signal)."""
    return b"\x00" in raw


def _unavailable(
    parts: List[str], name: str, size: Optional[int], reason: str
) -> Dict[str, Any]:
    """Return a bounded ``unavailable`` document result."""
    return {
        "path": "/".join(parts),
        "name": name,
        "size": size,
        "kind": "unavailable",
        "reason": reason,
    }


__all__ = [
    "SOURCE_FILENAMES",
    "SOURCE_SUFFIXES",
    "PREVIEW_FILENAMES",
    "PREVIEW_SUFFIXES",
    "BINARY_SUFFIXES",
    "FILE_KINDS",
    "UNAVAILABLE_UNSUPPORTED",
    "UNAVAILABLE_BINARY",
    "UNAVAILABLE_TOO_LARGE",
    "UNAVAILABLE_NOT_FOUND",
    "UNAVAILABLE_NOT_READABLE",
    "UNAVAILABLE_REASONS",
    "EXCLUDED_DIR_NAMES",
    "classify_file",
    "resolve_root",
    "build_tree",
    "read_document",
]
