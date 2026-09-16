"""Hook collector and importer: the smallest local capture boundary for M4.1.

Two entry points share one spool format:

``collect``
    Invoked once per hook event by the client, with the documented hook
    payload on stdin. It validates the payload, translates it through
    :mod:`hrca.claude_code_hooks` and appends the *already redacted and
    bounded* adapter-neutral events to a capture spool. Raw hook JSON is never
    written anywhere: translation and redaction happen in memory, and only the
    projection the adapter produced reaches the filesystem.

``import``
    Reads a spool, assembles the bounded stream into one session and replays
    it through :func:`hrca.memory.ingest_session`, then persists the resulting
    store through :mod:`hrca.memory_store`. Re-importing the same spool is an
    idempotent no-op because identity is derived from the payload, not from
    the order events happened to be read in.

The spool is transient capture material. It lives outside the repository, it
holds nothing content-bearing, and :func:`cleanup_spool` removes it after
import.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import claude_code_hooks as _adapter
from . import memory as _memory
from . import memory_store as _store

SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.jsonl"
REJECTIONS_FILENAME = "rejections.jsonl"

# Bounded, content-free reasons. A reason never interpolates source content, so
# a rejection record can never leak a payload, a path or a secret.
REASON_INPUT_EMPTY = "hook input is empty"
REASON_INPUT_OVERSIZED = "hook input exceeds the bounded size"
REASON_INPUT_NOT_JSON = "hook input is not valid JSON"
REASON_INPUT_NOT_MAPPING = "hook input is not a JSON mapping"
REASON_INPUT_NO_SESSION = "hook input carries no usable session identity"
REASON_SESSION_MISMATCH = "hook payload belongs to a different session than the spool"
REASON_SESSION_UNREADABLE = "capture session header is unreadable"
REASON_SPOOL_UNREADABLE = "capture spool is not readable"
REASON_SPOOL_EMPTY = "capture spool holds no events"
REASON_NOT_A_SESSION = "captured events do not form an identified session"
GENERATOR_LABEL = "hrca-hook-capture"


def _canonical(obj: Any) -> str:
    """Return the canonical JSON encoding used for every spool record."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def root_digest(root: Optional[str]) -> Optional[str]:
    """Return a non-reversible fingerprint of the session root.

    The spool records only this digest, so the capture material proves which
    root the paths were relativized against without preserving the root.
    """
    if not isinstance(root, str) or not root.strip():
        return None
    return "sha256:" + _memory.sha256_hex(root.strip().encode("utf-8"))


# -- spool writing -------------------------------------------------------


def _append_line(path: str, record: Any) -> Optional[str]:
    """Append one canonical JSON line to ``path``; return an error or ``None``."""
    try:
        line = _canonical(record) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return REASON_SPOOL_UNREADABLE
    return None


def ensure_session_header(
    spool_dir: str,
    session_id: str,
    root: Optional[str],
    project: Optional[Dict[str, Any]] = None,
    work_package: Optional[Dict[str, Any]] = None,
    run_key: Any = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Create the spool's session header once, then verify it on every use.

    A spool belongs to exactly one session. A payload that names a different
    session is refused rather than appended, so two runs can never share a
    store.
    """
    path = os.path.join(spool_dir, SESSION_FILENAME)
    header = {
        "schema_version": _memory.MEMORY_SCHEMA_VERSION,
        "record_kind": "capture_session",
        "generator": GENERATOR_LABEL,
        "adapter": _adapter.ADAPTER_NAME,
        "session_id": session_id,
        "root_digest": root_digest(root),
        "project": project if isinstance(project, dict) else None,
        "work_package": work_package if isinstance(work_package, dict) else None,
        "run_key": run_key if isinstance(run_key, str) else None,
    }
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, ValueError):
            return None, REASON_SESSION_UNREADABLE
        if not isinstance(existing, dict):
            return None, REASON_SESSION_UNREADABLE
        if existing.get("session_id") != session_id:
            return None, REASON_SESSION_MISMATCH
        return existing, None
    error = _append_line(path, header)
    if error is not None:
        return None, error
    return header, None


def collect_hook_input(
    raw: bytes,
    spool_dir: str,
    root: Optional[str] = None,
    project: Optional[Dict[str, Any]] = None,
    work_package: Optional[Dict[str, Any]] = None,
    run_key: Any = None,
    block_events: Sequence[str] = (),
    policy: Optional[_memory.PrivacyPolicy] = None,
) -> int:
    """Translate one hook payload into the spool; return the process exit code.

    The exit code is the hook's documented decision channel, so it is chosen
    conservatively: ``0`` for every normal outcome including a refused payload,
    ``2`` only for an event the capture configuration explicitly blocks, and
    ``1`` for an internal failure that has no blocking meaning for any event.
    """
    policy = policy or _memory.DEFAULT_POLICY
    rejections_path = os.path.join(spool_dir, REJECTIONS_FILENAME)

    def reject(reason: str) -> None:
        _append_line(
            rejections_path,
            {
                "record_kind": "capture_rejection",
                "reason": reason,
                "bytes": len(raw),
                "digest": "sha256:" + _memory.sha256_hex(raw),
            },
        )

    if not raw.strip():
        reject(REASON_INPUT_EMPTY)
        return 0
    if len(raw) > _adapter.MAX_HOOK_INPUT_BYTES:
        reject(REASON_INPUT_OVERSIZED)
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        reject(REASON_INPUT_NOT_JSON)
        return 0
    if not isinstance(payload, dict):
        reject(REASON_INPUT_NOT_MAPPING)
        return 0

    session_id = _adapter.safe_token(payload.get("session_id"))
    if session_id is None:
        reject(REASON_INPUT_NO_SESSION)
        return 0

    if root is None:
        candidate = payload.get("cwd")
        root = candidate.strip() if isinstance(candidate, str) else None

    _, error = ensure_session_header(
        spool_dir, session_id, root, project, work_package, run_key
    )
    if error is not None:
        reject(error)
        return 0

    events = _adapter.translate_hook_event(payload, root, policy)
    name = _adapter.hook_event_name(payload)
    blocked = name is not None and name in set(block_events)
    if blocked and events:
        # The block is the reason the run never concluded, so it is recorded as
        # an explicit decision rather than left as an unexplained absence.
        events[-1].setdefault("decisions", []).append(
            {
                "summary": "prompt blocked by the capture configuration",
                "source_id": "capture-block:" + str(name),
            }
        )

    events_path = os.path.join(spool_dir, EVENTS_FILENAME)
    for event in events:
        error = _append_line(events_path, event)
        if error is not None:
            return 1
    return 2 if blocked else 0


# -- spool reading -------------------------------------------------------


def read_spool(
    spool_dir: str,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
    """Return ``(header, events, rejections, error)`` for a capture spool.

    A payload the collector refused writes a rejection but no session header,
    so the record files are always read even when the header is absent: a
    refusal must stay reportable rather than disappearing from the account.
    """
    events_path = os.path.join(spool_dir, EVENTS_FILENAME)
    rejections_path = os.path.join(spool_dir, REJECTIONS_FILENAME)
    header_path = os.path.join(spool_dir, SESSION_FILENAME)

    if not any(
        os.path.isfile(path) for path in (header_path, events_path, rejections_path)
    ):
        return None, [], [], REASON_SPOOL_UNREADABLE

    header: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    if os.path.isfile(header_path):
        try:
            with open(header_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            return None, [], [], REASON_SESSION_UNREADABLE
        if not isinstance(loaded, dict):
            return None, [], [], REASON_SESSION_UNREADABLE
        header = loaded

    events: List[Dict[str, Any]] = []
    rejections: List[Dict[str, Any]] = []
    for path, target in ((events_path, events), (rejections_path, rejections)):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        target.append(record)
        except OSError:
            return header, events, rejections, REASON_SPOOL_UNREADABLE
    return header, events, rejections, error


def import_spool(
    spool_dir: str,
    policy: Optional[_memory.PrivacyPolicy] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    """Replay a capture spool into one finalized memory store.

    Returns ``(store, error, report)``. Exactly one of ``store`` and ``error``
    is ``None``. The store is not persisted here; the caller owns storage so
    that a dry run can inspect the result first.
    """
    policy = policy or _memory.DEFAULT_POLICY
    header, events, rejections, error = read_spool(spool_dir)
    if error is not None:
        return None, error, {}
    if header is None:
        return None, REASON_SPOOL_UNREADABLE, {}
    if not events:
        return None, REASON_SPOOL_EMPTY, {}

    session, report = _adapter.assemble_session(
        events,
        project=header.get("project"),
        work_package=header.get("work_package"),
        run_key=header.get("run_key"),
    )
    report["capture_rejections"] = len(rejections)
    report["root_digest"] = header.get("root_digest")
    if session is None:
        return None, REASON_NOT_A_SESSION, report

    store, ingest_error, outcomes = _memory.ingest_session(session, policy)
    if store is None:
        return None, ingest_error, report

    counts: Dict[str, int] = {}
    for outcome in outcomes:
        status = str(outcome.get("status"))
        counts[status] = counts.get(status, 0) + 1
    report["ingest_outcomes"] = dict(sorted(counts.items()))
    return store, None, report


def cleanup_spool(spool_dir: str) -> bool:
    """Remove a capture spool entirely; return True when nothing remains."""
    if not os.path.exists(spool_dir):
        return True
    resolved = os.path.abspath(spool_dir)
    # Refuse to remove anything that is not clearly a capture spool, so a
    # mistyped argument can never delete a repository or a home directory.
    if not os.path.isfile(os.path.join(resolved, SESSION_FILENAME)):
        return False
    shutil.rmtree(resolved, ignore_errors=True)
    return not os.path.exists(resolved)


# -- command line --------------------------------------------------------


def _project_from_args(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if not args.project_id:
        return None
    return {
        "source_id": args.project_id,
        "name": args.project_name or args.project_id,
    }


def _work_package_from_args(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if not args.work_package_id:
        return None
    return {
        "source_id": args.work_package_id,
        "title": args.work_package_title or args.work_package_id,
    }


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--work-package-id", default=None)
    parser.add_argument("--work-package-title", default=None)


def _cmd_collect(args: argparse.Namespace) -> int:
    raw = _read_stdin(_adapter.MAX_HOOK_INPUT_BYTES + 1)
    return collect_hook_input(
        raw,
        args.spool,
        root=args.root,
        project=_project_from_args(args),
        work_package=_work_package_from_args(args),
        run_key=args.run_key,
        block_events=tuple(args.block_event or ()),
    )


def _read_stdin(limit: int) -> bytes:
    """Read at most ``limit`` bytes of hook input from stdin."""
    try:
        return sys.stdin.buffer.read(limit)
    except (AttributeError, OSError):
        return b""


def _cmd_import(args: argparse.Namespace) -> int:
    store, error, report = import_spool(args.spool)
    if store is None:
        _emit({"status": "error", "reason": error, "report": report})
        return 1
    run_id = store["agent_run"]["id"]
    if args.base is not None:
        save_error = _store.save(args.base, run_id, store)
        if save_error is not None:
            _emit({"status": "error", "reason": save_error, "report": report})
            return 1
    _emit(
        {
            "status": "imported" if args.base is not None else "validated",
            "run_id": run_id,
            "state": _memory.run_state(store),
            "success": _memory.is_success(store),
            "events": len(_memory.event_ids(store)),
            "evidence": len(_memory.evidence_ids(store)),
            "report": report,
        }
    )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    header, events, rejections, error = read_spool(args.spool)
    if error is not None:
        _emit({"status": "error", "reason": error})
        return 1
    header = header or {}
    store, import_error, report = import_spool(args.spool)
    _emit(
        {
            "status": "ok" if store is not None else "error",
            "reason": import_error,
            "session_id": header.get("session_id"),
            "root_digest": header.get("root_digest"),
            "captured_events": len(events),
            "capture_rejections": len(rejections),
            "state": _memory.run_state(store) if store is not None else None,
            "success": _memory.is_success(store) if store is not None else None,
            "report": report,
        }
    )
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    removed = cleanup_spool(args.spool)
    _emit({"status": "removed" if removed else "refused", "spool": args.spool})
    return 0 if removed else 1


def _emit(record: Dict[str, Any]) -> None:
    sys.stdout.write(_canonical(record) + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-hook-capture",
        description="Collect Claude Code hook input into M4.1 Developer Memory.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect_parser = sub.add_parser("collect", help="collect one hook payload from stdin")
    collect_parser.add_argument("--spool", required=True)
    collect_parser.add_argument("--root", default=None)
    collect_parser.add_argument("--run-key", default=None)
    collect_parser.add_argument(
        "--block-event",
        action="append",
        default=None,
        help="block (exit 2) the named hook event; repeatable",
    )
    _add_identity_arguments(collect_parser)
    collect_parser.set_defaults(handler=_cmd_collect)

    import_parser = sub.add_parser("import", help="replay a spool into a store")
    import_parser.add_argument("--spool", required=True)
    import_parser.add_argument("--base", default=None)
    import_parser.set_defaults(handler=_cmd_import)

    report_parser = sub.add_parser("report", help="summarize a spool")
    report_parser.add_argument("--spool", required=True)
    report_parser.set_defaults(handler=_cmd_report)

    cleanup_parser = sub.add_parser("cleanup", help="remove a capture spool")
    cleanup_parser.add_argument("--spool", required=True)
    cleanup_parser.set_defaults(handler=_cmd_cleanup)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
