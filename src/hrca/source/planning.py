"""Deterministic task intake and plan builder for the Human-Readable Code Twin.

Bridges the P2.1 workflow contract with the P2.2 report builder. It validates a
bounded task input and returns an ordered, schema-compatible list of plan
entries in the P2.1 version-1 shape (``step``, ``action``, ``requires_approval``,
``expected_evidence``).

Planning only *describes* approval requirements; it never grants or performs
repository actions. Task request text is treated strictly as data to validate
and plan over, never as executable instructions.
"""

from __future__ import annotations

from typing import Any, Dict, List

# P2.1 plan schema version.
PLAN_VERSION = 1

# Ordered set of fields a valid task input must supply. Validation raises
# :class:`TaskValidationError` if any field is missing or malformed.
_REQUIRED_FIELDS = (
    "task_id",
    "title",
    "request",
    "repository_context",
    "allowed_actions",
    "constraints",
    "acceptance_criteria",
    "risk_level",
    "approval_required",
)

# Bounded action vocabulary. Read-only actions never require approval on their
# own; edit/commit/remote are always gated behind approval and are never
# performed by this module.
_READ_ONLY_ACTIONS = frozenset({"read", "analyze", "scan", "inspect", "plan"})
_APPROVAL_GATED_ACTIONS = frozenset({"edit", "commit", "remote"})
_KNOWN_ACTIONS = _READ_ONLY_ACTIONS | _APPROVAL_GATED_ACTIONS

# Bounded risk-level vocabulary.
_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})

# Bounded repository-context verification statuses.
_REPOSITORY_STATUSES = frozenset({"Verified", "Unverified"})

# Deterministic evidence description per action. Each string states what a
# completed step is expected to produce, without performing it.
_EXPECTED_EVIDENCE = {
    "read": (
        "Scanner evidence read from the repository: files, symbols, and "
        "relations recorded without fabrication."
    ),
    "analyze": (
        "Analysis derived from scanner evidence; literal targets left "
        "unresolved, never guessed."
    ),
    "scan": (
        "Canonical scanner JSON document with deterministically sorted records."
    ),
    "inspect": (
        "Observed repository facts from static scanning; no file or branch "
        "modified."
    ),
    "plan": (
        "Approval description for each step; no repository action performed."
    ),
    "edit": (
        "Proposed edit described as a plan entry only; no file modified "
        "without approval."
    ),
    "commit": (
        "Proposed commit described as a plan entry only; no commit created "
        "without approval."
    ),
    "remote": (
        "Proposed remote operation described as a plan entry only; no remote "
        "write performed."
    ),
}


class TaskValidationError(ValueError):
    """Raised when a task input fails validation.

    A :class:`ValueError` subclass keeps the planning layer dependency-free and
    consistent with the repository's standard-library-only conventions.
    """


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{field!r} must be a non-empty string")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TaskValidationError(f"{field!r} must be a boolean")
    return value


def _require_string_list(value: Any, field: str) -> List[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise TaskValidationError(f"{field!r} must be a list of non-empty strings")
    return list(value)


def _require_optional_string(value: Any, field: str) -> None:
    """Reject a supplied empty or non-string (non-null) optional value."""
    if value is None:
        return
    if not isinstance(value, str):
        raise TaskValidationError(f"{field!r} must be a string or null")
    if not value.strip():
        raise TaskValidationError(f"{field!r} must be a non-empty string when supplied")


def _require_repository_context(value: Any) -> Dict[str, Any]:
    """Validate the ``repository_context`` mapping against the P2.1 contract.

    ``status`` must be one of the bounded verification statuses. A ``Verified``
    context requires non-empty ``branch`` and ``commit_sha`` strings; an
    ``Unverified`` context may omit ``branch``/``commit_sha`` or set them to
    ``None``, but a supplied value must be a non-empty string. ``head_sha`` and
    ``base_sha`` are optional snapshot evidence only and never substitute for
    ``branch`` or ``commit_sha``.
    """
    if not isinstance(value, dict):
        raise TaskValidationError("'repository_context' must be a mapping")

    status = value.get("status")
    if not isinstance(status, str) or status not in _REPOSITORY_STATUSES:
        raise TaskValidationError(
            "'repository_context' must contain 'status' equal to one of "
            f"{sorted(_REPOSITORY_STATUSES)}"
        )

    branch = value.get("branch")
    commit_sha = value.get("commit_sha")

    if status == "Verified":
        if not isinstance(branch, str) or not branch.strip():
            raise TaskValidationError(
                "'repository_context' with status 'Verified' requires a "
                "non-empty 'branch' string"
            )
        if not isinstance(commit_sha, str) or not commit_sha.strip():
            raise TaskValidationError(
                "'repository_context' with status 'Verified' requires a "
                "non-empty 'commit_sha' string"
            )
    else:
        _require_optional_string(branch, "branch")
        _require_optional_string(commit_sha, "commit_sha")

    return value


def validate_task(task: Any) -> None:
    """Validate a bounded task input, raising :class:`TaskValidationError`.

    Required fields: ``task_id``, ``title``, ``request``, ``repository_context``,
    ``allowed_actions``, ``constraints``, ``acceptance_criteria``, ``risk_level``,
    and ``approval_required``.
    """
    if not isinstance(task, dict):
        raise TaskValidationError("task input must be a mapping")

    for field in _REQUIRED_FIELDS:
        if field not in task:
            raise TaskValidationError(f"missing required field {field!r}")

    _require_string(task["task_id"], "task_id")
    _require_string(task["title"], "title")
    _require_string(task["request"], "request")
    _require_repository_context(task["repository_context"])

    allowed = task["allowed_actions"]
    if not isinstance(allowed, list) or not allowed:
        raise TaskValidationError("'allowed_actions' must be a non-empty list")
    for action in allowed:
        if not isinstance(action, str) or action not in _KNOWN_ACTIONS:
            raise TaskValidationError(
                f"invalid action {action!r}; expected one of {sorted(_KNOWN_ACTIONS)}"
            )

    _require_string_list(task["constraints"], "constraints")
    _require_string_list(task["acceptance_criteria"], "acceptance_criteria")

    risk = task["risk_level"]
    if not isinstance(risk, str) or risk not in _RISK_LEVELS:
        raise TaskValidationError(f"'risk_level' must be one of {sorted(_RISK_LEVELS)}")

    _require_bool(task["approval_required"], "approval_required")


def _requires_approval(task: Dict[str, Any], action: str) -> bool:
    """Return whether a plan entry for ``action`` requires approval.

    A task-level ``approval_required`` gates every step; otherwise only the
    mutating / outward-facing actions (edit, commit, remote) are gated.
    """
    return bool(task["approval_required"]) or action in _APPROVAL_GATED_ACTIONS


def build_plan(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Validate ``task`` and return deterministic, ordered plan entries.

    Each entry carries the P2.1 version-1 shape: ``step``, ``action``,
    ``requires_approval``, and ``expected_evidence``. Entry order follows the
    order of ``allowed_actions``. The builder is pure: it performs no repository
    action and never resolves or executes the request text.
    """
    validate_task(task)
    plan: List[Dict[str, Any]] = []
    for index, action in enumerate(task["allowed_actions"], start=1):
        plan.append(
            {
                "step": index,
                "action": action,
                "requires_approval": _requires_approval(task, action),
                "expected_evidence": _EXPECTED_EVIDENCE[action],
            }
        )
    return plan
