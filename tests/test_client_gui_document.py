"""Tests for the P4.4 document/version GUI surface, run offscreen."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

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

    def test_versions_combo_marks_current(self):
        state = _sample_state()
        state["versions"] = [
            {"version_id": "ver:1", "restore_of": None},
            {"version_id": "ver:2", "restore_of": "ver:1"},
        ]
        state["current_accepted_version_id"] = "ver:2"
        self._open_state(state)
        self.assertEqual(self.window._document_versions_combo.count(), 2)
        self.assertIn("(current)", self.window._document_versions_combo.itemText(1))

    def test_failure_maps_to_bounded_message(self):
        self.window._document_id = "doc:d1"
        self.window._on_document_error("document_stale")
        self.assertIn("changed since it was opened", self.window._document_result.toPlainText())


if __name__ == "__main__":
    unittest.main()
