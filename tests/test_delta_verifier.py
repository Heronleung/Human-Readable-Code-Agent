"""Tests for the P4.7a independent rule-delta oracle (:mod:`hrca.delta_verifier`)."""

from __future__ import annotations

import unittest

from hrca import delta_verifier, rule_delta


class VerifyTests(unittest.TestCase):
    def test_quotation_baseline_and_changed(self):
        # Independent oracle for the 5% -> 10% delta.
        params = {"member_discount_rate": "0.10"}
        cases = delta_verifier.expected_cases(rule_delta.RESULT_KIND_QUOTATION, params)
        self.assertIsNone(delta_verifier.verify("quotation", params, cases))
        by_input = {tuple(sorted(c["input"].items())): c["output"] for c in cases}

        def case(subtotal, member, region):
            key = tuple(sorted([("subtotal", subtotal), ("member", member), ("region", region)]))
            return by_input[key]

        # Changed behaviour: member discount 5% -> 10% (discount 20, total 180).
        self.assertEqual(case("200.00", True, "west")["discount"], "20.00")
        self.assertEqual(case("200.00", True, "west")["total"], "180.00")
        # Preserved: non-member has no discount, shipping threshold holds.
        self.assertEqual(case("200.00", False, "west")["discount"], "0.00")
        self.assertEqual(case("200.00", False, "west")["shipping_fee"], "0.00")
        self.assertEqual(case("200.00", False, "west")["total"], "200.00")
        # Preserved: below threshold -> shipping 10, north regional 5.
        self.assertEqual(case("99.99", False, "north")["shipping_fee"], "10.00")
        self.assertEqual(case("99.99", False, "north")["regional_fee"], "5.00")
        self.assertEqual(case("99.99", False, "north")["total"], "114.99")
        # Changed discount + preserved regional east 8.
        self.assertEqual(case("50.00", True, "east")["discount"], "5.00")
        self.assertEqual(case("50.00", True, "east")["total"], "63.00")

    def test_late_fee_cap_changed_and_preserved(self):
        params = {"cap": "24"}
        cases = delta_verifier.expected_cases(rule_delta.RESULT_KIND_LATE_RETURN_FEE, params)
        self.assertIsNone(delta_verifier.verify("late_return_fee", params, cases))
        fees = {c["input"]["days_late"]: c["output"]["fee"] for c in cases}
        self.assertEqual(fees[1], "3.00")    # below cap (rate preserved)
        self.assertEqual(fees[8], "24.00")   # boundary (8 * 3 = 24)
        self.assertEqual(fees[11], "24.00")  # above cap (capped)
        self.assertEqual(fees[0], "0.00")

    def test_late_fee_baseline_default_cap(self):
        cases = delta_verifier.expected_cases(rule_delta.RESULT_KIND_LATE_RETURN_FEE, {})
        fees = {c["input"]["days_late"]: c["output"]["fee"] for c in cases}
        self.assertEqual(fees[11], "30.00")  # default cap 30

    def test_forged_output_rejected(self):
        params = {"member_discount_rate": "0.10"}
        cases = delta_verifier.expected_cases(rule_delta.RESULT_KIND_QUOTATION, params)
        forged = [dict(c) for c in cases]
        forged[0] = dict(forged[0])
        forged[0]["output"] = dict(forged[0]["output"])
        forged[0]["output"]["total"] = "0.01"
        self.assertEqual(
            delta_verifier.verify("quotation", params, forged),
            delta_verifier.REASON_EVIDENCE_MISMATCH,
        )

    def test_missing_case_rejected(self):
        params = {"member_discount_rate": "0.10"}
        cases = delta_verifier.expected_cases(rule_delta.RESULT_KIND_QUOTATION, params)[1:]
        self.assertEqual(
            delta_verifier.verify("quotation", params, cases),
            delta_verifier.REASON_EVIDENCE_MISSING,
        )

    def test_extra_case_rejected(self):
        params = {"cap": "24"}
        cases = list(delta_verifier.expected_cases(rule_delta.RESULT_KIND_LATE_RETURN_FEE, params))
        cases.append({"input": {"days_late": 99}, "output": {"fee": "24.00"}})
        self.assertEqual(
            delta_verifier.verify("late_return_fee", params, cases),
            delta_verifier.REASON_EVIDENCE_EXTRA,
        )

    def test_unknown_rule_rejected(self):
        self.assertEqual(
            delta_verifier.verify("other", {}, []), delta_verifier.REASON_UNKNOWN_RULE
        )

    def test_evidence_not_list_rejected(self):
        self.assertEqual(
            delta_verifier.verify("quotation", {}, "nope"),
            delta_verifier.REASON_EVIDENCE_NOT_LIST,
        )

    def test_malformed_entry_rejected(self):
        self.assertEqual(
            delta_verifier.verify("quotation", {}, ["bad"]),
            delta_verifier.REASON_EVIDENCE_MALFORMED,
        )


if __name__ == "__main__":
    unittest.main()
