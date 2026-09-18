"""Versioned export and local-sensitive backup packages for Developer Memory.

M4.5/v2a. Two profiles, deliberately kept apart:

``export``
    a least-disclosure package built only from named allowlisted projections —
    the human-facing documents, the effective confirmed corrections and bounded
    evidence metadata. It is the only profile that may be shared, and it carries
    no raw store, no source, no transcript, no payload, no credential and no
    internal fingerprint.
``backup``
    a lossless local-sensitive package built from the run store itself, so an
    exact restoration is possible. It is **never** labelled safe to share.

Both profiles are deterministic: entries are sorted, every archive member is
stamped with a fixed instant, and the manifest carries per-entry checksums that
attest the *package bytes* only — never a stored source or evidence fingerprint.
No clock is consulted: a creation instant is used only when a caller supplies
one, so packaging the same snapshot twice yields identical bytes.

A package is data, never code, and is treated as hostile until validated: an
archive naming a path outside itself, carrying a link, colliding on a name,
mismatching a checksum, or expanding past its bound is refused before anything
is written anywhere.

A backup is a claim about a *cross-run state*, so it is built from one verified
snapshot rather than from independently timed reads: every store in scope is
captured, re-read and proven unmoved (by content identity *and* by the storage
owner's change-detector), and the set is proven to resolve every reference
inside itself, before a single entry exists. The manifest then binds each run to
the snapshot entry that carries it, and staged recovery re-derives that same
binding from the staged bytes before an active store is in scope. See
:func:`capture_stores`, :func:`verify_capture` and :func:`verify_staged_snapshot`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from . import memory
from . import memory_docs
from . import memory_revisions
from . import memory_store

PACKAGE_SCHEMA_VERSION = "1.0.0"
PACKAGE_GENERATOR = "hrca-memory-package"

PROFILE_EXPORT = "export"
PROFILE_BACKUP = "backup"
PROFILES = (PROFILE_EXPORT, PROFILE_BACKUP)

# The label each profile carries. They are deliberately different sentences: an
# operator must never be able to mistake a local-sensitive backup for something
# that is safe to send.
PROFILE_LABELS = {
    PROFILE_EXPORT: "Shareable export — least disclosure, allowlisted projections only",
    PROFILE_BACKUP: "Local-sensitive backup — never safe to share",
}

MANIFEST_NAME = "manifest.json"
NOTE_NAME = "PACKAGE.txt"

# The manifest's name inside a staging directory. It is deliberately outside the
# package's own entry namespace, so it can never be confused with content.
STAGED_MANIFEST_NAME = ".memory-package-manifest.json"

EXPORT_DOCUMENTS_PREFIX = "documents/"
EXPORT_EFFECTIVE_PREFIX = "effective/"
EXPORT_EVIDENCE_PREFIX = "evidence/"
BACKUP_STORES_PREFIX = "stores/"

# -- bounds --------------------------------------------------------------

MAX_ENTRIES = 512
MAX_ENTRY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# A package that expands far beyond its archive is refused whether or not it is
# malicious: a legitimate package of this shape never reaches this ratio.
MAX_EXPANSION_RATIO = 200
MAX_NAME_CHARS = 256
MAX_NAME_DEPTH = 6
MAX_CREATED_AT_CHARS = 64

# Zip stamps every member; a fixed instant keeps two runs byte-identical.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_COMPRESS_LEVEL = 9

# -- bounded, content-free reasons ---------------------------------------

REASON_UNREADABLE = "the package could not be read"
REASON_NOT_ZIP = "the package is not a readable archive"
REASON_MANIFEST_MISSING = "the package carries no manifest"
REASON_MANIFEST_MALFORMED = "the package manifest is malformed"
REASON_PACKAGE_VERSION = "the package schema version is not supported"
REASON_PROFILE_UNSUPPORTED = "the package profile is not supported"
REASON_MEMORY_SCHEMA = "the package was written for an unsupported Memory schema"
REASON_ENTRY_COUNT = "the package declares more entries than the bound allows"
REASON_ENTRY_TOO_LARGE = "a package entry exceeds the bounded size"
REASON_TOTAL_TOO_LARGE = "the package exceeds the bounded total size"
REASON_EXPANSION = "the package expands beyond the bounded ratio"
REASON_UNSAFE_NAME = "a package entry name is not a plain relative path"
REASON_DUPLICATE_NAME = "the package declares the same entry twice"
REASON_CASE_COLLISION = "two package entry names differ only by case"
REASON_LINK_ENTRY = "a package entry is not a regular file"
REASON_MISSING_ENTRY = "a declared entry is absent from the archive"
REASON_UNDECLARED_ENTRY = "the archive carries an entry the manifest does not declare"
REASON_CHECKSUM_MISMATCH = "a package entry does not match its declared checksum"
REASON_SIZE_MISMATCH = "a package entry does not match its declared size"
REASON_NOT_A_STORE = "package content is not a Memory store"
REASON_STAGING_UNUSABLE = "the staging root is not usable"
REASON_ACTIVE_UNREADABLE = "the active store could not be read"
REASON_ACTIVE_MISMATCH = "the active store is not the one this plan was built against"
REASON_NOT_PLANNED = "the run is not part of this restore plan"
REASON_ROLLBACK_FAILED = "rollback material could not be preserved"
REASON_SNAPSHOT_MISSING = "a backup package must declare the snapshot it was taken from"
REASON_SNAPSHOT_MALFORMED = "the declared snapshot is malformed"
REASON_SNAPSHOT_MISMATCH = "the package does not match the snapshot it declares"
REASON_SNAPSHOT_UNSTABLE = (
    "a store changed while the snapshot was being taken, so no coherent "
    "cross-run state was captured"
)
REASON_NO_RUNS = "no run store was found to snapshot"
REASON_RUN_MISSING = "a requested run has no readable store"
REASON_RUN_SET_CHANGED = (
    "the set of stored runs changed while the snapshot was being taken"
)
REASON_CROSS_STORE = "a store references a record outside itself"

# The snapshot model this boundary proves. Stores are independent documents: no
# record in one references, orders, supersedes or otherwise depends on a record
# in another, so a set of per-store atomic reads *is* a coherent cross-run state
# once each store is proven unchanged for the whole capture.
SNAPSHOT_MODEL = "independent-store"


# -- canonical helpers ---------------------------------------------------


def canonical(value: Any) -> str:
    """Return the canonical JSON encoding used for every package document."""
    return memory.dumps(value)


def checksum_of(data: bytes) -> str:
    """Return the package checksum of ``data``."""
    return "sha256:" + memory.sha256_hex(data)


def _entry_token(value: Any) -> str:
    """Return the archive-safe namespace for a run id.

    It matches the store's own directory naming exactly, so a package entry and
    the store it came from can never disagree about which run they name.
    """
    return memory_store.store_namespace(str(value))


# -- entry-name policy ---------------------------------------------------


def name_error(name: Any) -> Optional[str]:
    """Return why ``name`` is not a usable package entry name, or ``None``.

    A package may only name a plain relative path inside itself: no absolute
    name, no drive letter, no backslash, no traversal segment, no empty segment
    and no bound exceeded. The check is on the name alone, so it cannot be
    evaded by how the archive happens to be read.
    """
    if not isinstance(name, str) or not name:
        return REASON_UNSAFE_NAME
    if len(name) > MAX_NAME_CHARS:
        return REASON_UNSAFE_NAME
    if name.startswith("/") or name.startswith("\\"):
        return REASON_UNSAFE_NAME
    if "\\" in name:
        return REASON_UNSAFE_NAME
    if ":" in name:
        return REASON_UNSAFE_NAME
    segments = name.split("/")
    if len(segments) > MAX_NAME_DEPTH:
        return REASON_UNSAFE_NAME
    for segment in segments:
        if not segment or segment in (".", ".."):
            return REASON_UNSAFE_NAME
    return None


def _entry(kind: str, name: str, data: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    error = name_error(name)
    if error is not None:
        return None, error
    if len(data) > MAX_ENTRY_BYTES:
        return None, REASON_ENTRY_TOO_LARGE
    return {
        "name": name,
        "kind": kind,
        "bytes": len(data),
        "checksum": checksum_of(data),
    }, None


# -- profiles ------------------------------------------------------------


def export_entries(
    store: Any, run_id: str
) -> Tuple[Optional[List[Tuple[Dict[str, Any], bytes]]], Optional[str]]:
    """Build the least-disclosure entries of one run.

    Only named allowlisted projections are used: the projected documents, the
    effective resolution of each, and bounded evidence metadata. Nothing here
    reads a payload, a transcript, a fingerprint or a path outside the session
    root, because nothing here reaches past the accepted projector.
    """
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE
    document_set, _err, _report = memory_docs.project_store(store)
    if document_set is None:
        return None, REASON_NOT_A_STORE

    token = _entry_token(run_id)
    entries: List[Tuple[Dict[str, Any], bytes]] = []
    for document_type in sorted(document_set.get("documents") or {}):
        document = (document_set.get("documents") or {})[document_type]
        payload = canonical(
            {
                "run_id": run_id,
                "document_type": document_type,
                "document": document,
                "limitations": document_set.get("limitations") or [],
            }
        ).encode("utf-8")
        record, error = _entry(
            "document",
            "%s%s/%s.json" % (EXPORT_DOCUMENTS_PREFIX, token, document_type),
            payload,
        )
        if error is not None:
            return None, error
        entries.append((record, payload))

        version = _latest_version(store, run_id, document_type)
        effective, _reason = memory_revisions.resolve_effective(
            store, run_id, document_type, document, version
        )
        if effective is None:
            continue
        effective_payload = canonical(effective).encode("utf-8")
        record, error = _entry(
            "effective",
            "%s%s/%s.json" % (EXPORT_EFFECTIVE_PREFIX, token, document_type),
            effective_payload,
        )
        if error is not None:
            return None, error
        entries.append((record, effective_payload))

    evidence = _evidence_metadata(store)
    evidence_payload = canonical(evidence).encode("utf-8")
    record, error = _entry(
        "evidence", "%s%s.json" % (EXPORT_EVIDENCE_PREFIX, token), evidence_payload
    )
    if error is not None:
        return None, error
    entries.append((record, evidence_payload))

    note = _note(PROFILE_EXPORT, run_id).encode("utf-8")
    record, error = _entry("note", NOTE_NAME, note)
    if error is not None:
        return None, error
    entries.append((record, note))
    return entries, None


def _latest_version(store: Dict[str, Any], run_id: str, document_type: str) -> Any:
    versions = [
        record
        for record in store.get("generated_documents", [])
        if isinstance(record, dict)
        and record.get("run_id") == run_id
        and record.get("document_type") == document_type
    ]
    versions.sort(key=lambda record: record.get("revision", 0))
    return versions[-1] if versions else None


def _evidence_metadata(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return bounded evidence *metadata*: kind, reference and size only.

    The internal digest is deliberately absent, and so is anything the contract
    would not already render.
    """
    records = []
    for record in store.get("evidence", []):
        if not isinstance(record, dict):
            continue
        records.append(
            {
                "id": record.get("id"),
                "kind": record.get("kind"),
                "artifact_ref": record.get("artifact_ref"),
                "bytes": record.get("bytes"),
            }
        )
    return {"evidence": sorted(records, key=lambda r: str(r.get("id")))}


def backup_entries(
    snapshot: Any,
) -> Tuple[Optional[List[Tuple[Dict[str, Any], bytes]]], Optional[str]]:
    """Build the lossless local-sensitive entries of one verified snapshot.

    The snapshot is the *only* source: a backup is built from the stores a single
    verified capture produced, never from independently timed reads, so a
    multi-run backup can never mix logical moments. Each store is written whole,
    because an exact restoration needs everything the contract already retains,
    and nothing new is collected: no source, no transcript, no credential, no
    filesystem content.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("stores"), dict):
        return None, REASON_SNAPSHOT_MALFORMED
    stores = snapshot["stores"]
    if not stores:
        return None, REASON_NO_RUNS
    if verify_independence(stores) is not None:
        return None, REASON_CROSS_STORE

    run_ids = sorted(stores)
    note = _note(PROFILE_BACKUP, ", ".join(run_ids)).encode("utf-8")
    note_record, note_error = _entry("note", NOTE_NAME, note)
    if note_error is not None:
        return None, note_error
    entries: List[Tuple[Dict[str, Any], bytes]] = [(note_record, note)]
    for run_id in run_ids:
        payload = canonical(stores[run_id]).encode("utf-8")
        record, error = _entry(
            "store", "%s%s.json" % (BACKUP_STORES_PREFIX, _entry_token(run_id)), payload
        )
        if error is not None:
            return None, error
        entries.append((record, payload))
    return entries, None


def _note(profile: str, run_id: str) -> str:
    lines = [
        "Developer Memory package",
        PROFILE_LABELS[profile],
        "profile: %s" % profile,
        "run: %s" % run_id,
        "package schema: %s" % PACKAGE_SCHEMA_VERSION,
        "memory schema: %s" % memory.MEMORY_SCHEMA_VERSION,
        "",
    ]
    if profile == PROFILE_EXPORT:
        lines.extend(
            [
                "This package holds human-facing projections only. It carries no raw",
                "store, no source, no transcript, no hook payload and no internal",
                "fingerprint. Every statement in it was produced by the offline",
                "projector from normalized records.",
            ]
        )
    else:
        lines.extend(
            [
                "This package holds a complete normalized store so the run can be",
                "restored exactly. It is LOCAL-SENSITIVE: it retains every record the",
                "contract already stored. Do not share it.",
            ]
        )
    return "\n".join(lines) + "\n"


# -- manifest and archive ------------------------------------------------


def build_manifest(
    profile: str,
    entries: List[Tuple[Dict[str, Any], bytes]],
    created_at: Optional[str] = None,
    snapshot: Any = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the canonical manifest for one package."""
    if profile not in PROFILES:
        return None, REASON_PROFILE_UNSUPPORTED
    if profile == PROFILE_BACKUP:
        # A backup is a claim about a cross-run state. Without a declared
        # snapshot it would be an unverifiable set of files.
        declared, error = _snapshot_block(snapshot, entries)
        if declared is None:
            return None, error
    if created_at is not None:
        if not isinstance(created_at, str) or not created_at.strip():
            return None, REASON_MANIFEST_MALFORMED
        created_at = created_at.strip()[:MAX_CREATED_AT_CHARS]
    if len(entries) > MAX_ENTRIES:
        return None, REASON_ENTRY_COUNT

    catalogue = []
    seen = {}
    folded = {}
    total = 0
    for record, data in entries:
        name = record["name"]
        error = name_error(name)
        if error is not None:
            return None, error
        if name in seen:
            return None, REASON_DUPLICATE_NAME
        lowered = name.lower()
        if lowered in folded:
            return None, REASON_CASE_COLLISION
        seen[name] = True
        folded[lowered] = True
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            return None, REASON_TOTAL_TOO_LARGE
        catalogue.append(dict(record))

    catalogue.sort(key=lambda record: record["name"])
    manifest: Dict[str, Any] = {
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "generator": PACKAGE_GENERATOR,
        "profile": profile,
        "label": PROFILE_LABELS[profile],
        "memory_schema_version": memory.MEMORY_SCHEMA_VERSION,
        # No clock is consulted: absent unless a caller supplies one, which is
        # what makes two packagings of one snapshot byte-identical.
        "created_at": created_at,
        "entry_count": len(catalogue),
        "total_bytes": total,
        "entries": catalogue,
        "limitations": [
            "entry checksums attest the package bytes only; no stored source or "
            "evidence fingerprint is exposed",
            "a package is data, never code: it is validated before it is read",
        ]
        + (
            ["an export is a projection; it cannot restore a run"]
            if profile == PROFILE_EXPORT
            else ["a backup is local-sensitive and must never be shared"]
        ),
    }
    if profile == PROFILE_BACKUP:
        manifest["snapshot"] = declared
    return manifest, None


def _snapshot_block(
    snapshot: Any, entries: List[Tuple[Dict[str, Any], bytes]]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the manifest's snapshot block, bound to the entries it covers.

    The block is only meaningful if it vouches for *all* the store content the
    package carries: every run it declares must name an entry that is really
    there, and every ``stores/`` entry must be declared by a run. A package
    holding a store no run accounts for would otherwise be a package carrying
    content its own snapshot does not attest.
    """
    if not isinstance(snapshot, dict):
        return None, REASON_SNAPSHOT_MISSING
    model = snapshot.get("model")
    runs = snapshot.get("runs")
    identity = snapshot.get("identity")
    if model != SNAPSHOT_MODEL or not isinstance(runs, list) or not runs:
        return None, REASON_SNAPSHOT_MALFORMED
    if not isinstance(identity, str) or not identity:
        return None, REASON_SNAPSHOT_MALFORMED

    checksums = {record["name"]: record["checksum"] for record, _data in entries}
    store_entries = {
        record["name"]
        for record, _data in entries
        if str(record["name"]).startswith(BACKUP_STORES_PREFIX)
    }
    declared = []
    claimed = set()
    for run in runs:
        if not isinstance(run, dict):
            return None, REASON_SNAPSHOT_MALFORMED
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return None, REASON_SNAPSHOT_MALFORMED
        entry = run.get("entry")
        # One entry cannot be two runs, and an entry no run names cannot be
        # covered: both would leave content the snapshot does not account for.
        if entry not in checksums or entry in claimed:
            return None, REASON_SNAPSHOT_MISMATCH
        claimed.add(entry)
        # The run's identity *is* the checksum of its entry, so the declared
        # snapshot and the package bytes cannot disagree.
        if run.get("identity") != checksums[entry]:
            return None, REASON_SNAPSHOT_MISMATCH
        declared.append(
            {"run_id": run_id, "entry": entry, "identity": run.get("identity")}
        )
    if claimed != store_entries:
        return None, REASON_SNAPSHOT_MISMATCH
    declared.sort(key=lambda run: run["run_id"])
    if declared != [
        {"run_id": run.get("run_id"), "entry": run.get("entry"),
         "identity": run.get("identity")}
        for run in runs
    ]:
        return None, REASON_SNAPSHOT_MALFORMED
    if _snapshot_identity(declared) != identity:
        return None, REASON_SNAPSHOT_MISMATCH
    return {"model": model, "identity": identity, "runs": declared}, None


def _snapshot_identity(declared_runs: List[Dict[str, Any]]) -> str:
    """Return the identity of a declared run set."""
    return "snap:" + memory.sha256_hex(
        canonical({"model": SNAPSHOT_MODEL, "runs": declared_runs}).encode("utf-8")
    )[:32]


def write_package(
    path: str,
    profile: str,
    entries: List[Tuple[Dict[str, Any], bytes]],
    created_at: Optional[str] = None,
    snapshot: Any = None,
) -> Optional[str]:
    """Write one deterministic package atomically; return a reason or ``None``."""
    manifest, error = build_manifest(profile, entries, created_at, snapshot)
    if manifest is None:
        return error
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        return REASON_STAGING_UNUSABLE

    handle = None
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".memory-package-", suffix=".tmp", dir=directory
        )
        handle = os.fdopen(descriptor, "wb")
        with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=_COMPRESS_LEVEL) as archive:
            _write_member(archive, MANIFEST_NAME, canonical(manifest).encode("utf-8"))
            for record, data in entries:
                _write_member(archive, record["name"], data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_path, path)
        temp_path = None
    except OSError:
        return REASON_UNREADABLE
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if temp_path is not None and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return None


def _write_member(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    archive.writestr(info, data)


def read_package(
    path: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, bytes]], Optional[str]]:
    """Validate and read one package.

    Returns ``(manifest, entries, reason)``. Exactly one of the first two is set
    on success; on any failure both are ``None`` and ``reason`` is a bounded
    constant. Every check happens before a caller can see a byte of content, so
    an unsafe archive is refused rather than partially believed.
    """
    if not isinstance(path, str) or not os.path.isfile(path):
        return None, None, REASON_UNREADABLE
    try:
        with zipfile.ZipFile(path, "r") as archive:
            return _read_archive(archive)
    except (zipfile.BadZipFile, OSError, EOFError):
        return None, None, REASON_NOT_ZIP


def _read_archive(
    archive: zipfile.ZipFile,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, bytes]], Optional[str]]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES + 1:
        return None, None, REASON_ENTRY_COUNT

    # 1. Structure and names, before any content is read.
    seen = {}
    folded = {}
    total_uncompressed = 0
    total_compressed = 0
    actual: Dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        error = name_error(info.filename)
        if error is not None:
            return None, None, error
        if info.filename in seen:
            return None, None, REASON_DUPLICATE_NAME
        lowered = info.filename.lower()
        if lowered in folded:
            return None, None, REASON_CASE_COLLISION
        seen[info.filename] = True
        folded[lowered] = True
        # Only an entry that *declares* a non-regular type is refused. An
        # archive written without file-type bits (a plain ``writestr``, and
        # plenty of real tools) is ordinary content, not a link.
        mode = info.external_attr >> 16
        if (mode & 0o170000) and not stat.S_ISREG(mode):
            return None, None, REASON_LINK_ENTRY
        if info.file_size > MAX_ENTRY_BYTES:
            return None, None, REASON_ENTRY_TOO_LARGE
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        actual[info.filename] = info

    if total_uncompressed > MAX_TOTAL_BYTES:
        return None, None, REASON_TOTAL_TOO_LARGE
    if total_compressed > 0 and total_uncompressed / total_compressed > MAX_EXPANSION_RATIO:
        return None, None, REASON_EXPANSION

    if MANIFEST_NAME not in actual:
        return None, None, REASON_MANIFEST_MISSING
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, KeyError, OSError):
        return None, None, REASON_MANIFEST_MALFORMED
    if not isinstance(manifest, dict):
        return None, None, REASON_MANIFEST_MALFORMED

    # 2. Manifest shape and compatibility.
    if manifest.get("package_schema_version") != PACKAGE_SCHEMA_VERSION:
        return None, None, REASON_PACKAGE_VERSION
    if manifest.get("generator") != PACKAGE_GENERATOR:
        return None, None, REASON_MANIFEST_MALFORMED
    profile = manifest.get("profile")
    if profile not in PROFILES:
        return None, None, REASON_PROFILE_UNSUPPORTED
    schema = manifest.get("memory_schema_version")
    if not isinstance(schema, str) or not schema:
        return None, None, REASON_MANIFEST_MALFORMED
    try:
        if memory.version_tuple(schema) > memory.version_tuple(
            memory.MEMORY_SCHEMA_VERSION
        ):
            return None, None, REASON_MEMORY_SCHEMA
    except ValueError:
        return None, None, REASON_MEMORY_SCHEMA

    declared = manifest.get("entries")
    if not isinstance(declared, list) or len(declared) > MAX_ENTRIES:
        return None, None, REASON_MANIFEST_MALFORMED
    if manifest.get("entry_count") != len(declared):
        return None, None, REASON_MANIFEST_MALFORMED

    # 3. Declared versus actual: nothing missing, nothing extra.
    declared_names = set()
    for record in declared:
        if not isinstance(record, dict):
            return None, None, REASON_MANIFEST_MALFORMED
        name = record.get("name")
        error = name_error(name)
        if error is not None:
            return None, None, error
        declared_names.add(name)
    # The manifest names the content, not itself: it is declared by being there.
    actual_names = set(actual) - {MANIFEST_NAME}
    if declared_names != actual_names:
        if declared_names - actual_names:
            return None, None, REASON_MISSING_ENTRY
        return None, None, REASON_UNDECLARED_ENTRY

    # 4. Content: size and checksum, per declared entry.
    entries: Dict[str, bytes] = {}
    for record in declared:
        name = record["name"]
        try:
            data = archive.read(name)
        except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
            return None, None, REASON_UNREADABLE
        if record.get("bytes") != len(data):
            return None, None, REASON_SIZE_MISMATCH
        if record.get("checksum") != checksum_of(data):
            return None, None, REASON_CHECKSUM_MISMATCH
        entries[name] = data

    # 5. The declared snapshot must match the bytes it claims to cover. This runs
    #    before a caller can stage anything, so an inconsistent capture is
    #    refused before it can reach an active store.
    if profile == PROFILE_BACKUP:
        pairs = [(record, entries[record["name"]]) for record in declared]
        _block, block_error = _snapshot_block(manifest.get("snapshot"), pairs)
        if _block is None:
            return None, None, block_error
    elif manifest.get("snapshot") is not None:
        return None, None, REASON_MANIFEST_MALFORMED
    return manifest, entries, None


def inspect(path: str) -> Dict[str, Any]:
    """Return a bounded, content-free summary of one package.

    A backup reports the snapshot identity it declares and the runs that
    snapshot covers, so the binding a reader has to trust can be read off the
    package rather than taken from whoever wrote it. Nothing here exposes a
    stored fingerprint, a payload or a path: a run id and the identity of the
    package bytes are all a reader gets.
    """
    manifest, entries, error = read_package(path)
    if manifest is None:
        return {"status": "refused", "reason": error}
    block = manifest.get("snapshot")
    summary = {
        "status": "ok",
        "profile": manifest.get("profile"),
        "label": manifest.get("label"),
        "package_schema_version": manifest.get("package_schema_version"),
        "memory_schema_version": manifest.get("memory_schema_version"),
        "created_at": manifest.get("created_at"),
        "entry_count": manifest.get("entry_count"),
        "total_bytes": manifest.get("total_bytes"),
        "entries": [record.get("name") for record in manifest.get("entries") or []],
        "limitations": list(manifest.get("limitations") or []),
    }
    if isinstance(block, dict):
        summary["snapshot"] = {
            "model": block.get("model"),
            "identity": block.get("identity"),
            "runs": [run.get("run_id") for run in block.get("runs") or []],
        }
    return summary


# -- staging, recovery and rollback --------------------------------------


def stage_package(
    package_path: str, staging_root: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """Validate a package and extract it into a fresh isolated staging root.

    Returns ``(staging_dir, manifest, reason)``. Nothing is extracted until the
    whole archive has validated, and nothing is ever extracted over existing
    content: the staging directory is created fresh and refuses to be reused.
    """
    manifest, entries, error = read_package(package_path)
    if manifest is None:
        return None, None, error
    if not isinstance(staging_root, str) or not staging_root:
        return None, None, REASON_STAGING_UNUSABLE
    try:
        os.makedirs(staging_root, exist_ok=True)
        staging_dir = tempfile.mkdtemp(prefix="memory-stage-", dir=staging_root)
    except OSError:
        return None, None, REASON_STAGING_UNUSABLE
    for name, data in sorted(entries.items()):
        if name == MANIFEST_NAME:
            continue
        target = os.path.join(staging_dir, *name.split("/"))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(data)
        except OSError:
            return None, None, REASON_STAGING_UNUSABLE
    # The manifest is kept beside the staged content (outside the entry
    # namespace) so recovery can re-verify the staged bytes against the snapshot
    # the package declared rather than trusting what it read earlier.
    try:
        with open(os.path.join(staging_dir, STAGED_MANIFEST_NAME), "wb") as handle:
            handle.write(canonical(manifest).encode("utf-8"))
    except OSError:
        return None, None, REASON_STAGING_UNUSABLE
    return staging_dir, manifest, None


def _staged_store(data: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse and migrate one staged store.

    Migration runs on a copy taken from the staging directory, never on anything
    a caller owns, and a staged store that does not parse or does not name a run
    is refused rather than repaired.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, REASON_NOT_A_STORE
    store, _error = memory.migrate_memory(raw)
    if store is None:
        return None, REASON_NOT_A_STORE
    run = store.get("agent_run")
    if not isinstance(run, dict) or not isinstance(run.get("id"), str) or not run["id"]:
        return None, REASON_NOT_A_STORE
    return store, None


def verify_store(store: Dict[str, Any]) -> Optional[str]:
    """Return why a loaded store is not internally consistent, or ``None``.

    Identity, reference and replay integrity are checked here, and the check is
    deliberately *complete*: every reference the contract guarantees must resolve
    is verified, so a reference that escapes its own store is caught rather than
    silently tolerated. That completeness is what makes
    :func:`verify_independence` a proof rather than an assertion.

    ``rejection.event_ref`` is not required to resolve — it names the event the
    contract refused, which by definition was never stored — and a correction's
    optional ``target.record_id`` is context rather than a binding, so neither is
    treated as a reference the store owes.
    """
    run = store.get("agent_run")
    if not isinstance(run, dict):
        return REASON_NOT_A_STORE
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id:
        return REASON_NOT_A_STORE
    identifiers = {}
    for array in memory.STORE_ARRAYS:
        records = store.get(array)
        if not isinstance(records, list):
            return REASON_NOT_A_STORE
        identifiers[array] = {
            record.get("id") for record in records if isinstance(record, dict)
        }

    # The run's own descriptors.
    for key, array in (("project_id", "projects"),
                       ("work_package_id", "work_packages")):
        value = run.get(key)
        if value is not None and value not in identifiers[array]:
            return REASON_NOT_A_STORE

    # Every run-scoped record belongs to this run, and nothing else does.
    for array, kind in (("events", "events"), ("evidence", "evidence"),
                        ("decisions", "decisions"), ("change_sets", "change_sets"),
                        ("code_entity_links", "code_entity_links"),
                        ("rejections", "rejections"), ("quarantines", "quarantines"),
                        ("generated_documents", "generated_documents"),
                        ("corrections", "corrections")):
        for record in store.get(array, []):
            if not isinstance(record, dict):
                return REASON_NOT_A_STORE
            if record.get("run_id") != run_id:
                return REASON_NOT_A_STORE

    for record in store.get("work_packages", []):
        project_id = record.get("project_id")
        if project_id is not None and project_id not in identifiers["projects"]:
            return REASON_NOT_A_STORE
    for record in store.get("quarantines", []):
        event_id = record.get("event_id")
        if event_id is not None and event_id not in identifiers["events"]:
            return REASON_NOT_A_STORE
    for record in store.get("events", []):
        for key, array in (("change_set_id", "change_sets"),
                           ("evidence_ids", "evidence"),
                           ("decision_ids", "decisions"),
                           ("code_entity_link_ids", "code_entity_links")):
            value = record.get(key)
            refs = value if isinstance(value, list) else [value]
            for reference in refs:
                if reference is not None and reference not in identifiers[array]:
                    return REASON_NOT_A_STORE
    for record in store.get("corrections", []):
        for parent in record.get("supersedes") or []:
            if parent not in identifiers["corrections"]:
                return REASON_NOT_A_STORE
    for record in store.get("generated_documents", []):
        if not isinstance(record.get("content"), dict):
            return REASON_NOT_A_STORE
    return None


def verify_independence(stores: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Return why a set of stores is *not* independent, or ``None``.

    Stores are independent when every reference each one makes resolves inside
    itself. Two runs of one project share a project and work-package *id* — the
    id is derived from the adapter and the source's own identifier — but each
    store carries its own descriptor record and resolves the reference locally,
    so sharing a spelling is not a dependency. A reference that did not resolve
    locally would be a dependency on another store, and that is what this refuses.
    """
    for run_id, store in stores.items():
        if verify_store(store) is not None:
            return REASON_CROSS_STORE
        run = store.get("agent_run")
        if run.get("id") != run_id:
            return REASON_CROSS_STORE
    return None


# -- snapshot ------------------------------------------------------------


def _store_identity(store: Dict[str, Any]) -> str:
    """Return the content identity of one store as the package will carry it."""
    return checksum_of(canonical(store).encode("utf-8"))


def capture_stores(
    base_dir: str, run_ids: Optional[List[str]] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read every run store in scope once and record exactly what was read.

    The capture is *content-addressed and stamped*: it holds each store's content
    identity and the storage owner's change-detector for the file it came from,
    so a later verification can prove nothing moved rather than assume it.

    Scope is either declared or enumerated, and the difference is deliberate. A
    caller that names runs has fixed the scope, so only those stores have to hold
    still. A caller that names none is asking for *the store root*, and that set
    is enumerated through :func:`hrca.memory_store.list_run_ids`, which refuses
    rather than skips: a run store that cannot be read is never quietly dropped
    from a package that will claim to cover the root.
    """
    if not isinstance(base_dir, str) or not base_dir:
        return None, REASON_ACTIVE_UNREADABLE

    declared = run_ids is not None
    if declared:
        if not isinstance(run_ids, (list, tuple)):
            return None, REASON_SNAPSHOT_MALFORMED
        selected = sorted({str(run_id) for run_id in run_ids})
    else:
        available, error = memory_store.list_run_ids(base_dir)
        if error is not None:
            return None, REASON_ACTIVE_UNREADABLE
        selected = available
    if not selected:
        return None, REASON_NO_RUNS

    stores: Dict[str, Dict[str, Any]] = {}
    identities: Dict[str, str] = {}
    stamps: Dict[str, str] = {}
    for run_id in selected:
        store, error = memory_store.load(base_dir, run_id)
        if store is None:
            return None, (
                REASON_ACTIVE_UNREADABLE if error is not None else REASON_RUN_MISSING
            )
        stores[run_id] = store
        identities[run_id] = _store_identity(store)
        stamps[run_id] = memory_store.store_stamp(base_dir, run_id)
    return {
        "base_dir": base_dir,
        "declared": declared,
        "run_ids": selected,
        "stores": stores,
        "identities": identities,
        "stamps": stamps,
    }, None


def verify_capture(capture: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Re-read every captured store and confirm none of them moved.

    Returns ``(snapshot, reason)``. A store whose content identity *or* whose
    change-detector differs from the capture is reported as an unstable snapshot;
    a capture whose scope was not declared is also refused when the store root no
    longer holds exactly the runs it read.

    Why equality at both ends is enough. A store is replaced whole by an atomic
    rename, so its stamp moves on every write; an unchanged stamp therefore means
    the file was not written at all between the two reads, and an unchanged
    identity confirms what was read either side is the same content. Every
    capture read happens before every verification read, so the windows in which
    each store is pinned to its captured content overlap in
    ``[last capture read, first verification read]`` — non-empty by construction.
    At that instant the whole captured set coexisted on disk, which is exactly
    what a snapshot claims. Note that content identity alone would *not* suffice:
    a re-import or a restore can legitimately write an earlier state back, and
    the stamp is what catches a store that moved while looking unchanged.
    """
    if not isinstance(capture, dict):
        return None, REASON_SNAPSHOT_MALFORMED
    stores = capture.get("stores")
    identities = capture.get("identities")
    stamps = capture.get("stamps")
    base_dir = capture.get("base_dir")
    if (
        not isinstance(stores, dict)
        or not isinstance(identities, dict)
        or not isinstance(stamps, dict)
    ):
        return None, REASON_SNAPSHOT_MALFORMED
    if not stores:
        return None, REASON_NO_RUNS

    for run_id in sorted(stores):
        current, error = memory_store.load(base_dir, run_id)
        if current is None:
            return None, REASON_SNAPSHOT_UNSTABLE
        if _store_identity(current) != identities.get(run_id):
            return None, REASON_SNAPSHOT_UNSTABLE
        if memory_store.store_stamp(base_dir, run_id) != stamps.get(run_id):
            return None, REASON_SNAPSHOT_UNSTABLE

    # An undeclared scope claims the whole store root, so the root must still
    # hold exactly the runs that were read: a run that appeared or vanished
    # during the capture would make the package an incomplete answer to the
    # question it was asked.
    if not capture.get("declared"):
        available, error = memory_store.list_run_ids(base_dir)
        if error is not None or available != capture.get("run_ids"):
            return None, REASON_RUN_SET_CHANGED

    # The captured set must be internally independent *and* internally consistent
    # before it can be called a snapshot at all.
    reason = verify_independence(stores)
    if reason is not None:
        return None, reason

    runs = [
        {
            "run_id": run_id,
            "entry": "%s%s.json" % (BACKUP_STORES_PREFIX, _entry_token(run_id)),
            "identity": identities[run_id],
        }
        for run_id in sorted(stores)
    ]
    identity = "snap:" + memory.sha256_hex(
        canonical({"model": SNAPSHOT_MODEL, "runs": runs}).encode("utf-8")
    )[:32]
    return {
        "model": SNAPSHOT_MODEL,
        "identity": identity,
        "runs": runs,
        "stores": stores,
    }, None


def snapshot_stores(
    base_dir: str, run_ids: Optional[List[str]] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Take one verifiable cross-run snapshot of the stores under ``base_dir``.

    This is the only way the backup profile obtains its content, so a backup can
    never be assembled from independently timed reads: the capture is verified
    against the stores before it is called a snapshot, and refused if any store
    moved, if the store root changed under an undeclared scope, or if any store
    carries a reference it cannot resolve inside itself.
    """
    capture, error = capture_stores(base_dir, run_ids)
    if capture is None:
        return None, error
    return verify_capture(capture)


def _active_identity(active_base: str, run_id: str) -> str:
    """Return a bounded identity for the active store of ``run_id``."""
    store, error = memory_store.load(active_base, run_id)
    if store is None:
        return "absent" if error is None else "unreadable"
    return checksum_of(canonical(store).encode("utf-8"))[:24]


def verify_staged_snapshot(
    staging_dir: str,
) -> Tuple[
    Optional[Dict[str, Any]], Optional[Dict[str, Dict[str, Any]]], Optional[str]
]:
    """Hold the staged content to the snapshot the package declared.

    Returns ``(manifest, stores, reason)``. Recovery never trusts the manifest it
    read earlier, so everything is re-derived from *one* read of the staging
    directory: the declared snapshot is checked against the staged bytes, the
    staged store set must be exactly the declared set, and every staged store
    must parse to the run its entry claims at exactly the content identity the
    snapshot declares. Because the stores are returned from the same read they
    were verified from, a caller cannot apply bytes it did not verify, and a
    staging directory that was altered, partially written or interrupted is
    refused before an active store is in scope at all.
    """
    manifest_path = os.path.join(staging_dir, STAGED_MANIFEST_NAME)
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError):
        return None, None, REASON_SNAPSHOT_MALFORMED
    if not isinstance(manifest, dict) or manifest.get("profile") != PROFILE_BACKUP:
        return None, None, REASON_NOT_A_STORE

    block = manifest.get("snapshot")
    declared_runs = block.get("runs") if isinstance(block, dict) else None
    if not isinstance(declared_runs, list) or not declared_runs:
        return None, None, REASON_SNAPSHOT_MALFORMED

    entries = []
    for run in declared_runs:
        entry = run.get("entry") if isinstance(run, dict) else None
        if name_error(entry) is not None or not entry.startswith(BACKUP_STORES_PREFIX):
            return None, None, REASON_SNAPSHOT_MISMATCH
        entries.append(entry)
    if len(set(entries)) != len(entries):
        return None, None, REASON_SNAPSHOT_MISMATCH

    # The staging directory must carry exactly the declared stores: an extra file
    # would be a store the snapshot does not vouch for, and a missing one is a
    # snapshot that cannot be honoured.
    root = os.path.join(staging_dir, BACKUP_STORES_PREFIX.rstrip("/"))
    try:
        staged = sorted(
            name
            for name in os.listdir(root)
            if name.endswith(".json") and os.path.isfile(os.path.join(root, name))
        )
    except OSError:
        return None, None, REASON_SNAPSHOT_MISMATCH
    if staged != sorted(entry.split("/", 1)[1] for entry in entries):
        return None, None, REASON_SNAPSHOT_MISMATCH

    pairs = []
    stores: Dict[str, Dict[str, Any]] = {}
    for run, entry in zip(declared_runs, entries):
        try:
            with open(os.path.join(staging_dir, *entry.split("/")), "rb") as handle:
                data = handle.read()
        except OSError:
            return None, None, REASON_SNAPSHOT_MISMATCH
        store, error = _staged_store(data)
        if store is None:
            return None, None, error
        run_id = store["agent_run"]["id"]
        # A staged store that names another run, or whose content is not the
        # identity the snapshot declares, is not this snapshot.
        if run_id != run.get("run_id") or _store_identity(store) != run.get("identity"):
            return None, None, REASON_SNAPSHOT_MISMATCH
        if run_id in stores:
            return None, None, REASON_SNAPSHOT_MISMATCH
        stores[run_id] = store
        pairs.append(({"name": entry, "checksum": checksum_of(data)}, data))

    reinstated, error = _snapshot_block(block, pairs)
    if reinstated is None:
        return None, None, error
    return manifest, stores, None


def plan_restore(
    staging_dir: str, active_base: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return a reviewable restore plan without touching the active store.

    The plan states, per run, what the package holds, what the active store
    currently is, and what a replacement would do — plus the active identity a
    caller must echo back before anything is written. The staged bytes are
    re-verified against the declared snapshot first, so an inconsistent or
    interrupted staging directory cannot reach a plan.
    """
    manifest, stores, snapshot_error = verify_staged_snapshot(staging_dir)
    if manifest is None:
        return None, snapshot_error
    if not isinstance(active_base, str) or not active_base:
        return None, REASON_ACTIVE_UNREADABLE

    entries = []
    for run_id in sorted(stores):
        store = stores[run_id]
        verification = verify_store(store)
        active = _active_identity(active_base, run_id)
        if verification is not None:
            action = "refused"
        elif active == "absent":
            action = "create"
        elif active == "unreadable":
            action = "refused"
        elif active == checksum_of(canonical(store).encode("utf-8"))[:24]:
            action = "identical"
        else:
            action = "replace"
        entries.append(
            {
                "run_id": run_id,
                "action": action,
                "reason": verification,
                "memory_schema_version": store.get("schema_version"),
                "run_state": memory.run_state(store),
                "events": len(store.get("events", [])),
                "corrections": len(store.get("corrections", [])),
                "generated_documents": len(store.get("generated_documents", [])),
                "active_identity": active,
            }
        )
    return {
        "profile": PROFILE_BACKUP,
        "staging_dir": staging_dir,
        "snapshot": manifest.get("snapshot"),
        "runs": entries,
        "restorable": bool(entries)
        and all(entry["action"] in ("create", "replace", "identical") for entry in entries),
        "limitations": [
            "a plan changes nothing: the active store is untouched until a caller "
            "echoes the exact active identity back",
            "a run whose staged store fails verification is refused, never repaired",
            "the staged bytes are re-verified against the declared snapshot before "
            "any plan is produced",
        ],
    }, None


def apply_restore(
    staging_dir: str,
    active_base: str,
    expected_active_identity: str,
    run_id: str,
    rollback_root: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Replace one run in the active store, gated on its exact prior identity.

    The prior store is preserved as rollback material *before* anything is
    written, the replacement goes through the storage owner's atomic write, and
    the result is re-read to prove the store reopens with the expected state. A
    stale expectation, an unreadable active store or a failed rollback write all
    leave the active store exactly as it was.
    """
    plan, error = plan_restore(staging_dir, active_base)
    if plan is None:
        return None, error
    entry = next(
        (item for item in plan["runs"] if item["run_id"] == run_id), None
    )
    if entry is None:
        return None, REASON_NOT_PLANNED
    if entry["action"] == "refused":
        return None, REASON_NOT_A_STORE
    if entry["active_identity"] != expected_active_identity:
        return None, REASON_ACTIVE_MISMATCH

    # The staged bytes are verified again here, in the same read that yields the
    # store about to be written, so what is applied is always what was proven.
    manifest, stores, error = verify_staged_snapshot(staging_dir)
    if manifest is None:
        return None, error
    store = stores.get(run_id)
    if store is None:
        return None, REASON_NOT_PLANNED

    rollback_path = None
    if entry["action"] == "replace":
        rollback_root = rollback_root or staging_dir
        try:
            os.makedirs(os.path.join(rollback_root, "rollback"), exist_ok=True)
            current, _load_error = memory_store.load(active_base, run_id)
            if current is None:
                return None, REASON_ACTIVE_UNREADABLE
            rollback_path = os.path.join(
                rollback_root, "rollback", "%s.json" % _entry_token(run_id)
            )
            with open(rollback_path, "wb") as handle:
                handle.write(canonical(current).encode("utf-8"))
        except OSError:
            return None, REASON_ROLLBACK_FAILED

    save_error = memory_store.save(active_base, run_id, store)
    if save_error is not None:
        return None, REASON_ACTIVE_UNREADABLE

    reopened, load_error = memory_store.load(active_base, run_id)
    if reopened is None:
        return None, REASON_ACTIVE_UNREADABLE
    verification = verify_store(reopened)
    if verification is not None:
        return None, verification
    return {
        "run_id": run_id,
        "action": entry["action"],
        "state": memory.run_state(reopened),
        "schema_version": reopened.get("schema_version"),
        "rollback_path": rollback_path,
        "verified": True,
    }, None


def render(payload: Any) -> str:
    """Return the canonical serialization of a package report."""
    return memory.dumps(payload)
