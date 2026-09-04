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

# Closed allowlist of verified model identifiers. Exactly one model is enabled
# for P4.2a; adding another is a separate, gated change.
ALLOWED_MODELS = frozenset({"deepseek-v4-flash"})
DEFAULT_MODEL = "deepseek-v4-flash"


def is_allowed_model(model: Any) -> bool:
    """Return True when ``model`` is one of the allowlisted identifiers."""
    return isinstance(model, str) and model in ALLOWED_MODELS


# -- redacted local readiness states ---------------------------------------

READY_STATE_CONFIGURED = "configured"
READY_STATE_MISSING_CREDENTIAL = "missing_credential"
READY_STATE_UNAVAILABLE = "unavailable"
READY_STATE_INVALID_CONFIG = "invalid_config"
READY_STATES = frozenset(
    {
        READY_STATE_CONFIGURED,
        READY_STATE_MISSING_CREDENTIAL,
        READY_STATE_UNAVAILABLE,
        READY_STATE_INVALID_CONFIG,
    }
)


def readiness_state(
    *,
    config_error: Optional[str],
    credential_present: bool,
    store_available: bool,
) -> str:
    """Return the deterministic redacted readiness state.

    Precedence:

    * ``unavailable`` when the platform has no credential store;
    * ``invalid_config`` when the non-secret config cannot be validated;
    * ``missing_credential`` when the config is valid but no credential exists;
    * ``configured`` otherwise.

    ``configured`` means local configuration plus credential presence only —
    never authenticated, online, authorized, billed or request-capable.
    """
    if not store_available:
        return READY_STATE_UNAVAILABLE
    if config_error is not None:
        return READY_STATE_INVALID_CONFIG
    if not credential_present:
        return READY_STATE_MISSING_CREDENTIAL
    return READY_STATE_CONFIGURED


def redacted_readiness(
    *,
    config: Optional[Dict[str, Any]],
    config_error: Optional[str],
    credential_present: bool,
    store_available: bool,
) -> Dict[str, Any]:
    """Assemble the redacted readiness result from non-secret facts only.

    The result never contains a credential, an endpoint detail, a header value
    or a ``config_error`` reason; it carries only a bounded state, the fixed
    provider id, the allowlisted model (or ``None`` when the config is invalid)
    and explicit ``authenticated``/``online``/``executable`` flags that are
    always false in P4.2a.
    """
    state = readiness_state(
        config_error=config_error,
        credential_present=credential_present,
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
        "credential_present": bool(credential_present),
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
    "is_allowed_model",
    "READY_STATE_CONFIGURED",
    "READY_STATE_MISSING_CREDENTIAL",
    "READY_STATE_UNAVAILABLE",
    "READY_STATE_INVALID_CONFIG",
    "READY_STATES",
    "readiness_state",
    "redacted_readiness",
]
