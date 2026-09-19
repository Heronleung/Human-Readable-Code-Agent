"""Qt-free client supervision logic (P3.2).

The deterministic, testable half of the PySide6 client. It depends only on the
standard library and :mod:`hrca.contract` — it never imports the scanner,
planner, report builder, provider protocol, Git tooling, or any
command-execution code, and it never decides that an action is permitted.

It provides:

* ``LineBuffer`` — incremental accumulation of backend stdout into
  newline-delimited lines, with a maximum-message-size guard,
* ``ResponseRouter`` — correlation-id matching so stale responses are
  discarded and abandoned requests are marked blocked rather than failed,
* ``build_fixture_task`` / ``build_scan_task`` — the fixed read-only tasks,
* ``build_request`` / ``build_scan_request`` / ``build_open_project_request`` /
  ``build_get_tree_request`` / ``build_get_document_request`` — the request
  envelopes a client sends (scan and workspace actions),
* ``resolve_backend_command`` — the documented source/frozen launch rule,
* bounded client-side state labels (supervision, Twin, provider, repository
  and validation states).
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import Any, Dict, List, Optional

from . import contract

# Client-side supervision state labels. A request is "blocked" (not "failed")
# when it is abandoned by a timeout or restart, and "unavailable" when the
# backend cannot be reached or exits without a usable response.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_SUCCESS = "success"
STATE_FAILED = "failed"
STATE_BLOCKED = "blocked"
STATE_UNAVAILABLE = "unavailable"

# Human-Readable Twin presentation states (P3.2). The Twin surface can display
# any of these; in P3.2 no Twin entity exists, so the honest default is
# ``empty`` and no semantic description is fabricated.
TWIN_EMPTY = "empty"
TWIN_LOADING = "loading"
TWIN_AVAILABLE = "available"
TWIN_STALE = "stale"
TWIN_CONFLICT = "conflict"
TWIN_UNSUPPORTED = "unsupported"
TWIN_STATES = frozenset(
    {TWIN_EMPTY, TWIN_LOADING, TWIN_AVAILABLE, TWIN_STALE, TWIN_CONFLICT, TWIN_UNSUPPORTED}
)

# Provider readiness (P3.2): the agent-chat surface is always unavailable and
# never makes a provider, credential, network or inference call.
PROVIDER_UNAVAILABLE = "unavailable"

# Provider readiness presentation states (P4.2a). The boundary reports one of
# these bounded states from non-secret facts — the fixed provider/model identity
# plus credential *presence* — and the client renders them through this
# vocabulary, never importing the provider/credential seam. Unknown values fall
# back to their raw token.
PROVIDER_READINESS_STATE_LABELS = {
    "configured": "Configured",
    "missing_credential": "Credential missing",
    "no_profile": "No API key profile",
    "credential_unretrievable": "API key unreadable",
    "unavailable": "Unavailable",
    "invalid_config": "Invalid configuration",
}


def provider_readiness_state_label(state: str) -> str:
    """Return the human label for a provider readiness ``state``."""
    return PROVIDER_READINESS_STATE_LABELS.get(state, state)


# The six presentation states for the fixed-height provider status region
# (P4.2a). ``pending`` and ``failed`` are client-side transient states; the
# other four are the boundary's bounded readiness states mapped verbatim. The
# message for each state is the exact user-facing sentence the desktop renders;
# no technical or Win32 diagnostic text ever reaches the user.
PROVIDER_STATUS_PENDING = "pending"
PROVIDER_STATUS_CONFIGURED = "configured"
PROVIDER_STATUS_MISSING_CREDENTIAL = "missing_credential"
PROVIDER_STATUS_UNAVAILABLE = "unavailable"
PROVIDER_STATUS_INVALID_CONFIG = "invalid_config"
PROVIDER_STATUS_FAILED = "failed"

PROVIDER_STATUS_MESSAGES = {
    PROVIDER_STATUS_PENDING: "Checking local provider configuration…",
    PROVIDER_STATUS_CONFIGURED: "DeepSeek is configured locally",
    PROVIDER_STATUS_MISSING_CREDENTIAL: "DeepSeek API key not configured",
    # The two new bounded states (P4.8b): the boundary now distinguishes a
    # metadata-only/orphaned profile and an unreadable credential from a real,
    # retrievable key, so a misleading "configured" success can no longer occur.
    "no_profile": "No DeepSeek API key profile — add one in Settings",
    "credential_unretrievable": "DeepSeek API key unreadable — replace it in Settings",
    PROVIDER_STATUS_UNAVAILABLE: "Provider setup is unavailable on this platform",
    PROVIDER_STATUS_INVALID_CONFIG: "Provider configuration needs repair",
    PROVIDER_STATUS_FAILED: "Provider check failed; try again.",
}

# Bounded messages for the backend-owned credential manage/remove actions.
# Each maps a redacted action state to a safe, action-oriented sentence.
CREDENTIAL_ACTION_MESSAGES = {
    "stored": "API key stored securely.",
    "cancelled": "No change — the secure prompt was cancelled.",
    "removed": "API key removed.",
    "unavailable": "Secure key management is unavailable on this platform.",
    "failed": "The operation could not be completed.",
}

# Bounded failure-category messages for a ``failed`` credential action. The
# boundary reports the real failure category as a ``reason`` token; the desktop
# maps it to a safe, action-oriented sentence instead of collapsing every
# failure into the generic "could not be completed" text. Neither message names
# a key, an endpoint or a raw OS error code.
CREDENTIAL_FAILURE_MESSAGES = {
    "prompt_failed": "The secure credential prompt could not be shown.",
    "prompt_invalid_argument": (
        "The secure credential prompt could not be shown (invalid configuration)."
    ),
    "prompt_session_unavailable": (
        "The secure credential prompt is not available in this Windows session."
    ),
    "store_failed": "The API key could not be stored securely.",
}

# Bounded pending message shown the instant a manage/remove credential action is
# submitted, before any response arrives. On Windows it names the native
# Credential Manager prompt; elsewhere it stays platform-neutral because the
# action resolves to "unavailable" on those platforms. It never mentions a key.
CREDENTIAL_ACTION_PENDING = (
    "Opening secure Windows credential prompt…"
    if sys.platform == "win32"
    else "Opening secure credential prompt…"
)


def provider_status_message(status: str) -> str:
    """Return the user-facing message for a provider status ``status``."""
    return PROVIDER_STATUS_MESSAGES.get(status, status)


def credential_action_message(state: str, reason: Optional[str] = None) -> str:
    """Return the user-facing message for a credential action ``state``.

    A ``failed`` state may carry a bounded failure ``reason`` (``prompt_failed``
    / ``store_failed``); when present its category-specific message is returned
    instead of the generic failure sentence. Any unknown reason falls back to
    the generic message, so an unexpected token can never surface raw text.
    """
    if state == "failed" and reason in CREDENTIAL_FAILURE_MESSAGES:
        return CREDENTIAL_FAILURE_MESSAGES[reason]
    return CREDENTIAL_ACTION_MESSAGES.get(state, state)


# The fixed, read-only presence mask rendered for every stored credential
# profile (P4.2a). It is a UI presence symbol — a constant string — never
# derived from, or a copy of, the secret or its length, and never persisted.
CREDENTIAL_MASK = "••••••••"

# Bounded user-facing messages for successful credential-profile actions.
PROFILE_ACTION_MESSAGES = {
    "added": "Profile added.",
    "renamed": "Profile renamed.",
    "deleted": "Profile removed.",
    "active_updated": "Active credential updated.",
}

# Bounded failure messages for the credential-profile protocol error codes. Each
# maps a bounded boundary code to a safe sentence — never a key, a display name
# or a raw error.
PROFILE_FAILURE_MESSAGES = {
    "profile_not_found": "That profile no longer exists.",
    "profile_name_invalid": "That profile name is not valid or is already in use.",
    "profile_credential_missing": "The API key for this profile is not present.",
    "profile_persist_failed": "The profile change could not be saved.",
}


def profile_failure_message(code: str) -> str:
    """Return the user-facing message for a bounded profile failure ``code``."""
    return PROFILE_FAILURE_MESSAGES.get(code, "The operation could not be completed.")

# Repository state the client reports; always ``Unverified`` until a later
# approved boundary capability supplies real repository state.
REPOSITORY_UNVERIFIED = "Unverified"

# Validation state for the scan pipeline surfaced in the status area.
VALIDATION_IDLE = "idle"
VALIDATION_RUNNING = "running"
VALIDATION_OK = "ok"
VALIDATION_FAILED = "failed"


class LineBuffer:
    """Accumulate decoded backend stdout into newline-delimited lines.

    ``feed`` appends a chunk and returns any complete lines; a partial line is
    retained. If the retained (not-yet-terminated) partial exceeds the maximum
    message size, a bounded :class:`contract.ContractError` (``message_too_large``)
    is raised so the supervisor can mark the request failed rather than hang on
    an oversized backend line.
    """

    def __init__(self, max_bytes: int = contract.MAX_MESSAGE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._buf = ""

    def feed(self, text: str) -> List[str]:
        self._buf += text
        lines: List[str] = []
        while "\n" in self._buf:
            idx = self._buf.index("\n")
            line = self._buf[:idx]
            self._buf = self._buf[idx + 1 :]
            if len(line.encode("utf-8")) > self._max_bytes:
                self._buf = ""
                raise contract.ContractError("message_too_large")
            lines.append(line)
        if len(self._buf.encode("utf-8")) > self._max_bytes:
            self._buf = ""
            raise contract.ContractError("message_too_large")
        return lines

    def remaining(self) -> str:
        """Return the current partial line (empty when none is pending)."""
        return self._buf


class ResponseRouter:
    """Track in-flight correlation identifiers and route backend responses.

    A response whose correlation id no longer matches an in-flight request is
    stale and must be discarded; :meth:`match` is the single source of truth
    for that decision. :meth:`abandon_all` returns the tracked ids and clears
    them so the caller can mark each abandoned request as ``blocked``.
    """

    def __init__(self) -> None:
        self._inflight: Dict[str, None] = {}

    def track(self, correlation_id: str) -> None:
        self._inflight[correlation_id] = None

    def match(self, correlation_id: Optional[str]) -> bool:
        return isinstance(correlation_id, str) and correlation_id in self._inflight

    def resolve(self, correlation_id: str) -> None:
        self._inflight.pop(correlation_id, None)

    def inflight(self) -> List[str]:
        return sorted(self._inflight)

    def abandon_all(self) -> List[str]:
        ids = sorted(self._inflight)
        self._inflight.clear()
        return ids


def build_fixture_task(scan_path: str) -> Dict[str, Any]:
    """Return the fixed, read-only task the P3.1 client submits.

    ``scan_path`` is the filesystem root the boundary is asked to scan; it is
    carried separately in the request envelope and does not appear in the task
    payload. The repository context is ``Unverified`` because a client must not
    verify repository state (that would import the core).
    """
    return {
        "task_id": "P3.1",
        "title": "Scan and analyze the fixture corpus",
        "request": (
            "Read the fixture corpus and produce a deterministic, no-change "
            "structured report without modifying anything."
        ),
        "repository_context": {
            "status": "Unverified",
            "branch": None,
            "commit_sha": None,
        },
        "allowed_actions": ["read", "analyze", "scan"],
        "constraints": ["Read-only: do not modify any file."],
        "acceptance_criteria": ["A no-change structured report is produced."],
        "risk_level": "low",
        "approval_required": False,
    }


def build_request(correlation_id: str, scan_path: str) -> Dict[str, Any]:
    """Build the P3.1 request envelope for the fixture task."""
    return contract.build_request(
        correlation_id=correlation_id,
        action=contract.ACTION_SCAN,
        path=os.path.abspath(scan_path),
        task=build_fixture_task(scan_path),
    )


def default_fixture_root(frozen: Optional[bool] = None) -> str:
    """Return the deterministic default fixture corpus root.

    The root is resolved from the *module location*, never the current working
    directory, so it is stable regardless of where the process is launched:

    * **source mode** — the repository ``fixtures`` directory, found two parents
      up from this module (``src/hrca/client_core.py`` -> repository root);
    * **frozen mode** — the PyInstaller resource directory ``sys._MEIPASS``,
      where ``--add-data`` bundles the corpus into the distribution artifact.

    ``frozen`` may be overridden for tests.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        # One-folder build: bundled data lives under sys._MEIPASS (the
        # ``_internal`` directory). Fall back to the executable's directory
        # only if that resource marker is absent.
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
            os.path.abspath(sys.executable)
        )
        return os.path.join(base, "fixtures")
    here = os.path.dirname(os.path.abspath(__file__))
    repository_root = os.path.dirname(os.path.dirname(here))
    return os.path.join(repository_root, "fixtures")


def resolve_backend_command(frozen: Optional[bool] = None) -> List[str]:
    """Return the command that launches the headless backend.

    The backend is exposed through the same entry executable using the
    ``--serve`` argument sentinel:

    * frozen build — ``[sys.executable, "--serve"]``,
    * source build — ``[sys.executable, "-m", "hrca.boundary", "--serve"]``.

    ``sys.executable`` and ``sys.argv`` are used rather than assuming an
    installed interpreter, so the resolution works from a venv, a system
    interpreter, or a PyInstaller one-file bundle. ``frozen`` may be overridden
    for tests.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        return [sys.executable, contract.SERVE_SENTINEL]
    return [sys.executable, "-m", "hrca.boundary", contract.SERVE_SENTINEL]


def resolve_credential_host_command(frozen: Optional[bool] = None) -> List[str]:
    """Return the command that launches the dedicated native credential host.

    The host is the short-lived, single-purpose process that owns the native
    secure credential prompt and the Credential Manager write:

    * frozen build — ``[sys.executable, "--credential"]``,
    * source build — ``[sys.executable, "-m", "hrca.credential_host"]``.

    The operation and the parent window handle are carried in the stdin request
    (not the command line), so the command itself names no secret and no
    per-operation argument.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        return [sys.executable, contract.CREDENTIAL_SENTINEL]
    return [sys.executable, "-m", "hrca.credential_host"]


def build_scan_task(scan_path: str) -> Dict[str, Any]:
    """Return a generic read-only scan task for an opened project (P3.2)."""
    return {
        "task_id": "P3.2",
        "title": "Read-only scan of the opened project",
        "request": (
            "Produce a deterministic, no-change structured report without "
            "modifying anything."
        ),
        "repository_context": {
            "status": REPOSITORY_UNVERIFIED,
            "branch": None,
            "commit_sha": None,
        },
        "allowed_actions": ["read", "analyze", "scan"],
        "constraints": ["Read-only: do not modify any file."],
        "acceptance_criteria": ["A no-change structured report is produced."],
        "risk_level": "low",
        "approval_required": False,
    }


def build_scan_request(correlation_id: str, scan_path: str) -> Dict[str, Any]:
    """Build a generic read-only scan request envelope (P3.2).

    Unlike :func:`build_request` (the P3.1 fixture scan), this uses a
    project-neutral task and is used to scan an externally opened root.
    """
    return contract.build_request(
        correlation_id=correlation_id,
        action=contract.ACTION_SCAN,
        path=os.path.abspath(scan_path),
        task=build_scan_task(scan_path),
    )


def build_open_project_request(correlation_id: str, root_path: str) -> Dict[str, Any]:
    """Build an ``open_project`` request for a user-selected root.

    The client collects the root with a directory chooser only; the boundary is
    the authority that validates and accepts it.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_OPEN_PROJECT,
        "path": os.path.abspath(root_path),
    }


def build_get_tree_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``get_tree`` request (operates on the boundary's accepted root)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_TREE,
    }


def build_get_document_request(correlation_id: str, rel_path: str) -> Dict[str, Any]:
    """Build a ``get_document`` request for ``rel_path`` below the accepted root."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_DOCUMENT,
        "path": rel_path,
    }


def build_sync_twin_request(
    correlation_id: str, changed_paths: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Build a ``sync_twin`` request for the accepted workspace (P3.3).

    ``changed_paths`` is an optional list of root-relative ``.py`` paths scoping
    an incremental sync; when omitted (or ``None``) the boundary performs a full
    reconciliation. The client never computes fingerprints, IDs, or Twin state —
    it only names the scope and lets the boundary own all Twin access.
    """
    task: Dict[str, Any] = {}
    if changed_paths is not None:
        task["changed_paths"] = list(changed_paths)
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_SYNC_TWIN,
        "task": task,
    }


def build_get_twin_request(correlation_id: str, selector: str) -> Dict[str, Any]:
    """Build a ``get_twin`` request for one source-linked projection (P3.3).

    ``selector`` is a root-relative file path or a ``module.path.Class.method``
    symbol selector; the boundary resolves it against the synchronized Twin and
    returns a bounded projection bundle or a ``twin_not_found`` error.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_TWIN,
        "task": {"selector": selector},
    }


def build_get_anchor_request(correlation_id: str, node_id: str) -> Dict[str, Any]:
    """Build a ``get_anchor`` request for a behavior node's source anchor (P3.3).

    ``node_id`` is a deterministic behavior-node identifier; the boundary maps it
    to a bounded ``{file, source_range, sync_state}`` navigation result.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_ANCHOR,
        "task": {"node_id": node_id},
    }


# Sync states reported by the deterministic Twin domain (P3.3) map onto the
# bounded client presentation states. ``synchronized`` / ``no_change`` mean the
# Twin is current; ``needs_review`` / ``stale`` / ``blocked`` show as ``stale``
# (not cleanly current); ``conflict`` and ``unsupported`` map verbatim. The keys
# are the Twin domain's fixed vocabulary, held here as literals so the client
# never imports the Twin domain.
_TWIN_STATE_FROM_SYNC = {
    "synchronized": TWIN_AVAILABLE,
    "no_change": TWIN_AVAILABLE,
    "needs_review": TWIN_STALE,
    "stale": TWIN_STALE,
    "blocked": TWIN_STALE,
    "conflict": TWIN_CONFLICT,
    "unsupported": TWIN_UNSUPPORTED,
}


def twin_state_from_sync(sync_state: str) -> str:
    """Return the client Twin presentation state for a Twin sync ``sync_state``."""
    return _TWIN_STATE_FROM_SYNC.get(sync_state, TWIN_AVAILABLE)


# Root-relative suffixes the Twin models; a selected file outside this set has
# no projection and must show a bounded state rather than trigger a source sync.
TWIN_SOURCE_SUFFIXES = (".py", ".pyi")


def is_twin_source_path(rel_path: str) -> bool:
    """Return True when ``rel_path`` names a Python source the Twin models."""
    return isinstance(rel_path, str) and rel_path.endswith(TWIN_SOURCE_SUFFIXES)


def behavior_node_label(node: Dict[str, Any]) -> str:
    """Return one deterministic line describing a behavior ``node`` (P3.3).

    A verified node lists its items; an unresolved node (no items, provenance
    ``unresolved``) is labelled as such and never fabricated with content.
    """
    category = str(node.get("category", "unknown"))
    items = node.get("items") or []
    if items:
        return f"{category}: {', '.join(str(i) for i in items)}"
    if node.get("provenance") == "unresolved":
        return f"{category} (unresolved)"
    return category


def format_twin_projection(bundle: Dict[str, Any]) -> str:
    """Render a Twin projection bundle as deterministic plain text (P3.3).

    Kind, path, provenance, confidence, sync state, details and limitations are
    each shown as text, never colour alone.
    """
    projection = bundle.get("projection") or {}
    lines: List[str] = []
    summary = projection.get("summary")
    if summary:
        lines.append(str(summary))
    lines.append("")
    lines.append(f"Kind: {projection.get('kind', 'unknown')}")
    if projection.get("path"):
        lines.append(f"Path: {projection['path']}")
    if projection.get("locator"):
        lines.append(f"Locator: {projection['locator']}")
    lines.append(f"Provenance: {projection.get('provenance', 'unknown')}")
    lines.append(f"Confidence: {projection.get('confidence', 'unknown')}")
    lines.append(f"Sync state: {projection.get('sync_state', 'unknown')}")
    details = projection.get("details") or []
    if details:
        lines.append("")
        lines.append("Details:")
        for detail in details:
            lines.append(f"  - {detail}")
    limitations = projection.get("limitations") or []
    if limitations:
        lines.append("")
        lines.append("Limitations:")
        for limitation in limitations:
            lines.append(f"  - {limitation}")
    return "\n".join(lines)


def format_twin_sync(result: Dict[str, Any]) -> str:
    """Render a Twin SynchronizationResult as deterministic plain text (P3.3)."""
    state = str(result.get("state", "unknown"))
    counts = result.get("counts") or {}
    lines = [
        f"Twin state: {state}",
        "",
        "Synchronized records:",
        f"  artifacts: {counts.get('artifacts', 0)}",
        f"  behavior nodes: {counts.get('behavior_nodes', 0)}",
        f"  correspondences: {counts.get('correspondences', 0)}",
        f"  projections: {counts.get('projections', 0)}",
    ]
    changed = result.get("changed_paths") or []
    if changed:
        lines.append("")
        lines.append(f"Changed paths: {len(changed)}")
    reason = result.get("reason")
    if reason:
        lines.append("")
        lines.append(f"Reason: {reason}")
    return "\n".join(lines)


# -- Code Map Procedural Language client vocabulary (P3.4) ---------------
#
# The procedural block types, typed draft operations and intent classes are
# held here as literals so the client never imports the Code Map or Code Map
# Draft domain (which it must not import). Each label is a bounded, fixed
# presentation string; unknown values fall back to their raw token.

BLOCK_TYPE_LABELS = {
    "entity": "Entity",
    "purpose": "Purpose",
    "input": "Inputs",
    "step": "Step",
    "decision": "Decision",
    "loop": "Loop",
    "call": "Call",
    "exception": "Exception",
    "return": "Return",
    "side_effect": "Side effect",
    "dependency": "Dependency",
    "invariant": "Invariant",
    "limitation": "Limitation",
    "note": "Note",
}

OPERATION_LABELS = {
    "replace_description": "Replace description",
    "insert_block": "Insert block",
    "delete_draft_block": "Delete draft block",
    "move_draft_block": "Move draft block",
    "replace_condition_intent": "Replace condition intent",
    "mark_unresolved": "Mark unresolved",
    "restore_block": "Restore block",
}

INTENT_CLASS_LABELS = {
    "documentation_intent": "Documentation",
    "behavior_change_intent": "Behavior change",
}


def block_type_label(block_type: str) -> str:
    """Return the human label for a Code Map ``block_type``."""
    return BLOCK_TYPE_LABELS.get(block_type, block_type)


def operation_label(op: str) -> str:
    """Return the human label for a typed draft ``op``."""
    return OPERATION_LABELS.get(op, op)


def intent_class_label(intent_class: str) -> str:
    """Return the human label for a draft ``intent_class``."""
    return INTENT_CLASS_LABELS.get(intent_class, intent_class)


# -- Proposal Package client vocabulary (P4.1) ---------------------------
#
# The terminal proposal states are held here as literals so the client never
# imports the proposal domain (which it must not import). Each label is a
# bounded, fixed presentation string; unknown values fall back to their raw
# token.

PROPOSAL_STATE_LABELS = {
    "ready": "Ready",
    "clarification_required": "Clarification required",
    "unsupported": "Unsupported",
    "no_change": "No change",
    "blocked": "Blocked",
}


def proposal_state_label(state: str) -> str:
    """Return the human label for a proposal ``state``."""
    return PROPOSAL_STATE_LABELS.get(state, state)


def build_get_code_map_request(
    correlation_id: str, selector: Optional[str] = None
) -> Dict[str, Any]:
    """Build a ``get_code_map`` request for the procedural Code Map.

    ``selector`` optionally scopes the document to one entity locator
    (``module.path.Class.method``); when omitted the whole module-level
    document is returned.
    """
    task: Dict[str, Any] = {}
    if selector is not None:
        task["selector"] = selector
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_CODE_MAP,
        "task": task,
    }


def build_save_draft_request(
    correlation_id: str, operations: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Build a ``save_draft`` request carrying the ordered typed ``operations``."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_SAVE_DRAFT,
        "task": {"operations": list(operations)},
    }


def build_get_draft_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``get_draft`` request for the saved Code Map Draft."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_DRAFT,
    }


def build_discard_draft_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``discard_draft`` request (delete the saved draft)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DISCARD_DRAFT,
    }


def build_reset_draft_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``reset_draft`` request (reset the draft to the baseline)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_RESET_DRAFT,
    }


def build_compare_draft_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``compare_draft`` request (draft operations vs baseline)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_COMPARE_DRAFT,
    }


def build_generate_intent_delta_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``generate_intent_delta`` request (derive the Intent Delta)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GENERATE_INTENT_DELTA,
    }


def build_plan_proposal_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``plan_proposal`` request (derive the non-applied Proposal Package)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_PLAN_PROPOSAL,
    }


def build_get_readiness_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``get_readiness`` request (redacted local provider readiness).

    Carries no path, task or credential material: the boundary derives the
    redacted state from non-secret configuration and credential presence alone.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_READINESS,
    }


def build_manage_credential_request(
    correlation_id: str,
    hwnd: Optional[int] = None,
    profile_id: Optional[str] = None,
    display_name: Optional[str] = None,
    theme: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``manage_credential`` request (backend-owned secure enrollment).

    The request carries only the correlation id, the action name, the optional
    native parent window handle ``hwnd`` (an integer handle, never a key, path
    or task), the optional opaque ``profile_id`` naming the credential target
    the host derives itself (replace mode), the optional ``display_name`` shown
    read-only in replace mode, and the optional non-secret ``theme``
    (``"light"`` / ``"dark"``) the native sheet uses to match the desktop. When
    ``profile_id`` is absent the host renders Add mode (editable name). The
    native credential host owns the entry sheet and writes the credential
    straight to the platform store; the desktop never sees the key.
    """
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MANAGE_CREDENTIAL,
    }
    if hwnd is not None:
        request["hwnd"] = int(hwnd)
    if profile_id is not None:
        request["profile_id"] = profile_id
    if display_name is not None:
        request["display_name"] = display_name
    if theme is not None:
        request["theme"] = theme
    return request


def build_remove_credential_request(
    correlation_id: str,
    hwnd: Optional[int] = None,
    profile_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``remove_credential`` request (backend-owned delete).

    Carries only the correlation id, the action name, the optional native
    parent window handle ``hwnd``, and the optional opaque ``profile_id``; the
    host deletes the stored credential and returns a redacted result. No key is
    ever surfaced.
    """
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_REMOVE_CREDENTIAL,
    }
    if hwnd is not None:
        request["hwnd"] = int(hwnd)
    if profile_id is not None:
        request["profile_id"] = profile_id
    return request


def build_get_profiles_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``get_profiles`` request (list saved credential profiles)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_PROFILES,
    }


def build_add_profile_request(
    correlation_id: str, profile_id: str, display_name: str
) -> Dict[str, Any]:
    """Build an ``add_profile`` request (metadata only; the secret is already stored)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_ADD_PROFILE,
        "profile_id": profile_id,
        "display_name": display_name,
    }


def build_rename_profile_request(
    correlation_id: str, profile_id: str, display_name: str
) -> Dict[str, Any]:
    """Build a ``rename_profile`` request (metadata only; id and secret unchanged)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_RENAME_PROFILE,
        "profile_id": profile_id,
        "display_name": display_name,
    }


def build_delete_profile_request(
    correlation_id: str, profile_id: str, active_profile_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a ``delete_profile`` request (credential + metadata, with a fallback)."""
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DELETE_PROFILE,
        "profile_id": profile_id,
    }
    if active_profile_id is not None:
        request["active_profile_id"] = active_profile_id
    return request


def build_set_active_profile_request(
    correlation_id: str, profile_id: Optional[str]
) -> Dict[str, Any]:
    """Build a ``set_active_profile`` request (select one active profile)."""
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_SET_ACTIVE_PROFILE,
    }
    if profile_id is not None:
        request["profile_id"] = profile_id
    return request


def format_procedural_document(document: Any) -> str:
    """Return the procedural Code Map document text (rendered by the boundary).

    The boundary already renders the ordered procedural document as plain text;
    this returns it verbatim (empty when absent) so the client never reformats
    source-grounded wording.
    """
    if not document:
        return ""
    return str(document)


def format_entity_list(entities: List[Dict[str, Any]]) -> str:
    """Render the compact ordered entity list as deterministic plain text."""
    if not entities:
        return "No entities."
    lines: List[str] = []
    for entity in entities:
        kind = str(entity.get("kind", "unknown"))
        locator = str(entity.get("locator", "?"))
        subject = entity.get("subject")
        if subject:
            lines.append(f"{kind}: {locator} — {subject}")
        else:
            lines.append(f"{kind}: {locator}")
    return "\n".join(lines)


def format_draft_operations(operations: List[Dict[str, Any]]) -> str:
    """Render a draft's typed ``operations`` as deterministic plain text."""
    if not operations:
        return "No operations."
    lines: List[str] = []
    for op in operations:
        label = operation_label(str(op.get("op", "unknown")))
        target = str(op.get("target_block_id", "?"))
        intent = intent_class_label(str(op.get("intent_class", "unknown")))
        proposed = op.get("proposed") or {}
        text = proposed.get("display_text") or proposed.get("payload")
        entry = f"{label} — {target} ({intent})"
        if text:
            entry += f": {text}"
        lines.append(entry)
    return "\n".join(lines)


def format_intent_delta(intent_delta: Dict[str, Any]) -> str:
    """Render a non-executable Intent Delta as deterministic plain text.

    Each normalized entry is one line; only explicitly proposed outcomes are
    shown — no fabricated implementation requirements.
    """
    lines: List[str] = [
        "Intent Delta (not executable)",
        f"Intent: {intent_delta.get('intent', 'unknown')}",
        "Executable: false",
        f"Entries: {len(intent_delta.get('entries', []))}",
    ]
    for entry in intent_delta.get("entries") or []:
        op = operation_label(str(entry.get("operation", "unknown")))
        entity = str(entry.get("owning_entity_id", "?"))
        approval = str(entry.get("required_approval_level", "unknown"))
        lines.append(f"  - {op} on {entity} (approval: {approval})")
    return "\n".join(lines)


def format_proposal(proposal: Dict[str, Any]) -> str:
    """Render a non-applied Proposal Package as deterministic plain text.

    The header marks the package non-executable and non-applied; each structured
    field is shown as a labelled section so a user can inspect what a future
    change would involve without any claim that code was generated or applied.
    """
    if not proposal:
        return ""
    state = proposal_state_label(str(proposal.get("state", "unknown")))
    lines: List[str] = [
        "Proposal Package (not applied)",
        f"State: {state}",
        "Executable: false",
        "Applied: false",
    ]
    pid = proposal.get("proposal_id")
    if pid:
        lines.append(f"Proposal: {pid}")

    scope = proposal.get("target_scope") or {}
    entities = scope.get("entities") or []
    artifacts = scope.get("artifacts") or []
    lines.append("")
    lines.append("Target scope:")
    lines.append(f"  entities: {', '.join(str(e) for e in entities) if entities else 'none'}")
    lines.append(f"  artifacts: {', '.join(str(a) for a in artifacts) if artifacts else 'none'}")

    affected = proposal.get("affected_artifacts") or []
    lines.append("")
    lines.append(f"Affected artifacts: {len(affected)}")
    for ref in affected:
        role = ref.get("role", "target")
        kind = ref.get("kind", "unknown")
        path = ref.get("path") or ref.get("locator", "?")
        lines.append(f"  - {kind}: {path} ({role})")

    constraints = proposal.get("preserved_constraints") or []
    lines.append("")
    lines.append(f"Preserved constraints: {len(constraints)}")
    for constraint in constraints:
        entity = constraint.get("entity_id") or "global"
        lines.append(f"  - {entity}: {constraint.get('invariant', '')}")

    assumptions = proposal.get("assumptions") or []
    lines.append("")
    lines.append(f"Assumptions: {len(assumptions)}")
    for assumption in assumptions:
        lines.append(f"  - {assumption}")

    clarifications = proposal.get("clarifications") or []
    lines.append("")
    lines.append(f"Clarifications: {len(clarifications)}")
    for clarification in clarifications:
        entity = clarification.get("entity_id") or "?"
        lines.append(f"  - {entity}: {clarification.get('question', '')}")

    steps = proposal.get("plan_steps") or []
    lines.append("")
    lines.append(f"Plan steps: {len(steps)}")
    for step in steps:
        approval = " (approval)" if step.get("requires_approval") else ""
        lines.append(
            f"  {step.get('step', '?')}. {step.get('description', '')}{approval}"
        )

    risks = proposal.get("risks") or []
    lines.append("")
    lines.append(f"Risks: {len(risks)}")
    for risk in risks:
        lines.append(f"  - [{risk.get('level', 'unknown')}] {risk.get('description', '')}")

    validation = proposal.get("validation_plan") or []
    lines.append("")
    lines.append(f"Validation plan: {len(validation)}")
    for check in validation:
        lines.append(f"  - {check.get('check', '')}: {check.get('expected_outcome', '')}")

    reason = proposal.get("reason")
    if reason:
        lines.append("")
        lines.append(f"Reason: {reason}")

    return "\n".join(lines)


def format_provider_readiness(result: Dict[str, Any]) -> str:
    """Render a redacted readiness result as deterministic plain text (P4.2a).

    Shows only the fixed provider id, the allowlisted model and the bounded
    state, and asserts the honest ``false`` flags for authenticated / online /
    executable. It never renders a credential, endpoint or header value.
    """
    state = provider_readiness_state_label(str(result.get("state", "unavailable")))
    provider_id = str(result.get("provider_id", "unknown"))
    model = result.get("model")
    lines = [
        f"Provider: {provider_id}",
        f"State: {state}",
    ]
    if model:
        lines.append(f"Model: {model}")
    lines.append("Authenticated: false")
    lines.append("Online: false")
    lines.append("Executable: false")
    return "\n".join(lines)


# -- Advisory hosted-planning client vocabulary (P4.2b) -------------------
#
# The normalized advisory states and the bounded "advisory not available"
# reasons are held here as literals so the client never imports the advisory or
# transport domain (which it must not import). Unknown values fall back to
# their raw token, so an unexpected value can never surface raw text.

ADVISORY_STATE_LABELS = {
    "ready": "Ready",
    "credential_missing": "Credential missing",
    "provider_unavailable": "Provider unavailable",
    "network_denied": "Network denied",
    "timeout": "Timed out",
    "rate_limited": "Rate limited",
    "quota_exceeded": "Quota exceeded",
    "invalid_output": "Invalid output",
    "context_rejected": "Context rejected",
    "over_limit": "Over limit",
    "cancel_requested": "Cancelled",
    "stale_response": "Stale",
    "provider_failure": "Provider failure",
}

ADVISORY_UNAVAILABLE_REASONS = {
    "no_change": "The draft contains no changes.",
    "stale": "The draft is stale against the current baseline.",
    "unsupported": "The intent targets an unknown entity.",
    "clarification_required": "The intent needs clarification before planning.",
    "secret_like": "A source excerpt looks secret-like and was excluded.",
    "binary": "A source file is binary and cannot be excerpted.",
    "missing_anchor": "A required source anchor is missing.",
    "outside_root": "A source path is outside the project root.",
    "unsupported_path": "A source path is not a supported source file.",
    "ignored_path": "A source path is excluded from the workspace.",
    "over_limit": "The context exceeds a bounded limit.",
}


def advisory_state_label(state: str) -> str:
    """Return the human label for an advisory ``state``."""
    return ADVISORY_STATE_LABELS.get(state, state)


def advisory_unavailable_reason_label(reason: str) -> str:
    """Return the human label for an advisory-not-available ``reason``."""
    return ADVISORY_UNAVAILABLE_REASONS.get(reason, reason)


def build_prepare_advisory_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``prepare_advisory`` request (build the disclosure context).

    Carries no path, task, token or credential material: the boundary derives
    the disclosure from the current Intent Delta and deterministic proposal.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_PREPARE_ADVISORY,
    }


def build_plan_advisory_request(
    correlation_id: str, advisory_token: str, confirmed: bool
) -> Dict[str, Any]:
    """Build a ``plan_advisory`` request for one confirmed provider call.

    ``advisory_token`` is the content-addressed token from a prior
    ``prepare_advisory``; ``confirmed`` must be ``True`` for any network
    request, and ``False`` sends nothing (the boundary returns
    ``cancel_requested``).
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_PLAN_ADVISORY,
        "task": {"advisory_token": advisory_token, "confirmed": bool(confirmed)},
    }


def format_advisory_disclosure(disclosure: Dict[str, Any]) -> str:
    """Render an itemized disclosure manifest as deterministic plain text.

    Shows the fixed provider/model, the one-attempt policy, every context item
    with its kind, repository-relative label and byte size, the request/output
    caps, and the data-egress statement. It never renders a credential or an
    endpoint.
    """
    if not disclosure:
        return ""
    caps = disclosure.get("caps") or {}
    lines = [
        f"Provider: {disclosure.get('provider_id', 'unknown')}",
        f"Model: {disclosure.get('model', 'unknown')}",
        "One attempt: yes",
        "",
        "Disclosure — the items below will be sent to the provider:",
    ]
    for item in disclosure.get("items") or []:
        lines.append(
            f"  - {item.get('kind', 'unknown')}: {item.get('label', '?')} "
            f"({item.get('bytes', 0)} bytes)"
        )
    lines.append("")
    lines.append(
        f"Caps: {caps.get('request_bytes', 0)} request bytes, "
        f"{caps.get('context_items', 0)} context items, "
        f"{caps.get('output_tokens', 0)} output tokens, "
        f"{caps.get('timeout_seconds', 0)}s timeout."
    )
    lines.append("")
    lines.append(str(disclosure.get("egress_statement", "")))
    return "\n".join(lines)


def format_advisory_result(result: Dict[str, Any]) -> str:
    """Render a versioned advisory result as deterministic plain text.

    Provider-suggested fields (clarification needs, impact, assumptions, risks,
    plan suggestions) are shown under an explicit "provider-suggested" heading;
    the deterministic proposal is labelled authoritative and never re-merged.
    Limitations and usage metadata are shown when present; a credential, raw
    prompt, raw response or hidden reasoning never appears.
    """
    if not result:
        return ""
    state = advisory_state_label(str(result.get("state", "unknown")))
    lines = [
        "Advisory plan (provider-suggested)",
        f"State: {state}",
        f"Provider: {result.get('provider_id', 'unknown')}",
        f"Model: {result.get('model', 'unknown')}",
        f"Sent: {'yes' if result.get('sent') else 'no'}",
    ]
    suggested = result.get("provider_suggested")
    if suggested:
        lines.append("")
        lines.append("Provider-suggested (advisory only, never applied):")
        impact = suggested.get("impact")
        if impact:
            lines.append(f"  Impact: {impact}")
        for label, key in (
            ("Clarification needs", "clarification_needs"),
            ("Assumptions", "assumptions"),
            ("Risks", "risks"),
        ):
            entries = suggested.get(key) or []
            if entries:
                lines.append(f"  {label}:")
                for entry in entries:
                    lines.append(f"    - {entry}")
        suggestions = suggested.get("plan_suggestions") or []
        if suggestions:
            lines.append("  Plan suggestions:")
            for step in suggestions:
                lines.append(f"    {step.get('step', '?')}. {step.get('description', '')}")
    deterministic = result.get("deterministic") or {}
    lines.append("")
    lines.append("Deterministic proposal: authoritative (unchanged by the provider).")
    scope = deterministic.get("target_scope") or {}
    entities = scope.get("entities") or []
    if entities:
        lines.append(f"  Target entities: {', '.join(str(e) for e in entities)}")
    limitations = result.get("limitations") or []
    if limitations:
        lines.append("")
        lines.append("Limitations:")
        for limitation in limitations:
            lines.append(f"  - {limitation}")
    usage = result.get("usage")
    if usage:
        lines.append("")
        lines.append(
            "Usage: "
            f"prompt {usage.get('prompt_tokens')}, "
            f"completion {usage.get('completion_tokens')}, "
            f"total {usage.get('total_tokens')}"
        )
    return "\n".join(lines)


# -- Provider-to-rule-delta interpretation client vocabulary (P4.8) ---------
#
# The normalized interpretation states and the request builders are held here as
# literals so the client never imports the interpretation or transport domain
# (which it must not import). Unknown values fall back to their raw token, so an
# unexpected value can never surface raw text.

DELTA_INTERPRET_STATE_LABELS = {
    "preflight": "Ready to review",
    "sending": "Sending…",
    "cancel_requested": "Cancelled",
    "stale": "Out of date",
    "clarification_required": "Needs clarification",
    "unsupported": "Unsupported",
    "invalid_output": "Invalid response",
    "usage_unknown": "Usage unknown",
    "pricing_unknown": "Pricing unknown",
    "reservation_failed": "Reservation failed",
    "runner_unavailable": "Runtime unavailable",
    "verification_failed": "Verification failed",
    "over_limit": "Over limit",
    "reviewable_candidate": "Reviewable",
    "credential_missing": "Credential missing",
    "credential_rejected": "API key rejected",
    "network_denied": "Network denied",
    "timeout": "Timed out",
    "rate_limited": "Rate limited",
    "quota_exceeded": "Quota exceeded",
    "provider_unavailable": "Provider unavailable",
    "context_rejected": "Context rejected",
    "provider_failure": "Provider failure",
}


def delta_interpret_state_label(state: str) -> str:
    """Return the human label for an interpretation ``state``."""
    return DELTA_INTERPRET_STATE_LABELS.get(state, state)


def build_prepare_rule_delta_request(
    correlation_id: str, document_id: str
) -> Dict[str, Any]:
    """Build a ``prepare_rule_delta`` request (build the disclosure manifest).

    Carries only the document id; the boundary derives the disclosure from the
    saved requirement text and the code-owned instructions. No network access.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_PREPARE_RULE_DELTA,
        "document_id": document_id,
    }


def build_interpret_rule_delta_request(
    correlation_id: str, document_id: str, token: str, confirmed: bool
) -> Dict[str, Any]:
    """Build an ``interpret_rule_delta`` request for one confirmed provider call.

    ``token`` is the content-addressed token from a prior ``prepare_rule_delta``;
    ``confirmed`` must be ``True`` for any network request, and ``False`` sends
    nothing (the boundary returns ``cancel_requested``).
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_INTERPRET_RULE_DELTA,
        "document_id": document_id,
        "task": {"token": token, "confirmed": bool(confirmed)},
    }


def format_delta_disclosure(disclosure: Dict[str, Any]) -> str:
    """Render a preflight interpretation disclosure as deterministic plain text.

    Shows the exact recipient/model, the itemized outgoing manifest, the
    policy/retention warning, the worst-case reservation and the one-attempt
    limits. It never renders a credential or an endpoint.
    """
    if not disclosure:
        return ""
    caps = disclosure.get("caps") or {}
    reservation = disclosure.get("reservation") or {}
    model = disclosure.get("model") or {}
    lines = [f"Provider: {disclosure.get('provider_id', 'unknown')}"]
    if isinstance(model, dict):
        lines += [
            f"Requested model: {model.get('requested_id', 'unknown')}",
            f"Canonical model: {model.get('canonical_id', 'unknown')}",
            f"Effective model: {model.get('effective_version', 'unknown')}",
            f"Model facts verified: {model.get('verified_at', 'unknown')}",
        ]
    else:
        lines.append(f"Model: {model or 'unknown'}")
    lines += [
        "One attempt: yes — no retry, no paid repair",
        "",
        "Disclosure — the items below will be sent to the provider:",
    ]
    for item in disclosure.get("items") or []:
        lines.append(
            f"  - {item.get('kind', 'unknown')}: {item.get('label', '?')} "
            f"({item.get('bytes', 0)} bytes)"
        )
    lines.append("")
    lines.append(
        f"Limits: {caps.get('request_bytes', 0)} request bytes, "
        f"{caps.get('input_tokens', 0)} input tokens, "
        f"{caps.get('output_tokens', 0)} output tokens, "
        f"{caps.get('timeout_seconds', 0)}s provider deadline, "
        f"{caps.get('workflow_timeout_seconds', 0)}s workflow deadline."
    )
    lines.append("")
    lines.append(
        "Reservation: US$" + str(reservation.get("amount_usd", "?"))
        + " (worst case US$" + str(reservation.get("worst_case_cost_usd", "?"))
        + " at peak input US$" + str(reservation.get("input_rate_usd_per_1m", "?"))
        + " / output US$" + str(reservation.get("output_rate_usd_per_1m", "?"))
        + " per 1M tokens)."
    )
    lines.append("")
    lines.append(str(disclosure.get("egress_statement", "")))
    lines.append("")
    lines.append(str(disclosure.get("policy_warning", "")))
    lines.append("")
    lines.append(str(disclosure.get("account_cap_statement", "")))
    if isinstance(model, dict):
        lines.append("")
        lines.append(str(model.get("retirement_warning", "")))
    return "\n".join(lines)


def format_delta_interpret_result(result: Dict[str, Any]) -> str:
    """Render a versioned interpretation result as deterministic plain text.

    A reviewable candidate is shown as a distinct, non-adopted record with its
    bound revision and the evidence produced by the isolated runner; every other
    state is a calm bounded sentence. A credential, raw prompt, raw response or
    hidden reasoning never appears.
    """
    if not result:
        return ""
    state = delta_interpret_state_label(str(result.get("state", "unknown")))
    lines = [
        "Rule interpretation (provider-backed)",
        f"State: {state}",
        f"Provider: {result.get('provider_id', 'unknown')}",
        f"Model: {result.get('model', 'unknown')}",
        f"Sent: {'yes' if result.get('sent') else 'no'}",
    ]
    usage = result.get("usage")
    if usage:
        lines.append(
            "Usage: "
            f"prompt {usage.get('prompt_tokens')}, "
            f"completion {usage.get('completion_tokens')}, "
            f"total {usage.get('total_tokens')}"
        )
    else:
        lines.append("Usage: unknown")
    candidate = result.get("candidate")
    if candidate:
        binding = candidate.get("binding") or {}
        lines += [
            "",
            "Candidate (not adopted)",
            f"Provenance: {candidate.get('provenance', 'unknown')}",
            f"Bound revision: {binding.get('document_revision_id', 'unknown')}",
            "Evidence: isolated runner output, independently verified.",
        ]
    limitations = result.get("limitations") or []
    if limitations:
        lines.append("")
        lines.append("Limitations:")
        for limitation in limitations:
            lines.append(f"  - {limitation}")
    return "\n".join(lines)


# -- Document-driven app-package client vocabulary (P4.3) -----------------
#
# The normalized run-result states are held here as literals so the client never
# imports the package/runner/broker domain (which it must not import). Unknown
# values fall back to their raw token.

RUN_STATE_LABELS = {
    "ok": "Completed",
    "input_invalid": "Input invalid",
    "package_invalid": "Package invalid",
    "runtime_unavailable": "Runtime unavailable",
    "runtime_blocked": "Runtime blocked",
    "timeout": "Timed out",
    "output_invalid": "Output invalid",
    "runner_failed": "Runner failed",
}


def run_state_label(state: str) -> str:
    """Return the human label for a package run ``state``."""
    return RUN_STATE_LABELS.get(state, state)


def build_get_package_request(
    correlation_id: str, package_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a ``get_package`` request for one reference package schema."""
    task: Dict[str, Any] = {}
    if package_id is not None:
        task["package_id"] = package_id
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_GET_PACKAGE,
        "task": task,
    }


def build_run_package_request(
    correlation_id: str, package_id: str, form_input: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a ``run_package`` request executing one package on one form input."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_RUN_PACKAGE,
        "task": {"package_id": package_id, "input": form_input},
    }


def format_run_result(result: Dict[str, Any]) -> str:
    """Render a normalized package run result as deterministic plain text.

    The bounded result fields are shown in sorted key order; limitations (a
    bounded reason) and the state label make blocked/failed outcomes legible.
    No secret, path, command or runner protocol detail is ever rendered.
    """
    if not result:
        return ""
    state = run_state_label(str(result.get("state", "unknown")))
    lines = [
        "Package run",
        f"State: {state}",
        f"Package: {result.get('package_id', 'unknown')}",
    ]
    data = result.get("result")
    if data:
        lines.append("")
        lines.append("Result:")
        for key in sorted(data):
            lines.append(f"  {key}: {data[key]}")
    limitations = result.get("limitations") or []
    if limitations:
        lines.append("")
        lines.append("Limitations:")
        for limitation in limitations:
            lines.append(f"  - {limitation}")
    return "\n".join(lines)


# -- Document/version-authority client vocabulary (P4.4) -------------------
#
# The Working Document, Candidate and Accepted Version identities and the bounded
# failure messages are held here as literals so the client never imports the
# document/version domain or store (which it must not import). Unknown values
# fall back to their raw token.

DOCUMENT_KIND_LABELS = {
    "md": "Markdown",
    "txt": "Plain text",
}

# Bounded failure messages for the document/version protocol error codes. Each
# maps a bounded boundary code to a safe sentence — never a document id, name,
# fingerprint, candidate id or user prose.
DOCUMENT_FAILURE_MESSAGES = {
    "document_not_found": "That document no longer exists.",
    "document_name_invalid": "Use a valid .md or .txt name without path characters.",
    "document_name_in_use": "A document with this name already exists. Choose another name.",
    "document_oversized": "The document is too large to save.",
    "document_stale": "The document changed since it was opened.",
    "document_persist_failed": "The document could not be saved.",
    "document_not_saved": "Save the document before creating a candidate.",
    "candidate_not_found": "That candidate no longer exists.",
    "candidate_stale": "The candidate is stale against the current document.",
    "candidate_invalid": "The candidate record is invalid.",
    "adopt_not_allowed": "Adoption is not allowed for this candidate.",
    "already_adopted": "This candidate has already been adopted.",
    "version_not_found": "That accepted version no longer exists.",
    "restore_not_allowed": "The version cannot be restored.",
    # Document-library errors (P4.6). Fixed sentences, never an id, name or
    # user prose.
    "folder_not_found": "That folder no longer exists.",
    "folder_name_invalid": "Use a valid folder name without path characters.",
    "item_not_found": "That item no longer exists.",
    "name_in_use": "An item with this name already exists in this folder.",
    "invalid_parent": "The parent folder is not valid.",
    "cyclic_move": "A folder cannot be moved into itself or its own subfolder.",
    "parent_trashed": "The parent folder is in the trash.",
    "item_trashed": "The item is in the trash.",
    "item_not_trashed": "The item is not in the trash.",
    "restore_collision": "An item with this name already exists where it would be restored.",
    "library_persist_failed": "The document library could not be saved.",
}


def document_kind_label(kind: str) -> str:
    """Return the human label for a document ``kind`` (md/txt)."""
    return DOCUMENT_KIND_LABELS.get(kind, kind)


def document_failure_message(code: str) -> str:
    """Return the user-facing message for a bounded document failure ``code``."""
    return DOCUMENT_FAILURE_MESSAGES.get(code, "The operation could not be completed.")


def build_create_document_request(
    correlation_id: str, name: str, parent_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a ``create_document`` request for a validated md/txt name.

    ``parent_id`` is the optional target folder (absent = root).
    """
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_CREATE,
        "name": name,
    }
    if parent_id is not None:
        request["parent_id"] = parent_id
    return request


def build_open_document_request(correlation_id: str, document_id: str) -> Dict[str, Any]:
    """Build an ``open_document`` request (reopen one Working Document)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_OPEN,
        "document_id": document_id,
    }


def build_save_document_request(
    correlation_id: str, document_id: str, content: str, base_revision_id: Optional[str]
) -> Dict[str, Any]:
    """Build a ``save_document`` request appending an immutable revision.

    ``base_revision_id`` is the head the client loaded; the boundary refuses the
    save when it no longer matches (external change).
    """
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_SAVE,
        "document_id": document_id,
        "content": content,
    }
    if base_revision_id is not None:
        request["base_revision_id"] = base_revision_id
    return request


def build_list_documents_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``list_documents`` request (summaries of every document)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_LIST,
    }


def build_create_candidate_request(correlation_id: str, document_id: str) -> Dict[str, Any]:
    """Build a ``create_candidate`` request bound to the current head revision."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_CREATE_CANDIDATE,
        "document_id": document_id,
    }


def build_get_candidate_request(
    correlation_id: str, document_id: str, candidate_id: str
) -> Dict[str, Any]:
    """Build a ``get_candidate`` request for one candidate's state."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_GET_CANDIDATE,
        "document_id": document_id,
        "candidate_id": candidate_id,
    }


def build_adopt_candidate_request(
    correlation_id: str, document_id: str, candidate_id: str
) -> Dict[str, Any]:
    """Build an ``adopt_candidate`` request (explicit, revalidated adoption)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_ADOPT,
        "document_id": document_id,
        "candidate_id": candidate_id,
    }


def build_list_versions_request(correlation_id: str, document_id: str) -> Dict[str, Any]:
    """Build a ``list_versions`` request (accepted-version history)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_LIST_VERSIONS,
        "document_id": document_id,
    }


def build_restore_version_request(
    correlation_id: str, document_id: str, version_id: str
) -> Dict[str, Any]:
    """Build a ``restore_version`` request re-pointing the Accepted Version."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_RESTORE,
        "document_id": document_id,
        "version_id": version_id,
    }


def format_document_state(result: Dict[str, Any]) -> str:
    """Render the document/version state as deterministic plain text (P4.4).

    The Working Document, Candidate and Accepted Version are shown as distinct,
    labelled sections; the candidate's fixed non-interpretation limitation is
    always printed so it is never conflated with generated behavior.
    """
    if not result:
        return ""
    doc = result.get("document") or {}
    head = result.get("head_revision") or {}
    candidate = result.get("candidate")
    accepted = result.get("accepted")

    lines = [
        "Working Document",
        f"Name: {doc.get('name', 'unknown')}",
        f"Kind: {document_kind_label(str(doc.get('kind', 'unknown')))}",
        f"Revision: {doc.get('head_revision_number', 0)}",
    ]
    fingerprint = head.get("content_fingerprint")
    if fingerprint:
        lines.append(f"Fingerprint: {fingerprint}")

    lines.append("")
    if candidate:
        lines.append("Candidate (not accepted behavior)")
        lines.append(f"Candidate: {candidate.get('candidate_id', 'unknown')}")
        lines.append(f"Bound to revision: {candidate.get('document_revision_id', 'unknown')}")
        lines.append(f"Source: {candidate.get('generation_source', 'unknown')}")
        lines.append(f"Adopted: {'yes' if candidate.get('adopted') else 'no'}")
        lines.append(f"Note: {candidate.get('limitation', '')}")
    else:
        lines.append("Candidate: none")

    lines.append("")
    if accepted:
        lines.append("Accepted Version")
        lines.append(f"Version: {accepted.get('version_id', 'unknown')}")
        lines.append(f"Accepted at: {accepted.get('accepted_at', 'unknown')}")
        lines.append(f"Restore of: {accepted.get('restore_of') or 'n/a (original adoption)'}")
    else:
        lines.append("Accepted Version: none")

    return "\n".join(lines)


def format_candidate(candidate: Dict[str, Any]) -> str:
    """Render one candidate with its current/adopted status."""
    if not candidate:
        return ""
    lines = [
        "Candidate (not accepted behavior)",
        f"Candidate: {candidate.get('candidate_id', 'unknown')}",
        f"Current: {'yes' if candidate.get('current') else 'no'}",
        f"Adopted: {'yes' if candidate.get('adopted') else 'no'}",
        f"Bound to revision: {candidate.get('document_revision_id', 'unknown')}",
        f"Package: {candidate.get('package_id', 'unknown')}",
        f"Runtime: {candidate.get('runtime_identity', 'unknown')}",
        f"Source: {candidate.get('generation_source', 'unknown')}",
        f"Note: {candidate.get('limitation', '')}",
    ]
    return "\n".join(lines)


def format_version_list(versions: List[Dict[str, Any]], current_id: Optional[str]) -> str:
    """Render the accepted-version history with the current pointer marked."""
    if not versions:
        return "No accepted versions."
    lines: List[str] = []
    for version in versions:
        version_id = str(version.get("version_id", "unknown"))
        marker = " (current)" if version_id == current_id else ""
        restored = " (restored)" if version.get("restore_of") else ""
        lines.append(f"{version_id}{marker}{restored}")
    return "\n".join(lines)


# -- Version-bound preview client vocabulary (P4.5) ------------------------
#
# The bounded preview states and binding kinds are held here as literals so the
# client never imports the document/version domain. Unknown values fall back to
# their raw token, so an unexpected token never surfaces raw protocol text.

PREVIEW_STATE_LABELS = {
    "no_document": "No document",
    "no_candidate": "No preview yet",
    "current": "Current",
    "stale": "Out of date",
    "invalid": "Invalid",
    "insufficient_evidence": "Insufficient evidence",
}

PREVIEW_KIND_LABELS = {
    "candidate": "Candidate",
    "accepted": "Accepted Version",
}

# Bounded next-step messages for each preview state. These are the user-facing
# sentences the Preview surface shows; none names an opaque id, raw reason or
# protocol token.
PREVIEW_STATE_MESSAGES = {
    "no_document": "Save the document to see a preview.",
    "no_candidate": "Save the document and choose \"Create preview\" to create a demonstration preview.",
    "current": "This preview is bound to the current document revision.",
    "stale": "The document changed after this was created.",
    "invalid": "The candidate record is invalid.",
    "insufficient_evidence": "The candidate's evidence no longer matches the current package.",
}


def preview_state_label(state: str) -> str:
    """Return the human label for a preview ``state``."""
    return PREVIEW_STATE_LABELS.get(state, state)


def preview_kind_label(kind: str) -> str:
    """Return the human label for a preview binding ``kind``."""
    return PREVIEW_KIND_LABELS.get(kind, kind)


def preview_state_message(state: str) -> str:
    """Return the user-facing next-step message for a preview ``state``."""
    return PREVIEW_STATE_MESSAGES.get(state, "")


def preview_badge(state: str, kind: Optional[str] = None) -> str:
    """Return the one-line badge word for a preview state + binding kind.

    Accepted versions read as "Accepted app"; a stale accepted app with newer
    saved requirements reads as "Newer requirements" rather than "Out of date".
    """
    if kind == "accepted":
        kind_word = "Accepted app"
    elif kind == "candidate":
        kind_word = "Candidate"
    else:
        kind_word = ""
    if state == "stale" and kind == "accepted":
        state_word = "Newer requirements"
    elif state == "stale":
        state_word = "Out of date"
    else:
        state_word = preview_state_label(state)
    return f"{kind_word} — {state_word}" if kind_word else state_word


def build_preview_request(correlation_id: str, document_id: str) -> Dict[str, Any]:
    """Build a ``preview_document`` request for one Working Document."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_DOCUMENT_PREVIEW,
        "document_id": document_id,
    }


def _preview_field_line(field: Dict[str, Any]) -> str:
    """Render one fixed form/result field as a single bounded line."""
    name = str(field.get("name", "?"))
    ftype = str(field.get("type", "unknown"))
    if ftype == "choice":
        options = ", ".join(str(o) for o in field.get("options") or [])
        return f"{name} (choice: {options})"
    if ftype in ("decimal", "integer"):
        lo = field.get("min")
        hi = field.get("max")
        if lo is not None and hi is not None:
            return f"{name} ({ftype}, between {lo} and {hi})"
        if lo is not None:
            return f"{name} ({ftype}, minimum {lo})"
        if hi is not None:
            return f"{name} ({ftype}, maximum {hi})"
    return f"{name} ({ftype})"


def _preview_yes_no(value: Any) -> str:
    """Return ``yes``/``no``/``n/a`` for a tri-state evidence flag."""
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def format_preview(preview: Dict[str, Any]) -> str:
    """Render a version-bound preview as deterministic plain text (P4.5b).

    Empty states (no document / no candidate) show only what exists and one next
    step — never global fixture schema or evidence. Fixture form/result/
    business-rule detail is shown only for a stored Candidate/Accepted record
    that validates its binding to the quotation package (current or out-of-date),
    plainly labelled as a deterministic demonstration fixture. It never shows a
    raw id or the document content, and never implies a package was executed.
    """
    if not preview:
        return ""
    doc = preview.get("document") or {}
    state = str(preview.get("state", "unknown"))
    binding = preview.get("binding")
    kind = binding.get("kind") if binding else None

    name = doc.get("name") or "Untitled"
    head_number = doc.get("revision_number", 0)

    if state == "no_document":
        return "\n".join(
            [
                f"Document: {name}",
                "",
                "No saved revision yet.",
                "Save the document to see a preview.",
            ]
        )
    if state == "no_candidate":
        return "\n".join(
            [
                f"Document: {name} — revision {head_number}",
                "",
                "This document has a saved requirement, but no app preview yet.",
                "Return to Document and choose \"Create preview\" to create a "
                "demonstration preview.",
            ]
        )
    if state == "invalid":
        return "\n".join(
            [
                f"Document: {name} — revision {head_number}",
                "",
                "The candidate record is invalid and cannot be shown.",
                "Create a new preview to replace it.",
            ]
        )
    if state == "insufficient_evidence":
        return "\n".join(
            [
                f"Document: {name} — revision {head_number}",
                "",
                "The candidate's evidence no longer matches the current "
                "quotation package.",
                "Create a new preview to replace it.",
            ]
        )

    # current / out-of-date with a validated binding + package detail.
    bound_number = (binding or {}).get("document_revision_number")
    lines: List[str] = [f"Document: {name} — revision {head_number}", ""]

    if kind == "accepted":
        if state == "stale":
            lines += [
                f"Current app: accepted at revision {bound_number}.",
                f"The document now has newer requirements at revision "
                f"{head_number}, not yet applied.",
            ]
        else:
            lines += [f"Current app: accepted at revision {bound_number}."]
    else:  # candidate
        if state == "stale":
            lines += [
                f"Candidate bound to revision {bound_number} (not adopted).",
                f"The document is now at revision {head_number}. Create a new "
                "candidate to reflect the latest requirements.",
            ]
        else:
            lines += [f"Candidate bound to revision {bound_number} (not adopted)."]

    lines += _fixture_lines(preview)
    return "\n".join(lines)


def _fixture_lines(preview: Dict[str, Any]) -> List[str]:
    """Render the deterministic-fixture detail for a bound preview record."""
    pkg = preview.get("package") or {}
    evidence = preview.get("evidence") or {}
    limitation = preview.get("limitation")
    lines: List[str] = []

    if preview.get("provenance"):
        lines += [
            "",
            "Provenance: deterministic demonstration fixture — it did not "
            "interpret the document or generate code.",
        ]

    lines += [
        "",
        f"{pkg.get('title') or 'Package'} — package {pkg.get('package_id', 'unknown')}, "
        f"schema {pkg.get('schema_version', 'unknown')}, "
        f"runtime {pkg.get('runtime_identity', 'unknown')}",
        "",
        "Inputs:",
    ]
    for field in pkg.get("form") or []:
        lines.append(f"  - {_preview_field_line(field)}")
    lines += ["", "Results:"]
    for field in pkg.get("result") or []:
        lines.append(f"  - {_preview_field_line(field)}")
    lines += [
        "",
        "Business rules: 5% member discount, free shipping at or above 100.00, "
        "a per-region fee, non-negative subtotal, and half-up rounding to two "
        "decimal places.",
        "",
        "Evidence:",
        f"  - Package validates: {_preview_yes_no(evidence.get('package_validates'))}",
        f"  - Package matches: {_preview_yes_no(evidence.get('package_matches'))}",
        f"  - Runtime matches: {_preview_yes_no(evidence.get('runtime_matches'))}",
        f"  - Validation identity matches: "
        f"{_preview_yes_no(evidence.get('validation_matches'))}",
        "  - Package executed: no",
    ]
    if limitation:
        lines += [
            "",
            "Limits:",
            f"  - {limitation}",
            "  - Result fields are the declared schema, not live output.",
        ]
    return lines


# -- App-owned document-library client vocabulary (P4.6) --------------------
#
# Request builders for the library actions (get_library, create_folder,
# rename/move/trash/restore an item). None of these carries a credential, a
# command or a path; the boundary owns every validation decision.

def build_get_library_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``get_library`` request (the joined folder/document tree)."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_LIBRARY_GET,
    }


def build_create_folder_request(
    correlation_id: str, name: str, parent_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a ``create_folder`` request (``parent_id`` absent = root)."""
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_LIBRARY_CREATE_FOLDER,
        "name": name,
    }
    if parent_id is not None:
        request["parent_id"] = parent_id
    return request


def build_rename_item_request(
    correlation_id: str, item_id: str, name: str
) -> Dict[str, Any]:
    """Build a ``rename_item`` request for a folder or document id."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_LIBRARY_RENAME,
        "item_id": item_id,
        "name": name,
    }


def build_move_item_request(
    correlation_id: str, item_id: str, parent_id: Optional[str] = None
) -> Dict[str, Any]:
    """Build a ``move_item`` request (``parent_id`` absent = root)."""
    request = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_LIBRARY_MOVE,
        "item_id": item_id,
    }
    if parent_id is not None:
        request["parent_id"] = parent_id
    return request


def build_trash_item_request(correlation_id: str, item_id: str) -> Dict[str, Any]:
    """Build a ``trash_item`` request for a folder or document id."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_LIBRARY_TRASH,
        "item_id": item_id,
    }


def build_restore_item_request(correlation_id: str, item_id: str) -> Dict[str, Any]:
    """Build a ``restore_item`` request for a trashed folder or document id."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_LIBRARY_RESTORE,
        "item_id": item_id,
    }


# -- Memory documents and exact support records (M4.3/v2a) ----------------
#
# The Qt-free half of the Developer Memory read workflow. It builds the two
# read-only requests and turns a bounded response into display rows with
# stable identities and *textual* state, provenance and origin labels, so a
# later surface never has to infer meaning from a raw token and never has to
# carry meaning in colour alone.
#
# These constants mirror the projector's vocabulary: the desktop may not import
# the Memory seam, so the shared vocabulary is restated here and a boundary test
# asserts the two stay identical.

MEMORY_DOCUMENT_TYPES = (
    "session_summary",
    "change_record",
    "decision_record",
    "issues_and_actions",
)

# The record kinds a support reference may name, in the projector's link
# vocabulary so a claim link maps onto a record request without translation.
MEMORY_RECORD_KINDS = (
    "run",
    "project",
    "work_package",
    "event",
    "evidence",
    "decision",
    "change_set",
    "code_entity_link",
    "rejection",
    "quarantine",
)

# Textual run-state labels. Every non-success state says so in words, so state
# is never carried by colour or by an unlabelled token alone.
MEMORY_STATE_LABELS = {
    "unstarted": "Unstarted (not success)",
    "open": "Open (not success)",
    "completed": "Completed (success)",
    "failed": "Failed (not success)",
    "cancelled": "Cancelled (not success)",
    "blocked": "Blocked (not success)",
    "missing_terminal": "Missing terminal (not success)",
    "unknown_outcome": "Unknown outcome (not success)",
    "unsupported": "Unsupported (not success)",
}

MEMORY_SUCCESS_STATE = "completed"

# Textual provenance labels. ``user-confirmed`` is declared because the taxonomy
# is four-valued, and it is reported as unsupported rather than presented as
# something a record can currently justify.
MEMORY_PROVENANCE_LABELS = {
    "observed": "Observed",
    "reported": "Reported by the source",
    "inferred": "Inferred",
    "user-confirmed": "User-confirmed (unsupported)",
}

MEMORY_RECORD_KIND_LABELS = {
    "run": "Run",
    "project": "Project",
    "work_package": "Work package",
    "event": "Run event",
    "evidence": "Evidence",
    "decision": "Decision",
    "change_set": "Change set",
    "code_entity_link": "Code entity",
    "rejection": "Refused record",
    "quarantine": "Quarantined redelivery",
}

# The capture origin is never stored, so it is only ever a caller declaration.
MEMORY_ORIGIN_DECLARED_LABELS = {
    "live": "Live (declared by caller)",
    "offline": "Offline deterministic (declared by caller)",
}
MEMORY_ORIGIN_UNDECLARED = "Origin not recorded (no claim is live observation)"


def memory_state_label(state: Any) -> str:
    """Return the textual label for a run state; unknown tokens pass through."""
    if isinstance(state, str) and state in MEMORY_STATE_LABELS:
        return MEMORY_STATE_LABELS[state]
    return str(state)


def memory_state_is_success(state: Any) -> bool:
    """Return True only for the one state the contract reports as success."""
    return state == MEMORY_SUCCESS_STATE


def memory_provenance_label(provenance: Any) -> str:
    """Return the textual label for a claim provenance."""
    if isinstance(provenance, str) and provenance in MEMORY_PROVENANCE_LABELS:
        return MEMORY_PROVENANCE_LABELS[provenance]
    return str(provenance)


def memory_record_kind_label(kind: Any) -> str:
    """Return the textual label for a record kind."""
    if isinstance(kind, str) and kind in MEMORY_RECORD_KIND_LABELS:
        return MEMORY_RECORD_KIND_LABELS[kind]
    return str(kind)


def memory_origin_label(origin: Any) -> str:
    """Return the textual label for a declared capture origin."""
    if isinstance(origin, str) and origin in MEMORY_ORIGIN_DECLARED_LABELS:
        return MEMORY_ORIGIN_DECLARED_LABELS[origin]
    return MEMORY_ORIGIN_UNDECLARED


def build_get_memory_documents_request(
    correlation_id: str,
    run_ids: Optional[List[str]] = None,
    documents: Optional[List[str]] = None,
    origin: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a bounded ``get_memory_documents`` request.

    No path is ever sent: the boundary roots every store itself, so a client
    cannot influence which directory is read.
    """
    request: Dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_DOCUMENTS,
    }
    if run_ids is not None:
        request["runs"] = list(run_ids)
    if documents is not None:
        request["documents"] = list(documents)
    if origin is not None:
        request["origin"] = origin
    return request


def build_get_memory_record_request(
    correlation_id: str, run_id: str, kind: str, record_id: str
) -> Dict[str, Any]:
    """Build an exact ``get_memory_record`` request for one typed identity."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_RECORD,
        "run_id": run_id,
        "kind": kind,
        "record_id": record_id,
    }


def memory_run_rows(result: Any) -> List[Dict[str, Any]]:
    """Return one bounded display row per projected run, in response order."""
    rows: List[Dict[str, Any]] = []
    if not isinstance(result, dict):
        return rows
    document_sets = result.get("document_sets")
    if not isinstance(document_sets, list):
        return rows
    for index, document_set in enumerate(document_sets):
        run = document_set.get("run") if isinstance(document_set, dict) else None
        if not isinstance(run, dict):
            continue
        documents = document_set.get("documents")
        rows.append(
            {
                "row_id": "memory-run:%d:%s" % (index, run.get("run_id")),
                "run_id": run.get("run_id"),
                "state": run.get("state"),
                "state_label": memory_state_label(run.get("state")),
                "success": bool(run.get("success")),
                "stale": bool(run.get("stale")),
                "stale_reasons": list(run.get("stale_reasons") or []),
                "baseline_status": (run.get("baseline") or {}).get("status"),
                "document_types": sorted(documents) if isinstance(documents, dict) else [],
            }
        )
    return rows


def claim_rows(document_set: Any, document_type: str) -> List[Dict[str, Any]]:
    """Return the display rows of one document's claims, in document order.

    Row identity and reference identity both come from the claim, so a surface
    never has to derive a target from row position.
    """
    rows: List[Dict[str, Any]] = []
    if not isinstance(document_set, dict):
        return rows
    documents = document_set.get("documents")
    document = documents.get(document_type) if isinstance(documents, dict) else None
    if not isinstance(document, dict):
        return rows
    run = document_set.get("run") if isinstance(document_set.get("run"), dict) else {}
    for claim in document.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        rows.append(
            {
                "claim_id": claim.get("id"),
                "text": claim.get("statement"),
                "provenance": claim.get("provenance"),
                "provenance_label": memory_provenance_label(claim.get("provenance")),
                "run_id": run.get("run_id"),
                "run_state": run.get("state"),
                "run_state_label": memory_state_label(run.get("state")),
                "limitations": list(claim.get("limitations") or []),
                "targets": claim_targets(claim),
            }
        )
    return rows


def claim_targets(claim: Any) -> List[Dict[str, Any]]:
    """Return the support targets a claim exposes, resolved and unresolved.

    A resolved target is exactly the typed identity a record request names. An
    unresolved target keeps the *requested* identity and its bounded reason and
    is marked non-actionable, so missing support stays visible instead of being
    dropped or silently substituted.
    """
    targets: List[Dict[str, Any]] = []
    if not isinstance(claim, dict):
        return targets
    for link in claim.get("links") or []:
        if not isinstance(link, dict):
            continue
        targets.append(
            {
                "target_id": "target:%s:%s" % (link.get("kind"), link.get("id")),
                "kind": link.get("kind"),
                "kind_label": memory_record_kind_label(link.get("kind")),
                "record_id": link.get("id"),
                "resolved": True,
                "reason": None,
            }
        )
    for missing in claim.get("unresolved") or []:
        if not isinstance(missing, dict):
            continue
        targets.append(
            {
                "target_id": "target:%s:%s" % (missing.get("kind"), missing.get("id")),
                "kind": missing.get("kind"),
                "kind_label": memory_record_kind_label(missing.get("kind")),
                "record_id": missing.get("id"),
                "resolved": False,
                "reason": missing.get("reason"),
            }
        )
    return targets


def record_detail_rows(view: Any) -> List[Dict[str, Any]]:
    """Return the labelled field rows of one resolved support record.

    A field the projector marks as source-reported is labelled as such, and a
    content digest is presented only as presence: the digest value never left
    the boundary, so a surface cannot display it even by accident.
    """
    rows: List[Dict[str, Any]] = []
    if not isinstance(view, dict):
        return rows
    reported = view.get("reported_fields")
    reported_names = set(reported) if isinstance(reported, list) else set()
    fields = view.get("fields")
    if not isinstance(fields, dict):
        return rows
    for name in sorted(fields):
        value = fields[name]
        rows.append(
            {
                "field": name,
                "value": value,
                "reported": name in reported_names,
                "reported_label": "Reported by the source" if name in reported_names else None,
            }
        )
    if view.get("digest_present") is not None:
        rows.append(
            {
                "field": "digest",
                "value": "present" if view.get("digest_present") else "absent",
                "reported": False,
                "reported_label": None,
            }
        )
    return rows


def format_memory_record(view: Any) -> str:
    """Return a bounded plain-text rendering of one support record."""
    if not isinstance(view, dict):
        return ""
    lines = [
        "%s %s" % (memory_record_kind_label(view.get("kind")), view.get("record_id")),
        "Owning run: %s" % view.get("run_id"),
    ]
    for row in record_detail_rows(view):
        lines.append(
            "%s: %s" % (row["field"], contract.dumps(row["value"]) if not isinstance(
                row["value"], str) else row["value"])
        )
    return "\n".join(lines)


# -- Memory query: search, timeline and resume (M4.4/v1) ------------------
#
# The facade mirrors the query model's facet and order vocabulary; the desktop
# may not import the Memory seam, so a boundary test asserts the two stay
# identical.

MEMORY_QUERY_FACETS = (
    "project",
    "work_package",
    "run_state",
    "date",
    "file",
    "symbol",
    "decision",
    "text",
    "test_result",
)

MEMORY_ORDER_RELEVANCE = "relevance"
MEMORY_ORDER_RECORDED_TIME = "recorded_time"

MEMORY_QUERY_ORDERS = (MEMORY_ORDER_RELEVANCE, MEMORY_ORDER_RECORDED_TIME)

# The most filters the surface will build into one query. It is held at or below
# the query model's per-facet term bound and its facet bound, so a query the
# surface can construct can never be one the model must reject on size.
MEMORY_MAX_FILTERS = 8

MEMORY_FACET_LABELS = {
    "project": "Project",
    "work_package": "Work package",
    "run_state": "Run state",
    "date": "Date",
    "file": "File",
    "symbol": "Symbol",
    "decision": "Decision",
    "text": "Text",
    "test_result": "Test result",
}

MEMORY_ORDER_LABELS = {
    "relevance": "Most matched facets first",
    "recorded_time": "Recorded time (ordered items only)",
}

# Facets the schema cannot satisfy. Named so a surface can disable them rather
# than offer a filter that can only ever return nothing.
MEMORY_UNSUPPORTED_FACETS = ("test_result",)

MEMORY_TIME_STATUS_LABELS = {
    "comparable": "Recorded time",
    "missing": "No recorded time",
    "incomparable": "Recorded time not comparable",
}

MEMORY_RESUME_UNSUPPORTED = "Unsupported"
MEMORY_RESUME_NOT_VERIFIED = "Not verified"


def memory_facet_label(facet: Any) -> str:
    """Return the textual label for a query facet."""
    if isinstance(facet, str) and facet in MEMORY_FACET_LABELS:
        return MEMORY_FACET_LABELS[facet]
    return str(facet)


def memory_order_label(order: Any) -> str:
    """Return the textual label for a result order."""
    if isinstance(order, str) and order in MEMORY_ORDER_LABELS:
        return MEMORY_ORDER_LABELS[order]
    return str(order)


def memory_time_status_label(status: Any) -> str:
    """Return the textual label for a hit's recorded-time status."""
    if isinstance(status, str) and status in MEMORY_TIME_STATUS_LABELS:
        return MEMORY_TIME_STATUS_LABELS[status]
    return str(status)


def build_search_memory_request(
    correlation_id: str,
    filters: Optional[Dict[str, Any]] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
    run_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a bounded ``search_memory`` request. No path is ever sent."""
    request: Dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_SEARCH,
    }
    if filters:
        request["filters"] = {
            facet: list(terms) for facet, terms in filters.items() if terms
        }
    if order is not None:
        request["order"] = order
    if limit is not None:
        request["limit"] = limit
    if run_ids is not None:
        request["runs"] = list(run_ids)
    return request


def build_memory_resume_request(
    correlation_id: str, run_ids: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Build a bounded ``memory_resume`` request. No path is ever sent."""
    request: Dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_RESUME,
    }
    if run_ids is not None:
        request["runs"] = list(run_ids)
    return request


def memory_hit_rows(result: Any) -> Dict[str, Any]:
    """Return the display rows of a search result, ordered and unordered apart.

    An item whose recorded time is missing or incomparable is never merged into
    the ordered list: it is returned in its own bucket carrying the reason, so a
    surface cannot present it as chronological.
    """
    if not isinstance(result, dict):
        return {"ordered": [], "unordered": [], "unsupported_facets": []}
    return {
        "ordered": [_hit_row(hit) for hit in result.get("results") or []],
        "unordered": [_hit_row(hit) for hit in result.get("unordered") or []],
        "unsupported_facets": [
            memory_facet_label(f) for f in result.get("unsupported_facets") or []
        ],
    }


def _hit_row(hit: Any) -> Dict[str, Any]:
    if not isinstance(hit, dict):
        return {}
    target = hit.get("target") if isinstance(hit.get("target"), dict) else {}
    return {
        "hit_id": hit.get("hit_id"),
        "kind": hit.get("kind"),
        "kind_label": memory_record_kind_label(hit.get("kind")),
        "record_id": hit.get("record_id"),
        "run_id": hit.get("run_id"),
        "run_state_label": memory_state_label(hit.get("run_state")),
        "run_success": bool(hit.get("run_success")),
        "matched_facets": [
            memory_facet_label(f) for f in hit.get("matched_facets") or []
        ],
        "matched_fields": list(hit.get("matched_fields") or []),
        "provenance_label": memory_provenance_label(hit.get("provenance")),
        "time_status_label": memory_time_status_label(hit.get("time_status")),
        "recorded_time": hit.get("recorded_time"),
        "limitations": list(hit.get("limitations") or []),
        "target": {
            "run_id": target.get("run_id"),
            "kind": target.get("kind"),
            "record_id": target.get("record_id"),
        },
        "actionable": bool(target.get("record_id")),
    }


def memory_resume_view(result: Any) -> Dict[str, Any]:
    """Return the display view of a Resume, keeping unsupported facts visible.

    Acceptance, the current baseline and verification are reported as absent
    facts, never softened into a positive statement, and a run's completion is
    never presented as acceptance.
    """
    if not isinstance(result, dict):
        return {}
    acceptance = result.get("last_accepted_change") or {}
    baseline = result.get("current_baseline") or {}
    goal = result.get("current_goal") or {}
    return {
        "run_count": result.get("run_count"),
        "runs": [
            {
                "run_id": run.get("run_id"),
                "state_label": memory_state_label(run.get("state")),
                "success": bool(run.get("success")),
                "stale": bool(run.get("stale")),
                "baseline_status": run.get("baseline_status"),
            }
            for run in result.get("runs") or []
            if isinstance(run, dict)
        ],
        "last_accepted_change": {
            "status": acceptance.get("status"),
            "label": MEMORY_RESUME_UNSUPPORTED,
            "reason": acceptance.get("reason"),
        },
        "completed_runs": [
            {
                "run_id": run.get("run_id"),
                "state_label": memory_state_label(run.get("state")),
                "target": run.get("target"),
            }
            for run in result.get("completed_runs") or []
            if isinstance(run, dict)
        ],
        "current_goal": {
            "status": goal.get("status"),
            "title": goal.get("title"),
            "provenance_label": memory_provenance_label(goal.get("provenance")),
            "limitations": list(goal.get("limitations") or []),
            "target": goal.get("target"),
        },
        "current_baseline": {
            "status": baseline.get("status"),
            "label": MEMORY_RESUME_NOT_VERIFIED,
            "reason": baseline.get("reason"),
        },
        "blockers": [
            {
                "kind": blocker.get("kind"),
                "kind_label": memory_record_kind_label(blocker.get("kind")),
                "state_label": memory_state_label(blocker.get("state"))
                if blocker.get("state")
                else None,
                "reason": blocker.get("reason"),
                "limitations": list(blocker.get("limitations") or []),
                "target": blocker.get("target"),
            }
            for blocker in result.get("blockers") or []
            if isinstance(blocker, dict)
        ],
        "unverified_claims": [
            {
                "claim_id": claim.get("claim_id"),
                "statement": claim.get("statement"),
                "provenance_label": memory_provenance_label(claim.get("provenance")),
                "reason": claim.get("reason"),
                "target": claim.get("target"),
            }
            for claim in result.get("unverified_claims") or []
            if isinstance(claim, dict)
        ],
        "next_actions": [
            {
                "text": action.get("text"),
                "provenance_label": memory_provenance_label(action.get("provenance")),
                "run_id": action.get("run_id"),
                "target": action.get("target"),
            }
            for action in result.get("next_actions") or []
            if isinstance(action, dict)
        ],
        "limitations": list(result.get("limitations") or []),
    }


# -- Memory revisions: correction, history and effective (M4.5/v1a) ------

MEMORY_CORRECTION_OPERATIONS = ("keep", "merge", "supersede", "reject")

MEMORY_CORRECTION_STATES = ("draft", "confirmed", "rejected", "superseded", "archived")

MEMORY_OPERATION_LABELS = {
    "keep": "Keep as generated",
    "merge": "Replace with human text",
    "supersede": "Supersede an earlier revision",
    "reject": "Reject this claim",
}

# Every state says what it means, and only ``confirmed`` says the overlay is
# authority — colour is never the cue.
MEMORY_CORRECTION_STATE_LABELS = {
    "draft": "Draft (not authority)",
    "confirmed": "Confirmed (authority)",
    "rejected": "Rejected (history only)",
    "superseded": "Superseded (history only)",
    "archived": "Archived (history only)",
}

MEMORY_CONFLICT_LABEL = "Unresolved conflict"

MEMORY_EFFECTIVE_LIMITATION_KEYS = (
    "never changes a normalized record",
    "not verified",
    "not acceptance",
    "never guessed",
)


def memory_operation_label(operation: Any) -> str:
    """Return the textual label for a correction operation."""
    if isinstance(operation, str) and operation in MEMORY_OPERATION_LABELS:
        return MEMORY_OPERATION_LABELS[operation]
    return str(operation)


def memory_correction_state_label(state: Any) -> str:
    """Return the textual label for a correction state."""
    if isinstance(state, str) and state in MEMORY_CORRECTION_STATE_LABELS:
        return MEMORY_CORRECTION_STATE_LABELS[state]
    return str(state)


def build_memory_history_request(
    correlation_id: str, run_id: str, document_type: Optional[str] = None
) -> Dict[str, Any]:
    """Build a bounded ``get_memory_history`` request. No path is ever sent."""
    request: Dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_HISTORY,
        "run_id": run_id,
    }
    if document_type is not None:
        request["document_type"] = document_type
    return request


def build_memory_effective_request(
    correlation_id: str, run_id: str, document_type: str
) -> Dict[str, Any]:
    """Build a bounded ``resolve_memory_effective`` request."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_EFFECTIVE,
        "run_id": run_id,
        "document_type": document_type,
    }


def build_memory_correction_request(
    correlation_id: str,
    run_id: str,
    document_type: str,
    operation: str,
    claim_id: str,
    text: Optional[str] = None,
    actor: Optional[str] = None,
    supersedes: Optional[List[str]] = None,
    source_id: Optional[str] = None,
    expected_version_id: Optional[str] = None,
    state: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a bounded ``append_memory_correction`` request.

    The target is a claim's stable projected identity. No path is ever sent, and
    the baseline the correction binds to is computed by the boundary from the
    document it projects — never asserted by the caller.
    """
    request: Dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_CORRECTION,
        "run_id": run_id,
        "document_type": document_type,
        "operation": operation,
        "target": {"claim_id": claim_id},
    }
    if text is not None:
        request["text"] = text
    if actor is not None:
        request["actor"] = actor
    if supersedes:
        request["supersedes"] = list(supersedes)
    if source_id is not None:
        request["source_id"] = source_id
    if expected_version_id is not None:
        request["expected_version_id"] = expected_version_id
    if state is not None:
        request["state"] = state
    return request


def memory_history_view(result: Any) -> Dict[str, Any]:
    """Return the display view of a history, states and limits kept textual."""
    if not isinstance(result, dict):
        return {}
    return {
        "run_id": result.get("run_id"),
        "document_type": result.get("document_type"),
        "generated_versions": [
            {
                "version_id": version.get("id"),
                "revision": version.get("revision"),
                "document_type_label": version.get("document_type"),
            }
            for version in result.get("generated_versions") or []
            if isinstance(version, dict)
        ],
        "corrections": [
            {
                "correction_id": correction.get("id"),
                "operation": correction.get("operation"),
                "operation_label": memory_operation_label(correction.get("operation")),
                "state": correction.get("state"),
                "state_label": memory_correction_state_label(correction.get("state")),
                "target": correction.get("target"),
                "text": correction.get("text"),
                "actor": correction.get("actor"),
                "created_at": correction.get("created_at"),
                "supersedes": list(correction.get("supersedes") or []),
                "is_superseded": correction.get("id") in (result.get("superseded_ids") or []),
                "is_authority": correction.get("state") == "confirmed",
                "base_recorded": bool(correction.get("base_recorded")),
            }
            for correction in result.get("corrections") or []
            if isinstance(correction, dict)
        ],
        "superseded_ids": list(result.get("superseded_ids") or []),
        "limitations": list(result.get("limitations") or []),
    }


def memory_effective_view(result: Any) -> Dict[str, Any]:
    """Return the display view of an effective document.

    A claim keeps both its generated and its effective statement, so a reader can
    always see what the projection said and what a human made of it, and an
    unresolved conflict is listed with its reason rather than hidden.
    """
    if not isinstance(result, dict):
        return {}
    return {
        "run_id": result.get("run_id"),
        "document_type": result.get("document_type"),
        "generated": result.get("generated") or {},
        "claims": [
            {
                "claim_id": claim.get("claim_id"),
                "generated_statement": claim.get("generated_statement"),
                "effective_statement": claim.get("effective_statement"),
                "changed": claim.get("generated_statement")
                != claim.get("effective_statement"),
                "generated_provenance_label": memory_provenance_label(
                    claim.get("generated_provenance")
                ),
                "effective_provenance_label": memory_provenance_label(
                    claim.get("effective_provenance")
                ),
                "rejected": bool(claim.get("rejected")),
                "overlay": claim.get("overlay"),
            }
            for claim in result.get("claims") or []
            if isinstance(claim, dict)
        ],
        "conflicts": [
            {
                "label": MEMORY_CONFLICT_LABEL,
                "outcome": conflict.get("outcome"),
                "correction_id": conflict.get("correction_id"),
                "operation_label": memory_operation_label(conflict.get("operation")),
                "state_label": memory_correction_state_label(conflict.get("state")),
                "reason": conflict.get("reason"),
                "target": conflict.get("target"),
                "base_recorded": bool(conflict.get("base_recorded")),
            }
            for conflict in result.get("conflicts") or []
            if isinstance(conflict, dict)
        ],
        "applied_correction_ids": list(result.get("applied_correction_ids") or []),
        "limitations": list(result.get("limitations") or []),
    }


# -- Memory review: corrections, confirmation and history (M4.5/v1b) ------
#
# The framework-free half of the correction workflow: what a claim currently
# reads as, what a human made of it, which choices a conflict can honestly
# offer, and a stable per-attempt identity for an append.

# The four operations a conflict can be resolved with.
MEMORY_REVIEW_OPERATIONS = ("keep", "merge", "supersede", "reject")

# What each choice means to a reader, stated as an outcome rather than a verb.
MEMORY_CONFLICT_CHOICE_LABELS = {
    "keep": "Keep the generated claim",
    "merge": "Merge into a new revision",
    "supersede": "Supersede with a new revision",
    "reject": "Reject the claim",
}

# Why a choice can be impossible: without a claim in the current document there
# is nothing for a revision to bind to, and the boundary would refuse it.
MEMORY_CONFLICT_UNAVAILABLE = (
    "the corrected claim is not in the current document, so no revision can bind to it"
)

MEMORY_AUTHORITY_LABEL_AUTHORITY = "Authority"
MEMORY_AUTHORITY_LABEL_HISTORY = "History only"


def memory_authority_label(state: Any) -> str:
    """Return whether a revision state is authority or history only."""
    return (
        MEMORY_AUTHORITY_LABEL_AUTHORITY
        if state in ("confirmed", "rejected")
        else MEMORY_AUTHORITY_LABEL_HISTORY
    )


def memory_correction_source_id(
    document_type: Any, claim_id: Any, operation: Any, attempt_token: Any
) -> str:
    """Return a stable, bounded source id for one append attempt.

    Stability is per *attempt*, not per click: retrying the same attempt after a
    timeout keeps its identity and is idempotent, while starting a new attempt
    produces a new revision. Nothing here is derived from human text, so the
    identity never carries content.
    """
    basis = "%s|%s|%s|%s" % (document_type, claim_id, operation, attempt_token)
    return "corr:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def memory_conflict_choices(
    conflict: Any, known_claim_ids: Any = ()
) -> List[Dict[str, Any]]:
    """Return the choices one conflict can honestly offer.

    A choice is offered only when it can be represented: every operation needs
    the corrected claim to exist in the current document, because that is what a
    new revision binds to. When it does not, every choice is disabled with the
    same bounded reason rather than hidden or silently attempted.
    """
    target = conflict.get("target") if isinstance(conflict, dict) else None
    claim_id = target.get("claim_id") if isinstance(target, dict) else None
    known = set(known_claim_ids) if known_claim_ids else set()
    available = isinstance(claim_id, str) and claim_id in known
    choices = []
    for operation in MEMORY_REVIEW_OPERATIONS:
        choices.append(
            {
                "operation": operation,
                "label": MEMORY_CONFLICT_CHOICE_LABELS[operation],
                "enabled": available,
                "reason": None if available else MEMORY_CONFLICT_UNAVAILABLE,
            }
        )
    return choices


def memory_review_view(effective: Any, history: Any) -> Dict[str, Any]:
    """Compose the comparison, conflict choices and history of one review.

    The generated statement is always present alongside the effective one, so a
    reader can never lose sight of what the projection said. Conflicts carry
    their choices, and history carries an authority label per revision.
    """
    effective_view = memory_effective_view(effective)
    history_view = memory_history_view(history)
    claims = effective_view.get("claims") or []
    claim_ids = [claim.get("claim_id") for claim in claims if claim.get("claim_id")]
    conflicts = []
    for conflict in effective_view.get("conflicts") or []:
        entry = dict(conflict)
        entry["choices"] = memory_conflict_choices(conflict, claim_ids)
        conflicts.append(entry)

    versions = history_view.get("generated_versions") or []
    corrections = []
    for correction in history_view.get("corrections") or []:
        entry = dict(correction)
        entry["authority_label"] = memory_authority_label(correction.get("state"))
        corrections.append(entry)

    return {
        "run_id": effective_view.get("run_id") or history_view.get("run_id"),
        "document_type": effective_view.get("document_type")
        or history_view.get("document_type"),
        "generated": effective_view.get("generated") or {},
        "claims": claims,
        "conflicts": conflicts,
        "generated_versions": versions,
        "corrections": corrections,
        "superseded_ids": history_view.get("superseded_ids") or [],
        "limitations": list(effective_view.get("limitations") or [])
        + list(history_view.get("limitations") or []),
    }


# -- M4.5/v2c Code Twin link review ---------------------------------------
#
# The desktop renders the link pair's own returned vocabulary. It never imports
# the link domain, so the words below are presentation only: a verdict this
# module does not recognise is shown as an unknown rather than mapped away, and
# nothing here decides that a link is current.

MEMORY_TWIN_FRESHNESS_LABELS = {
    "current": "Current",
    "historical": "Historical",
    "stale": "Stale",
    "missing": "Missing",
    "unsupported": "Unsupported",
}

# The two states in which a link may be opened. Actionability is *returned*, so
# this pair exists only to narrow: the desktop refuses a verdict that claimed
# actionability in a state the protocol never calls actionable. It can never
# widen a returned claim, and a link the boundary called non-actionable stays
# inert whatever state accompanies it.
MEMORY_TWIN_OPENABLE_STATES = ("current", "stale")

# The Twin's own artifact kinds. A Memory record states only whether its claim
# is a file or a symbol; which of the three symbol kinds an exact locator is can
# be said by the authoritative Twin alone, so each candidate is offered as its
# own exact identity and a miss is answered as a miss rather than guessed here.
MEMORY_TWIN_FILE_KIND = "file"
MEMORY_TWIN_SYMBOL_KINDS = ("class", "function", "method")

MEMORY_TWIN_KIND_LABELS = {
    "file": "File",
    "class": "Class",
    "function": "Function",
    "method": "Method",
}

# The Memory record kind that carries a source claim, and the prefix of every
# Twin entity identity.
MEMORY_TWIN_LINK_RECORD_KIND = "code_entity_link"
MEMORY_TWIN_ENTITY_PREFIX = "artifact:"

MEMORY_TWIN_UNKNOWN = "Unknown"
MEMORY_TWIN_NOT_REPORTED = "not reported"
MEMORY_TWIN_NOT_AVAILABLE = "not available"
MEMORY_TWIN_LIMITATION_NONE = "None reported"

# Bounded reasons the desktop refuses to open, so a refusal is always a sentence
# and never an identifier, a path or a digest.
MEMORY_TWIN_REFUSED_UNREADABLE = "the source entity could not be read"
MEMORY_TWIN_REFUSED_NO_ARTIFACT = "no source artifact was returned for this entity"
MEMORY_TWIN_REFUSED_SUBSTITUTE = "the returned artifact is not the linked entity"
MEMORY_TWIN_REFUSED_NO_SELECTOR = "this identity has no exact source selector"
MEMORY_TWIN_REFUSED_NOT_ACTIONABLE = "the link is not actionable"


def memory_twin_kind_label(kind: Any) -> str:
    """Return the display word for one Twin artifact kind."""
    if isinstance(kind, str) and kind in MEMORY_TWIN_KIND_LABELS:
        return MEMORY_TWIN_KIND_LABELS[kind]
    return MEMORY_TWIN_UNKNOWN


def memory_twin_freshness_label(state: Any) -> str:
    """Return the display word for a returned freshness verdict."""
    if isinstance(state, str) and state in MEMORY_TWIN_FRESHNESS_LABELS:
        return MEMORY_TWIN_FRESHNESS_LABELS[state]
    return MEMORY_TWIN_UNKNOWN


def memory_twin_actionable_label(verdict: Any) -> str:
    """Return returned actionability as a word, never as colour alone.

    A verdict that did not report actionability reads as *not reported* rather
    than as either answer, so an absent field can never be mistaken for consent.
    """
    actionable = verdict.get("actionable") if isinstance(verdict, dict) else None
    if actionable is True:
        return "Yes"
    if actionable is False:
        return "No"
    return "Not reported"


def _revision_text(value: Any) -> str:
    """Return one bounded revision as text; a non-integer is not a revision."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return MEMORY_TWIN_NOT_REPORTED


def memory_twin_identity_label(kind: Any, body: Any) -> str:
    """Return the bounded display label of one exact Twin identity."""
    return "%s %s" % (memory_twin_kind_label(kind), body)


def memory_twin_candidate_identities(view: Any) -> List[Dict[str, Any]]:
    """Return the exact Twin identities one record view admits, in fixed order.

    The bodies come only from the record's own allowlisted ``path`` and
    ``symbol`` fields — never from a caller, from a path this program resolved,
    or from the text of a surface. A record that states a file claim admits
    exactly one identity. A symbol claim admits one per symbol kind, because
    only the authoritative Twin can say which kind an exact locator is: the
    desktop offers each exact identity and lets the boundary answer, so an
    ambiguity is reported as a miss or as several exact hits, never resolved by
    preferring one spelling over another.
    """
    fields = view.get("fields") if isinstance(view, dict) else None
    if not isinstance(fields, dict):
        return []
    path = fields.get("path")
    symbol = fields.get("symbol")
    declared = fields.get("entity_kind")
    bodies: tuple = ()
    if declared == "file" and isinstance(path, str) and path:
        bodies = ((MEMORY_TWIN_FILE_KIND, path),)
    elif declared == "symbol" and isinstance(symbol, str) and symbol:
        bodies = tuple((kind, symbol) for kind in MEMORY_TWIN_SYMBOL_KINDS)
    elif isinstance(path, str) and path:
        bodies = ((MEMORY_TWIN_FILE_KIND, path),)
    elif isinstance(symbol, str) and symbol:
        bodies = tuple((kind, symbol) for kind in MEMORY_TWIN_SYMBOL_KINDS)
    candidates: List[Dict[str, Any]] = []
    for kind, body in bodies:
        candidates.append(
            {
                "entity_id": "%s%s:%s" % (MEMORY_TWIN_ENTITY_PREFIX, kind, body),
                "entity_kind": kind,
                "body": body,
                "label": memory_twin_identity_label(kind, body),
            }
        )
    return candidates


def memory_twin_selector_for(entity_id: Any) -> Optional[str]:
    """Return the existing typed selector of a returned identity, or ``None``.

    An identity is exactly ``artifact:<kind>:<body>``, and the read-only Twin
    request selects a projection by that body. Only that exact split is
    performed — an identity that is not of that shape, or that names a kind the
    Twin does not own, yields no selector at all rather than a guess.
    """
    if not isinstance(entity_id, str):
        return None
    if not entity_id.startswith(MEMORY_TWIN_ENTITY_PREFIX):
        return None
    remainder = entity_id[len(MEMORY_TWIN_ENTITY_PREFIX):]
    kind, separator, body = remainder.partition(":")
    if not separator or kind not in MEMORY_TWIN_KIND_LABELS or not body:
        return None
    return body


def memory_twin_link_is_openable(link: Any, verdict: Any) -> bool:
    """Return True only for a returned actionable verdict about this exact link.

    Three conditions have to hold together, and all three are read from what the
    boundary returned: the verdict must be actionable, its state must be one the
    protocol calls actionable, and it must be a verdict about *this* link. A
    verdict for another record, run or revision is never a licence to open.
    """
    if not isinstance(link, dict) or not isinstance(verdict, dict):
        return False
    if verdict.get("actionable") is not True:
        return False
    if verdict.get("freshness") not in MEMORY_TWIN_OPENABLE_STATES:
        return False
    return (
        verdict.get("entity_id") == link.get("entity_id")
        and verdict.get("entity_kind") == link.get("entity_kind")
        and verdict.get("workspace_id") == link.get("workspace_id")
        and verdict.get("memory_run_id") == link.get("memory_run_id")
        and verdict.get("memory_record_id") == link.get("memory_record_id")
        and verdict.get("recorded_revision") == link.get("recorded_revision")
    )


def memory_twin_open_refusal(bundle: Any, entity_id: Any) -> Optional[str]:
    """Return a bounded reason the bundle is not the exact entity, or ``None``.

    Opening is an exact-identity claim. The Twin request answers with whatever
    artifact its own selector resolved, and that artifact's id must be the
    identity that was asked for. A different artifact — including a same-named
    entity from another module, or a current artifact standing in for a
    retained one — is refused without opening anything.
    """
    if not isinstance(bundle, dict):
        return MEMORY_TWIN_REFUSED_UNREADABLE
    artifact = bundle.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("id"):
        return MEMORY_TWIN_REFUSED_NO_ARTIFACT
    if artifact.get("id") != entity_id:
        return MEMORY_TWIN_REFUSED_SUBSTITUTE
    return None


def memory_twin_link_records(document_set: Any) -> List[Dict[str, Any]]:
    """Return the distinct source-claim records one run's documents name.

    A record id comes from the claim the projector emitted, so no surface ever
    derives a record identity from a row position or a display label. The order
    follows the documents and their claims, which are themselves ordered.
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(document_set, dict):
        return records
    documents = document_set.get("documents")
    if not isinstance(documents, dict):
        return records
    seen = set()
    for document_type in sorted(documents):
        document = documents.get(document_type)
        if not isinstance(document, dict):
            continue
        for claim in document.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            for link in claim.get("links") or []:
                if not isinstance(link, dict):
                    continue
                if link.get("kind") != MEMORY_TWIN_LINK_RECORD_KIND:
                    continue
                record_id = link.get("id")
                if not isinstance(record_id, str) or record_id in seen:
                    continue
                seen.add(record_id)
                records.append(
                    {
                        "record_id": record_id,
                        "label": "%s %s"
                        % (memory_record_kind_label(MEMORY_TWIN_LINK_RECORD_KIND), record_id),
                    }
                )
    return records


def memory_link_rows(link: Any) -> List[tuple]:
    """Return the bounded review rows of one typed link."""
    if not isinstance(link, dict):
        return []
    return [
        ("Entity identity", str(link.get("entity_id") or MEMORY_TWIN_NOT_REPORTED)),
        ("Entity kind", memory_twin_kind_label(link.get("entity_kind"))),
        ("Recorded revision", _revision_text(link.get("recorded_revision"))),
        ("Memory run", str(link.get("memory_run_id") or MEMORY_TWIN_NOT_REPORTED)),
        ("Memory record", str(link.get("memory_record_id") or MEMORY_TWIN_NOT_REPORTED)),
    ]


def memory_freshness_rows(verdict: Any) -> List[tuple]:
    """Return the bounded review rows of one returned freshness verdict.

    The state and the returned actionability are both words, so the two things
    a reader must not read from colour alone are never colour alone.
    """
    if not isinstance(verdict, dict):
        return []
    reason = verdict.get("reason")
    return [
        ("State", memory_twin_freshness_label(verdict.get("freshness"))),
        ("Actionable", memory_twin_actionable_label(verdict)),
        ("Recorded revision", _revision_text(verdict.get("recorded_revision"))),
        ("Current revision", _revision_text(verdict.get("current_revision"))),
        ("Limitation", str(reason) if isinstance(reason, str) and reason else MEMORY_TWIN_LIMITATION_NONE),
    ]


def build_memory_code_link_request(
    correlation_id: str,
    entity_id: str,
    entity_kind: str,
    run_id: str,
    record_id: str,
    kind: str = MEMORY_TWIN_LINK_RECORD_KIND,
) -> Dict[str, Any]:
    """Build a ``get_memory_code_link`` request for one exact Twin identity.

    The request carries the identity a caller asks about; the *revision* is
    never sent, because the boundary reads it from the authoritative store.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_CODE_LINK,
        "entity_id": entity_id,
        "entity_kind": entity_kind,
        "run_id": run_id,
        "record_id": record_id,
        "kind": kind,
    }


def build_memory_code_freshness_request(
    correlation_id: str, link: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a ``resolve_memory_code_freshness`` request for one returned link.

    Exactly the link's own returned fields are echoed — nothing is added, and
    no comparison input is supplied by the client.
    """
    request: Dict[str, Any] = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MEMORY_CODE_FRESHNESS,
    }
    if isinstance(link, dict):
        for field in (
            "link_schema_version",
            "workspace_id",
            "entity_id",
            "entity_kind",
            "memory_run_id",
            "memory_record_id",
            "recorded_revision",
        ):
            if field in link:
                request[field] = link[field]
    return request


__all__ = [
    "STATE_IDLE",
    "STATE_RUNNING",
    "STATE_SUCCESS",
    "STATE_FAILED",
    "STATE_BLOCKED",
    "STATE_UNAVAILABLE",
    "TWIN_EMPTY",
    "TWIN_LOADING",
    "TWIN_AVAILABLE",
    "TWIN_STALE",
    "TWIN_CONFLICT",
    "TWIN_UNSUPPORTED",
    "TWIN_STATES",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_READINESS_STATE_LABELS",
    "provider_readiness_state_label",
    "PROVIDER_STATUS_PENDING",
    "PROVIDER_STATUS_CONFIGURED",
    "PROVIDER_STATUS_MISSING_CREDENTIAL",
    "PROVIDER_STATUS_UNAVAILABLE",
    "PROVIDER_STATUS_INVALID_CONFIG",
    "PROVIDER_STATUS_FAILED",
    "PROVIDER_STATUS_MESSAGES",
    "provider_status_message",
    "CREDENTIAL_ACTION_MESSAGES",
    "CREDENTIAL_FAILURE_MESSAGES",
    "CREDENTIAL_ACTION_PENDING",
    "credential_action_message",
    "CREDENTIAL_MASK",
    "PROFILE_ACTION_MESSAGES",
    "PROFILE_FAILURE_MESSAGES",
    "profile_failure_message",
    "REPOSITORY_UNVERIFIED",
    "VALIDATION_IDLE",
    "VALIDATION_RUNNING",
    "VALIDATION_OK",
    "VALIDATION_FAILED",
    "LineBuffer",
    "ResponseRouter",
    "build_fixture_task",
    "build_request",
    "build_scan_task",
    "build_scan_request",
    "build_open_project_request",
    "build_get_tree_request",
    "build_get_document_request",
    "build_sync_twin_request",
    "build_get_twin_request",
    "build_get_anchor_request",
    "twin_state_from_sync",
    "TWIN_SOURCE_SUFFIXES",
    "is_twin_source_path",
    "behavior_node_label",
    "format_twin_projection",
    "format_twin_sync",
    "BLOCK_TYPE_LABELS",
    "OPERATION_LABELS",
    "INTENT_CLASS_LABELS",
    "block_type_label",
    "operation_label",
    "intent_class_label",
    "PROPOSAL_STATE_LABELS",
    "proposal_state_label",
    "build_get_code_map_request",
    "build_save_draft_request",
    "build_get_draft_request",
    "build_discard_draft_request",
    "build_reset_draft_request",
    "build_compare_draft_request",
    "build_generate_intent_delta_request",
    "build_plan_proposal_request",
    "build_get_readiness_request",
    "build_manage_credential_request",
    "build_remove_credential_request",
    "build_get_profiles_request",
    "build_add_profile_request",
    "build_rename_profile_request",
    "build_delete_profile_request",
    "build_set_active_profile_request",
    "format_procedural_document",
    "format_entity_list",
    "format_draft_operations",
    "format_intent_delta",
    "format_proposal",
    "format_provider_readiness",
    "ADVISORY_STATE_LABELS",
    "ADVISORY_UNAVAILABLE_REASONS",
    "advisory_state_label",
    "advisory_unavailable_reason_label",
    "build_prepare_advisory_request",
    "build_plan_advisory_request",
    "format_advisory_disclosure",
    "format_advisory_result",
    "DELTA_INTERPRET_STATE_LABELS",
    "delta_interpret_state_label",
    "build_prepare_rule_delta_request",
    "build_interpret_rule_delta_request",
    "format_delta_disclosure",
    "format_delta_interpret_result",
    "RUN_STATE_LABELS",
    "run_state_label",
    "build_get_package_request",
    "build_run_package_request",
    "format_run_result",
    "DOCUMENT_KIND_LABELS",
    "DOCUMENT_FAILURE_MESSAGES",
    "document_kind_label",
    "document_failure_message",
    "build_create_document_request",
    "build_open_document_request",
    "build_save_document_request",
    "build_list_documents_request",
    "build_create_candidate_request",
    "build_get_candidate_request",
    "build_adopt_candidate_request",
    "build_list_versions_request",
    "build_restore_version_request",
    "format_document_state",
    "format_candidate",
    "format_version_list",
    "PREVIEW_STATE_LABELS",
    "PREVIEW_KIND_LABELS",
    "PREVIEW_STATE_MESSAGES",
    "preview_state_label",
    "preview_kind_label",
    "preview_state_message",
    "preview_badge",
    "build_preview_request",
    "format_preview",
    "build_get_library_request",
    "build_create_folder_request",
    "build_rename_item_request",
    "build_move_item_request",
    "build_trash_item_request",
    "build_restore_item_request",
    "default_fixture_root",
    "resolve_backend_command",
    "resolve_credential_host_command",
    "MEMORY_DOCUMENT_TYPES",
    "MEMORY_RECORD_KINDS",
    "MEMORY_STATE_LABELS",
    "MEMORY_SUCCESS_STATE",
    "MEMORY_PROVENANCE_LABELS",
    "MEMORY_RECORD_KIND_LABELS",
    "MEMORY_ORIGIN_DECLARED_LABELS",
    "MEMORY_ORIGIN_UNDECLARED",
    "memory_state_label",
    "memory_state_is_success",
    "memory_provenance_label",
    "memory_record_kind_label",
    "memory_origin_label",
    "build_get_memory_documents_request",
    "build_get_memory_record_request",
    "memory_run_rows",
    "claim_rows",
    "claim_targets",
    "record_detail_rows",
    "format_memory_record",
    "MEMORY_QUERY_FACETS",
    "MEMORY_QUERY_ORDERS",
    "MEMORY_ORDER_RELEVANCE",
    "MEMORY_ORDER_RECORDED_TIME",
    "MEMORY_MAX_FILTERS",
    "MEMORY_FACET_LABELS",
    "MEMORY_ORDER_LABELS",
    "MEMORY_UNSUPPORTED_FACETS",
    "MEMORY_TIME_STATUS_LABELS",
    "MEMORY_RESUME_UNSUPPORTED",
    "MEMORY_RESUME_NOT_VERIFIED",
    "memory_facet_label",
    "memory_order_label",
    "memory_time_status_label",
    "build_search_memory_request",
    "build_memory_resume_request",
    "memory_hit_rows",
    "memory_resume_view",
    "MEMORY_CORRECTION_OPERATIONS",
    "MEMORY_CORRECTION_STATES",
    "MEMORY_OPERATION_LABELS",
    "MEMORY_CORRECTION_STATE_LABELS",
    "MEMORY_CONFLICT_LABEL",
    "MEMORY_EFFECTIVE_LIMITATION_KEYS",
    "memory_operation_label",
    "memory_correction_state_label",
    "build_memory_history_request",
    "build_memory_effective_request",
    "build_memory_correction_request",
    "memory_history_view",
    "memory_effective_view",
    "MEMORY_REVIEW_OPERATIONS",
    "MEMORY_CONFLICT_CHOICE_LABELS",
    "MEMORY_CONFLICT_UNAVAILABLE",
    "MEMORY_AUTHORITY_LABEL_AUTHORITY",
    "MEMORY_AUTHORITY_LABEL_HISTORY",
    "memory_authority_label",
    "memory_correction_source_id",
    "memory_conflict_choices",
    "memory_review_view",
    "MEMORY_TWIN_FRESHNESS_LABELS",
    "MEMORY_TWIN_OPENABLE_STATES",
    "MEMORY_TWIN_FILE_KIND",
    "MEMORY_TWIN_SYMBOL_KINDS",
    "MEMORY_TWIN_KIND_LABELS",
    "MEMORY_TWIN_LINK_RECORD_KIND",
    "MEMORY_TWIN_ENTITY_PREFIX",
    "MEMORY_TWIN_LIMITATION_NONE",
    "MEMORY_TWIN_REFUSED_UNREADABLE",
    "MEMORY_TWIN_REFUSED_NO_ARTIFACT",
    "MEMORY_TWIN_REFUSED_SUBSTITUTE",
    "MEMORY_TWIN_REFUSED_NO_SELECTOR",
    "MEMORY_TWIN_REFUSED_NOT_ACTIONABLE",
    "memory_twin_kind_label",
    "memory_twin_freshness_label",
    "memory_twin_actionable_label",
    "memory_twin_identity_label",
    "memory_twin_candidate_identities",
    "memory_twin_selector_for",
    "memory_twin_link_is_openable",
    "memory_twin_open_refusal",
    "memory_twin_link_records",
    "memory_link_rows",
    "memory_freshness_rows",
    "build_memory_code_link_request",
    "build_memory_code_freshness_request",
]
