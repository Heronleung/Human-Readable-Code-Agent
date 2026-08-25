"""Unified entry executable (P3.1).

Runs the desktop client by default, and the headless boundary when invoked
with the ``--serve`` argument sentinel. This is the entry point a PyInstaller
build bundles so that one frozen executable serves both roles:

* ``hrca-app`` (or ``python -m hrca.app``) launches the desktop client,
* ``hrca-app --serve`` (or ``python -m hrca.app --serve``) runs the headless
  boundary over stdin/stdout.

The client and the boundary are imported lazily so each branch pulls in only
what it needs; the architecture import rule — that the client never imports the
deterministic core — is preserved because this launcher, not the client, is the
place that knows about both halves.
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from . import contract


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if contract.SERVE_SENTINEL in args:
        from .boundary import main as serve

        return serve(args)
    from .client import main as run_client

    return run_client(args)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
