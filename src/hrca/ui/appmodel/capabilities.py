"""The capabilities the desktop can actually reach, and their honest limits.

A capability is a named thing the product can really do, backed by one of the
boundary's own actions. The catalogue deliberately lists **only** what the
accepted boundary exposes to the desktop: nothing is invented, and nothing the
desktop cannot reach is offered as a control.

Every entry carries the exact boundary action-name string it dispatches, so a
test can prove the catalogue has not drifted from ``hrca.core.contract``. The
action names are held as literals rather than imported, because this package
stays pure — it must not depend on the contract module to be importable.

``Control`` describes what a job using the capability may be offered. A
capability that the backend cannot interrupt reports ``False`` for pause,
resume, cancel and reassign, so the workspace shows no control it cannot
honour rather than a button that does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import authority as _authority


@dataclass(frozen=True)
class Control:
    """Which lifecycle controls a capability can actually honour."""

    pause: bool = False
    resume: bool = False
    cancel: bool = False
    reassign: bool = False


NO_CONTROL = Control()

# The desktop's cancellation version 1 is terminate-and-restart of the single
# supervised backend process, which abandons the one in-flight request. A
# capability that is one bounded backend request can therefore be cancelled;
# pause, resume and reassign have no such mechanism, so they stay ``False``
# everywhere and the workspace renders no control for them.
CANCELLABLE = Control(cancel=True)


@dataclass(frozen=True)
class Capability:
    """One thing the product can do, and the honest bounds on it."""

    key: str
    label: str
    summary: str
    authority: str
    action: Optional[str]
    available: bool = True
    unavailable_reason: str = ""
    control: Control = NO_CONTROL
    evidence_hint: str = ""

    @property
    def is_protected(self) -> bool:
        """Whether using this capability needs an explicit confirmation."""
        return _authority.is_protected(self.authority)

    @property
    def disabled_reason(self) -> str:
        """Return why this capability cannot be used, or ``""`` if it can."""
        if self.available:
            return ""
        return self.unavailable_reason or "This capability is not available."


# ---------------------------------------------------------------------------
# The catalogue. Keys are stable identifiers used by plans and jobs.
# ---------------------------------------------------------------------------
CAPABILITIES: Tuple[Capability, ...] = (
    Capability(
        key="project.open",
        label="Open a project",
        summary="Bind the workspace to a repository root and read its recorded state.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="open_project",
        evidence_hint="The repository state the boundary reported for that root.",
    ),
    Capability(
        key="source.scan",
        label="Read-only source scan",
        summary="Parse the project's Python with the deterministic scanner and record the result.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="scan",
        control=CANCELLABLE,
        evidence_hint="The scan report and the source evidence it emitted.",
    ),
    Capability(
        key="document.read",
        label="Read a document",
        summary="Open a working document and its head revision.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="open_document",
    ),
    Capability(
        key="document.create",
        label="Create a document",
        summary="Add a new working document inside PrimaAgent.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="create_document",
    ),
    Capability(
        key="document.save",
        label="Save a document revision",
        summary="Append a new immutable revision to a working document.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="save_document",
        evidence_hint="The stored revision and its content fingerprint.",
    ),
    Capability(
        key="document.preview",
        label="Preview a document",
        summary="Read the candidate and accepted state bound to a document revision.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="preview_document",
    ),
    Capability(
        key="library.read",
        label="Read the document library",
        summary="List the folders and documents the product holds.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="get_library",
    ),
    Capability(
        key="library.organize",
        label="Organize the library",
        summary="Create folders, rename, move, trash and restore items. Trash stays recoverable.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="rename_item",
    ),
    Capability(
        key="candidate.create",
        label="Create a candidate",
        summary="Materialize the reviewable candidate for a document revision.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="create_candidate",
        evidence_hint="The candidate record and the revision it is bound to.",
    ),
    Capability(
        key="candidate.adopt",
        label="Adopt an accepted version",
        summary="Move an accepted version forward. This is an explicit human decision, never automatic.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="adopt_candidate",
        control=NO_CONTROL,
        evidence_hint="The accepted version record and the baseline it establishes.",
    ),
    Capability(
        key="version.read",
        label="Read accepted versions",
        summary="List a document's accepted versions and which one is current.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="list_versions",
    ),
    Capability(
        key="version.restore",
        label="Restore an accepted version",
        summary="Make an earlier accepted version current again.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="restore_version",
    ),
    Capability(
        key="rule_delta.prepare",
        label="Prepare an interpretation (offline)",
        summary="Assemble the disclosure and token for one rule-interpretation request. Sends nothing.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="prepare_rule_delta",
        control=CANCELLABLE,
        evidence_hint="The disclosure shown before any request leaves the machine.",
    ),
    Capability(
        key="rule_delta.interpret",
        label="Interpret a rule change",
        summary="Make exactly one confirmed provider request that becomes a reviewable candidate.",
        authority=_authority.AUTHORITY_PROVIDER_CALL,
        action="interpret_rule_delta",
        control=Control(cancel=True),
        evidence_hint="The returned candidate, its limitations and the usage reported.",
    ),
    Capability(
        key="memory.read",
        label="Read Developer Memory",
        summary="Read the projected documents of the recorded runs.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="get_memory_documents",
    ),
    Capability(
        key="memory.search",
        label="Search Memory",
        summary="Run a bounded cross-run query over the recorded records.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="search_memory",
        control=CANCELLABLE,
    ),
    Capability(
        key="memory.resume",
        label="Compose a Memory resume",
        summary="Reconstruct completed work, blockers and next actions from the records.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="memory_resume",
        control=CANCELLABLE,
    ),
    Capability(
        key="memory.correction",
        label="Append a human correction",
        summary="Append an append-only correction. It changes what is shown, never what was recorded.",
        authority=_authority.AUTHORITY_LOCAL_WRITE,
        action="append_memory_correction",
        evidence_hint="The appended revision and the generated version it is bound to.",
    ),
    Capability(
        key="provider.readiness",
        label="Check provider readiness",
        summary="Read the local provider configuration. No network, no credential value.",
        authority=_authority.AUTHORITY_READ_ONLY,
        action="get_readiness",
    ),
    Capability(
        key="credential.manage",
        label="Manage the stored credential",
        summary="Present the native secure prompt, or delete the stored credential.",
        authority=_authority.AUTHORITY_CREDENTIAL_USE,
        action="manage_credential",
        control=NO_CONTROL,
    ),
    # A capability the product genuinely does not have. It is listed so the
    # Validator role can be shown honestly as unavailable-with-a-reason rather
    # than omitted or faked.
    Capability(
        key="validation.run",
        label="Run validation checks",
        summary=(
            "Run the code-owned check table through the accepted isolated runner "
            "and store append-only evidence."
        ),
        authority=_authority.AUTHORITY_READ_ONLY,
        action=None,
        available=False,
        unavailable_reason=(
            "Validation runs from the offline operator CLI, not from the desktop. "
            "The workspace displays stored validation evidence but cannot start a run."
        ),
    ),
    Capability(
        key="repository.write",
        label="Write into the project repository",
        summary=(
            "Apply accepted changes to the repository under version control."
        ),
        authority=_authority.AUTHORITY_REPOSITORY_WRITE,
        action=None,
        available=False,
        unavailable_reason=(
            "The desktop holds no repository-write path. Applying changes to the "
            "repository is a separate, explicitly authorized operation."
        ),
    ),
)

_CAPABILITY_INDEX: Dict[str, Capability] = {item.key: item for item in CAPABILITIES}


def get(key: str) -> Optional[Capability]:
    """Return the capability with ``key``, or ``None``."""
    return _CAPABILITY_INDEX.get(key)


def require(key: str) -> Capability:
    """Return the capability with ``key`` or raise ``KeyError``."""
    try:
        return _CAPABILITY_INDEX[key]
    except KeyError as error:  # pragma: no cover - defensive
        raise KeyError(f"unknown capability: {key}") from error


def keys() -> Tuple[str, ...]:
    """Return every capability key, in catalogue order."""
    return tuple(item.key for item in CAPABILITIES)


def available_keys() -> Tuple[str, ...]:
    """Return the keys of the capabilities the desktop can actually use."""
    return tuple(item.key for item in CAPABILITIES if item.available)


__all__ = [
    "Control",
    "NO_CONTROL",
    "CANCELLABLE",
    "Capability",
    "CAPABILITIES",
    "get",
    "require",
    "keys",
    "available_keys",
]
