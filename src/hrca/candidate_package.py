"""Versioned candidate-package contract (P4.7).

The smallest additive, bounded record that a future provider-origin quotation
variation would have to conform to before it could ever be staged, run or
adopted. It is distinct from the deterministic fixture (:mod:`hrca.app_package`)
and from the document/version authority (:mod:`hrca.document`): the fixture
proves the runner; a *candidate package* is the envelope a later flow would
validate, bind and independently verify.

A candidate package is data only and carries:

* a bounded ``package`` manifest that must be **byte-identical** to one of the
  code-owned variants (the reference or the reviewed alternative), so the
  renderer schema is code-owned and a candidate can never smuggle its own form/
  result schema, handler or rules;
* a ``provenance`` token (deterministic fixture / manually validated variant /
  provider-produced);
* a ``binding`` that names the Working Document revision + content fingerprint,
  the runtime identity and the protected verifier identity — all of which must
  match code-owned values exactly;
* an ``evidence`` list that the protected verifier (:mod:`hrca.verifier`) checks
  against frozen regression cases.

This module is pure (Qt-free, stdlib-only): no filesystem, network, credential,
command, Git, provider, runner or package-execution access. It performs no
execution and no adoption.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from . import app_package, verifier

CANDIDATE_PACKAGE_SCHEMA_VERSION = "1.0.0"
CANDIDATE_PACKAGE_GENERATOR = "hrca-candidate-package"

# Bounded limits (code-owned; never from candidate input).
MAX_CANDIDATE_PACKAGE_BYTES = 64 * 1024  # 64 KiB (package + evidence)
MAX_EVIDENCE_CASES = 64

# Provenance tokens. ``provider_produced`` is reserved for the later,
# separately authorized flow — no provider request is made here.
PROVENANCE_DETERMINISTIC_FIXTURE = "deterministic_fixture"
PROVENANCE_MANUAL_VARIANT = "manually_validated_variant"
PROVENANCE_PROVIDER = "provider_produced"
PROVENANCES = frozenset(
    {PROVENANCE_DETERMINISTIC_FIXTURE, PROVENANCE_MANUAL_VARIANT, PROVENANCE_PROVIDER}
)

# Read-only evidence states (P4.7). They distinguish the deterministic fixture,
# a manually validated readiness variant, a valid candidate awaiting review, and
# the fail-closed states.
STATE_DETERMINISTIC_FIXTURE = "deterministic_fixture"
STATE_MANUAL_VARIANT = "manually_validated_variant"
STATE_VALID_CANDIDATE = "valid_candidate"
STATE_INVALID = "invalid"
STATE_BLOCKED = "blocked"
STATE_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATES = frozenset(
    {
        STATE_DETERMINISTIC_FIXTURE,
        STATE_MANUAL_VARIANT,
        STATE_VALID_CANDIDATE,
        STATE_INVALID,
        STATE_BLOCKED,
        STATE_INSUFFICIENT_EVIDENCE,
    }
)

# The exact top-level keys a candidate package may carry. Any ``script``,
# ``code``, ``html``, ``import``/``imports``, ``path``, ``command``/``cmd``,
# ``url``, ``network``, ``install``/``dependencies``, ``mount``/``mounts``,
# ``env``/``environment``, ``dockerfile``, ``image``, ``entrypoint`` or ``args``
# field is rejected as an unknown key.
_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "candidate_package_id",
        "provenance",
        "variant_id",
        "package",
        "binding",
        "evidence",
    }
)

# The exact binding keys. A binding is the identity contract, not a free-form
# map; any extra key (e.g. a second runtime, an alternate verifier, a path) is
# rejected.
_ALLOWED_BINDING_KEYS = frozenset(
    {"document_revision_id", "document_fingerprint", "runtime", "verifier_identity"}
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

# Bounded refusal reasons (never interpolate candidate content).
REASON_NOT_MAPPING = "candidate package is not a mapping"
REASON_TOO_LARGE = "candidate package is too large"
REASON_UNKNOWN_KEYS = "candidate package has unsupported fields"
REASON_UNSUPPORTED_SCHEMA = "unsupported schema_version"
REASON_INVALID_PROVENANCE = "invalid provenance"
REASON_UNKNOWN_VARIANT = "unknown variant"
REASON_PACKAGE_MISMATCH = "package does not match the code-owned variant"
REASON_BINDING_INVALID = "binding is invalid"
REASON_EVIDENCE_INVALID = "evidence is invalid"


def new_candidate_package_id() -> str:
    """Return a fresh opaque candidate-package id (``cp:<32 hex>``)."""
    return "cp:" + uuid.uuid4().hex


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return app_package.dumps(obj)


def provenance_state(provenance: str) -> str:
    """Map a provenance token to its read-only evidence state.

    ``deterministic_fixture`` -> the reference fixture; ``manually_validated_
    variant`` -> a reviewed variant; ``provider_produced`` -> a valid candidate
    awaiting review.
    """
    if provenance == PROVENANCE_DETERMINISTIC_FIXTURE:
        return STATE_DETERMINISTIC_FIXTURE
    if provenance == PROVENANCE_MANUAL_VARIANT:
        return STATE_MANUAL_VARIANT
    return STATE_VALID_CANDIDATE


def _valid_binding(binding: Any) -> bool:
    """True when ``binding`` carries exactly the required, well-formed identities."""
    if not isinstance(binding, dict):
        return False
    if set(binding) != _ALLOWED_BINDING_KEYS:
        return False
    revision_id = binding.get("document_revision_id")
    fingerprint = binding.get("document_fingerprint")
    if not isinstance(revision_id, str) or not revision_id:
        return False
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.match(fingerprint):
        return False
    if binding.get("runtime") != app_package.RUNNER_IDENTITY:
        return False
    if binding.get("verifier_identity") != verifier.VERIFIER_IDENTITY:
        return False
    return True


def validate_candidate_package(value: Any) -> Optional[str]:
    """Return a bounded reason when ``value`` is not a valid candidate package.

    Rejects a non-mapping, an oversized record, any top-level key outside the
    fixed allowlist (implicitly rejecting script/HTML/import/path/command/url/
    network/install/mount/env/dependency/dockerfile/image/entrypoint/args
    fields), a wrong schema version, an unknown provenance or variant, a
    ``package`` that is not byte-identical to the code-owned variant, an invalid
    binding, or a malformed evidence list. Performs no execution.
    """
    if not isinstance(value, dict):
        return REASON_NOT_MAPPING
    if len(dumps(value).encode("utf-8")) > MAX_CANDIDATE_PACKAGE_BYTES:
        return REASON_TOO_LARGE
    if set(value) - _ALLOWED_KEYS:
        return REASON_UNKNOWN_KEYS
    if value.get("schema_version") != CANDIDATE_PACKAGE_SCHEMA_VERSION:
        return REASON_UNSUPPORTED_SCHEMA
    if value.get("provenance") not in PROVENANCES:
        return REASON_INVALID_PROVENANCE

    variant_id = value.get("variant_id")
    if variant_id not in verifier.VARIANTS:
        return REASON_UNKNOWN_VARIANT

    package = value.get("package")
    expected = verifier.variant_package(variant_id)
    if not isinstance(package, dict) or dumps(package) != dumps(expected):
        return REASON_PACKAGE_MISMATCH

    if not _valid_binding(value.get("binding")):
        return REASON_BINDING_INVALID

    evidence = value.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_CASES:
        return REASON_EVIDENCE_INVALID

    return None


__all__ = [
    "CANDIDATE_PACKAGE_SCHEMA_VERSION",
    "CANDIDATE_PACKAGE_GENERATOR",
    "MAX_CANDIDATE_PACKAGE_BYTES",
    "MAX_EVIDENCE_CASES",
    "PROVENANCE_DETERMINISTIC_FIXTURE",
    "PROVENANCE_MANUAL_VARIANT",
    "PROVENANCE_PROVIDER",
    "PROVENANCES",
    "STATE_DETERMINISTIC_FIXTURE",
    "STATE_MANUAL_VARIANT",
    "STATE_VALID_CANDIDATE",
    "STATE_INVALID",
    "STATE_BLOCKED",
    "STATE_INSUFFICIENT_EVIDENCE",
    "STATES",
    "REASON_NOT_MAPPING",
    "REASON_TOO_LARGE",
    "REASON_UNKNOWN_KEYS",
    "REASON_UNSUPPORTED_SCHEMA",
    "REASON_INVALID_PROVENANCE",
    "REASON_UNKNOWN_VARIANT",
    "REASON_PACKAGE_MISMATCH",
    "REASON_BINDING_INVALID",
    "REASON_EVIDENCE_INVALID",
    "new_candidate_package_id",
    "dumps",
    "provenance_state",
    "validate_candidate_package",
]
