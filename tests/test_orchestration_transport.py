"""ORCH-BACKBONE-1V: the *supervised* transport, not a synchronous stand-in.

`tests/test_orchestration_desktop.py` drives `MainWindow` against an in-process
`boundary.handle_request`. That proves the client's binding but not the shipped
transport: the real desktop supervises a **separate backend process** over
NDJSON on stdin/stdout, and every request crosses that boundary.

This module exercises that path end to end. It spawns the real
:class:`~hrca.ui.client.BackendSupervisor`, roots the child's app data at an
isolated temporary directory through the supported ``XDG_DATA_HOME``
convention (no code path is added or changed to make this testable), and drives
goal → plan → confirmation → run → review → decision, then disposes of both the
backend process and the window, recreates them, and reopens.

Two things are measured rather than asserted by inspection:

* the number of claimed executions, read straight out of the orchestration
  database as a *test-only* observation — never a probe in product code; and
* the synthetic source's bytes, hashed before and after.

If the supervised transport cannot run on a host, the wait below times out and
the assertions fail loudly rather than the test passing for the wrong reason.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QInputDialog

    from hrca.core import identity, storage
    from hrca.ui.client import BackendSupervisor, MainWindow
    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - environment without the desktop extra
    HAS_PYSIDE6 = False


_CLEAN = "def main():\n    return 1\n"
_BROKEN = "def broken(:\n"
_SETTLE_MS = 20_000


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class SupervisedTransportTests(unittest.TestCase):
    """The real desktop talking to a real supervised backend process."""

    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="orcht-root-")
        self.data_home = tempfile.mkdtemp(prefix="orcht-data-")
        self._write("app.py", _CLEAN)
        self._write("broken.py", _BROKEN)
        # The child inherits this, so the backend roots its store here and
        # nowhere near the selected project.
        self._env = mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": self.data_home}
        )
        self._env.start()
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self._dispose(self.window)
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.data_home, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def _write(self, relative: str, text: str) -> None:
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _dispose(self, window) -> None:
        """Stop the supervised backend and drop the window."""
        window._supervisor.terminate()
        window._credential_supervisor.terminate()
        window.close()
        window.deleteLater()
        _app().processEvents()

    def _start(self) -> MainWindow:
        """A real window supervising a real backend process.

        Opening a project also makes the client read back whatever workflow the
        backend already holds, and the supervisor carries one request at a time.
        The wait is therefore on that read *settling*, not merely on the root
        arriving — otherwise the next request is refused as one already in
        flight.
        """
        window = MainWindow()
        window._open_project_root(self.root)
        self.assertTrue(
            self._wait(lambda: window._root == self.root),
            "the supervised backend never reported the opened project",
        )
        self.assertTrue(
            self._wait(lambda: window._orchestration_workflow is not None),
            "the supervised backend never answered the workflow read",
        )
        return window

    def _wait(self, predicate, timeout_ms: int = _SETTLE_MS) -> bool:
        """Pump the Qt event loop until ``predicate`` holds or time runs out."""
        if predicate():
            return True
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(50)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        poll.start()
        guard.start(timeout_ms)
        loop.exec()
        poll.stop()
        guard.stop()
        return bool(predicate())

    def _workflow(self, window) -> dict:
        return window._orchestration_workflow or {}

    @property
    def _database(self) -> str:
        return os.path.join(
            storage.app_data_dir(), "orchestration", "orchestration.db"
        )

    def _run_count(self) -> int:
        """Count claimed executions straight from the store (test-only)."""
        if not os.path.exists(self._database):
            return 0
        connection = sqlite3.connect(self._database)
        try:
            row = connection.execute("SELECT COUNT(*) FROM agent_run").fetchone()
            return int(row[0]) if row else 0
        finally:
            connection.close()

    def source_fingerprint(self) -> str:
        shape = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                with open(full, "rb") as handle:
                    shape.append([os.path.relpath(full, self.root), identity.sha256_hex(handle.read())])
        return identity.sha256_hex(repr(shape).encode("utf-8"))

    def _drive_to_review(self, window) -> dict:
        """goal -> plan -> confirm -> run, through the supervised transport."""
        window.submit_goal("scan the project")
        self.assertTrue(
            self._wait(lambda: self._workflow(window).get("has_plan")),
            "the plan was never persisted through the supervised backend",
        )
        window.confirm_plan()
        self.assertTrue(
            self._wait(lambda: self._workflow(window).get("plan_phase") == "confirmed"),
            "the plan was never confirmed through the supervised backend",
        )
        window.dispatch_job("scan")
        self.assertTrue(
            self._wait(lambda: bool(self._workflow(window).get("run_id"))),
            "the scan never ran through the supervised backend",
        )
        return self._workflow(window)

    # -- acceptance ---------------------------------------------------------
    def test_the_supervised_transport_completes_the_persisted_workflow(self):
        self.window = self._start()
        flow = self._drive_to_review(self.window)
        self.assertTrue(flow["plan_id"].startswith("plan:"))
        self.assertTrue(flow["run_id"].startswith("run:"))
        self.assertEqual(flow["review"]["run_outcome"], "succeeded")
        self.assertEqual(flow["review"]["freshness"], "current")
        self.assertEqual(self._run_count(), 1)

    def test_the_supervised_decision_reaches_the_store(self):
        self.window = self._start()
        self._drive_to_review(self.window)
        with mock.patch.object(
            QInputDialog, "getText", side_effect=[("heron", True), ("seen", True)]
        ):
            self.window.record_decision("approve")
        self.assertTrue(
            self._wait(
                lambda: (self._workflow(self.window).get("review") or {}).get("decision")
            ),
            "the acknowledgement never reached the store",
        )
        decision = self._workflow(self.window)["review"]["decision"]
        self.assertEqual(decision["outcome"], "acknowledged")
        self.assertEqual(decision["actor"], "heron")
        # Approving is not adopting: no baseline appears, and the next action
        # never suggests one.
        resume = self._workflow(self.window)["resume"]
        self.assertIsNone(resume["accepted_baseline_ref"])
        self.assertNotIn("adopt", resume["next_action"].lower())

    def test_a_recreated_desktop_and_backend_recover_the_same_records(self):
        self.window = self._start()
        flow = self._drive_to_review(self.window)
        plan_id, run_id, digest = flow["plan_id"], flow["run_id"], flow["plan_digest"]
        self.assertEqual(self._run_count(), 1)

        # Dispose of both the process and the window, then recreate them.
        self._dispose(self.window)
        self.window = self._start()
        reopened = self._workflow(self.window)
        self.assertTrue(
            self._wait(lambda: self._workflow(self.window).get("has_plan")),
            "the recreated desktop did not recover the persisted workflow",
        )
        reopened = self._workflow(self.window)
        self.assertEqual(reopened["plan_id"], plan_id)
        self.assertEqual(reopened["run_id"], run_id)
        self.assertEqual(reopened["plan_digest"], digest)
        # Reopening is a read: no second execution was claimed.
        self.assertEqual(self._run_count(), 1)

    def test_no_accepted_baseline_is_manufactured_and_source_is_untouched(self):
        before = self.source_fingerprint()
        self.window = self._start()
        self._drive_to_review(self.window)
        self.assertEqual(self.source_fingerprint(), before)
        self.assertIsNone(
            self._workflow(self.window)["resume"]["accepted_baseline_ref"]
        )
        # The store lives in app data, never inside the project.
        self.assertFalse(os.path.exists(os.path.join(self.root, "orchestration")))

    def test_running_before_confirmation_is_refused_with_zero_scans(self):
        self.window = self._start()
        self.window.submit_goal("scan the project")
        self.assertTrue(self._wait(lambda: self._workflow(self.window).get("has_plan")))
        self.assertEqual(self._run_count(), 0)
        # Dispatch without confirming: the local gate refuses and no execution
        # is claimed.
        self.window.dispatch_job("scan")
        _app().processEvents()
        self.assertEqual(self._run_count(), 0)
        self.assertFalse(self._workflow(self.window).get("run_id"))

    def test_a_stale_source_cannot_be_acknowledged_through_the_transport(self):
        self.window = self._start()
        self._drive_to_review(self.window)
        self._write("app.py", "def main():\n    return 2\n")
        self.window._refresh_orchestration()
        self.assertTrue(
            self._wait(
                lambda: (self._workflow(self.window).get("review") or {}).get("freshness")
                == "stale"
            )
        )
        review = self._workflow(self.window)["review"]
        self.assertTrue(review["blocking"])
        with mock.patch.object(
            QInputDialog, "getText", side_effect=[("heron", True), ("", True)]
        ):
            self.window.record_decision("approve")
        _app().processEvents()
        self.assertIsNone(
            (self._workflow(self.window).get("review") or {}).get("decision")
        )


if __name__ == "__main__":
    unittest.main()
