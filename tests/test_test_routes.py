"""Tests for the explicit, fail-closed test routes (B0).

These tests never *run* a route that loads modules — a safe route would recurse
into the suite that is running them, and a dispatching route is the thing this
whole mechanism exists to keep out of ordinary work. What is exercised here is
the decision surface: the table's integrity, the refusals, the acknowledgement
gate, and the structural promise that the route mechanism itself is not a way
into the product.
"""

from __future__ import annotations

import ast
import io
import os
import unittest

from hrca import test_routes

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

_ROUTE_MODULE = os.path.join(_SRC, "test_routes.py")
_CLI_MODULE = os.path.join(_SRC, "test_routes_cli.py")

# Modules the route mechanism must never import: it is dev tooling that
# partitions the surface, so it cannot be part of it.
_FORBIDDEN_IMPORTS = frozenset(
    {
        "container_runner",
        "runner_broker",
        "runner_image_policy",
        "runner_image_setup",
        "runtime_handlers",
        "provider",
        "provider_cli",
        "provider_config",
        "deepseek",
        "deepseek_transport",
        "delta_transport",
        "credential_store",
        "credential_store_win",
        "credential_host",
        "credential_sheet_win",
        "boundary",
        "client",
        "client_core",
        "twin",
        "twin_store",
        "codemap",
        "codemap_draft",
        "proposal",
        "advisory",
        "subprocess",
        "socket",
        "urllib",
        "ssl",
        "http",
        "requests",
    }
)


def _imported_top_level_names(path: str) -> set:
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name.split(".")[0])
    return names


class RouteTableIntegrityTests(unittest.TestCase):
    def test_the_policy_is_self_consistent(self):
        self.assertIsNone(test_routes.validate_policy())

    def test_the_full_route_is_the_whole_test_surface(self):
        self.assertIsNone(test_routes.full_coverage_reason())

    def test_every_route_has_a_non_empty_allowlist(self):
        for route in test_routes.route_names():
            self.assertTrue(test_routes.route_allowlist(route), route)

    def test_no_route_lists_a_module_twice(self):
        for route in test_routes.route_names():
            modules = test_routes.route_allowlist(route)
            self.assertEqual(len(set(modules)), len(modules), route)

    def test_the_setup_route_mirrors_the_setup_verification_allowlist(self):
        from hrca import setup_verification

        self.assertEqual(
            tuple(test_routes.route_allowlist(test_routes.ROUTE_SETUP)),
            tuple(setup_verification.ALLOWED_MODULES),
        )

    def test_every_allowlisted_module_has_a_test_file(self):
        root = test_routes.tests_root()
        self.assertIsNotNone(root)
        for route in test_routes.route_names():
            for name in test_routes.route_allowlist(route):
                self.assertTrue(
                    os.path.isfile(os.path.join(root, name + ".py")), name
                )


class ContainerIsolationTests(unittest.TestCase):
    """The partition the whole package exists to establish."""

    def test_safe_routes_never_reach_a_container_module(self):
        container = set(test_routes.CONTAINER_MODULES)
        for route in test_routes.SAFE_ROUTES:
            self.assertFalse(
                set(test_routes.route_allowlist(route)) & container, route
            )

    def test_container_modules_appear_only_on_the_dispatching_routes(self):
        for module in test_routes.CONTAINER_MODULES:
            allowed = {
                route
                for route in test_routes.route_names()
                if module in test_routes.route_allowlist(route)
            }
            self.assertEqual(
                allowed, set(test_routes.ACKNOWLEDGED_ROUTES), module
            )

    def test_every_container_module_states_its_reason(self):
        for module in test_routes.CONTAINER_MODULES:
            self.assertIn(module, test_routes.CONTAINER_REASONS)
            self.assertTrue(test_routes.CONTAINER_REASONS[module])

    def test_every_unrouted_module_states_its_reason(self):
        # "Unrouted" means outside every *safe* route. These modules are still
        # on ``full`` — the whole-suite route — so excluding them from the safe
        # routes costs no coverage, it only declines to claim they are safe.
        self.assertTrue(test_routes.UNROUTED_MODULES)
        for module, reason in test_routes.UNROUTED_MODULES.items():
            self.assertTrue(reason, module)
            for route in test_routes.SAFE_ROUTES:
                self.assertNotIn(
                    module, test_routes.route_allowlist(route), (module, route)
                )
            self.assertIn(
                module, test_routes.route_allowlist(test_routes.ROUTE_FULL), module
            )

    def test_candidate_identity_module_is_not_on_a_safe_route(self):
        # The one exclusion that costs coverage, pinned so a later edit has to
        # confront it rather than rediscover it.
        self.assertIn("test_candidate", test_routes.UNROUTED_MODULES)
        self.assertNotIn(
            "test_candidate", test_routes.route_allowlist(test_routes.ROUTE_CORE)
        )


class SelectionRefusalTests(unittest.TestCase):
    def test_an_unknown_route_is_refused(self):
        modules, reason = test_routes.resolve_selection("no-such-route")
        self.assertIsNone(modules)
        self.assertEqual(test_routes.REASON_UNKNOWN_ROUTE, reason)

    def test_the_bare_route_returns_its_whole_allowlist(self):
        modules, reason = test_routes.resolve_selection(test_routes.ROUTE_CORE)
        self.assertIsNone(reason)
        self.assertEqual(
            sorted(modules),
            sorted(test_routes.route_allowlist(test_routes.ROUTE_CORE)),
        )

    def test_a_module_from_another_route_is_refused_by_name(self):
        modules, reason = test_routes.resolve_selection(
            test_routes.ROUTE_CORE, ["test_twin"]
        )
        self.assertIsNone(modules)
        self.assertEqual(test_routes.REASON_MODULE_NOT_LISTED, reason)

    def test_a_container_module_is_refused_on_a_safe_route(self):
        for name in test_routes.CONTAINER_MODULES:
            modules, reason = test_routes.resolve_selection(
                test_routes.ROUTE_CORE, [name]
            )
            self.assertIsNone(modules, name)
            self.assertEqual(test_routes.REASON_MODULE_CONTAINER, reason)

    def test_an_unrouted_module_is_refused_with_its_own_reason(self):
        modules, reason = test_routes.resolve_selection(
            test_routes.ROUTE_CORE, ["test_scanner_grammar"]
        )
        self.assertIsNone(modules)
        self.assertEqual(test_routes.REASON_MODULE_UNROUTED, reason)

    def test_an_empty_or_non_list_selection_is_refused(self):
        from hrca import setup_verification

        modules, reason = test_routes.resolve_selection(test_routes.ROUTE_CORE, [])
        self.assertIsNone(modules)
        self.assertEqual(setup_verification.REASON_EMPTY_SELECTION, reason)

        modules, reason = test_routes.resolve_selection(
            test_routes.ROUTE_CORE, "test_memory"
        )
        self.assertIsNone(modules)
        self.assertEqual(setup_verification.REASON_SELECTION_NOT_LIST, reason)

    def test_a_narrowed_selection_is_sorted_and_deduplicated(self):
        modules, reason = test_routes.resolve_selection(
            test_routes.ROUTE_CORE, ["test_memory", "test_scanner", "test_memory"]
        )
        self.assertIsNone(reason)
        self.assertEqual(["test_memory", "test_scanner"], modules)


class AcknowledgementGateTests(unittest.TestCase):
    def test_the_dispatching_routes_require_acknowledgement(self):
        self.assertTrue(
            test_routes.requires_acknowledgement(test_routes.ROUTE_CONTAINER)
        )
        self.assertTrue(
            test_routes.requires_acknowledgement(test_routes.ROUTE_FULL)
        )

    def test_the_safe_routes_do_not_require_acknowledgement(self):
        for route in test_routes.SAFE_ROUTES:
            self.assertFalse(test_routes.requires_acknowledgement(route), route)

    def test_the_container_route_refuses_before_it_loads_anything(self):
        stream = io.StringIO()
        code = test_routes.run(test_routes.ROUTE_CONTAINER, stream=stream)
        self.assertEqual(test_routes.EXIT_REFUSED, code)
        self.assertIn(
            test_routes.REASON_ACKNOWLEDGEMENT_REQUIRED, stream.getvalue()
        )

    def test_the_full_route_refuses_before_it_loads_anything(self):
        stream = io.StringIO()
        code = test_routes.run(test_routes.ROUTE_FULL, stream=stream)
        self.assertEqual(test_routes.EXIT_REFUSED, code)
        self.assertIn(
            test_routes.REASON_ACKNOWLEDGEMENT_REQUIRED, stream.getvalue()
        )

    def test_an_unknown_route_is_refused_without_the_flag(self):
        stream = io.StringIO()
        code = test_routes.run("no-such-route", stream=stream)
        self.assertEqual(test_routes.EXIT_REFUSED, code)
        self.assertIn(test_routes.REASON_UNKNOWN_ROUTE, stream.getvalue())


class VacuityTests(unittest.TestCase):
    def test_no_modules_at_all_is_vacuous(self):
        self.assertEqual(
            test_routes.REASON_VACUOUS, test_routes.vacuity_reason({})
        )

    def test_zero_tests_is_vacuous(self):
        self.assertEqual(
            test_routes.REASON_VACUOUS,
            test_routes.vacuity_reason({"test_memory": 0}),
        )

    def test_a_module_contributing_nothing_is_vacuous(self):
        self.assertEqual(
            test_routes.REASON_VACUOUS_MODULE,
            test_routes.vacuity_reason({"test_memory": 3, "test_scanner": 0}),
        )

    def test_a_real_contribution_is_not_vacuous(self):
        self.assertIsNone(test_routes.vacuity_reason({"test_memory": 3}))


class RouteMechanismStaysOutsideTheProductTests(unittest.TestCase):
    """The route mechanism must not itself be a route into the surface."""

    def test_route_modules_exist(self):
        self.assertTrue(os.path.isfile(_ROUTE_MODULE))
        self.assertTrue(os.path.isfile(_CLI_MODULE))

    def test_the_route_module_imports_no_product_module(self):
        for path in (_ROUTE_MODULE, _CLI_MODULE):
            imported = _imported_top_level_names(path)
            offending = sorted(imported & _FORBIDDEN_IMPORTS)
            self.assertEqual([], offending, os.path.basename(path))

    def test_the_route_module_imports_only_stdlib_and_the_setup_guard(self):
        imported = _imported_top_level_names(_ROUTE_MODULE)
        hrca_imports = {name for name in imported if name in _ALL_HRCA_MODULES}
        self.assertEqual({"setup_verification"}, hrca_imports)

    def test_the_route_module_contains_no_dynamic_import(self):
        # The hard rule: modules are selected from a literal allowlist and are
        # loaded by name from that allowlist. Nothing is discovered.
        with open(_ROUTE_MODULE, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=_ROUTE_MODULE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                self.assertNotIn(
                    name,
                    {
                        "__import__",
                        "import_module",
                        "walk_packages",
                        "iter_modules",
                        "entry_points",
                        "__subclasses__",
                    },
                    name,
                )

    def test_directory_enumeration_is_confined_to_the_full_coverage_check(self):
        # A separate rule, stated separately because it is a different thing:
        # the ``full`` route refuses when its literal list has fallen behind the
        # tree, and that single check is the only place the mechanism may look
        # at a directory. No selection anywhere reads one.
        with open(_ROUTE_MODULE, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=_ROUTE_MODULE)
        enum_attrs = {"listdir", "scandir", "walk", "glob", "iglob"}
        offenders = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(
                        inner.func, ast.Attribute
                    ):
                        if inner.func.attr in enum_attrs:
                            offenders.add(node.name)
        self.assertEqual({"full_coverage_reason"}, offenders)

    def test_the_route_module_refuses_to_run_as_main(self):
        with open(_ROUTE_MODULE, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('if __name__ == "__main__":', source)
        self.assertIn("raise SystemExit(EXIT_REFUSED)", source)


_ALL_HRCA_MODULES = frozenset(
    name[:-3]
    for name in os.listdir(_SRC)
    if name.endswith(".py") and name != "__init__.py"
)


class CliTests(unittest.TestCase):
    def test_list_routes_prints_every_route_without_running_one(self):
        from hrca import test_routes_cli

        captured = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(captured):
            code = test_routes_cli.main(["--list-routes"])
        self.assertEqual(test_routes_cli.EXIT_VERIFIED, code)
        output = captured.getvalue()
        for route in test_routes.route_names():
            self.assertIn("%s (" % route, output)

    def test_full_is_refused_through_the_cli_without_the_flag(self):
        from hrca import test_routes_cli

        captured = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(captured):
            code = test_routes_cli.main(["--route", "full"])
        self.assertEqual(test_routes_cli.EXIT_REFUSED, code)
        self.assertIn(
            test_routes.REASON_ACKNOWLEDGEMENT_REQUIRED, captured.getvalue()
        )

    def test_an_unknown_route_name_is_a_usage_error_not_a_pass(self):
        from hrca import test_routes_cli

        captured = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(captured):
            code = test_routes_cli.main(["--route", "no-such-route"])
        self.assertEqual(test_routes_cli.EXIT_REFUSED, code)


if __name__ == "__main__":
    unittest.main()
