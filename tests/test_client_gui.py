"""Tests for the PySide6 desktop client (P3.2), run offscreen.

These tests import PySide6 and are skipped when it is not installed, so the
core and its tests remain installable without Qt. Every test runs with
``QT_QPA_PLATFORM=offscreen`` so no display server is required.
"""

from __future__ import annotations

import gc
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QEventLoop, QPointF, QProcess, QTimer, Qt, qInstallMessageHandler
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QPushButton,
        QStackedWidget,
        QTabBar,
        QToolButton,
        QWidget,
    )

    from hrca import contract, style
    from hrca.client import (
        BackendSupervisor,
        CodeView,
        DocumentView,
        MainWindow,
        PythonHighlighter,
    )
    from hrca.client_core import (
        TWIN_AVAILABLE,
        TWIN_LOADING,
        TWIN_STALE,
        VALIDATION_OK,
        build_request,
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
        self.assertIn("No Code Map", window._codemap_document.toPlainText())
        self.assertIn("empty", window._twin_label.text())

    def test_twin_state_transitions(self):
        window = MainWindow()
        window._set_twin_state(TWIN_STALE)
        self.assertEqual(window._twin_chip.text(), "Stale")
        self.assertIn("stale", window._codemap_document.toPlainText())
        self.assertIn("stale", window._twin_label.text())

    def test_all_six_twin_states(self):
        window = MainWindow()
        for state, word in style.TWIN_STATE_WORD.items():
            with self.subTest(state=state):
                window._set_twin_state(state)
                self.assertEqual(window._twin_chip.text(), word)
                self.assertTrue(window._codemap_document.toPlainText())
                self.assertIn(state, window._twin_label.text())

    def test_six_status_fields_populated(self):
        window = MainWindow()
        for field in (
            window._root_label,
            window._repo_label,
            window._file_label,
            window._twin_label,
            window._provider_label,
            window._validation_label,
        ):
            self.assertTrue(field.fullText(), field.objectName())

    def _laid_out_sizes(self, window, width):
        window.resize(width, 840)
        window.show()
        QApplication.processEvents()
        return list(window._horizontal_splitter.sizes())

    def test_horizontal_splitter_stretch_factors(self):
        # PySide6 exposes only ``setStretchFactor``, not a getter, so the
        # factors are verified by how the three panes share extra width.
        window = MainWindow()
        narrow = self._laid_out_sizes(window, 1024)
        wide = self._laid_out_sizes(window, 1920)
        explorer_narrow, source_narrow, twin_narrow = narrow
        explorer_wide, source_wide, twin_wide = wide
        # Explorer has stretch factor 0: it keeps its width as the window grows.
        self.assertEqual(explorer_wide, explorer_narrow)
        # Source and Twin split the added width 3:2 (stretch factors 3 and 2).
        source_growth = source_wide - source_narrow
        twin_growth = twin_wide - twin_narrow
        self.assertGreater(source_growth, 0)
        self.assertGreater(twin_growth, 0)
        self.assertAlmostEqual(source_growth / twin_growth, 3.0 / 2.0, delta=0.15)

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
                    self.assertEqual(window._horizontal_splitter.count(), 3)
                    self.assertEqual(window._vertical_splitter.count(), 2)
                    self.assertEqual(window._horizontal_splitter.widget(0), window._explorer_panel)
                    self.assertEqual(window._horizontal_splitter.widget(1), window._source_panel)
                    self.assertEqual(window._horizontal_splitter.widget(2), window._twin_panel)

    def test_main_window_uses_supplied_palette(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                window = MainWindow(palette=palette)
                self.assertIs(window._palette, palette)

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
        self.assertEqual(window._tree_model.rowCount(), 3)
        # Folder labels are plain names: the disclosure chevron is painted in a
        # fixed branch slot, never embedded in the label text.
        self.assertEqual(window._tree_model.item(0, 0).text(), "app")
        self.assertEqual(window._tree_model.item(1, 0).text(), "empty_dir")

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

    def test_bottom_panel_spans_full_width_beneath_panes(self):
        window = MainWindow()
        self.assertEqual(window._vertical_splitter.count(), 2)
        # The bottom panel is the second (and only lower) child of the vertical
        # splitter, directly beneath the primary workspace.
        self.assertEqual(window._vertical_splitter.widget(1), window._bottom_panel)
        self.assertEqual(window._bottom_tabs.count(), 6)
        # The body is visible by default (not explicitly hidden).
        self.assertFalse(window._bottom_body.isHidden())

    def test_bottom_panel_defaults_to_expanded_agent_chat(self):
        window = MainWindow()
        self.assertTrue(window._is_expanded)
        self.assertEqual(window._selected_tab, "chat")
        self.assertEqual(window._bottom_tabs.currentIndex(), 0)
        self.assertEqual(window._bottom_body.currentIndex(), 0)

    def test_disclosure_toggles_collapse_and_expand(self):
        window = MainWindow()
        window._set_expanded(False)
        self.assertFalse(window._is_expanded)
        self.assertTrue(window._bottom_body.isHidden())
        self.assertEqual(window._disclosure_button.text(), "▴")
        window._set_expanded(True)
        self.assertTrue(window._is_expanded)
        self.assertFalse(window._bottom_body.isHidden())
        self.assertEqual(window._disclosure_button.text(), "▾")

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
        splitter = window._horizontal_splitter
        sizes = splitter.sizes()
        explorer_w, source_w, twin_w = sizes

        # Explorer stays within its 180-420 px band (and is not collapsed).
        self.assertGreaterEqual(explorer_w, style.EXPLORER_MIN_WIDTH)
        self.assertLessEqual(explorer_w, style.EXPLORER_MAX_WIDTH)
        # Source and Twin meet their minimum widths.
        self.assertGreaterEqual(source_w, style.SOURCE_MIN_WIDTH)
        self.assertGreaterEqual(twin_w, style.TWIN_MIN_WIDTH)

        # Every primary pane has a positive, visible rectangle.
        for pane in (window._explorer_panel, window._source_panel, window._twin_panel):
            self.assertGreater(pane.width(), 0)
            self.assertGreater(pane.height(), 0)

        # No pair of primary panes overlaps: their geometries share the
        # horizontal splitter's coordinate space, so compare them directly.
        explorer_rect = window._explorer_panel.geometry()
        source_rect = window._source_panel.geometry()
        twin_rect = window._twin_panel.geometry()
        self.assertLessEqual(explorer_rect.right(), source_rect.left())
        self.assertLessEqual(source_rect.right(), twin_rect.left())

        # The bottom panel body spans the full primary-workspace width.
        self.assertAlmostEqual(window._bottom_body.width(), splitter.width(), delta=1)

        # The expanded bottom panel body has a positive, usable height.
        self.assertGreater(window._bottom_body.height(), 0)

        # The status bar is below the workspace/lower-area region and is one
        # fixed-height row.
        status_bar = window.findChild(QWidget, "statusBar")
        self.assertIsNotNone(status_bar)
        self.assertEqual(status_bar.height(), style.STATUS_BAR_HEIGHT)
        self.assertGreaterEqual(
            status_bar.geometry().top(),
            window._vertical_splitter.geometry().bottom(),
        )

        # No large dead region: the horizontal splitter fully allocates its
        # width to the three panes plus the two handles.
        allocated = sum(sizes) + 2 * style.SPLITTER_HANDLE_WIDTH
        self.assertLessEqual(abs(allocated - splitter.width()), 2)

    def test_geometry_matrix_across_palettes_and_sizes(self):
        sizes = ((1024, 640), (1360, 840), (1920, 1080))
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
        window._current_document = (
            "src/hrca/very_deeply_nested_directory_structure/level_one/"
            "level_two/level_three/level_four/level_five/"
            "a_particularly_long_source_module_name.py"
        )
        window._update_status()
        window.resize(1360, 840)
        window.show()
        QApplication.processEvents()

        for name, label, full in (
            ("root", window._root_label, f"Root: {window._root}"),
            ("file", window._file_label, f"File: {window._current_document}"),
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
        window._current_document = "src/hrca/" + "y" * 120 + ".py"
        window._update_status()
        window.resize(1360, 840)
        window.show()
        QApplication.processEvents()

        root_label = window._root_label
        file_label = window._file_label
        # Both fields remain visible and take a non-zero width on the row.
        self.assertTrue(root_label.isVisible())
        self.assertTrue(file_label.isVisible())
        self.assertGreater(root_label.width(), 0)
        self.assertGreater(file_label.width(), 0)
        # They share one status row (the same vertical position in the bar).
        status_bar = window.findChild(QWidget, "statusBar")
        self.assertIsNotNone(status_bar)
        root_y = root_label.mapTo(status_bar, root_label.rect().topLeft()).y()
        file_y = file_label.mapTo(status_bar, file_label.rect().topLeft()).y()
        self.assertAlmostEqual(root_y, file_y, delta=1)


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
class TwinPaneTests(unittest.TestCase):
    """P3.4 read-only Code Map pane: auto-sync, procedural document and entity list.

    These tests drive the *presentation* half only — they feed a bounded
    ``get_code_map`` result or a fake ``_send`` and assert the pane renders the
    procedural document and the compact entity list as text and issues the
    sync → get_code_map selection chain. No filesystem, Twin store or backend is
    touched.
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

    def _chain_send(self, window, sync_result=None, result=None,
                    sync_error=None, get_error=None):
        """A ``_send`` double that synchronously completes the sync→get chain."""
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            action = request["action"]
            if action == contract.ACTION_SYNC_TWIN:
                if sync_error is not None:
                    on_error(sync_error)
                else:
                    on_success(sync_result or {"state": "synchronized",
                                               "persisted": True, "counts": {}})
            elif action == contract.ACTION_GET_CODE_MAP:
                if get_error is not None:
                    on_error(get_error)
                else:
                    on_success(result or _code_map_result())
            else:
                on_success({})
            return True

        window._send = fake_send
        return sent

    def test_tree_load_triggers_auto_sync_when_root_open(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_tree_loaded(_sample_tree())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SYNC_TWIN)
        self.assertNotIn("path", sent[0])
        self.assertEqual(sent[0]["task"], {})

    def test_tree_load_without_root_does_not_sync(self):
        window = MainWindow()
        sent = self._fake_send(window)
        window._on_tree_loaded(_sample_tree())
        self.assertEqual(sent, [])

    def test_code_map_loaded_renders_document_and_entity_list(self):
        window = MainWindow()
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        self.assertEqual(window._twin_chip.text(), "Available")
        doc = window._codemap_document.toPlainText()
        self.assertIn("Module app.service", doc)
        self.assertIn("Method handle(request)", doc)
        # The compact ordered entity list renders module + method entries.
        self.assertTrue(window._codemap_entity_list.isVisibleTo(window._twin_panel))
        self.assertEqual(window._codemap_entity_list.count(), 2)
        self.assertEqual(
            window._codemap_entity_list.item(0).text(),
            "module: app.service — Module app.service",
        )
        self.assertEqual(
            window._codemap_entity_list.item(1).text(),
            "method: app.service.Service.handle — Method handle(request)",
        )

    def test_code_map_without_entities_hides_list(self):
        window = MainWindow()
        result = _code_map_result()
        result["entities"] = []
        window._on_code_map_loaded(result, rel_path="app/service.py")
        self.assertEqual(window._codemap_entity_list.count(), 0)
        self.assertFalse(window._codemap_entity_list.isVisibleTo(window._twin_panel))

    def test_sync_result_sets_chip_and_status(self):
        window = MainWindow()
        window._on_twin_synced(
            {
                "state": "synchronized",
                "persisted": True,
                "counts": {"artifacts": 3, "behavior_nodes": 2,
                           "correspondences": 5, "projections": 4},
            }
        )
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("twin synchronized", window.status_label.text())

    def test_sync_conflict_maps_to_conflict_chip(self):
        window = MainWindow()
        window._on_twin_synced({"state": "conflict", "counts": {}, "reason": "draft"})
        self.assertEqual(window._twin_chip.text(), "Conflict")

    def test_entity_selection_requests_scoped_code_map(self):
        window = MainWindow()
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        sent = self._fake_send(window)
        window._on_entity_selected(window._codemap_entity_list.item(0))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_CODE_MAP)
        self.assertEqual(sent[0]["task"]["selector"], "app.service")

    def test_document_open_loads_code_map_for_python(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # Selection immediately sets Loading and issues a scoped sync first; the
        # previous projection (here, the empty state) stays mounted under an
        # in-place "Updating…" status line — it is not cleared or replaced.
        self.assertEqual(window._twin_chip.text(), "Loading")
        self.assertEqual(window._codemap_document.toPlainText(),
                         "No Code Map has been generated for this project.")
        self.assertTrue(window._codemap_status.isVisibleTo(window._twin_panel))
        self.assertIn("Updating Code Map for app/main.py",
                      window._codemap_status.text())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SYNC_TWIN)
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/main.py"])

    def test_document_open_skips_code_map_for_non_python(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened(
            "notes.txt",
            {"path": "notes.txt", "name": "notes.txt", "size": 5,
             "kind": "preview", "content": "hello\n"},
        )
        self.assertEqual(sent, [])
        self.assertEqual(window._twin_chip.text(), "Empty")
        self.assertEqual(window._codemap_entity_list.count(), 0)

    def test_selection_syncs_then_renders_code_map(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        self.assertEqual([r["action"] for r in sent],
                         [contract.ACTION_SYNC_TWIN, contract.ACTION_GET_CODE_MAP])
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/main.py"])
        self.assertEqual(sent[1]["task"], {})  # whole-document (no selector)
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._codemap_document.toPlainText())
        self.assertEqual(window._codemap_entity_list.count(), 2)

    def test_no_change_sync_still_renders_code_map(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(
            window, sync_result={"state": "no_change", "persisted": True, "counts": {}}
        )
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # ``no_change`` is a successful sync: the Code Map is still fetched.
        self.assertEqual([r["action"] for r in sent][1], contract.ACTION_GET_CODE_MAP)
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._codemap_document.toPlainText())

    def test_pyi_selection_triggers_same_code_map_chain(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("app/stubs.pyi", _source_doc("app/stubs.pyi"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SYNC_TWIN)
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/stubs.pyi"])

    def test_late_code_map_for_previous_selection_is_discarded(self):
        window = MainWindow()
        window._root = "/some/root"
        self._fake_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))  # gen 1
        window._on_document_opened("app/service.py", _source_doc("app/service.py"))  # gen 2
        # A late Code Map for generation 1 must not overwrite generation 2.
        window._on_code_map_loaded(_code_map_result(), generation=1, rel_path="app/main.py")
        self.assertEqual(window._twin_chip.text(), "Loading")
        self.assertNotIn("Method handle", window._codemap_document.toPlainText())
        # A current-generation Code Map (2) does render.
        window._on_code_map_loaded(_code_map_result(), generation=2, rel_path="app/service.py")
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._codemap_document.toPlainText())

    def test_late_scoped_sync_does_not_trigger_get_code_map(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))  # gen 1
        window._on_document_opened("app/service.py", _source_doc("app/service.py"))  # gen 2
        before = len(sent)  # two scoped syncs, no get_code_map yet
        window._on_selection_synced("app/main.py", 1,
                                    {"state": "synchronized", "counts": {}})
        self.assertEqual(len(sent), before)  # stale generation: no get_code_map

    def test_get_code_map_failure_shows_bounded_state(self):
        window = MainWindow()
        window._root = "/some/root"
        self._chain_send(window, get_error="twin_not_found")
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # A failed load retains the previous projection (the empty state) and
        # surfaces the reason in the in-place status line, not by flashing Empty.
        self.assertEqual(window._twin_chip.text(), "Empty")
        self.assertEqual(window._codemap_document.toPlainText(),
                         "No Code Map has been generated for this project.")
        self.assertIn("twin_not_found", window._codemap_status.text())
        self.assertTrue(window._codemap_status.isVisibleTo(window._twin_panel))
        self.assertIn("failed", window.status_label.text())

    def test_sync_failure_shows_bounded_state(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(window, sync_error="blocked")
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # Only the scoped sync was issued; its failure surfaces a bounded status
        # line while the previous projection stays mounted.
        self.assertEqual([r["action"] for r in sent], [contract.ACTION_SYNC_TWIN])
        self.assertEqual(window._twin_chip.text(), "Empty")
        self.assertIn("blocked", window._codemap_status.text())
        self.assertTrue(window._codemap_status.isVisibleTo(window._twin_panel))

    def test_active_file_scope_switches_and_pin_prevents_replacement(self):
        window = MainWindow()
        window._root = "/some/root"
        self._chain_send(window, result=_function_code_map_result())
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        self.assertIn("Function add", window._codemap_document.toPlainText())
        self.assertNotIn("fmt", window._codemap_document.toPlainText())

        self._chain_send(window, result=_helpers_code_map_result())
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        doc = window._codemap_document.toPlainText()
        self.assertIn("Function fmt(value) -> str", doc)
        self.assertNotIn("calculator", doc)  # old content is not mislabelled as helpers

        # Pinning the helpers projection freezes it against a later switch back.
        window._twin_lock_button.click()
        doc_before = window._codemap_document.toPlainText()
        sent = self._fake_send(window)
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        self.assertEqual(sent, [])
        self.assertEqual(window._codemap_document.toPlainText(), doc_before)

    def test_file_switch_retains_projection_until_atomic_replace(self):
        window = MainWindow()
        window._root = "/some/root"
        self._chain_send(window, result=_function_code_map_result())
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        calc_doc = window._codemap_document.toPlainText()
        self.assertIn("Function add", calc_doc)

        # Switch to helpers with a deferred response: the calculator projection
        # stays mounted and a small in-place indicator appears.
        sent = self._fake_send(window)
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        self.assertEqual(window._codemap_document.toPlainText(), calc_doc)
        self.assertTrue(window._codemap_status.isVisibleTo(window._twin_panel))
        self.assertIn("Updating Code Map for helpers.py", window._codemap_status.text())
        self.assertEqual(window._twin_chip.text(), "Loading")

        # A single atomic replacement once the helpers response arrives.
        window._on_code_map_loaded(
            _helpers_code_map_result(),
            generation=window._twin_generation,
            rel_path="helpers.py",
        )
        helpers_doc = window._codemap_document.toPlainText()
        self.assertIn("Function fmt(value) -> str", helpers_doc)
        self.assertNotIn("calculator", helpers_doc)
        # The status message clears, but its fixed-height region stays mounted.
        self.assertEqual(window._codemap_status.text(), "")
        self.assertTrue(window._codemap_status.isVisibleTo(window._twin_panel))

        # A late calculator response (older generation) is discarded.
        window._on_code_map_loaded(
            _function_code_map_result(),
            generation=window._twin_generation - 1,
            rel_path="calculator.py",
        )
        self.assertEqual(window._codemap_document.toPlainText(), helpers_doc)

    def test_file_switch_failure_retains_projection(self):
        window = MainWindow()
        window._root = "/some/root"
        self._chain_send(window, result=_function_code_map_result())
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        calc_doc = window._codemap_document.toPlainText()

        # Switch to helpers whose Code Map load fails: the calculator projection
        # is retained and a bounded failure status is shown (no flash to Empty).
        self._chain_send(window, get_error="twin_not_found")
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        self.assertEqual(window._codemap_document.toPlainText(), calc_doc)
        self.assertIn("twin_not_found", window._codemap_status.text())
        self.assertTrue(window._codemap_status.isVisibleTo(window._twin_panel))
        self.assertEqual(window._twin_chip.text(), "Available")

    def _laid_out_twin_window(self):
        """A shown, settled MainWindow so Code Map geometry is actually computed."""
        window = MainWindow()
        window._root = "/some/root"
        window.resize(1200, 800)
        window.show()
        QApplication.processEvents()
        return window

    def test_updating_state_preserves_document_geometry(self):
        """Available -> Updating keeps the procedural document geometry fixed.

        The in-place status region is a fixed-height, always-mounted label, so
        entering the Updating state must not move the document, change its size,
        alter its scroll range/position, or change the status region's own
        geometry — the observable values that would visibly "jump" on a reflow.
        """
        window = self._laid_out_twin_window()
        self._chain_send(window, result=_function_code_map_result())
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        QApplication.processEvents()

        doc = window._codemap_document
        status = window._codemap_status
        calc_doc = doc.toPlainText()
        self.assertIn("Function add", calc_doc)
        self.assertGreater(doc.height(), 0)

        before = {
            "doc": doc.geometry(),
            "scroll_value": doc.verticalScrollBar().value(),
            "scroll_max": doc.verticalScrollBar().maximum(),
            "status": status.geometry(),
        }

        # Deferred switch: the calculator projection stays mounted under an
        # "Updating…" message. No geometry may change while the request is pending.
        sent = self._fake_send(window)
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        QApplication.processEvents()

        self.assertEqual(doc.toPlainText(), calc_doc)  # projection retained
        self.assertIn("Updating Code Map for helpers.py", status.text())
        self.assertEqual(len(sent), 1)

        self.assertEqual(doc.geometry(), before["doc"])
        self.assertEqual(doc.verticalScrollBar().value(), before["scroll_value"])
        self.assertEqual(doc.verticalScrollBar().maximum(), before["scroll_max"])
        self.assertEqual(status.geometry(), before["status"])
        self.assertEqual(status.height(), style.CODEMAP_STATUS_HEIGHT)

        # A matching complete response replaces the projection atomically and
        # clears the message; the reserved status region itself never moves.
        window._on_code_map_loaded(
            _helpers_code_map_result(),
            generation=window._twin_generation,
            rel_path="helpers.py",
        )
        QApplication.processEvents()
        self.assertIn("Function fmt(value) -> str", doc.toPlainText())
        self.assertNotIn("calculator", doc.toPlainText())
        self.assertEqual(status.text(), "")
        self.assertEqual(status.geometry(), before["status"])
        self.assertEqual(status.height(), style.CODEMAP_STATUS_HEIGHT)

    def test_updating_failure_preserves_document_geometry(self):
        """A failed switch retains the projection and geometry, showing a bounded status."""
        window = self._laid_out_twin_window()
        self._chain_send(window, result=_function_code_map_result())
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        QApplication.processEvents()

        doc = window._codemap_document
        status = window._codemap_status
        calc_doc = doc.toPlainText()
        before = {
            "doc": doc.geometry(),
            "scroll_value": doc.verticalScrollBar().value(),
            "status": status.geometry(),
        }

        self._chain_send(window, get_error="twin_not_found")
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        QApplication.processEvents()

        self.assertEqual(doc.toPlainText(), calc_doc)
        self.assertIn("twin_not_found", status.text())
        self.assertEqual(doc.geometry(), before["doc"])
        self.assertEqual(doc.verticalScrollBar().value(), before["scroll_value"])
        self.assertEqual(status.geometry(), before["status"])
        self.assertEqual(window._twin_chip.text(), "Available")

    def test_scan_completed_refreshes_selected_code_map(self):
        window = MainWindow()
        window._root = "/some/root"
        window._current_document = "app/main.py"
        sent = self._fake_send(window)
        window._on_scan_completed(_sample_result())
        self.assertEqual(sent[0]["action"], contract.ACTION_SYNC_TWIN)
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/main.py"])

    def test_scan_completed_without_supported_selection_is_noop(self):
        window = MainWindow()
        window._root = "/some/root"
        window._current_document = None
        sent = self._fake_send(window)
        window._on_scan_completed(_sample_result())
        self.assertEqual(sent, [])


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class CodeMapPinTests(unittest.TestCase):
    """P3.4 Code Map pane follow/pin (lock) and entity-list navigation.

    The pane is renamed "Code Map", starts unlocked and auto-follows the active
    supported source tab; a single monochrome lock pins the displayed Code Map
    to its source path until unpinned. The compact entity list is plain ordered
    text: selecting an entry scopes the document to that entity's nested
    procedure.
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

    def _chain_send(self, window, result=None):
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            action = request["action"]
            if action == contract.ACTION_SYNC_TWIN:
                on_success({"state": "synchronized", "persisted": True, "counts": {}})
            elif action == contract.ACTION_GET_CODE_MAP:
                on_success(result or _code_map_result())
            return True

        window._send = fake_send
        return sent

    # -- rename + lock control ------------------------------------------

    def test_pane_title_is_code_map(self):
        window = MainWindow()
        self.assertEqual(window._twin_header_label.text(), "CODE MAP")
        self.assertEqual(window._twin_header_label.accessibleName(), "Code Map")
        self.assertEqual(window._codemap_document.accessibleName(), "Code Map content")

    def test_single_non_emoji_lock_control(self):
        window = MainWindow()
        lock = window._twin_lock_button
        self.assertIsInstance(lock, QToolButton)
        self.assertTrue(lock.isCheckable())
        self.assertEqual(lock.text(), "")
        self.assertFalse(lock.icon().isNull())
        buttons = window._twin_panel.findChildren(QToolButton, "twinLockButton")
        self.assertEqual(len(buttons), 1)

    def test_starts_unlocked(self):
        window = MainWindow()
        self.assertFalse(window._twin_pinned)
        self.assertFalse(window._twin_lock_button.isChecked())
        self.assertEqual(window._twin_lock_button.accessibleName(), "Pin Code Map")

    def test_lock_disabled_without_selection(self):
        window = MainWindow()
        self.assertFalse(window._twin_lock_button.isEnabled())
        self.assertIn("No supported source file", window._twin_lock_button.toolTip())
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        self.assertTrue(window._twin_lock_button.isEnabled())

    # -- follow / pin / unpin -------------------------------------------

    def test_python_selection_auto_renders(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(window)
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        self.assertEqual(
            [r["action"] for r in sent],
            [contract.ACTION_SYNC_TWIN, contract.ACTION_GET_CODE_MAP],
        )
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._codemap_document.toPlainText())

    def test_switch_follows_new_selection(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        syncs = [r for r in sent if r["action"] == contract.ACTION_SYNC_TWIN]
        self.assertEqual(len(syncs), 2)
        self.assertEqual(syncs[0]["task"]["changed_paths"], ["calculator.py"])
        self.assertEqual(syncs[1]["task"]["changed_paths"], ["helpers.py"])

    def test_pin_retains_code_map_across_switch(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        window._twin_lock_button.click()  # pin to app/service.py
        self.assertTrue(window._twin_pinned)
        self.assertTrue(window._twin_lock_button.isChecked())
        self.assertEqual(window._twin_lock_button.accessibleName(), "Unpin Code Map")
        doc_before = window._codemap_document.toPlainText()
        sent = self._fake_send(window)
        window._on_document_opened("other.py", _source_doc("other.py"))
        self.assertEqual(sent, [])
        self.assertEqual(window._codemap_document.toPlainText(), doc_before)
        self.assertEqual(window._active_twin_path, "app/service.py")

    def test_unlock_immediately_follows_active_tab(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        window._twin_lock_button.click()  # pin
        self._fake_send(window)
        window._on_document_opened("helpers.py", _source_doc("helpers.py"))
        sent = self._fake_send(window)
        window._twin_lock_button.click()  # unpin -> follow helpers.py now
        self.assertFalse(window._twin_pinned)
        syncs = [r for r in sent if r["action"] == contract.ACTION_SYNC_TWIN]
        self.assertEqual(len(syncs), 1)
        self.assertEqual(syncs[0]["task"]["changed_paths"], ["helpers.py"])

    def test_late_response_does_not_relabel_pinned_content(self):
        window = MainWindow()
        window._root = "/some/root"
        self._chain_send(window)
        window._on_document_opened("app/service.py", _source_doc("app/service.py"))
        self.assertEqual(window._twin_chip.text(), "Available")
        window._twin_lock_button.click()  # pin advances the generation
        doc_before = window._codemap_document.toPlainText()
        window._on_code_map_loaded(_code_map_result(), generation=1, rel_path="app/service.py")
        self.assertEqual(window._codemap_document.toPlainText(), doc_before)

    def test_pinned_survives_unsupported_switch(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        window._twin_lock_button.click()
        doc_before = window._codemap_document.toPlainText()
        sent = self._fake_send(window)
        window._on_document_opened(
            "notes.txt",
            {
                "path": "notes.txt",
                "name": "notes.txt",
                "size": 5,
                "kind": "preview",
                "content": "hello\n",
            },
        )
        self.assertEqual(sent, [])
        self.assertEqual(window._codemap_document.toPlainText(), doc_before)
        self.assertTrue(window._twin_lock_button.isChecked())

    # -- entity list navigation + evidence -------------------------------

    def test_entity_list_items_are_plain_ordered_items(self):
        window = MainWindow()
        window._on_code_map_loaded(_function_code_map_result(), rel_path="calculator.py")
        self.assertEqual(window._codemap_entity_list.count(), 3)
        expected = (
            ("module: calculator — Module calculator", "calculator"),
            ("function: calculator.add — Function add(left: float, right: float) -> float",
             "calculator.add"),
            ("function: calculator.divide — Function divide(left: float, right: float) -> float",
             "calculator.divide"),
        )
        for i, (label, locator) in enumerate(expected):
            item = window._codemap_entity_list.item(i)
            self.assertEqual(item.text(), label)
            self.assertEqual(item.data(Qt.UserRole), locator)
            # Plain ordered items, not interactive buttons.
            self.assertIsNone(window._codemap_entity_list.itemWidget(item))

    def test_divide_selection_sends_scoped_get_code_map(self):
        window = MainWindow()
        window._on_code_map_loaded(_function_code_map_result(), rel_path="calculator.py")
        sent = self._fake_send(window)
        window._on_entity_selected(window._codemap_entity_list.item(2))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_CODE_MAP)
        self.assertEqual(sent[0]["task"]["selector"], "calculator.divide")

    def test_details_toggle_shows_and_hides_evidence(self):
        window = MainWindow()
        window._on_code_map_loaded(_function_code_map_result(), rel_path="calculator.py")
        self.assertTrue(window._codemap_details_button.isCheckable())
        self.assertEqual(window._codemap_details_button.accessibleName(),
                         "Show Code Map evidence")
        self.assertFalse(window._codemap_details.isVisibleTo(window._twin_panel))
        window._codemap_details_button.click()
        self.assertTrue(window._codemap_details.isVisibleTo(window._twin_panel))
        self.assertEqual(window._codemap_details_button.accessibleName(),
                         "Hide Code Map evidence")
        window._codemap_details_button.click()
        self.assertFalse(window._codemap_details.isVisibleTo(window._twin_panel))
        self.assertEqual(window._codemap_details_button.accessibleName(),
                         "Show Code Map evidence")

    def test_evidence_renders_block_metadata(self):
        window = MainWindow()
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        window._codemap_details_button.click()
        evidence = window._codemap_details.toPlainText()
        self.assertIn("Entity — codemap:app.service:entity:0", evidence)
        self.assertIn("source: app/service.py:1", evidence)
        self.assertIn("provenance: verified", evidence)
        self.assertIn("confidence: high", evidence)
        self.assertIn("state: current", evidence)
        self.assertIn("editability: replace_description", evidence)

    def test_document_and_details_are_read_only_text(self):
        window = MainWindow()
        window._on_code_map_loaded(_function_code_map_result(), rel_path="calculator.py")
        self.assertIsInstance(window._codemap_document, QPlainTextEdit)
        self.assertTrue(window._codemap_document.isReadOnly())
        self.assertIsInstance(window._codemap_details, QPlainTextEdit)
        self.assertTrue(window._codemap_details.isReadOnly())
        self.assertIn("Function add", window._codemap_document.toPlainText())
        self.assertEqual(window._codemap_entity_list.count(), 3)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class CodeMapDraftTests(unittest.TestCase):
    """P3.4 editable Code Map draft surface (offscreen, presentation-only).

    These tests drive the presentation half only: they feed a bounded
    ``get_code_map`` result and a fake ``_send``, then assert the edit surface
    renders read-only facts, exposes only typed-editable blocks as one-line
    editors (purpose text, decision condition), offers draft-only Add note /
    Add step structure controls, and issues the seven draft actions through the
    boundary as *typed operations*. No Twin store, draft persistence, source
    mutation or network is exercised.
    """

    PURPOSE_ID = "codemap:app.service.Service.handle:purpose:1"
    DECISION_ID = "codemap:app.service.Service.handle:decision:2"

    def setUp(self):
        _app()

    def _fake_send(self, window):
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        return sent

    def _loaded_edit_surface(self):
        """A window with a Code Map loaded, edit mode entered, and a scoped
        ``get_code_map`` result rendered. Returns ``(window, sent)``."""
        window = MainWindow()
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        sent = self._fake_send(window)
        window._edit_button.click()  # enter edit mode -> get_code_map
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        return window, sent

    # -- edit action enablement + surface ----------------------------------

    def test_edit_button_disabled_without_active_code_map(self):
        window = MainWindow()
        self.assertFalse(window._edit_button.isEnabled())

    def test_edit_button_enabled_with_code_map(self):
        window = MainWindow()
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        self.assertTrue(window._edit_button.isEnabled())
        self.assertEqual(window._edit_button.objectName(), "editCodeMapButton")

    def test_enter_edit_mode_requests_scoped_code_map_and_switches_page(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        sent = self._fake_send(window)
        window._edit_button.click()
        self.assertEqual([r["action"] for r in sent], [contract.ACTION_GET_CODE_MAP])
        self.assertEqual(sent[0]["task"]["selector"], "app.service.Service.handle")
        self.assertEqual(window._twin_stack.currentIndex(), 1)
        self.assertTrue(window._edit_mode)

    def test_exit_edit_mode_returns_to_readonly(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_code_map_loaded(_code_map_result(), rel_path="app/service.py")
        self._fake_send(window)
        window._edit_button.click()
        self.assertEqual(window._twin_stack.currentIndex(), 1)
        window._exit_edit_mode()
        self.assertFalse(window._edit_mode)
        self.assertFalse(window._edit_button.isChecked())
        self.assertEqual(window._twin_stack.currentIndex(), 0)

    def test_draft_notice_is_present_and_bounded(self):
        window, _ = self._loaded_edit_surface()
        self.assertEqual(
            window._draft_notice.text(),
            "Edits create a draft only. Source code is unchanged.",
        )
        self.assertIn("Source code is unchanged", window._draft_notice.text())

    # -- structured controls -----------------------------------------------

    def test_read_only_facts_are_a_label_not_editable(self):
        window, _ = self._loaded_edit_surface()
        self.assertIsInstance(window._draft_facts, QLabel)
        self.assertEqual(window._draft_facts.accessibleName(), "Read-only facts")
        text = window._draft_facts.text()
        self.assertIn("Scope: app.service.Service.handle", text)
        self.assertIn("Baseline revision: abc123", text)

    def test_controls_cover_only_typed_editable_blocks(self):
        window, _ = self._loaded_edit_surface()
        # Verified facts are not editable: only purpose and decision rows exist.
        self.assertEqual(len(window._draft_controls), 2)
        self.assertIn(self.PURPOSE_ID, window._draft_controls)
        self.assertIn(self.DECISION_ID, window._draft_controls)

    def test_purpose_and_condition_are_one_line_editors(self):
        window, _ = self._loaded_edit_surface()
        purpose = window._draft_controls[self.PURPOSE_ID]
        condition = window._draft_controls[self.DECISION_ID]
        self.assertIsInstance(purpose, QLineEdit)
        self.assertIsInstance(condition, QLineEdit)
        self.assertEqual(purpose.text(), "Handles a request")
        self.assertEqual(condition.text(), "request is valid")
        self.assertEqual(purpose.accessibleName(), "purpose editor")
        self.assertEqual(condition.accessibleName(), "condition editor")
        # Each row carries a checkable Mark unresolved toggle.
        self.assertTrue(window._draft_unresolved[self.PURPOSE_ID].isCheckable())
        self.assertTrue(window._draft_unresolved[self.DECISION_ID].isCheckable())

    def test_add_note_and_step_inputs_are_present(self):
        window, _ = self._loaded_edit_surface()
        self.assertEqual(window._note_input.accessibleName(), "Add note text")
        self.assertEqual(window._step_input.accessibleName(), "Add step text")
        self.assertIsInstance(window._note_input, QLineEdit)
        self.assertIsInstance(window._step_input, QLineEdit)

    # -- save / dirty lifecycle -------------------------------------------

    def test_typing_marks_dirty_and_save_collects_operations(self):
        window, sent = self._loaded_edit_surface()
        purpose = window._draft_controls[self.PURPOSE_ID]
        purpose.setText("  A service handler  ")
        self.assertTrue(window._draft_dirty)
        window.save_draft_button.click()
        self.assertEqual(sent[-1]["action"], contract.ACTION_SAVE_DRAFT)
        operations = sent[-1]["task"]["operations"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["op"], "replace_description")
        self.assertEqual(operations[0]["target_block_id"], self.PURPOSE_ID)
        self.assertEqual(operations[0]["proposed_text"], "A service handler")

    def test_add_note_step_and_mark_unresolved_collect_typed_operations(self):
        window, sent = self._loaded_edit_surface()
        window._note_input.setText("Review this handler")
        window._step_input.setText("   result = total   ")
        window._draft_unresolved[self.DECISION_ID].setChecked(True)
        self.assertTrue(window._draft_dirty)
        window.save_draft_button.click()
        operations = sent[-1]["task"]["operations"]
        self.assertEqual(len(operations), 3)
        note_op = next(
            o for o in operations
            if o["op"] == "insert_block" and o["block_type"] == "note"
        )
        self.assertEqual(note_op["owning_entity_id"], "app.service.Service.handle")
        self.assertEqual(note_op["proposed_text"], "Review this handler")
        step_op = next(
            o for o in operations
            if o["op"] == "insert_block" and o["block_type"] == "step"
        )
        self.assertEqual(step_op["proposed_payload"], {"operation": "assign"})
        self.assertEqual(step_op["proposed_text"], "result = total")
        unresolved_op = next(o for o in operations if o["op"] == "mark_unresolved")
        self.assertEqual(unresolved_op["target_block_id"], self.DECISION_ID)
        self.assertEqual(unresolved_op["reason"], "review")

    def test_save_success_shows_operations_and_clears_dirty(self):
        window, _ = self._loaded_edit_surface()
        window._draft_dirty = True
        window._on_draft_saved(
            {
                "draft": {
                    "operations": [
                        {
                            "op": "replace_description",
                            "target_block_id": self.PURPOSE_ID,
                            "intent_class": "documentation_intent",
                            "proposed": {"display_text": "A service handler"},
                        }
                    ]
                },
                "persisted": True,
            }
        )
        self.assertFalse(window._draft_dirty)
        text = window._draft_result.toPlainText()
        self.assertIn("Replace description — ", text)
        self.assertIn("A service handler", text)

    # -- lifecycle actions -------------------------------------------------

    def test_discard_reset_compare_generate_send_actions(self):
        for button, action in (
            ("discard_draft_button", contract.ACTION_DISCARD_DRAFT),
            ("reset_draft_button", contract.ACTION_RESET_DRAFT),
            ("compare_draft_button", contract.ACTION_COMPARE_DRAFT),
            ("generate_draft_button", contract.ACTION_GENERATE_INTENT_DELTA),
        ):
            window, sent = self._loaded_edit_surface()
            getattr(window, button).click()
            self.assertEqual(sent[-1]["action"], action)

    # -- result rendering --------------------------------------------------

    def test_no_change_intent_delta_is_honest(self):
        window, _ = self._loaded_edit_surface()
        window._on_intent_delta_ready({"no_change": True})
        self.assertIn("No changes", window._draft_result.toPlainText())
        self.assertIn("no changes", window.status_label.text())

    def test_intent_delta_marks_non_executable(self):
        window, _ = self._loaded_edit_surface()
        window._on_intent_delta_ready(
            {
                "no_change": False,
                "intent_delta": {
                    "intent": "documentation_intent",
                    "entries": [
                        {
                            "operation": "replace_description",
                            "owning_entity_id": "app.service.Service.handle",
                            "required_approval_level": "human",
                        }
                    ],
                },
            }
        )
        text = window._draft_result.toPlainText()
        self.assertIn("Executable: false", text)
        self.assertIn("Intent: documentation_intent", text)
        self.assertIn("Replace description on app.service.Service.handle", text)

    def test_compare_conflict_is_surfaced(self):
        window, _ = self._loaded_edit_surface()
        window._on_draft_compared(
            {
                "draft_id": "draft:1",
                "operations": [
                    {
                        "op": "replace_description",
                        "target_block_id": self.PURPOSE_ID,
                        "intent_class": "documentation_intent",
                        "proposed": {"display_text": "Changed"},
                    }
                ],
                "conflict": {"state": "stale", "reason": "baseline moved"},
            }
        )
        text = window._draft_result.toPlainText()
        self.assertIn("Conflict", text)
        self.assertIn("baseline moved", text)

    def test_draft_error_shows_bounded_reason(self):
        window, _ = self._loaded_edit_surface()
        window._on_draft_error("draft_stale")
        self.assertIn("draft_stale", window._draft_result.toPlainText())
        self.assertIn("failed", window.status_label.text())

    # -- dirty leave (no auto-save) ----------------------------------------

    def test_dirty_leave_save_routes_to_save_then_exit(self):
        window, sent = self._loaded_edit_surface()
        purpose = window._draft_controls[self.PURPOSE_ID]
        purpose.setText("A service handler")
        self.assertTrue(window._draft_dirty)
        with mock.patch.object(window, "_prompt_dirty_leave", return_value="save"):
            window._attempt_leave_edit_mode()
        self.assertEqual(sent[-1]["action"], contract.ACTION_SAVE_DRAFT)
        # Leaving is deferred until the save completes.
        self.assertTrue(window._edit_mode)
        window._on_draft_saved({"draft": {"operations": []}, "persisted": True})
        self.assertFalse(window._edit_mode)
        self.assertEqual(window._twin_stack.currentIndex(), 0)

    def test_dirty_leave_discard_exits_without_saving(self):
        window, sent = self._loaded_edit_surface()
        window._draft_dirty = True
        with mock.patch.object(window, "_prompt_dirty_leave", return_value="discard"):
            window._attempt_leave_edit_mode()
        self.assertFalse(any(r["action"] == contract.ACTION_SAVE_DRAFT for r in sent))
        self.assertFalse(window._edit_mode)

    def test_dirty_leave_remain_keeps_edit_mode(self):
        window, sent = self._loaded_edit_surface()
        window._draft_dirty = True
        with mock.patch.object(window, "_prompt_dirty_leave", return_value="remain"):
            window._attempt_leave_edit_mode()
        self.assertTrue(window._edit_mode)
        self.assertTrue(window._edit_button.isChecked())

    def test_file_switch_does_not_discard_or_retarget_dirty_draft(self):
        window, sent = self._loaded_edit_surface()
        purpose = window._draft_controls[self.PURPOSE_ID]
        purpose.setText("A service handler")  # marks the draft dirty
        self.assertTrue(window._draft_dirty)

        # Switching files while a dirty draft is open must not silently discard
        # or retarget the draft: edit mode, the dirty flag and the target scope
        # all survive the switch.
        window._on_document_opened("other.py", _source_doc("other.py"))
        self.assertTrue(window._edit_mode)
        self.assertTrue(window._draft_dirty)
        self.assertIn("Scope: app.service.Service.handle", window._draft_facts.text())

    # -- stale guard --------------------------------------------------------

    def test_clearing_active_path_disables_edit_action(self):
        window, _ = self._loaded_edit_surface()
        self.assertTrue(window._edit_button.isEnabled())
        window._exit_edit_mode()
        window._set_active_twin_path(None)
        self.assertFalse(window._edit_button.isEnabled())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ExplorerTreeTests(unittest.TestCase):
    """P3.2 v2.6 stable Project Explorer disclosure indicators.

    Folder rows carry a plain name; the disclosure chevron is painted by the
    branch style in a fixed 20 px slot, so toggling a folder never moves its
    label, child indentation or row geometry. Leaf folders show no indicator
    but keep normal depth alignment; kind-aware documents still open through
    the boundary.
    """

    def setUp(self):
        _app()

    def _window_with_tree(self, palette=None):
        window = MainWindow(palette=palette)
        window.resize(1360, 840)
        window.show()
        QApplication.processEvents()
        window._on_tree_loaded(_sample_tree())
        QApplication.processEvents()
        return window

    def _app_index(self, window):
        return window._tree_model.indexFromItem(window._tree_model.item(0, 0))

    def _mouse_event(self, view, etype, index):
        pos = view.visualRect(index).center()
        return QMouseEvent(
            etype,
            QPointF(pos),
            QPointF(view.viewport().mapToGlobal(pos)),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    def _press(self, view, index):
        view.mousePressEvent(self._mouse_event(view, QEvent.MouseButtonPress, index))

    def test_folder_labels_are_plain_names_without_chevron_glyphs(self):
        window = self._window_with_tree()
        app_item = window._tree_model.item(0, 0)
        self.assertEqual(app_item.text(), "app")
        self.assertNotIn("›", app_item.text())
        self.assertNotIn("⌄", app_item.text())

    def test_folder_toggle_never_shifts_label_x(self):
        window = self._window_with_tree()
        index = self._app_index(window)
        x_before = window._tree_view.visualRect(index).x()
        rect_before = window._tree_view.visualRect(index)
        for _ in range(3):
            window._tree_view.expand(index)
            QApplication.processEvents()
            self.assertEqual(window._tree_view.visualRect(index).x(), x_before)
            self.assertEqual(window._tree_view.visualRect(index), rect_before)
            window._tree_view.collapse(index)
            QApplication.processEvents()
            self.assertEqual(window._tree_view.visualRect(index).x(), x_before)
            self.assertEqual(window._tree_view.visualRect(index), rect_before)
        self.assertEqual(window._tree_model.item(0, 0).text(), "app")

    def test_child_indentation_is_constant(self):
        window = self._window_with_tree()
        index = self._app_index(window)
        window._tree_view.expand(index)
        QApplication.processEvents()
        child = window._tree_model.item(0, 0).child(0)  # main.py
        child_index = window._tree_model.indexFromItem(child)
        self.assertEqual(
            window._tree_view.visualRect(child_index).x(),
            window._tree_view.visualRect(index).x() + style.TREE_INDENT,
        )
        self.assertEqual(window._tree_view.indentation(), style.TREE_INDENT)

    def test_siblings_align_at_same_depth(self):
        window = self._window_with_tree()
        window._tree_view.expand(self._app_index(window))
        QApplication.processEvents()
        main_py = window._tree_model.item(0, 0).child(0)
        data_json = window._tree_model.item(0, 0).child(1)
        self.assertEqual(
            window._tree_view.visualRect(window._tree_model.indexFromItem(main_py)).x(),
            window._tree_view.visualRect(window._tree_model.indexFromItem(data_json)).x(),
        )

    def test_leaf_folder_has_no_false_disclosure(self):
        window = self._window_with_tree()
        empty_item = window._tree_model.item(1, 0)  # empty_dir
        self.assertEqual(empty_item.text(), "empty_dir")
        self.assertEqual(empty_item.rowCount(), 0)

    def test_file_rows_have_no_chevron(self):
        window = self._window_with_tree()
        notes_item = window._tree_model.item(2, 0)  # notes.txt
        self.assertEqual(notes_item.text(), "notes.txt")

    def test_folder_click_toggles_not_open_document(self):
        window = self._window_with_tree()
        index = self._app_index(window)
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        self._press(window._tree_view, index)
        self.assertEqual(sent, [])  # no document request for a folder
        self.assertTrue(window._tree_view.isExpanded(index))

    def test_folder_rapid_double_click_toggles_twice(self):
        window = self._window_with_tree()
        index = self._app_index(window)
        view = window._tree_view
        self.assertFalse(view.isExpanded(index))
        # A rapid second click arrives as a MouseButtonDblClick; both clicks must
        # toggle, so an open then a close leaves the folder collapsed again.
        self._press(view, index)
        self.assertTrue(view.isExpanded(index))
        view.mouseDoubleClickEvent(
            self._mouse_event(view, QEvent.MouseButtonDblClick, index)
        )
        self.assertFalse(view.isExpanded(index))

    def test_keyboard_right_expands_left_collapses(self):
        window = self._window_with_tree()
        index = self._app_index(window)
        window._tree_view.setCurrentIndex(index)
        window._tree_view.keyPressEvent(
            QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.KeyboardModifier.NoModifier)
        )
        self.assertTrue(window._tree_view.isExpanded(index))
        window._tree_view.setCurrentIndex(index)
        window._tree_view.keyPressEvent(
            QKeyEvent(QEvent.KeyPress, Qt.Key_Left, Qt.KeyboardModifier.NoModifier)
        )
        self.assertFalse(window._tree_view.isExpanded(index))

    def test_indicator_and_geometry_stable_across_sizes_and_palettes(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for width, height in ((1024, 640), (1360, 840), (1920, 1080)):
                with self.subTest(palette=palette.name, size=(width, height)):
                    window = MainWindow(palette=palette)
                    window.resize(width, height)
                    window.show()
                    QApplication.processEvents()
                    window._on_tree_loaded(_sample_tree())
                    QApplication.processEvents()
                    index = self._app_index(window)
                    x_before = window._tree_view.visualRect(index).x()
                    self.assertEqual(window._tree_view.indentation(), style.TREE_INDENT)
                    window._tree_view.expand(index)
                    QApplication.processEvents()
                    self.assertEqual(window._tree_view.visualRect(index).x(), x_before)
                    child_index = window._tree_model.indexFromItem(
                        window._tree_model.item(0, 0).child(0)
                    )
                    self.assertEqual(
                        window._tree_view.visualRect(child_index).x(),
                        x_before + style.TREE_INDENT,
                    )
                    window._tree_view.collapse(index)
                    QApplication.processEvents()
                    self.assertEqual(window._tree_view.visualRect(index).x(), x_before)

    def test_kind_aware_documents_render(self):
        window = MainWindow()
        window._on_document_opened(
            "app/main.py",
            {"path": "app/main.py", "name": "main.py", "size": 10,
             "kind": "source", "content": "print('hi')\n"},
        )
        source_view = window._open_tabs["app/main.py"]
        self.assertIsInstance(source_view, DocumentView)
        self.assertTrue(source_view._banner.isHidden())

        window._on_document_opened(
            "app/data.json",
            {"path": "app/data.json", "name": "data.json", "size": 8,
             "kind": "preview", "content": '{"k": 1}\n'},
        )
        preview_view = window._open_tabs["app/data.json"]
        self.assertFalse(preview_view._banner.isHidden())
        self.assertIn("Read-only preview", preview_view._banner.text())

        window._on_document_opened(
            "app/image.png",
            {"path": "app/image.png", "name": "image.png", "size": 4,
             "kind": "unavailable", "reason": "binary"},
        )
        unavailable_view = window._open_tabs["app/image.png"]
        self.assertFalse(unavailable_view._banner.isHidden())
        self.assertIn("Binary", unavailable_view._banner.text())
        self.assertEqual(unavailable_view._body.toPlainText(), "")

    def test_expanding_folder_preserves_state_and_geometry(self):
        window = MainWindow()
        window.resize(1360, 840)
        window.show()
        QApplication.processEvents()
        window._on_tree_loaded(_sample_tree())
        window._on_document_opened(
            "app/main.py",
            {"path": "app/main.py", "name": "main.py", "size": 10,
             "kind": "source", "content": "print('hi')\n"},
        )
        tabs_before = window._source_tabs.count()
        selected_before = window._selected_tab
        expanded_before = window._is_expanded
        sizes_before = list(window._horizontal_splitter.sizes())

        app_item = window._tree_model.item(0, 0)
        index = window._tree_model.indexFromItem(app_item)
        self._press(window._tree_view, index)  # expand a folder

        self.assertEqual(window._source_tabs.count(), tabs_before)
        self.assertEqual(window._selected_tab, selected_before)
        self.assertEqual(window._is_expanded, expanded_before)
        self.assertEqual(list(window._horizontal_splitter.sizes()), sizes_before)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class BottomPanelTests(unittest.TestCase):
    """P3.2 single bottom utility panel + monochrome disclosure control.

    One bottom panel with one tab bar (six tabs, Agent Chat first) and one
    disclosure chevron replaces the former Agent Chat / Review & Evidence
    drawer pair. The primary workspace, the panel and the status bar must tile
    the vertical content area continuously — no unowned blank band — at every
    supported size in both palettes, in the expanded and collapsed states, and
    after a splitter move. Tab switching must never move or resize the panel
    or the workspace, and the disclosure glyphs are non-emoji text chevrons.
    """

    SIZES = ((1024, 640), (1360, 840), (1920, 1080))
    TAB_KEYS = ("chat", "plan", "diff", "problems", "tests", "evidence")
    TAB_LABELS = ("Agent Chat", "Plan", "Diff", "Problems", "Tests", "Evidence")

    def setUp(self):
        _app()

    def _laid_out(self, palette, width, height):
        window = MainWindow(palette=palette)
        window.resize(width, height)
        window.show()
        QApplication.processEvents()
        return window

    # -- widget hierarchy and legacy removal -----------------------------

    def test_single_bottom_panel_hierarchy(self):
        window = MainWindow()
        # The vertical splitter holds exactly the workspace and the one panel.
        self.assertEqual(window._vertical_splitter.count(), 2)
        self.assertEqual(window._vertical_splitter.widget(1), window._bottom_panel)
        # One tab bar with six tabs, one disclosure, one stacked body.
        self.assertIsInstance(window._bottom_tabs, QTabBar)
        self.assertEqual(window._bottom_tabs.count(), 6)
        self.assertIsInstance(window._disclosure_button, QToolButton)
        self.assertIsInstance(window._bottom_body, QStackedWidget)
        self.assertEqual(window._bottom_body.count(), 6)
        # The header is a single fixed-height row.
        self.assertEqual(
            window._bottom_panel_header.height(), style.BOTTOM_PANEL_HEADER_HEIGHT
        )

    def test_legacy_drawer_and_chat_controls_removed(self):
        window = MainWindow()
        for attr in (
            "_lower_area",
            "_chat_header",
            "_chat_body",
            "_chat_collapse_button",
            "_drawer",
            "_drawer_header",
            "_drawer_body",
            "_drawer_tabs",
            "_drawer_toggle_button",
            "_drawer_expanded",
        ):
            self.assertFalse(hasattr(window, attr), f"legacy attribute {attr} remains")
        for method in (
            "_build_lower_area",
            "_build_chat_body",
            "_build_drawer",
            "_on_drawer_toggle",
            "_set_drawer_expanded",
            "_toggle_chat",
        ):
            self.assertFalse(hasattr(window, method), f"legacy method {method} remains")

    def test_required_tabs_in_order(self):
        window = MainWindow()
        labels = [
            window._bottom_tabs.tabText(i) for i in range(window._bottom_tabs.count())
        ]
        self.assertEqual(labels, list(self.TAB_LABELS))

    # -- tab selection ----------------------------------------------------

    def test_tab_switch_selects_one_body_without_moving_panel(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        panel = window._bottom_panel
        panel_top = panel.mapTo(window, panel.rect().topLeft()).y()
        panel_height = panel.height()
        workspace_rect = window._horizontal_splitter.geometry()

        for index, key in enumerate(self.TAB_KEYS):
            with self.subTest(index=index, key=key):
                window._bottom_tabs.setCurrentIndex(index)
                QApplication.processEvents()

                self.assertEqual(window._selected_tab, key)
                self.assertEqual(window._bottom_tabs.currentIndex(), index)
                self.assertEqual(window._bottom_body.currentIndex(), index)
                self.assertIs(
                    window._bottom_body.currentWidget(), window._bottom_body.widget(index)
                )
                # Exactly one body page is shown; every other is hidden.
                for i in range(window._bottom_body.count()):
                    self.assertEqual(
                        window._bottom_body.widget(i).isHidden(), i != index
                    )
                # The panel and the workspace keep their geometry.
                self.assertEqual(
                    panel.mapTo(window, panel.rect().topLeft()).y(), panel_top
                )
                self.assertEqual(panel.height(), panel_height)
                self.assertEqual(
                    window._horizontal_splitter.geometry(), workspace_rect
                )

    def test_tab_switch_while_collapsed_stays_collapsed(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        window._set_expanded(False)
        QApplication.processEvents()
        self.assertFalse(window._is_expanded)
        self.assertEqual(window._bottom_panel.height(), style.BOTTOM_PANEL_HEADER_HEIGHT)

        window._bottom_tabs.setCurrentIndex(2)  # Diff
        QApplication.processEvents()

        self.assertEqual(window._selected_tab, "diff")
        self.assertEqual(window._bottom_body.currentIndex(), 2)
        # Still collapsed: switching tabs must not resurrect the body.
        self.assertFalse(window._is_expanded)
        self.assertEqual(window._bottom_panel.height(), style.BOTTOM_PANEL_HEADER_HEIGHT)

    # -- collapse / expand ------------------------------------------------

    def test_collapse_and_expand_preserve_tab_and_height(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        # Move to a non-default tab and give the panel a known taller height.
        window._bottom_tabs.setCurrentIndex(4)  # Tests
        QApplication.processEvents()
        total = sum(window._vertical_splitter.sizes())
        window._vertical_splitter.setSizes([total - 300, 300])
        QApplication.processEvents()
        expanded_height = window._bottom_panel.height()
        self.assertGreaterEqual(expanded_height, style.BOTTOM_PANEL_MIN_HEIGHT)
        workspace_expanded = window._horizontal_splitter.height()

        # Collapse: only the header row remains; height returns to the workspace.
        window._set_expanded(False)
        QApplication.processEvents()
        self.assertFalse(window._is_expanded)
        self.assertTrue(window._bottom_body.isHidden())
        self.assertEqual(
            window._bottom_panel.height(), style.BOTTOM_PANEL_HEADER_HEIGHT
        )
        self.assertGreater(window._horizontal_splitter.height(), workspace_expanded)
        # The selected tab survives the collapse.
        self.assertEqual(window._selected_tab, "tests")
        self.assertEqual(window._bottom_tabs.currentIndex(), 4)

        # Expand: the tab and the last usable height are restored.
        window._set_expanded(True)
        QApplication.processEvents()
        self.assertTrue(window._is_expanded)
        self.assertFalse(window._bottom_body.isHidden())
        self.assertEqual(window._selected_tab, "tests")
        self.assertEqual(window._bottom_tabs.currentIndex(), 4)
        self.assertEqual(window._bottom_body.currentIndex(), 4)
        self.assertEqual(window._bottom_panel.height(), expanded_height)
        self.assertEqual(window._horizontal_splitter.height(), workspace_expanded)

    def test_repeated_collapse_expand_cycles_are_stable(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        total = sum(window._vertical_splitter.sizes())
        window._vertical_splitter.setSizes([total - 280, 280])
        QApplication.processEvents()
        expanded_height = window._bottom_panel.height()

        for _ in range(3):
            window._set_expanded(False)
            QApplication.processEvents()
            self.assertFalse(window._is_expanded)
            self.assertEqual(
                window._bottom_panel.height(), style.BOTTOM_PANEL_HEADER_HEIGHT
            )
            window._set_expanded(True)
            QApplication.processEvents()
            self.assertTrue(window._is_expanded)
            self.assertEqual(window._bottom_panel.height(), expanded_height)

    def test_set_expanded_is_idempotent(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        expanded_sizes = list(window._vertical_splitter.sizes())
        window._set_expanded(True)  # already expanded — must not move anything
        QApplication.processEvents()
        self.assertEqual(list(window._vertical_splitter.sizes()), expanded_sizes)

        window._set_expanded(False)
        QApplication.processEvents()
        collapsed_sizes = list(window._vertical_splitter.sizes())
        window._set_expanded(False)  # already collapsed — must not move anything
        QApplication.processEvents()
        self.assertEqual(list(window._vertical_splitter.sizes()), collapsed_sizes)

    # -- disclosure control ----------------------------------------------

    def test_disclosure_chevron_and_accessibility(self):
        window = MainWindow()
        # Expanded: down chevron + "Collapse ..." semantics.
        self.assertEqual(window._disclosure_button.text(), "▾")
        self.assertEqual(
            window._disclosure_button.accessibleName(), "Collapse bottom panel"
        )
        self.assertEqual(
            window._disclosure_button.toolTip(), "Collapse bottom panel"
        )

        window._set_expanded(False)
        self.assertEqual(window._disclosure_button.text(), "▴")
        self.assertEqual(
            window._disclosure_button.accessibleName(), "Expand bottom panel"
        )
        self.assertEqual(
            window._disclosure_button.toolTip(), "Expand bottom panel"
        )

        window._set_expanded(True)
        self.assertEqual(window._disclosure_button.text(), "▾")

    def test_disclosure_click_toggles(self):
        window = self._laid_out(style.LIGHT_PALETTE, 1360, 840)
        self.assertTrue(window._is_expanded)
        window._disclosure_button.click()
        QApplication.processEvents()
        self.assertFalse(window._is_expanded)
        self.assertTrue(window._bottom_body.isHidden())
        window._disclosure_button.click()
        QApplication.processEvents()
        self.assertTrue(window._is_expanded)
        self.assertFalse(window._bottom_body.isHidden())

    # -- vertical tiling --------------------------------------------------

    def _assert_vertical_tiling(self, window):
        vsplit = window._vertical_splitter
        hsplit = window._horizontal_splitter
        panel = window._bottom_panel
        header = window._bottom_panel_header

        # The vertical splitter allocates its full height to the workspace, the
        # handle and the panel — no slack.
        self.assertEqual(
            hsplit.height() + style.SPLITTER_HANDLE_WIDTH + panel.height(),
            vsplit.height(),
        )

        # The panel is exactly its header plus (when expanded) the body.
        if window._is_expanded:
            self.assertGreater(window._bottom_body.height(), 0)
            self.assertEqual(
                header.height() + window._bottom_body.height(), panel.height()
            )
        else:
            self.assertEqual(panel.height(), style.BOTTOM_PANEL_HEADER_HEIGHT)

        # The panel sits directly on the status bar (no dead band).
        status_bar = window.findChild(QWidget, "statusBar")
        self.assertIsNotNone(status_bar)
        self.assertEqual(
            panel.mapTo(window, panel.rect().topLeft()).y() + panel.height(),
            status_bar.mapTo(window, status_bar.rect().topLeft()).y(),
        )

    def test_vertical_tiling_expanded_and_collapsed(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for width, height in self.SIZES:
                for state in ("expanded", "collapsed"):
                    with self.subTest(
                        palette=palette.name, size=(width, height), state=state
                    ):
                        window = self._laid_out(palette, width, height)
                        if state == "collapsed":
                            window._set_expanded(False)
                            QApplication.processEvents()
                        self._assert_vertical_tiling(window)

    def test_vertical_tiling_after_splitter_move(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for width, height in self.SIZES:
                with self.subTest(palette=palette.name, size=(width, height)):
                    window = self._laid_out(palette, width, height)
                    # Drag the divider toward each end; the content must stay
                    # fully tiled with no gap after each move.
                    window._vertical_splitter.moveSplitter(height // 4, 1)
                    QApplication.processEvents()
                    self._assert_vertical_tiling(window)
                    window._vertical_splitter.moveSplitter(height * 3 // 4, 1)
                    QApplication.processEvents()
                    self._assert_vertical_tiling(window)

    def test_expanded_panel_respects_min_height(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            for width, height in self.SIZES:
                with self.subTest(palette=palette.name, size=(width, height)):
                    window = self._laid_out(palette, width, height)
                    self.assertGreaterEqual(
                        window._bottom_panel.height(), style.BOTTOM_PANEL_MIN_HEIGHT
                    )
                    self.assertGreaterEqual(
                        window._bottom_body.height(), style.BOTTOM_PANEL_BODY_MIN_HEIGHT
                    )

    def test_collapsed_panel_reserves_only_header(self):
        for palette in (style.LIGHT_PALETTE, style.DARK_PALETTE):
            with self.subTest(palette=palette.name):
                window = self._laid_out(palette, 1360, 840)
                window._set_expanded(False)
                QApplication.processEvents()
                self.assertTrue(window._bottom_body.isHidden())
                self.assertEqual(
                    window._bottom_panel.height(), style.BOTTOM_PANEL_HEADER_HEIGHT
                )
                self.assertEqual(
                    window._bottom_panel_header.height(), style.BOTTOM_PANEL_HEADER_HEIGHT
                )


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


if __name__ == "__main__":
    unittest.main()
