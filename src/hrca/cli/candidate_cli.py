"""Offline operator CLI for the P5.4 candidate contract.

The bounded, offline way to reach :mod:`hrca.candidate` from a terminal. It is
deliberately not a desktop surface and not a protocol action: the accepted
desktop actions carry no edit grammar and no candidate output root, so no
existing route fits this contract and none is added.

What it will and will not do:

* it reads the explicit JSON files and the repository directory it is given, and
  it writes **only** beneath the output base passed to ``build``;
* ``review`` and ``derive`` write nothing at all;
* it opens no socket, reaches no provider, credential store, runner or Git, sets
  no validation, approval or adoption status, and never applies a candidate;
* it prints bounded reasons on failure and never a caller value, a source body,
  an environment value or an internal exception.

Commands
--------

``derive --intent <f> --scanner <f> --twin <f>``
    Bind one intent to one scanner document and one Twin store and print the
    exact identities an edit must declare.
``review --edit <f> --intent <f> --scanner <f> --twin <f> --repo <dir>``
    Render the candidate review envelope without creating anything.
``build --edit <f> --intent <f> --scanner <f> --twin <f> --repo <dir> --output-base <dir>``
    Materialize the candidate under a fresh root in ``--output-base`` and print
    the same envelope.

Exit codes: ``0`` a ready candidate or an honest no-change, ``2`` a bounded
refusal or a terminal non-ready state, ``1`` a usage or read failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence, Tuple

from ..authoring import candidate
from ..authoring import candidate_edit
from ..authoring import impact_proposal
from ..authoring import intent_delta

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2

_OK_STATES = frozenset({candidate.STATE_CANDIDATE_READY, candidate.STATE_NO_CHANGE})


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _bind_intent(argv: argparse.Namespace):
    """Build the intent delta and its bound impact proposal from the inputs.

    The proposal is always *derived* here rather than accepted from a file: the
    accepted evidence is the authority, and a proposal supplied separately could
    be one the evidence no longer supports.

    Returns ``(delta, proposal, evidence, exit_code, message)``. A file that
    cannot be read is a usage failure; a document that is read and then refused
    is a bounded refusal. The two must stay distinct, or a typo in a path would
    be reported as a rejected intent.
    """
    try:
        raw_intent = _read_json(argv.intent)
        scanner_doc = _read_json(argv.scanner)
        store = _read_json(argv.twin)
    except (OSError, ValueError):
        return None, None, None, EXIT_USAGE, "an input file could not be read"

    evidence = {"scanner": scanner_doc, "twin": store}
    delta, error = intent_delta.build_intent_delta(raw_intent)
    if error is not None:
        return None, None, None, EXIT_REFUSED, "intent refused: %s" % error
    proposal, error = impact_proposal.build_impact_proposal(delta, evidence)
    if error is not None:
        return None, None, None, EXIT_REFUSED, "evidence refused: %s" % error
    return delta, proposal, evidence, EXIT_OK, None


def _cmd_derive(argv: argparse.Namespace) -> int:
    delta, proposal, _evidence, exit_code, error = _bind_intent(argv)
    if error is not None:
        sys.stderr.write(error + "\n")
        return exit_code
    summary = {
        "intent_delta_id": delta["intent_delta_id"],
        "proposal_id": proposal["proposal_id"],
        "proposal_state": proposal["state"],
        "binding_fingerprint": proposal["binding"]["binding_fingerprint"],
        "baseline": delta["baseline"],
        "authorizes_an_edit": proposal["state"] in candidate.AUTHORIZING_PROPOSAL_STATES,
        "candidate_edit_schema_version": candidate_edit.CANDIDATE_EDIT_SCHEMA_VERSION,
    }
    sys.stdout.write(candidate.dumps(summary) + "\n")
    return EXIT_OK if summary["authorizes_an_edit"] else EXIT_REFUSED


def _run(argv: argparse.Namespace, materialize: bool) -> int:
    delta, proposal, evidence, exit_code, error = _bind_intent(argv)
    if error is not None:
        sys.stderr.write(error + "\n")
        return exit_code
    try:
        raw_edit = _read_json(argv.edit)
    except (OSError, ValueError):
        sys.stderr.write("the edit file could not be read\n")
        return EXIT_USAGE
    edit, error = candidate_edit.build_edit(raw_edit)
    if error is not None:
        sys.stderr.write("edit refused: %s\n" % error)
        return EXIT_REFUSED

    envelope, error = candidate.build_candidate(
        edit,
        delta,
        proposal,
        evidence,
        argv.repo,
        getattr(argv, "output_base", None),
        materialize=materialize,
    )
    if error is not None:
        sys.stderr.write("refused: %s\n" % error)
        return EXIT_REFUSED

    if materialize and envelope["candidate_root_name"]:
        # The operator's own action feedback names the identity the envelope
        # carries and the container it went into; neither line is the artifact.
        sys.stderr.write(
            "wrote %s under %s\n"
            % (envelope["candidate_id"], envelope["candidate_root_name"])
        )
    sys.stdout.write(candidate.dumps(envelope) + "\n")
    return EXIT_OK if envelope["state"] in _OK_STATES else EXIT_REFUSED


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-candidate",
        description=(
            "Isolated content-addressed candidate review. Read-only unless "
            "'build' is used, and then only beneath --output-base."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_binding(sub_parser):
        sub_parser.add_argument("--intent", required=True, help="intent JSON file")
        sub_parser.add_argument("--scanner", required=True, help="scanner document JSON")
        sub_parser.add_argument("--twin", required=True, help="Twin store JSON")

    derive_parser = sub.add_parser("derive", help="print the identities an edit must declare")
    add_binding(derive_parser)
    derive_parser.set_defaults(func=_cmd_derive)

    review_parser = sub.add_parser("review", help="render the envelope; create nothing")
    add_binding(review_parser)
    review_parser.add_argument("--edit", required=True, help="edit request JSON file")
    review_parser.add_argument("--repo", required=True, help="accepted repository root")
    review_parser.set_defaults(func=lambda args: _run(args, False))

    build_parser = sub.add_parser("build", help="materialize the candidate")
    add_binding(build_parser)
    build_parser.add_argument("--edit", required=True, help="edit request JSON file")
    build_parser.add_argument("--repo", required=True, help="accepted repository root")
    build_parser.add_argument(
        "--output-base", required=True, help="existing directory outside the repository"
    )
    build_parser.set_defaults(func=lambda args: _run(args, True))

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
