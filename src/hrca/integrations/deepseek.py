"""Fixed DeepSeek adapter identity and redacted local readiness (P4.2a).

Code-controlled allowlist data only. The official HTTPS origin, the
authentication scheme and the single verified model identifier are constants
here — never accepted from a CLI flag, a UI field, a configuration value or an
environment variable, so there is no configurable endpoint and no arbitrary
model input.

This module performs no network access, no credential read, no model inference
and no request. It exists so a future P4.2b adapter can resolve these constants
at the provider-side seam, while P4.2a readiness reports only this identity
plus a redacted state derived from non-secret configuration and credential
*presence* (never the credential value).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import credential_store

# Fixed provider identity. The desktop/NDJSON surface may display this id, but
# must never receive an endpoint, header, model free-text or credential.
PROVIDER_ID = "deepseek"

# Official HTTPS API origin, verified from the DeepSeek API documentation
# (https://api-docs.deepseek.com) on 2026-09-04. Fixed; never user-configurable.
API_ORIGIN = "https://api.deepseek.com"

# Authentication scheme: an opaque bearer API key sent in the Authorization
# header. Only the scheme and the header names are held here; the key itself is
# never present in this module.
AUTH_SCHEME = "bearer"
AUTH_HEADER = "Authorization"
AUTH_PREFIX = "Bearer"

# Canonical API model id, verified 2026-09-12 against the official Models &
# Pricing page (https://api-docs.deepseek.com/quick_start/pricing) and the
# DeepSeek-V4.1-Flash release note (https://api-docs.deepseek.com/news/news260910/):
# "Use deepseek-flash as the model name." The effective model serving requests is
# DeepSeek-V4.1-Flash.
CANONICAL_MODEL_ID = "deepseek-flash"
DEFAULT_MODEL = CANONICAL_MODEL_ID

# Closed allowlist of the single enabled model. The retired compatibility aliases
# below are *not* allowlisted: they route to V4.1-Flash only temporarily and must
# be migrated, never dispatched.
ALLOWED_MODELS = frozenset({CANONICAL_MODEL_ID})

# Retired compatibility aliases. Official language (pricing footnote + release
# note): "the legacy names deepseek-v4-flash and deepseek-v4-flash-vision-exp are
# still accepted, but the corresponding models have been retired, their requests
# are served by the DeepSeek-V4.1-Flash model and billed at the Flash price."
COMPATIBILITY_ALIASES = frozenset(
    {"deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
)


def is_allowed_model(model: Any) -> bool:
    """Return True when ``model`` is the allowlisted canonical id."""
    return isinstance(model, str) and model in ALLOWED_MODELS


def is_compatibility_alias(model: Any) -> bool:
    """Return True when ``model`` is a retired alias that routes to canonical."""
    return isinstance(model, str) and model in COMPATIBILITY_ALIASES


def migrate_model(model: Any) -> Optional[str]:
    """Return the canonical model id for ``model``, or ``None`` when unknown.

    Maps the canonical id and every retired compatibility alias to
    :data:`CANONICAL_MODEL_ID`. Idempotent: migrating the canonical id returns it
    unchanged. Any other value returns ``None`` (the caller keeps or rejects the
    original value — no guess is made).
    """
    if model == CANONICAL_MODEL_ID or is_compatibility_alias(model):
        return CANONICAL_MODEL_ID
    return None


# -- redacted local readiness states ---------------------------------------

READY_STATE_CONFIGURED = "configured"
READY_STATE_MISSING_CREDENTIAL = "missing_credential"
READY_STATE_NO_PROFILE = "no_profile"
READY_STATE_CREDENTIAL_UNRETRIEVABLE = "credential_unretrievable"
READY_STATE_UNAVAILABLE = "unavailable"
READY_STATE_INVALID_CONFIG = "invalid_config"
READY_STATES = frozenset(
    {
        READY_STATE_CONFIGURED,
        READY_STATE_MISSING_CREDENTIAL,
        READY_STATE_NO_PROFILE,
        READY_STATE_CREDENTIAL_UNRETRIEVABLE,
        READY_STATE_UNAVAILABLE,
        READY_STATE_INVALID_CONFIG,
    }
)


def readiness_state(
    *,
    config_error: Optional[str],
    credential_state: str,
    store_available: bool,
) -> str:
    """Return the deterministic redacted readiness state.

    ``credential_state`` is one of the bounded
    :data:`~hrca.credential_store.CREDENTIAL_RETRIEVAL_STATES` classifications
    produced by :func:`hrca.credential_store.classify_retrieval` — never the
    secret. Precedence:

    * ``unavailable`` when the platform has no credential store;
    * ``invalid_config`` when the non-secret config cannot be validated;
    * ``no_profile`` when the config is valid but no active profile (and no
      legacy credential) applies;
    * ``missing_credential`` when a target applies but no non-empty secret can
      be read (metadata-only / orphaned / empty);
    * ``credential_unretrievable`` when the read failed with a bounded store
      error (inaccessible / corrupted);
    * ``configured`` only when a non-empty secret was actually read.

    ``configured`` means local configuration plus an actually-retrievable
    credential only — never authenticated, online, authorized, billed or
    request-capable.
    """
    if not store_available:
        return READY_STATE_UNAVAILABLE
    if config_error is not None:
        return READY_STATE_INVALID_CONFIG
    if credential_state == credential_store.CREDENTIAL_RETRIEVABLE:
        return READY_STATE_CONFIGURED
    if credential_state == credential_store.CREDENTIAL_UNRETRIEVABLE:
        return READY_STATE_CREDENTIAL_UNRETRIEVABLE
    if credential_state == credential_store.CREDENTIAL_NO_TARGET:
        return READY_STATE_NO_PROFILE
    return READY_STATE_MISSING_CREDENTIAL


def redacted_readiness(
    *,
    config: Optional[Dict[str, Any]],
    config_error: Optional[str],
    credential_state: str,
    store_available: bool,
) -> Dict[str, Any]:
    """Assemble the redacted readiness result from non-secret facts only.

    The result never contains a credential, an endpoint detail, a header value
    or a ``config_error`` reason; it carries only a bounded state, the fixed
    provider id, the allowlisted model (or ``None`` when the config is invalid),
    a redacted ``credential_present`` flag (true only when a non-empty secret
    was actually read) and explicit ``authenticated``/``online``/``executable``
    flags that are always false in P4.2a.
    """
    state = readiness_state(
        config_error=config_error,
        credential_state=credential_state,
        store_available=store_available,
    )
    model = None
    if config_error is None and isinstance(config, dict):
        candidate = config.get("model")
        if is_allowed_model(candidate):
            model = candidate
    return {
        "state": state,
        "provider_id": PROVIDER_ID,
        "model": model,
        "credential_present": credential_state == credential_store.CREDENTIAL_RETRIEVABLE,
        "authenticated": False,
        "online": False,
        "executable": False,
    }


__all__ = [
    "PROVIDER_ID",
    "API_ORIGIN",
    "AUTH_SCHEME",
    "AUTH_HEADER",
    "AUTH_PREFIX",
    "ALLOWED_MODELS",
    "DEFAULT_MODEL",
    "CANONICAL_MODEL_ID",
    "COMPATIBILITY_ALIASES",
    "is_allowed_model",
    "is_compatibility_alias",
    "migrate_model",
    "READY_STATE_CONFIGURED",
    "READY_STATE_MISSING_CREDENTIAL",
    "READY_STATE_NO_PROFILE",
    "READY_STATE_CREDENTIAL_UNRETRIEVABLE",
    "READY_STATE_UNAVAILABLE",
    "READY_STATE_INVALID_CONFIG",
    "READY_STATES",
    "readiness_state",
    "redacted_readiness",
]
