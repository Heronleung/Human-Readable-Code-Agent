"""Tests for the PySide6 desktop client (P3.2), run offscreen.

These tests import PySide6 and are skipped when it is not installed, so the
core and its tests remain installable without Qt. Every test runs with
``QT_QPA_PLATFORM=offscreen`` so no display server is required.
"""

from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from hrca import contract
    from hrca.client import (
        BackendSupervisor,
        CodeView,
        MainWindow,
        PythonHighlighter,
    )
    from hrca.client_core import TWIN_STALE, VALIDATION_OK, build_request

    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - exercised in the no-Qt environment
    HAS_PYSIDE6 = False


def _app() -> "QApplication":
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _sample_result() -> dict:
    return {
        "task_id": "P3.2",
        "title": "掃描與分析範例程式碼",
        "report": {
            "outcome": {"status": "no_change", "changed_files": []},
            "validation": {"scanner_summary": {"files": 5}},
            "limitations": [{"kind": "static_analysis"}],
            "plan": [{"step": 1, "action": "read"}],
        },
        "evidence": {"files": [{"path": "app/main.py"}], "parse_errors": []},
    }


def _sample_tree() -> dict:
    return {
        "root": "/some/root",
        "truncated": False,
        "children": [
            {
                "name": "app",
                "type": "dir",
                "path": "app",
                "children": [
                    {"name": "main.py", "type": "file", "path": "app/main.py", "size": 10},
                ],
            },
        ],
    }


def _run_supervisor(command, timeout_ms=8000, test_timeout_ms=12000):
    """Run one supervised request and return the first outcome signal."""
    _app()
    loop = QEventLoop()
    outcome = {}
    supervisor = BackendSupervisor(command=command, timeout_ms=timeout_ms)

    def done(status, **detail):
        if outcome:
            return
        outcome["status"] = status
        outcome.update(detail)
        loop.quit()

    supervisor.completed.connect(lambda cid, res: done("success", cid=cid, result=res))
    supervisor.failed.connect(lambda cid, reason: done("failed", cid=cid, reason=reason))
    supervisor.blocked.connect(lambda cid: done("blocked", cid=cid))
    supervisor.unavailable.connect(lambda message: done("unavailable", message=message))

    safety = QTimer()
    safety.setSingleShot(True)
    safety.timeout.connect(lambda: done("test_timeout"))
    safety.start(test_timeout_ms)

    supervisor.submit("cid-test", build_request("cid-test", "fixtures"))
    loop.exec()
    safety.stop()
    supervisor.terminate()
    return outcome, supervisor


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class CodeViewTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_code_view_and_highlighter(self):
        view = CodeView()
        view.setPlainText("def f():\n    return 1  # comment\n")
        self.assertIsInstance(view._highlighter, PythonHighlighter)
        self.assertTrue(view.isReadOnly())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class MainWindowLayoutTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_window_starts_with_no_project(self):
        window = MainWindow()
        self.assertIsNone(window._root)
        self.assertEqual(window._project_label.text(), "No project open")

    def test_default_status_fields(self):
        window = MainWindow()
        self.assertIn("none", window._root_label.text())
        self.assertIn("Unverified", window._repo_label.text())
        self.assertIn("unavailable", window._provider_label.text())
        self.assertIn("idle", window._validation_label.text())
        self.assertIn("empty", window._twin_label.text())

    def test_twin_default_state_is_empty(self):
        window = MainWindow()
        self.assertEqual(window._twin_chip.text(), "Empty")
        self.assertIn("No Human-Readable Twin", window._twin_body.text())
        self.assertIn("empty", window._twin_label.text())

    def test_twin_state_transitions(self):
        window = MainWindow()
        window._set_twin_state(TWIN_STALE)
        self.assertEqual(window._twin_chip.text(), "Stale")
        self.assertIn("stale", window._twin_body.text())
        self.assertIn("stale", window._twin_label.text())

    def test_chat_composer_and_send_disabled(self):
        window = MainWindow()
        self.assertFalse(window._chat_composer.isEnabled())
        self.assertFalse(window._chat_send.isEnabled())

    def test_secondary_surfaces_present(self):
        window = MainWindow()
        for key in ("plan", "diff", "problems", "tests", "evidence"):
            self.assertIn(key, window._views)

    def test_open_project_sets_root_and_requests_tree(self):
        window = MainWindow()
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window._on_project_opened({"root": "/some/root", "repository_state": "Unverified"})
        self.assertEqual(window._root, "/some/root")
        self.assertEqual(window._project_label.text(), "/some/root")
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_TREE)

    def test_tree_load_populates_model(self):
        window = MainWindow()
        window._on_tree_loaded(_sample_tree())
        self.assertEqual(window._tree_model.rowCount(), 1)
        self.assertEqual(window._tree_model.item(0, 0).text(), "app")

    def test_tree_click_requests_document(self):
        window = MainWindow()
        window._on_tree_loaded(_sample_tree())
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        file_item = window._tree_model.item(0, 0).child(0)
        index = window._tree_model.indexFromItem(file_item)
        window._on_tree_clicked(index)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_DOCUMENT)
        self.assertEqual(sent[0]["path"], "app/main.py")

    def test_document_open_adds_tab(self):
        window = MainWindow()
        window._on_document_opened(
            "app/main.py",
            {"path": "app/main.py", "name": "main.py", "size": 10, "content": "print('hi')\n"},
        )
        self.assertEqual(window._source_tabs.count(), 1)
        self.assertEqual(window._current_document, "app/main.py")

    def test_scan_renders_secondary_surfaces(self):
        window = MainWindow()
        window._on_scan_completed(_sample_result())
        self.assertIn("read-only", window._views["diff"].toPlainText())
        self.assertIn("app/main.py", window._views["evidence"].toPlainText())
        self.assertIn("掃描與分析", window._views["evidence"].toPlainText())
        self.assertEqual(window._validation_state, VALIDATION_OK)

    def test_three_pane_primary_layout(self):
        window = MainWindow()
        splitter = window._horizontal_splitter
        self.assertEqual(splitter.count(), 3)
        self.assertEqual(splitter.widget(0), window._explorer_panel)
        self.assertEqual(splitter.widget(1), window._source_panel)
        self.assertEqual(splitter.widget(2), window._twin_panel)

    def test_chat_is_full_width_beneath_panes(self):
        window = MainWindow()
        self.assertEqual(window._vertical_splitter.count(), 2)
        # The lower area carries the chat header + body and the drawer; the chat
        # header is present and the chat body is not hidden by default (the
        # window is never shown in offscreen tests, so check the explicit-hidden
        # flag rather than isVisible()).
        self.assertIsNotNone(window._chat_header)
        self.assertFalse(window._chat_body.isHidden())

    def test_drawer_starts_collapsed(self):
        window = MainWindow()
        self.assertFalse(window._drawer_expanded)
        self.assertTrue(window._drawer_body.isHidden())
        self.assertFalse(window._drawer_toggle_button.isChecked())

    def test_drawer_toggles_expand(self):
        window = MainWindow()
        window._on_drawer_toggle()
        self.assertTrue(window._drawer_expanded)
        self.assertFalse(window._drawer_body.isHidden())
        self.assertTrue(window._drawer_toggle_button.isChecked())

    def test_source_starts_on_empty_state(self):
        window = MainWindow()
        self.assertEqual(window._source_stack.currentIndex(), 0)
        window._on_document_opened(
            "a.py", {"path": "a.py", "name": "a.py", "size": 6, "content": "x = 1\n"}
        )
        self.assertEqual(window._source_stack.currentIndex(), 1)

    def test_scan_button_disabled_until_project_open(self):
        window = MainWindow()
        self.assertFalse(window.scan_button.isEnabled())
        window._on_project_opened({"root": "/some/root", "repository_state": "Unverified"})
        self.assertTrue(window.scan_button.isEnabled())

    def test_status_bar_single_row_text(self):
        window = MainWindow()
        self.assertEqual(window.status_label.text(), "Status: idle — ready")

    def test_failed_state(self):
        window = MainWindow()
        window._on_open_failed("path_not_found")
        self.assertIn("failed", window.status_label.text())

    def test_blocked_state(self):
        window = MainWindow()
        window._on_blocked("cid-1")
        self.assertIn("blocked", window.status_label.text())

    def test_unavailable_state(self):
        window = MainWindow()
        window._on_unavailable("backend failed to start")
        self.assertIn("unavailable", window.status_label.text())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class BackendSupervisorTests(unittest.TestCase):
    def test_completes_real_backend(self):
        outcome, _ = _run_supervisor(None)  # None -> resolve_backend_command()
        self.assertEqual(outcome.get("status"), "success")
        self.assertEqual(outcome["result"]["task_id"], "P3.1")
        self.assertEqual(outcome["result"]["report"]["outcome"]["status"], "no_change")

    def test_non_json_stdout_marks_failed(self):
        outcome, _ = _run_supervisor([sys.executable, "-c", "print('not json')"])
        self.assertEqual(outcome.get("status"), "failed")
        self.assertEqual(outcome.get("reason"), "non_json_output")

    def test_early_exit_marks_failed(self):
        outcome, _ = _run_supervisor(
            [sys.executable, "-c", "import sys; sys.exit(3)"]
        )
        self.assertEqual(outcome.get("status"), "failed")
        self.assertEqual(outcome.get("reason"), "backend_exited")

    def test_request_timeout_marks_blocked(self):
        outcome, _ = _run_supervisor(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_ms=300,
            test_timeout_ms=8000,
        )
        self.assertEqual(outcome.get("status"), "blocked")
        self.assertEqual(outcome.get("cid"), "cid-test")

    def test_oversized_stdout_marks_failed(self):
        outcome, _ = _run_supervisor(
            [sys.executable, "-c", "print('x' * 1200000)"],
            test_timeout_ms=15000,
        )
        self.assertEqual(outcome.get("status"), "failed")
        self.assertEqual(outcome.get("reason"), "message_too_large")


if __name__ == "__main__":
    unittest.main()
