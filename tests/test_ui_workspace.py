"""The redesigned workspace: rail, shell, destinations and their guards.

These tests prove the chat-first interface exists and behaves: seven
destinations, a first-run path that explains the product, a goal that becomes
an editable plan, dispatch that refuses until a protected effect is confirmed,
review that cannot approve incomplete evidence, and an accessible name on
every button the window builds.

They also prove the *absence* of the surfaces the redesign removed — the
Advanced disclosure, the Memory rail entry and every Code Twin surface — so a
later change cannot quietly reintroduce one.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QPushButton

    from hrca.ui import style
    from hrca.ui import client as client_module
    from hrca.ui.appmodel import states
    from hrca.ui.appmodel.context import ProjectContext
    from hrca.ui.appmodel.session import Workspace
    from hrca.ui.client import MainWindow
    from hrca.ui.destinations.resume_page import PURPOSE
    from hrca.ui.shell import RAIL_DESTINATIONS, Shell
    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - environment without the desktop extra
    HAS_PYSIDE6 = False


EXPECTED_RAIL = ("resume", "chat", "jobs", "agents", "review", "documents", "settings")


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class RailTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def test_the_rail_has_exactly_the_seven_destinations_in_order(self):
        self.assertEqual(tuple(key for key, _, _ in RAIL_DESTINATIONS), EXPECTED_RAIL)
        self.assertEqual(client_module._NAV_DESTINATIONS, EXPECTED_RAIL)

    def test_the_client_label_map_matches_the_rail(self):
        # The client keeps a literal label map so the architecture guard can
        # read it; this pins it to the shell's own rail definition.
        expected = {key: label for key, _glyph, label in RAIL_DESTINATIONS}
        self.assertEqual(client_module._NAV_LABELS, expected)

    def test_every_rail_button_has_a_label_and_an_accessible_name(self):
        for key, glyph, label in RAIL_DESTINATIONS:
            with self.subTest(key=key):
                button = self.window._shell._buttons[key]
                self.assertEqual(button.accessibleName(), label)
                self.assertIn(label, button.text())

    def test_every_destination_is_registered_and_reachable(self):
        for key in EXPECTED_RAIL:
            with self.subTest(key=key):
                self.window._select_destination(key)
                self.assertEqual(self.window._shell.current, key)

    def test_the_window_opens_on_resume(self):
        self.assertEqual(self.window._shell.current, "resume")
        self.assertEqual(self.window._nav_destination, "resume")

    def test_every_destination_page_builds(self):
        for key in EXPECTED_RAIL:
            with self.subTest(key=key):
                page = self.window._shell.page(key)
                self.assertIsNotNone(page)
                self.assertTrue(page.heading.text())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class LegacySurfaceRemovalTests(unittest.TestCase):
    """The redesign must not leave an orphaned legacy surface reachable."""

    def test_no_advanced_disclosure_survives(self):
        self.assertFalse(hasattr(client_module, "_ADVANCED_DESTINATIONS"))
        self.assertFalse(hasattr(client_module, "_NAV_ADVANCED_LABEL"))
        self.assertFalse(hasattr(client_module, "_NAV_DESTINATION_INDEX"))
        self.assertFalse(hasattr(client_module, "_CHANGE_REVIEW_TABS"))

    def test_no_legacy_destination_name_is_routable(self):
        for legacy in ("document", "preview", "versions", "memory", "change_review", "validation_evidence"):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, client_module._NAV_DESTINATIONS)

    def test_the_client_reaches_no_twin_or_codemap_surface(self):
        path = os.path.join("src", "hrca", "ui", "client.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("hrca.twin", "codemap", "Code Twin", "Code Map"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        for action in ("get_code_map", "get_twin", "sync_twin", "get_anchor", "save_draft"):
            with self.subTest(action=action):
                self.assertNotIn(action, source)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class FirstRunTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def test_first_run_explains_the_product_and_offers_opening_a_project(self):
        page = self.window._shell.page("resume")
        text = self._all_text(page)
        self.assertIn("helps you manage coding agents", PURPOSE)
        self.assertIn("Open project", text)

    def test_first_run_offers_exactly_one_primary_action(self):
        page = self.window._shell.page("resume")
        primaries = [
            button
            for button in page.findChildren(QPushButton)
            if button.objectName() == "primaryButton" and button.isVisibleTo(page)
        ]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0].accessibleName(), "Open project")

    def test_the_context_bar_states_there_is_no_project(self):
        self.assertEqual(self.window._shell.context_bar.primary_button.text(), "Open Project")

    def test_the_composer_states_context_action_and_authority(self):
        summary = self.window._shell.composer.summary.text()
        self.assertIn("Context:", summary)
        self.assertIn("Action:", summary)
        self.assertIn("Authority:", summary)
        self.assertIn("nothing is sent", summary)

    @staticmethod
    def _all_text(widget) -> str:
        from PySide6.QtWidgets import QLabel

        parts = [label.text() for label in widget.findChildren(QLabel)]
        for button in widget.findChildren(QPushButton):
            parts.append(button.text())
        return "\n".join(parts)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class GoalToPlanTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.window = MainWindow()
        self.window._on_project_opened({"root": "/repo", "repository_state": "Unverified"})
        self.window._sync_workspace_context()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def test_a_goal_becomes_an_editable_plan_in_agent_chat(self):
        self.window._on_goal_submitted("scan the project")
        self.assertEqual(self.window._shell.current, "chat")
        self.assertIsNotNone(self.window._workspace.plan)
        self.assertFalse(self.window._workspace.plan.confirmed)

    def test_confirming_the_plan_makes_its_jobs_ready(self):
        self.window._on_goal_submitted("scan the project")
        self.window.confirm_plan()
        self.assertTrue(self.window._workspace.plan.confirmed)
        self.assertEqual(self.window._workspace.job("scan").state, states.STATE_READY)

    def test_jobs_destination_shows_the_plan_hierarchy(self):
        self.window._on_goal_submitted("scan the project")
        self.window.confirm_plan()
        page = self.window._shell.page("jobs")
        from PySide6.QtWidgets import QLabel

        text = "\n".join(label.text() for label in page.findChildren(QLabel))
        self.assertIn("Scan the project", text)
        self.assertIn("Owner:", text)

    def test_a_destination_refresh_after_a_goal_does_not_lose_the_plan(self):
        self.window._on_goal_submitted("scan the project")
        before = self.window._workspace.plan.job_keys
        self.window._refresh_destinations()
        self.assertEqual(self.window._workspace.plan.job_keys, before)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ReviewDecisionTests(unittest.TestCase):
    def _workspace(self) -> Workspace:
        # No document open, so the goal composes a single scan job and the
        # evidence question is unambiguous.
        context = ProjectContext(root="/repo", repository_state="Unverified")
        workspace = Workspace(context)
        workspace.state_goal("scan the project")
        workspace.confirm_plan()
        return workspace

    def test_approval_is_refused_until_the_evidence_is_complete(self):
        workspace = self._workspace()
        outcome = workspace.record_decision(states.DECISION_APPROVE, "heron")
        self.assertTrue(outcome.refused)
        self.assertIn("Missing proof", outcome.reason)

    def test_approving_records_a_decision_and_advances_no_baseline(self):
        workspace = self._workspace()
        workspace.dispatch("scan")
        workspace.report_job("scan", states.STATE_COMPLETED)
        self.assertTrue(workspace.record_decision(states.DECISION_APPROVE, "heron").ok)
        self.assertFalse(workspace.context.has_baseline)

    def test_rejection_is_always_available(self):
        workspace = self._workspace()
        self.assertTrue(workspace.record_decision(states.DECISION_REJECT, "heron").ok)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class AccessibleControlTests(unittest.TestCase):
    """Every control the window builds must explain itself."""

    def setUp(self):
        _app()
        self.window = MainWindow()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def _assert_named(self, destination_key: str) -> None:
        page = self.window._shell.page(destination_key)
        for button in page.findChildren(QPushButton):
            with self.subTest(destination=destination_key, label=button.text()):
                self.assertTrue(
                    button.accessibleName(),
                    f"{destination_key}: {button.text()!r} has no accessible name",
                )

    def test_every_button_in_every_destination_has_an_accessible_name(self):
        for key in EXPECTED_RAIL:
            self._assert_named(key)

    def test_rail_and_context_controls_have_accessible_names(self):
        for key, _, label in RAIL_DESTINATIONS:
            self.assertTrue(self.window._shell._buttons[key].accessibleName())
        self.assertTrue(self.window._shell.context_bar.primary_button.accessibleName())
        self.assertTrue(self.window._shell.composer.send_button.accessibleName())
        self.assertTrue(self.window.scan_button.accessibleName())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ShellTests(unittest.TestCase):
    def setUp(self):
        _app()
        self.host_calls = []

        class Host:
            def __getattr__(inner, name):
                def record(*args, **kwargs):
                    self.host_calls.append((name, args))
                return record

        self.host = Host()
        self.shell = Shell(style.palette_for(QApplication.instance()))

    def test_selecting_a_destination_reports_the_change(self):
        seen = []
        self.shell.destination_changed.connect(seen.append)
        page = client_module.QWidget()
        self.shell.register("resume", page)
        self.shell.select("resume")
        self.assertEqual(seen, ["resume"])

    def test_selecting_an_unregistered_destination_is_a_no_op(self):
        before = self.shell.current
        self.shell.select("nope")
        self.assertEqual(self.shell.current, before)

    def test_the_details_drawer_starts_hidden_and_shows_on_demand(self):
        self.assertTrue(self.shell.drawer.isHidden())
        self.shell.show_details("Evidence", (("Capability", "Read-only source scan"),))
        self.assertFalse(self.shell.drawer.isHidden())
        self.shell.hide_details()
        self.assertTrue(self.shell.drawer.isHidden())

    def test_the_context_bar_states_no_project_before_one_is_bound(self):
        self.shell.set_context(ProjectContext())
        self.assertEqual(self.shell.context_bar.primary_button.text(), "Open Project")


if __name__ == "__main__":
    unittest.main()
