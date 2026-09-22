"""Declarative, product-owned validation plan (P5.5a).

A plan is the *product's* answer to "which checks apply to this exact
candidate". It is not authored by a caller: the caller names checks, and
:mod:`hrca.validation_policy` supplies everything else. A plan is therefore
reproducible from the candidate binding and the check selection alone, which is
what makes its identity meaningful — the same candidate and the same policy
always yield the same plan, and a plan that says anything else is refused.

What a plan binds
-----------------

* the exact P5.4 **candidate**: its identity, the hash and size of its manifest
  and of its review envelope, the candidate root's own name, and the exact
  path/hash/size of every staged file, so the plan is bound to bytes and not to
  a name;
* the exact **P5.3 binding** the candidate was built against: the Intent Delta
  id, the Impact Proposal id, the binding fingerprint, and the workspace,
  baseline, scan-generation, scanner schema and grammar context;
* the **policy version** and the ordered, canonical check records.

What a plan may not contain
---------------------------

Anything that would configure how code runs. A plan request may name checks and
nothing else: a command, an argv, an image, a mount, an environment variable, a
timeout, a resource limit, a network mode, a credential, a user, a working
directory or a redefinition of a check is refused with its own bounded reason,
so a caller learns which surface does not exist rather than discovering it by
watching a flag take effect.

Compatibility
-------------

Version ``1.0.0``. The registry is empty; a newer, missing, malformed or older
unmigratable version is refused with a bounded reason, never half-read.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import validation_policy
from .identity import sha256_hex

VALIDATION_PLAN_SCHEMA_VERSION = "1.0.0"
VALIDATION_PLAN_GENERATOR = "hrca-validation-plan"
PLAN_ID_PREFIX = "plan:"

MAX_FILES = 64

_BINDING_FIELDS = (
    "candidate_id",
    "candidate_root_name",
    "manifest_sha256",
    "manifest_bytes",
    "review_sha256",
    "review_bytes",
    "edit_id",
    "intent_delta_id",
    "proposal_id",
    "binding_fingerprint",
    "files",
)

_BASELINE_FIELDS = (
    "workspace_id",
    "scan_generation",
    "baseline_fingerprint",
    "scanner_schema_version",
    "grammar",
)

REASON_NOT_MAPPING = "plan is not a mapping"
REASON_MISSING_VERSION = "missing schema_version"
REASON_INVALID_VERSION = "invalid schema_version"
REASON_NEWER_VERSION = "schema_version is newer than supported"
REASON_NOT_MIGRATABLE = "schema_version is not migratable"
REASON_BINDING_INVALID = "candidate binding is missing or malformed"
REASON_BASELINE_INVALID = "candidate binding baseline is missing or malformed"
REASON_EMPTY_FILES = "candidate binding names no files"
REASON_TOO_MANY_FILES = "candidate binding names too many files"
REASON_FILE_INVALID = "a candidate binding file entry is malformed"
REASON_ID_INVALID = "plan_id does not match the plan content"
REASON_CHECKS_NOT_CANONICAL = "plan checks are not the code-owned policy records"
REASON_NOT_EXECUTABLE_OR_APPLIED = "plan must be neither executable nor applied"
REASON_UNKNOWN_GENERATOR = "unknown plan generator"

MIGRATIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def dumps(obj: Any) -> str:
    """Serialize a plan canonically (sorted keys, compact, ASCII-safe)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


class _Refusal(ValueError):
    """Internal: a bounded refusal carrying a safe reason string."""


def _is_hex(value: Any, width: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and all(char in "0123456789abcdef" for char in value)
    )


def normalize_binding(binding: Any) -> Dict[str, Any]:
    """Validate and canonicalize a candidate binding, or raise a refusal.

    The binding is produced by :func:`hrca.validation.verify_candidate`, which
    reads the candidate and refuses before this function is reached. This
    function exists so that a *supplied* plan's binding is held to the same
    shape: a plan whose binding is malformed cannot be trusted to describe a
    candidate, and is refused rather than partially believed.
    """
    if not isinstance(binding, dict):
        raise _Refusal(REASON_BINDING_INVALID)
    for field in _BINDING_FIELDS:
        if field not in binding:
            raise _Refusal(REASON_BINDING_INVALID)

    for field in ("candidate_id", "edit_id", "intent_delta_id", "proposal_id",
                  "binding_fingerprint"):
        if not isinstance(binding[field], str) or not binding[field]:
            raise _Refusal(REASON_BINDING_INVALID)
    for field in ("manifest_sha256", "review_sha256"):
        if not _is_hex(binding[field]):
            raise _Refusal(REASON_BINDING_INVALID)
    for field in ("manifest_bytes", "review_bytes"):
        value = binding[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _Refusal(REASON_BINDING_INVALID)

    root_name = binding["candidate_root_name"]
    if (
        not isinstance(root_name, str)
        or not root_name.startswith("candidate-")
        or len(root_name) > 64
        or "/" in root_name
        or "\\" in root_name
    ):
        # The root *name* only: a plan never carries where the candidate lives,
        # so an absolute path here is a malformed binding, not a location.
        raise _Refusal(REASON_BINDING_INVALID)

    files = binding["files"]
    if not isinstance(files, list) or not files:
        raise _Refusal(REASON_EMPTY_FILES)
    if len(files) > MAX_FILES:
        raise _Refusal(REASON_TOO_MANY_FILES)
    normalized: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise _Refusal(REASON_FILE_INVALID)
        path = entry.get("path")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or len(path) > 512
        ):
            raise _Refusal(REASON_FILE_INVALID)
        if not _is_hex(entry.get("sha256")):
            raise _Refusal(REASON_FILE_INVALID)
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _Refusal(REASON_FILE_INVALID)
        if path in seen:
            raise _Refusal(REASON_FILE_INVALID)
        seen.add(path)
        normalized.append({"path": path, "sha256": entry["sha256"], "bytes": size})

    return {
        "candidate_id": binding["candidate_id"],
        "candidate_root_name": root_name,
        "manifest_sha256": binding["manifest_sha256"],
        "manifest_bytes": binding["manifest_bytes"],
        "review_sha256": binding["review_sha256"],
        "review_bytes": binding["review_bytes"],
        "edit_id": binding["edit_id"],
        "intent_delta_id": binding["intent_delta_id"],
        "proposal_id": binding["proposal_id"],
        "binding_fingerprint": binding["binding_fingerprint"],
        "baseline": _normalize_baseline(binding.get("baseline")),
        "files": sorted(normalized, key=lambda item: item["path"]),
    }


def _normalize_baseline(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise _Refusal(REASON_BASELINE_INVALID)
    for field in _BASELINE_FIELDS:
        if field not in value:
            raise _Refusal(REASON_BASELINE_INVALID)
    generation = value["scan_generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise _Refusal(REASON_BASELINE_INVALID)
    grammar = value["grammar"]
    if (
        not isinstance(grammar, dict)
        or not isinstance(grammar.get("implementation"), str)
        or not isinstance(grammar.get("version"), str)
    ):
        raise _Refusal(REASON_BASELINE_INVALID)
    for field in ("workspace_id", "baseline_fingerprint", "scanner_schema_version"):
        if not isinstance(value[field], str) or not value[field]:
            raise _Refusal(REASON_BASELINE_INVALID)
    return {
        "workspace_id": value["workspace_id"],
        "scan_generation": generation,
        "baseline_fingerprint": value["baseline_fingerprint"],
        "scanner_schema_version": value["scanner_schema_version"],
        "grammar": {
            "implementation": grammar["implementation"],
            "version": grammar["version"],
        },
    }


def plan_id_for(plan: Dict[str, Any]) -> str:
    """Return the content-addressed identity of a plan (never time-derived)."""
    canon = dumps({k: v for k, v in plan.items() if k != "plan_id"})
    return PLAN_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def build_plan(binding: Any, request: Any = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(plan, reason)`` for a candidate binding and a plan request.

    ``request`` may name checks (``{"checks": [...]}``) and nothing else. Every
    other key is refused — a named runtime surface with its own bounded reason,
    or the generic unknown-field reason.
    """
    try:
        normalized = normalize_binding(binding)
    except _Refusal as exc:
        return None, str(exc)

    if request is None:
        requested = None
    else:
        if not isinstance(request, dict):
            return None, validation_policy.REASON_REQUEST_NOT_MAPPING
        for key in sorted(request):
            if key == "checks":
                continue
            sentence = validation_policy.OVERRIDE_KEYS.get(key)
            return None, sentence or validation_policy.REASON_UNKNOWN_REQUEST_KEY
        requested = request.get("checks")

    check_ids, reason = validation_policy.resolve_checks(requested)
    if reason is not None:
        return None, reason

    plan: Dict[str, Any] = {
        "schema_version": VALIDATION_PLAN_SCHEMA_VERSION,
        "generator": VALIDATION_PLAN_GENERATOR,
        "plan_id": "",
        "policy_version": validation_policy.POLICY_VERSION,
        "candidate": normalized,
        "checks": [validation_policy.check_record(check_id) for check_id in check_ids],
        # A plan describes checks. It approves nothing and applies nothing.
        "executable": False,
        "applied": False,
    }
    plan["plan_id"] = plan_id_for(plan)
    return plan, None


def migrate_plan(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate a serialized plan's declared version, fail-closed."""
    if not isinstance(raw, dict):
        return None, REASON_NOT_MAPPING
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, REASON_MISSING_VERSION
    try:
        current = _version_tuple(VALIDATION_PLAN_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, REASON_INVALID_VERSION
    if found == current:
        return raw, None
    if found > current:
        return None, REASON_NEWER_VERSION
    if version not in MIGRATIONS:
        return None, REASON_NOT_MIGRATABLE
    return MIGRATIONS[version](dict(raw)), None


def validate_plan(plan: Any) -> Optional[str]:
    """Validate a plan against the P5.5a schema; return a bounded reason or ``None``.

    The checks are not merely well formed: they must equal, exactly and in
    order, the records the code-owned policy would produce. A plan that named a
    package, an input, a timeout or a resource profile of its own would be a
    plan describing a run the product would never perform.
    """
    if not isinstance(plan, dict):
        return REASON_NOT_MAPPING
    migrated, error = migrate_plan(plan)
    if error is not None:
        return error
    if migrated is not plan:  # pragma: no cover - no migration is registered
        return REASON_NOT_MAPPING
    if plan.get("generator") != VALIDATION_PLAN_GENERATOR:
        return REASON_UNKNOWN_GENERATOR
    if plan.get("executable") is not False or plan.get("applied") is not False:
        return REASON_NOT_EXECUTABLE_OR_APPLIED
    if plan.get("policy_version") != validation_policy.POLICY_VERSION:
        return "unsupported policy_version"

    try:
        binding = normalize_binding(plan.get("candidate"))
    except _Refusal as exc:
        return str(exc)
    if binding != plan.get("candidate"):
        return REASON_BINDING_INVALID

    checks = plan.get("checks")
    if not isinstance(checks, list) or not checks:
        return validation_policy.REASON_NO_CHECKS
    if len(checks) > validation_policy.MAX_CHECKS:
        return validation_policy.REASON_TOO_MANY_CHECKS
    for record in checks:
        if not isinstance(record, dict):
            return REASON_CHECKS_NOT_CANONICAL
        if record.get("check_id") not in validation_policy.POLICY:
            return validation_policy.REASON_UNKNOWN_CHECK
    # Each record must equal the code-owned record *and* the list must be in the
    # canonical order: a reordered plan describes the same checks but is not the
    # plan the product would have built, so it is not a plan this contract signs.
    expected = [
        validation_policy.check_record(check_id)
        for check_id in sorted(record["check_id"] for record in checks)
    ]
    if expected != checks:
        return REASON_CHECKS_NOT_CANONICAL

    identity = plan.get("plan_id")
    if not isinstance(identity, str) or not identity.startswith(PLAN_ID_PREFIX):
        return "missing or malformed plan_id"
    if identity != plan_id_for(plan):
        return REASON_ID_INVALID
    return None


__all__ = [
    "VALIDATION_PLAN_SCHEMA_VERSION",
    "VALIDATION_PLAN_GENERATOR",
    "PLAN_ID_PREFIX",
    "MAX_FILES",
    "REASON_NOT_MAPPING",
    "REASON_MISSING_VERSION",
    "REASON_INVALID_VERSION",
    "REASON_NEWER_VERSION",
    "REASON_NOT_MIGRATABLE",
    "REASON_BINDING_INVALID",
    "REASON_BASELINE_INVALID",
    "REASON_EMPTY_FILES",
    "REASON_TOO_MANY_FILES",
    "REASON_FILE_INVALID",
    "REASON_ID_INVALID",
    "REASON_CHECKS_NOT_CANONICAL",
    "REASON_NOT_EXECUTABLE_OR_APPLIED",
    "REASON_UNKNOWN_GENERATOR",
    "MIGRATIONS",
    "dumps",
    "normalize_binding",
    "build_plan",
    "migrate_plan",
    "validate_plan",
    "plan_id_for",
]
