"""Command-line operator boundary for Developer Memory packages (M4.5/v2a).

This is the offline operator surface for the two package profiles. It is
deliberately not a UI and not a service:

* it reads and writes only the paths it is given, opens no socket and reaches no
  provider, credential or transcript;
* it never replaces an active store unless a caller both passes ``--apply`` *and*
  echoes back the exact active identity the plan reported, and even then the
  prior store is preserved as rollback material first;
* it writes nothing until a package has fully validated.

Commands
--------

``export --base <dir> --run <id> --out <path>``
    Write a least-disclosure shareable package of one run's projections.
``backup --base <dir> --run <id> --out <path>``
    Write a lossless local-sensitive package of one run's store.
``inspect <package>``
    Validate one package and print a bounded, content-free summary.
``recover --package <path> --active <dir> --staging <dir>``
    Validate, stage, verify and print a restore plan. Changes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, Optional, Sequence

from . import memory_package as package
from . import memory_store


def _emit(record: Dict[str, Any]) -> None:
    sys.stdout.write(package.render(record) + "\n")


def _load(base_dir: str, run_id: Optional[str]) -> "tuple":
    """Return ``(store, run_id, error)`` for one run of the given base."""
    if not isinstance(run_id, str) or not run_id:
        runs = memory_store.list_runs(base_dir)
        if not runs:
            return None, None, "no run store was found under the given base"
        run_id = runs[0]["run_id"]
    store, error = memory_store.load(base_dir, run_id)
    if store is None:
        return None, run_id, error or "no such run store"
    return store, run_id, None


def _write(args: argparse.Namespace, profile: str, builder) -> int:
    store, run_id, error = _load(args.base, args.run)
    if store is None:
        _emit({"status": "blocked", "reason": error})
        return 1
    entries, entry_error = builder(store, run_id)
    if entries is None:
        _emit({"status": "blocked", "reason": entry_error, "run_id": run_id})
        return 1
    write_error = package.write_package(args.out, profile, entries, args.created_at)
    if write_error is not None:
        _emit({"status": "blocked", "reason": write_error, "run_id": run_id})
        return 1
    summary = package.inspect(args.out)
    summary["status"] = "written"
    summary["run_id"] = run_id
    summary["out"] = args.out
    _emit(summary)
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    return _write(args, package.PROFILE_EXPORT, package.export_entries)


def _cmd_backup(args: argparse.Namespace) -> int:
    return _write(args, package.PROFILE_BACKUP, package.backup_entries)


def _cmd_inspect(args: argparse.Namespace) -> int:
    summary = package.inspect(args.package)
    _emit(summary)
    return 0 if summary.get("status") == "ok" else 1


def _cmd_recover(args: argparse.Namespace) -> int:
    staging, manifest, error = package.stage_package(args.package, args.staging)
    if staging is None:
        _emit({"status": "blocked", "reason": error})
        return 1
    if manifest.get("profile") != package.PROFILE_BACKUP:
        # An export is a projection: it is shareable precisely because it cannot
        # restore anything, so it is refused as a recovery source.
        _emit({"status": "blocked", "reason": "only a backup package can restore a run"})
        return 1
    plan, plan_error = package.plan_restore(staging, args.active)
    if plan is None:
        _emit({"status": "blocked", "reason": plan_error})
        return 1
    if not args.apply:
        plan["status"] = "planned"
        plan["staging_dir"] = staging
        _emit(plan)
        return 0 if plan.get("restorable") else 1

    run_id = args.run or (plan["runs"][0]["run_id"] if plan["runs"] else None)
    entry = next((item for item in plan["runs"] if item["run_id"] == run_id), None)
    if entry is None:
        _emit({"status": "blocked", "reason": package.REASON_NOT_PLANNED})
        return 1
    if args.expected != entry["active_identity"]:
        # The caller must echo the identity the plan reported; anything else
        # means the active store moved on and the plan is stale.
        _emit(
            {
                "status": "blocked",
                "reason": package.REASON_ACTIVE_MISMATCH,
                "active_identity": entry["active_identity"],
            }
        )
        return 1
    result, apply_error = package.apply_restore(
        staging, args.active, args.expected, run_id, args.rollback
    )
    if result is None:
        _emit({"status": "blocked", "reason": apply_error, "run_id": run_id})
        return 1
    result["status"] = "restored"
    _emit(result)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hrca-memory-package",
        description="Export, back up and recover Developer Memory packages offline.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_write_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--base", required=True, help="Memory store base directory")
        target.add_argument("--run", default=None, help="run id; defaults to the first")
        target.add_argument("--out", required=True, help="package file to write")
        target.add_argument(
            "--created-at", default=None,
            help="optional creation instant; omitted keeps packaging byte-identical",
        )

    export_parser = sub.add_parser("export", help="write a shareable export package")
    _add_write_arguments(export_parser)
    export_parser.set_defaults(handler=_cmd_export)

    backup_parser = sub.add_parser("backup", help="write a local-sensitive backup")
    _add_write_arguments(backup_parser)
    backup_parser.set_defaults(handler=_cmd_backup)

    inspect_parser = sub.add_parser("inspect", help="validate one package")
    inspect_parser.add_argument("package", help="package file to validate")
    inspect_parser.set_defaults(handler=_cmd_inspect)

    recover_parser = sub.add_parser("recover", help="stage, verify and plan a restore")
    recover_parser.add_argument("--package", required=True, help="backup package")
    recover_parser.add_argument("--active", required=True, help="active store base")
    recover_parser.add_argument("--staging", required=True, help="staging root")
    recover_parser.add_argument(
        "--apply", action="store_true",
        help="replace the active store; requires --expected to match the plan",
    )
    recover_parser.add_argument("--expected", default=None)
    recover_parser.add_argument("--run", default=None)
    recover_parser.add_argument(
        "--rollback", default=None, help="directory to preserve the prior store in"
    )
    recover_parser.set_defaults(handler=_cmd_recover)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
