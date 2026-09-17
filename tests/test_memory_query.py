"""Tests for the M4.4/v1 bounded Search, Timeline and Resume read model.

The model answers across runs from normalized records and accepted projected
claims, adds no index and no storage, and reaches nothing the M4.3 read boundary
would refuse. These tests pin deterministic ranking and ordering, honest time and
acceptance semantics, exact evidence targets, and the privacy allowlist.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from hrca import boundary, client_core, contract, memory, memory_docs
from hrca import memory_query as q
from hrca import memory_store

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

# Timestamps chosen so ordering is exercised, including two on the same instant.
COMPLETED = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"prompt": _SECRET},
     "source_timestamp": "2026-09-17T10:00:00"},
    {"event_type": "run_progress", "source_event_id": "e2",
     "payload": {"tool_response": {"content": _SECRET}},
     "message": _SECRET,
     "paths": ["pkg/mod.py"],
     "code_entities": [
         {"path": "pkg/mod.py", "symbol": "Widget.render", "entity_kind": "symbol"},
         {"path": "pkg/mod.py"},
     ],
     "decisions": [{"summary": "chose the bounded path"}],
     "evidence": [{"kind": "artifact", "artifact_ref": "pkg/mod.py",
                   "digest": "sha256:" + "a" * 64}]},
    {"event_type": "run_terminated", "source_event_id": "e3", "outcome": "completed",
     "payload": {}, "source_timestamp": "2026-09-17T10:05:00"},
    {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}},
]

UNKNOWN = [
    {"event_type": "run_started", "source_event_id": "u1", "payload": {},
     "source_timestamp": "2026-09-17T10:05:00"},
    {"event_type": "run_terminated", "source_event_id": "u2",
     "outcome": "not-a-real-outcome", "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "u3", "payload": {}},
]

FAILED = [
    {"event_type": "run_started", "source_event_id": "f1", "payload": {},
     "source_timestamp": "not a timestamp"},
    {"event_type": "run_terminated", "source_event_id": "f2", "outcome": "failed",
     "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "f3", "payload": {}},
]


def store_for(events, sid="s-1", project=True, work_package=True, **extra):
    session = {"adapter": "smoke", "session_id": sid, "events": events}
    if project is True:
        session["project"] = {"source_id": "p-1", "name": "Project One"}
    elif isinstance(project, dict):
        session["project"] = project
    if work_package is True:
        session["work_package"] = {"source_id": "w-1", "title": "Bounded query work"}
    elif isinstance(work_package, dict):
        session["work_package"] = work_package
    session.update(extra)
    store, error, _ = memory.ingest_session(session)
    assert error is None, error
    return store


class QueryModelTestCase(unittest.TestCase):
    """Pure-model fixture: stores in memory, no boundary."""

    def corpus(self):
        return [store_for(COMPLETED, "s-c"), store_for(FAILED, "s-f")]

    def search(self, stores=None, **kwargs):
        result, error, unsupported = q.search(stores if stores is not None else self.corpus(), **kwargs)
        return result, error, unsupported

    def resume(self, stores=None):
        result, error = q.resume(stores if stores is not None else self.corpus())
        self.assertIsNone(error)
        return result


class DeterminismTests(QueryModelTestCase):
    def test_identical_input_is_byte_stable(self):
        first = q.render(self.search()[0])
        second = q.render(self.search()[0])
        self.assertEqual(first, second)

    def test_limit_is_enforced_and_reported(self):
        result, _err, _u = self.search(limit=2)
        self.assertEqual(2, len(result["results"]))
        self.assertTrue(result["truncated"])

    def test_a_larger_limit_is_clamped_not_rejected(self):
        result, error, _u = self.search(limit=10_000)
        self.assertIsNone(error)
        self.assertEqual(q.MAX_QUERY_RESULTS, result["limit"])

    def test_no_filters_returns_every_allowlisted_record(self):
        result, _err, _u = self.search()
        expected = sum(
            len(list(memory_docs.iter_records(s))) for s in self.corpus()
        )
        self.assertEqual(expected, result["hit_count"])

    def test_result_carries_the_model_identity(self):
        result, _err, _u = self.search()
        self.assertEqual(q.QUERY_SCHEMA_VERSION, result["schema_version"])
        self.assertEqual(q.QUERY_GENERATOR, result["generator"])
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, result["memory_schema_version"])


class FailClosedTests(QueryModelTestCase):
    def test_an_unknown_facet_is_refused(self):
        _r, error, _u = self.search(filters={"nope": ["x"]})
        self.assertEqual(q.REASON_UNKNOWN_FACET, error)

    def test_an_unusable_term_is_refused(self):
        for filters in ({"text": [""]}, {"text": [7]}, {"text": []}, {"text": "x" * 200}):
            with self.subTest(filters=filters):
                _r, error, _u = self.search(filters=filters)
                self.assertIn(error, (q.REASON_TERM_UNUSABLE, q.REASON_TOO_MANY_TERMS))

    def test_too_many_terms_are_refused(self):
        _r, error, _u = self.search(
            filters={"text": ["a"] * (q.MAX_QUERY_TERMS_PER_FACET + 1)}
        )
        self.assertEqual(q.REASON_TOO_MANY_TERMS, error)

    def test_an_unusable_date_is_refused(self):
        for term in ("17-09-2026", "2026/09/17", "yesterday"):
            with self.subTest(term=term):
                _r, error, _u = self.search(filters={"date": [term]})
                self.assertEqual(q.REASON_DATE_UNUSABLE, error)

    def test_an_unsupported_order_is_refused(self):
        _r, error, _u = self.search(order="chronological")
        self.assertEqual(q.REASON_ORDER_UNSUPPORTED, error)

    def test_an_unusable_limit_is_refused(self):
        for limit in (0, -1, True, "5"):
            with self.subTest(limit=limit):
                _r, error, _u = self.search(limit=limit)
                self.assertEqual(q.REASON_LIMIT_UNUSABLE, error)

    def test_a_non_list_corpus_is_refused(self):
        result, error, _u = q.search("nope")
        self.assertIsNone(result)
        self.assertEqual(q.REASON_NOT_STORES, error)


class FacetTests(QueryModelTestCase):
    def hits_for(self, **filters):
        result, error, _u = self.search(filters=filters)
        self.assertIsNone(error, filters)
        return result

    def test_project_facet(self):
        result = self.hits_for(project=["p-1"])
        self.assertTrue(result["hit_count"])
        self.assertIn("project", result["results"][0]["matched_facets"])

    def test_project_facet_matches_by_name(self):
        self.assertTrue(self.hits_for(project=["project one"])["hit_count"])

    def test_work_package_facet(self):
        self.assertTrue(self.hits_for(work_package=["bounded query"])["hit_count"])

    def test_run_state_facet(self):
        failed = self.hits_for(run_state=["failed"])
        self.assertTrue(failed["hit_count"])
        for hit in failed["results"]:
            self.assertEqual("failed", hit["run_state"])

    def test_date_facet(self):
        result = self.hits_for(date=["2026-09-17"])
        self.assertTrue(result["hit_count"])
        for hit in result["results"]:
            self.assertEqual("comparable", hit["time_status"])

    def test_file_facet_matches_change_set_paths_and_entity_paths(self):
        result = self.hits_for(file=["mod.py"])
        kinds = {hit["kind"] for hit in result["results"]}
        self.assertIn("change_set", kinds)
        self.assertIn("code_entity_link", kinds)

    def test_symbol_facet(self):
        result = self.hits_for(symbol=["widget.render"])
        self.assertEqual(1, result["hit_count"])
        self.assertEqual("code_entity_link", result["results"][0]["kind"])

    def test_decision_facet(self):
        result = self.hits_for(decision=["bounded path"])
        self.assertEqual(1, result["hit_count"])
        self.assertEqual("decision", result["results"][0]["kind"])

    def test_text_facet_matches_allowlisted_text_only(self):
        result = self.hits_for(text=["bounded"])
        self.assertTrue(result["hit_count"])
        for hit in result["results"]:
            self.assertTrue(hit["matched_fields"])

    def test_facets_are_combined_with_and_within_a_facet_with_or(self):
        both = self.hits_for(file=["mod.py"], run_state=["completed"])
        self.assertTrue(both["hit_count"])
        for hit in both["results"]:
            self.assertEqual("completed", hit["run_state"])
            self.assertIn("file", hit["matched_facets"])
        either = self.hits_for(file=["mod.py", "nothing-here"])
        self.assertGreaterEqual(either["hit_count"], both["hit_count"])

    def test_an_unsupported_facet_returns_nothing_and_says_so(self):
        result, error, unsupported = self.search(filters={"test_result": ["pass"]})
        self.assertIsNone(error)
        self.assertEqual([q.FACET_TEST_RESULT], unsupported)
        self.assertEqual(0, result["hit_count"])
        self.assertEqual([], result["results"])
        self.assertEqual([q.FACET_TEST_RESULT], result["unsupported_facets"])
        self.assertIn(q.FACET_TEST_RESULT, result["unsupported_facet_names"])
        self.assertTrue(
            any("no typed data" in text for text in result["limitations"])
        )

    def test_every_required_facet_is_declared(self):
        for facet in ("project", "work_package", "date", "run_state", "file",
                      "symbol", "decision", "test_result", "text"):
            with self.subTest(facet=facet):
                self.assertIn(facet, q.QUERY_FACETS)


class RankingTests(QueryModelTestCase):
    def test_ranking_is_total_and_deterministic(self):
        result, _err, _u = self.search()
        keys = [
            (h["run_id"], h["kind"], h["record_id"]) for h in result["results"]
        ]
        self.assertEqual(len(keys), len(set(keys)), "ranking must be a total order")
        again, _e, _u2 = self.search()
        self.assertEqual(
            [h["hit_id"] for h in result["results"]],
            [h["hit_id"] for h in again["results"]],
        )

    def test_more_matched_facets_rank_first(self):
        result, _err, _u = self.search(
            filters={"file": ["mod.py"], "run_state": ["completed"]}
        )
        counts = [h["rank"]["matched_facet_count"] for h in result["results"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_the_strongest_facet_is_reported(self):
        result, _err, _u = self.search(filters={"symbol": ["widget.render"]})
        hit = result["results"][0]
        self.assertEqual("symbol", hit["rank"]["strongest_facet"])

    def test_within_one_facet_count_ties_break_on_identity(self):
        result, _err, _u = self.search(filters={"file": ["mod.py"]})
        ordered = sorted(
            result["results"],
            key=lambda h: (h["run_id"], h["kind"], h["record_id"]),
        )
        self.assertEqual([h["hit_id"] for h in ordered],
                         [h["hit_id"] for h in result["results"]])


class TargetTests(QueryModelTestCase):
    def test_every_hit_carries_an_exact_typed_target(self):
        result, _err, _u = self.search()
        for hit in result["results"]:
            with self.subTest(hit=hit["hit_id"]):
                target = hit["target"]
                self.assertEqual(hit["run_id"], target["run_id"])
                self.assertEqual(hit["kind"], target["kind"])
                self.assertEqual(hit["record_id"], target["record_id"])
                self.assertIn(target["kind"], memory_docs.RECORD_VIEW_KINDS)

    def test_a_hit_target_resolves_through_the_read_boundary(self):
        stores = self.corpus()
        result, _err, _u = self.search(stores)
        base = tempfile.mkdtemp(prefix="hrca-m44-target-")
        try:
            session = boundary.WorkspaceSession(store_base=base)
            for store in stores:
                self.assertIsNone(
                    memory_store.save(base, store["agent_run"]["id"], store)
                )
            for hit in result["results"]:
                with self.subTest(hit=hit["hit_id"]):
                    target = hit["target"]
                    envelope = boundary.handle_request(
                        {
                            "contract_version": contract.CONTRACT_VERSION,
                            "correlation_id": "c1",
                            "action": contract.ACTION_MEMORY_RECORD,
                            "run_id": target["run_id"],
                            "kind": target["kind"],
                            "record_id": target["record_id"],
                        },
                        session,
                    )
                    self.assertTrue(envelope["ok"], envelope)
                    self.assertEqual(
                        target["record_id"], envelope["result"]["record_id"]
                    )
                    self.assertEqual(
                        target["run_id"], envelope["result"]["run_id"]
                    )
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_hit_provenance_reflects_the_matched_field(self):
        symbol, _e, _u = self.search(filters={"symbol": ["widget.render"]})
        self.assertEqual("observed", symbol["results"][0]["provenance"])
        decision, _e2, _u2 = self.search(filters={"decision": ["bounded path"]})
        self.assertEqual("reported", decision["results"][0]["provenance"])


class TimelineTests(QueryModelTestCase):
    def test_comparable_items_are_ordered_by_recorded_time(self):
        result, error, _u = self.search(order=q.ORDER_RECORDED_TIME)
        self.assertIsNone(error)
        times = [h["recorded_time"] for h in result["results"]]
        self.assertEqual(times, sorted(times))
        for hit in result["results"]:
            self.assertEqual("comparable", hit["time_status"])

    def test_ties_break_on_stable_identity(self):
        result, _err, _u = self.search(order=q.ORDER_RECORDED_TIME)
        tied = {}
        for hit in result["results"]:
            tied.setdefault(hit["recorded_time"], []).append(
                (hit["run_id"], hit["kind"], hit["record_id"])
            )
        for time, keys in tied.items():
            with self.subTest(time=time):
                if len(keys) > 1:
                    self.assertEqual(keys, sorted(keys))

    def test_missing_and_incomparable_times_are_unordered_and_labelled(self):
        result, _err, _u = self.search(order=q.ORDER_RECORDED_TIME)
        self.assertTrue(result["unordered"])
        statuses = {h["time_status"] for h in result["unordered"]}
        self.assertTrue(statuses <= {q.TIME_MISSING, q.TIME_INCOMPARABLE})
        self.assertIn(q.TIME_INCOMPARABLE, statuses)
        for hit in result["unordered"]:
            self.assertIsNone(hit["recorded_time"])

    def test_an_incomparable_time_is_never_placed_in_order(self):
        stores = [store_for(FAILED, "s-f")]
        result, error, _u = q.search(stores, order=q.ORDER_RECORDED_TIME)
        self.assertIsNone(error)
        ordered_ids = {h["hit_id"] for h in result["results"]}
        unordered_ids = {h["hit_id"] for h in result["unordered"]}
        self.assertEqual(set(), ordered_ids & unordered_ids)
        self.assertTrue(unordered_ids)

    def test_order_does_not_claim_causality(self):
        result, _err, _u = self.search(order=q.ORDER_RECORDED_TIME)
        self.assertTrue(
            any("no claim about causality" in text for text in result["limitations"])
        )
        self.assertTrue(
            any("explicitly unordered" in text for text in result["limitations"])
        )

    def test_relevance_order_keeps_every_hit_in_one_ranked_list(self):
        # Nothing is "out of order" in a relevance ranking, so the unordered
        # bucket is empty and each hit still reports its own time status.
        result, _err, _u = self.search(order=q.ORDER_RELEVANCE)
        self.assertEqual([], result["unordered"])
        self.assertEqual(result["hit_count"], len(result["results"]))
        statuses = {hit["time_status"] for hit in result["results"]}
        self.assertTrue(statuses <= {q.TIME_COMPARABLE, q.TIME_MISSING, q.TIME_INCOMPARABLE})
        self.assertIn(q.TIME_MISSING, statuses)

    def test_a_date_only_value_is_comparable(self):
        value, status = q.recorded_time("2026-09-17")
        self.assertEqual(("2026-09-17", q.TIME_COMPARABLE), (value, status))

    def test_an_exotic_time_is_incomparable(self):
        for value in ("2026-09-17T10:00:00Z", "2026-09-17T10:00:00.123", "yesterday", 7):
            with self.subTest(value=value):
                _v, status = q.recorded_time(value)
                self.assertEqual(q.TIME_INCOMPARABLE, status)

    def test_an_absent_time_is_missing(self):
        for value in (None, ""):
            with self.subTest(value=value):
                _v, status = q.recorded_time(value)
                self.assertEqual(q.TIME_MISSING, status)


class ResumeTests(QueryModelTestCase):
    def test_acceptance_is_unsupported_and_separate_from_completion(self):
        result = self.resume()
        self.assertEqual("unsupported", result["last_accepted_change"]["status"])
        self.assertIn("no acceptance or adoption decision",
                      result["last_accepted_change"]["reason"])
        completed = [r["state"] for r in result["completed_runs"]]
        self.assertEqual(["completed"], completed)
        # Completion is reported as completion, never as acceptance.
        self.assertNotEqual(
            result["last_accepted_change"]["status"], "accepted"
        )

    def test_the_current_baseline_is_not_verified(self):
        result = self.resume()
        self.assertEqual("not_verified", result["current_baseline"]["status"])
        self.assertIn("no baseline or revision identity",
                      result["current_baseline"]["reason"])

    def test_a_shared_work_package_is_a_single_named_goal(self):
        result = self.resume()
        self.assertEqual("named", result["current_goal"]["status"])
        self.assertEqual("Bounded query work", result["current_goal"]["title"])
        self.assertEqual("reported", result["current_goal"]["provenance"])

    def test_distinct_work_packages_are_ambiguous_not_guessed(self):
        stores = [
            store_for(COMPLETED, "s-1"),
            store_for(COMPLETED, "s-2",
                      work_package={"source_id": "w-2", "title": "Other work"}),
        ]
        goal = self.resume(stores)["current_goal"]
        self.assertEqual("ambiguous", goal["status"])
        self.assertEqual(2, len(goal["candidates"]))
        self.assertTrue(goal["limitations"])

    def test_runs_without_a_work_package_yield_an_unsupported_goal(self):
        stores = [store_for(COMPLETED, "s-1", work_package=False)]
        goal = self.resume(stores)["current_goal"]
        self.assertEqual("unsupported", goal["status"])
        self.assertTrue(goal["limitations"])

    def test_failed_and_unknown_outcome_runs_become_blockers(self):
        stores = [store_for(FAILED, "s-f"), store_for(UNKNOWN, "s-u")]
        result = self.resume(stores)
        states = {b.get("state") for b in result["blockers"]}
        self.assertIn("failed", states)
        self.assertIn("unknown_outcome", states)
        for blocker in result["blockers"]:
            with self.subTest(blocker=blocker["kind"]):
                self.assertTrue(blocker["target"])
                self.assertEqual(blocker["run_id"], blocker["target"]["run_id"])

    def test_a_refused_record_is_a_blocker_with_its_target(self):
        store = store_for(
            [
                {"event_type": "run_started", "source_event_id": "r1", "payload": {}},
                {"event_type": "run_terminated", "source_event_id": "r2",
                 "outcome": "nonsense", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "r3", "payload": {}},
            ],
            "s-r",
        )
        result = self.resume([store])
        kinds = {b["kind"] for b in result["blockers"]}
        self.assertIn("rejection", kinds)

    def test_next_actions_come_from_the_recorded_state(self):
        result = self.resume()
        self.assertTrue(result["next_actions"])
        for action in result["next_actions"]:
            with self.subTest(action=action["text"][:30]):
                self.assertEqual("inferred", action["provenance"])
                self.assertTrue(action["target"])

    def test_unverified_claims_are_reported_with_a_reason(self):
        result = self.resume()
        self.assertTrue(result["unverified_claims"])
        reasons = {c["reason"] for c in result["unverified_claims"]}
        self.assertIn(q.REASON_REPORTED_UNVERIFIED, reasons)
        for claim in result["unverified_claims"]:
            with self.subTest(claim=claim["claim_id"]):
                self.assertIn(claim["provenance"], docs_provenances())

    def test_a_dangling_claim_reference_is_unverified(self):
        store = store_for(COMPLETED, "s-d")
        store["agent_run"]["terminal_event_id"] = "event:not-in-this-run"
        result = self.resume([store])
        reasons = {c["reason"] for c in result["unverified_claims"]}
        self.assertIn(memory_docs.REASON_DANGLING_LINK, reasons)

    def test_an_unfinalized_snapshot_is_marked_stale(self):
        store = store_for(COMPLETED, "s-n")
        store["agent_run"]["finalized"] = False
        result = self.resume([store])
        run = result["runs"][0]
        self.assertTrue(run["stale"])
        self.assertEqual("unsupported", run["baseline_status"])

    def test_the_resume_is_byte_stable(self):
        self.assertEqual(q.render(self.resume()), q.render(self.resume()))

    def test_an_empty_corpus_resumes_without_invention(self):
        result = self.resume([])
        self.assertEqual(0, result["run_count"])
        self.assertEqual("unsupported", result["last_accepted_change"]["status"])
        self.assertEqual("not_verified", result["current_baseline"]["status"])
        self.assertEqual("unsupported", result["current_goal"]["status"])
        self.assertEqual([], result["blockers"])
        self.assertEqual([], result["next_actions"])

    def test_a_non_list_corpus_is_refused(self):
        self.assertEqual(q.REASON_NOT_STORES, q.resume("nope")[1])


def docs_provenances():
    return set(memory_docs.PROVENANCE_TAXONOMY)


class PrivacyTests(QueryModelTestCase):
    def _blobs(self):
        blobs = [q.render(self.search()[0])]
        blobs.append(q.render(self.search(order=q.ORDER_RECORDED_TIME)[0]))
        blobs.append(q.render(self.search(filters={"file": ["mod.py"]})[0]))
        blobs.append(q.render(self.search(filters={"test_result": ["x"]})[0]))
        blobs.append(q.render(self.resume()))
        return blobs

    def test_no_prohibited_value_reaches_a_response(self):
        for blob in self._blobs():
            for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", "sha256:",
                              '"payload"', "content_fingerprint", "privacy",
                              "existing_fingerprint"):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, blob)

    def test_match_metadata_exposes_fields_not_values(self):
        result, _e, _u = self.search(filters={"decision": ["bounded path"]})
        hit = result["results"][0]
        self.assertEqual(["summary"], hit["matched_fields"])
        self.assertNotIn("bounded path", json.dumps(hit["matched_fields"]))

    def test_query_errors_never_echo_caller_input(self):
        markers = (
            ("nope-" + _SECRET, {"filters": {"nope-" + _SECRET: ["x"]}}),
            (_PERSONAL, {"filters": {"date": [_PERSONAL]}}),
        )
        for marker, kwargs in markers:
            with self.subTest(marker=marker[:12]):
                _r, error, _u = self.search(**kwargs)
                self.assertIsNotNone(error)
                self.assertNotIn(marker, error)
                self.assertIn(error, {
                    q.REASON_UNKNOWN_FACET, q.REASON_DATE_UNUSABLE,
                })

    def test_the_pure_view_model_text_carries_no_prohibited_value(self):
        result, _e, _u = self.search(filters={"file": ["mod.py"]})
        rows = client_core.memory_hit_rows(result)
        resume_view = client_core.memory_resume_view(self.resume())
        blob = json.dumps(rows, sort_keys=True) + json.dumps(resume_view, sort_keys=True)
        self.assertTrue(blob)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", "sha256:",
                          "payload", "content_fingerprint"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_only_allowlisted_fields_are_searchable(self):
        # A term that exists only in a dropped field, a payload or content the
        # contract never stored must not match anything.
        for term in ("prompt", "tool_response", "transcript", "payload",
                     "fingerprint", "sk-ant"):
            with self.subTest(term=term):
                result, _e, _u = self.search(filters={"text": [term]})
                self.assertEqual(0, result["hit_count"])


class BoundaryTests(unittest.TestCase):
    """The 3.7.0 actions through the real boundary."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-m44-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        for sid, events in (("s-c", COMPLETED), ("s-f", FAILED)):
            store = store_for(events, sid)
            memory_store.save(self.base, store["agent_run"]["id"], store)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def call(self, payload, **overrides):
        request = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "c1",
        }
        request.update(payload)
        request.update(overrides)
        return boundary.handle_request(request, self.session)

    def test_search_is_reachable_and_bounded(self):
        result = self.call({"action": contract.ACTION_MEMORY_SEARCH})
        self.assertTrue(result["ok"])
        self.assertIn("hit_count", result["result"])
        self.assertFalse(result["result"]["runs_truncated"])

    def test_resume_is_reachable(self):
        result = self.call({"action": contract.ACTION_MEMORY_RESUME})
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["result"]["run_count"])

    def test_an_invalid_query_fails_closed(self):
        for payload in ({"filters": {"nope": ["x"]}},
                        {"order": "chronological"},
                        {"limit": 0},
                        {"filters": {"date": ["nope"]}}):
            with self.subTest(payload=payload):
                result = self.call({"action": contract.ACTION_MEMORY_SEARCH}, **payload)
                self.assertFalse(result["ok"])
                self.assertEqual("memory_query_invalid", result["error"]["code"])
                self.assertEqual(
                    contract.error_message("memory_query_invalid"),
                    result["error"]["message"],
                )

    def test_an_old_contract_version_is_rejected(self):
        for version in ("3.6.0", "3.5.0", "9.9.9"):
            with self.subTest(version=version):
                result = self.call(
                    {"action": contract.ACTION_MEMORY_SEARCH}, contract_version=version
                )
                self.assertEqual("unknown_contract_version", result["error"]["code"])

    def test_a_request_cannot_change_the_store_root(self):
        baseline = contract.dumps(self.call({"action": contract.ACTION_MEMORY_SEARCH}))
        for field, value in (("root", "/etc"), ("path", _PERSONAL),
                             ("base", "C:/Windows"), ("store_base", "/tmp"),
                             ("cwd", "/"), ("transcript_path", "t.jsonl")):
            with self.subTest(field=field):
                injected = self.call(
                    {"action": contract.ACTION_MEMORY_SEARCH, field: value}
                )
                self.assertEqual(baseline, contract.dumps(injected))

    def test_a_query_reports_the_same_corpus_the_read_actions_use(self):
        documents = self.call({"action": contract.ACTION_MEMORY_DOCUMENTS})
        search = self.call({"action": contract.ACTION_MEMORY_SEARCH})
        self.assertEqual(documents["result"]["run_count"],
                         search["result"]["run_count"])

    def test_the_boundary_response_carries_no_prohibited_value(self):
        for action in (contract.ACTION_MEMORY_SEARCH, contract.ACTION_MEMORY_RESUME):
            with self.subTest(action=action):
                blob = contract.dumps(self.call({"action": action}))
                for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh",
                                  "sha256:", '"payload"', "content_fingerprint"):
                    self.assertNotIn(forbidden, blob)

    def test_the_client_vocabulary_matches_the_query_model(self):
        # The desktop may not import the Memory seam, so its mirror is asserted
        # here rather than trusted.
        self.assertEqual(set(q.QUERY_FACETS), set(client_core.MEMORY_QUERY_FACETS))
        self.assertEqual(
            tuple(q.QUERY_ORDERS), tuple(client_core.MEMORY_QUERY_ORDERS)
        )
        self.assertEqual(
            tuple(q.UNSUPPORTED_FACETS), tuple(client_core.MEMORY_UNSUPPORTED_FACETS)
        )
        for label in ("project", "work_package", "run_state", "date", "file",
                      "symbol", "decision", "text", "test_result"):
            with self.subTest(facet=label):
                self.assertTrue(client_core.memory_facet_label(label))

    def test_the_client_builders_send_no_path(self):
        for request in (
            client_core.build_search_memory_request("c1"),
            client_core.build_memory_resume_request("c1"),
        ):
            with self.subTest(action=request["action"]):
                self.assertEqual(contract.CONTRACT_VERSION, request["contract_version"])
                for forbidden in ("root", "path", "base", "store_base", "cwd"):
                    self.assertNotIn(forbidden, request)


if __name__ == "__main__":
    unittest.main()
