"""Tests for the PySide6 desktop client (P3.2), run offscreen.

These tests import PySide6 and are skipped when it is not installed, so the
core and its tests remain installable without Qt. Every test runs with
``QT_QPA_PLATFORM=offscreen`` so no display server is required.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, QProcess, QTimer, Qt, qInstallMessageHandler
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QPushButton,
        QStackedWidget,
        QToolButton,
        QWidget,
    )

    from hrca.core import contract
    from hrca.ui import style
    from hrca.ui.widgets import PythonHighlighter
    from hrca.ui.client import BackendSupervisor, CodeView, MainWindow, _NAV_LABELS, _SETTINGS_ACTIVE_LABEL, _SETTINGS_ADD_PROFILE, _SETTINGS_NO_PROFILES, _SETTINGS_RENAME, _SETTINGS_REPLACE
    from hrca.boundary.client_core import CREDENTIAL_ACTION_PENDING, CREDENTIAL_MASK, PROFILE_ACTION_MESSAGES, PROVIDER_STATUS_PENDING, STATE_BLOCKED, VALIDATION_OK, build_open_project_request, build_request, resolve_credential_host_command

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
                    {"name": "main.py", "type": "file", "path": "app/main.py",
                     "size": 10, "kind": "source"},
                    {"name": "data.json", "type": "file", "path": "app/data.json",
                     "size": 8, "kind": "preview"},
                    {"name": "image.png", "type": "file", "path": "app/image.png",
                     "size": 4, "kind": "binary"},
                    {"name": "blob.xyz", "type": "file", "path": "app/blob.xyz",
                     "size": 3, "kind": "unsupported"},
                    {
                        "name": "sub",
                        "type": "dir",
                        "path": "app/sub",
                        "children": [
                            {"name": "README.md", "type": "file",
                             "path": "app/sub/README.md", "size": 6, "kind": "source"},
                        ],
                    },
                ],
            },
            {"name": "empty_dir", "type": "dir", "path": "empty_dir", "children": []},
            {"name": "notes.txt", "type": "file", "path": "notes.txt",
             "size": 5, "kind": "preview"},
        ],
    }


def _run_supervisor(command, timeout_ms=8000, test_timeout_ms=12000,
                    request=None):
    """Run one supervised request and return the first outcome signal.

    ``request`` defaults to the read-only scan of ``fixtures``, so every
    caller that names only a command keeps its original behaviour.
    """
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

    supervisor.submit(
        "cid-test",
        request if request is not None else build_request("cid-test", "fixtures"),
    )
    loop.exec()
    safety.stop()
    supervisor.terminate()
    return outcome, supervisor


def _pump_until(predicate, timeout_s: float = 5.0) -> bool:
    """Pump the Qt event loop until ``predicate()`` is true, or time out.

    Bounded so a test whose child never reaches the expected state fails fast
    instead of hanging the suite.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


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

    def test_default_status_fields(self):
        window = MainWindow()
        self.assertIn("none", window._root_label.text())
        self.assertIn("Unverified", window._repo_label.text())
        self.assertIn("unavailable", window._provider_label.text())
        self.assertIn("idle", window._validation_label.text())


    def test_layout_builds_for_both_palettes_and_sizes(self):
        sizes = ((1024, 640), (1360, 840), (1920, 1080))
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for width, height in sizes:
                with self.subTest(palette=palette.name, size=(width, height)):
                    window = MainWindow(palette=palette)
                    window.resize(width, height)
                    window.show()
                    QApplication.processEvents()
                    self.assertIs(window._palette, palette)
                    # Four rail groups, anchored Settings, and the off-rail
                    # Project history page Home opens.
                    self.assertEqual(window._shell.stack.count(), 6)

    def test_main_window_uses_supplied_palette(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                window = MainWindow(palette=palette)
                self.assertIs(window._palette, palette)

    def test_agent_chat_owns_the_single_composer(self):
        from hrca.ui.components import Composer

        window = MainWindow()
        # The legacy disabled chat placeholder is gone, and the shell no longer
        # carries a global composer: Agent Chat owns the one input, and it is
        # enabled because proposing a plan sends nothing.
        self.assertFalse(hasattr(window, "_chat_composer"))
        self.assertFalse(hasattr(window, "_chat_send"))
        self.assertFalse(hasattr(window._shell, "composer"))
        chat = window._chat_destination
        self.assertIsInstance(chat.composer, Composer)
        self.assertTrue(chat.composer.send_button.isEnabled())
        for key in ("home", "work", "documents", "settings"):
            with self.subTest(destination=key):
                self.assertEqual(window._shell.page(key).findChildren(Composer), [])

    def test_secondary_surfaces_present(self):
        window = MainWindow()
        for key in ("plan", "diff", "problems", "tests", "evidence"):
            self.assertIn(key, window._views)

    def test_open_project_sets_root_without_loading_a_tree(self):
        window = MainWindow()
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window._on_project_opened({"root": "/some/root", "repository_state": "Unverified"})
        self.assertEqual(window._root, "/some/root")
        # Opening a project binds the workspace root and requests nothing: the
        # tree view it used to load is not part of this product surface.
        self.assertEqual(sent, [])


    def test_scan_renders_secondary_surfaces(self):
        window = MainWindow()
        window._on_scan_completed(_sample_result())
        self.assertIn("read-only", window._views["diff"].toPlainText())
        self.assertIn("app/main.py", window._views["evidence"].toPlainText())
        self.assertIn("掃描與分析", window._views["evidence"].toPlainText())
        self.assertEqual(window._validation_state, VALIDATION_OK)


    def test_the_rail_is_the_primary_navigation(self):
        window = MainWindow()
        self.assertIsInstance(window._shell.stack, QStackedWidget)
        self.assertEqual(window._shell.stack.count(), 6)
        # Home is the landing destination.
        self.assertEqual(window._nav_destination, "home")
        self.assertTrue(window._shell._buttons["home"].isChecked())

    def test_first_use_offers_home_and_settings_only(self):
        window = MainWindow()
        self.assertEqual(window._nav_destination, "home")
        # Project-dependent entries stay hidden until there is a project, so
        # first use is one obvious path rather than four inactive choices.
        for key in ("chat", "work", "documents"):
            with self.subTest(key=key):
                self.assertTrue(window._shell._buttons[key].isHidden())
        self.assertFalse(window._shell._buttons["home"].isHidden())
        self.assertFalse(window._shell._buttons["settings"].isHidden())

    def test_there_is_no_advanced_disclosure(self):
        window = MainWindow()
        self.assertFalse(hasattr(window, "_advanced_button"))
        self.assertFalse(hasattr(window, "_nav_group_container"))


    def test_scan_action_appears_only_once_a_project_is_open(self):
        window = MainWindow()
        self.assertTrue(window.scan_button.isHidden())
        window._on_project_opened({"root": "/some/root", "repository_state": "Unverified"})
        self.assertFalse(window.scan_button.isHidden())
        self.assertTrue(window.scan_button.isEnabled())

    def test_scan_button_tooltip_is_explanatory(self):
        window = MainWindow()
        self.assertEqual(
            window.scan_button.toolTip(),
            "Run a local read-only scan of the open project.",
        )

    def test_scan_button_dispatches_local_read_only_scan(self):
        window = MainWindow()
        window._on_project_opened({"root": "/some/root", "repository_state": "Unverified"})
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window.scan_button.click()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SCAN)
        self.assertIn("read", sent[0]["task"]["allowed_actions"])
        self.assertNotIn("edit", sent[0]["task"]["allowed_actions"])
        self.assertNotIn("secret", contract.dumps(sent[0]))

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

    # -- direct geometry / elision contracts -----------------------------

    def _laid_out_window(self, palette, width, height):
        """Create, size, show and settle a MainWindow for geometry assertions."""
        window = MainWindow(palette=palette)
        window.resize(width, height)
        window.show()
        QApplication.processEvents()
        return window

    def _assert_geometry(self, palette, width, height):
        window = self._laid_out_window(palette, width, height)

        # The workspace opens on Home, which is laid out at every size.
        self.assertEqual(window._shell.stack.currentIndex(), 0)

        # The Documents destination holds a full-height, usable editor.
        window._select_destination("documents")
        QApplication.processEvents()
        self.assertGreater(window._document_editor.width(), 0)
        self.assertGreater(window._document_editor.height(), 0)

        # The rail's Home destination is laid out (a labelled column).
        self.assertGreater(window._shell._buttons["home"].width(), 0)

        # The status bar remains one fixed-height row at the bottom.
        status_bar = window.findChild(QWidget, "statusBar")
        self.assertIsNotNone(status_bar)
        self.assertEqual(status_bar.height(), style.STATUS_BAR_HEIGHT)

        # Every destination still pages to a laid-out, non-empty surface at
        # each size: the rail never selects a page it cannot show.
        for key in window._shell._buttons:
            window._select_destination(key)
            QApplication.processEvents()
            page = window._shell.stack.currentWidget()
            self.assertGreater(page.width(), 0)
            self.assertGreater(page.height(), 0)

    def test_geometry_matrix_across_palettes_and_sizes(self):
        # The narrowest size is above the rail + library-explorer squeeze point
        # so every destination page still holds a usable geometry.
        sizes = ((1440, 640), (1680, 840), (1920, 1080))
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for width, height in sizes:
                with self.subTest(palette=palette.name, size=(width, height)):
                    self._assert_geometry(palette, width, height)

    def test_long_path_root_and_file_elide_middle(self):
        window = MainWindow()
        window._root = (
            "/home/heron/projects/Human-Readable-Code-Agent/src/hrca/"
            "very_deeply_nested_directory_structure/level_one/level_two/"
            "level_three/level_four/level_five/final_target_project_root"
        )
        window._update_status()
        window.resize(1360, 840)
        window.show()
        QApplication.processEvents()

        for name, label, full in (
            ("root", window._root_label, f"Root: {window._root}"),
        ):
            with self.subTest(field=name):
                # The complete value is preserved un-elided in the tooltip and
                # the accessible full-text metadata.
                self.assertEqual(label.fullText(), full)
                self.assertEqual(label.toolTip(), full)
                # The visible text is shorter (elided), middle-elided with the
                # recognizable beginning and ending retained, and never wraps.
                displayed = label.text()
                self.assertLess(len(displayed), len(full))
                self.assertIn("…", displayed)
                self.assertTrue(displayed.startswith(full[:6]))
                self.assertTrue(displayed.endswith(full[-6:]))
                self.assertFalse(label.wordWrap())
                self.assertNotIn("\n", displayed)

    def test_long_path_fields_stay_on_one_status_row(self):
        window = MainWindow()
        window._root = "/home/heron/projects/Human-Readable-Code-Agent/" + "x" * 120
        window._update_status()
        # The footer is hidden at idle and appears for a warning or failure;
        # its diagnostic row is collapsed until Details is toggled.
        self.assertTrue(window._shell._footer.isHidden())
        window._set_status(STATE_BLOCKED, "diagnostics under test")
        self.assertFalse(window._shell._footer.isHidden())
        self.assertTrue(all(lbl.isHidden() for lbl in window._diagnostic_labels))
        window._status_details_button.setChecked(True)
        self.assertTrue(all(not lbl.isHidden() for lbl in window._diagnostic_labels))
        window.resize(1360, 840)
        window.show()
        QApplication.processEvents()

        root_label = window._root_label
        repo_label = window._repo_label
        # Both fields remain visible and take a non-zero width on the row.
        self.assertTrue(root_label.isVisible())
        self.assertTrue(repo_label.isVisible())
        self.assertGreater(root_label.width(), 0)
        self.assertGreater(repo_label.width(), 0)
        # They share one status row (the same vertical position in the bar).
        status_bar = window.findChild(QWidget, "statusBar")
        self.assertIsNotNone(status_bar)
        root_y = root_label.mapTo(status_bar, root_label.rect().topLeft()).y()
        repo_y = repo_label.mapTo(status_bar, repo_label.rect().topLeft()).y()
        self.assertAlmostEqual(root_y, repo_y, delta=1)


def _source_doc(rel_path: str) -> dict:
    """A bounded ``get_document`` result for a Python source file."""
    name = rel_path.rsplit("/", 1)[-1]
    return {"path": rel_path, "name": name, "size": 10,
            "kind": "source", "content": "print('hi')\n"}


def _block(block_id: str, block_type: str, **overrides) -> dict:
    """A bounded procedural block with full source correspondence.

    The default is a verified, current, high-confidence block anchored to
    ``app/service.py``; tests override ``editability``, ``display_text`` and
    ``payload`` to exercise the purpose/decision inline edit rows.
    """
    block = {
        "block_id": block_id,
        "block_type": block_type,
        "parent_id": None,
        "order": 0,
        "subject": block_type,
        "payload": {},
        "display_text": block_type,
        "source_anchors": [
            {"file": "app/service.py", "lineno": 1, "col_offset": 0,
             "end_lineno": 1, "end_col_offset": 0, "source_id": "app/service.py:1"}
        ],
        "baseline_revision": "abc123",
        "source_fingerprint": "fp-" + block_id,
        "provenance": "verified",
        "confidence": "high",
        "confidence_reason": None,
        "editability": None,
        "state": "current",
        "language_version": "0.1",
    }
    block.update(overrides)
    return block


def _code_map_result() -> dict:
    """A bounded ``get_code_map`` result for ``app/service.py``.

    Carries a module entity, a method entity, an editable purpose block and an
    editable decision block, so the read-mode document and the edit surface both
    render without touching the boundary, Twin store or filesystem.
    """
    module_id = "codemap:app.service:entity:0"
    method_id = "codemap:app.service.Service.handle:entity:0"
    purpose_id = "codemap:app.service.Service.handle:purpose:1"
    decision_id = "codemap:app.service.Service.handle:decision:2"
    return {
        "language_version": "0.1",
        "generator": "hrca-codemap",
        "entity": "app.service.Service.handle",
        "entities": [
            {"block_id": module_id, "locator": "app.service", "kind": "module",
             "name": "app.service", "subject": "Module app.service",
             "parent_id": None, "order": 0},
            {"block_id": method_id, "locator": "app.service.Service.handle",
             "kind": "method", "name": "handle", "subject": "Method handle(request)",
             "parent_id": module_id, "order": 1},
        ],
        "blocks": [
            _block(module_id, "entity", parent_id=None, order=0,
                   subject="Module app.service", display_text="Module app.service",
                   payload={"name": "app.service", "kind": "module",
                            "locator": "app.service"}),
            _block(method_id, "entity", parent_id=module_id, order=1,
                   subject="Method handle(request)",
                   display_text="Method handle(request)",
                   payload={"name": "handle", "kind": "method",
                            "locator": "app.service.Service.handle"}),
            _block(purpose_id, "purpose", parent_id=method_id, order=2,
                   subject="Handles a request", display_text="Handles a request",
                   editability="replace_description",
                   payload={"text": "Handles a request"}),
            _block(decision_id, "decision", parent_id=method_id, order=3,
                   subject="If request is valid, the following runs:",
                   display_text="If request is valid, the following runs:",
                   editability="replace_condition_intent",
                   payload={"condition": "request is valid"}),
        ],
        "document": "Module app.service\n\nMethod handle(request)\n\n"
                    "Handles a request\n\nIf request is valid, the following runs:",
        "baseline": {"workspace_id": "ws-1", "baseline_revision": "abc123",
                     "scan_generation": 1, "sync_state": "synchronized"},
        "draft": None,
        "conflict": {"state": "none", "reason": None},
    }


def _function_code_map_result() -> dict:
    """A bounded ``get_code_map`` result for ``calculator.py`` with two functions."""
    module_id = "codemap:calculator:entity:0"
    add_id = "codemap:calculator.add:entity:0"
    divide_id = "codemap:calculator.divide:entity:0"
    return {
        "language_version": "0.1",
        "generator": "hrca-codemap",
        "entity": "calculator",
        "entities": [
            {"block_id": module_id, "locator": "calculator", "kind": "module",
             "name": "calculator", "subject": "Module calculator",
             "parent_id": None, "order": 0},
            {"block_id": add_id, "locator": "calculator.add", "kind": "function",
             "name": "add",
             "subject": "Function add(left: float, right: float) -> float",
             "parent_id": module_id, "order": 1},
            {"block_id": divide_id, "locator": "calculator.divide", "kind": "function",
             "name": "divide",
             "subject": "Function divide(left: float, right: float) -> float",
             "parent_id": module_id, "order": 2},
        ],
        "blocks": [
            _block(module_id, "entity", parent_id=None, order=0,
                   subject="Module calculator", display_text="Module calculator",
                   payload={"name": "calculator", "kind": "module",
                            "locator": "calculator"}),
            _block(add_id, "entity", parent_id=module_id, order=1,
                   subject="Function add(left: float, right: float) -> float",
                   display_text="Function add(left: float, right: float) -> float",
                   payload={"name": "add", "kind": "function", "locator": "calculator.add"}),
            _block(divide_id, "entity", parent_id=module_id, order=2,
                   subject="Function divide(left: float, right: float) -> float",
                   display_text="Function divide(left: float, right: float) -> float",
                   payload={"name": "divide", "kind": "function", "locator": "calculator.divide"}),
        ],
        "document": "Module calculator\n\n"
                    "Function add(left: float, right: float) -> float\n\n"
                    "Function divide(left: float, right: float) -> float",
        "baseline": {"workspace_id": "ws-1", "baseline_revision": "abc123",
                     "scan_generation": 1, "sync_state": "synchronized"},
        "draft": None,
        "conflict": {"state": "none", "reason": None},
    }


def _helpers_code_map_result() -> dict:
    """A bounded ``get_code_map`` result for ``helpers.py`` (one function)."""
    module_id = "codemap:helpers:entity:0"
    func_id = "codemap:helpers.fmt:entity:0"
    return {
        "language_version": "0.1",
        "generator": "hrca-codemap",
        "entity": "helpers",
        "entities": [
            {"block_id": module_id, "locator": "helpers", "kind": "module",
             "name": "helpers", "subject": "Module helpers",
             "parent_id": None, "order": 0},
            {"block_id": func_id, "locator": "helpers.fmt", "kind": "function",
             "name": "fmt", "subject": "Function fmt(value) -> str",
             "parent_id": module_id, "order": 1},
        ],
        "blocks": [
            _block(module_id, "entity", parent_id=None, order=0,
                   subject="Module helpers", display_text="Module helpers",
                   payload={"name": "helpers", "kind": "module", "locator": "helpers"}),
            _block(func_id, "entity", parent_id=module_id, order=1,
                   subject="Function fmt(value) -> str",
                   display_text="Function fmt(value) -> str",
                   payload={"name": "fmt", "kind": "function", "locator": "helpers.fmt"}),
        ],
        "document": "Module helpers\n\nFunction fmt(value) -> str",
        "baseline": {"workspace_id": "ws-1", "baseline_revision": "abc123",
                     "scan_generation": 1, "sync_state": "synchronized"},
        "draft": None,
        "conflict": {"state": "none", "reason": None},
    }


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class NavigationRailTests(unittest.TestCase):
    """P4.4a document-first navigation rail.

    The six always-visible bottom tabs are replaced by a compact labelled rail:
    Document and Preview are the only always-visible primary destinations, then
    a divider, Versions, and a collapsed Advanced disclosure grouping
    Change Review / Validation Evidence. Every retained surface stays reachable
    and no output is silently discarded.
    """

    def setUp(self):
        _app()

    def _laid_out(self, palette, width, height):
        window = MainWindow(palette=palette)
        window.resize(width, height)
        window.show()
        QApplication.processEvents()
        return window

    # -- widget hierarchy and legacy removal -----------------------------

    def test_nav_rail_hierarchy(self):
        window = MainWindow()
        self.assertIsInstance(window._shell.stack, QStackedWidget)
        # Four rail groups + anchored Settings + the off-rail history page.
        self.assertEqual(window._shell.stack.count(), 6)
        self.assertEqual(
            set(window._shell._buttons),
            {"home", "chat", "work", "documents", "settings"},
        )
        # Jobs, Agents and Review are Work's contextual views, not rail entries.
        for key in ("jobs", "agents", "review"):
            with self.subTest(key=key):
                self.assertNotIn(key, window._shell._buttons)
        self.assertIsNotNone(window._shell.page("history"))
        # The Advanced disclosure and its collapsed group are gone.
        self.assertFalse(hasattr(window, "_advanced_button"))
        self.assertFalse(hasattr(window, "_nav_group_container"))

    def test_legacy_bottom_panel_removed(self):
        window = MainWindow()
        for attr in (
            "_bottom_panel",
            "_bottom_tabs",
            "_bottom_body",
            "_bottom_panel_header",
            "_disclosure_button",
            "_vertical_splitter",
        ):
            self.assertFalse(hasattr(window, attr), f"legacy attribute {attr} remains")
        for method in (
            "_on_bottom_tab_changed",
            "_toggle_expanded",
            "_set_expanded",
            "_update_disclosure",
        ):
            self.assertFalse(hasattr(window, method), f"legacy method {method} remains")

    def test_every_rail_group_is_visible_once_started(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        window._on_project_opened({"root": "/some/root", "repository_state": "Unverified"})
        for key in ("home", "chat", "work", "documents", "settings"):
            with self.subTest(key=key):
                self.assertFalse(window._shell._buttons[key].isHidden())

    # -- destination selection -------------------------------------------

    def test_destination_selection_switches_stack_and_checks_button(self):
        window = MainWindow()
        for index, key in enumerate(
            ("home", "chat", "work", "documents", "settings")
        ):
            with self.subTest(key=key):
                window._select_destination(key)
                self.assertEqual(window._nav_destination, key)
                self.assertEqual(window._shell.stack.currentIndex(), index)
                self.assertTrue(window._shell._buttons[key].isChecked())

    def test_document_selection_refreshes_documents(self):
        window = MainWindow()
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window._select_destination("documents")
        self.assertEqual(sent[-1]["action"], contract.ACTION_LIBRARY_GET)

    def test_preview_selection_refreshes_preview(self):
        window = MainWindow()
        window._document_id = "doc:d1"
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window._select_destination("review")
        preview_requests = [
            r for r in sent if r["action"] == contract.ACTION_DOCUMENT_PREVIEW
        ]
        self.assertEqual(len(preview_requests), 1)

    def test_review_rehomes_the_retained_technical_surfaces(self):
        window = MainWindow()
        # The plan/diff projection, the raw candidate metadata and the scan
        # evidence are all reachable from Review, which Work groups.
        for key in ("plan", "diff", "problems", "tests", "evidence"):
            self.assertIn(key, window._views)
        self.assertIsNotNone(window._document_result)
        review = window._review_destination
        self.assertIs(window._shell.page("work"), window._work_destination)
        for view in window._views.values():
            with self.subTest(view=id(view)):
                self.assertTrue(review.isAncestorOf(view))

    # -- accessibility ----------------------------------------------------

    def test_scan_output_still_reaches_secondary_views(self):
        window = MainWindow()
        window._on_scan_completed(_sample_result())
        self.assertIn("read-only", window._views["diff"].toPlainText())
        self.assertIn("app/main.py", window._views["evidence"].toPlainText())

    # -- accessibility ----------------------------------------------------

    def test_nav_buttons_are_labelled_and_focusable(self):
        window = MainWindow()
        for key, button in window._shell._buttons.items():
            self.assertEqual(button.accessibleName(), _NAV_LABELS[key])
            self.assertTrue(button.isCheckable())
            self.assertNotEqual(button.focusPolicy(), Qt.NoFocus)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class BackendSupervisorTests(unittest.TestCase):
    def test_completes_real_backend(self):
        outcome, _ = _run_supervisor(None)  # None -> resolve_backend_command()
        self.assertEqual(outcome.get("status"), "success")
        self.assertEqual(outcome["result"]["task_id"], "P3.1")
        self.assertEqual(outcome["result"]["report"]["outcome"]["status"], "no_change")

    def test_open_project_completes_against_the_real_backend(self):
        # The desktop-to-backend project-open exchange, end to end: the client
        # resolves the source-checkout backend command, starts it as a real child
        # process, and one ``open_project`` request comes back with the root it
        # accepted. Before UI-TRANSITION-1R the launch died with
        # "No module named hrca.boundary.__main__" and no response ever arrived,
        # so project open was only unit-verified.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        outcome, _ = _run_supervisor(
            None, request=build_open_project_request("cid-test", root)
        )
        self.assertEqual(outcome.get("status"), "success")
        self.assertEqual(outcome["result"]["root"], root)

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

    def test_gc_reaps_backend_without_warning(self):
        # Regression: a supervisor collected without an explicit terminate()
        # (the closeEvent path) must still reap its QProcess, so Qt never
        # warns "QProcess: Destroyed while process is still running".
        _app()
        captured = []
        previous = qInstallMessageHandler(
            lambda msg_type, context, message: captured.append(message)
        )
        try:
            supervisor = BackendSupervisor(
                command=[sys.executable, "-c", "import time; time.sleep(30)"]
            )
            supervisor.submit("cid-gc", build_request("cid-gc", "fixtures"))
            del supervisor
            gc.collect()
        finally:
            qInstallMessageHandler(previous)
        self.assertFalse(
            any("Destroyed while process" in message for message in captured),
            captured,
        )

    def test_terminate_reaps_running_child_without_warning(self):
        # A proven-running child must be reaped on teardown: the child reaches
        # QProcess.Running, terminate() brings it to NotRunning, the supervisor
        # drops its QProcess reference (idempotent cleanup), and Qt emits no
        # "QProcess: Destroyed while process is still running" warning. Qt
        # messages are captured unfiltered via qInstallMessageHandler.
        _app()
        captured = []
        previous = qInstallMessageHandler(
            lambda msg_type, context, message: captured.append(message)
        )
        try:
            supervisor = BackendSupervisor(
                command=[sys.executable, "-c", "import time; time.sleep(30)"]
            )
            supervisor.submit("cid-run", build_request("cid-run", "fixtures"))

            # Fail if the child never reaches Running.
            reached_running = _pump_until(
                lambda: supervisor._proc is not None
                and supervisor._proc.state() == QProcess.Running
            )
            self.assertTrue(reached_running, "child never reached QProcess.Running")

            proc = supervisor._proc
            self.assertIsNotNone(proc)

            supervisor.terminate()

            # Fail if the child remains running after teardown, and prove the
            # cleanup is idempotent (the QProcess reference is dropped).
            self.assertEqual(proc.state(), QProcess.NotRunning)
            self.assertIsNone(supervisor._proc)
        finally:
            qInstallMessageHandler(previous)

        # Fail if Qt warned that the QProcess was destroyed while still running.
        self.assertFalse(
            any("Destroyed while process" in message for message in captured),
            captured,
        )


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class CredentialHostLaunchTests(unittest.TestCase):
    """The Settings add-credential flow launches a real child process.

    UI-TRANSITION-1C: the resolver named ``-m hrca.credential_host``, a module
    that does not exist, so the launch died at dispatch with
    "No module named hrca.credential_host" before the host could answer.

    These tests start the *real* resolved command and prove it reaches the
    host's own request handling. No credential is requested, supplied, read,
    written or logged: the request stream is empty, or carries one malformed
    line, which the host answers with a bounded secret-free envelope before it
    ever looks at a store, a sheet or the Credential Manager.
    """

    def setUp(self):
        _app()

    def _launch(self, request_text):
        return subprocess.run(
            resolve_credential_host_command(frozen=False),
            input=request_text, capture_output=True, text=True, timeout=60,
        )

    def test_empty_request_stream_exits_cleanly(self):
        # No request at all: the host reads EOF and exits without touching
        # anything. The point is that the *process* started.
        proc = self._launch("")
        self.assertNotIn("No module named", proc.stderr)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual("", proc.stdout.strip())

    def test_one_malformed_line_reaches_the_host_request_handler(self):
        # The envelope can only have been produced by credential_host.main, so
        # it is the proof that dispatch reached the host rather than failing
        # earlier. A malformed request names no operation, so the native prompt
        # and the Credential Manager write are unreachable from it.
        proc = self._launch("not-a-request\n")
        self.assertNotIn("No module named", proc.stderr)
        self.assertEqual(proc.returncode, 0)
        envelope = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "malformed_request")
        self.assertEqual(envelope["contract_version"], contract.CONTRACT_VERSION)
        self.assertNotIn("hwnd", json.dumps(envelope))


class ProviderReadinessGuiTests(unittest.TestCase):
    """P4.2a redacted provider readiness presentation (offscreen).

    There is no manual Provider status command: local readiness is refreshed
    automatically (startup, Settings open, credential/profile changes) through
    the local boundary as a ``get_profiles`` request. The desktop surfaces only
    the bounded readiness state — never a credential, endpoint or network claim.
    """

    def setUp(self):
        _app()

    def _fake_send(self, window):
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        return sent

    def _fake_send_credential(self, window):
        sent = []

        def fake_send_credential(request, on_success, on_error):
            sent.append(request)
            return True

        window._send_credential = fake_send_credential
        return sent

    def test_provider_button_is_absent(self):
        window = MainWindow()
        self.assertFalse(hasattr(window, "provider_button"))

    def test_settings_is_a_rail_destination_before_any_project_opens(self):
        window = MainWindow()
        # Settings moved from a command-bar ghost action to its own rail entry.
        self.assertFalse(hasattr(window, "settings_button"))
        settings = window._shell._buttons["settings"]
        self.assertEqual(settings.accessibleName(), "Settings")
        # The context bar carries the workspace actions and the readiness chip,
        # in one right-aligned row: primary action, then the chip, then scan.
        bar = window._shell.context_bar.layout()
        self.assertLess(
            bar.indexOf(window._shell.context_bar.primary_button),
            bar.indexOf(window._provider_status_label),
        )
        self.assertLess(
            bar.indexOf(window._provider_status_label),
            bar.indexOf(window.scan_button),
        )

    def test_provider_status_region_is_reserved(self):
        window = MainWindow()
        self.assertIsNotNone(window._provider_status_label)
        # Blank until a refresh runs; the region itself is permanently mounted.
        self.assertEqual(window._provider_status_label.text(), "")

    def test_refresh_profiles_sends_get_profiles(self):
        window = MainWindow()
        sent = self._fake_send(window)
        window._refresh_profiles()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_PROFILES)
        self.assertNotIn("path", sent[0])
        self.assertNotIn("task", sent[0])
        self.assertNotIn("secret", contract.dumps(sent[0]))

    def test_profiles_ready_updates_provider_state(self):
        window = MainWindow()
        self.assertIn("unavailable", window._provider_label.text())
        window._on_profiles_ready(
            {
                "state": "configured",
                "provider_id": "deepseek",
                "model": "deepseek-flash",
                "profiles": [],
                "active_profile_id": None,
                "credential_present": True,
                "authenticated": False,
                "online": False,
                "executable": False,
                "migrated": False,
                "store_available": True,
            }
        )
        self.assertEqual(window._provider_state, "configured")
        self.assertIn("configured", window._provider_label.text())
        self.assertIn("Configured", window.status_label.text())

    def test_profiles_ready_missing_credential(self):
        window = MainWindow()
        window._on_profiles_ready(
            {
                "state": "missing_credential",
                "provider_id": "deepseek",
                "model": "deepseek-flash",
                "profiles": [],
                "active_profile_id": None,
                "credential_present": False,
                "authenticated": False,
                "online": False,
                "executable": False,
                "migrated": False,
                "store_available": True,
            }
        )
        self.assertEqual(window._provider_state, "missing_credential")
        self.assertIn("missing_credential", window._provider_label.text())

    def test_profiles_error_sets_failed_status(self):
        window = MainWindow()
        window._on_profiles_error("invalid_config")
        self.assertIn("failed", window.status_label.text())
        self.assertIn("invalid_config", window.status_label.text())

    def test_profiles_ready_updates_provider_status_region(self):
        window = MainWindow()
        window._on_profiles_ready(
            {
                "state": "configured",
                "provider_id": "deepseek",
                "model": "deepseek-flash",
                "profiles": [],
                "active_profile_id": None,
                "credential_present": True,
                "authenticated": False,
                "online": False,
                "executable": False,
                "migrated": False,
                "store_available": True,
            }
        )
        self.assertIn("DeepSeek is configured locally", window._provider_status_label.text())
        self.assertEqual(window._provider_model, "deepseek-flash")
        self.assertTrue(window._provider_credential_present)

    def test_profiles_error_sets_provider_status_failed(self):
        window = MainWindow()
        window._on_profiles_error("invalid_config")
        self.assertIn("Provider check failed", window._provider_status_label.text())

    def test_show_credential_result_stored_updates_status(self):
        window = MainWindow()
        self.assertIsNotNone(window._settings_surface)
        window._show_credential_result({"state": "stored", "credential_present": True})
        self.assertEqual(window._settings_action_status.text(), "API key stored securely.")

    def test_show_credential_result_cancelled_is_bounded(self):
        window = MainWindow()
        self.assertIsNotNone(window._settings_surface)
        window._show_credential_result({"state": "cancelled", "credential_present": False})
        self.assertEqual(
            window._settings_action_status.text(),
            "No change — the secure prompt was cancelled.",
        )


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class SettingsDialogTests(unittest.TestCase):
    """P4.2a credential-profile manager Settings surface (offscreen).

    The Settings control is text-only (no gear), the four toolbar controls are
    compact peers of one shared height, the dialog is a sizeable left-nav sheet
    of five honest sections, and the Provider page owns only non-secret profile
    metadata: an active-credential selector, an Add action, and a card per saved
    profile with a fixed read-only mask plus Rename / Replace / Delete controls.
    Every action visibly reports its pending state and bounded outcome.
    """

    def setUp(self):
        _app()

    def _dialog(self, window):
        self.assertIsNotNone(window._settings_surface)
        return window._settings_surface

    def _fake_send(self, window):
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        return sent

    def _fake_send_credential(self, window):
        sent = []

        def fake_send_credential(request, on_success, on_error):
            sent.append(request)
            return True

        window._send_credential = fake_send_credential
        return sent

    def _profile(self, profile_id="a" * 32, name="Work", present=True):
        return {
            "profile_id": profile_id,
            "provider_id": "deepseek",
            "display_name": name,
            "credential_present": present,
        }

    def test_the_settings_rail_entry_is_text_only_without_icon(self):
        window = MainWindow()
        button = window._shell._buttons["settings"]
        self.assertIn("Settings", button.text())
        self.assertTrue(button.icon().isNull())
        self.assertEqual(button.accessibleName(), "Settings")

    def test_context_bar_controls_share_one_compact_height(self):
        window = MainWindow()
        for button in (window.scan_button, window._shell.context_bar.primary_button):
            with self.subTest(button=button.text()):
                self.assertEqual(button.minimumHeight(), style.COMMAND_BAR_BUTTON_HEIGHT)
                self.assertEqual(button.maximumHeight(), style.COMMAND_BAR_BUTTON_HEIGHT)

    def test_context_bar_uses_the_named_action_vocabulary(self):
        # The context bar carries explicit roles rather than one undifferentiated
        # button class: the safest next action and the scan are secondary peers.
        window = MainWindow()
        self.assertEqual(
            window._shell.context_bar.primary_button.objectName(), "secondaryButton"
        )
        self.assertEqual(window.scan_button.objectName(), "secondaryButton")

    def test_settings_surface_is_a_destination_page(self):
        # Settings is a rail destination now, not a modal dialog: its surface
        # lives inside the settings page and keeps all five sections.
        window = MainWindow()
        surface = self._dialog(window)
        self.assertIsNotNone(surface)
        self.assertEqual(window._settings_stack.count(), 5)
        self.assertTrue(window._shell.page("settings").isAncestorOf(surface))

    def test_navigation_lists_five_sections_in_order(self):
        window = MainWindow()
        self._dialog(window)
        self.assertEqual(window._settings_nav.count(), 5)
        labels = [window._settings_nav.item(i).text() for i in range(5)]
        self.assertEqual(
            labels, ["Provider", "Appearance", "Workspace", "Privacy & Safety", "About"]
        )

    def test_navigation_switches_content_page(self):
        window = MainWindow()
        self._dialog(window)
        for i in range(5):
            window._settings_nav.setCurrentRow(i)
            QApplication.processEvents()
            self.assertEqual(window._settings_stack.currentIndex(), i)

    def test_provider_page_shows_fixed_identity_and_model(self):
        window = MainWindow()
        self._dialog(window)
        window._provider_model = "deepseek-flash"
        window._refresh_settings_dialog()
        self.assertEqual(window._settings_provider_value.text(), "DeepSeek")
        self.assertEqual(window._settings_model_value.text(), "deepseek-flash")

    def test_active_selector_is_present_and_accessible(self):
        window = MainWindow()
        self._dialog(window)
        self.assertIsNotNone(window._settings_active_combo)
        self.assertEqual(
            window._settings_active_combo.accessibleName(), _SETTINGS_ACTIVE_LABEL
        )

    def test_add_button_is_present_and_accessible(self):
        window = MainWindow()
        self._dialog(window)
        self.assertEqual(window._add_profile_button.text(), _SETTINGS_ADD_PROFILE)
        self.assertEqual(
            window._add_profile_button.accessibleName(), _SETTINGS_ADD_PROFILE
        )

    def test_empty_state_shows_no_profiles(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = []
        window._refresh_settings_dialog()
        layout = window._settings_profiles_layout
        # Only widgets count; the trailing stretch is a spacer, not a widget.
        labels = [
            layout.itemAt(i).widget()
            for i in range(layout.count())
            if layout.itemAt(i).widget() is not None
        ]
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].text(), _SETTINGS_NO_PROFILES)

    def test_profiles_render_name_provider_and_fixed_mask(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [self._profile(name="Work")]
        window._refresh_settings_dialog()
        card = window._settings_profiles_layout.itemAt(0).widget()
        self.assertEqual(card.objectName(), "profileCard")
        texts = [label.text() for label in card.findChildren(QLabel)]
        self.assertIn("Work", texts)
        self.assertTrue(any(CREDENTIAL_MASK in text for text in texts))
        self.assertTrue(any("deepseek" in text for text in texts))
        joined = " ".join(texts)
        self.assertNotIn("secret", joined)
        self.assertNotIn("api_key", joined)

    def test_profile_card_controls_are_accessible(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [self._profile(name="Work")]
        window._refresh_settings_dialog()
        card = window._settings_profiles_layout.itemAt(0).widget()
        buttons = card.findChildren(QPushButton)
        texts = {button.text() for button in buttons}
        self.assertIn(_SETTINGS_RENAME, texts)
        self.assertIn(_SETTINGS_REPLACE, texts)
        for button in buttons:
            self.assertTrue(button.accessibleName())

    def test_delete_control_is_icon_only_accessible_and_focusable(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [self._profile(name="Work")]
        window._refresh_settings_dialog()
        card = window._settings_profiles_layout.itemAt(0).widget()
        deletes = card.findChildren(QToolButton)
        self.assertEqual(len(deletes), 1)
        delete = deletes[0]
        self.assertEqual(delete.accessibleName(), "Delete Work")
        self.assertEqual(delete.toolTip(), "Delete Work")
        self.assertFalse(delete.icon().isNull())
        self.assertEqual(delete.text(), "")
        self.assertEqual(delete.minimumWidth(), style.PROFILE_ACTION_BUTTON_SIZE)
        self.assertEqual(delete.minimumHeight(), style.PROFILE_ACTION_BUTTON_SIZE)
        self.assertEqual(delete.focusPolicy(), Qt.StrongFocus)

    def test_add_profile_sends_manage_credential_without_profile_id(self):
        window = MainWindow()
        self._dialog(window)
        sent = self._fake_send_credential(window)
        window._on_add_profile()
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request["action"], contract.ACTION_MANAGE_CREDENTIAL)
        self.assertIsInstance(request["hwnd"], int)
        # Add mode carries no profile id or name — the native sheet collects both.
        self.assertNotIn("profile_id", request)
        self.assertNotIn("display_name", request)
        self.assertTrue(window._profile_action_pending)
        self.assertNotIn("secret", contract.dumps(request))

    def test_add_secret_stored_dispatches_add_profile_with_result_metadata(self):
        window = MainWindow()
        self._dialog(window)
        sent = self._fake_send(window)
        window._profile_action_pending = True
        window._on_add_secret_stored(
            {
                "state": "stored",
                "credential_present": True,
                "profile_id": "a" * 32,
                "display_name": "Work",
            }
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_ADD_PROFILE)
        self.assertEqual(sent[0]["profile_id"], "a" * 32)
        self.assertEqual(sent[0]["display_name"], "Work")

    def test_add_secret_cancelled_does_not_add_profile(self):
        window = MainWindow()
        self._dialog(window)
        sent = self._fake_send(window)
        window._profile_action_pending = True
        window._on_add_secret_stored(
            {"state": "cancelled", "credential_present": False}
        )
        self.assertEqual(len(sent), 0)
        self.assertFalse(window._profile_action_pending)

    def test_add_secret_invalid_metadata_ends_bounded(self):
        window = MainWindow()
        self._dialog(window)
        sent = self._fake_send(window)
        window._profile_action_pending = True
        window._on_add_secret_stored({"state": "stored", "credential_present": True})
        self.assertEqual(len(sent), 0)
        self.assertFalse(window._profile_action_pending)

    def test_replace_sends_manage_credential_with_profile_and_name(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [self._profile("a" * 32, "Work")]
        sent = self._fake_send_credential(window)
        window._on_replace_profile("a" * 32)
        self.assertEqual(len(sent), 1)
        request = sent[0]
        self.assertEqual(request["action"], contract.ACTION_MANAGE_CREDENTIAL)
        self.assertEqual(request["profile_id"], "a" * 32)
        self.assertEqual(request["display_name"], "Work")
        self.assertNotIn("secret", contract.dumps(request))

    def test_cards_keep_fixed_height_across_six_profiles(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [self._profile(f"{i:032x}", f"P{i}") for i in range(6)]
        window._refresh_settings_dialog()
        cards = [window._profile_cards[f"{i:032x}"]["card"] for i in range(6)]
        for card in cards:
            self.assertEqual(card.minimumHeight(), style.PROFILE_CARD_HEIGHT)
            self.assertEqual(card.maximumHeight(), style.PROFILE_CARD_HEIGHT)

    def test_profile_list_has_fixed_viewport(self):
        window = MainWindow()
        self._dialog(window)
        scroll = window._settings_profiles_scroll
        self.assertEqual(scroll.minimumHeight(), style.PROFILE_LIST_HEIGHT)
        self.assertEqual(scroll.maximumHeight(), style.PROFILE_LIST_HEIGHT)

    def test_cards_are_top_anchored_in_the_viewport(self):
        # Card zero must start at the viewport top (y == 0), not be vertically
        # centred, and stack downward with identical fixed gaps.
        window = MainWindow()
        self._dialog(window)
        window._select_destination("settings")
        window.show()
        window._settings_surface.show()
        window._profiles = [self._profile(f"{i:032x}", f"P{i}") for i in range(6)]
        window._refresh_settings_dialog()
        QApplication.processEvents()
        QApplication.processEvents()
        viewport = window._settings_profiles_scroll.viewport()
        ys = [
            window._profile_cards[f"{i:032x}"]["card"].mapTo(viewport, QPoint(0, 0)).y()
            for i in range(6)
        ]
        self.assertEqual(ys[0], 0)
        # Uniform 64px pitch = 56px card height + 8px gap.
        for upper, lower in zip(ys, ys[1:]):
            self.assertEqual(lower - upper, style.PROFILE_CARD_HEIGHT + style.GAP_TIGHT)

    def test_cards_are_not_rebuilt_on_refresh(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [self._profile("a" * 32, "A"), self._profile("b" * 32, "B")]
        window._refresh_settings_dialog()
        first_card = window._profile_cards["a" * 32]["card"]
        # A refresh with the same profiles keeps the same mounted card object.
        window._refresh_settings_dialog()
        self.assertIs(window._profile_cards["a" * 32]["card"], first_card)
        # A rename updates the mounted card in place (no rebuild, no new object).
        window._profiles = [self._profile("a" * 32, "A2"), self._profile("b" * 32, "B")]
        window._refresh_settings_dialog()
        self.assertIs(window._profile_cards["a" * 32]["card"], first_card)
        self.assertEqual(window._profile_cards["a" * 32]["name_label"].text(), "A2")
        self.assertEqual(
            window._profile_cards["a" * 32]["delete_button"].accessibleName(),
            "Delete A2",
        )

    def test_active_selector_change_sends_set_active_profile(self):
        window = MainWindow()
        self._dialog(window)
        window._profiles = [
            self._profile("a" * 32, "A"),
            self._profile("b" * 32, "B"),
        ]
        window._active_profile_id = "a" * 32
        window._refresh_settings_dialog()
        sent = self._fake_send(window)
        window._settings_active_combo.setCurrentIndex(1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SET_ACTIVE_PROFILE)
        self.assertEqual(sent[0]["profile_id"], "b" * 32)

    def test_profile_action_saved_refreshes_state(self):
        window = MainWindow()
        self._dialog(window)
        window._profile_action_pending = True
        window._on_profile_action_saved(
            "added",
            {
                "state": "configured",
                "provider_id": "deepseek",
                "model": "deepseek-flash",
                "profiles": [self._profile("a" * 32, "Work")],
                "active_profile_id": "a" * 32,
                "credential_present": True,
                "authenticated": False,
                "online": False,
                "executable": False,
                "migrated": False,
                "store_available": True,
            },
        )
        self.assertEqual(len(window._profiles), 1)
        self.assertEqual(window._active_profile_id, "a" * 32)
        self.assertFalse(window._profile_action_pending)
        self.assertEqual(
            window._settings_action_status.text(), PROFILE_ACTION_MESSAGES["added"]
        )

    def test_profile_action_error_is_bounded(self):
        window = MainWindow()
        self._dialog(window)
        self._fake_send(window)  # _refresh_profiles goes through the boundary
        window._profile_action_pending = True
        window._on_profile_action_error("profile_name_invalid")
        self.assertFalse(window._profile_action_pending)
        self.assertEqual(
            window._settings_action_status.text(),
            "That profile name is not valid or is already in use.",
        )

    def test_provider_status_region_never_reflows(self):
        window = MainWindow()
        window.show()
        QApplication.processEvents()
        stack_geom = window._shell.stack.geometry()
        region_height = window._provider_status_label.parentWidget().height()
        window._set_provider_status(PROVIDER_STATUS_PENDING)
        QApplication.processEvents()
        self.assertEqual(
            window._provider_status_label.parentWidget().height(), region_height
        )
        self.assertEqual(window._shell.stack.geometry(), stack_geom)

    def test_navigation_rows_are_compact_and_do_not_touch(self):
        # The left-nav rows share one centrally-owned compact height; the
        # hover/selected/focus rectangle never crowds or touches an adjacent
        # row, so the five sections stay visually separated.
        window = MainWindow()
        self._dialog(window)
        dialog = window._settings_surface
        dialog.show()
        QApplication.processEvents()
        nav = window._settings_nav
        for i in range(nav.count()):
            self.assertEqual(nav.sizeHintForRow(i), style.SETTINGS_NAV_ROW_HEIGHT)
        nav.doItemsLayout()
        QApplication.processEvents()
        rects = [nav.visualItemRect(nav.item(i)) for i in range(nav.count())]
        for rect in rects:
            self.assertEqual(rect.height(), style.SETTINGS_NAV_ROW_HEIGHT)
        for upper, lower in zip(rects, rects[1:]):
            self.assertLessEqual(upper.bottom(), lower.top())

    def test_credential_failure_category_is_surfaced(self):
        # A failed native-host action with a bounded reason shows the category's
        # own message, never a raw error or the secret.
        window = MainWindow()
        self._dialog(window)
        window._show_credential_result(
            {"state": "failed", "reason": "prompt_failed", "credential_present": False}
        )
        self.assertEqual(
            window._settings_action_status.text(),
            "The secure credential prompt could not be shown.",
        )


if __name__ == "__main__":
    unittest.main()
