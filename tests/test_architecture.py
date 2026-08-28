"""Architecture import-rule tests (P3.1).

A client module must never import the deterministic core (scanner, planner,
report builder), the provider protocol, Git tooling, or any command-execution
code. The client is a client only: it consumes the versioned contract, never
the core, and never decides that an action is permitted.

Import statements are inspected with :mod:`ast`, so prose references in
docstrings do not produce false positives.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

_CLIENT_PATH = os.path.join(_SRC, "client.py")

# Modules that are part of the client boundary and must therefore stay free of
# any core / provider / Git / command-execution import. ``hrca.style`` is
# included because it is the desktop-only visual layer and must own nothing but
# presentation tokens.
_CLIENT_MODULES = {
    "hrca.client": os.path.join(_SRC, "client.py"),
    "hrca.client_core": os.path.join(_SRC, "client_core.py"),
    "hrca.style": os.path.join(_SRC, "style.py"),
}

# The shared contract is Qt-free and must not import the core either.
_CONTRACT_MODULE = os.path.join(_SRC, "contract.py")

# The workspace policy is boundary-side: it may import stdlib and the contract,
# but never the deterministic core.
_WORKSPACE_MODULE = os.path.join(_SRC, "workspace.py")

# Top-level module names that a client or contract module must never import.
_FORBIDDEN_TOP_LEVEL = frozenset(
    {"scanner", "planning", "report", "provider", "subprocess", "git"}
)


def _imported_top_level_names(path: str) -> set:
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                names.add(node.module.split(".")[0])
            elif node.level:  # relative import such as ``from . import scanner``
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
    return names


class ClientArchitectureTests(unittest.TestCase):
    def test_client_modules_do_not_import_core(self):
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_FORBIDDEN_TOP_LEVEL),
                    f"{module} imports forbidden modules: "
                    f"{sorted(imported & _FORBIDDEN_TOP_LEVEL)}",
                )

    def test_contract_module_does_not_import_core(self):
        imported = _imported_top_level_names(_CONTRACT_MODULE)
        self.assertTrue(imported.isdisjoint(_FORBIDDEN_TOP_LEVEL))

    def test_workspace_module_does_not_import_core(self):
        imported = _imported_top_level_names(_WORKSPACE_MODULE)
        self.assertTrue(imported.isdisjoint(_FORBIDDEN_TOP_LEVEL))

    def test_client_modules_do_not_import_workspace(self):
        # The workspace policy owns path containment on the boundary side; a
        # client that imported it would gain direct filesystem access.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertNotIn(
                    "workspace",
                    imported,
                    f"{module} imports the boundary-side workspace policy",
                )

    def test_client_modules_exist(self):
        for path in _CLIENT_MODULES.values():
            self.assertTrue(os.path.isfile(path), path)


def _stylesheet_calls(path: str) -> list:
    """Return every ``setStyleSheet(...)`` call in ``path`` (ast.Call nodes)."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setStyleSheet":
                calls.append(node)
    return calls


class StyleOwnershipTests(unittest.TestCase):
    """``hrca.style`` owns every visual value; ``hrca.client`` composes none.

    The client must contain no hex colour literal, no visual geometry literal
    (a pixel value controlling component size, margin, padding, spacing, radius,
    typography or splitter geometry), and every widget style-sheet must be
    produced by a ``style.*`` factory rather than assembled inline.
    """

    # Geometry methods where *every* numeric argument is a visual value (px).
    _ALL_ARG_GEOMETRY = frozenset(
        {
            "setContentsMargins",
            "setSpacing",
            "setFixedHeight",
            "setFixedWidth",
            "setFixedSize",
            "setMinimumWidth",
            "setMaximumWidth",
            "setMinimumHeight",
            "setMaximumHeight",
            "setMinimumSize",
            "setMaximumSize",
            "setBaseSize",
            "resize",
            "setIndentation",
            "setHandleWidth",
            "setGeometry",
        }
    )

    # Geometry methods where only certain *positional* arguments are visual
    # (the rest are widget indexes, enum modes, or booleans — not pixel values).
    _POSITIONAL_ARG_GEOMETRY = {
        "setStretchFactor": (1,),  # index, factor — only the factor is visual
        "setLineHeight": (0,),     # height, mode — only the height is visual
    }

    @staticmethod
    def _contains_numeric_literal(node: ast.AST) -> bool:
        """Return True if ``node`` or any descendant is an int/float literal."""
        return any(
            isinstance(child, ast.Constant) and isinstance(child.value, (int, float))
            for child in ast.walk(node)
        )

    def test_client_has_no_geometry_literal(self):
        with open(_CLIENT_PATH, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if name == "setSizes":
                # The sole argument is a list whose elements are pane widths.
                if node.args and isinstance(node.args[0], ast.List):
                    for element in node.args[0].elts:
                        if self._contains_numeric_literal(element):
                            violations.append((node.lineno, name, "pane width"))
            elif name in self._ALL_ARG_GEOMETRY:
                for arg in node.args:
                    if self._contains_numeric_literal(arg):
                        violations.append((node.lineno, name))
            elif name in self._POSITIONAL_ARG_GEOMETRY:
                for index in self._POSITIONAL_ARG_GEOMETRY[name]:
                    if index < len(node.args) and self._contains_numeric_literal(
                        node.args[index]
                    ):
                        violations.append((node.lineno, name))
            elif name == "_status_field":
                # The helper forwards ``max_width`` straight to setMaximumWidth,
                # so its keyword value is a fixed-widget-width constant.
                for kw in node.keywords:
                    if kw.arg == "max_width" and self._contains_numeric_literal(kw.value):
                        violations.append((node.lineno, name, "max_width"))

        self.assertFalse(
            violations,
            "client.py hard-codes a visual geometry literal; move it to hrca.style: "
            f"{violations}",
        )

    def test_client_has_no_hex_color_literal(self):
        with open(_CLIENT_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIsNone(
            re.search(r"#[0-9a-fA-F]{3,8}\b", source),
            "client.py hard-codes a colour literal; move it to hrca.style",
        )

    def test_client_stylesheets_use_style_factories(self):
        for call in _stylesheet_calls(_CLIENT_PATH):
            arg = call.args[0]
            self.assertIsInstance(arg, ast.Call)
            self.assertIsInstance(arg.func, ast.Attribute)
            self.assertIsInstance(arg.func.value, ast.Name)
            self.assertEqual(
                arg.func.value.id,
                "style",
                "client.py assembles a style-sheet inline; use a style factory",
            )


if __name__ == "__main__":
    unittest.main()
