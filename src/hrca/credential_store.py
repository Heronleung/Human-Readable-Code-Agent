"""Credential-store port (P4.2a).

A narrow backend-owned seam for storing an opaque API key behind a stable
application-owned target name. It defines the port plus two concrete
implementations used by tests and by non-Windows platforms:

* :class:`FakeCredentialStore` — a deterministic in-memory store for tests; it
  holds non-secret fixtures only and never touches the operating system.
* :class:`UnavailableCredentialStore` — the honest non-Windows answer: presence
  is always ``False`` and every mutating call raises a bounded ``unavailable``
  error.

The production Windows implementation lives in
:mod:`hrca.credential_store_win` and is selected by :func:`make_credential_store`
on Windows only. The port never logs, prints, serializes or retains a secret or
a raw underlying error; every failure is mapped to a bounded
:class:`CredentialStoreError` drawn from a fixed catalogue.
"""

from __future__ import annotations

import abc
import os
from typing import Optional

# Stable, application-owned Windows Credential Manager target name. This is the
# only name the application ever writes under; it is a fixed constant and is
# never derived from a repository, an environment variable or user input.
TARGET_NAME = "hrca:deepseek"

# Maximum target-name length enforced by :func:`_require_target`.
_MAX_TARGET_CHARS = 256

# Bounded error code -> fixed message catalogue. A CredentialStoreError retains
# only its code; the message is always drawn from this table so a caller or an
# underlying OS error (and any secret it might carry) can never be retained or
# serialized. The valid codes are exactly the keys of this table.
_SAFE_MESSAGES = {
    "invalid_target": "the credential target name is invalid",
    "invalid_secret": "the credential secret is invalid",
    "store_failed": "the credential store operation failed",
    "unavailable": "credential storage is unavailable on this platform",
    "prompt_failed": "the secure credential prompt failed",
}


class CredentialStoreError(Exception):
    """Bounded, sanitized credential-store failure."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _SAFE_MESSAGES:
            raise ValueError("invalid credential store error code")
        self.code = code
        self.message = _SAFE_MESSAGES[code]
        super().__init__(code, self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _require_target(target: str) -> str:
    if (
        not isinstance(target, str)
        or not target
        or len(target) > _MAX_TARGET_CHARS
        or any(ord(ch) < 32 for ch in target)
    ):
        raise CredentialStoreError("invalid_target")
    return target


def _require_secret(secret: str) -> str:
    if not isinstance(secret, str) or not secret:
        raise CredentialStoreError("invalid_secret")
    return secret


class CredentialStore(abc.ABC):
    """Port for store/replace/delete/presence/read semantics.

    Implementations are platform-specific; the port itself never touches the
    operating system. ``read`` returns the stored secret only to future backend
    adapter code — the redacted readiness path must use :meth:`has` (presence)
    and never materialize the secret into a result.
    """

    @abc.abstractmethod
    def available(self) -> bool:
        """Return True when this store can actually persist credentials."""

    @abc.abstractmethod
    def store(self, target: str, secret: str) -> None:
        """Store or replace ``secret`` under ``target``."""

    @abc.abstractmethod
    def delete(self, target: str) -> None:
        """Delete ``target``; idempotent (deleting an absent target succeeds)."""

    @abc.abstractmethod
    def has(self, target: str) -> bool:
        """Return True when a credential is present for ``target``."""

    @abc.abstractmethod
    def read(self, target: str) -> Optional[str]:
        """Return the stored secret for ``target``, or ``None`` when absent."""


class FakeCredentialStore(CredentialStore):
    """Deterministic in-memory store for tests (non-secret fixtures only)."""

    def __init__(self) -> None:
        self._values = {}

    def available(self) -> bool:
        return True

    def store(self, target, secret) -> None:
        _require_target(target)
        _require_secret(secret)
        self._values[target] = secret

    def delete(self, target) -> None:
        _require_target(target)
        self._values.pop(target, None)

    def has(self, target) -> bool:
        _require_target(target)
        return target in self._values

    def read(self, target) -> Optional[str]:
        _require_target(target)
        return self._values.get(target)


class UnavailableCredentialStore(CredentialStore):
    """Honest non-Windows store: presence is False; mutation raises ``unavailable``."""

    def available(self) -> bool:
        return False

    def store(self, target, secret) -> None:
        _require_target(target)
        _require_secret(secret)
        raise CredentialStoreError("unavailable")

    def delete(self, target) -> None:
        _require_target(target)
        raise CredentialStoreError("unavailable")

    def has(self, target) -> bool:
        _require_target(target)
        return False

    def read(self, target) -> Optional[str]:
        _require_target(target)
        raise CredentialStoreError("unavailable")


def make_credential_store() -> CredentialStore:
    """Return the platform credential store (Windows) or the unavailable store.

    The Windows implementation is imported lazily so a non-Windows process (or a
    frozen ``--serve`` loop that never queries readiness) never pulls in
    ``ctypes`` credential code.
    """
    if os.name == "nt":
        from .credential_store_win import WindowsCredentialStore

        return WindowsCredentialStore()
    return UnavailableCredentialStore()


# Bounded result states for the backend-owned credential manage/remove actions
# (P4.2a). A result carries only a state token and credential *presence* — never
# the secret, its length, a prefix, a suffix or any representation of it.
CREDENTIAL_STATE_STORED = "stored"
CREDENTIAL_STATE_CANCELLED = "cancelled"
CREDENTIAL_STATE_REMOVED = "removed"
CREDENTIAL_STATE_UNAVAILABLE = "unavailable"
CREDENTIAL_STATE_FAILED = "failed"
CREDENTIAL_RESULT_STATES = frozenset(
    {
        CREDENTIAL_STATE_STORED,
        CREDENTIAL_STATE_CANCELLED,
        CREDENTIAL_STATE_REMOVED,
        CREDENTIAL_STATE_UNAVAILABLE,
        CREDENTIAL_STATE_FAILED,
    }
)


def redacted_credential_result(state: str, credential_present: bool) -> dict:
    """Return a bounded, secret-free credential action result (P4.2a).

    ``state`` is one of :data:`CREDENTIAL_RESULT_STATES`; ``credential_present``
    is the redacted presence fact. No secret, error text or representation ever
    appears in the returned mapping.
    """
    return {"state": state, "credential_present": bool(credential_present)}


def native_credential_prompt():
    """Return the platform secure credential prompt, or ``None`` when absent.

    On Windows this is :func:`hrca.credential_store_win.prompt_secret`, the
    operating system's own credential dialog; every other platform has no
    coherent secure prompt and returns ``None`` so the boundary reports the
    action unavailable rather than falling back to insecure input.
    """
    if os.name == "nt":
        from .credential_store_win import prompt_secret

        return prompt_secret
    return None


__all__ = [
    "TARGET_NAME",
    "CredentialStoreError",
    "CredentialStore",
    "FakeCredentialStore",
    "UnavailableCredentialStore",
    "make_credential_store",
    "CREDENTIAL_STATE_STORED",
    "CREDENTIAL_STATE_CANCELLED",
    "CREDENTIAL_STATE_REMOVED",
    "CREDENTIAL_STATE_UNAVAILABLE",
    "CREDENTIAL_STATE_FAILED",
    "CREDENTIAL_RESULT_STATES",
    "redacted_credential_result",
    "native_credential_prompt",
]
