"""Offline operator CLI for the typed Intent Delta and impact proposal (P5.3).

This is the bounded, offline way to reach the P5.3 contract from a terminal. It
is deliberately not a desktop surface and not a new protocol action: the
accepted desktop actions derive a proposal from a Code Map *draft*, which is a
different input with different authority, so no existing read-only route fits
this contract and none is added. The desktop therefore does not reach it, and
this CLI does not widen what the desktop can do.

What it will and will not do:

* it reads only the explicit JSON files it is given and writes only to stdout
  and stderr;
* it opens no socket, reaches no provider, no credential store, no runner, no
  Git and no repository;
* it writes no file, no store and no package state, and it never creates a
  candidate, a diff, a branch or a commit;
* it prints a bounded reason on refusal and never a caller value, a source body,
  an environment value or an internal exception.

Commands
--------

``verify <intent.json>``
    Validate one serialized Intent Delta against the P5.3 schema and print its
    identity and bounded section counts.
``propose --intent <f> --scanner <f> --twin <f>``
    Bind one intent to one scanner document and one Twin store and print the
    canonical advisory impact proposal.

Exit codes: ``0`` success, ``2`` a bounded refusal, ``1`` a usage or read
failure. A refusal is never reported as success.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence

from ..authoring import impact_proposal
from ..authoring import intent_delta

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# -- commands --------------------------------------------------------------


def _cmd_verify(argv: argparse.Namespace) -> int:
    """Validate one serialized Intent Delta and print a bounded summary."""
    try:
        raw = _read_json(argv.intent)
    except (OSError, ValueError):
        sys.stderr.write("intent file could not be read\n")
        return EXIT_USAGE

    # A serialized delta is validated as written; a raw authored intent (no
    # schema_version) is built first. Both paths end in validate_intent_delta,
    # so a document that validates here is one this contract accepts.
    if isinstance(raw, dict) and "schema_version" in raw:
        reason = intent_delta.validate_intent_delta(raw)
        delta: Optional[dict] = raw
    else:
        delta, error = intent_delta.build_intent_delta(raw)
        reason = error if error is not None else intent_delta.validate_intent_delta(delta)

    if reason is not None:
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_REFUSED

    summary = {
        "intent_delta_id": delta["intent_delta_id"],
        "schema_version": delta["schema_version"],
        "workspace_id": delta["baseline"]["workspace_id"],
        "scan_generation": delta["baseline"]["scan_generation"],
        "scope_entities": len(delta["scope"]["entities"]),
        "scope_artifacts": len(delta["scope"]["artifacts"]),
        "acceptance_criteria": len(delta["acceptance_criteria"]),
        "evidence_references": len(delta["origin"]["evidence"]),
        "prose_present": bool((delta.get("prose") or {}).get("text")),
        "executable": delta["executable"],
        "applied": delta["applied"],
    }
    sys.stdout.write(impact_proposal.dumps(summary) + "\n")
    return EXIT_OK


def _cmd_propose(argv: argparse.Namespace) -> int:
    """Bind one intent to its evidence and print the canonical proposal."""
    try:
        raw_intent = _read_json(argv.intent)
        scanner_doc = _read_json(argv.scanner)
        store = _read_json(argv.twin)
    except (OSError, ValueError):
        sys.stderr.write("an input file could not be read\n")
        return EXIT_USAGE

    intent, error = intent_delta.build_intent_delta(raw_intent)
    if error is not None:
        sys.stderr.write("refused: %s\n" % error)
        return EXIT_REFUSED

    proposal, error = impact_proposal.build_impact_proposal(
        intent, {"scanner": scanner_doc, "twin": store}
    )
    if error is not None:
        sys.stderr.write("refused: %s\n" % error)
        return EXIT_REFUSED

    reason = impact_proposal.validate_impact_proposal(proposal)
    if reason is not None:  # pragma: no cover - a built proposal always validates
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_REFUSED

    sys.stdout.write(impact_proposal.dumps(proposal) + "\n")
    return EXIT_OK


# -- entry point -----------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-intent",
        description=(
            "Offline typed Intent Delta and deterministic advisory impact "
            "proposal. Read-only: no file, store, candidate or repository state "
            "is written."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify_parser = sub.add_parser(
        "verify", help="validate one serialized Intent Delta"
    )
    verify_parser.add_argument("intent", help="path to an intent JSON file")
    verify_parser.set_defaults(func=_cmd_verify)

    propose_parser = sub.add_parser(
        "propose", help="bind one intent to evidence and print the proposal"
    )
    propose_parser.add_argument("--intent", required=True, help="intent JSON file")
    propose_parser.add_argument(
        "--scanner", required=True, help="scanner document JSON file"
    )
    propose_parser.add_argument("--twin", required=True, help="Twin store JSON file")
    propose_parser.set_defaults(func=_cmd_propose)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
