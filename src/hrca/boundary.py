"""Headless local application boundary (P3.2).

The boundary is the single place that turns a versioned desktop-to-core request
into the deterministic read-only result. It:

* reads newline-delimited JSON requests from stdin,
* validates each request and rejects unknown contract versions and
  non-allowlisted actions with bounded, sanitized errors,
* dispatches scan-pipeline actions (``scan`` / ``read`` / ``analyze`` /
  ``inspect`` / ``plan``) to the task intake (:mod:`hrca.planning`), planner,
  scanner (:mod:`hrca.scanner`) and report builder (:mod:`hrca.report`),
* dispatches workspace actions (``open_project`` / ``get_tree`` /
  ``get_document``) to the read-only filesystem policy in
  :mod:`hrca.workspace`,
* writes exactly one JSON response line per request to stdout.

The boundary is a review and safety *workflow* boundary, not an
operating-system privilege boundary: it owns contract validation, action
allowlisting, the accepted-project-root session, orchestration of the
deterministic core, path containment for workspace access, and bounded error
mapping — but it performs no repository write, Git operation, command
execution, network access, or provider call.

Stdio discipline: stdin and stdout are reconfigured to UTF-8 with newline
``"\\n"``, messages are serialized with ``ensure_ascii=True``, stdout is
reserved exclusively for protocol messages, and every log or diagnostic (there
are none in normal operation) goes to stderr.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TextIO, Sequence

from . import contract, twin, twin_draft, twin_store, workspace
from .planning import TaskValidationError, build_plan, validate_task
from .report import build_report
from .scanner import scan_directory

# Fixed, read-only next action reported by this slice: the boundary never
# performs a repository action.
_NEXT_ACTION = "Report only; no repository action performed."


class WorkspaceSession:
    """In-memory accepted-project state owned by one boundary loop (P3.2/P3.3).

    ``open_project`` sets the accepted root; ``get_tree`` / ``get_document``
    operate relative to it, and the P3.3 Twin actions (``sync_twin`` /
    ``get_twin`` / ``get_anchor``) persist and read the workspace's Twin under
    ``store_base`` (per-workspace app-data outside the accepted root). The
    session is per-``run_loop`` invocation, so a fresh boundary process (or a
    fresh test loop) always starts with no project accepted.
    """

    def __init__(self, store_base: Optional[str] = None) -> None:
        self.root: Optional[str] = None
        self.store_base: str = store_base or twin_store.app_data_dir()

    def open(self, root: str) -> None:
        self.root = root

    def close(self) -> None:
        self.root = None


def _configure_stdio(stream: TextIO) -> TextIO:
    """Reconfigure a standard stream to UTF-8 with ``"\\n"`` newlines.

    ``reconfigure`` exists on real standard streams (not on ``io.StringIO``
    test doubles), so it is applied only when available.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", newline="\n")
    return stream


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the headless boundary loop over stdin/stdout; returns an exit code."""
    _configure_stdio(sys.stdin)
    _configure_stdio(sys.stdout)
    _configure_stdio(sys.stderr)
    # ``argv`` is accepted for parity with other entry points; the ``--serve``
    # sentinel requires no further handling because this module *is* the serve
    # loop whenever it is invoked.
    return run_loop(sys.stdin, sys.stdout, sys.stderr)


def run_loop(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    store_base: Optional[str] = None,
) -> int:
    """Read requests from ``stdin``, write one response line per request.

    ``stderr`` is accepted for interface parity and reserved for diagnostics;
    the boundary emits none in normal operation. One :class:`WorkspaceSession`
    is shared across the whole loop so ``open_project`` establishes the root
    that later ``get_tree`` / ``get_document`` / Twin requests use.
    ``store_base`` overrides the Twin app-data directory (tests use a temp dir;
    normal operation uses the per-user app-data location).
    """
    session = WorkspaceSession(store_base)
    for raw in stdin:
        line = raw[:-1] if raw.endswith("\n") else raw
        if line == "":
            continue
        if len(line.encode("utf-8")) > contract.MAX_MESSAGE_BYTES:
            _emit(stdout, contract.build_error(None, "message_too_large"))
            continue
        try:
            request = contract.loads(line)
        except (ValueError, UnicodeDecodeError):
            _emit(stdout, contract.build_error(None, "malformed_request"))
            continue
        _emit(stdout, handle_request(request, session))
    return 0


def _emit(stdout: TextIO, payload: Dict[str, Any]) -> None:
    """Write exactly one response line to stdout, then flush.

    A response that would itself exceed the maximum size is replaced with a
    bounded ``message_too_large`` error so stdout never carries an oversized
    line.
    """
    text = contract.dumps(payload)
    if len(text.encode("utf-8")) > contract.MAX_MESSAGE_BYTES:
        text = contract.dumps(contract.build_error(None, "message_too_large"))
    stdout.write(text + "\n")
    stdout.flush()


def _correlation_id(request: Any) -> Optional[str]:
    """Return a valid correlation identifier from ``request``, else ``None``."""
    if isinstance(request, dict):
        cid = request.get("correlation_id")
        if (
            isinstance(cid, str)
            and cid
            and len(cid) <= contract.CORRELATION_ID_MAX_CHARS
        ):
            return cid
    return None


def handle_request(request: Any, session: Optional[WorkspaceSession] = None) -> Dict[str, Any]:
    """Validate and process one request, returning a result or error envelope.

    This is the boundary's single, testable core: it never raises, and always
    returns exactly one envelope. Failures are mapped to bounded codes with
    messages drawn from the contract catalogue, so no caller text or file
    content leaks. ``session`` carries the accepted project root; when omitted
    a fresh session is used so the function remains usable standalone.
    """
    if session is None:
        session = WorkspaceSession()
    correlation_id = _correlation_id(request)
    try:
        return _process(request, session)
    except contract.ContractError as exc:
        return contract.build_error(correlation_id, exc.code)
    except Exception:
        # Internal failures are mapped to a bounded code; the original
        # exception object and its message are never serialized.
        return contract.build_error(correlation_id, "internal_error")


def _process(request: Any, session: WorkspaceSession) -> Dict[str, Any]:
    """Validate and dispatch one request envelope to its read-only handler."""
    if not isinstance(request, dict):
        raise contract.ContractError("invalid_request")

    if request.get("contract_version") != contract.CONTRACT_VERSION:
        raise contract.ContractError("unknown_contract_version")

    action = request.get("action")
    if action not in contract.ALLOWED_ACTIONS:
        raise contract.ContractError("action_not_allowed")

    correlation_id = _correlation_id(request)

    if action in contract.SCAN_ACTIONS:
        result = _scan_result(request)
    elif action == contract.ACTION_OPEN_PROJECT:
        result = _open_project_result(request, session)
    elif action == contract.ACTION_GET_TREE:
        result = _get_tree_result(request, session)
    elif action == contract.ACTION_GET_DOCUMENT:
        result = _get_document_result(request, session)
    elif action == contract.ACTION_SYNC_TWIN:
        result = _sync_twin_result(request, session)
    elif action == contract.ACTION_GET_TWIN:
        result = _get_twin_result(request, session)
    elif action == contract.ACTION_GET_ANCHOR:
        result = _get_anchor_result(request, session)
    elif action == contract.ACTION_GET_CODE_MAP:
        result = _get_code_map_result(request, session)
    elif action == contract.ACTION_SAVE_DRAFT:
        result = _save_draft_result(request, session)
    elif action == contract.ACTION_GET_DRAFT:
        result = _get_draft_result(request, session)
    elif action == contract.ACTION_DISCARD_DRAFT:
        result = _discard_draft_result(request, session)
    elif action == contract.ACTION_RESET_DRAFT:
        result = _reset_draft_result(request, session)
    elif action == contract.ACTION_COMPARE_DRAFT:
        result = _compare_draft_result(request, session)
    elif action == contract.ACTION_GENERATE_INTENT_DELTA:
        result = _generate_intent_delta_result(request, session)
    else:  # pragma: no cover - guarded by the allowlist above
        raise contract.ContractError("action_not_allowed")

    return contract.build_success(correlation_id, result)


def _scan_result(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run the deterministic scan -> plan -> report pipeline (P3.1)."""
    path = request.get("path")
    if not isinstance(path, str) or not path.strip():
        raise contract.ContractError("invalid_request")

    task = request.get("task")
    if not isinstance(task, dict):
        raise contract.ContractError("invalid_request")

    # Task intake: validate against the P2.3 shape. The specific validation
    # reason is not propagated; only the bounded code is serialized.
    try:
        validate_task(task)
    except TaskValidationError:
        raise contract.ContractError("invalid_request")

    # The boundary owns the allowlist decision: reject any task that names a
    # mutating action, even though the planner would accept it.
    if any(a not in contract.READ_ONLY_TASK_ACTIONS for a in task["allowed_actions"]):
        raise contract.ContractError("action_not_allowed")

    scanner_doc = scan_directory(path)
    plan = build_plan(task)
    report = build_report(
        scanner_doc,
        {
            "task_id": task["task_id"],
            "plan": plan,
            "next_action": _NEXT_ACTION,
            "repository_context": task["repository_context"],
        },
    )

    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "report": report,
        "evidence": scanner_doc,
    }


def _open_project_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Validate and accept a project root."""
    path = request.get("path")
    if not isinstance(path, str) or not path.strip():
        raise contract.ContractError("invalid_request")
    root = workspace.resolve_root(path)
    session.open(root)
    return {"root": root, "repository_state": "Unverified"}


def _get_tree_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the filtered tree for the accepted project root."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    return workspace.build_tree(session.root)


def _get_document_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return one permitted document below the accepted project root."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    rel_path = request.get("path")
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise contract.ContractError("invalid_request")
    return workspace.read_document(session.root, rel_path)


# -- Twin handlers (P3.3) ------------------------------------------------

def _compute_fingerprints(root: str, scanner_doc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Return ``{path: fingerprint}`` for every ``.py``/``.pyi`` file in ``scanner_doc``.

    Fingerprints are read from disk below ``root`` (the accepted project, which
    the scanner already walked); an unreadable file yields ``None`` so its
    fingerprint never invents content. The result is deterministic and
    path-ordered.
    """
    fingerprints: Dict[str, Optional[str]] = {}
    for file_rec in scanner_doc.get("files", []):
        rel_path = file_rec.get("path")
        if not isinstance(rel_path, str) or not rel_path.endswith((".py", ".pyi")):
            continue
        try:
            with open(os.path.join(root, rel_path), "rb") as fh:
                fingerprints[rel_path] = twin.fingerprint_bytes(fh.read())
        except OSError:
            fingerprints[rel_path] = None
    return fingerprints


def _twin_store(session: WorkspaceSession) -> Dict[str, Any]:
    """Load the workspace Twin store, raising ``twin_not_synchronized`` if absent."""
    workspace_id = twin.workspace_id_for(session.root)
    store, err = twin_store.load(session.store_base, workspace_id)
    if err is not None or store is None:
        raise contract.ContractError("twin_not_synchronized")
    return store


def _next_generation(previous: Optional[Dict[str, Any]]) -> int:
    """Return the next scan generation (one past the previous store's)."""
    if previous is None:
        return 1
    prev_gen = (previous.get("workspace_revision") or {}).get("scan_generation")
    return prev_gen + 1 if isinstance(prev_gen, int) else 1


def _now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sync_twin_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Synchronize the Structured Twin for the accepted workspace.

    A full sync (``changed_paths`` absent) or a changed-path sync (a list of
    root-relative ``.py`` paths) reconciles the scanner facts against the last
    valid store, persists atomically, and returns the SynchronizationResult.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")

    task = request.get("task")
    changed_paths: Optional[List[str]] = None
    if isinstance(task, dict) and "changed_paths" in task:
        changed_paths = task["changed_paths"]
        if not isinstance(changed_paths, list) or not all(
            isinstance(p, str) for p in changed_paths
        ):
            raise contract.ContractError("invalid_request")

    scanner_doc = scan_directory(session.root)
    fingerprints = _compute_fingerprints(session.root, scanner_doc)
    workspace_id = twin.workspace_id_for(session.root)
    previous, _ = twin_store.load(session.store_base, workspace_id)
    generation = _next_generation(previous)

    store, result = twin.sync_twin(
        scanner_doc,
        fingerprints,
        previous,
        workspace_id,
        generation,
        _now_iso(),
        changed_paths,
    )

    # Persist atomically; a failed write retains the last valid store and the
    # result still carries the reconciled state, flagged ``persisted`` false.
    result["persisted"] = twin_store.save(session.store_base, workspace_id, store) is None
    return result


def _get_twin_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the source-linked Twin projection bundle for a selector."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    task = request.get("task")
    selector = task.get("selector") if isinstance(task, dict) else None
    if not isinstance(selector, str) or not selector.strip():
        raise contract.ContractError("invalid_request")

    store = _twin_store(session)
    bundle = twin.projection_bundle(store, selector)
    if bundle is None:
        raise contract.ContractError("twin_not_found")
    return bundle


def _get_anchor_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return a bounded source-anchor navigation result for a behavior node."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    task = request.get("task")
    node_id = task.get("node_id") if isinstance(task, dict) else None
    if not isinstance(node_id, str) or not node_id.strip():
        raise contract.ContractError("invalid_request")

    store = _twin_store(session)
    anchor = twin.anchor_for(store, node_id)
    if anchor is None:
        raise contract.ContractError("twin_not_found")
    return anchor


# -- Editable Code Map handlers (P3.4) -----------------------------------

def _workspace_id(session: WorkspaceSession) -> str:
    """Return the canonical workspace identifier for the accepted root."""
    return twin.workspace_id_for(session.root)


def _field_schema() -> Dict[str, Any]:
    """Return the editable Code Map field schema (cardinality + scope + read-only)."""
    return {
        "editable_fields": {
            field: twin_draft.FIELD_CARDINALITY[field]
            for field in sorted(twin_draft.EDITABLE_FIELDS)
        },
        "artifact_fields": list(twin_draft.ARTIFACT_FIELDS),
        "behavior_fields": list(twin_draft.BEHAVIOR_FIELDS),
        "read_only_fields": sorted(twin_draft.READ_ONLY_FIELDS),
    }


def _load_draft_or_raise(session: WorkspaceSession) -> Dict[str, Any]:
    """Load the saved draft, raising a bounded ``draft_not_found`` when absent.

    A corrupt or future-version draft also maps to ``draft_not_found`` (the
    fail-closed load returns an error reason), so no draft content ever leaks.
    """
    draft, err = twin_store.load_draft(session.store_base, _workspace_id(session))
    if err is not None or draft is None:
        raise contract.ContractError("draft_not_found")
    return draft


def _get_code_map_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the editable Code Map baseline, field schema and saved draft state."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    workspace_id = _workspace_id(session)
    draft, _ = twin_store.load_draft(session.store_base, workspace_id)
    revision = store.get("workspace_revision") or {}
    return {
        "field_schema": _field_schema(),
        "baseline": {
            "workspace_id": revision.get("workspace_id"),
            "baseline_revision": revision.get("baseline_fingerprint"),
            "scan_generation": revision.get("scan_generation"),
            "sync_state": revision.get("sync_state"),
        },
        "draft": draft,
        "conflict": (
            twin_draft.conflict_for(draft, store)
            if draft is not None else {"state": twin_draft.CONFLICT_NONE, "reason": None}
        ),
    }


def _save_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Validate and atomically persist a Twin Draft against the current baseline.

    The draft is built from a list of ``{target_id, field, proposed}`` edits and
    validated against the synchronized Twin. A read-only-field edit, an unknown
    target, an unsupported field, or an oversized value is rejected with a
    bounded error — never persisted.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)

    task = request.get("task")
    if not isinstance(task, dict):
        raise contract.ContractError("invalid_request")
    edits = task.get("edits")
    if not isinstance(edits, list):
        raise contract.ContractError("invalid_request")
    if len(contract.dumps(edits).encode("utf-8")) > contract.MAX_DRAFT_BYTES:
        raise contract.ContractError("draft_oversized")

    workspace_id = _workspace_id(session)
    now = _now_iso()
    draft, err = twin_draft.build_draft(workspace_id, store, edits, now, now)
    if err is not None:
        # Only the oversized reason maps to ``draft_oversized``; every other
        # domain rejection (read-only, unknown target, unsupported, duplicate)
        # maps to the fixed ``draft_invalid`` code. The reason never leaks.
        if err == twin_draft.REASON_OVERSIZED:
            raise contract.ContractError("draft_oversized")
        raise contract.ContractError("draft_invalid")

    persisted = twin_store.save_draft(session.store_base, workspace_id, draft) is None
    return {"draft": draft, "persisted": persisted}


def _get_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the saved Twin Draft plus its conflict state against the baseline."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    draft = _load_draft_or_raise(session)
    return {"draft": draft, "conflict": twin_draft.conflict_for(draft, store)}


def _discard_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Discard (delete) the saved Twin Draft. Idempotent; never touches source."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    twin_store.discard_draft(session.store_base, _workspace_id(session))
    return {"discarded": True, "draft": None}


def _reset_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Reset the draft to the baseline by removing every saved edit."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    twin_store.discard_draft(session.store_base, _workspace_id(session))
    return {"reset": True, "draft": None}


def _compare_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the draft's field-level changes relative to the baseline."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    draft = _load_draft_or_raise(session)
    return {
        "draft_id": draft.get("draft_id"),
        "changes": draft.get("changes", []),
        "conflict": twin_draft.conflict_for(draft, store),
    }


def _generate_intent_delta_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Generate (or retrieve) the deterministic, non-executable Intent Delta.

    A no-op draft yields an honest ``no_change`` result; a stale draft is
    blocked with a bounded ``draft_stale`` error. The delta is never claimed to
    be executable and never contains source content.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    draft = _load_draft_or_raise(session)
    if twin_draft.is_noop(draft):
        return {"intent_delta": None, "no_change": True}
    if twin_draft.conflict_for(draft, store)["state"] != twin_draft.CONFLICT_NONE:
        raise contract.ContractError("draft_stale")
    delta, err = twin_draft.generate_intent_delta(draft, store)
    if err is not None:  # pragma: no cover - guarded by the checks above
        raise contract.ContractError("draft_invalid")
    return {"intent_delta": delta, "no_change": False}


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_loop", "handle_request", "WorkspaceSession"]
