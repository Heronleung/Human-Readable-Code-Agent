"""ORCH-BACKBONE-1B: the desktop bound to the persisted workflow.

These drive a real :class:`MainWindow` through the real boundary request loop.
The window's ``_send`` is routed straight into ``boundary.handle_request`` over
an isolated store, so every click exercises the same request path the shipped
client uses — there is no fake backend and no stubbed response.

Two things are measured rather than asserted by inspection: the scanner's
dispatch count (so "the desktop did not rescan on reopen" is a number) and the
synthetic source's bytes before and after (so the read-only claim is checked in
bytes).

The non-success cases matter as much as the happy path: a bounded backend
refusal must reach the surface as a refusal, and must never become a success
card.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QInputDialog

    from hrca import boundary
    from hrca.core import identity
    from hrca.source import scanner
    from hrca.ui.client import MainWindow
    from hrca.ui.destinations.review_page import ReviewDestination
    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - environment without the desktop extra
    HAS_PYSIDE6 = False


_CLEAN = "def main():\n    return 1\n"
_BROKEN = "def broken(:\n"


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


class _Backend:
    """A real boundary session the window can talk to synchronously."""

    def __init__(self, store_base: str) -> None:
        self.session = boundary.WorkspaceSession(store_base=store_base)
        self.responses = []

    def send(self, request, on_success, on_error):
        """Answer one client request through the real request path."""
        response = boundary.handle_request(request, session=self.session)
        self.responses.append(response)
        if response.get("ok"):
            on_success(response.get("result"))
        else:
            on_error((response.get("error") or {}).get("code", "internal_error"))
        return True


class _DesktopCase(unittest.TestCase):
    def setUp(self):
        _app()
        self.root = tempfile.mkdtemp(prefix="orchd-root-")
        self.store_base = tempfile.mkdtemp(prefix="orchd-store-")
        self._write("app.py", _CLEAN)
        self._write("broken.py", _BROKEN)
        self.scans = 0
        real = scanner.scan_directory

        def counted(path):
            self.scans += 1
            return real(path)

        patcher = mock.patch.object(scanner, "scan_directory", counted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if getattr(self, "window", None) is not None:
            self._close(self.window)
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.store_base, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def _write(self, relative: str, text: str) -> None:
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _close(self, window) -> None:
        window._supervisor.terminate()
        window._credential_supervisor.terminate()
        window.close()
        window.deleteLater()

    def _start(self, backend: _Backend) -> MainWindow:
        """Create a window wired to ``backend`` and bound to the project.

        The project is opened through the same request path the app uses, so the
        backend session is the one that establishes the root — the window never
        tells the boundary what its root is.
        """
        window = MainWindow()
        window._send = backend.send
        window._send_credential = backend.send
        window._open_project_root(self.root)
        return window

    def source_fingerprint(self) -> str:
        shape = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                with open(full, "rb") as handle:
                    shape.append([os.path.relpath(full, self.root), identity.sha256_hex(handle.read())])
        return identity.sha256_hex(repr(shape).encode("utf-8"))

    def drive_flow(self, window: MainWindow):
        """goal -> plan -> confirm -> run, exactly as the UI does it."""
        window.submit_goal("scan the project")
        window.confirm_plan()
        window.dispatch_job("scan")
        return window._orchestration_workflow


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class DesktopFlowTests(_DesktopCase):
    def setUp(self):
        super().setUp()
        self.backend = _Backend(self.store_base)
        self.window = self._start(self.backend)

    def test_the_desktop_completes_the_persisted_workflow(self):
        flow = self.drive_flow(self.window)
        self.assertIsNotNone(flow)
        self.assertTrue(flow["has_plan"])
        self.assertTrue(flow["plan_id"])
        self.assertTrue(flow["run_id"])
        self.assertEqual(flow["review"]["run_outcome"], "succeeded")
        self.assertEqual(flow["review"]["freshness"], "current")
        self.assertEqual(self.scans, 1)

    def test_the_desktop_renders_the_persisted_review(self):
        self.drive_flow(self.window)
        page = self.window._review_destination
        page.refresh()
        from PySide6.QtWidgets import QLabel

        text = "\n".join(label.text() for label in page.findChildren(QLabel))
        # The persisted record ids and the honest coverage are on screen.
        self.assertIn(self.window._orchestration_workflow["run_id"], text)
        self.assertIn("Acceptance coverage", text)
        self.assertIn("What is under review", text)

    def test_a_parse_error_is_rendered_as_a_preserved_limitation(self):
        # The synthetic source has a file that cannot be parsed. The scan
        # succeeded, so the supported checks pass — but the review also shows
        # the limitation rather than a clean bill of health.
        self.drive_flow(self.window)
        review = self.window._orchestration_workflow["review"]
        self.assertTrue(review["limitations"])
        self.assertIn("could not be parsed", review["limitations"][0])
        page = self.window._review_destination
        page.refresh()
        from PySide6.QtWidgets import QLabel

        text = "\n".join(label.text() for label in page.findChildren(QLabel))
        self.assertIn("Limitations", text)
        self.assertIn("could not be parsed", text)

    def test_no_supported_check_is_covered_by_a_state_the_backend_did_not_return(self):
        # Before any run the backend still answers, and every supported check is
        # uncovered because no evidence exists. A UI-side "completed" has
        # nothing to attach to.
        self.window.submit_goal("scan the project")
        review = self.window._orchestration_workflow["review"]
        self.assertEqual(review["run_outcome"], "")
        self.assertTrue(review["coverage"])
        self.assertFalse(any(row["covered"] for row in review["coverage"]))
        self.assertTrue(review["blocking"])

    def test_an_unsupported_requirement_renders_as_open(self):
        # The renderer is what the acceptance calls out: a criterion the slice
        # cannot establish must read as open however the scan turned out, even
        # beside supported checks that passed.
        page = self.window._review_destination
        page.clear_body()
        page._render_persisted(
            {
                "goal": "scan the project",
                "plan_id": "plan:x",
                "plan_revision": 1,
                "job_id": "job:x",
                "job_state": "needs_review",
                "run_id": "run:x",
                "run_outcome": "succeeded",
                "run_reason": "",
                "freshness": "current",
                "evidence": [],
                "coverage": [
                    {"criterion_id": "c1", "label": "Scan evidence bound",
                     "covered": True, "supported": True, "note": ""},
                    {"criterion_id": "c5", "label": "The application works",
                     "covered": False, "supported": False, "note": ""},
                ],
                "blocking": [],
                "limitations": [],
                "decision": None,
            }
        )
        from PySide6.QtWidgets import QLabel

        text = "\n".join(label.text() for label in page.findChildren(QLabel))
        self.assertIn("The application works", text)
        self.assertIn("No supported check establishes this requirement.", text)
        self.assertIn("1 of 2 criteria have evidence.", text)

    def test_decision_reaches_the_backend_and_is_rendered(self):
        self.drive_flow(self.window)
        with mock.patch.object(QInputDialog, "getText", side_effect=[("heron", True), ("seen", True)]):
            self.window.record_decision("approve")
        review = self.window._orchestration_workflow["review"]
        self.assertIsNotNone(review["decision"])
        self.assertEqual(review["decision"]["outcome"], "acknowledged")
        self.assertEqual(review["decision"]["actor"], "heron")

    def test_approval_never_manufactures_a_baseline(self):
        self.drive_flow(self.window)
        with mock.patch.object(QInputDialog, "getText", side_effect=[("heron", True), ("", True)]):
            self.window.record_decision("approve")
        resume = self.window._orchestration_workflow["resume"]
        self.assertEqual(resume["accepted_baseline"], "unknown")
        self.assertIsNone(resume["accepted_baseline_ref"])
        self.assertNotIn("adopt", resume["next_action"].lower())

    def test_the_repository_is_not_written_by_the_desktop_flow(self):
        before = self.source_fingerprint()
        self.drive_flow(self.window)
        with mock.patch.object(QInputDialog, "getText", side_effect=[("heron", True), ("", True)]):
            self.window.record_decision("approve")
        self.assertEqual(self.source_fingerprint(), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "orchestration")))

    def test_running_before_confirmation_is_refused_without_scanning(self):
        self.window.submit_goal("scan the project")
        # Confirm nothing: drive the job straight from the plan card.
        self.window.dispatch_job("scan")
        self.assertEqual(self.scans, 0)
        # The card must not claim success: the persisted state has no run.
        workflow = self.window._orchestration_workflow
        self.assertFalse((workflow or {}).get("run_id"))

    def test_a_stale_source_cannot_be_acknowledged(self):
        self.drive_flow(self.window)
        self._write("app.py", "def main():\n    return 2\n")
        self.window._refresh_orchestration()
        review = self.window._orchestration_workflow["review"]
        self.assertEqual(review["freshness"], "stale")
        self.assertTrue(review["blocking"])
        with mock.patch.object(QInputDialog, "getText", side_effect=[("heron", True), ("", True)]):
            self.window.record_decision("approve")
        after = self.window._orchestration_workflow["review"]
        self.assertIsNone(after.get("decision"))


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class RestartTests(_DesktopCase):
    def test_a_reopened_desktop_and_backend_recover_the_same_records(self):
        first = _Backend(self.store_base)
        window = self._start(first)
        flow = self.drive_flow(window)
        plan_id, run_id = flow["plan_id"], flow["run_id"]
        scans_after_run = self.scans
        self._close(window)

        # A fresh desktop and a fresh backend session over the same store.
        second = _Backend(self.store_base)
        self.window = self._start(second)
        reopened = self.window._orchestration_workflow
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened["plan_id"], plan_id)
        self.assertEqual(reopened["run_id"], run_id)
        self.assertEqual(reopened["plan_digest"], flow["plan_digest"])
        # Reopening is a read: it dispatched no scan.
        self.assertEqual(self.scans, scans_after_run)

    def test_a_reopened_desktop_renders_home_from_the_persisted_resume(self):
        first = _Backend(self.store_base)
        window = self._start(first)
        self.drive_flow(window)
        self._close(window)

        second = _Backend(self.store_base)
        self.window = self._start(second)
        page = self.window._shell.page("home")
        page.render()
        from PySide6.QtWidgets import QLabel, QPushButton

        text = "\n".join(label.text() for label in page.findChildren(QLabel))
        buttons = [button.text() for button in page.findChildren(QPushButton)]
        self.assertIn("Continue", text)
        # The recommended action comes from the persisted resume, and it is a
        # review — never an adoption, because a scan has nothing to adopt.
        self.assertIn("Review the scan evidence", buttons)
        self.assertIn("No accepted baseline is recorded", text)


if __name__ == "__main__":
    unittest.main()
