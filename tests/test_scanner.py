"""Tests for the deterministic scanner (Phase 1 baseline)."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout

from hrca.scanner import scan_directory

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures"))

EXPECTED_FILES = {
    "app/main.py",
    "app/service.py",
    "app/dynamic.py",
    "tests/test_service.py",
    "broken/syntax_error.py",
}


def _scan() -> dict:
    return scan_directory(FIXTURES)


class ScannerTests(unittest.TestCase):
    def test_emits_expected_files(self):
        doc = _scan()
        self.assertEqual({f["path"] for f in doc["files"]}, EXPECTED_FILES)

    def test_syntax_error_does_not_stop_scan(self):
        doc = _scan()
        errors = [e for e in doc["parse_errors"] if e["file"] == "broken/syntax_error.py"]
        self.assertEqual(len(errors), 1)
        self.assertIn("lineno", errors[0])

        module_ids = {s["id"] for s in doc["symbols"] if s["kind"] == "module"}
        self.assertIn("app.main", module_ids)
        self.assertIn("app.service", module_ids)
        self.assertIn("app.dynamic", module_ids)
        self.assertIn("tests.test_service", module_ids)
        # A file that fails to parse produces no module symbol.
        self.assertNotIn("broken.syntax_error", module_ids)

    def test_dynamic_import_is_explicitly_unresolved(self):
        doc = _scan()
        dynamic = [
            r
            for r in doc["relations"]
            if r["kind"] == "imports" and r["status"] == "unresolved"
        ]
        self.assertEqual(len(dynamic), 1)
        rel = dynamic[0]
        self.assertEqual(rel["file"], "app/dynamic.py")
        self.assertIsNone(rel["target"])
        self.assertEqual(rel["confidence"], "low")
        self.assertIn("dynamic import", rel["reason"])

        notes = [c for c in doc["confidence"] if c["item_id"] == rel["id"]]
        self.assertEqual(len(notes), 1)

    def test_stable_ids_across_rescans(self):
        first = json.dumps(_scan(), sort_keys=True)
        second = json.dumps(_scan(), sort_keys=True)
        self.assertEqual(first, second)

    def test_symbol_kinds_are_covered(self):
        doc = _scan()
        kinds = {s["kind"] for s in doc["symbols"]}
        self.assertTrue(
            {"module", "class", "function", "async_function", "parameter", "variable"}
            <= kinds
        )

    def test_relation_kinds_are_covered(self):
        doc = _scan()
        kinds = {r["kind"] for r in doc["relations"]}
        self.assertTrue({"imports", "calls", "returns", "raises", "inherits"} <= kinds)

    def test_decorators_recorded(self):
        doc = _scan()
        version = [s for s in doc["symbols"] if s["name"] == "version"]
        self.assertTrue(version)
        self.assertEqual(version[0]["decorators"], ["staticmethod"])

    def test_no_fabricated_target_paths(self):
        doc = _scan()
        self.assertTrue(doc["relations"])
        for rel in doc["relations"]:
            target = rel["target"]
            if target is not None:
                self.assertNotIn("/", target)
                self.assertNotIn("\\", target)
                self.assertFalse(target.endswith(".py"))
            self.assertIn("source_range", rel)

    def test_relation_ids_are_unique(self):
        doc = _scan()
        ids = [r["id"] for r in doc["relations"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_symbol_ids_are_unique(self):
        doc = _scan()
        ids = [s["id"] for s in doc["symbols"]]
        self.assertEqual(len(ids), len(set(ids)))


class CliTests(unittest.TestCase):
    def test_cli_emits_valid_json(self):
        from hrca.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main([FIXTURES])
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["schema_version"], "1.0.0")
        self.assertEqual(doc["generator"], "hrca-scanner")
        self.assertEqual(doc["root"], FIXTURES.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
