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
    "behavior_node_label",
    "format_twin_projection",
    "format_twin_sync",
    "default_fixture_root",
    "resolve_backend_command",
]
