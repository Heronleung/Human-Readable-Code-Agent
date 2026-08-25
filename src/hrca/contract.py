"""Qt-free application-boundary contract (P3.1).

This module is the single, versioned contract shared by every client — the
PySide6 desktop window today, and any future IDE plugin — so that no client
ever imports the deterministic core (:mod:`hrca.scanner`, :mod:`hrca.planning`,
:mod:`hrca.report`, :mod:`hrca.provider`) directly.

It defines, and only defines:

* the contract version constant,
* the request and result payload envelopes,
* the correlation identifier rules,
* the allowed read-only action names,
* the bounded error codes and their fixed messages,
* the maximum inbound/outbound message size, and
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
CONTRACT_VERSION = "3.1.0"

# Correlation identifier: a client-generated opaque string that the boundary
# echoes verbatim so a client can match each response to its in-flight request.
# It must be an ASCII-safe string bounded to this many characters; anything
# else is treated as absent (and is therefore not echoed).
CORRELATION_ID_MAX_CHARS = 64

# Request-level action names the boundary accepts. Every name here is
# read-only; write, Git, command, network and provider actions are absent from
# this set and are therefore rejected with a bounded ``action_not_allowed``
# error. In this slice every allowed action runs the same read-only
# scan -> plan -> report pipeline.
ACTION_SCAN = "scan"
ALLOWED_ACTIONS = frozenset({"scan", "read", "analyze", "inspect", "plan"})

# Task-level ``allowed_actions`` that the read-only slice permits. A task that
# names a mutating action (edit / commit / remote) is rejected even though the
# deterministic planner would otherwise accept it — the boundary owns the
# allowlist decision.
READ_ONLY_TASK_ACTIONS = frozenset({"read", "analyze", "scan", "inspect", "plan"})

# Maximum inbound and outbound message size, in UTF-8 bytes. Enforced by the
# boundary on inbound requests and by the client on inbound backend output.
MAX_MESSAGE_BYTES = 1_048_576  # 1 MiB

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
    "ALLOWED_ACTIONS",
    "READ_ONLY_TASK_ACTIONS",
    "MAX_MESSAGE_BYTES",
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
