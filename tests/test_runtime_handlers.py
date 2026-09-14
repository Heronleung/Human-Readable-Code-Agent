"""Acceptance tests for the trusted quotation-rules handler (P4.3).

The handler is the deterministic business logic the isolated runner executes;
these tests prove the exact expected results and rejections without any runner,
container or network.
"""

from __future__ import annotations

import unittest

from hrca import runtime_handlers


def _evaluate(form):
    result, error = runtime_handlers.quotation_rules_evaluate(form)
    return result, error


class AcceptanceExamplesTests(unittest.TestCase):
    def test_member_discount(self):
        result, error = _evaluate({"subtotal": "200.00", "member": True, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "10.00")
        self.assertEqual(result["shipping_fee"], "0.00")
        self.assertEqual(result["regional_fee"], "0.00")
        self.assertEqual(result["total"], "190.00")

    def test_discount_after_shipping_threshold(self):
        result, error = _evaluate({"subtotal": "50.00", "member": False, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "0.00")
        self.assertEqual(result["shipping_fee"], "10.00")
        self.assertEqual(result["total"], "60.00")

    def test_regional_fee(self):
        result, error = _evaluate({"subtotal": "100.00", "member": False, "region": "north"})
        self.assertIsNone(error)
        self.assertEqual(result["shipping_fee"], "0.00")
        self.assertEqual(result["regional_fee"], "5.00")
        self.assertEqual(result["total"], "105.00")

    def test_explicit_rounding(self):
        # 9.99 * 0.05 = 0.4995 -> half-up to 0.50; 9.99 - 0.50 = 9.49 < 100.
        result, error = _evaluate({"subtotal": "9.99", "member": True, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "0.50")
        self.assertEqual(result["shipping_fee"], "10.00")
        self.assertEqual(result["total"], "19.49")

    def test_accepts_numeric_subtotal(self):
        result, error = _evaluate({"subtotal": 200, "member": True, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "10.00")


class RejectionTests(unittest.TestCase):
    def test_negative_subtotal_rejected(self):
        result, error = _evaluate({"subtotal": "-1.00", "member": False, "region": "west"})
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_SUBTOTAL)

    def test_blank_required_field_rejected(self):
        result, error = _evaluate({"subtotal": "10.00", "region": "west"})
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_MISSING)

    def test_invalid_region_rejected(self):
        result, error = _evaluate({"subtotal": "10.00", "member": False, "region": "nope"})
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_REGION)

    def test_non_boolean_member_rejected(self):
        result, error = _evaluate({"subtotal": "10.00", "member": "yes", "region": "west"})
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_MEMBER)


class AltVariantAcceptanceTests(unittest.TestCase):
    """The P4.7 alternative benign variant must agree with the frozen verifier cases."""

    def _alt(self, form):
        return runtime_handlers.quotation_rules_alt_evaluate(form)

    def test_alt_member_discount(self):
        result, error = self._alt({"subtotal": "200.00", "member": True, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "6.00")
        self.assertEqual(result["shipping_fee"], "0.00")
        self.assertEqual(result["regional_fee"], "1.00")
        self.assertEqual(result["total"], "195.00")

    def test_alt_shipping_fee_below_threshold(self):
        result, error = self._alt({"subtotal": "50.00", "member": False, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["shipping_fee"], "12.00")
        self.assertEqual(result["regional_fee"], "1.00")
        self.assertEqual(result["total"], "63.00")

    def test_alt_free_shipping_at_threshold(self):
        result, error = self._alt({"subtotal": "160.00", "member": False, "region": "north"})
        self.assertIsNone(error)
        self.assertEqual(result["shipping_fee"], "0.00")
        self.assertEqual(result["regional_fee"], "6.00")
        self.assertEqual(result["total"], "166.00")

    def test_alt_rounding(self):
        result, error = self._alt({"subtotal": "9.99", "member": True, "region": "west"})
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "0.30")
        self.assertEqual(result["total"], "22.69")

    def test_alt_rejects_negative_subtotal(self):
        result, error = self._alt({"subtotal": "-1.00", "member": False, "region": "west"})
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_SUBTOTAL)


class DeltaParameterTests(unittest.TestCase):
    """The P4.7a parameterized quotation evaluator: 5% -> 10% via a delta parameter."""

    def test_baseline_member_discount(self):
        result, error = runtime_handlers.quotation_rules_evaluate(
            {"subtotal": "200.00", "member": True, "region": "west"}
        )
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "10.00")
        self.assertEqual(result["total"], "190.00")

    def test_delta_discount_10_percent(self):
        result, error = runtime_handlers.quotation_rules_evaluate(
            {"subtotal": "200.00", "member": True, "region": "west"},
            {"member_discount_rate": "0.10"},
        )
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "20.00")
        self.assertEqual(result["total"], "180.00")

    def test_non_member_discount_unchanged(self):
        result, error = runtime_handlers.quotation_rules_evaluate(
            {"subtotal": "200.00", "member": False, "region": "west"},
            {"member_discount_rate": "0.10"},
        )
        self.assertIsNone(error)
        self.assertEqual(result["discount"], "0.00")
        self.assertEqual(result["shipping_fee"], "0.00")
        self.assertEqual(result["total"], "200.00")

    def test_invalid_subtotal_still_rejected(self):
        result, error = runtime_handlers.quotation_rules_evaluate(
            {"subtotal": "-1.00", "member": False, "region": "west"},
            {"member_discount_rate": "0.10"},
        )
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_SUBTOTAL)


class LateReturnFeeTests(unittest.TestCase):
    """The P4.7a held-out late-return-fee evaluator (3/day, capped; cap is a parameter)."""

    def _fee(self, days, cap=None):
        params = {"cap": cap} if cap is not None else None
        return runtime_handlers.late_return_fee_evaluate({"days_late": days}, params)

    def test_baseline_below_cap(self):
        result, error = self._fee(1)
        self.assertIsNone(error)
        self.assertEqual(result["fee"], "3.00")

    def test_baseline_boundary(self):
        result, error = self._fee(10)
        self.assertIsNone(error)
        self.assertEqual(result["fee"], "30.00")  # 10 * 3 = 30 = default cap

    def test_baseline_above_cap(self):
        result, error = self._fee(11)
        self.assertIsNone(error)
        self.assertEqual(result["fee"], "30.00")

    def test_delta_cap_24(self):
        result, error = self._fee(11, cap="24")
        self.assertIsNone(error)
        self.assertEqual(result["fee"], "24.00")

    def test_delta_cap_24_boundary(self):
        result, error = self._fee(8, cap="24")
        self.assertIsNone(error)
        self.assertEqual(result["fee"], "24.00")  # 8 * 3 = 24 = new cap

    def test_delta_cap_24_below(self):
        result, error = self._fee(1, cap="24")
        self.assertIsNone(error)
        self.assertEqual(result["fee"], "3.00")  # daily rate preserved

    def test_negative_days_rejected(self):
        result, error = self._fee(-1)
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_DAYS)

    def test_non_integer_days_rejected(self):
        result, error = runtime_handlers.late_return_fee_evaluate({"days_late": "3"})
        self.assertIsNone(result)
        self.assertEqual(error, runtime_handlers.REASON_DAYS)


class RegistryTests(unittest.TestCase):
    def test_resolve_handler(self):
        self.assertIs(
            runtime_handlers.resolve_handler("quotation_rules.evaluate"),
            runtime_handlers.quotation_rules_evaluate,
        )

    def test_resolve_alt_handler(self):
        self.assertIs(
            runtime_handlers.resolve_handler("quotation_rules_alt.evaluate"),
            runtime_handlers.quotation_rules_alt_evaluate,
        )

    def test_resolve_late_fee_handler(self):
        self.assertIs(
            runtime_handlers.resolve_handler("late_return_fee.evaluate"),
            runtime_handlers.late_return_fee_evaluate,
        )

    def test_resolve_unknown_handler(self):
        self.assertIsNone(runtime_handlers.resolve_handler("other.handler"))


if __name__ == "__main__":
    unittest.main()
