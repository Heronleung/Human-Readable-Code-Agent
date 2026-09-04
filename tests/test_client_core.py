"""Tests for the Qt-free client supervision logic (P3.1)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from hrca import contract
from hrca.client_core import (
    BLOCK_TYPE_LABELS,
    CREDENTIAL_ACTION_MESSAGES,
    INTENT_CLASS_LABELS,
    OPERATION_LABELS,
    PROPOSAL_STATE_LABELS,
    PROVIDER_READINESS_STATE_LABELS,
    PROVIDER_STATUS_MESSAGES,
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
    block_type_label,
    credential_action_message,
    build_compare_draft_request,
    build_discard_draft_request,
    build_fixture_task,
    build_generate_intent_delta_request,
    build_get_anchor_request,
    build_get_code_map_request,
    build_get_document_request,
    build_get_draft_request,
    build_get_readiness_request,
    build_get_tree_request,
    build_get_twin_request,
    build_manage_credential_request,
    build_open_project_request,
    build_plan_proposal_request,
    build_remove_credential_request,
    build_request,
    build_reset_draft_request,
    build_save_draft_request,
    build_scan_request,
    build_scan_task,
    build_sync_twin_request,
    default_fixture_root,
    format_draft_operations,
    format_entity_list,
    format_intent_delta,
    format_procedural_document,
    format_proposal,
    format_provider_readiness,
    format_twin_projection,
    format_twin_sync,
    intent_class_label,
    is_twin_source_path,
    operation_label,
    proposal_state_label,
    provider_readiness_state_label,
    provider_status_message,
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

    def test_save_draft_request_copies_operations(self):
        source = [
            {
                "op": "replace_description",
                "target_block_id": "codemap:app.service:purpose:1",
                "proposed_text": "A service module",
            }
        ]
        req = build_save_draft_request("cid-1", source)
        self.assertEqual(req["action"], contract.ACTION_SAVE_DRAFT)
        self.assertEqual(
            req["task"]["operations"],
            [
                {
                    "op": "replace_description",
                    "target_block_id": "codemap:app.service:purpose:1",
                    "proposed_text": "A service module",
                }
            ],
        )
        source.append(
            {
                "op": "insert_block",
                "owning_entity_id": "app.service",
                "block_type": "note",
                "proposed_text": "a note",
            }
        )
        self.assertEqual(len(req["task"]["operations"]), 1)

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


class ProposalRequestBuilderTests(unittest.TestCase):
    def test_plan_proposal_request_shape(self):
        req = build_plan_proposal_request("cid-1")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_PLAN_PROPOSAL)
        self.assertNotIn("path", req)
        self.assertNotIn("task", req)


class CodeMapVocabularyTests(unittest.TestCase):
    def test_block_type_label_falls_back_to_token(self):
        self.assertEqual(block_type_label("entity"), "Entity")
        self.assertEqual(block_type_label("limitation"), "Limitation")
        self.assertEqual(block_type_label("unknown_type"), "unknown_type")

    def test_operation_label_falls_back_to_token(self):
        self.assertEqual(operation_label("replace_description"), "Replace description")
        self.assertEqual(operation_label("insert_block"), "Insert block")
        self.assertEqual(operation_label("not_an_op"), "not_an_op")

    def test_intent_class_label_falls_back_to_token(self):
        self.assertEqual(intent_class_label("documentation_intent"), "Documentation")
        self.assertEqual(intent_class_label("behavior_change_intent"), "Behavior change")
        self.assertEqual(intent_class_label("unknown"), "unknown")

    def test_block_type_labels_cover_all_14_types(self):
        self.assertEqual(
            set(BLOCK_TYPE_LABELS),
            {
                "entity",
                "purpose",
                "input",
                "step",
                "decision",
                "loop",
                "call",
                "exception",
                "return",
                "side_effect",
                "dependency",
                "invariant",
                "limitation",
                "note",
            },
        )

    def test_operation_labels_cover_all_typed_ops(self):
        self.assertEqual(
            set(OPERATION_LABELS),
            {
                "replace_description",
                "insert_block",
                "delete_draft_block",
                "move_draft_block",
                "replace_condition_intent",
                "mark_unresolved",
                "restore_block",
            },
        )

    def test_intent_class_labels_cover_both_intents(self):
        self.assertEqual(
            set(INTENT_CLASS_LABELS),
            {"documentation_intent", "behavior_change_intent"},
        )


class DraftPresentationTests(unittest.TestCase):
    def test_format_procedural_document_passes_through(self):
        self.assertEqual(format_procedural_document("Module app.service"), "Module app.service")
        self.assertEqual(format_procedural_document(None), "")
        self.assertEqual(format_procedural_document(""), "")

    def test_format_entity_list_empty(self):
        self.assertEqual(format_entity_list([]), "No entities.")

    def test_format_entity_list_lists_entities(self):
        entities = [
            {"kind": "module", "locator": "app.service", "subject": "Module app.service"},
            {"kind": "function", "locator": "app.service.handle"},
        ]
        text = format_entity_list(entities)
        self.assertIn("module: app.service — Module app.service", text)
        self.assertIn("function: app.service.handle", text)

    def test_format_draft_operations_empty(self):
        self.assertEqual(format_draft_operations([]), "No operations.")

    def test_format_draft_operations_typed(self):
        operations = [
            {
                "op": "replace_description",
                "target_block_id": "codemap:app.service:purpose:1",
                "intent_class": "documentation_intent",
                "proposed": {"display_text": "A service module"},
            },
            {
                "op": "replace_condition_intent",
                "target_block_id": "codemap:app.service:decision:4",
                "intent_class": "behavior_change_intent",
                "proposed": {"display_text": "If x > 0 is true, the following runs:"},
            },
        ]
        text = format_draft_operations(operations)
        self.assertIn("Replace description — codemap:app.service:purpose:1 (Documentation): A service module", text)
        self.assertIn("Replace condition intent", text)
        self.assertIn("(Behavior change)", text)

    def test_format_intent_delta_marks_non_executable(self):
        delta = {
            "intent": "user_authored",
            "entries": [
                {
                    "operation": "replace_description",
                    "owning_entity_id": "app.service",
                    "required_approval_level": "low",
                }
            ],
        }
        text = format_intent_delta(delta)
        self.assertIn("Intent Delta (not executable)", text)
        self.assertIn("Executable: false", text)
        self.assertIn("Entries: 1", text)
        self.assertIn("Replace description on app.service (approval: low)", text)


class ProposalPresentationTests(unittest.TestCase):
    def test_proposal_state_label_falls_back_to_token(self):
        self.assertEqual(proposal_state_label("ready"), "Ready")
        self.assertEqual(proposal_state_label("clarification_required"), "Clarification required")
        self.assertEqual(proposal_state_label("unsupported"), "Unsupported")
        self.assertEqual(proposal_state_label("no_change"), "No change")
        self.assertEqual(proposal_state_label("blocked"), "Blocked")
        self.assertEqual(proposal_state_label("not_a_state"), "not_a_state")

    def test_proposal_state_labels_cover_all_states(self):
        self.assertEqual(
            set(PROPOSAL_STATE_LABELS),
            {"ready", "clarification_required", "unsupported", "no_change", "blocked"},
        )

    def test_format_proposal_empty(self):
        self.assertEqual(format_proposal({}), "")
        self.assertEqual(format_proposal(None), "")

    def test_format_proposal_marks_non_applied_and_structured(self):
        package = {
            "state": "ready",
            "proposal_id": "proposal:abc123",
            "target_scope": {"entities": ["app.service"], "artifacts": ["app/service.py"]},
            "affected_artifacts": [
                {"role": "target", "kind": "function", "path": "app/service.py"}
            ],
            "preserved_constraints": [{"entity_id": None, "invariant": "never rewritten"}],
            "assumptions": ["the baseline is current"],
            "clarifications": [],
            "plan_steps": [
                {"step": 1, "description": "Plan: replace the purpose description of x.", "requires_approval": False}
            ],
            "risks": [{"level": "low", "description": "documentation only"}],
            "validation_plan": [{"check": "deterministic", "expected_outcome": "identical package"}],
            "reason": None,
        }
        text = format_proposal(package)
        self.assertIn("Proposal Package (not applied)", text)
        self.assertIn("State: Ready", text)
        self.assertIn("Executable: false", text)
        self.assertIn("Applied: false", text)
        self.assertIn("Proposal: proposal:abc123", text)
        self.assertIn("app.service", text)
        self.assertIn("Plan steps: 1", text)
        self.assertIn("Preserved constraints: 1", text)
        self.assertIn("Validation plan: 1", text)

    def test_format_proposal_shows_clarification_and_reason(self):
        package = {
            "state": "clarification_required",
            "proposal_id": "proposal:def",
            "target_scope": {"entities": ["app.main"], "artifacts": []},
            "affected_artifacts": [],
            "preserved_constraints": [],
            "assumptions": [],
            "clarifications": [
                {"entity_id": "app.main", "question": "confirm the behavior impact"}
            ],
            "plan_steps": [],
            "risks": [],
            "validation_plan": [],
            "reason": "ambiguous behavior intent",
        }
        text = format_proposal(package)
        self.assertIn("State: Clarification required", text)
        self.assertIn("Reason: ambiguous behavior intent", text)
        self.assertIn("confirm the behavior impact", text)


class ProviderReadinessTests(unittest.TestCase):
    def test_get_readiness_request_shape(self):
        req = build_get_readiness_request("cid-1")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_GET_READINESS)
        self.assertNotIn("path", req)
        self.assertNotIn("task", req)

    def test_readiness_state_label_falls_back_to_token(self):
        self.assertEqual(provider_readiness_state_label("configured"), "Configured")
        self.assertEqual(
            provider_readiness_state_label("missing_credential"), "Credential missing"
        )
        self.assertEqual(provider_readiness_state_label("unavailable"), "Unavailable")
        self.assertEqual(
            provider_readiness_state_label("invalid_config"), "Invalid configuration"
        )
        self.assertEqual(
            provider_readiness_state_label("not_a_state"), "not_a_state"
        )

    def test_readiness_state_labels_cover_all_states(self):
        self.assertEqual(
            set(PROVIDER_READINESS_STATE_LABELS),
            {"configured", "missing_credential", "unavailable", "invalid_config"},
        )

    def test_format_provider_readiness_is_redacted_and_bounded(self):
        text = format_provider_readiness(
            {
                "state": "configured",
                "provider_id": "deepseek",
                "model": "deepseek-v4-flash",
                "credential_present": True,
                "authenticated": False,
                "online": False,
                "executable": False,
            }
        )
        self.assertIn("Provider: deepseek", text)
        self.assertIn("State: Configured", text)
        self.assertIn("Model: deepseek-v4-flash", text)
        self.assertIn("Authenticated: false", text)
        self.assertIn("Online: false", text)
        self.assertIn("Executable: false", text)

    def test_format_provider_readiness_never_claims_network(self):
        text = format_provider_readiness(
            {
                "state": "configured",
                "provider_id": "deepseek",
                "model": "deepseek-v4-flash",
                "credential_present": True,
                "authenticated": True,
                "online": True,
                "executable": True,
            }
        )
        # Whatever the (defensive) input claims, the presentation of a local
        # readiness check never asserts the provider is reachable.
        self.assertNotIn("Authenticated: true", text)
        self.assertNotIn("Online: true", text)


class ProviderStatusMessageTests(unittest.TestCase):
    def test_provider_status_messages_are_exact(self):
        self.assertEqual(
            provider_status_message("pending"), "Checking local provider configuration…"
        )
        self.assertEqual(
            provider_status_message("configured"), "DeepSeek is configured locally"
        )
        self.assertEqual(
            provider_status_message("missing_credential"),
            "DeepSeek API key not configured",
        )
        self.assertEqual(
            provider_status_message("unavailable"),
            "Provider setup is unavailable on this platform",
        )
        self.assertEqual(
            provider_status_message("invalid_config"),
            "Provider configuration needs repair",
        )
        self.assertEqual(
            provider_status_message("failed"), "Provider check failed; try again."
        )
        self.assertEqual(provider_status_message("unknown"), "unknown")

    def test_provider_status_messages_cover_all_states(self):
        self.assertEqual(
            set(PROVIDER_STATUS_MESSAGES),
            {
                "pending",
                "configured",
                "missing_credential",
                "unavailable",
                "invalid_config",
                "failed",
            },
        )

    def test_credential_action_messages_are_exact(self):
        self.assertEqual(credential_action_message("stored"), "API key stored securely.")
        self.assertEqual(
            credential_action_message("cancelled"),
            "No change — the secure prompt was cancelled.",
        )
        self.assertEqual(credential_action_message("removed"), "API key removed.")
        self.assertEqual(
            credential_action_message("unavailable"),
            "Secure key management is unavailable on this platform.",
        )
        self.assertEqual(
            credential_action_message("failed"),
            "The operation could not be completed.",
        )

    def test_credential_action_messages_cover_all_states(self):
        self.assertEqual(
            set(CREDENTIAL_ACTION_MESSAGES),
            {"stored", "cancelled", "removed", "unavailable", "failed"},
        )

    def test_manage_credential_request_shape(self):
        req = build_manage_credential_request("cid-1")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_MANAGE_CREDENTIAL)
        self.assertNotIn("path", req)
        self.assertNotIn("task", req)
        self.assertNotIn("secret", req)
        self.assertNotIn("credential", req)

    def test_remove_credential_request_shape(self):
        req = build_remove_credential_request("cid-1")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["correlation_id"], "cid-1")
        self.assertEqual(req["action"], contract.ACTION_REMOVE_CREDENTIAL)
        self.assertNotIn("path", req)
        self.assertNotIn("task", req)


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
