"""The declarative, product-owned validation plan (P5.5a).

A plan says which checks apply to which candidate. It may not say *how* they
run: every operational value comes from the code-owned policy, and a request
that tries to supply a command, an image, a mount, an environment variable, a
timeout, a resource limit or a working directory is refused by name.

This module also carries the oracle for the container-free half of the
validation dispatcher (:class:`PureValidationTests`). Those tests need no
daemon, so they live on a core route where every local verification run reaches
them; the dispatch path itself stays verified only through the container-gated
modules, which this module does not replace.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from hrca.authoring import validation, validation_plan, validation_policy
from hrca.execution import app_package, container_runner

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


# -- the container-free half of the dispatcher -----------------------------
#
# ``authoring.validation`` holds three different things: the dispatch (which
# needs a container client), the append-only evidence store (which needs a
# filesystem), and a set of helpers that are pure functions of their arguments.
# The helpers are read here directly, from hand-built inputs, so the properties
# they claim are checked on every core-route run rather than only behind the
# container gate.
#
# Nothing here dispatches, opens a socket or touches a store. Nothing here
# replaces the container-gated modules: those remain the authority for the argv
# that is actually built and handed to the client, and for the run's outcome.

_DOCKER = "docker"
_CONTAINER_NAME = "<container>"

# The runner's own argv shape (``ContainerRunner``'s base argv), with the two
# staged mounts as a package check builds them. The image, user and network come
# from the runner's constants, so this argv cannot silently describe a different
# runner than the one that would have produced it; the mount destinations are
# the literals ``isolation_facts`` itself reads.
_STAGED_MOUNT_IN = "type=bind,src=<staged-in>,dst=/in,readonly"
_STAGED_MOUNT_OUT = "type=bind,src=<staged-out>,dst=/out"


def _runner_argv(
    *,
    network=None,
    user=None,
    image=None,
    read_only=True,
    cap_drop=True,
    no_new_privileges=True,
    init=True,
    remove=True,
    resources=True,
    mounts=(_STAGED_MOUNT_IN, _STAGED_MOUNT_OUT),
):
    """Build a runner argv, so a case can state what it removed and why."""
    argv = [
        _DOCKER,
        "run",
        "--name",
        _CONTAINER_NAME,
    ]
    if remove:
        argv += ["--rm"]
    if init:
        argv += ["--init"]
    argv += [
        "--network",
        container_runner.RUNNER_NETWORK if network is None else network,
        "--user",
        container_runner.RUNNER_USER if user is None else user,
    ]
    if no_new_privileges:
        argv += ["--security-opt", "no-new-privileges"]
    if cap_drop:
        argv += ["--cap-drop", "ALL"]
    if read_only:
        argv += ["--read-only"]
    if resources:
        argv += [
            "--memory", container_runner.RUNNER_MEMORY,
            "--memory-swap", container_runner.RUNNER_MEMORY,
            "--cpus", container_runner.RUNNER_CPUS,
            "--pids-limit", container_runner.RUNNER_PIDS_LIMIT,
            "--tmpfs", container_runner.RUNNER_TMPFS,
        ]
    for mount in mounts:
        argv += ["--mount", mount]
    argv.append(container_runner.RUNNER_IMAGE if image is None else image)
    return argv


class PureValidationTests(unittest.TestCase):
    """The dispatcher's pure helpers, asserted without a container (B-A2).

    These live in a module named for the plan because a core-route test needs a
    core-route module; the guards this file already carries are the reason it
    was the host rather than a new route-table entry. What they cover is the
    property that matters most about ``isolation_facts`` in particular: it is
    the record of what was *actually* dispatched, and until now nothing on a
    route without a daemon asserted that it can report a property as **absent**.
    """

    # -- isolation_facts -------------------------------------------------

    def test_the_isolation_facts_match_the_accepted_manifest(self):
        # The independent side of this assertion is the hand-authored fixture
        # every container-gated validation test is measured against.
        self.assertEqual(
            _corpus()["expected_isolation"],
            validation.isolation_facts(_runner_argv()),
        )

    def test_removing_an_isolation_property_reports_it_as_absent(self):
        cases = (
            ("read_only_rootfs", {"read_only": False}),
            ("capabilities_dropped", {"cap_drop": False}),
            ("no_new_privileges", {"no_new_privileges": False}),
            ("init_reaps_children", {"init": False}),
            ("removed_after_run", {"remove": False}),
            ("resource_bounded", {"resources": False}),
            ("network_disabled", {"network": "bridge"}),
            ("non_root", {"user": "0:0"}),
            ("image_is_code_owned", {"image": "someone-elses-image:latest"}),
        )
        for fact, overrides in cases:
            with self.subTest(fact=fact):
                facts = validation.isolation_facts(_runner_argv(**overrides))
                self.assertIs(False, facts[fact], fact)

    def test_a_third_mount_is_reported_as_a_staged_shape_violation(self):
        # The branch the container-gated candidate test asserts with the
        # comment "three, not two": the staged shape is exactly two mounts, one
        # of them read-only.
        facts = validation.isolation_facts(
            _runner_argv(
                mounts=(_STAGED_MOUNT_IN, _STAGED_MOUNT_OUT, "type=bind,src=<extra>,dst=/out2")
            )
        )
        self.assertEqual(3, facts["mount_count"])
        self.assertIs(False, facts["write_mounts_are_staged_only"])
        self.assertIs(True, facts["input_mount_read_only"])

    def test_a_writable_input_mount_is_reported_as_not_read_only(self):
        facts = validation.isolation_facts(
            _runner_argv(
                mounts=("type=bind,src=<staged-in>,dst=/in", _STAGED_MOUNT_OUT)
            )
        )
        self.assertIs(False, facts["input_mount_read_only"])
        self.assertIs(False, facts["write_mounts_are_staged_only"])

    def test_a_docker_socket_mount_is_reported(self):
        facts = validation.isolation_facts(
            _runner_argv(
                mounts=(
                    _STAGED_MOUNT_IN,
                    _STAGED_MOUNT_OUT,
                    "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                )
            )
        )
        self.assertIs(False, facts["no_docker_socket_mount"])

    # -- canonical_argv --------------------------------------------------

    def test_canonical_argv_redacts_the_host_paths_and_container_name(self):
        reviewable = validation.canonical_argv(_runner_argv())
        # The client path is redacted to the fixed name, whatever it was.
        self.assertEqual("docker", reviewable[0])
        self.assertEqual("<container>", reviewable[reviewable.index("--name") + 1])
        mounts = [
            reviewable[index + 1]
            for index, element in enumerate(reviewable)
            if element == "--mount"
        ]
        self.assertTrue(mounts)
        for mount in mounts:
            self.assertIn("src=<staged>", mount)
            self.assertNotIn("<staged-in>", mount)
            self.assertNotIn("<staged-out>", mount)

    def test_canonical_argv_keeps_the_reviewed_surface_verbatim(self):
        # The flags, the image and the mount destinations are what a reviewer
        # reads, so none of them may be redacted away.
        reviewable = validation.canonical_argv(_runner_argv())
        for element in ("--read-only", "--cap-drop", "ALL", "no-new-privileges"):
            with self.subTest(element=element):
                self.assertIn(element, reviewable)
        self.assertIn(container_runner.RUNNER_IMAGE, reviewable)
        self.assertTrue(any("dst=/in" in mount for mount in reviewable))
        self.assertTrue(any("dst=/out" in mount for mount in reviewable))

    # -- content-addressed identities ------------------------------------

    def test_the_attempt_identity_is_content_addressed(self):
        attempt = {"ordinal": 1, "state": validation.STATE_PASSED}
        identity = validation.attempt_id_for(attempt)
        self.assertTrue(identity.startswith(validation.ATTEMPT_ID_PREFIX))
        self.assertEqual(identity, validation.attempt_id_for(dict(attempt)))
        # Key order is not content.
        self.assertEqual(
            identity,
            validation.attempt_id_for({"state": validation.STATE_PASSED, "ordinal": 1}),
        )
        # The identity field is excluded, so re-reading re-derives the same id.
        self.assertEqual(
            identity, validation.attempt_id_for(dict(attempt, attempt_id="attempt:x"))
        )
        # Different content is a different identity.
        self.assertNotEqual(identity, validation.attempt_id_for(dict(attempt, ordinal=2)))

    def test_the_result_identity_is_content_addressed(self):
        result = {"state": validation.STATE_PASSED}
        identity = validation.result_id_for(result)
        self.assertTrue(identity.startswith(validation.RESULT_ID_PREFIX))
        self.assertEqual(identity, validation.result_id_for(dict(result)))
        self.assertEqual(
            identity, validation.result_id_for(dict(result, result_id="result:x"))
        )
        self.assertNotEqual(
            identity, validation.result_id_for(dict(result, state=validation.STATE_FAILED))
        )

    # -- validate_attempt ------------------------------------------------

    def _attempt(self, **overrides):
        attempt = {
            "schema_version": validation.VALIDATION_ATTEMPT_SCHEMA_VERSION,
            "generator": validation.VALIDATION_ATTEMPT_GENERATOR,
            "state": validation.STATE_PASSED,
            "approved": False,
            "adopted": False,
            "applied": False,
            "ordinal": 1,
        }
        attempt.update(overrides)
        attempt["attempt_id"] = validation.attempt_id_for(attempt)
        return attempt

    def test_a_well_formed_attempt_validates(self):
        self.assertIsNone(validation.validate_attempt(self._attempt()))

    def test_a_non_mapping_attempt_is_refused(self):
        for value in ("x", 5, [], None):
            with self.subTest(value=value):
                self.assertEqual(
                    "attempt is not a mapping", validation.validate_attempt(value)
                )

    def test_a_wrong_schema_or_generator_is_refused(self):
        cases = (
            ({"schema_version": "9.9.9"}, "unsupported schema_version"),
            ({"generator": "someone-else"}, "unknown attempt generator"),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    reason, validation.validate_attempt(self._attempt(**overrides))
                )

    def test_an_unknown_state_is_refused(self):
        self.assertEqual(
            "unknown check state",
            validation.validate_attempt(self._attempt(state="probably_fine")),
        )

    def test_an_attempt_that_approves_adopts_or_applies_is_refused(self):
        # Passing evidence is not approval: every attempt pins all three false.
        for field in ("approved", "adopted", "applied"):
            with self.subTest(field=field):
                self.assertEqual(
                    "attempt must not approve, adopt or apply",
                    validation.validate_attempt(self._attempt(**{field: True})),
                )

    def test_a_bad_ordinal_is_refused(self):
        for ordinal in (0, -1, "1", True, None):
            with self.subTest(ordinal=ordinal):
                self.assertEqual(
                    "invalid ordinal",
                    validation.validate_attempt(self._attempt(ordinal=ordinal)),
                )

    def test_a_missing_or_malformed_identity_is_refused(self):
        for identity in (None, "", "no-prefix"):
            attempt = self._attempt()
            attempt["attempt_id"] = identity
            with self.subTest(identity=identity):
                self.assertEqual(
                    "missing or malformed attempt_id",
                    validation.validate_attempt(attempt),
                )
        attempt = self._attempt()
        del attempt["attempt_id"]
        self.assertEqual(
            "missing or malformed attempt_id", validation.validate_attempt(attempt)
        )

    def test_an_identity_that_does_not_match_the_content_is_refused(self):
        # The identity is re-derived from the record, so tampering with a field
        # after the identity was taken is detected rather than trusted.
        attempt = self._attempt()
        attempt["ordinal"] = 7
        self.assertEqual(
            "attempt_id does not match the attempt content",
            validation.validate_attempt(attempt),
        )

    # -- the overall state -----------------------------------------------

    def test_the_overall_state_is_the_most_severe_any_check_reached(self):
        # Pins the documented precedence (the comment beside ``_SEVERITY``): the
        # overall state is the first severity any check holds, so one failure is
        # never averaged away by passing neighbours.
        severity = (
            validation.STATE_FAILED,
            validation.STATE_TIMED_OUT,
            validation.STATE_CANCELLED,
            validation.STATE_REFUSED,
            validation.STATE_UNAVAILABLE,
            validation.STATE_UNKNOWN,
            validation.STATE_PASSED,
        )
        self.assertEqual(set(validation.CHECK_STATES), set(severity))
        for index, state in enumerate(severity):
            with self.subTest(state=state):
                self.assertEqual(state, validation._overall_state([state]))
                for milder in severity[index + 1:]:
                    self.assertEqual(
                        state, validation._overall_state([milder, state, milder])
                    )

    # -- the runner-token translation tables -----------------------------

    def test_no_runner_error_token_can_be_recorded_as_a_pass(self):
        # The property that matters most about these two tables: a token the
        # runner emits when something went wrong must never reach the state that
        # says everything passed.
        for name, table in (
            ("runner", validation._RUNNER_ERROR_STATES),
            ("candidate", validation._CANDIDATE_ERROR_STATES),
        ):
            with self.subTest(table=name):
                self.assertTrue(table)
                for token, state in sorted(table.items()):
                    self.assertIn(state, validation.CHECK_STATES, token)
                    self.assertNotEqual(validation.STATE_PASSED, state, token)

    def test_the_candidate_table_treats_a_pre_dispatch_refusal_as_refused(self):
        # A digest that is absent or does not match, a candidate root that is
        # not the verified shape and a declaration that is not safe all mean
        # nothing was dispatched, which is what ``refused`` says and ``failed``
        # would not.
        for token in (
            container_runner.REASON_DIGEST_ABSENT,
            container_runner.REASON_DIGEST_MISMATCH,
            container_runner.REASON_CANDIDATE_ROOT_INVALID,
            container_runner.REASON_DECLARED_FILES_INVALID,
        ):
            with self.subTest(token=token):
                self.assertEqual(
                    validation.STATE_REFUSED,
                    validation._CANDIDATE_ERROR_STATES[token],
                )

    def test_the_runner_timeout_token_is_the_one_mapped_to_timed_out(self):
        for name, table in (
            ("runner", validation._RUNNER_ERROR_STATES),
            ("candidate", validation._CANDIDATE_ERROR_STATES),
        ):
            with self.subTest(table=name):
                self.assertEqual(
                    validation.STATE_TIMED_OUT, table[app_package.STATE_TIMEOUT]
                )


if __name__ == "__main__":
    unittest.main()
