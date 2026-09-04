"""Tests for the headless local application boundary (P3.1)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest import mock

from hrca import boundary, contract, twin, twin_store
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


if __name__ == "__main__":
    unittest.main()
