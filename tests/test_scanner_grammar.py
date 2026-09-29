"""Grammar attribution, scanner-schema compatibility and the differential (P5.2).

The scanner states which grammar read the source, because the same source can be
a genuine ``SyntaxError`` under one Python grammar and valid under a later one.
These tests hold that attribution to three rules: it is bounded, it is
deterministic, and it is *context* rather than a verdict — a file that fails to
parse stays a ``parse_error``.

The differential compares the scanner run under two interpreters when a second
one is available. It is skipped, with the limitation stated, when it is not: a
missing second interpreter is a gap in coverage, never a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from hrca.source import scanner

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
FIXTURES = os.path.join(REPO, "fixtures")
GRAMMAR_FIXTURES = os.path.join(REPO, "grammar_fixtures")
MANIFEST = os.path.join(GRAMMAR_FIXTURES, "manifest.json")

# Interpreters that might host a second grammar. Discovery never installs one.
_CANDIDATE_INTERPRETERS = (
    "/usr/bin/python3.15",
    "/usr/bin/python3.14",
    "/usr/bin/python3.13",
    "/usr/bin/python3.12",
    "python3.15",
    "python3.14",
    "python3.13",
    "python3.12",
)

_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "generator",
        "grammar",
        "root",
        "files",
        "symbols",
        "relations",
        "parse_errors",
        "confidence",
    }
)

_GRAMMAR_KEYS = frozenset({"implementation", "version"})


def _scan(root: str) -> dict:
    return scanner.scan_directory(root)


def _manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _symbols_in(doc: dict, rel_path: str):
    return sorted(
        rec["id"] for rec in doc["symbols"] if rec.get("file") == rel_path
    )


def _parse_errors_in(doc: dict, rel_path: str):
    return [rec for rec in doc["parse_errors"] if rec.get("file") == rel_path]


def _run_under(interpreter: str, corpus: str) -> dict:
    """Run the scanner under ``interpreter`` and return its parsed document."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    proc = subprocess.run(
        [interpreter, "-m", "hrca", corpus],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "%s exited %d: %s" % (interpreter, proc.returncode, proc.stderr.strip()[:300])
        )
    return json.loads(proc.stdout)


def _other_interpreter():
    """Return ``(path, "major.minor")`` for a second grammar, or ``None``.

    The running interpreter is excluded on purpose: comparing an interpreter
    with itself would prove nothing about grammar dependence.
    """
    running = "%d.%d" % (sys.version_info[0], sys.version_info[1])
    seen = set()
    for candidate in _CANDIDATE_INTERPRETERS:
        path = shutil.which(candidate)
        if path is None and os.path.exists(candidate):
            path = candidate
        if path is None or path in seen:
            continue
        seen.add(path)
        try:
            probe = subprocess.run(
                [path, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode != 0:
            continue
        version = probe.stdout.strip()
        if version and version != running:
            return path, version
    return None


class GrammarContextTests(unittest.TestCase):
    """The attribution is bounded, well-formed and constant within a process."""

    def test_the_document_carries_the_grammar_context(self):
        doc = _scan(FIXTURES)
        self.assertIn("grammar", doc)
        self.assertEqual(_GRAMMAR_KEYS, frozenset(doc["grammar"]))

    def test_the_grammar_context_names_a_grammar_and_not_a_machine(self):
        grammar = _scan(FIXTURES)["grammar"]
        self.assertRegex(grammar["version"], r"^(\d+\.\d+|unknown)$")
        self.assertRegex(grammar["implementation"], r"^([a-z0-9_-]{1,32}|unknown)$")

    def test_the_reported_context_is_the_running_one(self):
        grammar = _scan(FIXTURES)["grammar"]
        self.assertEqual(
            "%d.%d" % (sys.version_info[0], sys.version_info[1]), grammar["version"]
        )

    def test_no_prohibited_environment_fact_reaches_the_attribution(self):
        doc = _scan(FIXTURES)
        attribution = json.dumps(doc["grammar"], sort_keys=True)
        for forbidden in (
            sys.executable,
            sys.prefix,
            sys.base_prefix,
            sys.version,
            sys.argv[0],
            os.path.expanduser("~"),
        ):
            if forbidden:
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, attribution)

    def test_attribution_adds_no_second_environment_fact(self):
        # The attribution is a single bounded block. Apart from the scan root the
        # caller itself supplied, the document carries no machine path.
        doc = _scan(FIXTURES)
        self.assertEqual(_DOCUMENT_KEYS, frozenset(doc))
        without_root = {key: value for key, value in doc.items() if key != "root"}
        home = os.path.expanduser("~")
        if home:
            self.assertNotIn(home, json.dumps(without_root, sort_keys=True))

    def test_the_grammar_context_carries_no_path_or_platform_shape(self):
        grammar = _scan(FIXTURES)["grammar"]
        for value in grammar.values():
            with self.subTest(value=value):
                self.assertNotIn("/", value)
                self.assertNotIn("\\", value)
                self.assertNotIn(":", value)
                self.assertLessEqual(len(value), 32)

    def test_the_document_keys_are_exactly_the_accepted_set(self):
        self.assertEqual(_DOCUMENT_KEYS, frozenset(_scan(FIXTURES)))

    def test_the_context_is_stable_within_a_process(self):
        self.assertEqual(scanner.grammar_context(), scanner.grammar_context())

    def test_repeated_scans_are_byte_identical(self):
        first = json.dumps(_scan(FIXTURES), sort_keys=True)
        second = json.dumps(_scan(FIXTURES), sort_keys=True)
        self.assertEqual(first, second)

    def test_cli_output_is_byte_identical_across_runs(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC
        outputs = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, "-m", "hrca", FIXTURES],
                cwd=REPO,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(0, proc.returncode)
            outputs.append(proc.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn('"grammar"', outputs[0])


class SchemaCompatibilityTests(unittest.TestCase):
    """1.1.0 is additive; anything unknown fails closed and is never repaired."""

    def test_the_scanner_declares_schema_1_1_0(self):
        self.assertEqual("1.1.0", scanner.SCHEMA_VERSION)
        self.assertEqual("1.1.0", _scan(FIXTURES)["schema_version"])

    def test_a_current_document_passes_through_unchanged(self):
        doc = _scan(FIXTURES)
        migrated, error = scanner.migrate_document(doc)
        self.assertIsNone(error)
        self.assertIs(doc, migrated)

    def test_a_1_0_0_document_migrates_with_an_unknown_grammar(self):
        legacy = {"schema_version": "1.0.0", "generator": "hrca-scanner", "files": []}
        migrated, error = scanner.migrate_document(legacy)
        self.assertIsNone(error)
        self.assertEqual("1.1.0", migrated["schema_version"])
        self.assertEqual(scanner.GRAMMAR_UNKNOWN, migrated["grammar"]["implementation"])
        self.assertEqual(scanner.GRAMMAR_UNKNOWN, migrated["grammar"]["version"])

    def test_migration_invents_no_record_and_fabricates_no_grammar(self):
        record = {"record_type": "file", "path": "a.py"}
        legacy = {"schema_version": "1.0.0", "files": [record]}
        migrated, _ = scanner.migrate_document(legacy)
        self.assertEqual([record], migrated["files"])
        # The unknown context must not be replaced by the grammar now running:
        # a 1.0.0 document never recorded one, so nothing is known.
        self.assertNotEqual(
            scanner.grammar_context()["version"], migrated["grammar"]["version"]
        )

    def test_migration_does_not_mutate_the_callers_document(self):
        legacy = {"schema_version": "1.0.0", "files": []}
        scanner.migrate_document(legacy)
        self.assertEqual("1.0.0", legacy["schema_version"])
        self.assertNotIn("grammar", legacy)

    def test_a_newer_version_is_refused(self):
        for version in ("1.2.0", "2.0.0", "9.9.9"):
            with self.subTest(version=version):
                doc, error = scanner.migrate_document({"schema_version": version})
                self.assertIsNone(doc)
                self.assertEqual("schema_version is newer than supported", error)

    def test_a_missing_or_malformed_version_is_refused(self):
        for raw, expected in (
            ({}, "missing schema_version"),
            ({"schema_version": ""}, "missing schema_version"),
            ({"schema_version": 7}, "missing schema_version"),
            ("not a document", "scan document is not a mapping"),
            (None, "scan document is not a mapping"),
        ):
            with self.subTest(raw=raw):
                doc, error = scanner.migrate_document(raw)
                self.assertIsNone(doc)
                self.assertEqual(expected, error)

    def test_the_registry_records_exactly_the_additive_step(self):
        self.assertEqual(["1.0.0"], sorted(scanner.MIGRATIONS))

    def test_a_refusal_names_no_path(self):
        _, error = scanner.migrate_document({"schema_version": "9.9.9"})
        self.assertNotIn("/", error)
        self.assertNotIn("\\", error)


class GoldenCorpusTests(unittest.TestCase):
    """The corpus is held to expectations authored independently of the scanner."""

    def setUp(self):
        self.manifest = _manifest()
        self.case = {
            entry["file"]: entry for entry in self.manifest["cases"]
        }
        self.doc = _scan(GRAMMAR_FIXTURES)
        self.grammar = self.doc["grammar"]

    def test_the_corpus_is_exactly_the_manifest_cases(self):
        self.assertEqual(
            sorted(self.case), sorted(entry["path"] for entry in self.doc["files"])
        )

    def test_a_grammar_independent_case_holds(self):
        entry = self.case["plain.py"]
        self.assertTrue(entry["grammar_independent"])
        self.assertEqual(sorted(entry["symbols"]), _symbols_in(self.doc, "plain.py"))
        self.assertEqual(
            entry["parse_error"], bool(_parse_errors_in(self.doc, "plain.py"))
        )

    def _capable(self) -> bool:
        """Whether the reported grammar can express the manifest's capability."""
        rule = self.manifest["capability"]
        if self.grammar["implementation"] != rule["implementation"]:
            self.skipTest(
                "the manifest states its version rule for %s only; this reported %s, "
                "so the expectation is unverified rather than assumed"
                % (rule["implementation"], self.grammar["implementation"])
            )
        minimum = tuple(int(p) for p in rule["pep695"]["minimum_version"].split("."))
        reported = tuple(int(p) for p in self.grammar["version"].split("."))
        return reported >= minimum

    def test_a_grammar_dependent_case_matches_its_selected_branch(self):
        entry = self.case["pep695.py"]
        self.assertFalse(entry["grammar_independent"])
        expected = (
            entry["when_capable"] if self._capable() else entry["when_not_capable"]
        )
        self.assertEqual(sorted(expected["symbols"]), _symbols_in(self.doc, "pep695.py"))
        self.assertEqual(
            expected["parse_error"], bool(_parse_errors_in(self.doc, "pep695.py"))
        )

    def test_the_unmodelled_alias_is_absent_from_both_branches(self):
        self._capable()  # establishes the interpretation of this context
        names = {rec["name"] for rec in self.doc["symbols"]}
        for name in self.manifest["unmodelled"]["names"]:
            with self.subTest(name=name):
                self.assertNotIn(name, names)

    def test_a_parse_error_keeps_its_bounded_details(self):
        if self._capable():
            self.skipTest("this grammar parses the fixture, so there is no error to inspect")
        errors = _parse_errors_in(self.doc, "pep695.py")
        self.assertEqual(1, len(errors))
        record = errors[0]
        # A real SyntaxError stays exactly what it was: a parse error with its
        # own bounded details, not a grammar verdict.
        self.assertEqual("parse_error", record["record_type"])
        self.assertTrue(record["message"])
        self.assertLessEqual(len(record["message"]), 200)
        for field in ("lineno", "col_offset"):
            self.assertIsInstance(record[field], int)
        # The error is about the file that genuinely cannot be read, and no other.
        self.assertEqual(["pep695.py"], [e["file"] for e in self.doc["parse_errors"]])

    def test_no_source_fact_is_reclassified_as_a_grammar_verdict(self):
        # Nothing in the document may claim a file is grammar-unsupported: the
        # only grammar information is the context block.
        rendered = json.dumps(self.doc, sort_keys=True)
        for invented in ("grammar_unsupported", "unsupported_syntax", "would_parse"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, rendered)

    def test_the_frozen_corpus_records_are_unaffected_by_attribution(self):
        # The accepted corpus keeps every record it had; attribution only adds.
        doc = _scan(FIXTURES)
        self.assertEqual(_DOCUMENT_KEYS, frozenset(doc))
        self.assertTrue(doc["files"] and doc["symbols"] and doc["relations"])


class DifferentialTests(unittest.TestCase):
    """Two grammars over the same source: same evidence, attributable difference."""

    def test_the_same_context_repeats_byte_for_byte(self):
        first = json.dumps(_scan(FIXTURES), sort_keys=True)
        second = json.dumps(_scan(FIXTURES), sort_keys=True)
        self.assertEqual(first, second)
        self.assertEqual(
            _scan(GRAMMAR_FIXTURES), _scan(GRAMMAR_FIXTURES)
        )

    def test_the_accepted_corpus_differs_across_grammars_only_by_context(self):
        other = _other_interpreter()
        if other is None:
            self.skipTest(
                "no second interpreter is installed, so the cross-grammar half of "
                "the differential is unverified; same-grammar coverage above still ran"
            )
        path, version = other
        ours = _scan(FIXTURES)
        theirs = _run_under(path, FIXTURES)

        self.assertNotEqual(
            ours["grammar"],
            theirs["grammar"],
            "the two interpreters report one grammar, so nothing is compared",
        )
        self.assertEqual(version, theirs["grammar"]["version"])

        ours.pop("grammar")
        theirs.pop("grammar")
        self.assertEqual(
            json.dumps(ours, sort_keys=True),
            json.dumps(theirs, sort_keys=True),
            "the accepted corpus must carry identical records in both grammars",
        )

    def test_the_differential_fixture_differs_only_as_its_branches_say(self):
        other = _other_interpreter()
        if other is None:
            self.skipTest(
                "no second interpreter is installed, so the grammar-dependent "
                "difference is unverified; the golden corpus still ran"
            )
        path, version = other
        ours = _scan(GRAMMAR_FIXTURES)
        theirs = _run_under(path, GRAMMAR_FIXTURES)

        self.assertNotEqual(ours["grammar"], theirs["grammar"])
        # The control case is identical in both, so the difference cannot be
        # blamed on the corpus as a whole.
        self.assertEqual(
            _symbols_in(ours, "plain.py"), _symbols_in(theirs, "plain.py")
        )

        # Exactly one file differs, and it differs as a parser outcome.
        changed = {
            path_
            for path_ in ("plain.py", "pep695.py")
            if _symbols_in(ours, path_) != _symbols_in(theirs, path_)
        }
        self.assertEqual({"pep695.py"}, changed)

        def _version(value: str):
            return tuple(int(part) for part in value.split("."))

        ours_version = _version(ours["grammar"]["version"])
        theirs_version = _version(theirs["grammar"]["version"])
        if ours_version > theirs_version:
            newer, newer_version, older = ours, ours_version, theirs
        else:
            newer, newer_version, older = theirs, theirs_version, ours

        rule = _manifest()["capability"]["pep695"]["minimum_version"]
        minimum = tuple(int(part) for part in rule.split("."))
        if newer_version < minimum:
            self.skipTest(
                "neither interpreter reaches %s, so the grammar-dependent branch "
                "could not be observed" % rule
            )
        # The capable grammar reads entities; the older one records a parse error
        # for the same bytes. Nothing is presented as a source defect.
        self.assertTrue(_symbols_in(newer, "pep695.py"))
        self.assertEqual([], _symbols_in(older, "pep695.py"))
        self.assertEqual(1, len(_parse_errors_in(older, "pep695.py")))
        self.assertEqual([], _parse_errors_in(newer, "pep695.py"))

    def test_the_differential_never_claims_cross_version_success(self):
        other = _other_interpreter()
        if other is None:
            self.skipTest("no second interpreter is installed")
        path, _version = other
        doc = _run_under(path, GRAMMAR_FIXTURES)
        rendered = json.dumps(doc, sort_keys=True)
        for invented in ("grammar_unsupported", "will_parse", "other_interpreter"):
            with self.subTest(invented=invented):
                self.assertNotIn(invented, rendered)


if __name__ == "__main__":
    unittest.main()
