"""Tests for the P4.7a rule-delta candidate record (:mod:`hrca.delta_candidate`)."""

from __future__ import annotations

import unittest

from hrca import app_package, delta_candidate, delta_verifier, rule_delta

_FP = "a" * 64


def _quotation_delta():
    return {
        "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
        "result_kind": rule_delta.RESULT_KIND_QUOTATION,
        "changes": [{"operation": "set_parameter", "rule_id": "quotation",
                     "parameter_id": "member_discount_rate", "value": "0.10"}],
    }


def _candidate(**overrides):
    delta = _quotation_delta()
    binding = {
        "document_revision_id": "rev:1",
        "document_fingerprint": _FP,
        "baseline_fingerprint": None,
        "delta_fingerprint": delta_candidate.fingerprint(delta),
        "runner_identity": app_package.RUNNER_IDENTITY,
        "verifier_identity": delta_verifier.DELTA_VERIFIER_IDENTITY,
    }
    evidence = delta_verifier.expected_cases("quotation", {"member_discount_rate": "0.10"})
    record = {
        "schema_version": delta_candidate.DELTA_CANDIDATE_SCHEMA_VERSION,
        "provenance": rule_delta.PROVENANCE_MANUAL_DELTA,
        "delta": delta,
        "binding": binding,
        "evidence": evidence,
    }
    record.update(overrides)
    return record


class ValidTests(unittest.TestCase):
    def test_manual_delta_candidate_is_valid(self):
        self.assertIsNone(delta_candidate.validate_candidate(_candidate()))

    def test_each_provenance_is_valid(self):
        for provenance in rule_delta.PROVENANCES:
            with self.subTest(provenance=provenance):
                self.assertIsNone(delta_candidate.validate_candidate(_candidate(provenance=provenance)))

    def test_late_fee_candidate_is_valid(self):
        delta = {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "result_kind": rule_delta.RESULT_KIND_LATE_RETURN_FEE,
            "changes": [{"operation": "set_parameter", "rule_id": "late_return_fee",
                         "parameter_id": "cap", "value": "24"}],
        }
        binding = {
            "document_revision_id": "rev:1",
            "document_fingerprint": _FP,
            "baseline_fingerprint": None,
            "delta_fingerprint": delta_candidate.fingerprint(delta),
            "runner_identity": app_package.RUNNER_IDENTITY,
            "verifier_identity": delta_verifier.DELTA_VERIFIER_IDENTITY,
        }
        evidence = delta_verifier.expected_cases("late_return_fee", {"cap": "24"})
        record = {
            "schema_version": delta_candidate.DELTA_CANDIDATE_SCHEMA_VERSION,
            "provenance": rule_delta.PROVENANCE_MANUAL_DELTA,
            "delta": delta,
            "binding": binding,
            "evidence": evidence,
        }
        self.assertIsNone(delta_candidate.validate_candidate(record))


class RejectTests(unittest.TestCase):
    def test_rejects_non_mapping(self):
        self.assertEqual(delta_candidate.validate_candidate(None), delta_candidate.REASON_NOT_MAPPING)

    def test_rejects_control_field(self):
        for key in ("code", "script", "import", "path", "command", "url", "network",
                    "dependencies", "mount", "env", "ui", "runtime", "verifier", "adopt"):
            with self.subTest(key=key):
                record = _candidate()
                record[key] = "evil"
                self.assertEqual(
                    delta_candidate.validate_candidate(record),
                    delta_candidate.REASON_UNKNOWN_KEYS,
                )

    def test_rejects_wrong_schema(self):
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(schema_version="9.9.9")),
            delta_candidate.REASON_UNSUPPORTED_SCHEMA,
        )

    def test_rejects_unknown_provenance(self):
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(provenance="generated")),
            delta_candidate.REASON_INVALID_PROVENANCE,
        )

    def test_rejects_invalid_delta(self):
        bad_delta = _quotation_delta()
        bad_delta["code"] = "import os"
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(delta=bad_delta)),
            delta_candidate.REASON_INVALID_DELTA,
        )

    def test_rejects_delta_fingerprint_mismatch(self):
        binding = dict(_candidate()["binding"])
        binding["delta_fingerprint"] = "0" * 64
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(binding=binding)),
            delta_candidate.REASON_DELTA_FINGERPRINT,
        )

    def test_rejects_runtime_mismatch(self):
        binding = dict(_candidate()["binding"])
        binding["runner_identity"] = "other:v9"
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(binding=binding)),
            delta_candidate.REASON_BINDING_INVALID,
        )

    def test_rejects_verifier_mismatch(self):
        binding = dict(_candidate()["binding"])
        binding["verifier_identity"] = "other-verifier:1"
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(binding=binding)),
            delta_candidate.REASON_BINDING_INVALID,
        )

    def test_rejects_malformed_fingerprint(self):
        binding = dict(_candidate()["binding"])
        binding["document_fingerprint"] = "not-hex"
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(binding=binding)),
            delta_candidate.REASON_BINDING_INVALID,
        )

    def test_rejects_missing_binding_key(self):
        binding = dict(_candidate()["binding"])
        del binding["document_fingerprint"]
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(binding=binding)),
            delta_candidate.REASON_BINDING_INVALID,
        )

    def test_rejects_extra_binding_key(self):
        binding = dict(_candidate()["binding"])
        binding["alternate_verifier"] = "other"
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(binding=binding)),
            delta_candidate.REASON_BINDING_INVALID,
        )

    def test_rejects_evidence_not_list(self):
        self.assertEqual(
            delta_candidate.validate_candidate(_candidate(evidence="nope")),
            delta_candidate.REASON_EVIDENCE_INVALID,
        )


class StateTests(unittest.TestCase):
    def test_provenance_state_mapping(self):
        self.assertEqual(
            delta_candidate.provenance_state(rule_delta.PROVENANCE_DETERMINISTIC_FIXTURE),
            delta_candidate.STATE_DETERMINISTIC_FIXTURE,
        )
        self.assertEqual(
            delta_candidate.provenance_state(rule_delta.PROVENANCE_MANUAL_DELTA),
            delta_candidate.STATE_MANUAL_DELTA,
        )
        self.assertEqual(
            delta_candidate.provenance_state(rule_delta.PROVENANCE_PROVIDER_DELTA),
            delta_candidate.STATE_VALID_CANDIDATE,
        )


if __name__ == "__main__":
    unittest.main()
