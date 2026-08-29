"""Tests for the Qt-free application-boundary contract (P3.2)."""

from __future__ import annotations

import json
import unittest

from hrca import contract
from hrca.contract import (
    ACTION_GET_ANCHOR,
    ACTION_GET_DOCUMENT,
    ACTION_GET_TREE,
    ACTION_GET_TWIN,
    ACTION_OPEN_PROJECT,
    ACTION_SCAN,
    ACTION_SYNC_TWIN,
    ALLOWED_ACTIONS,
    CONTRACT_VERSION,
    CORRELATION_ID_MAX_CHARS,
    MAX_DOCUMENT_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    READ_ONLY_TASK_ACTIONS,
    SCAN_ACTIONS,
    SERVE_SENTINEL,
    TWIN_ACTIONS,
    WORKSPACE_ACTIONS,
    ContractError,
    build_error,
    build_request,
    build_success,
    dumps,
    error_message,
    loads,
    new_correlation_id,
)

# Actions that must never be permitted by the read-only boundary.
_FORBIDDEN_ACTIONS = ("write", "git", "commit", "command", "exec", "network",
                      "provider", "remote", "push", "delete")


class ContractVersionTests(unittest.TestCase):
    def test_contract_version_is_pinned(self):
        self.assertEqual(CONTRACT_VERSION, "3.3.0")

    def test_serve_sentinel(self):
        self.assertEqual(SERVE_SENTINEL, "--serve")

    def test_size_limits_are_positive(self):
        self.assertGreater(MAX_MESSAGE_BYTES, 0)
        self.assertGreater(CORRELATION_ID_MAX_CHARS, 0)

    def test_workspace_limits_are_positive(self):
        self.assertGreater(MAX_TREE_ENTRIES, 0)
        self.assertGreater(MAX_TREE_DEPTH, 0)
        self.assertGreater(MAX_DOCUMENT_BYTES, 0)
        # A document is bounded to fit on the wire even when every byte escapes.
        self.assertLessEqual(MAX_DOCUMENT_BYTES, MAX_MESSAGE_BYTES)


class AllowedActionTests(unittest.TestCase):
    def test_allowed_actions_are_read_only(self):
        self.assertEqual(
            ALLOWED_ACTIONS,
            SCAN_ACTIONS | WORKSPACE_ACTIONS | TWIN_ACTIONS,
        )

    def test_workspace_actions_are_allowlisted(self):
        self.assertEqual(
            WORKSPACE_ACTIONS,
            frozenset({ACTION_OPEN_PROJECT, ACTION_GET_TREE, ACTION_GET_DOCUMENT}),
        )

    def test_twin_actions_are_allowlisted(self):
        self.assertEqual(
            TWIN_ACTIONS,
            frozenset({ACTION_SYNC_TWIN, ACTION_GET_TWIN, ACTION_GET_ANCHOR}),
        )

    def test_twin_actions_are_read_only(self):
        for action in TWIN_ACTIONS:
            self.assertIn(action, ALLOWED_ACTIONS)
            self.assertNotIn(action, WORKSPACE_ACTIONS)
            self.assertNotIn(action, SCAN_ACTIONS)

    def test_no_forbidden_action_is_allowed(self):
        for action in _FORBIDDEN_ACTIONS:
            with self.subTest(action=action):
                self.assertNotIn(action, ALLOWED_ACTIONS)

    def test_task_actions_exclude_mutators(self):
        self.assertTrue(READ_ONLY_TASK_ACTIONS <= ALLOWED_ACTIONS)
        for action in ("edit", "commit", "remote"):
            self.assertNotIn(action, READ_ONLY_TASK_ACTIONS)

    def test_scan_action_is_available(self):
        self.assertIn(ACTION_SCAN, ALLOWED_ACTIONS)


class SerializationTests(unittest.TestCase):
    def test_dumps_is_ascii_on_the_wire(self):
        text = dumps({"label": "繁體中文"})
        self.assertTrue(all(ord(ch) < 128 for ch in text))
        self.assertIn("\\u7e41", text)

    def test_non_ascii_round_trips_losslessly(self):
        original = "繁體中文測試"
        self.assertEqual(loads(dumps({"label": original}))["label"], original)

    def test_dumps_is_single_line_and_deterministic(self):
        obj = {"b": 2, "a": 1}
        first = dumps(obj)
        self.assertNotIn("\n", first)
        self.assertEqual(first, dumps(obj))

    def test_loads_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            loads("not json")


class EnvelopeTests(unittest.TestCase):
    def test_build_request_shape(self):
        task = {"task_id": "t"}
        req = build_request("cid-1", "scan", "fixtures", task)
        self.assertEqual(req["contract_version"], CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], "scan")
        self.assertEqual(req["path"], "fixtures")
        self.assertEqual(req["task"], task)

    def test_build_success_shape(self):
        resp = build_success("cid-1", {"task_id": "t"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["correlation_id"], "cid-1")
        self.assertEqual(resp["result"], {"task_id": "t"})

    def test_build_error_shape(self):
        resp = build_error("cid-1", "unknown_contract_version")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["correlation_id"], "cid-1")
        self.assertEqual(
            resp["error"],
            {"code": "unknown_contract_version",
             "message": error_message("unknown_contract_version")},
        )


class ContractErrorTests(unittest.TestCase):
    def test_message_is_fixed_catalogue_value(self):
        for code in contract.ERROR_CODES:
            with self.subTest(code=code):
                self.assertEqual(ContractError(code).message, error_message(code))

    def test_rejects_unknown_code(self):
        with self.assertRaises(ValueError):
            ContractError("not-a-real-code")

    def test_rejects_arbitrary_text(self):
        with self.assertRaises(ValueError):
            ContractError("secret-token-abc123")

    def test_to_dict_is_bounded(self):
        err = ContractError("action_not_allowed")
        self.assertEqual(
            err.to_dict(),
            {"code": "action_not_allowed",
             "message": error_message("action_not_allowed")},
        )
        self.assertNotIn("secret", json.dumps(err.to_dict()))


class CorrelationIdTests(unittest.TestCase):
    def test_new_correlation_id_is_ascii_safe(self):
        cid = new_correlation_id()
        self.assertTrue(cid.isalnum())
        self.assertTrue(all(ord(ch) < 128 for ch in cid))
        self.assertLessEqual(len(cid), CORRELATION_ID_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
