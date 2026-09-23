"""Deterministic structured report builder for the Human-Readable Code Twin.

Converts scanner evidence (:mod:`hrca.scanner`) and task metadata into a
version-1 workflow report. The builder is read-only with respect to the scanned
repository: it reports observed facts only, never resolves literal relation
targets to definitions or file paths, and it surfaces parse errors and
explicitly-unresolved dynamic imports as limitations rather than guessing.
"""

from __future__ import annotations

from typing import Any, Dict, List

REPORT_VERSION = 1
GENERATOR = "hrca-report"

# Fixed statement of the scanner's known static-analysis bounds, surfaced as a
# limitation in every report so that downstream consumers never mistake a
# literal relation target for a resolved definition.
_STATIC_ANALYSIS_LIMITATION = (
    "Literal relation targets are not resolved to definitions or file paths; "
    "dynamic imports, reflection, dependency injection, and monkey-patching "
    "are reported as unresolved rather than guessed."
)


def _scanner_summary(doc: Dict[str, Any]) -> Dict[str, int]:
    """Return deterministic counts of the scanner's record collections."""
    return {
        "files": len(doc.get("files", [])),
        "symbols": len(doc.get("symbols", [])),
        "relations": len(doc.get("relations", [])),
        "parse_errors": len(doc.get("parse_errors", [])),
        "confidence": len(doc.get("confidence", [])),
    }


def _limitations(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive limitations from scanner evidence (parse errors, unresolved)."""
    limitations: List[Dict[str, Any]] = []

    for err in doc.get("parse_errors", []):
        limitations.append(
            {
                "kind": "parse_error",
                "file": err.get("file"),
                "message": err.get("message"),
            }
        )

    for rel in doc.get("relations", []):
        if rel.get("status") == "unresolved":
            limitations.append(
                {
                    "kind": "unresolved_import",
                    "file": rel.get("file"),
                    "target": rel.get("target"),
                    "reason": rel.get("reason"),
                }
            )

    limitations.append(
        {
            "kind": "static_analysis",
            "detail": _STATIC_ANALYSIS_LIMITATION,
        }
    )

    return limitations


def build_report(
    scanner_doc: Dict[str, Any], task_metadata: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a version-1 structured workflow report.

    ``scanner_doc`` is the document produced by
    :func:`hrca.scanner.scan_directory`. ``task_metadata`` supplies the
    task-level fields: ``task_id``, ``plan`` (a structured list of P2.1 plan
    entries, each with ``step``, ``action``, ``requires_approval``, and
    ``expected_evidence``), ``next_action``, and a ``repository_context``
    mapping with ``branch``, ``head_sha``, and ``base_sha``.
    """
    repo_context = task_metadata.get("repository_context", {})
    report: Dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generator": GENERATOR,
        "task_id": task_metadata.get("task_id"),
        "repository_context": {
            "branch": repo_context.get("branch"),
            "head_sha": repo_context.get("head_sha"),
            "base_sha": repo_context.get("base_sha"),
        },
        "plan": task_metadata.get("plan"),
        "outcome": {
            "status": "no_change",
            "changed_files": [],
        },
        "validation": {
            "scanner_schema_version": scanner_doc.get("schema_version"),
            "scanner_root": scanner_doc.get("root"),
            "scanner_summary": _scanner_summary(scanner_doc),
        },
        "limitations": _limitations(scanner_doc),
        "next_action": task_metadata.get("next_action"),
    }
    return report
