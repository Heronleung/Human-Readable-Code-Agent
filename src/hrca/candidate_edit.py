"""Typed candidate edit request (P5.4).

The versioned, canonical, non-executable record of **what a developer wants
changed**: a list of whole-file replacements, each naming one exact
repository-relative path, the SHA-256 the file is expected to have now, and the
complete UTF-8 text it should have instead.

This module is pure. It performs no filesystem access, no network call, no
provider or credential access, no command execution and no Git operation, and
it writes nothing. It decides only whether a *request* is well formed and
whether every identifier in it is one this contract is willing to name. Whether
a request is *authorized* against accepted evidence is a separate question,
answered in :mod:`hrca.candidate`, which is also the only module allowed to
touch a filesystem.

Why the grammar is this narrow
------------------------------

The narrowest grammar that can still express a real change is *exact whole-file
replacement of an existing UTF-8 Python source file*. Everything else is
refused by name rather than ignored, so a caller who tries to delete, rename,
chmod, link, patch or create learns which capability is missing instead of
guessing at a validation error:

* **Creation is unsupported**, not merely unimplemented. A new file has no
  predecessor artifact, so it has no exact identity to bind a scope, a
  fingerprint or an evidence reference to; authorizing one would mean inventing
  the scope that requirement 6 forbids this contract from inventing.
* **Deletion, rename, move and mode changes are unsupported.** A withdrawal or
  a relocation is not a replacement, and representing one as the other would
  misdescribe what happened.
* **Symlinks, hardlinks and submodules are unsupported.** They make a path name
  something other than the bytes it appears to contain.
* **Patch and unified-diff input is unsupported.** An arbitrary patch parser is
  a second, unbounded input language; this contract accepts complete content
  only, so the desired bytes are always fully known before anything is written.
* **Binary content is unsupported.** A payload that is not decodable UTF-8 is a
  refusal here, because there is no text to reason about; a decodable payload
  that carries a NUL byte or a byte-order mark is content this contract will not
  render as a text diff, and :mod:`hrca.candidate` reports that as the
  ``unsupported_content`` state rather than as a malformed request.

Paths are single-valued
-----------------------

A path is accepted only in one exact spelling: forward slashes, no leading or
trailing separator, no empty, ``.`` or ``..`` component, no drive letter, no
UNC prefix, no control character, no trailing dot or space in a component, no
reserved device name, at most :data:`MAX_PATH_COMPONENTS` components, and
already in Unicode NFC. Two operations that differ only by case, or whose paths
are not already normal, are refused: on a case-insensitive or
normalization-insensitive filesystem they would name one file twice, and which
one the developer meant would not be knowable.
"""

from __future__ import annotations

import base64
import binascii
import json
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import twin

CANDIDATE_EDIT_SCHEMA_VERSION = "1.0.0"
CANDIDATE_EDIT_GENERATOR = "hrca-candidate-edit"
EDIT_ID_PREFIX = "edit:"

# The one operation this contract supports.
OP_REPLACE_FILE = "replace_file"

# Operations that are known, named and deliberately unsupported. Naming them
# turns "unknown operation" into a statement about *why* the capability is
# absent, which is the honest answer.
UNSUPPORTED_OPERATIONS: Dict[str, str] = {
    "create_file": "new-file creation is unsupported",
    "new_file": "new-file creation is unsupported",
    "add_file": "new-file creation is unsupported",
    "delete_file": "deletion is unsupported",
    "remove_file": "deletion is unsupported",
    "rename_file": "rename is unsupported",
    "move_file": "move is unsupported",
    "copy_file": "copy is unsupported",
    "chmod": "mode changes are unsupported",
    "set_mode": "mode changes are unsupported",
    "chown": "ownership changes are unsupported",
    "symlink": "symlinks are unsupported",
    "hardlink": "hardlinks are unsupported",
    "submodule": "submodules are unsupported",
    "run_command": "commands are unsupported",
    "apply_patch": "arbitrary patch input is unsupported",
    "apply_diff": "unified-diff input is unsupported",
    "patch": "arbitrary patch input is unsupported",
}

# Bounded limits. Every one is a refusal, never a truncation.
MAX_OPERATIONS = 32
MAX_PATH_CHARS = 512
MAX_PATH_COMPONENT_CHARS = 128
MAX_PATH_COMPONENTS = 32
MAX_FILE_BYTES = 64 * 1024
MAX_DIFF_LINES = 2000

# Only files the Twin models can carry an exact fingerprint and an exact
# evidence binding, so only these can ever be authorized. Widening this would
# let a request name a file no accepted evidence describes.
ALLOWED_SUFFIXES = (".py", ".pyi")

# Mirrors the reserved-device list the document contract already refuses
# (``hrca.document._WINDOWS_RESERVED_BASE_NAMES``). A file whose base name is a
# device name is one that cannot be created or read portably.
_RESERVED_BASE_NAMES = frozenset(
    {
        "con", "prn", "aux", "nul",
        "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    }
)

# Bounded reasons. Field names come from this module's own vocabulary; a caller
# value is never interpolated.
REASON_NOT_MAPPING = "edit request is not a mapping"
REASON_MISSING_VERSION = "missing schema_version"
REASON_INVALID_VERSION = "invalid schema_version"
REASON_NEWER_VERSION = "schema_version is newer than supported"
REASON_NOT_MIGRATABLE = "schema_version is not migratable"
REASON_NO_OPERATIONS = "edit request has no operations"
REASON_TOO_MANY_OPERATIONS = "edit request has too many operations"
REASON_OPERATION_NOT_MAPPING = "an operation is not a mapping"
REASON_UNKNOWN_OPERATION = "operation is not supported"
REASON_DUPLICATE_PATH = "duplicate operation for the same path"
REASON_CASE_COLLISION = "two operations differ only by case"
REASON_PATH_INVALID = "path is not an exact repository-relative path"
REASON_PATH_ABSOLUTE = "absolute paths are not accepted"
REASON_PATH_TRAVERSAL = "path traversal is not accepted"
REASON_PATH_SEPARATOR = "a path must use forward slashes only"
REASON_PATH_COMPONENT = "a path component is empty, dotted or padded"
REASON_PATH_RESERVED = "a path component names a reserved device"
REASON_PATH_SUFFIX = "only Python source files may be replaced"
REASON_PATH_OVERLONG = "path is too long"
REASON_PATH_NOT_NORMAL = "path is not in Unicode normal form"
REASON_BOTH_CONTENT = "an operation supplied both text and bytes_b64"
REASON_NO_CONTENT = "an operation supplied no replacement content"
REASON_MISSING_EXPECTED = "missing expected_sha256"
REASON_EXPECTED_INVALID = "expected_sha256 is not a lowercase SHA-256 digest"
REASON_BINDING_INVALID = "edit request is missing or malformed"
REASON_MISSING_BINDING = "edit request is missing a required field"
REASON_CONTENT_NOT_UTF8 = "replacement content is not valid UTF-8"
REASON_CONTENT_OVERSIZED = "replacement content is larger than the accepted bound"

_BUILDING_BINDING_FIELDS = (
    "intent_delta_id",
    "proposal_id",
    "binding_fingerprint",
    "baseline",
)

_VERSION_KEYS = (
    "workspace_id",
    "scan_generation",
    "baseline_fingerprint",
    "scanner_schema_version",
    "grammar",
)

# No historical versions exist before 1.0.0; the registry is here so a later
# phase can add an upgrade step without changing the read path. A future or
# unknown version is never migrated and never half-read.
MIGRATIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def dumps(obj: Any) -> str:
    """Serialize an edit request canonically (sorted keys, compact, ASCII-safe)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


class _Refusal(ValueError):
    """Internal: a bounded refusal carrying a safe reason string."""


# -- path policy -----------------------------------------------------------


def normalize_edit_path(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(path, reason)`` for one exact repository-relative path.

    A path is accepted only in a single exact spelling; everything ambiguous is
    refused rather than repaired, because repairing a path is how a request ends
    up naming a file the developer did not write.
    """
    if not isinstance(raw, str) or not raw:
        return None, REASON_PATH_INVALID
    if len(raw) > MAX_PATH_CHARS:
        return None, REASON_PATH_OVERLONG

    # A control character, a NUL or a surrogate has no place in a path and no
    # portable reading.
    for char in raw:
        if ord(char) < 32 or ord(char) == 127:
            return None, REASON_PATH_INVALID

    # Absolute, drive-qualified and UNC spellings, in either separator.
    if raw.startswith("/") or raw.startswith("\\"):
        return None, REASON_PATH_ABSOLUTE
    if len(raw) >= 2 and raw[1] == ":":
        return None, REASON_PATH_ABSOLUTE

    # Exactly one separator style. A backslash is a legal filename character on
    # POSIX and a separator on Windows, so accepting it would make one request
    # mean two different files.
    if "\\" in raw:
        return None, REASON_PATH_SEPARATOR

    if raw.endswith("/"):
        return None, REASON_PATH_COMPONENT

    components = raw.split("/")
    if len(components) > MAX_PATH_COMPONENTS:
        return None, REASON_PATH_OVERLONG
    for component in components:
        if not component or component in (".", ".."):
            return None, REASON_PATH_COMPONENT
        if len(component) > MAX_PATH_COMPONENT_CHARS:
            return None, REASON_PATH_OVERLONG
        if component != component.strip():
            return None, REASON_PATH_COMPONENT
        if component.endswith((".", " ")):
            return None, REASON_PATH_COMPONENT

    # Unicode normal form: a decomposed spelling and a composed spelling are two
    # byte strings that many filesystems treat as one name.
    if unicodedata.normalize("NFC", raw) != raw:
        return None, REASON_PATH_NOT_NORMAL

    return raw, None


def _check_component_names(components: List[str]) -> Optional[str]:
    for component in components:
        base = component.rsplit(".", 1)[0] if "." in component else component
        if base.lower() in _RESERVED_BASE_NAMES:
            return REASON_PATH_RESERVED
    return None


def _check_suffix(path: str) -> Optional[str]:
    lowered = path.lower()
    if lowered.endswith(ALLOWED_SUFFIXES):
        # The suffix must be spelled exactly, not merely case-insensitively:
        # ``.PY`` and ``.py`` are one file on a case-insensitive filesystem and
        # two on a case-sensitive one, so neither spelling is exact.
        if not path.endswith(ALLOWED_SUFFIXES):
            return REASON_PATH_SUFFIX
        return None
    return REASON_PATH_SUFFIX


# -- content policy --------------------------------------------------------


def _decode_text(raw: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(text, reason)`` for one operation's replacement content.

    Text and base64 bytes are two spellings of one payload; supplying both is
    refused because the two could disagree, and a request whose content is
    ambiguous is not a request this contract can carry out.

    A payload that cannot be *decoded* — malformed base64, or bytes that are not
    UTF-8 — is a refusal, because then there is no text to have an opinion
    about. Whether the decoded text is reviewable *as a diff* is a separate
    question answered in :mod:`hrca.candidate`, which reports a NUL byte or a
    byte-order mark as the ``unsupported_content`` state rather than treating a
    developer's content as a malformed request.
    """
    has_text = "text" in raw
    has_bytes = "bytes_b64" in raw
    if has_text and has_bytes:
        return None, REASON_BOTH_CONTENT
    if not has_text and not has_bytes:
        return None, REASON_NO_CONTENT

    if has_text:
        text = raw["text"]
        if not isinstance(text, str):
            return None, REASON_CONTENT_NOT_UTF8
    else:
        encoded = raw["bytes_b64"]
        if not isinstance(encoded, str):
            return None, REASON_CONTENT_NOT_UTF8
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None, REASON_CONTENT_NOT_UTF8
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, REASON_CONTENT_NOT_UTF8

    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        return None, REASON_CONTENT_OVERSIZED
    return text, None


def _check_expected(raw: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    if "expected_sha256" not in raw:
        return None, REASON_MISSING_EXPECTED
    value = raw["expected_sha256"]
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        return None, REASON_EXPECTED_INVALID
    return value, None


# -- baseline --------------------------------------------------------------


def _normalize_baseline(value: Any) -> Dict[str, Any]:
    """Validate the accepted baseline identity an edit is authored against."""
    if not isinstance(value, dict):
        raise _Refusal(REASON_MISSING_BINDING)
    for key in _VERSION_KEYS:
        if key not in value:
            raise _Refusal(REASON_MISSING_BINDING)
    generation = value["scan_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise _Refusal(REASON_MISSING_BINDING)
    grammar = value["grammar"]
    if (
        not isinstance(grammar, dict)
        or not isinstance(grammar.get("implementation"), str)
        or not isinstance(grammar.get("version"), str)
    ):
        raise _Refusal(REASON_MISSING_BINDING)
    for key in ("workspace_id", "baseline_fingerprint", "scanner_schema_version"):
        if not isinstance(value[key], str) or not value[key]:
            raise _Refusal(REASON_MISSING_BINDING)
    return {
        "workspace_id": value["workspace_id"],
        "scan_generation": generation,
        "baseline_fingerprint": value["baseline_fingerprint"],
        "scanner_schema_version": value["scanner_schema_version"],
        "grammar": {
            "implementation": grammar["implementation"],
            "version": grammar["version"],
        },
    }


def _normalize_identifiers(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field, prefix in (
        ("intent_delta_id", "intent:"),
        ("proposal_id", "impact:"),
        ("binding_fingerprint", "bind:"),
    ):
        value = raw.get(field)
        if not isinstance(value, str) or not value.startswith(prefix):
            raise _Refusal(REASON_MISSING_BINDING)
        out[field] = value
    return out


# -- building --------------------------------------------------------------


def edit_id_for(edit: Dict[str, Any]) -> str:
    """Return the content-addressed identity of an edit (never time-derived)."""
    canon = dumps({k: v for k, v in edit.items() if k != "edit_id"})
    return EDIT_ID_PREFIX + twin.sha256_hex(canon.encode("utf-8"))


def build_edit(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a typed edit request and return its canonical form.

    Returns ``(edit, error)``. A malformed request, an unsupported operation, an
    ambiguous or hostile path, or content that is not the complete UTF-8 text of
    a Python source file yields ``(None, bounded reason)``.
    """
    try:
        if not isinstance(raw, dict):
            raise _Refusal(REASON_NOT_MAPPING)

        identifiers = _normalize_identifiers(raw)
        baseline = _normalize_baseline(raw.get("baseline"))

        operations = raw.get("operations")
        if not isinstance(operations, list) or not operations:
            raise _Refusal(REASON_NO_OPERATIONS)
        if len(operations) > MAX_OPERATIONS:
            raise _Refusal(REASON_TOO_MANY_OPERATIONS)

        normalized: List[Dict[str, Any]] = []
        seen: Dict[str, str] = {}
        folded: Dict[str, str] = {}
        for entry in operations:
            if not isinstance(entry, dict):
                raise _Refusal(REASON_OPERATION_NOT_MAPPING)

            op = entry.get("op")
            if op != OP_REPLACE_FILE:
                if isinstance(op, str) and op in UNSUPPORTED_OPERATIONS:
                    raise _Refusal(UNSUPPORTED_OPERATIONS[op])
                raise _Refusal(REASON_UNKNOWN_OPERATION)

            path, reason = normalize_edit_path(entry.get("path"))
            if reason is not None:
                raise _Refusal(reason)
            reason = _check_component_names(path.split("/"))
            if reason is not None:
                raise _Refusal(reason)
            reason = _check_suffix(path)
            if reason is not None:
                raise _Refusal(reason)

            key = path.casefold()
            if path in seen:
                raise _Refusal(REASON_DUPLICATE_PATH)
            if key in folded and folded[key] != path:
                raise _Refusal(REASON_CASE_COLLISION)
            seen[path] = path
            folded[key] = path

            expected, reason = _check_expected(entry)
            if reason is not None:
                raise _Refusal(reason)

            text, reason = _decode_text(entry)
            if reason is not None:
                raise _Refusal(reason)

            normalized.append(
                {
                    "op": OP_REPLACE_FILE,
                    "path": path,
                    "expected_sha256": expected,
                    "text": text,
                }
            )

        normalized.sort(key=lambda record: record["path"])
        edit: Dict[str, Any] = {
            "schema_version": CANDIDATE_EDIT_SCHEMA_VERSION,
            "generator": CANDIDATE_EDIT_GENERATOR,
            "edit_id": "",
            **identifiers,
            "baseline": baseline,
            "operations": normalized,
            "executable": False,
            "applied": False,
        }
        edit["edit_id"] = edit_id_for(edit)
        return edit, None
    except _Refusal as exc:
        return None, str(exc)


def migrate_edit(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a serialized edit's declared version, fail-closed."""
    if not isinstance(raw, dict):
        return None, REASON_NOT_MAPPING
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, REASON_MISSING_VERSION
    try:
        current = _version_tuple(CANDIDATE_EDIT_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, REASON_INVALID_VERSION
    if found == current:
        return raw, None
    if found > current:
        return None, REASON_NEWER_VERSION
    if version not in MIGRATIONS:
        return None, REASON_NOT_MIGRATABLE
    return MIGRATIONS[version](dict(raw)), None


def validate_edit(edit: Any) -> Optional[str]:
    """Validate a built edit request against the P5.4 schema.

    A valid edit declares the current schema and generator, carries an
    ``edit:``-prefixed identity that matches its own content, is canonically
    normalized operation by operation, and is neither executable nor applied.
    """
    if not isinstance(edit, dict):
        return REASON_NOT_MAPPING
    migrated, error = migrate_edit(edit)
    if error is not None:
        return error
    if migrated is not edit:  # pragma: no cover - no migration is registered
        return REASON_BINDING_INVALID
    if edit.get("generator") != CANDIDATE_EDIT_GENERATOR:
        return REASON_BINDING_INVALID
    if edit.get("executable") is not False or edit.get("applied") is not False:
        return REASON_BINDING_INVALID
    for field in _BUILDING_BINDING_FIELDS:
        if field not in edit:
            return REASON_MISSING_BINDING
    try:
        _normalize_identifiers(edit)
        _normalize_baseline(edit.get("baseline"))
    except _Refusal as exc:
        return str(exc)

    operations = edit.get("operations")
    if not isinstance(operations, list) or not operations:
        return REASON_NO_OPERATIONS
    rebuilt, error = build_edit(
        {
            "intent_delta_id": edit["intent_delta_id"],
            "proposal_id": edit["proposal_id"],
            "binding_fingerprint": edit["binding_fingerprint"],
            "baseline": edit["baseline"],
            "operations": [
                {
                    key: record.get(key)
                    for key in ("op", "path", "expected_sha256", "text")
                }
                for record in operations
                if isinstance(record, dict)
            ],
        }
    )
    if error is not None:
        return error
    if rebuilt.get("operations") != operations:
        return "operations are not canonical"
    identity = edit.get("edit_id")
    if not isinstance(identity, str) or not identity.startswith(EDIT_ID_PREFIX):
        return "missing or malformed edit_id"
    if identity != edit_id_for(edit):
        return "edit_id does not match the edit content"
    return None


__all__ = [
    "CANDIDATE_EDIT_SCHEMA_VERSION",
    "CANDIDATE_EDIT_GENERATOR",
    "EDIT_ID_PREFIX",
    "OP_REPLACE_FILE",
    "UNSUPPORTED_OPERATIONS",
    "MAX_OPERATIONS",
    "MAX_PATH_CHARS",
    "MAX_PATH_COMPONENT_CHARS",
    "MAX_PATH_COMPONENTS",
    "MAX_FILE_BYTES",
    "MAX_DIFF_LINES",
    "ALLOWED_SUFFIXES",
    "REASON_NOT_MAPPING",
    "REASON_MISSING_VERSION",
    "REASON_INVALID_VERSION",
    "REASON_NEWER_VERSION",
    "REASON_NOT_MIGRATABLE",
    "REASON_NO_OPERATIONS",
    "REASON_TOO_MANY_OPERATIONS",
    "REASON_OPERATION_NOT_MAPPING",
    "REASON_UNKNOWN_OPERATION",
    "REASON_DUPLICATE_PATH",
    "REASON_CASE_COLLISION",
    "REASON_PATH_INVALID",
    "REASON_PATH_ABSOLUTE",
    "REASON_PATH_TRAVERSAL",
    "REASON_PATH_SEPARATOR",
    "REASON_PATH_COMPONENT",
    "REASON_PATH_RESERVED",
    "REASON_PATH_SUFFIX",
    "REASON_PATH_OVERLONG",
    "REASON_PATH_NOT_NORMAL",
    "REASON_BOTH_CONTENT",
    "REASON_NO_CONTENT",
    "REASON_MISSING_EXPECTED",
    "REASON_EXPECTED_INVALID",
    "REASON_BINDING_INVALID",
    "REASON_MISSING_BINDING",
    "REASON_CONTENT_NOT_UTF8",
    "REASON_CONTENT_OVERSIZED",
    "MIGRATIONS",
    "dumps",
    "normalize_edit_path",
    "build_edit",
    "migrate_edit",
    "validate_edit",
    "edit_id_for",
]
