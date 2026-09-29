"""Operator CLI for the explicit test routes (B0).

The one deliberate way to run a bounded slice of the suite:

    python -m hrca.test_routes_cli                        # the core route
    python -m hrca.test_routes_cli --route twin
    python -m hrca.test_routes_cli --route provider
    python -m hrca.test_routes_cli --route setup
    python -m hrca.test_routes_cli --route core --module test_memory
    python -m hrca.test_routes_cli --route container --acknowledge-container-risk
    python -m hrca.test_routes_cli --route full --acknowledge-container-risk

``--route`` defaults to ``core`` because ``core`` is the route ordinary work
should use: it is container-free, network-free, credential-free and needs no
optional dependency. The two dispatching routes (``container`` and ``full``)
refuse before they import anything unless ``--acknowledge-container-risk`` is
passed, so a container is never started by a typo.

Exit codes: ``0`` verified, ``1`` the selection ran and failed, ``2`` the run
was refused before it could verify anything. A usage error is remapped to ``2``
as well, so a typo is never mistaken for a pass.

Why this is a separate module
-----------------------------

The same reason :mod:`hrca.setup_verification_cli` is separate from
:mod:`hrca.setup_verification`: ``python -m hrca.x`` executes ``x`` as
``__main__``, so state placed here would exist twice in one process once the
tests under it imported the canonical name. This module holds no state, so its
duplication is harmless; exactly one copy of :mod:`hrca.test_routes` and one
copy of the setup guard are ever live.

It dispatches nothing, writes nothing and reaches no network.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from . import test_routes

EXIT_VERIFIED = test_routes.EXIT_VERIFIED
EXIT_FAILED = test_routes.EXIT_FAILED
EXIT_REFUSED = test_routes.EXIT_REFUSED


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-test-routes",
        description=(
            "Run one explicit test route. Every route is a code-owned "
            "allowlist: a module that is not on it is refused by name before "
            "it is imported, and the safe routes run under the setup guard, "
            "which turns any attempt to start a process into a failure."
        ),
    )
    parser.add_argument(
        "--route",
        choices=list(test_routes.route_names()),
        default=test_routes.ROUTE_CORE,
        help="which route to run (default: core)",
    )
    parser.add_argument(
        "--module",
        action="append",
        default=None,
        help="narrow the run to an allowlisted module; repeatable",
    )
    parser.add_argument(
        "--acknowledge-container-risk",
        action="store_true",
        help=(
            "required by the container and full routes, which can start a real "
            "Docker container"
        ),
    )
    parser.add_argument(
        "--list-routes",
        action="store_true",
        help="print each route's allowlist and exit without running anything",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_error:
        if exit_error.code in (0, None):
            return EXIT_VERIFIED
        return EXIT_REFUSED

    if args.list_routes:
        for route in test_routes.route_names():
            modules = test_routes.route_allowlist(route)
            print("%s (%d):" % (route, len(modules)))
            for name in modules:
                print("  %s" % name)
        return EXIT_VERIFIED

    return test_routes.run(
        args.route,
        args.module,
        acknowledge_container_risk=args.acknowledge_container_risk,
    )


if __name__ == "__main__":
    raise SystemExit(main())
