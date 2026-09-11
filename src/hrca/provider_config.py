"""Non-secret DeepSeek credential-profile configuration (P4.2a).

Persists a small, versioned, validated configuration value in the per-user
application-data directory — never in a repository. The value is non-secret: it
may contain only a schema version, the fixed provider id, one allowlisted model
id, a bounded list of named credential *profiles* and one active profile id.

A profile is metadata only: an opaque immutable profile id, a fixed provider id
and a user display name. It never contains key material, key length, prefix,
suffix, hash or any reversible derivative — the secret lives in Windows
Credential Manager under a target derived from the profile id (see
:mod:`hrca.credential_store`), never in this file.

The module is Qt-free and stdlib-only; it imports :mod:`hrca.deepseek` for the
fixed provider/model allowlist and :mod:`hrca.contract` for the opaque profile
id format, and mirrors the atomic-write discipline of :mod:`hrca.twin_store`.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Optional, Tuple

from . import contract, deepseek

CONFIG_SCHEMA_VERSION = "2.0.0"
_LEGACY_SCHEMA_VERSION = "1.0.0"
CONFIG_FILENAME = "provider-config.json"
_TMP_PREFIX = ".provider-config-"
_TMP_SUFFIX = ".tmp"

# The only top-level keys the config may contain. A credential, endpoint or
# header field is not among them, so a malformed or hostile config can never
# introduce one.
_ALLOWED_KEYS = frozenset(
    {"schema_version", "provider_id", "model", "profiles", "active_profile_id"}
)

# A profile entry carries exactly these keys — nothing more (no secret, no
# credential state, no target name).
_PROFILE_KEYS = frozenset({"profile_id", "provider_id", "display_name"})

_MAX_PROFILE_NAME_CHARS = 64
_MAX_PROFILES = 64


def config_path(base_dir: str) -> str:
    return os.path.join(base_dir, CONFIG_FILENAME)


def default_config() -> Dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "provider_id": deepseek.PROVIDER_ID,
        "model": deepseek.DEFAULT_MODEL,
        "profiles": [],
        "active_profile_id": None,
    }


# -- profile helpers -------------------------------------------------------


def _valid_display_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_PROFILE_NAME_CHARS
    )


def _profile_entry(profile_id: str, display_name: str) -> Dict[str, Any]:
    return {
        "profile_id": profile_id,
        "provider_id": deepseek.PROVIDER_ID,
        "display_name": display_name.strip(),
    }


def add_profile(
    config: Dict[str, Any],
    profile_id: str,
    display_name: str,
    provider_id: str = deepseek.PROVIDER_ID,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(config_with_profile, error)`` — append one profile entry.

    Exactly one of the two values is ``None``. A non-canonical profile id, an
    invalid display name, an unsupported provider, a duplicate profile id or
    display name, or an at-capacity list is a bounded error; the input config
    is never mutated.
    """
    if not contract.is_valid_profile_id(profile_id):
        return None, "invalid profile id"
    if provider_id != deepseek.PROVIDER_ID:
        return None, "unsupported provider_id"
    if not _valid_display_name(display_name):
        return None, "invalid display name"
    existing_ids = {p["profile_id"] for p in config.get("profiles", [])}
    existing_names = {p["display_name"].lower() for p in config.get("profiles", [])}
    if profile_id in existing_ids:
        return None, "duplicate profile id"
    if display_name.strip().lower() in existing_names:
        return None, "duplicate display name"
    if len(config.get("profiles", [])) >= _MAX_PROFILES:
        return None, "too many profiles"
    out = dict(config)
    out["profiles"] = [dict(p) for p in config.get("profiles", [])]
    out["profiles"].append(_profile_entry(profile_id, display_name))
    return out, None


def rename_profile(
    config: Dict[str, Any], profile_id: str, display_name: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(config_renamed, error)`` — change a profile's display name.

    Rename touches metadata only: the opaque profile id (and therefore the
    credential target) is unchanged, so the secret is never read, copied or
    rewritten.
    """
    if not contract.is_valid_profile_id(profile_id):
        return None, "invalid profile id"
    if not _valid_display_name(display_name):
        return None, "invalid display name"
    profiles = config.get("profiles", [])
    names = {
        p["display_name"].lower() for p in profiles if p["profile_id"] != profile_id
    }
    if display_name.strip().lower() in names:
        return None, "duplicate display name"
    out = dict(config)
    out["profiles"] = []
    found = False
    for p in profiles:
        if p["profile_id"] == profile_id:
            out["profiles"].append(_profile_entry(profile_id, display_name))
            found = True
        else:
            out["profiles"].append(dict(p))
    if not found:
        return None, "profile not found"
    return out, None


def remove_profile(
    config: Dict[str, Any], profile_id: str, active_profile_id: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(config_without_profile, error)`` — remove one profile.

    ``active_profile_id`` is the caller-chosen fallback active id (an existing
    remaining profile id) or ``None`` to leave no active profile. It is
    validated here so the persisted config is always self-consistent; the
    boundary never silently chooses a fallback.
    """
    if not contract.is_valid_profile_id(profile_id):
        return None, "invalid profile id"
    profiles = config.get("profiles", [])
    remaining = [dict(p) for p in profiles if p["profile_id"] != profile_id]
    if len(remaining) == len(profiles):
        return None, "profile not found"
    if active_profile_id is not None:
        if not contract.is_valid_profile_id(active_profile_id):
            return None, "invalid active profile id"
        if active_profile_id not in {p["profile_id"] for p in remaining}:
            return None, "invalid active profile id"
    out = dict(config)
    out["profiles"] = remaining
    out["active_profile_id"] = active_profile_id
    return out, None


def set_active_profile(
    config: Dict[str, Any], profile_id: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(config_active, error)`` — select exactly one active profile.

    ``profile_id`` is an existing profile id, or ``None`` to clear the active
    selection. Setting the active profile is a pure non-secret metadata write
    and makes no provider or network call.
    """
    if profile_id is not None:
        if not contract.is_valid_profile_id(profile_id):
            return None, "invalid active profile id"
        if profile_id not in {p["profile_id"] for p in config.get("profiles", [])}:
            return None, "profile not found"
    out = dict(config)
    out["active_profile_id"] = profile_id
    return out, None


# -- validation ------------------------------------------------------------


def validate_config(value: Any) -> Optional[str]:
    """Return a bounded reason when ``value`` is not a valid config, else None."""
    if not isinstance(value, dict):
        return "config is not a mapping"
    unknown = set(value) - _ALLOWED_KEYS
    if unknown:
        return "config has unknown fields"
    if value.get("schema_version") != CONFIG_SCHEMA_VERSION:
        return "unsupported schema_version"
    if value.get("provider_id") != deepseek.PROVIDER_ID:
        return "unsupported provider_id"
    if not deepseek.is_allowed_model(value.get("model")):
        return "unsupported model"

    profiles = value.get("profiles")
    if not isinstance(profiles, list):
        return "profiles is not a list"
    if len(profiles) > _MAX_PROFILES:
        return "too many profiles"
    seen_ids = set()
    seen_names = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != _PROFILE_KEYS:
            return "invalid profile entry"
        if not contract.is_valid_profile_id(profile.get("profile_id")):
            return "invalid profile id"
        if profile.get("provider_id") != deepseek.PROVIDER_ID:
            return "unsupported profile provider_id"
        name = profile.get("display_name")
        if not _valid_display_name(name):
            return "invalid display name"
        if profile["profile_id"] in seen_ids:
            return "duplicate profile id"
        if name.strip().lower() in seen_names:
            return "duplicate display name"
        seen_ids.add(profile["profile_id"])
        seen_names.add(name.strip().lower())

    active = value.get("active_profile_id")
    if active is not None:
        if not contract.is_valid_profile_id(active):
            return "invalid active profile id"
        if active not in seen_ids:
            return "active profile id does not exist"
    return None


# -- migration -------------------------------------------------------------


def migrate_model(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a config with its model id migrated to canonical (idempotent).

    Only the non-secret ``model`` field changes; the profiles and the active
    profile id are preserved untouched, and the credential is never read or
    rewritten (it lives in the platform credential store, not in this file). An
    already-canonical or unknown model returns the input unchanged — the unknown
    case is left for :func:`validate_config` to reject.
    """
    model = raw.get("model")
    canonical = deepseek.migrate_model(model)
    if canonical is None or canonical == model:
        return raw
    out = dict(raw)
    out["model"] = canonical
    return out


def migrate_structure(raw: Any) -> Optional[Dict[str, Any]]:
    """Return a v2 config from a v1 or v2 raw config, or ``None`` when unknown.

    The legacy v1 schema (a single ``label`` field and no profiles) is folded
    into an empty v2 profile list with no active profile. The credential
    *migration* (moving the legacy secret into a named profile) is a separate
    boundary-side concern that reads the platform credential store; this
    function only migrates the non-secret structure.
    """
    if not isinstance(raw, dict):
        return None
    version = raw.get("schema_version")
    if version == CONFIG_SCHEMA_VERSION:
        return raw
    if version == _LEGACY_SCHEMA_VERSION:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "provider_id": raw.get("provider_id", deepseek.PROVIDER_ID),
            "model": raw.get("model", deepseek.DEFAULT_MODEL),
            "profiles": [],
            "active_profile_id": None,
        }
    return None


# -- persistence -----------------------------------------------------------


def load(base_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fail-closed load of the provider config.

    Returns ``(config, error)`` where exactly one is ``None`` (an absent config
    yields ``(None, None)``). A v1 config is structurally migrated to v2 before
    validation. Any read, parse or validation failure returns ``(None, reason)``
    and never touches the on-disk file.
    """
    path = config_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "could not read provider config"
    try:
        raw = json.loads(raw_text)
    except ValueError:
        return None, "provider config is not valid JSON"
    migrated = migrate_structure(raw)
    if migrated is None:
        return None, "unsupported schema_version"
    migrated = migrate_model(migrated)
    reason = validate_config(migrated)
    if reason is not None:
        return None, reason
    return migrated, None


def _ensure_dir(dirpath: str) -> Optional[str]:
    try:
        os.makedirs(dirpath, exist_ok=True)
    except OSError:
        return "could not create configuration directory"
    return None


def _atomic_write(dirpath: str, path: str, data: bytes, label: str) -> Optional[str]:
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=dirpath, prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX
        )
    except OSError:
        return f"could not create {label} temporary file"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return f"could not write {label}"
    return None


def save(base_dir: str, config: Dict[str, Any]) -> Optional[str]:
    """Atomically persist ``config``; returns a reason on failure or ``None``."""
    reason = validate_config(config)
    if reason is not None:
        return reason
    path = config_path(base_dir)
    err = _ensure_dir(base_dir)
    if err is not None:
        return err
    data = json.dumps(
        config, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _atomic_write(base_dir, path, data, "provider config")


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "CONFIG_FILENAME",
    "config_path",
    "default_config",
    "add_profile",
    "rename_profile",
    "remove_profile",
    "set_active_profile",
    "validate_config",
    "migrate_model",
    "migrate_structure",
    "load",
    "save",
]
