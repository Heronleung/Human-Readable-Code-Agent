"""Execution broker for document-driven app packages (P4.3).

The broker is the single orchestrator between the deterministic boundary and the
isolated runner. It owns the order of operations and never bypasses a gate:

    validate package -> validate form input -> preflight runner -> run ->
    validate result.

The package manifest and the form input are untrusted; the runner is the only
place package code executes, and it never runs on the host. The broker is
Qt-free and imports only the pure domain (:mod:`hrca.app_package`) and the
runner adapter (:mod:`hrca.container_runner`), so the desktop client can never
import it (enforced by :mod:`tests.test_architecture`).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import app_package, rule_delta
from .container_runner import (
    ContainerRunner,
    PREFLIGHT_RUNTIME_BLOCKED,
)

BROKER_SCHEMA_VERSION = "1.0.0"


def _normalize(
    *, state: str, package_id: Optional[str], result: Optional[Dict[str, Any]], limitations: list
) -> Dict[str, Any]:
    return {
        "schema_version": BROKER_SCHEMA_VERSION,
        "state": state,
        "package_id": package_id,
        "result": result,
        "limitations": list(limitations),
    }


def run_package(
    package: Any,
    form_input: Any,
    runner: Optional[ContainerRunner] = None,
) -> Dict[str, Any]:
    """Validate and execute one package against one form input.

    Returns a normalized result whose ``state`` is one of
    :data:`hrca.app_package.RUN_STATES`. A non-``ok`` state carries a bounded
    ``limitations`` reason and a ``result`` of ``None``; the deterministic
    validation happens entirely before any runner is started.
    """
    runner = runner if runner is not None else ContainerRunner()
    package_id = package.get("package_id") if isinstance(package, dict) else None

    reason = app_package.validate_package(package)
    if reason is not None:
        return _normalize(
            state=app_package.STATE_PACKAGE_INVALID,
            package_id=package_id,
            result=None,
            limitations=[reason],
        )

    reason = app_package.validate_form_input(package, form_input)
    if reason is not None:
        return _normalize(
            state=app_package.STATE_INPUT_INVALID,
            package_id=package_id,
            result=None,
            limitations=[reason],
        )

    preflight = runner.preflight()
    if not preflight.get("available"):
        state = (
            app_package.STATE_RUNTIME_BLOCKED
            if preflight.get("reason") == PREFLIGHT_RUNTIME_BLOCKED
            else app_package.STATE_RUNTIME_UNAVAILABLE
        )
        return _normalize(
            state=state,
            package_id=package_id,
            result=None,
            limitations=[preflight.get("reason") or "runtime unavailable"],
        )

    result, run_err = runner.run(
        handler=package["handler"], input_payload=form_input
    )
    if run_err is not None:
        return _normalize(
            state=run_err,
            package_id=package_id,
            result=None,
            limitations=[run_err],
        )

    reason = app_package.validate_result(package, result)
    if reason is not None:
        return _normalize(
            state=app_package.STATE_OUTPUT_INVALID,
            package_id=package_id,
            result=None,
            limitations=[reason],
        )

    return _normalize(
        state=app_package.STATE_OK,
        package_id=package_id,
        result=result,
        limitations=[],
    )


def _rule_package(rule_id: str):
    """Return the code-owned package manifest for ``rule_id``, or ``None``."""
    if rule_id == rule_delta.RESULT_KIND_QUOTATION:
        return app_package.quotation_reference_package()
    if rule_id == rule_delta.RESULT_KIND_LATE_RETURN_FEE:
        return app_package.late_return_fee_package()
    return None


def run_rule_delta(
    delta: Any,
    form_input: Any,
    runner: Optional[ContainerRunner] = None,
) -> Dict[str, Any]:
    """Validate and execute a declarative rule delta against one form input.

    The delta is resolved to a bounded ``{rule_id, parameters}`` mapping by the
    code-owned resolver; the code-owned package manifest for that rule validates
    the form input and result schema; and the isolated runner executes the
    code-owned handler with the resolved parameters. A non-``ok`` state carries
    a bounded limitation and a ``result`` of ``None``; validation happens before
    any runner is started.
    """
    runner = runner if runner is not None else ContainerRunner()
    resolved = rule_delta.resolve_delta(delta)
    if resolved is None:
        return _normalize(
            state=app_package.STATE_PACKAGE_INVALID,
            package_id=None,
            result=None,
            limitations=[rule_delta.REASON_INVALID_CHANGE],
        )

    rule_id = resolved["rule_id"]
    parameters = resolved["parameters"]
    package = _rule_package(rule_id)
    if package is None:  # pragma: no cover - guarded by the resolver
        return _normalize(
            state=app_package.STATE_PACKAGE_INVALID,
            package_id=rule_id,
            result=None,
            limitations=[rule_delta.REASON_UNKNOWN_RESULT_KIND],
        )

    reason = app_package.validate_form_input(package, form_input)
    if reason is not None:
        return _normalize(
            state=app_package.STATE_INPUT_INVALID,
            package_id=rule_id,
            result=None,
            limitations=[reason],
        )

    preflight = runner.preflight()
    if not preflight.get("available"):
        state = (
            app_package.STATE_RUNTIME_BLOCKED
            if preflight.get("reason") == PREFLIGHT_RUNTIME_BLOCKED
            else app_package.STATE_RUNTIME_UNAVAILABLE
        )
        return _normalize(
            state=state,
            package_id=rule_id,
            result=None,
            limitations=[preflight.get("reason") or "runtime unavailable"],
        )

    result, run_err = runner.run(
        handler=package["handler"], input_payload=form_input, parameters=parameters
    )
    if run_err is not None:
        return _normalize(
            state=run_err,
            package_id=rule_id,
            result=None,
            limitations=[run_err],
        )

    reason = app_package.validate_result(package, result)
    if reason is not None:
        return _normalize(
            state=app_package.STATE_OUTPUT_INVALID,
            package_id=rule_id,
            result=None,
            limitations=[reason],
        )

    return _normalize(
        state=app_package.STATE_OK,
        package_id=rule_id,
        result=result,
        limitations=[],
    )


__all__ = [
    "BROKER_SCHEMA_VERSION",
    "run_package",
    "run_rule_delta",
]
