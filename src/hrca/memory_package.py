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
    store: Any, run_id: str
) -> Tuple[Optional[List[Tuple[Dict[str, Any], bytes]]], Optional[str]]:
    """Build the lossless local-sensitive entries of one run.

    The store is written whole, because an exact restoration needs everything the
    contract already retains. Nothing new is collected: no source, no transcript,
    no credential, no filesystem content.
    """
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE
    token = _entry_token(run_id)
    payload = canonical(store).encode("utf-8")
    record, error = _entry("store", "%s%s.json" % (BACKUP_STORES_PREFIX, token), payload)
    if error is not None:
        return None, error
    note = _note(PROFILE_BACKUP, run_id).encode("utf-8")
    note_record, note_error = _entry("note", NOTE_NAME, note)
    if note_error is not None:
        return None, note_error
    return [(record, payload), (note_record, note)], None


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
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the canonical manifest for one package."""
    if profile not in PROFILES:
        return None, REASON_PROFILE_UNSUPPORTED
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
    return {
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
    }, None


def write_package(
    path: str,
    profile: str,
    entries: List[Tuple[Dict[str, Any], bytes]],
    created_at: Optional[str] = None,
) -> Optional[str]:
    """Write one deterministic package atomically; return a reason or ``None``."""
    manifest, error = build_manifest(profile, entries, created_at)
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
    return manifest, entries, None


def inspect(path: str) -> Dict[str, Any]:
    """Return a bounded, content-free summary of one package."""
    manifest, entries, error = read_package(path)
    if manifest is None:
        return {"status": "refused", "reason": error}
    return {
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
    return staging_dir, manifest, None


def _load_backup_stores(
    staging_dir: str,
) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Optional[str]]:
    """Load and migrate every store a staged backup carries, in the staging root."""
    stores: Dict[str, Dict[str, Any]] = {}
    root = os.path.join(staging_dir, BACKUP_STORES_PREFIX.rstrip("/"))
    if not os.path.isdir(root):
        return None, REASON_NOT_A_STORE
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return None, REASON_NOT_A_STORE
        # Migration runs on the staged copy, never on anything the caller owns.
        store, error = memory.migrate_memory(raw)
        if store is None:
            return None, REASON_NOT_A_STORE
        run = store.get("agent_run")
        if not isinstance(run, dict) or not isinstance(run.get("id"), str):
            return None, REASON_NOT_A_STORE
        stores[run["id"]] = store
    if not stores:
        return None, REASON_NOT_A_STORE
    return stores, None


def verify_store(store: Dict[str, Any]) -> Optional[str]:
    """Return why a loaded store is not internally consistent, or ``None``.

    Identity, reference and replay integrity are checked here: an event must
    belong to its run, every child reference must resolve, a correction must name
    revisions that exist, and a generated version must name its own document.
    """
    run = store.get("agent_run")
    if not isinstance(run, dict):
        return REASON_NOT_A_STORE
    run_id = run.get("id")
    identifiers = {}
    for array in memory.STORE_ARRAYS:
        records = store.get(array)
        if not isinstance(records, list):
            return REASON_NOT_A_STORE
        identifiers[array] = {
            record.get("id") for record in records if isinstance(record, dict)
        }
    for record in store.get("events", []):
        if record.get("run_id") != run_id:
            return REASON_NOT_A_STORE
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
        if record.get("run_id") != run_id:
            return REASON_NOT_A_STORE
        for parent in record.get("supersedes") or []:
            if parent not in identifiers["corrections"]:
                return REASON_NOT_A_STORE
    for record in store.get("generated_documents", []):
        if record.get("run_id") != run_id:
            return REASON_NOT_A_STORE
        if not isinstance(record.get("content"), dict):
            return REASON_NOT_A_STORE
    return None


def _active_identity(active_base: str, run_id: str) -> str:
    """Return a bounded identity for the active store of ``run_id``."""
    store, error = memory_store.load(active_base, run_id)
    if store is None:
        return "absent" if error is None else "unreadable"
    return checksum_of(canonical(store).encode("utf-8"))[:24]


def plan_restore(
    staging_dir: str, active_base: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return a reviewable restore plan without touching the active store.

    The plan states, per run, what the package holds, what the active store
    currently is, and what a replacement would do — plus the active identity a
    caller must echo back before anything is written.
    """
    stores, error = _load_backup_stores(staging_dir)
    if stores is None:
        return None, error
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
        "runs": entries,
        "restorable": bool(entries)
        and all(entry["action"] in ("create", "replace", "identical") for entry in entries),
        "limitations": [
            "a plan changes nothing: the active store is untouched until a caller "
            "echoes the exact active identity back",
            "a run whose staged store fails verification is refused, never repaired",
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

    stores, error = _load_backup_stores(staging_dir)
    if stores is None:
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
