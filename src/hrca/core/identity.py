"""Pure, stable identities (B1).

This module owns the deterministic digests, content fingerprints and stable
identifier constructors that more than one capability binds evidence by, plus
the one artifact-kind constant those identifiers are built from. It is not a
general utility module and must not become one.

**Admission rule.** A symbol belongs here only when it is a *pure function from
bytes or strings to a stable digest or identifier*, or a constant naming an
artifact kind such a function is built from. It takes no store, holds no state,
applies no policy, reads no clock, performs no I/O, and imports no other
``hrca`` module. A candidate symbol that fails that sentence belongs in
:mod:`hrca.storage` (I/O), :mod:`hrca.contract` (protocol), or the capability
that owns it.

Why it exists
-------------

These functions were first written inside :mod:`hrca.twin`, which is where
their first consumer needed them. The Twin derives every identifier a record
carries from them — but so does every identifier a *candidate*, a *typed
intent*, a *validation plan* or an *impact proposal* carries. Eight modules
that are not Twin modules therefore imported :mod:`hrca.twin` to reach a
SHA-256 wrapper, which is a dependency on a capability where only an identity
was meant. Relocating them here leaves ``hrca.twin`` re-exporting every name
below, so those importers move without any behaviour moving with them.

What is deliberately *not* here
-------------------------------

The Twin's own vocabulary stays in :mod:`hrca.twin`: the confidence levels
(``CONF_*``), the symbol artifact taxonomy (``ARTIFACT_CLASS``,
``ARTIFACT_FUNCTION``, ``ARTIFACT_METHOD`` and ``ARTIFACT_KINDS``), the
provenance and synchronization states, the behavior categories, and the store
schema version with its migration registry. None of those is an identity, and
gathering them here is exactly how a "primitives" module becomes the
miscellaneous drawer the admission rule above exists to prevent.
``ARTIFACT_FILE`` is the single exception, because a file *identifier* and a
file *kind* are the same concept and the identifier constructor needs it.

Every value is byte-preserved. A stored identifier computed before this module
existed still matches one computed after it, and ``twin.sha256_hex`` is the
same object as :func:`identity.sha256_hex`, not a copy of it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict, Optional

# The artifact kind a *file* artifact carries. Shared rather than Twin-owned
# because ``file_artifact_id`` is built from it; the symbol kinds stay in
# :mod:`hrca.twin` with the taxonomy they belong to. The value is byte-preserved
# — it appears verbatim in stored artifact records.
ARTIFACT_FILE = "file"


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def fingerprint_bytes(data: bytes) -> str:
    """Return a content fingerprint (SHA-256 hex) for raw source ``data``."""
    return sha256_hex(data)


def fingerprint_source(source: str) -> str:
    """Return a content fingerprint for ``source`` text (UTF-8 encoded)."""
    return fingerprint_bytes(source.encode("utf-8"))


# -- deterministic identifiers -------------------------------------------

# Identifiers are stable and deterministic. Symbol identifiers keep the
# scanner's ``module.path.Class.method`` locator; file identifiers use the
# portable root-relative path. A formatting-only change never changes any
# identifier because they are derived from the path and qualified name, never
# from content.


def workspace_id_for(canonical_root: str) -> str:
    """Return the canonical workspace identifier for a canonical root path."""
    return "ws:" + sha256_hex(canonical_root.encode("utf-8"))


def _portable(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


def file_artifact_id(rel_path: str) -> str:
    """Return the SourceArtifact id for a file (``artifact:file:<path>``)."""
    return f"artifact:file:{_portable(rel_path)}"


def symbol_artifact_id(qname: str, kind: str) -> str:
    """Return the SourceArtifact id for a symbol (``artifact:<kind>:<qname>``)."""
    return f"artifact:{kind}:{qname}"


# -- baseline fingerprint ------------------------------------------------

def baseline_fingerprint(file_fingerprints: Dict[str, Optional[str]]) -> str:
    """Return a deterministic fingerprint over ``{path: fingerprint}``.

    The baseline captures *which* supported files exist and *what* content each
    has, so a content change, an addition, or a removal always changes the
    fingerprint while a byte-identical rescan keeps it unchanged.
    """
    pairs = [(p, fp) for p, fp in sorted(file_fingerprints.items())]
    canon = json.dumps(pairs, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256_hex(canon.encode("utf-8"))
