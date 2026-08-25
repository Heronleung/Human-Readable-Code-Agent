"""Tests for the headless local application boundary (P3.1)."""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

from hrca import boundary, contract
from hrca.client_core import build_fixture_task
from hrca.contract import dumps, loads

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures"))
_NONASCII = os.path.join(FIXTURES, "nonascii", "traditional_chinese.txt")


def _run(*raw_lines):
    """Feed raw request lines to the boundary loop; return responses + stderr."""
    stdin = io.StringIO("\n".join(raw_lines) + ("\n" if raw_lines else ""))
    stdout = io.StringIO()
    stderr = io.StringIO()
    rc = boundary.run_loop(stdin, stdout, stderr)
    return rc, stdout.getvalue().splitlines(), stderr.getvalue()


def _request(**overrides):
    req = contract.build_request(
        "cid-1", "scan", FIXTURES, build_fixture_task(FIXTURES)
    )
    req.update(overrides)
    return req


def _first_response(*raw_lines):
    _, responses, _ = _run(*raw_lines)
    return loads(responses[0])


class BoundarySuccessTests(unittest.TestCase):
    def test_valid_request_round_trips(self):
        req = _request()
        _, responses, stderr = _run(dumps(req))
        self.assertEqual(len(responses), 1)
        self.assertEqual(stderr, "")
        env = loads(responses[0])
        self.assertTrue(env["ok"])
        self.assertEqual(env["correlation_id"], "cid-1")
        result = env["result"]
        self.assertEqual(result["task_id"], "P3.1")
        self.assertEqual(result["title"], "Scan and analyze the fixture corpus")
        self.assertIn("report", result)
        self.assertIn("evidence", result)
        self.assertEqual(result["report"]["outcome"]["status"], "no_change")
        self.assertEqual(result["report"]["outcome"]["changed_files"], [])
        self.assertEqual(result["evidence"]["files"][0]["path"], "app/dynamic.py")

    def test_empty_lines_are_skipped(self):
        req = _request()
        _, responses, _ = _run("", dumps(req), "")
        self.assertEqual(len(responses), 1)

    def test_one_response_per_request(self):
        req = _request()
        _, responses, _ = _run(dumps(req), dumps(req))
        self.assertEqual(len(responses), 2)
        for line in responses:
            self.assertTrue(loads(line)["ok"])

    def test_deterministic_result(self):
        req = _request()
        _, first, _ = _run(dumps(req))
        _, second, _ = _run(dumps(req))
        self.assertEqual(first, second)


class BoundaryRejectionTests(unittest.TestCase):
    def test_malformed_json_rejected(self):
        env = _first_response("{not json")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "malformed_request")

    def test_non_object_json_rejected(self):
        env = _first_response("[1, 2, 3]")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_unknown_contract_version_rejected(self):
        req = _request(contract_version="0.0.0")
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "unknown_contract_version")

    def test_unknown_action_rejected(self):
        for action in ("write", "git", "commit", "command", "network", "provider",
                       "remote", "push", "delete"):
            with self.subTest(action=action):
                env = _first_response(dumps(_request(action=action)))
                self.assertFalse(env["ok"])
                self.assertEqual(env["error"]["code"], "action_not_allowed")

    def test_write_action_in_task_rejected(self):
        task = build_fixture_task(FIXTURES)
        task["allowed_actions"] = ["read", "edit"]
        env = _first_response(dumps(_request(task=task)))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "action_not_allowed")

    def test_missing_path_rejected(self):
        req = _request()
        del req["path"]
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_missing_task_rejected(self):
        req = _request()
        del req["task"]
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_invalid_task_rejected(self):
        req = _request(task={"task_id": "t"})
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_oversized_message_rejected(self):
        big = "x" * (contract.MAX_MESSAGE_BYTES + 1)
        env = _first_response(big)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "message_too_large")


class BoundarySanitizationTests(unittest.TestCase):
    def test_error_does_not_echo_caller_text(self):
        secret = "secret-token-abc123"
        req = _request(action="write", task={"task_id": secret})
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        serialized = dumps(env)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("write", serialized)
        self.assertEqual(env["error"]["message"], contract.error_message("action_not_allowed"))

    def test_internal_error_is_bounded(self):
        req = _request()
        with mock.patch(
            "hrca.boundary.scan_directory",
            side_effect=RuntimeError("boom-secret-detail"),
        ):
            env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "internal_error")
        self.assertNotIn("boom-secret-detail", dumps(env))
        self.assertNotIn("boom-secret-detail", env["error"]["message"])


class BoundaryNonAsciiTests(unittest.TestCase):
    def test_non_ascii_fixture_round_trips_losslessly(self):
        with open(_NONASCII, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("繁體", text)

        task = build_fixture_task(FIXTURES)
        task["title"] = text.strip()
        task["request"] = text.strip()
        req = contract.build_request("cid-繁體-1", "scan", FIXTURES, task)

        _, responses, _ = _run(dumps(req))
        env = loads(responses[0])
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["title"], text.strip())


class BoundaryStdioDisciplineTests(unittest.TestCase):
    def test_stdout_is_exactly_one_json_line_per_request(self):
        req = _request()
        _, responses, stderr = _run(dumps(req), dumps(req))
        self.assertEqual(len(responses), 2)
        for line in responses:
            env = loads(line)
            self.assertIn("ok", env)
        self.assertEqual(stderr, "")

    def test_handle_request_never_raises(self):
        cases = [
            [1, 2, 3],                          # non-mapping
            {"action": "write"},                # non-allowlisted action
            _request(),                         # valid read-only request
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:20]):
                response = boundary.handle_request(payload)
                self.assertIsInstance(response, dict)
                self.assertIn("ok", response)

    def test_success_result_is_json_serializable(self):
        req = _request()
        env = boundary.handle_request(req)
        self.assertTrue(env["ok"])
        # Round-trippable and deterministic.
        self.assertEqual(loads(dumps(env)), env)


if __name__ == "__main__":
    unittest.main()
