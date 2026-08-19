"""Allow ``python -m hrca`` to run the scanner CLI."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
