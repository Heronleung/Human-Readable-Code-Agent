"""PySide6 IDE workspace shell for the P3.2 read-only desktop slice.

The client is a *client only*: it supervises a headless backend process through
the versioned NDJSON boundary, submits bounded read-only workspace actions
(``open_project`` / ``get_tree`` / ``get_document``) plus the read-only scan
pipeline, and renders the results in a read-only IDE-style layout. It never
imports the scanner, planner, report builder, provider protocol, Git tooling or
any command-execution code, never enumerates or reads project files directly
(all filesystem access is mediated by the boundary), and never decides that an
action is permitted — that decision belongs to the boundary.

Layout (presentation only, no semantics invented):

* **Command bar** — ``Open Project`` (primary) and ``Run read-only scan``
  (secondary, disabled until a project is open);
* **Primary workspace** — one horizontal splitter with exactly three children:
  **Project Explorer** (collapsible tree, 240 px default), **Source Code**
  (flat closable tabs over a read-only document view) and an independent
  **Code Map** pane (never nested inside Source Code);
* **Bottom utility panel** — one full-width surface directly beneath the three
  panes: a flat six-tab bar (**Agent Chat | Plan | Diff | Problems | Tests |
  Evidence**) plus a single disclosure control. Agent Chat keeps a message
  area, a disabled composer and send action labelled provider-unavailable (no
  provider, credential, network or inference call is ever made); the other five
  tabs carry the read-only P3.1 plan / diff / problems / tests / evidence.
  Diff is explicitly unavailable in this slice; nothing is ever written to disk;
* **Status bar** — one row with a transient message and six right-aligned
  persistent fields.

Every visual value is owned by :mod:`hrca.style`; no widget hard-codes an
ad-hoc colour, radius or padding. Supervision constraints honoured here:

* the backend is supervised with :class:`QProcess` and ``readyReadStandardOutput``
  plus an incremental :class:`~hrca.client_core.LineBuffer` and a request timeout;
  there are no blocking reads, no ``subprocess.communicate``, and no manual
  threads on the graphical thread;
* cancellation version 1 is terminate-and-restart: the client terminates the
  backend, discards any response whose correlation id no longer matches an
  in-flight request, and marks the abandoned request ``blocked`` rather than
  silently ``failed``;
* code is viewed with :class:`QPlainTextEdit` and :class:`QSyntaxHighlighter`
  (no QScintilla, QtWebEngine, Monaco, CodeMirror or any web-based editor).

The wire protocol stays ASCII (``ensure_ascii=True``); only the *display* uses
``ensure_ascii=False`` so non-ASCII text renders readably.
"""

from __future__ import annotations

import json
import os
import re
import sys
import weakref
from functools import partial
from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QProcess, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QStandardItem,
    QStandardItemModel,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSplitterHandle,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from . import contract, style
from .client_core import (
    PROVIDER_UNAVAILABLE,
    REPOSITORY_UNVERIFIED,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_SUCCESS,
    STATE_UNAVAILABLE,
    TWIN_AVAILABLE,
    TWIN_EMPTY,
    TWIN_LOADING,
    VALIDATION_FAILED,
    VALIDATION_IDLE,
    VALIDATION_OK,
    VALIDATION_RUNNING,
    LineBuffer,
    ResponseRouter,
    block_type_label,
    build_compare_draft_request,
    build_discard_draft_request,
    build_generate_intent_delta_request,
    build_get_code_map_request,
    build_get_document_request,
    build_get_draft_request,
    build_get_tree_request,
    build_open_project_request,
    build_request,
    build_reset_draft_request,
    build_save_draft_request,
    build_scan_request,
    build_sync_twin_request,
    default_fixture_root,
    format_draft_operations,
    format_entity_list,
    format_intent_delta,
    format_procedural_document,
    intent_class_label,
    is_twin_source_path,
    operation_label,
    resolve_backend_command,
    twin_state_from_sync,
)

# Client-side failure reasons for backend misbehaviour that is not a bounded
# boundary error. These are display-only; they are distinct from the contract
# error catalogue, which is reserved for the boundary's own rejections.
REASON_NON_JSON = "non_json_output"
REASON_BACKEND_EXITED = "backend_exited"
REASON_LAUNCH_FAILED = "launch_failed"

_DEFAULT_SCAN_PATH = default_fixture_root()
_DEFAULT_TIMEOUT_MS = 15000

# Fixed, honest one-line descriptions for each bounded Twin presentation state.
# No Twin entity exists in P3.2, so none of these is derived from source; they
# are the only text the surface ever shows and are not persisted.
_TWIN_LABELS = {
    "empty": "No Code Map has been generated for this project.",
    "loading": "Code Map synchronization is in progress.",
    "available": "A Code Map is available.",
    "stale": "The Code Map is stale relative to the source.",
    "conflict": "The Code Map conflicts with the source.",
    "unsupported": "Code Map generation is unsupported for this project.",
}

# Fixed, honest unavailable text for the Diff surface. Until a code proposal
# capability exists there is nothing to diff and no way to apply changes, so
# Diff never implies editing is possible.
_DIFF_UNAVAILABLE = (
    "Diff is unavailable in this read-only slice.\n\n"
    "No code proposal capability exists yet, so there is nothing to diff and "
    "no way to apply changes."
)

# The fixed, honest notice shown on the editable Code Map surface. Every edit
# becomes a Twin Draft only — it never modifies source, Git state or files.
_DRAFT_NOTICE = "Edits create a draft only. Source code is unchanged."

# Fixed, honest unavailable messages for the document surface. Each ``reason``
# is one of the workspace's bounded unavailable reasons; the banner never echoes
# a requested path or file content.
_UNAVAILABLE_TEXT = {
    "binary": "Binary file — preview unavailable.",
    "unsupported_type": "Unsupported file type — preview unavailable.",
    "file_too_large": "File too large to preview.",
    "path_not_found": "File not found.",
    "path_not_readable": "File is not readable.",
}
_UNAVAILABLE_FALLBACK = "This file cannot be previewed."

# The six bottom-panel tabs, in the fixed order the tab bar presents them. The
# first key ("chat") maps to the Agent Chat surface; the remaining five map to
# the read-only secondary surfaces populated by the scan pipeline. The keys are
# the single source of truth for the ``selected_tab`` state.
_BOTTOM_TAB_KEYS = ("chat", "plan", "diff", "problems", "tests", "evidence")

# Human-readable tab labels (one per key, same order).
_BOTTOM_TAB_LABELS = {
    "chat": "Agent Chat",
    "plan": "Plan",
    "diff": "Diff",
    "problems": "Problems",
    "tests": "Tests",
    "evidence": "Evidence",
}

_PY_KEYWORDS = (
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
    "or", "pass", "raise", "return", "try", "while", "with", "yield",
    "None", "True", "False",
)


class _WeakCallback:
    """A weak reference to a request callback's bound instance.

    ``MainWindow._pending`` maps a correlation id to the success/error callback
    for the in-flight request. Storing the bound methods directly forms a
    reference cycle (window → ``_pending`` → bound method → window), which keeps
    the window — and therefore its QProcess-backed supervisor — alive until
    interpreter shutdown, where the QProcess child can be destroyed before any
    Python finalizer reaps it. Wrapping each callback here keeps only a weak
    reference to the instance, so the window is collected as soon as the caller
    drops it and the supervisor's ``__del__`` reaps the backend promptly.
    """

    __slots__ = ("_obj_ref", "_func", "_args")

    def __init__(self, callback) -> None:
        if isinstance(callback, partial):
            bound = callback.func
            self._args = callback.args
        else:
            bound = callback
            self._args = ()
        self._obj_ref = weakref.ref(bound.__self__)
        self._func = bound.__func__

    def __call__(self, *call_args):
        obj = self._obj_ref()
        if obj is None:
            return None
        return self._func.__get__(obj, type(obj))(*self._args, *call_args)


class PythonHighlighter(QSyntaxHighlighter):
    """A minimal Python syntax highlighter whose colours come from the palette."""

    def __init__(self, document, palette: style.Palette) -> None:
        super().__init__(document)
        self._rules: List[tuple] = []

        keyword_fmt = QTextCharFormat()
        keyword_fmt.setForeground(QColor(palette.syntax_keyword))
        keyword_fmt.setFontWeight(QFont.Bold)

        string_fmt = QTextCharFormat()
        string_fmt.setForeground(QColor(palette.syntax_string))

        comment_fmt = QTextCharFormat()
        comment_fmt.setForeground(QColor(palette.syntax_comment))

        number_fmt = QTextCharFormat()
        number_fmt.setForeground(QColor(palette.syntax_number))

        self._rules = [
            (r"\b(?:" + "|".join(_PY_KEYWORDS) + r")\b", keyword_fmt),
            (r"\".*?\"|'.*?'", string_fmt),
            (r"#[^\n]*", comment_fmt),
            (r"\b\d+(?:\.\d+)?\b", number_fmt),
        ]

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class CodeView(QPlainTextEdit):
    """A read-only, monospaced, syntax-highlighted code/JSON view."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        palette: Optional[style.Palette] = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette or style.palette_for()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(style.code_font())
        self._highlighter = PythonHighlighter(self.document(), self._palette)
        self._apply_line_height()

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        self._apply_line_height()

    def _apply_line_height(self) -> None:
        """Set about 1.45 proportional line spacing across the document."""
        fmt = QTextBlockFormat()
        fmt.setLineHeight(
            style.CODE_LINE_HEIGHT_PERCENT,
            QTextBlockFormat.ProportionalHeight.value,
        )
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.Document)
        cursor.mergeBlockFormat(fmt)

    def reveal_line(self, lineno: int) -> None:
        """Move the cursor to ``lineno`` (1-based), select the line, and scroll it into view.

        Selecting the whole line gives a brief visible highlight of the anchored
        source after a behavior-node navigation, without leaving an edit cursor
        (the view stays read-only).
        """
        block = self.document().findBlockByNumber(max(0, int(lineno) - 1))
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()


class DocumentView(QWidget):
    """A read-only document surface: a labelled banner over a code body.

    Three modes, driven by the boundary's document ``kind``:

    * ``source``      — syntax-highlighted source, banner hidden;
    * ``preview``     — plain read-only text under a "Read-only preview" banner;
    * ``unavailable`` — a bounded banner explaining why the file cannot be shown
      (binary / unsupported / missing / unreadable / oversized), empty body.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        palette: Optional[style.Palette] = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette or style.palette_for()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        layout.setSpacing(style.SPACE_0)

        self._banner = QLabel()
        self._banner.setObjectName("documentBanner")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        layout.addWidget(self._banner)

        self._body = CodeView(self, palette=self._palette)
        layout.addWidget(self._body, stretch=1)

    def show_source(self, content: str) -> None:
        """Show syntax-highlighted source with no banner."""
        self._banner.setVisible(False)
        self._body.setPlainText(content)

    def show_preview(self, name: str, content: str) -> None:
        """Show a labelled read-only text preview."""
        self._banner.setText(f"Read-only preview — {name}")
        self._banner.setStyleSheet(style.preview_banner_style(self._palette))
        self._banner.setVisible(True)
        self._body.setPlainText(content)

    def show_unavailable(self, name: str, reason: str) -> None:
        """Show a bounded unavailable banner and an empty body."""
        message = _UNAVAILABLE_TEXT.get(reason, _UNAVAILABLE_FALLBACK)
        self._banner.setText(f"{name} — {message}")
        self._banner.setStyleSheet(style.unavailable_banner_style(self._palette))
        self._banner.setVisible(True)
        self._body.setPlainText("")

    def reveal_line(self, lineno: int) -> None:
        """Scroll the code body to ``lineno`` (1-based source line)."""
        self._body.reveal_line(lineno)


class ElidedLabel(QLabel):
    """A :class:`QLabel` that elides its full text to fit its width.

    ``text()`` returns the full text when the widget has no width yet (so
    offscreen tests read the un-elided value); once laid out, the text is
    elided in the middle (or the given mode) rather than wrapping or growing.
    The complete text is always preserved un-elided in ``fullText()`` and, for
    long paths, in the widget tooltip so it is never lost when it elides.
    """

    def __init__(
        self,
        text: str = "",
        elide_mode: Qt.TextElideMode = Qt.ElideMiddle,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self._elide_mode = elide_mode
        self.setToolTip(text)
        self._refresh()

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._refresh()

    def fullText(self) -> str:
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        if self.width() <= 0:
            super().setText(self._full_text)
        else:
            super().setText(
                self.fontMetrics().elidedText(
                    self._full_text, self._elide_mode, self.width()
                )
            )


class _HairlineHandle(QSplitterHandle):
    """A 1 px hairline splitter handle inside a 6 px interactive hit area."""

    def __init__(
        self,
        orientation: Qt.Orientation,
        parent: QSplitter,
        palette: style.Palette,
    ) -> None:
        super().__init__(orientation, parent)
        self._palette = palette
        self._hovered = False
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.transparent)
        color = QColor(self._palette.accent if self._hovered else self._palette.border)
        painter.setPen(QPen(color, style.SPLITTER_HAIRLINE_WIDTH))
        if self.orientation() == Qt.Horizontal:
            x = self.width() // 2
            painter.drawLine(x, 0, x, self.height())
        else:
            y = self.height() // 2
            painter.drawLine(0, y, self.width(), y)
        painter.end()


class HairlineSplitter(QSplitter):
    """A :class:`QSplitter` whose handles are 1 px hairlines with a 6 px hit area."""

    def __init__(
        self,
        orientation: Qt.Orientation,
        palette: style.Palette,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._palette = palette
        self.setHandleWidth(style.SPLITTER_HANDLE_WIDTH)

    def createHandle(self) -> QSplitterHandle:
        return _HairlineHandle(self.orientation(), self, self._palette)


def _json_text(value: Any) -> str:
    """Pretty-print ``value`` for display (non-ASCII rendered readably)."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


class _ProjectTreeView(QTreeView):
    """A :class:`QTreeView` that toggles a folder on the *first* click.

    Qt delivers a rapid second click as a ``MouseButtonDblClick`` and routes it
    to :meth:`mouseDoubleClickEvent`, which neither emits ``clicked`` /
    ``doubleClicked`` for the branch indicator nor toggles it. The visible
    result is a folder that will not close until the double-click interval has
    elapsed. Toggling on both the press and the double-click makes every click a
    single, immediate toggle, and routing the branch press through
    :class:`QAbstractItemView` (instead of QTreeView's native branch handler)
    prevents a double toggle.
    """

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._toggle_dir_at(event):
            QAbstractItemView.mousePressEvent(self, event)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._toggle_dir_at(event):
            QAbstractItemView.mouseDoubleClickEvent(self, event)
            return
        super().mouseDoubleClickEvent(event)

    def _toggle_dir_at(self, event) -> bool:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return False
        item = self.model().itemFromIndex(index)
        if item is None or item.data(Qt.UserRole + 1) != "dir":
            return False
        if self.isExpanded(index):
            self.collapse(index)
        else:
            self.expand(index)
        return True


class MainWindow(QMainWindow):
    """Render the IDE workspace shell (P3.2 presentation-only surface)."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        palette: Optional[style.Palette] = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette or style.palette_for(QApplication.instance())
        if QApplication.instance() is not None:
            style.apply(QApplication.instance(), self._palette)

        self._root: Optional[str] = None
        self._repository_state: str = REPOSITORY_UNVERIFIED
        self._twin_state: str = TWIN_EMPTY
        self._provider_state: str = PROVIDER_UNAVAILABLE
        self._validation_state: str = VALIDATION_IDLE
        self._current_document: Optional[str] = None
        self._tree: Optional[Dict[str, Any]] = None
        self._open_tabs: Dict[str, DocumentView] = {}
        self._pending: Dict[str, tuple] = {}
        self._pending_reveal_line: Optional[int] = None
        # Monotonic selection generation: every Twin request chain is tagged with
        # the generation that started it, so a late response for a previously
        # selected file is discarded instead of overwriting the current one.
        self._twin_generation: int = 0
        # Follow/pin state for the Code Map pane: unlocked (follow) by default,
        # pinned (lock) to the currently displayed Code Map's source path.
        self._twin_pinned: bool = False
        self._active_twin_path: Optional[str] = None
        # Procedural Code Map (P3.4) state: the current blocks, document, entity
        # list and active entity locator, plus edit-mode / dirty / block bookkeeping.
        self._codemap_blocks: List[Dict[str, Any]] = []
        self._codemap_text: str = ""
        self._codemap_entities: List[Dict[str, Any]] = []
        self._active_entity_locator: Optional[str] = None
        self._codemap_details_visible: bool = False
        self._edit_mode: bool = False
        self._draft_dirty: bool = False
        self._draft_operations: List[Dict[str, Any]] = []
        self._draft_controls: Dict[str, QWidget] = {}
        self._draft_op_kinds: Dict[str, str] = {}
        self._draft_originals: Dict[str, str] = {}
        self._draft_unresolved: Dict[str, QPushButton] = {}
        # Deferred exit intents resolved after a save completes: "edit" returns
        # to the read-only projection; "close" closes the window.
        self._leave_after_save: bool = False
        self._close_after_save: bool = False
        # Single bottom-panel state model (replaces the old drawer/chat booleans):
        # the selected tab key, whether the panel body is visible, and the last
        # usable expanded height to restore on the next expand.
        self._selected_tab: str = "chat"
        self._is_expanded: bool = True
        self._last_expanded_height: int = style.BOTTOM_PANEL_DEFAULT_HEIGHT

        self._supervisor = BackendSupervisor()
        self._supervisor.completed.connect(self._on_completed)
        self._supervisor.failed.connect(self._on_failed)
        self._supervisor.blocked.connect(self._on_blocked)
        self._supervisor.unavailable.connect(self._on_unavailable)
        self._build_ui()
        self._set_status(STATE_IDLE, "ready")
        self._set_twin_state(TWIN_EMPTY)

    # -- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle("Human-Readable Code Agent")
        self.resize(style.WINDOW_DEFAULT_WIDTH, style.WINDOW_DEFAULT_HEIGHT)
        self.setMinimumSize(style.WINDOW_MIN_WIDTH, style.WINDOW_MIN_HEIGHT)

        central = QWidget(self)
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        root.setSpacing(style.SPACE_0)

        root.addWidget(self._build_command_bar())

        self._vertical_splitter = HairlineSplitter(Qt.Vertical, self._palette)
        self._vertical_splitter.setObjectName("verticalSplitter")

        self._horizontal_splitter = HairlineSplitter(Qt.Horizontal, self._palette)
        self._horizontal_splitter.setObjectName("primaryWorkspace")

        self._explorer_panel = self._build_explorer()
        self._source_panel = self._build_source()
        self._twin_panel = self._build_twin()

        self._horizontal_splitter.addWidget(self._explorer_panel)
        self._horizontal_splitter.addWidget(self._source_panel)
        self._horizontal_splitter.addWidget(self._twin_panel)
        self._configure_primary_splitter()

        self._bottom_panel = self._build_bottom_panel()

        self._vertical_splitter.addWidget(self._horizontal_splitter)
        self._vertical_splitter.addWidget(self._bottom_panel)
        self._vertical_splitter.setStretchFactor(0, style.PRIMARY_WORKSPACE_STRETCH)
        self._vertical_splitter.setStretchFactor(1, style.BOTTOM_PANEL_STRETCH)
        self._vertical_splitter.setCollapsible(0, False)
        self._vertical_splitter.setCollapsible(1, False)
        self._vertical_splitter.setSizes(
            [style.PRIMARY_WORKSPACE_INITIAL_HEIGHT, style.BOTTOM_PANEL_DEFAULT_HEIGHT]
        )

        root.addWidget(self._vertical_splitter, stretch=1)
        root.addWidget(self._build_status_bar())

    def _configure_primary_splitter(self) -> None:
        splitter = self._horizontal_splitter
        splitter.setCollapsible(0, True)   # explorer collapsible
        splitter.setCollapsible(1, False)
        splitter.setCollapsible(2, False)
        splitter.setStretchFactor(0, style.EXPLORER_STRETCH)
        splitter.setStretchFactor(1, style.SOURCE_STRETCH)
        splitter.setStretchFactor(2, style.TWIN_STRETCH)
        splitter.setSizes(
            [
                style.EXPLORER_DEFAULT_WIDTH,
                style.PRIMARY_SOURCE_INITIAL_WIDTH,
                style.PRIMARY_TWIN_INITIAL_WIDTH,
            ]
        )

    def _build_command_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("commandBar")
        bar.setFixedHeight(style.COMMAND_BAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(style.INSET, style.SPACE_0, style.INSET, style.SPACE_0)
        layout.setSpacing(style.GAP_TIGHT)

        self.open_project_button = QPushButton("Open Project")
        self.open_project_button.setObjectName("primaryButton")
        self.open_project_button.setAccessibleName("Open Project")
        self.open_project_button.clicked.connect(self._on_open_project)

        self.scan_button = QPushButton("Run read-only scan")
        self.scan_button.setAccessibleName("Run read-only scan")
        self.scan_button.setEnabled(False)
        self.scan_button.setToolTip("Open a project before running a read-only scan.")
        self.scan_button.clicked.connect(self._on_run_scan)

        layout.addWidget(self.open_project_button)
        layout.addWidget(self.scan_button)
        layout.addStretch(1)
        return bar

    def _build_explorer(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("explorerPanel")
        panel.setMinimumWidth(style.EXPLORER_MIN_WIDTH)
        panel.setMaximumWidth(style.EXPLORER_MAX_WIDTH)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        layout.setSpacing(style.SPACE_0)

        header, _ = self._header_row("Project Explorer")
        layout.addWidget(header)

        self._tree_model = QStandardItemModel()
        self._tree_model.setHorizontalHeaderLabels(["Name"])
        self._tree_view = _ProjectTreeView()
        self._tree_view.setObjectName("projectTree")
        self._tree_view.setModel(self._tree_model)
        self._tree_view.setHeaderHidden(True)
        self._tree_view.setRootIsDecorated(True)
        self._tree_view.setIndentation(style.TREE_INDENT)
        self._tree_view.setUniformRowHeights(True)
        self._tree_view.setAnimated(False)
        self._tree_view.setSortingEnabled(False)
        self._tree_view.setAlternatingRowColors(False)
        self._tree_view.setFrameShape(QFrame.NoFrame)
        self._tree_view.setAccessibleName("Project Explorer")
        self._tree_view.clicked.connect(self._on_tree_clicked)
        layout.addWidget(self._tree_view, stretch=1)

        self._project_label = ElidedLabel("No project open")
        self._project_label.setObjectName("projectRootLabel")
        self._project_label.setStyleSheet(style.project_root_label_style(self._palette))
        layout.addWidget(self._project_label)
        return panel

    def _build_source(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sourcePanel")
        panel.setMinimumWidth(style.SOURCE_MIN_WIDTH)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        layout.setSpacing(style.SPACE_0)

        self._source_stack = QStackedWidget()

        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch(1)
        empty_layout.addWidget(
            self._empty_label(
                "No document open — select a file in the Project Explorer to "
                "view its source."
            )
        )
        empty_layout.addStretch(1)
        self._source_stack.addWidget(empty_page)

        self._source_tabs = QTabWidget()
        self._source_tabs.setObjectName("sourceTabs")
        self._source_tabs.setTabsClosable(True)
        self._source_tabs.setMovable(True)
        self._source_tabs.setDocumentMode(True)
        self._source_tabs.setUsesScrollButtons(True)
        self._source_tabs.setElideMode(Qt.ElideNone)
        self._source_tabs.tabCloseRequested.connect(self._close_tab)
        self._source_stack.addWidget(self._source_tabs)

        layout.addWidget(self._source_stack, stretch=1)
        self._source_stack.setCurrentIndex(0)
        return panel

    def _build_twin(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("twinPanel")
        panel.setMinimumWidth(style.TWIN_MIN_WIDTH)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        layout.setSpacing(style.SPACE_0)

        header, header_layout = self._header_row("Code Map")
        self._twin_header_label = header_layout.itemAt(0).widget()
        self._twin_header_label.setAccessibleName("Code Map")
        self._twin_chip = QLabel()
        self._twin_chip.setObjectName("twinChip")
        self._twin_chip.setAccessibleName("Twin state")
        header_layout.addWidget(self._twin_chip)
        header_layout.addStretch(1)

        # Editable Code Map action (P3.4): a checkable control that switches the
        # pane between the read-only procedural document and the draft surface.
        self._edit_button = QPushButton("Edit Code Map")
        self._edit_button.setObjectName("editCodeMapButton")
        self._edit_button.setCheckable(True)
        self._edit_button.setAccessibleName("Edit Code Map")
        self._edit_button.setToolTip(
            "Edit the Code Map procedural blocks; edits become a draft only."
        )
        self._edit_button.toggled.connect(self._on_edit_toggled)
        header_layout.addWidget(self._edit_button)

        # Single monochrome pin control on the right of the header, after the
        # state chip. It is a checkable vector-drawn lock, never an emoji, glyph
        # or icon asset.
        self._twin_lock_button = QToolButton()
        self._twin_lock_button.setObjectName("twinLockButton")
        self._twin_lock_button.setCheckable(True)
        self._twin_lock_button.setFocusPolicy(Qt.StrongFocus)
        self._twin_lock_button.setAccessibleDescription(
            "Pins the Code Map to the current file; unpinning follows the active "
            "source tab."
        )
        self._twin_lock_button.toggled.connect(self._on_twin_lock_toggled)
        header_layout.addWidget(self._twin_lock_button)
        layout.addWidget(header)

        # The pane body is a two-page stack: page 0 is the read-only procedural
        # document (entity list + document + evidence); page 1 is the editable
        # draft surface. Editing never removes the read-only document.
        self._twin_stack = QStackedWidget()
        self._twin_stack.setObjectName("twinStack")
        self._twin_stack.addWidget(self._build_twin_readonly_body())
        self._twin_stack.addWidget(self._build_edit_surface())
        layout.addWidget(self._twin_stack, stretch=1)

        self._update_lock_control()
        self._update_edit_control()
        return panel

    def _build_twin_readonly_body(self) -> QWidget:
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(
            style.INSET, style.GAP_TIGHT, style.INSET, style.INSET
        )
        body_layout.setSpacing(style.GAP_TIGHT)

        # Compact ordered entity list (file view): module / class / function
        # entries. Selecting one reveals that entity's nested procedure.
        entity_header = QWidget()
        entity_header_layout = QHBoxLayout(entity_header)
        entity_header_layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        entity_label = QLabel("Entities")
        entity_label.setObjectName("codemapEntitiesLabel")
        entity_label.setStyleSheet(style.secondary_text_style(self._palette))
        entity_header_layout.addWidget(entity_label)
        entity_header_layout.addStretch(1)

        self._codemap_details_button = QPushButton("Details")
        self._codemap_details_button.setObjectName("codemapDetailsButton")
        self._codemap_details_button.setCheckable(True)
        self._codemap_details_button.setAccessibleName("Show Code Map evidence")
        self._codemap_details_button.toggled.connect(self._on_details_toggled)
        entity_header_layout.addWidget(self._codemap_details_button)
        body_layout.addWidget(entity_header)

        self._codemap_entity_list = QListWidget()
        self._codemap_entity_list.setObjectName("codemapEntityList")
        self._codemap_entity_list.setAccessibleName("Code Map entities")
        self._codemap_entity_list.setMaximumHeight(style.CODEMAP_ENTITY_LIST_MAX_HEIGHT)
        self._codemap_entity_list.setVisible(False)
        self._codemap_entity_list.itemClicked.connect(self._on_entity_selected)
        body_layout.addWidget(self._codemap_entity_list)

        self._codemap_document = QPlainTextEdit()
        self._codemap_document.setObjectName("codemapDocument")
        self._codemap_document.setReadOnly(True)
        self._codemap_document.setAccessibleName("Code Map content")
        body_layout.addWidget(self._codemap_document, stretch=1)

        self._codemap_details = QPlainTextEdit()
        self._codemap_details.setObjectName("codemapDetails")
        self._codemap_details.setReadOnly(True)
        self._codemap_details.setAccessibleName("Code Map evidence")
        self._codemap_details.setVisible(False)
        body_layout.addWidget(self._codemap_details)
        return body

    def _build_edit_surface(self) -> QWidget:
        surface = QWidget()
        surface.setObjectName("editCodeMapSurface")
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(style.INSET, style.GAP_TIGHT, style.INSET, style.INSET)
        layout.setSpacing(style.GAP_TIGHT)

        self._draft_notice = QLabel(_DRAFT_NOTICE)
        self._draft_notice.setObjectName("draftNotice")
        self._draft_notice.setWordWrap(True)
        self._draft_notice.setStyleSheet(style.draft_notice_style(self._palette))
        self._draft_notice.setAccessibleName("Draft notice")
        layout.addWidget(self._draft_notice)

        self._draft_facts = QLabel("")
        self._draft_facts.setObjectName("draftFacts")
        self._draft_facts.setWordWrap(True)
        self._draft_facts.setTextFormat(Qt.PlainText)
        self._draft_facts.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._draft_facts.setStyleSheet(style.secondary_text_style(self._palette))
        self._draft_facts.setAccessibleName("Read-only facts")
        layout.addWidget(self._draft_facts)

        # Structure controls: draft-only note/step inputs. A non-empty value is
        # collected on save as an ``insert_block`` operation (a draft-scoped
        # block, never a verified source fact).
        structure = QWidget()
        structure_layout = QVBoxLayout(structure)
        structure_layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        structure_layout.setSpacing(style.GAP_TIGHT)
        note_row = QWidget()
        note_layout = QHBoxLayout(note_row)
        note_layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        note_layout.setSpacing(style.GAP_TIGHT)
        note_label = QLabel("Add note")
        note_label.setObjectName("draftFieldLabel")
        note_label.setStyleSheet(style.draft_field_label_style(self._palette))
        self._note_input = QLineEdit()
        self._note_input.setObjectName("draftNoteInput")
        self._note_input.setAccessibleName("Add note text")
        self._note_input.textChanged.connect(self._mark_draft_dirty)
        note_layout.addWidget(note_label)
        note_layout.addWidget(self._note_input, stretch=1)
        structure_layout.addWidget(note_row)

        step_row = QWidget()
        step_layout = QHBoxLayout(step_row)
        step_layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        step_layout.setSpacing(style.GAP_TIGHT)
        step_label = QLabel("Add step")
        step_label.setObjectName("draftFieldLabel")
        step_label.setStyleSheet(style.draft_field_label_style(self._palette))
        self._step_input = QLineEdit()
        self._step_input.setObjectName("draftStepInput")
        self._step_input.setAccessibleName("Add step text")
        self._step_input.textChanged.connect(self._mark_draft_dirty)
        step_layout.addWidget(step_label)
        step_layout.addWidget(self._step_input, stretch=1)
        structure_layout.addWidget(step_row)
        layout.addWidget(structure)

        # Scrollable inline edits: one row per editable block (purpose text and
        # decision condition) plus a mark-unresolved toggle. Verified facts are
        # never exposed as editable.
        self._draft_fields = QWidget()
        self._draft_fields_layout = QVBoxLayout(self._draft_fields)
        self._draft_fields_layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        self._draft_fields_layout.setSpacing(style.GAP_TIGHT)
        self._draft_scroll = QScrollArea()
        self._draft_scroll.setObjectName("draftFieldsScroll")
        self._draft_scroll.setWidgetResizable(True)
        self._draft_scroll.setFrameShape(QFrame.NoFrame)
        self._draft_scroll.setWidget(self._draft_fields)
        layout.addWidget(self._draft_scroll, stretch=1)

        # Read-only Compare / Generate result area (hidden until produced).
        self._draft_result = QPlainTextEdit()
        self._draft_result.setObjectName("draftResult")
        self._draft_result.setReadOnly(True)
        self._draft_result.setFixedHeight(style.DRAFT_RESULT_HEIGHT)
        self._draft_result.setAccessibleName("Draft result")
        self._draft_result.setVisible(False)
        layout.addWidget(self._draft_result)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(
            style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0
        )
        actions_layout.setSpacing(style.GAP_TIGHT)
        self.save_draft_button = QPushButton("Save")
        self.discard_draft_button = QPushButton("Discard")
        self.reset_draft_button = QPushButton("Reset")
        self.compare_draft_button = QPushButton("Compare")
        self.generate_draft_button = QPushButton("Generate")
        self.save_draft_button.setAccessibleName("Save Code Map draft")
        self.discard_draft_button.setAccessibleName("Discard Code Map draft")
        self.reset_draft_button.setAccessibleName("Reset Code Map draft")
        self.compare_draft_button.setAccessibleName("Compare Code Map draft")
        self.generate_draft_button.setAccessibleName("Generate Intent Delta")
        self.save_draft_button.clicked.connect(self._save_draft)
        self.discard_draft_button.clicked.connect(self._discard_draft)
        self.reset_draft_button.clicked.connect(self._reset_draft)
        self.compare_draft_button.clicked.connect(self._compare_draft)
        self.generate_draft_button.clicked.connect(self._generate_intent_delta)
        for button in (
            self.save_draft_button,
            self.discard_draft_button,
            self.reset_draft_button,
            self.compare_draft_button,
            self.generate_draft_button,
        ):
            actions_layout.addWidget(button)
        layout.addWidget(actions)
        return surface

    def _build_bottom_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("bottomPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        layout.setSpacing(style.SPACE_0)

        # Header: the flat tab bar plus the single disclosure control at the
        # far right. No separate chat/drawer collapse controls exist.
        header = QWidget()
        header.setObjectName("bottomPanelHeader")
        header.setFixedHeight(style.BOTTOM_PANEL_HEADER_HEIGHT)
        self._bottom_panel_header = header
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(style.INSET, style.SPACE_0, style.INSET, style.SPACE_0)
        header_layout.setSpacing(style.GAP_TIGHT)

        self._bottom_tabs = QTabBar()
        self._bottom_tabs.setObjectName("bottomPanelTabs")
        self._bottom_tabs.setDrawBase(False)
        self._bottom_tabs.setExpanding(False)
        self._bottom_tabs.setUsesScrollButtons(True)
        for key in _BOTTOM_TAB_KEYS:
            self._bottom_tabs.addTab(_BOTTOM_TAB_LABELS[key])
        self._bottom_tabs.currentChanged.connect(self._on_bottom_tab_changed)
        header_layout.addWidget(self._bottom_tabs)
        header_layout.addStretch(1)

        self._disclosure_button = QToolButton()
        self._disclosure_button.setObjectName("bottomPanelDisclosure")
        self._disclosure_button.clicked.connect(self._toggle_expanded)
        header_layout.addWidget(self._disclosure_button)
        layout.addWidget(header)

        # Body: one stacked widget with six pages (Agent Chat + five surfaces).
        self._bottom_body = QStackedWidget()
        self._bottom_body.setObjectName("bottomPanelBody")
        self._bottom_body.setMinimumHeight(style.BOTTOM_PANEL_BODY_MIN_HEIGHT)

        self._bottom_body.addWidget(self._build_chat_page())
        self._views: Dict[str, CodeView] = {}
        for key in _BOTTOM_TAB_KEYS[1:]:
            view = CodeView(self._bottom_body, palette=self._palette)
            self._views[key] = view
            self._bottom_body.addWidget(view)
        self._views["diff"].setPlainText(_DIFF_UNAVAILABLE)
        self._views["diff"].setStyleSheet(style.secondary_text_style(self._palette))

        layout.addWidget(self._bottom_body, stretch=1)

        # Initial state: Agent Chat selected, panel expanded.
        self._bottom_body.setCurrentIndex(0)
        self._update_disclosure()
        return panel

    def _build_chat_page(self) -> QWidget:
        body = QWidget()
        body.setObjectName("chatPanel")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(
            style.INSET, style.GAP_TIGHT, style.INSET, style.INSET
        )
        layout.setSpacing(style.GAP_TIGHT)

        # Message area (empty state until a provider-backed chat exists).
        self._chat_messages = QWidget()
        messages_layout = QVBoxLayout(self._chat_messages)
        messages_layout.addStretch(1)
        messages_layout.addWidget(
            self._empty_label("No messages — provider-backed chat is unavailable.")
        )
        messages_layout.addStretch(1)
        layout.addWidget(self._chat_messages, stretch=1)

        # Composer + send (both disabled; no provider/credential/network call).
        composer_row = QHBoxLayout()
        composer_row.setSpacing(style.GAP_TIGHT)
        self._chat_composer = QTextEdit()
        self._chat_composer.setObjectName("chatComposer")
        self._chat_composer.setPlaceholderText("Chat input is disabled.")
        self._chat_composer.setEnabled(False)
        self._chat_composer.setAccessibleName("Chat input")
        self._chat_composer.setFixedHeight(style.CHAT_COMPOSER_HEIGHT)
        composer_row.addWidget(self._chat_composer, stretch=1)

        self._chat_send = QPushButton("Send")
        self._chat_send.setAccessibleName("Send message")
        self._chat_send.setEnabled(False)
        composer_row.addWidget(self._chat_send)
        layout.addLayout(composer_row)

        notice = QLabel("Provider-backed chat is unavailable in this read-only slice.")
        notice.setObjectName("secondary")
        notice.setStyleSheet(style.secondary_text_style(self._palette))
        notice.setWordWrap(True)
        notice.setAccessibleName("Chat availability")
        layout.addWidget(notice)

        return body

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(style.STATUS_BAR_HEIGHT)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(style.INSET, style.SPACE_0, style.INSET, style.SPACE_0)
        layout.setSpacing(style.GAP_GROUP)

        self.status_label = ElidedLabel("", elide_mode=Qt.ElideRight)
        self.status_label.setStyleSheet(style.status_label_style(self._palette))
        self.status_label.setAccessibleName("Status")
        layout.addWidget(self.status_label, stretch=1)

        self._root_label = self._status_field(max_width=style.STATUS_ROOT_MAX_WIDTH)
        self._repo_label = self._status_field()
        self._file_label = self._status_field(max_width=style.STATUS_FILE_MAX_WIDTH)
        self._twin_label = self._status_field()
        self._provider_label = self._status_field()
        self._validation_label = self._status_field()
        for lbl in (
            self._root_label,
            self._repo_label,
            self._file_label,
            self._twin_label,
            self._provider_label,
            self._validation_label,
        ):
            layout.addWidget(lbl)
        return bar

    # -- small widget helpers -------------------------------------------

    def _header_row(self, text: str):
        """Return a panel header container and its layout (label pre-added)."""
        container = QWidget()
        container.setFixedHeight(style.PANEL_HEADER_HEIGHT)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(style.INSET, style.SPACE_0, style.INSET, style.SPACE_0)
        layout.setSpacing(style.GAP_TIGHT)
        label = QLabel(text.upper())
        label.setFont(style.panel_header_font())
        label.setStyleSheet(style.secondary_text_style(self._palette))
        layout.addWidget(label)
        return container, layout

    def _empty_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("emptyState")
        label.setStyleSheet(style.secondary_text_style(self._palette))
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        return label

    def _status_field(self, max_width: Optional[int] = None) -> ElidedLabel:
        label = ElidedLabel("")
        label.setStyleSheet(style.status_field_style(self._palette))
        if max_width is not None:
            label.setMaximumWidth(max_width)
        return label

    # -- bottom panel tab selection and collapse ------------------------

    def _on_bottom_tab_changed(self, index: int) -> None:
        if 0 <= index < len(_BOTTOM_TAB_KEYS):
            self._selected_tab = _BOTTOM_TAB_KEYS[index]
            self._bottom_body.setCurrentIndex(index)

    def _toggle_expanded(self) -> None:
        self._set_expanded(not self._is_expanded)

    def _set_expanded(self, expanded: bool) -> None:
        """Collapse or expand the bottom panel body.

        Collapsing keeps only the tab/header row and returns the body height to
        the primary workspace; expanding restores the last usable height. The
        selected tab and its body visibility are independent of the collapsed
        state, so switching tabs never creates a second panel.
        """
        if expanded == self._is_expanded:
            self._update_disclosure()
            return
        if expanded:
            self._is_expanded = True
            self._bottom_body.setVisible(True)
            self._bottom_panel.setMinimumHeight(style.BOTTOM_PANEL_MIN_HEIGHT)
            self._bottom_panel.setMaximumHeight(style.BOTTOM_PANEL_MAX_HEIGHT)
            total = sum(self._vertical_splitter.sizes())
            # Restore the last usable height, clamped so the primary workspace
            # always keeps a positive band even after a window shrink.
            panel_height = min(
                self._last_expanded_height, total - style.BOTTOM_PANEL_HEADER_HEIGHT
            )
            panel_height = max(style.BOTTOM_PANEL_MIN_HEIGHT, panel_height)
            self._vertical_splitter.setSizes([total - panel_height, panel_height])
        else:
            self._last_expanded_height = self._vertical_splitter.sizes()[1]
            self._is_expanded = False
            self._bottom_body.setVisible(False)
            header_height = style.BOTTOM_PANEL_HEADER_HEIGHT
            self._bottom_panel.setMinimumHeight(header_height)
            self._bottom_panel.setMaximumHeight(header_height)
            total = sum(self._vertical_splitter.sizes())
            self._vertical_splitter.setSizes([total - header_height, header_height])
        self._update_disclosure()

    def _update_disclosure(self) -> None:
        """Sync the disclosure chevron, accessible name and tooltip to state."""
        if self._is_expanded:
            self._disclosure_button.setText("▾")
            name = "Collapse bottom panel"
        else:
            self._disclosure_button.setText("▴")
            name = "Expand bottom panel"
        self._disclosure_button.setAccessibleName(name)
        self._disclosure_button.setToolTip(name)

    # -- status helpers --------------------------------------------------

    def _set_status(self, state: str, detail: str = "") -> None:
        self._status = state
        text = f"Status: {state}"
        if detail:
            text += f" — {detail}"
        self.status_label.setText(text)

    def _set_twin_chip(self, state: str) -> None:
        """Update only the Twin state chip and the status field.

        Kept separate from :meth:`_set_twin_state` so a projection or sync
        result can set the chip without replacing the richer body text.
        """
        self._twin_state = state
        word = style.TWIN_STATE_WORD.get(state, state.title())
        self._twin_chip.setText(word)
        self._twin_chip.setStyleSheet(style.twin_chip_style(self._palette, state))
        self._twin_chip.setToolTip(word)
        self._update_status()

    def _set_twin_state(self, state: str) -> None:
        self._set_twin_chip(state)
        self._codemap_text = _TWIN_LABELS.get(state, "")
        self._codemap_blocks = []
        self._codemap_entities = []
        self._render_code_map()

    def _set_validation_state(self, state: str) -> None:
        self._validation_state = state
        self._update_status()

    def _update_status(self) -> None:
        self._root_label.setText(f"Root: {self._root or 'none'}")
        self._repo_label.setText(f"Repo: {self._repository_state}")
        self._file_label.setText(f"File: {self._current_document or 'none'}")
        self._twin_label.setText(f"Twin: {self._twin_state}")
        self._provider_label.setText(f"Provider: {self._provider_state}")
        self._validation_label.setText(f"Validation: {self._validation_state}")

    def _update_scan_enabled(self) -> None:
        enabled = self._root is not None
        self.scan_button.setEnabled(enabled)
        self.scan_button.setToolTip(
            "" if enabled else "Open a project before running a read-only scan."
        )

    # -- request plumbing ------------------------------------------------

    def _send(self, request: Dict[str, Any], on_success, on_error) -> bool:
        """Submit ``request`` and remember its success/error callbacks.

        The callbacks are wrapped in :class:`_WeakCallback` so ``_pending`` never
        holds a strong reference back to this window.
        """
        cid = request.get("correlation_id")
        if self._supervisor.submit(cid, request):
            self._pending[cid] = (_WeakCallback(on_success), _WeakCallback(on_error))
            return True
        return False

    # -- actions ---------------------------------------------------------

    def _on_open_project(self) -> None:
        start = self._root or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "Open Project", start)
        if not path:
            return
        cid = contract.new_correlation_id()
        request = build_open_project_request(cid, path)
        self._set_status(STATE_RUNNING, "opening project")
        if not self._send(request, self._on_project_opened, self._on_open_failed):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _on_run_scan(self) -> None:
        if not self._root:
            self._set_status(STATE_FAILED, "no project open")
            return
        cid = contract.new_correlation_id()
        request = build_scan_request(cid, self._root)
        self._set_status(STATE_RUNNING, "scanning")
        self._set_validation_state(VALIDATION_RUNNING)
        if not self._send(request, self._on_scan_completed, self._on_scan_failed):
            self._set_status(STATE_FAILED, "a request is already in progress")
            self._set_validation_state(VALIDATION_IDLE)

    def _open_document(self, rel_path: str) -> None:
        cid = contract.new_correlation_id()
        request = build_get_document_request(cid, rel_path)
        self._set_status(STATE_RUNNING, f"opening {rel_path}")
        on_success = partial(self._on_document_opened, rel_path)
        if not self._send(request, on_success, self._on_document_failed):
            self._set_status(STATE_FAILED, "a request is already in progress")

    # -- action result handlers -----------------------------------------

    def _on_project_opened(self, result: Dict[str, Any]) -> None:
        self._root = result.get("root")
        self._repository_state = result.get("repository_state", REPOSITORY_UNVERIFIED)
        self._project_label.setText(self._root or "No project open")
        self._update_status()
        self._update_scan_enabled()
        cid = contract.new_correlation_id()
        request = build_get_tree_request(cid)
        if self._send(request, self._on_tree_loaded, self._on_tree_failed):
            self._set_status(STATE_RUNNING, "loading project tree")

    def _on_tree_loaded(self, tree: Dict[str, Any]) -> None:
        self._tree = tree
        self._populate_tree(tree)
        self._set_status(STATE_SUCCESS, "project open")
        self._set_validation_state(VALIDATION_OK)
        # Auto-synchronize the Twin once the accepted root is established (a
        # no-op when there is no open project, e.g. in tree-only unit tests).
        self._sync_twin()

    def _on_document_opened(self, rel_path: str, doc: Dict[str, Any]) -> None:
        name = doc.get("name", rel_path)
        kind = doc.get("kind", "source")
        view = self._open_tabs.get(rel_path)
        if view is None:
            view = DocumentView(self._source_tabs, palette=self._palette)
            index = self._source_tabs.addTab(view, name)
            self._open_tabs[rel_path] = view
            self._source_tabs.setCurrentIndex(index)
        else:
            self._source_tabs.setCurrentWidget(view)
        if kind == "preview":
            view.show_preview(name, doc.get("content", ""))
        elif kind == "unavailable":
            view.show_unavailable(name, doc.get("reason", ""))
        else:
            view.show_source(doc.get("content", ""))
        self._source_stack.setCurrentIndex(1)
        self._current_document = rel_path
        self._set_status(STATE_SUCCESS, f"opened {rel_path}")
        self._update_status()

        # Reveal a pending source-anchor line, then load the file's Code Map.
        if self._pending_reveal_line is not None and kind == "source":
            view.reveal_line(self._pending_reveal_line)
            self._pending_reveal_line = None
        self._load_codemap(rel_path)

    def _on_scan_completed(self, result: Dict[str, Any]) -> None:
        self._render_scan(result)
        self._set_status(STATE_SUCCESS, "scan complete")
        self._set_validation_state(VALIDATION_OK)
        # Refresh the selected file's Code Map after a successful scan: a
        # supported file re-syncs and reloads; no supported file is a no-op.
        if self._current_document and is_twin_source_path(self._current_document):
            self._load_codemap(self._current_document)

    # -- Code Map synchronization, load and render (P3.4) ----------------

    def _next_twin_generation(self) -> int:
        """Advance the selection generation and return its new value.

        Every Twin request chain is tagged with the generation that started it;
        a late response whose generation no longer matches is discarded so it can
        never overwrite the currently selected file's projection.
        """
        self._twin_generation += 1
        return self._twin_generation

    def _set_active_twin_path(self, path: Optional[str]) -> None:
        """Record the source path backing the displayed Code Map and refresh the
        pin control. ``None`` means no valid Code Map is shown, so the pin
        control is disabled (unless the pane is already pinned). Clearing the
        path also clears the active entity locator, which disables the edit
        action."""
        self._active_twin_path = path
        if path is None:
            self._active_entity_locator = None
        self._update_lock_control()
        self._update_edit_control()

    def _update_lock_control(self) -> None:
        """Sync the pin control's accessible name, tooltip, enablement and icon
        to the follow/pin state (P3.3)."""
        locked = self._twin_pinned
        enabled = locked or self._active_twin_path is not None
        if locked:
            name = "Unpin Code Map"
            tooltip = "Follow the active source tab"
        elif enabled:
            name = "Pin Code Map"
            tooltip = "Pin Code Map to the current file"
        else:
            name = "Pin Code Map"
            tooltip = "No supported source file is active; open one to pin the Code Map."
        self._twin_lock_button.setAccessibleName(name)
        self._twin_lock_button.setToolTip(tooltip)
        self._twin_lock_button.setEnabled(enabled)
        self._twin_lock_button.setIcon(style.lock_icon(self._palette, locked, enabled))

    def _on_twin_lock_toggled(self, checked: bool) -> None:
        """Pin or unpin the Code Map pane (P3.3).

        Pinning freezes the displayed Code Map to its current source path and
        invalidates any in-flight chain so no late response relabels it.
        Unpinning immediately follows the active supported source tab (a scoped
        sync plus Code Map), or shows a bounded empty state when there is none.
        """
        self._twin_pinned = checked
        if checked:
            self._next_twin_generation()
            self._update_lock_control()
            return
        self._update_lock_control()
        if self._current_document and is_twin_source_path(self._current_document):
            self._load_codemap(self._current_document)
        else:
            self._set_twin_state(TWIN_EMPTY)
            self._set_active_twin_path(None)

    def _sync_twin(self, changed_paths: Optional[List[str]] = None) -> None:
        """Auto-synchronize the Structured Twin for the accepted workspace.

        A no-op without an open project. ``changed_paths`` optionally scopes the
        sync to specific root-relative source paths; ``None`` means full sync.
        """
        if not self._root:
            return
        cid = contract.new_correlation_id()
        request = build_sync_twin_request(cid, changed_paths)
        self._set_twin_chip(TWIN_LOADING)
        if not self._send(request, self._on_twin_synced, self._on_twin_failed):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _load_codemap(self, rel_path: str) -> None:
        """Drive the selection -> sync -> get -> render Code Map lifecycle (P3.4).

        A supported Python source (``.py`` / ``.pyi``) sets the pane Loading,
        synchronizes that file's scope, then loads and renders its procedural
        document. Any other file shows a bounded state and never triggers a
        source sync. When the pane is pinned, the current Code Map is frozen and
        the pin guard returns without following, clearing, reloading or
        relabelling it.
        """
        if not self._root:
            return
        if self._twin_pinned:
            return  # pinned: never follow, clear, reload or relabel the pinned Code Map
        generation = self._next_twin_generation()
        if not is_twin_source_path(rel_path):
            self._set_twin_state(TWIN_EMPTY)
            self._set_active_twin_path(None)
            return
        self._set_twin_chip(TWIN_LOADING)
        self._codemap_text = _TWIN_LABELS[TWIN_LOADING]
        self._codemap_blocks = []
        self._codemap_entities = []
        self._render_code_map()
        self._set_active_twin_path(None)
        self._sync_twin_scoped(rel_path, generation)

    def _sync_twin_scoped(self, rel_path: str, generation: int) -> None:
        """Synchronize the selected file's scope before loading its projection."""
        cid = contract.new_correlation_id()
        request = build_sync_twin_request(cid, [rel_path])
        on_success = partial(self._on_selection_synced, rel_path, generation)
        on_error = partial(self._on_selection_sync_failed, generation)
        if not self._send(request, on_success, on_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _on_selection_synced(
        self, rel_path: str, generation: int, result: Dict[str, Any]
    ) -> None:
        """After a successful scoped sync, load and render the Code Map."""
        if generation != self._twin_generation:
            return
        cid = contract.new_correlation_id()
        request = build_get_code_map_request(cid)
        on_success = partial(self._on_code_map_loaded, generation=generation, rel_path=rel_path)
        on_error = partial(self._on_code_map_failed, generation)
        if not self._send(request, on_success, on_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _on_selection_sync_failed(self, generation: int, reason: str) -> None:
        """A failed selection sync shows a bounded failure state with its reason."""
        if generation != self._twin_generation:
            return
        self._set_status(STATE_FAILED, reason)
        self._set_code_map_failure(reason)

    def _on_twin_synced(self, result: Dict[str, Any]) -> None:
        # A workspace-level sync updates only the chip and status; the rich Code
        # Map body is populated by the follow-up ``get_code_map`` in the
        # selection chain, not here.
        state = result.get("state", "synchronized")
        self._set_twin_chip(twin_state_from_sync(state))
        self._set_status(STATE_SUCCESS, f"twin {state}")

    def _on_code_map_loaded(
        self,
        result: Dict[str, Any],
        generation: Optional[int] = None,
        rel_path: Optional[str] = None,
    ) -> None:
        """Render a loaded Code Map: document, entity list, evidence and state.

        Shared by the selection chain (which passes ``generation``/``rel_path``)
        and the edit surface (which re-requests the scoped blocks with no
        generation). A late response whose generation no longer matches is
        discarded; in edit mode the edit surface is (re)populated afterwards.
        """
        if generation is not None and generation != self._twin_generation:
            return  # a late response for a previously selected file
        baseline = result.get("baseline") or {}
        sync_state = baseline.get("sync_state", "synchronized")
        self._set_twin_chip(twin_state_from_sync(sync_state))
        self._codemap_blocks = result.get("blocks") or []
        self._codemap_entities = result.get("entities") or []
        self._codemap_text = format_procedural_document(result.get("document"))
        self._active_entity_locator = result.get("entity") or None
        self._render_code_map()
        self._render_evidence(result)
        self._set_active_twin_path(rel_path or self._current_document)
        self._set_status(STATE_SUCCESS, f"twin {sync_state}")
        if self._edit_mode:
            self._render_edit_surface(result)

    def _on_twin_failed(self, reason: str) -> None:
        # A workspace-level sync failure leaves no synchronized Code Map.
        self._set_status(STATE_FAILED, reason)
        self._set_twin_state(TWIN_EMPTY)
        self._set_active_twin_path(None)

    def _on_code_map_failed(self, generation: int, reason: str) -> None:
        """A failed Code Map load shows a bounded failure state, never stale Empty."""
        if generation != self._twin_generation:
            return
        self._set_status(STATE_FAILED, reason)
        self._set_code_map_failure(reason)

    def _set_code_map_failure(self, reason: str) -> None:
        """Show an explicit bounded failure state with its reason (Code Map pane only)."""
        self._set_twin_chip(TWIN_EMPTY)
        self._codemap_text = f"Code Map unavailable.\n\nReason: {reason}"
        self._codemap_blocks = []
        self._codemap_entities = []
        self._render_code_map()
        self._set_active_twin_path(None)

    def _render_code_map(self) -> None:
        """Render the read-only procedural document and the compact entity list."""
        self._codemap_document.setPlainText(self._codemap_text)
        self._populate_entity_list()

    def _populate_entity_list(self) -> None:
        """Populate the compact ordered entity list (module/class/function).

        Each entry stores its entity locator as user data; selecting one scopes
        the document to that entity's nested procedure. Verified entities only —
        draft blocks never appear here.
        """
        self._codemap_entity_list.clear()
        for entity in self._codemap_entities:
            locator = entity.get("locator") or "?"
            kind = entity.get("kind") or "unknown"
            subject = entity.get("subject") or entity.get("name") or locator
            label = f"{kind}: {locator} — {subject}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, locator)
            self._codemap_entity_list.addItem(item)
        self._codemap_entity_list.setVisible(bool(self._codemap_entities))

    def _on_entity_selected(self, item) -> None:
        """Scope the Code Map to the selected entity's nested procedure (P3.4)."""
        locator = item.data(Qt.UserRole)
        if not locator:
            return
        generation = self._next_twin_generation()
        cid = contract.new_correlation_id()
        request = build_get_code_map_request(cid, selector=locator)
        self._set_status(STATE_RUNNING, f"loading Code Map for {locator}")
        on_success = partial(
            self._on_code_map_loaded, generation=generation, rel_path=self._current_document
        )
        on_error = partial(self._on_code_map_failed, generation)
        if not self._send(request, on_success, on_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _on_details_toggled(self, checked: bool) -> None:
        """Toggle the source-correspondence evidence view."""
        self._codemap_details_visible = checked
        self._codemap_details.setVisible(checked)
        if checked:
            self._codemap_details_button.setAccessibleName("Hide Code Map evidence")
        else:
            self._codemap_details_button.setAccessibleName("Show Code Map evidence")

    def _render_evidence(self, result: Dict[str, Any]) -> None:
        """Render the source-correspondence evidence for the displayed blocks."""
        if not self._codemap_blocks:
            self._codemap_details.setPlainText("")
            return
        self._codemap_details.setPlainText(
            "\n\n".join(self._format_block_evidence(b) for b in self._codemap_blocks)
        )

    def _format_block_evidence(self, block: Dict[str, Any]) -> str:
        """Return one bounded evidence paragraph for a block: its typed identity,
        provenance, confidence, state, editability and source anchor."""
        label = block_type_label(str(block.get("block_type", "unknown")))
        anchors = block.get("source_anchors") or []
        if anchors:
            first = anchors[0]
            location = f"{first.get('file', '?')}:{first.get('lineno', '?')}"
        else:
            location = "no source anchor"
        lines = [
            f"{label} — {block.get('block_id', '?')}",
            f"  source: {location}",
            f"  provenance: {block.get('provenance', 'unknown')}",
            f"  confidence: {block.get('confidence', 'unknown')}",
            f"  state: {block.get('state', 'unknown')}",
            f"  editability: {block.get('editability', 'unknown')}",
        ]
        reason = block.get("confidence_reason")
        if reason:
            lines.append(f"  reason: {reason}")
        fingerprint = block.get("source_fingerprint")
        if fingerprint:
            lines.append(f"  fingerprint: {fingerprint}")
        return "\n".join(lines)

    # -- Editable Code Map draft (P3.4) ----------------------------------

    def _update_edit_control(self) -> None:
        """Enable the ``Edit Code Map`` action only when a Code Map is displayed,
        and leave edit mode when the target disappears."""
        enabled = self._active_twin_path is not None
        self._edit_button.setEnabled(enabled)
        if not enabled and self._edit_mode:
            self._exit_edit_mode()

    def _on_edit_toggled(self, checked: bool) -> None:
        """Toggle the editable Code Map surface (P3.4)."""
        if checked:
            self._enter_edit_mode()
        else:
            self._attempt_leave_edit_mode()

    def _enter_edit_mode(self) -> None:
        """Show the edit surface and load the editable Code Map blocks."""
        if self._active_twin_path is None:
            self._edit_button.setChecked(False)
            return
        self._edit_mode = True
        self._draft_dirty = False
        self._twin_stack.setCurrentIndex(1)
        cid = contract.new_correlation_id()
        request = build_get_code_map_request(cid, selector=self._active_entity_locator)
        self._set_status(STATE_RUNNING, "loading editable Code Map")
        if not self._send(request, self._on_code_map_loaded, self._on_draft_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _attempt_leave_edit_mode(self) -> None:
        """Leave edit mode, prompting to save or discard a dirty draft first.

        A dirty draft is never auto-saved; the user chooses save, discard or
        remain. Discard drops only the unsaved edits and returns to read-only;
        save leaves after the save completes; remain stays put."""
        if self._draft_dirty:
            choice = self._prompt_dirty_leave()
            if choice == "save":
                self._leave_after_save = True
                self._save_draft()
                return  # exit after a successful save
            if choice == "remain":
                self._edit_button.setChecked(True)
                return
        self._exit_edit_mode()

    def _exit_edit_mode(self) -> None:
        """Return the Code Map pane to its read-only projection."""
        self._edit_mode = False
        self._draft_dirty = False
        self._twin_stack.setCurrentIndex(0)
        self._edit_button.setChecked(False)
        self._set_status(STATE_IDLE, "edit mode closed")

    def _prompt_dirty_leave(self) -> str:
        """Ask how to resolve a dirty draft; returns ``save``/``discard``/
        ``remain``. This is a bounded modal; it never writes or auto-saves."""
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Code Map edits")
        box.setText(
            "You have unsaved Code Map edits. Edits create a draft only; "
            "source code is unchanged."
        )
        save_button = box.addButton("Save", QMessageBox.AcceptRole)
        discard_button = box.addButton("Discard", QMessageBox.DestructiveRole)
        remain_button = box.addButton("Remain", QMessageBox.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is discard_button:
            return "discard"
        return "remain"

    def _owning_entity_locator(self) -> Optional[str]:
        """Return the owning entity locator for draft insert operations.

        Prefers the active (scoped) entity locator; falls back to the module
        entity so whole-document edits still name a valid owning entity."""
        if self._active_entity_locator:
            return self._active_entity_locator
        for entity in self._codemap_entities:
            if entity.get("kind") == "module":
                return entity.get("locator")
        return None

    def _collect_operations(self) -> List[Dict[str, Any]]:
        """Read the current controls into ordered typed draft operations.

        Only authored values are included; an empty or unchanged control is
        skipped so a no-op never fabricates a spurious operation."""
        operations: List[Dict[str, Any]] = []
        owning = self._owning_entity_locator()
        if owning:
            note = (self._note_input.text() or "").strip()
            if note:
                operations.append(
                    {
                        "op": "insert_block",
                        "owning_entity_id": owning,
                        "block_type": "note",
                        "proposed_text": note,
                    }
                )
            step = (self._step_input.text() or "").strip()
            if step:
                operations.append(
                    {
                        "op": "insert_block",
                        "owning_entity_id": owning,
                        "block_type": "step",
                        "proposed_payload": {"operation": "assign"},
                        "proposed_text": step,
                    }
                )

        for key, control in self._draft_controls.items():
            op_kind = self._draft_op_kinds.get(key)
            value = self._read_single_value(control)
            if value is None or value == self._draft_originals.get(key, ""):
                continue
            if op_kind == "purpose":
                operations.append(
                    {"op": "replace_description", "target_block_id": key, "proposed_text": value}
                )
            elif op_kind == "condition":
                operations.append(
                    {
                        "op": "replace_condition_intent",
                        "target_block_id": key,
                        "proposed_condition": value,
                    }
                )

        for block_id, button in self._draft_unresolved.items():
            if button.isChecked():
                operations.append(
                    {"op": "mark_unresolved", "target_block_id": block_id, "reason": "review"}
                )

        return operations

    def _read_single_value(self, control: QWidget) -> Optional[str]:
        if isinstance(control, QLineEdit):
            return control.text().strip() or None
        if isinstance(control, QPlainTextEdit):
            return control.toPlainText().strip() or None
        return None

    def _save_draft(self) -> None:
        """Collect the current controls into typed operations and save the draft."""
        operations = self._collect_operations()
        cid = contract.new_correlation_id()
        request = build_save_draft_request(cid, operations)
        self._set_status(STATE_RUNNING, "saving Code Map draft")
        if not self._send(request, self._on_draft_saved, self._on_draft_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _discard_draft(self) -> None:
        """Discard the saved draft (never touches source)."""
        cid = contract.new_correlation_id()
        request = build_discard_draft_request(cid)
        self._set_status(STATE_RUNNING, "discarding Code Map draft")
        if not self._send(request, self._on_draft_discarded, self._on_draft_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _reset_draft(self) -> None:
        """Reset the draft to the baseline (removes every saved edit)."""
        cid = contract.new_correlation_id()
        request = build_reset_draft_request(cid)
        self._set_status(STATE_RUNNING, "resetting Code Map draft")
        if not self._send(request, self._on_draft_reset, self._on_draft_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _compare_draft(self) -> None:
        """Compare the saved draft against the baseline."""
        cid = contract.new_correlation_id()
        request = build_compare_draft_request(cid)
        self._set_status(STATE_RUNNING, "comparing Code Map draft")
        if not self._send(request, self._on_draft_compared, self._on_draft_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _generate_intent_delta(self) -> None:
        """Generate the deterministic, non-executable Intent Delta."""
        cid = contract.new_correlation_id()
        request = build_generate_intent_delta_request(cid)
        self._set_status(STATE_RUNNING, "generating Intent Delta")
        if not self._send(request, self._on_intent_delta_ready, self._on_draft_error):
            self._set_status(STATE_FAILED, "a request is already in progress")

    def _mark_draft_dirty(self, *_args: Any) -> None:
        self._draft_dirty = True

    def _show_draft_result(self, text: str) -> None:
        self._draft_result.setPlainText(text)
        self._draft_result.setVisible(bool(text))

    def _render_edit_surface(self, result: Dict[str, Any]) -> None:
        """Populate the edit surface's read-only facts and inline editable rows.

        Verified facts stay read-only; only blocks whose ``editability`` names a
        typed operation get an inline one-line editor (purpose text, decision
        condition). Structure controls (Add note / Add step) are always present.
        """
        self._clear_draft_controls()
        baseline = result.get("baseline") or {}
        entity = self._active_entity_locator
        facts = f"Scope: {entity}" if entity else "Scope: whole document"
        facts += f"\nBaseline revision: {baseline.get('baseline_revision') or 'none'}"
        self._draft_facts.setText(facts)

        for block in self._codemap_blocks:
            editability = block.get("editability")
            if editability == "replace_description":
                self._add_edit_row(block, "purpose", block.get("display_text", ""))
            elif editability == "replace_condition_intent":
                self._add_edit_row(block, "condition", self._condition_text(block))

        self._draft_fields_layout.addStretch(1)
        self._show_draft_result("")

    def _condition_text(self, block: Dict[str, Any]) -> str:
        """Return the current condition text for a decision block."""
        payload = block.get("payload") or {}
        return payload.get("condition") or ""

    def _clear_draft_controls(self) -> None:
        """Remove every previously built control and its bookkeeping."""
        while self._draft_fields_layout.count():
            item = self._draft_fields_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._draft_controls.clear()
        self._draft_op_kinds.clear()
        self._draft_originals.clear()
        self._draft_unresolved.clear()

    def _add_edit_row(self, block: Dict[str, Any], op_kind: str, original_text: str) -> None:
        """Add one labelled inline edit row for an editable block.

        The row is a block label plus a one-line editor pre-filled with the
        block's current text and a checkable Mark Unresolved toggle. The editor
        is connected to mark the surface dirty *after* it is pre-filled, so
        loading never marks the surface dirty."""
        block_id = block.get("block_id", "?")
        block_label = block_type_label(str(block.get("block_type", "unknown")))
        label = QLabel(f"{block_label} — {block_id}")
        label.setObjectName("draftFieldLabel")
        label.setWordWrap(True)
        label.setStyleSheet(style.draft_field_label_style(self._palette))
        label.setAccessibleName("Code Map editable block")
        self._draft_fields_layout.addWidget(label)

        control = QLineEdit()
        control.setObjectName("draftSingleEdit")
        control.setAccessibleName(f"{op_kind} editor")
        control.setText(original_text or "")
        self._draft_fields_layout.addWidget(control)

        unresolved = QPushButton("Mark unresolved")
        unresolved.setObjectName("draftMarkUnresolvedButton")
        unresolved.setCheckable(True)
        unresolved.setAccessibleName(f"Mark unresolved {block_id}")
        unresolved.toggled.connect(self._mark_draft_dirty)
        self._draft_fields_layout.addWidget(unresolved)

        self._draft_controls[block_id] = control
        self._draft_op_kinds[block_id] = op_kind
        self._draft_originals[block_id] = original_text or ""
        self._draft_unresolved[block_id] = unresolved
        control.textChanged.connect(self._mark_draft_dirty)

    # -- Editable Code Map draft result handlers -------------------------

    def _on_draft_saved(self, result: Dict[str, Any]) -> None:
        draft = result.get("draft") or {}
        operations = draft.get("operations", [])
        self._draft_dirty = False
        self._show_draft_result(format_draft_operations(operations))
        self._set_status(STATE_SUCCESS, "draft saved")
        if self._close_after_save:
            self._close_after_save = False
            self.close()  # re-enter closeEvent with a clean draft
        elif self._leave_after_save:
            self._leave_after_save = False
            self._exit_edit_mode()

    def _on_draft_discarded(self, result: Dict[str, Any]) -> None:
        self._draft_dirty = False
        self._clear_draft_controls()
        self._show_draft_result("Draft discarded.")
        self._set_status(STATE_SUCCESS, "draft discarded")

    def _on_draft_reset(self, result: Dict[str, Any]) -> None:
        self._draft_dirty = False
        self._show_draft_result("Draft reset to baseline.")
        self._set_status(STATE_SUCCESS, "draft reset")

    def _on_draft_compared(self, result: Dict[str, Any]) -> None:
        conflict = result.get("conflict") or {}
        operations = result.get("operations", [])
        text = format_draft_operations(operations)
        if conflict.get("state") not in (None, "none"):
            text = f"Conflict: {conflict.get('reason', conflict.get('state'))}\n\n{text}"
        self._show_draft_result(text)
        self._set_status(STATE_SUCCESS, "draft compared")

    def _on_intent_delta_ready(self, result: Dict[str, Any]) -> None:
        if result.get("no_change"):
            self._show_draft_result("No changes. The draft is a no-op.")
            self._set_status(STATE_SUCCESS, "no changes")
            return
        self._show_draft_result(format_intent_delta(result.get("intent_delta") or {}))
        self._set_status(STATE_SUCCESS, "Intent Delta generated")

    def _on_draft_error(self, reason: str) -> None:
        self._set_status(STATE_FAILED, reason)
        self._show_draft_result(f"Draft action unavailable.\n\nReason: {reason}")

    # -- action error handlers ------------------------------------------

    def _on_open_failed(self, reason: str) -> None:
        self._set_status(STATE_FAILED, reason)
        self._set_validation_state(VALIDATION_FAILED)
        self._update_scan_enabled()

    def _on_tree_failed(self, reason: str) -> None:
        self._set_status(STATE_FAILED, reason)
        self._set_validation_state(VALIDATION_FAILED)

    def _on_document_failed(self, reason: str) -> None:
        self._set_status(STATE_FAILED, reason)

    def _on_scan_failed(self, reason: str) -> None:
        self._set_status(STATE_FAILED, reason)
        self._set_validation_state(VALIDATION_FAILED)

    # -- rendering -------------------------------------------------------

    def _render_scan(self, result: Dict[str, Any]) -> None:
        """Map the scan result onto the secondary evidence surfaces."""
        report = result.get("report", {})
        evidence = result.get("evidence", {})
        self._views["plan"].setPlainText(_json_text(report.get("plan", [])))
        self._views["diff"].setPlainText(_DIFF_UNAVAILABLE)
        self._views["problems"].setPlainText(
            _json_text(
                {
                    "limitations": report.get("limitations", []),
                    "parse_errors": evidence.get("parse_errors", []),
                }
            )
        )
        self._views["tests"].setPlainText(
            "No test execution is available in this read-only slice."
        )
        self._views["evidence"].setPlainText(
            _json_text(
                {
                    "validation": report.get("validation", {}),
                    "scanner_evidence": evidence,
                    "raw_result": result,
                }
            )
        )

    def _populate_tree(self, tree: Dict[str, Any]) -> None:
        self._tree_model.clear()
        self._tree_model.setHorizontalHeaderLabels(["Name"])
        self._add_tree_nodes(self._tree_model.invisibleRootItem(), tree.get("children", []))

    def _add_tree_nodes(self, parent: QStandardItem, nodes: List[Dict[str, Any]]) -> None:
        for node in nodes:
            node_type = node.get("type")
            name = node.get("name", "")
            children = node.get("children", [])
            if node_type == "dir":
                # The disclosure chevron is painted by the branch style in the
                # fixed indicator slot, never embedded in the label, so the
                # label is the plain name and never shifts when it toggles.
                item = QStandardItem(name)
                item.setEditable(False)
                item.setData(node.get("path"), Qt.UserRole)
                item.setData(node_type, Qt.UserRole + 1)
                item.setFont(style.tree_folder_font())
                parent.appendRow(item)
                self._add_tree_nodes(item, children)
            else:
                item = QStandardItem(name)
                item.setEditable(False)
                item.setData(node.get("path"), Qt.UserRole)
                item.setData(node_type, Qt.UserRole + 1)
                item.setData(node.get("kind"), Qt.UserRole + 2)
                parent.appendRow(item)

    def _on_tree_clicked(self, index) -> None:
        item = self._tree_model.itemFromIndex(index)
        if item is None:
            return
        node_type = item.data(Qt.UserRole + 1)
        if node_type != "file":
            return
        rel_path = item.data(Qt.UserRole)
        if not rel_path:
            return
        self._open_document(rel_path)

    def _close_tab(self, index: int) -> None:
        widget = self._source_tabs.widget(index)
        if widget is None:
            return
        self._source_tabs.removeTab(index)
        for rel_path, view in list(self._open_tabs.items()):
            if view is widget:
                del self._open_tabs[rel_path]
        widget.deleteLater()
        if self._current_document not in self._open_tabs:
            self._current_document = None
        if self._source_tabs.count() == 0:
            self._source_stack.setCurrentIndex(0)
        self._update_status()

    # -- supervisor signal handlers -------------------------------------

    def _on_completed(self, correlation_id: str, result: Dict[str, Any]) -> None:
        callbacks = self._pending.pop(correlation_id, None)
        if callbacks is None:
            return
        on_success, _ = callbacks
        on_success(result)

    def _on_failed(self, correlation_id: str, reason: str) -> None:
        callbacks = self._pending.pop(correlation_id, None)
        if callbacks is None:
            return
        _, on_error = callbacks
        on_error(reason)

    def _on_blocked(self, correlation_id: str) -> None:
        self._pending.pop(correlation_id, None)
        self._set_status(STATE_BLOCKED, correlation_id)
        # A Code Map chain interrupted by a timeout/restart must not stay Loading.
        if self._twin_state == TWIN_LOADING:
            self._set_code_map_failure("blocked")

    def _on_unavailable(self, message: str) -> None:
        self._set_status(STATE_UNAVAILABLE, message)

    def closeEvent(self, event) -> None:
        """Reap the supervised backend when the window closes.

        A dirty Code Map draft blocks close until the user resolves it (save,
        discard or remain); it is never auto-saved."""
        if self._edit_mode and self._draft_dirty:
            choice = self._prompt_dirty_leave()
            if choice == "save":
                self._close_after_save = True
                self._save_draft()
                event.ignore()
                return
            if choice == "remain":
                event.ignore()
                return
        self._supervisor.terminate()
        super().closeEvent(event)


def _parse_scan_path(args: Sequence[str]) -> str:
    for arg in args:
        if arg == "--scan-once" or arg == contract.SERVE_SENTINEL:
            continue
        if not arg.startswith("-"):
            return arg
    return _DEFAULT_SCAN_PATH


def run_gui(argv: Optional[Sequence[str]] = None) -> int:
    """Create and run the desktop window; returns the application exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0]] + args)
    palette = style.palette_for(app)
    style.apply(app, palette)
    window = MainWindow(palette=palette)
    window.show()
    return app.exec()


def run_scan_once(
    scan_path: str = _DEFAULT_SCAN_PATH, timeout_ms: int = _DEFAULT_TIMEOUT_MS
) -> int:
    """Run one supervised scan headlessly and print the result to stdout.

    Used by ``--scan-once`` so the same QProcess supervision path — not the
    boundary's own loop — can be exercised from a script or a frozen build.
    """
    app = QCoreApplication.instance() or QCoreApplication([])
    loop = QEventLoop()
    supervisor = BackendSupervisor(timeout_ms=timeout_ms)
    outcome: Dict[str, Any] = {}

    supervisor.completed.connect(
        lambda cid, result: _finish(outcome, loop, STATE_SUCCESS, result=result)
    )
    supervisor.failed.connect(
        lambda cid, reason: _finish(outcome, loop, STATE_FAILED, reason=reason)
    )
    supervisor.blocked.connect(lambda cid: _finish(outcome, loop, STATE_BLOCKED))
    supervisor.unavailable.connect(
        lambda message: _finish(outcome, loop, STATE_UNAVAILABLE, message=message)
    )

    correlation_id = contract.new_correlation_id()
    supervisor.submit(correlation_id, build_request(correlation_id, scan_path))
    loop.exec()

    # Reap the backend before returning so the supervised QProcess is never
    # destroyed while its child is still running.
    supervisor.terminate()

    if outcome.get("status") == STATE_SUCCESS:
        print(json.dumps(outcome["result"], indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    print(
        f"scan did not complete: {outcome.get('status')} "
        f"({outcome.get('reason') or outcome.get('message') or 'no detail'})",
        file=sys.stderr,
    )
    return 1


def _finish(
    outcome: Dict[str, Any],
    loop: QEventLoop,
    status: str,
    **detail: Any,
) -> None:
    outcome["status"] = status
    outcome.update(detail)
    loop.quit()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: ``--scan-once`` runs headless supervision; else the GUI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--scan-once" in args:
        return run_scan_once(_parse_scan_path(args))
    return run_gui(args)


class BackendSupervisor(QObject):
    """Supervise the headless backend and route its responses.

    The supervisor owns one :class:`QProcess`, an incremental
    :class:`~hrca.client_core.LineBuffer`, a :class:`~hrca.client_core.ResponseRouter`,
    and a single-shot request timeout. It supports one in-flight request at a
    time, which is all this slice requires.
    """

    completed = Signal(str, object)  # correlation_id, result dict
    failed = Signal(str, str)        # correlation_id, reason/code
    blocked = Signal(str)            # correlation_id (abandoned)
    unavailable = Signal(str)        # human-readable backend-level message

    def __init__(
        self,
        command: Optional[List[str]] = None,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._command = command if command is not None else resolve_backend_command()
        self._timeout_ms = timeout_ms
        self._router = ResponseRouter()
        self._line_buffer = LineBuffer()
        self._proc: Optional[QProcess] = None
        self._started = False
        self._current: Optional[str] = None
        self._pending_write: Optional[str] = None

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)

    # -- public ----------------------------------------------------------

    def submit(self, correlation_id: str, request: Dict[str, Any]) -> bool:
        """Submit one pre-built request envelope; returns False if already busy.

        The caller builds the envelope (with :mod:`hrca.client_core` builders)
        so the supervisor never decides what action to take; it only carries the
        bytes across the boundary and matches the response by correlation id.
        """
        if self._current is not None:
            return False
        self._current = correlation_id
        self._router.track(correlation_id)
        self._ensure_started()

        line = contract.dumps(request) + "\n"
        if self._started:
            self._write(line)
        else:
            self._pending_write = line
        self._timer.start(self._timeout_ms)
        return True

    def terminate(self) -> None:
        """Terminate the backend (cancellation v1) and abandon in-flight work."""
        ids = self._router.abandon_all()
        self._current = None
        for correlation_id in ids:
            self.blocked.emit(correlation_id)
        self._reap()

    def _reap(self) -> None:
        """Stop the timer and reap the QProcess child, without emitting signals.

        Kept separate from :meth:`terminate` so a Python finalizer can reap the
        process without emitting ``blocked`` (whose slots reference the owning
        window, which may already be mid-teardown when the finalizer runs).

        ``_current`` is cleared first so the ``finished`` / ``errorOccurred``
        signals that ``waitForFinished`` can trigger during reaping are ignored
        instead of cascading into ``failed`` / ``unavailable``.
        """
        self._current = None
        self._timer.stop()
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            self._started = False
            # Bounded reaping so the child never outlives the QProcess object:
            # graceful SIGTERM first, then SIGKILL, each with a bounded wait.
            if proc.state() != QProcess.NotRunning:
                proc.terminate()
                if not proc.waitForFinished(1500):
                    proc.kill()
                    proc.waitForFinished(1500)
            proc.deleteLater()

    def __del__(self) -> None:
        """Reap the backend when the supervisor is collected (no explicit close).

        PySide6 does not emit ``QObject.destroyed`` when a parent-less object is
        reclaimed by the Python garbage collector, so a Python-level finalizer is
        the only reliable hook to terminate the QProcess child before its C++
        side is destroyed. It reaps directly (via :meth:`_reap`) rather than
        through :meth:`terminate`, because emitting ``blocked`` here would invoke
        slots on a window that is already being torn down.

        :meth:`_reap` is bounded (SIGTERM, then SIGKILL, each with a bounded
        wait) and idempotent (it clears ``_proc`` before reaping), so a second
        call is safe. The only exception tolerated here is the narrow PySide6
        ``RuntimeError`` raised when Qt has already torn down the child
        ``QObject`` (``Internal C++ object already deleted``) during interpreter
        shutdown — in that case there is no live process left to reap. Any other
        exception propagates: an unexpected lifecycle failure must not be hidden.
        """
        try:
            self._reap()
        except RuntimeError:
            pass

    # -- QProcess plumbing ----------------------------------------------

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.NotRunning:
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.started.connect(self._on_started)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.errorOccurred.connect(self._on_error)
        proc.finished.connect(self._on_finished)
        self._proc = proc
        self._started = False
        self._pending_write = None
        proc.start(self._command[0], self._command[1:])

    def _write(self, line: str) -> None:
        if self._proc is not None:
            self._proc.write(line.encode("utf-8"))

    def _on_started(self) -> None:
        self._started = True
        if self._pending_write is not None:
            line = self._pending_write
            self._pending_write = None
            self._write(line)

    def _on_stdout(self) -> None:
        proc = self.sender()
        if proc is None or proc is not self._proc:
            return
        data = bytes(proc.readAllStandardOutput())
        text = data.decode("utf-8", errors="replace")
        try:
            lines = self._line_buffer.feed(text)
        except contract.ContractError:
            self._fail_current("message_too_large")
            self.terminate()
            return
        for line in lines:
            if not line:
                continue
            try:
                envelope = contract.loads(line)
            except (ValueError, UnicodeDecodeError):
                self._fail_current(REASON_NON_JSON)
                self.terminate()
                return
            if not isinstance(envelope, dict):
                self._fail_current(REASON_NON_JSON)
                self.terminate()
                return
            self._route(envelope)

    def _on_stderr(self) -> None:
        # Backend logs belong on stderr; the client drains them so the pipe
        # never fills and, in this slice, ignores their content.
        proc = self.sender()
        if proc is not None and proc is self._proc:
            proc.readAllStandardError()

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if self.sender() is not None and self.sender() is not self._proc:
            return
        if self._current is not None:
            self._fail_current(REASON_LAUNCH_FAILED)
            self.unavailable.emit("backend failed to start")

    def _on_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        proc = self.sender()
        if self._current is not None:
            self._fail_current(REASON_BACKEND_EXITED)
        if self._proc is proc:
            self._proc = None
            self._started = False

    def _on_timeout(self) -> None:
        # Cancellation v1: terminate and restart; the abandoned request is
        # marked blocked, never silently failed.
        self.terminate()

    # -- routing ---------------------------------------------------------

    def _route(self, envelope: Dict[str, Any]) -> None:
        correlation_id = envelope.get("correlation_id")
        if not self._router.match(correlation_id):
            # Stale response whose correlation id no longer matches an
            # in-flight request: discard.
            return
        self._router.resolve(correlation_id)
        self._current = None
        self._timer.stop()
        if envelope.get("ok"):
            self.completed.emit(correlation_id, envelope.get("result", {}))
        else:
            error = envelope.get("error") or {}
            code = error.get("code") if isinstance(error.get("code"), str) else "internal_error"
            self.failed.emit(correlation_id, code)

    def _fail_current(self, reason: str) -> None:
        if self._current is None:
            return
        correlation_id = self._current
        self._current = None
        self._router.resolve(correlation_id)
        self._timer.stop()
        self.failed.emit(correlation_id, reason)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PythonHighlighter",
    "CodeView",
    "DocumentView",
    "ElidedLabel",
    "HairlineSplitter",
    "BackendSupervisor",
    "MainWindow",
    "run_gui",
    "run_scan_once",
    "main",
]
