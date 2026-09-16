"""Command-line interface for the offline Developer Memory contract (M4.1).

This is a bounded, offline developer tool. It replays fixture *sessions* and
inspects fixture *stores* so the contract's invariants can be reviewed as local
evidence. It is deliberately not a Memory UI and not a capture path:

* it never installs a hook, never reads a real session, never parses a
  transcript, never opens a socket and never reaches a provider or credential;
* it reads only the explicit fixture path it is given and writes only to stdout.

Commands
--------

``replay <session.json>``
    Normalize and replay one bounded source session; print its canonical store.
``summary <fixture-dir>``
    Print one deterministic line per session fixture with its terminal state,
    counts, rejection reasons and quarantines.
``verify <fixture-dir>``
    Replay every session fixture and prove byte-stability, one logical event per
    identity and an unchanged terminal state across re-runs.
``migrate <store.json>``
    Load and migrate one store fixture; report the migrated state or the
    explicit blocker.
``project --base <dir>``
    Project the evidence-linked documents of one or more stored runs (M4.3).
    Reads normalized stores through the storage owner and writes canonical JSON
    to stdout; it never reads a raw hook payload, a transcript or a log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import memory
from . import memory_docs
from . import memory_store


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _session_files(root: str) -> List[str]:
    """Return every session fixture under ``root/sessions``, sorted by name."""
    sessions = os.path.join(root, "sessions")
    if not os.path.isdir(sessions):
        return []
    return [
        os.path.join(sessions, name)
        for name in sorted(os.listdir(sessions))
        if name.endswith(".json")
    ]


def _replay(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    session = _read_json(path)
    store, err, _ = memory.ingest_session(session)
    return store, err


def _cmd_replay(args: argparse.Namespace) -> int:
    store, err = _replay(args.session)
    if store is None:
        sys.stderr.write(f"session refused: {err}\n")
        return 1
    json.dump(store, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    for path in _session_files(args.fixture_dir):
        store, err = _replay(path)
        name = os.path.relpath(path, args.fixture_dir)
        if store is None:
            sys.stdout.write(f"{name}\trefused\t{err}\n")
            continue
        result = memory.replay_result(store)
        reasons = ",".join(result["rejection_reasons"]) or "-"
        sys.stdout.write(
            "\t".join(
                [
                    name,
                    result["state"],
                    f"events={len(result['event_ids'])}",
                    f"evidence={len(result['evidence_ids'])}",
                    f"decisions={len(result['decision_ids'])}",
                    f"rejections={len(result['rejection_reasons'])}",
                    f"quarantines={len(result['quarantine_ids'])}",
                    f"reasons={reasons}",
                ]
            )
            + "\n"
        )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Prove replay determinism over the whole fixture corpus."""
    failures: List[str] = []
    for path in _session_files(args.fixture_dir):
        name = os.path.relpath(path, args.fixture_dir)
        first, err = _replay(path)
        if first is None:
            failures.append(f"{name}: session refused: {err}")
            continue
        second, err = _replay(path)
        if second is None:
            failures.append(f"{name}: second replay refused: {err}")
            continue
        if memory.dumps(first) != memory.dumps(second):
            failures.append(f"{name}: replay is not byte-stable")
        ids = memory.event_ids(first)
        if len(ids) != len(set(ids)):
            failures.append(f"{name}: more than one logical event per identity")
        if memory.run_state(first) != memory.run_state(second):
            failures.append(f"{name}: terminal state drifted")
        sys.stdout.write(f"{name}\t{memory.run_state(first)}\tok\n")

    if failures:
        for failure in failures:
            sys.stderr.write(f"FAIL {failure}\n")
        return 1
    sys.stdout.write(f"verified {len(_session_files(args.fixture_dir))} sessions\n")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    raw = _read_json(args.store)
    store, err = memory.migrate_memory(raw)
    if store is None:
        sys.stdout.write(f"blocked\t{err}\n")
        return 1
    sys.stdout.write(f"migrated\t{memory.run_state(store)}\t{store.get('schema_version')}\n")
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    """Project the evidence-linked documents of one or more stored runs."""
    if args.run:
        run_ids = list(args.run)
    else:
        run_ids = [s["run_id"] for s in memory_store.list_runs(args.base)]
    if args.limit is not None:
        run_ids = run_ids[: args.limit]

    stores: List[Dict[str, Any]] = []
    for run_id in run_ids:
        store, err = memory_store.load(args.base, run_id)
        if store is None:
            sys.stdout.write(f"blocked\t{run_id}\t{err or 'no such run store'}\n")
            return 1
        stores.append(store)
    if not stores:
        sys.stdout.write(f"blocked\tno run stores under {args.base}\n")
        return 1

    document_sets, err, _ = memory_docs.project_stores(
        stores, evidence_origin=args.origin, document_types=args.document
    )
    if document_sets is None:
        sys.stdout.write(f"blocked\t{err}\n")
        return 1
    sys.stdout.write(memory_docs.render_sets(document_sets) + "\n")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one memory-contract command. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="hrca-memory",
        description="Replay bounded offline Developer Memory fixtures.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    replay_parser = sub.add_parser("replay", help="replay one session fixture")
    replay_parser.add_argument("session", help="path to a session fixture JSON file")
    replay_parser.set_defaults(func=_cmd_replay)

    summary_parser = sub.add_parser("summary", help="summarize a fixture corpus")
    summary_parser.add_argument("fixture_dir", help="path to a fixtures/memory directory")
    summary_parser.set_defaults(func=_cmd_summary)

    verify_parser = sub.add_parser("verify", help="prove replay determinism")
    verify_parser.add_argument("fixture_dir", help="path to a fixtures/memory directory")
    verify_parser.set_defaults(func=_cmd_verify)

    migrate_parser = sub.add_parser("migrate", help="load and migrate one store fixture")
    migrate_parser.add_argument("store", help="path to a store fixture JSON file")
    migrate_parser.set_defaults(func=_cmd_migrate)

    project_parser = sub.add_parser(
        "project", help="project evidence-linked documents for stored runs"
    )
    project_parser.add_argument("--base", required=True, help="store base directory")
    project_parser.add_argument(
        "--run", action="append", default=None, help="run id to project; repeatable"
    )
    project_parser.add_argument("--limit", type=int, default=None)
    project_parser.add_argument(
        "--document", action="append", default=None,
        choices=list(memory_docs.DOCUMENT_TYPES), help="document type; repeatable",
    )
    project_parser.add_argument(
        "--origin", default=None, choices=["live", "offline"],
        help="declare the capture origin; recorded as caller-declared, not stored",
    )
    project_parser.set_defaults(func=_cmd_project)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
