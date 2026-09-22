"""Tests for the headless local application boundary (P3.1)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest import mock

from hrca import (
    boundary,
    contract,
    memory,
    memory_store,
    twin,
    twin_store,
    workspace,
)
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


def _workspace_request(action, **overrides):
    """Build a P3.2 workspace-action request envelope."""
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-ws",
        "action": action,
    }
    req.update(overrides)
    return req


def _twin_request(action, **overrides):
    """Build a P3.3 Twin-action request envelope."""
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-twin",
        "action": action,
    }
    req.update(overrides)
    return req


def _run_twin(store_base, *raw_lines):
    """Feed requests to the boundary loop with a temporary Twin store base."""
    stdin = io.StringIO("\n".join(raw_lines) + ("\n" if raw_lines else ""))
    stdout = io.StringIO()
    stderr = io.StringIO()
    boundary.run_loop(stdin, stdout, stderr, store_base=store_base)
    return stdout.getvalue().splitlines(), stderr.getvalue()


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


class BoundaryWorkspaceTests(unittest.TestCase):
    def test_open_project_then_get_tree(self):
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_tree = _workspace_request(contract.ACTION_GET_TREE)
        _, responses, _ = _run(dumps(req_open), dumps(req_tree))
        open_env = loads(responses[0])
        tree_env = loads(responses[1])
        self.assertTrue(open_env["ok"])
        self.assertEqual(open_env["result"]["root"], os.path.realpath(FIXTURES))
        self.assertEqual(open_env["result"]["repository_state"], "Unverified")
        self.assertTrue(tree_env["ok"])
        self.assertEqual(tree_env["result"]["root"], os.path.realpath(FIXTURES))
        self.assertIn("children", tree_env["result"])

    def test_get_tree_without_open_project_rejected(self):
        env = _first_response(dumps(_workspace_request(contract.ACTION_GET_TREE)))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_get_document_without_open_project_rejected(self):
        req = _workspace_request(contract.ACTION_GET_DOCUMENT, path="app/main.py")
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_open_project_flow_reads_document(self):
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_doc = _workspace_request(contract.ACTION_GET_DOCUMENT, path="app/main.py")
        _, responses, _ = _run(dumps(req_open), dumps(req_doc))
        doc_env = loads(responses[1])
        self.assertTrue(doc_env["ok"])
        self.assertEqual(doc_env["result"]["path"], "app/main.py")
        self.assertEqual(doc_env["result"]["kind"], "source")
        self.assertIn("print", doc_env["result"]["content"])

    def test_open_project_flow_reads_preview_document(self):
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_doc = _workspace_request(
            contract.ACTION_GET_DOCUMENT, path="nonascii/traditional_chinese.txt"
        )
        _, responses, _ = _run(dumps(req_open), dumps(req_doc))
        doc_env = loads(responses[1])
        self.assertTrue(doc_env["ok"])
        self.assertEqual(doc_env["result"]["kind"], "preview")
        self.assertIn("繁體", doc_env["result"]["content"])

    def test_open_project_flow_missing_document_is_unavailable(self):
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_doc = _workspace_request(contract.ACTION_GET_DOCUMENT, path="no/such/file.py")
        _, responses, _ = _run(dumps(req_open), dumps(req_doc))
        doc_env = loads(responses[1])
        self.assertTrue(doc_env["ok"])
        self.assertEqual(doc_env["result"]["kind"], "unavailable")
        self.assertEqual(doc_env["result"]["reason"], "path_not_found")

    def test_open_project_missing_path_rejected(self):
        missing = os.path.join(FIXTURES, "does-not-exist")
        req = _workspace_request(contract.ACTION_OPEN_PROJECT, path=missing)
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "path_not_found")

    def test_open_project_non_directory_rejected(self):
        req = _workspace_request(
            contract.ACTION_OPEN_PROJECT, path=os.path.join(FIXTURES, "app", "main.py")
        )
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "path_not_found")

    def test_get_document_traversal_rejected(self):
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_doc = _workspace_request(contract.ACTION_GET_DOCUMENT, path="../secret.py")
        _, responses, _ = _run(dumps(req_open), dumps(req_doc))
        doc_env = loads(responses[1])
        self.assertFalse(doc_env["ok"])
        self.assertEqual(doc_env["error"]["code"], "path_not_allowed")

    def test_workspace_session_does_not_leak_across_loops(self):
        # Each run_loop owns a fresh WorkspaceSession; opening in one loop must
        # not make a later loop's get_tree succeed.
        _run(dumps(_workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)))
        env = _first_response(dumps(_workspace_request(contract.ACTION_GET_TREE)))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_workspace_error_does_not_echo_path(self):
        req = _workspace_request(
            contract.ACTION_OPEN_PROJECT, path=os.path.join(FIXTURES, "secret-token")
        )
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertNotIn("secret-token", dumps(env))


class BoundaryTwinTests(unittest.TestCase):
    """The P3.3 read-only Twin protocol over the NDJSON boundary.

    Twin storage is isolated to a temporary base directory so the real per-user
    app-data directory is never written during a test run.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _open_and(self, *requests):
        reqs = [_twin_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)] + list(requests)
        lines, _ = _run_twin(self.store_base, *[dumps(r) for r in reqs])
        return [loads(line) for line in lines]

    def test_sync_twin_without_open_project_rejected(self):
        lines, _ = _run_twin(
            self.store_base, dumps(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        )
        env = loads(lines[0])
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_get_twin_without_open_project_rejected(self):
        lines, _ = _run_twin(
            self.store_base,
            dumps(_twin_request(contract.ACTION_GET_TWIN, task={"selector": "a.py"})),
        )
        self.assertEqual(loads(lines[0])["error"]["code"], "project_not_open")

    def test_get_anchor_without_open_project_rejected(self):
        lines, _ = _run_twin(
            self.store_base,
            dumps(_twin_request(contract.ACTION_GET_ANCHOR, task={"node_id": "behavior:x"})),
        )
        self.assertEqual(loads(lines[0])["error"]["code"], "project_not_open")

    def test_full_sync_then_retrieve_and_anchor(self):
        envs = self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        sync_env = envs[1]
        self.assertTrue(sync_env["ok"])
        result = sync_env["result"]
        self.assertEqual(result["state"], "synchronized")
        self.assertTrue(result["persisted"])
        self.assertIn("counts", result)
        self.assertGreater(result["counts"]["artifacts"], 0)

    def test_get_twin_retrieves_file_projection(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        envs = self._open_and(
            _twin_request(contract.ACTION_GET_TWIN, task={"selector": "app/service.py"})
        )
        env = envs[1]
        self.assertTrue(env["ok"])
        bundle = env["result"]
        self.assertEqual(bundle["projection"]["kind"], "file")
        self.assertEqual(bundle["projection"]["path"], "app/service.py")
        self.assertIn("provenance", bundle["projection"])
        self.assertIn("confidence", bundle["projection"])
        self.assertIn("sync_state", bundle["projection"])

    def test_get_twin_retrieves_pyi_file_projection(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        envs = self._open_and(
            _twin_request(contract.ACTION_GET_TWIN, task={"selector": "app/stubs.pyi"})
        )
        env = envs[1]
        self.assertTrue(env["ok"])
        bundle = env["result"]
        self.assertEqual(bundle["projection"]["kind"], "file")
        self.assertEqual(bundle["projection"]["path"], "app/stubs.pyi")
        self.assertIn("sync_state", bundle["projection"])

    def test_get_twin_retrieves_symbol_projection_with_behavior_nodes(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        envs = self._open_and(
            _twin_request(
                contract.ACTION_GET_TWIN, task={"selector": "app.service.Service.handle"}
            )
        )
        bundle = envs[1]["result"]
        self.assertEqual(bundle["projection"]["kind"], "method")
        self.assertGreater(len(bundle["behavior_nodes"]), 0)

    def test_get_anchor_navigates_behavior_node(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        envs = self._open_and(
            _twin_request(
                contract.ACTION_GET_TWIN, task={"selector": "app.service.Service.handle"}
            )
        )
        node_id = envs[1]["result"]["behavior_nodes"][0]["id"]
        envs = self._open_and(
            _twin_request(contract.ACTION_GET_ANCHOR, task={"node_id": node_id})
        )
        anchor = envs[1]["result"]
        self.assertTrue(anchor["available"])
        self.assertEqual(anchor["file"], "app/service.py")
        self.assertIn("source_range", anchor)
        self.assertIn("lineno", anchor["source_range"])

    def test_no_change_sync_is_idempotent(self):
        envs = self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        self.assertEqual(envs[1]["result"]["state"], "synchronized")
        envs = self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        self.assertEqual(envs[1]["result"]["state"], "no_change")

    def test_unknown_selector_is_bounded(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        envs = self._open_and(
            _twin_request(contract.ACTION_GET_TWIN, task={"selector": "does-not-exist.py"})
        )
        env = envs[1]
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "twin_not_found")
        self.assertNotIn("does-not-exist", dumps(env))

    def test_unknown_anchor_is_bounded(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        envs = self._open_and(
            _twin_request(contract.ACTION_GET_ANCHOR, task={"node_id": "behavior:none:1"})
        )
        self.assertFalse(envs[1]["ok"])
        self.assertEqual(envs[1]["error"]["code"], "twin_not_found")

    def test_twin_store_is_isolated_to_store_base(self):
        self._open_and(_twin_request(contract.ACTION_SYNC_TWIN, task={}))
        store_files = []
        for dirpath, _dirs, files in os.walk(self.store_base):
            store_files.extend(os.path.join(dirpath, f) for f in files)
        self.assertTrue(store_files)
        # Nothing is written into the selected repository.
        self.assertFalse(any(FIXTURES in f for f in store_files))


class BoundaryDraftTests(unittest.TestCase):
    """The P3.4 editable Code Map protocol over the NDJSON boundary.

    A single :class:`~hrca.boundary.WorkspaceSession` is shared across requests
    (as in a live boundary loop) so ``open_project`` establishes the root that
    later draft actions operate on. Draft storage is isolated to a temporary
    base directory so the real per-user app-data directory is never written and
    the selected repository is never modified.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_base = self._tmp.name
        self.session = boundary.WorkspaceSession(store_base=self.store_base)
        self.wsid = twin.workspace_id_for(os.path.realpath(FIXTURES))

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        req = _twin_request(action, **overrides)
        return boundary.handle_request(req, self.session)

    def _open_sync(self):
        open_env = self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        self.assertTrue(open_env["ok"])
        sync_env = self._do(contract.ACTION_SYNC_TWIN, task={})
        self.assertTrue(sync_env["ok"])
        return sync_env

    def _blocks(self):
        return self._do(contract.ACTION_GET_CODE_MAP)["result"]["blocks"]

    def _module_entity_id(self):
        return next(
            b["block_id"]
            for b in self._blocks()
            if b.get("block_type") == "entity"
            and (b.get("payload") or {}).get("kind") == "module"
            and (b.get("payload") or {}).get("locator") == "app.service"
        )

    def _module_purpose_id(self):
        blocks = self._blocks()
        module_id = next(
            b["block_id"]
            for b in blocks
            if b.get("block_type") == "entity"
            and (b.get("payload") or {}).get("kind") == "module"
            and (b.get("payload") or {}).get("locator") == "app.service"
        )
        return next(
            b["block_id"]
            for b in blocks
            if b.get("block_type") == "purpose" and b.get("parent_id") == module_id
        )

    def _purpose_ops(self, value="entry point for the service"):
        return [
            {
                "op": "replace_description",
                "target_block_id": self._module_purpose_id(),
                "proposed_text": value,
            }
        ]

    def test_get_code_map_without_open_rejected(self):
        env = self._do(contract.ACTION_GET_CODE_MAP)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_get_code_map_without_sync_rejected(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        env = self._do(contract.ACTION_GET_CODE_MAP)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "twin_not_synchronized")

    def test_get_code_map_returns_language_version_and_no_draft(self):
        self._open_sync()
        result = self._do(contract.ACTION_GET_CODE_MAP)["result"]
        self.assertEqual(result["language_version"], "0.1")
        self.assertEqual(result["generator"], "hrca-codemap")
        self.assertTrue(result["document"])
        self.assertTrue(result["entities"])
        self.assertTrue(result["blocks"])
        self.assertIn("baseline_revision", result["baseline"])
        self.assertIsNone(result["draft"])
        self.assertEqual(result["conflict"]["state"], "none")

    def test_save_draft_persists_and_round_trips(self):
        self._open_sync()
        save_env = self._do(
            contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()}
        )
        self.assertTrue(save_env["ok"])
        self.assertTrue(save_env["result"]["persisted"])
        self.assertEqual(len(save_env["result"]["draft"]["operations"]), 1)
        get_env = self._do(contract.ACTION_GET_DRAFT)
        self.assertTrue(get_env["ok"])
        self.assertEqual(
            get_env["result"]["draft"]["draft_id"],
            save_env["result"]["draft"]["draft_id"],
        )

    def test_save_draft_read_only_block_rejected(self):
        self._open_sync()
        ops = [
            {
                "op": "replace_description",
                "target_block_id": self._module_entity_id(),
                "proposed_text": "hacked.py",
            }
        ]
        env = self._do(contract.ACTION_SAVE_DRAFT, task={"operations": ops})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_invalid")
        self.assertNotIn("hacked.py", dumps(env))

    def test_save_draft_unknown_target_rejected(self):
        self._open_sync()
        ops = [
            {
                "op": "replace_description",
                "target_block_id": "codemap:app.service:purpose:99999",
                "proposed_text": "x",
            }
        ]
        env = self._do(contract.ACTION_SAVE_DRAFT, task={"operations": ops})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_invalid")

    def test_save_draft_oversized_rejected(self):
        self._open_sync()
        ops = [
            {
                "op": "replace_description",
                "target_block_id": self._module_purpose_id(),
                "proposed_text": "x" * 5000,
            }
        ]
        env = self._do(contract.ACTION_SAVE_DRAFT, task={"operations": ops})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_oversized")

    def test_noop_draft_generates_no_change(self):
        self._open_sync()
        save_env = self._do(contract.ACTION_SAVE_DRAFT, task={"operations": []})
        self.assertTrue(save_env["ok"])
        delta_env = self._do(contract.ACTION_GENERATE_INTENT_DELTA)
        self.assertTrue(delta_env["ok"])
        self.assertTrue(delta_env["result"]["no_change"])
        self.assertIsNone(delta_env["result"]["intent_delta"])

    def test_intent_delta_is_deterministic(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        first = self._do(contract.ACTION_GENERATE_INTENT_DELTA)["result"]["intent_delta"]
        second = self._do(contract.ACTION_GENERATE_INTENT_DELTA)["result"]["intent_delta"]
        self.assertIsNotNone(first)
        self.assertFalse(first["executable"])
        self.assertEqual(dumps(first), dumps(second))

    def test_stale_draft_blocks_intent_delta(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        # Simulate a re-sync that changed the baseline fingerprint.
        store, _ = twin_store.load(self.store_base, self.wsid)
        store["workspace_revision"]["baseline_fingerprint"] = "fp:changed"
        twin_store.save(self.store_base, self.wsid, store)
        env = self._do(contract.ACTION_GENERATE_INTENT_DELTA)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_stale")

    def test_compare_draft_returns_operations(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        result = self._do(contract.ACTION_COMPARE_DRAFT)["result"]
        self.assertEqual(len(result["operations"]), 1)
        self.assertEqual(result["operations"][0]["op"], "replace_description")
        self.assertEqual(
            result["operations"][0]["proposed"]["display_text"],
            "entry point for the service",
        )
        self.assertEqual(result["conflict"]["state"], "none")

    def test_discard_draft_then_get_is_not_found(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        discard_env = self._do(contract.ACTION_DISCARD_DRAFT)
        self.assertTrue(discard_env["ok"])
        self.assertTrue(discard_env["result"]["discarded"])
        get_env = self._do(contract.ACTION_GET_DRAFT)
        self.assertFalse(get_env["ok"])
        self.assertEqual(get_env["error"]["code"], "draft_not_found")

    def test_reset_draft_returns_to_baseline(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        reset_env = self._do(contract.ACTION_RESET_DRAFT)
        self.assertTrue(reset_env["ok"])
        self.assertTrue(reset_env["result"]["reset"])
        get_env = self._do(contract.ACTION_GET_DRAFT)
        self.assertEqual(get_env["error"]["code"], "draft_not_found")

    def test_get_draft_without_draft_rejected(self):
        self._open_sync()
        env = self._do(contract.ACTION_GET_DRAFT)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_not_found")

    def test_draft_write_stays_out_of_repository(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        draft_path = twin_store.workspace_draft_path(self.store_base, self.wsid)
        self.assertTrue(os.path.isfile(draft_path))
        # The draft lives under the app-data store base, never the repository.
        self.assertFalse(draft_path.startswith(FIXTURES))


class BoundaryProposalTests(unittest.TestCase):
    """The P4.1 read-only proposal-planning protocol over the NDJSON boundary.

    A single :class:`~hrca.boundary.WorkspaceSession` is shared across requests
    (as in a live boundary loop) so ``open_project`` establishes the root that
    later proposal actions operate on. Proposal planning derives a non-applied
    package from the saved draft and synchronized Twin — it never writes the
    selected repository.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_base = self._tmp.name
        self.session = boundary.WorkspaceSession(store_base=self.store_base)
        self.wsid = twin.workspace_id_for(os.path.realpath(FIXTURES))

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        req = _twin_request(action, **overrides)
        return boundary.handle_request(req, self.session)

    def _open_sync(self):
        open_env = self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        self.assertTrue(open_env["ok"])
        sync_env = self._do(contract.ACTION_SYNC_TWIN, task={})
        self.assertTrue(sync_env["ok"])
        return sync_env

    def _module_purpose_id(self):
        blocks = self._do(contract.ACTION_GET_CODE_MAP)["result"]["blocks"]
        module_id = next(
            b["block_id"]
            for b in blocks
            if b.get("block_type") == "entity"
            and (b.get("payload") or {}).get("kind") == "module"
            and (b.get("payload") or {}).get("locator") == "app.service"
        )
        return next(
            b["block_id"]
            for b in blocks
            if b.get("block_type") == "purpose" and b.get("parent_id") == module_id
        )

    def _purpose_ops(self, value="entry point for the service"):
        return [
            {
                "op": "replace_description",
                "target_block_id": self._module_purpose_id(),
                "proposed_text": value,
            }
        ]

    def test_plan_proposal_without_open_rejected(self):
        env = self._do(contract.ACTION_PLAN_PROPOSAL)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_plan_proposal_without_sync_rejected(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        env = self._do(contract.ACTION_PLAN_PROPOSAL)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "twin_not_synchronized")

    def test_plan_proposal_without_draft_rejected(self):
        self._open_sync()
        env = self._do(contract.ACTION_PLAN_PROPOSAL)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_not_found")

    def test_noop_draft_plans_no_change(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": []})
        env = self._do(contract.ACTION_PLAN_PROPOSAL)
        self.assertTrue(env["ok"])
        self.assertTrue(env["result"]["no_change"])
        self.assertIsNone(env["result"]["proposal"])

    def test_documentation_draft_plans_ready_package(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        env = self._do(contract.ACTION_PLAN_PROPOSAL)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertFalse(result["no_change"])
        self.assertEqual(result["state"], "ready")
        package = result["proposal"]
        self.assertFalse(package["executable"])
        self.assertFalse(package["applied"])
        self.assertTrue(package["proposal_id"].startswith("proposal:"))
        self.assertEqual(package["target_scope"]["entities"], ["app.service"])

    def test_proposal_planning_is_deterministic(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        first = self._do(contract.ACTION_PLAN_PROPOSAL)["result"]["proposal"]
        second = self._do(contract.ACTION_PLAN_PROPOSAL)["result"]["proposal"]
        self.assertEqual(dumps(first), dumps(second))

    def test_stale_draft_blocks_proposal(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        store, _ = twin_store.load(self.store_base, self.wsid)
        store["workspace_revision"]["baseline_fingerprint"] = "fp:changed"
        twin_store.save(self.store_base, self.wsid, store)
        env = self._do(contract.ACTION_PLAN_PROPOSAL)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_stale")

    def test_proposal_planning_stays_out_of_repository(self):
        self._open_sync()
        self._do(contract.ACTION_SAVE_DRAFT, task={"operations": self._purpose_ops()})
        self._do(contract.ACTION_PLAN_PROPOSAL)
        draft_path = twin_store.workspace_draft_path(self.store_base, self.wsid)
        self.assertTrue(os.path.isfile(draft_path))
        self.assertFalse(draft_path.startswith(FIXTURES))


# The registry's contents, in registration order. Held as one literal so the
# tests below pin the *whole* set rather than a count, and so adding an action
# to the registry without extending this tuple is a failure rather than a
# silently updated expectation.
_EXPECTED_REGISTRY = (
    (contract.ACTION_GET_TREE, boundary._get_tree_result),
    (contract.ACTION_GET_DOCUMENT, boundary._get_document_result),
    (contract.ACTION_DOCUMENT_LIST, boundary._list_documents_result),
    (contract.ACTION_DOCUMENT_LIST_VERSIONS, boundary._list_versions_result),
    (contract.ACTION_DOCUMENT_GET_CANDIDATE, boundary._get_candidate_result),
    (contract.ACTION_DOCUMENT_PREVIEW, boundary._preview_document_result),
    (contract.ACTION_MEMORY_DOCUMENTS, boundary._get_memory_documents_result),
    (contract.ACTION_MEMORY_RECORD, boundary._get_memory_record_result),
    (contract.ACTION_MEMORY_SEARCH, boundary._search_memory_result),
    (contract.ACTION_MEMORY_RESUME, boundary._memory_resume_result),
    (contract.ACTION_MEMORY_HISTORY, boundary._get_memory_history_result),
    (contract.ACTION_MEMORY_EFFECTIVE, boundary._resolve_memory_effective_result),
    # The scan family: five synonyms of one session-free pipeline, reached
    # through the registry's only adapter.
    (contract.ACTION_SCAN, boundary._scan_handler),
    (contract.ACTION_READ, boundary._scan_handler),
    (contract.ACTION_ANALYZE, boundary._scan_handler),
    (contract.ACTION_INSPECT, boundary._scan_handler),
    (contract.ACTION_PLAN, boundary._scan_handler),
)

# Actions that look read-only and are deliberately held back, with the reason.
_HELD_BACK = {
    contract.ACTION_OPEN_PROJECT: "it mutates in-memory session state",
    contract.ACTION_DOCUMENT_SAVE: "it writes the version store",
    contract.ACTION_DOCUMENT_ADOPT: "it changes accepted state",
    contract.ACTION_LIBRARY_GET: "it creates and persists the library store",
    contract.ACTION_GET_PACKAGE: "package/runner group",
    contract.ACTION_GET_READINESS: "provider/credential seam",
    contract.ACTION_MEMORY_CORRECTION: "the one writing Memory action",
    contract.ACTION_SYNC_TWIN: "Twin family, held for B4",
    contract.ACTION_MEMORY_CODE_LINK: "Memory-Twin bridge, held for B4",
    contract.ACTION_MEMORY_CODE_FRESHNESS: "Memory-Twin bridge, held for B4",
}


class BoundaryHandlerRegistryTests(unittest.TestCase):
    """The B3a registry proof: one action registered, everything else legacy.

    The registry is deliberately an internal implementation detail, so these
    tests reach it directly rather than through the protocol. What they hold is
    that it is deterministic, that a bad registration fails while it is being
    built rather than when a request arrives, that it owns exactly one action,
    and that `get_tree` — the action it owns — is byte-for-byte the same as it
    was on the dispatch chain.
    """

    def test_the_registry_is_constructed_when_the_module_loads(self):
        # Built at import, so a duplicate or a non-callable entry is a startup
        # failure rather than a request-time surprise.
        self.assertIsInstance(boundary._BOUNDARY_HANDLERS, boundary._HandlerRegistry)

    def test_the_registry_owns_the_read_only_actions(self):
        self.assertEqual(
            tuple(action for action, _ in _EXPECTED_REGISTRY),
            boundary._BOUNDARY_HANDLERS.actions(),
        )

    def test_the_registered_handler_is_the_existing_handler(self):
        self.assertIs(
            boundary._get_tree_result,
            boundary._BOUNDARY_HANDLERS.resolve(contract.ACTION_GET_TREE),
        )

    def test_the_entries_name_the_action_by_contract_constant(self):
        # Every entry pairs a contract constant with the very handler the chain
        # used to call for it: the registry took over dispatch, it did not
        # replace the handler.
        self.assertEqual(_EXPECTED_REGISTRY, boundary._BOUNDARY_HANDLER_ENTRIES)

    def test_no_other_allowed_action_is_registered(self):
        # These twelve are registered, and the remaining 49 allowed actions are
        # still on the chain they always used.
        registered = {
            action
            for action in contract.ALLOWED_ACTIONS
            if boundary._BOUNDARY_HANDLERS.resolve(action) is not None
        }
        self.assertEqual(
            {action for action, _ in _EXPECTED_REGISTRY}, registered
        )
        self.assertEqual(
            len(contract.ALLOWED_ACTIONS) - len(_EXPECTED_REGISTRY),
            len([a for a in contract.ALLOWED_ACTIONS
                 if boundary._BOUNDARY_HANDLERS.resolve(a) is None]),
        )

    def test_actions_held_for_later_packages_are_not_registered(self):
        for action, why in sorted(_HELD_BACK.items()):
            with self.subTest(action=action):
                self.assertIsNone(
                    boundary._BOUNDARY_HANDLERS.resolve(action), why
                )

    def test_no_memory_action_that_writes_is_registered(self):
        # The Memory group is read-only by construction. The one action that
        # appends to a store must never drift in because its neighbours did.
        self.assertIsNone(
            boundary._BOUNDARY_HANDLERS.resolve(contract.ACTION_MEMORY_CORRECTION)
        )
        for action in (
            contract.ACTION_MEMORY_CODE_LINK,
            contract.ACTION_MEMORY_CODE_FRESHNESS,
        ):
            with self.subTest(action=action):
                self.assertIsNone(boundary._BOUNDARY_HANDLERS.resolve(action))

    def test_no_twin_provider_or_execution_action_is_registered(self):
        for action in (
            contract.ACTION_SYNC_TWIN,
            contract.ACTION_GET_TWIN,
            contract.ACTION_GET_ANCHOR,
            contract.ACTION_GET_CODE_MAP,
            contract.ACTION_MEMORY_CODE_LINK,
            contract.ACTION_RUN_PACKAGE,
            contract.ACTION_PLAN_ADVISORY,
            contract.ACTION_MANAGE_CREDENTIAL,
        ):
            with self.subTest(action=action):
                self.assertIsNone(boundary._BOUNDARY_HANDLERS.resolve(action))

    def test_registration_order_is_deterministic(self):
        registry = boundary._HandlerRegistry(
            [
                (contract.ACTION_GET_TREE, boundary._get_tree_result),
                (contract.ACTION_OPEN_PROJECT, boundary._open_project_result),
                (contract.ACTION_GET_DOCUMENT, boundary._get_document_result),
            ]
        )
        self.assertEqual(
            (
                contract.ACTION_GET_TREE,
                contract.ACTION_OPEN_PROJECT,
                contract.ACTION_GET_DOCUMENT,
            ),
            registry.actions(),
        )
        # And the same entries in the same order give the same result twice.
        again = boundary._HandlerRegistry(
            [
                (contract.ACTION_GET_TREE, boundary._get_tree_result),
                (contract.ACTION_OPEN_PROJECT, boundary._open_project_result),
                (contract.ACTION_GET_DOCUMENT, boundary._get_document_result),
            ]
        )
        self.assertEqual(registry.actions(), again.actions())

    def test_duplicate_registration_is_refused_while_building(self):
        with self.assertRaises(boundary._RegistryError) as caught:
            boundary._HandlerRegistry(
                [
                    (contract.ACTION_GET_TREE, boundary._get_tree_result),
                    (contract.ACTION_GET_TREE, boundary._get_tree_result),
                ]
            )
        self.assertIn("duplicate", str(caught.exception))

    def test_a_non_callable_handler_is_refused_while_building(self):
        for bad in (None, "not callable", 5, {}):
            with self.subTest(handler=bad):
                with self.assertRaises(boundary._RegistryError) as caught:
                    boundary._HandlerRegistry([(contract.ACTION_GET_TREE, bad)])
                self.assertIn("callable", str(caught.exception))

    def test_an_action_the_contract_does_not_allow_is_refused(self):
        for bad in ("no_such_action", "", 5, None):
            with self.subTest(action=bad):
                with self.assertRaises(boundary._RegistryError):
                    boundary._HandlerRegistry([(bad, boundary._get_tree_result)])

    def test_resolve_is_total_for_every_input_the_dispatcher_can_hand_it(self):
        for value in (None, 5, [], {}, "", "no_such_action"):
            with self.subTest(value=value):
                self.assertIsNone(boundary._BOUNDARY_HANDLERS.resolve(value))

    def test_get_tree_returns_the_result_the_handler_produces(self):
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_tree = _workspace_request(contract.ACTION_GET_TREE)
        _, responses, _ = _run(dumps(req_open), dumps(req_tree))
        tree_env = loads(responses[1])
        self.assertTrue(tree_env["ok"])
        self.assertEqual("cid-ws", tree_env["correlation_id"])
        self.assertEqual(
            workspace.build_tree(os.path.realpath(FIXTURES)), tree_env["result"]
        )

    def test_get_tree_envelope_shape_is_unchanged(self):
        req_tree = _workspace_request(contract.ACTION_GET_TREE)
        env = _first_response(dumps(req_tree))
        self.assertEqual(
            {"contract_version", "correlation_id", "ok", "error"}, set(env)
        )
        self.assertFalse(env["ok"])
        self.assertEqual("project_not_open", env["error"]["code"])
        self.assertEqual(
            contract.CONTRACT_VERSION, env["contract_version"]
        )

    def test_get_tree_without_an_accepted_root_still_refuses(self):
        env = _first_response(dumps(_workspace_request(contract.ACTION_GET_TREE)))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_a_wrong_contract_version_is_refused_before_the_registry(self):
        # If the version check ran after dispatch, this would report
        # ``project_not_open`` for a registered action. It reports the version
        # refusal, so the registry is never reached.
        req = _workspace_request(
            contract.ACTION_GET_TREE, contract_version="0.0.1"
        )
        env = _first_response(dumps(req))
        self.assertFalse(env["ok"])
        self.assertEqual("unknown_contract_version", env["error"]["code"])

    def test_an_unknown_action_still_gets_the_bounded_refusal(self):
        env = _first_response(dumps(_workspace_request("no_such_action")))
        self.assertFalse(env["ok"])
        self.assertEqual("action_not_allowed", env["error"]["code"])
        self.assertEqual(
            contract.error_message("action_not_allowed"), env["error"]["message"]
        )
        self.assertEqual(
            "action is not allowed by the read-only boundary",
            env["error"]["message"],
        )

    def test_unregistered_actions_keep_working_through_the_legacy_chain(self):
        # `open_project` and `get_document` are both allowed, both unregistered,
        # and both still served — the registry did not take them over.
        req_open = _workspace_request(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        req_doc = _workspace_request(
            contract.ACTION_GET_DOCUMENT, path="app/main.py"
        )
        _, responses, _ = _run(dumps(req_open), dumps(req_doc))
        self.assertTrue(loads(responses[0])["ok"])
        doc_env = loads(responses[1])
        self.assertTrue(doc_env["ok"])
        self.assertEqual("main.py", doc_env["result"]["name"])


class BoundaryRegisteredReadTests(unittest.TestCase):
    """The six registered read actions, exercised end to end.

    These go through ``handle_request``, so they exercise the registry itself:
    the architecture tests prove no legacy ``elif`` still claims these actions,
    and these prove the actions still answer exactly as they did.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(
            _workspace_request(action, **overrides), self.session
        )

    def _make_document(self, name="notes.md"):
        created = self._do(contract.ACTION_DOCUMENT_CREATE, name=name)
        self.assertTrue(created["ok"])
        return created["result"]["document"]["document_id"]

    def _make_candidate(self):
        document_id = self._make_document()
        saved = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=document_id,
            content="v1",
            base_revision_id=None,
        )
        self.assertTrue(saved["ok"])
        created = self._do(
            contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=document_id
        )
        self.assertTrue(created["ok"])
        return document_id, created["result"]["candidate"]["candidate_id"]

    # -- get_document ----------------------------------------------------

    def test_get_document_reads_a_permitted_file(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        env = self._do(contract.ACTION_GET_DOCUMENT, path="app/main.py")
        self.assertTrue(env["ok"])
        self.assertEqual("main.py", env["result"]["name"])
        self.assertEqual("source", env["result"]["kind"])

    def test_get_document_refuses_without_an_accepted_root(self):
        env = self._do(contract.ACTION_GET_DOCUMENT, path="app/main.py")
        self.assertFalse(env["ok"])
        self.assertEqual("project_not_open", env["error"]["code"])

    def test_get_document_refuses_a_path_that_escapes(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        env = self._do(contract.ACTION_GET_DOCUMENT, path="../escape.py")
        self.assertFalse(env["ok"])
        self.assertEqual("path_not_allowed", env["error"]["code"])

    def test_get_document_is_unavailable_for_a_missing_file(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        env = self._do(contract.ACTION_GET_DOCUMENT, path="nope.py")
        self.assertTrue(env["ok"])
        self.assertEqual("unavailable", env["result"]["kind"])
        self.assertEqual("path_not_found", env["result"]["reason"])

    # -- list_documents --------------------------------------------------

    def test_list_documents_starts_empty(self):
        env = self._do(contract.ACTION_DOCUMENT_LIST)
        self.assertTrue(env["ok"])
        self.assertEqual([], env["result"]["documents"])

    def test_list_documents_lists_a_created_document(self):
        document_id = self._make_document()
        env = self._do(contract.ACTION_DOCUMENT_LIST)
        self.assertTrue(env["ok"])
        self.assertEqual(
            [document_id], [d["document_id"] for d in env["result"]["documents"]]
        )

    def test_list_documents_refuses_an_invalid_request_never(self):
        # The action takes no arguments, so the only way it could refuse is a
        # malformed envelope — which the dispatcher refuses before dispatch.
        env = boundary.handle_request(
            {"contract_version": "0.0.1", "action": contract.ACTION_DOCUMENT_LIST}
        )
        self.assertFalse(env["ok"])
        self.assertEqual("unknown_contract_version", env["error"]["code"])

    # -- list_versions ---------------------------------------------------

    def test_list_versions_reads_an_existing_document(self):
        document_id = self._make_document()
        env = self._do(
            contract.ACTION_DOCUMENT_LIST_VERSIONS, document_id=document_id
        )
        self.assertTrue(env["ok"])
        self.assertEqual([], env["result"]["versions"])

    def test_list_versions_refuses_a_missing_document(self):
        env = self._do(
            contract.ACTION_DOCUMENT_LIST_VERSIONS, document_id="doc:absent"
        )
        self.assertFalse(env["ok"])
        self.assertEqual("document_not_found", env["error"]["code"])

    def test_list_versions_refuses_an_invalid_request(self):
        env = self._do(contract.ACTION_DOCUMENT_LIST_VERSIONS)
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- get_candidate ---------------------------------------------------

    def test_get_candidate_reads_a_created_candidate(self):
        document_id, candidate_id = self._make_candidate()
        env = self._do(
            contract.ACTION_DOCUMENT_GET_CANDIDATE,
            document_id=document_id,
            candidate_id=candidate_id,
        )
        self.assertTrue(env["ok"])
        self.assertEqual(candidate_id, env["result"]["candidate"]["candidate_id"])

    def test_get_candidate_refuses_an_unknown_candidate(self):
        document_id = self._make_document()
        env = self._do(
            contract.ACTION_DOCUMENT_GET_CANDIDATE,
            document_id=document_id,
            candidate_id="cand:absent",
        )
        self.assertFalse(env["ok"])
        self.assertEqual("candidate_not_found", env["error"]["code"])

    def test_get_candidate_refuses_an_invalid_request(self):
        env = self._do(contract.ACTION_DOCUMENT_GET_CANDIDATE)
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- preview_document ------------------------------------------------

    def test_preview_document_reads_a_created_document(self):
        document_id = self._make_document()
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=document_id)
        self.assertTrue(env["ok"])
        self.assertIn("binding", env["result"])
        self.assertIn("evidence", env["result"])
        # A document with no candidate has no bound evidence yet, and the
        # preview says so rather than inventing an execution outcome.
        self.assertIsNone(env["result"]["evidence"])

    def test_preview_document_reports_evidence_for_a_bound_candidate(self):
        document_id, _ = self._make_candidate()
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=document_id)
        self.assertTrue(env["ok"])
        evidence = env["result"]["evidence"]
        self.assertIsNotNone(evidence)
        # Previewing is not executing: the read-model says so explicitly.
        self.assertFalse(evidence["execution_performed"])

    def test_preview_document_refuses_a_missing_document(self):
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id="doc:absent")
        self.assertFalse(env["ok"])
        self.assertEqual("document_not_found", env["error"]["code"])

    def test_preview_document_refuses_an_invalid_request(self):
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW)
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- what the registered set does and does not need -------------------

    def test_the_store_backed_reads_need_no_project_root(self):
        # Four of the six read the app-data store rather than the accepted
        # repository, so they have no ``project_not_open`` path at all. Only
        # ``get_document`` and ``get_tree`` are root-scoped.
        self.assertTrue(self._do(contract.ACTION_DOCUMENT_LIST)["ok"])
        document_id = self._make_document()
        self.assertTrue(
            self._do(
                contract.ACTION_DOCUMENT_LIST_VERSIONS, document_id=document_id
            )["ok"]
        )
        self.assertTrue(
            self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=document_id)["ok"]
        )
        self.assertIsNone(self.session.root)

    def test_dispatch_really_goes_through_the_registry(self):
        # The direct proof of the seam: replace a registry entry and watch the
        # registered handler answer. If the legacy chain still owned this
        # action, the spy would never be called.
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        seen = []
        real = boundary._get_document_result

        def spy(request, session):
            seen.append(request.get("action"))
            return real(request, session)

        with mock.patch.dict(
            boundary._BOUNDARY_HANDLERS._handlers,
            {contract.ACTION_GET_DOCUMENT: spy},
        ):
            env = self._do(contract.ACTION_GET_DOCUMENT, path="app/main.py")
        self.assertEqual(["get_document"], seen)
        self.assertTrue(env["ok"])
        self.assertEqual("main.py", env["result"]["name"])


_MEMORY_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
    {
        "event_type": "run_progress",
        "source_event_id": "e2",
        "payload": {},
        "paths": ["pkg/mod.py"],
        "decisions": [{"summary": "chose option A", "source_id": "d-1"}],
        "evidence": [
            {
                "kind": "artifact",
                "artifact_ref": "pkg/mod.py",
                "digest": "sha256:" + "a" * 64,
            }
        ],
    },
    {
        "event_type": "run_terminated",
        "source_event_id": "e3",
        "outcome": "completed",
        "payload": {},
    },
    {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}},
]


class BoundaryRegisteredMemoryReadTests(unittest.TestCase):
    """The six registered read-only Memory actions, exercised end to end.

    Like the document group, these go through ``handle_request`` so they
    exercise the registry: the architecture tests prove no legacy ``elif``
    claims them, and these prove they still answer exactly as they did.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.session = boundary.WorkspaceSession(store_base=self.base)
        session_payload = {
            "adapter": "smoke",
            "session_id": "s-1",
            "events": _MEMORY_EVENTS,
            "project": {"source_id": "p-1", "name": "P1"},
            "work_package": {"source_id": "w-1", "title": "W1"},
        }
        store, error, _ = memory.ingest_session(session_payload)
        self.assertIsNone(error)
        self.run_id = store["agent_run"]["id"]
        self.evidence_id = store["evidence"][0]["id"]
        self.assertIsNone(memory_store.save(self.base, self.run_id, store))

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(
            _workspace_request(action, **overrides), self.session
        )

    # -- get_memory_documents --------------------------------------------

    def test_get_memory_documents_projects_a_seeded_run(self):
        env = self._do(contract.ACTION_MEMORY_DOCUMENTS)
        self.assertTrue(env["ok"])
        self.assertEqual(1, env["result"]["run_count"])
        self.assertFalse(env["result"]["truncated"])

    def test_get_memory_documents_refuses_an_unsupported_document_type(self):
        env = self._do(contract.ACTION_MEMORY_DOCUMENTS, documents=["nope"])
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    def test_get_memory_documents_refuses_an_unsupported_origin(self):
        env = self._do(contract.ACTION_MEMORY_DOCUMENTS, origin="bogus")
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- get_memory_record -----------------------------------------------

    def test_get_memory_record_resolves_an_exact_evidence_record(self):
        env = self._do(
            contract.ACTION_MEMORY_RECORD,
            run_id=self.run_id,
            kind="evidence",
            record_id=self.evidence_id,
        )
        self.assertTrue(env["ok"])
        self.assertEqual(self.evidence_id, env["result"]["record_id"])
        self.assertEqual("evidence", env["result"]["kind"])

    def test_get_memory_record_refuses_an_unsupported_kind(self):
        env = self._do(
            contract.ACTION_MEMORY_RECORD,
            run_id=self.run_id,
            kind="nope",
            record_id=self.evidence_id,
        )
        self.assertFalse(env["ok"])
        self.assertEqual("memory_kind_not_supported", env["error"]["code"])

    def test_get_memory_record_refuses_an_absent_record(self):
        env = self._do(
            contract.ACTION_MEMORY_RECORD,
            run_id=self.run_id,
            kind="evidence",
            record_id="evidence:absent",
        )
        self.assertFalse(env["ok"])
        self.assertEqual("memory_record_not_found", env["error"]["code"])

    def test_get_memory_record_refuses_an_unknown_run(self):
        env = self._do(
            contract.ACTION_MEMORY_RECORD,
            run_id="run:nope",
            kind="evidence",
            record_id=self.evidence_id,
        )
        self.assertFalse(env["ok"])
        self.assertEqual("memory_run_not_found", env["error"]["code"])

    def test_get_memory_record_refuses_an_invalid_request(self):
        env = self._do(contract.ACTION_MEMORY_RECORD, kind="evidence")
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- search_memory ---------------------------------------------------

    def test_search_memory_returns_bounded_hits(self):
        env = self._do(contract.ACTION_MEMORY_SEARCH)
        self.assertTrue(env["ok"])
        self.assertGreater(env["result"]["hit_count"], 0)
        self.assertEqual("hrca-memory-query", env["result"]["generator"])

    def test_search_memory_refuses_an_invalid_limit(self):
        env = self._do(contract.ACTION_MEMORY_SEARCH, limit="ten")
        self.assertFalse(env["ok"])
        self.assertEqual("memory_query_invalid", env["error"]["code"])

    def test_search_memory_refuses_an_unknown_order(self):
        env = self._do(contract.ACTION_MEMORY_SEARCH, order="nope")
        self.assertFalse(env["ok"])
        self.assertEqual("memory_query_invalid", env["error"]["code"])

    # -- memory_resume ---------------------------------------------------

    def test_memory_resume_composes_over_the_run_set(self):
        env = self._do(contract.ACTION_MEMORY_RESUME)
        self.assertTrue(env["ok"])
        self.assertIn("runs_truncated", env["result"])

    # -- get_memory_history ----------------------------------------------

    def test_get_memory_history_lists_a_run(self):
        env = self._do(contract.ACTION_MEMORY_HISTORY, run_id=self.run_id)
        self.assertTrue(env["ok"])

    def test_get_memory_history_refuses_a_missing_run_id(self):
        env = self._do(contract.ACTION_MEMORY_HISTORY)
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- resolve_memory_effective ----------------------------------------

    def test_resolve_memory_effective_resolves_a_run(self):
        # Unlike the other five, this one requires a document type: it resolves
        # *one* effective document, so a run id alone does not name a request.
        env = self._do(
            contract.ACTION_MEMORY_EFFECTIVE,
            run_id=self.run_id,
            document_type="session_summary",
        )
        self.assertTrue(env["ok"])

    def test_resolve_memory_effective_refuses_a_missing_document_type(self):
        env = self._do(contract.ACTION_MEMORY_EFFECTIVE, run_id=self.run_id)
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    def test_resolve_memory_effective_refuses_a_non_string_document_type(self):
        env = self._do(
            contract.ACTION_MEMORY_EFFECTIVE, run_id=self.run_id, document_type=5
        )
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- what the registered set does and does not need -------------------

    def test_the_memory_reads_need_no_project_root(self):
        # All six read the app-data store, never the accepted repository, so
        # none of them has a ``project_not_open`` path.
        self.assertIsNone(self.session.root)
        for action, overrides in (
            (contract.ACTION_MEMORY_DOCUMENTS, {}),
            (contract.ACTION_MEMORY_SEARCH, {}),
            (contract.ACTION_MEMORY_RESUME, {}),
            (contract.ACTION_MEMORY_HISTORY, {"run_id": self.run_id}),
            (
                contract.ACTION_MEMORY_EFFECTIVE,
                {"run_id": self.run_id, "document_type": "session_summary"},
            ),
        ):
            with self.subTest(action=action):
                self.assertTrue(self._do(action, **overrides)["ok"])

    def test_memory_dispatch_really_goes_through_the_registry(self):
        # The direct proof of the seam, for this group: replace a registry
        # entry and watch the registered handler answer.
        seen = []
        real = boundary._search_memory_result

        def spy(request, session):
            seen.append(request.get("action"))
            return real(request, session)

        with mock.patch.dict(
            boundary._BOUNDARY_HANDLERS._handlers,
            {contract.ACTION_MEMORY_SEARCH: spy},
        ):
            env = self._do(contract.ACTION_MEMORY_SEARCH)
        self.assertEqual(["search_memory"], seen)
        self.assertTrue(env["ok"])

    def test_a_wrong_contract_version_is_refused_before_the_registry(self):
        env = boundary.handle_request(
            {
                "contract_version": "0.0.1",
                "action": contract.ACTION_MEMORY_DOCUMENTS,
            },
            self.session,
        )
        self.assertFalse(env["ok"])
        self.assertEqual("unknown_contract_version", env["error"]["code"])


_SCAN_FAMILY = (
    contract.ACTION_SCAN,
    contract.ACTION_READ,
    contract.ACTION_ANALYZE,
    contract.ACTION_INSPECT,
    contract.ACTION_PLAN,
)


class BoundaryRegisteredScanTests(unittest.TestCase):
    """The five scan synonyms, exercised end to end through the registry.

    ``scan``, ``read``, ``analyze``, ``inspect`` and ``plan`` name one
    pipeline. They are registered as five ordinary keys pointing at the one
    ``_scan_handler`` adapter, so these tests hold both halves: that each alias
    still answers, and that they answer *identically*.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _req(self, action, **overrides):
        req = contract.build_request(
            "cid-scan", action, FIXTURES, build_fixture_task(FIXTURES)
        )
        req.update(overrides)
        return req

    def _call(self, action, **overrides):
        return boundary.handle_request(self._req(action, **overrides), self.session)

    # -- every alias still runs the pipeline -----------------------------

    def test_every_scan_alias_runs_the_pipeline(self):
        for action in _SCAN_FAMILY:
            with self.subTest(action=action):
                env = self._call(action)
                self.assertTrue(env["ok"])
                self.assertEqual("cid-scan", env["correlation_id"])
                # scan -> plan -> report: the scanner evidence, the derived
                # plan inside the report, and the task identity it was run for.
                self.assertIn("evidence", env["result"])
                self.assertIn("report", env["result"])
                self.assertTrue(env["result"]["report"]["plan"])
                self.assertEqual("P3.1", env["result"]["task_id"])

    def test_the_five_aliases_are_synonyms(self):
        # The strongest form of "unchanged": a request that differs only in the
        # action verb produces a byte-identical result.
        baseline = self._call(contract.ACTION_SCAN)["result"]
        for action in _SCAN_FAMILY[1:]:
            with self.subTest(action=action):
                self.assertEqual(baseline, self._call(action)["result"])

    def test_every_scan_alias_refuses_an_empty_path(self):
        for action in _SCAN_FAMILY:
            with self.subTest(action=action):
                env = self._call(action, path="")
                self.assertFalse(env["ok"])
                self.assertEqual("invalid_request", env["error"]["code"])

    def test_every_scan_alias_refuses_a_mutating_task(self):
        for action in _SCAN_FAMILY:
            with self.subTest(action=action):
                task = dict(build_fixture_task(FIXTURES))
                task["allowed_actions"] = ["edit"]
                env = self._call(action, task=task)
                self.assertFalse(env["ok"])
                self.assertEqual("action_not_allowed", env["error"]["code"])

    def test_a_wrong_contract_version_is_refused_before_scan_dispatch(self):
        env = boundary.handle_request(
            self._req(contract.ACTION_SCAN, contract_version="0.0.1"), self.session
        )
        self.assertFalse(env["ok"])
        self.assertEqual("unknown_contract_version", env["error"]["code"])

    def test_a_non_dict_request_is_still_invalid_request(self):
        env = boundary.handle_request("not-a-dict", self.session)
        self.assertFalse(env["ok"])
        self.assertEqual("invalid_request", env["error"]["code"])

    # -- one handler, reached through the registry ------------------------

    def test_all_five_aliases_resolve_through_the_registry(self):
        for action in _SCAN_FAMILY:
            with self.subTest(action=action):
                self.assertIs(
                    boundary._scan_handler,
                    boundary._BOUNDARY_HANDLERS.resolve(action),
                )

    def test_the_five_aliases_share_one_handler(self):
        handlers = {
            boundary._BOUNDARY_HANDLERS.resolve(action) for action in _SCAN_FAMILY
        }
        self.assertEqual({boundary._scan_handler}, handlers)
        self.assertIs(boundary._scan_handler, boundary._scan_handler)

    def test_the_scan_handler_ignores_its_session(self):
        # A session double records every attribute access, call and item get.
        # The adapter must record none of them — it reads the request and
        # nothing else.
        request = self._req(contract.ACTION_SCAN)
        sentinel = mock.Mock()
        via_adapter = boundary._scan_handler(request, sentinel)
        self.assertEqual([], sentinel.mock_calls)
        self.assertEqual(boundary._scan_result(request), via_adapter)

    def test_scan_dispatch_really_goes_through_the_registry(self):
        seen = []
        real = boundary._scan_result

        def spy(request):
            seen.append(request.get("action"))
            return real(request)

        with mock.patch.object(boundary, "_scan_result", spy):
            env = self._call(contract.ACTION_ANALYZE)
        self.assertEqual(["analyze"], seen)
        self.assertTrue(env["ok"])

    def test_no_legacy_branch_owns_a_scan_alias(self):
        # The group branch is gone, not narrowed: every one of the five is
        # registered, so none may be reachable through the chain.
        for action in _SCAN_FAMILY:
            with self.subTest(action=action):
                self.assertIn(action, boundary._BOUNDARY_HANDLERS.actions())


if __name__ == "__main__":
    unittest.main()
