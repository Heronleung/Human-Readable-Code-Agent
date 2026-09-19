"""Offscreen tests for the Code Twin link review workflow (M4.5/v2c).

These drive the real protocol 3.9.0 path: the surface builds a request through
``client_core``, the request goes to the local boundary over a temp store base
and a real scanned Twin, and the returned link and freshness verdict are
rendered. The desktop never imports the Memory or Twin seam, never supplies a
revision, and never opens anything the boundary did not report actionable.

Export, backup and recovery are deliberately out of scope here: they are
offline operator workflows with no protocol route into this process, and the
tests assert that no packaging control exists rather than pretending otherwise.

Every test runs with ``QT_QPA_PLATFORM=offscreen`` and is skipped without PySide6.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

    from hrca import boundary, contract, memory, memory_store, scanner, twin, twin_store
    from hrca.client import MEMORY_SCOPE_NOTE, MainWindow

    _QT_AVAILABLE = True
except ImportError:  # pragma: no cover - only without PySide6
    _QT_AVAILABLE = False

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

# The workspace the session is rooted at. The Twin hashes this string, so the
# tests never need a real directory at this path.
ROOT = "/tmp/hrca-v2c-workspace"
OTHER_ROOT = "/tmp/hrca-v2c-other-workspace"

# The exact identities the fixture record's body admits.
CLASS_ID = "artifact:class:pkg.mod.f"
FUNCTION_ID = "artifact:function:pkg.mod.f"
METHOD_ID = "artifact:method:pkg.mod.f"

TAB_INDEX = 4

EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"prompt": _SECRET}},
    {"event_type": "run_progress", "source_event_id": "e2", "payload": {},
     "paths": ["pkg/mod.py"],
     "code_entities": [{"path": "pkg/mod.py", "symbol": "pkg.mod.f"}],
     "evidence": [{"kind": "artifact", "artifact_ref": "pkg/mod.py",
                   "digest": "sha256:" + "a" * 64}]},
    {"event_type": "run_terminated", "source_event_id": "e3", "outcome": "completed",
     "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}},
]

PLAIN_EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
    {"event_type": "run_terminated", "source_event_id": "e2", "outcome": "completed",
     "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "e3", "payload": {}},
]


@unittest.skipUnless(_QT_AVAILABLE, "PySide6 is not installed")
class MemoryTwinTestCase(unittest.TestCase):
    """A really-scanned project, its authoritative Twin, and stored runs."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-v2c-")
        self.project = tempfile.mkdtemp(prefix="hrca-v2c-src-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        self.session.root = ROOT
        self.workspace_id = twin.workspace_id_for(ROOT)
        self.write("pkg/__init__.py", "")
        self.write("pkg/mod.py", "def f():\n    return 1\n")
        # A same-named function in another module: the collision exact identity
        # exists to survive.
        self.write("pkg/other.py", "def f():\n    return 2\n")
        self.store = self.publish(1)
        self.run_id = self.seed_run(EVENTS, "s-1")
        self.plain_run_id = self.seed_run(PLAIN_EVENTS, "s-2")

        self.window = MainWindow()
        self.window.show()
        self._app.processEvents()
        self.window._select_destination("memory")
        self.window._memory_tabs.setCurrentIndex(TAB_INDEX)
        self.sent = []
        self._patch_send()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self._app.processEvents()
        shutil.rmtree(self.base, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)

    # -- fixtures ----------------------------------------------------------

    def write(self, rel_path, text):
        path = os.path.join(self.project, *rel_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def scan(self):
        document = scanner.scan_directory(self.project)
        fingerprints = {}
        for file_rec in document.get("files", []):
            rel_path = file_rec.get("path")
            if not isinstance(rel_path, str) or not rel_path.endswith((".py", ".pyi")):
                continue
            try:
                with open(os.path.join(self.project, rel_path), "rb") as handle:
                    fingerprints[rel_path] = twin.fingerprint_bytes(handle.read())
            except OSError:
                fingerprints[rel_path] = None
        return document, fingerprints

    def publish(self, generation, workspace_id=None, store_override=None):
        """Write a fresh authoritative Twin store at ``generation``."""
        workspace_id = workspace_id or self.workspace_id
        if store_override is not None:
            store = store_override
        else:
            document, fingerprints = self.scan()
            store = twin.build_store(
                document, fingerprints, workspace_id, generation,
                "2026-01-01T00:00:00Z",
            )
        self.assertIsNone(twin_store.save(self.base, workspace_id, store))
        return store

    def resync(self, generation):
        """Reconcile the current source against the stored Twin incrementally."""
        document, fingerprints = self.scan()
        store, _result = twin.sync_twin(
            document, fingerprints, self.store, self.workspace_id, generation,
            "2026-01-01T00:00:00Z",
        )
        self.assertIsNone(twin_store.save(self.base, self.workspace_id, store))
        return store

    def seed_run(self, events, session_id):
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": session_id, "events": events,
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Review work"},
        })
        self.assertIsNone(error)
        run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        return run_id

    # -- harness -----------------------------------------------------------

    def _patch_send(self):
        def send(request, on_success, on_error):
            self.sent.append(request)
            envelope = boundary.handle_request(request, self.session)
            if envelope.get("ok"):
                on_success(envelope.get("result", {}))
            else:
                on_error((envelope.get("error") or {}).get("code", "internal_error"))
            return True

        patcher = mock.patch.object(self.window, "_send", side_effect=send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_send_with(self, hook):
        """Replace the transport with one whose response the test supplies.

        ``hook(request)`` returns ``(ok, payload)`` or ``None`` to fall through
        to the real boundary. This exists to exercise client-side guards that a
        well-behaved boundary cannot provoke on demand — a substituted artifact
        id, a second identity resolving, an unconditional refusal.
        """
        def send(request, on_success, on_error):
            self.sent.append(request)
            answer = hook(request)
            if answer is None:
                envelope = boundary.handle_request(request, self.session)
                if envelope.get("ok"):
                    on_success(envelope.get("result", {}))
                else:
                    on_error((envelope.get("error") or {}).get("code", "internal_error"))
                return True
            ok, payload = answer
            if ok:
                on_success(payload)
            else:
                on_error(payload)
            return True

        patcher = mock.patch.object(self.window, "_send", side_effect=send)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- actions -----------------------------------------------------------

    def review(self):
        """Bind the link and read its freshness; the patched transport is
        synchronous, so the whole chain completes inside this call."""
        self.window._bind_memory_twin_link()
        self._app.processEvents()
        return self.window._memory_twin_freshness

    def refresh(self):
        """Re-compare the held link with the Twin as it is now."""
        self.window._refresh_memory_twin_freshness()
        self._app.processEvents()
        return self.window._memory_twin_freshness

    def select_run(self, run_id):
        selector = self.window._memory_twin_run_selector
        for index in range(selector.count()):
            if selector.itemData(index) == run_id:
                selector.setCurrentIndex(index)
                return
        self.fail("no run %s in the selector" % run_id)

    def select_record(self, position):
        selector = self.window._memory_twin_record_selector
        selector.setCurrentIndex(position)

    def open_entity(self):
        self.window._open_memory_twin_entity()
        self._app.processEvents()

    def open_button(self):
        return self.window._memory_twin_open_button

    # -- rendering readers -------------------------------------------------

    def widgets(self, root=None):
        root = root if root is not None else self.window._memory_tabs.widget(TAB_INDEX)
        return [w for w in root.findChildren(QWidget)]

    def texts(self, root=None):
        collected = []
        for widget in self.widgets(root):
            for name in ("text", "toolTip", "accessibleName"):
                getter = getattr(widget, name, None)
                if callable(getter):
                    value = getter()
                    if value:
                        collected.append(str(value))
        return collected

    def outcome_texts(self):
        return [
            label.text()
            for label in self.window._memory_twin_outcome_body.findChildren(QLabel)
        ]

    def panel_texts(self):
        """Everything the Code Twin tab renders, in one list."""
        return self.texts()

    def source_text(self):
        return self.window._memory_twin_source_text.text()

    # ``format_twin_projection`` always renders this line, so its presence is
    # how a test can tell an opened projection from a bounded refusal.
    _PROJECTION_MARKER = "Sync state:"

    def assert_no_entity_opened(self):
        self.assertNotIn(self._PROJECTION_MARKER, self.source_text())

    def assert_entity_opened(self):
        self.assertIn(self._PROJECTION_MARKER, self.source_text())

    def all_rendered_text(self):
        """Every string a user could read, copy or hear on the whole window."""
        return self.texts(self.window)


class ReviewTests(MemoryTwinTestCase):
    """Every freshness state is rendered exactly as the boundary returned it."""

    def test_a_bound_link_is_current_and_reports_its_revision(self):
        verdict = self.review()
        self.assertEqual("current", verdict["freshness"])
        rendered = " | ".join(self.panel_texts())
        self.assertIn("Current", rendered)
        self.assertIn("Recorded revision", rendered)
        self.assertIn("Actionable", rendered)

    def test_every_candidate_identity_is_answered_exactly(self):
        self.review()
        outcomes = " | ".join(self.outcome_texts())
        # All three symbol kinds are offered, and the one the Twin really holds
        # is the only one that binds.
        self.assertIn("Class pkg.mod.f", outcomes)
        self.assertIn("Function pkg.mod.f", outcomes)
        self.assertIn("Method pkg.mod.f", outcomes)
        self.assertIn("Current", outcomes)

    def test_the_identity_body_comes_from_the_record_not_a_path(self):
        # The interface never asks for a path: the identities offered are built
        # from the record's own stored body.
        self.review()
        self.assertIn(FUNCTION_ID, " | ".join(self.panel_texts()))
        self.assertNotIn(self.project, " | ".join(self.all_rendered_text()))

    def test_a_source_change_is_stale_and_still_actionable(self):
        self.review()
        self.write("pkg/mod.py", "def f():\n    return 11\n")
        self.publish(2)
        verdict = self.refresh()
        self.assertEqual("stale", verdict["freshness"])
        self.assertTrue(verdict["actionable"])
        self.assertIn("Stale", " | ".join(self.panel_texts()))
        # The link still records the revision it was bound at, so the drift is
        # readable rather than erased by a fresh bind.
        self.assertEqual(1, verdict["recorded_revision"])
        self.assertEqual(2, verdict["current_revision"])

    def test_a_retained_version_is_historical_and_not_actionable(self):
        self.review()
        # Break the file: the Twin keeps the last valid symbol record and says so.
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.store = self.resync(2)
        verdict = self.refresh()
        self.assertEqual("historical", verdict["freshness"])
        self.assertFalse(verdict["actionable"])
        rendered = " | ".join(self.panel_texts())
        self.assertIn("Historical", rendered)
        self.assertIn("Actionable: No", rendered)

    def test_a_removed_entity_is_missing_and_not_actionable(self):
        self.review()
        os.remove(os.path.join(self.project, "pkg", "mod.py"))
        self.publish(2)
        verdict = self.refresh()
        self.assertEqual("missing", verdict["freshness"])
        self.assertFalse(verdict["actionable"])
        self.assertIn("Missing", " | ".join(self.panel_texts()))

    def test_a_workspace_mismatch_is_unsupported_and_not_actionable(self):
        # Bind at this workspace, then re-root the session at another one, so no
        # comparable authority exists for the held link.
        self.review()
        self.publish(1, workspace_id=twin.workspace_id_for(OTHER_ROOT))
        self.session.root = OTHER_ROOT
        verdict = self.refresh()
        self.assertEqual("unsupported", verdict["freshness"])
        self.assertFalse(verdict["actionable"])
        rendered = " | ".join(self.panel_texts())
        self.assertIn("Unsupported", rendered)
        self.assertIn("Actionable: No", rendered)
        # The limitation the boundary returned is shown, not paraphrased away.
        self.assertIn("different workspace", rendered)

    def test_a_link_cannot_be_bound_against_a_retained_entity(self):
        # The Twin retains the entity, so no *new* link may be taken against it:
        # the bind is refused outright with a bounded reason.
        self.review()
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.store = self.resync(2)
        self.review()
        rendered = " | ".join(self.outcome_texts() + self.panel_texts())
        self.assertIn("not a current projection", rendered)
        self.assertFalse(self.open_button().isEnabled())

    def test_a_link_cannot_be_bound_against_an_absent_entity(self):
        self.review()
        os.remove(os.path.join(self.project, "pkg", "mod.py"))
        self.publish(2)
        self.review()
        rendered = " | ".join(self.outcome_texts() + self.panel_texts())
        self.assertIn("does not exist in that workspace", rendered)
        self.assertFalse(self.open_button().isEnabled())

    def test_a_run_without_source_claims_reports_a_bounded_state(self):
        self.review()
        self.select_run(self.plain_run_id)
        self._app.processEvents()
        rendered = " | ".join(self.panel_texts())
        self.assertIn("no source claim", rendered)
        self.assertFalse(self.open_button().isEnabled())

    def test_a_miss_is_never_filled_by_a_similar_name(self):
        # pkg.other.f exists with the same name in another module; the identity
        # asked for is the one in pkg.mod, so only that one may bind.
        self.review()
        outcomes = " | ".join(self.outcome_texts())
        self.assertNotIn("pkg.other.f", outcomes)
        self.assertIn("Function pkg.mod.f", outcomes)

    def test_the_link_and_its_verdict_stay_visible_when_not_actionable(self):
        self.review()
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.store = self.resync(2)
        self.refresh()
        rendered = " | ".join(self.panel_texts())
        self.assertIn(FUNCTION_ID, rendered)
        self.assertIn("Historical", rendered)


class OpenTests(MemoryTwinTestCase):
    """Opening happens for exactly one actionable link, and proves identity."""

    def test_opening_is_disabled_before_any_review(self):
        button = self.open_button()
        self.assertFalse(button.isEnabled())
        self.window._open_memory_twin_entity()
        self._app.processEvents()
        self.assert_no_entity_opened()

    def test_a_current_link_opens_the_exact_artifact(self):
        self.review()
        button = self.open_button()
        self.assertTrue(button.isEnabled(), button.toolTip())
        button.click()
        self._app.processEvents()
        self.assertIn("Opened %s" % FUNCTION_ID, self.window._memory_twin_open_status.text())
        self.assert_entity_opened()

    def test_a_stale_link_still_opens_the_exact_artifact(self):
        self.review()
        self.write("pkg/mod.py", "def f():\n    return 11\n")
        self.publish(2)
        self.refresh()
        self.assertEqual("stale", self.window._memory_twin_freshness["freshness"])
        self.assertTrue(self.open_button().isEnabled())
        self.open_entity()
        self.assertIn("Opened", self.window._memory_twin_open_status.text())

    def test_a_historical_link_cannot_be_opened(self):
        self.review()
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.store = self.resync(2)
        self.refresh()
        self.assertFalse(self.open_button().isEnabled())
        self.open_entity()
        self.assert_no_entity_opened()

    def test_a_missing_link_cannot_be_opened(self):
        self.review()
        os.remove(os.path.join(self.project, "pkg", "mod.py"))
        self.publish(2)
        self.refresh()
        self.assertFalse(self.open_button().isEnabled())
        self.open_entity()
        self.assert_no_entity_opened()

    def test_an_unsupported_link_cannot_be_opened(self):
        self.review()
        self.publish(1, workspace_id=twin.workspace_id_for(OTHER_ROOT))
        self.session.root = OTHER_ROOT
        self.refresh()
        self.assertFalse(self.open_button().isEnabled())
        self.open_entity()
        self.assert_no_entity_opened()

    def test_an_id_mismatch_is_refused_without_opening(self):
        # The selector resolves, but to a different artifact than the link names:
        # a same-named entity in another module. Nothing may be opened.
        def hook(request):
            if request.get("action") == contract.ACTION_GET_TWIN:
                return True, {
                    "projection": {"path": "pkg/other.py", "locator": "pkg.other.f"},
                    "artifact": {"id": "artifact:function:pkg.other.f",
                                 "sync_state": "synchronized"},
                    "behavior_nodes": [],
                    "correspondences": [],
                }
            return None

        self.review()
        self.assertTrue(self.open_button().isEnabled())
        self._patch_send_with(hook)
        self.open_entity()
        self.assert_no_entity_opened()
        self.assertIn(
            "not the linked entity", self.window._memory_twin_open_status.text()
        )

    def test_an_absent_artifact_is_refused_without_opening(self):
        def hook(request):
            if request.get("action") == contract.ACTION_GET_TWIN:
                return True, {"projection": None, "artifact": None,
                              "behavior_nodes": [], "correspondences": []}
            return None

        self.review()
        self._patch_send_with(hook)
        self.open_entity()
        self.assert_no_entity_opened()
        self.assertIn("no source artifact", self.window._memory_twin_open_status.text())

    def test_an_ambiguous_resolution_cannot_be_opened(self):
        # Two exact identities resolving for one claim is an ambiguity, not a
        # choice: the surface reports it and opens neither.
        def hook(request):
            action = request.get("action")
            if action == contract.ACTION_MEMORY_CODE_LINK:
                return True, {
                    "link_schema_version": "1.0.0",
                    "workspace_id": self.workspace_id,
                    "entity_id": request.get("entity_id"),
                    "entity_kind": request.get("entity_kind"),
                    "memory_run_id": self.run_id,
                    "memory_record_id": request.get("record_id"),
                    "recorded_revision": 1,
                }
            if action == contract.ACTION_MEMORY_CODE_FRESHNESS:
                return True, {
                    "entity_id": request.get("entity_id"),
                    "entity_kind": request.get("entity_kind"),
                    "workspace_id": self.workspace_id,
                    "memory_run_id": self.run_id,
                    "memory_record_id": request.get("memory_record_id"),
                    "recorded_revision": 1,
                    "current_revision": 1,
                    "freshness": "current",
                    "reason": None,
                    "actionable": True,
                }
            return None

        self._patch_send_with(hook)
        self.review()
        self.assertEqual(3, self.window._memory_twin_resolved_count)
        self.assertFalse(self.open_button().isEnabled())
        self.assertIn("More than one exact identity", self.window._memory_twin_open_status.text())
        # Even if the action were reached another way, it still opens nothing.
        self.open_entity()
        self.assert_no_entity_opened()
        self.assertIn(
            "did not resolve to exactly one identity",
            self.window._memory_twin_open_status.text(),
        )

    def test_a_second_open_cannot_reach_another_entity(self):
        self.review()
        self.open_entity()
        opened = self.source_text()
        self.assert_entity_opened()
        # Re-opening the same link re-proves the same identity.
        self.open_entity()
        self.assertEqual(opened, self.source_text())


class InvalidationTests(MemoryTwinTestCase):
    """A superseded read can never open a stale or unrelated entity."""

    def test_changing_the_record_clears_the_review(self):
        self.review()
        self.assertTrue(self.open_button().isEnabled())
        self.select_record(0)
        self.window._on_memory_twin_record_changed()
        self._app.processEvents()
        self.assertFalse(self.open_button().isEnabled())
        self.assertIsNone(self.window._memory_twin_link)
        self.assert_no_entity_opened()

    def test_changing_the_run_clears_the_review_and_rereads_records(self):
        self.review()
        self.assertTrue(self.open_button().isEnabled())
        # The other run records no source claim, so the review is replaced by a
        # bounded state rather than left showing the previous run's link.
        self.select_run(self.plain_run_id)
        self._app.processEvents()
        self.assertFalse(self.open_button().isEnabled())
        self.assertIsNone(self.window._memory_twin_link)
        self.assertIn("no source claim", " | ".join(self.panel_texts()))
        self.open_entity()
        self.assert_no_entity_opened()

    def test_a_late_link_response_is_discarded(self):
        self.review()
        settled = self.window._memory_twin_link
        stale = self.window._memory_twin_generation - 1
        self.window._on_memory_twin_link(
            stale,
            {"label": "Function pkg.mod.f"},
            {"entity_id": "artifact:function:pkg.other.f", "entity_kind": "function",
             "recorded_revision": 9, "memory_run_id": self.run_id,
             "memory_record_id": "record:other"},
        )
        self._app.processEvents()
        self.assertEqual(settled, self.window._memory_twin_link)
        self.assertNotEqual(
            "artifact:function:pkg.other.f",
            (self.window._memory_twin_link or {}).get("entity_id"),
        )

    def test_a_late_freshness_response_is_discarded(self):
        self.review()
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.store = self.resync(2)
        self.refresh()
        frozen = self.window._memory_twin_freshness["freshness"]
        stale = self.window._memory_twin_generation - 1
        self.window._on_memory_twin_freshness(
            stale,
            {"label": "Function pkg.mod.f"},
            {"entity_id": FUNCTION_ID, "entity_kind": "function",
             "workspace_id": self.workspace_id, "memory_run_id": self.run_id,
             "memory_record_id": "record:1", "recorded_revision": 1,
             "current_revision": 2, "freshness": "stale", "reason": None,
             "actionable": True},
        )
        self._app.processEvents()
        self.assertEqual(frozen, self.window._memory_twin_freshness["freshness"])
        self.assertFalse(self.open_button().isEnabled())

    def test_a_late_open_response_is_discarded(self):
        self.review()
        self.open_entity()
        opened = self.source_text()
        stale = self.window._memory_twin_generation - 1
        self.window._on_memory_twin_opened(
            stale,
            {"projection": {"path": "pkg/other.py"},
             "artifact": {"id": "artifact:function:pkg.other.f"},
             "behavior_nodes": [], "correspondences": []},
        )
        self._app.processEvents()
        self.assertEqual(opened, self.source_text())

    def test_an_open_after_the_selection_moved_refuses(self):
        self.review()
        # Move the selection without letting the change handler run, so the
        # stored context and the live selection disagree.
        self.window._memory_twin_record_selector.blockSignals(True)
        self.select_record(0)
        self.window._memory_twin_record_selector.blockSignals(False)
        self.window._memory_twin_context = ("run:other", "record:other")
        self.open_entity()
        self.assert_no_entity_opened()
        self.assertIn("selection changed", self.window._memory_twin_open_status.text())

    def test_a_failed_read_clears_rather_than_keeps_the_previous_link(self):
        self.review()
        self.assertTrue(self.open_button().isEnabled())
        self.window._on_memory_twin_failed(
            self.window._memory_twin_generation, "twin_not_synchronized"
        )
        self._app.processEvents()
        self.assertFalse(self.open_button().isEnabled())
        self.assertIsNone(self.window._memory_twin_link)

    def test_refreshing_without_a_link_is_bounded(self):
        self.window._refresh_memory_twin_freshness()
        self._app.processEvents()
        self.assertIn("bind a link first", self.window._memory_twin_open_status.text())
        self.assert_no_entity_opened()

    def test_a_failed_refresh_keeps_the_link(self):
        self.review()
        held = self.window._memory_twin_link
        self.window._on_memory_twin_refresh_failed(
            self.window._memory_twin_generation, "twin_not_synchronized"
        )
        self._app.processEvents()
        self.assertEqual(held, self.window._memory_twin_link)
        self.assertIsNone(self.window._memory_twin_freshness)
        self.assertFalse(self.open_button().isEnabled())
        self.assertIn("could not be read", self.window._memory_twin_open_status.text())

    def test_rebinding_replaces_the_held_link(self):
        self.review()
        first = self.window._memory_twin_link
        self.write("pkg/mod.py", "def f():\n    return 11\n")
        self.publish(2)
        # A bind taken now agrees with itself, which is exactly why the surface
        # keeps the two steps apart.
        second = self.review()
        self.assertEqual("current", second["freshness"])
        self.assertEqual(2, second["recorded_revision"])
        self.assertNotEqual(first, self.window._memory_twin_link)


class AccessibilityTests(MemoryTwinTestCase):
    """Every control is named, reachable and carries its state in words."""

    def test_every_twin_control_is_named_and_focusable(self):
        controls = [
            widget for widget in self.widgets() if isinstance(widget, QPushButton)
        ]
        self.assertTrue(controls)
        for control in controls:
            with self.subTest(control=control.objectName()):
                self.assertTrue(control.accessibleName())
                self.assertTrue(control.toolTip())
                self.assertIn(
                    control.focusPolicy(),
                    (Qt.StrongFocus, Qt.WheelFocus, Qt.TabFocus),
                )

    def test_every_selector_is_named_and_described(self):
        for selector in (
            self.window._memory_twin_run_selector,
            self.window._memory_twin_record_selector,
        ):
            with self.subTest(selector=selector.objectName()):
                self.assertTrue(selector.accessibleName())
                self.assertTrue(selector.toolTip())

    def test_the_open_button_can_take_focus_and_reports_its_state(self):
        button = self.open_button()
        self.assertEqual(Qt.StrongFocus, button.focusPolicy())
        button.setFocus()
        self._app.processEvents()
        self.assertIn("disabled", button.accessibleName().lower())
        self.review()
        self.assertIn("Open the source entity", button.accessibleName())
        self.assertTrue(button.isEnabled())

    def test_the_state_is_never_colour_alone(self):
        self.review()
        rendered = " | ".join(self.panel_texts())
        self.assertIn("State", rendered)
        self.assertIn("Actionable", rendered)
        self.assertIn("Current", rendered)

    def test_the_source_text_is_keyboard_selectable(self):
        flags = self.window._memory_twin_source_text.textInteractionFlags()
        self.assertTrue(flags & Qt.TextSelectableByKeyboard)
        self.assertTrue(flags & Qt.TextSelectableByMouse)


class PrivacyTests(MemoryTwinTestCase):
    """No secret, path, payload or digest reaches any rendered surface."""

    def _forbidden(self):
        return (_SECRET, _PERSONAL, ".ssh", "sha256:", '"payload"',
                "content_fingerprint", "base_content_fingerprint")

    def test_nothing_forbidden_is_rendered_after_a_review(self):
        self.review()
        self.open_entity()
        rendered = " | ".join(self.all_rendered_text())
        for needle in self._forbidden():
            with self.subTest(needle=needle):
                self.assertNotIn(needle, rendered)
        self.assertNotIn(self.project, rendered)

    def test_nothing_forbidden_is_rendered_in_the_source_panel(self):
        self.review()
        self.open_entity()
        text = self.source_text()
        self.assertTrue(text)
        for needle in self._forbidden():
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_a_refusal_carries_no_identifier_or_path(self):
        def hook(request):
            if request.get("action") == contract.ACTION_GET_TWIN:
                return False, "twin_not_found"
            return None

        self.review()
        self._patch_send_with(hook)
        self.open_entity()
        text = self.source_text() + " " + self.window._memory_twin_open_status.text()
        for needle in self._forbidden():
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)
        self.assertNotIn(self.project, text)

    def test_an_error_message_carries_no_identifier_or_path(self):
        self.window._on_memory_twin_failed(
            self.window._memory_twin_generation, "memory_run_not_found"
        )
        self._app.processEvents()
        rendered = " | ".join(self.all_rendered_text())
        for needle in self._forbidden():
            with self.subTest(needle=needle):
                self.assertNotIn(needle, rendered)

    def test_the_revision_shown_is_a_number_not_a_digest(self):
        self.review()
        rendered = " | ".join(self.panel_texts())
        self.assertIn("Recorded revision", rendered)
        self.assertNotIn("sha256:", rendered)


class ScopeTests(MemoryTwinTestCase):
    """The retained packaging scope is stated, not implied away."""

    def test_the_scope_note_states_packaging_is_offline(self):
        labels = [
            label.text()
            for label in self.widgets(self.window._memory_tabs.parent())
            if isinstance(label, QLabel)
        ]
        self.assertIn(MEMORY_SCOPE_NOTE, labels)
        self.assertIn("offline", MEMORY_SCOPE_NOTE)
        self.assertIn("never packages", MEMORY_SCOPE_NOTE)

    def test_no_packaging_control_exists_anywhere(self):
        rendered = " | ".join(self.all_rendered_text()).lower()
        for word in ("export", "backup", "recover", "restore", "archive",
                     "package this", "zip"):
            with self.subTest(word=word):
                # The scope note legitimately names the words to disclaim them,
                # so the check is for a control, not for the vocabulary.
                for control in self.widgets(self.window._memory_tabs.widget(TAB_INDEX)):
                    if isinstance(control, QPushButton):
                        self.assertNotIn(word, control.text().lower())
        self.assertEqual(5, self.window._memory_tabs.count())
        self.assertNotIn("Packages", [
            self.window._memory_tabs.tabText(index)
            for index in range(self.window._memory_tabs.count())
        ])

    def test_the_destination_has_the_expected_pages(self):
        names = [
            self.window._memory_tabs.tabText(index)
            for index in range(self.window._memory_tabs.count())
        ]
        self.assertEqual(
            ["Documents", "Search", "Resume", "Corrections", "Code Twin"], names
        )


if __name__ == "__main__":
    unittest.main()
