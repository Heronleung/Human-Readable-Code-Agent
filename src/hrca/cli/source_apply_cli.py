"""Offline operator CLI for the source application coordinator (P5-X A2).

    python -m hrca.cli.source_apply_cli --mode plan \\
        --evidence <evidence.json> --apply-root <dir>

    python -m hrca.cli.source_apply_cli --mode apply \\
        --evidence <evidence.json> --apply-root <dir> \\
        --receipt <receipt.json> --base <custody-dir>

The thin adapter around :mod:`hrca.authoring.source_apply`. It exists so the
composition can stay a leaf: this is where the two documents are read from
disk, and where the caller-supplied roots are passed through. It decides
nothing — every verdict, refusal and state comes from the module that owns the
contract.

What it will not do
-------------------

It runs no command, launches no process, reads no store, scans nothing, and
writes nothing itself: the only writes in the whole path are the four the
coordinator owns, beneath the custody base this caller names. An absent
``--receipt`` is not a usage error — it is the ``refused_missing_approval``
state, because "no approval was supplied" is a fact an operator must be able to
observe as a verdict rather than as an argument-parsing failure.

Exit codes: ``0`` ready or applied, ``1`` usage, ``2`` refused (the input could
not be bound, or the tree is not one this operation may touch), ``3`` a bounded
non-success state, ``4`` a write landed but the record could not be persisted —
reported distinctly because an unrecorded application is exactly the situation
an operator must not miss.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

from ..authoring import source_apply
from ..authoring import validation

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2
EXIT_NOT_SUCCESS = 3
EXIT_UNPERSISTED = 4

MAX_EVIDENCE_BYTES = source_apply.MAX_RECORD_BYTES

REASON_EVIDENCE_UNREADABLE = "the evidence document could not be read"
REASON_RECEIPT_UNREADABLE = "the approval receipt could not be read"
REASON_BASE_REQUIRED = "applying requires a custody base"
REASON_CANDIDATE_UNVERIFIED = "the candidate could not be verified under its identity"


def _read_bytes(path: str, limit: int) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data


def _read_mapping(path: str, limit: int) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], Optional[str]]:
    """Read one document, returning ``(mapping, raw bytes, reason)``.

    The raw bytes are returned alongside the mapping because a receipt's raw
    digest is taken over what was read, not over a re-serialization of it.
    """
    data = _read_bytes(path, limit)
    if data is None:
        return None, None, "unreadable"
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None, "unreadable"
    if not isinstance(document, dict):
        return None, None, "unreadable"
    return document, data, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-source-apply",
        description=(
            "Plan or apply one bound candidate to one bound target. The plan "
            "writes nothing; the apply writes only the coordinator's own "
            "pre-image, temporary file, target replacement and record."
        ),
    )
    parser.add_argument(
        "--mode", required=True, choices=sorted(source_apply.MODES)
    )
    parser.add_argument("--evidence", required=True, help="the bound evidence document")
    parser.add_argument("--apply-root", required=True, help="the accepted source root")
    parser.add_argument(
        "--receipt",
        default=None,
        help="the client-authored approval receipt; absent means no approval",
    )
    parser.add_argument(
        "--base", default=None, help="the custody base for the pre-image and record"
    )
    args = parser.parse_args(argv)

    evidence, _raw, _reason = _read_mapping(args.evidence, MAX_EVIDENCE_BYTES)
    if evidence is None:
        sys.stderr.write("refused: %s\n" % REASON_EVIDENCE_UNREADABLE)
        return EXIT_REFUSED

    # The owner's verifier is invoked here, in the adapter — which is the whole
    # reason the adapter exists. The coordinator binds the verdict and never
    # imports the module that owns it, because that module also owns the
    # container-gated runner. A supplied binding is never trusted: it is
    # replaced by this call's result.
    group = evidence.get("candidate")
    if not isinstance(group, dict):
        sys.stderr.write("refused: %s\n" % REASON_CANDIDATE_UNVERIFIED)
        return EXIT_REFUSED
    binding, binding_reason = validation.verify_candidate(
        group.get("root"), group.get("review")
    )
    if binding_reason is not None:
        sys.stderr.write("refused: %s\n" % REASON_CANDIDATE_UNVERIFIED)
        return EXIT_REFUSED
    group["binding"] = binding

    if args.mode == source_apply.MODE_PLAN:
        record, reason = source_apply.plan_application(evidence, args.apply_root)
    else:
        receipt = None
        receipt_bytes = None
        if args.receipt is not None:
            receipt, receipt_bytes, _r = _read_mapping(
                args.receipt, source_apply.MAX_RECEIPT_BYTES
            )
            if receipt is None:
                sys.stderr.write("refused: %s\n" % REASON_RECEIPT_UNREADABLE)
                return EXIT_REFUSED
        if args.base is None:
            sys.stderr.write("refused: %s\n" % REASON_BASE_REQUIRED)
            return EXIT_REFUSED
        record, reason = source_apply.apply_application(
            evidence, args.apply_root, args.base, receipt, receipt_bytes
        )

    if reason is not None:
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_REFUSED

    sys.stdout.write(source_apply.dumps(record) + "\n")
    if record.get("record_persisted") is False and record.get("write_surface", {}).get(
        "target_written"
    ):
        return EXIT_UNPERSISTED
    if record["state"] in (source_apply.STATE_READY, source_apply.STATE_APPLIED):
        return EXIT_OK
    return EXIT_NOT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
