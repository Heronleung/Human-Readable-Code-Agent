"""No-network guards for the P4.2a provider/credential seam.

The whole seam — fixed DeepSeek identity, credential store (including the Win32
binding), non-secret config and the backend CLI — must stay offline. It touches
only the local credential store and a non-secret config file and never makes a
provider, HTTP or inference request.
"""

from __future__ import annotations

import ast
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

_HRCA_ROOT = _SRC

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

_PROVIDER_SEAM_MODULES = {
    "deepseek": _hrca_module("deepseek"),
    "credential_store": _hrca_module("credential_store"),
    "credential_store_win": _hrca_module("credential_store_win"),
    "credential_sheet_win": _hrca_module("credential_sheet_win"),
    "provider_config": _hrca_module("provider_config"),
    "provider_cli": _hrca_module("provider_cli"),
    "advisory": _hrca_module("advisory"),
}

# Import statements that would indicate a network or HTTP dependency. None of
# these may appear in a seam module's source.
_NETWORK_IMPORT_TOKENS = (
    "import socket",
    "import urllib",
    "import requests",
    "import httpx",
    "import aiohttp",
    "import http",
    "from urllib",
    "from http",
    "urlopen",
)


class ProviderSeamNoNetworkTests(unittest.TestCase):
    def test_provider_seam_modules_exist(self):
        for module, path in _PROVIDER_SEAM_MODULES.items():
            with self.subTest(module=module):
                self.assertTrue(os.path.isfile(path), path)

    def test_provider_seam_modules_have_no_network_dependency(self):
        for module, path in _PROVIDER_SEAM_MODULES.items():
            with self.subTest(module=module):
                with open(path, "r", encoding="utf-8") as fh:
                    source = fh.read().lower()
                for token in _NETWORK_IMPORT_TOKENS:
                    self.assertNotIn(
                        token, source, f"{module} appears to import a network module"
                    )


if __name__ == "__main__":
    unittest.main()
