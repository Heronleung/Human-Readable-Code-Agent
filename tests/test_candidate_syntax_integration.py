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
import uuid
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

    def test_a_creation_race_leaves_no_container_without_manual_reaping(self):
        """The race this repair is about, on the real daemon.

        The bound is far below container creation time on purpose: the client is
        killed while the create is still in flight, the container then appears in
        ``created`` state where ``--rm`` never reaps it, and only the bounded
        exact-name reconciliation can find and remove it.

        Nothing here reaps anything. If production cleanup did not do the work,
        this test fails — the previous version of it did the reaping itself,
        which is not a cleanup path.
        """
        self.assertEqual([], _residual_containers(), "started with a stray container")

        recorder = _Recorder(subprocess.run)
        runner = container_runner.ContainerRunner(spawn=recorder, timeout=0.05)
        result, error = runner.run_candidate(
            candidate_dir=self.root,
            declared_files=self.declared,
            expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
        )
        self.assertIsNone(result)
        # ``timeout`` is only returned once the exact container is conclusively
        # absent, so this token is itself the absence claim.
        self.assertEqual("timeout", error)

        # ...and it is true: no reaping by the test, just an observation.
        self.assertEqual([], _residual_containers())
        self.assertEqual([], _staged_leftovers())

        # Only the one product-owned name was ever named.
        named = {
            argv[-1]
            for argv in recorder.calls
            if argv[1:2] in (["kill"], ["rm"]) or argv[1:3] == ["container", "inspect"]
        }
        self.assertEqual(1, len(named), sorted(named))
        self.assertTrue(named.pop().startswith("hrca-run-"))

    def test_a_container_in_the_product_name_is_removed_by_the_reconciliation(self):
        """The late-appearance branch, made deterministic on the real daemon.

        The timing race itself is narrow — the client must die *after* sending
        the create and *before* the daemon finishes it — so rather than hope it
        fires, this pins the random part of the product-owned name, puts a real
        container in exactly that name's place in the ``created`` state the race
        produces, and disables the initial kill so the **reconciliation is the
        only thing that can remove it**.

        If the reconciliation did not find and force-remove it, the run could
        not report ``timeout`` — and a direct query below confirms the container
        is gone.
        """
        fixed = uuid.uuid4().hex
        name = "hrca-run-" + fixed
        # A stale container from an earlier failure would make the token
        # meaningless, so start from a state this test owns.
        self.assertEqual([], _residual_containers(), "started with a stray container")

        class _Uuid:
            hex = fixed

        created = subprocess.run(
            ["docker", "create", "--name", name, container_runner.RUNNER_IMAGE, "true"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, created.returncode, created.stderr)
        try:
            recorder = _Recorder(subprocess.run)
            with mock.patch.object(
                container_runner.uuid, "uuid4", return_value=_Uuid()
            ), mock.patch.object(
                container_runner.ContainerRunner, "_kill", lambda self, name: True
            ):
                runner = container_runner.ContainerRunner(spawn=recorder, timeout=0.05)
                result, error = runner.run_candidate(
                    candidate_dir=self.root,
                    declared_files=self.declared,
                    expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
                )
            # ``timeout`` is returned only once the container is conclusively
            # absent, and the initial kill was disabled — so this token is the
            # reconciliation's own claim.
            self.assertIsNone(result)
            self.assertEqual("timeout", error)

            inspect = subprocess.run(
                ["docker", "container", "inspect", name],
                capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(0, inspect.returncode, "the late container survived")
            self.assertEqual([], _residual_containers())

            # It asked about, and removed, exactly that name.
            self.assertIn(name, recorder.calls[-1])
            named = {
                argv[-1]
                for argv in recorder.calls
                if argv[1:2] == ["rm"] or argv[1:3] == ["container", "inspect"]
            }
            self.assertEqual({name}, named)
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=60
            )

    def test_an_unrelated_container_is_never_touched(self):
        """A different name, left in the same state the race produces.

        Created from the reviewed image and left in ``created`` state, so it
        looks as much like the raced container as a bystander can. Nothing the
        timeout lifecycle does may touch it.
        """
        probe = "hrca-unrelated-" + uuid.uuid4().hex
        created = subprocess.run(
            ["docker", "create", "--name", probe, container_runner.RUNNER_IMAGE, "true"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, created.returncode, created.stderr)
        try:
            recorder = _Recorder(subprocess.run)
            runner = container_runner.ContainerRunner(spawn=recorder, timeout=0.05)
            _result, error = runner.run_candidate(
                candidate_dir=self.root,
                declared_files=self.declared,
                expected_digest=container_runner.RUNNER_IMAGE_DIGEST,
            )
            self.assertEqual("timeout", error)

            named = {
                argv[-1]
                for argv in recorder.calls
                if argv[1:2] in (["kill"], ["rm"])
                or argv[1:3] == ["container", "inspect"]
            }
            self.assertNotIn(probe, named)
            self.assertEqual(1, len(named), sorted(named))

            # The bystander is still exactly as it was.
            inspect = subprocess.run(
                ["docker", "container", "inspect", probe, "--format", "{{.State.Status}}"],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(0, inspect.returncode)
            self.assertEqual("created", inspect.stdout.strip())
        finally:
            subprocess.run(
                ["docker", "rm", "-f", probe], capture_output=True, text=True, timeout=60
            )

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
