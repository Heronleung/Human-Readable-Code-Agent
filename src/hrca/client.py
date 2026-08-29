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
  **Human-Readable Twin** pane (never nested inside Source Code);
* **Agent Chat** — a full-width lower surface directly beneath the three panes:
  a message area, a disabled composer and send action, labelled provider-
  unavailable; no provider, credential, network or inference call is ever made;
* **Review & Evidence drawer** — a collapsed-by-default secondary surface that
  carries the P3.1 plan / diff / problems / tests / evidence, opened from a
  labelled control near the chat header; it is never written to disk;
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
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSplitterHandle,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
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
    TWIN_EMPTY,
    VALIDATION_FAILED,
    VALIDATION_IDLE,
    VALIDATION_OK,
    VALIDATION_RUNNING,
    LineBuffer,
    ResponseRouter,
    build_get_document_request,
    build_get_tree_request,
    build_open_project_request,
    build_request,
    build_scan_request,
    default_fixture_root,
    resolve_backend_command,
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
    "empty": "No Human-Readable Twin has been generated for this project.",
    "loading": "Twin synchronization is in progress.",
    "available": "A Human-Readable Twin is available.",
    "stale": "The Human-Readable Twin is stale relative to the source.",
    "conflict": "The Human-Readable Twin conflicts with the source.",
    "unsupported": "Human-Readable Twin generation is unsupported for this project.",
}

# Fixed, honest unavailable text for the Diff surface. Until a code proposal
# capability exists there is nothing to diff and no way to apply changes, so
# Diff never implies editing is possible.
_DIFF_UNAVAILABLE = (
    "Diff is unavailable in this read-only slice.\n\n"
    "No code proposal capability exists yet, so there is nothing to diff and "
    "no way to apply changes."
)

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
        self._open_tabs: Dict[str, CodeView] = {}
        self._pending: Dict[str, tuple] = {}
        self._drawer_expanded: bool = False

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

        self._lower_area = self._build_lower_area()

        self._vertical_splitter.addWidget(self._horizontal_splitter)
        self._vertical_splitter.addWidget(self._lower_area)
        self._vertical_splitter.setStretchFactor(0, style.PRIMARY_WORKSPACE_STRETCH)
        self._vertical_splitter.setStretchFactor(1, style.LOWER_AREA_STRETCH)
        self._vertical_splitter.setCollapsible(0, False)
        self._vertical_splitter.setCollapsible(1, False)
        self._vertical_splitter.setSizes(
            [style.PRIMARY_WORKSPACE_INITIAL_HEIGHT, style.LOWER_DEFAULT_HEIGHT]
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
        self._tree_view = QTreeView()
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

        header, header_layout = self._header_row("Human-Readable Twin")
        self._twin_chip = QLabel()
        self._twin_chip.setObjectName("twinChip")
        self._twin_chip.setAccessibleName("Twin state")
        header_layout.addWidget(self._twin_chip)
        layout.addWidget(header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(
            style.INSET, style.GAP_TIGHT, style.INSET, style.INSET
        )
        self._twin_body = QLabel()
        self._twin_body.setObjectName("twinBody")
        self._twin_body.setWordWrap(True)
        self._twin_body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._twin_body.setTextFormat(Qt.PlainText)
        self._twin_body.setMaximumWidth(style.TWIN_CONTENT_MAX_WIDTH)
        self._twin_body.setAccessibleName("Human-Readable Twin content")
        body_layout.addWidget(self._twin_body)
        body_layout.addStretch(1)
        layout.addWidget(body, stretch=1)
        return panel

    def _build_lower_area(self) -> QWidget:
        area = QWidget()
        area.setObjectName("lowerArea")
        layout = QVBoxLayout(area)
        layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        layout.setSpacing(style.SPACE_0)

        # Chat header: title, chat-collapse, and the single labelled control
        # that opens the Review & Evidence drawer.
        self._chat_header = QWidget()
        self._chat_header.setObjectName("chatHeader")
        chat_header_layout = QHBoxLayout(self._chat_header)
        chat_header_layout.setContentsMargins(style.INSET, style.SPACE_0, style.INSET, style.SPACE_0)
        chat_header_layout.setSpacing(style.GAP_TIGHT)

        chat_title = QLabel("Agent Chat".upper())
        chat_title.setFont(style.panel_header_font())
        chat_title.setStyleSheet(style.secondary_text_style(self._palette))
        chat_header_layout.addWidget(chat_title)
        chat_header_layout.addStretch(1)

        self._chat_collapse_button = QToolButton()
        self._chat_collapse_button.setText("▴")
        self._chat_collapse_button.setToolTip("Collapse chat to its header")
        self._chat_collapse_button.setAccessibleName("Collapse Agent Chat")
        self._chat_collapse_button.clicked.connect(self._toggle_chat)
        chat_header_layout.addWidget(self._chat_collapse_button)

        self._drawer_toggle_button = QToolButton()
        self._drawer_toggle_button.setText("▾")
        self._drawer_toggle_button.setAccessibleName("Open Review & Evidence")
        self._drawer_toggle_button.setToolTip("Open Review & Evidence")
        self._drawer_toggle_button.setCheckable(True)
        self._drawer_toggle_button.clicked.connect(self._on_drawer_toggle)
        chat_header_layout.addWidget(self._drawer_toggle_button)
        layout.addWidget(self._chat_header)

        self._chat_body = self._build_chat_body()
        layout.addWidget(self._chat_body, stretch=1)

        layout.addWidget(self._build_drawer())
        return area

    def _build_chat_body(self) -> QWidget:
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

        body.setMinimumHeight(style.CHAT_BODY_MIN_HEIGHT)
        return body

    def _build_drawer(self) -> QWidget:
        self._drawer = QWidget()
        self._drawer.setObjectName("drawer")
        layout = QVBoxLayout(self._drawer)
        layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        layout.setSpacing(style.SPACE_0)

        # Compact header row: the plain-text label only (no leading glyph).
        self._drawer_header = QWidget()
        self._drawer_header.setObjectName("drawerHeader")
        header_layout = QHBoxLayout(self._drawer_header)
        header_layout.setContentsMargins(style.INSET, style.SPACE_0, style.INSET, style.SPACE_0)
        header_layout.setSpacing(style.GAP_TIGHT)

        drawer_label = QLabel("Review & Evidence")
        drawer_label.setAccessibleName("Review & Evidence")
        drawer_label.setStyleSheet(style.secondary_text_style(self._palette))
        header_layout.addWidget(drawer_label)
        header_layout.addStretch(1)
        layout.addWidget(self._drawer_header)

        # Body: the five surfaces as tabs inside the expanded drawer.
        self._drawer_body = QWidget()
        self._drawer_body.setObjectName("drawerBody")
        self._drawer_body.setMinimumHeight(style.DRAWER_BODY_MIN_HEIGHT)
        body_layout = QVBoxLayout(self._drawer_body)
        body_layout.setContentsMargins(style.SPACE_0, style.SPACE_0, style.SPACE_0, style.SPACE_0)
        body_layout.setSpacing(style.SPACE_0)

        self._drawer_tabs = QTabWidget()
        self._drawer_tabs.setObjectName("drawerTabs")
        self._views: Dict[str, CodeView] = {}
        for key, label in (
            ("plan", "Plan"),
            ("diff", "Diff"),
            ("problems", "Problems"),
            ("tests", "Tests"),
            ("evidence", "Evidence"),
        ):
            view = CodeView(self._drawer_tabs, palette=self._palette)
            self._views[key] = view
            self._drawer_tabs.addTab(view, label)
        self._views["diff"].setPlainText(_DIFF_UNAVAILABLE)
        self._views["diff"].setStyleSheet(style.secondary_text_style(self._palette))
        body_layout.addWidget(self._drawer_tabs, stretch=1)
        layout.addWidget(self._drawer_body)

        self._drawer_body.hide()
        return self._drawer

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

    # -- drawer / chat toggles ------------------------------------------

    def _on_drawer_toggle(self) -> None:
        self._set_drawer_expanded(not self._drawer_expanded)

    def _set_drawer_expanded(self, expanded: bool) -> None:
        self._drawer_expanded = expanded
        self._drawer_body.setVisible(expanded)
        self._drawer_toggle_button.setText("▴" if expanded else "▾")
        self._drawer_toggle_button.setAccessibleName(
            "Close Review & Evidence" if expanded else "Open Review & Evidence"
        )
        self._drawer_toggle_button.setToolTip(
            "Close Review & Evidence" if expanded else "Open Review & Evidence"
        )
        self._drawer_toggle_button.setChecked(expanded)

    def _toggle_chat(self) -> None:
        visible = self._chat_body.isVisible()
        self._chat_body.setVisible(not visible)
        expanded = not visible
        self._chat_collapse_button.setText("▴" if expanded else "▾")
        self._chat_collapse_button.setAccessibleName(
            "Collapse Agent Chat" if expanded else "Expand Agent Chat"
        )
        self._chat_collapse_button.setToolTip(
            "Collapse chat to its header" if expanded else "Expand Agent Chat"
        )

    # -- status helpers --------------------------------------------------

    def _set_status(self, state: str, detail: str = "") -> None:
        self._status = state
        text = f"Status: {state}"
        if detail:
            text += f" — {detail}"
        self.status_label.setText(text)

    def _set_twin_state(self, state: str) -> None:
        self._twin_state = state
        word = style.TWIN_STATE_WORD.get(state, state.title())
        self._twin_chip.setText(word)
        self._twin_chip.setStyleSheet(style.twin_chip_style(self._palette, state))
        self._twin_chip.setToolTip(word)
        self._twin_body.setText(_TWIN_LABELS.get(state, ""))
        self._update_status()

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

    def _on_document_opened(self, rel_path: str, doc: Dict[str, Any]) -> None:
        content = doc.get("content", "")
        name = doc.get("name", rel_path)
        view = self._open_tabs.get(rel_path)
        if view is None:
            view = CodeView(self._source_tabs, palette=self._palette)
            view.setPlainText(content)
            index = self._source_tabs.addTab(view, name)
            self._open_tabs[rel_path] = view
            self._source_tabs.setCurrentIndex(index)
        else:
            view.setPlainText(content)
            self._source_tabs.setCurrentWidget(view)
        self._source_stack.setCurrentIndex(1)
        self._current_document = rel_path
        self._set_status(STATE_SUCCESS, f"opened {rel_path}")
        self._update_status()

    def _on_scan_completed(self, result: Dict[str, Any]) -> None:
        self._render_scan(result)
        self._set_status(STATE_SUCCESS, "scan complete")
        self._set_validation_state(VALIDATION_OK)

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
            item = QStandardItem(node.get("name", ""))
            item.setEditable(False)
            item.setData(node.get("path"), Qt.UserRole)
            item.setData(node.get("type"), Qt.UserRole + 1)
            if node.get("type") == "dir":
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            parent.appendRow(item)
            if node.get("type") == "dir":
                self._add_tree_nodes(item, node.get("children", []))

    def _on_tree_clicked(self, index) -> None:
        item = self._tree_model.itemFromIndex(index)
        if item is None:
            return
        if item.data(Qt.UserRole + 1) != "file":
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

    def _on_unavailable(self, message: str) -> None:
        self._set_status(STATE_UNAVAILABLE, message)

    def closeEvent(self, event) -> None:
        """Reap the supervised backend when the window closes."""
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
    "ElidedLabel",
    "HairlineSplitter",
    "BackendSupervisor",
    "MainWindow",
    "run_gui",
    "run_scan_once",
    "main",
]
