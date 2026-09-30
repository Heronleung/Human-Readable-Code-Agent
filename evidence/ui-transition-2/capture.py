"""Deterministic viewport evidence for the UI-TRANSITION-2 workspace.

Renders the desktop offscreen from fixed, hand-built state and writes one PNG
per scenario per viewport. Nothing here touches the network, a provider, a
credential or a store: the window is built, fed recorded values through its
own `_apply_*` / host methods, laid out and grabbed.

Run it from the repository root:

    uv run python evidence/ui-transition-2/capture.py

The PNGs it writes are regenerable byte-for-byte for a given Qt build, so this
script — not the images — is the durable evidence.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join("src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from hrca.ui.appmodel import states  # noqa: E402
from hrca.ui.client import MainWindow  # noqa: E402

VIEWPORTS = ((1024, 640), (1920, 1080))
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
DESTINATIONS = ("resume", "chat", "jobs", "agents", "review", "documents", "settings")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _windows() -> list:
    """Return ``(scenario_name, workspace-setup callable)`` pairs."""
    return [
        ("first-use", lambda window: None),
        ("project-open", lambda window: window._on_project_opened(
            {"root": "/repo/Human-Readable-Code-Agent", "repository_state": "Unverified"}
        )),
    ]


def _shoot(window: MainWindow, name: str, width: int, height: int) -> str:
    window.resize(width, height)
    window.show()
    QApplication.processEvents()
    QApplication.processEvents()
    path = os.path.join(OUT_DIR, f"{name}-{width}x{height}.png")
    window.grab().save(path)
    return path


def main() -> int:
    _app()
    written = []

    for scenario, setup in _windows():
        for width, height in VIEWPORTS:
            window = MainWindow()
            setup(window)
            window._refresh_destinations()
            for key in DESTINATIONS:
                window._select_destination(key)
                written.append(_shoot(window, f"{scenario}-{key}", width, height))
            window._supervisor.terminate()
            window._credential_supervisor.terminate()
            window.close()

    # The plan and a blocked job, both at the smaller viewport.
    window = MainWindow()
    window._on_project_opened(
        {"root": "/repo/Human-Readable-Code-Agent", "repository_state": "Unverified"}
    )
    window._on_goal_submitted("interpret the discount rule and scan the project")
    for key in ("chat", "jobs", "review"):
        window._select_destination(key)
        written.append(_shoot(window, f"plan-{key}", 1024, 640))

    window.confirm_plan()
    window._workspace.report_job("scan", states.STATE_BLOCKED, blocker="The root is not readable.")
    window._refresh_destinations()
    for key in ("jobs", "review", "resume"):
        window._select_destination(key)
        written.append(_shoot(window, f"blocked-{key}", 1024, 640))

    window._supervisor.terminate()
    window._credential_supervisor.terminate()
    window.close()

    for path in written:
        print(os.path.relpath(path, OUT_DIR))
    print(f"{len(written)} renders written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
