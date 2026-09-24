"""The frozen-build launcher's import target (P5-E2/C).

``packaging/launcher.py`` is the entry a PyInstaller build bundles. Nothing in
the package imports it and no runtime or test closure reaches it, so when the
package was organised by responsibility its import target was left naming
``hrca.app`` — a module that ceased to exist — and nothing failed.

These tests hold the target to a module that really exists, is really importable
without the optional desktop extra, and really exposes the entry point. They
build nothing: no PyInstaller, no frozen artifact, no GUI, and no inspection of
any other checkout. Importing the launcher by path is what makes the last
assertion possible without packaging it.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
LAUNCHER = os.path.join(REPO, "packaging", "launcher.py")


def _imported_modules(path):
    """Return every module the file at ``path`` imports, by dotted name."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                names.append(node.module)
    return names


def _module_level_imports(path):
    """Return the dotted names imported at module scope of ``path``."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                names.append(node.module)
    return names


def _load_launcher():
    """Import ``packaging/launcher.py`` by path, without packaging it."""
    name = "hrca_packaging_launcher_under_test"
    spec = importlib.util.spec_from_file_location(name, LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherTargetTests(unittest.TestCase):
    """The launcher names a module that exists, and the entry it names."""

    def test_the_launcher_exists(self):
        self.assertTrue(os.path.isfile(LAUNCHER))

    def test_the_launcher_names_exactly_one_product_module(self):
        # A launcher that imported several product modules would be building
        # something other than the unified entry it documents.
        product = [
            name
            for name in _imported_modules(LAUNCHER)
            if name == "hrca" or name.startswith("hrca.")
        ]
        self.assertEqual(1, len(product), product)

    def test_the_named_module_exists(self):
        # The defect this test exists for: the target resolving to nothing.
        for name in _imported_modules(LAUNCHER):
            if name == "hrca" or name.startswith("hrca."):
                with self.subTest(module=name):
                    self.assertIsNotNone(
                        importlib.util.find_spec(name),
                        "the launcher's import target does not exist: %s" % name,
                    )

    def test_the_named_module_is_importable_and_exposes_main(self):
        module = importlib.import_module("hrca.cli.app")
        self.assertTrue(callable(module.main))

    def test_the_launcher_binds_the_entry_points_own_main(self):
        # The strongest form the assertion can take without building: the
        # object the frozen entry would run is the entry's own.
        launcher = _load_launcher()
        entry = importlib.import_module("hrca.cli.app")
        self.assertIs(entry.main, launcher.main)

    def test_the_entry_point_does_not_need_the_desktop_extra_to_import(self):
        # This is what lets the test above run on a core route. The desktop
        # client is imported lazily inside the entry, never at module scope, so
        # an install without the optional extra can still import it.
        imported = _module_level_imports(importlib.util.find_spec("hrca.cli.app").origin)
        for name in imported:
            with self.subTest(module=name):
                self.assertNotIn(name.split(".")[0], {"PySide6", "PyQt5", "PyQt6"})
        self.assertNotIn("ui.client", imported)

    def test_the_entry_point_carries_no_time_of_its_own(self):
        # A guard against the launcher being repaired by copying a timestamped
        # build script: the entry itself is deterministic.
        with open(
            importlib.util.find_spec("hrca.cli.app").origin, "r", encoding="utf-8"
        ) as handle:
            source = handle.read()
        for marker in ("__version__", "build_time", "BUILD_TIME"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
