"""Protected independent rule-delta oracle (P4.7a).

The **delta verifier** independently recomputes the expected result for a rule
family and a resolved parameter set, so a candidate's claimed evidence can be
checked without trusting the candidate, the runner evaluator, or any provider
output. It is deliberately independent of :mod:`hrca.runtime_handlers`: it holds
its own reviewed arithmetic and its own frozen protected input cases, so a
candidate/delta can neither read nor modify the protected expectations, and a
future provider or repair loop never receives them.

It performs no package execution and no filesystem/network/credential/command/
provider access. It is code-owned and pure (Qt-free, stdlib-only).
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from . import rule_delta

DELTA_VERIFIER_IDENTITY = "hrca-rule-delta-verifier:1"

_TWO = Decimal("0.01")

# Bounded refusal reasons (never interpolate candidate content).
REASON_UNKNOWN_RULE = "unknown rule"
REASON_EVIDENCE_NOT_LIST = "evidence is not a list"
REASON_EVIDENCE_MALFORMED = "evidence entry is malformed"
REASON_EVIDENCE_MISMATCH = "evidence does not match the protected expectations"
REASON_EVIDENCE_EXTRA = "evidence contains unexpected cases"
REASON_EVIDENCE_MISSING = "evidence is missing expected cases"


def _decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _money(value: Decimal) -> str:
    return str(value.quantize(_TWO, rounding=ROUND_HALF_UP))


# -- independent arithmetic (code-owned, never shared with the runner) --------


def _quotation_expected(form: Dict[str, Any], parameters: Dict[str, str]) -> Optional[Dict[str, str]]:
    subtotal = _decimal(form.get("subtotal"))
    member = form.get("member")
    region = form.get("region")
    if subtotal is None or subtotal < 0 or not isinstance(member, bool):
        return None
    regional = {"west": "0", "north": "5", "south": "3", "east": "8"}.get(region)
    if regional is None:
        return None

    rate = _decimal(parameters.get("member_discount_rate")) or Decimal("0.05")
    discount = (subtotal * rate if member else Decimal("0")).quantize(_TWO, rounding=ROUND_HALF_UP)
    base = subtotal - discount
    shipping = Decimal("0") if base >= Decimal("100") else Decimal("10")
    regional_fee = Decimal(regional)
    total = subtotal - discount + shipping + regional_fee
    return {
        "discount": _money(discount),
        "shipping_fee": _money(shipping),
        "regional_fee": _money(regional_fee),
        "total": _money(total),
    }


def _late_fee_expected(form: Dict[str, Any], parameters: Dict[str, str]) -> Optional[Dict[str, str]]:
    days = form.get("days_late")
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        return None
    cap = _decimal(parameters.get("cap")) or Decimal("30")
    fee = min(Decimal(days) * Decimal("3"), cap)
    return {"fee": _money(fee)}


_EXPECTED = {
    rule_delta.RESULT_KIND_QUOTATION: _quotation_expected,
    rule_delta.RESULT_KIND_LATE_RETURN_FEE: _late_fee_expected,
}

# Frozen protected input cases (hand-authored, reviewed). Each family pins a
# small set of inputs that exercise both changed and preserved behaviour; the
# expected output is recomputed from these inputs and the resolved parameters.
_PROTECTED_INPUTS = {
    rule_delta.RESULT_KIND_QUOTATION: [
        {"subtotal": "200.00", "member": True, "region": "west"},
        {"subtotal": "200.00", "member": False, "region": "west"},
        {"subtotal": "99.99", "member": False, "region": "north"},
        {"subtotal": "50.00", "member": True, "region": "east"},
    ],
    rule_delta.RESULT_KIND_LATE_RETURN_FEE: [
        {"days_late": 1},
        {"days_late": 8},
        {"days_late": 11},
        {"days_late": 0},
    ],
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def verify(rule_id: str, parameters: Dict[str, str], evidence: Any) -> Optional[str]:
    """Verify candidate ``evidence`` against the independent oracle.

    ``parameters`` is the resolved delta parameter mapping; ``evidence`` is a
    list of ``{"input": ..., "output": ...}`` claims. For each frozen protected
    input the expected output is recomputed independently and the evidence must
    cover exactly the protected inputs with matching outputs. Returns ``None``
    on success, else a bounded refusal reason.
    """
    expected_fn = _EXPECTED.get(rule_id)
    if expected_fn is None:
        return REASON_UNKNOWN_RULE
    if not isinstance(evidence, list):
        return REASON_EVIDENCE_NOT_LIST

    expected: Dict[str, Dict[str, str]] = {}
    for inp in _PROTECTED_INPUTS[rule_id]:
        out = expected_fn(inp, parameters)
        if out is None:  # pragma: no cover - protected inputs are valid
            return REASON_UNKNOWN_RULE
        expected[_canonical(inp)] = out

    seen = set()
    for entry in evidence:
        if not isinstance(entry, dict):
            return REASON_EVIDENCE_MALFORMED
        inp = entry.get("input")
        out = entry.get("output")
        if not isinstance(inp, dict) or not isinstance(out, dict):
            return REASON_EVIDENCE_MALFORMED
        key = _canonical(inp)
        if key not in expected:
            return REASON_EVIDENCE_EXTRA
        if _canonical(out) != _canonical(expected[key]):
            return REASON_EVIDENCE_MISMATCH
        seen.add(key)

    if set(expected) != seen:
        return REASON_EVIDENCE_MISSING
    return None


def protected_inputs(rule_id: str) -> List[Dict[str, Any]]:
    """Return the frozen protected inputs for ``rule_id`` (or ``[]``)."""
    return list(_PROTECTED_INPUTS.get(rule_id, []))


def expected_cases(rule_id: str, parameters: Dict[str, str]) -> List[Dict[str, Any]]:
    """Return ``[{input, output}, ...]`` for the protected inputs and ``parameters``."""
    expected_fn = _EXPECTED.get(rule_id)
    if expected_fn is None:
        return []
    cases = []
    for inp in _PROTECTED_INPUTS.get(rule_id, []):
        out = expected_fn(inp, parameters)
        if out is not None:
            cases.append({"input": inp, "output": out})
    return cases


__all__ = [
    "DELTA_VERIFIER_IDENTITY",
    "REASON_UNKNOWN_RULE",
    "REASON_EVIDENCE_NOT_LIST",
    "REASON_EVIDENCE_MALFORMED",
    "REASON_EVIDENCE_MISMATCH",
    "REASON_EVIDENCE_EXTRA",
    "REASON_EVIDENCE_MISSING",
    "verify",
    "protected_inputs",
    "expected_cases",
]
