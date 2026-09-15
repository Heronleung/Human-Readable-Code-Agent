"""Replayable offline Developer Memory contract (M4.1).

This module is the Qt-free, dependency-free *domain* for bounded coding-agent
runs. It turns an adapter-neutral, bounded source session into a versioned,
canonical Developer Memory store, and reduces the same event stream to the same
terminal ``AgentRun`` state every time.

The module is deliberately pure in the same sense as :mod:`hrca.twin` and
:mod:`hrca.document`:

* no filesystem, network, credential, provider, model, command, Git or
  repository access — persistence belongs to :mod:`hrca.memory_store`;
* stdlib only, Qt-free, so the desktop client can never import it (enforced by
  :mod:`tests.test_architecture`);
* every record is assembled deterministically from its inputs, and every
  rejection is a bounded reason constant that never interpolates source content.

The core is **source-neutral**. It knows nothing about Claude Code hooks,
transcripts or any other provider vocabulary; an *adapter* (a later work
package, M4.2) maps a provider's native stream onto the :data:`SESSION_KEYS`
shape below. Source-specific field mapping must never enter this module.

Record kinds
------------

One bounded source session normalizes into one ``AgentRun`` aggregate holding:

``project`` / ``work_package``
    Adapter-namespaced descriptors of the repository and unit of work the run
    belongs to.
``agent_run``
    The run itself: identity, state, ingest ledger and the source timestamps
    retained **as evidence only**.
``agent_run_event``
    Typed, validated events in monotonic ingest order.
``change_set``
    The bounded set of paths/entities an event reports as touched.
``evidence``
    Bounded artifact *metadata* (kind, reference, byte size, digest). Evidence
    references an artifact; it never carries artifact content.
``decision``
    A recorded decision with a bounded, redacted rationale.
``code_entity_link``
    A link between a run record and a code entity (file path or
    ``module.path.Class.method`` symbol locator).

Determinism and identity
------------------------

* Identity is namespaced by adapter and session, and derived from a *stable
  source id* when the source supplies one, otherwise from a deterministic
  fingerprint over canonical **non-secret** fields. Source timestamps are
  excluded from the identity projection and retained only as evidence.
* Ingest order is assigned by a **per-run single writer** as a monotonic
  ordinal. Ordering never derives from source timestamps.
* Identical redelivery is an idempotent no-op. The same identity with different
  canonical content is quarantined: it never overwrites evidence and never
  advances run state.
* A store serializes to a single-line, ASCII-safe, key-sorted JSON string, so
  identical re-imports are byte-identical.

Terminal state
--------------

Terminal state is owned exclusively by a typed, validated ``run_terminated``
transition carrying an outcome from :data:`TERMINAL_OUTCOMES`. It is never
inferred from the last message, command text, timestamp order, event ordering or
agent narrative. A bounded stream that ends without such a transition resolves
to :data:`RUN_MISSING_TERMINAL`; an unrecognized outcome resolves to
:data:`RUN_UNKNOWN_OUTCOME`; an unrecognized event type resolves to
:data:`RUN_UNSUPPORTED`. None of the three is ever success.

Every terminal state is **absorbing**. Finalizing a store marks it a closed
snapshot of one bounded session: a later, genuinely new event is refused,
because it belongs to a new session. An identical redelivery stays an
idempotent no-op and a conflicting redelivery stays a quarantine — neither is
blocked by finalization, because neither may advance the run.

Privacy
-------

Path policy, payload bounds and secret redaction (see :class:`PrivacyPolicy`)
are applied **before** any record is assembled, so no raw source payload and no
secret-like value can reach a durable write. The identity projection is computed
over the already-redacted content, so a secret can never influence identity and
redaction can never break idempotency.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

MEMORY_SCHEMA_VERSION = "1.0.0"
MEMORY_GENERATOR = "hrca-memory"

# -- record kinds --------------------------------------------------------

RECORD_PROJECT = "project"
RECORD_WORK_PACKAGE = "work_package"
RECORD_RUN = "agent_run"
RECORD_EVENT = "agent_run_event"
RECORD_CHANGE_SET = "change_set"
RECORD_EVIDENCE = "evidence"
RECORD_DECISION = "decision"
RECORD_CODE_ENTITY_LINK = "code_entity_link"
RECORD_REJECTION = "rejection"
RECORD_QUARANTINE = "quarantine"
RECORD_KINDS = frozenset(
    {
        RECORD_PROJECT,
        RECORD_WORK_PACKAGE,
        RECORD_RUN,
        RECORD_EVENT,
        RECORD_CHANGE_SET,
        RECORD_EVIDENCE,
        RECORD_DECISION,
        RECORD_CODE_ENTITY_LINK,
        RECORD_REJECTION,
        RECORD_QUARANTINE,
    }
)

# The top-level arrays of a memory store, in canonical serialization order.
STORE_ARRAYS = (
    "projects",
    "work_packages",
    "events",
    "change_sets",
    "evidence",
    "decisions",
    "code_entity_links",
    "rejections",
    "quarantines",
)

# -- provenance ----------------------------------------------------------

# ``source``     — the value is carried literally by the adapter payload;
# ``derived``    — the contract computed the value deterministically;
# ``unresolved`` — the contract could not model the value and says so.
PROVENANCE_SOURCE = "source"
PROVENANCE_DERIVED = "derived"
PROVENANCE_UNRESOLVED = "unresolved"
PROVENANCES = frozenset({PROVENANCE_SOURCE, PROVENANCE_DERIVED, PROVENANCE_UNRESOLVED})

# -- adapter-neutral source session shape --------------------------------

# The complete, bounded input contract an adapter must produce. Anything not
# listed here is ignored; nothing outside the contract is reconstructed.
SESSION_KEYS = frozenset(
    {
        "adapter",
        "session_id",
        "run_key",
        "project",
        "work_package",
        "events",
    }
)

# The complete, bounded input contract for one source event.
SOURCE_EVENT_KEYS = frozenset(
    {
        "event_type",
        "source_event_id",
        "source_sequence",
        "source_timestamp",
        "outcome",
        "message",
        "command",
        "payload",
        "paths",
        "change_set",
        "evidence",
        "decisions",
        "code_entities",
    }
)

# -- event types (adapter-neutral) ---------------------------------------

EVENT_RUN_STARTED = "run_started"
EVENT_RUN_PROGRESS = "run_progress"
EVENT_RUN_TERMINATED = "run_terminated"
EVENT_STREAM_ENDED = "stream_ended"

SUPPORTED_EVENT_TYPES = frozenset(
    {EVENT_RUN_STARTED, EVENT_RUN_PROGRESS, EVENT_RUN_TERMINATED, EVENT_STREAM_ENDED}
)

# The outcome vocabulary a typed terminal transition may carry. An outcome
# outside this set is *unknown*, never success.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_BLOCKED = "blocked"
TERMINAL_OUTCOMES = {
    OUTCOME_COMPLETED: "completed",
    OUTCOME_FAILED: "failed",
    OUTCOME_CANCELLED: "cancelled",
    OUTCOME_BLOCKED: "blocked",
}

# -- run states ----------------------------------------------------------

# Transient reducer states. ``unstarted`` means the session is known but no run
# event has been observed yet; ``open`` means the run started and no typed
# terminal transition has been validated. A *finalized* store never carries
# either: both resolve to ``missing_terminal``.
RUN_UNSTARTED = "unstarted"
RUN_OPEN = "open"

# Terminal states. Every one of these is absorbing: no later event may move a
# run out of it, and only ``completed`` is success.
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_BLOCKED = "blocked"
RUN_MISSING_TERMINAL = "missing_terminal"
RUN_UNKNOWN_OUTCOME = "unknown_outcome"
RUN_UNSUPPORTED = "unsupported"

# The states a persisted (finalized) store may report.
REPORTED_RUN_STATES = frozenset(
    {
        RUN_COMPLETED,
        RUN_FAILED,
        RUN_CANCELLED,
        RUN_BLOCKED,
        RUN_MISSING_TERMINAL,
        RUN_UNKNOWN_OUTCOME,
        RUN_UNSUPPORTED,
    }
)

# Every state the reducer may hold, including the two transient ones.
RUN_STATES = frozenset({RUN_UNSTARTED, RUN_OPEN}) | REPORTED_RUN_STATES

TERMINAL_RUN_STATES = REPORTED_RUN_STATES

# The single success state. Nothing else may ever be reported as success.
SUCCESS_RUN_STATES = frozenset({RUN_COMPLETED})

# -- bounded rejection reasons -------------------------------------------

# Reasons are fixed constants and never interpolate source content, so a
# rejection record can never leak a payload, a path or a secret.
REASON_NOT_MAPPING = "store is not a mapping"
REASON_MISSING_VERSION = "missing schema_version"
REASON_INVALID_VERSION = "invalid schema_version"
REASON_FUTURE_VERSION = "schema_version is newer than supported"
REASON_NOT_MIGRATABLE = "schema_version is not migratable"
REASON_MIGRATION_FAILED = "migration failed"
REASON_MIGRATION_DRIFT = "migration changed identity, evidence or replay result"
REASON_NOT_A_SESSION = "session is not a mapping"
REASON_SESSION_MISSING_ADAPTER = "session is missing a usable adapter"
REASON_SESSION_MISSING_ID = "session is missing a usable session_id"
REASON_SESSION_EVENTS_NOT_LIST = "session events is not a list"
REASON_SESSION_EVENT_LIMIT = "session exceeds the bounded event count"
REASON_MALFORMED_EVENT = "event is not a mapping with a string event_type"
REASON_UNSUPPORTED_EVENT_TYPE = "event_type is not supported by the contract"
REASON_INVALID_TRANSITION = "transition is not valid from the current run state"
REASON_UNKNOWN_OUTCOME = "run_terminated carries an unrecognized outcome"
REASON_AFTER_STREAM_END = "event arrives after the stream was ended"
REASON_ALREADY_FINALIZED = "run store is finalized; a new event belongs to a new session"
REASON_CONFLICTING_REDELIVERY = "same identity redelivered with different content"
REASON_PAYLOAD_OVERSIZED = "redacted event payload exceeds the bounded size"

# -- privacy policy ------------------------------------------------------

REDACTION_MARKER = "[redacted]"

# Bounded defaults. Every limit is code-owned; no limit is ever read from
# source content.
MAX_EVENTS_PER_SESSION = 4096
MAX_TEXT_CHARS = 2048
MAX_REF_CHARS = 512
MAX_PATH_CHARS = 512
MAX_SYMBOL_CHARS = 512
MAX_TIMESTAMP_CHARS = 64
MAX_SOURCE_ID_CHARS = 256
MAX_PAYLOAD_BYTES = 16384
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_KEYS = 128
MAX_PAYLOAD_LIST = 128
MAX_EVIDENCE_PER_EVENT = 64
MAX_DECISIONS_PER_EVENT = 64
MAX_CODE_ENTITIES_PER_EVENT = 256
MAX_PATHS_PER_CHANGE_SET = 512
MAX_EVIDENCE_BYTES = 1 << 53

# Path policy: a reference to any of these is never persisted, so an excluded
# artifact can never be reconstructed from a memory store.
EXCLUDED_PATH_SEGMENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
    }
)
EXCLUDED_PATH_NAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
EXCLUDED_PATH_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".ppk",
)
EXCLUDED_PATH_PREFIXES = ("secrets/", "private/")

# Fields that would carry artifact *content*. The contract accepts artifact
# references only: a source that supplies content has it dropped and counted,
# never stored. This is the hard boundary between "a transcript path is a
# reference" and "ingesting transcript content".
CONTENT_BEARING_KEYS = frozenset(
    {
        "content",
        "text",
        "body",
        "transcript",
        "transcript_content",
        "output",
        "stdout",
        "stderr",
        "raw",
        "blob",
        "data",
    }
)

# -- secret redaction ----------------------------------------------------

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

_SECRET_TOKEN_RES = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgho_[A-Za-z0-9]{16,}"),
    re.compile(r"\bghs_[A-Za-z0-9]{16,}"),
    re.compile(r"\bghu_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@]{1,64}:[^/\s:@]{1,128}@"),
)

# ``key = value`` / ``key: value`` for credential-bearing key names. Only the
# value is replaced; the key name is non-secret and keeps the record readable.
# A quoted value keeps its quotes and an unquoted value stops at whitespace or
# a quote, so surrounding punctuation survives the redaction.
_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"secret[_-]?key|client[_-]?secret|private[_-]?key|authorization|credential)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)

# Redaction runs to a fixpoint so it is idempotent: redacting already-redacted
# text is always a no-op.
_REDACTION_PASSES = 3


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprint(obj: Any) -> str:
    """Return the SHA-256 hex digest of the canonical serialization of ``obj``."""
    return sha256_hex(dumps(obj).encode("utf-8"))


def _identity_key(basis: Any) -> str:
    """Return the bounded 32-hex identity key for ``basis``."""
    return _fingerprint(basis)[:32]


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


# -- privacy primitives --------------------------------------------------


def redact_text(text: str) -> str:
    """Return ``text`` with secret-like values replaced by the redaction marker.

    The replacement is deterministic and idempotent: redacting an already
    redacted string returns it unchanged.
    """
    if not isinstance(text, str):
        return text
    out = text
    for _ in range(_REDACTION_PASSES):
        before = out
        out = _PEM_RE.sub(REDACTION_MARKER, out)
        for pattern in _SECRET_TOKEN_RES:
            out = pattern.sub(REDACTION_MARKER, out)
        out = _KV_SECRET_RE.sub(
            lambda m: m.group(1) + m.group(2) + REDACTION_MARKER, out
        )
        if out == before:
            break
    return out


def _redact_counted(text: str) -> Tuple[str, int]:
    """Redact ``text`` and report how many replacements were made."""
    if not isinstance(text, str):
        return text, 0
    redacted = redact_text(text)
    count = 0 if redacted == text else 1
    return redacted, count


def bound_text(text: str, limit: int) -> Tuple[str, int]:
    """Bound ``text`` to ``limit`` characters; report 1 when truncated."""
    if not isinstance(text, str):
        return text, 0
    if len(text) <= limit:
        return text, 0
    return text[:limit], 1


def portable_path(value: str) -> str:
    """Return ``value`` with separators folded to ``/`` for portable matching."""
    return value.replace("\\", "/")


def path_excluded(path: Any) -> bool:
    """Return True when ``path`` is outside the persistence policy.

    An excluded path is one whose normalized form sits under an excluded
    segment, ends with an excluded suffix, or names an excluded file. The check
    is case-insensitive and separator-agnostic so it cannot be evaded by
    spelling.
    """
    if not isinstance(path, str) or not path.strip():
        return True
    norm = portable_path(path).strip().lower()
    while norm.startswith("./"):
        norm = norm[2:]
    for prefix in EXCLUDED_PATH_PREFIXES:
        if norm.startswith(prefix):
            return True
    segments = [s for s in norm.split("/") if s]
    if not segments:
        return True
    if any(seg in EXCLUDED_PATH_SEGMENTS for seg in segments[:-1]):
        return True
    name = segments[-1]
    if name in EXCLUDED_PATH_NAMES:
        return True
    if name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in EXCLUDED_PATH_SUFFIXES):
        return True
    return False


def _sanitize_path(value: Any) -> Tuple[Optional[str], int, int]:
    """Return ``(path, redactions, exclusions)`` for one candidate path.

    ``exclusions`` is 1 when the path is refused by policy (``path`` is then
    ``None``); ``redactions`` counts secret-like replacements inside it.
    """
    if not isinstance(value, str):
        return None, 0, 1
    redacted, count = _redact_counted(value)
    bounded, _ = bound_text(redacted, MAX_PATH_CHARS)
    if path_excluded(bounded):
        return None, count, 1
    return bounded, count, 0


class PrivacyPolicy:
    """Bounded, code-owned pre-storage privacy controls.

    Every limit is a constructor argument with a safe default so a test can
    tighten a bound without mutating module state. A policy is applied *before*
    a record is assembled, so nothing unredacted is ever fingerprinted, stored
    or attributed to a run.
    """

    __slots__ = (
        "max_events",
        "max_text_chars",
        "max_payload_bytes",
        "max_evidence_per_event",
        "max_decisions_per_event",
        "max_code_entities_per_event",
    )

    def __init__(
        self,
        max_events: int = MAX_EVENTS_PER_SESSION,
        max_text_chars: int = MAX_TEXT_CHARS,
        max_payload_bytes: int = MAX_PAYLOAD_BYTES,
        max_evidence_per_event: int = MAX_EVIDENCE_PER_EVENT,
        max_decisions_per_event: int = MAX_DECISIONS_PER_EVENT,
        max_code_entities_per_event: int = MAX_CODE_ENTITIES_PER_EVENT,
    ) -> None:
        self.max_events = max_events
        self.max_text_chars = max_text_chars
        self.max_payload_bytes = max_payload_bytes
        self.max_evidence_per_event = max_evidence_per_event
        self.max_decisions_per_event = max_decisions_per_event
        self.max_code_entities_per_event = max_code_entities_per_event


DEFAULT_POLICY = PrivacyPolicy()


def _counts(redactions: int = 0, truncations: int = 0, exclusions: int = 0) -> Dict[str, int]:
    """Return a bounded, content-free privacy accounting record."""
    return {
        "redactions": redactions,
        "truncations": truncations,
        "exclusions": exclusions,
    }


def _merge_counts(*records: Dict[str, int]) -> Dict[str, int]:
    total = _counts()
    for rec in records:
        for key in total:
            total[key] += rec.get(key, 0)
    return total


def _bound_payload(value: Any, policy: PrivacyPolicy, depth: int = 0) -> Tuple[Any, Dict[str, int]]:
    """Redact and bound an arbitrary payload; return ``(value, counts)``.

    Depth, key count and list length are bounded, every string is redacted and
    truncated, and any content-bearing key is dropped and counted. Content-
    bearing keys are refused even when empty so the rule has no spelling
    loophole.
    """
    counts = _counts()
    if depth > MAX_PAYLOAD_DEPTH:
        return None, _counts(truncations=1)
    if isinstance(value, str):
        redacted, redactions = _redact_counted(value)
        bounded, truncations = bound_text(redacted, policy.max_text_chars)
        return bounded, _counts(redactions=redactions, truncations=truncations)
    if isinstance(value, bool) or value is None:
        return value, counts
    if isinstance(value, (int, float)):
        return value, counts
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        truncations = 0
        for index, key in enumerate(sorted(value, key=lambda k: str(k))):
            if index >= MAX_PAYLOAD_KEYS:
                truncations = 1
                break
            if not isinstance(key, str):
                truncations = 1
                continue
            if key.lower() in CONTENT_BEARING_KEYS:
                counts["exclusions"] += 1
                continue
            child, child_counts = _bound_payload(value[key], policy, depth + 1)
            counts = _merge_counts(counts, child_counts)
            out[key] = child
        counts["truncations"] += truncations
        return out, counts
    if isinstance(value, (list, tuple)):
        out_list: List[Any] = []
        truncations = 0
        for index, item in enumerate(value):
            if index >= MAX_PAYLOAD_LIST:
                truncations = 1
                break
            child, child_counts = _bound_payload(item, policy, depth + 1)
            counts = _merge_counts(counts, child_counts)
            out_list.append(child)
        counts["truncations"] += truncations
        return out_list, counts
    # Any other type (set, bytes, custom object) is not modeled by the contract.
    return None, _counts(exclusions=1)


def _payload_oversized(payload: Any, policy: PrivacyPolicy) -> bool:
    """Return True when the redacted payload exceeds the bounded serialized size."""
    if payload is None:
        return False
    try:
        encoded = dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return True
    return len(encoded) > policy.max_payload_bytes


# -- identity ------------------------------------------------------------


def _token(value: Any) -> Optional[str]:
    """Return a bounded, filesystem- and id-safe token for ``value``.

    Any character outside ``[a-z0-9._-]`` folds to ``-`` and the result is
    lowercased and bounded, so a raw adapter or session string can never inject
    a separator or a path fragment into an identity.
    """
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    chars = []
    for char in lowered:
        if char.isascii() and char.isalnum():
            chars.append(char)
        elif char in "._-":
            chars.append(char)
        else:
            chars.append("-")
    token = re.sub(r"-{2,}", "-", "".join(chars)).strip(".-")
    token = token[:64]
    return token or None


def project_id_for(adapter: str, raw: Dict[str, Any]) -> str:
    """Return the Project id for an adapter-namespaced project descriptor."""
    adapter_tok = _token(adapter) or "unknown"
    source_id = raw.get("source_id")
    basis = {"adapter": adapter_tok, "source_id": source_id if isinstance(source_id, str) else None,
             "name": raw.get("name") if isinstance(raw.get("name"), str) else None}
    return f"project:{adapter_tok}:{_identity_key(basis)}"


def work_package_id_for(adapter: str, raw: Dict[str, Any]) -> str:
    """Return the WorkPackage id for an adapter-namespaced descriptor."""
    adapter_tok = _token(adapter) or "unknown"
    source_id = raw.get("source_id")
    basis = {"adapter": adapter_tok, "source_id": source_id if isinstance(source_id, str) else None,
             "title": raw.get("title") if isinstance(raw.get("title"), str) else None}
    return f"workpackage:{adapter_tok}:{_identity_key(basis)}"


def run_id_for(adapter: str, session_id: str, run_key: Any = None) -> Optional[str]:
    """Return the AgentRun id namespaced by adapter and session.

    Returns ``None`` when the adapter or session cannot produce a usable token,
    so an unidentified session is refused rather than silently colliding with
    another one.
    """
    adapter_tok = _token(adapter)
    session_tok = _token(session_id)
    if adapter_tok is None or session_tok is None:
        return None
    run_tok = _token(run_key) or "run"
    return f"run:{adapter_tok}:{session_tok}:{run_tok}"


# -- record identity helpers ---------------------------------------------


def event_id_for(run_id: str, raw: Dict[str, Any], payload: Any) -> str:
    """Return the AgentRunEvent id for one source event.

    Identity is resolved in strict preference order, exactly as the contract
    requires:

    1. a **stable source id** when the source supplies one — the id alone owns
       identity, so a redelivery carrying corrected content is recognized as
       the same identity and quarantined rather than accepted as a new event;
    2. otherwise a **stable source sequence**, which the source also owns;
    3. otherwise a **deterministic fingerprint** over the canonical non-secret
       fields ``{event_type, payload_fingerprint}``, computed over the already
       redacted payload.

    Because redaction runs before identity, a secret can never influence an
    identity and a redelivery always resolves to the same one.
    """
    raw_id = raw.get("source_event_id")
    source_event_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else None
    if source_event_id is not None:
        basis: Any = {"source_event_id": source_event_id}
    else:
        sequence = _int_or_none(raw.get("source_sequence"))
        if sequence is not None:
            basis = {"source_sequence": sequence}
        else:
            basis = {
                "event_type": raw.get("event_type"),
                "payload_fingerprint": _fingerprint(payload),
            }
    return f"event:{run_id}:{_identity_key(basis)}"


def change_set_id_for(run_id: str, raw: Dict[str, Any]) -> str:
    """Return the ChangeSet id for one change-set descriptor."""
    source_id = raw.get("source_id")
    basis = {
        "source_id": source_id if isinstance(source_id, str) else None,
        "paths": raw.get("paths"),
        "summary": raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        "entity_refs": raw.get("entity_refs"),
    }
    return f"changeset:{run_id}:{_identity_key(basis)}"


def evidence_id_for(run_id: str, raw: Dict[str, Any]) -> str:
    """Return the Evidence id for one evidence descriptor."""
    basis = {
        "kind": raw.get("kind"),
        "source_id": raw.get("source_id") if isinstance(raw.get("source_id"), str) else None,
        "artifact_ref": raw.get("artifact_ref"),
        "bytes": _int_or_none(raw.get("bytes")),
        "digest": raw.get("digest"),
    }
    return f"evidence:{run_id}:{_identity_key(basis)}"


def decision_id_for(run_id: str, raw: Dict[str, Any]) -> str:
    """Return the Decision id for one decision descriptor."""
    basis = {
        "source_id": raw.get("source_id") if isinstance(raw.get("source_id"), str) else None,
        "summary": raw.get("summary"),
        "decided_at": raw.get("decided_at"),
    }
    return f"decision:{run_id}:{_identity_key(basis)}"


def code_entity_link_id_for(run_id: str, raw: Dict[str, Any]) -> str:
    """Return the CodeEntityLink id for one code-entity descriptor."""
    basis = {
        "path": raw.get("path"),
        "symbol": raw.get("symbol"),
        "entity_kind": raw.get("entity_kind"),
    }
    return f"link:{run_id}:{_identity_key(basis)}"


def rejection_id_for(run_id: str, ordinal: int, reason: str) -> str:
    """Return the id of a fail-closed rejection record."""
    return f"rejection:{run_id}:{_identity_key([ordinal, reason])}"


def quarantine_id_for(run_id: str, ordinal: int, event_id: str) -> str:
    """Return the id of a quarantined conflicting-redelivery record."""
    return f"quarantine:{run_id}:{_identity_key([ordinal, event_id])}"


def _int_or_none(value: Any) -> Optional[int]:
    """Return ``value`` as an int when it is a genuine integer, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# -- transitions ---------------------------------------------------------

# Each supported event type declares the run states it may be applied from. A
# state absent from the tuple is an invalid transition and is refused without
# advancing the run.
#
# ``stream_ended`` is legal from every state (it only closes the bounded
# stream); every other type is refused from a terminal state, which makes every
# terminal state absorbing.
_OPEN_STATES = (RUN_UNSTARTED, RUN_OPEN)
_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    EVENT_RUN_STARTED: (RUN_UNSTARTED,),
    EVENT_RUN_PROGRESS: (RUN_OPEN,),
    EVENT_RUN_TERMINATED: _OPEN_STATES,
    EVENT_STREAM_ENDED: tuple(sorted(RUN_STATES)),
}


def _state_after(event_type: str, raw: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(next_state, reason)`` for an accepted event.

    Exactly one of the two is ``None``. An unrecognized ``run_terminated``
    outcome yields ``RUN_UNKNOWN_OUTCOME`` and an explicit reason.
    """
    if event_type == EVENT_RUN_STARTED:
        return RUN_OPEN, None
    if event_type == EVENT_RUN_PROGRESS:
        return RUN_OPEN, None
    if event_type == EVENT_STREAM_ENDED:
        return None, None  # closes the stream; the run state is unchanged
    outcome = raw.get("outcome")
    if not isinstance(outcome, str) or outcome not in TERMINAL_OUTCOMES:
        return RUN_UNKNOWN_OUTCOME, REASON_UNKNOWN_OUTCOME
    return TERMINAL_OUTCOMES[outcome], None


# -- store construction --------------------------------------------------


def empty_store(
    adapter: str,
    session_id: str,
    run_key: Any = None,
    project: Optional[Dict[str, Any]] = None,
    work_package: Optional[Dict[str, Any]] = None,
    policy: Optional[PrivacyPolicy] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return a new, empty memory store for one bounded source session.

    Returns ``(store, error)``; exactly one is ``None``. The store is created
    with ``state = unstarted``; :func:`finalize` resolves it.
    """
    policy = policy or DEFAULT_POLICY
    run_id = run_id_for(adapter, session_id, run_key)
    if run_id is None:
        if _token(adapter) is None:
            return None, REASON_SESSION_MISSING_ADAPTER
        return None, REASON_SESSION_MISSING_ID

    adapter_tok = _token(adapter) or "unknown"
    session_tok = _token(session_id) or "unknown"

    runs_project_id: Optional[str] = None
    projects: List[Dict[str, Any]] = []
    if isinstance(project, dict):
        normalized, counts = _normalize_descriptor(project, ("source_id", "name"), policy)
        runs_project_id = project_id_for(adapter, normalized)
        projects.append(
            {
                "id": runs_project_id,
                "record_kind": RECORD_PROJECT,
                "adapter": adapter_tok,
                "session_id": session_tok,
                "source_id": normalized.get("source_id"),
                "name": normalized.get("name"),
                "provenance": PROVENANCE_SOURCE,
                "privacy": counts,
            }
        )

    runs_work_package_id: Optional[str] = None
    work_packages: List[Dict[str, Any]] = []
    if isinstance(work_package, dict):
        normalized_wp, wp_counts = _normalize_descriptor(
            work_package, ("source_id", "title"), policy
        )
        runs_work_package_id = work_package_id_for(adapter, normalized_wp)
        work_packages.append(
            {
                "id": runs_work_package_id,
                "record_kind": RECORD_WORK_PACKAGE,
                "adapter": adapter_tok,
                "session_id": session_tok,
                "project_id": runs_project_id,
                "source_id": normalized_wp.get("source_id"),
                "title": normalized_wp.get("title"),
                "provenance": PROVENANCE_SOURCE,
                "privacy": wp_counts,
            }
        )

    store: Dict[str, Any] = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "generator": MEMORY_GENERATOR,
        "agent_run": {
            "id": run_id,
            "record_kind": RECORD_RUN,
            "adapter": adapter_tok,
            "session_id": session_tok,
            "project_id": runs_project_id,
            "work_package_id": runs_work_package_id,
            "state": RUN_UNSTARTED,
            "stream_closed": False,
            "finalized": False,
            "ingest_sequence": 0,
            "rejection_sequence": 0,
            "quarantine_sequence": 0,
            "event_ids": [],
            "first_source_timestamp": None,
            "last_source_timestamp": None,
            "terminal_event_id": None,
            "provenance": PROVENANCE_DERIVED,
            "privacy": _counts(),
        },
    }
    for array in STORE_ARRAYS:
        store[array] = []
    store["projects"] = projects
    store["work_packages"] = work_packages
    return store, None


def _normalize_descriptor(
    raw: Dict[str, Any], keys: Tuple[str, ...], policy: PrivacyPolicy
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Redact and bound the named string fields of a descriptor mapping."""
    out: Dict[str, Any] = {}
    counts = _counts()
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            redacted, redactions = _redact_counted(value)
            bounded, truncations = bound_text(redacted, policy.max_text_chars)
            out[key] = bounded
            counts = _merge_counts(counts, _counts(redactions, truncations))
        else:
            out[key] = None
    return out, counts


# -- per-event normalization ---------------------------------------------


def _normalize_change_set(
    run_id: str, raw: Any, policy: PrivacyPolicy
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
    """Normalize one change-set descriptor, applying path policy and bounds."""
    if not isinstance(raw, dict):
        return None, _counts(exclusions=1)
    counts = _counts()
    paths: List[str] = []
    raw_paths = raw.get("paths")
    if isinstance(raw_paths, list):
        for candidate in raw_paths[:MAX_PATHS_PER_CHANGE_SET]:
            path, redactions, exclusions = _sanitize_path(candidate)
            counts = _merge_counts(counts, _counts(redactions, 0, exclusions))
            if path is not None:
                paths.append(path)
    entity_refs: List[str] = []
    raw_refs = raw.get("entity_refs")
    if isinstance(raw_refs, list):
        for candidate in raw_refs[:MAX_PATHS_PER_CHANGE_SET]:
            if not isinstance(candidate, str):
                continue
            redacted, redactions = _redact_counted(candidate)
            bounded, truncations = bound_text(redacted, MAX_SYMBOL_CHARS)
            counts = _merge_counts(counts, _counts(redactions, truncations))
            entity_refs.append(bounded)
    summary = raw.get("summary")
    summary_text: Optional[str] = None
    if isinstance(summary, str):
        redacted, redactions = _redact_counted(summary)
        bounded, truncations = bound_text(redacted, policy.max_text_chars)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        summary_text = bounded
    source_id = raw.get("source_id")
    normalized = {
        "source_id": source_id if isinstance(source_id, str) else None,
        "paths": sorted(set(paths)),
        "entity_refs": sorted(set(entity_refs)),
        "summary": summary_text,
    }
    return {
        "id": change_set_id_for(run_id, normalized),
        "record_kind": RECORD_CHANGE_SET,
        "run_id": run_id,
        "source_id": normalized["source_id"],
        "paths": normalized["paths"],
        "entity_refs": normalized["entity_refs"],
        "summary": summary_text,
        "provenance": PROVENANCE_SOURCE,
        "privacy": counts,
    }, counts


def _normalize_evidence(
    run_id: str, raw: Any, policy: PrivacyPolicy
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
    """Normalize one evidence descriptor into bounded artifact *metadata*.

    The contract stores a reference to an artifact and its bounded metadata
    only. Any content-bearing field is dropped and counted, so transcript
    content is never durably stored by default. An artifact reference refused by
    path policy drops the whole record.
    """
    if not isinstance(raw, dict):
        return None, _counts(exclusions=1)
    counts = _counts()

    # Content-bearing fields are refused outright and counted.
    for key in sorted(raw):
        if isinstance(key, str) and key.lower() in CONTENT_BEARING_KEYS:
            counts["exclusions"] += 1

    kind = raw.get("kind")
    kind_text = kind if isinstance(kind, str) and kind.strip() else "artifact"
    kind_text, kind_trunc = bound_text(redact_text(kind_text), 64)
    counts = _merge_counts(counts, _counts(0, kind_trunc))

    ref_raw = raw.get("artifact_ref")
    artifact_ref: Optional[str] = None
    if isinstance(ref_raw, str) and ref_raw.strip():
        path, redactions, exclusions = _sanitize_path(ref_raw)
        counts = _merge_counts(counts, _counts(redactions, 0, exclusions))
        if path is None:
            return None, counts
        artifact_ref = path

    size = _int_or_none(raw.get("bytes"))
    if size is not None and (size < 0 or size > MAX_EVIDENCE_BYTES):
        size = None

    digest_raw = raw.get("digest")
    digest: Optional[str] = None
    if isinstance(digest_raw, str) and digest_raw.strip():
        redacted, redactions = _redact_counted(digest_raw)
        bounded, truncations = bound_text(redacted, MAX_REF_CHARS)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        digest = bounded

    source_id = raw.get("source_id")
    normalized = {
        "kind": kind_text,
        "source_id": source_id if isinstance(source_id, str) else None,
        "artifact_ref": artifact_ref,
        "bytes": size,
        "digest": digest,
    }
    return {
        "id": evidence_id_for(run_id, normalized),
        "record_kind": RECORD_EVIDENCE,
        "run_id": run_id,
        "kind": kind_text,
        "source_id": normalized["source_id"],
        "artifact_ref": artifact_ref,
        "bytes": size,
        "digest": digest,
        "provenance": PROVENANCE_SOURCE if artifact_ref else PROVENANCE_UNRESOLVED,
        "privacy": counts,
    }, counts


def _normalize_decision(
    run_id: str, raw: Any, policy: PrivacyPolicy
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
    """Normalize one decision descriptor with a bounded, redacted rationale."""
    if not isinstance(raw, dict):
        return None, _counts(exclusions=1)
    counts = _counts()
    summary_raw = raw.get("summary")
    rationale_raw = raw.get("rationale")
    decided_at_raw = raw.get("decided_at")
    summary: Optional[str] = None
    rationale: Optional[str] = None
    if isinstance(summary_raw, str):
        redacted, redactions = _redact_counted(summary_raw)
        bounded, truncations = bound_text(redacted, policy.max_text_chars)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        summary = bounded
    if isinstance(rationale_raw, str):
        redacted, redactions = _redact_counted(rationale_raw)
        bounded, truncations = bound_text(redacted, policy.max_text_chars)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        rationale = bounded
    decided_at: Optional[str] = None
    if isinstance(decided_at_raw, str):
        redacted, redactions = _redact_counted(decided_at_raw)
        bounded, truncations = bound_text(redacted, MAX_TIMESTAMP_CHARS)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        decided_at = bounded
    source_id = raw.get("source_id")
    normalized = {
        "source_id": source_id if isinstance(source_id, str) else None,
        "summary": summary,
        "decided_at": decided_at,
    }
    return {
        "id": decision_id_for(run_id, normalized),
        "record_kind": RECORD_DECISION,
        "run_id": run_id,
        "source_id": normalized["source_id"],
        "summary": summary,
        "rationale": rationale,
        "decided_at": decided_at,
        "provenance": PROVENANCE_SOURCE,
        "privacy": counts,
    }, counts


def _normalize_code_entity_link(
    run_id: str, raw: Any, policy: PrivacyPolicy
) -> Tuple[Optional[Dict[str, Any]], Dict[str, int]]:
    """Normalize one code-entity descriptor into a CodeEntityLink.

    A link refused by path policy is dropped: an excluded artifact must not be
    reachable from a memory store even indirectly.
    """
    if not isinstance(raw, dict):
        return None, _counts(exclusions=1)
    counts = _counts()
    path: Optional[str] = None
    path_raw = raw.get("path")
    if isinstance(path_raw, str) and path_raw.strip():
        path, redactions, exclusions = _sanitize_path(path_raw)
        counts = _merge_counts(counts, _counts(redactions, 0, exclusions))
        if path is None:
            return None, counts

    symbol: Optional[str] = None
    symbol_raw = raw.get("symbol")
    if isinstance(symbol_raw, str) and symbol_raw.strip():
        redacted, redactions = _redact_counted(symbol_raw)
        bounded, truncations = bound_text(redacted, MAX_SYMBOL_CHARS)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        symbol = bounded

    kind_raw = raw.get("entity_kind")
    if kind_raw == "symbol":
        entity_kind = "symbol"
    elif kind_raw == "file":
        entity_kind = "file"
    else:
        entity_kind = "symbol" if symbol else "file"

    if path is None and symbol is None:
        return None, _counts(exclusions=1)

    normalized = {"path": path, "symbol": symbol, "entity_kind": entity_kind}
    return {
        "id": code_entity_link_id_for(run_id, normalized),
        "record_kind": RECORD_CODE_ENTITY_LINK,
        "run_id": run_id,
        "path": path,
        "symbol": symbol,
        "entity_kind": entity_kind,
        "provenance": PROVENANCE_SOURCE,
        "privacy": counts,
    }, counts


def _normalize_source_id(value: Any) -> Tuple[Optional[str], Dict[str, int]]:
    """Redact and bound a source-supplied identifier for storage as evidence."""
    if not isinstance(value, str) or not value.strip():
        return None, _counts()
    redacted, redactions = _redact_counted(value)
    bounded, truncations = bound_text(redacted, MAX_SOURCE_ID_CHARS)
    return bounded, _counts(redactions, truncations)


# -- reducer -------------------------------------------------------------


def _normalize_children(
    store: Dict[str, Any], run_id: str, raw: Dict[str, Any], policy: PrivacyPolicy
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Normalize and attach one event's child records; return ``(ids, counts)``.

    Each child list is bounded by the policy, each child is redacted and
    path-policed by its own normalizer, and a child that policy refuses is
    simply absent from the event's id list.
    """
    counts = _counts()

    change_set_id: Optional[str] = None
    change_set_raw: Any = raw.get("change_set")
    if change_set_raw is None and isinstance(raw.get("paths"), list):
        change_set_raw = {"paths": raw.get("paths")}
    if isinstance(change_set_raw, dict):
        change_set, change_set_counts = _normalize_change_set(run_id, change_set_raw, policy)
        counts = _merge_counts(counts, change_set_counts)
        if change_set is not None:
            _append_unique(store, "change_sets", change_set)
            change_set_id = change_set["id"]

    children: Dict[str, Any] = {
        "change_set_id": change_set_id,
        "evidence_ids": [],
        "decision_ids": [],
        "code_entity_link_ids": [],
    }
    for record_key, source_key, array, limit, normalizer in (
        ("evidence_ids", "evidence", "evidence", policy.max_evidence_per_event,
         _normalize_evidence),
        ("decision_ids", "decisions", "decisions", policy.max_decisions_per_event,
         _normalize_decision),
        ("code_entity_link_ids", "code_entities", "code_entity_links",
         policy.max_code_entities_per_event, _normalize_code_entity_link),
    ):
        raw_children = raw.get(source_key)
        if not isinstance(raw_children, list):
            continue
        for candidate in raw_children[:limit]:
            record, child_counts = normalizer(run_id, candidate, policy)
            counts = _merge_counts(counts, child_counts)
            if record is not None:
                _append_unique(store, array, record)
                children[record_key].append(record["id"])

    return children, counts


def _append_unique(store: Dict[str, Any], array: str, record: Dict[str, Any]) -> None:
    """Append ``record`` to ``store[array]`` unless its id is already present."""
    target = store.setdefault(array, [])
    ident = record.get("id")
    if any(isinstance(existing, dict) and existing.get("id") == ident for existing in target):
        return
    target.append(record)


def _record_rejection(store: Dict[str, Any], reason: str, event_ref: Optional[str]) -> Dict[str, Any]:
    """Append a fail-closed rejection record that never carries source content.

    Rejections have their own monotonic ledger counter, so two malformed events
    that arrive before any event is accepted still produce two distinct records
    rather than collapsing onto one identity.
    """
    run = store["agent_run"]
    run["rejection_sequence"] = run.get("rejection_sequence", 0) + 1
    sequence = run["rejection_sequence"]
    record = {
        "id": rejection_id_for(run["id"], sequence, reason),
        "record_kind": RECORD_REJECTION,
        "run_id": run["id"],
        "reason": reason,
        "event_ref": event_ref,
        "rejection_ordinal": sequence,
        "ingest_sequence": run["ingest_sequence"],
        "provenance": PROVENANCE_UNRESOLVED,
        "privacy": _counts(),
    }
    _append_unique(store, "rejections", record)
    return record


def _record_quarantine(
    store: Dict[str, Any], event_id: str, existing_fingerprint: str, incoming_fingerprint: str
) -> Dict[str, Any]:
    """Append a quarantine record for a conflicting redelivery.

    Only fingerprints are recorded — never the incoming content — so a
    quarantined payload cannot leak through the quarantine record itself.
    """
    run = store["agent_run"]
    run["quarantine_sequence"] = run.get("quarantine_sequence", 0) + 1
    sequence = run["quarantine_sequence"]
    record = {
        "id": quarantine_id_for(run["id"], sequence, event_id),
        "record_kind": RECORD_QUARANTINE,
        "run_id": run["id"],
        "event_id": event_id,
        "existing_fingerprint": existing_fingerprint,
        "incoming_fingerprint": incoming_fingerprint,
        "reason": REASON_CONFLICTING_REDELIVERY,
        "quarantine_ordinal": sequence,
        "ingest_sequence": run["ingest_sequence"],
        "provenance": PROVENANCE_UNRESOLVED,
        "privacy": _counts(),
    }
    _append_unique(store, "quarantines", record)
    return record


def _event_content(raw: Dict[str, Any], payload: Any, change_set: Any, evidence: Any,
                   decisions: Any, links: Any) -> Dict[str, Any]:
    """Return the canonical content of an event, excluding ingest-only fields.

    The content fingerprint is computed over this projection, so two
    redeliveries that normalize identically are recognized as the same event
    and two that do not are recognized as a conflict.
    """
    return {
        "event_type": raw.get("event_type"),
        "outcome": raw.get("outcome") if isinstance(raw.get("outcome"), str) else None,
        "source_sequence": _int_or_none(raw.get("source_sequence")),
        "payload": payload,
        "change_set": change_set,
        "evidence": evidence,
        "decisions": decisions,
        "code_entity_links": links,
    }


def apply_event(
    store: Dict[str, Any],
    raw: Any,
    policy: Optional[PrivacyPolicy] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Apply one source event to ``store``; return ``(store, outcome)``.

    The reducer is the per-run single writer: it assigns the next monotonic
    ``ingest_ordinal`` to an accepted event, recognizes an identical redelivery
    as a no-op, quarantines a conflicting redelivery, and refuses every invalid
    transition, unsupported event type, unknown outcome and oversized payload
    with an explicit, fail-closed record.

    ``outcome`` is a bounded, content-free summary of what happened:
    ``{"status": ..., "event_id": ..., "reason": ...}``.
    """
    policy = policy or DEFAULT_POLICY
    run = store["agent_run"]

    # 1. Structural validation. A malformed event is refused before it can
    #    influence identity, ordering or state.
    if not isinstance(raw, dict):
        _record_rejection(store, REASON_MALFORMED_EVENT, None)
        return store, {"status": "rejected", "event_id": None, "reason": REASON_MALFORMED_EVENT}
    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        _record_rejection(store, REASON_MALFORMED_EVENT, None)
        return store, {"status": "rejected", "event_id": None, "reason": REASON_MALFORMED_EVENT}

    # 2. Redact and bound before anything is fingered, stored or attributed.
    raw_payload = raw.get("payload")
    if raw_payload is None:
        raw_payload = {}
    elif not isinstance(raw_payload, dict):
        # A non-mapping payload is preserved under a single bounded key rather
        # than being discarded when a message or command is also present.
        raw_payload = {"payload": raw_payload}
    payload, payload_counts = _bound_payload(raw_payload, policy)
    message_counts = _counts()
    for key in ("message", "command"):
        value = raw.get(key)
        if isinstance(value, str):
            redacted, redactions = _redact_counted(value)
            bounded, truncations = bound_text(redacted, policy.max_text_chars)
            payload[key] = bounded
            message_counts = _merge_counts(message_counts, _counts(redactions, truncations))
    counts = _merge_counts(payload_counts, message_counts)

    if _payload_oversized(payload, policy):
        _record_rejection(store, REASON_PAYLOAD_OVERSIZED, None)
        return store, {"status": "rejected", "event_id": None, "reason": REASON_PAYLOAD_OVERSIZED}

    event_id = event_id_for(run["id"], raw, payload)

    # 3. Idempotency and conflict detection run before any state transition, so
    #    the same identity can never advance a run twice.
    content = _event_content(raw, payload, raw.get("change_set"), raw.get("evidence"),
                             raw.get("decisions"), raw.get("code_entities"))
    content_fingerprint = _fingerprint(content)

    for existing in store.get("events", []):
        if existing.get("id") != event_id:
            continue
        if existing.get("content_fingerprint") == content_fingerprint:
            return store, {"status": "duplicate", "event_id": event_id, "reason": None}
        _record_quarantine(store, event_id, existing.get("content_fingerprint", ""),
                           content_fingerprint)
        return store, {
            "status": "quarantined",
            "event_id": event_id,
            "reason": REASON_CONFLICTING_REDELIVERY,
        }

    # 4. A finalized store is an immutable snapshot of one bounded session. A
    #    genuinely new event belongs to a new session, so it is refused rather
    #    than silently reopening a run that has already been reported.
    if run.get("finalized"):
        _record_rejection(store, REASON_ALREADY_FINALIZED, event_id)
        return store, {
            "status": "rejected",
            "event_id": event_id,
            "reason": REASON_ALREADY_FINALIZED,
        }

    # 5. Terminal states are absorbing: no event may move a run out of one.
    state = run.get("state", RUN_UNSTARTED)
    if state in TERMINAL_RUN_STATES and event_type != EVENT_STREAM_ENDED:
        _record_rejection(store, REASON_INVALID_TRANSITION, event_id)
        return store, {
            "status": "rejected",
            "event_id": event_id,
            "reason": REASON_INVALID_TRANSITION,
        }

    # 6. An unsupported event type is explicit and never success: it resolves a
    #    non-terminal run to ``unsupported``, which is absorbing.
    if event_type not in SUPPORTED_EVENT_TYPES:
        if state not in TERMINAL_RUN_STATES:
            run["state"] = RUN_UNSUPPORTED
            run["terminal_event_id"] = event_id
        _record_rejection(store, REASON_UNSUPPORTED_EVENT_TYPE, event_id)
        return store, {
            "status": "rejected",
            "event_id": event_id,
            "reason": REASON_UNSUPPORTED_EVENT_TYPE,
        }

    # 6b. A stream the source explicitly ended accepts nothing further.
    if run.get("stream_closed") and event_type != EVENT_STREAM_ENDED:
        _record_rejection(store, REASON_AFTER_STREAM_END, event_id)
        return store, {
            "status": "rejected",
            "event_id": event_id,
            "reason": REASON_AFTER_STREAM_END,
        }

    # 7. Typed transition validation.
    allowed = _TRANSITIONS.get(event_type, ())
    if state not in allowed:
        _record_rejection(store, REASON_INVALID_TRANSITION, event_id)
        return store, {
            "status": "rejected",
            "event_id": event_id,
            "reason": REASON_INVALID_TRANSITION,
        }

    next_state, transition_reason = _state_after(event_type, raw)

    # 8. Accept: assign the monotonic ingest ordinal, then normalize children.
    ordinal = run["ingest_sequence"] + 1
    run["ingest_sequence"] = ordinal

    children, child_counts = _normalize_children(store, run["id"], raw, policy)
    counts = _merge_counts(counts, child_counts)

    source_id, sid_counts = _normalize_source_id(raw.get("source_event_id"))
    counts = _merge_counts(counts, sid_counts)

    timestamp: Optional[str] = None
    raw_timestamp = raw.get("source_timestamp")
    if isinstance(raw_timestamp, str) and raw_timestamp.strip():
        redacted, redactions = _redact_counted(raw_timestamp)
        bounded, truncations = bound_text(redacted, MAX_TIMESTAMP_CHARS)
        counts = _merge_counts(counts, _counts(redactions, truncations))
        timestamp = bounded

    previous_state = state
    record = {
        "id": event_id,
        "record_kind": RECORD_EVENT,
        "run_id": run["id"],
        "event_type": event_type,
        "source_event_id": source_id,
        "source_sequence": _int_or_none(raw.get("source_sequence")),
        "source_timestamp": timestamp,
        "ingest_ordinal": ordinal,
        "content_fingerprint": content_fingerprint,
        "payload": payload,
        "change_set_id": children["change_set_id"],
        "evidence_ids": sorted(children["evidence_ids"]),
        "decision_ids": sorted(children["decision_ids"]),
        "code_entity_link_ids": sorted(children["code_entity_link_ids"]),
        "transition": {"from": previous_state, "to": next_state},
        "provenance": PROVENANCE_SOURCE,
        "privacy": counts,
    }
    _append_unique(store, "events", record)
    run["event_ids"] = [
        e["id"]
        for e in sorted(
            store["events"],
            key=lambda r: (r.get("ingest_ordinal", 0), r.get("id", "")),
        )
    ]

    if event_type == EVENT_STREAM_ENDED:
        run["stream_closed"] = True
    if next_state is not None:
        run["state"] = next_state
    if next_state in TERMINAL_RUN_STATES:
        run["terminal_event_id"] = event_id
    if transition_reason is not None:
        _record_rejection(store, transition_reason, event_id)

    if timestamp is not None:
        if run.get("first_source_timestamp") is None:
            run["first_source_timestamp"] = timestamp
        run["last_source_timestamp"] = timestamp

    run["privacy"] = _merge_counts(run.get("privacy", _counts()), counts)

    return store, {
        "status": "accepted",
        "event_id": event_id,
        "reason": transition_reason,
    }


def finalize(store: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a bounded stream to a reported terminal state and canonicalize.

    A run that is still ``unstarted`` or ``open`` when the bounded stream ends
    resolves to :data:`RUN_MISSING_TERMINAL`: the absence of a typed terminal
    transition is explicit and is never reported as success. Finalizing marks
    the store as a closed snapshot, so a later genuinely new event is refused
    (an identical redelivery stays an idempotent no-op and a conflicting
    redelivery stays a quarantine). Every array is sorted so the store is
    byte-stable across re-imports.

    Finalizing is idempotent: finalizing an already-finalized store returns an
    equal store.
    """
    run = store["agent_run"]
    if run.get("state") in (RUN_UNSTARTED, RUN_OPEN):
        run["state"] = RUN_MISSING_TERMINAL
    run["finalized"] = True

    for array in STORE_ARRAYS:
        records = store.get(array, [])
        if array == "events":
            store[array] = sorted(records, key=lambda r: (r.get("ingest_ordinal", 0), r.get("id", "")))
        else:
            store[array] = sorted(records, key=lambda r: str(r.get("id", "")))
    return store


def ingest_session(
    session: Any, policy: Optional[PrivacyPolicy] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[str], List[Dict[str, Any]]]:
    """Normalize and replay one bounded source session.

    Returns ``(store, error, outcomes)``. On a session-level failure ``store``
    is ``None`` and ``error`` is a bounded reason constant; otherwise ``store``
    is a finalized, canonical memory store and ``outcomes`` is the ordered list
    of per-event reducer outcomes.

    The bounded input is exhausted by definition, so the returned store is
    always finalized.
    """
    policy = policy or DEFAULT_POLICY
    if not isinstance(session, dict):
        return None, REASON_NOT_A_SESSION, []
    if _token(session.get("adapter")) is None:
        return None, REASON_SESSION_MISSING_ADAPTER, []
    if _token(session.get("session_id")) is None:
        return None, REASON_SESSION_MISSING_ID, []

    events = session.get("events")
    if events is None:
        events = []
    if not isinstance(events, list):
        return None, REASON_SESSION_EVENTS_NOT_LIST, []
    if len(events) > policy.max_events:
        return None, REASON_SESSION_EVENT_LIMIT, []

    store, error = empty_store(
        session.get("adapter"),
        session.get("session_id"),
        session.get("run_key"),
        session.get("project") if isinstance(session.get("project"), dict) else None,
        session.get("work_package") if isinstance(session.get("work_package"), dict) else None,
        policy,
    )
    if store is None:
        return None, error, []

    outcomes: List[Dict[str, Any]] = []
    for raw in events:
        _, outcome = apply_event(store, raw, policy)
        outcomes.append(outcome)

    return finalize(store), None, outcomes


# -- queries -------------------------------------------------------------


def run_state(store: Dict[str, Any]) -> str:
    """Return the reported run state of ``store``."""
    return str(store.get("agent_run", {}).get("state"))


def is_success(store: Dict[str, Any]) -> bool:
    """Return True only for a run whose validated terminal state is success."""
    return run_state(store) in SUCCESS_RUN_STATES


def event_ids(store: Dict[str, Any]) -> List[str]:
    """Return every logical event id in ingest order."""
    return [
        r["id"]
        for r in sorted(
            store.get("events", []),
            key=lambda r: (r.get("ingest_ordinal", 0), r.get("id", "")),
        )
        if isinstance(r, dict) and isinstance(r.get("id"), str)
    ]


def evidence_ids(store: Dict[str, Any]) -> List[str]:
    """Return every evidence id, sorted for determinism."""
    return sorted(
        r["id"] for r in store.get("evidence", []) if isinstance(r, dict) and r.get("id")
    )


def replay_result(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return the bounded, comparable replay result of ``store``.

    Two imports of the same source session must produce equal replay results;
    an additive migration must not change one.
    """
    run = store.get("agent_run", {})
    return {
        "run_id": run.get("id"),
        "state": run.get("state"),
        "ingest_sequence": run.get("ingest_sequence"),
        "event_ids": event_ids(store),
        "evidence_ids": evidence_ids(store),
        "change_set_ids": sorted(
            r["id"] for r in store.get("change_sets", []) if isinstance(r, dict) and r.get("id")
        ),
        "decision_ids": sorted(
            r["id"] for r in store.get("decisions", []) if isinstance(r, dict) and r.get("id")
        ),
        "code_entity_link_ids": sorted(
            r["id"] for r in store.get("code_entity_links", []) if isinstance(r, dict) and r.get("id")
        ),
        "rejection_reasons": sorted(
            str(r.get("reason")) for r in store.get("rejections", []) if isinstance(r, dict)
        ),
        "quarantine_ids": sorted(
            r["id"] for r in store.get("quarantines", []) if isinstance(r, dict) and r.get("id")
        ),
    }


# -- schema migration ----------------------------------------------------

# Additive migrations map an *older* ``schema_version`` to a function that
# upgrades a store dict without changing any identity, event count, evidence
# link or replay result. A store with a *future* (or unknown) version is never
# migrated and never overwritten.
#
# ``0.9.0`` is the pre-freeze draft of this contract, kept so the additive-
# migration path is proven by fixture rather than asserted. The draft predates
# the promotion of CodeEntityLink to a first-class record and the run-level
# privacy accounting block, so both are added here. The migration is purely
# additive: no existing record is touched.
def _migrate_0_9_0(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade a ``0.9.0`` draft store to ``1.0.0`` additively."""
    store = dict(raw)
    if not isinstance(store.get("code_entity_links"), list):
        store["code_entity_links"] = []
    run = store.get("agent_run")
    if isinstance(run, dict):
        run = dict(run)
        run.setdefault("privacy", _counts())
        store["agent_run"] = run
    store["schema_version"] = MEMORY_SCHEMA_VERSION
    return store


MIGRATIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "0.9.0": _migrate_0_9_0,
}


def migrate_memory(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and migrate a raw store to the current schema version.

    Returns ``(store, error)``: on success ``error`` is ``None``; on a
    future/unknown version, a malformed store, a failed migration or a migration
    that drifts identity, evidence or replay result, ``store`` is ``None`` and
    ``error`` is a bounded reason. The caller must retain the last valid store
    whenever this returns an error, so an invalid migration can never destroy a
    readable prior state.

    A successful migration is *verified*: the migrated store must preserve the
    event identities, evidence identities and terminal replay result of the raw
    store it came from. A migration that would change replay meaning returns an
    explicit blocker instead.
    """
    if not isinstance(raw, dict):
        return None, REASON_NOT_MAPPING
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, REASON_MISSING_VERSION
    try:
        current = _version_tuple(MEMORY_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, REASON_INVALID_VERSION

    if found == current:
        return raw, None
    if found > current:
        return None, REASON_FUTURE_VERSION
    if version not in MIGRATIONS:
        return None, REASON_NOT_MIGRATABLE

    before = _migration_snapshot(raw)
    try:
        migrated = MIGRATIONS[version](dict(raw))
    except Exception:
        return None, REASON_MIGRATION_FAILED

    after = _migration_snapshot(migrated)
    if after != before:
        return None, REASON_MIGRATION_DRIFT
    if not isinstance(migrated.get("schema_version"), str):
        return None, REASON_MIGRATION_FAILED
    return migrated, None


def _migration_snapshot(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return the replay-meaning snapshot a migration must preserve exactly."""
    run = store.get("agent_run")
    if not isinstance(run, dict):
        run = {}
    return {
        "run_id": run.get("id"),
        "state": run.get("state"),
        "ingest_sequence": run.get("ingest_sequence"),
        "event_ids": sorted(
            str(r.get("id")) for r in store.get("events", []) if isinstance(r, dict)
        ),
        "evidence_ids": sorted(
            str(r.get("id")) for r in store.get("evidence", []) if isinstance(r, dict)
        ),
        "decision_ids": sorted(
            str(r.get("id")) for r in store.get("decisions", []) if isinstance(r, dict)
        ),
        "change_set_ids": sorted(
            str(r.get("id")) for r in store.get("change_sets", []) if isinstance(r, dict)
        ),
    }


__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "MEMORY_GENERATOR",
    "RECORD_PROJECT",
    "RECORD_WORK_PACKAGE",
    "RECORD_RUN",
    "RECORD_EVENT",
    "RECORD_CHANGE_SET",
    "RECORD_EVIDENCE",
    "RECORD_DECISION",
    "RECORD_CODE_ENTITY_LINK",
    "RECORD_REJECTION",
    "RECORD_QUARANTINE",
    "RECORD_KINDS",
    "STORE_ARRAYS",
    "PROVENANCE_SOURCE",
    "PROVENANCE_DERIVED",
    "PROVENANCE_UNRESOLVED",
    "PROVENANCES",
    "SESSION_KEYS",
    "SOURCE_EVENT_KEYS",
    "EVENT_RUN_STARTED",
    "EVENT_RUN_PROGRESS",
    "EVENT_RUN_TERMINATED",
    "EVENT_STREAM_ENDED",
    "SUPPORTED_EVENT_TYPES",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_CANCELLED",
    "OUTCOME_BLOCKED",
    "TERMINAL_OUTCOMES",
    "RUN_UNSTARTED",
    "RUN_OPEN",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_CANCELLED",
    "RUN_BLOCKED",
    "RUN_MISSING_TERMINAL",
    "RUN_UNKNOWN_OUTCOME",
    "RUN_UNSUPPORTED",
    "REPORTED_RUN_STATES",
    "RUN_STATES",
    "TERMINAL_RUN_STATES",
    "SUCCESS_RUN_STATES",
    "REASON_NOT_MAPPING",
    "REASON_MISSING_VERSION",
    "REASON_INVALID_VERSION",
    "REASON_FUTURE_VERSION",
    "REASON_NOT_MIGRATABLE",
    "REASON_MIGRATION_FAILED",
    "REASON_MIGRATION_DRIFT",
    "REASON_NOT_A_SESSION",
    "REASON_SESSION_MISSING_ADAPTER",
    "REASON_SESSION_MISSING_ID",
    "REASON_SESSION_EVENTS_NOT_LIST",
    "REASON_SESSION_EVENT_LIMIT",
    "REASON_MALFORMED_EVENT",
    "REASON_UNSUPPORTED_EVENT_TYPE",
    "REASON_INVALID_TRANSITION",
    "REASON_UNKNOWN_OUTCOME",
    "REASON_AFTER_STREAM_END",
    "REASON_ALREADY_FINALIZED",
    "REASON_CONFLICTING_REDELIVERY",
    "REASON_PAYLOAD_OVERSIZED",
    "REDACTION_MARKER",
    "MAX_EVENTS_PER_SESSION",
    "MAX_TEXT_CHARS",
    "MAX_REF_CHARS",
    "MAX_PATH_CHARS",
    "MAX_SYMBOL_CHARS",
    "MAX_TIMESTAMP_CHARS",
    "MAX_SOURCE_ID_CHARS",
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_DEPTH",
    "MAX_PAYLOAD_KEYS",
    "MAX_PAYLOAD_LIST",
    "MAX_EVIDENCE_PER_EVENT",
    "MAX_DECISIONS_PER_EVENT",
    "MAX_CODE_ENTITIES_PER_EVENT",
    "MAX_PATHS_PER_CHANGE_SET",
    "MAX_EVIDENCE_BYTES",
    "EXCLUDED_PATH_SEGMENTS",
    "EXCLUDED_PATH_NAMES",
    "EXCLUDED_PATH_SUFFIXES",
    "EXCLUDED_PATH_PREFIXES",
    "CONTENT_BEARING_KEYS",
    "sha256_hex",
    "dumps",
    "redact_text",
    "bound_text",
    "portable_path",
    "path_excluded",
    "PrivacyPolicy",
    "DEFAULT_POLICY",
    "project_id_for",
    "work_package_id_for",
    "run_id_for",
    "event_id_for",
    "change_set_id_for",
    "evidence_id_for",
    "decision_id_for",
    "code_entity_link_id_for",
    "rejection_id_for",
    "quarantine_id_for",
    "empty_store",
    "apply_event",
    "finalize",
    "ingest_session",
    "run_state",
    "is_success",
    "event_ids",
    "evidence_ids",
    "replay_result",
    "MIGRATIONS",
    "migrate_memory",
]
