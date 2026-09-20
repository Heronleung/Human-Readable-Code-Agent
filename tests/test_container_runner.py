"""Tests for the P4.3 isolated container-runner adapter.

No real container is used: the Docker client and daemon are faked. The tests
prove the hardened command surface (network disabled, non-root, least
privilege, resource bounds, staged mounts only, no host secrets/socket), the
fail-closed preflight, timeout/cleanup behaviour, and bounded output
collection.
"""

from __future__ import annotations

import json
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from hrca import app_package, container_runner


class _Result:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _output_dir_from_argv(argv):
    for i, arg in enumerate(argv):
        if arg == "--mount" and "dst=/out" in argv[i + 1]:
            mount = argv[i + 1]
            for part in mount.split(","):
                if part.startswith("src="):
                    return part[4:]
    return None


def _staged_dirs_from_argv(argv):
    """Return ``(input_dir, output_dir)`` from a ``docker run`` argv."""
    found = {"/in": None, "/out": None}
    for index, arg in enumerate(argv):
        if arg != "--mount" or index + 1 >= len(argv):
            continue
        mount = argv[index + 1]
        for part in mount.split(","):
            if not part.startswith("src="):
                continue
            for destination in found:
                if f"dst={destination}" in mount:
                    found[destination] = part[4:]
    return found["/in"], found["/out"]


class CommandConstructionTests(unittest.TestCase):
    def setUp(self):
        self.runner = container_runner.ContainerRunner(which=lambda name: "docker")
        self.input_dir = "/tmp/in-dir"
        self.output_dir = "/tmp/out-dir"

    def _command(self):
        return self.runner.build_command(
            handler="quotation_rules.evaluate",
            input_payload={"subtotal": "1.00"},
            container_name="hrca-run-x",
            input_dir=self.input_dir,
            output_dir=self.output_dir,
        )

    def test_network_disabled(self):
        argv = self._command()
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")

    def test_non_root_and_no_new_privileges(self):
        argv = self._command()
        self.assertEqual(argv[argv.index("--user") + 1], "65534:65534")
        self.assertIn("no-new-privileges", argv)

    def test_capabilities_dropped(self):
        argv = self._command()
        self.assertIn("--cap-drop", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")

    def test_read_only_rootfs_and_resource_bounds(self):
        argv = self._command()
        self.assertIn("--read-only", argv)
        for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit", "--tmpfs"):
            self.assertIn(flag, argv)

    def test_process_tree_cleanup_flags(self):
        argv = self._command()
        self.assertIn("--rm", argv)
        self.assertIn("--init", argv)
        self.assertIn("--stop-timeout", argv)

    def test_no_forbidden_mounts(self):
        argv = self._command()
        joined = " ".join(argv)
        for forbidden in ("/home", "/root", "docker.sock", "/etc/passwd"):
            self.assertNotIn(forbidden, joined)
        self.assertNotIn("--volume", argv)

    def test_mounts_only_staged_dirs(self):
        argv = self._command()
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertIn(f"src={self.input_dir}", mounts[0])
        self.assertIn(f"src={self.output_dir}", mounts[1])
        self.assertIn("readonly", mounts[0])
        self.assertNotIn("readonly", mounts[1])


class PreflightTests(unittest.TestCase):
    def test_unavailable_when_docker_missing(self):
        runner = container_runner.ContainerRunner(which=lambda name: None)
        result = runner.preflight()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE)
        self.assertFalse(result["checks"]["docker"])

    def test_unavailable_when_daemon_down(self):
        def spawn(argv, **kw):
            return _Result(returncode=1)

        runner = container_runner.ContainerRunner(which=lambda name: "docker", spawn=spawn)
        result = runner.preflight()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE)
        self.assertFalse(result["checks"]["daemon"])

    def test_blocked_when_image_missing(self):
        def spawn(argv, **kw):
            if argv[:2] == ["docker", "info"]:
                return _Result(returncode=0)
            return _Result(returncode=1)

        runner = container_runner.ContainerRunner(which=lambda name: "docker", spawn=spawn)
        result = runner.preflight()
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], container_runner.PREFLIGHT_RUNTIME_BLOCKED)
        self.assertFalse(result["checks"]["image"])

    def test_available_when_all_pass(self):
        def spawn(argv, **kw):
            return _Result(returncode=0)

        runner = container_runner.ContainerRunner(which=lambda name: "docker", spawn=spawn)
        result = runner.preflight()
        self.assertTrue(result["available"])
        self.assertIsNone(result["reason"])


class RunTests(unittest.TestCase):
    def test_timeout_returns_timeout_and_kills(self):
        calls = []

        def spawn(argv, **kw):
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                raise TimeoutError()
            if argv[1:3] == ["container", "inspect"]:
                # No such container: the reconciliation confirms the absence the
                # token depends on.
                return _Result(returncode=1)
            return _Result(returncode=0)

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=spawn
        )
        result, error = runner.run(
            handler="quotation_rules.evaluate", input_payload={"subtotal": "1.00"}
        )
        self.assertIsNone(result)
        self.assertEqual(error, app_package.STATE_TIMEOUT)
        self.assertTrue(any(a[:2] == ["docker", "kill"] for a in calls))
        self.assertTrue(any(a[:2] == ["docker", "rm"] for a in calls))
        self.assertTrue(any(a[1:3] == ["container", "inspect"] for a in calls))

    def test_nonzero_exit_returns_runner_failed(self):
        def spawn(argv, **kw):
            return _Result(returncode=1)

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=spawn
        )
        result, error = runner.run(
            handler="quotation_rules.evaluate", input_payload={"subtotal": "1.00"}
        )
        self.assertIsNone(result)
        self.assertEqual(error, app_package.STATE_RUNNER_FAILED)

    def test_success_collects_result(self):
        def spawn(argv, **kw):
            output_dir = _output_dir_from_argv(argv)
            with open(os.path.join(output_dir, "output.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {"result": {"discount": "10.00", "shipping_fee": "0.00",
                                "regional_fee": "0.00", "total": "190.00"}},
                    fh,
                )
            return _Result(returncode=0)

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=spawn
        )
        result, error = runner.run(
            handler="quotation_rules.evaluate", input_payload={"subtotal": "200.00"}
        )
        self.assertIsNone(error)
        self.assertEqual(result["total"], "190.00")

    def test_handler_error_maps_to_input_invalid(self):
        def spawn(argv, **kw):
            output_dir = _output_dir_from_argv(argv)
            with open(os.path.join(output_dir, "output.json"), "w", encoding="utf-8") as fh:
                json.dump({"error": "subtotal must be non-negative"}, fh)
            return _Result(returncode=0)

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=spawn
        )
        result, error = runner.run(
            handler="quotation_rules.evaluate", input_payload={"subtotal": "-1"}
        )
        self.assertIsNone(result)
        self.assertEqual(error, app_package.STATE_INPUT_INVALID)

    def test_staged_input_directory_is_traversable_by_non_root(self):
        # ``tempfile.mkdtemp`` creates a 0700 directory, which the container's
        # non-root user could not read; the runner must widen only the staged
        # input directory to 0755 so the reviewed handler can read its input.
        observed = {}

        def spawn(argv, **kw):
            output_dir = _output_dir_from_argv(argv)
            for i, arg in enumerate(argv):
                if arg == "--mount" and "dst=/in" in argv[i + 1]:
                    for part in argv[i + 1].split(","):
                        if part.startswith("src="):
                            observed["input_mode"] = os.stat(part[4:]).st_mode & 0o777
            with open(os.path.join(output_dir, "output.json"), "w", encoding="utf-8") as fh:
                json.dump({"result": {"total": "1.00"}}, fh)
            return _Result(returncode=0)

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=spawn
        )
        result, error = runner.run(
            handler="quotation_rules.evaluate", input_payload={"subtotal": "1.00"}
        )
        self.assertIsNone(error)
        self.assertEqual(observed["input_mode"], 0o755)


class ParameterStagingTests(unittest.TestCase):
    def test_parameters_are_staged(self):
        runner = container_runner.ContainerRunner(which=lambda name: "docker")
        input_dir = tempfile.mkdtemp()
        try:
            runner._stage_input(
                input_dir, "quotation_rules.evaluate", {"subtotal": "200.00"},
                {"member_discount_rate": "0.10"},
            )
            with open(os.path.join(input_dir, "input.json"), "r", encoding="utf-8") as fh:
                payload = json.loads(fh.read())
            self.assertEqual(payload["handler"], "quotation_rules.evaluate")
            self.assertEqual(payload["parameters"], {"member_discount_rate": "0.10"})
        finally:
            import shutil

            shutil.rmtree(input_dir, ignore_errors=True)

    def test_no_parameters_are_staged(self):
        runner = container_runner.ContainerRunner(which=lambda name: "docker")
        input_dir = tempfile.mkdtemp()
        try:
            runner._stage_input(input_dir, "quotation_rules.evaluate", {"subtotal": "200.00"})
            with open(os.path.join(input_dir, "input.json"), "r", encoding="utf-8") as fh:
                payload = json.loads(fh.read())
            self.assertNotIn("parameters", payload)
        finally:
            import shutil

            shutil.rmtree(input_dir, ignore_errors=True)


class OutputBoundsTests(unittest.TestCase):
    def setUp(self):
        self.runner = container_runner.ContainerRunner(which=lambda name: "docker")
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, content):
        path = os.path.join(self._tmp, "output.json")
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_reads_valid_output(self):
        self._write(b'{"result": {"total": "1.00"}}')
        self.assertEqual(self.runner._read_output(self._tmp)["result"]["total"], "1.00")

    def test_missing_output_returns_none(self):
        self.assertIsNone(self.runner._read_output(self._tmp))

    def test_oversized_output_returns_none(self):
        self._write(b"x" * (container_runner.RUNNER_MAX_OUTPUT_BYTES + 10))
        self.assertIsNone(self.runner._read_output(self._tmp))

    def test_malformed_output_returns_none(self):
        self._write(b"not json")
        self.assertIsNone(self.runner._read_output(self._tmp))


# A child that outlasts every bound in this module by a wide margin, so a
# timeout here is deterministic rather than a race against the bound itself.
_HANG = "import time; time.sleep(60)"


class _RealClientSpawn:
    """A Docker double whose ``run`` really hangs, so CPython raises for real.

    ``info``/``image``/``kill``/``rm`` are answered immediately; the ``run``
    call is delegated to the **real** :func:`subprocess.run` on a command that
    really sleeps, with the runner's own configured timeout. The exception the
    runner receives is therefore the genuine ``subprocess.TimeoutExpired``
    produced by CPython's own timeout machinery — not a literal ``TimeoutError``
    constructed to look like one, and not a hand-built ``TimeoutExpired``.
    """

    def __init__(self, container_states=None):
        self.calls = []
        self.raised = None
        # What the reconciliation's ``container inspect`` answers, consumed one
        # per query; the last value repeats. ``absent`` by default, because a
        # double that did not answer would leave the query unknown — which is
        # deliberately not the same as absent.
        self.container_states = list(container_states or ["absent"])
        self.queried = []
        self.removed = []
        self.kills = []

    def _next_state(self):
        if len(self.container_states) > 1:
            return self.container_states.pop(0)
        return self.container_states[0] if self.container_states else "absent"

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["container", "inspect"]:
            self.queried.append(argv[-1])
            return _Result(returncode=0 if self._next_state() == "present" else 1)
        if argv[1:2] == ["rm"]:
            self.removed.append(argv[-1])
            return _Result(returncode=0)
        if argv[1:2] == ["kill"]:
            self.kills.append(argv[-1])
            return _Result(returncode=0)
        if argv[1:2] in (["info"], ["image"]):
            return _Result(returncode=0)
        try:
            return subprocess.run(
                [sys.executable, "-c", _HANG],
                capture_output=True,
                timeout=kwargs.get("timeout"),
            )
        except subprocess.TimeoutExpired as exc:
            self.raised = exc
            raise

    def commands(self, prefix):
        return [argv for argv in self.calls if argv[1:2] == [prefix]]


class RealTimeoutLifecycleTests(unittest.TestCase):
    """A real ``subprocess.TimeoutExpired`` runs the timeout lifecycle once.

    ``subprocess.run(timeout=...)`` raises ``subprocess.TimeoutExpired``, which
    is a ``SubprocessError`` and **not** a ``TimeoutError``. Catching only
    ``TimeoutError`` let a real timeout escape the dispatch handler, and with it
    the kill, the removal, the staged-root cleanup and the bounded token.
    """

    def _runner(self, spawn, **overrides):
        settings = dict(which=lambda name: "docker", spawn=spawn, timeout=0.3)
        settings.update(overrides)
        return container_runner.ContainerRunner(**settings)

    def _run(self, spawn):
        return self._runner(spawn).run(
            handler="quotation_rules.evaluate", input_payload={"subtotal": "1.00"}
        )

    def test_the_premise_is_true(self):
        # The whole defect rests on this, so it is machine-checked rather than
        # asserted in a comment.
        self.assertFalse(issubclass(subprocess.TimeoutExpired, TimeoutError))
        self.assertTrue(issubclass(subprocess.TimeoutExpired, subprocess.SubprocessError))

    def test_a_real_timeout_runs_the_whole_lifecycle_exactly_once(self):
        spawn = _RealClientSpawn()
        result, error = self._run(spawn)

        # The exception really was CPython's, and really was a real timeout.
        self.assertIsNotNone(spawn.raised)
        self.assertIsInstance(spawn.raised, subprocess.TimeoutExpired)
        self.assertNotIsInstance(spawn.raised, TimeoutError)

        # One bounded token, and the only passing-looking thing about it is
        # that it is a timeout: it is not a result.
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_TIMEOUT, error)

        # Exactly one kill and exactly one removal.
        self.assertEqual(1, len(spawn.commands("kill")))
        self.assertEqual(1, len(spawn.commands("rm")))
        run_argv = spawn.commands("run")[0]
        self.assertEqual("hrca-run-" + run_argv[3].split("hrca-run-", 1)[1],
                         spawn.commands("kill")[0][2])
        self.assertEqual(spawn.commands("kill")[0][2], spawn.commands("rm")[0][3])

        # The staged roots the run was handed are gone.
        input_dir, output_dir = _staged_dirs_from_argv(run_argv)
        self.assertIsNotNone(input_dir)
        self.assertIsNotNone(output_dir)
        self.assertFalse(os.path.exists(input_dir))
        self.assertFalse(os.path.exists(output_dir))

    def test_the_staged_roots_are_removed_on_a_successful_run_too(self):
        spawn = _RealClientSpawn()
        spawn.calls = []

        def answering(argv, **kwargs):
            argv = list(argv)
            spawn.calls.append(argv)
            if argv[1:2] == ["run"]:
                output_dir = _output_dir_from_argv(argv)
                with open(os.path.join(output_dir, "output.json"), "w") as fh:
                    fh.write('{"result": {"total": "1.00"}}')
            return _Result(returncode=0)

        result, error = self._run(answering)
        self.assertEqual({"total": "1.00"}, result)
        self.assertIsNone(error)
        input_dir, output_dir = _staged_dirs_from_argv(spawn.commands("run")[0])
        self.assertFalse(os.path.exists(input_dir))
        self.assertFalse(os.path.exists(output_dir))

    def _run_with_failure(self, failing_step):
        """Run once with one client step failing, returning ``(error, spawn)``."""
        spawn = _RealClientSpawn()
        real = spawn.__call__

        def failing(argv, **kwargs):
            if argv[1:2] == [failing_step]:
                spawn.calls.append(list(argv))
                if failing_step == "kill":
                    raise OSError("kill failed")
                raise subprocess.TimeoutExpired(list(argv), 5.0)
            return real(argv, **kwargs)

        result, error = self._run(failing)
        self.assertIsNone(result)
        return error, spawn

    def test_a_kill_failure_does_not_by_itself_decide_the_token(self):
        # The kill raising is not the question any more. The question is whether
        # the exact container is gone — and here the reconciliation confirms it
        # is, so the honest answer is still a clean timeout. The old proxy (did
        # the kill answer?) could not tell "removed" from "never existed".
        error, spawn = self._run_with_failure("kill")
        self.assertEqual(app_package.STATE_TIMEOUT, error)
        # The removal still ran, and the reconciliation still asked.
        self.assertEqual(1, len(spawn.removed))
        self.assertTrue(spawn.queried)

    def test_a_removal_failure_does_not_by_itself_decide_the_token(self):
        error, spawn = self._run_with_failure("rm")
        self.assertEqual(app_package.STATE_TIMEOUT, error)
        self.assertEqual(1, len(spawn.commands("kill")))

    def test_a_late_created_container_is_removed_and_confirmed_absent(self):
        # The race this repair is about: the first query finds it — the daemon
        # finished creating it after the client died — so it is force-removed by
        # exactly that name and the next query confirms it is gone.
        spawn = _RealClientSpawn(container_states=["present", "absent"])
        result, error = self._run(spawn)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_TIMEOUT, error)
        # One query finds it, the next confirms it gone.
        self.assertEqual(2, len(spawn.queried))
        # Two removals name it: the initial kill/removal, and the one the
        # reconciliation issues when the query says it is still there.
        self.assertEqual(2, len(spawn.removed))
        self.assertEqual({spawn.queried[0]}, set(spawn.removed))

    def test_only_the_one_product_owned_name_is_ever_touched(self):
        spawn = _RealClientSpawn(container_states=["present", "present", "absent"])
        self._run(spawn)
        touched = set(spawn.queried) | set(spawn.removed) | set(spawn.kills)
        self.assertEqual(1, len(touched), sorted(touched))
        name = touched.pop()
        self.assertTrue(name.startswith("hrca-run-"), name)
        # Nothing was ever enumerated: every client call is either a daemon
        # probe, a dispatch, or an operation naming that one container.
        for argv in spawn.calls:
            with self.subTest(argv=argv[:3]):
                self.assertIn(
                    argv[1],
                    {"run", "info", "image", "kill", "rm", "container"},
                )
                self.assertNotIn("ps", argv)
                self.assertNotIn("ls", argv)

    def test_the_reconciliation_bounds_are_exact(self):
        # Present on every query: the loop must stop at the fixed attempt count
        # rather than spinning, and must ask exactly one more time than it
        # removes.
        spawn = _RealClientSpawn(container_states=["present"])
        self._run(spawn)
        # One query per attempt, plus the final confirmation.
        self.assertEqual(container_runner.RECONCILE_ATTEMPTS + 1, len(spawn.queried))
        # One removal per present round, plus the initial kill/removal.
        self.assertEqual(container_runner.RECONCILE_ATTEMPTS + 1, len(spawn.removed))
        self.assertEqual({spawn.queried[0]}, set(spawn.removed))

    def test_the_bounds_are_fixed_constants_not_settings(self):
        self.assertEqual(5, container_runner.RECONCILE_ATTEMPTS)
        self.assertEqual(0.25, container_runner.RECONCILE_INTERVAL_SECONDS)
        self.assertGreater(container_runner.RECONCILE_QUERY_TIMEOUT, 0)
        self.assertGreater(container_runner.RECONCILE_REMOVE_TIMEOUT, 0)

    def test_no_caller_can_tune_the_reconciliation_bounds(self):
        # The bounds are module constants. Nothing on the constructor, and
        # nothing a plan or candidate carries, can reach them.
        parameters = set(
            inspect.signature(container_runner.ContainerRunner.__init__).parameters
        )
        for forbidden in (
            "attempts", "reconcile_attempts", "retries", "retry",
            "interval", "backoff", "deadline", "reconcile", "timeout_seconds",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)
        self.assertIn("timeout", parameters)  # the dispatch bound, and only it

    def test_a_container_that_will_not_go_away_is_not_a_clean_timeout(self):
        # The honest negative: the exact name keeps resolving, so the container
        # is still there and the token must say so.
        spawn = _RealClientSpawn(container_states=["present"])
        result, error = self._run(spawn)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_RUNNER_FAILED, error)
        self.assertGreaterEqual(len(spawn.queried), container_runner.RECONCILE_ATTEMPTS)

    def test_an_unanswerable_daemon_is_not_absence(self):
        spawn = _RealClientSpawn()

        def unreachable(argv, **kwargs):
            if argv[1:2] == ["info"]:
                spawn.calls.append(list(argv))
                return _Result(returncode=1)
            return spawn(argv, **kwargs)

        result, error = self._run(unreachable)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_RUNNER_FAILED, error)
        # It never claimed to know the container was gone.
        self.assertEqual([], spawn.queried)

    def test_a_real_timeout_during_reconciliation_is_not_absence(self):
        spawn = _RealClientSpawn()

        def hanging_query(argv, **kwargs):
            if argv[1:3] == ["container", "inspect"]:
                spawn.calls.append(list(argv))
                raise subprocess.TimeoutExpired(list(argv), 2.0)
            return spawn(argv, **kwargs)

        result, error = self._run(hanging_query)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_RUNNER_FAILED, error)

    def test_a_cleanup_failure_is_not_reported_as_a_clean_timeout(self):
        spawn = _RealClientSpawn()
        with mock.patch.object(
            container_runner.shutil, "rmtree", side_effect=OSError("cleanup failed")
        ):
            result, error = self._run(spawn)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_RUNNER_FAILED, error)
        # The kill and the removal were still attempted exactly once.
        self.assertEqual(1, len(spawn.commands("kill")))
        self.assertEqual(1, len(spawn.commands("rm")))
        # Clean up what the patched rmtree could not, so the suite leaves
        # nothing behind.
        input_dir, output_dir = _staged_dirs_from_argv(spawn.commands("run")[0])
        for path in (input_dir, output_dir):
            shutil.rmtree(path, ignore_errors=True)

    def test_cleanup_removes_only_the_roots_it_was_given(self):
        spawn = _RealClientSpawn()
        result, error = self._run(spawn)
        self.assertEqual(app_package.STATE_TIMEOUT, error)
        input_dir, output_dir = _staged_dirs_from_argv(spawn.commands("run")[0])
        # A sibling root in the same parent directory is none of its business.
        sibling = tempfile.mkdtemp(prefix="hrca-unrelated-")
        try:
            with open(os.path.join(sibling, "keep.txt"), "w") as fh:
                fh.write("keep")
            self.assertFalse(os.path.exists(input_dir))
            self.assertFalse(os.path.exists(output_dir))
            self.assertTrue(os.path.isdir(sibling))
            with open(os.path.join(sibling, "keep.txt")) as fh:
                self.assertEqual("keep", fh.read())
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

    def test_an_injected_literal_timeout_still_returns_the_timeout_token(self):
        # The coverage that existed before the repair is retained unchanged in
        # spirit: the shape the runner always handled keeps working.
        spawn = _RealClientSpawn()
        real = spawn.__call__

        def literal(argv, **kwargs):
            if argv[1:2] == ["run"]:
                spawn.calls.append(list(argv))
                raise TimeoutError()
            return real(argv, **kwargs)

        result, error = self._run(literal)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_TIMEOUT, error)
        self.assertEqual(1, len(spawn.commands("kill")))
        self.assertEqual(1, len(spawn.commands("rm")))

    def test_an_ordinary_failure_is_unchanged(self):
        spawn = _RealClientSpawn()

        def failing(argv, **kwargs):
            argv = list(argv)
            spawn.calls.append(argv)
            if argv[1:2] == ["run"]:
                return _Result(returncode=1)
            return _Result(returncode=0)

        result, error = self._run(failing)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_RUNNER_FAILED, error)
        # No timeout lifecycle ran, but the staged roots are still cleaned.
        self.assertEqual([], spawn.commands("kill"))
        self.assertEqual([], spawn.commands("rm"))
        input_dir, output_dir = _staged_dirs_from_argv(spawn.commands("run")[0])
        self.assertFalse(os.path.exists(input_dir))
        self.assertFalse(os.path.exists(output_dir))

    def test_a_non_timeout_exception_is_unchanged(self):
        spawn = _RealClientSpawn()

        def exploding(argv, **kwargs):
            if argv[1:2] == ["run"]:
                spawn.calls.append(list(argv))
                raise OSError("client exploded")
            return _Result(returncode=0)

        result, error = self._run(exploding)
        self.assertIsNone(result)
        self.assertEqual(app_package.STATE_RUNNER_FAILED, error)
        self.assertEqual([], spawn.commands("kill"))

    def test_a_real_timeout_during_preflight_is_unavailable_not_an_exception(self):
        # The same defect lived in preflight: a hanging client must read as an
        # unreachable daemon, not as an escaping exception. Preflight bounds its
        # own calls at 5 seconds rather than using the run timeout, so this case
        # takes that long — the wait *is* the production bound, and shortening
        # it here would test a bound the runner does not use.
        def hanging(argv, **kwargs):
            return subprocess.run(
                [sys.executable, "-c", _HANG], capture_output=True,
                timeout=kwargs.get("timeout"),
            )

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=hanging, timeout=0.2
        )
        preflight = runner.preflight()
        self.assertIs(False, preflight["available"])
        self.assertEqual(
            container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE, preflight["reason"]
        )
        self.assertIs(False, preflight["checks"]["daemon"])

    def test_a_real_timeout_during_the_image_inspect_is_blocked(self):
        def spawn(argv, **kwargs):
            argv = list(argv)
            if argv[1:2] == ["info"]:
                return _Result(returncode=0)
            return subprocess.run(
                [sys.executable, "-c", _HANG], capture_output=True,
                timeout=kwargs.get("timeout"),
            )

        runner = container_runner.ContainerRunner(
            which=lambda name: "docker", spawn=spawn, timeout=0.2
        )
        preflight = runner.preflight()
        self.assertIs(False, preflight["available"])
        self.assertEqual(
            container_runner.PREFLIGHT_RUNTIME_BLOCKED, preflight["reason"]
        )
        self.assertIs(True, preflight["checks"]["daemon"])
        self.assertIs(False, preflight["checks"]["image"])


if __name__ == "__main__":
    unittest.main()
