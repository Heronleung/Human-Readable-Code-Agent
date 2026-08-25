 """PyInstaller entry point for the P3.1 frozen slice.

Bundles the single unified entry ``hrca.app`` so that one frozen executable
serves both roles: desktop client by default, headless boundary with
``--serve``. PyInstaller cannot be pointed at ``src/hrca/app.py`` directly
because that module uses package-relative imports; this top-level launcher
imports it as ``hrca.app`` (with ``--paths src`` on the build command) so the
relative imports resolve correctly.
"""

from __future__ import annotations

from hrca.app import main

if __name__ == "__main__":
    raise SystemExit(main())
