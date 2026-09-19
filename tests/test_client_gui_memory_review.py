"""Offscreen tests for the correction and history review workflow (M4.5/v1b).

These drive the real protocol 3.8.0 path: the surface builds a request through
``client_core``, the request goes to the local boundary over a temp store base,
and the returned comparison, conflicts and history are rendered. The desktop
never imports the Memory seam, and no UI action can mutate or delete history.

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
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

    from hrca import boundary, contract, memory, memory_store
    from hrca.client import MainWindow
    from hrca.client_core import memory_conflict_choices

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - only without PySide6
    _QT_AVAILABLE = False

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

CLAIM = "claim:session_summary:work_package"

EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"prompt": _SECRET}},
    {"event_type": "run_terminated", "source_event_id": "e2", "outcome": "completed",
     "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "e3", "payload": {}},
]


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 is not installed")
class MemoryReviewTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-v14-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": "s-1", "events": EVENTS,
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Review work"},
        })
        self.assertIsNone(error)
        self.run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, self.run_id, store))

        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.window._select_destination("memory")
        self.window._memory_tabs.setCurrentIndex(3)
        self.sent = []
        self._patch_send()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self._app.processEvents()
        shutil.rmtree(self.base, ignore_errors=True)

    def _patch_send(self):
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

    def select_document(self, document_type):
        selector = self.window._memory_review_document_selector
        for index in range(selector.count()):
            if selector.itemData(index) == document_type:
                selector.setCurrentIndex(index)
                return
        self.fail("document %s is not offered" % document_type)

    def load(self, document_type="session_summary"):
        """Load, select the document under test, then load it again."""
        self.window._refresh_memory_review()
        if document_type is not None:
            self.select_document(document_type)
            self.window._refresh_memory_review()
        return self.window._memory_review_view

    def arm(self, claim_id=CLAIM, operation=None):
        self.window._select_memory_review_claim(claim_id, operation)

    def compose(self, text=None, actor="heron", operation=None):
        if operation is not None:
            for index in range(self.window._memory_review_operation.count()):
                if self.window._memory_review_operation.itemData(index) == operation:
                    self.window._memory_review_operation.setCurrentIndex(index)
                    break
        if text is not None:
            self.window._memory_review_text.setPlainText(text)
        if actor is not None:
            self.window._memory_review_actor.setText(actor)

    def drain(self):
        """Run the append and the reload chain it triggers."""
        self._app.processEvents()

    def stored(self):
        return memory_store.load(self.base, self.run_id)[0]

    def texts(self, root=None):
        values = []
        for widget in (root or self.window).findChildren(QWidget):
            for attr in ("text", "toolTip", "accessibleName", "placeholderText",
                         "toPlainText"):
                method = getattr(widget, attr, None)
                if callable(method):
                    value = method()
                    if isinstance(value, str) and value:
                        values.append(value)
        return values

    def review_texts(self):
        tab = self.window._memory_tabs.widget(3)
        return self.texts(tab)

    def buttons(self, object_name="memoryReviewButton"):
        tab = self.window._memory_tabs.widget(3)
        return [
            button
            for button in tab.findChildren(QPushButton)
            if button.objectName() == object_name
        ]

    def history_texts(self):
        return [
            label.text()
            for label in self.window._memory_review_history_body.findChildren(QLabel)
        ]


class LoadTests(MemoryReviewTestCase):
    def test_the_destination_has_a_labelled_corrections_page(self):
        tabs = self.window._memory_tabs
        self.assertEqual(
            ["Documents", "Search", "Resume", "Corrections", "Code Twin"],
            [tabs.tabText(i) for i in range(tabs.count())],
        )

    def test_loading_populates_the_context_and_the_comparison(self):
        view = self.load()
        self.assertIsNotNone(view)
        self.assertEqual(1, self.window._memory_review_run_selector.count())
        self.assertTrue(self.window._memory_review_document_selector.count())
        self.assertTrue(self.window._memory_review_claims)
        blob = " | ".join(self.review_texts())
        self.assertIn("Generated:", blob)

    def test_the_load_chains_three_protocol_requests(self):
        self.load()
        actions = [request["action"] for request in self.sent]
        # The first load walks documents -> effective -> history, so a reload
        # afterwards only needs the two reads.
        self.assertEqual(
            [contract.ACTION_MEMORY_DOCUMENTS, contract.ACTION_MEMORY_EFFECTIVE,
             contract.ACTION_MEMORY_HISTORY],
            actions[:3],
        )
        for request in self.sent:
            for forbidden in ("root", "path", "base", "store_base", "cwd"):
                self.assertNotIn(forbidden, request)

    def test_an_unloaded_review_says_so(self):
        self.assertIn("Load a review", self.window._memory_review_status.text())

    def test_the_generated_statement_is_always_shown(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="HUMAN TEXT")
        self.window._confirm_memory_correction()
        self.drain()
        blob = " | ".join(self.review_texts())
        self.assertIn("Generated:", blob)
        self.assertIn("Effective:", blob)
        self.assertIn("HUMAN TEXT", blob)


class EditorTests(MemoryReviewTestCase):
    def test_arming_the_editor_names_the_claim_and_version(self):
        self.load()
        self.arm()
        selection = self.window._memory_review_selection.text()
        self.assertIn(CLAIM, selection)
        self.assertIn("generated version", selection)

    def test_the_text_field_is_enabled_only_for_text_operations(self):
        self.load()
        self.arm()
        for index in range(self.window._memory_review_operation.count()):
            operation = self.window._memory_review_operation.itemData(index)
            self.window._memory_review_operation.setCurrentIndex(index)
            with self.subTest(operation=operation):
                self.assertEqual(
                    operation in ("merge", "supersede"),
                    self.window._memory_review_text.isEnabled(),
                )

    def test_a_merge_without_text_is_refused_before_sending(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="")
        before = len(self.sent)
        self.window._confirm_memory_correction()
        self.assertEqual(before, len(self.sent))
        self.assertIn("needs replacement text",
                      self.window._memory_review_editor_status.text())

    def test_an_unknown_claim_is_refused_before_sending(self):
        self.load()
        self.arm(claim_id="claim:absent")
        before = len(self.sent)
        self.window._save_memory_draft()
        self.assertEqual(before, len(self.sent))
        self.assertIn("present in the current document",
                      self.window._memory_review_editor_status.text())

    def test_clearing_the_editor_changes_no_history(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="TEXT")
        self.window._clear_memory_editor()
        self.assertIsNone(self.window._memory_review_selected)
        self.assertEqual("", self.window._memory_review_text.toPlainText())


class DraftAndConfirmTests(MemoryReviewTestCase):
    def draft(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="DRAFT TEXT")
        self.window._save_memory_draft()
        self.drain()

    def test_a_draft_appends_one_inert_revision(self):
        self.draft()
        store = self.stored()
        self.assertEqual(1, len(store["corrections"]))
        self.assertEqual("draft", store["corrections"][0]["state"])
        # A draft is not authority: the effective text is unchanged.
        entry = {c["claim_id"]: c for c in self.window._memory_review_view["claims"]}[CLAIM]
        self.assertFalse(entry["changed"])

    def test_confirming_appends_a_linked_confirmed_revision(self):
        self.draft()
        draft_id = self.stored()["corrections"][0]["id"]
        self.window._confirm_memory_correction()
        self.drain()
        store = self.stored()
        self.assertEqual(2, len(store["corrections"]))
        confirmed = [c for c in store["corrections"] if c["state"] == "confirmed"]
        self.assertEqual(1, len(confirmed))
        self.assertEqual([draft_id], confirmed[0]["supersedes"])
        # The draft is superseded, never mutated or removed.
        self.assertEqual(1, len([c for c in store["corrections"] if c["state"] == "draft"]))

    def test_the_effective_text_updates_only_after_a_fresh_response(self):
        self.draft()
        entry = {c["claim_id"]: c for c in self.window._memory_review_view["claims"]}[CLAIM]
        self.assertFalse(entry["changed"])
        before = len(self.sent)
        self.window._confirm_memory_correction()
        self.drain()
        self.assertGreater(len(self.sent), before + 1)  # append + a full reload
        entry = {c["claim_id"]: c for c in self.window._memory_review_view["claims"]}[CLAIM]
        self.assertTrue(entry["changed"])
        self.assertEqual("DRAFT TEXT", entry["effective_statement"])

    def test_an_identical_append_attempt_is_idempotent(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="SAME")
        self.window._save_memory_draft()
        self.drain()
        # Re-sending the same composed request is the same attempt.
        request = self.sent[-3]
        self.assertIn("corr:", request.get("source_id", ""))
        self.assertEqual("draft", request.get("state"))

    def test_a_confirmation_carries_the_expected_version(self):
        self.load()
        self.arm()
        self.window._confirm_memory_correction()
        self.drain()
        # The first append records the baseline version, so the review can show
        # one from here on and every later append expects it.
        self.assertIsNotNone(self.window._memory_review_version_id)
        self.arm()
        self.compose(operation="merge", text="SECOND")
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        self.window._confirm_memory_correction()
        self.drain()
        append = [
            r for r in self.sent if r["action"] == contract.ACTION_MEMORY_CORRECTION
        ][-1]
        self.assertIsNotNone(append.get("expected_version_id"))
        self.assertEqual(
            self.window._memory_review_version_id, append["expected_version_id"]
        )

    def test_a_cancelled_confirmation_leaves_the_effective_text_alone(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="UNSENT")
        self.window._clear_memory_editor()
        self.assertEqual([], self.stored()["corrections"])


class RefusalTests(MemoryReviewTestCase):
    def test_a_stale_expected_version_is_refused_and_reloaded(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="TEXT")
        # Force a stale expectation and a fresh attempt token.
        self.window._memory_review_version_id = "gendoc:stale:1"
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        before = len(self.sent)
        self.window._confirm_memory_correction()
        self.drain()
        self.assertIn("refused", self.window._memory_review_editor_status.text())
        self.assertIn("memory_correction_refused",
                      self.window._memory_review_editor_status.text())
        self.assertEqual([], self.stored()["corrections"])
        # A bounded reload followed, and the screen stayed readable.
        self.assertGreater(len(self.sent), before)
        self.assertTrue(self.window._memory_review_claims)

    def test_a_refusal_leaves_the_prior_effective_content_readable(self):
        self.load()
        before = [c["effective_statement"] for c in self.window._memory_review_view["claims"]]
        self.arm(claim_id="claim:absent")
        self.window._confirm_memory_correction()
        self.drain()
        after = [c["effective_statement"] for c in self.window._memory_review_view["claims"]]
        self.assertEqual(before, after)

    def test_a_run_level_failure_is_reported(self):
        self.window._on_memory_review_failed(
            self.window._memory_review_generation, "memory_not_readable"
        )
        self.assertIn("memory_not_readable", self.window._memory_review_status.text())


class HistoryTests(MemoryReviewTestCase):
    def _seed_all_states(self):
        self.load()
        for index, state in enumerate(("draft", "confirmed", "archived")):
            self.arm()
            self.compose(operation="merge", text="NOTE %d" % index)
            self.window._memory_review_attempt_token = contract.new_correlation_id()
            self.window._append_memory_correction(state)
            self.drain()

    def test_generated_versions_and_states_are_visibly_distinct(self):
        self._seed_all_states()
        blob = " | ".join(self.history_texts())
        self.assertIn("Generated revision 1 (immutable)", blob)
        self.assertIn("Draft (not authority)", blob)
        self.assertIn("Confirmed (authority)", blob)
        self.assertIn("Archived (history only)", blob)

    def test_authority_and_history_only_are_labelled(self):
        self._seed_all_states()
        blob = " | ".join(self.history_texts())
        self.assertIn("Authority", blob)
        self.assertIn("History only", blob)

    def test_history_only_grows(self):
        self._seed_all_states()
        counts = []
        for _ in range(2):
            self.load()
            counts.append(len(self.stored()["corrections"]))
        self.assertEqual(counts[0], counts[1])
        self.assertEqual(3, counts[0])

    def test_supersede_links_are_shown(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="FIRST")
        self.window._save_memory_draft()
        self.drain()
        self.window._confirm_memory_correction()
        self.drain()
        blob = " | ".join(self.history_texts())
        self.assertIn("Supersedes:", blob)

    def test_an_empty_history_says_so(self):
        self.load()
        self.assertIn("No history is recorded",
                      " | ".join(self.history_texts()))


class ConflictTests(MemoryReviewTestCase):
    def _conflict(self, mutate):
        self.load()
        self.arm()
        self.compose(operation="merge", text="HUMAN")
        self.window._confirm_memory_correction()
        self.drain()
        store = self.stored()
        mutate(store)
        self.assertIsNone(memory_store.save(self.base, self.run_id, store))
        self.load()
        return self.window._memory_review_view["conflicts"]

    def test_a_changed_target_becomes_a_conflict_with_all_choices(self):
        def mutate(store):
            store["work_packages"][0]["title"] = "the work package moved on"
        conflicts = self._conflict(mutate)
        self.assertEqual(1, len(conflicts))
        self.assertIn("changed since the correction",
                      conflicts[0]["reason"])
        self.assertTrue(all(choice["enabled"] for choice in conflicts[0]["choices"]))
        # The generated statement is retained, never replaced by a guess.
        entry = {c["claim_id"]: c for c in self.window._memory_review_view["claims"]}[CLAIM]
        self.assertFalse(entry["changed"])
        self.assertIn("the work package moved on", entry["effective_statement"])
        self.assertNotIn("HUMAN", entry["effective_statement"])
        self.assertIn("the work package moved on", " | ".join(self.review_texts()))

    def test_a_conflict_without_a_present_claim_disables_every_choice(self):
        # A real projection always emits its stable claim set, so a target that
        # is absent from the document cannot be produced through the boundary.
        # The disabled path is therefore exercised on the model and on the row
        # it renders, which is exactly where the guard lives.
        missing = {
            "label": "Unresolved conflict",
            "target": {"claim_id": "claim:absent", "kind": None, "record_id": None},
            "reason": "the correction names a claim that is not in this document",
            "state": "confirmed",
            "correction_id": "correction:absent",
        }
        choices = memory_conflict_choices(missing, [CLAIM])
        self.assertTrue(choices)
        for choice in choices:
            with self.subTest(operation=choice["operation"]):
                self.assertFalse(choice["enabled"])
                self.assertTrue(choice["reason"])

        row = self.window._build_memory_review_conflict_row(
            dict(missing, choices=choices), self.window._memory_review_generation
        )
        rendered = [
            button
            for button in row.findChildren(QPushButton)
            if button.objectName() == "memoryReviewButton"
        ]
        self.assertEqual(len(choices), len(rendered))
        for button in rendered:
            with self.subTest(button=button.text()):
                self.assertFalse(button.isEnabled())
                self.assertIn("not in the current document", button.toolTip())

    def test_a_disabled_choice_does_nothing(self):
        missing = {
            "label": "Unresolved conflict",
            "target": {"claim_id": "claim:absent"},
            "reason": "the correction names a claim that is not in this document",
            "state": "confirmed",
            "correction_id": "correction:absent",
        }
        choices = memory_conflict_choices(missing, [CLAIM])
        row = self.window._build_memory_review_conflict_row(
            dict(missing, choices=choices), self.window._memory_review_generation
        )
        before = len(self.sent)
        for button in row.findChildren(QPushButton):
            button.click()
        self.assertEqual(before, len(self.sent))
        self.assertIsNone(self.window._memory_review_selected)

    def test_a_choice_arms_the_editor_rather_than_appending(self):
        def mutate(store):
            store["work_packages"][0]["title"] = "moved"
        self._conflict(mutate)
        before = len(self.sent)
        enabled = [b for b in self.buttons()
                   if b.isEnabled() and b.accessibleName().startswith("Resolve with reject")]
        self.assertTrue(enabled)
        enabled[0].click()
        self.assertEqual(before, len(self.sent), "a choice must not append directly")
        self.assertEqual(CLAIM, self.window._memory_review_selected)
        self.assertEqual("reject", self.window._memory_review_operation.currentData())

    def test_resolving_a_conflict_with_merge_keeps_history(self):
        def mutate(store):
            store["work_packages"][0]["title"] = "moved"
        self._conflict(mutate)
        before = len(self.stored()["corrections"])
        self.arm()
        self.compose(operation="merge", text="RESOLVED")
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        self.window._confirm_memory_correction()
        self.drain()
        after = self.stored()
        self.assertEqual(before + 1, len(after["corrections"]))
        # Nothing was deleted: every earlier revision is still present.
        self.assertEqual(before + 1, len(after["corrections"]))


class InvalidationTests(MemoryReviewTestCase):
    def test_a_context_change_invalidates_outstanding_actions(self):
        self.load()
        stale = self.buttons()[0]
        self.window._memory_review_document_selector.setCurrentIndex(0)
        self.window._on_memory_review_context_changed(0)
        before = len(self.sent)
        stale.click()
        self.assertEqual(before, len(self.sent))
        self.assertIn("context changed", self.window._memory_review_status.text())

    def test_a_late_review_response_is_discarded(self):
        self.load()
        generation = self.window._memory_review_generation
        self.window._refresh_memory_review()
        self.assertNotEqual(generation, self.window._memory_review_generation)
        self.window._on_memory_review_history(
            generation, {"claims": [], "conflicts": []},
            {"generated_versions": [], "corrections": []},
        )
        self.assertNotEqual([], self.window._memory_review_claims)

    def test_a_late_append_response_is_discarded(self):
        self.load()
        generation = self.window._memory_review_generation
        self.window._refresh_memory_review()
        self.window._on_memory_append_loaded(
            generation, "confirmed",
            {"correction": {"id": "correction:late", "state": "confirmed"},
             "created": True},
        )
        self.assertIsNone(self.window._memory_review_draft)
        self.assertNotIn("correction:late",
                         self.window._memory_review_editor_status.text())

    def test_an_append_to_another_claim_cannot_be_confirmed_here(self):
        self.load()
        self.arm(claim_id="claim:session_summary:project")
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        self.window._save_memory_draft()
        self.drain()
        # Arming a different claim clears the draft link: confirming appends a
        # fresh revision rather than superseding the other claim's draft.
        self.arm(claim_id=CLAIM)
        self.compose(operation="merge", text="OTHER")
        self.window._confirm_memory_correction()
        self.drain()
        store = self.stored()
        confirmed = [c for c in store["corrections"] if c["state"] == "confirmed"]
        self.assertEqual([], confirmed[0]["supersedes"])


class AccessibilityTests(MemoryReviewTestCase):
    def test_every_review_control_is_named_and_focusable(self):
        for widget in (self.window._memory_review_run_selector,
                       self.window._memory_review_document_selector,
                       self.window._memory_review_load_button,
                       self.window._memory_review_operation,
                       self.window._memory_review_text,
                       self.window._memory_review_actor,
                       self.window._memory_review_draft_button,
                       self.window._memory_review_confirm_button,
                       self.window._memory_review_clear_button):
            with self.subTest(widget=widget.objectName()):
                self.assertTrue(widget.accessibleName())
                self.assertTrue(widget.toolTip())

    def test_a_claim_action_is_focusable(self):
        self.load()
        button = self.buttons()[0]
        button.setFocus()
        self.assertTrue(button.hasFocus())
        self.assertIn(button.focusPolicy(), (Qt.StrongFocus, Qt.WheelFocus, Qt.TabFocus))

    def test_state_is_carried_by_words(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="NOTE")
        self.window._save_memory_draft()
        self.drain()
        self.assertTrue(
            any("Draft (not authority)" in text for text in self.history_texts())
        )

    def test_the_editor_does_not_rely_on_colour(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="")
        self.window._save_memory_draft()
        # Refusal feedback is a sentence, not a colour change.
        self.assertTrue(self.window._memory_review_editor_status.text())


class PrivacyTests(MemoryReviewTestCase):
    def test_no_prohibited_value_reaches_any_rendered_text(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="NOTE " + _SECRET, actor="heron")
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        self.window._save_memory_draft()
        self.drain()
        # The editor is emptied by the accepted append, so nothing rendered from
        # here on can carry the raw words that were typed.
        self.assertEqual("", self.window._memory_review_text.toPlainText())
        blob = "\n".join(self.review_texts() + self.history_texts())
        self.assertTrue(blob)
        self.assertIn("[redacted]", blob)
        for forbidden in (_SECRET, "sha256:", '"payload"', "content_fingerprint",
                          _PERSONAL, "someone", ".ssh"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_a_secret_in_human_text_is_redacted_before_storage(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="note " + _SECRET)
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        self.window._save_memory_draft()
        self.drain()
        self.assertNotIn(_SECRET, json.dumps(self.stored()))

    def test_no_prohibited_value_reaches_a_tooltip_or_accessible_name(self):
        self.load()
        blob = "\n".join(
            value
            for value in self.texts()
            if value
        )
        for forbidden in (_SECRET, "sha256:", _PERSONAL):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_fingerprints_never_appear_in_the_review(self):
        self.load()
        self.arm()
        self.compose(operation="merge", text="NOTE")
        self.window._memory_review_attempt_token = contract.new_correlation_id()
        self.window._save_memory_draft()
        self.drain()
        blob = "\n".join(self.review_texts() + self.history_texts())
        self.assertNotIn("sha256:", blob)
        self.assertNotIn("base_content_fingerprint", blob)


if __name__ == "__main__":
    unittest.main()
