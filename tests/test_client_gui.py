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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QEventLoop, QPointF, QProcess, QTimer, Qt, qInstallMessageHandler
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
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
        self.assertIn("No Code Map", window._twin_body.text())
        self.assertIn("empty", window._twin_label.text())

    def test_twin_state_transitions(self):
        window = MainWindow()
        window._set_twin_state(TWIN_STALE)
        self.assertEqual(window._twin_chip.text(), "Stale")
        self.assertIn("stale", window._twin_body.text())
        self.assertIn("stale", window._twin_label.text())

    def test_all_six_twin_states(self):
        window = MainWindow()
        for state, word in style.TWIN_STATE_WORD.items():
            with self.subTest(state=state):
                window._set_twin_state(state)
                self.assertEqual(window._twin_chip.text(), word)
                self.assertTrue(window._twin_body.text())
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


def _sample_twin_bundle() -> dict:
    """A bounded Twin projection bundle for one method with two behavior nodes."""
    return {
        "projection": {
            "kind": "method",
            "path": "app/service.py",
            "locator": "app.service.Service.handle",
            "summary": "Method handle(request)",
            "provenance": "verified",
            "confidence": "high",
            "sync_state": "synchronized",
            "details": ["Parameters: request"],
            "limitations": ["a dynamic dependency is marked low confidence"],
        },
        "behavior_nodes": [
            {"id": "behavior:calls:1", "category": "calls",
             "provenance": "verified", "confidence": "high",
             "items": ["open", "<unresolved>"]},
            {"id": "behavior:conditions:1", "category": "conditions",
             "provenance": "unresolved", "confidence": "low", "items": []},
        ],
    }


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class TwinPaneTests(unittest.TestCase):
    """P3.3 read-only Twin pane: auto-sync, projection and anchor navigation.

    These tests drive the *presentation* half only — they feed a bounded bundle
    or a fake ``_send`` and assert the pane renders provenance / confidence /
    sync state as text and issues the three Twin actions. No filesystem, Twin
    store or backend is touched.
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

    def _chain_send(self, window, sync_result=None, bundle=None,
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
            elif action == contract.ACTION_GET_TWIN:
                if get_error is not None:
                    on_error(get_error)
                else:
                    on_success(bundle or _sample_twin_bundle())
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

    def test_twin_projection_renders_fields_as_text(self):
        window = MainWindow()
        window._on_twin_projection_loaded(_sample_twin_bundle())
        self.assertEqual(window._twin_chip.text(), "Available")
        body = window._twin_body.text()
        self.assertIn("Method handle(request)", body)
        self.assertIn("Kind: method", body)
        self.assertIn("Provenance: verified", body)
        self.assertIn("Confidence: high", body)
        self.assertIn("Sync state: synchronized", body)
        self.assertIn("Limitations:", body)
        # Behavior nodes render as a visible, populated list.
        self.assertTrue(window._twin_nodes.isVisibleTo(window._twin_panel))
        self.assertEqual(window._twin_nodes.count(), 2)
        self.assertEqual(
            window._twin_nodes.item(0).text(), "calls: open, <unresolved>"
        )
        self.assertEqual(
            window._twin_nodes.item(1).text(), "conditions (unresolved)"
        )

    def test_projection_without_behavior_nodes_hides_list(self):
        window = MainWindow()
        bundle = _sample_twin_bundle()
        bundle["behavior_nodes"] = []
        window._on_twin_projection_loaded(bundle)
        self.assertEqual(window._twin_nodes.count(), 0)
        self.assertFalse(window._twin_nodes.isVisibleTo(window._twin_panel))

    def test_sync_result_renders_state_and_counts_as_text(self):
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
        body = window._twin_body.text()
        self.assertIn("Twin state: synchronized", body)
        self.assertIn("artifacts: 3", body)
        self.assertIn("behavior nodes: 2", body)

    def test_sync_conflict_maps_to_conflict_chip(self):
        window = MainWindow()
        window._on_twin_synced({"state": "conflict", "counts": {}, "reason": "draft"})
        self.assertEqual(window._twin_chip.text(), "Conflict")
        self.assertIn("Reason: draft", window._twin_body.text())

    def test_behavior_node_click_requests_anchor(self):
        window = MainWindow()
        window._on_twin_projection_loaded(_sample_twin_bundle())
        sent = self._fake_send(window)
        window._on_behavior_node_clicked(window._twin_nodes.item(0))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_ANCHOR)
        self.assertEqual(sent[0]["task"]["node_id"], "behavior:calls:1")

    def test_anchor_loaded_opens_document_and_sets_reveal_line(self):
        window = MainWindow()
        sent = self._fake_send(window)
        window._on_anchor_loaded(
            {"available": True, "file": "app/service.py",
             "source_range": {"lineno": 7}}
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_DOCUMENT)
        self.assertEqual(sent[0]["path"], "app/service.py")
        self.assertEqual(window._pending_reveal_line, 7)

    def test_anchor_unavailable_does_not_open_document(self):
        window = MainWindow()
        sent = self._fake_send(window)
        window._on_anchor_loaded({"available": False, "reason": "no_anchor"})
        self.assertEqual(sent, [])
        self.assertIn("failed", window.status_label.text())

    def test_document_open_loads_twin_projection_for_python(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # Selection immediately sets Loading and issues a scoped sync first; the
        # projection is fetched only after that sync succeeds.
        self.assertEqual(window._twin_chip.text(), "Loading")
        self.assertEqual(window._twin_body.text(),
                         "Code Map synchronization is in progress.")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SYNC_TWIN)
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/main.py"])

    def test_document_open_skips_twin_projection_for_non_python(self):
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
        self.assertEqual(window._twin_nodes.count(), 0)

    def test_selection_syncs_then_renders_projection(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        self.assertEqual([r["action"] for r in sent],
                         [contract.ACTION_SYNC_TWIN, contract.ACTION_GET_TWIN])
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/main.py"])
        self.assertEqual(sent[1]["task"]["selector"], "app/main.py")
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._twin_body.text())
        self.assertEqual(window._twin_nodes.count(), 2)

    def test_no_change_sync_still_renders_projection(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(
            window, sync_result={"state": "no_change", "persisted": True, "counts": {}}
        )
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # ``no_change`` is a successful sync: the projection is still fetched.
        self.assertEqual([r["action"] for r in sent][1], contract.ACTION_GET_TWIN)
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._twin_body.text())

    def test_pyi_selection_triggers_same_twin_chain(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("app/stubs.pyi", _source_doc("app/stubs.pyi"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_SYNC_TWIN)
        self.assertEqual(sent[0]["task"]["changed_paths"], ["app/stubs.pyi"])

    def test_late_projection_for_previous_selection_is_discarded(self):
        window = MainWindow()
        window._root = "/some/root"
        self._fake_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))  # gen 1
        window._on_document_opened("app/service.py", _source_doc("app/service.py"))  # gen 2
        # A late projection for generation 1 must not overwrite generation 2.
        window._on_twin_projection_loaded(_sample_twin_bundle(), generation=1)
        self.assertEqual(window._twin_chip.text(), "Loading")
        self.assertNotIn("Method handle", window._twin_body.text())
        # A current-generation projection (2) does render.
        window._on_twin_projection_loaded(_sample_twin_bundle(), generation=2)
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._twin_body.text())

    def test_late_scoped_sync_does_not_trigger_get_twin(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._fake_send(window)
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))  # gen 1
        window._on_document_opened("app/service.py", _source_doc("app/service.py"))  # gen 2
        before = len(sent)  # two scoped syncs, no get_twin yet
        window._on_selection_synced("app/main.py", 1,
                                    {"state": "synchronized", "counts": {}})
        self.assertEqual(len(sent), before)  # stale generation: no get_twin

    def test_get_twin_failure_shows_bounded_state(self):
        window = MainWindow()
        window._root = "/some/root"
        self._chain_send(window, get_error="twin_not_found")
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        self.assertEqual(window._twin_chip.text(), "Empty")
        self.assertIn("twin_not_found", window._twin_body.text())
        self.assertIn("failed", window.status_label.text())

    def test_sync_failure_shows_bounded_state(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(window, sync_error="blocked")
        window._on_document_opened("app/main.py", _source_doc("app/main.py"))
        # Only the scoped sync was issued; its failure surfaces a bounded state.
        self.assertEqual([r["action"] for r in sent], [contract.ACTION_SYNC_TWIN])
        self.assertEqual(window._twin_chip.text(), "Empty")
        self.assertIn("blocked", window._twin_body.text())

    def test_scan_completed_refreshes_selected_twin(self):
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
    """P3.3 Code Map pane follow/pin (lock) and interactive behavior nodes.

    The pane is renamed "Code Map", starts unlocked and auto-follows the active
    supported source tab; a single monochrome lock pins the displayed projection
    to its source path until unpinned. Anchorable behavior nodes render as
    accessible, focusable buttons that navigate by their stored backend id.
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

    def _chain_send(self, window, bundle=None):
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            action = request["action"]
            if action == contract.ACTION_SYNC_TWIN:
                on_success({"state": "synchronized", "persisted": True, "counts": {}})
            elif action == contract.ACTION_GET_TWIN:
                on_success(bundle or _sample_twin_bundle())
            return True

        window._send = fake_send
        return sent

    def _function_bundle(self):
        return {
            "projection": {
                "kind": "module",
                "path": "calculator.py",
                "locator": "calculator",
                "summary": "Calculator module",
                "provenance": "verified",
                "confidence": "high",
                "sync_state": "synchronized",
                "details": ["Function add(left: float, right: float) -> float"],
                "limitations": [],
            },
            "behavior_nodes": [
                {
                    "id": "behavior:calculator:add:0",
                    "category": "Function",
                    "provenance": "verified",
                    "confidence": "high",
                    "items": ["add(left: float, right: float) -> float"],
                },
                {
                    "id": "behavior:calculator:divide:0",
                    "category": "Function",
                    "provenance": "verified",
                    "confidence": "high",
                    "items": ["divide(left: float, right: float) -> float"],
                },
            ],
        }

    # -- rename + lock control ------------------------------------------

    def test_pane_title_is_code_map(self):
        window = MainWindow()
        self.assertEqual(window._twin_header_label.text(), "CODE MAP")
        self.assertEqual(window._twin_header_label.accessibleName(), "Code Map")
        self.assertEqual(window._twin_body.accessibleName(), "Code Map content")

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
        window._on_twin_projection_loaded(_sample_twin_bundle())
        self.assertTrue(window._twin_lock_button.isEnabled())

    # -- follow / pin / unpin -------------------------------------------

    def test_python_selection_auto_renders(self):
        window = MainWindow()
        window._root = "/some/root"
        sent = self._chain_send(window)
        window._on_document_opened("calculator.py", _source_doc("calculator.py"))
        self.assertEqual(
            [r["action"] for r in sent],
            [contract.ACTION_SYNC_TWIN, contract.ACTION_GET_TWIN],
        )
        self.assertEqual(window._twin_chip.text(), "Available")
        self.assertIn("Method handle(request)", window._twin_body.text())

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

    def test_pin_retains_projection_across_switch(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_twin_projection_loaded(_sample_twin_bundle())
        window._twin_lock_button.click()  # pin to app/service.py
        self.assertTrue(window._twin_pinned)
        self.assertTrue(window._twin_lock_button.isChecked())
        self.assertEqual(window._twin_lock_button.accessibleName(), "Unpin Code Map")
        body_before = window._twin_body.text()
        sent = self._fake_send(window)
        window._on_document_opened("other.py", _source_doc("other.py"))
        self.assertEqual(sent, [])
        self.assertEqual(window._twin_body.text(), body_before)
        self.assertEqual(window._active_twin_path, "app/service.py")

    def test_unlock_immediately_follows_active_tab(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_twin_projection_loaded(_sample_twin_bundle())
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
        body_before = window._twin_body.text()
        window._on_twin_projection_loaded(_sample_twin_bundle(), generation=1)
        self.assertEqual(window._twin_body.text(), body_before)

    def test_pinned_survives_unsupported_switch(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_twin_projection_loaded(_sample_twin_bundle())
        window._twin_lock_button.click()
        body_before = window._twin_body.text()
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
        self.assertEqual(window._twin_body.text(), body_before)
        self.assertTrue(window._twin_lock_button.isChecked())

    # -- interactive behavior nodes -------------------------------------

    def test_behavior_nodes_are_interactive_controls(self):
        window = MainWindow()
        window._on_twin_projection_loaded(self._function_bundle())
        self.assertEqual(window._twin_nodes.count(), 2)
        for i in range(2):
            item = window._twin_nodes.item(i)
            button = window._twin_nodes.itemWidget(item)
            self.assertIsInstance(button, QPushButton)
            self.assertNotIsInstance(button, QLabel)
            self.assertTrue(button.accessibleName().startswith("Navigate to Function"))
            self.assertTrue(button.toolTip().startswith("Reveal source for Function"))
            self.assertEqual(button.focusPolicy(), Qt.StrongFocus)

    def test_divide_sends_get_anchor_with_stored_id(self):
        window = MainWindow()
        window._on_twin_projection_loaded(self._function_bundle())
        divide_button = window._twin_nodes.itemWidget(window._twin_nodes.item(1))
        self.assertEqual(divide_button._node_id, "behavior:calculator:divide:0")
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        divide_button.click()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], contract.ACTION_GET_ANCHOR)
        self.assertEqual(sent[0]["task"]["node_id"], "behavior:calculator:divide:0")

    def test_valid_anchor_opens_document_and_sets_reveal_line(self):
        window = MainWindow()
        window._root = "/some/root"
        window._on_twin_projection_loaded(self._function_bundle())
        divide_button = window._twin_nodes.itemWidget(window._twin_nodes.item(1))
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            if request["action"] == contract.ACTION_GET_ANCHOR:
                on_success(
                    {
                        "available": True,
                        "file": "calculator.py",
                        "source_range": {"lineno": 7},
                    }
                )
            return True

        window._send = fake_send
        divide_button.click()
        self.assertEqual(
            [r["action"] for r in sent],
            [contract.ACTION_GET_ANCHOR, contract.ACTION_GET_DOCUMENT],
        )
        self.assertEqual(sent[1]["path"], "calculator.py")
        self.assertEqual(window._pending_reveal_line, 7)

    def test_behavior_node_mouse_and_keyboard_activation(self):
        window = MainWindow()
        window._on_twin_projection_loaded(self._function_bundle())
        divide_button = window._twin_nodes.itemWidget(window._twin_nodes.item(1))
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send

        divide_button.click()
        self.assertEqual(sent[-1]["task"]["node_id"], "behavior:calculator:divide:0")

        QTest.keyClick(divide_button, Qt.Key_Space)
        self.assertEqual(sent[-1]["task"]["node_id"], "behavior:calculator:divide:0")

        QTest.keyClick(divide_button, Qt.Key_Enter)
        self.assertEqual(sent[-1]["task"]["node_id"], "behavior:calculator:divide:0")

        self.assertEqual(len(sent), 3)

    def test_missing_anchor_does_not_move_and_preserves_projection(self):
        window = MainWindow()
        window._on_twin_projection_loaded(self._function_bundle())
        body_before = window._twin_body.text()
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window._on_anchor_loaded({"available": False, "reason": "no_anchor"})
        self.assertEqual(sent, [])
        self.assertEqual(window._twin_body.text(), body_before)
        self.assertIn("failed", window.status_label.text())

    def test_projection_details_are_non_clickable_text(self):
        window = MainWindow()
        window._on_twin_projection_loaded(self._function_bundle())
        self.assertIsInstance(window._twin_body, QLabel)
        self.assertIn("Details:", window._twin_body.text())
        self.assertIn("Function add", window._twin_body.text())
        self.assertEqual(window._twin_nodes.count(), 2)
        for i in range(window._twin_nodes.count()):
            self.assertIsInstance(
                window._twin_nodes.itemWidget(window._twin_nodes.item(i)), QPushButton
            )


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
