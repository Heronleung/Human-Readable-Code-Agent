"""Provider-neutral coding-agent protocol (P2.5).

Establishes the internal contract that lets a future coding-agent role use
either a hosted API adapter or a fine-tuned-model adapter without changing core
workflow semantics.

The provider is *not* the agent. Scanner facts, task planning, approval gates,
repository writes, commands, validation, Git actions, secrets, and reporting
all remain outside provider-generated text and outside this module's scope.

Only the shape of the contract is defined here: no network client, SDK, model
loading, streaming, cancellation, tool calling, environment configuration, or
credential access. Providers declare their actual capabilities instead of
claiming universal streaming, cancellation, structured output, or tool support.
"""

from __future__ import annotations

import abc
import copy
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = 1

# Bounded error code -> safe message catalogue. A ProviderError retains only
# its code; the message is always drawn from this fixed table so that
# caller-supplied text (and any secret it might carry) can never be retained or
# serialized. The valid codes are exactly the keys of this table.
_SAFE_MESSAGES = {
    "invalid_request": "the request is invalid",
    "unsupported_capability": "the requested capability is not supported",
    "unavailable": "the provider is unavailable",
    "provider_failure": "the provider failed",
}

_ERROR_CODES = frozenset(_SAFE_MESSAGES)

# Bounded readiness status strings.
_STATUS_READY = "ready"
_STATUS_UNAVAILABLE = "unavailable"

# Reserved task id that triggers :class:`FakeProvider`'s controlled failure.
FAILURE_TASK_ID = "__provider_failure__"


# -- bounded error representation ----------------------------------------


def _require_non_empty(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("invalid_request")
    return value


def _require_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ProviderError("invalid_request")
    return value


def _require_optional_int(value: Any, *, minimum: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ProviderError("invalid_request")
    return value


def _is_json_simple(value: Any) -> bool:
    """Return True when ``value`` is JSON-safe.

    Accepts ``None``, booleans, finite numbers, strings, and lists/dictionaries
    recursively built from those values. Dictionary keys must be strings.
    Tuples, sets, arbitrary objects, non-string keys, and non-finite floats are
    rejected.
    """
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_is_json_simple(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_simple(item)
            for key, item in value.items()
        )
    return False


class ProviderError(Exception):
    """Bounded, sanitized provider failure.

    Carries only a bounded ``code``; ``message`` is always drawn from the fixed
    safe-message catalogue. No caller-supplied text, credential, or
    provider-specific exception object is ever retained.
    """

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _SAFE_MESSAGES:
            raise ValueError("invalid provider error code")
        self.code = code
        self.message = _SAFE_MESSAGES[code]
        super().__init__(code, self.message)

    def to_dict(self) -> Dict[str, str]:
        """Return a canonical ``{code, message}`` mapping."""
        return {"code": self.code, "message": self.message}


def map_error(exc: BaseException) -> ProviderError:
    """Map any exception to a bounded, sanitized :class:`ProviderError`.

    The original exception object and its string representation are never
    propagated, so request content, credentials, and provider-specific
    exception detail cannot leak into the protocol result.
    """
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError("provider_failure")


# -- typed protocol data -------------------------------------------------


@dataclass(frozen=True)
class ProviderCapabilities:
    """A provider's declared capabilities.

    Every capability is opt-in: a field is ``True`` only when the provider
    actually supports it. There is no universal streaming, cancellation,
    structured-output, or tool support assumed here.
    """

    structured_output: bool
    streaming: bool
    cancellation: bool
    tool_calling: bool
    context_limit: Optional[int]
    readiness_available: bool

    def __post_init__(self) -> None:
        _require_bool(self.structured_output)
        _require_bool(self.streaming)
        _require_bool(self.cancellation)
        _require_bool(self.tool_calling)
        _require_optional_int(self.context_limit, minimum=1)
        _require_bool(self.readiness_available)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "structured_output": self.structured_output,
            "streaming": self.streaming,
            "cancellation": self.cancellation,
            "tool_calling": self.tool_calling,
            "context_limit": self.context_limit,
            "readiness_available": self.readiness_available,
        }


@dataclass(frozen=True)
class ProviderUsage:
    """Optional deterministic usage metadata for a single result."""

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        _require_optional_int(self.prompt_tokens, minimum=0)
        _require_optional_int(self.completion_tokens, minimum=0)
        _require_optional_int(self.total_tokens, minimum=0)

    def to_dict(self) -> Dict[str, Optional[int]]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class ProviderReadiness:
    """A deterministic readiness/health result."""

    ready: bool
    detail: str = ""

    def __post_init__(self) -> None:
        _require_bool(self.ready)
        if not isinstance(self.detail, str):
            raise ProviderError("invalid_request")

    @property
    def status(self) -> str:
        """Bounded status string derived from ``ready``."""
        return _STATUS_READY if self.ready else _STATUS_UNAVAILABLE

    def to_dict(self) -> Dict[str, Any]:
        return {"ready": self.ready, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class ProviderRequest:
    """A structured provider request: a task plus ordered context inputs.

    ``context`` accepts only an explicit list or tuple of non-empty strings and
    is normalized to an immutable tuple. A bare string, any non-sequence value,
    or a sequence containing empty or non-string items is rejected.
    """

    task_id: str
    task: str
    context: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id)
        _require_non_empty(self.task)
        if not isinstance(self.context, (list, tuple)):
            raise ProviderError("invalid_request")
        for item in self.context:
            _require_non_empty(item)
        object.__setattr__(self, "context", tuple(self.context))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "context": list(self.context),
        }


@dataclass(frozen=True)
class ProviderResult:
    """A structured provider result.

    ``content`` is the response text; ``structured_payload`` is an optional
    deterministic structured payload produced only when the provider supports
    structured output and restricted to JSON-safe values; ``provider`` and
    ``model`` identify the source; ``usage`` is optional deterministic usage
    metadata.
    """

    task_id: str
    content: str
    provider: str
    model: str
    structured_payload: Optional[Dict[str, Any]] = None
    usage: Optional[ProviderUsage] = None

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id)
        if not isinstance(self.content, str):
            raise ProviderError("invalid_request")
        _require_non_empty(self.provider)
        _require_non_empty(self.model)
        if self.structured_payload is not None and not (
            isinstance(self.structured_payload, dict)
            and _is_json_simple(self.structured_payload)
        ):
            raise ProviderError("invalid_request")
        if self.usage is not None and not isinstance(self.usage, ProviderUsage):
            raise ProviderError("invalid_request")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "provider": self.provider,
            "model": self.model,
            "structured_payload": (
                copy.deepcopy(self.structured_payload)
                if self.structured_payload is not None
                else None
            ),
            "usage": self.usage.to_dict() if self.usage is not None else None,
        }


# -- provider interface --------------------------------------------------


class Provider(abc.ABC):
    """Provider-neutral coding-agent interface.

    Three operations only: capability discovery, readiness checking, and one
    structured generation call. Streaming, cancellation, and tool calling are
    deliberately absent from the interface.
    """

    @abc.abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return the provider's declared capabilities."""

    @abc.abstractmethod
    def readiness(self) -> ProviderReadiness:
        """Return a deterministic readiness/health result."""

    @abc.abstractmethod
    def generate(self, request: ProviderRequest) -> ProviderResult:
        """Perform one structured generation.

        Returns a deterministic :class:`ProviderResult` on success and raises a
        bounded :class:`ProviderError` on failure.
        """


# -- deterministic test provider -----------------------------------------


def _token_estimate(text: str) -> int:
    """Deterministic token-count approximation (whitespace-delimited words)."""
    return len(text.split())


class FakeProvider(Provider):
    """Deterministic, offline provider for tests and conformance checks only.

    Reports exactly the capabilities it is constructed with, returns a
    readiness result tied to its construction, and produces repeatable,
    byte-stable results for equivalent input. It performs no network access,
    never mutates its inputs, and raises a controlled :class:`ProviderError`
    for the reserved :data:`FAILURE_TASK_ID`.
    """

    def __init__(
        self,
        *,
        provider: str = "fake",
        model: str = "fake-model-1",
        structured_output: bool = True,
        streaming: bool = False,
        cancellation: bool = False,
        tool_calling: bool = False,
        context_limit: Optional[int] = 8192,
        readiness: bool = True,
        readiness_detail: str = "fake provider is ready",
    ) -> None:
        self._provider = _require_non_empty(provider)
        self._model = _require_non_empty(model)
        self._capabilities = ProviderCapabilities(
            structured_output=structured_output,
            streaming=streaming,
            cancellation=cancellation,
            tool_calling=tool_calling,
            context_limit=context_limit,
            readiness_available=True,
        )
        self._readiness = ProviderReadiness(ready=readiness, detail=readiness_detail)

    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def readiness(self) -> ProviderReadiness:
        return self._readiness

    def generate(self, request: ProviderRequest) -> ProviderResult:
        if not isinstance(request, ProviderRequest):
            raise ProviderError("invalid_request")
        if request.task_id == FAILURE_TASK_ID:
            raise ProviderError("provider_failure")

        content = f"fake response for task {request.task_id}"
        prompt_tokens = _token_estimate(request.task)
        completion_tokens = _token_estimate(content)
        structured_payload = None
        if self._capabilities.structured_output:
            structured_payload = {
                "task_id": request.task_id,
                "context": list(request.context),
                "context_count": len(request.context),
            }
        return ProviderResult(
            task_id=request.task_id,
            content=content,
            provider=self._provider,
            model=self._model,
            structured_payload=structured_payload,
            usage=ProviderUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
