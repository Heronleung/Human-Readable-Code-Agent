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

import sys
from typing import Any, Dict, Optional, TextIO, Sequence

from . import contract, workspace
from .planning import TaskValidationError, build_plan, validate_task
from .report import build_report
from .scanner import scan_directory

# Fixed, read-only next action reported by this slice: the boundary never
# performs a repository action.
_NEXT_ACTION = "Report only; no repository action performed."


class WorkspaceSession:
    """In-memory accepted-project state owned by one boundary loop (P3.2).

    ``open_project`` sets the accepted root; ``get_tree`` and ``get_document``
    operate relative to it. The session is per-``run_loop`` invocation, so a
    fresh boundary process (or a fresh test loop) always starts with no project
    accepted.
    """

    def __init__(self) -> None:
        self.root: Optional[str] = None

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


def run_loop(stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Read requests from ``stdin``, write one response line per request.

    ``stderr`` is accepted for interface parity and reserved for diagnostics;
    the boundary emits none in normal operation. One :class:`WorkspaceSession`
    is shared across the whole loop so ``open_project`` establishes the root
    that later ``get_tree`` / ``get_document`` requests use.
    """
    session = WorkspaceSession()
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_loop", "handle_request", "WorkspaceSession"]
