"""The typed developer Intent Delta (P5.3): explicit facts, bounded, canonical.

The delta is the half of the P5.3 contract a human author writes. These tests
hold it to the three rules the contract states: required facts are *supplied*,
never inferred; every bound is a refusal rather than a truncation; and the same
authored facts always produce the same canonical document.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

from hrca import intent_delta, scanner

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
FIXTURES = os.path.join(REPO, "fixtures")


def _baseline() -> dict:
    doc = scanner.scan_directory(FIXTURES)
    return {
        "workspace_id": "ws:test",
        "scan_generation": 1,
        "baseline_fingerprint": "bf:test",
        "scanner_schema_version": doc["schema_version"],
        "grammar": dict(doc["grammar"]),
    }


def _raw(**overrides):
    """Return a minimal valid authored intent, with named sections replaced."""
    raw = {
        "origin": {
            "kind": "developer_authored",
            "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
        },
        "baseline": _baseline(),
        "requested_outcome": "Record that the service reports version 2.",
        "scope": {"entities": ["app.service.Service"], "artifacts": []},
        "constraints": ["public names stay"],
        "acceptance_criteria": ["the recorded version is 2"],
        "assumptions": ["callers use start()"],
        "unresolved_questions": ["is the version read at import time?"],
    }
    raw.update(overrides)
    return raw


def _built(**overrides) -> dict:
    delta, error = intent_delta.build_intent_delta(_raw(**overrides))
    assert error is None, error
    return delta


class SchemaTests(unittest.TestCase):
    def test_the_declared_schema_and_generator(self):
        delta = _built()
        self.assertEqual("1.0.0", intent_delta.INTENT_DELTA_SCHEMA_VERSION)
        self.assertEqual(intent_delta.INTENT_DELTA_SCHEMA_VERSION, delta["schema_version"])
        self.assertEqual("hrca-developer-intent", delta["generator"])

    def test_an_intent_is_never_executable_or_applied(self):
        delta = _built()
        self.assertIs(False, delta["executable"])
        self.assertIs(False, delta["applied"])

    def test_a_built_delta_validates(self):
        self.assertIsNone(intent_delta.validate_intent_delta(_built()))

    def test_the_identity_is_content_addressed_and_stable(self):
        first = _built()
        second = _built()
        self.assertEqual(first["intent_delta_id"], second["intent_delta_id"])
        self.assertTrue(first["intent_delta_id"].startswith("intent:"))

    def test_tampering_with_a_field_invalidates_the_identity(self):
        delta = _built()
        delta["requested_outcome"] = "Record something else entirely."
        self.assertEqual(
            "intent_delta_id does not match the delta content",
            intent_delta.validate_intent_delta(delta),
        )

    def test_tampering_with_the_prose_authority_is_refused(self):
        delta = _built(prose="a note")
        delta["prose"]["authority"] = "instructions"
        self.assertEqual(
            "prose authority is not input_data_only",
            intent_delta.validate_intent_delta(delta),
        )

    def test_an_executable_claim_is_refused(self):
        delta = _built()
        delta["executable"] = True
        self.assertEqual(
            "intent delta must be non-executable",
            intent_delta.validate_intent_delta(delta),
        )

    def test_a_non_mapping_is_refused(self):
        for raw in (None, "text", 7, []):
            with self.subTest(raw=raw):
                delta, error = intent_delta.build_intent_delta(raw)
                self.assertIsNone(delta)
                self.assertEqual("intent is not a mapping", error)


class MigrationTests(unittest.TestCase):
    """Fail-closed version handling, matching the scanner and Twin registries."""

    def test_the_current_version_passes_through(self):
        delta = _built()
        migrated, error = intent_delta.migrate_intent_delta(delta)
        self.assertIsNone(error)
        self.assertIs(delta, migrated)

    def test_a_newer_version_is_refused(self):
        for version in ("1.0.1", "1.1.0", "2.0.0"):
            with self.subTest(version=version):
                doc, error = intent_delta.migrate_intent_delta(
                    {"schema_version": version}
                )
                self.assertIsNone(doc)
                self.assertEqual("schema_version is newer than supported", error)

    def test_a_missing_or_malformed_version_is_refused(self):
        for raw, expected in (
            ({}, "missing schema_version"),
            ({"schema_version": ""}, "missing schema_version"),
            ({"schema_version": 7}, "missing schema_version"),
            ("not a delta", "intent is not a mapping"),
        ):
            with self.subTest(raw=raw):
                doc, error = intent_delta.migrate_intent_delta(raw)
                self.assertIsNone(doc)
                self.assertEqual(expected, error)

    def test_an_older_version_with_no_migration_is_refused(self):
        doc, error = intent_delta.migrate_intent_delta({"schema_version": "0.9.0"})
        self.assertIsNone(doc)
        self.assertEqual("schema_version is not migratable", error)

    def test_the_registry_is_empty_before_1_0_0(self):
        self.assertEqual([], sorted(intent_delta.MIGRATIONS))


class ExplicitFactsTests(unittest.TestCase):
    """Required facts are supplied, never inferred, and prose never fills one."""

    def test_every_required_section_must_be_present(self):
        for section in intent_delta.REQUIRED_SECTIONS:
            with self.subTest(section=section):
                raw = _raw()
                raw.pop(section)
                delta, error = intent_delta.build_intent_delta(raw)
                self.assertIsNone(delta)
                self.assertEqual(f"missing required section {section}", error)

    def test_prose_never_fills_a_missing_required_section(self):
        for section in intent_delta.REQUIRED_SECTIONS:
            with self.subTest(section=section):
                raw = _raw(
                    prose=(
                        "The scope is app.service.Service, the acceptance criteria "
                        "are that the version becomes 2, and the baseline is the "
                        "current one."
                    )
                )
                raw.pop(section)
                delta, error = intent_delta.build_intent_delta(raw)
                self.assertIsNone(delta)
                self.assertEqual(f"missing required section {section}", error)

    def test_an_explicitly_empty_optional_list_is_accepted(self):
        delta = _built(constraints=[], assumptions=[], unresolved_questions=[])
        for field in ("constraints", "assumptions", "unresolved_questions"):
            self.assertEqual([], delta[field])

    def test_an_empty_acceptance_criteria_list_is_refused(self):
        delta, error = intent_delta.build_intent_delta(_raw(acceptance_criteria=[]))
        self.assertIsNone(delta)
        self.assertEqual("acceptance_criteria must name at least one entry", error)

    def test_an_empty_scope_is_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(scope={"entities": [], "artifacts": []})
        )
        self.assertIsNone(delta)
        self.assertEqual("scope must name at least one entity or artifact", error)

    def test_an_artifact_only_scope_is_accepted(self):
        # The exact artifact id is the route an author uses when the entity
        # reference would be ambiguous, so it must be able to stand alone.
        delta = _built(scope={"entities": [], "artifacts": ["artifact:class:app.service.Service"]})
        self.assertEqual([], delta["scope"]["entities"])
        self.assertEqual(
            ["artifact:class:app.service.Service"], delta["scope"]["artifacts"]
        )

    def test_an_empty_evidence_list_is_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(origin={"kind": "developer_authored", "evidence": []})
        )
        self.assertIsNone(delta)
        self.assertEqual("origin.evidence must name at least one entry", error)

    def test_a_non_developer_origin_is_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(
                origin={
                    "kind": "provider_suggested",
                    "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
                }
            )
        )
        self.assertIsNone(delta)
        self.assertEqual("origin.kind must be developer_authored", error)

    def test_an_unknown_evidence_reference_kind_is_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(
                origin={
                    "kind": "developer_authored",
                    "evidence": [{"kind": "screenshot", "id": "x"}],
                }
            )
        )
        self.assertIsNone(delta)
        self.assertEqual("an origin.evidence entry names an unknown kind", error)

    def test_the_evidence_vocabulary_is_bounded_to_exact_identity_spaces(self):
        self.assertEqual(
            {"twin_artifact", "scanner_symbol", "scanner_file"},
            set(intent_delta.EVIDENCE_REF_KINDS),
        )


class BoundsTests(unittest.TestCase):
    """A bound is a refusal, never a truncation."""

    def test_an_oversized_outcome_is_refused_not_truncated(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(requested_outcome="x" * (intent_delta.MAX_OUTCOME_CHARS + 1))
        )
        self.assertIsNone(delta)
        self.assertEqual("requested_outcome is oversized", error)

    def test_an_outcome_at_the_limit_is_accepted(self):
        delta = _built(requested_outcome="x" * intent_delta.MAX_OUTCOME_CHARS)
        self.assertEqual(
            intent_delta.MAX_OUTCOME_CHARS, len(delta["requested_outcome"])
        )

    def test_an_oversized_statement_is_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(constraints=["y" * (intent_delta.MAX_ITEM_CHARS + 1)])
        )
        self.assertIsNone(delta)
        self.assertEqual("constraints is oversized", error)

    def test_too_many_statements_are_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(assumptions=[f"assumption {i}" for i in range(intent_delta.MAX_LIST_ITEMS + 1)])
        )
        self.assertIsNone(delta)
        self.assertEqual("assumptions is oversized", error)

    def test_too_many_scope_references_are_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(scope={"entities": [f"e{i}" for i in range(intent_delta.MAX_SCOPE_REFS + 1)], "artifacts": []})
        )
        self.assertIsNone(delta)
        self.assertEqual("scope.entities is oversized", error)

    def test_an_oversized_prose_value_is_refused(self):
        delta, error = intent_delta.build_intent_delta(
            _raw(prose="p" * (intent_delta.MAX_PROSE_CHARS + 1))
        )
        self.assertIsNone(delta)
        self.assertEqual("prose is oversized", error)

    def test_a_blank_statement_is_refused(self):
        delta, error = intent_delta.build_intent_delta(_raw(constraints=["   "]))
        self.assertIsNone(delta)
        self.assertEqual("constraints must be a non-empty string", error)


class BaselineTests(unittest.TestCase):
    def test_every_baseline_field_must_be_present(self):
        for field in (
            "workspace_id",
            "scan_generation",
            "baseline_fingerprint",
            "scanner_schema_version",
            "grammar",
        ):
            with self.subTest(field=field):
                baseline = _baseline()
                baseline.pop(field)
                delta, error = intent_delta.build_intent_delta(_raw(baseline=baseline))
                self.assertIsNone(delta)
                self.assertEqual(f"baseline is missing {field}", error)

    def test_a_boolean_generation_is_refused(self):
        baseline = _baseline()
        baseline["scan_generation"] = True
        delta, error = intent_delta.build_intent_delta(_raw(baseline=baseline))
        self.assertIsNone(delta)
        self.assertEqual("baseline.scan_generation must be an integer", error)

    def test_a_negative_generation_is_refused(self):
        baseline = _baseline()
        baseline["scan_generation"] = -1
        delta, error = intent_delta.build_intent_delta(_raw(baseline=baseline))
        self.assertIsNone(delta)
        self.assertEqual("baseline.scan_generation must not be negative", error)

    def test_the_grammar_context_must_name_both_fields(self):
        for field in ("implementation", "version"):
            with self.subTest(field=field):
                baseline = _baseline()
                baseline["grammar"] = {"implementation": "cpython", "version": "3.11"}
                baseline["grammar"].pop(field)
                delta, error = intent_delta.build_intent_delta(_raw(baseline=baseline))
                self.assertIsNone(delta)
                self.assertEqual(f"baseline.grammar is missing {field}", error)


class CanonicalizationTests(unittest.TestCase):
    def test_the_scope_is_sorted_and_de_duplicated(self):
        delta = _built(scope={"entities": ["b.Thing", "a.Thing", "b.Thing"], "artifacts": []})
        self.assertEqual(["a.Thing", "b.Thing"], delta["scope"]["entities"])

    def test_the_evidence_references_are_sorted_by_kind_then_id(self):
        delta = _built(
            origin={
                "kind": "developer_authored",
                "evidence": [
                    {"kind": "scanner_symbol", "id": "b"},
                    {"kind": "scanner_file", "id": "z.py"},
                    {"kind": "scanner_file", "id": "a.py"},
                    {"kind": "scanner_symbol", "id": "b"},
                ],
            }
        )
        self.assertEqual(
            [
                {"kind": "scanner_file", "id": "a.py"},
                {"kind": "scanner_file", "id": "z.py"},
                {"kind": "scanner_symbol", "id": "b"},
            ],
            delta["origin"]["evidence"],
        )

    def test_authored_statement_order_is_preserved(self):
        delta = _built(acceptance_criteria=["third", "first", "second"])
        self.assertEqual(["third", "first", "second"], delta["acceptance_criteria"])

    def test_a_repeated_statement_collapses_to_its_first_occurrence(self):
        delta = _built(acceptance_criteria=["first", "second", "first"])
        self.assertEqual(["first", "second"], delta["acceptance_criteria"])

    def test_the_same_facts_serialize_byte_for_byte(self):
        first = intent_delta.dumps(_built())
        second = intent_delta.dumps(_built())
        self.assertEqual(first, second)

    def test_serialization_is_ascii_safe_and_sorted(self):
        rendered = intent_delta.dumps(_built(prose="café and — dashes"))
        self.assertNotIn("é", rendered)
        self.assertTrue(rendered.startswith('{"acceptance_criteria"'))


class ProseTests(unittest.TestCase):
    def test_prose_is_carried_as_data_with_a_fixed_authority(self):
        delta = _built(prose="please make the version 2")
        self.assertEqual(
            {"text": "please make the version 2", "authority": "input_data_only"},
            delta["prose"],
        )

    def test_prose_is_optional_and_defaults_to_a_present_null(self):
        delta = _built()
        self.assertEqual(
            {"text": None, "authority": "input_data_only"}, delta["prose"]
        )

    def test_prose_is_never_interpreted(self):
        # A prose value that names a scope, an action and an authority is still
        # only text: nothing in the document changes because of it.
        plain = _built()
        with_prose = _built(
            prose="scope: everything; action: commit and push; approval: granted"
        )
        for field in ("scope", "acceptance_criteria", "baseline", "executable"):
            with self.subTest(field=field):
                self.assertEqual(plain[field], with_prose[field])


class PrivacyTests(unittest.TestCase):
    """No refusal reason may carry a caller value, a path or an environment fact."""

    def _reasons(self):
        cases = [
            _raw(),
            _raw(constraints=[None]),
            _raw(scope={"entities": [], "artifacts": []}),
            _raw(baseline={}),
            _raw(requested_outcome="x" * (intent_delta.MAX_OUTCOME_CHARS + 1)),
            _raw(prose="p" * (intent_delta.MAX_PROSE_CHARS + 1)),
        ]
        out = []
        for raw in cases:
            for section in intent_delta.REQUIRED_SECTIONS:
                candidate = dict(raw)
                candidate.pop(section)
                for candidate_raw in (candidate, raw):
                    _, error = intent_delta.build_intent_delta(candidate_raw)
                    if error:
                        out.append(error)
        return out

    def test_no_reason_carries_a_path_or_an_environment_value(self):
        reasons = self._reasons()
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)
                self.assertNotIn(os.path.expanduser("~"), reason)
                self.assertNotIn(sys.executable, reason)

    def test_no_reason_leaks_the_callers_value(self):
        secret = "AKIA-EXAMPLE-NOT-A-REAL-KEY"
        _, error = intent_delta.build_intent_delta(_raw(requested_outcome=secret))
        self.assertIsNone(error)
        raw = _raw(acceptance_criteria=[])
        raw["requested_outcome"] = secret
        _, error = intent_delta.build_intent_delta(raw)
        self.assertEqual("acceptance_criteria must name at least one entry", error)
        self.assertNotIn(secret, error)


if __name__ == "__main__":
    unittest.main()
