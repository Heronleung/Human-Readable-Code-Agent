"""Tests for the PySide6 desktop client (P3.1), run offscreen.

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

    from hrca.client import (
        BackendSupervisor,
        CodeView,
        MainWindow,
        PythonHighlighter,
    )

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
        "task_id": "P3.1",
        "title": "掃描與分析範例程式碼",
        "report": {
            "outcome": {"status": "no_change", "changed_files": []},
            "validation": {"scanner_summary": {"files": 5}},
            "limitations": [{"kind": "static_analysis"}],
            "plan": [{"step": 1, "action": "read"}],
        },
        "evidence": {"files": [{"path": "app/main.py"}]},
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

    supervisor.submit("cid-test", "fixtures")
    loop.exec()
    safety.stop()
    supervisor.terminate()
    return outcome, supervisor


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class CodeViewTests(unittest.TestCase):
    def test_code_view_and_highlighter(self):
        view = CodeView()
        view.setPlainText("def f():\n    return 1  # comment\n")
        self.assertIsInstance(view._highlighter, PythonHighlighter)
        self.assertTrue(view.isReadOnly())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class MainWindowTests(unittest.TestCase):
    def test_window_renders_no_change_result(self):
        window = MainWindow(scan_path="fixtures")
        window._on_completed("cid-1", _sample_result())
        self.assertTrue(window.status_label.text().startswith("Status: success"))
        self.assertIn("no_change", window._views["outcome"].toPlainText())
        self.assertIn("app/main.py", window._views["evidence"].toPlainText())

    def test_window_renders_non_ascii_title(self):
        window = MainWindow(scan_path="fixtures")
        window._on_completed("cid-1", _sample_result())
        self.assertIn("掃描與分析", window._views["result"].toPlainText())

    def test_window_failed_state(self):
        window = MainWindow(scan_path="fixtures")
        window._on_failed("cid-1", "action_not_allowed")
        self.assertIn("failed", window.status_label.text())
        self.assertIn("action_not_allowed", window.status_label.text())

    def test_window_blocked_state(self):
        window = MainWindow(scan_path="fixtures")
        window._on_blocked("cid-1")
        self.assertIn("blocked", window.status_label.text())

    def test_window_unavailable_state(self):
        window = MainWindow(scan_path="fixtures")
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
