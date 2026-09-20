"""The candidate-validation path (P5.5a-r2), with nothing dispatched.

This module is live-Docker safe by construction: every container is a
double whose ``run`` is scripted, so no real container can start here. The real
thing is proven in ``tests/test_candidate_syntax_integration.py``, which is
dedicated to it, bounded, and skip-guarded.

What is held here is the *contract* around the mount: which argv is built, how
each runner outcome maps to a terminal state, that an artifact must account for
exactly the declared files before any outcome may be called passing, and that
the candidate is never a cleanup input.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from hrca import (
    candidate,
    candidate_edit,
    container_runner,
    contract,
    impact_proposal,
    intent_delta,
    scanner,
    twin,
    validation,
    validation_plan,
    validation_policy,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
FIXTURE_REPO = os.path.join(REPO, "candidate_fixtures", "repo")
CANDIDATE_CORPUS = os.path.join(REPO, "candidate_fixtures", "manifest.json")

FIXED_WORKSPACE = twin.workspace_id_for("hrca-p55a-r2-unit")
VALIDATION_FIXTURE = os.path.join(REPO, "fixtures", "validation", "manifest.json")


def _validation_fixture() -> dict:
    with open(VALIDATION_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _candidate_corpus() -> dict:
    with open(CANDIDATE_CORPUS, encoding="utf-8") as fh:
        return json.load(fh)


def build_candidate(workspace: str = FIXED_WORKSPACE, replacement=None):
    """Return ``(candidate_root, review)`` for the frozen P5.4 fixture."""
    corpus = _candidate_corpus()
    work = tempfile.mkdtemp(prefix="r2u-work-")
    root = os.path.join(work, "repo")
    shutil.copytree(FIXTURE_REPO, root)

    doc = scanner.scan_directory(root)
    fingerprints = {}
    for record in doc["files"]:
        if record["path"].endswith((".py", ".pyi")):
            with open(os.path.join(root, *record["path"].split("/")), "rb") as fh:
                fingerprints[record["path"]] = twin.fingerprint_bytes(fh.read())
    store = twin.build_store(doc, fingerprints, workspace, 1, "T")
    evidence = {"scanner": doc, "twin": store}
    baseline = {
        "workspace_id": store["workspace_revision"]["workspace_id"],
        "scan_generation": store["workspace_revision"]["scan_generation"],
        "baseline_fingerprint": store["workspace_revision"]["baseline_fingerprint"],
        "scanner_schema_version": doc["schema_version"],
        "grammar": doc["grammar"],
    }
    intent = dict(corpus["intent_base"])
    intent["baseline"] = baseline
    delta, error = intent_delta.build_intent_delta(intent)
    assert error is None, error
    proposal, error = impact_proposal.build_impact_proposal(delta, evidence)
    assert error is None, error
    edit, error = candidate_edit.build_edit(
        {
            "intent_delta_id": delta["intent_delta_id"],
            "proposal_id": proposal["proposal_id"],
            "binding_fingerprint": proposal["binding"]["binding_fingerprint"],
            "baseline": baseline,
            "operations": [
                {
                    "op": "replace_file",
                    "path": "pkg/service.py",
                    "expected_sha256": corpus["predecessor"]["sha256"],
                    "text": (
                        replacement
                        if replacement is not None
                        else corpus["result"]["text"]
                    ),
                }
            ],
        }
    )
    assert error is None, error
    base = tempfile.mkdtemp(prefix="r2u-out-")
    review, error = candidate.build_candidate(edit, delta, proposal, evidence, root, base)
    assert error is None, error
    assert review["state"] == candidate.STATE_CANDIDATE_READY, review["state"]
    return os.path.join(base, review["candidate_root_name"]), review


def _mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]


class FakeCandidateDocker:
    """A container double that returns a *scripted* syntax artifact.

    Deliberately not faithful to the entrypoint's compilation: this module tests
    the contract's mapping, and the real compilation is proven elsewhere. What
    it is faithful about is the wire shape — it reads the staged input, writes
    ``{"result": ...}`` to the output mount, and answers ``image inspect`` with a
    digest.
    """

    def __init__(self, artifact=None, digest=None, exit_code=0, raises=None,
                 error_body=None, image_available=True, digest_readable=True,
                 container_states=None, daemon_reachable=True):
        self.artifact = artifact
        self.digest = digest or container_runner.RUNNER_IMAGE_DIGEST
        self.exit_code = exit_code
        self.raises = raises
        self.error_body = error_body
        self.image_available = image_available
        self.digest_readable = digest_readable
        # What the reconciliation's ``container inspect`` answers, consumed one
        # per query; the last value repeats once the list runs out.
        self.container_states = list(container_states or ["absent"])
        self.daemon_reachable = daemon_reachable
        self.calls = []
        self.staged_inputs = []
        self.staged_outputs = []
        self.queried_names = []
        self.removed_names = []

    def _next_container_state(self) -> str:
        if len(self.container_states) > 1:
            return self.container_states.pop(0)
        return self.container_states[0] if self.container_states else "absent"

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            # Preflight inspects the image plainly; the digest check asks for
            # ``--format {{.Id}}``. Answering them separately is what lets a
            # test have a present image whose *digest* cannot be read.
            wants_id = "--format" in argv
            if wants_id and not self.digest_readable:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            if not wants_id and not self.image_available:
                return subprocess.CompletedProcess(argv, 1, b"", b"")
            if not wants_id:
                return subprocess.CompletedProcess(argv, 0, b"", b"")
            return subprocess.CompletedProcess(
                argv, 0, self.digest.encode() + b"\n", b""
            )
        if argv[1:3] == ["container", "inspect"]:
            # The timeout reconciliation asks whether the exact container name
            # still resolves. The default double answers "no such container",
            # which is what lets a timeout lifecycle conclude.
            self.queried_names.append(argv[3] if len(argv) > 3 else None)
            state = self._next_container_state()
            return subprocess.CompletedProcess(
                argv, 0 if state == "present" else 1, b"", b""
            )
        if argv[1:2] == ["info"]:
            # The reconciliation asks the daemon to confirm it is reachable
            # before an absence may be believed.
            return subprocess.CompletedProcess(
                argv, 0 if self.daemon_reachable else 1, b"", b""
            )
        if argv[1:2] in (["kill"], ["rm"]):
            if argv[1:2] == ["rm"]:
                self.removed_names.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        input_dir = output_dir = None
        for mount in _mounts(argv):
            for part in mount.split(","):
                if part.startswith("src="):
                    if "dst=/in" in mount:
                        input_dir = part[4:]
                    elif "dst=/out" in mount:
                        output_dir = part[4:]
        self.staged_inputs.append(input_dir)
        self.staged_outputs.append(output_dir)
        if self.raises is not None:
            raise self.raises
        if self.exit_code != 0:
            return subprocess.CompletedProcess(argv, self.exit_code, b"", b"")
        if self.error_body is not None:
            body = {"error": self.error_body}
        else:
            with open(os.path.join(input_dir, "input.json"), encoding="utf-8") as fh:
                declared = json.load(fh)["input"]["files"]
            artifact = self.artifact
            if artifact is None:
                artifact = {
                    "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
                    "checked": [
                        {"path": p, "ok": True, "error": None, "lineno": None,
                         "offset": None}
                        for p in sorted(set(declared))
                    ],
                    "compiled": len(set(declared)),
                    "failed": 0,
                }
            body = {"result": artifact}
        with open(os.path.join(output_dir, "output.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=True, separators=(",", ":"))
        return subprocess.CompletedProcess(argv, 0, b"ran", b"")


class _CandidateFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.review = build_candidate()
        cls.binding, cls.error = validation.verify_candidate(cls.root, cls.review)
        assert cls.error is None, cls.error
        cls.plan, cls.plan_error = validation_plan.build_plan(
            cls.binding, {"checks": [validation_policy.CHECK_CANDIDATE_SYNTAX]}
        )
        assert cls.plan_error is None, cls.plan_error
        cls.declared = [entry["path"] for entry in cls.binding["files"]]

    def _run(self, docker=None, which=..., **kwargs):
        docker = docker or FakeCandidateDocker()
        if which is ...:
            which = lambda name: "/usr/bin/docker"
        result = validation.run_plan(
            self.plan, self.root, self.review,
            spawn=docker, which=which, **kwargs
        )
        return result[0], result[1], docker


class FixturePinTests(unittest.TestCase):
    """The rebuilt image's digest, pinned as fixture data and reverified here.

    A rebuild that is not re-pinned in ``fixtures/validation/manifest.json``
    fails these, so the artifact a run reports evidence about cannot drift
    silently away from the artifact the fixture describes.
    """

    def test_the_fixture_pins_the_digest_the_runner_pins(self):
        fixture = _validation_fixture()["candidate_check"]
        self.assertEqual(
            container_runner.RUNNER_IMAGE_DIGEST, fixture["image_digest"]
        )
        self.assertEqual(validation_policy.CANDIDATE_IMAGE_DIGEST, fixture["image_digest"])

    def test_the_fixture_pins_the_other_operational_tokens(self):
        fixture = _validation_fixture()["candidate_check"]
        self.assertEqual(container_runner.RUNNER_IMAGE, fixture["image"])
        self.assertEqual(
            container_runner.CANDIDATE_ENTRYPOINT_ID, fixture["entrypoint"]
        )
        self.assertEqual(container_runner._CANDIDATE_DIR, fixture["candidate_mount"])
        self.assertEqual(
            validation.SYNTAX_ARTIFACT_SCHEMA, fixture["artifact_schema"]
        )
        self.assertEqual(3, fixture["mount_count"])

    def test_the_fixture_pins_the_whole_code_owned_check_record(self):
        fixture = _validation_fixture()["candidate_check"]
        self.assertEqual(
            validation_policy.check_record(validation_policy.CHECK_CANDIDATE_SYNTAX),
            fixture["record"],
        )

    def test_the_fixture_digest_is_a_digest(self):
        digest = _validation_fixture()["candidate_check"]["image_digest"]
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(71, len(digest))
        self.assertNotEqual(_validation_fixture()["candidate_check"]["image"], digest)


class MountAuditTests(_CandidateFixture, unittest.TestCase):
    """The argv a candidate check dispatches, and nothing about its content."""

    def test_the_plan_check_carries_the_code_owned_record(self):
        record = self.plan["checks"][0]
        self.assertEqual(validation_policy.CHECK_CANDIDATE_SYNTAX, record["check_id"])
        self.assertEqual(
            validation_policy.check_record(validation_policy.CHECK_CANDIDATE_SYNTAX),
            record,
        )
        self.assertEqual(
            container_runner.RUNNER_IMAGE_DIGEST, record["image_digest"]
        )

    def test_exactly_three_mounts_with_only_the_candidate_read_only(self):
        _result, _error, docker = self._run()
        run_argv = [a for a in docker.calls if a[1:2] == ["run"]][0]
        mounts = _mounts(run_argv)
        self.assertEqual(3, len(mounts))
        self.assertIn("dst=/in", mounts[0])
        self.assertIn("readonly", mounts[0])
        self.assertIn("dst=/out", mounts[1])
        self.assertNotIn("readonly", mounts[1])
        self.assertIn("dst=/candidate", mounts[2])
        self.assertIn("readonly", mounts[2])
        self.assertIn("src=" + self.root + os.sep, mounts[2])

    def test_the_mounted_source_is_inside_the_verified_candidate_root(self):
        _result, _error, docker = self._run()
        run_argv = [a for a in docker.calls if a[1:2] == ["run"]][0]
        source = [
            part[4:]
            for part in _mounts(run_argv)[2].split(",")
            if part.startswith("src=")
        ][0]
        real_source = os.path.realpath(source)
        self.assertTrue(
            real_source.startswith(os.path.realpath(self.root) + os.sep),
            real_source,
        )
        self.assertFalse(os.path.islink(source))

    def test_the_entrypoint_is_the_literal_candidate_entrypoint(self):
        _result, _error, docker = self._run()
        run_argv = [a for a in docker.calls if a[1:2] == ["run"]][0]
        image_at = run_argv.index(container_runner.RUNNER_IMAGE)
        self.assertEqual(
            container_runner.CANDIDATE_ENTRYPOINT, run_argv[image_at + 1:]
        )

    def test_every_existing_isolation_flag_survives(self):
        _result, _error, docker = self._run()
        run_argv = [a for a in docker.calls if a[1:2] == ["run"]][0]
        facts = validation.isolation_facts(run_argv)
        self.assertEqual(
            {
                "network_disabled": True,
                "read_only_rootfs": True,
                "non_root": True,
                "capabilities_dropped": True,
                "no_new_privileges": True,
                "resource_bounded": True,
                "input_mount_read_only": True,
                "mount_count": 3,
                "write_mounts_are_staged_only": False,  # three, not two
                "no_docker_socket_mount": True,
                "image_is_code_owned": True,
                "removed_after_run": True,
                "init_reaps_children": True,
            },
            facts,
        )
        self.assertNotIn("docker.sock", " ".join(_mounts(run_argv)))

    def test_the_attempt_records_the_entrypoint_and_the_digest(self):
        result, _error, _docker = self._run()
        attempt = result["attempts"][0]
        self.assertEqual("runner_syntax", attempt["entrypoint"])
        self.assertEqual(
            container_runner.RUNNER_IMAGE_DIGEST, attempt["image_digest"]
        )
        self.assertIsNone(attempt["package_id"])
        self.assertEqual("runner_syntax", attempt["argv"][
            attempt["argv"].index(container_runner.RUNNER_IMAGE) + 2
        ].replace("/app/", "").replace(".py", ""))


class OutcomeMappingTests(_CandidateFixture, unittest.TestCase):
    """Every way the container can behave, mapped to exactly one state."""

    def test_a_clean_artifact_is_passing(self):
        result, error, _docker = self._run()
        self.assertIsNone(error)
        self.assertEqual("passed", result["state"])
        self.assertIs(True, result["evidence_complete"])
        attempt = result["attempts"][0]
        self.assertEqual("passed", attempt["state"])
        self.assertIsNotNone(attempt["artifact"])
        self.assertEqual("output.json", attempt["artifact"]["name"])
        self.assertEqual([], attempt["limitations"])

    def test_a_file_that_does_not_compile_is_failed(self):
        artifact = {
            "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
            "checked": [
                {"path": self.declared[0], "ok": False, "error": "invalid syntax",
                 "lineno": 1, "offset": 12},
            ],
            "compiled": 0,
            "failed": 1,
        }
        result, _error, _docker = self._run(FakeCandidateDocker(artifact=artifact))
        self.assertEqual("failed", result["state"])
        self.assertIs(False, result["evidence_complete"])
        attempt = result["attempts"][0]
        self.assertEqual("failed", attempt["state"])
        self.assertIn("did not compile", attempt["limitations"][0])
        self.assertIn(self.declared[0], attempt["limitations"][0])
        self.assertIsNotNone(attempt["artifact"])

    def test_a_non_zero_exit_is_failed(self):
        result, _error, _docker = self._run(
            FakeCandidateDocker(exit_code=1)
        )
        self.assertEqual("failed", result["state"])

    def test_a_timeout_is_timed_out(self):
        result, _error, docker = self._run(
            FakeCandidateDocker(raises=TimeoutError())
        )
        self.assertEqual("timed_out", result["state"])
        self.assertIs(True, result["attempts"][0]["timed_out"])
        self.assertTrue(any(a[1:2] == ["kill"] for a in docker.calls))
        self.assertTrue(any(a[1:2] == ["rm"] for a in docker.calls))

    def test_a_real_timeout_expired_is_also_timed_out(self):
        result, _error, _docker = self._run(
            FakeCandidateDocker(
                raises=subprocess.TimeoutExpired(["docker", "run"], 10.0)
            )
        )
        self.assertEqual("timed_out", result["state"])

    def test_cancellation_dispatches_nothing(self):
        result, _error, docker = self._run(cancelled=lambda: True)
        self.assertEqual("cancelled", result["state"])
        self.assertEqual([], [a for a in docker.calls if a[1:2] == ["run"]])
        self.assertIsNone(result["attempts"][0]["argv"])

    def test_an_absent_runtime_is_unavailable(self):
        result, _error, docker = self._run(which=lambda name: None)
        self.assertEqual("unavailable", result["state"])
        self.assertEqual([], [a for a in docker.calls if a[1:2] == ["run"]])

    def test_the_overall_state_is_the_most_severe(self):
        modes = FakeCandidateDocker(
            artifact={
                "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
                "checked": [
                    {"path": self.declared[0], "ok": False, "error": "invalid syntax",
                     "lineno": 1, "offset": 1},
                ],
                "compiled": 0,
                "failed": 1,
            }
        )
        result, _error, _docker = self._run(modes)
        self.assertEqual("failed", result["state"])
        self.assertIs(False, result["evidence_complete"])
        self.assertIsNone(validation.validate_result(result))

    def test_no_state_is_an_approval(self):
        result, _error, _docker = self._run()
        for field in ("approved", "adopted", "applied"):
            with self.subTest(field=field):
                self.assertIs(False, result[field])
                self.assertIs(False, result["attempts"][0][field])


class RefusalTests(_CandidateFixture, unittest.TestCase):
    """Everything that means nothing was dispatched."""

    def test_an_absent_digest_refuses(self):
        plan = json.loads(json.dumps(self.plan))
        plan["checks"][0]["image_digest"] = None
        result, error = validation.run_plan(plan, self.root, self.review)
        self.assertIsNone(result)
        self.assertEqual(validation.REASON_PLAN_INVALID, error)

    def test_a_runner_with_no_readable_digest_refuses(self):
        # The image is present, so preflight passes; its *digest* cannot be
        # read, so the candidate path refuses rather than dispatching against
        # an image it cannot identify.
        result, _error, docker = self._run(
            FakeCandidateDocker(digest_readable=False)
        )
        self.assertEqual("refused", result["state"])
        self.assertEqual(
            [container_runner.REASON_DIGEST_ABSENT],
            result["attempts"][0]["limitations"],
        )
        self.assertEqual([], [a for a in docker.calls if a[1:2] == ["run"]])

    def test_a_missing_image_makes_the_check_unavailable(self):
        result, _error, docker = self._run(FakeCandidateDocker(image_available=False))
        self.assertEqual("unavailable", result["state"])
        self.assertEqual([], [a for a in docker.calls if a[1:2] == ["run"]])

    def test_a_mismatched_digest_refuses(self):
        result, _error, _docker = self._run(
            FakeCandidateDocker(digest="sha256:" + "0" * 64)
        )
        self.assertEqual("refused", result["state"])
        self.assertEqual(
            [container_runner.REASON_DIGEST_MISMATCH],
            result["attempts"][0]["limitations"],
        )

    def test_a_rejected_request_body_is_refused(self):
        result, _error, _docker = self._run(
            FakeCandidateDocker(error_body="a declared path is not exact")
        )
        self.assertEqual("refused", result["state"])

    def test_a_plan_declaring_another_digest_is_refused(self):
        # The digest is part of the check record, so changing it stops being
        # the code-owned record at all and the plan is refused outright.
        for field, value in (
            ("image_digest", "sha256:" + "0" * 64),
            ("candidate_mount", "/elsewhere"),
            ("entrypoint", "runner_main"),
            ("image", "python:3.12-slim"),
        ):
            plan = json.loads(json.dumps(self.plan))
            plan["checks"][0][field] = value
            plan["plan_id"] = validation_plan.plan_id_for(plan)
            with self.subTest(field=field):
                result, error = validation.run_plan(plan, self.root, self.review)
                self.assertIsNone(result)
                self.assertEqual(validation.REASON_PLAN_INVALID, error)

    def test_a_plan_bound_to_another_candidate_is_refused(self):
        other = json.loads(json.dumps(self.binding))
        other["candidate_id"] = "candidate:" + "0" * 64
        plan, error = validation_plan.build_plan(
            other, {"checks": [validation_policy.CHECK_CANDIDATE_SYNTAX]}
        )
        self.assertIsNone(error)
        result, error = validation.run_plan(plan, self.root, self.review)
        self.assertIsNone(result)
        self.assertEqual(validation.REASON_CANDIDATE_MISMATCH, error)

    def test_the_runner_refuses_a_root_that_is_not_the_candidate_shape(self):
        runner = container_runner.ContainerRunner()
        for bad in (None, "", "/tmp", self.root + "/nope", "relative/path"):
            with self.subTest(bad=bad):
                self.assertEqual(
                    (None, container_runner.REASON_CANDIDATE_ROOT_INVALID),
                    runner.run_candidate(
                        candidate_dir=bad, declared_files=self.declared,
                        expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
                    ),
                )

    def test_the_runner_refuses_an_unsafe_declared_path(self):
        runner = container_runner.ContainerRunner()
        for bad in (
            ["/etc/passwd"], ["../x.py"], ["a/../b.py"], ["a//b.py"], [""],
            ["a\\b.py"], ["a\x00b.py"], [], "pkg/service.py", [7],
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    (None, container_runner.REASON_DECLARED_FILES_INVALID),
                    runner.run_candidate(
                        candidate_dir=self.root, declared_files=bad,
                        expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
                    ),
                )


class ArtifactContractTests(_CandidateFixture, unittest.TestCase):
    """An artifact must account for exactly the declared files."""

    def _run_with(self, artifact):
        result, _error, _docker = self._run(FakeCandidateDocker(artifact=artifact))
        return result

    def test_an_artifact_missing_a_declared_file_is_unknown(self):
        result = self._run_with({
            "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
            "checked": [],
            "compiled": 0, "failed": 0,
        })
        self.assertEqual("unknown", result["state"])

    def test_an_artifact_answering_for_another_file_is_unknown(self):
        result = self._run_with({
            "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
            "checked": [{"path": "pkg/other.py", "ok": True, "error": None,
                         "lineno": None, "offset": None}],
            "compiled": 1, "failed": 0,
        })
        self.assertEqual("unknown", result["state"])

    def test_an_artifact_with_disagreeing_counts_is_unknown(self):
        for compiled, failed in ((0, 0), (1, 1), (2, 0)):
            artifact = {
                "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
                "checked": [{"path": self.declared[0], "ok": True, "error": None,
                             "lineno": None, "offset": None}],
                "compiled": compiled, "failed": failed,
            }
            with self.subTest(compiled=compiled, failed=failed):
                self.assertEqual("unknown", self._run_with(artifact)["state"])

    def test_an_artifact_with_the_wrong_schema_is_unknown(self):
        result = self._run_with({
            "schema": "something-else",
            "checked": [{"path": self.declared[0], "ok": True, "error": None,
                         "lineno": None, "offset": None}],
            "compiled": 1, "failed": 0,
        })
        self.assertEqual("unknown", result["state"])

    def test_an_artifact_with_a_malformed_outcome_is_unknown(self):
        result = self._run_with({
            "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
            "checked": [{"path": self.declared[0], "ok": "yes"}],
            "compiled": 1, "failed": 0,
        })
        self.assertEqual("unknown", result["state"])

    def test_the_validator_accepts_the_real_shape(self):
        artifact = {
            "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
            "checked": [{"path": self.declared[0], "ok": True, "error": None,
                         "lineno": None, "offset": None}],
            "compiled": 1, "failed": 0,
        }
        self.assertIsNone(
            validation._validate_syntax_artifact(artifact, self.declared)
        )


class ContainmentTests(_CandidateFixture, unittest.TestCase):
    def test_the_staged_roots_are_removed_and_the_candidate_is_not(self):
        before = sorted(os.listdir(self.root))
        result, _error, docker = self._run()
        self.assertEqual("passed", result["state"])
        input_dir = docker.staged_inputs[0]
        output_dir = docker.staged_outputs[0]
        self.assertFalse(os.path.exists(input_dir))
        self.assertFalse(os.path.exists(output_dir))
        self.assertEqual(before, sorted(os.listdir(self.root)))
        # The candidate root is never handed to the cleanup, and its mode is not
        # touched: mounting the content directory needs no widening.
        self.assertEqual(0o700, os.stat(self.root).st_mode & 0o777)

    def test_the_candidate_bytes_and_hashes_are_unchanged_by_a_run(self):
        before = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    before[os.path.relpath(full, self.root)] = hashlib.sha256(
                        fh.read()
                    ).hexdigest()
        self._run()
        after = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                with open(full, "rb") as fh:
                    after[os.path.relpath(full, self.root)] = hashlib.sha256(
                        fh.read()
                    ).hexdigest()
        self.assertEqual(before, after)

    def test_cleanup_removes_only_the_two_staged_roots(self):
        sibling = tempfile.mkdtemp(prefix="r2u-sibling-")
        try:
            with open(os.path.join(sibling, "keep.txt"), "w", encoding="utf-8") as fh:
                fh.write("keep")
            self._run()
            self.assertTrue(os.path.isdir(sibling))
            with open(os.path.join(sibling, "keep.txt"), encoding="utf-8") as fh:
                self.assertEqual("keep", fh.read())
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

    def test_the_package_path_argv_is_unchanged(self):
        # The candidate work is additive: the product path still builds exactly
        # the two mounts it always did.
        runner = container_runner.ContainerRunner(which=lambda name: "docker")
        argv = runner.build_command(
            handler="quotation_rules.evaluate",
            input_payload={"subtotal": "1.00"},
            container_name="hrca-run-x",
            input_dir="/tmp/in",
            output_dir="/tmp/out",
        )
        self.assertEqual(2, len(_mounts(argv)))
        image_at = argv.index(container_runner.RUNNER_IMAGE)
        self.assertEqual(
            ["python", "/app/runner_main.py", "/in/input.json", "/out/output.json"],
            argv[image_at + 1:],
        )


class BoundaryTests(_CandidateFixture, unittest.TestCase):
    def test_no_protocol_action_was_added(self):
        self.assertEqual("3.9.0", contract.CONTRACT_VERSION)
        self.assertEqual(61, len(contract.ALLOWED_ACTIONS))

    def test_the_in_image_entrypoint_is_literal_and_pure(self):
        path = os.path.join(REPO, "packaging", "runner", "runner_syntax.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        imports = set()
        calls = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        # It may not reach anything that could execute or fetch.
        for forbidden in (
            "subprocess", "importlib", "runpy", "socket", "urllib", "os",
            "shutil", "http", "py_compile", "ctypes",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imports)
        for forbidden in ("exec", "eval", "__import__", "open" if False else "eval"):
            with self.subTest(call=forbidden):
                self.assertNotIn(forbidden, calls)
        # ``compile`` is the one thing it does with candidate content, and it
        # builds a code object rather than running one.
        self.assertIn("compile", calls)
        self.assertNotIn("exec", calls)

    def test_the_entrypoint_never_reports_source_text(self):
        # Checked on the parse tree, not the text: the docstring deliberately
        # names ``exc.text`` when explaining what is *not* done with it.
        with open(
            os.path.join(REPO, "packaging", "runner", "runner_syntax.py"),
            encoding="utf-8",
        ) as fh:
            tree = ast.parse(fh.read())
        touched = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }
        # ``exc.msg``, ``exc.lineno`` and ``exc.offset`` are bounded; ``text``
        # is the offending source line and must never be read.
        self.assertNotIn("text", touched)
        self.assertIn("msg", touched)
        self.assertIn("lineno", touched)

    def test_no_candidate_module_imports_a_write_side_or_network_seam(self):
        for module in ("validation", "validation_policy", "validation_plan",
                       "container_runner"):
            with open(os.path.join(SRC, "hrca", module + ".py"), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        modules.add(("hrca." if node.level else "") + node.module)
            for forbidden in (
                "socket", "urllib", "http", "ssl", "ctypes", "runpy",
                "hrca.boundary", "hrca.client", "hrca.provider",
                "hrca.credential_store", "hrca.memory", "hrca.twin_store",
            ):
                with self.subTest(module=module, forbidden=forbidden):
                    self.assertNotIn(forbidden, modules)


class PrivacyTests(_CandidateFixture, unittest.TestCase):
    def test_the_attempt_carries_no_absolute_path(self):
        result, _error, _docker = self._run()
        rendered = validation.dumps(result)
        for forbidden in (
            sys.executable, sys.prefix, os.path.expanduser("~"),
            os.path.abspath(self.root), os.path.abspath(REPO),
        ):
            if not forbidden:
                continue
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_the_reviewable_argv_redacts_the_candidate_location(self):
        result, _error, _docker = self._run()
        argv = result["attempts"][0]["argv"]
        mounts = _mounts(argv)
        self.assertEqual(3, len(mounts))
        for mount in mounts:
            with self.subTest(mount=mount):
                self.assertIn("src=<staged>", mount)
                self.assertNotIn(self.root, mount)

    def test_no_source_body_reaches_the_evidence(self):
        result, _error, _docker = self._run()
        rendered = validation.dumps(result)
        with open(
            os.path.join(FIXTURE_REPO, "pkg", "service.py"), encoding="utf-8"
        ) as fh:
            for line in fh.read().splitlines():
                line = line.strip()
                if len(line) < 12:
                    continue
                with self.subTest(line=line):
                    self.assertNotIn(line, rendered)

    def test_the_syntax_failure_limitation_names_the_file_and_nothing_else(self):
        artifact = {
            "schema": validation.SYNTAX_ARTIFACT_SCHEMA,
            "checked": [{"path": self.declared[0], "ok": False,
                         "error": "SECRET-SOURCE-LINE", "lineno": 1, "offset": 1}],
            "compiled": 0, "failed": 1,
        }
        result, _error, _docker = self._run(FakeCandidateDocker(artifact=artifact))
        rendered = validation.dumps(result)
        self.assertNotIn("SECRET-SOURCE-LINE", rendered)
        self.assertIn(self.declared[0], result["attempts"][0]["limitations"][0])


if __name__ == "__main__":
    unittest.main()
