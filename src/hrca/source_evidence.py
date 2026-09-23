"""Source-evidence read model (B-T2A).

This module owns the *vocabulary and the reads* for the source-evidence facts
that authoring consumes out of a Twin-produced evidence document: the workspace
baseline (``workspace_id``, ``scan_generation``, ``baseline_fingerprint``) and
the source artifacts themselves (identity, kind, path, module, name, locator,
fingerprint).

Why it exists
-------------

Authoring already read those facts, but it read them by importing
:mod:`hrca.twin` — the capability that *produced* the document — to reach a
handful of values that are not Twin knowledge. That import edge made an
advisory read model depend on a whole capability. Moving the reads behind this
module leaves the reader's dependency expressed as what it actually needs:
one vocabulary, imported by both sides.

What it is, and is not
----------------------

It is a **pure read model over a mapping someone else hands it**. It is not a
document schema: it declares no schema version, registers no migration,
validates no version, and owns no store. It performs no I/O, opens no file,
reads no clock, spawns nothing, and reaches no provider, credential, runner,
container or network. It imports only the standard library and
:mod:`hrca.core.identity` — the same identifiers the records it reads were
built from — and it deliberately does not import :mod:`hrca.twin`.

The Twin store's *schema version* and its migration registry stay Twin's, and
they are read as such: this module never names them. The scanner document has
its own accepted seam (:mod:`hrca.source.scanner`), so this module reads the
Twin-produced store only and never the scan document.

Accessors return the stored value verbatim
------------------------------------------

No accessor normalises, coerces, renames or defaults a value. What a record
holds is what an accessor returns, including ``None`` when the key is absent
and whatever a malformed record put there instead. That is deliberate: a
caller's comparison must remain exactly the comparison it would have made
against the raw record, so moving a read behind this seam can never change
which refusal a caller reaches. Only two reads apply a rule of their own, and
both are stated below: :func:`artifacts` filters to records that carry an
identity, and :func:`is_file_artifact` compares a kind.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .core.identity import ARTIFACT_FILE

# Confidence levels carried through from scanner evidence. They live here rather
# than in :mod:`hrca.twin` because both sides consume them: the Twin stamps them
# on the records it builds, and the advisory impact proposal reports one as its
# own envelope's confidence. The values are byte-preserved — they appear
# verbatim in stored records and serialized proposals. ``hrca.twin`` re-exports
# both, so ``twin.CONF_HIGH`` is this same object.
CONF_HIGH = "high"
CONF_LOW = "low"


def _mapping(value: Any) -> Dict[str, Any]:
    """Return ``value`` itself when it is a mapping, else an empty mapping."""
    return value if isinstance(value, dict) else {}


# -- workspace baseline ----------------------------------------------------


def workspace_revision(store: Any) -> Optional[Dict[str, Any]]:
    """Return the workspace-baseline record, or ``None`` when there is none.

    ``None`` is the *absence of a baseline*, which is a different answer from a
    baseline whose facts disagree: a document that cannot say which revision it
    is is not evidence for a particular workspace, and a caller that needs to
    tell those apart asks this question first.
    """
    revision = _mapping(store).get("workspace_revision")
    return revision if isinstance(revision, dict) else None


def workspace_id(store: Any) -> Any:
    """Return the workspace identity the document declares, verbatim."""
    return _mapping(workspace_revision(store)).get("workspace_id")


def scan_generation(store: Any) -> Any:
    """Return the scan generation the document declares, verbatim."""
    return _mapping(workspace_revision(store)).get("scan_generation")


def baseline_fingerprint(store: Any) -> Any:
    """Return the baseline fingerprint the document declares, verbatim.

    This is a *read* of a fingerprint the document carries. It computes
    nothing; :func:`hrca.core.identity.baseline_fingerprint` is the unrelated
    constructor that derives one from a set of file fingerprints.
    """
    return _mapping(workspace_revision(store)).get("baseline_fingerprint")


# -- source artifacts ------------------------------------------------------


def artifacts(store: Any) -> List[Dict[str, Any]]:
    """Return the source-artifact records that carry an identity, in order.

    A record is returned only when it is a mapping holding a non-empty string
    ``id``: a record with no identity cannot be bound, compared or named, so it
    is not evidence. Document order is preserved exactly, because a later read
    that keeps the first record for an identity depends on that order.
    """
    out: List[Dict[str, Any]] = []
    for record in _mapping(store).get("artifacts") or []:
        if (
            isinstance(record, dict)
            and isinstance(record.get("id"), str)
            and record["id"]
        ):
            out.append(record)
    return out


def artifact_id(artifact: Any) -> Any:
    """Return the artifact's own identity, verbatim."""
    return _mapping(artifact).get("id")


def artifact_kind(artifact: Any) -> Any:
    """Return the artifact's kind, verbatim."""
    return _mapping(artifact).get("kind")


def artifact_path(artifact: Any) -> Any:
    """Return the artifact's workspace-relative path, verbatim."""
    return _mapping(artifact).get("path")


def artifact_module(artifact: Any) -> Any:
    """Return the artifact's module, verbatim."""
    return _mapping(artifact).get("module")


def artifact_name(artifact: Any) -> Any:
    """Return the artifact's name, verbatim."""
    return _mapping(artifact).get("name")


def artifact_locator(artifact: Any) -> Any:
    """Return the artifact's locator, verbatim.

    A file artifact carries none, so this returns ``None`` for one rather than
    inventing a locator from its path.
    """
    return _mapping(artifact).get("locator")


def artifact_fingerprint(artifact: Any) -> Any:
    """Return the artifact's content fingerprint, verbatim.

    A symbol artifact carries none, so this returns ``None`` for one rather
    than deriving a fingerprint from the file that declares it.
    """
    return _mapping(artifact).get("fingerprint")


def is_file_artifact(artifact: Any) -> bool:
    """Return whether the artifact is a whole-file artifact.

    This is the one comparison the seam applies for a caller, because "this
    artifact is a file" is a fact about the artifact and every reader that
    needs it needs the same answer. A record of any other shape is not a file
    artifact.
    """
    return artifact_kind(artifact) == ARTIFACT_FILE


__all__ = [
    "CONF_HIGH",
    "CONF_LOW",
    "workspace_revision",
    "workspace_id",
    "scan_generation",
    "baseline_fingerprint",
    "artifacts",
    "artifact_id",
    "artifact_kind",
    "artifact_path",
    "artifact_module",
    "artifact_name",
    "artifact_locator",
    "artifact_fingerprint",
    "is_file_artifact",
]
