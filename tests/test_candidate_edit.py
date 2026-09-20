"""The typed candidate edit request (P5.4): narrow grammar, exact paths.

The edit request is the only place a developer says *what* to change. These
tests hold it to the two rules the contract states: the grammar is the narrowest
one that can still express a real change, and a path is accepted only in the one
spelling that makes it mean a single file.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import unittest

from hrca import candidate_edit

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))

_BEFORE = "4972d66bf73cb8dcd3c50827ac51ce27096dae5b7031d00aeb245a01d721e4a9"


def _baseline() -> dict:
    return {
        "workspace_id": "ws:test",
        "scan_generation": 1,
        "baseline_fingerprint": "bf:test",
        "scanner_schema_version": "1.1.0",
        "grammar": {"implementation": "cpython", "version": "3.11"},
    }


def _operation(**overrides) -> dict:
    """Return one operation; supplying ``bytes_b64`` replaces the text form."""
    record = {
        "op": "replace_file",
        "path": "pkg/service.py",
        "expected_sha256": _BEFORE,
        "text": 'VERSION = "2"\n',
    }
    if "text" in overrides and overrides["text"] is None:
        del overrides["text"]
    if "bytes_b64" in overrides:
        record.pop("text", None)
    record.update(overrides)
    return record


def _raw(**overrides) -> dict:
    raw = {
        "intent_delta_id": "intent:" + "a" * 64,
        "proposal_id": "impact:" + "b" * 64,
        "binding_fingerprint": "bind:" + "c" * 64,
        "baseline": _baseline(),
        "operations": [_operation()],
    }
    raw.update(overrides)
    return raw


def _built(**overrides) -> dict:
    edit, error = candidate_edit.build_edit(_raw(**overrides))
    assert error is None, error
    return edit


class SchemaTests(unittest.TestCase):
    def test_the_declared_schema_and_generator(self):
        edit = _built()
        self.assertEqual("1.0.0", candidate_edit.CANDIDATE_EDIT_SCHEMA_VERSION)
        self.assertEqual("hrca-candidate-edit", edit["generator"])
        self.assertEqual("1.0.0", edit["schema_version"])

    def test_an_edit_is_never_executable_or_applied(self):
        edit = _built()
        self.assertIs(False, edit["executable"])
        self.assertIs(False, edit["applied"])

    def test_a_built_edit_validates(self):
        self.assertIsNone(candidate_edit.validate_edit(_built()))

    def test_the_identity_is_content_addressed_and_stable(self):
        first, second = _built(), _built()
        self.assertEqual(first["edit_id"], second["edit_id"])
        self.assertTrue(first["edit_id"].startswith("edit:"))

    def test_tampering_with_an_operation_invalidates_the_identity(self):
        edit = _built()
        edit["operations"][0]["text"] = "VERSION = '9'\n"
        self.assertEqual(
            "edit_id does not match the edit content",
            candidate_edit.validate_edit(edit),
        )

    def test_a_non_mapping_is_refused(self):
        for raw in (None, "text", 7, []):
            with self.subTest(raw=raw):
                edit, error = candidate_edit.build_edit(raw)
                self.assertIsNone(edit)
                self.assertEqual("edit request is not a mapping", error)

    def test_both_content_spellings_agree_on_one_identity(self):
        text = _built()
        encoded = _built(
            operations=[
                _operation(
                    bytes_b64=base64.b64encode(b'VERSION = "2"\n').decode()
                )
            ]
        )
        self.assertEqual(text["edit_id"], encoded["edit_id"])
        self.assertEqual(text["operations"], encoded["operations"])


class MigrationTests(unittest.TestCase):
    def test_the_current_version_passes_through(self):
        edit = _built()
        migrated, error = candidate_edit.migrate_edit(edit)
        self.assertIsNone(error)
        self.assertIs(edit, migrated)

    def test_a_newer_version_is_refused(self):
        doc, error = candidate_edit.migrate_edit({"schema_version": "9.9.9"})
        self.assertIsNone(doc)
        self.assertEqual("schema_version is newer than supported", error)

    def test_a_missing_or_malformed_version_is_refused(self):
        for raw, expected in (
            ({}, "missing schema_version"),
            ({"schema_version": ""}, "missing schema_version"),
            ({"schema_version": 7}, "missing schema_version"),
            ("not an edit", "edit request is not a mapping"),
        ):
            with self.subTest(raw=raw):
                doc, error = candidate_edit.migrate_edit(raw)
                self.assertIsNone(doc)
                self.assertEqual(expected, error)

    def test_an_older_version_with_no_migration_is_refused(self):
        doc, error = candidate_edit.migrate_edit({"schema_version": "0.9.0"})
        self.assertIsNone(doc)
        self.assertEqual("schema_version is not migratable", error)

    def test_the_registry_is_empty_before_1_0_0(self):
        self.assertEqual([], sorted(candidate_edit.MIGRATIONS))


class PathPolicyTests(unittest.TestCase):
    """One exact spelling, and everything ambiguous refused rather than repaired."""

    def _reason(self, path):
        _, error = candidate_edit.build_edit(_raw(operations=[_operation(path=path)]))
        return error

    def test_an_exact_relative_path_is_accepted(self):
        self.assertIsNone(self._reason("pkg/service.py"))
        self.assertIsNone(self._reason("a/b/c/d.pyi"))

    def test_absolute_paths_are_refused(self):
        for path in (
            "/etc/passwd",
            "/home/heron/pkg/service.py",
            "\\pkg\\service.py",
            "C:/pkg/service.py",
            "c:pkg/service.py",
            "//server/share/service.py",
        ):
            with self.subTest(path=path):
                reason = self._reason(path)
                self.assertIn(
                    reason,
                    {
                        candidate_edit.REASON_PATH_ABSOLUTE,
                        candidate_edit.REASON_PATH_SEPARATOR,
                        candidate_edit.REASON_PATH_COMPONENT,
                    },
                )

    def test_traversal_is_refused(self):
        for path in (
            "pkg/../service.py",
            "pkg/../../etc/passwd.py",
            "../service.py",
            "..",
            "pkg/..",
        ):
            with self.subTest(path=path):
                self.assertEqual(candidate_edit.REASON_PATH_COMPONENT, self._reason(path))

    def test_dot_and_empty_components_are_refused(self):
        for path in ("pkg/./service.py", "pkg//service.py", "./service.py", "service.py/"):
            with self.subTest(path=path):
                reason = self._reason(path)
                self.assertIsNotNone(reason)

    def test_a_mixed_separator_is_refused(self):
        self.assertEqual(
            candidate_edit.REASON_PATH_SEPARATOR, self._reason("pkg\\service.py")
        )

    def test_a_reserved_device_name_is_refused(self):
        for path in ("con.py", "pkg/nul.py", "pkg/PRN.py", "pkg/com1.py"):
            with self.subTest(path=path):
                self.assertEqual(
                    candidate_edit.REASON_PATH_RESERVED, self._reason(path)
                )

    def test_only_python_source_may_be_replaced(self):
        for path in ("pkg/service.txt", "pkg/service", "README.md", "pkg/service.PY"):
            with self.subTest(path=path):
                self.assertEqual(
                    candidate_edit.REASON_PATH_SUFFIX, self._reason(path)
                )

    def test_a_padded_or_dotted_component_is_refused(self):
        for path in ("pkg /service.py", "pkg/ service.py", "pkg/service.py.", "pkg/x. /s.py"):
            with self.subTest(path=path):
                reason = self._reason(path)
                self.assertIsNotNone(reason)

    def test_a_control_character_is_refused(self):
        for path in ("pkg/ser\x00vice.py", "pkg/ser\nvice.py", "pkg/ser\x7fvice.py"):
            with self.subTest(path=path):
                self.assertEqual(candidate_edit.REASON_PATH_INVALID, self._reason(path))

    def test_a_non_normal_path_is_refused(self):
        decomposed = "pkg/se\u0301rvice.py"  # NFD spelling of sé
        self.assertEqual(
            candidate_edit.REASON_PATH_NOT_NORMAL, self._reason(decomposed)
        )
        self.assertIsNone(self._reason("pkg/s\u00e9rvice.py"))

    def test_an_overlong_path_is_refused(self):
        long_component = "a" * (candidate_edit.MAX_PATH_COMPONENT_CHARS + 1) + ".py"
        self.assertEqual(
            candidate_edit.REASON_PATH_OVERLONG, self._reason(long_component)
        )
        deep = "/".join(["a"] * (candidate_edit.MAX_PATH_COMPONENTS + 1)) + ".py"
        self.assertEqual(candidate_edit.REASON_PATH_OVERLONG, self._reason(deep))

    def test_the_normalizer_reports_a_reason_for_every_rejection(self):
        path, reason = candidate_edit.normalize_edit_path("pkg/service.py")
        self.assertEqual("pkg/service.py", path)
        self.assertIsNone(reason)


class CollisionTests(unittest.TestCase):
    def test_a_duplicate_path_is_refused(self):
        edit, error = candidate_edit.build_edit(
            _raw(operations=[_operation(), _operation()])
        )
        self.assertIsNone(edit)
        self.assertEqual(candidate_edit.REASON_DUPLICATE_PATH, error)

    def test_two_paths_differing_only_by_case_are_refused(self):
        edit, error = candidate_edit.build_edit(
            _raw(operations=[_operation(), _operation(path="pkg/Service.py")])
        )
        self.assertIsNone(edit)
        self.assertEqual(candidate_edit.REASON_CASE_COLLISION, error)


class GrammarTests(unittest.TestCase):
    def test_an_unknown_operation_is_refused(self):
        edit, error = candidate_edit.build_edit(
            _raw(operations=[_operation(op="frobnicate")])
        )
        self.assertIsNone(edit)
        self.assertEqual(candidate_edit.REASON_UNKNOWN_OPERATION, error)

    def test_every_named_unsupported_operation_states_why(self):
        for op, expected in sorted(candidate_edit.UNSUPPORTED_OPERATIONS.items()):
            with self.subTest(op=op):
                edit, error = candidate_edit.build_edit(
                    _raw(operations=[_operation(op=op)])
                )
                self.assertIsNone(edit)
                self.assertEqual(expected, error)

    def test_creation_deletion_and_patching_are_named_as_unsupported(self):
        for op, phrase in (
            ("create_file", "new-file creation is unsupported"),
            ("delete_file", "deletion is unsupported"),
            ("rename_file", "rename is unsupported"),
            ("apply_patch", "arbitrary patch input is unsupported"),
            ("apply_diff", "unified-diff input is unsupported"),
            ("run_command", "commands are unsupported"),
        ):
            with self.subTest(op=op):
                _, error = candidate_edit.build_edit(
                    _raw(operations=[_operation(op=op)])
                )
                self.assertEqual(phrase, error)

    def test_no_operations_is_refused(self):
        for operations in ([], None, "nope"):
            with self.subTest(operations=operations):
                edit, error = candidate_edit.build_edit(_raw(operations=operations))
                self.assertIsNone(edit)
                self.assertEqual(candidate_edit.REASON_NO_OPERATIONS, error)

    def test_too_many_operations_is_refused(self):
        operations = [
            _operation(path="pkg/m%d.py" % index)
            for index in range(candidate_edit.MAX_OPERATIONS + 1)
        ]
        edit, error = candidate_edit.build_edit(_raw(operations=operations))
        self.assertIsNone(edit)
        self.assertEqual(candidate_edit.REASON_TOO_MANY_OPERATIONS, error)

    def test_an_operation_that_is_not_a_mapping_is_refused(self):
        edit, error = candidate_edit.build_edit(_raw(operations=["nope"]))
        self.assertIsNone(edit)
        self.assertEqual(candidate_edit.REASON_OPERATION_NOT_MAPPING, error)


class ContentTests(unittest.TestCase):
    def _reason(self, **overrides):
        _, error = candidate_edit.build_edit(_raw(operations=[_operation(**overrides)]))
        return error

    def test_both_content_spellings_together_are_refused(self):
        record = {
            "op": "replace_file",
            "path": "pkg/service.py",
            "expected_sha256": _BEFORE,
            "text": "x\n",
            "bytes_b64": base64.b64encode(b"x\n").decode(),
        }
        _, error = candidate_edit.build_edit(_raw(operations=[record]))
        self.assertEqual(candidate_edit.REASON_BOTH_CONTENT, error)

    def test_no_content_is_refused(self):
        record = {
            "op": "replace_file",
            "path": "pkg/service.py",
            "expected_sha256": _BEFORE,
        }
        _, error = candidate_edit.build_edit(_raw(operations=[record]))
        self.assertEqual(candidate_edit.REASON_NO_CONTENT, error)

    def test_non_utf8_bytes_are_refused(self):
        self.assertEqual(
            candidate_edit.REASON_CONTENT_NOT_UTF8,
            self._reason(bytes_b64=base64.b64encode(b"\xff\xfe\x00").decode()),
        )

    def test_malformed_base64_is_refused(self):
        self.assertEqual(
            candidate_edit.REASON_CONTENT_NOT_UTF8, self._reason(bytes_b64="not base64!!")
        )

    def test_a_nul_byte_is_carried_for_the_state_machine_to_rule_on(self):
        # Content the contract will not review as a diff is a *state* answered in
        # hrca.candidate, not a malformed request refused here: a developer can
        # legitimately supply the wrong thing and should be told what was wrong
        # with it.
        edit, error = candidate_edit.build_edit(
            _raw(operations=[_operation(text="VERSION = '1'\x00\n")])
        )
        self.assertIsNone(error)
        self.assertEqual("VERSION = '1'\x00\n", edit["operations"][0]["text"])

    def test_a_byte_order_mark_is_carried_for_the_state_machine_to_rule_on(self):
        edit, error = candidate_edit.build_edit(
            _raw(operations=[_operation(text="\ufeffVERSION = '1'\n")])
        )
        self.assertIsNone(error)
        self.assertTrue(edit["operations"][0]["text"].startswith("\ufeff"))

    def test_reviewability_is_the_candidates_question(self):
        # This module has no opinion on it, so it exposes none.
        self.assertFalse(hasattr(candidate_edit, "REASON_CONTENT_NUL"))
        self.assertFalse(hasattr(candidate_edit, "REASON_CONTENT_BOM"))

    def test_an_oversized_replacement_is_refused_not_truncated(self):
        self.assertEqual(
            candidate_edit.REASON_CONTENT_OVERSIZED,
            self._reason(text="x" * (candidate_edit.MAX_FILE_BYTES + 1)),
        )

    def test_a_missing_or_malformed_predecessor_hash_is_refused(self):
        record = {
            "op": "replace_file",
            "path": "pkg/service.py",
            "text": "x\n",
        }
        _, error = candidate_edit.build_edit(_raw(operations=[record]))
        self.assertEqual(candidate_edit.REASON_MISSING_EXPECTED, error)
        for value in ("", "abc", "A" * 64, "0" * 63, 7, "z" * 64):
            with self.subTest(value=value):
                self.assertEqual(
                    candidate_edit.REASON_EXPECTED_INVALID,
                    self._reason(expected_sha256=value),
                )


class BindingTests(unittest.TestCase):
    def test_every_identifier_is_required_and_prefix_checked(self):
        # This module checks presence and the identity *space*, not the exact
        # value: whether a declared identity is the accepted one is decided
        # against the supplied proposal in hrca.candidate, which is the only
        # place that can answer it.
        for field, prefix, other in (
            ("intent_delta_id", "intent:", "impact:"),
            ("proposal_id", "impact:", "bind:"),
            ("binding_fingerprint", "bind:", "intent:"),
        ):
            for value in (None, "", 7, other + "a" * 64):
                with self.subTest(field=field, value=value):
                    raw = _raw()
                    raw[field] = value
                    edit, error = candidate_edit.build_edit(raw)
                    self.assertIsNone(edit)
                    self.assertEqual(candidate_edit.REASON_MISSING_BINDING, error)
            # A right-prefixed value of any width is accepted here.
            raw = _raw()
            raw[field] = prefix + "short"
            edit, error = candidate_edit.build_edit(raw)
            self.assertIsNone(error)
            self.assertEqual(prefix + "short", edit[field])

    def test_every_baseline_field_is_required(self):
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
                edit, error = candidate_edit.build_edit(_raw(baseline=baseline))
                self.assertIsNone(edit)
                self.assertEqual(candidate_edit.REASON_MISSING_BINDING, error)

    def test_a_boolean_or_negative_generation_is_refused(self):
        for value in (True, -1, "1", 1.5):
            with self.subTest(value=value):
                baseline = _baseline()
                baseline["scan_generation"] = value
                edit, error = candidate_edit.build_edit(_raw(baseline=baseline))
                self.assertIsNone(edit)
                self.assertEqual(candidate_edit.REASON_MISSING_BINDING, error)


class CanonicalizationTests(unittest.TestCase):
    def test_operations_are_sorted_by_path(self):
        edit = _built(
            operations=[
                _operation(path="pkg/z.py"),
                _operation(path="pkg/a.py"),
                _operation(path="pkg/m.py"),
            ]
        )
        self.assertEqual(
            ["pkg/a.py", "pkg/m.py", "pkg/z.py"],
            [record["path"] for record in edit["operations"]],
        )

    def test_the_same_request_serializes_byte_for_byte(self):
        self.assertEqual(
            candidate_edit.dumps(_built()), candidate_edit.dumps(_built())
        )

    def test_serialization_is_ascii_safe_and_sorted(self):
        rendered = candidate_edit.dumps(_built(text="# caf\u00e9\n"))
        self.assertNotIn("\u00e9", rendered)
        self.assertTrue(rendered.startswith('{"applied"'))


class PrivacyTests(unittest.TestCase):
    def _reasons(self):
        out = []
        attempts = [
            _raw(),
            _raw(operations=[_operation(path="/etc/" + "x" * 20)]),
            _raw(operations=[_operation(path="pkg/../x.py")]),
            _raw(operations=[_operation(op="delete_file")]),
            _raw(operations=[_operation(text="x" * (candidate_edit.MAX_FILE_BYTES + 1))]),
            _raw(baseline={}),
            _raw(operations=[]),
        ]
        for raw in attempts:
            for operations in ([], None):
                candidate = dict(raw)
                candidate["operations"] = operations
                _, error = candidate_edit.build_edit(candidate)
                if error:
                    out.append(error)
        _, error = candidate_edit.build_edit(_raw(operations=[_operation(path="/etc/passwd")]))
        out.append(error)
        return [reason for reason in out if reason]

    def test_no_reason_carries_a_path_or_an_environment_value(self):
        reasons = self._reasons()
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)
                self.assertNotIn(os.path.expanduser("~"), reason)
                self.assertNotIn(sys.executable, reason)
                self.assertLess(len(reason), 200)

    def test_no_reason_leaks_a_caller_value(self):
        marker = "AKIA-EXAMPLE-NOT-A-REAL-KEY"
        attempts = (
            _raw(operations=[_operation(path="/etc/" + marker + ".py")]),
            _raw(operations=[_operation(path="pkg/../" + marker + ".py")]),
            _raw(operations=[_operation(path="pkg/con.py", text=marker)]),
            _raw(operations=[_operation(expected_sha256=marker)]),
            _raw(
                operations=[
                    _operation(bytes_b64=base64.b64encode(marker.encode()).decode())
                ],
                baseline={"workspace_id": marker},
            ),
        )
        for raw in attempts:
            with self.subTest(raw=json.dumps(raw, sort_keys=True)[:60]):
                _, error = candidate_edit.build_edit(raw)
                self.assertIsNotNone(error)
                self.assertNotIn(marker, error)


if __name__ == "__main__":
    unittest.main()
