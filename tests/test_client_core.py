"""Tests for the Qt-free client supervision logic (P3.1)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from hrca import contract
from hrca.client_core import (
    ARTIFACT_FIELDS,
    BEHAVIOR_FIELDS,
    DRAFT_LIST,
    DRAFT_SINGLE,
    FIELD_CARDINALITY,
    FIELD_LABELS,
    PROVIDER_UNAVAILABLE,
    REPOSITORY_UNVERIFIED,
    TWIN_AVAILABLE,
    TWIN_CONFLICT,
    TWIN_EMPTY,
    TWIN_LOADING,
    TWIN_STALE,
    TWIN_UNSUPPORTED,
    TWIN_STATES,
    VALIDATION_FAILED,
    VALIDATION_IDLE,
    VALIDATION_OK,
    VALIDATION_RUNNING,
    LineBuffer,
    ResponseRouter,
    behavior_node_label,
    build_compare_draft_request,
    build_discard_draft_request,
    build_fixture_task,
    build_generate_intent_delta_request,
    build_get_anchor_request,
    build_get_code_map_request,
    build_get_document_request,
    build_get_draft_request,
    build_get_tree_request,
    build_get_twin_request,
    build_open_project_request,
    build_request,
    build_reset_draft_request,
    build_save_draft_request,
    build_scan_request,
    build_scan_task,
    build_sync_twin_request,
    default_fixture_root,
    draft_edit,
    field_cardinality,
    field_label,
    format_draft_changes,
    format_draft_facts,
    format_intent_delta,
    format_twin_projection,
    format_twin_sync,
    is_twin_source_path,
    resolve_backend_command,
    twin_state_from_sync,
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


class WorkspaceRequestBuilderTests(unittest.TestCase):
    def test_open_project_request_shape(self):
        req = build_open_project_request("cid-1", "some/root")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_OPEN_PROJECT)
        self.assertEqual(req["path"], os.path.abspath("some/root"))

    def test_get_tree_request_has_no_path(self):
        req = build_get_tree_request("cid-1")
        self.assertEqual(req["action"], contract.ACTION_GET_TREE)
        self.assertNotIn("path", req)

    def test_get_document_request_shape(self):
        req = build_get_document_request("cid-1", "app/main.py")
        self.assertEqual(req["action"], contract.ACTION_GET_DOCUMENT)
        self.assertEqual(req["path"], "app/main.py")

    def test_scan_request_uses_generic_task(self):
        req = build_scan_request("cid-1", "some/root")
        self.assertEqual(req["action"], contract.ACTION_SCAN)
        self.assertEqual(req["task"]["task_id"], "P3.2")
        self.assertEqual(
            req["task"]["repository_context"]["status"], REPOSITORY_UNVERIFIED
        )

    def test_scan_task_is_read_only(self):
        task = build_scan_task("some/root")
        self.assertTrue(
            set(task["allowed_actions"]) <= contract.READ_ONLY_TASK_ACTIONS
        )


class TwinRequestBuilderTests(unittest.TestCase):
    def test_sync_twin_request_full_sync_has_empty_task(self):
        req = build_sync_twin_request("cid-1")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_SYNC_TWIN)
        self.assertEqual(req["task"], {})
        self.assertNotIn("path", req)

    def test_sync_twin_request_scoped_to_changed_paths(self):
        req = build_sync_twin_request("cid-1", ["app/main.py", "app/service.py"])
        self.assertEqual(req["task"]["changed_paths"], ["app/main.py", "app/service.py"])

    def test_sync_twin_request_copies_changed_paths(self):
        source = ["app/main.py"]
        req = build_sync_twin_request("cid-1", source)
        source.append("app/extra.py")
        self.assertEqual(req["task"]["changed_paths"], ["app/main.py"])

    def test_get_twin_request_shape(self):
        req = build_get_twin_request("cid-1", "app.service.Service.handle")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_GET_TWIN)
        self.assertEqual(req["task"], {"selector": "app.service.Service.handle"})
        self.assertNotIn("path", req)

    def test_get_anchor_request_shape(self):
        req = build_get_anchor_request("cid-1", "behavior:abc123")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_GET_ANCHOR)
        self.assertEqual(req["task"], {"node_id": "behavior:abc123"})
        self.assertNotIn("path", req)


class TwinPresentationTests(unittest.TestCase):
    def test_sync_state_maps_to_bounded_presentation_state(self):
        for sync_state, present in (
            ("synchronized", TWIN_AVAILABLE),
            ("no_change", TWIN_AVAILABLE),
            ("needs_review", TWIN_STALE),
            ("stale", TWIN_STALE),
            ("blocked", TWIN_STALE),
            ("conflict", TWIN_CONFLICT),
            ("unsupported", TWIN_UNSUPPORTED),
        ):
            with self.subTest(sync_state=sync_state):
                self.assertEqual(twin_state_from_sync(sync_state), present)

    def test_unknown_sync_state_is_bounded(self):
        self.assertEqual(twin_state_from_sync("not-a-sync-state"), TWIN_AVAILABLE)

    def test_is_twin_source_path_accepts_python_and_stub(self):
        self.assertTrue(is_twin_source_path("app/main.py"))
        self.assertTrue(is_twin_source_path("app/stubs.pyi"))
        self.assertFalse(is_twin_source_path("app/notes.txt"))
        self.assertFalse(is_twin_source_path("app/data.json"))
        self.assertFalse(is_twin_source_path("app/module"))
        self.assertFalse(is_twin_source_path(None))
        self.assertFalse(is_twin_source_path(""))

    def test_behavior_node_label_lists_items(self):
        self.assertEqual(
            behavior_node_label(
                {"category": "calls", "provenance": "verified",
                 "items": ["open", "<unresolved>"]}
            ),
            "calls: open, <unresolved>",
        )

    def test_behavior_node_label_marks_unresolved(self):
        self.assertEqual(
            behavior_node_label(
                {"category": "conditions", "provenance": "unresolved", "items": []}
            ),
            "conditions (unresolved)",
        )

    def test_behavior_node_label_without_items_or_reason(self):
        self.assertEqual(
            behavior_node_label({"category": "loops", "items": []}), "loops"
        )

    def test_format_twin_projection_shows_fields_as_text(self):
        bundle = {
            "projection": {
                "kind": "method",
                "path": "app/service.py",
                "locator": "app.service.Service.handle",
                "summary": "Method handle(request)",
                "provenance": "verified",
                "confidence": "high",
                "sync_state": "synchronized",
                "details": ["Parameters: request"],
                "limitations": ["a dynamic dependency is marked low confidence"],
            },
            "behavior_nodes": [],
        }
        text = format_twin_projection(bundle)
        self.assertIn("Method handle(request)", text)
        self.assertIn("Kind: method", text)
        self.assertIn("Path: app/service.py", text)
        self.assertIn("Provenance: verified", text)
        self.assertIn("Confidence: high", text)
        self.assertIn("Sync state: synchronized", text)
        self.assertIn("Limitations:", text)

    def test_format_twin_projection_is_deterministic(self):
        bundle = {"projection": {"kind": "file", "summary": "Python module app"}}
        self.assertEqual(format_twin_projection(bundle), format_twin_projection(bundle))

    def test_format_twin_sync_shows_state_and_counts(self):
        result = {
            "state": "synchronized",
            "counts": {
                "artifacts": 3,
                "behavior_nodes": 2,
                "correspondences": 5,
                "projections": 4,
            },
        }
        text = format_twin_sync(result)
        self.assertIn("Twin state: synchronized", text)
        self.assertIn("artifacts: 3", text)
        self.assertIn("behavior nodes: 2", text)

    def test_format_twin_sync_includes_reason_when_present(self):
        result = {"state": "stale", "counts": {}, "reason": "parse error"}
        self.assertIn("Reason: parse error", format_twin_sync(result))


class DraftRequestBuilderTests(unittest.TestCase):
    def test_get_code_map_request_shape(self):
        req = build_get_code_map_request("cid-1")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_GET_CODE_MAP)
        self.assertNotIn("path", req)

    def test_save_draft_request_copies_edits(self):
        source = [draft_edit("artifact:a", "purpose", "A service module")]
        req = build_save_draft_request("cid-1", source)
        self.assertEqual(req["action"], contract.ACTION_SAVE_DRAFT)
        self.assertEqual(
            req["task"]["edits"],
            [{"target_id": "artifact:a", "field": "purpose", "proposed": "A service module"}],
        )
        source.append(draft_edit("artifact:a", "invariants", ["idempotent"]))
        self.assertEqual(len(req["task"]["edits"]), 1)

    def test_get_draft_request_shape(self):
        req = build_get_draft_request("cid-1")
        self.assertEqual(req["action"], contract.ACTION_GET_DRAFT)
        self.assertNotIn("path", req)

    def test_discard_draft_request_shape(self):
        req = build_discard_draft_request("cid-1")
        self.assertEqual(req["action"], contract.ACTION_DISCARD_DRAFT)

    def test_reset_draft_request_shape(self):
        req = build_reset_draft_request("cid-1")
        self.assertEqual(req["action"], contract.ACTION_RESET_DRAFT)

    def test_compare_draft_request_shape(self):
        req = build_compare_draft_request("cid-1")
        self.assertEqual(req["action"], contract.ACTION_COMPARE_DRAFT)

    def test_generate_intent_delta_request_shape(self):
        req = build_generate_intent_delta_request("cid-1")
        self.assertEqual(req["action"], contract.ACTION_GENERATE_INTENT_DELTA)


class DraftVocabularyTests(unittest.TestCase):
    def test_field_cardinality_matches_schema(self):
        self.assertEqual(field_cardinality("purpose"), DRAFT_SINGLE)
        self.assertEqual(field_cardinality("workflow_steps"), DRAFT_LIST)
        self.assertIsNone(field_cardinality("not_a_field"))

    def test_field_label_falls_back_to_key(self):
        self.assertEqual(field_label("purpose"), "Purpose")
        self.assertEqual(field_label("unknown_field"), "unknown_field")

    def test_artifact_and_behavior_fields_are_disjoint_scopes(self):
        self.assertEqual(
            set(ARTIFACT_FIELDS), {"purpose", "dependencies", "invariants", "limitations"}
        )
        self.assertLess(set(ARTIFACT_FIELDS), set(BEHAVIOR_FIELDS))
        self.assertIn("workflow_steps", BEHAVIOR_FIELDS)
        self.assertNotIn("workflow_steps", ARTIFACT_FIELDS)

    def test_field_labels_cover_editable_fields(self):
        self.assertEqual(set(FIELD_LABELS), set(FIELD_CARDINALITY))

    def test_draft_edit_shape(self):
        self.assertEqual(
            draft_edit("behavior:b1", "conditions", ["x > 0"]),
            {"target_id": "behavior:b1", "field": "conditions", "proposed": ["x > 0"]},
        )


class DraftPresentationTests(unittest.TestCase):
    def test_format_draft_facts_lists_read_only_facts(self):
        projection = {
            "path": "app/service.py",
            "locator": "app.service.Service.handle",
            "kind": "method",
            "provenance": "verified",
            "confidence": "high",
            "sync_state": "synchronized",
        }
        text = format_draft_facts(projection)
        self.assertIn("Path: app/service.py", text)
        self.assertIn("Locator: app.service.Service.handle", text)
        self.assertIn("Kind: method", text)
        self.assertIn("Provenance: verified", text)
        self.assertIn("Confidence: high", text)
        self.assertIn("Sync state: synchronized", text)

    def test_format_draft_changes_empty(self):
        self.assertEqual(format_draft_changes([]), "No changes.")

    def test_format_draft_changes_single_and_list(self):
        changes = [
            {"field": "purpose", "proposed": "A service module"},
            {"field": "workflow_steps", "proposed": ["load", "parse", "emit"]},
            {"field": "invariants", "proposed": []},
        ]
        text = format_draft_changes(changes)
        self.assertIn("Purpose: A service module", text)
        self.assertIn("Workflow steps:", text)
        self.assertIn("  - load", text)
        self.assertIn("Explicit invariants:", text)
        self.assertIn("  - (cleared)", text)

    def test_format_intent_delta_marks_non_executable(self):
        delta = {
            "intent": "user_authored",
            "targets": [{"target_id": "artifact:a"}],
            "affected_sources": ["artifact:a"],
            "affected_behavior_nodes": [],
            "acceptance_criteria": ["artifact:a: purpose authored"],
            "constraints": [],
        }
        text = format_intent_delta(delta)
        self.assertIn("Intent Delta (not executable)", text)
        self.assertIn("Executable: false", text)
        self.assertIn("Targets: 1", text)
        self.assertIn("Affected sources: artifact:a", text)


class ClientStateConstantsTests(unittest.TestCase):
    def test_twin_states_are_bounded(self):
        self.assertEqual(
            TWIN_STATES,
            {
                TWIN_EMPTY,
                TWIN_LOADING,
                TWIN_AVAILABLE,
                TWIN_STALE,
                TWIN_CONFLICT,
                TWIN_UNSUPPORTED,
            },
        )
        for state in TWIN_STATES:
            self.assertIsInstance(state, str)

    def test_validation_states(self):
        self.assertIn(VALIDATION_IDLE, {VALIDATION_IDLE, VALIDATION_RUNNING, VALIDATION_OK, VALIDATION_FAILED})

    def test_provider_and_repository_defaults(self):
        self.assertEqual(PROVIDER_UNAVAILABLE, "unavailable")
        self.assertEqual(REPOSITORY_UNVERIFIED, "Unverified")


if __name__ == "__main__":
    unittest.main()
