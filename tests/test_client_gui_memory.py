"""Offscreen tests for the read-only Memory destination (M4.3/v2b).

These tests drive the real protocol 3.6.0 path: the surface builds a request
through ``client_core``, the request goes to the local boundary over a temp
store base, and the normalized response is rendered. Nothing here reaches the
Memory seam from a desktop module — the surface only ever sees protocol results.

Every test runs with ``QT_QPA_PLATFORM=offscreen`` and is skipped when PySide6
is not installed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

    from hrca import boundary, contract, memory, memory_store
    from hrca.client import MainWindow, _NAV_LABELS

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without PySide6
    _QT_AVAILABLE = False

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
        "decisions": [{"summary": "chose the bounded path"}],
        "evidence": [
            {"kind": "artifact", "artifact_ref": "pkg/mod.py", "digest": "sha256:" + "a" * 64}
        ],
    },
    {"event_type": "run_terminated", "source_event_id": "e3", "outcome": "completed", "payload": {}},
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

_ENVELOPE_KEYS = {"contract_version", "correlation_id", "ok", "result"}


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 is not installed")
class MemorySurfaceTestCase(unittest.TestCase):
    """Shared fixture: a temp store base behind a real boundary session."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-v2b-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.window._select_destination("memory")
        self.sent = []
        self.wire_backend()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self._app.processEvents()
        shutil.rmtree(self.base, ignore_errors=True)

    def seed(self, events, session_id="s-1"):
        store, error, _ = memory.ingest_session(
            {
                "adapter": "smoke",
                "session_id": session_id,
                "project": {"source_id": "p-1", "name": "Project One"},
                "work_package": {"source_id": "w-1", "title": "Work Package One"},
                "events": events,
            }
        )
        self.assertIsNone(error)
        run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        return run_id, store

    def wire_backend(self):
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
        return patcher

    def load(self):
        """Load documents through the surface over the real boundary."""
        self.window._refresh_memory()
        self.assertTrue(self.sent, "the surface sent no request")
        return self.sent[-1]

    def apply_with_origin(self, origin):
        """Load documents with an explicit caller-declared capture origin."""
        from hrca.client_core import build_get_memory_documents_request

        request = build_get_memory_documents_request(
            contract.new_correlation_id(), origin=origin
        )
        envelope = boundary.handle_request(request, self.session)
        self.assertTrue(envelope.get("ok"), envelope)
        self.window._apply_memory_documents(envelope["result"], origin=origin)
        self.sent.append(request)

    def _widget_texts(self, root=None):
        """Return every visible text, tooltip and accessible name in the tree."""
        root = root or self.window
        values = []
        for widget in root.findChildren(QWidget):
            for attr in ("text", "toolTip", "accessibleName", "placeholderText"):
                method = getattr(widget, attr, None)
                if callable(method):
                    try:
                        value = method()
                    except TypeError:
                        continue
                    if isinstance(value, str) and value:
                        values.append(value)
        for attr in ("text", "toolTip", "accessibleName"):
            method = getattr(root, attr, None)
            if callable(method):
                value = method()
                if isinstance(value, str) and value:
                    values.append(value)
        return values

    def _target_buttons(self):
        return list(getattr(self.window, "_memory_target_buttons", []))

    def _resolved_buttons(self):
        return [b for b in self._target_buttons() if b.isEnabled()]

    def _blocked_buttons(self):
        return [b for b in self._target_buttons() if not b.isEnabled()]

    def _detail_texts(self):
        return [
            label.text()
            for label in self.window._memory_detail_body.findChildren(QLabel)
        ]

    def _rows(self):
        from hrca.client_core import claim_rows

        document_set = self.window._current_memory_document_set()
        document_type = self.window._memory_document_selector.currentData()
        return claim_rows(document_set, document_type)

    def _texts(self, root=None):
        """Every visible text, tooltip and accessible name under ``root``."""
        return self._widget_texts(root)

    def _memory_texts(self):
        """Texts scoped to the Memory destination, not the whole window."""
        return self._widget_texts(self.window._memory_claim_panel) + \
            self._widget_texts(self.window._memory_detail_panel) + [
                self.window._memory_status.text(),
                self.window._memory_run_status.text(),
            ]

    def _select_document(self, document_type):
        selector = self.window._memory_document_selector
        for index in range(selector.count()):
            if selector.itemData(index) == document_type:
                selector.setCurrentIndex(index)
                return
        self.fail("document %s is not selectable" % document_type)

    def _select_run(self, run_id):
        selector = self.window._memory_run_selector
        for index in range(selector.count()):
            if selector.itemData(index) == run_id:
                selector.setCurrentIndex(index)
                return
        self.fail("run %s is not selectable" % run_id)


class NavigationTests(MemorySurfaceTestCase):
    def test_the_destination_is_labelled_and_reachable(self):
        self.assertEqual("Memory", _NAV_LABELS["memory"])
        self.assertIn("memory", self.window._nav_buttons)
        self.assertEqual(
            _NAV_LABELS["memory"], self.window._nav_buttons["memory"].accessibleName()
        )
        self.window._select_destination("memory")
        self.assertEqual("memory", self.window._nav_destination)

    def test_load_requests_documents_through_the_protocol(self):
        self.seed(COMPLETED_EVENTS)
        request = self.load()
        self.assertEqual(contract.ACTION_MEMORY_DOCUMENTS, request["action"])
        self.assertEqual(contract.CONTRACT_VERSION, request["contract_version"])
        for forbidden in ("root", "path", "base", "store_base", "cwd"):
            self.assertNotIn(forbidden, request)

    def test_a_loaded_document_renders_claims_and_targets(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        self.assertTrue(self.window._memory_run_selector.count())
        self.assertTrue(self.window._memory_document_selector.count())
        self.assertTrue(self._rows())
        self.assertTrue(self._target_buttons())


class ResolvedEvidenceTests(MemorySurfaceTestCase):
    def test_every_resolved_target_opens_its_exact_record(self):
        run_id, _ = self.seed(COMPLETED_EVENTS)
        self.load()
        resolved = self._resolved_buttons()
        self.assertTrue(resolved)
        for button in resolved:
            with self.subTest(target=button.accessibleName()):
                before = len(self.sent)
                button.click()
                self.assertEqual(before + 1, len(self.sent), "the action sent no request")
                request = self.sent[-1]
                self.assertEqual(contract.ACTION_MEMORY_RECORD, request["action"])
                self.assertEqual(run_id, request["run_id"])
                self.assertIn(request["kind"], _KINDS)
                self.assertTrue(request["record_id"])
                # The detail names the exact identity the action named.
                self.assertEqual(request["record_id"], self.window._memory_detail["record_id"])
                self.assertEqual(run_id, self.window._memory_detail["run_id"])
                self.assertEqual(request["kind"], self.window._memory_detail["kind"])

    def test_opening_a_record_preserves_the_claim_context(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        document_type = self.window._memory_document_selector.currentData()
        run_index = self.window._memory_run_selector.currentIndex()
        button = self._resolved_buttons()[0]
        button.click()
        self.assertTrue(self._detail_texts())
        self.assertEqual(document_type, self.window._memory_document_selector.currentData())
        self.assertEqual(run_index, self.window._memory_run_selector.currentIndex())

    def test_the_detail_shows_the_owning_run_and_labelled_fields(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        # The run reference is a session-summary claim.
        self._select_document("session_summary")
        button = next(
            b for b in self._resolved_buttons() if "Run" in b.accessibleName()
        )
        button.click()
        blob = " | ".join(self._detail_texts())
        self.assertIn(self.window._memory_detail["record_id"], blob)
        self.assertIn(self.window._memory_detail["run_id"], blob)
        self.assertIn("Owning run", blob)

    def test_a_reported_field_is_labelled_as_reported(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        # The decision document's summary is source-reported.
        for index in range(self.window._memory_document_selector.count()):
            if self.window._memory_document_selector.itemData(index) == "decision_record":
                self.window._memory_document_selector.setCurrentIndex(index)
                break
        button = self._resolved_buttons()[0]
        button.click()
        accessible = self._detail_texts()
        self.assertTrue(any("chose the bounded path" in text for text in accessible))

    def test_a_digest_is_shown_as_presence_only(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        for index in range(self.window._memory_document_selector.count()):
            if self.window._memory_document_selector.currentData() == "change_record":
                break
            self.window._memory_document_selector.setCurrentIndex(index)
        button = next(
            (b for b in self._resolved_buttons() if "Evidence" in b.accessibleName()), None
        )
        if button is None:
            self.skipTest("no evidence reference in this document")
        button.click()
        blob = " | ".join(self._texts(self.window._memory_detail_body))
        self.assertNotIn("sha256:", blob)


_KINDS = (
    "run", "project", "work_package", "event", "evidence", "decision",
    "change_set", "code_entity_link", "rejection", "quarantine",
)


class StateFidelityTests(MemorySurfaceTestCase):
    def test_unknown_outcome_is_non_success_in_list_and_detail(self):
        self.seed(COMPLETED_EVENTS, session_id="s-c")
        unknown_run, _ = self.seed(UNKNOWN_EVENTS, session_id="s-u")
        self.load()
        self._select_run(unknown_run)
        labels = [self.window._memory_run_selector.itemText(i)
                  for i in range(self.window._memory_run_selector.count())]
        self.assertTrue(any("Unknown outcome (not success)" in t for t in labels))
        blob = " | ".join(self._memory_texts())
        self.assertIn("Unknown outcome (not success)", blob)
        self.assertIn("Run state: Unknown outcome (not success)", blob)
        self.assertNotIn("(success)", blob.replace("(not success)", ""))

    def test_offline_failure_says_offline_and_not_success(self):
        self.seed(FAILED_EVENTS)
        self.load()
        self.apply_with_origin("offline")
        self._select_document("session_summary")
        labels = [self.window._memory_run_selector.itemText(i)
                  for i in range(self.window._memory_run_selector.count())]
        self.assertTrue(any("Failed (not success)" in t for t in labels))
        blob = " | ".join(self._memory_texts())
        self.assertIn("Failed (not success)", blob)
        # The claim states the origin in the source's own words...
        self.assertIn("Capture origin is declared by the caller as 'offline'.", blob)
        self.assertIn("not live-session observation", blob)
        # ...and the run status states the declaration, never a live claim.
        status = self.window._memory_run_status.text()
        self.assertIn("origin: Offline deterministic (declared by caller)", status)

    def test_no_freshness_or_verification_claim_is_made(self):
        # The words "verify"/"verification" do appear — inside the projector's
        # own *disclaimers* ("never that anything was verified"). What must be
        # absent is a positive claim, so this asserts on the absence of the
        # claim rather than on the absence of a substring.
        self.seed(COMPLETED_EVENTS)
        self.load()
        for document_type in ("session_summary", "change_record",
                              "decision_record", "issues_and_actions"):
            with self.subTest(document=document_type):
                self._select_document(document_type)
                blob = " | ".join(self._memory_texts()).lower()
                for forbidden in ("current", "up to date", "fresh", "verified:",
                                  "is verified", "has been verified"):
                    self.assertNotIn(forbidden, blob)
        # The strongest statement this surface makes about verification is a
        # disclaimer, and only the document that carries evidence makes it.
        self._select_document("change_record")
        blob = " | ".join(self._memory_texts()).lower()
        self.assertIn("never that anything was verified", blob)
        self.assertIn("no verification result is recorded", blob)
        status = self.window._memory_run_status.text().lower()
        for forbidden in ("current", "verified", "fresh"):
            self.assertNotIn(forbidden, status)

    def test_the_run_status_states_finality_and_baseline_separately(self):
        run_id, store = self.seed(UNFINISHED_EVENTS)
        self.load()
        status = self.window._memory_run_status.text()
        self.assertIn("Missing terminal (not success)", status)
        self.assertIn("baseline: unsupported", status)
        self.assertIn("snapshot: finalized", status)
        self.assertNotIn("stale", status.lower())

        store["agent_run"]["finalized"] = False
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        self.window._refresh_memory()
        status = self.window._memory_run_status.text()
        self.assertIn("snapshot: stale", status)
        self.assertIn("store is not a finalized snapshot", status)
        # Still distinct: staleness does not replace or hide the baseline gap.
        self.assertIn("baseline: unsupported", status)


class UnresolvedTargetTests(MemorySurfaceTestCase):
    def _dangling(self):
        run_id, store = self.seed(COMPLETED_EVENTS)
        store["agent_run"]["terminal_event_id"] = "event:not-in-this-run"
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        return run_id

    def test_a_dangling_target_is_visible_and_disabled(self):
        self._dangling()
        self.load()
        # The dangling reference lives in the terminal-state claim.
        self._select_document("session_summary")
        blocked = self._blocked_buttons()
        self.assertTrue(blocked, "a dangling reference must remain visible")
        blob = " | ".join(self._memory_texts())
        self.assertIn("event:not-in-this-run", blob)
        for button in blocked:
            self.assertIn("Unavailable", button.text())
            self.assertIn("record that is not present", button.toolTip())

    def test_activating_an_unresolved_target_navigates_nowhere(self):
        self._dangling()
        self.load()
        self._select_document("session_summary")
        before = len(self.sent)
        detail_before = list(self._detail_texts())
        blocked = self._blocked_buttons()
        self.assertTrue(blocked)
        for button in blocked:
            button.click()
        self.assertEqual(before, len(self.sent), "a disabled action sent a request")
        self.assertEqual(detail_before, self._detail_texts())
        self.assertIsNone(self.window._memory_detail)

    def test_every_operable_target_names_a_supported_kind(self):
        # Behavioural form: whatever a claim exposes, activating it produces a
        # request the boundary accepts — an unsupported kind would fail closed.
        self._dangling()
        self.load()
        self._select_document("session_summary")
        operable = self._resolved_buttons()
        self.assertTrue(operable)
        for button in operable:
            with self.subTest(target=button.accessibleName()):
                before = len(self.sent)
                button.click()
                self.assertEqual(before + 1, len(self.sent))
                self.assertIn(self.sent[-1]["kind"], _KINDS)
                self.assertIsNotNone(self.window._memory_detail)

    def test_an_unresolved_target_names_its_own_status(self):
        self._dangling()
        self.load()
        self._select_document("session_summary")
        for button in self._blocked_buttons():
            with self.subTest(target=button.accessibleName()):
                self.assertIn("Unavailable", button.accessibleName())
                self.assertIn("record that is not present", button.accessibleName())


class InvalidationTests(MemorySurfaceTestCase):
    def test_a_run_switch_invalidates_outstanding_actions(self):
        first, _ = self.seed(COMPLETED_EVENTS, session_id="s-c")
        second, _ = self.seed(UNKNOWN_EVENTS, session_id="s-u")
        self.load()
        stale_button = self._resolved_buttons()[0]
        captured = self.window._memory_generation

        # Switch the run: the old action is now obsolete.
        self.window._memory_run_selector.setCurrentIndex(
            1 if self.window._memory_run_selector.currentIndex() == 0 else 0
        )
        self.assertNotEqual(captured, self.window._memory_generation)

        self.sent.clear()
        stale_button.click()
        self.assertEqual([], self.sent, "a stale action still sent a request")
        self.assertIsNone(self.window._memory_detail)

    def test_a_document_switch_invalidates_outstanding_actions(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        stale_button = self._resolved_buttons()[0]
        self.window._memory_document_selector.setCurrentIndex(
            1 if self.window._memory_document_selector.currentIndex() == 0 else 0
        )
        self.sent.clear()
        stale_button.click()
        self.assertEqual([], self.sent)

    def test_replacing_the_result_set_invalidates_outstanding_actions(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        stale_button = self._resolved_buttons()[0]
        self.window._refresh_memory()
        self.sent.clear()
        stale_button.click()
        self.assertEqual([], self.sent)

    def test_a_superseded_response_is_discarded(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        button = self._resolved_buttons()[0]
        button.click()
        current = self.window._memory_detail
        self.assertIsNotNone(current)

        stale_generation = self.window._memory_generation
        self.window._refresh_memory()
        self.assertNotEqual(stale_generation, self.window._memory_generation)

        # A late response carrying the superseded generation must not replace
        # the detail shown for the current selection.
        view = {"kind": "event", "record_id": "event:superseded", "run_id": "run:x",
                "fields": {"id": "event:superseded"}, "reported_fields": [],
                "digest_present": None}
        self.window._on_memory_record_loaded(stale_generation, view)
        self.assertNotIn("event:superseded", "\n".join(self._detail_texts()))

        # The current generation still resolves normally, through a button the
        # current result set built.
        fresh = self._resolved_buttons()[0]
        fresh.click()
        self.assertIsNotNone(self.window._memory_detail)
        self.assertNotEqual("event:superseded", self.window._memory_detail["record_id"])

    def test_removing_the_selection_clears_the_detail(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        self._resolved_buttons()[0].click()
        self.assertIsNotNone(self.window._memory_detail)
        self.window._apply_memory_documents({"run_count": 0, "document_sets": []})
        self.assertIsNone(self.window._memory_detail)


class AccessibilityTests(MemorySurfaceTestCase):
    def test_every_target_is_named_and_focusable(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        for button in self._target_buttons():
            with self.subTest(target=button.text()):
                self.assertTrue(button.accessibleName())
                self.assertIn(
                    button.focusPolicy(), (Qt.StrongFocus, Qt.WheelFocus, Qt.TabFocus)
                )
                self.assertTrue(button.toolTip())

    def test_keyboard_traversal_reaches_a_target_and_activation_works(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        button = self._resolved_buttons()[0]
        button.setFocus()
        self.assertTrue(button.hasFocus())
        before = len(self.sent)
        button.click()
        self.assertEqual(before + 1, len(self.sent))

    def test_state_and_provenance_are_words_not_only_colour(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        blob = " | ".join(self._texts())
        self.assertIn("Run state: Completed (success)", blob)
        self.assertIn("Observed", blob)

    def test_the_provenance_chip_text_matches_its_colour_token(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        chips = [
            label
            for label in self.window.findChildren(QLabel)
            if label.objectName() == "memoryProvenanceChip"
        ]
        self.assertTrue(chips)
        for chip in chips:
            # The chip's accessible name states the same provenance word, so the
            # colour is decoration rather than the signal.
            self.assertIn(chip.text(), chip.accessibleName())

    def test_a_claim_row_exposes_its_stable_identity(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        ids = [
            label.text()
            for label in self.window.findChildren(QLabel)
            if label.objectName() == "memoryClaimId"
        ]
        self.assertTrue(ids)
        for value in ids:
            self.assertTrue(value.startswith("claim:"))

    def test_the_placeholder_is_reachable_before_any_load(self):
        self.assertTrue(self.window._memory_status.text() == "")
        self.assertTrue(self._detail_texts())


class PrivacyTests(MemorySurfaceTestCase):
    def test_no_prohibited_value_reaches_any_surface_text(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        for button in self._resolved_buttons():
            button.click()
        texts = self._texts() + self._detail_texts()
        blob = "\n".join(texts)
        self.assertTrue(blob)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh",
                          '"payload"', "content_fingerprint", "privacy"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_no_prohibited_value_reaches_a_tooltip_or_accessible_name(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        self._resolved_buttons()[0].click()
        values = []
        for widget in self.window.findChildren(QWidget):
            values.append(widget.toolTip() or "")
            values.append(widget.accessibleName() or "")
        blob = "\n".join(values)
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", "sha256:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_a_personal_path_is_absent_from_every_rendered_value(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        blob = "\n".join(self._texts())
        self.assertNotIn("Users", blob)

    def test_the_detail_never_renders_a_digest_value(self):
        self.seed(COMPLETED_EVENTS)
        self.load()
        for button in self._resolved_buttons():
            button.click()
        blob = "\n".join(self._detail_texts())
        self.assertNotIn("sha256:" + "a" * 64, blob)


if __name__ == "__main__":
    unittest.main()
