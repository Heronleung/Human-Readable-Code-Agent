"""Operator CLI for setup verification (P5.5r3c1).

The one explicit, deterministic way to verify a setup-only change:

    python -m hrca.setup_verification_cli

It runs the allowlist in :mod:`hrca.setup_verification` and refuses anything
else — a module that dispatches containers is refused *by name*, before it is
imported, and while the selection runs, any attempt to start a process or to
import an excluded module is a failure rather than a note.

Exit codes: ``0`` verified, ``1`` the selection ran and failed, ``2`` the run was
refused before it could verify anything. A usage error is remapped to ``2`` as
well, so a typo is never mistaken for a pass.

Why this is a separate module
-----------------------------

``python -m hrca.x`` executes ``x`` as ``__main__``. If the state and the guard
lived here, they would exist twice in one process — once as ``__main__``, once as
``hrca.setup_verification`` when the tests under it import the canonical name —
and the guard the tests inspect would not be the guard that was armed. This
module holds no state, so its duplication is harmless; exactly one copy of
:mod:`hrca.setup_verification` is ever live.

It dispatches nothing, writes nothing and reaches no network.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import setup_verification

EXIT_VERIFIED = setup_verification.EXIT_VERIFIED
EXIT_FAILED = setup_verification.EXIT_FAILED
EXIT_REFUSED = setup_verification.EXIT_REFUSED


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-setup-verification",
        description=(
            "Run the setup-verification test selection. It selects only an "
            "explicit allowlist and refuses to start a process or import a "
            "container integration module while it runs."
        ),
    )
    parser.add_argument(
        "--module",
        action="append",
        default=None,
        help="narrow the run to an allowlisted module; repeatable",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_error:
        if exit_error.code in (0, None):
            return EXIT_VERIFIED
        return EXIT_REFUSED
    return setup_verification.run(args.module)


if __name__ == "__main__":
    raise SystemExit(main())
