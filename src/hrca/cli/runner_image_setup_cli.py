"""Operator CLI for the P5.5a-r3c runner-image setup.

The bounded way to reach :mod:`hrca.runner_image_setup` from a terminal. It is
deliberately not a desktop surface and not a protocol action: no accepted
desktop action carries a base reference, a Dockerfile or an evidence base, so no
existing route fits this setup and none is added.

What it will and will not do:

* ``inspect`` dispatches only read-only Docker resolution and prints what the
  local artifact is;
* ``build`` dispatches exactly one bounded ``docker build`` and prints the
  record of what it changed and what it did not;
* it writes nothing except the record inside an evidence base passed with
  ``--evidence-base``, and it writes that only when the option is given;
* it never logs in, never pushes, never mounts or runs a container, never
  touches a candidate, and never sets an approval, adoption or application
  status.

Exit codes: ``0`` the artifact is ready to be planned against, ``2`` it is not
(or the setup was refused), ``1`` a usage or read failure. **Exit zero is
readiness to plan, not a validation result and not an approval.** A usage error
is remapped to ``1`` as well, so a mistyped command line is never mistaken for
"not ready".

Commands
--------

``inspect [--base <ref>]``
    Resolve the base and report the local runner image's readiness.
``build [--base <ref>] [--evidence-base <dir>]``
    Prepare the runner image once and report the record.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from ..execution import runner_image_policy as policy
from ..execution import runner_image_setup as setup

EXIT_READY = 0
EXIT_USAGE = 1
EXIT_NOT_READY = 2


def _report(record, error: Optional[str], evidence_base: Optional[str]) -> int:
    sys.stdout.write(setup.dumps(record) + "\n")
    if evidence_base is not None:
        path, reason = setup.write_record(record, evidence_base)
        if reason is not None:
            sys.stderr.write("the record could not be written: %s\n" % reason)
            return EXIT_USAGE
        sys.stderr.write("record written to %s\n" % path)
    readiness = record.get("readiness") or {}
    if error is not None:
        sys.stderr.write("refused: %s\n" % error)
        return EXIT_NOT_READY
    if readiness.get("state") != policy.READY:
        sys.stderr.write("not ready: %s\n" % (readiness.get("reason") or "unknown"))
        return EXIT_NOT_READY
    return EXIT_READY


def _cmd_inspect(argv: argparse.Namespace) -> int:
    record, error = setup.RunnerImageSetup().inspect(argv.base)
    return _report(record, error, argv.evidence_base)


def _cmd_build(argv: argparse.Namespace) -> int:
    record, error = setup.RunnerImageSetup().build(argv.base)
    return _report(record, error, argv.evidence_base)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-runner-image-setup",
        description=(
            "Bounded setup evidence for one exact runner image. Exit zero is "
            "readiness to plan, not a validation result and not an approval."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("inspect", _cmd_inspect, "resolve the base and report readiness"),
        ("build", _cmd_build, "prepare the runner image once and report it"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--base",
            default=policy.BASE_REFERENCE,
            help="the base reference, by digest; must be the pinned base",
        )
        command.add_argument(
            "--evidence-base",
            default=None,
            help="an existing directory to write the record into",
        )
        command.set_defaults(func=handler)

    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_error:
        # argparse exits 2 on a usage error, which is this program's "not
        # ready". Remap it so a caller reading $? cannot confuse a typo with a
        # refused artifact; --help still exits 0.
        if exit_error.code in (0, None):
            return EXIT_READY
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
