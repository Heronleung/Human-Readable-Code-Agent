"""No-network guards for the P4.2a provider/credential seam.

The whole seam — fixed DeepSeek identity, credential store (including the Win32
binding), non-secret config and the backend CLI — must stay offline. It touches
only the local credential store and a non-secret config file and never makes a
provider, HTTP or inference request.
"""

from __future__ import annotations

import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

_PROVIDER_SEAM_MODULES = {
    "deepseek": os.path.join(_SRC, "deepseek.py"),
    "credential_store": os.path.join(_SRC, "credential_store.py"),
    "credential_store_win": os.path.join(_SRC, "credential_store_win.py"),
    "provider_config": os.path.join(_SRC, "provider_config.py"),
    "provider_cli": os.path.join(_SRC, "provider_cli.py"),
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
