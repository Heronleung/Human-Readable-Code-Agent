"""Code-owned validation command policy (P5.5a).

The single place that decides **which** checks a validation plan may contain and
**exactly** what each one runs. A check is a name for a fixed, reviewed
invocation of the accepted isolated runner; nothing about that invocation is
reachable from a plan, a candidate, a fixture or prose.

Why a check is not a command
----------------------------

The accepted runner does not take a command. :meth:`hrca.container_runner.
ContainerRunner.run` takes a ``handler`` name and an input payload, and the
container's entrypoint argv is a module constant. So the strongest possible
policy here is not "a reviewed argv per check" — it is "a reviewed *package*
per check, from which the handler, the form schema and the result schema all
come". A caller who could supply an argv, a shell string, an image, a mount, an
environment variable, a timeout or a resource limit would be choosing how code
runs; this module is what makes that impossible rather than merely discouraged.

Everything below is derived from modules that already own the value:

* the accepted package ids come from :data:`hrca.app_package.ALLOWED_PACKAGE_IDS`;
* the package manifests, their handlers and their form/result schemas come from
  the code-owned builders in :mod:`hrca.app_package`;
* the fixed form inputs are the code-owned protected inputs in
  :mod:`hrca.delta_verifier` — the same inputs the delta verifier already treats
  as protected from provider influence.

Nothing here is a new runner policy and nothing here relaxes one. The isolation
(network, mounts, capabilities, resources) is the runner's, declared once in
:mod:`hrca.container_runner`, and this module neither restates it as if it owned
it nor allows it to be overridden.

Purity
------

Standard library only, no filesystem, no network, no process, no clock. It
imports the pure domains (:mod:`hrca.app_package`, :mod:`hrca.delta_verifier`,
:mod:`hrca.rule_delta`) and never the runner adapter, so it cannot dispatch
anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import app_package, delta_verifier, rule_delta

POLICY_VERSION = "1.0.0"

# Bounded check identifiers. A plan names checks from this set and nothing else.
CHECK_QUOTATION_REFERENCE = "check:quotation_reference"
CHECK_QUOTATION_ALTERNATE = "check:quotation_alternate"
CHECK_LATE_RETURN_FEE = "check:late_return_fee"

# The bounded value vocabularies a check may declare. Each is a fixed token from
# *this* module; none of them is a setting a caller can reach, because a caller
# can only name checks.
RESOURCE_PROFILE_DEFAULT = "runner_default"
NETWORK_POLICY_NONE = "none"
CREDENTIAL_POLICY_NONE = "none"

# The working directory is not set by this contract at all. Overriding it would
# be a runtime-configuration surface, and the contract refuses to have one; the
# image owns its own working directory and the runner never passes ``--workdir``.
WORKING_DIRECTORY_IMAGE_OWNED = "image_owned"

RESULT_ARTIFACT_FILENAME = "output.json"

# Bounded plan-request reasons. A request may name checks and nothing else.
REASON_REQUEST_NOT_MAPPING = "plan request is not a mapping"
REASON_UNKNOWN_CHECK = "unknown check id"
REASON_NO_CHECKS = "plan request names no checks"
REASON_DUPLICATE_CHECK = "plan request names a check twice"
REASON_TOO_MANY_CHECKS = "plan request names too many checks"
REASON_UNKNOWN_REQUEST_KEY = "plan request has unsupported fields"
REASON_BAD_CHECK_LIST = "plan request checks must be a list of check ids"

MAX_CHECKS = 8

# Every key a plan request might use to try to configure the runtime, mapped to
# the bounded sentence that refuses it. A key outside this set gets the generic
# unknown-field reason, so an unrecognised name is still refused.
OVERRIDE_KEYS: Dict[str, str] = {}
for _keys, _sentence in (
    (
        ("argv", "args", "arguments", "command", "cmd", "shell", "entrypoint",
         "script", "exec", "executable", "binary", "program"),
        "a plan request may not supply a command",
    ),
    (("image", "docker_image", "container_image", "tag"), "a plan request may not select an image"),
    (("mount", "mounts", "volume", "volumes", "bind", "binds"), "a plan request may not add a mount"),
    (
        ("env", "environment", "env_file", "variables"),
        "a plan request may not set the environment",
    ),
    (("timeout", "timeout_seconds", "deadline", "budget"), "a plan request may not set a timeout"),
    (
        ("resources", "resource_profile", "memory", "cpus", "pids", "pids_limit",
         "cpu_shares", "limits"),
        "a plan request may not set resource limits",
    ),
    (
        ("network", "net", "network_mode", "ports", "publish"),
        "a plan request may not change the network policy",
    ),
    (
        ("credential", "credentials", "secret", "secrets", "token", "key"),
        "a plan request may not supply a credential",
    ),
    (("user", "uid", "gid", "capabilities", "cap_add", "privileged"), "a plan request may not change the privilege model"),
    (("cwd", "workdir", "working_directory", "chdir"), "a plan request may not set a working directory"),
    (("handler", "package_id", "parameters", "input", "form_input", "result_schema"), "a plan request may not redefine a check"),
):
    for _key in _keys:
        OVERRIDE_KEYS[_key] = _sentence
del _keys, _sentence, _key


def _quotation_input() -> Dict[str, Any]:
    return dict(
        delta_verifier.protected_inputs(rule_delta.RESULT_KIND_QUOTATION)[0]
    )


def _late_return_fee_input() -> Dict[str, Any]:
    return dict(
        delta_verifier.protected_inputs(rule_delta.RESULT_KIND_LATE_RETURN_FEE)[0]
    )


# The registry. Each entry names one accepted package, the fixed form input the
# check runs it against, and the bounded operational tokens the run records. The
# handler and the schemas are not repeated here: they are read from the package
# at dispatch time, so a check cannot describe a package it does not match.
POLICY: Dict[str, Dict[str, Any]] = {
    CHECK_QUOTATION_REFERENCE: {
        "ordinal": 1,
        "package_id": "quotation-rules",
        "form_input": _quotation_input(),
        "parameters": None,
        "timeout_seconds": 10.0,
        "resource_profile": RESOURCE_PROFILE_DEFAULT,
        "network_policy": NETWORK_POLICY_NONE,
        "credential_policy": CREDENTIAL_POLICY_NONE,
        "working_directory": WORKING_DIRECTORY_IMAGE_OWNED,
        "expected_artifact": RESULT_ARTIFACT_FILENAME,
    },
    CHECK_QUOTATION_ALTERNATE: {
        "ordinal": 2,
        "package_id": "quotation-rules-alt",
        "form_input": _quotation_input(),
        "parameters": None,
        "timeout_seconds": 10.0,
        "resource_profile": RESOURCE_PROFILE_DEFAULT,
        "network_policy": NETWORK_POLICY_NONE,
        "credential_policy": CREDENTIAL_POLICY_NONE,
        "working_directory": WORKING_DIRECTORY_IMAGE_OWNED,
        "expected_artifact": RESULT_ARTIFACT_FILENAME,
    },
    CHECK_LATE_RETURN_FEE: {
        "ordinal": 3,
        "package_id": "late-return-fee",
        "form_input": _late_return_fee_input(),
        "parameters": None,
        "timeout_seconds": 10.0,
        "resource_profile": RESOURCE_PROFILE_DEFAULT,
        "network_policy": NETWORK_POLICY_NONE,
        "credential_policy": CREDENTIAL_POLICY_NONE,
        "working_directory": WORKING_DIRECTORY_IMAGE_OWNED,
        "expected_artifact": RESULT_ARTIFACT_FILENAME,
    },
}

CHECK_IDS = tuple(sorted(POLICY))

# The fixed fields a check record carries, in canonical order. A plan is
# validated against exactly this set, so a check cannot smuggle an extra setting.
CHECK_FIELDS = (
    "ordinal",
    "package_id",
    "form_input",
    "parameters",
    "timeout_seconds",
    "resource_profile",
    "network_policy",
    "credential_policy",
    "working_directory",
    "expected_artifact",
)


def resolve_checks(requested: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return ``(check_ids, reason)`` for a requested check list.

    ``None`` selects the default set. A request that names an unknown check, a
    duplicate, too many, or anything that is not a list of ids is refused with a
    bounded reason. The returned list is always in canonical order.
    """
    if requested is None:
        return [check_id for check_id in CHECK_IDS], None
    if not isinstance(requested, list):
        return None, REASON_BAD_CHECK_LIST
    if not requested:
        return None, REASON_NO_CHECKS
    if len(requested) > MAX_CHECKS:
        return None, REASON_TOO_MANY_CHECKS
    seen: set = set()
    for check_id in requested:
        if not isinstance(check_id, str) or check_id not in POLICY:
            return None, REASON_UNKNOWN_CHECK
        if check_id in seen:
            return None, REASON_DUPLICATE_CHECK
        seen.add(check_id)
    return sorted(seen), None


def check_record(check_id: str) -> Dict[str, Any]:
    """Return the canonical, self-contained record for one policy check."""
    entry = POLICY[check_id]
    return {
        "check_id": check_id,
        **{field: entry[field] for field in CHECK_FIELDS},
    }


def package_for(check_id: str) -> Optional[Dict[str, Any]]:
    """Return the code-owned package manifest for a check, or ``None``.

    The handler, the form schema and the result schema all come from here, so a
    check cannot describe a package whose contract it does not match.
    """
    entry = POLICY.get(check_id)
    if entry is None:
        return None
    package_id = entry["package_id"]
    if package_id == "quotation-rules":
        return app_package.quotation_reference_package()
    if package_id == "quotation-rules-alt":
        return app_package.quotation_rules_alt_package()
    if package_id == "late-return-fee":
        return app_package.late_return_fee_package()
    return None  # pragma: no cover - the registry is code-owned


def validate_policy() -> Optional[str]:
    """Return a bounded reason when the registry contradicts the accepted set.

    A registry that named a package or a handler the accepted allowlist does not
    hold would be a policy the runner could not honour; this makes that a
    start-up-visible failure rather than a dispatch-time surprise.
    """
    for check_id, entry in sorted(POLICY.items()):
        if entry["package_id"] not in app_package.ALLOWED_PACKAGE_IDS:
            return "a check names an unsupported package_id"
        package = package_for(check_id)
        if package is None:
            return "a check has no code-owned package"
        if package.get("handler") not in app_package.ALLOWED_HANDLERS:
            return "a check names an unsupported handler"
        reason = app_package.validate_form_input(package, entry["form_input"])
        if reason is not None:
            return "a check input does not satisfy its package form"
    return None


__all__ = [
    "POLICY_VERSION",
    "POLICY",
    "CHECK_IDS",
    "CHECK_FIELDS",
    "CHECK_QUOTATION_REFERENCE",
    "CHECK_QUOTATION_ALTERNATE",
    "CHECK_LATE_RETURN_FEE",
    "RESOURCE_PROFILE_DEFAULT",
    "NETWORK_POLICY_NONE",
    "CREDENTIAL_POLICY_NONE",
    "WORKING_DIRECTORY_IMAGE_OWNED",
    "RESULT_ARTIFACT_FILENAME",
    "MAX_CHECKS",
    "REASON_REQUEST_NOT_MAPPING",
    "REASON_UNKNOWN_CHECK",
    "REASON_NO_CHECKS",
    "REASON_DUPLICATE_CHECK",
    "REASON_TOO_MANY_CHECKS",
    "REASON_UNKNOWN_REQUEST_KEY",
    "REASON_BAD_CHECK_LIST",
    "resolve_checks",
    "check_record",
    "package_for",
    "validate_policy",
]
