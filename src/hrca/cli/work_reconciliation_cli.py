"""Offline operator CLI for the controlled-change reconciliation record (P5-E2).

    python -m hrca.cli.work_reconciliation_cli \\
        --result <result.json> --plan <plan.json> --link <link.json> \\
        --twin-store <dir> --workspace-id ws:... \\
        --accepted-generation 1 --accepted-fingerprint <hex> \\
        --run-id run:... --record-id evidence:... \\
        --actor <name> --decided-at <iso> --declared applied \\
        --base <dir>

The thin adapter around :mod:`hrca.authoring.work_reconciliation`. It exists so
the composition can stay a pure leaf: this is where the stores are read, where
the *owners'* gates are invoked, and where the record is written.

What it invokes, and why from here
----------------------------------

* ``validation.validate_result`` — the validation result contract has one
  implementation and it is that one. The adapter refuses an unsound result
  rather than letting the composition bind facts from a document nobody checked.
* ``validation_plan.migrate_plan`` — the plan's own version gate.
* ``twin_store.load`` — the authoritative Twin store, for the revision observed
  after the operator's rescan. **The adapter never runs a rescan**: it reads
  whatever the store already holds, so the observed baseline stays independent
  of the operator's word.
* ``memory_twin_link.resolve_freshness`` — the Memory link verdict has one
  implementation and it is that one; the composition binds the verdict it is
  given rather than deriving a second opinion.

What it will not do
-------------------

It runs no command, materialises no candidate, applies no source, alters no
accepted repository state, and infers no acceptance: ``--actor`` and
``--decision`` are the operator's, and an absent decision is refused rather than
assumed. Its only write is one record file beneath ``--base``.

Exit codes: ``0`` a complete record, ``1`` usage, ``2`` refused (the supplied
evidence could not be bound), ``3`` a bounded non-success record.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

from ..authoring import validation
from ..authoring import validation_plan
from ..authoring import work_reconciliation
from ..twin import memory_twin_link
from ..twin import twin_store

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2
EXIT_NOT_COMPLETE = 3

# The subdirectory the records are written beneath, so a caller-supplied base
# is never written to directly.
_RECORDS_DIRNAME = "reconciliations"

REASON_RESULT_UNREADABLE = "the validation result could not be read"
REASON_PLAN_UNREADABLE = "the validation plan could not be read"
REASON_LINK_UNREADABLE = "the Memory link could not be read"
REASON_RESULT_UNSOUND = "the validation result is not a valid one"
REASON_PLAN_UNSOUND = "the validation plan is not a valid one"
REASON_LINK_NOT_MAPPING = "the Memory link is not a mapping"
REASON_TWIN_UNREADABLE = "the Twin store could not be read"
REASON_TWIN_ABSENT = "no Twin store exists for that workspace"
REASON_BASE_UNUSABLE = "the output base could not be written"


def _read_mapping(path: str, unreadable: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read one supplied document, or return a bounded reason.

    The reason never carries the path: a caller-supplied location is not
    something an operator error message needs to repeat back.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None, unreadable
    if not isinstance(document, dict):
        return None, unreadable
    return document, None


def _write_record(base: str, record: Dict[str, Any]) -> Optional[str]:
    """Write one record beneath ``base``, or return a bounded reason.

    One file per content-addressed identity, named by the identity's body so no
    path separator or drive-relative character enters a filename. An existing
    file holding exactly these bytes is idempotent; one holding anything else is
    a different record under the same identity, which is refused rather than
    overwritten.
    """
    directory = os.path.join(base, _RECORDS_DIRNAME)
    body = record["reconcile_id"].split(":", 1)[1]
    path = os.path.join(directory, body + ".json")
    payload = (work_reconciliation.dumps(record) + "\n").encode("utf-8")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return REASON_BASE_UNUSABLE
    if os.path.exists(path):
        try:
            with open(path, "rb") as handle:
                existing = handle.read()
        except OSError:
            return REASON_BASE_UNUSABLE
        return None if existing == payload else REASON_BASE_UNUSABLE
    try:
        with open(path, "xb") as handle:
            handle.write(payload)
    except OSError:
        return REASON_BASE_UNUSABLE
    return None


def _bundle(argv: argparse.Namespace) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Assemble the eight groups the composition binds, or return a reason."""
    result, reason = _read_mapping(argv.result, REASON_RESULT_UNREADABLE)
    if reason is not None:
        return None, reason
    if validation.validate_result(result) is not None:
        return None, REASON_RESULT_UNSOUND

    plan, reason = _read_mapping(argv.plan, REASON_PLAN_UNREADABLE)
    if reason is not None:
        return None, reason
    plan, reason = validation_plan.migrate_plan(plan)
    if reason is not None:
        return None, REASON_PLAN_UNSOUND

    link, reason = _read_mapping(argv.link, REASON_LINK_UNREADABLE)
    if reason is not None:
        return None, reason
    if not isinstance(link, dict):  # pragma: no cover - _read_mapping guarantees it
        return None, REASON_LINK_NOT_MAPPING

    store, reason = twin_store.load(argv.twin_store, argv.workspace_id)
    if reason is not None:
        return None, REASON_TWIN_UNREADABLE
    if store is None:
        return None, REASON_TWIN_ABSENT

    binding = plan["candidate"]
    revision = store["workspace_revision"]
    freshness = memory_twin_link.resolve_freshness(link, store, argv.workspace_id)
    return {
        "work_package": {"run_id": argv.run_id, "record_id": argv.record_id},
        "change": {
            "intent_delta_id": binding["intent_delta_id"],
            "proposal_id": binding["proposal_id"],
            "edit_id": binding["edit_id"],
            "candidate_id": binding["candidate_id"],
            "binding_fingerprint": binding["binding_fingerprint"],
            "plan_id": plan["plan_id"],
            "policy_version": plan["policy_version"],
        },
        "validation": {
            "result_id": result["result_id"],
            "plan_id": result["plan_id"],
            "candidate_id": result["candidate_id"],
            "policy_version": result["policy_version"],
            "state": result["state"],
            "evidence_complete": result["evidence_complete"],
        },
        "acceptance": {
            "actor": argv.actor,
            "decision": argv.decision,
            "decided_at": argv.decided_at,
            "candidate_id": binding["candidate_id"],
        },
        "application": {"declared": argv.declared},
        "accepted_revision": {
            "workspace_id": argv.workspace_id,
            "scan_generation": argv.accepted_generation,
            "baseline_fingerprint": argv.accepted_fingerprint,
        },
        "observed": {
            "workspace_id": revision["workspace_id"],
            "scan_generation": revision["scan_generation"],
            "baseline_fingerprint": revision["baseline_fingerprint"],
        },
        "freshness": {
            "verdict": freshness["freshness"],
            "entity_id": freshness["entity_id"],
        },
    }, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-work-reconciliation",
        description=(
            "Derive one controlled-change reconciliation record from supplied "
            "evidence. Reads documents and stores, writes one record beneath "
            "the given base, and applies nothing."
        ),
    )
    parser.add_argument("--result", required=True, help="the validation result document")
    parser.add_argument("--plan", required=True, help="the validation plan document")
    parser.add_argument("--link", required=True, help="the Memory-to-Twin link document")
    parser.add_argument("--twin-store", required=True, help="the Twin store base directory")
    parser.add_argument("--workspace-id", required=True, help="the accepted workspace identity")
    parser.add_argument("--accepted-generation", required=True, type=int)
    parser.add_argument("--accepted-fingerprint", required=True)
    parser.add_argument("--run-id", required=True, help="the Memory Agent Run identity")
    parser.add_argument("--record-id", required=True, help="the Memory record identity")
    parser.add_argument("--actor", required=True, help="who recorded the decision")
    parser.add_argument("--decided-at", required=True, help="when the decision was recorded")
    parser.add_argument(
        "--decision",
        default=work_reconciliation.DECISION_ACCEPTED,
        choices=sorted(work_reconciliation.DECISIONS),
    )
    parser.add_argument(
        "--declared",
        required=True,
        choices=sorted(work_reconciliation.APPLICATION_OUTCOMES),
        help="what the operator declares happened to the repository",
    )
    parser.add_argument("--base", required=True, help="where the record is written")
    args = parser.parse_args(argv)

    bundle, reason = _bundle(args)
    if reason is not None:
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_REFUSED

    record, reason = work_reconciliation.build_reconciliation(bundle)
    if reason is not None:
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_REFUSED

    reason = _write_record(args.base, record)
    if reason is not None:
        sys.stderr.write("refused: %s\n" % reason)
        return EXIT_REFUSED

    sys.stdout.write(work_reconciliation.dumps(record) + "\n")
    return EXIT_OK if record["state"] == work_reconciliation.STATE_COMPLETE else EXIT_NOT_COMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
