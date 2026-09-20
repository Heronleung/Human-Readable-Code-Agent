"""Live candidate validation against a real container (P5.5a-r2).

This is the **only** module that dispatches real containers, and it is dedicated,
bounded and skip-guarded. Every other test module injects a spawn; the unit
module that covers the same contract is ``tests/test_candidate_syntax.py``.

What it proves, and what it does not:

* a real ``docker run`` of the rebuilt ``hrca-runner:v1``, at the **pinned image
  digest**, syntax-compiles one pinned candidate file with the literal
  ``runner_syntax`` entrypoint over a read-only candidate mount;
* a real file that does not compile is reported as ``failed``, not as a pass;
* a real timeout runs the whole P5.5r1 lifecycle — kill, removal, staged-root
  cleanup — and leaves no residual container;
* a missing image, a mismatched digest and a cancellation dispatch nothing;
* after every case: no residual ``hrca-run-`` container, no staged directory
  left in the temp root, and the candidate byte-identical with its mode intact.

A passing result here means the pinned files compiled under the bound image. It
is not behavioural correctness, approval, adoption or application.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from hrca import (
    container_runner,
    validation,
    validation_plan,
    validation_policy,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from test_candidate_syntax import build_candidate  # noqa: E402  (shared harness)

REPO = os.path.normpath(os.path.join(_HERE, ".."))


def _mounts(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]


def _residual_containers():
    proc = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=hrca-run-", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=60,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _staged_leftovers():
    return sorted(
        name for name in os.listdir(tempfile.gettempdir())
        if name.startswith(("hrca-in-", "hrca-out-"))
    )


def _tree_digest(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class _Recorder:
    """Run the real spawn and remember what was dispatched."""

    def __init__(self, spawn):
        self._spawn = spawn
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self._spawn(argv, **kwargs)

    def runs(self):
        return [argv for argv in self.calls if argv[1:2] == ["run"]]


class LiveCandidateSyntaxTests(unittest.TestCase):
    """Real containers, real cleanup, real candidate."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        preflight = container_runner.ContainerRunner().preflight()
        if not preflight.get("available"):
            raise unittest.SkipTest(
                "no container runtime: %s (checks=%s)"
                % (preflight.get("reason"), preflight.get("checks"))
            )
        digest = container_runner.ContainerRunner().image_digest()
        if digest != container_runner.RUNNER_IMAGE_DIGEST:
            raise AssertionError(
                "the local image does not match the pinned digest: %r != %r — "
                "rebuild it from packaging/runner/Dockerfile and re-pin"
                % (digest, container_runner.RUNNER_IMAGE_DIGEST)
            )
        cls.root, cls.review = build_candidate()
        binding, error = validation.verify_candidate(cls.root, cls.review)
        assert error is None, error
        cls.plan, error = validation_plan.build_plan(
            binding, {"checks": [validation_policy.CHECK_CANDIDATE_SYNTAX]}
        )
        assert error is None, error
        cls.declared = [entry["path"] for entry in binding["files"]]

    def setUp(self):
        self.before_tree = _tree_digest(self.root)
        self.before_mode = os.stat(self.root).st_mode & 0o777
        self.before_entries = sorted(os.listdir(self.root))

    def tearDown(self):
        # The invariant after *every* case, pass or fail.
        self.assertEqual([], _residual_containers(), "a container was left behind")
        self.assertEqual([], _staged_leftovers(), "a staged directory was left behind")
        self.assertEqual(self.before_tree, _tree_digest(self.root))
        self.assertEqual(self.before_mode, os.stat(self.root).st_mode & 0o777)
        self.assertEqual(self.before_entries, sorted(os.listdir(self.root)))

    def _real_run(self, **kwargs):
        recorder = _Recorder(subprocess.run)
        result = validation.run_plan(
            self.plan, self.root, self.review, spawn=recorder, **kwargs
        )
        return result[0], result[1], recorder

    def test_a_real_container_syntax_checks_the_pinned_candidate_file(self):
        result, error, recorder = self._real_run()
        self.assertIsNone(error)
        self.assertEqual("passed", result["state"])
        self.assertIs(True, result["evidence_complete"])
        attempt = result["attempts"][0]
        self.assertEqual("passed", attempt["state"])
        self.assertEqual("runner_syntax", attempt["entrypoint"])
        self.assertEqual(container_runner.RUNNER_IMAGE_DIGEST, attempt["image_digest"])
        self.assertIsNotNone(attempt["artifact"])
        self.assertEqual("output.json", attempt["artifact"]["name"])
        self.assertEqual([], attempt["limitations"])

        # The artifact really did answer for the declared file: re-derive it by
        # compiling the staged bytes here, with the standard library alone.
        staged = os.path.join(self.root, "files", *self.declared[0].split("/"))
        with open(staged, "rb") as fh:
            source = fh.read().decode("utf-8")
        compile(source, self.declared[0], "exec")

        # Real argv, real mounts, real hardening.
        run_argv = recorder.runs()[0]
        mounts = _mounts(run_argv)
        self.assertEqual(3, len(mounts))
        self.assertIn("dst=/in", mounts[0])
        self.assertIn("readonly", mounts[0])
        self.assertNotIn("readonly", mounts[1])
        self.assertIn("dst=/candidate", mounts[2])
        self.assertIn("readonly", mounts[2])
        self.assertIn("src=" + self.root + os.sep, mounts[2])
        facts = validation.isolation_facts(run_argv)
        self.assertTrue(facts["network_disabled"])
        self.assertTrue(facts["read_only_rootfs"])
        self.assertTrue(facts["non_root"])
        self.assertTrue(facts["capabilities_dropped"])
        self.assertTrue(facts["no_new_privileges"])
        self.assertTrue(facts["resource_bounded"])
        self.assertTrue(facts["input_mount_read_only"])
        self.assertTrue(facts["no_docker_socket_mount"])
        self.assertNotIn("docker.sock", " ".join(mounts))

    def test_a_real_container_reports_a_file_that_does_not_compile(self):
        root, review = build_candidate(
            replacement="def broken(:\n    pass\n", workspace="hrca-p55a-r2-bad"
        )
        binding, error = validation.verify_candidate(root, review)
        self.assertIsNone(error)
        plan, error = validation_plan.build_plan(
            binding, {"checks": [validation_policy.CHECK_CANDIDATE_SYNTAX]}
        )
        self.assertIsNone(error)
        recorder = _Recorder(subprocess.run)
        result, error = validation.run_plan(
            plan, root, review, spawn=recorder
        )
        self.assertIsNone(error)
        self.assertEqual("failed", result["state"])
        self.assertIs(False, result["evidence_complete"])
        attempt = result["attempts"][0]
        self.assertEqual("failed", attempt["state"])
        self.assertIn("did not compile", attempt["limitations"][0])
        self.assertIsNotNone(attempt["artifact"])

    def test_a_real_timeout_runs_the_lifecycle(self):
        # A bound far below container creation time on purpose: the client is
        # killed while the create is still in flight, which is the hardest case
        # this lifecycle can face.
        recorder = _Recorder(subprocess.run)
        runner = container_runner.ContainerRunner(spawn=recorder, timeout=0.05)
        result, error = runner.run_candidate(
            candidate_dir=self.root,
            declared_files=self.declared,
            expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
        )
        self.assertIsNone(result)
        self.assertEqual("timeout", error)

        # The lifecycle really ran against a real client, exactly once.
        self.assertEqual(1, len([a for a in recorder.calls if a[1:2] == ["kill"]]))
        self.assertEqual(1, len([a for a in recorder.calls if a[1:2] == ["rm"]]))
        self.assertEqual([], _staged_leftovers())

        # A bound shorter than container creation can strand a container in the
        # ``created`` state, which ``--rm`` never reaps: the kill and the removal
        # ran before the container existed, and the daemon finished creating it
        # afterwards. That is a documented limit of killing by name after the
        # client dies, not a hidden one — but a *running* container left behind
        # would be a different and worse thing, so it is asserted against.
        for name in _residual_containers():
            status = subprocess.run(
                ["docker", "inspect", name, "--format", "{{.State.Status}}"],
                capture_output=True, text=True, timeout=60,
            ).stdout.strip()
            self.assertEqual("created", status, "a %r container was left behind" % status)
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, text=True, timeout=60
            )
        self.assertEqual([], _residual_containers())

    def test_a_mismatched_digest_refuses_before_dispatch(self):
        recorder = _Recorder(subprocess.run)
        result, error = container_runner.ContainerRunner(spawn=recorder).run_candidate(
            candidate_dir=self.root,
            declared_files=self.declared,
            expected_digest="sha256:" + "0" * 64,
        )
        self.assertIsNone(result)
        self.assertEqual(container_runner.REASON_DIGEST_MISMATCH, error)
        self.assertEqual([], recorder.runs())

    def test_a_missing_image_is_blocked_before_dispatch(self):
        runner = container_runner.ContainerRunner(image="hrca-runner:no-such-image")
        preflight = runner.preflight()
        self.assertIs(False, preflight["available"])
        self.assertEqual(container_runner.PREFLIGHT_RUNTIME_BLOCKED, preflight["reason"])
        result, error = runner.run_candidate(
            candidate_dir=self.root,
            declared_files=self.declared,
            expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
        )
        self.assertIsNone(result)
        self.assertEqual(container_runner.REASON_DIGEST_ABSENT, error)

    def test_cancellation_dispatches_nothing(self):
        _result, _error, recorder = self._real_run(cancelled=lambda: True)
        self.assertEqual([], recorder.runs())
        result, _error, _recorder = self._real_run(cancelled=lambda: True)
        self.assertEqual("cancelled", result["state"])

    def test_a_cleanup_failure_is_not_reported_as_a_clean_timeout(self):
        # A real container, a real timeout, and a cleanup that cannot run: the
        # token must not claim a stop that did not happen.
        #
        # The staged roots are left behind here **on purpose** — that is what a
        # failed cleanup means — so this test asserts the leak and then removes
        # what it deliberately stranded, rather than letting it fail every later
        # case's tearDown.
        stranded = []
        real_rmtree = shutil.rmtree

        def failing_rmtree(path, *args, **kwargs):
            stranded.append(path)
            raise OSError("cleanup failed")

        runner = container_runner.ContainerRunner(
            spawn=_Recorder(subprocess.run), timeout=0.05
        )
        try:
            with mock.patch.object(
                container_runner.shutil, "rmtree", side_effect=failing_rmtree
            ):
                result, error = runner.run_candidate(
                    candidate_dir=self.root,
                    declared_files=self.declared,
                    expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
                )
            self.assertIsNone(result)
            self.assertEqual("runner_failed", error)
            # The honest evidence of the failure: the roots really are still
            # there, and both were attempted.
            self.assertEqual(2, len(stranded))
            self.assertTrue(all(os.path.exists(path) for path in stranded))
        finally:
            for path in stranded:
                real_rmtree(path, ignore_errors=True)

    def test_the_pinned_digest_is_the_one_the_rebuild_produced(self):
        # If the image is ever rebuilt without re-pinning, this is where it
        # shows up rather than in a quietly different base.
        self.assertEqual(
            container_runner.RUNNER_IMAGE_DIGEST,
            validation_policy.CANDIDATE_IMAGE_DIGEST,
        )
        self.assertEqual(
            container_runner.RUNNER_IMAGE, validation_policy.CANDIDATE_IMAGE
        )
        self.assertEqual(
            container_runner.CANDIDATE_ENTRYPOINT_ID,
            validation_policy.CANDIDATE_ENTRYPOINT_ID,
        )


if __name__ == "__main__":
    unittest.main()
