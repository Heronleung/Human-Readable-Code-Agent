"""Tests for the P4.7 protected code-owned verifier (:mod:`hrca.verifier`)."""

from __future__ import annotations

import unittest

from hrca import verifier


def _evidence(variant_id):
    return verifier.regression_cases(variant_id)


class VerifyTests(unittest.TestCase):
    def test_correct_evidence_for_both_variants(self):
        for variant_id in verifier.VARIANTS:
            with self.subTest(variant=variant_id):
                self.assertIsNone(verifier.verify(variant_id, _evidence(variant_id)))

    def test_forged_output_rejected(self):
        evidence = _evidence(verifier.VARIANT_REFERENCE)
        forged = [dict(e) for e in evidence]
        forged[0] = dict(forged[0])
        forged[0]["output"] = dict(forged[0]["output"])
        forged[0]["output"]["total"] = "0.01"
        self.assertEqual(
            verifier.verify(verifier.VARIANT_REFERENCE, forged),
            verifier.REASON_EVIDENCE_MISMATCH,
        )

    def test_tampered_input_rejected(self):
        evidence = _evidence(verifier.VARIANT_REFERENCE)
        tampered = [dict(e) for e in evidence]
        tampered[0] = dict(tampered[0])
        tampered[0]["input"] = {"subtotal": "1.00", "member": True, "region": "west"}
        # The tampered entry no longer matches a protected input (it reads as an
        # unexpected case), and the real case is now uncovered — rejected.
        self.assertIsNotNone(verifier.verify(verifier.VARIANT_REFERENCE, tampered))

    def test_missing_case_rejected(self):
        evidence = _evidence(verifier.VARIANT_REFERENCE)[1:]
        self.assertEqual(
            verifier.verify(verifier.VARIANT_REFERENCE, evidence),
            verifier.REASON_EVIDENCE_MISSING,
        )

    def test_extra_case_rejected(self):
        evidence = list(_evidence(verifier.VARIANT_REFERENCE))
        evidence.append(
            {"input": {"subtotal": "1.00", "member": False, "region": "east"},
             "output": {"discount": "0.00", "shipping_fee": "10.00",
                        "regional_fee": "8.00", "total": "19.00"}}
        )
        self.assertEqual(
            verifier.verify(verifier.VARIANT_REFERENCE, evidence),
            verifier.REASON_EVIDENCE_EXTRA,
        )

    def test_unknown_variant_rejected(self):
        self.assertEqual(
            verifier.verify("other-variant", []), verifier.REASON_UNKNOWN_VARIANT
        )

    def test_evidence_not_list_rejected(self):
        self.assertEqual(
            verifier.verify(verifier.VARIANT_REFERENCE, "not-a-list"),
            verifier.REASON_EVIDENCE_NOT_LIST,
        )

    def test_malformed_entry_rejected(self):
        self.assertEqual(
            verifier.verify(verifier.VARIANT_REFERENCE, ["not-a-dict"]),
            verifier.REASON_EVIDENCE_MALFORMED,
        )

    def test_alt_variant_expectations_are_distinct(self):
        ref = {e["input"]["subtotal"]: e["output"]["total"]
               for e in _evidence(verifier.VARIANT_REFERENCE)}
        alt = {e["input"]["subtotal"]: e["output"]["total"]
               for e in _evidence(verifier.VARIANT_ALT)}
        # The same first input yields a different total in the alternative variant.
        self.assertEqual(ref["200.00"], "190.00")
        self.assertEqual(alt["200.00"], "195.00")


class RegistryTests(unittest.TestCase):
    def test_variant_package_identity(self):
        self.assertEqual(
            verifier.variant_package(verifier.VARIANT_REFERENCE)["package_id"],
            "quotation-rules",
        )
        self.assertEqual(
            verifier.variant_package(verifier.VARIANT_ALT)["package_id"],
            "quotation-rules-alt",
        )

    def test_unknown_variant_package_is_none(self):
        self.assertIsNone(verifier.variant_package("nope"))


if __name__ == "__main__":
    unittest.main()
