"""The bounded live DeepSeek adapter (AGENT-LOOP-LIVE-1).

One provider-neutral typed inference call, for **one** approved model, over the
official HTTPS endpoint. Everything about the request is code-owned: the host,
the path, the model id, the thinking mode, the response format, the token caps
and the timeout. Nothing a caller or an environment value supplies can widen any
of it — a caller supplies bounded task text and bounded context items, and that
is all.

**Where it runs.** The call is made *inside the owned provider worker child*,
never in the backend. The parent keeps the deadline, the kill and the reap; the
child has no lifetime of its own beyond that.

**What it is not.** No SDK, no retry, no re-prompt, no fallback, no second
provider, no endpoint discovery, no redirect following, no proxy substitution,
no streaming, no tools parameter, no tracing. A response that is not one
complete, bounded JSON object carrying a valid choice payload is a bounded
non-success — never repaired, never rounded up.

**Conservatism.** The per-request input bound is the UTF-8 **byte** length of
what is actually sent. Any byte-level tokeniser maps one token to one or more
bytes, so bytes are an upper bound on tokens on every host, with no tokeniser
installed and no optimistic estimate anywhere. Over-budget input is refused
before dispatch, never truncated.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
from typing import Any, Dict, List, Optional, Tuple

# -- code-owned configuration (never caller- or environment-widenable) ------

#: The one approved model id.
MODEL_ID = "deepseek-flash"

#: The worker scenario name for one live call. It is deliberately **not** in the
#: worker's offline ``SCENARIOS`` registry, so a ``HRCA_AGENT_OFFLINE_SCENARIO``
#: value can never select a live call: the live path is reached only from the
#: parent that was explicitly configured for it.
LIVE_SCENARIO = "live"
#: The fixed official host and path. There is no URL parameter anywhere.
BASE_HOST = "api.deepseek.com"
COMPLETIONS_PATH = "/chat/completions"

#: The client's own per-request caps. ``MAX_INPUT_TOKENS`` is enforced on the
#: conservative byte bound below; ``MAX_OUTPUT_TOKENS`` is the requested
#: ``max_tokens`` ceiling, which the gate may only lower.
MAX_INPUT_TOKENS = 2500
MAX_OUTPUT_TOKENS = 500

#: Bounded waits and bounded reads. The parent's own deadline is what actually
#: ends a call; these are the child's own, smaller, first line.
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_BODY_BYTES = 256 * 1024

#: The credential's environment name. The value is read here and nowhere else,
#: is never returned to a caller, is never logged and never crosses the wire.
CREDENTIAL_ENV = "DEEPSEEK_API_KEY"

#: The fixed control fragment. ``thinking`` is disabled **explicitly** rather
#: than left to a default, because the documented default for this model family
#: is enabled-and-high-effort; a silent default change would silently change
#: both the latency and the bill.
THINKING_DISABLED = {"type": "disabled"}
RESPONSE_FORMAT = {"type": "json_object"}

#: Code-owned system framing. It states the shape, never the decision.
SYSTEM_PROMPT = (
    "You are the bounded planning step of a local coding agent. Reply with "
    "one JSON object and nothing else: no prose, no markdown, no code fences. "
    "You may not run commands, read files, change scope or approve anything. "
    "The allowed choices and their arguments are stated in the user message."
)

#: The example that goes with the instruction. A schema example is smaller and
#: far more reliable than prose, and it is code-owned, so it cannot drift into
#: inventing envelope keys.
SCHEMA_EXAMPLE = (
    '{"schema_version":"1.0.0","choice":"answer","rationale":"short reason",'
    '"arguments":{"answer":"bounded text","references":[]}}'
)

# -- bounded reasons -------------------------------------------------------
CODE_BLANK = "the model answer was empty"
CODE_MALFORMED = "the model answer was not one JSON object"
CODE_OVERSIZED = "the model answer exceeded the bounded size"
CODE_INCOMPLETE = "the model answer did not complete"
CODE_HTTP = "the model endpoint did not answer successfully"
CODE_TRANSPORT = "the model request did not complete"
CODE_NO_CREDENTIAL = "the live model credential is not configured"
CODE_TOO_LARGE = "the model request exceeded the bounded input size"


def credential_present() -> bool:
    """Return whether the approved credential variable is present and non-empty.

    Presence only: the value is never returned, printed, hashed or logged.
    """
    value = os.environ.get(CREDENTIAL_ENV)
    return bool(value and value.strip())


def input_upper_bound(text: str) -> int:
    """Return a conservative upper bound on the tokens ``text`` costs.

    One byte is at least one token, so the UTF-8 byte length is an upper bound
    for any byte-level tokeniser. It is deliberately not an estimate: an
    optimistic estimate is exactly how a batch overshoots its cap.
    """
    return len(text.encode("utf-8"))


def build_user_message(task: str, context: Tuple[str, ...]) -> str:
    """Build the bounded user message: the instruction, the facts, the example."""
    lines = ["INSTRUCTION:", task.strip(), "", "PROJECT FACTS (bounded):"]
    if context:
        lines.extend("- " + item for item in context)
    else:
        lines.append("- (none)")
    lines += ["", "Reply with one JSON object only. Example:", SCHEMA_EXAMPLE]
    return "\n".join(lines)


def build_body(
    task: str, context: Tuple[str, ...], *, max_tokens: int
) -> Tuple[str, int]:
    """Return ``(body_text, input_upper_bound)`` for one bounded request.

    The bound covers **everything that is sent** — the system framing, the
    instruction, the facts and the schema example — because the provider bills
    the whole payload, not the part a caller happened to think of as input.
    """
    user = build_user_message(task, context)
    body = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "thinking": dict(THINKING_DISABLED),
        "stream": False,
        "max_tokens": int(max_tokens),
        "response_format": dict(RESPONSE_FORMAT),
    }
    text = json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    # The bound is the byte length of the *whole serialized body*: every byte
    # that leaves the process is counted, including the envelope keys and the
    # JSON punctuation, and every byte is at least one token.
    return text, input_upper_bound(text)


def _https_post(body: str, key: str, timeout: float) -> Tuple[int, bytes]:
    """Send one request to the fixed endpoint. No retry, no redirect, no proxy.

    ``http.client`` does not follow redirects and is given the host explicitly,
    so an ``Authorization`` header can only ever reach ``api.deepseek.com``.
    """
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(BASE_HOST, timeout=timeout, context=context)
    try:
        connection.request(
            "POST",
            COMPLETIONS_PATH,
            body=body.encode("utf-8"),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()


def parse_response(status: int, raw: bytes) -> Dict[str, Any]:
    """Turn one raw response into a bounded answer. Never repairs, never guesses.

    Returns ``{"ok": True, "payload": ..., "usage": ..., "model": ...,
    "finish_reason": ...}`` or ``{"ok": False, "code": ...}``. A ``200`` that
    carries empty content, a truncated body, an incomplete finish reason, a
    tool call or a non-object is a bounded non-success: the documented JSON mode
    does not make the answer valid, and this is where that is enforced.
    """
    if status != 200:
        return {"ok": False, "code": CODE_HTTP}
    if len(raw) > MAX_RESPONSE_BYTES:
        return {"ok": False, "code": CODE_OVERSIZED}
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "code": CODE_MALFORMED}
    if not isinstance(envelope, dict):
        return {"ok": False, "code": CODE_MALFORMED}
    # From here on the envelope is readable, so whatever it reports about the
    # bill is carried outward even when the answer itself is unusable: a failed
    # answer that was still billed must not look like a free one.
    reported = _bounded_usage(
        envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    )
    identity = {
        "usage": reported,
        "model": str(envelope.get("model") or ""),
        "fingerprint": str(envelope.get("system_fingerprint") or ""),
    }
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return {"ok": False, "code": CODE_MALFORMED, **identity}
    choice = choices[0]
    if not isinstance(choice, dict):
        return {"ok": False, "code": CODE_MALFORMED, **identity}
    finish = choice.get("finish_reason")
    if finish != "stop":
        # ``length``/``content_filter``/``tool_calls``/``None`` are all an
        # answer that did not complete. Recording which one is honest; using it
        # as an answer is not.
        return {
            "ok": False,
            "code": CODE_INCOMPLETE,
            "finish_reason": finish if isinstance(finish, str) else "",
            **identity,
        }
    message = choice.get("message")
    if not isinstance(message, dict):
        return {"ok": False, "code": CODE_MALFORMED, **identity}
    if message.get("tool_calls"):
        # No tools parameter is ever sent, so a tool call is not ours to use.
        return {"ok": False, "code": CODE_MALFORMED, **identity}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return {"ok": False, "code": CODE_BLANK, **identity}
    if len(content.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return {"ok": False, "code": CODE_OVERSIZED, **identity}
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "code": CODE_MALFORMED, **identity}
    if not isinstance(payload, dict):
        return {"ok": False, "code": CODE_MALFORMED, **identity}
    return {"ok": True, "payload": payload, "finish_reason": finish, **identity}


def _bounded_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    """Return the billable usage fields as bounded non-negative integers.

    Missing or unusable fields are reported as ``0`` here and are treated as
    *unknown* by the gate, which keeps its worst-case reservation rather than
    reading a missing number as a free call.
    """

    def _int(name: str) -> int:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return int(value)

    prompt = _int("prompt_tokens")
    completion = _int("completion_tokens")
    details = usage.get("completion_tokens_details")
    reasoning = 0
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            reasoning = int(value)
    cached = _int("prompt_cache_hit_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": _int("total_tokens"),
        "reasoning_tokens": reasoning,
        "prompt_cache_hit_tokens": cached,
        "reported": 1 if (prompt or completion) else 0,
    }


def live_call(
    task: str,
    context: Tuple[str, ...],
    *,
    max_tokens: int,
    poster: Optional[Any] = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Perform one bounded live call and return a bounded child answer.

    ``poster`` is the test seam: a caller may inject ``(body, key, timeout) ->
    (status, bytes)`` to exercise every branch offline. The default is the real
    fixed-endpoint HTTPS poster and is the only thing that can reach the network.
    """
    key = os.environ.get(CREDENTIAL_ENV)
    if not key or not key.strip():
        # The child refuses before any transport is constructed; a missing
        # credential never becomes a request.
        return {"ok": False, "code": CODE_NO_CREDENTIAL}
    body, bound = build_body(task, context, max_tokens=max_tokens)
    if bound > MAX_INPUT_TOKENS or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        return {"ok": False, "code": CODE_TOO_LARGE}
    send = poster if poster is not None else _https_post
    try:
        status, raw = send(body, key, timeout)
    except Exception:
        # A transport failure carries no detail outward: an exception message
        # can quote a header, and a header can quote a key.
        return {"ok": False, "code": CODE_TRANSPORT}
    return parse_response(int(status), bytes(raw))


__all__ = [
    "MODEL_ID",
    "LIVE_SCENARIO",
    "BASE_HOST",
    "COMPLETIONS_PATH",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "REQUEST_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "CREDENTIAL_ENV",
    "THINKING_DISABLED",
    "RESPONSE_FORMAT",
    "SYSTEM_PROMPT",
    "SCHEMA_EXAMPLE",
    "CODE_BLANK",
    "CODE_MALFORMED",
    "CODE_OVERSIZED",
    "CODE_INCOMPLETE",
    "CODE_HTTP",
    "CODE_TRANSPORT",
    "CODE_NO_CREDENTIAL",
    "CODE_TOO_LARGE",
    "credential_present",
    "input_upper_bound",
    "build_user_message",
    "build_body",
    "parse_response",
    "live_call",
]
