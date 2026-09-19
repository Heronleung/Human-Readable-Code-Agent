"""Offscreen tests for the integrated Search / Timeline / Resume workflow (M4.4/v2).

These drive the real protocol 3.7.0 path: the surface builds a request through
``client_core``, the request goes to the local boundary over a temp store base,
and the returned bounded result is rendered. The desktop never imports the
Memory seam, and the query model is never asked for anything the boundary would
not return.

Every test runs with ``QT_QPA_PLATFORM=offscreen`` and is skipped without PySide6.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QWidget

    from hrca import boundary, contract, memory, memory_store
    from hrca.client import MainWindow
    from hrca.client_core import (
        MEMORY_MAX_FILTERS,
        MEMORY_ORDER_RECORDED_TIME,
        MEMORY_ORDER_RELEVANCE,
    )

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - only without PySide6
    _QT_AVAILABLE = False

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

# Comparable instants, so the timeline has something to order.
TIMED = [
    {"event_type": "run_started", "source_event_id": "t1",
     "payload": {"prompt": _SECRET}, "source_timestamp": "2026-09-17T10:00:00"},
    {"event_type": "run_progress", "source_event_id": "t2", "payload": {},
     "paths": ["pkg/timed.py"],
     "decisions": [{"summary": "chose the timed path"}]},
    {"event_type": "run_terminated", "source_event_id": "t3", "outcome": "completed",
     "payload": {}, "source_timestamp": "2026-09-17T10:05:00"},
    {"event_type": "stream_ended", "source_event_id": "t4", "payload": {}},
]

UNTIMED = [
    {"event_type": "run_started", "source_event_id": "u1", "payload": {},
     "source_timestamp": "not a timestamp"},
    {"event_type": "run_progress", "source_event_id": "u2", "payload": {},
     "paths": ["pkg/untimed.py"]},
    {"event_type": "run_terminated", "source_event_id": "u3",
     "outcome": "not-a-real-outcome", "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "u4", "payload": {}},
]

FAILED = [
    {"event_type": "run_started", "source_event_id": "f1", "payload": {}},
    {"event_type": "run_terminated", "source_event_id": "f2", "outcome": "failed",
     "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "f3", "payload": {}},
]


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 is not installed")
class MemoryQuerySurfaceTestCase(unittest.TestCase):
    """Shared fixture: a temp store base behind a real boundary session."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-v2b4-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.window._select_destination("memory")
        self.sent = []
        self._patch_send()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self._app.processEvents()
        shutil.rmtree(self.base, ignore_errors=True)

    def seed(self, events, session_id="s-1", work_package=True):
        session = {
            "adapter": "smoke",
            "session_id": session_id,
            "project": {"source_id": "p-1", "name": "Project One"},
            "events": events,
        }
        if work_package is True:
            session["work_package"] = {"source_id": "w-1", "title": "Bounded query work"}
        elif isinstance(work_package, dict):
            session["work_package"] = work_package
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertIsNone(memory_store.save(self.base, store["agent_run"]["id"], store))
        return store["agent_run"]["id"], store

    def _patch_send(self):
        """Route the surface's requests to the real boundary and back."""

        def send(request, on_success, on_error):
            self.sent.append(request)
            envelope = boundary.handle_request(request, self.session)
            if envelope.get("ok"):
                on_success(envelope.get("result", {}))
            else:
                on_error((envelope.get("error") or {}).get("code", "internal_error"))
            return True

        patcher = mock.patch.object(self.window, "_send", side_effect=send)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers -----------------------------------------------------------

    def add_filter(self, facet, term):
        selector = self.window._memory_facet_selector
        for index in range(selector.count()):
            if selector.itemData(index) == facet:
                selector.setCurrentIndex(index)
                break
        else:
            self.fail("facet %s is not offered" % facet)
        self.window._memory_term_field.setText(term)
        self.window._add_memory_filter()

    def choose_order(self, order):
        selector = self.window._memory_order_selector
        for index in range(selector.count()):
            if selector.itemData(index) == order:
                selector.setCurrentIndex(index)
                return
        self.fail("order %s is not offered" % order)

    def search(self):
        self.window._run_memory_search()
        return self.window._memory_search_result

    def texts(self, root=None):
        values = []
        for widget in (root or self.window).findChildren(QWidget):
            for attr in ("text", "toolTip", "accessibleName"):
                method = getattr(widget, attr, None)
                if callable(method):
                    value = method()
                    if isinstance(value, str) and value:
                        values.append(value)
        return values

    def search_texts(self):
        return self.texts(self.window._memory_tabs.widget(1))

    def resume_texts(self):
        return self.texts(self.window._memory_tabs.widget(2))

    def detail_texts(self, prefix):
        """The labels of one detail pane."""
        body = getattr(self.window, prefix + "_body")
        return [label.text() for label in body.findChildren(QLabel)]

    def target_buttons(self, root=None):
        from PySide6.QtWidgets import QPushButton

        return [
            button
            for button in (root or self.window).findChildren(QPushButton)
            if button.objectName() == "memoryQueryTargetButton"
        ]

    def headings(self, root):
        return [
            label.text()
            for label in root.findChildren(QLabel)
            if label.objectName() in ("memoryResultHeading", "memoryResumeHeading")
        ]


class SurfaceTests(MemoryQuerySurfaceTestCase):
    def test_the_destination_has_labelled_pages(self):
        tabs = self.window._memory_tabs
        self.assertEqual(
            ["Documents", "Search", "Resume", "Corrections", "Code Twin"],
            [tabs.tabText(i) for i in range(tabs.count())],
        )
        self.assertEqual("Memory pages", tabs.accessibleName())

    def test_the_documents_page_still_works(self):
        self.seed(TIMED)
        self.window._refresh_memory()
        self.assertTrue(self.window._memory_run_selector.count())

    def test_supported_facets_are_offered_and_unsupported_ones_are_disabled(self):
        selector = self.window._memory_facet_selector
        offered = {}
        for index in range(selector.count()):
            item = selector.model().item(index)
            offered[selector.itemText(index)] = item.isEnabled()
        self.assertTrue(offered["File"])
        self.assertTrue(offered["Run state"])
        # Named, labelled and unselectable rather than hidden.
        self.assertIn("Test result (unsupported)", offered)
        self.assertFalse(offered["Test result (unsupported)"])

    def test_both_orders_are_offered_with_words(self):
        selector = self.window._memory_order_selector
        self.assertEqual(
            [MEMORY_ORDER_RELEVANCE, MEMORY_ORDER_RECORDED_TIME],
            [selector.itemData(i) for i in range(selector.count())],
        )
        for index in range(selector.count()):
            self.assertTrue(selector.itemText(index))
        self.assertEqual(MEMORY_ORDER_RELEVANCE, self.window._memory_selected_order())


class FilterTests(MemoryQuerySurfaceTestCase):
    def test_a_filter_is_added_listed_and_counted(self):
        self.add_filter("file", "mod.py")
        self.assertEqual([("file", "mod.py")], self.window._memory_query_filters)
        self.assertIn("Filters: 1 of %d" % MEMORY_MAX_FILTERS,
                      self.window._memory_filters_heading.text())
        self.assertIn("File: mod.py", " | ".join(self.search_texts()))

    def test_a_filter_can_be_removed(self):
        self.add_filter("file", "mod.py")
        self.window._remove_memory_filter(0)
        self.assertEqual([], self.window._memory_query_filters)
        self.assertEqual("Filters: none", self.window._memory_filters_heading.text())

    def test_filters_can_be_cleared(self):
        self.add_filter("file", "mod.py")
        self.add_filter("symbol", "widget")
        self.window._clear_memory_filters()
        self.assertEqual([], self.window._memory_query_filters)
        self.assertEqual("Filters: none", self.window._memory_filters_heading.text())

    def test_an_empty_term_is_refused_with_words(self):
        self.add_filter("file", "")
        self.assertEqual([], self.window._memory_query_filters)
        self.assertIn("Enter a term", self.window._memory_search_status.text())

    def test_the_filter_bound_is_visible_and_enforced(self):
        for index in range(MEMORY_MAX_FILTERS):
            self.add_filter("file", "mod%d" % index)
        self.assertEqual(MEMORY_MAX_FILTERS, len(self.window._memory_query_filters))
        self.add_filter("file", "one-too-many")
        self.assertEqual(MEMORY_MAX_FILTERS, len(self.window._memory_query_filters))
        self.assertIn("maximum %d filters" % MEMORY_MAX_FILTERS,
                      self.window._memory_search_status.text())

    def test_an_unsupported_facet_cannot_be_added_from_the_surface(self):
        # The disabled item cannot become current, so the surface cannot build a
        # query it knows the model must refuse.
        selector = self.window._memory_facet_selector
        for index in range(selector.count()):
            if selector.itemData(index) is None:
                selector.setCurrentIndex(index)
        self.assertIsNone(selector.currentData())
        self.window._memory_term_field.setText("pass")
        self.window._add_memory_filter()
        self.assertEqual([], self.window._memory_query_filters)

    def test_a_filter_carries_no_path(self):
        self.add_filter("file", "mod.py")
        self.search()
        self.assertEqual("search_memory", self.sent[-1]["action"])
        for forbidden in ("root", "path", "base", "store_base", "cwd"):
            self.assertNotIn(forbidden, self.sent[-1])


class ResultTests(MemoryQuerySurfaceTestCase):
    def test_a_search_renders_results_and_reports_bounds(self):
        self.seed(TIMED)
        result = self.search()
        self.assertIsNotNone(result)
        status = self.window._memory_search_status.text()
        self.assertIn("result(s)", status)
        self.assertIn("limit", status)
        self.assertTrue(self.headings(self.window._memory_tabs.widget(1)))

    def test_relevance_order_has_one_ranked_section(self):
        self.seed(TIMED)
        self.choose_order(MEMORY_ORDER_RELEVANCE)
        self.search()
        headings = self.headings(self.window._memory_tabs.widget(1))
        self.assertIn("Ranked by matched facets", headings)
        self.assertNotIn("Not in time order", headings)

    def test_timeline_keeps_the_two_buckets_distinct(self):
        self.seed(TIMED, "s-t")
        self.seed(UNTIMED, "s-u")
        self.choose_order(MEMORY_ORDER_RECORDED_TIME)
        self.search()
        headings = self.headings(self.window._memory_tabs.widget(1))
        self.assertIn("In recorded-time order", headings)
        self.assertIn("Not in time order", headings)
        blob = " | ".join(self.search_texts())
        self.assertIn("says nothing about causality", blob)
        self.assertIn("unordered rather than placed in the sequence", blob)

    def test_an_ordered_result_shows_its_instant_and_an_unordered_one_says_so(self):
        self.seed(TIMED, "s-t")
        self.seed(UNTIMED, "s-u")
        self.choose_order(MEMORY_ORDER_RECORDED_TIME)
        self.search()
        blob = " | ".join(self.search_texts())
        self.assertIn("Recorded time: 2026-09-17T10:00:00", blob)
        self.assertIn("Recorded time not comparable", blob)

    def test_a_result_reports_its_match_reason_and_provenance(self):
        self.seed(TIMED)
        self.add_filter("file", "timed.py")
        self.search()
        blob = " | ".join(self.search_texts())
        self.assertIn("Matched:", blob)
        self.assertIn("Provenance:", blob)
        self.assertIn("Matched fields:", blob)

    def test_an_unsupported_facet_reports_nothing_and_says_why(self):
        self.seed(TIMED)
        # The surface cannot select it, so drive the boundary path directly.
        cid = contract.new_correlation_id()
        request = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": cid,
            "action": contract.ACTION_MEMORY_SEARCH,
            "filters": {"test_result": ["pass"]},
        }
        envelope = boundary.handle_request(request, self.session)
        self.window._apply_search_result(envelope["result"])
        status = self.window._memory_search_status.text()
        self.assertIn("unsupported facet", status)
        self.assertIn("Test result", status)
        self.assertEqual([], self.target_buttons(self.window._memory_tabs.widget(1)))

    def test_a_refused_query_is_reported_in_words(self):
        self.seed(TIMED)
        self.window._on_memory_query_failed(self.window._memory_query_generation, "memory_query_invalid")
        self.assertIn("refused", self.window._memory_search_status.text())
        self.assertIn("memory_query_invalid", self.window._memory_search_status.text())

    def test_truncation_is_visible(self):
        for index in range(3):
            self.seed(TIMED, "s-%02d" % index)
        self.search()
        result = self.window._memory_search_result
        self.assertIn("limit", self.window._memory_search_status.text())
        self.assertEqual(result.get("truncated"), "truncated to the limit" in
                         self.window._memory_search_status.text())


class NavigationTests(MemoryQuerySurfaceTestCase):
    def test_every_resolved_result_opens_its_exact_record(self):
        self.seed(TIMED)
        self.search()
        buttons = [b for b in self.target_buttons(self.window._memory_tabs.widget(1)) if b.isEnabled()]
        self.assertTrue(buttons)
        for button in buttons:
            with self.subTest(target=button.accessibleName()):
                before = len(self.sent)
                button.click()
                self.assertEqual(before + 1, len(self.sent))
                request = self.sent[-1]
                self.assertEqual(contract.ACTION_MEMORY_RECORD, request["action"])
                detail = self.window._memory_query_detail
                self.assertIsNotNone(detail)
                self.assertEqual(request["record_id"], detail["record_id"])
                self.assertEqual(request["run_id"], detail["run_id"])
                self.assertIn(detail["record_id"], self.detail_texts("_memory_query_detail"))

    def test_a_search_target_does_not_render_into_the_resume_pane(self):
        self.seed(TIMED)
        self.search()
        button = [b for b in self.target_buttons(self.window._memory_tabs.widget(1)) if b.isEnabled()][0]
        button.click()
        self.assertIsNotNone(self.window._memory_query_detail)
        self.assertIsNone(self.window._memory_resume_detail)

    def test_a_resume_target_does_not_render_into_the_search_pane(self):
        self.seed(TIMED)
        self.window._refresh_memory_resume()
        buttons = [b for b in self.target_buttons(self.window._memory_tabs.widget(2)) if b.isEnabled()]
        self.assertTrue(buttons)
        buttons[0].click()
        self.assertIsNotNone(self.window._memory_resume_detail)
        self.assertIsNone(self.window._memory_query_detail)


class InvalidationTests(MemoryQuerySurfaceTestCase):
    def test_changing_the_filters_invalidates_outstanding_actions(self):
        self.seed(TIMED)
        self.search()
        stale = [b for b in self.target_buttons(self.window._memory_tabs.widget(1)) if b.isEnabled()][0]
        self.add_filter("file", "timed.py")
        self.sent.clear()
        stale.click()
        self.assertEqual([], self.sent, "a stale result action still sent a request")

    def test_changing_the_order_invalidates_outstanding_actions(self):
        self.seed(TIMED)
        self.search()
        stale = [b for b in self.target_buttons(self.window._memory_tabs.widget(1)) if b.isEnabled()][0]
        self.choose_order(MEMORY_ORDER_RECORDED_TIME)
        self.sent.clear()
        stale.click()
        self.assertEqual([], self.sent)
        self.assertIn("order changed", " | ".join(self.detail_texts("_memory_query_detail")))

    def test_a_late_search_response_is_discarded(self):
        self.seed(TIMED)
        generation = self.window._memory_query_generation
        self.window._refresh_memory_resume()  # advances the generation
        self.assertNotEqual(generation, self.window._memory_query_generation)
        self.window._on_memory_search_loaded(
            generation, {"hit_count": 0, "results": [], "unordered": []}
        )
        self.assertIsNone(self.window._memory_search_result)

    def test_a_late_record_response_is_discarded(self):
        self.seed(TIMED)
        self.search()
        generation = self.window._memory_query_generation
        self.window._refresh_memory_resume()
        view = {"kind": "event", "record_id": "event:superseded", "run_id": "run:x",
                "fields": {"id": "event:superseded"}, "reported_fields": [],
                "digest_present": None}
        self.window._on_memory_query_record_loaded(
            generation, "_memory_query_detail", view
        )
        self.assertIsNone(self.window._memory_query_detail)
        self.assertNotIn("event:superseded",
                         " | ".join(self.detail_texts("_memory_query_detail")))

    def test_a_late_resume_response_is_discarded(self):
        self.seed(TIMED)
        generation = self.window._memory_query_generation
        self.window._run_memory_search()
        self.window._on_memory_resume_loaded(generation, {"run_count": 0, "runs": []})
        self.assertIsNone(self.window._memory_resume)

    def test_clearing_filters_invalidates_outstanding_actions(self):
        self.seed(TIMED)
        self.add_filter("file", "timed.py")
        self.search()
        stale = [b for b in self.target_buttons(self.window._memory_tabs.widget(1)) if b.isEnabled()][0]
        self.window._clear_memory_filters()
        self.sent.clear()
        stale.click()
        self.assertEqual([], self.sent)


class ResumeTests(MemoryQuerySurfaceTestCase):
    def resume(self):
        self.window._refresh_memory_resume()
        return self.window._memory_resume

    def test_acceptance_and_baseline_are_reported_as_absent_facts(self):
        self.seed(TIMED)
        self.resume()
        blob = " | ".join(self.resume_texts())
        self.assertIn("Last accepted change", blob)
        self.assertIn("Unsupported", blob)
        self.assertIn("no acceptance or adoption decision", blob)
        self.assertIn("Current baseline", blob)
        self.assertIn("Not verified", blob)
        self.assertIn("no baseline or revision identity", blob)

    def test_completion_is_reported_separately_from_acceptance(self):
        self.seed(TIMED)
        self.resume()
        headings = self.headings(self.window._memory_tabs.widget(2))
        self.assertIn("Completed runs", headings)
        self.assertIn("Last accepted change", headings)
        blob = " | ".join(self.resume_texts())
        self.assertIn("not acceptance", blob)

    def test_every_required_resume_section_is_rendered(self):
        self.seed(FAILED, "s-f")
        self.resume()
        headings = self.headings(self.window._memory_tabs.widget(2))
        for heading in ("Last accepted change", "Completed runs", "Current goal",
                        "Current baseline", "Blockers", "Unverified claims",
                        "Next actions", "Limitations"):
            with self.subTest(heading=heading):
                self.assertIn(heading, headings)

    def test_states_stay_distinct(self):
        self.seed(TIMED, "s-c")
        self.seed(UNTIMED, "s-u")
        self.seed(FAILED, "s-f")
        self.resume()
        blob = " | ".join(self.resume_texts())
        self.assertIn("Completed (success)", blob)
        self.assertIn("Unknown outcome (not success)", blob)
        self.assertIn("Failed (not success)", blob)

    def test_a_run_state_blocker_explains_itself_and_carries_a_target(self):
        self.seed(FAILED, "s-f")
        self.resume()
        blob = " | ".join(self.resume_texts())
        self.assertIn("Blockers", blob)
        self.assertIn("Failed (not success)", blob)
        # A run-state blocker carries no separate reason: its explanation is the
        # limitation the contract records for that state.
        self.assertIn("the run ended on a typed failure transition", blob)
        self.assertTrue(self.target_buttons(self.window._memory_tabs.widget(2)))

    def test_a_refused_record_blocker_carries_its_reason(self):
        self.seed(UNTIMED, "s-u")
        self.resume()
        blob = " | ".join(self.resume_texts())
        self.assertIn(
            "Reason: run_terminated carries an unrecognized outcome", blob
        )

    def test_an_unverified_claim_is_listed_with_its_reason(self):
        self.seed(TIMED)
        self.resume()
        blob = " | ".join(self.resume_texts())
        self.assertIn("Unverified claims", blob)
        self.assertIn("no record verifies it", blob)

    def test_the_resume_never_upgrades_an_unsupported_fact(self):
        self.seed(TIMED)
        self.resume()
        blob = " | ".join(self.resume_texts()).lower()
        for forbidden in ("accepted change:", "verified baseline", "is current",
                          "up to date"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_next_actions_are_ordered_and_traceable(self):
        self.seed(FAILED, "s-f")
        self.resume()
        blob = " | ".join(self.resume_texts())
        self.assertIn("Next actions", blob)
        self.assertIn("inspect the failing transition", blob)


class AccessibilityTests(MemoryQuerySurfaceTestCase):
    def test_controls_are_named_and_focusable(self):
        for widget in (self.window._memory_facet_selector,
                       self.window._memory_term_field,
                       self.window._memory_add_filter_button,
                       self.window._memory_order_selector,
                       self.window._memory_search_button,
                       self.window._memory_clear_filters_button,
                       self.window._memory_resume_button):
            with self.subTest(widget=widget.objectName()):
                self.assertTrue(widget.accessibleName())
                self.assertTrue(widget.toolTip())

    def test_a_result_target_is_focusable(self):
        self.seed(TIMED)
        self.search()
        button = [b for b in self.target_buttons(self.window._memory_tabs.widget(1)) if b.isEnabled()][0]
        button.setFocus()
        self.assertTrue(button.hasFocus())
        self.assertIn(button.focusPolicy(), (Qt.StrongFocus, Qt.WheelFocus, Qt.TabFocus))

    def test_state_is_carried_by_words_not_only_colour(self):
        self.seed(TIMED)
        self.search()
        blob = " | ".join(self.search_texts())
        self.assertIn("Run state: Completed (success)", blob)

    def test_the_time_bucket_note_is_textual(self):
        self.seed(UNTIMED, "s-u")
        self.choose_order(MEMORY_ORDER_RECORDED_TIME)
        self.search()
        blob = " | ".join(self.search_texts())
        self.assertIn("Not in time order", blob)
        self.assertIn("Recorded time not comparable", blob)


class PrivacyTests(MemoryQuerySurfaceTestCase):
    def test_no_prohibited_value_reaches_any_rendered_text(self):
        self.seed(TIMED, "s-t")
        self.seed(UNTIMED, "s-u")
        self.search()
        self.window._refresh_memory_resume()
        for button in self.target_buttons():
            if button.isEnabled():
                button.click()
        blob = "\n".join(
            self.texts()
            + self.detail_texts("_memory_query_detail")
            + self.detail_texts("_memory_resume_detail")
        )
        self.assertTrue(blob)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", "sha256:",
                          '"payload"', "content_fingerprint"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_no_prohibited_value_reaches_a_tooltip_or_accessible_name(self):
        self.seed(TIMED)
        self.search()
        self.window._refresh_memory_resume()
        values = []
        for widget in self.window.findChildren(QWidget):
            values.append(widget.toolTip() or "")
            values.append(widget.accessibleName() or "")
        blob = "\n".join(values)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", "sha256:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_a_digest_is_shown_as_presence_never_as_a_value(self):
        self.seed(TIMED)
        self.add_filter("file", "timed.py")
        self.search()
        for button in self.target_buttons(self.window._memory_tabs.widget(1)):
            if button.isEnabled():
                button.click()
        blob = " | ".join(self.detail_texts("_memory_query_detail"))
        self.assertNotIn("sha256:", blob)


if __name__ == "__main__":
    unittest.main()
