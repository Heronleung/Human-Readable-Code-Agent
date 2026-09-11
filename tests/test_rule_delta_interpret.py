"""Tests for the P4.8 rule-delta interpretation domain.

These prove the bounded disclosure manifest, the one-outcome provider-output
contract, the cost reservation arithmetic and the fail-closed states — all
offline and deterministic (no network, credential or provider access).
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from hrca import rule_delta, rule_delta_interpret


def _delta(**overrides):
    delta = {
        "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
        "result_kind": rule_delta.RESULT_KIND_QUOTATION,
        "changes": [
            {
                "operation": rule_delta.OPERATION_SET_PARAMETER,
                "rule_id": rule_delta.RESULT_KIND_QUOTATION,
                "parameter_id": "member_discount_rate",
                "value": "0.10",
            }
        ],
    }
    delta.update(overrides)
    return delta


def _delta_output(delta=None):
    return {
        "schema_version": rule_delta_interpret.RULE_DELTA_INTERPRET_SCHEMA_VERSION,
        "outcome": rule_delta_interpret.OUTCOME_DELTA,
        "delta": delta or _delta(),
    }


def _clarification_output():
    return {
        "schema_version": rule_delta_interpret.RULE_DELTA_INTERPRET_SCHEMA_VERSION,
        "outcome": rule_delta_interpret.OUTCOME_CLARIFICATION_REQUIRED,
        "clarification_questions": ["which rule should change?"],
    }


def _unsupported_output():
    return {
        "schema_version": rule_delta_interpret.RULE_DELTA_INTERPRET_SCHEMA_VERSION,
        "outcome": rule_delta_interpret.OUTCOME_UNSUPPORTED,
        "unsupported_requirements": ["a new rule family is required"],
    }


class InstructionTests(unittest.TestCase):
    def test_instruction_is_deterministic(self):
        self.assertEqual(
            rule_delta_interpret.build_instruction(),
            rule_delta_interpret.build_instruction(),
        )

    def test_instruction_names_schema_allowlist_and_baseline(self):
        instruction = rule_delta_interpret.build_instruction()
        self.assertIn(rule_delta.RULE_DELTA_SCHEMA_VERSION, instruction)
        self.assertIn("member_discount_rate", instruction)
        self.assertIn("quotation", instruction)
        self.assertIn("0.05", instruction)  # baseline default

    def test_examples_are_bounded(self):
        self.assertLessEqual(len(rule_delta_interpret.examples()),
                            rule_delta_interpret.MAX_EXAMPLES)
        for example in rule_delta_interpret.examples():
            self.assertIsNone(
                rule_delta.validate_delta(example["delta"])
            )

    def test_instruction_never_contains_protected_oracle_data(self):
        # The protected inputs and their expected outputs (held in
        # hrca.delta_verifier) must never appear in the provider instruction.
        # (``member`` is omitted because it is a substring of the allowlisted
        # ``member_discount_rate`` parameter, which *is* part of the schema.)
        instruction = rule_delta_interpret.build_instruction()
        for forbidden in (
            "subtotal", "region", "days_late",
            "200.00", "99.99", "50.00", "180.00", "20.00",
            "west", "north", "south", "east",
        ):
            self.assertNotIn(forbidden, instruction)


class LimitTests(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(rule_delta_interpret.estimate_tokens(""), 0)
        self.assertEqual(rule_delta_interpret.estimate_tokens("abcd"), 1)

    def test_request_too_large_within_bounds(self):
        self.assertFalse(
            rule_delta_interpret.request_too_large(
                rule_delta_interpret.build_instruction(), "Members get 10% off."
            )
        )

    def test_request_too_large_over_bytes(self):
        huge = "x" * (rule_delta_interpret.MAX_REQUEST_BYTES + 1)
        self.assertTrue(
            rule_delta_interpret.request_too_large(
                rule_delta_interpret.build_instruction(), huge
            )
        )


class ReservationTests(unittest.TestCase):
    def test_pricing_known_for_allowlisted_model(self):
        self.assertIsNotNone(
            rule_delta_interpret.pricing_for(rule_delta_interpret.MODEL_ID)
        )

    def test_pricing_unknown_for_other_model(self):
        self.assertIsNone(rule_delta_interpret.pricing_for("other-model"))

    def test_worst_case_cost_below_reservation(self):
        cost = rule_delta_interpret.worst_case_cost_usd()
        self.assertIsNotNone(cost)
        self.assertLess(cost, rule_delta_interpret.RESERVATION_USD)

    def test_reservation_record_is_sufficient(self):
        record = rule_delta_interpret.reservation_record()
        self.assertEqual(record["amount_usd"], "0.01")
        self.assertTrue(record["sufficient"])
        self.assertIsNotNone(record["worst_case_cost_usd"])

    def test_usage_within_reservation(self):
        self.assertTrue(
            rule_delta_interpret.usage_within_reservation(
                {"prompt_tokens": 100, "completion_tokens": 100}
            )
        )
        self.assertFalse(
            rule_delta_interpret.usage_within_reservation(
                {"prompt_tokens": 10_000_000, "completion_tokens": 10_000_000}
            )
        )


class TokenTests(unittest.TestCase):
    def test_token_is_stable_and_content_addressed(self):
        kwargs = dict(
            document_id="doc:1",
            revision_id="rev:1",
            fingerprint="a" * 64,
            baseline_fingerprint=None,
            requirement_text="Members get 10% off.",
        )
        first = rule_delta_interpret.interpretation_token(**kwargs)
        second = rule_delta_interpret.interpretation_token(**kwargs)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("delta:"))

    def test_token_changes_with_requirement(self):
        base = dict(
            document_id="doc:1", revision_id="rev:1", fingerprint="a" * 64,
            baseline_fingerprint=None,
        )
        a = rule_delta_interpret.interpretation_token(
            requirement_text="Members get 10% off.", **base
        )
        b = rule_delta_interpret.interpretation_token(
            requirement_text="Members get 20% off.", **base
        )
        self.assertNotEqual(a, b)


class DisclosureTests(unittest.TestCase):
    def test_disclosure_is_complete(self):
        disclosure = rule_delta_interpret.build_disclosure(
            requirement_text="Members get 10% off."
        )
        self.assertEqual(disclosure["provider_id"], "deepseek")
        self.assertEqual(disclosure["model"], "deepseek-v4-flash")
        self.assertTrue(disclosure["one_attempt"])
        self.assertTrue(disclosure["no_retry"])
        self.assertTrue(disclosure["no_paid_repair"])
        self.assertIn("policy_warning", disclosure)
        self.assertIn("egress_statement", disclosure)
        self.assertIn("account_cap_statement", disclosure)
        self.assertIn("US$8 is not an enforced account cap", disclosure["account_cap_statement"])
        self.assertEqual(
            disclosure["caps"]["request_bytes"],
            rule_delta_interpret.MAX_REQUEST_BYTES,
        )
        self.assertGreater(disclosure["total_bytes"], 0)
        self.assertEqual(len(disclosure["items"]), 2)

    def test_disclosure_never_contains_protected_oracle_data(self):
        serialized = rule_delta_interpret.dumps(
            rule_delta_interpret.build_disclosure(requirement_text="Members get 10% off.")
        )
        for forbidden in ("subtotal", "200.00", "180.00", "west", "days_late"):
            self.assertNotIn(forbidden, serialized)


class ProviderOutputValidationTests(unittest.TestCase):
    def test_accepts_delta(self):
        self.assertIsNone(
            rule_delta_interpret.validate_provider_output(_delta_output())
        )

    def test_accepts_clarification(self):
        self.assertIsNone(
            rule_delta_interpret.validate_provider_output(_clarification_output())
        )

    def test_accepts_unsupported(self):
        self.assertIsNone(
            rule_delta_interpret.validate_provider_output(_unsupported_output())
        )

    def test_rejects_non_mapping(self):
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(None),
            rule_delta_interpret.REASON_NOT_MAPPING,
        )

    def test_rejects_unknown_outcome(self):
        out = _delta_output()
        out["outcome"] = "code"
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(out),
            rule_delta_interpret.REASON_UNKNOWN_OUTCOME,
        )

    def test_rejects_unknown_field(self):
        out = _delta_output()
        out["code"] = "import os"
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(out),
            rule_delta_interpret.REASON_UNEXPECTED_FIELD,
        )

    def test_rejects_missing_delta(self):
        out = _delta_output()
        del out["delta"]
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(out),
            rule_delta_interpret.REASON_MISSING_DELTA,
        )

    def test_rejects_invalid_delta(self):
        bad = _delta()
        bad["code"] = "import os"
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(_delta_output(delta=bad)),
            rule_delta_interpret.REASON_INVALID_NOTES,
        )

    def test_rejects_clarification_without_notes(self):
        out = _clarification_output()
        out["clarification_questions"] = []
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(out),
            rule_delta_interpret.REASON_MISSING_NOTES,
        )

    def test_rejects_delta_with_stray_clarification(self):
        out = _delta_output()
        out["clarification_questions"] = ["stray"]
        self.assertEqual(
            rule_delta_interpret.validate_provider_output(out),
            rule_delta_interpret.REASON_UNEXPECTED_FIELD,
        )

    def test_normalize_delta_output(self):
        normalized = rule_delta_interpret.normalize_output(_delta_output())
        self.assertEqual(normalized["outcome"], rule_delta_interpret.OUTCOME_DELTA)
        self.assertIsNone(
            rule_delta.validate_delta(normalized["delta"])
        )

    def test_normalize_clarification_output(self):
        normalized = rule_delta_interpret.normalize_output(_clarification_output())
        self.assertEqual(
            normalized["clarification_questions"], ["which rule should change?"]
        )


class ResultAssemblyTests(unittest.TestCase):
    def test_reviewable_candidate_result(self):
        result = rule_delta_interpret.assemble_result(
            state=rule_delta_interpret.STATE_REVIEWABLE_CANDIDATE,
            token="delta:abc",
            sent=True,
            usage={"total_tokens": 15},
            candidate={"provenance": "provider_delta"},
            limitations=[],
        )
        self.assertEqual(
            result["state"], rule_delta_interpret.STATE_REVIEWABLE_CANDIDATE
        )
        self.assertEqual(result["generator"], rule_delta_interpret.RULE_DELTA_INTERPRET_GENERATOR)
        self.assertIsNotNone(result["candidate"])

    def test_failure_result_has_no_candidate(self):
        result = rule_delta_interpret.assemble_result(
            state=rule_delta_interpret.STATE_TIMEOUT,
            token="delta:abc",
            sent=True,
            usage=None,
            candidate=None,
            limitations=["timeout"],
        )
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["limitations"], ["timeout"])


if __name__ == "__main__":
    unittest.main()
