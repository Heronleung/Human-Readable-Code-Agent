"""Tests for the P4.3 isolated container-runner adapter.

No real container is used: the Docker client and daemon are faked. The tests
prove the hardened command surface (network disabled, non-root, least
privilege, resource bounds, staged mounts only, no host secrets/socket), the
fail-closed preflight, timeout/cleanup behaviour, and bounded output
collection.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
