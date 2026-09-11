"""Deterministic GUI tests for the P4.8 provider-to-rule-delta interpretation flow.

These run offscreen with an injected ``_send`` seam and a fake confirmation
dialog, so no real credential, socket, provider or Docker is used. They prove the
single contextual Build/Update preview action, the exact offline disclosure with
a default Cancel, one confirmed dispatch, duplicate-click suppression, late/stale
result rejection, document-switch invalidation, and that prepare/cancel/
navigation never call the provider.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from hrca import client, contract
    from hrca.client import MainWindow

    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - exercised in the no-Qt environment
    HAS_PYSIDE6 = False


def _app() -> "QApplication":
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakeSend:
    """Injected transport seam: records requests and never invokes callbacks."""

    def __init__(self):
        self.requests = []

    def __call__(self, request, on_success, on_error):
        self.requests.append(request)
        return True


class _FakeMessageBox:
    """A deterministic stand-in for the modal confirmation dialog."""

    def __init__(self, clicked_label=None):
        self.clicked_label = clicked_label
        self.buttons = {}
        self.default_button = None
        self.title = None
        self.text = None
        self.informative = None
        self.detailed = None

    def setWindowTitle(self, value):
        self.title = value

    def setText(self, value):
        self.text = value

    def setInformativeText(self, value):
        self.informative = value

    def setDetailedText(self, value):
        self.detailed = value

    def setDefaultButton(self, button):
        self.default_button = button

    def addButton(self, text, role):
        button = mock.Mock()
        button.label = text
        self.buttons[text] = button
        return button

    def exec(self):
        return None

    def clickedButton(self):
        if self.clicked_label is None:
            return None
        return self.buttons.get(self.clicked_label)


def _patch_box(clicked_label):
    """Patch ``client.QMessageBox`` with a deterministic fake; returns (patcher, boxes)."""
    boxes = []
    real_cls = client.QMessageBox

    def factory(*args, **kwargs):
        box = _FakeMessageBox(clicked_label)
        boxes.append(box)
        return box

    stand_in = mock.MagicMock()
    # Preserve the class-level role constants the client reads while replacing
    # construction with a deterministic fake box.
    stand_in.AcceptRole = real_cls.AcceptRole
    stand_in.RejectRole = real_cls.RejectRole
    stand_in.DestructiveRole = real_cls.DestructiveRole
    stand_in.side_effect = factory
    return mock.patch.object(client, "QMessageBox", stand_in), boxes


def _state(document_id="doc:d1", name="requirements.md", content="Members receive a 10% discount on quotations."):
    return {
        "document": {"document_id": document_id, "name": name, "kind": "md",
                      "head_revision_number": 1, "revision_count": 1},
        "head_revision": {"revision_id": "rev:1", "revision_number": 1,
                          "content": content, "content_fingerprint": "f" * 64},
        "candidate": None,
        "accepted": None,
        "current_accepted_version_id": None,
        "versions": [],
    }


def _disclosure():
    return {
        "provider_id": "deepseek",
        "model": "deepseek-v4-flash",
        "one_attempt": True,
        "no_retry": True,
        "no_paid_repair": True,
        "egress_statement": "data leaves this machine",
        "policy_warning": "no zero-retention promise; only synthetic text is permitted",
        "account_cap_statement": "US$8 is not an enforced account cap; the enforced reservation for this single request is US$0.01.",
        "caps": {"request_bytes": 12288, "input_tokens": 4096, "output_tokens": 1024,
                 "timeout_seconds": 45.0, "workflow_timeout_seconds": 120.0},
        "reservation": {"amount_usd": "0.01", "worst_case_cost_usd": "0.00315",
                        "input_rate_usd_per_1m": "0.44", "output_rate_usd_per_1m": "1.32",
                        "sufficient": True},
        "items": [
            {"kind": "instruction", "label": "code-owned rule-delta schema, allowlist, baseline and examples",
             "bytes": 1234},
            {"kind": "requirement", "label": "selected requirement text", "bytes": 40},
        ],
    }


def _prepare_result(document_id="doc:d1"):
    return {
        "provider_id": "deepseek",
        "model": "deepseek-v4-flash",
        "document_id": document_id,
        "document_name": "requirements.md",
        "revision_id": "rev:1",
        "revision_number": 1,
        "token": "delta:abc123",
        "disclosure": _disclosure(),
        "available": True,
        "reason": None,
    }


def _interpret_result(state="reviewable_candidate"):
    result = {
        "schema_version": "1.0.0",
        "state": state,
        "provider_id": "deepseek",
        "model": "deepseek-v4-flash",
        "token": "delta:abc123",
        "sent": True,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "candidate": None,
        "limitations": [],
    }
    if state == "reviewable_candidate":
        result["candidate"] = {
            "provenance": "provider_delta",
            "binding": {"document_revision_id": "rev:1"},
        }
    return result


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class RuleDeltaGuiTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    # -- action visibility -------------------------------------------------

    def test_build_preview_visible_and_labelled_after_save(self):
        self.window._apply_document_state(_state())
        button = self.window._document_candidate_button
        self.assertFalse(button.isHidden())
        self.assertEqual(button.text(), "Build preview")

    def test_build_preview_hidden_while_dirty(self):
        self.window._apply_document_state(_state())
        self.window._document_editor.setPlainText("edited")
        self.assertTrue(self.window._document_candidate_button.isHidden())

    def test_build_preview_hidden_without_saved_revision(self):
        state = _state()
        state["head_revision"] = None
        self.window._apply_document_state(state)
        self.assertTrue(self.window._document_candidate_button.isHidden())

    # -- prepare is offline and does not call the provider ------------------

    def test_build_preview_dispatches_prepare_only(self):
        self.window._apply_document_state(_state())
        fake = _FakeSend()
        self.window._send = fake
        self.window._build_preview()
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.requests[0]["action"], contract.ACTION_PREPARE_RULE_DELTA)
        self.assertEqual(fake.requests[0]["document_id"], "doc:d1")

    def test_duplicate_click_suppressed_while_pending(self):
        self.window._apply_document_state(_state())
        fake = _FakeSend()
        self.window._send = fake
        self.window._build_preview()
        self.assertTrue(self.window._rule_delta_pending)
        self.window._build_preview()
        self.assertEqual(len(fake.requests), 1)

    def test_prepare_opens_confirmation_with_disclosure(self):
        self.window._apply_document_state(_state())
        patcher, boxes = _patch_box(None)
        with patcher:
            self.window._build_preview()
            self.window._on_rule_delta_prepared(
                self.window._rule_delta_generation, _prepare_result()
            )
        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0].title, "Confirm rule interpretation request")
        self.assertIn("deepseek-v4-flash", boxes[0].informative)

    # -- disclosure is exact and defaults to Cancel -------------------------

    def test_disclosure_renders_exact_fields(self):
        self.window._apply_document_state(_state())
        patcher, boxes = _patch_box(None)
        with patcher:
            self.window._build_preview()
            self.window._on_rule_delta_prepared(
                self.window._rule_delta_generation, _prepare_result()
            )
        info = boxes[0].informative
        for expected in (
            "deepseek",
            "deepseek-v4-flash",
            "no retry",
            "US$0.01",
            "US$8 is not an enforced account cap",
            "instruction",
            "requirement",
            "12288 request bytes",
        ):
            self.assertIn(expected, info)

    def test_dialog_defaults_to_cancel(self):
        self.window._apply_document_state(_state())
        patcher, boxes = _patch_box(None)
        with patcher:
            self.window._build_preview()
            self.window._on_rule_delta_prepared(
                self.window._rule_delta_generation, _prepare_result()
            )
        self.assertEqual(boxes[0].default_button.label, "Cancel")

    def test_cancel_sends_nothing(self):
        self.window._apply_document_state(_state())
        fake = _FakeSend()
        self.window._send = fake
        patcher, _boxes = _patch_box(None)
        with patcher:
            self.window._build_preview()
            self.window._on_rule_delta_prepared(
                self.window._rule_delta_generation, _prepare_result()
            )
        actions = [r["action"] for r in fake.requests]
        self.assertEqual(actions, [contract.ACTION_PREPARE_RULE_DELTA])
        self.assertIsNone(self.window._pending_rule_delta_token)

    # -- one confirmed dispatch --------------------------------------------

    def test_explicit_confirm_dispatches_one_interpret(self):
        self.window._apply_document_state(_state())
        fake = _FakeSend()
        self.window._send = fake
        patcher, _boxes = _patch_box("Build preview")
        with patcher:
            self.window._build_preview()
            self.window._on_rule_delta_prepared(
                self.window._rule_delta_generation, _prepare_result()
            )
        actions = [r["action"] for r in fake.requests]
        self.assertEqual(
            actions,
            [contract.ACTION_PREPARE_RULE_DELTA, contract.ACTION_INTERPRET_RULE_DELTA],
        )
        interpret = fake.requests[-1]
        self.assertIs(interpret["task"]["confirmed"], True)
        self.assertEqual(interpret["task"]["token"], "delta:abc123")
        self.assertIsNone(self.window._pending_rule_delta_token)

    # -- late/stale rejection and document switching -----------------------

    def test_late_result_rejected_after_document_switch(self):
        self.window._apply_document_state(_state())
        self.window._build_preview()
        stale_generation = self.window._rule_delta_generation
        self.window._apply_document_state(_state("doc:d2", "other.md"))
        self.window._on_rule_delta_result(
            stale_generation, _interpret_result("reviewable_candidate")
        )
        self.assertNotIn("Reviewable", self.window._preview_body.toPlainText())

    def test_document_switch_clears_pending_scope(self):
        self.window._apply_document_state(_state())
        self.window._pending_rule_delta_token = "delta:abc123"
        self.window._pending_rule_delta_document_id = "doc:d1"
        self.window._rule_delta_reviewable_for = "doc:d1"
        before = self.window._rule_delta_generation
        self.window._apply_document_state(_state("doc:d2", "other.md"))
        self.assertIsNone(self.window._pending_rule_delta_token)
        self.assertIsNone(self.window._rule_delta_reviewable_for)
        self.assertGreater(self.window._rule_delta_generation, before)

    def test_navigation_never_dispatches_provider(self):
        self.window._apply_document_state(_state())
        fake = _FakeSend()
        self.window._send = fake
        self.window._select_destination("preview")
        self.window._select_destination("document")
        actions = [r["action"] for r in fake.requests]
        self.assertNotIn(contract.ACTION_PREPARE_RULE_DELTA, actions)
        self.assertNotIn(contract.ACTION_INTERPRET_RULE_DELTA, actions)

    # -- bounded result rendering ------------------------------------------

    def test_reviewable_result_renders_and_is_not_adopted(self):
        self.window._apply_document_state(_state())
        self.window._build_preview()
        self.window._on_rule_delta_result(
            self.window._rule_delta_generation, _interpret_result("reviewable_candidate")
        )
        body = self.window._preview_body.toPlainText()
        self.assertIn("Reviewable", body)
        self.assertIn("not adopted", body)
        self.assertEqual(self.window._document_candidate_button.text(), "Update preview")

    def test_clarification_result_renders_without_candidate(self):
        self.window._apply_document_state(_state())
        self.window._build_preview()
        self.window._on_rule_delta_result(
            self.window._rule_delta_generation, _interpret_result("clarification_required")
        )
        self.assertIn("Needs clarification", self.window._preview_body.toPlainText())
        self.assertEqual(self.window._document_candidate_button.text(), "Build preview")

    def test_prepare_unavailable_renders_bounded_failure(self):
        self.window._apply_document_state(_state())
        patcher, boxes = _patch_box(None)
        with patcher:
            self.window._build_preview()
            self.window._on_rule_delta_prepared(
                self.window._rule_delta_generation,
                {"provider_id": "deepseek", "model": "deepseek-v4-flash",
                 "document_id": "doc:d1", "document_name": "requirements.md",
                 "revision_id": "rev:1", "revision_number": 1,
                 "token": None, "disclosure": None,
                 "available": False, "reason": "over_limit"},
            )
        self.assertEqual(len(boxes), 0)
        self.assertIn("cannot be interpreted", self.window._preview_body.toPlainText())

    def test_rule_delta_error_is_bounded(self):
        self.window._apply_document_state(_state())
        self.window._rule_delta_pending = True
        self.window._on_rule_delta_error("document_not_saved")
        self.assertFalse(self.window._rule_delta_pending)


if __name__ == "__main__":
    unittest.main()
