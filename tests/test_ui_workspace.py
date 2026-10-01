"""The redesigned workspace: progressive disclosure, grouping and guards.

These tests prove the Form 2R presentation rules: first use offers one obvious
path, the rail carries four primary groups plus anchored Settings rather than
seven equal choices, Work reveals its contextual views only when they have
something to say, only Agent Chat carries a composer, and no region ever
presents the same action twice.

They also prove the *absence* of the surfaces the redesign removed — the
Advanced disclosure, the Memory rail entry and every Code Twin surface — and
that a blocking risk, a failed job or an unverified claim is never hidden by
the simplification.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QPushButton

    from hrca.ui import client as client_module
    from hrca.ui.appmodel import states
    from hrca.ui.appmodel.context import ProjectContext
    from hrca.ui.appmodel.session import Workspace
    from hrca.ui.client import MainWindow
    from hrca.ui.destinations.home_page import PURPOSE
    from hrca.ui.shell import (
        PROJECT_DEPENDENT,
        RAIL_DESTINATIONS,
        RAIL_PRIMARY,
        RAIL_SETTINGS,
        WORK_VIEWS,
    )
    HAS_PYSIDE6 = True
except ImportError:  # pragma: no cover - environment without the desktop extra
    HAS_PYSIDE6 = False


EXPECTED_PRIMARY = ("home", "chat", "work", "documents")
EXPECTED_RAIL = EXPECTED_PRIMARY + ("settings",)
WORK_VIEW_KEYS = ("jobs", "agents", "review")


def _document_state(document_id="doc:d1", revision_id="rev:1", name="requirements.md"):
    """A bounded ``open_document`` result the window can apply."""
    return {
        "document": {
            "document_id": document_id,
            "name": name,
            "kind": "md",
            "head_revision_number": 1,
            "revision_count": 1,
        },
        "head_revision": {
            "revision_id": revision_id,
            "revision_number": 1,
            "content": "Members receive a 10% discount on quotations.",
            "content_fingerprint": "f" * 64,
        },
        "candidate": None,
        "accepted": None,
        "current_accepted_version_id": None,
        "versions": [],
    }


def _app():
    app = QApplication.instance()
    return app if app is not None else QApplication([])


class _WindowCase(unittest.TestCase):
    """A MainWindow per test, with a bound project when the test needs one."""

    with_project = False

    def setUp(self):
        _app()
        self.window = MainWindow()
        if self.with_project:
            self.window._on_project_opened(
                {"root": "/repo/Human-Readable-Code-Agent", "repository_state": "Unverified"}
            )
            self.window._refresh_destinations()

    def tearDown(self):
        self.window._supervisor.terminate()
        self.window._credential_supervisor.terminate()
        self.window.close()
        self.window.deleteLater()

    def visible_buttons(self, widget) -> list:
        """Return the buttons a user could actually see and press."""
        return [
            button
            for button in widget.findChildren(QPushButton)
            if not button.isHidden() and button.isEnabled()
        ]


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class RailTests(_WindowCase):
    def test_the_rail_has_four_primary_groups_plus_anchored_settings(self):
        self.assertEqual(tuple(key for key, _, _ in RAIL_PRIMARY), EXPECTED_PRIMARY)
        self.assertEqual(RAIL_SETTINGS[0], "settings")
        self.assertEqual(tuple(key for key, _, _ in RAIL_DESTINATIONS), EXPECTED_RAIL)
        self.assertEqual(client_module._NAV_DESTINATIONS, EXPECTED_RAIL)

    def test_the_client_label_map_matches_the_rail(self):
        expected = {key: label for key, _glyph, label in RAIL_DESTINATIONS}
        self.assertEqual(client_module._NAV_LABELS, expected)

    def test_jobs_agents_and_review_are_not_rail_entries(self):
        # They are Work's contextual views, so they must not be top-level.
        for key in WORK_VIEW_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, EXPECTED_RAIL)
                self.assertNotIn(key, self.window._shell._buttons)

    def test_every_rail_button_has_a_label_and_an_accessible_name(self):
        for key, glyph, label in RAIL_DESTINATIONS:
            with self.subTest(key=key):
                button = self.window._shell._buttons[key]
                self.assertEqual(button.accessibleName(), label)
                self.assertIn(label, button.text())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class FirstUseTests(_WindowCase):
    """With no project, one obvious path and no inactive orchestration surface."""

    def test_only_home_and_settings_are_offered(self):
        for key in PROJECT_DEPENDENT:
            with self.subTest(key=key):
                self.assertTrue(self.window._shell._buttons[key].isHidden())
        self.assertFalse(self.window._shell._buttons["home"].isHidden())
        self.assertFalse(self.window._shell._buttons["settings"].isHidden())

    def test_no_composer_is_reachable_before_a_project_exists(self):
        self.assertTrue(self.window._chat_destination.composer.isHidden())

    def test_the_context_bar_offers_no_action_or_scan(self):
        bar = self.window._shell.context_bar
        self.assertTrue(bar.primary_button.isHidden())
        self.assertTrue(bar.authority_chip.isHidden())
        self.assertTrue(self.window.scan_button.isHidden())
        self.assertTrue(self.window._provider_status_label.isHidden())

    def test_the_status_footer_is_hidden_at_idle(self):
        self.assertTrue(self.window._shell._footer.isHidden())

    def test_home_offers_exactly_one_primary_action(self):
        page = self.window._shell.page("home")
        primaries = [
            button
            for button in self.visible_buttons(page)
            if button.objectName() == "primaryButton"
        ]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0].accessibleName(), "Open project")

    def test_home_states_the_products_purpose(self):
        page = self.window._shell.page("home")
        from PySide6.QtWidgets import QLabel

        text = "\n".join(
            label.text() for label in page.findChildren(QLabel) if not label.isHidden()
        )
        self.assertIn("manage coding agents", text)
        self.assertIn("manage coding agents", PURPOSE)

    def test_no_new_task_shortcut_before_a_project_exists(self):
        for key in ("home", "documents", "settings"):
            page = self.window._shell.page(key)
            button = getattr(page, "new_task_button", None)
            with self.subTest(key=key):
                self.assertIsNotNone(button)
                self.assertTrue(button.isHidden())

    def test_no_recent_projects_section_without_history(self):
        self.assertEqual(self.window._workspace.recent_projects, ())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class StartedTests(_WindowCase):
    with_project = True

    def test_all_five_rail_entries_are_offered(self):
        for key in EXPECTED_RAIL:
            with self.subTest(key=key):
                self.assertFalse(self.window._shell._buttons[key].isHidden())

    def test_the_context_bar_offers_its_action_and_scan(self):
        bar = self.window._shell.context_bar
        self.assertFalse(bar.primary_button.isHidden())
        self.assertFalse(self.window.scan_button.isHidden())
        self.assertFalse(bar.authority_chip.isHidden())

    def test_a_project_is_remembered_for_this_session(self):
        self.assertEqual(
            self.window._workspace.recent_projects, ("/repo/Human-Readable-Code-Agent",)
        )

    def test_every_destination_is_reachable_in_at_most_two_interactions(self):
        # One interaction: the four primary groups and anchored Settings.
        for key in EXPECTED_RAIL:
            with self.subTest(key=key):
                self.window._select_destination(key)
                self.assertEqual(self.window._shell.current, key)
        # Two interactions: Work, then one of its contextual views.
        for key in WORK_VIEW_KEYS:
            with self.subTest(key=key):
                self.window._select_destination(key)
                self.assertEqual(self.window._shell.current, "work")
        # Two interactions: Home, then Project history.
        self.window._select_destination("history")
        self.assertEqual(self.window._shell.current, "history")

    def test_only_agent_chat_carries_a_composer(self):
        chat = self.window._chat_destination
        self.assertIsNotNone(chat.composer)
        self.assertTrue(hasattr(chat, "composer"))
        for key in ("home", "work", "documents", "settings"):
            with self.subTest(key=key):
                page = self.window._shell.page(key)
                from hrca.ui.components import Composer

                self.assertEqual(page.findChildren(Composer), [])

    def test_other_pages_offer_one_new_task_shortcut(self):
        for key in ("home", "work", "documents", "settings"):
            page = self.window._shell.page(key)
            button = getattr(page, "new_task_button", None)
            with self.subTest(key=key):
                self.assertIsNotNone(button)
                self.assertFalse(button.isHidden())
                self.assertEqual(button.accessibleName(), "Start a new task in Agent Chat")


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class WorkDisclosureTests(_WindowCase):
    with_project = True

    def _work(self):
        return self.window._shell.page("work")

    def test_empty_work_has_one_start_action(self):
        work = self._work()
        work.refresh()
        primaries = [
            button
            for button in self.visible_buttons(work)
            if button.objectName() == "primaryButton"
        ]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0].accessibleName(), "Start in Agent Chat")

    def test_agents_is_hidden_until_a_plan_assigns_a_role(self):
        work = self._work()
        work.refresh()
        self.assertTrue(work._buttons["agents"].isHidden())

    def test_review_is_hidden_until_there_is_evidence(self):
        work = self._work()
        work.refresh()
        self.assertTrue(work._buttons["review"].isHidden())

    def test_a_plan_reveals_jobs_and_agents(self):
        self.window.submit_goal("scan the project")
        self.window.confirm_plan()
        work = self._work()
        work.refresh()
        self.assertFalse(work._buttons["jobs"].isHidden())
        self.assertFalse(work._buttons["agents"].isHidden())
        self.assertTrue(work._buttons["review"].isHidden())

    def test_completed_work_reveals_review(self):
        self.window.submit_goal("scan the project")
        self.window.confirm_plan()
        self.window._workspace.dispatch("scan")
        self.window._workspace.report_job("scan", states.STATE_COMPLETED)
        self.window._refresh_destinations()
        work = self._work()
        work.refresh()
        self.assertFalse(work._buttons["review"].isHidden())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class HomeDisclosureTests(_WindowCase):
    with_project = True

    def test_home_shows_continue_and_a_project_history_entry(self):
        from PySide6.QtWidgets import QLabel

        page = self.window._shell.page("home")
        page.refresh()
        text = "\n".join(label.text() for label in page.findChildren(QLabel))
        self.assertIn("Continue", text)
        self.assertIn("Project history", text)

    def test_home_does_not_show_the_raw_memory_reader(self):
        page = self.window._shell.page("home")
        page.refresh()
        self.assertFalse(page.isAncestorOf(self.window._memory_run_selector))

    def test_project_history_holds_the_memory_reader(self):
        page = self.window._shell.page("history")
        self.assertTrue(page.isAncestorOf(self.window._memory_run_selector))

    def _home_text(self) -> str:
        from PySide6.QtWidgets import QLabel

        page = self.window._shell.page("home")
        page.refresh()
        return "\n".join(label.text() for label in page.findChildren(QLabel))

    def test_needs_attention_is_shown_for_a_blocked_job(self):
        self.window.submit_goal("scan the project")
        self.window.confirm_plan()
        self.window._workspace.dispatch("scan")
        self.window._workspace.report_job(
            "scan", states.STATE_BLOCKED, blocker="The root is not readable."
        )
        self.window._refresh_destinations()
        text = self._home_text()
        self.assertIn("Needs attention", text)
        self.assertIn("The root is not readable.", text)

    def test_needs_attention_is_shown_for_a_pending_approval(self):
        # A pending decision is not a "bad" state, but it is something only a
        # human can clear, so it must never be hidden by the disclosure.
        self.window.submit_goal("scan the project")
        self.window.confirm_plan()
        self.window._workspace.dispatch("scan")
        self.window._workspace.report_job("scan", states.STATE_COMPLETED)
        self.window._refresh_destinations()
        text = self._home_text()
        self.assertIn("Needs attention", text)
        self.assertIn("decision is waiting", text)

    def test_needs_attention_is_shown_for_a_failed_job(self):
        self.window.submit_goal("scan the project")
        self.window.confirm_plan()
        self.window._workspace.dispatch("scan")
        self.window._workspace.report_job("scan", states.STATE_FAILED)
        self.window._refresh_destinations()
        text = self._home_text()
        self.assertIn("Needs attention", text)

    def test_needs_attention_is_shown_for_an_unverified_claim(self):
        self.window.submit_goal("scan the project")
        self.window.confirm_plan()
        self.window._workspace.dispatch("scan")
        self.window._workspace.report_job("scan", states.STATE_UNKNOWN)
        self.window._refresh_destinations()
        text = self._home_text()
        self.assertIn("Needs attention", text)

    def test_home_hides_the_attention_section_when_nothing_is_outstanding(self):
        # Nothing planned, nothing blocked: no safety banner to show.
        self.assertNotIn("Needs attention", self._home_text())

    def test_home_with_no_plan_has_exactly_one_primary_action(self):
        page = self.window._shell.page("home")
        page.refresh()
        primaries = [
            button
            for button in self.visible_buttons(page)
            if button.objectName() == "primaryButton"
        ]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0].accessibleName(), "State a goal in Agent Chat")

    def test_home_continue_card_is_one_compact_group(self):
        from hrca.ui.components import Card

        page = self.window._shell.page("home")
        page.refresh()
        self.window.resize(1024, 640)
        self.window.show()
        QApplication.processEvents()
        QApplication.processEvents()
        cards = [c for c in page.findChildren(Card) if not c.isHidden()]
        self.assertTrue(cards)
        for card in cards:
            with self.subTest(card=card._heading.text()):
                # Natural height, and the action not flung away from the title.
                self.assertLessEqual(card.height(), card.sizeHint().height() + 40)
                action = card.body.itemAt(0).widget()
                gap = action.mapTo(card, action.rect().topLeft()).y() - card._heading.mapTo(
                    card, card._heading.rect().topLeft()
                ).y()
                self.assertLess(gap, 120)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class DocumentsDisclosureTests(_WindowCase):
    with_project = True

    def test_the_empty_document_state_has_one_primary_action(self):
        page = self.window._shell.page("documents")
        empty = self.window._document_empty_state
        primaries = [
            button
            for button in self.visible_buttons(empty)
            if button.objectName() == "primaryButton"
        ]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(primaries[0].accessibleName(), "Create document")

    def test_the_empty_document_state_does_not_duplicate_open_project(self):
        empty = self.window._document_empty_state
        labels = [button.text() for button in empty.findChildren(QPushButton)]
        self.assertNotIn("Open project", labels)

    def test_the_versions_empty_state_offers_no_second_action(self):
        self.window._apply_library({"folders": [], "documents": []})
        self.window._populate_versions_list()
        labels = [
            button.text()
            for button in self.window._versions_page.findChildren(QPushButton)
        ]
        self.assertNotIn("Go to Document", labels)
        self.assertNotIn("Build preview", labels)

    def _show_empty_state(self) -> None:
        self.window._clear_open_document()
        self.window._update_document_actions()
        self.window._select_destination("documents")
        # The window must be laid out for geometry to mean anything; without
        # show() the widgets still carry the geometry of the previous state.
        self.window.resize(1024, 640)
        self.window.show()
        QApplication.processEvents()
        QApplication.processEvents()

    def test_the_library_create_row_is_hidden_while_the_empty_state_shows(self):
        self._show_empty_state()
        self.assertFalse(self.window._document_empty_state.isHidden())
        self.assertTrue(self.window._library_create_row.isHidden())

    def test_the_library_create_row_returns_with_a_document(self):
        self.window._apply_document_state(_document_state())
        self.window._select_destination("documents")
        QApplication.processEvents()
        self.assertFalse(self.window._library_create_row.isHidden())

    def test_exactly_one_create_document_action_while_empty(self):
        self._show_empty_state()
        page = self.window._shell.page("documents")
        names = [
            button.accessibleName()
            for button in self.visible_buttons(page)
            if "create" in button.accessibleName().lower()
        ]
        self.assertEqual(names, ["Create document"])

    def test_the_document_empty_state_is_natural_height(self):
        self._show_empty_state()
        empty = self.window._document_empty_state
        self.assertLessEqual(empty.height(), empty.sizeHint().height() + 4)


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class LegacySurfaceRemovalTests(unittest.TestCase):
    """The redesign must not leave an orphaned legacy surface reachable."""

    def test_no_advanced_disclosure_survives(self):
        self.assertFalse(hasattr(client_module, "_ADVANCED_DESTINATIONS"))
        self.assertFalse(hasattr(client_module, "_NAV_ADVANCED_LABEL"))
        self.assertFalse(hasattr(client_module, "_NAV_DESTINATION_INDEX"))
        self.assertFalse(hasattr(client_module, "_CHANGE_REVIEW_TABS"))

    def test_no_legacy_destination_name_is_routable(self):
        for legacy in ("document", "preview", "versions", "memory", "change_review",
                       "validation_evidence", "resume"):
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
class DuplicateActionTests(_WindowCase):
    """One region, one dominant primary action; no action in two places."""

    with_project = True

    def test_the_context_bar_and_home_do_not_both_offer_open_project(self):
        bar = self.window._shell.context_bar
        home = self.window._shell.page("home")
        home.refresh()
        self.assertNotEqual(
            bar.primary_button.accessibleName(), "Open project"
        )
        labels = [
            button.accessibleName()
            for button in home.findChildren(QPushButton)
            if not button.isHidden()
        ]
        self.assertNotIn("Open project", labels)

    def test_no_destination_shows_the_same_accessible_name_twice(self):
        for key in EXPECTED_RAIL:
            page = self.window._shell.page(key)
            names = [button.accessibleName() for button in self.visible_buttons(page)]
            with self.subTest(key=key):
                self.assertEqual(len(names), len(set(names)))


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class AccessibleControlTests(_WindowCase):
    with_project = True

    def test_every_button_in_every_destination_has_an_accessible_name(self):
        for key in EXPECTED_RAIL + ("history",):
            page = self.window._shell.page(key)
            for button in page.findChildren(QPushButton):
                with self.subTest(destination=key, label=button.text()):
                    self.assertTrue(
                        button.accessibleName(),
                        f"{key}: {button.text()!r} has no accessible name",
                    )

    def test_rail_and_context_controls_have_accessible_names(self):
        for key, _, label in RAIL_DESTINATIONS:
            self.assertTrue(self.window._shell._buttons[key].accessibleName())
        bar = self.window._shell.context_bar
        self.assertTrue(bar.primary_button.accessibleName())
        self.assertTrue(bar.activity_button.accessibleName())
        self.assertTrue(self.window._chat_destination.composer.send_button.accessibleName())
        self.assertTrue(self.window.scan_button.accessibleName())


@unittest.skipUnless(HAS_PYSIDE6, "PySide6 is not installed")
class ShellTests(unittest.TestCase):
    def setUp(self):
        _app()
        from hrca.ui.shell import Shell

        self.shell = Shell(__import__("hrca.ui.style", fromlist=["style"]).palette_for(_app()))

    def test_selecting_a_destination_reports_the_change(self):
        from PySide6.QtWidgets import QWidget

        seen = []
        self.shell.destination_changed.connect(seen.append)
        self.shell.register("home", QWidget())
        self.shell.select("home")
        self.assertEqual(seen, ["home"])

    def test_selecting_an_unregistered_destination_is_a_no_op(self):
        before = self.shell.current
        self.shell.select("nope")
        self.assertEqual(self.shell.current, before)

    def test_set_started_hides_the_project_dependent_entries(self):
        from PySide6.QtWidgets import QWidget

        for key, _, _ in RAIL_DESTINATIONS:
            self.shell.register(key, QWidget())
        self.shell.set_started(True)
        for key in PROJECT_DEPENDENT:
            self.assertFalse(self.shell._buttons[key].isHidden())
        self.shell.set_started(False)
        for key in PROJECT_DEPENDENT:
            self.assertTrue(self.shell._buttons[key].isHidden())
        self.assertFalse(self.shell._buttons["home"].isHidden())
        self.assertFalse(self.shell._buttons["settings"].isHidden())

    def test_the_details_drawer_starts_hidden_and_shows_on_demand(self):
        self.assertTrue(self.shell.drawer.isHidden())
        self.shell.show_details("Evidence", (("Capability", "Read-only source scan"),))
        self.assertFalse(self.shell.drawer.isHidden())
        self.shell.hide_details()
        self.assertTrue(self.shell.drawer.isHidden())

    def test_the_context_bar_states_no_project_before_one_is_bound(self):
        self.shell.set_context(ProjectContext())
        self.assertIn("No project", self.shell.context_bar._primary.text())

    def test_the_footer_stays_hidden_until_it_has_news(self):
        from PySide6.QtWidgets import QWidget

        footer = QWidget()
        self.shell.add_footer(footer)
        self.assertTrue(footer.isHidden())
        self.shell.set_footer(True)
        self.assertFalse(footer.isHidden())


if __name__ == "__main__":
    unittest.main()
