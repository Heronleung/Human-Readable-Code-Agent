"""Backend-owned DeepSeek credential configuration CLI (P4.2a).

Enroll/replace and delete an API key, and print redacted local readiness. The
API key is read only through a no-echo prompt (:func:`getpass.getpass`) and is
never accepted from a CLI argument, environment variable, stdin NDJSON field or
desktop text field, and never printed to stdout/stderr or placed in an
exception.

This CLI is backend infrastructure, not part of the NDJSON boundary: the
desktop client never invokes it and never imports this module. It is reachable
from source as ``python -m hrca.provider_cli`` and from the frozen executable as
``hrca-app --provider <enroll|delete|readiness>``.
"""

from __future__ import annotations

import getpass
import json
import sys
from typing import Any, Callable, Dict, Optional, Sequence

from . import credential_store, deepseek, provider_config, twin_store

# Argument sentinel used by the unified entry executable (see hrca.app).
PROVIDER_SENTINEL = "--provider"

_USAGE = "usage: hrca-provider <enroll|delete|readiness>"

_ENROLL = "enroll"
_DELETE = "delete"
_READINESS = "readiness"


def _readiness_result(
    base_dir: str, store: credential_store.CredentialStore
) -> Dict[str, Any]:
    config, config_error = provider_config.load(base_dir)
    if config is None and config_error is None:
        config = provider_config.default_config()
    store_available = store.available()
    credential_present = (
        store.has(credential_store.TARGET_NAME) if store_available else False
    )
    return deepseek.redacted_readiness(
        config=config,
        config_error=config_error,
        credential_present=credential_present,
        store_available=store_available,
    )


def _enroll(store: credential_store.CredentialStore, prompt: Callable) -> int:
    if not store.available():
        print("unavailable", file=sys.stderr)
        return 1
    try:
        secret = prompt("DeepSeek API key: ")
    except (EOFError, KeyboardInterrupt):
        print("aborted", file=sys.stderr)
        return 1
    if not isinstance(secret, str) or not secret:
        print("no key provided", file=sys.stderr)
        return 1
    try:
        store.store(credential_store.TARGET_NAME, secret)
    except credential_store.CredentialStoreError as exc:
        # ``exc.code`` is a bounded catalogue key; the secret is never printed.
        print(exc.code, file=sys.stderr)
        return 1
    print("credential stored")
    return 0


def _delete(store: credential_store.CredentialStore) -> int:
    if not store.available():
        print("unavailable", file=sys.stderr)
        return 1
    try:
        store.delete(credential_store.TARGET_NAME)
    except credential_store.CredentialStoreError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    print("credential deleted")
    return 0


def _readiness(base_dir: str, store: credential_store.CredentialStore) -> int:
    print(json.dumps(_readiness_result(base_dir, store), ensure_ascii=True, sort_keys=True))
    return 0


def run(
    subcommand: str,
    *,
    store: Optional[credential_store.CredentialStore] = None,
    base_dir: Optional[str] = None,
    prompt: Optional[Callable] = None,
) -> int:
    """Run one subcommand with injected dependencies (testable)."""
    store = store if store is not None else credential_store.make_credential_store()
    base_dir = base_dir if base_dir is not None else twin_store.app_data_dir()
    prompt = prompt if prompt is not None else getpass.getpass
    if subcommand == _ENROLL:
        return _enroll(store, prompt)
    if subcommand == _DELETE:
        return _delete(store)
    if subcommand == _READINESS:
        return _readiness(base_dir, store)
    print(_USAGE, file=sys.stderr)
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = [
        a for a in (sys.argv[1:] if argv is None else argv) if a != PROVIDER_SENTINEL
    ]
    subcommand = args[0] if args else ""
    return run(subcommand)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PROVIDER_SENTINEL",
    "run",
    "main",
]
