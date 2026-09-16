"""Tests for the evidence-linked document projector (M4.3).

The projector reads normalized records only. These tests pin its authority
boundary: documents are byte-stable views, every material claim carries a
provenance label and a resolvable record identity, a source's own report can
never be upgraded to observed fact, dropped content cannot reappear, and a
store the projector does not understand is refused rather than half-projected.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import unittest

from hrca import memory
from hrca import memory_docs as docs

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

# A value that must never reappear through projection.
_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"


def _session(events, project=True, work_package=True):
    session = {"adapter": "smoke", "session_id": "s-1"}
    if project:
        session["project"] = {"source_id": "p-1", "name": "Project One"}
    if work_package:
        session["work_package"] = {"source_id": "w-1", "title": "Work Package One"}
    session["events"] = events
    return session


COMPLETED_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"k": "v"}},
    {
        "event_type": "run_progress",
        "source_event_id": "e2",
        "payload": {"k": "v"},
        "code_entities": [{"path": "pkg/mod.py", "entity_kind": "file"}],
        "decisions": [{"summary": "chose the bounded path", "source_id": "d-1"}],
    },
    {
        "event_type": "run_progress",
        "source_event_id": "e3",
        "payload": {"k": "v"},
        "paths": ["pkg/mod.py"],
        "evidence": [
            {"kind": "artifact", "artifact_ref": "pkg/mod.py", "digest": "sha256:" + "a" * 64}
        ],
    },
    {
        "event_type": "run_terminated",
        "source_event_id": "e4",
        "outcome": "completed",
        "payload": {"k": "v"},
    },
    {"event_type": "stream_ended", "source_event_id": "e5", "payload": {"k": "v"}},
]

UNKNOWN_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"k": "v"}},
    {"event_type": "run_progress", "source_event_id": "e2", "payload": {"k": "v"}},
    {
        "event_type": "run_terminated",
        "source_event_id": "e3",
        "outcome": "not-a-real-outcome",
        "payload": {"k": "v"},
    },
    {"event_type": "stream_ended", "source_event_id": "e4", "payload": {"k": "v"}},
]

FAILED_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"k": "v"}},
    {
        "event_type": "run_terminated",
        "source_event_id": "e2",
        "outcome": "failed",
        "payload": {"k": "v"},
    },
    {"event_type": "stream_ended", "source_event_id": "e3", "payload": {"k": "v"}},
]


def _store(events):
    store, error, _ = memory.ingest_session(_session(events))
    assert error is None, error
    return store


def _claims(document_set):
    for document in document_set["documents"].values():
        for claim in document["claims"]:
            yield document["document_type"], claim


class DeterminismTests(unittest.TestCase):
    def test_reprojecting_identical_input_is_byte_stable(self):
        store = _store(COMPLETED_EVENTS)
        first = docs.render(docs.project_store(store)[0])
        second = docs.render(docs.project_store(store)[0])
        self.assertEqual(first, second)

    def test_a_reloaded_store_projects_identically(self):
        store = _store(COMPLETED_EVENTS)
        reloaded = json.loads(memory.dumps(store))
        self.assertEqual(
            docs.render(docs.project_store(store)[0]),
            docs.render(docs.project_store(reloaded)[0]),
        )

    def test_every_document_type_is_produced(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        self.assertEqual(
            sorted(docs.DOCUMENT_TYPES), sorted(document_set["documents"])
        )

    def test_claim_identities_are_unique_and_stable(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        ids = [claim["id"] for _doc, claim in _claims(document_set)]
        self.assertEqual(len(ids), len(set(ids)))
        again = [claim["id"] for _doc, claim in _claims(docs.project_store(_store(COMPLETED_EVENTS))[0])]
        self.assertEqual(ids, again)

    def test_every_claim_carries_a_known_provenance_label(self):
        for document_type, claim in _claims(docs.project_store(_store(COMPLETED_EVENTS))[0]):
            with self.subTest(claim=claim["id"]):
                self.assertIn(claim["provenance"], docs.PROVENANCE_TAXONOMY)
                self.assertTrue(claim["statement"])

    def test_every_link_resolves_to_a_stored_record(self):
        store = _store(COMPLETED_EVENTS)
        index = docs._index(store)
        for _doc, claim in _claims(docs.project_store(store)[0]):
            for link in claim["links"]:
                with self.subTest(claim=claim["id"], link=link):
                    self.assertIn(link["id"], index.get(link["kind"], {}))

    def test_the_link_set_is_not_empty(self):
        # At least one claim must actually point at records, or "evidence-linked"
        # would be an empty promise.
        linked = [
            claim
            for _doc, claim in _claims(docs.project_store(_store(COMPLETED_EVENTS))[0])
            if claim["links"]
        ]
        self.assertTrue(linked)


class ProvenanceAuthorityTests(unittest.TestCase):
    def test_a_source_report_is_never_upgraded_to_observed(self):
        store = _store(COMPLETED_EVENTS)
        reported = [
            claim
            for _doc, claim in _claims(docs.project_store(store)[0])
            if claim["provenance"] == docs.PROV_REPORTED
        ]
        self.assertTrue(reported, "the decision summary must be reported, not observed")
        for claim in reported:
            with self.subTest(claim=claim["id"]):
                self.assertNotEqual(docs.PROV_OBSERVED, claim["provenance"])

    def test_the_decision_summary_is_reported_not_observed(self):
        store = _store(COMPLETED_EVENTS)
        document = docs.project_store(store)[0]["documents"][docs.DOC_DECISION_RECORD]
        claims = {c["id"]: c for c in document["claims"]}
        claim = claims["claim:decision_record:decision:0"]
        self.assertEqual(docs.PROV_REPORTED, claim["provenance"])
        self.assertIn("chose the bounded path", claim["statement"])
        self.assertTrue(claim["limitations"])

    def test_user_confirmed_is_declared_and_unsupported(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        self.assertIn(docs.PROV_USER_CONFIRMED, document_set["unsupported_provenance"])
        emitted = {
            claim["provenance"] for _doc, claim in _claims(document_set)
        }
        # Declared in the taxonomy, unreachable in schema 1.0.0: the projector
        # must never mint a label no record type can justify.
        self.assertNotIn(docs.PROV_USER_CONFIRMED, emitted)

    def test_terminal_state_is_observed(self):
        store = _store(COMPLETED_EVENTS)
        document = docs.project_store(store)[0]["documents"][docs.DOC_SESSION_SUMMARY]
        claims = {c["id"]: c for c in document["claims"]}
        claim = claims["claim:session_summary:terminal_state"]
        self.assertEqual(docs.PROV_OBSERVED, claim["provenance"])
        self.assertIn("completed", claim["statement"])
        self.assertTrue(claim["links"])

    def test_the_taxonomy_is_described(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        self.assertEqual(
            sorted(docs.PROVENANCE_TAXONOMY),
            sorted(document_set["provenance_taxonomy"]),
        )


class TerminalStateTests(unittest.TestCase):
    def test_completed_and_unknown_outcome_are_distinguishable(self):
        completed = docs.project_store(_store(COMPLETED_EVENTS))[0]
        unknown = docs.project_store(_store(UNKNOWN_EVENTS))[0]

        self.assertEqual("completed", completed["run"]["state"])
        self.assertTrue(completed["run"]["success"])
        self.assertIn("observed", docs.PROVENANCE_TAXONOMY)

        self.assertEqual("unknown_outcome", unknown["run"]["state"])
        self.assertFalse(unknown["run"]["success"])

        self.assertNotEqual(docs.render(completed), docs.render(unknown))

    def test_a_non_success_run_carries_its_source_limitation(self):
        unknown = docs.project_store(_store(UNKNOWN_EVENTS))[0]
        summary = unknown["documents"][docs.DOC_SESSION_SUMMARY]
        claim = {c["id"]: c for c in summary["claims"]}[
            "claim:session_summary:terminal_state"
        ]
        self.assertTrue(claim["limitations"])
        self.assertTrue(
            any("never success" in text for text in claim["limitations"])
        )

    def test_a_completed_run_carries_no_state_limitation(self):
        completed = docs.project_store(_store(COMPLETED_EVENTS))[0]
        summary = completed["documents"][docs.DOC_SESSION_SUMMARY]
        claim = {c["id"]: c for c in summary["claims"]}[
            "claim:session_summary:terminal_state"
        ]
        self.assertEqual([], claim["limitations"])

    def test_unknown_outcome_reports_the_refusal_as_an_issue(self):
        issues = docs.project_store(_store(UNKNOWN_EVENTS))[0]["documents"][
            docs.DOC_ISSUES_AND_ACTIONS
        ]
        reasons = [c["statement"] for c in issues["claims"]]
        self.assertTrue(any("unrecognized outcome" in text for text in reasons))

    def test_the_success_claim_never_softens_a_failure(self):
        issues = docs.project_store(_store(UNKNOWN_EVENTS))[0]["documents"][
            docs.DOC_ISSUES_AND_ACTIONS
        ]
        claim = {c["id"]: c for c in issues["claims"]}[
            "claim:issues_and_actions:terminal_limitation"
        ]
        self.assertIn("not recorded as success", claim["statement"])


class OfflineEvidenceTests(unittest.TestCase):
    def test_a_declared_offline_origin_stays_visible(self):
        document_set = docs.project_store(
            _store(FAILED_EVENTS), evidence_origin="offline"
        )[0]
        claim = {c["id"]: c for c in document_set["documents"][docs.DOC_SESSION_SUMMARY]["claims"]}[
            "claim:session_summary:capture_origin"
        ]
        self.assertIn("offline", claim["statement"])
        self.assertEqual(docs.PROV_REPORTED, claim["provenance"])
        self.assertTrue(
            any("not live-session observation" in text for text in claim["limitations"])
        )

    def test_an_undeclared_origin_is_never_read_as_live(self):
        document_set = docs.project_store(_store(FAILED_EVENTS))[0]
        claim = {c["id"]: c for c in document_set["documents"][docs.DOC_SESSION_SUMMARY]["claims"]}[
            "claim:session_summary:capture_origin"
        ]
        self.assertIn("not recorded", claim["statement"])
        self.assertTrue(
            any("no claim" in text and "live-session" in text for text in claim["limitations"])
        )

    def test_a_typed_failure_is_not_success(self):
        document_set = docs.project_store(
            _store(FAILED_EVENTS), evidence_origin="offline"
        )[0]
        self.assertEqual(memory.RUN_FAILED, document_set["run"]["state"])
        self.assertFalse(document_set["run"]["success"])

    def test_an_invalid_origin_is_refused(self):
        store, error, _ = docs.project_store(
            _store(COMPLETED_EVENTS), evidence_origin="maybe"
        )
        self.assertIsNone(store)
        self.assertEqual(docs.REASON_ORIGIN_UNSUPPORTED, error)


class RedactionTests(unittest.TestCase):
    def _polluted_store(self):
        events = [
            {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
            {
                "event_type": "run_progress",
                "source_event_id": "e2",
                "payload": {"prompt": _SECRET, "tool_response": {"content": _SECRET}},
                "message": _SECRET,
                "command": _SECRET,
                "paths": [_PERSONAL],
                "code_entities": [{"path": _PERSONAL, "entity_kind": "file"}],
                "decisions": [{"summary": _SECRET}],
                "evidence": [
                    {"kind": "artifact", "artifact_ref": _PERSONAL, "content": _SECRET}
                ],
            },
            {
                "event_type": "run_terminated",
                "source_event_id": "e3",
                "outcome": "completed",
                "payload": {"error": _SECRET},
            },
            {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}},
        ]
        return _store(events)

    def test_no_secret_or_personal_path_survives_projection(self):
        document_set = docs.project_store(self._polluted_store())[0]
        blob = docs.render(document_set)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_an_excluded_path_is_absent_from_the_change_record(self):
        document_set = docs.project_store(self._polluted_store())[0]
        blob = docs.render(document_set["documents"][docs.DOC_CHANGE_RECORD])
        self.assertNotIn(".ssh", blob)

    def test_a_content_digest_is_reported_as_present_but_never_as_a_value(self):
        store = _store(COMPLETED_EVENTS)
        document_set = docs.project_store(store)[0]
        blob = docs.render(document_set)
        # The stored evidence carries a digest; the document must not reproduce it.
        stored_digests = [r.get("digest") for r in store["evidence"] if r.get("digest")]
        self.assertTrue(stored_digests, "the fixture must carry a digest")
        for digest in stored_digests:
            self.assertNotIn(digest, blob)

    def test_adapter_payload_detail_is_not_projected(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        blob = docs.render(document_set)
        self.assertNotIn('"payload"', blob)
        self.assertIn(docs.PAYLOAD_NOT_PROJECTED, blob)


class FailClosedTests(unittest.TestCase):
    def test_a_non_mapping_input_is_refused(self):
        self.assertEqual(docs.REASON_NOT_A_STORE, docs.project_store("nope")[1])

    def test_an_unsupported_schema_version_is_refused(self):
        store = _store(COMPLETED_EVENTS)
        store["schema_version"] = "9.9.9"
        document_set, error, _ = docs.project_store(store)
        self.assertIsNone(document_set)
        self.assertEqual(docs.REASON_UNSUPPORTED_VERSION, error)

    def test_a_foreign_generator_is_refused(self):
        store = _store(COMPLETED_EVENTS)
        store["generator"] = "something-else"
        self.assertEqual(docs.REASON_UNSUPPORTED_GENERATOR, docs.project_store(store)[1])

    def test_a_store_without_a_run_is_refused(self):
        store = _store(COMPLETED_EVENTS)
        del store["agent_run"]
        self.assertEqual(docs.REASON_NO_RUN, docs.project_store(store)[1])

    def test_an_unknown_document_type_is_refused(self):
        store, error, _ = docs.project_store(
            _store(COMPLETED_EVENTS), document_types=["nope"]
        )
        self.assertIsNone(store)
        self.assertIn("unknown document type", error)

    def test_a_dangling_reference_is_visible_not_dropped(self):
        store = _store(COMPLETED_EVENTS)
        store["agent_run"]["terminal_event_id"] = "event:not-in-this-store"
        document_set = docs.project_store(store)[0]
        claim = {c["id"]: c for c in document_set["documents"][docs.DOC_SESSION_SUMMARY]["claims"]}[
            "claim:session_summary:terminal_state"
        ]
        reasons = [u["reason"] for u in claim["unresolved"]]
        self.assertEqual([docs.REASON_DANGLING_LINK], reasons)

    def test_a_selected_subset_of_documents_is_honoured(self):
        document_set = docs.project_store(
            _store(COMPLETED_EVENTS), document_types=[docs.DOC_DECISION_RECORD]
        )[0]
        self.assertEqual([docs.DOC_DECISION_RECORD], list(document_set["documents"]))


class StaleAndBaselineTests(unittest.TestCase):
    def test_a_finalized_store_is_not_stale(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        self.assertFalse(document_set["run"]["stale"])
        self.assertEqual([], document_set["run"]["stale_reasons"])

    def test_an_open_store_is_marked_stale(self):
        store = _store(COMPLETED_EVENTS)
        store["agent_run"]["finalized"] = False
        document_set = docs.project_store(store)[0]
        self.assertTrue(document_set["run"]["stale"])
        self.assertIn(docs.REASON_NOT_FINALIZED, document_set["run"]["stale_reasons"])
        claim = {c["id"]: c for c in document_set["documents"][docs.DOC_SESSION_SUMMARY]["claims"]}[
            "claim:session_summary:staleness"
        ]
        self.assertIn("stale", claim["statement"])
        self.assertTrue(claim["limitations"])

    def test_the_baseline_is_explicitly_unsupported(self):
        document_set = docs.project_store(_store(COMPLETED_EVENTS))[0]
        self.assertEqual("unsupported", document_set["run"]["baseline"]["status"])
        claim = {c["id"]: c for c in document_set["documents"][docs.DOC_SESSION_SUMMARY]["claims"]}[
            "claim:session_summary:baseline"
        ]
        self.assertIn("not recorded", claim["statement"])
        self.assertTrue(claim["limitations"])

    def test_an_unstarted_run_projects_without_invention(self):
        store, error, _ = memory.ingest_session(_session([]))
        self.assertIsNone(error)
        document_set = docs.project_store(store)[0]
        self.assertEqual(memory.RUN_MISSING_TERMINAL, document_set["run"]["state"])
        self.assertFalse(document_set["run"]["success"])


class BoundedSetTests(unittest.TestCase):
    def test_an_ordered_set_projects_one_document_set_per_run(self):
        first = _store(COMPLETED_EVENTS)
        second = _store(UNKNOWN_EVENTS)
        second["agent_run"]["id"] = "run:smoke:s-2:run"
        sets, error, report = docs.project_stores([first, second])
        self.assertIsNone(error)
        self.assertEqual(2, report["documents"])
        self.assertEqual(2, len(sets))
        self.assertNotEqual(sets[0]["run"]["state"], sets[1]["run"]["state"])

    def test_a_set_with_one_bad_run_fails_closed(self):
        sets, error, _ = docs.project_stores([_store(COMPLETED_EVENTS), "nope"])
        self.assertIsNone(sets)
        self.assertIn(docs.REASON_NOT_A_STORE, error)

    def test_rendering_a_set_is_byte_stable_and_ordered(self):
        first = _store(COMPLETED_EVENTS)
        second = _store(UNKNOWN_EVENTS)
        second["agent_run"]["id"] = "run:smoke:s-2:run"
        sets, error, _ = docs.project_stores([first, second])
        self.assertIsNone(error)
        envelope = json.loads(docs.render_sets(sets))
        self.assertEqual(2, envelope["set_count"])
        self.assertEqual(docs.DOCUMENT_SCHEMA_VERSION, envelope["schema_version"])
        self.assertEqual(docs.DOCUMENT_GENERATOR, envelope["generator"])
        self.assertEqual(
            [s["run"]["run_id"] for s in envelope["document_sets"]],
            [sets[0]["run"]["run_id"], sets[1]["run"]["run_id"]],
            "the caller's order is preserved and never re-sorted",
        )
        self.assertEqual(docs.render_sets(sets), docs.render_sets(sets))

    def test_no_clock_claim_is_made(self):
        # Schema 1.0.0 carries no reliable run clock, so the projector must not
        # derive or repeat a date: a "daily" bucket would be invented inference.
        stamped = [dict(event, source_timestamp="2026-09-17T00:00:00Z")
                   for event in COMPLETED_EVENTS]
        store, error, _ = memory.ingest_session(_session(stamped))
        self.assertIsNone(error)
        self.assertEqual("2026-09-17T00:00:00Z", store["agent_run"]["first_source_timestamp"])
        blob = docs.render(docs.project_store(store)[0])
        self.assertNotIn("2026-09-17", blob)
        self.assertNotIn("source_timestamp", blob)


class BoundaryTests(unittest.TestCase):
    def test_the_projector_imports_only_the_domain(self):
        with open(os.path.join(_SRC, "memory_docs.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
        self.assertTrue(
            imported.isdisjoint(
                {"memory_store", "claude_code_hooks", "hook_capture", "os",
                 "subprocess", "datetime", "time"}
            ),
            "the projector reaches beyond normalized records: %s" % sorted(imported),
        )

    def test_the_projector_never_opens_a_file(self):
        with open(os.path.join(_SRC, "memory_docs.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", called)

    def test_projection_does_not_mutate_the_store(self):
        store = _store(COMPLETED_EVENTS)
        before = memory.dumps(store)
        docs.project_store(store)
        self.assertEqual(before, memory.dumps(store))

    def test_a_deep_copied_store_projects_identically(self):
        store = _store(COMPLETED_EVENTS)
        self.assertEqual(
            docs.render(docs.project_store(store)[0]),
            docs.render(docs.project_store(copy.deepcopy(store))[0]),
        )


if __name__ == "__main__":
    unittest.main()
