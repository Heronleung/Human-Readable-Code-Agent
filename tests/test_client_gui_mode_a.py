"""Focused fake-only review tests for the Mode A P4.8b/v9 + P0/P1 diff."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from hrca import contract
    from hrca.client import MainWindow
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _state(document_id="doc:d1", revision_id="rev:1", name="requirements.md"):
    return {
        "document": {
            "document_id": document_id,
            "name": name,
            "kind": "md",
            "head_revision_number": 1,
            "revision_count": 1,
        },
        "head_revision": {
            "revision_id": revision_id,
            "revision_number": 1,
            "content": "Members receive a 10% discount on quotations.",
            "content_fingerprint": "f" * 64,
        },
        "candidate": None,
        "accepted": None,
        "current_accepted_version_id": None,
        "versions": [],
    }


def _terminal(state, *, sent, usage=None):
    return {
        "schema_version": "1.0.0",
        "state": state,
        "provider_id": "deepseek",
        "model": "deepseek-flash",
        "token": "delta:fake",
        "sent": sent,
        "usage": usage,
        "candidate": None,
        "limitations": ["fake-only deterministic result"],
    }


class _FakeSend:
    def __init__(self):
        self.requests = []

    def __call__(self, request, on_success, on_error):
        self.requests.append(request)
        return True


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ModeAUiTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def test_first_run_has_one_primary_document_cta(self):
        self.assertFalse(self.window._document_empty_state.isHidden())
        self.assertTrue(self.window._document_editor.isHidden())
        labels = [button.text() for button in self.window._document_empty_state.findChildren(type(self.window._document_save_button))]
        self.assertIn("New document", labels)
        self.assertIn("Open project", labels)

    def test_document_header_and_dirty_hint_are_explicit(self):
        self.window._apply_document_state(_state())
        self.assertEqual(self.window._document_title_label.text(), "requirements.md")
        self.assertEqual(self.window._document_status_label.text(), "Saved")
        self.window._document_editor.setPlainText("edited")
        self.assertEqual(self.window._document_status_label.text(), "Unsaved changes")
        self.assertEqual(self.window._document_action_hint.text(), "Save changes to continue.")
        self.assertTrue(self.window._document_candidate_button.isHidden())

    def test_provider_readiness_is_a_toolbar_chip(self):
        self.assertEqual(
            self.window._provider_status_label.parentWidget().objectName(),
            "commandBar",
        )

    def test_diagnostics_are_collapsed_by_default(self):
        self.assertTrue(all(label.isHidden() for label in self.window._diagnostic_labels))
        self.window._status_details_button.setChecked(True)
        self.assertTrue(all(not label.isHidden() for label in self.window._diagnostic_labels))

    def test_empty_trash_does_not_reserve_space(self):
        self.window._apply_library({"folders": [], "documents": []})
        self.assertTrue(self.window._library_trash_header.isHidden())
        self.assertTrue(self.window._library_trash_scroll.isHidden())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class TerminalAttemptRetentionTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()
        self.window._apply_document_state(_state())
        self.fake = _FakeSend()
        self.window._send = self.fake

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def _render(self, result):
        generation = self.window._next_rule_delta_generation()
        self.window._on_rule_delta_result(generation, result)

    def test_rejected_attempt_survives_navigation_without_redispatch(self):
        self._render(_terminal("credential_rejected", sent=True))
        for destination in ("document", "preview", "versions", "preview"):
            self.window._select_destination(destination)
        body = self.window._preview_body.toPlainText()
        self.assertIn("API key rejected", body)
        self.assertIn("Sent: yes", body)
        self.assertIn("Usage: unknown", body)
        self.assertIn("App Candidate: none", body)
        actions = [request.get("action") for request in self.fake.requests]
        self.assertNotIn(contract.ACTION_PREPARE_RULE_DELTA, actions)
        self.assertNotIn(contract.ACTION_INTERPRET_RULE_DELTA, actions)
        self.assertNotIn("success — preview ready", self.window.status_label.text())

    def test_missing_attempt_survives_provider_refresh(self):
        self._render(_terminal("credential_missing", sent=False))
        before = len(self.fake.requests)
        self.window._apply_provider_state({
            "state": "configured",
            "provider_id": "deepseek",
            "model": "deepseek-flash",
            "credential_present": True,
        })
        body = self.window._preview_body.toPlainText()
        self.assertIn("Credential missing", body)
        self.assertIn("Sent: no", body)
        self.assertEqual(len(self.fake.requests), before)

    def test_binding_prevents_cross_document_or_revision_bleed(self):
        self._render(_terminal("credential_rejected", sent=True))
        self.window._apply_document_state(_state("doc:d2", "rev:1", "other.md"))
        self.assertNotIn("API key rejected", self.window._preview_body.toPlainText())
        self.window._apply_document_state(_state("doc:d1", "rev:2", "requirements.md"))
        self.assertNotIn("API key rejected", self.window._preview_body.toPlainText())
        self.window._apply_document_state(_state("doc:d1", "rev:1", "requirements.md"))
        self.assertIn("API key rejected", self.window._preview_body.toPlainText())


if __name__ == "__main__":
    unittest.main()
