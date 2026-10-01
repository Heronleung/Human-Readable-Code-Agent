"""Bind the actual bytes of a scope to one bounded content manifest.

The graph records are only meaningful if "the source" means something exact.
A commit id is not enough here: the accepted local work is uncommitted, so the
source that was scanned includes dirty and untracked files. This module
therefore reads the *files*, digests them, and records what it read.

Two honesty rules shape it:

* **No atomic snapshot is claimed.** The scope is listed, every file is read
  with a stat either side of the read, and the scope is listed again. If
  anything appeared, disappeared or changed while we looked, the manifest is
  marked ``complete=False`` — which makes any run against it produce
  non-current evidence rather than a confident lie.
* **Only bounded metadata is bound.** A manifest carries relative paths,
  content digests and sizes. It never carries file content, so no source and no
  secret can be persisted through it by default.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from hrca.core import identity, workspace

from . import domain

#: The scanner's own file set. Binding exactly what the scan will read keeps the
#: manifest and the evidence describing the same bytes.
SOURCE_SUFFIXES: Tuple[str, ...] = (".py", ".pyi")

#: Bounds, so a manifest stays a bounded artifact. Exceeding either marks the
#: observation incomplete rather than silently truncating it.
MAX_MANIFEST_FILES = 20000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024

#: Directory names never entered. The product's own containment policy, plus
#: Git metadata, which is never read by this slice.
EXCLUDED_DIR_NAMES = frozenset(set(workspace.EXCLUDED_DIR_NAMES) | {".git"})

MANIFEST_COMPLETE = True


def _skip_dir(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES


def _list_source_paths(root: str, scope: domain.Scope) -> Tuple[List[str], List[str]]:
    """Return ``(relative paths, unreadable)`` for the scope, sorted."""
    paths: List[str] = []
    unreadable: List[str] = []
    for start in (scope.include_paths or (".",)):
        base = os.path.join(root, *[p for p in str(start).replace("\\", "/").split("/") if p])
        if not os.path.isdir(base):
            unreadable.append(str(start))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(name for name in dirnames if not _skip_dir(name))
            for name in sorted(filenames):
                if not name.endswith(SOURCE_SUFFIXES):
                    continue
                absolute = os.path.join(dirpath, name)
                if os.path.islink(absolute):
                    continue
                relative = os.path.relpath(absolute, root).replace(os.sep, "/")
                paths.append(relative)
    return sorted(paths), sorted(unreadable)


def _read_entry(root: str, relative: str) -> Tuple[Optional[domain.ManifestEntry], Optional[str]]:
    """Read one file and return its entry, or the reason it could not be read.

    The stat either side of the read is what detects a file changing under us.
    """
    absolute = os.path.join(root, *relative.split("/"))
    try:
        before = os.stat(absolute)
        if before.st_size > MAX_MANIFEST_BYTES:
            return None, relative
        with open(absolute, "rb") as handle:
            data = handle.read()
        after = os.stat(absolute)
    except OSError:
        return None, relative
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None, relative
    if len(data) != after.st_size:
        return None, relative
    return (
        domain.ManifestEntry(
            path=relative, digest=identity.sha256_hex(data), size_bytes=len(data)
        ),
        None,
    )


def observe_scope(
    root: str,
    scope: domain.Scope,
    *,
    scanner_schema: str,
    grammar: Mapping[str, str],
    observed_at: str,
) -> domain.SourceManifest:
    """Return the manifest of the scope's actual bytes.

    ``complete`` is false when anything was unreadable, anything changed during
    the walk, the file set changed between the two listings, or a bound was
    exceeded. An incomplete manifest still names what was seen; it simply
    cannot produce current evidence.
    """
    first, unreadable = _list_source_paths(root, scope)
    entries: List[domain.ManifestEntry] = []
    unreachable = list(unreadable)
    total_bytes = 0

    for relative in first:
        entry, failed = _read_entry(root, relative)
        if entry is None:
            unreachable.append(failed or relative)
            continue
        total_bytes += entry.size_bytes
        if total_bytes > MAX_MANIFEST_BYTES:
            unreachable.append(relative)
            continue
        entries.append(entry)

    second, _ = _list_source_paths(root, scope)
    complete = (
        not unreachable
        and first == second
        and len(entries) == len(first)
        and len(entries) <= MAX_MANIFEST_FILES
    )
    if len(entries) > MAX_MANIFEST_FILES:
        unreachable.append("<manifest file bound exceeded>")

    return domain.build_manifest(
        entries,
        exclusions=sorted(EXCLUDED_DIR_NAMES),
        scanner_schema=scanner_schema,
        grammar=grammar,
        complete=complete,
        unreadable=sorted(set(unreachable)),
        observed_at=observed_at,
    )


def observe_current(
    root: str,
    scope: domain.Scope,
    *,
    scanner_schema: str,
    grammar: Mapping[str, str],
    observed_at: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(manifest_id, failure)`` for a freshness re-check.

    Used before dispatch and before a current-evidence acknowledgement. A
    failure to observe is reported as a failure, never as "unchanged".
    """
    try:
        manifest = observe_scope(
            root,
            scope,
            scanner_schema=scanner_schema,
            grammar=grammar,
            observed_at=observed_at,
        )
    except OSError:
        return None, "the scope could not be observed"
    if not manifest.complete:
        return None, "the scope could not be observed consistently"
    return manifest.manifest_id, None


def scanner_identity() -> Tuple[str, Dict[str, Any]]:
    """Return the scanner's schema version and bounded grammar context.

    Read from the scanner module itself, so the manifest always describes the
    grammar that actually produced the evidence.
    """
    from hrca.source import scanner as _scanner

    return _scanner.SCHEMA_VERSION, dict(_scanner.grammar_context())


def summarize(manifest: domain.SourceManifest) -> Dict[str, Any]:
    """Return the bounded summary of a manifest that evidence may carry."""
    return {
        "manifest_id": manifest.manifest_id,
        "file_count": manifest.file_count,
        "complete": manifest.complete,
        "unreadable_count": len(manifest.unreadable),
        "scanner_schema": manifest.scanner_schema,
        "grammar": dict(manifest.grammar),
    }


__all__ = [
    "SOURCE_SUFFIXES",
    "EXCLUDED_DIR_NAMES",
    "MAX_MANIFEST_FILES",
    "MAX_MANIFEST_BYTES",
    "observe_scope",
    "observe_current",
    "scanner_identity",
    "summarize",
]
