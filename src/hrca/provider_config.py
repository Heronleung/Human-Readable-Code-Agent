"""Non-secret DeepSeek configuration (P4.2a).

Persists a small, versioned, validated configuration value in the per-user
application-data directory — never in a repository. The value is non-secret: it
may contain only a schema version, the fixed provider id, one allowlisted model
id and an optional label. There is no configurable endpoint, arbitrary model
input, header, proxy, organization secret or credential field.

The module is Qt-free and stdlib-only; it imports :mod:`hrca.deepseek` for the
fixed provider/model allowlist and mirrors the atomic-write discipline of
:mod:`hrca.twin_store`.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Optional, Tuple

from . import deepseek

CONFIG_SCHEMA_VERSION = "1.0.0"
CONFIG_FILENAME = "provider-config.json"
_TMP_PREFIX = ".provider-config-"
_TMP_SUFFIX = ".tmp"

# The only top-level keys the config may contain. A credential, endpoint or
# header field is not among them, so a malformed or hostile config can never
# introduce one.
_ALLOWED_KEYS = frozenset({"schema_version", "provider_id", "model", "label"})
_MAX_LABEL_CHARS = 200


def config_path(base_dir: str) -> str:
    return os.path.join(base_dir, CONFIG_FILENAME)


def default_config() -> Dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "provider_id": deepseek.PROVIDER_ID,
        "model": deepseek.DEFAULT_MODEL,
        "label": None,
    }


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
    label = value.get("label")
    if label is not None and (
        not isinstance(label, str) or len(label) > _MAX_LABEL_CHARS
    ):
        return "invalid label"
    return None


def load(base_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fail-closed load of the provider config.

    Returns ``(config, error)`` where exactly one is ``None`` (an absent config
    yields ``(None, None)``). Any read, parse or validation failure returns
    ``(None, reason)`` and never touches the on-disk file.
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
    reason = validate_config(raw)
    if reason is not None:
        return None, reason
    return raw, None


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
    "validate_config",
    "load",
    "save",
]
