"""Minimal PySide6 desktop client for the P3.1 read-only vertical slice.

The client is a *client only*: it supervises a headless backend process, submits
one bounded read-only task, and renders the returned plan, scanner evidence,
limitations, validation status and explicit no-change result. It never imports
the scanner, planner, report builder, provider protocol, Git tooling or any
command-execution code, and it never decides that an action is permitted — that
decision belongs to the boundary.

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
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QProcess, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import contract
from .client_core import (
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_IDLE,
    STATE_RUNNING,
    STATE_SUCCESS,
    STATE_UNAVAILABLE,
    LineBuffer,
    ResponseRouter,
    build_request,
    resolve_backend_command,
)

# Client-side failure reasons for backend misbehaviour that is not a bounded
# boundary error. These are display-only; they are distinct from the contract
# error catalogue, which is reserved for the boundary's own rejections.
REASON_NON_JSON = "non_json_output"
REASON_BACKEND_EXITED = "backend_exited"
REASON_LAUNCH_FAILED = "launch_failed"

_DEFAULT_SCAN_PATH = "fixtures"
_DEFAULT_TIMEOUT_MS = 15000

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

    def submit(self, correlation_id: str, scan_path: str) -> bool:
        """Submit one read-only scan request; returns False if already busy."""
        if self._current is not None:
            return False
        self._current = correlation_id
        self._router.track(correlation_id)
        self._ensure_started()

        line = contract.dumps(build_request(correlation_id, scan_path)) + "\n"
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
    """Render the plan, evidence, limitations, validation and no-change result."""

    def __init__(self, scan_path: str = _DEFAULT_SCAN_PATH, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scan_path = scan_path
        self._supervisor = BackendSupervisor()
        self._supervisor.completed.connect(self._on_completed)
        self._supervisor.failed.connect(self._on_failed)
        self._supervisor.blocked.connect(self._on_blocked)
        self._supervisor.unavailable.connect(self._on_unavailable)
        self._build_ui()
        self._set_status(STATE_IDLE, "ready")

    def _build_ui(self) -> None:
        self.setWindowTitle("Human-Readable Code Agent — P3.1 read-only slice")
        self.resize(900, 600)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        self.run_button = QPushButton("Run read-only scan")
        self.run_button.clicked.connect(self._submit)
        top.addWidget(self.run_button)
        self.status_label = QLabel("")
        top.addWidget(self.status_label, stretch=1)
        layout.addLayout(top)

        self._tabs = QTabWidget(central)
        self._views: Dict[str, CodeView] = {}
        for key, label in (
            ("result", "Result"),
            ("plan", "Plan"),
            ("evidence", "Evidence"),
            ("limitations", "Limitations"),
            ("validation", "Validation"),
            ("outcome", "Outcome"),
        ):
            view = CodeView(self._tabs)
            self._views[key] = view
            self._tabs.addTab(view, label)
        layout.addWidget(self._tabs)

    def _set_status(self, state: str, detail: str = "") -> None:
        self._status = state
        text = f"Status: {state}"
        if detail:
            text += f" — {detail}"
        self.status_label.setText(text)

    def _submit(self) -> None:
        correlation_id = contract.new_correlation_id()
        if not self._supervisor.submit(correlation_id, self._scan_path):
            self._set_status(STATE_FAILED, "a scan is already in progress")
            return
        self._set_status(STATE_RUNNING, correlation_id)

    def _render(self, result: Dict[str, Any]) -> None:
        report = result.get("report", {})
        self._views["result"].setPlainText(_json_text(result))
        self._views["plan"].setPlainText(_json_text(report.get("plan", [])))
        self._views["evidence"].setPlainText(_json_text(result.get("evidence", {})))
        self._views["limitations"].setPlainText(_json_text(report.get("limitations", [])))
        self._views["validation"].setPlainText(_json_text(report.get("validation", {})))
        self._views["outcome"].setPlainText(_json_text(report.get("outcome", {})))

    def _on_completed(self, correlation_id: str, result: Dict[str, Any]) -> None:
        self._render(result)
        self._set_status(STATE_SUCCESS, correlation_id)

    def _on_failed(self, correlation_id: str, reason: str) -> None:
        self._set_status(STATE_FAILED, f"{correlation_id}: {reason}")

    def _on_blocked(self, correlation_id: str) -> None:
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
    window = MainWindow(scan_path=_parse_scan_path(args))
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
    supervisor.submit(correlation_id, scan_path)
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
