"""Rule-delta candidate record and binding (P4.7a).

The versioned, data-only record that wraps a validated rule delta, an exact
binding and claimed evidence, so a later provider flow can submit a *semantic
delta* rather than select a prewritten variant. It is distinct from the P4.7
byte-match candidate package and from the document/version authority.

The binding is the identity contract: the Working Document revision id and
content fingerprint, the accepted-baseline fingerprint, the delta fingerprint
(an immutable hash of the canonical delta), the runner identity and the
protected verifier identity. The evidence is a bounded list of input/output
claims that the independent oracle (:mod:`hrca.delta_verifier`) checks.

This module is pure (Qt-free, stdlib-only): no filesystem, network, credential,
command, Git, provider, runner or package-execution access, and no adoption.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from . import app_package, delta_verifier, rule_delta

DELTA_CANDIDATE_SCHEMA_VERSION = "1.0.0"
DELTA_CANDIDATE_GENERATOR = "hrca-rule-delta-candidate"

# Bounded limits (code-owned).
MAX_CANDIDATE_BYTES = 64 * 1024
MAX_EVIDENCE_CASES = 64

# Read-only evidence states.
STATE_DETERMINISTIC_FIXTURE = "deterministic_fixture"
STATE_MANUAL_DELTA = "manually_validated_delta"
STATE_VALID_CANDIDATE = "valid_candidate"
STATE_INVALID = "invalid"
STATE_BLOCKED = "blocked"
STATE_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
STATES = frozenset(
    {
        STATE_DETERMINISTIC_FIXTURE,
        STATE_MANUAL_DELTA,
        STATE_VALID_CANDIDATE,
        STATE_INVALID,
        STATE_BLOCKED,
        STATE_INSUFFICIENT_EVIDENCE,
    }
)

# The exact top-level keys. Anything else (code/script/import/path/command/url/
# network/mount/env/dependency/runtime/verifier/UI/...) is rejected.
_ALLOWED_KEYS = frozenset(
    {"schema_version", "provenance", "delta", "binding", "evidence"}
)
# The exact binding keys.
_ALLOWED_BINDING_KEYS = frozenset(
    {
        "document_revision_id",
        "document_fingerprint",
        "baseline_fingerprint",
        "delta_fingerprint",
        "runner_identity",
        "verifier_identity",
    }
)

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

# Bounded refusal reasons.
REASON_NOT_MAPPING = "candidate is not a mapping"
REASON_TOO_LARGE = "candidate is too large"
REASON_UNKNOWN_KEYS = "candidate has unsupported fields"
REASON_UNSUPPORTED_SCHEMA = "unsupported schema_version"
REASON_INVALID_PROVENANCE = "invalid provenance"
REASON_INVALID_DELTA = "delta is invalid"
REASON_BINDING_INVALID = "binding is invalid"
REASON_DELTA_FINGERPRINT = "delta fingerprint does not match the delta"
REASON_EVIDENCE_INVALID = "evidence is invalid"


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: Any) -> str:
    """Return the SHA-256 hex fingerprint of ``value``'s canonical serialization."""
    return sha256_hex(rule_delta.dumps(value).encode("utf-8"))


def provenance_state(provenance: str) -> str:
    """Map a provenance token to its read-only evidence state."""
    if provenance == rule_delta.PROVENANCE_DETERMINISTIC_FIXTURE:
        return STATE_DETERMINISTIC_FIXTURE
    if provenance == rule_delta.PROVENANCE_MANUAL_DELTA:
        return STATE_MANUAL_DELTA
    return STATE_VALID_CANDIDATE  # provider_delta


def _valid_binding(binding: Any) -> bool:
    if not isinstance(binding, dict) or set(binding) != _ALLOWED_BINDING_KEYS:
        return False
    if not isinstance(binding.get("document_revision_id"), str) or not binding.get("document_revision_id"):
        return False
    if not isinstance(binding.get("document_fingerprint"), str) or not _FINGERPRINT_RE.match(binding["document_fingerprint"]):
        return False
    baseline = binding.get("baseline_fingerprint")
    if baseline is not None and (not isinstance(baseline, str) or not _FINGERPRINT_RE.match(baseline)):
        return False
    if not isinstance(binding.get("delta_fingerprint"), str) or not _FINGERPRINT_RE.match(binding["delta_fingerprint"]):
        return False
    if binding.get("runner_identity") != app_package.RUNNER_IDENTITY:
        return False
    if binding.get("verifier_identity") != delta_verifier.DELTA_VERIFIER_IDENTITY:
        return False
    return True


def validate_candidate(value: Any) -> Optional[str]:
    """Return a bounded reason when ``value`` is not a valid delta candidate.

    Rejects a non-mapping, an oversized record, any top-level key outside the
    fixed allowlist, a wrong schema version, an unknown provenance, an invalid
    delta, an invalid binding (wrong identities, malformed fingerprints), a
    delta fingerprint that does not match the actual delta, or a malformed
    evidence list.
    """
    if not isinstance(value, dict):
        return REASON_NOT_MAPPING
    if len(rule_delta.dumps(value).encode("utf-8")) > MAX_CANDIDATE_BYTES:
        return REASON_TOO_LARGE
    if set(value) - _ALLOWED_KEYS:
        return REASON_UNKNOWN_KEYS
    if value.get("schema_version") != DELTA_CANDIDATE_SCHEMA_VERSION:
        return REASON_UNSUPPORTED_SCHEMA
    if value.get("provenance") not in rule_delta.PROVENANCES:
        return REASON_INVALID_PROVENANCE

    delta = value.get("delta")
    if rule_delta.validate_delta(delta) is not None:
        return REASON_INVALID_DELTA

    binding = value.get("binding")
    if not _valid_binding(binding):
        return REASON_BINDING_INVALID
    if binding["delta_fingerprint"] != fingerprint(delta):
        return REASON_DELTA_FINGERPRINT

    evidence = value.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE_CASES:
        return REASON_EVIDENCE_INVALID
    return None


__all__ = [
    "DELTA_CANDIDATE_SCHEMA_VERSION",
    "DELTA_CANDIDATE_GENERATOR",
    "MAX_CANDIDATE_BYTES",
    "MAX_EVIDENCE_CASES",
    "STATE_DETERMINISTIC_FIXTURE",
    "STATE_MANUAL_DELTA",
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
    "REASON_INVALID_DELTA",
    "REASON_BINDING_INVALID",
    "REASON_DELTA_FINGERPRINT",
    "REASON_EVIDENCE_INVALID",
    "sha256_hex",
    "fingerprint",
    "provenance_state",
    "validate_candidate",
]
