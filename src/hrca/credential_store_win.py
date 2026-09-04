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
    _require_secret,
    _require_target,
)

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

# Win32 error code for "element not found" (a credential that is absent).
_ERROR_NOT_FOUND = 1168


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
            return data.decode("utf-8")
        finally:
            self._advapi32.CredFree(ctypes.cast(pcred, ctypes.c_void_p))


__all__ = ["WindowsCredentialStore"]
