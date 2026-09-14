"""Backend-owned native credential host (P4.2a, Windows).

A short-lived, single-purpose helper process that owns the native dark entry
sheet (a Win32 modal replacing CredUI) and the Credential Manager write/delete
for one Add / Replace / Remove credential operation. It is launched only by the
desktop application —
through the same entry executable's ``--credential`` sentinel in a frozen
build, or ``python -m hrca.credential_host`` from source — runs in the
interactive user session it inherits from that application, parents the entry
sheet to the application's top-level window when a parent handle is supplied,
and emits exactly one bounded, secret-free contract envelope on stdout before
exiting.

The API key never crosses a command line, environment variable, NDJSON field,
file, log, Qt signal, IPC payload or serialized result: it travels from the
native entry sheet straight into Windows Credential Manager inside this process,
and the process then exits.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional, Sequence

from . import contract, credential_store

_OP_ENROLL = "enroll"
_OP_DELETE = "delete"


def run(
    op: str,
    hwnd_parent: Optional[int] = None,
    *,
    profile_id: Optional[str] = None,
    display_name: Optional[str] = None,
    theme: str = "dark",
    store: Optional[credential_store.CredentialStore] = None,
    sheet: Any = None,
) -> Dict[str, Any]:
    """Run one host operation and return a redacted result (never the key).

    ``op`` is ``enroll`` (store/replace) or ``delete``; ``hwnd_parent`` is the
    optional native parent window handle; ``profile_id`` is the optional opaque
    profile id — when present this is *replace* mode (the id names the target
    and the name is read-only), when absent this is *add* mode (the host
    generates a fresh id and collects a display name); ``display_name`` is the
    existing name shown read-only in replace mode; ``theme`` is the non-secret
    ``"light"``/``"dark"`` scheme the native sheet uses; ``store``/``sheet`` are
    injectable for tests. The returned mapping is the bounded redacted result —
    state, presence, an optional bounded failure reason, and (for a successful
    add) the generated ``profile_id`` and ``display_name`` — never the secret,
    its length, a prefix, or any representation of it.
    """
    store = store if store is not None else credential_store.make_credential_store()
    if not store.available():
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_UNAVAILABLE, False
        )

    if op == _OP_DELETE:
        target = (
            credential_store.profile_target(profile_id)
            if profile_id is not None
            else credential_store.TARGET_NAME
        )
        try:
            store.delete(target)
        except credential_store.CredentialStoreError as exc:
            return credential_store.redacted_credential_result(
                credential_store.CREDENTIAL_STATE_FAILED,
                store.has(target),
                reason=exc.code,
            )
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_REMOVED, False
        )

    is_replace = profile_id is not None
    target = credential_store.profile_target(profile_id) if is_replace else None
    presence = store.has(target) if is_replace else False

    sheet_fn = sheet if sheet is not None else credential_store.native_entry_sheet()
    if sheet_fn is None:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_UNAVAILABLE, presence
        )
    try:
        outcome = sheet_fn(
            display_name if is_replace else None,
            hwnd_parent=hwnd_parent,
            theme=theme,
        )
    except (EOFError, KeyboardInterrupt):
        outcome = None
    except credential_store.CredentialStoreError as exc:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_FAILED,
            presence,
            reason=exc.code,
        )
    if outcome is None:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_CANCELLED, presence
        )

    name, secret = outcome
    if not is_replace:
        profile_id = contract.new_profile_id()
        target = credential_store.profile_target(profile_id)
    try:
        store.store(target, secret)
    except credential_store.CredentialStoreError as exc:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_FAILED,
            store.has(target),
            reason=exc.code,
        )
    finally:
        # The secret is dropped from the local frame as soon as the store call
        # returns, success or failure.
        secret = None

    result = credential_store.redacted_credential_result(
        credential_store.CREDENTIAL_STATE_STORED, store.has(target)
    )
    if not is_replace:
        result["profile_id"] = profile_id
        result["display_name"] = name
    return result


def _correlation_id(request: Any) -> Optional[str]:
    if isinstance(request, dict):
        cid = request.get("correlation_id")
        if (
            isinstance(cid, str)
            and cid
            and len(cid) <= contract.CORRELATION_ID_MAX_CHARS
        ):
            return cid
    return None


def handle_request(
    request: Any,
    store: Optional[credential_store.CredentialStore] = None,
    sheet: Any = None,
) -> Dict[str, Any]:
    """Validate and dispatch one credential request, returning an envelope.

    Mirrors the boundary's discipline: only the two credential actions are
    accepted, the contract version must match, and the result is a bounded,
    secret-free success or error envelope. ``sheet`` is the injectable entry
    sheet (defaults to the native dark sheet on Windows).
    """
    if not isinstance(request, dict):
        return contract.build_error(None, "invalid_request")
    if request.get("contract_version") != contract.CONTRACT_VERSION:
        return contract.build_error(_correlation_id(request), "unknown_contract_version")

    action = request.get("action")
    if action not in contract.CREDENTIAL_ACTIONS:
        return contract.build_error(_correlation_id(request), "action_not_allowed")

    correlation_id = _correlation_id(request)
    hwnd = request.get("hwnd")
    if not isinstance(hwnd, int) or hwnd <= 0:
        hwnd = None

    # The optional profile id names the opaque profile whose credential target
    # the host derives itself (never a free-form target string). A malformed id
    # is a bounded invalid_request; the secret never appears here.
    profile_id = request.get("profile_id")
    if profile_id is not None and not contract.is_valid_profile_id(profile_id):
        return contract.build_error(correlation_id, "invalid_request")

    # In replace mode the request carries the existing display name (non-secret)
    # for the read-only name field; it is validated as plain text only.
    display_name = request.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        return contract.build_error(correlation_id, "invalid_request")

    # The non-secret theme ("light"/"dark") the native sheet uses to match the
    # desktop; an unknown token is treated as absent (the sheet defaults dark).
    theme = request.get("theme")
    if theme not in ("light", "dark"):
        theme = "dark"

    if action == contract.ACTION_MANAGE_CREDENTIAL:
        result = run(
            _OP_ENROLL,
            hwnd,
            profile_id=profile_id,
            display_name=display_name,
            theme=theme,
            store=store,
            sheet=sheet,
        )
    else:  # ACTION_REMOVE_CREDENTIAL
        result = run(_OP_DELETE, hwnd, profile_id=profile_id, store=store, sheet=sheet)
    return contract.build_success(correlation_id, result)


def _configure_stdio(stream) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", newline="\n")


def _emit(stdout, payload: Dict[str, Any]) -> None:
    text = contract.dumps(payload)
    if len(text.encode("utf-8")) > contract.MAX_MESSAGE_BYTES:
        text = contract.dumps(contract.build_error(None, "message_too_large"))
    stdout.write(text + "\n")
    stdout.flush()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Read one credential request from stdin, write one envelope, exit.

    ``argv`` is accepted for parity with the other entry points; the host never
    reads the key (or the operation) from the command line — the operation and
    the optional parent handle are named inside the stdin request, and the key
    comes exclusively from the native entry sheet.
    """
    _configure_stdio(sys.stdin)
    _configure_stdio(sys.stdout)
    line = sys.stdin.readline()
    if not line:
        return 0
    try:
        request = contract.loads(line)
        envelope = handle_request(request)
    except (ValueError, UnicodeDecodeError):
        envelope = contract.build_error(None, "malformed_request")
    except contract.ContractError as exc:
        envelope = contract.build_error(None, exc.code)
    except Exception:
        envelope = contract.build_error(None, "internal_error")
    _emit(sys.stdout, envelope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["run", "handle_request", "main"]
