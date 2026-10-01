"""Deterministic viewport evidence for the chat-first workspace (Form 2R).

Renders the desktop offscreen from fixed, hand-built state and writes one PNG
per acceptance state per viewport. Nothing here touches the network, a
provider, a credential or a store: the window is built, fed recorded values
through its own host methods, laid out and grabbed.

Run it from the repository root:

    uv run python evidence/ui-transition-2/capture.py

The PNGs it writes are regenerable for a given Qt build, so this script — not
the images — is the durable evidence.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join("src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from hrca.ui.appmodel import authority, states  # noqa: E402
from hrca.ui.client import MainWindow  # noqa: E402

VIEWPORTS = ((1024, 640), (1920, 1080))
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = "/repo/Human-Readable-Code-Agent"
DESTINATIONS = ("home", "chat", "work", "documents", "settings", "history")
WORK_VIEWS = ("jobs", "agents", "review")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _open_project(window: MainWindow) -> None:
    window._on_project_opened({"root": ROOT, "repository_state": "Unverified"})


def _propose(window: MainWindow) -> None:
    window.submit_goal("interpret the discount rule and scan the project")


def _confirm(window: MainWindow) -> None:
    window.confirm_plan()


def _block(window: MainWindow) -> None:
    window._workspace.report_job(
        "scan", states.STATE_BLOCKED, blocker="The root is not readable."
    )
    window._refresh_destinations()


def _finish(window: MainWindow) -> None:
    """Drive every job to a reported completion, then leave the decision open."""
    plan = window._workspace.plan
    if plan is None:
        return
    effects = (authority.EFFECT_PROVIDER_DISPATCH, authority.EFFECT_COST)
    for job in list(plan.jobs):
        window._workspace.dispatch(job.key, confirmed_effects=effects)
        window._workspace.report_job(job.key, states.STATE_COMPLETED)
    window._refresh_destinations()


def _resume_ready(window: MainWindow) -> None:
    """Record a decision so Home shows the post-decision resume."""
    window._workspace.record_decision(states.DECISION_APPROVE, "heron", "Looks right.")
    window._refresh_destinations()


#: (name, setup) — each builds one deterministic acceptance state.
SCENARIOS = (
    ("first-use", None),
    ("project-open", _open_project),
    ("plan", lambda window: (_open_project(window), _propose(window))),
    ("blocked", lambda window: (_open_project(window), _propose(window), _confirm(window), _block(window))),
    ("review-ready", lambda window: (_open_project(window), _propose(window), _confirm(window), _finish(window))),
    ("resumed", lambda window: (
        _open_project(window), _propose(window), _confirm(window), _finish(window),
        _resume_ready(window),
    )),
)


def _window_for(name: str) -> MainWindow:
    window = MainWindow()
    for scenario, setup in SCENARIOS:
        if scenario == name and setup is not None:
            setup(window)
    return window


def _shoot(window: MainWindow, name: str, width: int, height: int) -> list:
    window.resize(width, height)
    window.show()
    QApplication.processEvents()
    QApplication.processEvents()
    written = []
    for key in DESTINATIONS:
        # Only shoot what this state can actually reach: a destination whose
        # rail entry is hidden is not part of the frame, and Project history is
        # only offered once a project is bound.
        button = window._shell._buttons.get(key)
        if button is not None and button.isHidden():
            continue
        if key == "history" and not window._shell.started:
            continue
        window._select_destination(key)
        QApplication.processEvents()
        path = os.path.join(OUT_DIR, f"{name}-{key}-{width}x{height}.png")
        window.grab().save(path)
        written.append(path)
    # Work's contextual views are only reachable once there is work, so they
    # are shot as the Work destination with the relevant view selected. The
    # relevance rule mirrors WorkDestination.refresh exactly.
    plan = window._workspace.plan
    has_plan = plan is not None and bool(plan.jobs)
    relevant = {
        "jobs": has_plan,
        "agents": has_plan,
        "review": has_plan
        and any(
            job.state not in (states.STATE_DRAFT, states.STATE_READY)
            for job in plan.jobs
        ),
    }
    for view in WORK_VIEWS:
        if not relevant[view]:
            continue
        window._select_destination(view)
        QApplication.processEvents()
        path = os.path.join(OUT_DIR, f"{name}-work-{view}-{width}x{height}.png")
        window.grab().save(path)
        written.append(path)
    return written


def main() -> int:
    _app()
    written = []
    for name, _setup in SCENARIOS:
        for width, height in VIEWPORTS:
            window = _window_for(name)
            written.extend(_shoot(window, name, width, height))
            window._supervisor.terminate()
            window._credential_supervisor.terminate()
            window.close()

    for path in written:
        print(os.path.relpath(path, OUT_DIR))
    print(f"{len(written)} renders written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
