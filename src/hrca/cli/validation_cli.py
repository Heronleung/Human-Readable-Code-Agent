"""Offline operator CLI for the P5.5a validation contract.

The bounded, offline way to reach :mod:`hrca.validation` from a terminal. It is
deliberately not a desktop surface and not a protocol action: the accepted
desktop actions carry no candidate root, no plan and no evidence base, so no
existing route fits this contract and none is added.

What it will and will not do:

* it reads the candidate root, the review envelope and an optional plan, and it
  writes only beneath the evidence base passed to ``run``;
* ``plan`` writes nothing at all;
* it opens no socket, reaches no provider or credential store, sets no
  approval, adoption or application status, and never copies candidate bytes
  into accepted source;
* it prints bounded reasons on failure and never a caller value, a source body,
  an environment value or an internal exception.

Exit codes: ``0`` a passing result, ``2`` a non-passing result or a bounded
refusal, ``1`` a usage or read failure. **Exit zero is evidence, not approval**:
the contract has no approval to give.

Commands
--------

``plan --candidate <dir> --review <f> [--check <id>]...``
    Verify the candidate and print the product-owned plan for it.
``run --candidate <dir> --review <f> --evidence-base <dir> [--check <id>]...
       [--plan <f>]``
    Build (or verify a supplied) plan, dispatch every check, append the
    attempts, and print the result.
``verify --evidence-base <dir>``
    Re-read the stored evidence and report a bounded summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional, Sequence, Tuple

from ..authoring import validation
from ..authoring import validation_plan

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NOT_PASSING = 2


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _build(
    argv: argparse.Namespace,
) -> Tuple[Optional[dict], Optional[dict], Optional[dict], Optional[int], Optional[str]]:
    """Return ``(plan, candidate_root, review, exit_code, message)``.

    A file that cannot be read is a usage failure; a candidate or plan that is
    read and then refused is a bounded refusal. The two stay distinct, so a typo
    in a path is never reported as a rejected candidate.
    """
    try:
        review = _read_json(argv.review)
    except (OSError, ValueError):
        return None, None, None, EXIT_USAGE, "the review envelope could not be read"

    binding, reason = validation.verify_candidate(argv.candidate, review)
    if reason is not None:
        return None, None, None, EXIT_NOT_PASSING, "candidate refused: %s" % reason

    if getattr(argv, "plan", None):
        try:
            supplied = _read_json(argv.plan)
        except (OSError, ValueError):
            return None, None, None, EXIT_USAGE, "the plan could not be read"
        reason = validation_plan.validate_plan(supplied)
        if reason is not None:
            return None, None, None, EXIT_NOT_PASSING, "plan refused: %s" % reason
        if supplied["candidate"] != binding:
            return None, None, None, EXIT_NOT_PASSING, (
                "plan refused: the plan describes a different candidate"
            )
        return supplied, argv.candidate, review, None, None

    request = {"checks": list(argv.check)} if argv.check else {}
    plan, reason = validation_plan.build_plan(binding, request)
    if reason is not None:
        return None, None, None, EXIT_NOT_PASSING, "plan refused: %s" % reason
    return plan, argv.candidate, review, None, None


def _cmd_plan(argv: argparse.Namespace) -> int:
    plan, _root, _review, exit_code, error = _build(argv)
    if error is not None:
        sys.stderr.write(error + "\n")
        return exit_code
    sys.stdout.write(validation_plan.dumps(plan) + "\n")
    return EXIT_OK


def _cmd_run(argv: argparse.Namespace) -> int:
    plan, root, review, exit_code, error = _build(argv)
    if error is not None:
        sys.stderr.write(error + "\n")
        return exit_code

    result, error = validation.run_and_record(plan, root, review, argv.evidence_base)
    if error is not None:
        sys.stderr.write("refused: %s\n" % error)
        return EXIT_NOT_PASSING
    sys.stdout.write(validation.dumps(result) + "\n")
    return EXIT_OK if result["state"] == validation.STATE_PASSED else EXIT_NOT_PASSING


def _cmd_verify(argv: argparse.Namespace) -> int:
    attempts, reason = validation.read_attempts(argv.evidence_base)
    if reason is not None:
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_NOT_PASSING
    counted: List[str] = [attempt["state"] for attempt in attempts]
    summary = {
        "attempts": len(attempts),
        "states": counted,
        "ordinals": [attempt["ordinal"] for attempt in attempts],
        "checks": sorted({attempt["check_id"] for attempt in attempts}),
        "all_passed": bool(counted) and all(state == validation.STATE_PASSED for state in counted),
    }
    sys.stdout.write(validation.dumps(summary) + "\n")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-validation",
        description=(
            "Bounded validation evidence for one exact candidate. Exit zero is "
            "evidence, not approval: nothing here approves, adopts or applies."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_target(sub_parser):
        sub_parser.add_argument("--candidate", required=True, help="candidate root")
        sub_parser.add_argument("--review", required=True, help="review envelope JSON")
        sub_parser.add_argument(
            "--check", action="append", default=None, help="check id; repeatable"
        )

    plan_parser = sub.add_parser("plan", help="print the plan for a candidate")
    add_target(plan_parser)
    plan_parser.set_defaults(func=_cmd_plan)

    run_parser = sub.add_parser("run", help="run the plan and append the evidence")
    add_target(run_parser)
    run_parser.add_argument("--evidence-base", required=True, help="evidence directory")
    run_parser.add_argument("--plan", default=None, help="a plan to verify and use")
    run_parser.set_defaults(func=_cmd_run)

    verify_parser = sub.add_parser("verify", help="re-read the stored evidence")
    verify_parser.add_argument("--evidence-base", required=True)
    verify_parser.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
