"""Claude Code hook JSON -> the M4.1 adapter-neutral Developer Memory boundary.

This module is the adapter the M4.1 contract anticipated. It owns every piece
of provider-specific knowledge -- hook event names, hook field names, the
``SessionEnd`` reason vocabulary -- and translates *documented* Claude Code
hook payloads into the source-neutral ``run_started`` / ``run_progress`` /
``run_terminated`` / ``stream_ended`` vocabulary of :mod:`hrca.memory`. The
canonical domain stays source-neutral: nothing in :mod:`hrca.memory` knows this
module exists, and no provider name may enter a canonical record.

Design rules
------------
* **Documented fields only.** Every field read here is part of the documented
  hook input contract. A field that is absent stays absent; the adapter never
  reconstructs a value it did not observe, and it never invents a timestamp
  (hooks carry none).
* **The transcript is a reference, never a source.** ``transcript_path`` is
  normalized as a bounded artifact reference and is never opened. The contents
  of the transcript are outside this module's reach by construction.
* **Content is never persisted.** Prompt text, assistant text, tool responses
  and error text are dropped before anything is written. Only a SHA-256 digest
  and a character count survive, so a durable record can prove *that* content
  existed without being able to reproduce it.
* **Paths are relativized or refused.** A path inside the session root is
  stored relative to it; a path outside is refused entirely. An absolute
  personal path therefore cannot reach the store.
* **Terminal state is typed, never inferred.** ``Stop`` with
  ``stop_hook_active`` false and ``StopFailure`` are the only per-payload
  conclusions. A session that ends without one is terminated from the
  documented ``SessionEnd`` reason; a reason outside the cancellation set
  becomes an explicit unknown outcome. No narrative, message text, command
  text or timestamp order ever decides a terminal state.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import memory as _memory

# -- identity ------------------------------------------------------------

# The M4.1 adapter namespace. It is folded to a token by the contract, so the
# canonical run id reads ``run:claude-code:<session>:<run>``.
ADAPTER_NAME = "claude-code"

# -- documented hook events ----------------------------------------------

HOOK_SESSION_START = "SessionStart"
HOOK_SESSION_END = "SessionEnd"
HOOK_USER_PROMPT_SUBMIT = "UserPromptSubmit"
HOOK_PRE_TOOL_USE = "PreToolUse"
HOOK_POST_TOOL_USE = "PostToolUse"
HOOK_POST_TOOL_USE_FAILURE = "PostToolUseFailure"
HOOK_STOP = "Stop"
HOOK_STOP_FAILURE = "StopFailure"

# The bounded set this adapter installs and models. Capture is scoped to these
# events so the collector never subscribes to a surface it cannot map.
MODELLED_HOOK_EVENTS = (
    HOOK_SESSION_START,
    HOOK_USER_PROMPT_SUBMIT,
    HOOK_PRE_TOOL_USE,
    HOOK_POST_TOOL_USE,
    HOOK_POST_TOOL_USE_FAILURE,
    HOOK_STOP,
    HOOK_STOP_FAILURE,
    HOOK_SESSION_END,
)

# Every other event name the installed client documents. These are *known* but
# deliberately not modelled for v1: they are recorded as an explicit omission
# rather than silently dropped, and they never poison a run.
UNMODELLED_HOOK_EVENTS = frozenset(
    {
        "PostToolBatch",
        "Notification",
        "UserPromptExpansion",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
        "PreModelSwitch",
        "PostModelSwitch",
        "PermissionRequest",
        "PermissionDenied",
        "Setup",
        "TeammateIdle",
        "TaskCreated",
        "TaskCompleted",
        "Elicitation",
        "ElicitationResult",
        "ConfigChange",
        "WorktreeCreate",
        "WorktreeRemove",
        "InstructionsLoaded",
        "CwdChanged",
        "FileChanged",
        "DirectoryAdded",
        "MessageDisplay",
    }
)

# The documented ``SessionEnd`` reason vocabulary. A value outside this set is
# reported as an explicit unknown outcome, never as success and never silently
# folded into one of the named reasons.
SESSION_END_REASONS = frozenset(
    {"clear", "resume", "logout", "prompt_input_exit", "other"}
)

# The subset of reasons that describe a session torn down by an explicit user
# or environment action rather than by the agent concluding its work. A run
# that ends on one of these *without* an observed conclusion was cancelled.
CANCELLATION_REASONS = frozenset({"clear", "resume", "logout", "prompt_input_exit"})

# -- documented field sets -----------------------------------------------

# Delivered on every event.
COMMON_HOOK_FIELDS = ("session_id", "transcript_path", "cwd", "hook_event_name")

# Delivered on some events.
OPTIONAL_COMMON_HOOK_FIELDS = (
    "prompt_id",
    "permission_mode",
    "agent_id",
    "agent_type",
)

# The fields documented for each modelled event, excluding the common ones.
EVENT_HOOK_FIELDS: Dict[str, Tuple[str, ...]] = {
    HOOK_SESSION_START: (
        "source",
        "agent_type",
        "model",
        "session_title",
        "seconds_since_last_response",
    ),
    HOOK_SESSION_END: ("reason",),
    HOOK_USER_PROMPT_SUBMIT: ("prompt", "source"),
    HOOK_PRE_TOOL_USE: ("tool_name", "tool_input", "tool_use_id"),
    HOOK_POST_TOOL_USE: (
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_use_id",
        "duration_ms",
    ),
    HOOK_POST_TOOL_USE_FAILURE: (
        "tool_name",
        "tool_input",
        "tool_use_id",
        "error",
        "error_type",
        "is_interrupt",
        "is_timeout",
        "duration_ms",
    ),
    HOOK_STOP: ("stop_hook_active", "last_assistant_message", "background_tasks"),
    HOOK_STOP_FAILURE: ("error",),
}

# Documented fields whose *value* is content. The value is never written; a
# digest and a character count are recorded instead.
CONTENT_HOOK_FIELDS = frozenset(
    {"prompt", "last_assistant_message", "tool_response", "error", "session_title"}
)

# Tool-input keys that name a path. The value is relativized, then policed.
TOOL_INPUT_PATH_KEYS = ("file_path", "path", "notebook_path", "directory")

# The only tool-input keys whose bounded value is kept. Every other key is
# recorded by name only.
TOOL_INPUT_KEPT_KEYS = ("command", "pattern", "old_string", "new_string")

# Tool names whose successful use is an observed change to a path.
CHANGE_TOOL_NAMES = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# -- canonical mapping ---------------------------------------------------

# Hook event -> adapter-neutral event type. Terminal resolution happens in
# :func:`translate_hook_event` because ``Stop`` and ``StopFailure`` are typed
# conclusions while the remaining progress events are not.
HOOK_EVENT_TYPES: Dict[str, str] = {
    HOOK_SESSION_START: _memory.EVENT_RUN_STARTED,
    HOOK_SESSION_END: _memory.EVENT_STREAM_ENDED,
    HOOK_USER_PROMPT_SUBMIT: _memory.EVENT_RUN_PROGRESS,
    HOOK_PRE_TOOL_USE: _memory.EVENT_RUN_PROGRESS,
    HOOK_POST_TOOL_USE: _memory.EVENT_RUN_PROGRESS,
    HOOK_POST_TOOL_USE_FAILURE: _memory.EVENT_RUN_PROGRESS,
    HOOK_STOP: _memory.EVENT_RUN_PROGRESS,
    HOOK_STOP_FAILURE: _memory.EVENT_RUN_PROGRESS,
}

# -- bounds --------------------------------------------------------------

# The hook input the collector will read from stdin. A payload larger than
# this is refused rather than buffered.
MAX_HOOK_INPUT_BYTES = 262144

# Bounded scalar projection limits.
MAX_TOOL_INPUT_KEYS = 64
MAX_TOOL_RESPONSE_KEYS = 64


# -- small helpers -------------------------------------------------------


def _safe_text(value: Any, limit: int) -> Optional[str]:
    """Return a redacted, bounded copy of a non-content string field."""
    if not isinstance(value, str) or not value.strip():
        return None
    bounded, _ = _memory.bound_text(_memory.redact_text(value), limit)
    return bounded or None


def safe_token(value: Any) -> Optional[str]:
    """Return a bounded, redaction-safe identifier."""
    return _safe_text(value, _memory.MAX_SOURCE_ID_CHARS)


def _safe_int(value: Any) -> Optional[int]:
    """Return ``value`` when it is a real integer (never a bool)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _safe_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def content_reference(value: Any) -> Optional[Dict[str, Any]]:
    """Describe content without keeping it.

    Returns ``{"chars": int, "digest": "sha256:<hex>"}`` for a string, or
    ``None`` when the field carries no content. The digest makes a durable
    record able to prove two observations saw the *same* content without being
    able to reproduce either one.
    """
    if not isinstance(value, str) or not value:
        return None
    return {
        "chars": len(value),
        "digest": "sha256:" + _memory.sha256_hex(value.encode("utf-8")),
    }


def relativize_path(value: Any, root: Any) -> Optional[str]:
    """Return ``value`` relative to ``root``, or ``None`` when it is outside.

    A path outside the session root is refused rather than stored, so an
    absolute personal path -- a home directory, a credentials directory, a
    temporary capture directory -- can never reach a durable record. The
    comparison is separator- and case-insensitive so it cannot be evaded by
    spelling on a case-insensitive filesystem.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if not isinstance(root, str) or not root.strip():
        return None
    path = _memory.portable_path(value.strip())
    base = _memory.portable_path(root.strip()).rstrip("/")
    if not base:
        return None
    if path.lower() == base.lower():
        return "."
    prefix = base + "/"
    if not path.lower().startswith(prefix.lower()):
        return None
    relative = path[len(prefix) :]
    return relative or None


def tool_input_projection(
    tool_input: Any, root: Optional[str]
) -> Tuple[Dict[str, Any], List[str]]:
    """Return ``(kept, dropped_key_names)`` for one ``tool_input`` mapping.

    A path-valued key is kept relativized. A small allowlist of bounded
    non-content keys is kept redacted. Every other key is reported by *name*
    only, so an unrecognized tool argument is disclosed as an omission instead
    of being copied into a durable record.
    """
    kept: Dict[str, Any] = {}
    dropped: List[str] = []
    if not isinstance(tool_input, dict):
        return kept, dropped

    for key in sorted(tool_input):
        if not isinstance(key, str):
            continue
        if len(kept) >= MAX_TOOL_INPUT_KEYS:
            dropped.append(key)
            continue
        value = tool_input[key]
        if key in TOOL_INPUT_PATH_KEYS:
            relative = relativize_path(value, root)
            kept[key] = relative
            if relative is None:
                dropped.append(key)
            continue
        if key in TOOL_INPUT_KEPT_KEYS:
            text = _safe_text(value, _memory.MAX_TEXT_CHARS)
            kept[key] = text
            if text is None:
                dropped.append(key)
            continue
        kept[key + "_keys_only"] = True
        dropped.append(key)
    return kept, sorted(set(dropped))


def describe_tool_response(tool_response: Any) -> Dict[str, Any]:
    """Summarize a tool response without retaining any of it."""
    reference = content_reference(tool_response)
    if reference is not None:
        return {"chars": reference["chars"], "digest": reference["digest"]}
    if isinstance(tool_response, dict):
        keys = sorted(k for k in tool_response if isinstance(k, str))
        return {
            "keys": keys[:MAX_TOOL_RESPONSE_KEYS],
            "keys_omitted": max(0, len(keys) - MAX_TOOL_RESPONSE_KEYS),
        }
    if isinstance(tool_response, list):
        return {"items": len(tool_response)}
    return {"present": tool_response is not None}


# -- identity ------------------------------------------------------------


def hook_event_name(payload: Any) -> Optional[str]:
    """Return the documented event name of a hook payload, or ``None``."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("hook_event_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def source_event_id_for(name: str, payload: Dict[str, Any]) -> str:
    """Return a content-independent identity for one hook payload.

    Identity must not depend on the observed values, or a redelivery whose
    content changed could not be recognized as a conflicting duplicate. The
    client's own stable identifiers are used where the contract provides one:
    ``tool_use_id`` for tool events, ``prompt_id`` for a submitted prompt and
    ``agent_id`` for a subagent. Only an event with no such identifier falls
    back to its name.
    """
    if name in (HOOK_PRE_TOOL_USE, HOOK_POST_TOOL_USE, HOOK_POST_TOOL_USE_FAILURE):
        tool_use_id = safe_token(payload.get("tool_use_id"))
        if tool_use_id is not None:
            return name + ":" + tool_use_id
    if name == HOOK_USER_PROMPT_SUBMIT:
        prompt_id = safe_token(payload.get("prompt_id"))
        if prompt_id is not None:
            return name + ":" + prompt_id
    if name == HOOK_SESSION_START:
        source = safe_token(payload.get("source"))
        if source is not None:
            return name + ":" + source
    if name == HOOK_STOP:
        # A continuation ``Stop`` and a concluding ``Stop`` are different
        # observations of the same turn and must never collide.
        active = payload.get("stop_hook_active") is True
        return name + ":continuation" if active else name + ":final"
    if name == HOOK_STOP_FAILURE:
        return name
    return name


# -- translation ---------------------------------------------------------


def translate_hook_event(
    payload: Any,
    root: Optional[str] = None,
    policy: Optional[_memory.PrivacyPolicy] = None,
) -> List[Dict[str, Any]]:
    """Translate one documented hook payload into adapter-neutral events.

    Returns an ordered list of zero or more M4.1 source events. A payload that
    is not a mapping, that carries no event name, or that names an *unknown*
    event produces exactly one event whose ``event_type`` is outside the
    contract, so the reducer records it as an unsupported event rather than
    letting it pass as progress.
    """
    policy = policy or _memory.DEFAULT_POLICY

    if not isinstance(payload, dict):
        return [_unsupported_event("hook-payload-not-a-mapping", None)]

    name = hook_event_name(payload)
    if name is None:
        return [_unsupported_event("hook-payload-missing-event-name", None)]

    if name not in HOOK_EVENT_TYPES:
        # A documented but unmodelled event is disclosed as an omission. An
        # event this adapter does not recognize at all is fail-closed.
        if name in UNMODELLED_HOOK_EVENTS:
            return [_unmodelled_event(name, payload, root, policy)]
        return [_unsupported_event(name, payload)]

    projection = project_hook_payload(name, payload, root, policy)
    event_type = HOOK_EVENT_TYPES[name]
    event: Dict[str, Any] = {
        "event_type": event_type,
        "source_event_id": source_event_id_for(name, payload),
        "payload": projection,
        "evidence": [],
        "decisions": [],
        "code_entities": [],
        "paths": [],
    }

    if name == HOOK_STOP and payload.get("stop_hook_active") is not True:
        # The agent concluded its response. This is a typed lifecycle
        # transition, not an inference from the message it produced.
        event["event_type"] = _memory.EVENT_RUN_TERMINATED
        event["outcome"] = _memory.OUTCOME_COMPLETED
    elif name == HOOK_STOP_FAILURE:
        # The turn ended on an error instead of a conclusion.
        event["event_type"] = _memory.EVENT_RUN_TERMINATED
        event["outcome"] = _memory.OUTCOME_FAILED

    _attach_evidence(event, name, payload, root)
    _attach_change_set(event, name, payload, root, policy)
    return [event]


def _base_projection(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the projection skeleton shared by every modelled event."""
    document_name = name
    projection: Dict[str, Any] = {"hook_event": document_name}
    session_id = safe_token(payload.get("session_id"))
    if session_id is not None:
        projection["session_id"] = session_id
    return projection


def project_hook_payload(
    name: str,
    payload: Dict[str, Any],
    root: Optional[str],
    policy: _memory.PrivacyPolicy,
) -> Dict[str, Any]:
    """Return a bounded, redacted, content-free projection of one payload.

    The projection records what was *observed*: the documented fields that were
    present, the documented fields that were missing, the fields whose value
    was deliberately dropped, and the field names this adapter does not
    recognize. No absent value is reconstructed and no content is copied.
    """
    projection = _base_projection(name, payload)

    documented = set(COMMON_HOOK_FIELDS)
    documented.update(OPTIONAL_COMMON_HOOK_FIELDS)
    documented.update(EVENT_HOOK_FIELDS.get(name, ()))

    present: List[str] = []
    missing: List[str] = []
    omitted: List[str] = []
    unknown: List[str] = []

    for key in sorted(payload):
        if not isinstance(key, str):
            continue
        if key in documented:
            present.append(key)
        else:
            unknown.append(key)

    for key in sorted(documented):
        if key not in payload:
            missing.append(key)

    # -- the session root is never stored as an absolute path -------------
    cwd = relativize_path(payload.get("cwd"), root)
    projection["cwd"] = cwd
    if cwd is None:
        omitted.append("cwd")

    # -- content-bearing fields: digest and length only -------------------
    for key in sorted(CONTENT_HOOK_FIELDS):
        if key not in payload:
            continue
        reference = content_reference(payload.get(key))
        projection[key + "_content"] = reference
        omitted.append(key)

    # -- documented non-content scalars -----------------------------------
    if name == HOOK_SESSION_START:
        projection["source"] = safe_token(payload.get("source"))
        projection["model"] = _safe_text(payload.get("model"), _memory.MAX_TEXT_CHARS)
        projection["seconds_since_last_response"] = _safe_int(
            payload.get("seconds_since_last_response")
        )
    elif name == HOOK_SESSION_END:
        reason = payload.get("reason")
        # The reason is preserved verbatim as a typed value, including when it
        # is unrecognized: the adapter reports what it saw.
        projection["reason"] = reason if isinstance(reason, str) else None
        projection["reason_documented"] = reason in SESSION_END_REASONS
    elif name == HOOK_USER_PROMPT_SUBMIT:
        projection["source"] = safe_token(payload.get("source"))

    if name in (
        HOOK_PRE_TOOL_USE,
        HOOK_POST_TOOL_USE,
        HOOK_POST_TOOL_USE_FAILURE,
    ):
        projection["tool_name"] = _safe_text(
            payload.get("tool_name"), _memory.MAX_SYMBOL_CHARS
        )
        projection["tool_use_id"] = safe_token(payload.get("tool_use_id"))
        kept, dropped = tool_input_projection(payload.get("tool_input"), root)
        projection["tool_input"] = kept
        omitted.extend(dropped)

    if name == HOOK_POST_TOOL_USE:
        projection["duration_ms"] = _safe_int(payload.get("duration_ms"))
        projection["tool_response"] = describe_tool_response(
            payload.get("tool_response")
        )
    elif name == HOOK_POST_TOOL_USE_FAILURE:
        projection["duration_ms"] = _safe_int(payload.get("duration_ms"))
        projection["error_type"] = _safe_text(
            payload.get("error_type"), _memory.MAX_TEXT_CHARS
        )
        # ``is_interrupt`` is the only typed interrupt signal the surface
        # offers; it is preserved as an observation and never promoted to a
        # terminal state on its own.
        projection["is_interrupt"] = _safe_bool(payload.get("is_interrupt"))
        projection["is_timeout"] = _safe_bool(payload.get("is_timeout"))
    elif name == HOOK_STOP:
        projection["stop_hook_active"] = _safe_bool(payload.get("stop_hook_active"))
        background = payload.get("background_tasks")
        projection["background_tasks"] = (
            len(background) if isinstance(background, list) else None
        )

    for key in ("prompt_id", "permission_mode", "agent_id", "agent_type"):
        if key in payload:
            projection[key] = safe_token(payload.get(key))

    # -- disclose what was not used ---------------------------------------
    projection["present_fields"] = sorted(set(present))
    projection["missing_fields"] = sorted(set(missing))
    projection["omitted_fields"] = sorted(set(omitted))
    projection["unknown_fields"] = sorted(set(unknown))
    return projection


def _attach_evidence(
    event: Dict[str, Any], name: str, payload: Dict[str, Any], root: Optional[str]
) -> None:
    """Attach the bounded artifact references one payload justifies.

    ``transcript_path`` is normalized as a reference only. When the transcript
    sits outside the session root the reference is recorded with no location
    at all, which the contract stores as an unresolved artifact: the record
    proves an artifact was named without preserving where it lives.
    """
    evidence: List[Dict[str, Any]] = []
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript.strip():
        evidence.append(
            {
                "kind": "transcript",
                "artifact_ref": relativize_path(transcript, root),
                "source_id": None,
                "bytes": None,
                "digest": None,
            }
        )
    evidence.append({"kind": "hook_event", "source_id": name, "artifact_ref": None})
    event["evidence"] = evidence


def _attach_change_set(
    event: Dict[str, Any],
    name: str,
    payload: Dict[str, Any],
    root: Optional[str],
    policy: _memory.PrivacyPolicy,
) -> None:
    """Record an observed file change when a change tool reported success."""
    if name != HOOK_POST_TOOL_USE:
        return
    tool_name = payload.get("tool_name")
    if tool_name not in CHANGE_TOOL_NAMES:
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    for key in TOOL_INPUT_PATH_KEYS:
        relative = relativize_path(tool_input.get(key), root)
        if relative is not None:
            event["paths"] = [relative]
            event["code_entities"] = [{"path": relative, "entity_kind": "file"}]
            return


def _unsupported_event(name: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a fail-closed event for input outside the documented surface."""
    projection: Dict[str, Any] = {"hook_event": name, "supported": False}
    if isinstance(payload, dict):
        session_id = safe_token(payload.get("session_id"))
        if session_id is not None:
            projection["session_id"] = session_id
    return {
        # ``event_type`` is deliberately outside the contract's vocabulary, so
        # the reducer records an unsupported event and never reports success.
        "event_type": "hook-event:" + name,
        "source_event_id": name,
        "payload": projection,
        "evidence": [],
        "decisions": [],
        "code_entities": [],
        "paths": [],
    }


def _unmodelled_event(
    name: str,
    payload: Dict[str, Any],
    root: Optional[str],
    policy: _memory.PrivacyPolicy,
) -> Dict[str, Any]:
    """Disclose a documented event this adapter does not model."""
    projection = _base_projection(name, payload)
    projection["cwd"] = relativize_path(payload.get("cwd"), root)
    projection["modelled"] = False
    event: Dict[str, Any] = {
        "event_type": _memory.EVENT_RUN_PROGRESS,
        "source_event_id": source_event_id_for(name, payload),
        "payload": projection,
        "evidence": [],
        "decisions": [],
        "code_entities": [],
        "paths": [],
    }
    _attach_evidence(event, name, payload, root)
    return event


# -- session assembly ----------------------------------------------------


def conclusion_outcome(payload: Any) -> Optional[str]:
    """Return the typed terminal outcome one payload concludes with, if any."""
    if not isinstance(payload, dict):
        return None
    name = hook_event_name(payload)
    if name == HOOK_STOP and payload.get("stop_hook_active") is not True:
        return _memory.OUTCOME_COMPLETED
    if name == HOOK_STOP_FAILURE:
        return _memory.OUTCOME_FAILED
    return None


def outcome_from_session_end(reason: Any) -> Optional[str]:
    """Return the typed outcome a terminating session end justifies.

    A session that ends without an observed conclusion, on a reason that
    describes an explicit teardown, was cancelled. A reason outside that set
    -- including the documented catch-all ``other`` -- is *not* interpreted:
    ``None`` is returned so the caller carries an explicit unrecognized
    outcome, which the contract reports as unknown rather than success.
    """
    if isinstance(reason, str) and reason in CANCELLATION_REASONS:
        return _memory.OUTCOME_CANCELLED
    return None


# The outcome carried by a termination this adapter derived without an
# observed conclusion and without a recognized cancellation reason. It is
# deliberately not a member of the contract's outcome vocabulary, so the
# reducer reports an explicit unknown outcome rather than a guess. A
# terminating session is never reported as success.
OUTCOME_UNRECOGNIZED = "unrecognized"


def assemble_session(
    events: List[Dict[str, Any]],
    project: Optional[Dict[str, Any]] = None,
    work_package: Optional[Dict[str, Any]] = None,
    run_key: Any = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Assemble bounded, already-translated events into an adapter-neutral session.

    Returns ``(session, report)``. ``session`` is ``None`` only when the stream
    carries no usable session identity; otherwise it is the mapping
    :func:`hrca.memory.ingest_session` consumes. ``report`` is a bounded,
    content-free account of how the stream was interpreted.

    This function consumes *translated* events, never raw hook payloads, so the
    capture spool holds nothing that has not already been redacted and bounded.

    Terminal resolution is deliberately split from per-payload translation: a
    conclusion is only reachable once the whole bounded stream is known,
    because a ``SessionEnd`` closes a run that ``Stop`` never concluded.
    """
    events = [dict(event) for event in events]
    concluded = any(
        event.get("event_type") == _memory.EVENT_RUN_TERMINATED for event in events
    )

    session_end_reason: Optional[str] = None
    inserted = False
    if not concluded:
        for index, event in enumerate(events):
            projection = event.get("payload")
            if not isinstance(projection, dict):
                continue
            if projection.get("hook_event") != HOOK_SESSION_END:
                continue
            reason = projection.get("reason")
            session_end_reason = reason if isinstance(reason, str) else None
            outcome = outcome_from_session_end(session_end_reason) or OUTCOME_UNRECOGNIZED
            events.insert(
                index,
                {
                    "event_type": _memory.EVENT_RUN_TERMINATED,
                    "outcome": outcome,
                    "source_event_id": "terminal:" + HOOK_SESSION_END,
                    "payload": {
                        "hook_event": HOOK_SESSION_END,
                        "reason": session_end_reason,
                        "outcome_source": "session_end_reason",
                    },
                    "evidence": [],
                    "decisions": [],
                    "code_entities": [],
                    "paths": [],
                },
            )
            inserted = True
            break

    session_id = None
    for event in events:
        projection = event.get("payload")
        if isinstance(projection, dict):
            candidate = projection.get("session_id")
            if isinstance(candidate, str) and candidate:
                session_id = candidate
                break

    report = {
        "adapter": ADAPTER_NAME,
        "events": len(events),
        "concluded_by_agent": concluded,
        "terminated_from_session_end": inserted,
        "session_end_reason": session_end_reason,
        "unmodelled_events": sorted(
            {
                event.get("payload", {}).get("hook_event")
                for event in events
                if isinstance(event.get("payload"), dict)
                and event["payload"].get("modelled") is False
            }
        ),
        "unsupported_events": sorted(
            {
                event.get("payload", {}).get("hook_event")
                for event in events
                if isinstance(event.get("payload"), dict)
                and event["payload"].get("supported") is False
            }
        ),
    }
    if session_id is None:
        return None, report

    session: Dict[str, Any] = {"adapter": ADAPTER_NAME, "session_id": session_id}
    if run_key is not None:
        session["run_key"] = run_key
    if isinstance(project, dict):
        session["project"] = project
    if isinstance(work_package, dict):
        session["work_package"] = work_package
    session["events"] = events
    return session, report
