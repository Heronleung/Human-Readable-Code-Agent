"""Tests for the P4.7a declarative rule-delta contract (:mod:`hrca.rule_delta`)."""

from __future__ import annotations

import unittest

from hrca import rule_delta


def _delta(**overrides):
    d = {
        "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
        "result_kind": rule_delta.RESULT_KIND_QUOTATION,
        "changes": [
            {"operation": "set_parameter", "rule_id": "quotation",
             "parameter_id": "member_discount_rate", "value": "0.10"}
        ],
    }
    d.update(overrides)
    return d


class ValidTests(unittest.TestCase):
    def test_quotation_discount_delta_is_valid(self):
        self.assertIsNone(rule_delta.validate_delta(_delta()))

    def test_late_fee_cap_delta_is_valid(self):
        d = _delta(
            result_kind=rule_delta.RESULT_KIND_LATE_RETURN_FEE,
            changes=[{"operation": "set_parameter", "rule_id": "late_return_fee",
                      "parameter_id": "cap", "value": "24"}],
        )
        self.assertIsNone(rule_delta.validate_delta(d))

    def test_resolve_returns_exact_typed_value(self):
        resolved = rule_delta.resolve_delta(_delta())
        self.assertEqual(resolved["rule_id"], "quotation")
        self.assertEqual(resolved["parameters"], {"member_discount_rate": "0.10"})

    def test_optional_notes_are_valid(self):
        d = _delta(clarification_questions=["why?"], unsupported_requirements=["none"])
        self.assertIsNone(rule_delta.validate_delta(d))


class RejectTests(unittest.TestCase):
    def test_rejects_non_mapping(self):
        self.assertEqual(rule_delta.validate_delta(None), rule_delta.REASON_NOT_MAPPING)

    def test_rejects_unknown_top_level_field(self):
        d = _delta(code="import os")
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_UNKNOWN_KEYS)

    def test_rejects_control_fields(self):
        for key in ("script", "code", "import", "imports", "command", "cmd", "path",
                    "url", "network", "dependencies", "install", "mount", "mounts",
                    "env", "environment", "ui", "runtime", "verifier", "dockerfile",
                    "image", "entrypoint", "args"):
            with self.subTest(key=key):
                d = _delta()
                d[key] = "evil"
                self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_UNKNOWN_KEYS)

    def test_rejects_wrong_schema_version(self):
        self.assertEqual(
            rule_delta.validate_delta(_delta(schema_version="9.9.9")),
            rule_delta.REASON_UNSUPPORTED_SCHEMA,
        )

    def test_rejects_unknown_result_kind(self):
        self.assertEqual(
            rule_delta.validate_delta(_delta(result_kind="other")),
            rule_delta.REASON_UNKNOWN_RESULT_KIND,
        )

    def test_rejects_no_changes(self):
        self.assertEqual(
            rule_delta.validate_delta(_delta(changes=[])),
            rule_delta.REASON_NO_CHANGES,
        )

    def test_rejects_unknown_operation(self):
        d = _delta(changes=[{"operation": "eval", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": "0.10"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_UNKNOWN_OPERATION)

    def test_rejects_rule_mismatch(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "other",
                             "parameter_id": "member_discount_rate", "value": "0.10"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_RULE_MISMATCH)

    def test_rejects_unknown_parameter(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "shipping_threshold", "value": "0.10"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_UNKNOWN_PARAMETER)

    def test_rejects_duplicate_parameter_change(self):
        d = _delta(changes=[
            {"operation": "set_parameter", "rule_id": "quotation",
             "parameter_id": "member_discount_rate", "value": "0.10"},
            {"operation": "set_parameter", "rule_id": "quotation",
             "parameter_id": "member_discount_rate", "value": "0.20"},
        ])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_DUPLICATE)

    def test_rejects_malformed_decimal(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": "ten"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_INVALID_VALUE)

    def test_rejects_non_string_value(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": 0.10}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_INVALID_VALUE)

    def test_rejects_out_of_range(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": "1.5"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_OUT_OF_RANGE)

    def test_rejects_negative_value(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": "-0.1"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_OUT_OF_RANGE)

    def test_rejects_over_precise_value(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": "0.12345"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_PRECISION)

    def test_rejects_change_extra_field(self):
        d = _delta(changes=[{"operation": "set_parameter", "rule_id": "quotation",
                             "parameter_id": "member_discount_rate", "value": "0.10",
                             "expr": "1+1"}])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_INVALID_CHANGE)

    def test_rejects_invalid_notes(self):
        d = _delta(clarification_questions=["ok", 123])
        self.assertEqual(rule_delta.validate_delta(d), rule_delta.REASON_INVALID_NOTES)

    def test_resolve_invalid_returns_none(self):
        self.assertIsNone(rule_delta.resolve_delta(_delta(value="bad")))


if __name__ == "__main__":
    unittest.main()
