"""Direct proof that the setup-verification guard refuses dispatch (P5.5r3c1).

This module is deliberately **not** on the setup-verification allowlist, and the
reason is the point of the whole design: a test that proves a spawn is refused
has to *attempt a spawn*, and setup verification's claim is that it performs
none. If these tests were on the allowlist, a successful safe run would report
refused attempts and could no longer say "nothing attempted" and mean it.

So the surface is partitioned:

* ``tests/test_setup_verification.py`` — the selector, on the allowlist, which
  must never trigger a refusal;
* this module — the guard's own tripwires, reachable only through their explicit
  route, ``python -m unittest tests.test_setup_verification_guard``.

What the attempts would start if the guard were broken is chosen so that a broken
guard is a *passing surprise*, never a real dispatch:

* the generic spawn test starts the host interpreter running ``pass``;
* the container-runner test gives the runner a fake ``docker`` that is **the host
  interpreter itself**, so a broken guard runs ``python info`` — a harmless
  non-zero exit — and never the Docker client, never a container, never a mount.

The claim being proven is about the *primitive*, not a spelling of it:
:class:`hrca.container_runner.ContainerRunner` binds ``spawn=subprocess.run`` as a
default argument at definition time, so replacing the ``subprocess.run``
attribute afterwards is invisible to it. The audit hook fires at the C level
instead, which is why the runner's own dispatch is covered.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import unittest

from hrca import container_runner, setup_verification as verification

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))

LIVE_MODULE = "test_candidate_syntax_integration"


class GuardActivationTests(unittest.TestCase):
    def setUp(self):
        verification.reset_attempts()

    def test_a_spawn_under_the_guard_is_refused(self):
        # If the guard were broken this would run the host interpreter on
        # "pass": harmless, no container, no Docker.
        with verification.dispatch_guard() as guard:
            with self.assertRaises(verification.SetupRefused) as caught:
                subprocess.run([sys.executable, "-c", "pass"], check=False)
        self.assertEqual(verification.REASON_SPAWN, caught.exception.reason)
        self.assertEqual(1, len(guard.attempts))
        self.assertTrue(guard.attempts[0].startswith("subprocess.Popen"))

    def test_the_guard_refuses_the_container_runners_own_dispatch_primitive(self):
        # The fake "docker" is the host interpreter, so even a broken guard runs
        # ``python info`` rather than the Docker client.
        runner = container_runner.ContainerRunner(which=lambda name: sys.executable)
        with verification.dispatch_guard() as guard:
            with self.assertRaises(verification.SetupRefused):
                runner.preflight()
        self.assertTrue(guard.attempts)
        self.assertTrue(guard.attempts[0].startswith("subprocess.Popen"))

    def test_every_dispatch_path_uses_the_blocked_primitive(self):
        # The runtime proof above blocks ``_spawn``. This closes the argument
        # that blocking ``_spawn`` blocks *dispatch*: both dispatch paths call
        # it, so no path reaches a container by another route. Asserted
        # statically rather than by calling them, because staging a run would
        # create staged roots this task must not create.
        import ast

        path = os.path.join(REPO, "src", "hrca", "container_runner.py")
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        calls = {
            "run": set(),
            "run_candidate": set(),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in calls:
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                        if inner.func.attr == "_spawn":
                            calls[node.name].add(inner.func.attr)
        for name, found in calls.items():
            with self.subTest(method=name):
                self.assertIn("_spawn", found)

    def test_an_excluded_module_cannot_be_imported_while_the_guard_is_active(self):
        name = "tests." + LIVE_MODULE
        before = name in sys.modules
        with verification.dispatch_guard() as guard:
            with self.assertRaises(verification.SetupRefused) as caught:
                importlib.import_module(name)
        self.assertEqual(verification.REASON_IMPORT, caught.exception.reason)
        # The point: the attempt did not result in the module being loaded.
        self.assertEqual(before, name in sys.modules)
        self.assertTrue(any(entry.startswith("import:") for entry in guard.attempts))

    def test_the_import_guard_also_covers_the_bare_name(self):
        # The integration module inserts its own directory on sys.path, so the
        # bare name is importable too. Both spellings are watched.
        before = LIVE_MODULE in sys.modules
        with verification.dispatch_guard():
            with self.assertRaises(verification.SetupRefused):
                importlib.import_module(LIVE_MODULE)
        self.assertEqual(before, LIVE_MODULE in sys.modules)

    def test_an_allowed_module_is_still_importable_under_the_guard(self):
        with verification.dispatch_guard():
            module = importlib.import_module("tests.test_architecture")
        self.assertTrue(hasattr(module, "ClientArchitectureTests"))


class GuardInertnessTests(unittest.TestCase):
    def test_the_hook_does_nothing_when_the_guard_is_not_armed(self):
        # The audit hook cannot be uninstalled, so inertness is what keeps it
        # from affecting anything else in the process — including tests in this
        # same process that legitimately spawn. Asserted without spawning: the
        # hook body is called directly while the depth is zero.
        verification.reset_attempts()
        saved = verification._STATE.depth
        verification._STATE.depth = 0
        try:
            verification._audit("subprocess.Popen", (sys.executable,))
        finally:
            verification._STATE.depth = saved
        self.assertEqual([], verification._STATE.attempts)

    def test_nesting_arms_and_disarms_correctly(self):
        self.assertFalse(verification._STATE.active)
        with verification.dispatch_guard():
            self.assertTrue(verification._STATE.active)
            with verification.dispatch_guard():
                self.assertTrue(verification._STATE.active)
            self.assertTrue(verification._STATE.active)
        self.assertFalse(verification._STATE.active)

    def test_an_ordinary_spawn_outside_the_guard_still_works(self):
        # Proof that the guard is a partition and not a blanket ban: outside a
        # verification run, spawning behaves exactly as it always did.
        completed = subprocess.run(
            [sys.executable, "-c", "raise SystemExit(7)"], capture_output=True
        )
        self.assertEqual(7, completed.returncode)


class GuardCoverageTests(unittest.TestCase):
    def test_every_way_a_process_can_start_is_watched(self):
        for event in (
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.execv",
            "os.posix_spawn",
            "os.spawnl",
            "os.fork",
        ):
            with self.subTest(event=event):
                self.assertTrue(verification._is_spawn_event(event))
        for event in ("import", "open", "os.mkdir", "socket.connect", "os.remove"):
            with self.subTest(event=event):
                self.assertFalse(verification._is_spawn_event(event))

    def test_the_guard_records_what_it_refused_without_caller_text(self):
        with verification.dispatch_guard() as guard:
            with self.assertRaises(verification.SetupRefused):
                subprocess.run([sys.executable, "-c", "pass"], check=False)
        entry = guard.attempts[0]
        self.assertLessEqual(len(entry), 64 + len("subprocess.Popen:"))
        self.assertNotIn("-c", entry)


if __name__ == "__main__":
    unittest.main()
