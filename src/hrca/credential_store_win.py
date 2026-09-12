"""Windows Credential Manager implementation (P4.2a, Windows only).

A dependency-free :mod:`ctypes` binding to the ``CredWriteW`` / ``CredReadW`` /
``CredDeleteW`` / ``CredFree`` Win32 APIs in ``advapi32.dll``. The secret is
stored as a generic credential under the fixed
:data:`~hrca.credential_store.TARGET_NAME` with user-local persistence
(``CRED_PERSIST_LOCAL_MACHINE`` — the standard "Generic Credentials" location).

Safety:

* the secret is held only as an opaque UTF-8 byte buffer passed to the API; it
  is never logged, printed, retained on an exception or otherwise serialized;
* every Win32 failure is mapped to a bounded
  :class:`~hrca.credential_store.CredentialStoreError` — the raw error code and
  message never leave this module;
* this module imports ``ctypes`` and calls the API only on Windows, and is
  selected solely by :func:`hrca.credential_store.make_credential_store`.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

from .credential_store import (
    CredentialStore,
    CredentialStoreError,
    TARGET_NAME,
    _require_secret,
    _require_target,
)

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

# Win32 error code for "element not found" (a credential that is absent).
_ERROR_NOT_FOUND = 1168

# Flags and error codes for the native secure credential prompt (credui.dll).
# The prompt is a single generic (plaintext) password field: CREDUIWIN_GENERIC
# returns the credential in plain text, and CREDUIWIN_IN_CRED_ONLY limits the
# dialog to the lone input credential field (the prefilled username is not
# editable). Two API constraints the binding must honour:
#
# * CREDUIWIN_SECURE_PROMPT cannot be combined with CREDUIWIN_GENERIC.
# * CREDUIWIN_IN_CRED_ONLY requires a non-NULL input buffer; with a NULL
#   buffer CredUIPromptForWindowsCredentialsW fails with ERROR_INVALID_PARAMETER
#   and shows no dialog (the remaining P4.2a enrollment defect).
_CREDUIWIN_GENERIC = 0x1
_CREDUIWIN_IN_CRED_ONLY = 0x20
_CREDUIWIN_SECURE_PROMPT = 0x1000
_PROMPT_FLAGS = _CREDUIWIN_GENERIC | _CREDUIWIN_IN_CRED_ONLY
_ERROR_CANCELLED = 1223
_ERROR_INVALID_PARAMETER = 87
# Session/desktop eligibility failures: the prompt cannot be created because the
# calling process is not in an interactive logon session able to present a
# credential dialog (the P4.2a "no dialog appears" hypothesis).
_ERROR_ACCESS_DENIED = 5
_ERROR_NOT_SUPPORTED = 50
_ERROR_NO_SUCH_LOGON_SESSION = 1312
_CRED_PACK_GENERIC_CREDENTIALS = 0x4

# Generous unpack buffer sizes: an API key is far shorter than these, but the
# buffers are allocated up-front so the unpacked blob can never overflow.
_CREDUI_USERNAME_MAX = 256
_CREDUI_DOMAIN_MAX = 256
_CREDUI_PASSWORD_MAX = 4096


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    # ``use_last_error=True`` makes ctypes capture GetLastError after each call,
    # which is required to distinguish "absent" (ERROR_NOT_FOUND) from a real
    # failure without reading the raw error text.
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return adv


class WindowsCredentialStore(CredentialStore):
    """Windows Credential Manager-backed credential store."""

    def __init__(self) -> None:
        self._advapi32 = _advapi32()

    def available(self) -> bool:
        return True

    def store(self, target, secret) -> None:
        _require_target(target)
        _require_secret(secret)
        blob = secret.encode("utf-8")
        buf = ctypes.create_string_buffer(blob)
        cred = _CREDENTIALW()
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = target
        if not self._advapi32.CredWriteW(ctypes.byref(cred), 0):
            raise CredentialStoreError("store_failed")

    def delete(self, target) -> None:
        _require_target(target)
        if not self._advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return  # idempotent: deleting an absent target succeeds
            raise CredentialStoreError("store_failed")

    def has(self, target) -> bool:
        _require_target(target)
        pcred = ctypes.POINTER(_CREDENTIALW)()
        ok = self._advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if ok:
            self._advapi32.CredFree(ctypes.cast(pcred, ctypes.c_void_p))
            return True
        # Absent or unreadable both mean "not present" for readiness purposes;
        # the credential is never materialized.
        return False

    def read(self, target) -> Optional[str]:
        _require_target(target)
        pcred = ctypes.POINTER(_CREDENTIALW)()
        ok = self._advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ok:
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError("store_failed")
        try:
            size = pcred.contents.CredentialBlobSize
            if not size:
                return ""
            blob = ctypes.cast(pcred.contents.CredentialBlob, ctypes.c_void_p)
            data = ctypes.string_at(blob, size)
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                # A non-UTF-8 blob is a bounded retrieval failure, never a raw
                # decode error (which could carry credential-derived bytes).
                raise CredentialStoreError("store_failed")
        finally:
            self._advapi32.CredFree(ctypes.cast(pcred, ctypes.c_void_p))


class _CREDUI_INFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hwndParent", wintypes.HWND),
        ("pszMessageText", wintypes.LPCWSTR),
        ("pszCaptionText", wintypes.LPCWSTR),
        ("hbmBanner", wintypes.HANDLE),
    ]


def _credui():
    """Bind the credui credential-prompt entry points (lazy, Windows only)."""
    credui = ctypes.WinDLL("credui", use_last_error=True)
    credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(_CREDUI_INFOW),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.ULONG),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
        ctypes.POINTER(wintypes.BOOL),
        wintypes.DWORD,
    ]
    credui.CredUIPromptForWindowsCredentialsW.restype = wintypes.DWORD
    credui.CredUnPackAuthenticationBufferW.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    credui.CredUnPackAuthenticationBufferW.restype = wintypes.BOOL
    credui.CredPackAuthenticationBufferW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(wintypes.DWORD),
    ]
    credui.CredPackAuthenticationBufferW.restype = wintypes.BOOL
    return credui


def _pack_generic_input(credui):
    """Pack a prefilled generic credential to pass as the prompt's input buffer.

    ``CREDUIWIN_IN_CRED_ONLY`` requires a non-NULL ``pvInAuthBuffer``; passing
    ``NULL`` makes ``CredUIPromptForWindowsCredentialsW`` fail with
    ``ERROR_INVALID_PARAMETER`` and show no dialog. This packs a generic
    credential (fixed username = the application target name, empty password)
    into an opaque buffer so the dialog shows only the password field for the
    user to type the API key. The packed buffer is a plain byte blob holding no
    secret and is freed with the ``buf`` reference when the caller drops it.
    """
    username = TARGET_NAME
    password = ""
    # Two-pass: the first call with a NULL buffer reports the required size.
    size = wintypes.DWORD(0)
    credui.CredPackAuthenticationBufferW(
        _CRED_PACK_GENERIC_CREDENTIALS, username, password, None, ctypes.byref(size)
    )
    if size.value <= 0:
        raise CredentialStoreError("prompt_failed")
    buf = ctypes.create_string_buffer(size.value)
    if not credui.CredPackAuthenticationBufferW(
        _CRED_PACK_GENERIC_CREDENTIALS,
        username,
        password,
        ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
        ctypes.byref(size),
    ):
        raise CredentialStoreError("prompt_failed")
    return buf, size.value


def _prompt_error_code(result) -> str:
    """Map a non-zero, non-cancel CredUI return to a bounded error code.

    The Win32 return code is classified into a fixed, non-secret catalogue
    (never the raw error text or the numeric code): an argument rejection, a
    session/desktop eligibility failure, or a generic prompt failure.
    """
    if result == _ERROR_INVALID_PARAMETER:
        return "prompt_invalid_argument"
    if result in (_ERROR_ACCESS_DENIED, _ERROR_NOT_SUPPORTED, _ERROR_NO_SUCH_LOGON_SESSION):
        return "prompt_session_unavailable"
    return "prompt_failed"


def prompt_secret(
    message: str, hwnd_parent: Optional[int] = None
) -> Optional[str]:
    """Show the Windows native secure credential prompt and return the secret.

    The prompt is the operating system's own credential dialog; the returned
    secret is passed straight to :meth:`WindowsCredentialStore.store` by the
    boundary and is never logged, printed, retained on an exception or
    serialized. ``hwnd_parent`` is an optional native window handle (the desktop
    application's top-level window) that becomes the dialog's owner so the
    prompt appears modal to, and in front of, the visible application window in
    the interactive session; when ``None`` the dialog is unowned. Returns
    ``None`` when the user cancels and raises a bounded
    :class:`~hrca.credential_store.CredentialStoreError` on any other failure
    (``prompt_invalid_argument`` / ``prompt_session_unavailable`` /
    ``prompt_failed`` — never the raw Win32 error code or message).
    """
    credui = _credui()

    ui = _CREDUI_INFOW()
    ui.cbSize = ctypes.sizeof(_CREDUI_INFOW)
    ui.hwndParent = ctypes.c_void_p(hwnd_parent) if hwnd_parent is not None else None
    ui.pszMessageText = message
    ui.pszCaptionText = "DeepSeek API key"
    ui.hbmBanner = None

    # CREDUIWIN_IN_CRED_ONLY needs a non-NULL input buffer (see
    # _pack_generic_input); passing NULL would make the API fail with
    # ERROR_INVALID_PARAMETER and show no dialog.
    in_buffer, in_size = _pack_generic_input(credui)

    auth_package = wintypes.ULONG(0)
    out_buffer = ctypes.c_void_p()
    out_size = wintypes.ULONG(0)
    save = wintypes.BOOL(False)
    result = credui.CredUIPromptForWindowsCredentialsW(
        ctypes.byref(ui),
        0,
        ctypes.byref(auth_package),
        ctypes.cast(in_buffer, ctypes.c_void_p),
        in_size,
        ctypes.byref(out_buffer),
        ctypes.byref(out_size),
        ctypes.byref(save),
        _PROMPT_FLAGS,
    )
    if result != 0:
        if result == _ERROR_CANCELLED:
            return None
        raise CredentialStoreError(_prompt_error_code(result))

    try:
        # Unpack the generic credential blob (username + domain + password).
        # Only the password — the API key — is returned; the username is the
        # prefilled target name, hidden by CREDUIWIN_IN_CRED_ONLY.
        username = ctypes.create_unicode_buffer(_CREDUI_USERNAME_MAX)
        domain = ctypes.create_unicode_buffer(_CREDUI_DOMAIN_MAX)
        password = ctypes.create_unicode_buffer(_CREDUI_PASSWORD_MAX)
        u_size = wintypes.DWORD(_CREDUI_USERNAME_MAX)
        d_size = wintypes.DWORD(_CREDUI_DOMAIN_MAX)
        p_size = wintypes.DWORD(_CREDUI_PASSWORD_MAX)
        if not credui.CredUnPackAuthenticationBufferW(
            _CRED_PACK_GENERIC_CREDENTIALS,
            out_buffer,
            out_size,
            username,
            ctypes.byref(u_size),
            domain,
            ctypes.byref(d_size),
            password,
            ctypes.byref(p_size),
        ):
            raise CredentialStoreError("prompt_failed")
        return password.value
    finally:
        # The out buffer is a CoTaskMemAlloc block; free it with CoTaskMemFree.
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        ole32.CoTaskMemFree(out_buffer)


__all__ = ["WindowsCredentialStore", "prompt_secret"]
