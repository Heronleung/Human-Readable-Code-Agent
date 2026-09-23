"""The declarative, product-owned validation plan (P5.5a).

A plan says which checks apply to which candidate. It may not say *how* they
run: every operational value comes from the code-owned policy, and a request
that tries to supply a command, an image, a mount, an environment variable, a
timeout, a resource limit or a working directory is refused by name.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from hrca.authoring import validation_plan, validation_policy

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
MANIFEST = os.path.join(REPO, "fixtures", "validation", "manifest.json")


def _corpus() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _binding(**overrides) -> dict:
    binding = {
        "candidate_id": "candidate:" + "a" * 64,
        "candidate_root_name": "candidate-" + "a" * 32,
        "manifest_sha256": "b" * 64,
        "manifest_bytes": 1024,
        "review_sha256": "c" * 64,
        "review_bytes": 2048,
        "edit_id": "edit:" + "d" * 64,
        "intent_delta_id": "intent:" + "e" * 64,
        "proposal_id": "impact:" + "f" * 64,
        "binding_fingerprint": "bind:" + "1" * 64,
        "baseline": {
            "workspace_id": "ws:test",
            "scan_generation": 1,
            "baseline_fingerprint": "2" * 64,
            "scanner_schema_version": "1.1.0",
            "grammar": {"implementation": "cpython", "version": "3.11"},
        },
        "files": [{"path": "pkg/service.py", "sha256": "3" * 64, "bytes": 181}],
    }
    binding.update(overrides)
    return binding


def _built(request=None, **overrides) -> dict:
    plan, error = validation_plan.build_plan(_binding(**overrides), request)
    assert error is None, error
    return plan


class PlanShapeTests(unittest.TestCase):
    def test_the_declared_schema_and_generator(self):
        plan = _built()
        self.assertEqual("1.0.0", validation_plan.VALIDATION_PLAN_SCHEMA_VERSION)
        self.assertEqual("hrca-validation-plan", plan["generator"])
        self.assertEqual(validation_policy.POLICY_VERSION, plan["policy_version"])

    def test_a_plan_is_never_executable_or_applied(self):
        plan = _built()
        self.assertIs(False, plan["executable"])
        self.assertIs(False, plan["applied"])

    def test_a_built_plan_validates(self):
        self.assertIsNone(validation_plan.validate_plan(_built()))

    def test_the_checks_are_the_code_owned_records_in_canonical_order(self):
        corpus = _corpus()
        plan = _built()
        self.assertEqual(
            corpus["check_order"], [record["check_id"] for record in plan["checks"]]
        )
        for record in plan["checks"]:
            with self.subTest(check=record["check_id"]):
                expected = dict(corpus["checks"][record["check_id"]])
                expected["check_id"] = record["check_id"]
                self.assertEqual(expected, record)

    def test_the_plan_carries_the_candidate_binding_verbatim(self):
        plan = _built()
        self.assertEqual(_binding(), plan["candidate"])
        self.assertEqual(
            ["pkg/service.py"],
            [entry["path"] for entry in plan["candidate"]["files"]],
        )

    def test_the_identity_is_content_addressed_and_stable(self):
        first, second = _built(), _built()
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first, second)

    def test_a_changed_binding_changes_the_identity(self):
        other = _built(candidate_id="candidate:" + "9" * 64)
        self.assertNotEqual(_built()["plan_id"], other["plan_id"])

    def test_the_files_are_sorted_and_de_duplicated_in_the_binding(self):
        plan = _built(
            files=[
                {"path": "pkg/z.py", "sha256": "4" * 64, "bytes": 2},
                {"path": "pkg/a.py", "sha256": "5" * 64, "bytes": 1},
            ]
        )
        self.assertEqual(
            ["pkg/a.py", "pkg/z.py"],
            [entry["path"] for entry in plan["candidate"]["files"]],
        )

    def test_serialization_is_canonical(self):
        rendered = validation_plan.dumps(_built())
        self.assertEqual(rendered, validation_plan.dumps(_built()))
        self.assertTrue(rendered.startswith('{"applied"'))


class RequestTests(unittest.TestCase):
    """A request names checks. Everything else is a refusal with its own reason."""

    def test_a_request_may_name_a_subset(self):
        plan = _built({"checks": ["check:quotation_reference"]})
        self.assertEqual(
            ["check:quotation_reference"],
            [record["check_id"] for record in plan["checks"]],
        )

    def test_an_empty_request_selects_the_default_check_set(self):
        plan = _built({})
        self.assertEqual(list(validation_policy.DEFAULT_CHECKS),
                         [record["check_id"] for record in plan["checks"]])

    def test_the_candidate_check_can_be_requested_by_name(self):
        plan = _built({"checks": ["check:candidate_syntax"]})
        record = plan["checks"][0]
        self.assertEqual("check:candidate_syntax", record["check_id"])
        self.assertEqual(
            validation_policy.KIND_CANDIDATE_SYNTAX, record["kind"]
        )
        self.assertEqual(
            validation_policy.CANDIDATE_IMAGE_DIGEST, record["image_digest"]
        )
        self.assertIsNone(validation_plan.validate_plan(plan))

    def test_every_named_runtime_override_is_refused_by_name(self):
        corpus = _corpus()
        for key, sentence in sorted(corpus["override_rejections"].items()):
            with self.subTest(key=key):
                plan, error = validation_plan.build_plan(_binding(), {key: "whatever"})
                self.assertIsNone(plan)
                self.assertEqual(sentence, error)

    def test_an_unrecognised_field_is_refused(self):
        plan, error = validation_plan.build_plan(_binding(), {"surprise": 1})
        self.assertIsNone(plan)
        self.assertEqual(validation_policy.REASON_UNKNOWN_REQUEST_KEY, error)

    def test_a_non_mapping_request_is_refused(self):
        for request in ("checks", 7, ["checks"]):
            with self.subTest(request=request):
                plan, error = validation_plan.build_plan(_binding(), request)
                self.assertIsNone(plan)
                self.assertEqual(validation_policy.REASON_REQUEST_NOT_MAPPING, error)

    def test_a_shell_string_as_a_request_is_refused(self):
        plan, error = validation_plan.build_plan(_binding(), "rm -rf /")
        self.assertIsNone(plan)
        self.assertEqual(validation_policy.REASON_REQUEST_NOT_MAPPING, error)

    def test_an_unknown_check_in_a_request_is_refused(self):
        plan, error = validation_plan.build_plan(_binding(), {"checks": ["check:x"]})
        self.assertIsNone(plan)
        self.assertEqual(validation_policy.REASON_UNKNOWN_CHECK, error)


class BindingRefusalTests(unittest.TestCase):
    def _reason(self, **overrides):
        plan, error = validation_plan.build_plan(_binding(**overrides))
        self.assertIsNone(plan)
        return error

    def test_a_missing_field_is_refused(self):
        for field in (
            "candidate_id",
            "candidate_root_name",
            "manifest_sha256",
            "manifest_bytes",
            "review_sha256",
            "review_bytes",
            "edit_id",
            "intent_delta_id",
            "proposal_id",
            "binding_fingerprint",
            "files",
        ):
            with self.subTest(field=field):
                binding = _binding()
                binding.pop(field)
                plan, error = validation_plan.build_plan(binding)
                self.assertIsNone(plan)
                self.assertEqual(validation_plan.REASON_BINDING_INVALID, error)

    def test_a_non_mapping_binding_is_refused(self):
        for binding in (None, "binding", 7):
            with self.subTest(binding=binding):
                plan, error = validation_plan.build_plan(binding)
                self.assertIsNone(plan)
                self.assertEqual(validation_plan.REASON_BINDING_INVALID, error)

    def test_a_malformed_hash_is_refused(self):
        for field in ("manifest_sha256", "review_sha256"):
            for value in ("", "abc", "A" * 64, "0" * 63, 7):
                with self.subTest(field=field, value=value):
                    self.assertEqual(
                        validation_plan.REASON_BINDING_INVALID,
                        self._reason(**{field: value}),
                    )

    def test_a_root_name_that_is_a_path_is_refused(self):
        # The plan carries where the candidate lives only as its own name; an
        # absolute path here is a malformed binding, not a location.
        for value in (
            "/tmp/candidate-" + "a" * 32,
            "candidate-aaaa/../bbbb",
            "candidate-aaaa\\bbbb",
            "not-a-candidate",
            "",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    validation_plan.REASON_BINDING_INVALID,
                    self._reason(candidate_root_name=value),
                )

    def test_an_empty_or_oversized_file_list_is_refused(self):
        self.assertEqual(validation_plan.REASON_EMPTY_FILES, self._reason(files=[]))
        self.assertEqual(
            validation_plan.REASON_TOO_MANY_FILES,
            self._reason(
                files=[
                    {"path": "pkg/f%d.py" % index, "sha256": "6" * 64, "bytes": 1}
                    for index in range(validation_plan.MAX_FILES + 1)
                ]
            ),
        )

    def test_a_hostile_file_path_is_refused(self):
        for path in (
            "/etc/passwd.py",
            "pkg/../../outside.py",
            "pkg/./service.py",
            "pkg\\service.py",
            "pkg//service.py",
            "",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    validation_plan.REASON_FILE_INVALID,
                    self._reason(files=[{"path": path, "sha256": "7" * 64, "bytes": 1}]),
                )

    def test_a_duplicate_file_path_is_refused(self):
        entry = {"path": "pkg/service.py", "sha256": "8" * 64, "bytes": 1}
        self.assertEqual(
            validation_plan.REASON_FILE_INVALID, self._reason(files=[entry, dict(entry)])
        )

    def test_a_malformed_file_entry_is_refused(self):
        for entry in (
            "pkg/service.py",
            {"path": "pkg/service.py"},
            {"path": "pkg/service.py", "sha256": "9" * 64, "bytes": -1},
            {"path": "pkg/service.py", "sha256": "9" * 64, "bytes": True},
            {"path": 7, "sha256": "9" * 64, "bytes": 1},
        ):
            with self.subTest(entry=entry):
                self.assertEqual(
                    validation_plan.REASON_FILE_INVALID, self._reason(files=[entry])
                )

    def test_a_malformed_baseline_is_refused(self):
        for baseline in (
            None,
            {},
            {"workspace_id": "ws:x"},
            {
                "workspace_id": "",
                "scan_generation": 1,
                "baseline_fingerprint": "a",
                "scanner_schema_version": "1.1.0",
                "grammar": {},
            },
            {
                "workspace_id": "ws:x",
                "scan_generation": True,
                "baseline_fingerprint": "a",
                "scanner_schema_version": "1.1.0",
                "grammar": {"implementation": "cpython", "version": "3.11"},
            },
        ):
            with self.subTest(baseline=baseline):
                self.assertEqual(
                    validation_plan.REASON_BASELINE_INVALID,
                    self._reason(baseline=baseline),
                )


class ValidatePlanTests(unittest.TestCase):
    def test_a_tampered_check_record_is_refused(self):
        plan = _built()
        plan["checks"][0]["timeout_seconds"] = 3600.0
        self.assertEqual(
            validation_plan.REASON_CHECKS_NOT_CANONICAL,
            validation_plan.validate_plan(plan),
        )

    def test_a_plan_naming_an_unknown_check_is_refused(self):
        plan = _built()
        plan["checks"][0]["check_id"] = "check:not_a_check"
        self.assertEqual(
            validation_policy.REASON_UNKNOWN_CHECK, validation_plan.validate_plan(plan)
        )

    def test_a_plan_with_no_checks_is_refused(self):
        plan = _built()
        plan["checks"] = []
        plan["plan_id"] = validation_plan.plan_id_for(plan)
        self.assertEqual(
            validation_policy.REASON_NO_CHECKS, validation_plan.validate_plan(plan)
        )

    def test_a_reordered_check_list_is_refused(self):
        plan = _built()
        plan["checks"] = list(reversed(plan["checks"]))
        plan["plan_id"] = validation_plan.plan_id_for(plan)
        self.assertEqual(
            validation_plan.REASON_CHECKS_NOT_CANONICAL,
            validation_plan.validate_plan(plan),
        )

    def test_a_tampered_binding_invalidates_the_identity(self):
        plan = _built()
        plan["candidate"]["candidate_id"] = "candidate:" + "0" * 64
        self.assertIsNotNone(validation_plan.validate_plan(plan))

    def test_a_recomputed_identity_does_not_hide_a_malformed_binding(self):
        plan = _built()
        plan["candidate"]["files"][0]["sha256"] = "not a digest"
        plan["plan_id"] = validation_plan.plan_id_for(plan)
        self.assertEqual(
            validation_plan.REASON_FILE_INVALID, validation_plan.validate_plan(plan)
        )

    def test_a_well_formed_binding_that_disagrees_with_bytes_is_still_a_valid_plan(self):
        # The plan validator checks *structure*. Whether the binding describes
        # the candidate it names is decided against the candidate at dispatch,
        # which is the only place that can read one.
        plan = _built()
        plan["candidate"]["files"][0]["bytes"] = 999
        plan["plan_id"] = validation_plan.plan_id_for(plan)
        self.assertIsNone(validation_plan.validate_plan(plan))

    def test_a_plan_that_claims_to_be_executable_is_refused(self):
        plan = _built()
        plan["executable"] = True
        plan["plan_id"] = validation_plan.plan_id_for(plan)
        self.assertEqual(
            validation_plan.REASON_NOT_EXECUTABLE_OR_APPLIED,
            validation_plan.validate_plan(plan),
        )

    def test_an_unknown_policy_version_is_refused(self):
        plan = _built()
        plan["policy_version"] = "9.9.9"
        plan["plan_id"] = validation_plan.plan_id_for(plan)
        self.assertEqual(
            "unsupported policy_version", validation_plan.validate_plan(plan)
        )

    def test_a_non_mapping_is_refused(self):
        for plan in (None, "plan", 7, []):
            with self.subTest(plan=plan):
                self.assertEqual(
                    validation_plan.REASON_NOT_MAPPING,
                    validation_plan.validate_plan(plan),
                )

    def test_a_stale_identity_is_refused(self):
        plan = _built()
        plan["plan_id"] = "plan:" + "0" * 64
        self.assertEqual(
            validation_plan.REASON_ID_INVALID, validation_plan.validate_plan(plan)
        )


class MigrationTests(unittest.TestCase):
    def test_the_current_version_passes_through(self):
        plan = _built()
        migrated, error = validation_plan.migrate_plan(plan)
        self.assertIsNone(error)
        self.assertIs(plan, migrated)

    def test_a_newer_version_is_refused(self):
        doc, error = validation_plan.migrate_plan({"schema_version": "9.9.9"})
        self.assertIsNone(doc)
        self.assertEqual(validation_plan.REASON_NEWER_VERSION, error)

    def test_a_missing_or_malformed_version_is_refused(self):
        for raw, expected in (
            ({}, validation_plan.REASON_MISSING_VERSION),
            ({"schema_version": ""}, validation_plan.REASON_MISSING_VERSION),
            ({"schema_version": 7}, validation_plan.REASON_MISSING_VERSION),
            ("not a plan", validation_plan.REASON_NOT_MAPPING),
        ):
            with self.subTest(raw=raw):
                doc, error = validation_plan.migrate_plan(raw)
                self.assertIsNone(doc)
                self.assertEqual(expected, error)

    def test_an_older_version_with_no_migration_is_refused(self):
        doc, error = validation_plan.migrate_plan({"schema_version": "0.9.0"})
        self.assertIsNone(doc)
        self.assertEqual(validation_plan.REASON_NOT_MIGRATABLE, error)

    def test_the_registry_is_empty_before_1_0_0(self):
        self.assertEqual([], sorted(validation_plan.MIGRATIONS))


class PrivacyTests(unittest.TestCase):
    def test_no_reason_carries_a_path_or_a_caller_value(self):
        marker = "AKIA-EXAMPLE-NOT-A-REAL-KEY"
        attempts = [
            _binding(candidate_root_name="/etc/" + marker),
            _binding(files=[{"path": "/etc/" + marker, "sha256": "a" * 64, "bytes": 1}]),
            _binding(files=[{"path": marker + ".py", "sha256": marker, "bytes": -1}]),
            _binding(baseline={"workspace_id": marker}),
        ]
        reasons = []
        for binding in attempts:
            _plan, error = validation_plan.build_plan(binding)
            self.assertIsNotNone(error)
            reasons.append(error)
        _plan, error = validation_plan.build_plan(_binding(), {"surprise": marker})
        reasons.append(error)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertNotIn(marker, reason)
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)
                self.assertLess(len(reason), 200)


if __name__ == "__main__":
    unittest.main()
