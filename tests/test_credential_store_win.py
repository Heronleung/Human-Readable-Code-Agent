"""Native-safe tests for the Windows credential-store binding (P4.2a).

These tests import :mod:`hrca.credential_store_win` — safe on every platform,
because the module only *declares* its ``ctypes`` Win32 bindings and never
calls a Win32 API at import time — and assert the credential-prompt flag
invariant that the P4.2a enrollment repair depends on. No test here invokes
``CredUIPromptForWindowsCredentialsW``, writes to the real Credential Manager,
or reads a stored credential.
"""

from __future__ import annotations

import unittest

from hrca import credential_store_win


class PromptFlagTests(unittest.TestCase):
    """The native prompt flags must be a valid, single-field generic prompt.

    The P4.2a enrollment defect was caused by combining ``CREDUIWIN_GENERIC``
    with ``CREDUIWIN_SECURE_PROMPT``, which the Win32 API rejects with
    ``ERROR_INVALID_PARAMETER`` and shows no dialog. These tests lock the
    corrected flag invariant so the defect cannot regress silently.
    """

    def test_prompt_flags_request_a_generic_credential(self):
        # CREDUIWIN_GENERIC returns the credential in plain text, which is how
        # the API key is read back out of the native prompt for storage.
        self.assertTrue(
            credential_store_win._PROMPT_FLAGS & credential_store_win._CREDUIWIN_GENERIC
        )

    def test_prompt_flags_limit_to_input_credential(self):
        # CREDUIWIN_IN_CRED_ONLY limits the dialog to the lone input field, so
        # the user is prompted for exactly the API key (no username field).
        self.assertTrue(
            credential_store_win._PROMPT_FLAGS & credential_store_win._CREDUIWIN_IN_CRED_ONLY
        )

    def test_prompt_flags_exclude_secure_prompt(self):
        # CREDUIWIN_GENERIC and CREDUIWIN_SECURE_PROMPT are mutually exclusive;
        # the prompt therefore runs on the interactive desktop, never the
        # secure desktop.
        self.assertEqual(
            credential_store_win._PROMPT_FLAGS
            & credential_store_win._CREDUIWIN_SECURE_PROMPT,
            0,
        )

    def test_prompt_flags_are_nonzero_and_bounded(self):
        # The corrected combination is exactly GENERIC | IN_CRED_ONLY.
        self.assertEqual(
            credential_store_win._PROMPT_FLAGS,
            credential_store_win._CREDUIWIN_GENERIC
            | credential_store_win._CREDUIWIN_IN_CRED_ONLY,
        )


if __name__ == "__main__":
    unittest.main()
