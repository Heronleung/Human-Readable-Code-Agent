"""Qt-free application-boundary contract (P3.2).

This module is the single, versioned contract shared by every client — the
PySide6 desktop window today, and any future IDE plugin — so that no client
ever imports the deterministic core (:mod:`hrca.scanner`, :mod:`hrca.planning`,
:mod:`hrca.report`, :mod:`hrca.provider`) directly.

It defines, and only defines:

* the contract version constant,
* the request and result payload envelopes,
* the correlation identifier rules,
* the allowed read-only action names (scan pipeline and workspace actions),
* the bounded error codes and their fixed messages,
* the maximum inbound/outbound message size,
* the workspace size/depth limits (tree, file and document bytes), and
* the ``--serve`` sentinel used to launch the headless boundary.

The module is deliberately dependency-free (standard library only) and Qt-free.
It performs no validation of the *task* payload — task validation belongs to
the deterministic core and is invoked by the boundary, never by a client.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

# The version of the desktop-to-core contract. A boundary rejects any request
# whose ``contract_version`` differs from this constant with a bounded
# ``unknown_contract_version`` error.
CONTRACT_VERSION = "3.5.0"

# Correlation identifier: a client-generated opaque string that the boundary
# echoes verbatim so a client can match each response to its in-flight request.
# It must be an ASCII-safe string bounded to this many characters; anything
# else is treated as absent (and is therefore not echoed).
CORRELATION_ID_MAX_CHARS = 64

# Request-level action names the boundary accepts. Every name here is
# read-only; write, Git, command, network and provider actions are absent from
# this set and are therefore rejected with a bounded ``action_not_allowed``
# error.
#
# * ``SCAN_ACTIONS`` run the deterministic scan -> plan -> report pipeline and
#   require a ``task`` payload (the P2.3 intake shape).
# * ``WORKSPACE_ACTIONS`` are the P3.2 read-only workspace/document actions that
#   open a project root, list a filtered tree, and read one permitted document.
ACTION_SCAN = "scan"
ACTION_OPEN_PROJECT = "open_project"
ACTION_GET_TREE = "get_tree"
ACTION_GET_DOCUMENT = "get_document"
ACTION_SYNC_TWIN = "sync_twin"
ACTION_GET_TWIN = "get_twin"
ACTION_GET_ANCHOR = "get_anchor"
ACTION_GET_CODE_MAP = "get_code_map"
ACTION_SAVE_DRAFT = "save_draft"
ACTION_GET_DRAFT = "get_draft"
ACTION_DISCARD_DRAFT = "discard_draft"
ACTION_RESET_DRAFT = "reset_draft"
ACTION_COMPARE_DRAFT = "compare_draft"
ACTION_GENERATE_INTENT_DELTA = "generate_intent_delta"
ACTION_PLAN_PROPOSAL = "plan_proposal"
ACTION_GET_READINESS = "get_readiness"
ACTION_MANAGE_CREDENTIAL = "manage_credential"
ACTION_REMOVE_CREDENTIAL = "remove_credential"
# The P4.2a credential-profile protocol. The desktop owns profile *metadata*
# (display name, active selection) through these read-only local actions, while
# the dedicated native credential host owns the secret entry and storage. No
# profile action carries, returns or touches a credential; a profile is
# identified by an opaque immutable id, never its editable display name.
ACTION_GET_PROFILES = "get_profiles"
ACTION_ADD_PROFILE = "add_profile"
ACTION_RENAME_PROFILE = "rename_profile"
ACTION_DELETE_PROFILE = "delete_profile"
ACTION_SET_ACTIVE_PROFILE = "set_active_profile"
# The P4.2b advisory hosted-planning protocol. ``prepare_advisory`` builds the
# deterministic disclosure manifest (no network); ``plan_advisory`` performs the
# one user-confirmed, low-budget provider request. Neither action carries or
# returns a credential, and neither performs any source, Git, command or
# repository-write action.
ACTION_PREPARE_ADVISORY = "prepare_advisory"
ACTION_PLAN_ADVISORY = "plan_advisory"
# The P4.3 document-driven app-package protocol. ``get_package`` returns the
# validated reference package (form/result schema); ``run_package`` executes it
# through the isolated runner and returns a normalized result. Neither action
# carries a credential, a command, a mount, an environment variable or code.
ACTION_GET_PACKAGE = "get_package"
ACTION_RUN_PACKAGE = "run_package"
# The P4.4 document/version-authority protocol. It owns the user-authored
# Working Document (create/open/save/list), the deterministic candidate
# (create/get), explicit adoption, and accepted-version listing/restore. Every
# action writes only the per-document version store — never source, Git state,
# a command, a provider call, or a package execution. Saving a document appends
# an immutable revision and updates the Working Document only; it never touches
# candidate or accepted state.
ACTION_DOCUMENT_CREATE = "create_document"
ACTION_DOCUMENT_OPEN = "open_document"
ACTION_DOCUMENT_SAVE = "save_document"
ACTION_DOCUMENT_LIST = "list_documents"
ACTION_DOCUMENT_CREATE_CANDIDATE = "create_candidate"
ACTION_DOCUMENT_GET_CANDIDATE = "get_candidate"
ACTION_DOCUMENT_ADOPT = "adopt_candidate"
ACTION_DOCUMENT_LIST_VERSIONS = "list_versions"
ACTION_DOCUMENT_RESTORE = "restore_version"
# The P4.5 read-only version-bound preview. It returns a bounded read-model that
# connects the Working Document head, the latest Candidate or the current
# Accepted Version, the fixed package schema and the validation evidence — it
# never executes a package, changes version state or calls a provider.
ACTION_DOCUMENT_PREVIEW = "preview_document"
# The P4.6 app-owned document-library protocol. It owns the folder/document tree
# the explorer renders: ``get_library`` returns the joined tree (folders plus
# document references enriched with each Working Document's name/kind/revision
# summary), and the mutation actions create folders, rename/move/trash/restore a
# folder or document. Every mutation writes only the library store (folder
# names, parent relationships and the trash flag) and — for a document rename —
# the document's display name; it never rewrites a document id, revision,
# candidate/accepted record, package identity, evidence binding or the Working
# Document content. No action here touches source, Git state, a command, a
# provider or the network.
ACTION_LIBRARY_GET = "get_library"
ACTION_LIBRARY_CREATE_FOLDER = "create_folder"
ACTION_LIBRARY_RENAME = "rename_item"
ACTION_LIBRARY_MOVE = "move_item"
ACTION_LIBRARY_TRASH = "trash_item"
ACTION_LIBRARY_RESTORE = "restore_item"
# The P4.7 candidate-package staging seam. ``stage_candidate_package`` performs
# only offline validation, exact binding and protected verification of a bounded
# candidate package — it never executes a package, adopts a candidate, or makes
# a provider/credential/network/token call. It returns a read-only evidence
# state, never a run result.
ACTION_CANDIDATE_PACKAGE_STAGE = "stage_candidate_package"
# The P4.7a declarative rule-delta protocol. ``stage_rule_delta`` validates,
# binds and verifies a rule delta offline (returning a read-only evidence
# state); ``run_rule_delta`` validates and executes a reviewed delta through the
# isolated runner only. Neither action adopts a Candidate, makes a provider/
# credential/network/token call, or writes the repository.
ACTION_RULE_DELTA_STAGE = "stage_rule_delta"
ACTION_RULE_DELTA_RUN = "run_rule_delta"
# The P4.8 bounded provider-to-rule-delta interpretation protocol.
# ``prepare_rule_delta`` builds the visible preflight disclosure (offline, no
# network); ``interpret_rule_delta`` performs exactly one user-confirmed provider
# request, then validates, executes and independently verifies the delta into a
# reviewable Candidate. Neither action adopts a Candidate, writes the repository,
# or carries/returns a credential.
ACTION_PREPARE_RULE_DELTA = "prepare_rule_delta"
ACTION_INTERPRET_RULE_DELTA = "interpret_rule_delta"

SCAN_ACTIONS = frozenset({"scan", "read", "analyze", "inspect", "plan"})
WORKSPACE_ACTIONS = frozenset(
    {ACTION_OPEN_PROJECT, ACTION_GET_TREE, ACTION_GET_DOCUMENT}
)
# The P3.3 read-only Twin protocol: synchronize the Structured Code Twin for
# the accepted workspace (optionally scoped to changed paths), retrieve one
# source-linked projection, and navigate a behavior node to its source anchor.
TWIN_ACTIONS = frozenset({ACTION_SYNC_TWIN, ACTION_GET_TWIN, ACTION_GET_ANCHOR})
# The P3.4 editable Code Map protocol: retrieve the editable Code Map baseline
# plus any saved draft, validate/save/discard/reset a Twin Draft, compare a
# draft with the baseline, and generate/retrieve a non-executable Intent Delta.
# Every action here writes only the per-workspace draft store — never source,
# Git state, a command, or the network.
DRAFT_ACTIONS = frozenset(
    {
        ACTION_GET_CODE_MAP,
        ACTION_SAVE_DRAFT,
        ACTION_GET_DRAFT,
        ACTION_DISCARD_DRAFT,
        ACTION_RESET_DRAFT,
        ACTION_COMPARE_DRAFT,
        ACTION_GENERATE_INTENT_DELTA,
    }
)
# The P4.1 read-only proposal-planning protocol: derive a deterministic,
# non-applied Proposal Package from a validated Intent Delta. It reads only the
# per-workspace draft store and the synchronized Twin — never source, Git state,
# a command, or the network.
PROPOSAL_ACTIONS = frozenset({ACTION_PLAN_PROPOSAL})
# The P4.2a redacted local readiness protocol: report the fixed DeepSeek adapter
# identity plus a bounded, credential-presence-derived state. It performs no
# network access, no inference and never surfaces a credential, an endpoint, a
# header or a config error reason.
READINESS_ACTIONS = frozenset({ACTION_GET_READINESS})
# The P4.2a backend-owned credential management protocol: enroll/replace or
# remove the DeepSeek API key through the platform credential store. These
# actions are dispatched only through the local boundary to backend-owned
# secure enrollment (a native prompt) — the desktop renderer never receives,
# holds, logs or serializes the key, and no source, Git state, command or
# network is ever touched.
CREDENTIAL_ACTIONS = frozenset({ACTION_MANAGE_CREDENTIAL, ACTION_REMOVE_CREDENTIAL})
# The credential-profile metadata protocol (P4.2a): list saved profiles, add a
# profile whose secret was already stored by the native host, rename a profile,
# delete a profile (metadata plus its secret), and select exactly one active
# profile. Every action touches only the non-secret provider configuration and
# the platform credential store — never source, Git state, a command or the
# network.
PROFILE_ACTIONS = frozenset(
    {
        ACTION_GET_PROFILES,
        ACTION_ADD_PROFILE,
        ACTION_RENAME_PROFILE,
        ACTION_DELETE_PROFILE,
        ACTION_SET_ACTIVE_PROFILE,
    }
)
# The P4.2b advisory hosted-planning protocol. ``prepare_advisory`` is read-only
# and offline; ``plan_advisory`` performs exactly one confirmed provider request
# through the backend-owned transport. Neither action is automatic: the desktop
# must call ``prepare_advisory`` first and then ``plan_advisory`` with an
# explicit ``confirmed: true``.
ADVISORY_ACTIONS = frozenset({ACTION_PREPARE_ADVISORY, ACTION_PLAN_ADVISORY})
# The P4.3 document-driven app-package protocol. ``get_package`` is read-only
# and offline; ``run_package`` executes through the isolated runner only after
# the runner's fail-closed availability/isolation preflight.
PACKAGE_ACTIONS = frozenset({ACTION_GET_PACKAGE, ACTION_RUN_PACKAGE})
# The P4.4 document/version-authority protocol: the user-authored Working
# Document, the deterministic fixture-bound candidate, explicit adoption, and
# accepted-version listing/restore. No action here touches source, Git state, a
# command, a provider, the network or a package execution.
DOCUMENT_ACTIONS = frozenset(
    {
        ACTION_DOCUMENT_CREATE,
        ACTION_DOCUMENT_OPEN,
        ACTION_DOCUMENT_SAVE,
        ACTION_DOCUMENT_LIST,
        ACTION_DOCUMENT_CREATE_CANDIDATE,
        ACTION_DOCUMENT_GET_CANDIDATE,
        ACTION_DOCUMENT_ADOPT,
        ACTION_DOCUMENT_LIST_VERSIONS,
        ACTION_DOCUMENT_RESTORE,
        ACTION_DOCUMENT_PREVIEW,
    }
)
# The P4.6 app-owned document-library protocol. See the comment beside the
# ACTION_LIBRARY_* constants above: it organises documents/folders only.
LIBRARY_ACTIONS = frozenset(
    {
        ACTION_LIBRARY_GET,
        ACTION_LIBRARY_CREATE_FOLDER,
        ACTION_LIBRARY_RENAME,
        ACTION_LIBRARY_MOVE,
        ACTION_LIBRARY_TRASH,
        ACTION_LIBRARY_RESTORE,
    }
)
# The P4.7 candidate-package staging protocol. Offline only: validate, bind and
# verify a bounded candidate package; no execution, adoption, provider,
# credential, network or token access.
CANDIDATE_PACKAGE_ACTIONS = frozenset({ACTION_CANDIDATE_PACKAGE_STAGE})
# The P4.7a declarative rule-delta protocol. Offline validation/binding/
# verification plus isolated-runner execution of reviewed deltas only.
RULE_DELTA_ACTIONS = frozenset({ACTION_RULE_DELTA_STAGE, ACTION_RULE_DELTA_RUN})
# The P4.8 bounded provider-to-rule-delta interpretation protocol. ``prepare``
# is read-only and offline; ``interpret`` performs exactly one confirmed request
# through the distinct delta transport, then validates/executes/verifies only.
RULE_DELTA_INTERPRET_ACTIONS = frozenset(
    {ACTION_PREPARE_RULE_DELTA, ACTION_INTERPRET_RULE_DELTA}
)
ALLOWED_ACTIONS = (
    SCAN_ACTIONS
    | WORKSPACE_ACTIONS
    | TWIN_ACTIONS
    | DRAFT_ACTIONS
    | PROPOSAL_ACTIONS
    | READINESS_ACTIONS
    | CREDENTIAL_ACTIONS
    | PROFILE_ACTIONS
    | ADVISORY_ACTIONS
    | PACKAGE_ACTIONS
    | DOCUMENT_ACTIONS
    | LIBRARY_ACTIONS
    | CANDIDATE_PACKAGE_ACTIONS
    | RULE_DELTA_ACTIONS
    | RULE_DELTA_INTERPRET_ACTIONS
)

# Task-level ``allowed_actions`` that the read-only slice permits. A task that
# names a mutating action (edit / commit / remote) is rejected even though the
# deterministic planner would otherwise accept it — the boundary owns the
# allowlist decision.
READ_ONLY_TASK_ACTIONS = frozenset({"read", "analyze", "scan", "inspect", "plan"})

# Maximum inbound and outbound message size, in UTF-8 bytes. Enforced by the
# boundary on inbound requests and by the client on inbound backend output.
MAX_MESSAGE_BYTES = 1_048_576  # 1 MiB

# Workspace size/depth limits (P3.2). These bound the read-only tree and
# document surface so a single request can never produce unbounded output:
#
# * ``MAX_TREE_ENTRIES`` — maximum number of entries (directories + files) the
#   tree walk will emit; the walk stops and marks the tree ``truncated``.
# * ``MAX_TREE_DEPTH`` — maximum directory depth (path components below the
#   root) the walk will descend into; deeper directories are listed but not
#   expanded.
# * ``MAX_DOCUMENT_BYTES`` — a document read larger than this is reported as an
#   unavailable result with reason ``file_too_large``. It is kept well below
#   ``MAX_MESSAGE_BYTES`` so an escaped (non-ASCII) document still fits on the
#   wire.
MAX_TREE_ENTRIES = 2000
MAX_TREE_DEPTH = 32
MAX_DOCUMENT_BYTES = 64 * 1024  # 64 KiB

# Maximum inbound Twin Draft size, in UTF-8 bytes, enforced by the boundary so
# an oversized draft is rejected with a bounded error before any validation.
MAX_DRAFT_BYTES = 64 * 1024  # 64 KiB

# Maximum Working Document text size, in UTF-8 bytes, enforced by the boundary
# before a save so an oversized document is rejected with a bounded error. It is
# kept equal to ``MAX_DOCUMENT_BYTES`` so the editable document surface is
# bounded the same way the read-only preview surface is.
MAX_WORKING_DOCUMENT_BYTES = 64 * 1024  # 64 KiB

# Argument sentinel that turns the unified entry executable into the headless
# boundary. A frozen build launches ``[sys.executable, "--serve"]``; a source
# build launches ``[sys.executable, "-m", "hrca.boundary", "--serve"]``.
SERVE_SENTINEL = "--serve"

# Argument sentinel that turns the unified entry executable into the dedicated
# native credential host (a short-lived, single-purpose process that owns the
# secure credential prompt and the Credential Manager write). A frozen build
# launches ``[sys.executable, "--credential"]``; a source build launches
# ``[sys.executable, "-m", "hrca.credential_host"]``.
CREDENTIAL_SENTINEL = "--credential"

# Bounded error code -> fixed message catalogue. An error response carries only
# a code from this table; its message is always drawn from the catalogue, so
# arbitrary caller text, file contents, and exception detail can never leak
# into a protocol error. The valid codes are exactly the keys of this table.
_ERROR_MESSAGES = {
    "malformed_request": "request is not valid UTF-8 JSON",
    "invalid_request": "request payload is invalid",
    "unknown_contract_version": "contract version is not supported",
    "action_not_allowed": "action is not allowed by the read-only boundary",
    "message_too_large": "message exceeds the maximum allowed size",
    "internal_error": "backend internal error",
    # Workspace/path errors (P3.2). Messages are fixed and never interpolate a
    # requested path or file content, so caller text cannot leak.
    "project_not_open": "no project root has been accepted",
    "path_not_found": "the requested path does not exist",
    "path_not_allowed": "the path escapes the accepted project root or is excluded",
    "path_not_readable": "the path exists but is not a readable regular file",
    "unsupported_type": "the file type is not supported by the workspace",
    "file_too_large": "the file exceeds the maximum allowed size",
    # Twin errors (P3.3). Messages are fixed and never interpolate a selector,
    # path or file content, so caller text cannot leak.
    "twin_not_synchronized": "no Twin has been synchronized for this workspace",
    "twin_not_found": "the requested Twin entity does not exist",
    # Editable Code Map / Twin Draft errors (P3.4). Messages are fixed and never
    # interpolate caller text, so draft content cannot leak into a protocol
    # error.
    "draft_invalid": "the Twin Draft is invalid",
    "draft_not_found": "no Twin Draft exists for this workspace",
    "draft_stale": "the Twin Draft is stale against the current baseline",
    "draft_no_change": "the Twin Draft contains no changes",
    "draft_oversized": "the Twin Draft exceeds the maximum allowed size",
    # Credential-profile errors (P4.2a). Messages are fixed and never
    # interpolate a profile id, display name or credential, so caller text and
    # secret material can never leak into a protocol error.
    "profile_not_found": "the credential profile does not exist",
    "profile_name_invalid": "the credential profile name is invalid",
    "profile_credential_missing": "the credential for this profile is not present",
    "profile_persist_failed": "the credential profile could not be saved",
    # App-package errors (P4.3). Messages are fixed and never interpolate a
    # package id, path or input value.
    "package_not_found": "the named app package does not exist",
    # Document/version-authority errors (P4.4). Messages are fixed and never
    # interpolate a document id, name, revision, fingerprint, candidate id or
    # user prose, so caller text cannot leak into a protocol error.
    "document_not_found": "the document does not exist",
    "document_name_invalid": "the document name is invalid",
    "document_name_in_use": "a document with this name already exists",
    "document_oversized": "the document exceeds the maximum allowed size",
    "document_stale": "the document changed since it was loaded",
    "document_persist_failed": "the document could not be saved",
    "document_not_saved": "the document has no saved revision",
    "candidate_not_found": "the candidate does not exist",
    "candidate_stale": "the candidate is stale against the current document",
    "candidate_invalid": "the candidate record is invalid",
    "adopt_not_allowed": "adoption is not allowed for this candidate",
    "already_adopted": "the candidate has already been adopted",
    "version_not_found": "the accepted version does not exist",
    "restore_not_allowed": "the version cannot be restored",
    # Document-library errors (P4.6). Messages are fixed and never interpolate a
    # folder id, document id, name or user prose, so caller text cannot leak.
    "folder_not_found": "the folder does not exist",
    "folder_name_invalid": "the folder name is invalid",
    "item_not_found": "the item does not exist",
    "name_in_use": "an item with this name already exists in this folder",
    "invalid_parent": "the parent folder is not valid",
    "cyclic_move": "a folder cannot be moved into itself or its own subfolder",
    "parent_trashed": "the parent folder is in the trash",
    "item_trashed": "the item is in the trash",
    "item_not_trashed": "the item is not in the trash",
    "restore_collision": "an item with this name already exists where it would be restored",
    "library_persist_failed": "the document library could not be saved",
}

ERROR_CODES = frozenset(_ERROR_MESSAGES)


def error_message(code: str) -> str:
    """Return the fixed catalogue message for a bounded error ``code``."""
    return _ERROR_MESSAGES[code]


class ContractError(Exception):
    """Bounded, sanitized contract failure.

    Carries only a bounded ``code``; ``message`` is always drawn from the fixed
    catalogue. No caller-supplied text, file content, or exception object is
    ever retained or serialized.
    """

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _ERROR_MESSAGES:
            raise ValueError("invalid contract error code")
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(code, self.message)

    def to_dict(self) -> Dict[str, str]:
        """Return a canonical ``{code, message}`` mapping."""
        return {"code": self.code, "message": self.message}


# -- serialization helpers -----------------------------------------------


def dumps(obj: Any) -> str:
    """Serialize ``obj`` to a single-line, deterministic UTF-8-safe JSON string.

    ``ensure_ascii=True`` is kept deliberately: non-ASCII characters are
    escaped as ``\\uXXXX`` so every protocol message is pure ASCII on the wire
    and still round-trips losslessly through :func:`loads`.
    """
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> Any:
    """Parse one JSON message; raises :class:`ValueError` on invalid JSON."""
    return json.loads(text)


def new_correlation_id() -> str:
    """Return a fresh, ASCII-safe correlation identifier (32 hex chars)."""
    return uuid.uuid4().hex


# -- opaque credential-profile identifiers (P4.2a) ------------------------
#
# A profile id is an opaque, immutable 32-lowercase-hex identifier (a UUID hex).
# It is generated once at profile creation and never derived from, or replaced
# by, the editable display name; the Windows Credential Manager target for a
# profile is derived from this id, so renaming a profile never touches the
# secret's identity.

# Number of characters in a canonical profile id (a ``uuid4().hex`` string).
PROFILE_ID_LENGTH = 32

_PROFILE_ID_ALPHABET = frozenset("0123456789abcdef")


def is_valid_profile_id(value: Any) -> bool:
    """Return True when ``value`` is a canonical opaque profile id."""
    return (
        isinstance(value, str)
        and len(value) == PROFILE_ID_LENGTH
        and all(ch in _PROFILE_ID_ALPHABET for ch in value)
    )


def new_profile_id() -> str:
    """Return a fresh opaque profile id (32 lowercase hex chars)."""
    return uuid.uuid4().hex


# -- payload envelope builders -------------------------------------------


def build_request(
    correlation_id: str, action: str, path: str, task: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a request envelope for the given read-only ``action`` and ``task``.

    ``path`` is the filesystem root the boundary scans; ``task`` is the P2.3
    task-intake payload (validated by the boundary, never by the client).
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": action,
        "path": path,
        "task": task,
    }


def build_success(correlation_id: Optional[str], result: Dict[str, Any]) -> Dict[str, Any]:
    """Build a success result envelope for ``result``."""
    return {
        "contract_version": CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "ok": True,
        "result": result,
    }


def build_error(correlation_id: Optional[str], code: str) -> Dict[str, Any]:
    """Build a bounded error envelope for ``code`` (message from the catalogue)."""
    return {
        "contract_version": CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "ok": False,
        "error": {"code": code, "message": error_message(code)},
    }


__all__ = [
    "CONTRACT_VERSION",
    "CORRELATION_ID_MAX_CHARS",
    "ACTION_SCAN",
    "ACTION_OPEN_PROJECT",
    "ACTION_GET_TREE",
    "ACTION_GET_DOCUMENT",
    "ACTION_SYNC_TWIN",
    "ACTION_GET_TWIN",
    "ACTION_GET_ANCHOR",
    "ACTION_GET_CODE_MAP",
    "ACTION_SAVE_DRAFT",
    "ACTION_GET_DRAFT",
    "ACTION_DISCARD_DRAFT",
    "ACTION_RESET_DRAFT",
    "ACTION_COMPARE_DRAFT",
    "ACTION_GENERATE_INTENT_DELTA",
    "ACTION_PLAN_PROPOSAL",
    "ACTION_GET_READINESS",
    "ACTION_MANAGE_CREDENTIAL",
    "ACTION_REMOVE_CREDENTIAL",
    "ACTION_GET_PROFILES",
    "ACTION_ADD_PROFILE",
    "ACTION_RENAME_PROFILE",
    "ACTION_DELETE_PROFILE",
    "ACTION_SET_ACTIVE_PROFILE",
    "ACTION_PREPARE_ADVISORY",
    "ACTION_PLAN_ADVISORY",
    "ACTION_GET_PACKAGE",
    "ACTION_RUN_PACKAGE",
    "ACTION_DOCUMENT_CREATE",
    "ACTION_DOCUMENT_OPEN",
    "ACTION_DOCUMENT_SAVE",
    "ACTION_DOCUMENT_LIST",
    "ACTION_DOCUMENT_CREATE_CANDIDATE",
    "ACTION_DOCUMENT_GET_CANDIDATE",
    "ACTION_DOCUMENT_ADOPT",
    "ACTION_DOCUMENT_LIST_VERSIONS",
    "ACTION_DOCUMENT_RESTORE",
    "ACTION_DOCUMENT_PREVIEW",
    "ACTION_LIBRARY_GET",
    "ACTION_LIBRARY_CREATE_FOLDER",
    "ACTION_LIBRARY_RENAME",
    "ACTION_LIBRARY_MOVE",
    "ACTION_LIBRARY_TRASH",
    "ACTION_LIBRARY_RESTORE",
    "ACTION_CANDIDATE_PACKAGE_STAGE",
    "ACTION_RULE_DELTA_STAGE",
    "ACTION_RULE_DELTA_RUN",
    "ACTION_PREPARE_RULE_DELTA",
    "ACTION_INTERPRET_RULE_DELTA",
    "SCAN_ACTIONS",
    "WORKSPACE_ACTIONS",
    "TWIN_ACTIONS",
    "DRAFT_ACTIONS",
    "PROPOSAL_ACTIONS",
    "READINESS_ACTIONS",
    "CREDENTIAL_ACTIONS",
    "PROFILE_ACTIONS",
    "ADVISORY_ACTIONS",
    "PACKAGE_ACTIONS",
    "DOCUMENT_ACTIONS",
    "LIBRARY_ACTIONS",
    "CANDIDATE_PACKAGE_ACTIONS",
    "RULE_DELTA_ACTIONS",
    "RULE_DELTA_INTERPRET_ACTIONS",
    "ALLOWED_ACTIONS",
    "READ_ONLY_TASK_ACTIONS",
    "MAX_MESSAGE_BYTES",
    "MAX_TREE_ENTRIES",
    "MAX_TREE_DEPTH",
    "MAX_DOCUMENT_BYTES",
    "MAX_DRAFT_BYTES",
    "MAX_WORKING_DOCUMENT_BYTES",
    "SERVE_SENTINEL",
    "CREDENTIAL_SENTINEL",
    "ERROR_CODES",
    "error_message",
    "ContractError",
    "dumps",
    "loads",
    "new_correlation_id",
    "PROFILE_ID_LENGTH",
    "is_valid_profile_id",
    "new_profile_id",
    "build_request",
    "build_success",
    "build_error",
]
