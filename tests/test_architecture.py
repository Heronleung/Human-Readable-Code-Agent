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
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

# Modules that are part of the client boundary and must therefore stay free of
# any core / provider / Git / command-execution import.
_CLIENT_MODULES = {
    "hrca.client": os.path.join(_SRC, "client.py"),
    "hrca.client_core": os.path.join(_SRC, "client_core.py"),
}

# The shared contract is Qt-free and must not import the core either.
_CONTRACT_MODULE = os.path.join(_SRC, "contract.py")

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

    def test_client_modules_exist(self):
        for path in _CLIENT_MODULES.values():
            self.assertTrue(os.path.isfile(path), path)


if __name__ == "__main__":
    unittest.main()
