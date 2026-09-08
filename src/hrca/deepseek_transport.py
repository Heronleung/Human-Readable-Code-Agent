"""DeepSeek advisory transport (P4.2b).

The single, backend-owned module that performs the one permitted hosted
DeepSeek request. It implements the P2.5 :class:`hrca.provider.Provider`
interface so the advisory flow stays provider-neutral, and it is the *only*
place that reads the credential, opens a socket, or sends source-derived
content. The desktop client and the frozen scan/serve/readiness paths never
import this module (enforced by :mod:`tests.test_architecture` and by lazy
import at the boundary).

Hard controls, all code-owned (never user-configurable):

* **fixed origin** — :data:`hrca.deepseek.API_ORIGIN` plus the fixed chat
  completions path; no endpoint, proxy or header is accepted from anywhere;
* **allowlisted model** — :data:`hrca.deepseek.DEFAULT_MODEL` only;
* **documented auth** — ``Authorization: Bearer <key>``, where the key is read
  through the injected credential getter and dropped immediately after use;
* **one attempt** — exactly one ``http.client`` POST, with a bounded timeout,
  and **no retry, no background, no refresh and no provider fallback**;
* **bounded request** — request bytes and context-item count are checked
  before any network access;
* **bounded output** — ``max_tokens`` caps the provider output, and the
  returned payload is validated against the versioned advisory schema.

Failures are raised as a bounded :class:`TransportError` whose ``code`` is one
of the normalized advisory states, so the boundary can map every transport
outcome to a bounded result without leaking a credential, an endpoint detail,
a raw response, or an underlying exception.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
from typing import Any, Callable, Dict, Optional, Tuple

from . import advisory, deepseek, provider

# Fixed chat-completions path appended to the fixed API origin.
CHAT_COMPLETIONS_PATH = "/chat/completions"

# The single fixed endpoint URL. Never user-configurable.
ENDPOINT_URL = deepseek.API_ORIGIN + CHAT_COMPLETIONS_PATH

# Bounded transport error code -> fixed message catalogue. A TransportError
# retains only its code; the message is drawn from this table so an underlying
# HTTP error, credential or response text can never be retained or serialized.
_SAFE_MESSAGES = {
    "credential_missing": "the DeepSeek credential is missing or rejected",
    "network_denied": "the provider request could not reach the network",
    "timeout": "the provider request timed out",
    "rate_limited": "the provider rate-limited the request",
    "quota_exceeded": "the provider quota or balance is exhausted",
    "invalid_output": "the provider response failed schema validation",
    "provider_unavailable": "the provider is unavailable",
    "context_rejected": "the provider rejected the request context",
    "over_limit": "the request exceeds a bounded limit",
    "provider_failure": "the provider failed",
}

_TRANSPORT_CODES = frozenset(_SAFE_MESSAGES)

# HTTP status -> bounded code, for the well-known DeepSeek statuses. Anything
# outside this table is mapped by class (4xx -> context_rejected, 5xx ->
# provider_unavailable, else provider_failure).
_HTTP_TO_CODE = {
    400: "context_rejected",
    401: "credential_missing",
    402: "quota_exceeded",
    403: "credential_missing",
    404: "context_rejected",
    408: "timeout",
    413: "over_limit",
    422: "context_rejected",
    429: "rate_limited",
}


class TransportError(Exception):
    """Bounded, sanitized transport failure.

    Carries only a bounded ``code``; ``message`` is always drawn from the fixed
    catalogue. No credential, endpoint detail, raw response, or underlying
    exception object is ever retained.
    """

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _SAFE_MESSAGES:
            raise ValueError("invalid transport error code")
        self.code = code
        self.message = _SAFE_MESSAGES[code]
        super().__init__(code, self.message)

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


def _code_for_status(status: int) -> str:
    """Map an HTTP status to a bounded transport code."""
    if status in _HTTP_TO_CODE:
        return _HTTP_TO_CODE[status]
    if 400 <= status < 500:
        return "context_rejected"
    if 500 <= status < 600:
        return "provider_unavailable"
    return "provider_failure"


def _default_http_post(
    url: str, body: bytes, headers: Dict[str, str], timeout: float
) -> Tuple[int, bytes]:
    """Perform one POST to ``url``; returns ``(status, body_bytes)``.

    Raises ``OSError``/``socket.timeout``/``http.client.HTTPException`` on
    transport failure; the caller maps those to bounded codes.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or 443
    connection = http.client.HTTPSConnection(host, port, timeout=timeout)
    try:
        connection.request("POST", parts.path or "/", body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _extract_content(body: bytes) -> Optional[str]:
    """Return the first assistant message ``content`` from a chat response."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    return content


def _parse_json_content(content: str) -> Optional[Dict[str, Any]]:
    """Parse a provider JSON object, tolerating a markdown code fence."""
    text = content.strip()
    if text.startswith("```"):
        # Strip a leading and trailing ``` (with optional ``json`` tag).
        text = text.strip("`")
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        text = text.strip()
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class DeepSeekProvider(provider.Provider):
    """The bounded DeepSeek advisory transport (P2.5 ``Provider``).

    ``credential_getter`` returns the opaque API key (or ``None`` when absent)
    and is the only path the key takes; it is dropped from the local frame as
    soon as the request is built. ``http_post`` is injectable for deterministic
    tests (the default performs a real, single HTTPS POST).
    """

    def __init__(
        self,
        *,
        model: str = deepseek.DEFAULT_MODEL,
        credential_getter: Optional[Callable[[], Optional[str]]] = None,
        http_post: Optional[Callable[..., Tuple[int, bytes]]] = None,
        timeout: float = advisory.TIMEOUT_SECONDS,
        max_output_tokens: int = advisory.MAX_OUTPUT_TOKENS,
        max_request_bytes: int = advisory.MAX_REQUEST_BYTES,
        max_context_items: int = advisory.MAX_CONTEXT_ITEMS,
    ) -> None:
        if not deepseek.is_allowed_model(model):
            raise ValueError("invalid model")
        self._model = model
        self._credential_getter = credential_getter or (lambda: None)
        self._http_post = http_post or _default_http_post
        self._timeout = timeout
        self._max_output_tokens = max_output_tokens
        self._max_request_bytes = max_request_bytes
        self._max_context_items = max_context_items

    def capabilities(self) -> provider.ProviderCapabilities:
        return provider.ProviderCapabilities(
            structured_output=True,
            streaming=False,
            cancellation=False,
            tool_calling=False,
            context_limit=None,
            readiness_available=False,
        )

    def readiness(self) -> provider.ProviderReadiness:
        # The transport never performs a live readiness check; the redacted local
        # readiness is reported by the boundary through ``hrca.deepseek``.
        return provider.ProviderReadiness(
            ready=False, detail="offline transport; readiness is reported by the boundary"
        )

    def generate(self, request: provider.ProviderRequest) -> provider.ProviderResult:
        if not isinstance(request, provider.ProviderRequest):
            raise TransportError("provider_failure")

        # Pre-network bounds. Deny before any socket is opened.
        if len(request.context) > self._max_context_items:
            raise TransportError("over_limit")
        body_dict: Dict[str, Any] = self._build_body(request)
        payload_bytes = json.dumps(
            body_dict, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")
        if len(payload_bytes) > self._max_request_bytes:
            raise TransportError("over_limit")

        credential = self._credential_getter()
        if not credential:
            raise TransportError("credential_missing")
        headers = {
            "Content-Type": "application/json",
            deepseek.AUTH_HEADER: f"{deepseek.AUTH_PREFIX} {credential}",
        }
        # Drop the key from the local frame before the network call so it is not
        # reachable from a traceback or a retained frame.
        credential = None

        try:
            status, body = self._http_post(
                ENDPOINT_URL, payload_bytes, headers, self._timeout
            )
        except (TimeoutError, socket.timeout):
            raise TransportError("timeout")
        except (OSError, http.client.HTTPException):
            raise TransportError("network_denied")

        if status != 200:
            raise TransportError(_code_for_status(status))

        content = _extract_content(body)
        if content is None:
            raise TransportError("invalid_output")
        payload = _parse_json_content(content)
        if payload is None:
            raise TransportError("invalid_output")
        reason = advisory.validate_advisory_payload(payload)
        if reason is not None:
            raise TransportError("invalid_output")

        normalized = advisory.normalize_payload(payload)
        # ``content`` is deliberately the *sanitized* advisory object, never the
        # raw model output, so no source-derived text or hidden reasoning can be
        # retained or logged downstream.
        content = advisory.dumps(normalized)
        usage = None
        try:
            data = json.loads(body.decode("utf-8"))
            usage_data = data.get("usage")
            if isinstance(usage_data, dict):
                usage = provider.ProviderUsage(
                    prompt_tokens=usage_data.get("prompt_tokens"),
                    completion_tokens=usage_data.get("completion_tokens"),
                    total_tokens=usage_data.get("total_tokens"),
                )
        except (ValueError, UnicodeDecodeError, provider.ProviderError):
            usage = None
        return provider.ProviderResult(
            task_id=request.task_id,
            content=content,
            provider=deepseek.PROVIDER_ID,
            model=self._model,
            structured_payload=normalized,
            usage=usage,
        )

    def _build_body(self, request: provider.ProviderRequest) -> Dict[str, Any]:
        """Build the OpenAI-compatible chat-completions body."""
        user_text = "\n\n".join(request.context)
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.task},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": self._max_output_tokens,
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        }


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "ENDPOINT_URL",
    "TransportError",
    "DeepSeekProvider",
]
