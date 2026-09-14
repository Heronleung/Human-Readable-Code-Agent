"""Tests for the P4.3 app-package boundary actions (get_package / run_package)."""

from __future__ import annotations

import unittest

from hrca import app_package, boundary, contract


def _req(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-pkg",
        "action": action,
    }
    req.update(overrides)
    return req


class FakeRunner:
    def __init__(self, preflight=None, run_result=None, run_error=None):
        self.preflight_result = preflight or {"available": True, "reason": None}
        self.run_result = run_result
        self.run_error = run_error
        self.run_calls = 0

    def preflight(self):
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


class BoundaryPackageTests(unittest.TestCase):
    def setUp(self):
        self.session = boundary.WorkspaceSession()

    def _do(self, action, **overrides):
        return boundary.handle_request(_req(action, **overrides), self.session)

    def test_get_package_returns_valid_reference(self):
        env = self._do(contract.ACTION_GET_PACKAGE)
        self.assertTrue(env["ok"])
        package = env["result"]["package"]
        self.assertIsNone(app_package.validate_package(package))
        self.assertEqual(package["package_id"], "quotation-rules")

    def test_get_package_unknown_id(self):
        env = self._do(contract.ACTION_GET_PACKAGE, task={"package_id": "nope"})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "package_not_found")

    def test_run_package_requires_task(self):
        env = self._do(contract.ACTION_RUN_PACKAGE)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_run_package_unknown_id(self):
        env = self._do(
            contract.ACTION_RUN_PACKAGE,
            task={"package_id": "nope", "input": _VALID_INPUT},
        )
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "package_not_found")

    def test_run_package_input_invalid(self):
        runner = FakeRunner(run_result=_VALID_RESULT)
        self.session.runner = runner
        bad = dict(_VALID_INPUT)
        bad["subtotal"] = "-1.00"
        env = self._do(
            contract.ACTION_RUN_PACKAGE,
            task={"package_id": "quotation-rules", "input": bad},
        )
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], app_package.STATE_INPUT_INVALID)
        self.assertEqual(runner.run_calls, 0)

    def test_run_package_ok(self):
        runner = FakeRunner(run_result=_VALID_RESULT)
        self.session.runner = runner
        env = self._do(
            contract.ACTION_RUN_PACKAGE,
            task={"package_id": "quotation-rules", "input": _VALID_INPUT},
        )
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], app_package.STATE_OK)
        self.assertEqual(env["result"]["result"]["total"], "190.00")
        self.assertEqual(runner.run_calls, 1)

    def test_run_package_runtime_unavailable(self):
        runner = FakeRunner(
            preflight={"available": False, "reason": "runtime_unavailable"}
        )
        self.session.runner = runner
        env = self._do(
            contract.ACTION_RUN_PACKAGE,
            task={"package_id": "quotation-rules", "input": _VALID_INPUT},
        )
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], app_package.STATE_RUNTIME_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
