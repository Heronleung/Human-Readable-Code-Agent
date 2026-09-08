"""Tests for the P4.3 execution broker (with a deterministic fake runner)."""

from __future__ import annotations

import unittest

from hrca import app_package, runner_broker


class FakeRunner:
    """A deterministic runner double that records its calls."""

    def __init__(self, preflight=None, run_result=None, run_error=None):
        self.preflight_result = preflight or {"available": True, "reason": None}
        self.run_result = run_result
        self.run_error = run_error
        self.preflight_calls = 0
        self.run_calls = 0

    def preflight(self):
        self.preflight_calls += 1
        return self.preflight_result

    def run(self, *, handler, input_payload):
        self.run_calls += 1
        if self.run_error is not None:
            return None, self.run_error
        return self.run_result, None


_VALID_INPUT = {"subtotal": "200.00", "member": True, "region": "west"}
_VALID_RESULT = {
    "discount": "10.00",
    "shipping_fee": "0.00",
    "regional_fee": "0.00",
    "total": "190.00",
}


class BrokerTests(unittest.TestCase):
    def _package(self):
        return app_package.quotation_reference_package()

    def test_ok(self):
        runner = FakeRunner(run_result=_VALID_RESULT)
        result = runner_broker.run_package(self._package(), _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_OK)
        self.assertEqual(result["result"]["total"], "190.00")
        self.assertEqual(runner.preflight_calls, 1)
        self.assertEqual(runner.run_calls, 1)

    def test_package_invalid_never_reaches_runner(self):
        runner = FakeRunner(run_result=_VALID_RESULT)
        result = runner_broker.run_package({"schema_version": "9.9.9"}, _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_PACKAGE_INVALID)
        self.assertEqual(runner.preflight_calls, 0)
        self.assertEqual(runner.run_calls, 0)

    def test_input_invalid_never_reaches_runner(self):
        runner = FakeRunner(run_result=_VALID_RESULT)
        bad = dict(_VALID_INPUT)
        bad["subtotal"] = "-1.00"
        result = runner_broker.run_package(self._package(), bad, runner)
        self.assertEqual(result["state"], app_package.STATE_INPUT_INVALID)
        self.assertEqual(runner.preflight_calls, 0)
        self.assertEqual(runner.run_calls, 0)

    def test_runtime_unavailable(self):
        runner = FakeRunner(
            preflight={"available": False, "reason": "runtime_unavailable"}
        )
        result = runner_broker.run_package(self._package(), _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_RUNTIME_UNAVAILABLE)
        self.assertEqual(runner.run_calls, 0)

    def test_runtime_blocked(self):
        runner = FakeRunner(
            preflight={"available": False, "reason": "runtime_blocked"}
        )
        result = runner_broker.run_package(self._package(), _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_RUNTIME_BLOCKED)
        self.assertEqual(runner.run_calls, 0)

    def test_timeout(self):
        runner = FakeRunner(run_error=app_package.STATE_TIMEOUT)
        result = runner_broker.run_package(self._package(), _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_TIMEOUT)
        self.assertIsNone(result["result"])

    def test_output_invalid(self):
        runner = FakeRunner(run_result={"total": "190.00"})  # missing fields
        result = runner_broker.run_package(self._package(), _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_OUTPUT_INVALID)
        self.assertIsNone(result["result"])

    def test_runner_failed(self):
        runner = FakeRunner(run_error=app_package.STATE_RUNNER_FAILED)
        result = runner_broker.run_package(self._package(), _VALID_INPUT, runner)
        self.assertEqual(result["state"], app_package.STATE_RUNNER_FAILED)


if __name__ == "__main__":
    unittest.main()
