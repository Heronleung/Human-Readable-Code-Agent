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

* **Project Explorer** — a :class:`QTreeView` populated from the boundary's
  filtered ``get_tree`` response (never :class:`QFileSystemModel`, never a
  direct directory walk);
* **Source Code** — closable, read-only :class:`QPlainTextEdit` tabs opened via
  ``get_document``; a document is never read by the client;
* **Human-Readable Twin** — a presentation-only surface that can display the
  bounded empty / loading / available / stale / conflict / unsupported states;
  in this slice no Twin entity exists, so the honest default is ``empty``;
* **Agent Chat** — a disabled composer and send action, labelled as
  provider-backed chat unavailable; no provider, credential, network or
  inference call is ever made;
* **Plan / Diff / Problems / Tests / Evidence** — secondary surfaces that carry
  the P3.1 plan, raw result, validation, limitations and outcome data.

Supervision constraints honoured here:

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
from functools import partial
from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QProcess, QTimer, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QStandardItem,
    QStandardItemModel,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import contract
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
    TWIN_CONFLICT,
    TWIN_EMPTY,
    TWIN_LOADING,
    TWIN_STALE,
    TWIN_UNSUPPORTED,
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
    TWIN_EMPTY: "No Human-Readable Twin has been generated for this project.",
    TWIN_LOADING: "Twin synchronization is in progress.",
    TWIN_AVAILABLE: "A Human-Readable Twin is available.",
    TWIN_STALE: "The Human-Readable Twin is stale relative to the source.",
    TWIN_CONFLICT: "The Human-Readable Twin conflicts with the source.",
    TWIN_UNSUPPORTED: "Human-Readable Twin generation is unsupported for this project.",
}

_PY_KEYWORDS = (
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
    "or", "pass", "raise", "return", "try", "while", "with", "yield",
    "None", "True", "False",
)


class PythonHighlighter(QSyntaxHighlighter):
    """A minimal Python syntax highlighter for :class:`CodeView`."""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: List[tuple] = []

        keyword_fmt = QTextCharFormat()
        keyword_fmt.setForeground(QColor("#c586c0"))
        keyword_fmt.setFontWeight(QFont.Bold)

        string_fmt = QTextCharFormat()
        string_fmt.setForeground(QColor("#ce9178"))

        comment_fmt = QTextCharFormat()
        comment_fmt.setForeground(QColor("#6a9955"))

        number_fmt = QTextCharFormat()
        number_fmt.setForeground(QColor("#b5cea8"))

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

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self._highlighter = PythonHighlighter(self.document())


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
        self._timer.stop()
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            self._started = False
            proc.kill()
            # Bounded wait so the child is actually reaped before the QProcess
            # object is released; kill() is async otherwise.
            proc.waitForFinished(1000)

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


def _json_text(value: Any) -> str:
    """Pretty-print ``value`` for display (non-ASCII rendered readably)."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


class MainWindow(QMainWindow):
    """Render the IDE workspace shell (P3.2 presentation-only surface)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._root: Optional[str] = None
        self._repository_state: str = REPOSITORY_UNVERIFIED
        self._twin_state: str = TWIN_EMPTY
        self._provider_state: str = PROVIDER_UNAVAILABLE
        self._validation_state: str = VALIDATION_IDLE
        self._current_document: Optional[str] = None
        self._tree: Optional[Dict[str, Any]] = None
        self._open_tabs: Dict[str, CodeView] = {}
        self._pending: Dict[str, tuple] = {}
        self._supervisor = BackendSupervisor()
        self._supervisor.completed.connect(self._on_completed)
        self._supervisor.failed.connect(self._on_failed)
        self._supervisor.blocked.connect(self._on_blocked)
        self._supervisor.unavailable.connect(self._on_unavailable)
        self._build_ui()
        self._set_status(STATE_IDLE, "ready")
        self._set_twin_state(TWIN_EMPTY)
        self._update_status()

    # -- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle("Human-Readable Code Agent — IDE Workspace Shell")
        self.resize(1100, 700)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Toolbar row: open a project (directory chooser only) and run a scan.
        top = QHBoxLayout()
        self.open_project_button = QPushButton("Open Project")
        self.open_project_button.clicked.connect(self._on_open_project)
        self.scan_button = QPushButton("Run read-only scan")
        self.scan_button.clicked.connect(self._on_run_scan)
        top.addWidget(self.open_project_button)
        top.addWidget(self.scan_button)
        top.addStretch(1)
        layout.addLayout(top)

        # Main splitter: project explorer on the left, workspace on the right.
        splitter = QSplitter(Qt.Horizontal, central)

        explorer = QWidget(splitter)
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.addWidget(QLabel("Project Explorer"))
        self._tree_model = QStandardItemModel()
        self._tree_model.setHorizontalHeaderLabels(["Name"])
        self._tree_view = QTreeView()
        self._tree_view.setModel(self._tree_model)
        self._tree_view.setHeaderHidden(False)
        self._tree_view.clicked.connect(self._on_tree_clicked)
        explorer_layout.addWidget(self._tree_view)
        self._project_label = QLabel("No project open")
        self._project_label.setWordWrap(True)
        explorer_layout.addWidget(self._project_label)
        splitter.addWidget(explorer)

        right = QSplitter(Qt.Vertical, splitter)

        # Source code: closable, read-only document tabs.
        self._source_tabs = QTabWidget(right)
        self._source_tabs.setTabsClosable(True)
        self._source_tabs.setMovable(True)
        self._source_tabs.tabCloseRequested.connect(self._close_tab)
        right.addWidget(self._source_tabs)

        # Twin / chat / secondary surfaces.
        self._bottom_tabs = QTabWidget(right)
        self._twin_view = CodeView(self._bottom_tabs)
        self._bottom_tabs.addTab(self._twin_view, "Human-Readable Twin")
        self._chat_panel = self._build_chat_panel()
        self._bottom_tabs.addTab(self._chat_panel, "Agent Chat")
        self._views: Dict[str, CodeView] = {}
        for key, label in (
            ("plan", "Plan"),
            ("diff", "Diff"),
            ("problems", "Problems"),
            ("tests", "Tests"),
            ("evidence", "Evidence"),
        ):
            view = CodeView(self._bottom_tabs)
            self._views[key] = view
            self._bottom_tabs.addTab(view, label)
        right.addWidget(self._bottom_tabs)

        splitter.addWidget(right)
        layout.addWidget(splitter)

        # Status fields: primary supervision status plus the P3.2 status labels.
        status_row = QHBoxLayout()
        self.status_label = QLabel("")
        status_row.addWidget(self.status_label, stretch=2)
        self._root_label = QLabel("")
        self._repo_label = QLabel("")
        self._file_label = QLabel("")
        self._twin_label = QLabel("")
        self._provider_label = QLabel("")
        self._validation_label = QLabel("")
        for lbl in (
            self._root_label,
            self._repo_label,
            self._file_label,
            self._twin_label,
            self._provider_label,
            self._validation_label,
        ):
            status_row.addWidget(lbl)
        layout.addLayout(status_row)

    def _build_chat_panel(self) -> QWidget:
        """Build the disabled, provider-unavailable agent-chat surface."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        label = QLabel("Provider-backed chat is unavailable in this read-only slice.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self._chat_composer = QTextEdit()
        self._chat_composer.setPlaceholderText("Chat input is disabled.")
        self._chat_composer.setEnabled(False)
        layout.addWidget(self._chat_composer)
        self._chat_send = QPushButton("Send")
        self._chat_send.setEnabled(False)
        layout.addWidget(self._chat_send)
        layout.addStretch(1)
        return panel

    # -- status helpers --------------------------------------------------

    def _set_status(self, state: str, detail: str = "") -> None:
        self._status = state
        text = f"Status: {state}"
        if detail:
            text += f" — {detail}"
        self.status_label.setText(text)

    def _set_twin_state(self, state: str) -> None:
        self._twin_state = state
        self._twin_view.setPlainText(self._twin_text(state))
        self._update_status()

    def _set_validation_state(self, state: str) -> None:
        self._validation_state = state
        self._update_status()

    def _twin_text(self, state: str) -> str:
        label = _TWIN_LABELS.get(state, "")
        return f"Twin state: {state}\n\n{label}"

    def _update_status(self) -> None:
        self._root_label.setText(f"Root: {self._root or 'none'}")
        self._repo_label.setText(f"Repo: {self._repository_state}")
        self._file_label.setText(f"File: {self._current_document or 'none'}")
        self._twin_label.setText(f"Twin: {self._twin_state}")
        self._provider_label.setText(f"Provider: {self._provider_state}")
        self._validation_label.setText(f"Validation: {self._validation_state}")

    # -- request plumbing ------------------------------------------------

    def _send(self, request: Dict[str, Any], on_success, on_error) -> bool:
        """Submit ``request`` and remember its success/error callbacks."""
        cid = request.get("correlation_id")
        if self._supervisor.submit(cid, request):
            self._pending[cid] = (on_success, on_error)
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
            view = CodeView(self._source_tabs)
            view.setPlainText(content)
            index = self._source_tabs.addTab(view, name)
            self._open_tabs[rel_path] = view
            self._source_tabs.setCurrentIndex(index)
        else:
            view.setPlainText(content)
            self._source_tabs.setCurrentWidget(view)
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
        self._views["diff"].setPlainText(_json_text(report.get("outcome", {})))
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
    app = QApplication.instance() or QApplication(args)
    window = MainWindow()
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PythonHighlighter",
    "CodeView",
    "BackendSupervisor",
    "MainWindow",
    "run_gui",
    "run_scan_once",
    "main",
]
