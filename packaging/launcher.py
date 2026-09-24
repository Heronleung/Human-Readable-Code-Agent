"""PyInstaller entry point for the P3.1 frozen slice.

Bundles the single unified entry ``hrca.cli.app`` so that one frozen executable
serves both roles: desktop client by default, headless boundary with
``--serve``. PyInstaller cannot be pointed at ``src/hrca/cli/app.py`` directly
because that module uses package-relative imports; this top-level launcher
imports it as ``hrca.cli.app`` (with ``--paths src`` on the build command) so the
relative imports resolve correctly.

The import target moved when the package was organised by responsibility. It
used to name ``hrca.app``, a top-level module that no longer exists, so this
launcher could not be imported at all. ``tests/test_app_entry.py`` now holds the
target to a module that really is importable, without building anything.
"""

from __future__ import annotations

from hrca.cli.app import main

if __name__ == "__main__":
    raise SystemExit(main())
