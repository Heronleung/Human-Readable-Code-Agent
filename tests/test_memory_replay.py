"""Replay-reducer contract tests for Developer Memory (M4.1).

These tests exercise the per-run single writer and the typed transition rules:
monotonic ingest order, idempotent redelivery, conflict quarantine, absorbing
terminal states, and the explicit non-success outcomes (missing terminal,
unknown outcome, unsupported event type, invalid transition).

Terminal state is owned only by a validated typed transition. Nothing here
asserts that a run completes because a message, a command, a timestamp order or
an agent narrative says so.
"""

from __future__ import annotations

import unittest

from hrca import memory as m


def _open(session_id: str = "s-1"):
    store, err = m.empty_store("fixture", session_id)
    assert err is None, err
    return store


def _apply(store, event):
    return m.apply_event(store, event)


class ReducerStateTests(unittest.TestCase):
    def test_a_new_store_is_unstarted_and_not_finalized(self):
        store = _open()
        self.assertEqual(m.run_state(store), m.RUN_UNSTARTED)
        self.assertFalse(store["agent_run"]["finalized"])
        self.assertEqual(store["agent_run"]["ingest_sequence"], 0)

    def test_a_finalized_unstarted_run_is_missing_terminal(self):
        store = m.finalize(_open())
        self.assertEqual(m.run_state(store), m.RUN_MISSING_TERMINAL)
        self.assertTrue(store["agent_run"]["finalized"])

    def test_finalize_is_idempotent(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        once = m.dumps(m.finalize(store))
        twice = m.dumps(m.finalize(store))
        self.assertEqual(once, twice)

    def test_run_state_is_always_a_reported_state_after_finalize(self):
        store = m.finalize(_open())
        self.assertIn(m.run_state(store), m.REPORTED_RUN_STATES)


class TerminalStateTests(unittest.TestCase):
    """Each terminal outcome is distinct, and only ``completed`` is success."""

    def _terminated(self, outcome):
        store = _open(outcome or "unknown")
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_terminated", "source_event_id": "e-2",
                       "outcome": outcome})
        return m.finalize(store)

    def test_the_four_typed_outcomes_are_distinct(self):
        states = {}
        for outcome in ("completed", "failed", "cancelled", "blocked"):
            states[outcome] = m.run_state(self._terminated(outcome))
        self.assertEqual(len(set(states.values())), 4)
        self.assertEqual(states["completed"], m.RUN_COMPLETED)
        self.assertEqual(states["failed"], m.RUN_FAILED)
        self.assertEqual(states["cancelled"], m.RUN_CANCELLED)
        self.assertEqual(states["blocked"], m.RUN_BLOCKED)

    def test_only_completed_is_success(self):
        for outcome in ("completed", "failed", "cancelled", "blocked"):
            store = self._terminated(outcome)
            with self.subTest(outcome=outcome):
                self.assertEqual(m.is_success(store), outcome == "completed")

    def test_an_unknown_outcome_is_explicit_and_never_success(self):
        store = self._terminated("abandoned")
        self.assertEqual(m.run_state(store), m.RUN_UNKNOWN_OUTCOME)
        self.assertFalse(m.is_success(store))
        self.assertIn(
            m.REASON_UNKNOWN_OUTCOME, [r["reason"] for r in store["rejections"]]
        )

    def test_a_missing_terminal_is_explicit_and_never_success(self):
        store = _open("missing")
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2"})
        store = m.finalize(store)
        self.assertEqual(m.run_state(store), m.RUN_MISSING_TERMINAL)
        self.assertFalse(m.is_success(store))

    def test_a_terminal_run_ignores_a_later_narrative(self):
        """A trailing progress message may not reopen or re-decide a run."""
        store = _open("narrative")
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_terminated", "source_event_id": "e-2",
                       "outcome": "failed"})
        _, outcome = _apply(store, {"event_type": "run_progress", "source_event_id": "e-3",
                                    "message": "actually everything is fine"})
        self.assertEqual(outcome["status"], "rejected")
        self.assertEqual(outcome["reason"], m.REASON_INVALID_TRANSITION)
        self.assertEqual(m.run_state(store), m.RUN_FAILED)

    def test_no_non_success_state_is_ever_reported_as_success(self):
        for outcome in ("abandoned",):
            store = self._terminated(outcome)
            self.assertNotIn(m.run_state(store), m.SUCCESS_RUN_STATES)
        unsupported = _open("unsup")
        _apply(unsupported, {"event_type": "tool_use", "source_event_id": "e-1"})
        self.assertNotIn(m.run_state(unsupported), m.SUCCESS_RUN_STATES)


class TransitionTests(unittest.TestCase):
    def test_progress_before_start_is_refused(self):
        store = _open()
        _, outcome = _apply(store, {"event_type": "run_progress", "source_event_id": "e-1"})
        self.assertEqual(outcome["reason"], m.REASON_INVALID_TRANSITION)
        self.assertEqual(store["agent_run"]["ingest_sequence"], 0)
        self.assertEqual(len(store["events"]), 0)

    def test_a_second_start_is_refused(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _, outcome = _apply(store, {"event_type": "run_started", "source_event_id": "e-2"})
        self.assertEqual(outcome["reason"], m.REASON_INVALID_TRANSITION)
        self.assertEqual(m.run_state(store), m.RUN_OPEN)

    def test_a_terminal_state_is_absorbing_for_every_later_event(self):
        for outcome_type in ("completed", "failed", "cancelled", "blocked"):
            store = _open(outcome_type)
            _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
            _apply(store, {"event_type": "run_terminated", "source_event_id": "e-2",
                           "outcome": outcome_type})
            before = m.run_state(store)
            for later in (
                {"event_type": "run_started", "source_event_id": "x-1"},
                {"event_type": "run_progress", "source_event_id": "x-2"},
                {"event_type": "run_terminated", "source_event_id": "x-3",
                 "outcome": "failed"},
                {"event_type": "tool_use", "source_event_id": "x-4"},
            ):
                with self.subTest(terminal=outcome_type, later=later["event_type"]):
                    _, result = _apply(store, later)
                    self.assertEqual(result["status"], "rejected")
                    self.assertEqual(m.run_state(store), before)

    def test_a_terminal_event_may_arrive_without_a_start(self):
        store = _open()
        _, outcome = _apply(store, {"event_type": "run_terminated", "source_event_id": "e-1",
                                    "outcome": "failed"})
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(m.run_state(store), m.RUN_FAILED)

    def test_a_missing_outcome_is_malformed_not_success(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _, outcome = _apply(store, {"event_type": "run_terminated", "source_event_id": "e-2"})
        self.assertEqual(outcome["reason"], m.REASON_UNKNOWN_OUTCOME)
        self.assertEqual(m.run_state(store), m.RUN_UNKNOWN_OUTCOME)
        self.assertFalse(m.is_success(store))

    def test_an_unsupported_event_type_is_explicit_and_absorbing(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _, outcome = _apply(store, {"event_type": "tool_use", "source_event_id": "e-2"})
        self.assertEqual(outcome["reason"], m.REASON_UNSUPPORTED_EVENT_TYPE)
        self.assertEqual(m.run_state(store), m.RUN_UNSUPPORTED)
        # The later, otherwise-valid terminal transition may not certify it.
        _, later = _apply(store, {"event_type": "run_terminated", "source_event_id": "e-3",
                                  "outcome": "completed"})
        self.assertEqual(later["reason"], m.REASON_INVALID_TRANSITION)
        self.assertFalse(m.is_success(store))

    def test_stream_ended_closes_the_stream(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "stream_ended", "source_event_id": "e-2"})
        self.assertTrue(store["agent_run"]["stream_closed"])
        _, outcome = _apply(store, {"event_type": "run_progress", "source_event_id": "e-3"})
        self.assertEqual(outcome["reason"], m.REASON_AFTER_STREAM_END)
        self.assertEqual(m.run_state(m.finalize(store)), m.RUN_MISSING_TERMINAL)

    def test_stream_ended_does_not_change_a_terminal_state(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_terminated", "source_event_id": "e-2",
                       "outcome": "completed"})
        _apply(store, {"event_type": "stream_ended", "source_event_id": "e-3"})
        self.assertEqual(m.run_state(store), m.RUN_COMPLETED)

    def test_a_finalized_store_refuses_a_genuinely_new_event(self):
        store = m.finalize(_open())
        _, outcome = _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        self.assertEqual(outcome["reason"], m.REASON_ALREADY_FINALIZED)


class OrderingTests(unittest.TestCase):
    """A per-run single writer owns order; timestamps are evidence only."""

    def test_ingest_order_is_monotonic_in_arrival_order(self):
        store = _open()
        for index in range(1, 5):
            _apply(store, {"event_type": "run_progress" if index > 1 else "run_started",
                           "source_event_id": "e-%d" % index})
        self.assertEqual(
            [e["ingest_ordinal"] for e in store["events"]], [1, 2, 3, 4]
        )
        self.assertEqual(store["agent_run"]["ingest_sequence"], 4)

    def test_out_of_order_source_timestamps_do_not_reorder_ingest(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "late",
                       "source_timestamp": "2026-12-31T00:00:00Z"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "early",
                       "source_timestamp": "2026-01-01T00:00:00Z"})
        ordered = sorted(store["events"], key=lambda e: e["ingest_ordinal"])
        self.assertEqual([e["source_event_id"] for e in ordered], ["late", "early"])
        self.assertEqual(
            store["agent_run"]["first_source_timestamp"], "2026-12-31T00:00:00Z"
        )
        self.assertEqual(
            store["agent_run"]["last_source_timestamp"], "2026-01-01T00:00:00Z"
        )

    def test_page_order_never_decides_the_terminal_state(self):
        """A terminal transition arriving first still owns the state."""
        store = _open()
        _apply(store, {"event_type": "run_terminated", "source_event_id": "e-9",
                       "source_timestamp": "2026-01-01T00:00:01Z", "outcome": "blocked"})
        self.assertEqual(m.run_state(store), m.RUN_BLOCKED)

    def test_source_sequence_is_retained_but_not_used_for_ordering(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "a",
                       "source_sequence": 99})
        _apply(store, {"event_type": "run_progress", "source_event_id": "b",
                       "source_sequence": 1})
        ordered = sorted(store["events"], key=lambda e: e["ingest_ordinal"])
        self.assertEqual([e["source_sequence"] for e in ordered], [99, 1])


class IdempotencyTests(unittest.TestCase):
    def test_an_identical_redelivery_is_a_no_op(self):
        store = _open()
        event = {"event_type": "run_started", "source_event_id": "e-1",
                 "message": "same"}
        _apply(store, event)
        before = m.dumps(store)
        _, outcome = _apply(store, dict(event))
        self.assertEqual(outcome["status"], "duplicate")
        self.assertEqual(len(store["events"]), 1)
        self.assertEqual(store["agent_run"]["ingest_sequence"], 1)
        self.assertEqual(m.dumps(store), before)

    def test_a_redelivery_with_a_refreshed_timestamp_is_still_a_no_op(self):
        """Source timestamps are evidence, so they cannot break idempotency."""
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1",
                       "source_timestamp": "2026-01-01T00:00:00Z"})
        _, outcome = _apply(store, {"event_type": "run_started", "source_event_id": "e-1",
                                    "source_timestamp": "2026-07-07T00:00:00Z"})
        self.assertEqual(outcome["status"], "duplicate")
        self.assertEqual(
            store["events"][0]["source_timestamp"], "2026-01-01T00:00:00Z"
        )

    def test_one_logical_event_per_identity(self):
        store = _open()
        for _ in range(5):
            _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        ids = m.event_ids(store)
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(set(ids)), 1)


class ConflictTests(unittest.TestCase):
    def test_a_conflicting_redelivery_is_quarantined(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "first"})
        before = m.dumps(store)
        _, outcome = _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                                    "message": "second"})
        self.assertEqual(outcome["status"], "quarantined")
        self.assertEqual(outcome["reason"], m.REASON_CONFLICTING_REDELIVERY)
        self.assertEqual(len(store["quarantines"]), 1)

    def test_a_quarantine_never_overwrites_the_retained_evidence(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "first"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "second"})
        kept = [e for e in store["events"] if e["source_event_id"] == "e-2"][0]
        self.assertEqual(kept["payload"]["message"], "first")
        self.assertEqual(kept["ingest_ordinal"], 2)
        self.assertEqual(m.event_ids(store).__len__(), 2)

    def test_a_quarantine_does_not_advance_run_state(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "first"})
        state = m.run_state(store)
        sequence = store["agent_run"]["ingest_sequence"]
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "second"})
        self.assertEqual(m.run_state(store), state)
        self.assertEqual(store["agent_run"]["ingest_sequence"], sequence)

    def test_a_quarantine_record_carries_fingerprints_not_content(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "first"})
        _apply(store, {"event_type": "run_progress", "source_event_id": "e-2",
                       "message": "SECONDCONTENTMARKER"})
        self.assertNotIn("SECONDCONTENTMARKER", m.dumps(store["quarantines"]))
        self.assertNotIn("first", m.dumps(store["quarantines"]))

    def test_every_rejection_and_quarantine_gets_a_distinct_record(self):
        store = _open()
        for index in range(3):
            _apply(store, {"event_type": "run_progress", "source_event_id": "x-%d" % index})
        ids = [r["id"] for r in store["rejections"]]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)


class MalformedEventTests(unittest.TestCase):
    def test_non_mapping_events_are_refused(self):
        store = _open()
        for value in ("text", 7, None, [], ["a"]):
            with self.subTest(value=value):
                _, outcome = _apply(store, value)
                self.assertEqual(outcome["reason"], m.REASON_MALFORMED_EVENT)
        self.assertEqual(len(store["events"]), 0)
        self.assertEqual(len(store["rejections"]), 5)

    def test_a_missing_or_non_string_event_type_is_refused(self):
        store = _open()
        for value in ({}, {"event_type": ""}, {"event_type": "   "}, {"event_type": 7}):
            with self.subTest(value=value):
                _, outcome = _apply(store, value)
                self.assertEqual(outcome["reason"], m.REASON_MALFORMED_EVENT)

    def test_a_malformed_event_never_advances_the_run(self):
        store = _open()
        _apply(store, {"event_type": "run_started", "source_event_id": "e-1"})
        _apply(store, {"event_type": "run_terminated", "source_event_id": "e-2",
                       "outcome": "completed"})
        _apply(store, "not-an-event")
        self.assertEqual(m.run_state(store), m.RUN_COMPLETED)
        self.assertTrue(m.is_success(store))


class ReplayResultTests(unittest.TestCase):
    def test_replay_result_exposes_the_comparable_summary(self):
        store = m.finalize(_open("s-1"))
        result = m.replay_result(store)
        for key in ("run_id", "state", "ingest_sequence", "event_ids", "evidence_ids",
                    "rejection_reasons"):
            self.assertIn(key, result)

    def test_two_identical_replays_share_a_replay_result(self):
        session = {
            "adapter": "fixture",
            "session_id": "s-1",
            "events": [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_terminated", "source_event_id": "e-2",
                 "outcome": "blocked"},
            ],
        }
        first = m.replay_result(m.ingest_session(session)[0])
        second = m.replay_result(m.ingest_session(session)[0])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
