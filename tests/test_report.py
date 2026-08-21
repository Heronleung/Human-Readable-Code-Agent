"""Tests for the deterministic structured report builder (P2.2)."""

from __future__ import annotations

import json
import os
import unittest

from hrca.report import REPORT_VERSION, build_report
from hrca.scanner import scan_directory

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures"))

BASE_SHA = "16a5d217d5751722b98adb251ca63b134badfa86"


def _metadata() -> dict:
    return {
        "task_id": "P2.2",
        "plan": "Read-only validation of the Phase 1 scanner demonstration.",
        "next_action": "Proceed to code-twin content generation on origin/main.",
        "repository_context": {
            "branch": "feat/p2.2-structured-report",
            "head_sha": BASE_SHA,
            "base_sha": BASE_SHA,
        },
    }


def _report() -> dict:
    return build_report(scan_directory(FIXTURES), _metadata())


REQUIRED_FIELDS = {
    "task_id",
    "repository_context",
    "plan",
    "outcome",
    "validation",
    "limitations",
    "next_action",
}


class ReportBuilderTests(unittest.TestCase):
    def test_report_version_and_generator(self):
        report = _report()
        self.assertEqual(report["report_version"], REPORT_VERSION)
        self.assertEqual(REPORT_VERSION, 1)
        self.assertEqual(report["generator"], "hrca-report")

    def test_contains_required_fields(self):
        report = _report()
        self.assertTrue(REQUIRED_FIELDS <= set(report))

    def test_outcome_is_no_change_with_empty_files(self):
        report = _report()
        self.assertEqual(report["outcome"]["status"], "no_change")
        self.assertEqual(report["outcome"]["changed_files"], [])

    def test_repository_context_passthrough(self):
        report = _report()
        ctx = report["repository_context"]
        self.assertEqual(ctx["branch"], "feat/p2.2-structured-report")
        self.assertEqual(ctx["head_sha"], BASE_SHA)
        self.assertEqual(ctx["base_sha"], BASE_SHA)

    def test_scanner_summary_matches_document(self):
        report = _report()
        summary = report["validation"]["scanner_summary"]
        self.assertEqual(summary["files"], 5)
        self.assertEqual(summary["symbols"], 29)
        self.assertEqual(summary["relations"], 20)
        self.assertEqual(summary["parse_errors"], 1)
        self.assertEqual(summary["confidence"], 1)
        self.assertEqual(report["validation"]["scanner_schema_version"], "1.0.0")
        self.assertEqual(
            report["validation"]["scanner_root"], FIXTURES.replace("\\", "/")
        )

    def test_limitations_preserve_parse_error(self):
        report = _report()
        parse_errors = [
            lim for lim in report["limitations"] if lim["kind"] == "parse_error"
        ]
        self.assertEqual(len(parse_errors), 1)
        self.assertEqual(parse_errors[0]["file"], "broken/syntax_error.py")
        self.assertIn("invalid syntax", parse_errors[0]["message"])

    def test_limitations_preserve_unresolved_import(self):
        report = _report()
        unresolved = [
            lim for lim in report["limitations"] if lim["kind"] == "unresolved_import"
        ]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["file"], "app/dynamic.py")
        self.assertIsNone(unresolved[0]["target"])
        self.assertIn("dynamic import", unresolved[0]["reason"])

    def test_no_fabricated_target_paths(self):
        report = _report()
        for lim in report["limitations"]:
            if lim["kind"] != "unresolved_import":
                continue
            target = lim.get("target")
            if target is not None:
                self.assertNotIn("/", target)
                self.assertNotIn("\\", target)
                self.assertFalse(target.endswith(".py"))

    def test_report_is_deterministic(self):
        first = json.dumps(_report(), sort_keys=True)
        second = json.dumps(_report(), sort_keys=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
