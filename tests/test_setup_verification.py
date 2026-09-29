"""The setup-verification selector (P5.5r3c1).

This module is on the setup-verification allowlist, which constrains every test
in it: it runs **under the armed guard**, so nothing here may start a process or
trigger a refusal. The tests that deliberately *do* trigger one — the proof that a
spawn is refused, and the proof that an excluded module cannot be imported — live
in ``tests/test_setup_verification_guard.py``, precisely because they must
attempt what this surface must never do. Keeping the two apart is what lets the
safe run report "nothing attempted" and mean it.

The load-bearing claims here:

* the selectable surface is a code-owned allowlist, with no discovery;
* an excluded module is refused **by name**, before anything imports it;
* a selection that would verify nothing is refused rather than reported green;
* this module is the only copy of itself in the process.
"""

from __future__ import annotations

import ast
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hrca.cli import setup_verification as verification
from hrca.cli import setup_verification_cli as cli

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))

# The two modules that dispatch real containers when a daemon is reachable.
LIVE_MODULES = ("test_candidate_syntax_integration", "test_rule_delta_docker_integration")


def _imported(name):
    return name in sys.modules


_HRCA_ROOT = os.path.join(REPO, "src", "hrca")

def _hrca_module(name):
    """Return the path of an ``hrca`` module wherever it now lives.

    The package is organised by responsibility, so a module is no longer a
    fixed number of directories below ``src``; it is resolved by name. A
    compatibility shim is skipped in favour of the implementation it aliases,
    because these tests are about what a module *does* and a shim does
    nothing but point at another module.
    """
    stem = name[:-3] if name.endswith(".py") else name
    matches = []
    for dirpath, dirnames, filenames in os.walk(_HRCA_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if stem + ".py" in filenames:
            matches.append(os.path.join(dirpath, stem + ".py"))
        # A responsibility package's front door is its ``__init__``, so a name
        # that used to be a module may now be a package.
        if os.path.basename(dirpath) == stem and "__init__.py" in filenames:
            matches.append(os.path.join(dirpath, "__init__.py"))
    for path in sorted(matches, key=len, reverse=True):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        alias = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not node.targets:
                continue
            t = node.targets[0]
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Attribute)
                    and t.value.attr == "modules"
                    and isinstance(t.value.value, ast.Name)
                    and t.value.value.id == "sys"
                    and isinstance(t.slice, ast.Name)
                    and t.slice.id == "__name__"):
                alias = True
        if not alias:
            return path
    if matches:
        return matches[0]
    raise AssertionError("no module named %r in the hrca package" % stem)


class AllowlistTests(unittest.TestCase):
    def test_the_real_repository_policy_is_trustworthy(self):
        self.assertIsNone(verification.validate_policy())

    def test_the_allowlist_and_the_exclusions_are_disjoint(self):
        self.assertEqual(
            set(), set(verification.ALLOWED_MODULES) & set(verification.EXCLUDED_MODULES)
        )

    def test_every_allowed_module_has_a_test_file(self):
        for name in verification.ALLOWED_MODULES:
            with self.subTest(module=name):
                self.assertTrue(
                    os.path.isfile(os.path.join(REPO, "tests", name + ".py")), name
                )

    def test_every_excluded_module_is_excluded_for_a_stated_reason(self):
        for name in LIVE_MODULES:
            with self.subTest(module=name):
                self.assertIn(name, verification.EXCLUDED_MODULES)
                self.assertTrue(verification.EXCLUDED_MODULES[name])

    def test_the_live_container_modules_are_not_selectable(self):
        modules, reason = verification.resolve_selection(None)
        self.assertIsNone(reason)
        for name in LIVE_MODULES:
            with self.subTest(module=name):
                self.assertNotIn(name, verification.ALLOWED_MODULES)
                self.assertNotIn(name, modules)

    def test_the_guard_proof_module_is_not_on_the_allowlist(self):
        # It must attempt what this surface must never do.
        self.assertNotIn("test_setup_verification_guard", verification.ALLOWED_MODULES)
        self.assertTrue(
            os.path.isfile(
                os.path.join(REPO, "tests", "test_setup_verification_guard.py")
            )
        )

    def test_an_allowlist_that_admits_an_excluded_module_is_refused(self):
        # The tripwire: a future edit that adds an integration module to the
        # allowlist must make the whole policy untrustworthy, not quietly widen
        # the surface.
        with mock.patch.object(
            verification, "ALLOWED_MODULES", verification.ALLOWED_MODULES + LIVE_MODULES
        ):
            self.assertEqual(
                verification.REASON_POLICY_OVERLAP, verification.validate_policy()
            )

    def test_an_allowlist_naming_a_missing_module_is_refused(self):
        with mock.patch.object(
            verification, "ALLOWED_MODULES", ("test_no_such_module_exists",)
        ):
            self.assertEqual(
                verification.REASON_POLICY_MISSING, verification.validate_policy()
            )

    def test_a_duplicated_allowlist_entry_is_refused(self):
        with mock.patch.object(
            verification, "ALLOWED_MODULES", ("test_architecture", "test_architecture")
        ):
            self.assertEqual(
                verification.REASON_POLICY_OVERLAP, verification.validate_policy()
            )

    def test_an_empty_allowlist_is_refused(self):
        with mock.patch.object(verification, "ALLOWED_MODULES", ()):
            self.assertEqual(
                verification.REASON_EMPTY_SELECTION, verification.validate_policy()
            )


class SelectionTests(unittest.TestCase):
    def test_the_default_selection_is_the_whole_allowlist(self):
        modules, reason = verification.resolve_selection(None)
        self.assertIsNone(reason)
        self.assertEqual(list(verification.ALLOWED_MODULES), modules)

    def test_a_subset_is_accepted_and_canonically_ordered(self):
        modules, reason = verification.resolve_selection(
            ["test_runner_image_setup", "test_architecture"]
        )
        self.assertIsNone(reason)
        self.assertEqual(["test_architecture", "test_runner_image_setup"], modules)

    def test_a_duplicate_request_is_answered_once(self):
        modules, reason = verification.resolve_selection(
            ["test_architecture", "test_architecture"]
        )
        self.assertIsNone(reason)
        self.assertEqual(["test_architecture"], modules)

    def test_an_excluded_module_is_refused_by_its_own_reason_and_not_imported(self):
        for name in LIVE_MODULES:
            with self.subTest(module=name):
                before = _imported("tests." + name)
                modules, reason = verification.resolve_selection([name])
                self.assertIsNone(modules)
                self.assertEqual(verification.REASON_EXCLUDED_MODULE, reason)
                self.assertEqual(before, _imported("tests." + name))

    def test_an_unknown_module_is_refused_and_not_imported(self):
        modules, reason = verification.resolve_selection(["test_not_a_module"])
        self.assertIsNone(modules)
        self.assertEqual(verification.REASON_UNKNOWN_MODULE, reason)
        self.assertFalse(_imported("tests.test_not_a_module"))

    def test_a_malformed_selection_is_refused(self):
        for requested, reason in (
            ("test_architecture", verification.REASON_SELECTION_NOT_LIST),
            ([], verification.REASON_EMPTY_SELECTION),
            ([None], verification.REASON_SELECTION_NOT_LIST),
            ([7], verification.REASON_SELECTION_NOT_LIST),
        ):
            with self.subTest(requested=requested):
                modules, got = verification.resolve_selection(requested)
                self.assertIsNone(modules)
                self.assertEqual(reason, got)


class VacuityTests(unittest.TestCase):
    def test_a_selection_that_would_verify_nothing_is_refused(self):
        self.assertEqual(verification.REASON_VACUOUS, verification.vacuity_reason({}))
        self.assertEqual(
            verification.REASON_VACUOUS, verification.vacuity_reason({"a": 0})
        )
        self.assertEqual(
            verification.REASON_VACUOUS_MODULE,
            verification.vacuity_reason({"a": 3, "b": 0}),
        )
        self.assertIsNone(verification.vacuity_reason({"a": 3}))


class LoadingTests(unittest.TestCase):
    def test_the_selection_loads_only_allowlisted_modules(self):
        # Loading is exercised directly rather than through a nested run: the
        # cases are built and counted, and nothing is executed.
        modules, reason = verification.resolve_selection(None)
        self.assertIsNone(reason)
        suite, counts, load_reason = verification.build_selection(modules)
        self.assertIsNone(load_reason)
        self.assertIsNotNone(suite)
        self.assertEqual(sorted(verification.ALLOWED_MODULES), sorted(counts))
        for name, count in counts.items():
            with self.subTest(module=name):
                self.assertGreater(count, 0)
        self.assertIsNone(verification.vacuity_reason(counts))


class EntrypointTests(unittest.TestCase):
    """Refusals that happen *before* the guard is armed, so nothing is attempted."""

    def _run(self, requested):
        stream = io.StringIO()
        code = verification.run(requested, stream=stream)
        return code, stream.getvalue()

    def test_requesting_an_excluded_module_refuses_and_runs_nothing(self):
        for name in LIVE_MODULES:
            with self.subTest(module=name):
                before = _imported("tests." + name)
                code, message = self._run([name])
                self.assertEqual(verification.EXIT_REFUSED, code)
                self.assertIn(verification.REASON_EXCLUDED_MODULE, message)
                self.assertEqual(before, _imported("tests." + name))

    def test_an_empty_selection_refuses(self):
        code, message = self._run([])
        self.assertEqual(verification.EXIT_REFUSED, code)
        self.assertIn(verification.REASON_EMPTY_SELECTION, message)

    def test_a_nested_run_refuses_rather_than_recursing(self):
        verification._STATE.running = True
        try:
            code, message = self._run(None)
        finally:
            verification._STATE.running = False
        self.assertEqual(verification.EXIT_REFUSED, code)
        self.assertIn(verification.REASON_ALREADY_RUNNING, message)

    def test_a_usage_error_is_a_refusal_not_a_pass(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = cli.main(["not-a-module"])
        self.assertEqual(cli.EXIT_REFUSED, code)

    def test_the_cli_exit_codes_are_the_modules_exit_codes(self):
        self.assertEqual(verification.EXIT_VERIFIED, cli.EXIT_VERIFIED)
        self.assertEqual(verification.EXIT_FAILED, cli.EXIT_FAILED)
        self.assertEqual(verification.EXIT_REFUSED, cli.EXIT_REFUSED)

    def test_running_the_state_module_as_a_script_refuses(self):
        # ``python -m hrca.setup_verification`` would duplicate the module and
        # its guard state. It must refuse loudly rather than exit zero having
        # verified nothing.
        path = _hrca_module("setup_verification")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('if __name__ == "__main__":', source)
        self.assertIn("EXIT_REFUSED", source.split('if __name__ == "__main__":')[1])


class SingleCopyTests(unittest.TestCase):
    """The guard is only trustworthy if exactly one copy of it is live."""

    def test_the_module_under_test_is_the_canonical_one(self):
        # The canonical name follows the module to its responsibility package.
        # What is asserted is unchanged: the object under test is the single
        # registered copy, not a second one loaded under another name.
        self.assertEqual("hrca.cli.setup_verification", verification.__name__)
        self.assertIs(verification, sys.modules.get("hrca.cli.setup_verification"))

    def test_no_duplicate_of_the_state_module_is_running_as_main(self):
        # The precise hazard this guards: ``python -m hrca.setup_verification``
        # makes __main__ a second copy, with its own state and its own exception
        # class, so a refusal raised by the armed copy is not the class the
        # canonical copy documents.
        main_module = sys.modules.get("__main__")
        self.assertIsNot(verification, main_module)
        main_file = getattr(main_module, "__file__", None) or ""
        self.assertNotEqual("setup_verification.py", os.path.basename(main_file))


class StaticSurfaceTests(unittest.TestCase):
    """The allowlisted modules cannot reach a container on their own."""

    def _imports(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level:
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
        return imported

    def test_no_allowlisted_module_names_a_live_integration_module(self):
        for name in verification.ALLOWED_MODULES:
            with self.subTest(module=name):
                imported = self._imports(os.path.join(REPO, "tests", name + ".py"))
                self.assertTrue(
                    imported.isdisjoint(set(LIVE_MODULES)),
                    "%s imports a live integration module" % name,
                )

    def test_the_selector_imports_no_runner_and_no_network(self):
        imported = self._imports(
            _hrca_module("setup_verification")
        )
        self.assertTrue(
            imported.isdisjoint(
                {"container_runner", "validation", "candidate"}
                | {"socket", "http", "urllib", "ssl"}
            ),
            sorted(imported),
        )

    def test_no_allowlisted_module_builds_a_runner_or_dispatches(self):
        # The static half of the no-dispatch claim. It is deliberately narrow:
        # naming ``container_runner`` is legitimate here (the policy module
        # asserts its pin agrees with the runner's), but *constructing* a runner
        # or calling a dispatch method is not, and neither appears.
        # The needles are assembled so this assertion does not match its own
        # source — which it otherwise does, and did.
        constructs = "Container" + "Runner("
        dispatches = ".run_" + "candidate("
        for name in verification.ALLOWED_MODULES:
            path = os.path.join(REPO, "tests", name + ".py")
            with self.subTest(module=name):
                with open(path, "r", encoding="utf-8") as handle:
                    source = handle.read()
                self.assertNotIn(constructs, source)
                self.assertNotIn(dispatches, source)


if __name__ == "__main__":
    unittest.main()
