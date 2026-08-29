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
    from PySide6.QtCore import QEventLoop, QProcess, QTimer, qInstallMessageHandler
    from PySide6.QtWidgets import (
        QApplication,
        QLabel,
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
        self.assertIn("No Human-Readable Twin", window._twin_body.text())
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
        # The top-level "app" folder is non-leaf, so it carries a collapsed
        # chevron; the leaf "empty_dir" folder shows only its name.
        self.assertEqual(
            window._tree_model.item(0, 0).text(),
            style.tree_folder_label("app", False, False),
        )
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


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ExplorerTreeTests(unittest.TestCase):
    """P3.2 v2.5 Project Explorer completeness and folder affordances.

    The tree renders the complete safe tree with in-text ``›``/``⌄`` chevrons,
    folder rows toggle on click, Right/Left navigate expansion, leaf folders
    show no chevron, and kind-aware documents open through the boundary.
    """

    def setUp(self):
        _app()

    def _window_with_tree(self):
        window = MainWindow()
        window._on_tree_loaded(_sample_tree())
        return window

    def test_folder_chevron_updates_on_mouse_toggle(self):
        window = self._window_with_tree()
        app_item = window._tree_model.item(0, 0)
        index = window._tree_model.indexFromItem(app_item)
        self.assertEqual(app_item.text(), "› app")
        window._on_tree_clicked(index)  # expand
        self.assertTrue(window._tree_view.isExpanded(index))
        self.assertEqual(app_item.text(), "⌄ app")
        window._on_tree_clicked(index)  # collapse
        self.assertFalse(window._tree_view.isExpanded(index))
        self.assertEqual(app_item.text(), "› app")

    def test_folder_chevron_updates_on_keyboard_expand_collapse(self):
        window = self._window_with_tree()
        app_item = window._tree_model.item(0, 0)
        index = window._tree_model.indexFromItem(app_item)
        window._tree_view.setCurrentIndex(index)
        window._tree_view.expand(index)
        self.assertEqual(app_item.text(), "⌄ app")
        window._tree_view.collapse(index)
        self.assertEqual(app_item.text(), "› app")

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
        app_item = window._tree_model.item(0, 0)
        index = window._tree_model.indexFromItem(app_item)
        sent = []

        def fake_send(request, on_success, on_error):
            sent.append(request)
            return True

        window._send = fake_send
        window._on_tree_clicked(index)
        self.assertEqual(sent, [])  # no document request for a folder
        self.assertTrue(window._tree_view.isExpanded(index))

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
        window._on_tree_clicked(index)  # expand a folder

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
