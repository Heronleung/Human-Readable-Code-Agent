"""Tests for the P4.4 document/version GUI surface, run offscreen."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel

    from hrca import contract
    from hrca.client import MainWindow

    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - exercised in the no-Qt environment
    HAS_PYSIDE6 = False


def _app() -> "QApplication":
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _sample_state():
    return {
        "document": {"document_id": "doc:d1", "name": "requirements.md", "kind": "md",
                      "head_revision_number": 1, "revision_count": 1},
        "head_revision": {"revision_id": "rev:1", "content": "hello",
                          "content_fingerprint": "f" * 64},
        "candidate": {"candidate_id": "cand:c1", "document_revision_id": "rev:1",
                      "adopted": False, "generation_source": "deterministic_fixture",
                      "limitation": "no interpretation"},
        "accepted": None,
        "current_accepted_version_id": None,
        "versions": [],
    }


class _FakeSend:
    def __init__(self):
        self.requests = []

    def __call__(self, request, on_success, on_error):
        self.requests.append(request)
        return True


def _preview(state="current", name="requirements.md", kind="candidate"):
    return {
        "document": {"document_id": "doc:d1", "name": name, "kind": "md",
                      "revision_number": 1, "revision_id": "rev:1"},
        "state": state,
        "binding": {"kind": kind, "record_id": "cand:c1",
                     "adopted": kind == "accepted"},
        "provenance": "deterministic_fixture",
        "limitation": "Bound to the hand-written quotation fixture only; "
                      "no document-to-code interpretation occurred.",
        "package": {
            "package_id": "quotation-rules", "title": "Quotation rules",
            "runtime_identity": "hrca-runner:v1", "schema_version": "1.0.0",
            "form": [
                {"name": "subtotal", "type": "decimal", "min": 0},
                {"name": "member", "type": "boolean"},
                {"name": "region", "type": "choice", "options": ["west"]},
            ],
            "result": [{"name": "discount", "type": "decimal"}],
        },
        "evidence": {"package_validates": True, "package_matches": True,
                     "runtime_matches": True, "validation_matches": True,
                     "execution_performed": False},
    }


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class DocumentSurfaceTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def _open_state(self, state=None):
        state = state or _sample_state()
        self.window._document_id = "doc:d1"
        self.window._apply_document_state(state)

    def test_page_builds_and_actions_start_disabled(self):
        page = self.window._document_page
        self.assertIsNotNone(page)
        self.assertIsNotNone(self.window._document_editor)
        self.assertFalse(self.window._document_save_button.isEnabled())

    def test_documents_loaded_populates_combo(self):
        self.window._on_documents_loaded(
            {"documents": [{"document_id": "doc:a", "name": "a.md",
                            "head_revision_number": 2}]}
        )
        self.assertEqual(self.window._document_combo.count(), 1)
        self.assertEqual(self.window._document_combo.itemData(0), "doc:a")

    def test_open_working_document_dispatches(self):
        fake = _FakeSend()
        self.window._send = fake
        self.window._on_documents_loaded(
            {"documents": [{"document_id": "doc:a", "name": "a.md"}]}
        )
        self.window._open_working_document()
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["action"], contract.ACTION_DOCUMENT_OPEN)
        self.assertEqual(fake.requests[0]["document_id"], "doc:a")

    def test_apply_state_loads_editor_and_clears_dirty(self):
        self._open_state()
        self.assertEqual(self.window._document_editor.toPlainText(), "hello")
        self.assertEqual(self.window._document_base_revision_id, "rev:1")
        self.assertFalse(self.window._document_dirty)
        self.assertEqual(self.window._document_candidate_id, "cand:c1")
        self.assertTrue(self.window._document_save_button.isEnabled())

    def test_editor_edit_marks_dirty_and_save_clears(self):
        self._open_state()
        self.window._document_editor.setPlainText("edited")
        self.assertTrue(self.window._document_dirty)
        self.window._on_document_saved(
            {"revision": {"revision_id": "rev:2"}, "head_revision_number": 2}
        )
        self.assertFalse(self.window._document_dirty)
        self.assertEqual(self.window._document_base_revision_id, "rev:2")

    def test_save_dispatches_content_and_base(self):
        self._open_state()
        self.window._document_editor.setPlainText("edited")
        fake = _FakeSend()
        self.window._send = fake
        self.window._save_document()
        self.assertEqual(len(fake.requests), 1)
        req = fake.requests[0]
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_SAVE)
        self.assertEqual(req["content"], "edited")
        self.assertEqual(req["base_revision_id"], "rev:1")

    def test_create_candidate_dispatches(self):
        self._open_state()
        fake = _FakeSend()
        self.window._send = fake
        self.window._create_candidate()
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["action"], contract.ACTION_DOCUMENT_CREATE_CANDIDATE)
        self.assertEqual(fake.requests[0]["document_id"], "doc:d1")

    def test_versions_list_marks_current(self):
        state = _sample_state()
        state["versions"] = [
            {"version_id": "ver:1", "restore_of": None},
            {"version_id": "ver:2", "restore_of": "ver:1"},
        ]
        state["current_accepted_version_id"] = "ver:2"
        self._open_state(state)
        labels = [l.text() for l in self.window._versions_list.findChildren(QLabel)]
        self.assertIn("Version 1", labels)
        self.assertIn("Version 2 (current) (restored)", labels)

    def test_failure_maps_to_bounded_message(self):
        self.window._document_id = "doc:d1"
        self.window._on_document_error("document_stale")
        self.assertIn("changed since it was opened", self.window._document_result.toPlainText())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class DocumentSelectionTests(unittest.TestCase):
    """Regression tests for the P4.4a selector-reset correction.

    Selection must be preserved by the opaque document_id across list/model
    refreshes, and only an external delete/corrupt may clear it (with a bounded
    message and a safe fallback).
    """

    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def _documents(self):
        return [
            {"document_id": "doc:a", "name": "a.md", "head_revision_number": 1},
            {"document_id": "doc:b", "name": "b.md", "head_revision_number": 2},
            {"document_id": "doc:c", "name": "c.md", "head_revision_number": 1},
        ]

    def _open(self, document_id, revision_id="rev:1"):
        self.window._apply_document_state(
            {
                "document": {"document_id": document_id, "name": f"{document_id}.md", "kind": "md"},
                "head_revision": {"revision_id": revision_id, "content": "x"},
                "candidate": None,
                "accepted": None,
                "current_accepted_version_id": None,
                "versions": [],
            }
        )

    def test_apply_state_sets_document_id(self):
        # Opening an existing document must bind the editor/actions to the
        # document the boundary returned (the id is taken from the result, not
        # the selector), so Save is usable after an Open.
        self.window._on_documents_loaded({"documents": self._documents()})
        self.window._apply_document_state(
            {
                "document": {"document_id": "doc:b", "name": "b.md", "kind": "md"},
                "head_revision": {"revision_id": "rev:2", "content": "bee"},
                "candidate": None,
                "accepted": None,
                "current_accepted_version_id": None,
                "versions": [],
            }
        )
        self.assertEqual(self.window._document_id, "doc:b")
        self.assertTrue(self.window._document_save_button.isEnabled())
        self.assertEqual(self.window._document_combo.currentData(), "doc:b")

    def test_refresh_preserves_selection_by_document_id(self):
        self.window._on_documents_loaded({"documents": self._documents()})
        self._open("doc:b", revision_id="rev:2")
        self.assertEqual(self.window._document_combo.currentData(), "doc:b")
        # A refresh (as after save) must keep doc:b selected, never jump to the
        # first document.
        self.window._on_documents_loaded({"documents": self._documents()})
        self.assertEqual(self.window._document_combo.currentData(), "doc:b")
        self.assertEqual(self.window._document_id, "doc:b")

    def test_refresh_after_save_keeps_non_first_document(self):
        # The full save handler refreshes the list; the saved document stays
        # active rather than resetting to the first created document.
        self.window._on_documents_loaded({"documents": self._documents()})
        self._open("doc:c", revision_id="rev:3")
        self.window._document_editor.setPlainText("edited")
        self.window._on_document_saved(
            {"revision": {"revision_id": "rev:4"}, "head_revision_number": 2}
        )
        self.window._on_documents_loaded({"documents": self._documents()})
        self.assertEqual(self.window._document_combo.currentData(), "doc:c")
        self.assertEqual(self.window._document_id, "doc:c")

    def test_external_delete_clears_selection_with_fallback(self):
        self.window._on_documents_loaded({"documents": self._documents()})
        self._open("doc:b", revision_id="rev:2")
        # doc:b is externally removed; the open selection is cleared, a message
        # is shown, and the selector falls back to the first available document.
        self.window._on_documents_loaded(
            {"documents": [d for d in self._documents() if d["document_id"] != "doc:b"]}
        )
        self.assertIsNone(self.window._document_id)
        self.assertEqual(self.window._document_editor.toPlainText(), "")
        self.assertFalse(self.window._document_save_button.isEnabled())
        self.assertEqual(self.window._document_combo.currentData(), "doc:a")
        self.assertIn("removed", self.window._document_result.toPlainText())

    def test_empty_refresh_with_no_selection_selects_first(self):
        self.window._on_documents_loaded({"documents": self._documents()})
        self.assertEqual(self.window._document_combo.currentData(), "doc:a")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class PreviewSurfaceTests(unittest.TestCase):
    """P4.5 read-only version-bound Preview surface."""

    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def test_preview_is_read_only_without_run_button(self):
        self.assertTrue(self.window._preview_body.isReadOnly())
        self.assertFalse(hasattr(self.window, "_builder_run"))

    def test_render_preview_populates_state_and_document(self):
        self.window._render_preview(_preview(state="current", name="requirements.md"))
        self.assertIn("requirements.md", self.window._preview_document_label.text())
        self.assertIn("Candidate", self.window._preview_state_label.text())
        self.assertIn("Current", self.window._preview_state_label.text())
        body = self.window._preview_body.toPlainText()
        self.assertIn("subtotal", body)
        self.assertIn("discount", body)
        self.assertIn("Package executed: no", body)
        self.assertIn("no document-to-code", body)

    def test_render_preview_distinguishes_accepted_version(self):
        self.window._render_preview(_preview(state="current", kind="accepted"))
        self.assertIn("Accepted Version", self.window._preview_state_label.text())

    def test_refresh_preview_dispatches_for_open_document(self):
        fake = _FakeSend()
        self.window._send = fake
        self.window._document_id = "doc:d1"
        self.window._refresh_preview()
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["action"], contract.ACTION_DOCUMENT_PREVIEW)
        self.assertEqual(fake.requests[0]["document_id"], "doc:d1")

    def test_refresh_preview_no_document_clears(self):
        self.window._document_id = None
        self.window._refresh_preview()
        self.assertEqual(self.window._preview_document_label.text(), "")
        self.assertIn("No document", self.window._preview_state_label.text())

    def test_late_preview_response_is_discarded(self):
        fake = _FakeSend()
        self.window._send = fake
        self.window._document_id = "doc:d1"
        self.window._refresh_preview()  # generation 1
        self.window._refresh_preview()  # generation 2
        self.assertEqual(self.window._preview_generation, 2)
        # A late response for generation 1 is discarded.
        self.window._on_preview_loaded(1, _preview(name="old.md"))
        self.assertEqual(self.window._preview_document_label.text(), "")
        # The current generation's response renders.
        self.window._on_preview_loaded(2, _preview(name="new.md"))
        self.assertIn("new.md", self.window._preview_document_label.text())

    def test_preview_error_clears_surface(self):
        self.window._document_id = "doc:d1"
        self.window._on_preview_error("document_not_found")
        self.assertEqual(self.window._preview_document_label.text(), "")
        self.assertIn("No document", self.window._preview_state_label.text())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class DocumentLifecycleTests(unittest.TestCase):
    """P4.5a: immediate create/selection, save -> Create preview, name conflict."""

    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def _created(self, document_id="doc:new", name="new.md"):
        return {
            "document": {"document_id": document_id, "name": name, "kind": "md",
                          "head_revision_number": 0, "revision_count": 0},
            "head_revision": None,
            "candidate": None,
            "accepted": None,
            "current_accepted_version_id": None,
            "versions": [],
        }

    def test_created_document_is_immediately_selected(self):
        self.window._send = _FakeSend()
        self.window._on_document_created(self._created())
        self.assertEqual(self.window._document_id, "doc:new")
        self.assertEqual(self.window._document_combo.count(), 1)
        self.assertEqual(self.window._document_combo.currentData(), "doc:new")

    def test_created_document_needs_no_list_round_trip(self):
        fake = _FakeSend()
        self.window._send = fake
        self.window._on_document_created(self._created())
        actions = [r["action"] for r in fake.requests]
        self.assertNotIn(contract.ACTION_DOCUMENT_LIST, actions)

    def test_save_enables_create_preview(self):
        self.window._send = _FakeSend()
        self.window._apply_document_state(_sample_state())
        self.assertFalse(self.window._document_candidate_button.isHidden())
        # Editing hides it (dirty).
        self.window._document_editor.setPlainText("edited")
        self.assertTrue(self.window._document_candidate_button.isHidden())
        # Saving re-enables it and binds the new head.
        self.window._on_document_saved(
            {"revision": {"revision_id": "rev:2", "revision_number": 2,
                           "content_fingerprint": "f" * 64},
             "head_revision_number": 2}
        )
        self.assertFalse(self.window._document_candidate_button.isHidden())
        self.assertTrue(self.window._document_candidate_button.isEnabled())
        self.assertEqual(self.window._document_head.get("revision_id"), "rev:2")

    def test_create_preview_disabled_while_pending(self):
        self.window._send = _FakeSend()
        self.window._apply_document_state(_sample_state())
        self.window._create_candidate()
        self.assertTrue(self.window._document_candidate_pending)
        self.assertFalse(self.window._document_candidate_button.isEnabled())
        # A second click is a no-op (pending guard).
        fake = _FakeSend()
        self.window._send = fake
        self.window._create_candidate()
        self.assertEqual(len(fake.requests), 0)
        # Completing resets the pending flag.
        self.window._on_candidate_ready(
            {"candidate": None, "accepted": None,
             "current_accepted_version_id": None, "versions": []}
        )
        self.assertFalse(self.window._document_candidate_pending)

    def test_name_conflict_message_names_the_request(self):
        self.window._pending_document_name = "requirements.md"
        self.window._on_create_document_error("document_name_in_use")
        result_text = self.window._document_result.toPlainText()
        self.assertIn("requirements.md", result_text)
        self.assertIn("already in use", result_text)


if __name__ == "__main__":
    unittest.main()
