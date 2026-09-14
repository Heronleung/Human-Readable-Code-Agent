"""Native-safe tests for the Windows credential-store binding (P4.2a).

These tests import :mod:`hrca.credential_store_win` — safe on every platform,
because the module only *declares* its ``ctypes`` Win32 bindings and never
calls a Win32 API at import time — and assert the flag/struct/binding
invariants the P4.2a enrollment repair depends on. Tests that must actually
resolve a Win32 DLL or call a packing API are gated on ``os.name == "nt"`` so
the suite still runs (and skips) on a non-Windows host. No test here invokes
``CredUIPromptForWindowsCredentialsW``, writes to the real Credential Manager,
or reads a stored credential.
"""

from __future__ import annotations

import ctypes
import os
import unittest
from ctypes import wintypes

from hrca import credential_store_win


class PromptFlagTests(unittest.TestCase):
    """The native prompt flags must be a valid, single-field generic prompt.

    The P4.2a enrollment defect was caused by combining ``CREDUIWIN_GENERIC``
    with ``CREDUIWIN_SECURE_PROMPT`` (rejected with ERROR_INVALID_PARAMETER)
    and, after that was removed, by using ``CREDUIWIN_IN_CRED_ONLY`` with a
    NULL input buffer (also rejected with ERROR_INVALID_PARAMETER). These tests
    lock the corrected flag invariant so the defect cannot regress silently.
    """

    def test_prompt_flags_request_a_generic_credential(self):
        self.assertTrue(
            credential_store_win._PROMPT_FLAGS & credential_store_win._CREDUIWIN_GENERIC
        )

    def test_prompt_flags_limit_to_input_credential(self):
        self.assertTrue(
            credential_store_win._PROMPT_FLAGS & credential_store_win._CREDUIWIN_IN_CRED_ONLY
        )

    def test_prompt_flags_exclude_secure_prompt(self):
        self.assertEqual(
            credential_store_win._PROMPT_FLAGS
            & credential_store_win._CREDUIWIN_SECURE_PROMPT,
            0,
        )

    def test_prompt_flags_are_nonzero_and_bounded(self):
        self.assertEqual(
            credential_store_win._PROMPT_FLAGS,
            credential_store_win._CREDUIWIN_GENERIC
            | credential_store_win._CREDUIWIN_IN_CRED_ONLY,
        )


class PromptErrorClassificationTests(unittest.TestCase):
    """The CredUI return code maps to a bounded, non-secret failure category."""

    def test_invalid_argument_is_classified(self):
        self.assertEqual(
            credential_store_win._prompt_error_code(87), "prompt_invalid_argument"
        )

    def test_session_eligibility_failures_are_classified(self):
        for code in (5, 50, 1312):
            self.assertEqual(
                credential_store_win._prompt_error_code(code),
                "prompt_session_unavailable",
                code,
            )

    def test_unknown_failure_is_generic(self):
        self.assertEqual(credential_store_win._prompt_error_code(31), "prompt_failed")
        self.assertEqual(credential_store_win._prompt_error_code(9999), "prompt_failed")

    def test_classification_is_bounded(self):
        # Every classification is a CredentialStoreError catalogue code.
        for code in (5, 50, 87, 31, 1312, 9999):
            category = credential_store_win._prompt_error_code(code)
            self.assertIn(
                category,
                {"prompt_invalid_argument", "prompt_session_unavailable", "prompt_failed"},
            )


class StructureLayoutTests(unittest.TestCase):
    """The ctypes structures must match the Win32 ABI layout exactly.

    A size/offset mismatch makes ``CredUIPromptForWindowsCredentialsW`` reject
    the ``CREDUI_INFOW`` and show no dialog. These checks run on every platform
    because they only inspect the declared structure, never the Win32 API.
    """

    def test_credui_info_struct_has_the_five_fields(self):
        fields = [name for name, _ in credential_store_win._CREDUI_INFOW._fields_]
        self.assertEqual(
            fields,
            ["cbSize", "hwndParent", "pszMessageText", "pszCaptionText", "hbmBanner"],
        )

    def test_credui_info_cbsize_is_first_and_sized(self):
        first = credential_store_win._CREDUI_INFOW._fields_[0]
        self.assertEqual(first[0], "cbSize")
        self.assertEqual(first[1], wintypes.DWORD)
        # cbSize is the DWORD at offset 0, so it is always sizeof(DWORD).
        self.assertEqual(
            credential_store_win._CREDUI_INFOW.cbSize.offset, 0
        )

    def test_credui_info_pointer_fields_follow_pointer_size(self):
        # hwndParent / pszMessageText / pszCaptionText / hbmBanner are all
        # pointer-width on the target ABI.
        for name, ftype in credential_store_win._CREDUI_INFOW._fields_:
            if name in ("hwndParent", "pszMessageText", "pszCaptionText", "hbmBanner"):
                self.assertEqual(
                    ctypes.sizeof(ftype),
                    ctypes.sizeof(ctypes.c_void_p),
                    name,
                )


@unittest.skipUnless(os.name == "nt", "requires the Windows credui/advapi32 DLLs")
class NativeBindingTests(unittest.TestCase):
    """Resolve the real Win32 DLLs and verify the ctypes bindings.

    These run only on Windows. They bind the functions and, for the packing
    helper, perform a pure in-memory ``CredPackAuthenticationBufferW`` round-trip
    — no dialog is shown and no credential is stored or read.
    """

    def test_credui_binds_the_prompt_function(self):
        credui = credential_store_win._credui()
        fn = credui.CredUIPromptForWindowsCredentialsW
        self.assertEqual(fn.restype, wintypes.DWORD)
        self.assertEqual(len(fn.argtypes), 9)
        self.assertEqual(fn.argtypes[0], ctypes.POINTER(credential_store_win._CREDUI_INFOW))
        self.assertEqual(fn.argtypes[3], ctypes.c_void_p)  # pvInAuthBuffer
        self.assertEqual(fn.argtypes[5], ctypes.POINTER(ctypes.c_void_p))
        self.assertEqual(fn.argtypes[8], wintypes.DWORD)  # dwFlags

    def test_credui_binds_the_pack_and_unpack_functions(self):
        credui = credential_store_win._credui()
        pack = credui.CredPackAuthenticationBufferW
        self.assertEqual(pack.restype, wintypes.BOOL)
        self.assertEqual(len(pack.argtypes), 5)
        self.assertEqual(pack.argtypes[3], ctypes.POINTER(ctypes.c_ubyte))
        self.assertEqual(pack.argtypes[4], ctypes.POINTER(wintypes.DWORD))

        unpack = credui.CredUnPackAuthenticationBufferW
        self.assertEqual(unpack.restype, wintypes.BOOL)
        self.assertEqual(len(unpack.argtypes), 9)

    def test_advapi32_binds_the_store_functions(self):
        adv = credential_store_win._advapi32()
        self.assertEqual(adv.CredWriteW.restype, wintypes.BOOL)
        self.assertEqual(adv.CredReadW.restype, wintypes.BOOL)
        self.assertEqual(adv.CredDeleteW.restype, wintypes.BOOL)
        self.assertEqual(
            adv.CredReadW.argtypes[3], ctypes.POINTER(ctypes.POINTER(credential_store_win._CREDENTIALW))
        )

    def test_pack_generic_input_produces_a_non_null_buffer(self):
        # CREDUIWIN_IN_CRED_ONLY requires a non-NULL input buffer; this proves
        # the packing path actually produces a usable buffer of a positive size.
        credui = credential_store_win._credui()
        buf, size = credential_store_win._pack_generic_input(credui)
        self.assertGreater(size, 0)
        self.assertGreaterEqual(ctypes.sizeof(buf), size)

    def test_pack_then_unpack_round_trips(self):
        # The packed buffer is exactly what the prompt receives as its input;
        # unpacking it back must yield the prefilled username and the empty
        # password, proving both the pack and unpack bindings are well-formed.
        credui = credential_store_win._credui()
        buf, size = credential_store_win._pack_generic_input(credui)
        username = ctypes.create_unicode_buffer(credential_store_win._CREDUI_USERNAME_MAX)
        domain = ctypes.create_unicode_buffer(credential_store_win._CREDUI_DOMAIN_MAX)
        password = ctypes.create_unicode_buffer(credential_store_win._CREDUI_PASSWORD_MAX)
        u_size = wintypes.DWORD(credential_store_win._CREDUI_USERNAME_MAX)
        d_size = wintypes.DWORD(credential_store_win._CREDUI_DOMAIN_MAX)
        p_size = wintypes.DWORD(credential_store_win._CREDUI_PASSWORD_MAX)
        ok = credui.CredUnPackAuthenticationBufferW(
            credential_store_win._CRED_PACK_GENERIC_CREDENTIALS,
            ctypes.cast(buf, ctypes.c_void_p),
            size,
            username,
            ctypes.byref(u_size),
            domain,
            ctypes.byref(d_size),
            password,
            ctypes.byref(p_size),
        )
        self.assertTrue(ok)
        self.assertEqual(username.value, credential_store_win.TARGET_NAME)
        self.assertEqual(password.value, "")


if __name__ == "__main__":
    unittest.main()
