"""Tests for the P4.7 candidate-package contract (:mod:`hrca.candidate_package`)."""

from __future__ import annotations

import unittest

from hrca import app_package, candidate_package, verifier

_FINGERPRINT = "a" * 64


def _candidate(**overrides):
    record = {
        "schema_version": candidate_package.CANDIDATE_PACKAGE_SCHEMA_VERSION,
        "candidate_package_id": candidate_package.new_candidate_package_id(),
        "provenance": candidate_package.PROVENANCE_DETERMINISTIC_FIXTURE,
        "variant_id": verifier.VARIANT_REFERENCE,
        "package": verifier.variant_package(verifier.VARIANT_REFERENCE),
        "binding": {
            "document_revision_id": "rev:1",
            "document_fingerprint": _FINGERPRINT,
            "runtime": app_package.RUNNER_IDENTITY,
            "verifier_identity": verifier.VERIFIER_IDENTITY,
        },
        "evidence": verifier.regression_cases(verifier.VARIANT_REFERENCE),
    }
    record.update(overrides)
    return record


class ValidTests(unittest.TestCase):
    def test_reference_variant_is_valid(self):
        self.assertIsNone(candidate_package.validate_candidate_package(_candidate()))

    def test_alt_variant_is_valid(self):
        alt = _candidate(
            variant_id=verifier.VARIANT_ALT,
            package=verifier.variant_package(verifier.VARIANT_ALT),
            evidence=verifier.regression_cases(verifier.VARIANT_ALT),
        )
        self.assertIsNone(candidate_package.validate_candidate_package(alt))

    def test_each_provenance_is_valid(self):
        for provenance in candidate_package.PROVENANCES:
            with self.subTest(provenance=provenance):
                self.assertIsNone(
                    candidate_package.validate_candidate_package(_candidate(provenance=provenance))
                )


class RejectFieldTests(unittest.TestCase):
    """Every dangerous field is rejected as an unknown top-level key."""

    def test_rejects_control_fields(self):
        for key in (
            "script", "code", "html", "command", "cmd", "entrypoint", "args",
            "import", "imports", "path", "url", "network", "install",
            "dependencies", "mount", "mounts", "env", "environment",
            "dockerfile", "image",
        ):
            with self.subTest(key=key):
                record = _candidate()
                record[key] = "import os; os.system('evil')"
                self.assertEqual(
                    candidate_package.validate_candidate_package(record),
                    candidate_package.REASON_UNKNOWN_KEYS,
                )

    def test_rejects_non_mapping(self):
        self.assertEqual(
            candidate_package.validate_candidate_package(None),
            candidate_package.REASON_NOT_MAPPING,
        )

    def test_rejects_oversized(self):
        record = _candidate(evidence=[{"input": {"x": "y" * 2000}, "output": {"z": "w" * 2000}}] * 64)
        # Push the record over the bound with a huge title-like field via package
        # is hard (package must match); instead use a huge candidate_package_id.
        record["candidate_package_id"] = "cp:" + "x" * 4000
        self.assertEqual(
            candidate_package.validate_candidate_package(record),
            candidate_package.REASON_TOO_LARGE,
        )

    def test_rejects_wrong_schema_version(self):
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(schema_version="9.9.9")),
            candidate_package.REASON_UNSUPPORTED_SCHEMA,
        )

    def test_rejects_unknown_provenance(self):
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(provenance="generated")),
            candidate_package.REASON_INVALID_PROVENANCE,
        )

    def test_rejects_unknown_variant(self):
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(variant_id="other")),
            candidate_package.REASON_UNKNOWN_VARIANT,
        )


class PackageMismatchTests(unittest.TestCase):
    """The renderer schema is code-owned: a candidate cannot alter it."""

    def test_rejects_altered_result_schema(self):
        package = app_package.quotation_reference_package()
        package["result"] = package["result"] + [{"name": "secret", "type": "text"}]
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(package=package)),
            candidate_package.REASON_PACKAGE_MISMATCH,
        )

    def test_rejects_altered_form_schema(self):
        package = app_package.quotation_reference_package()
        package["form"][0]["type"] = "text"
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(package=package)),
            candidate_package.REASON_PACKAGE_MISMATCH,
        )

    def test_rejects_different_handler(self):
        package = app_package.quotation_reference_package()
        package["handler"] = "quotation_rules_alt.evaluate"
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(package=package)),
            candidate_package.REASON_PACKAGE_MISMATCH,
        )


class BindingTests(unittest.TestCase):
    def test_rejects_runtime_mismatch(self):
        binding = dict(_candidate()["binding"])
        binding["runtime"] = "other-runner:v9"
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(binding=binding)),
            candidate_package.REASON_BINDING_INVALID,
        )

    def test_rejects_verifier_mismatch(self):
        binding = dict(_candidate()["binding"])
        binding["verifier_identity"] = "other-verifier:1"
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(binding=binding)),
            candidate_package.REASON_BINDING_INVALID,
        )

    def test_rejects_missing_binding_key(self):
        binding = dict(_candidate()["binding"])
        del binding["document_fingerprint"]
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(binding=binding)),
            candidate_package.REASON_BINDING_INVALID,
        )

    def test_rejects_extra_binding_key(self):
        binding = dict(_candidate()["binding"])
        binding["alternate_verifier"] = "other"
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(binding=binding)),
            candidate_package.REASON_BINDING_INVALID,
        )

    def test_rejects_malformed_fingerprint(self):
        binding = dict(_candidate()["binding"])
        binding["document_fingerprint"] = "not-hex"
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(binding=binding)),
            candidate_package.REASON_BINDING_INVALID,
        )


class EvidenceTests(unittest.TestCase):
    def test_rejects_non_list_evidence(self):
        self.assertEqual(
            candidate_package.validate_candidate_package(_candidate(evidence="nope")),
            candidate_package.REASON_EVIDENCE_INVALID,
        )


class StateTests(unittest.TestCase):
    def test_provenance_state_mapping(self):
        self.assertEqual(
            candidate_package.provenance_state(candidate_package.PROVENANCE_DETERMINISTIC_FIXTURE),
            candidate_package.STATE_DETERMINISTIC_FIXTURE,
        )
        self.assertEqual(
            candidate_package.provenance_state(candidate_package.PROVENANCE_MANUAL_VARIANT),
            candidate_package.STATE_MANUAL_VARIANT,
        )
        self.assertEqual(
            candidate_package.provenance_state(candidate_package.PROVENANCE_PROVIDER),
            candidate_package.STATE_VALID_CANDIDATE,
        )


if __name__ == "__main__":
    unittest.main()
