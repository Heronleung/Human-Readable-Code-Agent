"""Protected code-owned quotation verifier (P4.7).

The **verifier** is the offline oracle that decides whether a candidate
package's claimed evidence is correct, without ever executing a package. It is
the code-owned source of truth for the supported quotation-rule variants and is
deliberately independent of both the runner and the candidate input:

* it holds a fixed identity (:data:`VERIFIER_IDENTITY`) and a code-owned
  variant registry (the reference package and the alternative benign variant);
* it holds **frozen regression cases** — hand-authored ``(form -> expected
  result)`` pairs — that no candidate input, repair path or provider payload can
  alter (there is no API to edit them);
* it performs no package execution, no filesystem, network, credential, command
  or provider access, and it never imports :mod:`hrca.runtime_handlers` (so the
  trusted in-image handler is never executed on the host as a fallback).

Verification is exact: a candidate's ``evidence`` must cover exactly the frozen
regression inputs and match the frozen expected outputs. Missing, extra,
malformed or mismatched evidence is a bounded refusal.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import app_package

VERIFIER_IDENTITY = "hrca-quotation-verifier:1"

# Code-owned variant identifiers (mirrored in hrca.app_package allowlists).
VARIANT_REFERENCE = "quotation-rules"
VARIANT_ALT = "quotation-rules-alt"
VARIANTS = frozenset({VARIANT_REFERENCE, VARIANT_ALT})

# Bounded refusal reasons (never interpolate candidate content).
REASON_UNKNOWN_VARIANT = "unknown variant"
REASON_EVIDENCE_NOT_LIST = "evidence is not a list"
REASON_EVIDENCE_MALFORMED = "evidence entry is malformed"
REASON_EVIDENCE_MISMATCH = "evidence does not match the protected expectations"
REASON_EVIDENCE_EXTRA = "evidence contains unexpected cases"
REASON_EVIDENCE_MISSING = "evidence is missing expected cases"


def variant_package(variant_id: str) -> Optional[Dict[str, Any]]:
    """Return the code-owned package manifest for ``variant_id``, or ``None``."""
    if variant_id == VARIANT_REFERENCE:
        return app_package.quotation_reference_package()
    if variant_id == VARIANT_ALT:
        return app_package.quotation_rules_alt_package()
    return None


# Frozen regression cases (hand-authored, reviewed). Each is a ``(form, result)``
# pair that pins the exact expected output for one benign input. These are the
# protected expectations; they are never derived from candidate input.
_REGRESSION_CASES: Dict[str, List[Tuple[Dict[str, Any], Dict[str, str]]]] = {
    VARIANT_REFERENCE: [
        (
            {"subtotal": "200.00", "member": True, "region": "west"},
            {"discount": "10.00", "shipping_fee": "0.00", "regional_fee": "0.00", "total": "190.00"},
        ),
        (
            {"subtotal": "50.00", "member": False, "region": "west"},
            {"discount": "0.00", "shipping_fee": "10.00", "regional_fee": "0.00", "total": "60.00"},
        ),
        (
            {"subtotal": "100.00", "member": False, "region": "north"},
            {"discount": "0.00", "shipping_fee": "0.00", "regional_fee": "5.00", "total": "105.00"},
        ),
        (
            {"subtotal": "9.99", "member": True, "region": "west"},
            {"discount": "0.50", "shipping_fee": "10.00", "regional_fee": "0.00", "total": "19.49"},
        ),
    ],
    VARIANT_ALT: [
        (
            {"subtotal": "200.00", "member": True, "region": "west"},
            {"discount": "6.00", "shipping_fee": "0.00", "regional_fee": "1.00", "total": "195.00"},
        ),
        (
            {"subtotal": "50.00", "member": False, "region": "west"},
            {"discount": "0.00", "shipping_fee": "12.00", "regional_fee": "1.00", "total": "63.00"},
        ),
        (
            {"subtotal": "160.00", "member": False, "region": "north"},
            {"discount": "0.00", "shipping_fee": "0.00", "regional_fee": "6.00", "total": "166.00"},
        ),
        (
            {"subtotal": "9.99", "member": True, "region": "west"},
            {"discount": "0.30", "shipping_fee": "12.00", "regional_fee": "1.00", "total": "22.69"},
        ),
    ],
}


def regression_cases(variant_id: str) -> List[Dict[str, Any]]:
    """Return the frozen regression cases as ``[{input, output}, ...]``."""
    return [
        {"input": inp, "output": out}
        for inp, out in _REGRESSION_CASES.get(variant_id, [])
    ]


def verify(variant_id: str, evidence: Any) -> Optional[str]:
    """Verify a candidate's ``evidence`` against the frozen expectations.

    ``evidence`` is a list of ``{"input": ..., "output": ...}`` mappings. It must
    cover exactly the frozen regression inputs (no missing, no extra) and match
    each frozen expected output. Returns ``None`` on success, else a bounded
    refusal reason. Performs no execution and never trusts candidate content.
    """
    if variant_id not in VARIANTS:
        return REASON_UNKNOWN_VARIANT
    if not isinstance(evidence, list):
        return REASON_EVIDENCE_NOT_LIST

    expected: Dict[str, Dict[str, str]] = {}
    for inp, out in _REGRESSION_CASES[variant_id]:
        expected[app_package.dumps(inp)] = out

    seen: set = set()
    for entry in evidence:
        if not isinstance(entry, dict):
            return REASON_EVIDENCE_MALFORMED
        inp = entry.get("input")
        out = entry.get("output")
        if not isinstance(inp, dict) or not isinstance(out, dict):
            return REASON_EVIDENCE_MALFORMED
        key = app_package.dumps(inp)
        if key not in expected:
            return REASON_EVIDENCE_EXTRA
        if app_package.dumps(out) != app_package.dumps(expected[key]):
            return REASON_EVIDENCE_MISMATCH
        seen.add(key)

    if set(expected) != seen:
        return REASON_EVIDENCE_MISSING
    return None


__all__ = [
    "VERIFIER_IDENTITY",
    "VARIANT_REFERENCE",
    "VARIANT_ALT",
    "VARIANTS",
    "REASON_UNKNOWN_VARIANT",
    "REASON_EVIDENCE_NOT_LIST",
    "REASON_EVIDENCE_MALFORMED",
    "REASON_EVIDENCE_MISMATCH",
    "REASON_EVIDENCE_EXTRA",
    "REASON_EVIDENCE_MISSING",
    "variant_package",
    "regression_cases",
    "verify",
]
