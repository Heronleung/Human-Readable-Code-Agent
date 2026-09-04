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


def provider_status_message(status: str) -> str:
    """Return the user-facing message for a provider status ``status``."""
    return PROVIDER_STATUS_MESSAGES.get(status, status)


def credential_action_message(state: str) -> str:
    """Return the user-facing message for a credential action ``state``."""
    return CREDENTIAL_ACTION_MESSAGES.get(state, state)

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


def build_manage_credential_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``manage_credential`` request (backend-owned secure enrollment).

    The request carries only the correlation id and action name — never a key,
    a path or a task. The boundary owns the native secure prompt and writes the
    credential straight to the platform store; the desktop never sees the key.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_MANAGE_CREDENTIAL,
    }


def build_remove_credential_request(correlation_id: str) -> Dict[str, Any]:
    """Build a ``remove_credential`` request (backend-owned delete).

    Carries only the correlation id and action name; the boundary deletes the
    stored credential and returns a redacted result. No key is ever surfaced.
    """
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": correlation_id,
        "action": contract.ACTION_REMOVE_CREDENTIAL,
    }


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
    "credential_action_message",
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
    "format_procedural_document",
    "format_entity_list",
    "format_draft_operations",
    "format_intent_delta",
    "format_proposal",
    "format_provider_readiness",
    "default_fixture_root",
    "resolve_backend_command",
]
