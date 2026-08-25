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
CONTRACT_VERSION = "3.2.0"

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

SCAN_ACTIONS = frozenset({"scan", "read", "analyze", "inspect", "plan"})
WORKSPACE_ACTIONS = frozenset(
    {ACTION_OPEN_PROJECT, ACTION_GET_TREE, ACTION_GET_DOCUMENT}
)
ALLOWED_ACTIONS = SCAN_ACTIONS | WORKSPACE_ACTIONS

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
# * ``MAX_FILE_BYTES`` — files larger than this are omitted from the tree.
# * ``MAX_DOCUMENT_BYTES`` — a document read larger than this is rejected with
#   a bounded ``file_too_large`` error. It is kept well below
#   ``MAX_MESSAGE_BYTES`` so an escaped (non-ASCII) document still fits on the
#   wire.
MAX_TREE_ENTRIES = 2000
MAX_TREE_DEPTH = 32
MAX_FILE_BYTES = 64 * 1024  # 64 KiB
MAX_DOCUMENT_BYTES = 64 * 1024  # 64 KiB

# Argument sentinel that turns the unified entry executable into the headless
# boundary. A frozen build launches ``[sys.executable, "--serve"]``; a source
# build launches ``[sys.executable, "-m", "hrca.boundary", "--serve"]``.
SERVE_SENTINEL = "--serve"

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
    "SCAN_ACTIONS",
    "WORKSPACE_ACTIONS",
    "ALLOWED_ACTIONS",
    "READ_ONLY_TASK_ACTIONS",
    "MAX_MESSAGE_BYTES",
    "MAX_TREE_ENTRIES",
    "MAX_TREE_DEPTH",
    "MAX_FILE_BYTES",
    "MAX_DOCUMENT_BYTES",
    "SERVE_SENTINEL",
    "ERROR_CODES",
    "error_message",
    "ContractError",
    "dumps",
    "loads",
    "new_correlation_id",
    "build_request",
    "build_success",
    "build_error",
]
