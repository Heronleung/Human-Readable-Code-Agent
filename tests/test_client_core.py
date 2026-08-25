"""Tests for the Qt-free client supervision logic (P3.1)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from hrca import contract
from hrca.client_core import (
    LineBuffer,
    ResponseRouter,
    build_fixture_task,
    build_request,
    default_fixture_root,
    resolve_backend_command,
)


class LineBufferTests(unittest.TestCase):
    def test_accumulates_complete_lines(self):
        buf = LineBuffer(max_bytes=1000)
        self.assertEqual(buf.feed('{"a":1}\n{"b":'), ['{"a":1}'])
        self.assertEqual(buf.feed("2}\n"), ['{"b":2}'])
        self.assertEqual(buf.remaining(), "")

    def test_splits_multiple_lines_in_one_chunk(self):
        buf = LineBuffer(max_bytes=1000)
        self.assertEqual(buf.feed("a\nb\nc\n"), ["a", "b", "c"])

    def test_retains_partial_line(self):
        buf = LineBuffer(max_bytes=1000)
        buf.feed("partial")
        self.assertEqual(buf.remaining(), "partial")

    def test_oversized_complete_line_raises(self):
        buf = LineBuffer(max_bytes=8)
        with self.assertRaises(contract.ContractError) as ctx:
            buf.feed("x" * 9 + "\n")
        self.assertEqual(ctx.exception.code, "message_too_large")

    def test_oversized_partial_line_raises(self):
        buf = LineBuffer(max_bytes=8)
        with self.assertRaises(contract.ContractError) as ctx:
            buf.feed("x" * 9)
        self.assertEqual(ctx.exception.code, "message_too_large")

    def test_message_does_not_leak_input(self):
        buf = LineBuffer(max_bytes=8)
        with self.assertRaises(contract.ContractError) as ctx:
            buf.feed("secret-payload")
        self.assertNotIn("secret-payload", ctx.exception.message)


class ResponseRouterTests(unittest.TestCase):
    def test_match_only_inflight(self):
        router = ResponseRouter()
        router.track("cid-1")
        self.assertTrue(router.match("cid-1"))
        self.assertFalse(router.match("cid-2"))
        self.assertFalse(router.match(None))

    def test_resolve_removes_inflight(self):
        router = ResponseRouter()
        router.track("cid-1")
        router.resolve("cid-1")
        self.assertFalse(router.match("cid-1"))

    def test_stale_response_is_discarded(self):
        router = ResponseRouter()
        router.track("cid-1")
        router.resolve("cid-1")
        # A late response for an already-resolved id must not match.
        self.assertFalse(router.match("cid-1"))

    def test_abandon_all_returns_and_clears(self):
        router = ResponseRouter()
        router.track("cid-b")
        router.track("cid-a")
        self.assertEqual(router.abandon_all(), ["cid-a", "cid-b"])
        self.assertEqual(router.inflight(), [])


class FixtureTaskTests(unittest.TestCase):
    def test_task_is_read_only_and_unverified(self):
        task = build_fixture_task("fixtures")
        self.assertEqual(task["task_id"], "P3.1")
        self.assertEqual(task["repository_context"]["status"], "Unverified")
        self.assertTrue(
            set(task["allowed_actions"]) <= contract.READ_ONLY_TASK_ACTIONS
        )
        self.assertFalse(task["approval_required"])
        for action in task["allowed_actions"]:
            self.assertIn(action, contract.ALLOWED_ACTIONS)

    def test_build_request_shape(self):
        req = build_request("cid-1", "fixtures")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_SCAN)
        self.assertEqual(req["path"], os.path.abspath("fixtures"))
        self.assertEqual(req["task"]["task_id"], "P3.1")


class BackendCommandTests(unittest.TestCase):
    def test_source_resolution(self):
        self.assertEqual(
            resolve_backend_command(frozen=False),
            [sys.executable, "-m", "hrca.boundary", contract.SERVE_SENTINEL],
        )

    def test_frozen_resolution(self):
        self.assertEqual(
            resolve_backend_command(frozen=True),
            [sys.executable, contract.SERVE_SENTINEL],
        )


class DefaultFixtureRootTests(unittest.TestCase):
    def test_source_resolution_points_into_repository(self):
        root = default_fixture_root(frozen=False)
        self.assertTrue(os.path.isabs(root))
        self.assertTrue(os.path.isdir(root), root)
        self.assertTrue(root.endswith(os.path.join("Human-Readable-Code-Agent", "fixtures")))

    def test_source_resolution_is_cwd_independent(self):
        root_a = default_fixture_root(frozen=False)
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                root_b = default_fixture_root(frozen=False)
            finally:
                os.chdir(old)
        self.assertEqual(root_a, root_b)

    def test_frozen_resolution_uses_meipass(self):
        with mock.patch.object(sys, "_MEIPASS", "/fake/meipass", create=True):
            root = default_fixture_root(frozen=True)
        self.assertEqual(os.path.normpath(root), os.path.normpath("/fake/meipass/fixtures"))

    def test_frozen_resolution_is_not_cwd_relative(self):
        cwd = os.path.abspath(os.getcwd())
        with mock.patch.object(sys, "_MEIPASS", "/fake/meipass", create=True):
            root = default_fixture_root(frozen=True)
        self.assertFalse(root.startswith(cwd + os.sep))

    def test_default_corpus_directory_is_present_and_nonempty(self):
        root = default_fixture_root(frozen=False)
        self.assertTrue(os.path.isdir(root), root)
        self.assertGreater(len(os.listdir(root)), 0)

    def test_default_corpus_produces_nonempty_scanner_evidence(self):
        from hrca.scanner import scan_directory

        root = default_fixture_root(frozen=False)
        doc = scan_directory(root)
        self.assertGreater(len(doc["files"]), 0)
        self.assertGreater(len(doc["symbols"]), 0)


if __name__ == "__main__":
    unittest.main()
