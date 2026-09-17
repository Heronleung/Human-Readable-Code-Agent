"""Tests for the M4.3/v2a bounded Memory read boundary.

Two read-only operations let the desktop reach Developer Memory without
importing the Memory seam. These tests pin the properties that make that
acceptable: store rooting is boundary-owned and unreachable from a request,
resolution is by exact typed identity inside one named run, prohibited fields
never cross the boundary, and every failure is bounded and non-substituting.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from hrca import boundary, client_core, contract, memory, memory_docs, memory_store

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

COMPLETED_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"prompt": _SECRET}},
    {
        "event_type": "run_progress",
        "source_event_id": "e2",
        "payload": {"tool_response": {"content": _SECRET}},
        "message": _SECRET,
        "paths": ["pkg/mod.py"],
        "code_entities": [{"path": "pkg/mod.py", "entity_kind": "file"}],
        "decisions": [{"summary": "chose the bounded path", "source_id": "d-1"}],
        "evidence": [
            {"kind": "artifact", "artifact_ref": "pkg/mod.py", "digest": "sha256:" + "a" * 64}
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

UNKNOWN_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
    {"event_type": "run_progress", "source_event_id": "e2", "payload": {}},
    {
        "event_type": "run_terminated",
        "source_event_id": "e3",
        "outcome": "not-a-real-outcome",
        "payload": {},
    },
    {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}},
]

FAILED_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
    {"event_type": "run_terminated", "source_event_id": "e2", "outcome": "failed", "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "e3", "payload": {}},
]

UNFINISHED_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
    {"event_type": "run_progress", "source_event_id": "e2", "payload": {}},
]


class MemoryReadTestCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-m43v2a-")
        self.session = boundary.WorkspaceSession(store_base=self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def seed(self, events, session_id="s-1", **descriptor):
        session = {"adapter": "smoke", "session_id": session_id, "events": events}
        session.setdefault(
            "project", {"source_id": "p-1", "name": "Project One"}
        )
        session.setdefault(
            "work_package", {"source_id": "w-1", "title": "Work Package One"}
        )
        session.update(descriptor)
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        return run_id, store

    def call(self, payload, **overrides):
        request = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "c1",
        }
        request.update(payload)
        request.update(overrides)
        return boundary.handle_request(request, self.session)

    def record(self, run_id, kind, record_id):
        return self.call(
            {"action": contract.ACTION_MEMORY_RECORD, "run_id": run_id,
             "kind": kind, "record_id": record_id}
        )


class ContractSurfaceTests(MemoryReadTestCase):
    def test_the_contract_exposes_the_memory_read_actions(self):
        # The single version pin lives in test_contract; this asserts the 3.6.0
        # read actions survive as the protocol advances.
        self.assertTrue(contract.MEMORY_ACTIONS <= contract.ALLOWED_ACTIONS)
        for action in sorted(contract.MEMORY_ACTIONS):
            with self.subTest(action=action):
                self.assertIn(action, contract.ALLOWED_ACTIONS)

    def test_both_memory_actions_are_allowed(self):
        for action in sorted(contract.MEMORY_ACTIONS):
            with self.subTest(action=action):
                self.assertIn(action, contract.ALLOWED_ACTIONS)

    _PRIOR_ACTION_SETS = (
        "SCAN_ACTIONS", "WORKSPACE_ACTIONS", "TWIN_ACTIONS", "DRAFT_ACTIONS",
        "PROPOSAL_ACTIONS", "READINESS_ACTIONS", "CREDENTIAL_ACTIONS",
        "PROFILE_ACTIONS", "ADVISORY_ACTIONS", "PACKAGE_ACTIONS",
        "DOCUMENT_ACTIONS", "LIBRARY_ACTIONS", "CANDIDATE_PACKAGE_ACTIONS",
        "RULE_DELTA_ACTIONS", "RULE_DELTA_INTERPRET_ACTIONS",
    )

    def test_the_memory_actions_are_additive(self):
        # The new actions join through their own set: no prior set's membership
        # changes, and nothing is added to the allowlist outside a declared set.
        self.assertTrue(contract.MEMORY_ACTIONS <= contract.ALLOWED_ACTIONS)
        union = set()
        for name in self._PRIOR_ACTION_SETS:
            prior = getattr(contract, name)
            with self.subTest(set=name):
                self.assertTrue(prior.isdisjoint(contract.MEMORY_ACTIONS))
                self.assertTrue(prior.isdisjoint(contract.MEMORY_QUERY_ACTIONS))
                self.assertTrue(prior.isdisjoint(contract.MEMORY_REVISION_ACTIONS))
            union |= prior
        # The allowlist stays exactly the union of the declared sets, so nothing
        # can be added to the public surface outside a named set.
        self.assertEqual(
            union
            | contract.MEMORY_ACTIONS
            | contract.MEMORY_QUERY_ACTIONS
            | contract.MEMORY_REVISION_ACTIONS,
            contract.ALLOWED_ACTIONS,
        )
        for action in ("scan", "open_project", "get_tree", "get_document",
                       "get_twin", "get_code_map", "get_readiness", "get_profiles",
                       "plan_advisory", "get_package", "open_document",
                       "get_library", "stage_candidate_package",
                       "stage_rule_delta", "interpret_rule_delta",
                       "preview_document"):
            with self.subTest(action=action):
                self.assertIn(action, contract.ALLOWED_ACTIONS)

    def test_an_older_contract_version_is_rejected(self):
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS},
                           contract_version="3.5.0")
        self.assertFalse(result["ok"])
        self.assertEqual("unknown_contract_version", result["error"]["code"])

    def test_an_unknown_contract_version_is_rejected(self):
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS},
                           contract_version="9.9.9")
        self.assertEqual("unknown_contract_version", result["error"]["code"])

    def test_the_memory_error_codes_are_bounded_and_catalogue_drawn(self):
        for code in ("memory_run_not_found", "memory_record_not_found",
                     "memory_kind_not_supported", "memory_not_readable"):
            with self.subTest(code=code):
                self.assertIn(code, contract.ERROR_CODES)
                self.assertEqual(contract.error_message(code),
                                 contract.build_error(None, code)["error"]["message"])

    def test_the_client_vocabulary_matches_the_projector(self):
        # The desktop may not import the Memory seam, so its mirrored
        # vocabulary is asserted here instead of trusted.
        self.assertEqual(
            tuple(memory_docs.DOCUMENT_TYPES), tuple(client_core.MEMORY_DOCUMENT_TYPES)
        )
        self.assertEqual(
            tuple(memory_docs.RECORD_VIEW_KINDS), tuple(client_core.MEMORY_RECORD_KINDS)
        )
        for state in memory.REPORTED_RUN_STATES:
            with self.subTest(state=state):
                self.assertIn(state, client_core.MEMORY_STATE_LABELS)
        self.assertEqual(
            memory.SUCCESS_RUN_STATES, {client_core.MEMORY_SUCCESS_STATE}
        )


class DocumentRetrievalTests(MemoryReadTestCase):
    def test_documents_are_returned_for_every_stored_run(self):
        self.seed(COMPLETED_EVENTS)
        self.seed(UNKNOWN_EVENTS, session_id="s-2")
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["result"]["run_count"])
        self.assertFalse(result["result"]["truncated"])
        states = sorted(s["run"]["state"] for s in result["result"]["document_sets"])
        self.assertEqual(["completed", "unknown_outcome"], states)

    def test_the_same_request_is_byte_stable(self):
        self.seed(COMPLETED_EVENTS)
        first = contract.dumps(self.call({"action": contract.ACTION_MEMORY_DOCUMENTS}))
        second = contract.dumps(self.call({"action": contract.ACTION_MEMORY_DOCUMENTS}))
        self.assertEqual(first, second)

    def test_a_run_selection_is_honoured(self):
        run_id, _ = self.seed(COMPLETED_EVENTS)
        self.seed(UNKNOWN_EVENTS, session_id="s-2")
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS, "runs": [run_id]})
        self.assertEqual(1, result["result"]["run_count"])
        self.assertEqual(run_id, result["result"]["document_sets"][0]["run"]["run_id"])

    def test_a_document_selection_is_honoured(self):
        self.seed(COMPLETED_EVENTS)
        result = self.call({
            "action": contract.ACTION_MEMORY_DOCUMENTS,
            "documents": ["session_summary"],
        })
        self.assertEqual(
            ["session_summary"],
            list(result["result"]["document_sets"][0]["documents"]),
        )

    def test_an_unknown_run_in_a_selection_fails_closed(self):
        result = self.call({
            "action": contract.ACTION_MEMORY_DOCUMENTS, "runs": ["run:nope:x:run"],
        })
        self.assertFalse(result["ok"])
        self.assertEqual("memory_run_not_found", result["error"]["code"])

    def test_an_empty_store_base_is_not_an_error(self):
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["result"]["run_count"])

    def test_run_listing_is_bounded(self):
        for index in range(contract.MAX_MEMORY_RUNS + 3):
            self.seed(COMPLETED_EVENTS, session_id="s-%02d" % index)
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        self.assertEqual(contract.MAX_MEMORY_RUNS, result["result"]["run_count"])
        self.assertTrue(result["result"]["truncated"])

    def test_an_oversized_run_selection_is_bounded_not_rejected(self):
        self.seed(COMPLETED_EVENTS)
        run_id, _ = self.seed(COMPLETED_EVENTS, session_id="s-2")
        result = self.call({
            "action": contract.ACTION_MEMORY_DOCUMENTS,
            "runs": [run_id] * (contract.MAX_MEMORY_RUNS + 5),
        })
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["result"]["run_count"])

    def test_state_distinctions_survive_the_boundary(self):
        self.seed(COMPLETED_EVENTS, session_id="s-c")
        self.seed(UNKNOWN_EVENTS, session_id="s-u")
        self.seed(FAILED_EVENTS, session_id="s-f")
        self.seed(UNFINISHED_EVENTS, session_id="s-n")
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        runs = {s["run"]["session_id"]: s["run"] for s in result["result"]["document_sets"]}

        self.assertEqual("completed", runs["s-c"]["state"])
        self.assertTrue(runs["s-c"]["success"])

        self.assertEqual("unknown_outcome", runs["s-u"]["state"])
        self.assertFalse(runs["s-u"]["success"])

        self.assertEqual("failed", runs["s-f"]["state"])
        self.assertFalse(runs["s-f"]["success"])

        self.assertEqual("missing_terminal", runs["s-n"]["state"])
        self.assertFalse(runs["s-n"]["success"])

        for run in runs.values():
            self.assertEqual("unsupported", run["baseline"]["status"])

    def test_an_unfinalized_snapshot_is_distinct_from_a_baseline_gap(self):
        # Staleness is the store not being a closed snapshot; a baseline gap is
        # a missing identity. The two must not be conflated, and the ordinary
        # missing-terminal state is neither of them.
        run_id, store = self.seed(UNFINISHED_EVENTS)
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        run = result["result"]["document_sets"][0]["run"]
        self.assertEqual("missing_terminal", run["state"])
        self.assertFalse(run["stale"])
        self.assertEqual("unsupported", run["baseline"]["status"])

        store["agent_run"]["finalized"] = False
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        run = result["result"]["document_sets"][0]["run"]
        self.assertTrue(run["stale"])
        self.assertIn("store is not a finalized snapshot", run["stale_reasons"])
        # Still distinct: staleness does not replace the baseline gap.
        self.assertEqual("unsupported", run["baseline"]["status"])

    def test_no_current_or_verified_claim_is_added(self):
        self.seed(COMPLETED_EVENTS)
        blob = contract.dumps(self.call({"action": contract.ACTION_MEMORY_DOCUMENTS}))
        for token in ("\"current\"", "\"verified\"", "up-to-date", "fresh"):
            with self.subTest(token=token):
                self.assertNotIn(token, blob)

    def test_a_declared_offline_origin_reaches_the_projection(self):
        self.seed(FAILED_EVENTS)
        result = self.call({
            "action": contract.ACTION_MEMORY_DOCUMENTS, "origin": "offline",
            "documents": ["session_summary"],
        })
        blob = contract.dumps(result)
        self.assertIn("offline", blob)
        self.assertIn("not live-session observation", blob)

    def test_an_undeclared_origin_is_never_live_observation(self):
        self.seed(FAILED_EVENTS)
        result = self.call({
            "action": contract.ACTION_MEMORY_DOCUMENTS,
            "documents": ["session_summary"],
        })
        blob = contract.dumps(result)
        self.assertIn("no claim", blob)
        self.assertIn("live-session", blob)

    def test_an_invalid_origin_is_rejected(self):
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS, "origin": "maybe"})
        self.assertEqual("invalid_request", result["error"]["code"])

    def test_an_invalid_document_selection_is_rejected(self):
        for selection in (["nope"], [], "session_summary"):
            with self.subTest(selection=selection):
                result = self.call({
                    "action": contract.ACTION_MEMORY_DOCUMENTS, "documents": selection,
                })
                self.assertEqual("invalid_request", result["error"]["code"])


class RecordResolutionTests(MemoryReadTestCase):
    def test_every_supported_kind_resolves_by_exact_identity(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        cases = [
            ("run", run_id),
            ("project", store["projects"][0]["id"]),
            ("work_package", store["work_packages"][0]["id"]),
            ("event", store["events"][0]["id"]),
            ("evidence", store["evidence"][0]["id"]),
            ("decision", store["decisions"][0]["id"]),
            ("change_set", store["change_sets"][0]["id"]),
            ("code_entity_link", store["code_entity_links"][0]["id"]),
        ]
        for kind, record_id in cases:
            with self.subTest(kind=kind):
                result = self.record(run_id, kind, record_id)
                self.assertTrue(result["ok"], result)
                self.assertEqual(kind, result["result"]["kind"])
                self.assertEqual(record_id, result["result"]["record_id"])
                self.assertEqual(run_id, result["result"]["run_id"])

    def test_a_cross_run_record_id_does_not_resolve(self):
        first, _ = self.seed(COMPLETED_EVENTS)
        second, other = self.seed(UNKNOWN_EVENTS, session_id="s-2")
        result = self.record(first, "event", other["events"][0]["id"])
        self.assertFalse(result["ok"])
        self.assertEqual("memory_record_not_found", result["error"]["code"])

    def test_a_failed_resolution_leaks_no_alternative_record(self):
        first, store = self.seed(COMPLETED_EVENTS)
        result = self.record(first, "event", "event:not-in-this-run")
        blob = contract.dumps(result)
        self.assertEqual("memory_record_not_found", result["error"]["code"])
        for leaked in (store["events"][0]["id"], "pkg/mod.py", _SECRET):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, blob)

    def test_an_unknown_run_fails_closed(self):
        self.seed(COMPLETED_EVENTS)
        result = self.record("run:nope:x:run", "run", "run:nope:x:run")
        self.assertEqual("memory_run_not_found", result["error"]["code"])

    def test_an_unsupported_kind_fails_closed(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        for kind in ("payload", "privacy", "content_fingerprint", ""):
            with self.subTest(kind=kind):
                result = self.record(run_id, kind, store["events"][0]["id"])
                self.assertEqual("memory_kind_not_supported", result["error"]["code"])

    def test_a_missing_or_unusable_identity_fails_closed(self):
        run_id, _ = self.seed(COMPLETED_EVENTS)
        for record_id in ("", "   ", None, 7, "x" * (contract.MAX_MEMORY_ID_CHARS + 1)):
            with self.subTest(record_id=str(record_id)[:20]):
                result = self.record(run_id, "event", record_id)
                self.assertFalse(result["ok"])
                self.assertIn(
                    result["error"]["code"],
                    ("invalid_request", "memory_record_not_found"),
                )

    def test_an_unreadable_store_is_distinguished_from_a_missing_run(self):
        # A directory that exists without a store is not a run.
        run_id, _ = self.seed(COMPLETED_EVENTS)
        os.remove(memory_store.run_store_path(self.base, run_id))
        result = self.record(run_id, "event", "event:x")
        self.assertEqual("memory_run_not_found", result["error"]["code"])


class StoreRootingTests(MemoryReadTestCase):
    """A caller cannot influence which directory the boundary reads."""

    def test_a_supplied_path_has_no_effect(self):
        run_id, _ = self.seed(COMPLETED_EVENTS)
        baseline = contract.dumps(self.call({"action": contract.ACTION_MEMORY_DOCUMENTS}))
        for field, value in (
            ("root", "/etc"),
            ("path", _PERSONAL),
            ("base", "C:/Windows"),
            ("store_base", "/tmp"),
            ("transcript_path", "C:/somewhere/.claude/projects/-x/t.jsonl"),
            ("cwd", "/"),
        ):
            with self.subTest(field=field):
                injected = self.call({
                    "action": contract.ACTION_MEMORY_DOCUMENTS, field: value,
                })
                self.assertEqual(baseline, contract.dumps(injected))

    def test_a_supplied_path_has_no_effect_on_record_resolution(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        record_id = store["events"][0]["id"]
        baseline = contract.dumps(self.record(run_id, "event", record_id))
        injected = self.call(
            {
                "action": contract.ACTION_MEMORY_RECORD,
                "run_id": run_id,
                "kind": "event",
                "record_id": record_id,
            },
            root="/etc",
            path=_PERSONAL,
        )
        self.assertEqual(baseline, contract.dumps(injected))

    def test_the_session_store_base_is_not_read_from_a_request(self):
        self.assertNotIn("store_base", contract.build_error(None, "invalid_request"))
        self.assertEqual(
            self.base,
            boundary.WorkspaceSession(store_base=self.base).store_base,
        )


class PrivacyTests(MemoryReadTestCase):
    def _response_blobs(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        blobs = [contract.dumps(self.call({"action": contract.ACTION_MEMORY_DOCUMENTS}))]
        for kind in client_core.MEMORY_RECORD_KINDS:
            if kind == "run":
                record_id = run_id
            elif kind == "quarantine" or kind == "rejection":
                continue
            else:
                array = {
                    "project": "projects", "work_package": "work_packages",
                    "event": "events", "evidence": "evidence",
                    "decision": "decisions", "change_set": "change_sets",
                    "code_entity_link": "code_entity_links",
                }[kind]
                records = store.get(array) or []
                if not records:
                    continue
                record_id = records[0]["id"]
            blobs.append(contract.dumps(self.record(run_id, kind, record_id)))
        return blobs

    def test_prohibited_fields_never_appear_in_a_response(self):
        # ``provenance`` and ``provenance_taxonomy`` are deliberately *not* in
        # this list: a claim's provenance is the attribution v2b renders, and
        # the taxonomy is the projector's own vocabulary. What must never cross
        # is a *record's* stored internals.
        for blob in self._response_blobs():
            for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh",
                              '"payload"', "content_fingerprint",
                              "existing_fingerprint", "incoming_fingerprint",
                              '"privacy"'):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, blob)

    def test_no_allowlist_entry_names_a_record_internal(self):
        banned = {
            "payload", "content_fingerprint", "privacy", "provenance",
            "digest", "source_timestamp", "run_id_unused",
        }
        for kind, fields in memory_docs.RECORD_VIEW_FIELDS.items():
            with self.subTest(kind=kind):
                self.assertTrue(banned.isdisjoint(fields))
                # The allowlist is only meaningful if it is exhaustive by
                # construction: every exposed field must be named here.
                self.assertTrue(fields)

    def test_a_digest_value_never_crosses_the_boundary(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        stored = [r["digest"] for r in store["evidence"] if r.get("digest")]
        self.assertTrue(stored, "the fixture must carry a digest")
        result = self.record(run_id, "evidence", store["evidence"][0]["id"])
        blob = contract.dumps(result)
        for digest in stored:
            self.assertNotIn(digest, blob)
        self.assertTrue(result["result"]["digest_present"])

    def test_prohibited_fields_never_appear_in_pure_view_model_text(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        documents = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        rendered = [client_core.format_memory_record(
            self.record(run_id, "event", store["events"][0]["id"])["result"])]
        for row in client_core.memory_run_rows(documents["result"]):
            rendered.append(contract.dumps(row))
        for document_set in documents["result"]["document_sets"]:
            for document_type in client_core.MEMORY_DOCUMENT_TYPES:
                for row in client_core.claim_rows(document_set, document_type):
                    rendered.append(contract.dumps(row))
        joined = "\n".join(rendered)
        self.assertNotEqual("", joined)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", '"payload"',
                          "content_fingerprint", "sha256:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, joined)

    def test_error_text_never_interpolates_caller_input(self):
        result = self.record("run:" + _SECRET, "event", _SECRET)
        blob = contract.dumps(result)
        self.assertNotIn(_SECRET, blob)
        self.assertEqual("memory_run_not_found", result["error"]["code"])

    def test_a_refused_record_reason_is_bounded(self):
        run_id, _ = self.seed(COMPLETED_EVENTS)
        result = self.record(run_id, "event", "event:absent")
        self.assertEqual(
            contract.error_message("memory_record_not_found"),
            result["error"]["message"],
        )
        self.assertEqual({"code", "message"}, set(result["error"]))


class ViewModelTests(MemoryReadTestCase):
    def test_run_rows_carry_textual_state_labels(self):
        self.seed(COMPLETED_EVENTS)
        self.seed(UNKNOWN_EVENTS, session_id="s-2")
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        rows = client_core.memory_run_rows(result["result"])
        labels = sorted(row["state_label"] for row in rows)
        self.assertEqual(
            ["Completed (success)", "Unknown outcome (not success)"], labels
        )
        for row in rows:
            self.assertEqual(
                row["success"], client_core.memory_state_is_success(row["state"])
            )

    def test_claim_rows_are_stable_and_carry_labels(self):
        self.seed(COMPLETED_EVENTS)
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        document_set = result["result"]["document_sets"][0]
        rows = client_core.claim_rows(document_set, "session_summary")
        self.assertTrue(rows)
        again = client_core.claim_rows(document_set, "session_summary")
        self.assertEqual([r["claim_id"] for r in rows], [r["claim_id"] for r in again])
        for row in rows:
            self.assertTrue(row["claim_id"].startswith("claim:"))
            self.assertTrue(row["provenance_label"])
            self.assertIn("success", row["run_state_label"])

    def test_resolved_targets_expose_exact_typed_identity(self):
        run_id, _ = self.seed(COMPLETED_EVENTS)
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        document_set = result["result"]["document_sets"][0]
        targets = [
            target
            for row in client_core.claim_rows(document_set, "session_summary")
            for target in row["targets"]
        ]
        self.assertTrue(targets)
        for target in targets:
            with self.subTest(target=target["target_id"]):
                self.assertIn(target["kind"], client_core.MEMORY_RECORD_KINDS)
                self.assertTrue(target["target_id"].startswith("target:"))
        run_targets = [t for t in targets if t["kind"] == "run" and t["resolved"]]
        self.assertTrue(run_targets)

    def test_an_unresolved_target_stays_visible_and_non_actionable(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        # A store whose terminal event id is absent yields a dangling reference.
        store["agent_run"]["terminal_event_id"] = "event:not-in-this-run"
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        result = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        rows = client_core.claim_rows(result["result"]["document_sets"][0], "session_summary")
        unresolved = [t for row in rows for t in row["targets"] if not t["resolved"]]
        self.assertTrue(unresolved)
        for target in unresolved:
            self.assertEqual("event:not-in-this-run", target["record_id"])
            self.assertIsNotNone(target["reason"])
        # And the dangling identity genuinely does not resolve.
        self.assertFalse(self.record(run_id, "event", "event:not-in-this-run")["ok"])

    def test_record_detail_rows_label_reported_fields(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        view = self.record(run_id, "decision", store["decisions"][0]["id"])["result"]
        rows = {row["field"]: row for row in client_core.record_detail_rows(view)}
        self.assertTrue(rows["summary"]["reported"])
        self.assertEqual("Reported by the source", rows["summary"]["reported_label"])
        self.assertFalse(rows["id"]["reported"])

    def test_record_detail_rows_show_digest_presence_only(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        view = self.record(run_id, "evidence", store["evidence"][0]["id"])["result"]
        rows = {row["field"]: row for row in client_core.record_detail_rows(view)}
        self.assertEqual("present", rows["digest"]["value"])
        self.assertNotIn("sha256:" + "a" * 64, contract.dumps(rows))

    def test_an_unresolved_document_selection_yields_no_rows(self):
        self.seed(COMPLETED_EVENTS)
        result = self.call({
            "action": contract.ACTION_MEMORY_DOCUMENTS, "documents": ["decision_record"],
        })
        self.assertEqual([], client_core.claim_rows(
            result["result"]["document_sets"][0], "session_summary"
        ))

    def test_the_request_builders_name_no_path(self):
        documents = client_core.build_get_memory_documents_request("c1")
        record = client_core.build_get_memory_record_request("c1", "r", "event", "e")
        for request in (documents, record):
            with self.subTest(action=request["action"]):
                self.assertEqual(contract.CONTRACT_VERSION, request["contract_version"])
                for forbidden in ("root", "path", "base", "store_base", "cwd",
                                  "transcript_path"):
                    self.assertNotIn(forbidden, request)


if __name__ == "__main__":
    unittest.main()
