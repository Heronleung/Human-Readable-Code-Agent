"""The one shared live-gate ledger (AGENT-LOOP-LIVE-1).

A local, code-owned spend/call ledger for the approved live batch. It is **not**
a product store, not a product schema, not a migration and not app-data: it is
one JSON document under the user's state directory, outside the repository and
outside PrimaAgent's own application data, addressed by one trusted gate id.

Why it exists: a budget that is only checked by a test's assertions is not a
budget. Admission happens *before* dispatch — a reservation is written durably
first — so a crash, a lost response, an unknown outcome or an extra call cannot
reset the allowance.

Format version 2 adds bounded execution identity and one supplement claim to
the same authoritative document. A reservation may carry a run id, a case id
and a per-case request ordinal; a claim binds one run id to the fixed C–F case
set and to an operator-attested prior state. Accounting and identity therefore
cannot disagree: there is still one owner and one document.

Coordination is a separate concern from durability and from remote outcome.
Every mutating operation runs as one short transaction under a stable
coordination lock — a code-owned lock file beside the ledger, used only as a
mutex and holding no accounting authority. The lock serializes read-check-write
across processes; it does not stop an uncooperative writer, it is advisory on
POSIX and mandatory for other processes on Windows, and it is not held across a
backend start, a provider request or any wait for a child.

Everything is conservative in one direction only:

* input is reserved at the **worst-case byte bound** of the payload actually
  built, priced at peak cache-miss rates;
* output is reserved at the full requested ``max_tokens``;
* a request whose outcome is unknown keeps its worst-case reservation instead of
  releasing it — "we could not measure it" is never read as "it was free";
* a failed or timed-out **dispatched** request keeps its call slot;
* a reservation that is still in flight blocks the next one, and a dispatched
  request whose outcome was never observed blocks further dispatch until the
  state is explicitly resolved outside this module.

The US$0.05 guard is a *local* projected-spend guard. It is not a provider
billing guarantee and it cannot cap what the provider chooses to charge.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: The single trusted gate identity. Every session, case and worker request in
#: the approved batch shares it.
GATE_ID = "AGENT-LOOP-LIVE-1"

#: The ordinary ceiling. A fresh whole-batch run never exceeds nine.
MAX_REQUESTS = 9

#: The cumulative ceiling of the approved C–F supplement (original three plus
#: seven planned requests). It is never a caller-supplied budget: it applies
#: only to reservations bound to an open, persisted supplement claim.
SUPPLEMENT_MAX_REQUESTS = 10

#: The approved aggregate caps, unchanged by format version 2.
MAX_INPUT_TOKENS = 20_000
MAX_OUTPUT_TOKENS = 4_000
MAX_SPEND_USD = 0.05
#: Per request, in addition to the adapter's own smaller caps. The output
#: per-request bound is stated here too because the supplement plan check needs
#: the planned worst case, independent of the adapter's own constant.
MAX_INPUT_TOKENS_PER_REQUEST = 2_500
MAX_OUTPUT_TOKENS_PER_REQUEST = 500

#: The published peak cache-miss prices for the approved model, per 1M tokens,
#: in US$. Cache-hit and off-peak discounts are deliberately **not** assumed.
INPUT_USD_PER_MILLION = 0.30
OUTPUT_USD_PER_MILLION = 1.20

#: Persisted-format identity. Version 1 is a document without
#: ``format_version``/``claims``; version 2 adds the claim list.
FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1

#: Coordination. The lock file is stable for the life of the gate directory:
#: never deleted or replaced, because the operating-system lock is bound to the
#: open file and a replacement would silently create a second lock domain.
LOCK_NAME = GATE_ID + ".lock"
LOCK_WAIT_SECONDS = 0.5
LOCK_POLL_SECONDS = 0.01

#: Code-owned environment names. They transport the persisted run/case identity
#: from the runner to the backend; they are not authority — the gate looks the
#: run id up in the ledger and refuses an unknown one.
RUN_ID_ENV = "HRCA_LIVE_RUN_ID"
CASE_ID_ENV = "HRCA_LIVE_CASE_ID"

#: The one supplement vocabulary. Closed on purpose: no caller-supplied mode,
#: subset, budget or resume index exists.
SUPPLEMENT_KIND = "supplement-cf"
SUPPLEMENT_CASES: Tuple[str, ...] = ("C", "D", "E", "F")
SUPPLEMENT_PLANNED: Dict[str, int] = {"C": 2, "D": 3, "E": 1, "F": 1}

#: The two approved provenance modes for the historical prior state. They mean
#: different things and are never interchangeable:
#:
#: ``operator-legacy-ab-v1``
#:     The operator attests the historical A/B identity, the report's
#:     authenticity and exclusive use of the gate.
#: ``supplied-chat-transcript-ab-v1``
#:     The client approved reliance on a supplied transcript **copy** whose
#:     historical assertions remain unverified. The report digest identifies the
#:     copy's bytes only; it does not authenticate it or map old reservations to
#:     cases.
#:
#: There is no default mode. Callers must select one explicitly, and an
#: existing claim is never relabelled.
ATTESTATION_OPERATOR = "operator-legacy-ab-v1"
ATTESTATION_SUPPLIED_TRANSCRIPT = "supplied-chat-transcript-ab-v1"
SUPPLEMENT_ATTESTATIONS: Tuple[str, ...] = (
    ATTESTATION_OPERATOR,
    ATTESTATION_SUPPLIED_TRANSCRIPT,
)
#: Backward-compatible alias for the pre-existing operator mode only. It is not
#: a default for new callers.
SUPPLEMENT_ATTESTATION = ATTESTATION_OPERATOR
CLAIM_STATUSES = ("claimed", "in_progress", "completed", "stopped")
OPEN_CLAIM_STATUSES = ("claimed", "in_progress")

#: The supplied historical state this supplement is bound to. These are
#: structural checks on the ledger's own record, not proof that A and B ran.
#: The operator attestation and the exact digest carry that claim.
LEGACY_RESERVED = 3
LEGACY_LABELS: Tuple[str, ...] = ("agent-turn-1", "agent-turn-1", "agent-turn-2")
LEGACY_INPUT_CHARGED = 972
LEGACY_OUTPUT_CHARGED = 245
#: The stored spend is replayed from the log; this tolerance only absorbs the
#: last floating-point bit, never a rounding of the console's "$0.0006".
LEGACY_SPEND_TOLERANCE = 1e-12

#: Bounded refusal reasons.
REASON_REQUESTS = "the gate's request allowance is exhausted"
REASON_INPUT = "the gate's input-token allowance is exhausted"
REASON_OUTPUT = "the gate's generated-token allowance is exhausted"
REASON_SPEND = "the gate's projected-spend allowance is exhausted"
REASON_PER_REQUEST = "the request exceeds the per-request input allowance"
REASON_LEDGER = "the gate ledger is unreadable or its identity does not match"
REASON_IN_FLIGHT = "an earlier live request is still outstanding"
REASON_UNCERTAIN = "an earlier live request has an unobserved outcome; continuation is refused"
REASON_BUSY = "the gate's coordination lock wait expired"
REASON_CLAIM = "the run identity does not match an open supplement claim"
REASON_CLAIM_EXISTS = "a supplement claim already exists"
REASON_CLAIM_CLOSED = "the supplement claim is closed"
REASON_CLAIM_ORDER = "the supplement case order or request identity is invalid"
REASON_CLAIM_ACTIVE = "a supplement claim is open; an ordinary reservation is refused"
REASON_IDENTITY = "the ledger does not match the attested prior state"
REASON_ATTESTATION = "the attestation mode is not one of the approved modes"
REASON_PLAN = "the supplement plan does not fit the remaining allowance"
REASON_SETTLE = "the settlement conflicts with the recorded reservation"

#: One process-local mutex serializes threads in this interpreter; the
#: operating-system lock serializes processes. The gate is used by one thread
#: per process today, so this is a small safety net rather than a new model.
_PROCESS_LOCK = threading.RLock()


class GateRefused(Exception):
    """A bounded refusal. ``reason`` is safe to surface verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def price_usd(input_tokens: int, output_tokens: int) -> float:
    """Return the projected cost of ``input_tokens`` + ``output_tokens`` in US$."""
    return (
        input_tokens * INPUT_USD_PER_MILLION + output_tokens * OUTPUT_USD_PER_MILLION
    ) / 1_000_000.0


def _usable_usage(
    usage: Any, reserved_input: int, reserved_output: int
) -> Optional[Tuple[int, int]]:
    """Return the chargeable counters, or ``None`` when the report is unusable.

    A reported usage is usable only when both counters are present,
    non-negative integers. A missing, negative, non-integer or non-object
    report keeps the reservation's worst case: it never releases headroom.
    Values are clamped to the reservation, so an over-report can only charge
    the reserved maximum.
    """
    if not isinstance(usage, dict) or not usage.get("reported"):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, bool) or not isinstance(prompt, int) or prompt < 0:
        return None
    if isinstance(completion, bool) or not isinstance(completion, int) or completion < 0:
        return None
    return min(reserved_input, prompt), min(reserved_output, completion)


def default_base_dir() -> str:
    """Return the code-owned legacy ledger directory.

    Outside the repository and outside the platform application-data directory,
    so a gate can never be confused with a product store and a repository copy
    can never be mistaken for the authoritative ledger.
    """
    return os.path.join(os.path.expanduser("~"), ".local", "state", "hrca-live-gate")


def m5_l0_base_dir() -> str:
    """Return the independent M5-L0 evidence ledger directory.

    A different directory from the legacy gate, so neither ledger can be read
    or written as the other. It is still outside the repository and app-data.
    """
    return os.path.join(
        os.path.expanduser("~"), ".local", "state", "hrca-m5-l0-evidence-1"
    )


def m5_l0_rg_base_dir() -> str:
    """Return the independent M5-L0-RG evidence ledger directory.

    A third directory, distinct from the legacy and M5-L0 gates, so no RG run
    can read, continue or overwrite either earlier ledger. It is still outside
    the repository and app-data.
    """
    return os.path.join(
        os.path.expanduser("~"), ".local", "state", "hrca-m5-l0-rg-evidence-1"
    )


def _empty_state(gate_id: str = GATE_ID) -> Dict[str, Any]:
    return {
        "gate_id": gate_id,
        "format_version": FORMAT_VERSION,
        "requests_reserved": 0,
        "requests_committed": 0,
        "input_tokens_charged": 0,
        "output_tokens_charged": 0,
        "spend_usd_charged": 0.0,
        "log": [],
        "claims": [],
    }


#: Profile keys are code-owned command values, never a ledger identity.
PROFILE_LEGACY = "legacy"
PROFILE_M5_L0 = "m5-l0"
PROFILE_M5_L0_RG = "m5-l0-rg"

#: The independent M5-L0 evidence gate. It never shares the legacy ledger.
M5_L0_GATE_ID = "M5-L0-EVIDENCE-1"

#: The independent C/D-only RG evidence gate. It never shares either earlier
#: ledger and it has no supplement claim.
M5_L0_RG_GATE_ID = "M5-L0-RG-EVIDENCE-1"

#: The child environment names the selected profile. The runner sets it; the
#: boundary reads it; a missing value means the legacy profile.
GATE_PROFILE_ENV = "HRCA_AGENT_LIVE_GATE_PROFILE"

#: The new gate has no supplement claim; reserving with supplement identity is
#: refused rather than interpreted as an ordinary call.
REASON_SUPPLEMENT_UNSUPPORTED = "this gate profile has no supplement allowance"


@dataclass(frozen=True)
class GateProfile:
    """One gate identity, ledger location and aggregate admission envelope."""

    key: str
    gate_id: str
    base_dir: str
    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    max_spend_usd: float
    #: ``None`` means this profile has no supplement claim at all.
    supplement_max_requests: Optional[int]


#: Every existing caller uses the legacy profile unless it selects another one.
LEGACY_PROFILE = GateProfile(
    key=PROFILE_LEGACY,
    gate_id=GATE_ID,
    base_dir=default_base_dir(),
    max_requests=MAX_REQUESTS,
    max_input_tokens=MAX_INPUT_TOKENS,
    max_output_tokens=MAX_OUTPUT_TOKENS,
    max_spend_usd=MAX_SPEND_USD,
    supplement_max_requests=SUPPLEMENT_MAX_REQUESTS,
)

#: The approved M5-L0 evidence run: nine A-F calls, no supplement, and its own
#: 50k/10k/US$0.10 aggregate envelope and ledger.
M5_L0_PROFILE = GateProfile(
    key=PROFILE_M5_L0,
    gate_id=M5_L0_GATE_ID,
    base_dir=m5_l0_base_dir(),
    max_requests=9,
    max_input_tokens=50_000,
    max_output_tokens=10_000,
    max_spend_usd=0.10,
    supplement_max_requests=None,
)

#: The C/D-only RG evidence run: exactly five requests, a 30k/6k/US$0.05
#: aggregate envelope, its own ledger and no supplement. The per-request caps
#: above are unchanged, so this profile can never widen one request.
M5_L0_RG_PROFILE = GateProfile(
    key=PROFILE_M5_L0_RG,
    gate_id=M5_L0_RG_GATE_ID,
    base_dir=m5_l0_rg_base_dir(),
    max_requests=5,
    max_input_tokens=30_000,
    max_output_tokens=6_000,
    max_spend_usd=0.05,
    supplement_max_requests=None,
)

PROFILES: Dict[str, GateProfile] = {
    PROFILE_LEGACY: LEGACY_PROFILE,
    PROFILE_M5_L0: M5_L0_PROFILE,
    PROFILE_M5_L0_RG: M5_L0_RG_PROFILE,
}


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _is_run_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("run:"):
        return False
    body = value[4:]
    if len(body) != 32:
        return False
    return all(char in "0123456789abcdef" for char in body)


def _is_case_outcome(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 32:
        return False
    return all(char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in value)


def _is_stop_reason(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    return all(char in "abcdefghijklmnopqrstuvwxyz0123456789:_-" for char in value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) >= 0.0
    )


def canonical_bytes(state: Dict[str, Any]) -> bytes:
    """Return the canonical bytes of the ledger's *semantic* content.

    The digest deliberately excludes ``format_version`` and ``claims``: the
    historical state can be fingerprinted before a claim upgrades the document,
    and the same fingerprint still matches afterwards. Nothing here is a
    product protocol; it is an operator-attestation binding for the gate.
    """
    semantic = {
        "gate_id": state["gate_id"],
        "requests_reserved": state["requests_reserved"],
        "requests_committed": state["requests_committed"],
        "input_tokens_charged": state["input_tokens_charged"],
        "output_tokens_charged": state["output_tokens_charged"],
        "spend_usd_charged": state["spend_usd_charged"],
        "log": state["log"],
    }
    return json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


class LiveGate:
    """One gate profile and its ledger. Admission is a durable reservation.

    The default is the legacy ``AGENT-LOOP-LIVE-1`` profile, so every existing
    caller keeps its exact gate identity, path and limits. A caller that selects
    another profile gets an independent gate id, ledger directory and aggregate
    envelope; the two profiles never share a file.
    """

    def __init__(
        self,
        base_dir: Optional[str] = None,
        profile: Optional[GateProfile] = None,
    ) -> None:
        self.profile = profile if profile is not None else LEGACY_PROFILE
        if base_dir is not None:
            self.base_dir = base_dir
        elif self.profile.key == PROFILE_LEGACY:
            # Resolve the legacy default at construction time, preserving the
            # existing patchable ``default_base_dir`` seam.
            self.base_dir = default_base_dir()
        else:
            self.base_dir = self.profile.base_dir
        self.path = os.path.join(self.base_dir, self.profile.gate_id + ".json")
        #: The coordination artifact. It carries no accounting state and is
        #: never authoritative; it exists so cooperating writers can serialize
        #: one short read-check-write transaction at a time.
        self.lock_path = os.path.join(self.base_dir, self.profile.gate_id + ".lock")

    # The legacy profile resolves the module constants at use time, preserving
    # the existing test seam that patches MAX_SPEND_USD or
    # SUPPLEMENT_MAX_REQUESTS on the module. The M5-L0 profile is a fixed,
    # independent declaration and never follows those legacy constants.
    @property
    def _max_requests(self) -> int:
        return (
            MAX_REQUESTS
            if self.profile.key == PROFILE_LEGACY
            else self.profile.max_requests
        )

    @property
    def _max_input_tokens(self) -> int:
        return (
            MAX_INPUT_TOKENS
            if self.profile.key == PROFILE_LEGACY
            else self.profile.max_input_tokens
        )

    @property
    def _max_output_tokens(self) -> int:
        return (
            MAX_OUTPUT_TOKENS
            if self.profile.key == PROFILE_LEGACY
            else self.profile.max_output_tokens
        )

    @property
    def _max_spend_usd(self) -> float:
        return (
            MAX_SPEND_USD
            if self.profile.key == PROFILE_LEGACY
            else self.profile.max_spend_usd
        )

    @property
    def _supplement_max_requests(self) -> Optional[int]:
        return (
            SUPPLEMENT_MAX_REQUESTS
            if self.profile.key == PROFILE_LEGACY
            else self.profile.supplement_max_requests
        )

    # -- storage ----------------------------------------------------------
    def read(self) -> Dict[str, Any]:
        """Return the ledger, or a fresh one when this is the first admission.

        A ledger that exists but cannot be read, that carries another gate's
        identity or that does not match its declared format version is a
        refusal: an unreadable budget is not an empty one.
        """
        if not os.path.isfile(self.path):
            return _empty_state(self.profile.gate_id)
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            raise GateRefused(REASON_LEDGER)
        self._validate_state(state)
        return state

    def _validate_state(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("gate_id") != self.profile.gate_id:
            raise GateRefused(REASON_LEDGER)
        for key in (
            "requests_reserved",
            "requests_committed",
            "input_tokens_charged",
            "output_tokens_charged",
        ):
            if not _is_int(state.get(key)):
                raise GateRefused(REASON_LEDGER)
        if not _is_number(state.get("spend_usd_charged")):
            raise GateRefused(REASON_LEDGER)
        if not isinstance(state.get("log"), list):
            raise GateRefused(REASON_LEDGER)
        claims = state.get("claims", [])
        if not isinstance(claims, list) or len(claims) > 1:
            raise GateRefused(REASON_LEDGER)
        if "format_version" not in state:
            # Version 1 never carried claims.
            if claims:
                raise GateRefused(REASON_LEDGER)
        else:
            version = state.get("format_version")
            if not isinstance(version, int) or isinstance(version, bool):
                raise GateRefused(REASON_LEDGER)
            if version not in (LEGACY_FORMAT_VERSION, FORMAT_VERSION):
                raise GateRefused(REASON_LEDGER)
            if version == LEGACY_FORMAT_VERSION and claims:
                raise GateRefused(REASON_LEDGER)
        for claim in claims:
            self._validate_claim(claim)

    def _validate_claim(self, claim: Any) -> None:
        if not isinstance(claim, dict):
            raise GateRefused(REASON_LEDGER)
        if not _is_run_id(claim.get("run_id")):
            raise GateRefused(REASON_LEDGER)
        if claim.get("kind") != SUPPLEMENT_KIND:
            raise GateRefused(REASON_LEDGER)
        if claim.get("status") not in CLAIM_STATUSES:
            raise GateRefused(REASON_LEDGER)
        if claim.get("cases") != list(SUPPLEMENT_CASES):
            raise GateRefused(REASON_LEDGER)
        if claim.get("planned_max") != SUPPLEMENT_PLANNED:
            raise GateRefused(REASON_LEDGER)
        prior = claim.get("prior")
        if not isinstance(prior, dict):
            raise GateRefused(REASON_LEDGER)
        for key in (
            "requests_reserved",
            "requests_settled_known_usage",
            "requests_settled_unknown_usage",
            "requests_open",
            "input_tokens_charged",
            "output_tokens_charged",
        ):
            if not _is_int(prior.get(key)):
                raise GateRefused(REASON_LEDGER)
        if not _is_number(prior.get("spend_usd_charged")):
            raise GateRefused(REASON_LEDGER)
        if not _is_digest(claim.get("ledger_digest")):
            raise GateRefused(REASON_LEDGER)
        if not _is_digest(claim.get("report_digest")):
            raise GateRefused(REASON_LEDGER)
        if claim.get("attestation") not in SUPPLEMENT_ATTESTATIONS:
            raise GateRefused(REASON_LEDGER)
        stop_reason = claim.get("stop_reason", "")
        if not _is_stop_reason(stop_reason):
            raise GateRefused(REASON_LEDGER)
        if claim.get("status") == "stopped" and not stop_reason:
            raise GateRefused(REASON_LEDGER)
        if claim.get("status") != "stopped" and stop_reason:
            raise GateRefused(REASON_LEDGER)

    @staticmethod
    def _fsync_directory(path: str) -> None:
        """Make the rename durable where the platform supports it.

        POSIX filesystems need the directory entry flushed; the standard
        library offers no equivalent on Windows, so this is deliberately a
        no-op there. That is a documented limit, not a claimed guarantee.
        """
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        handle = os.open(path, flags)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)

    def _write(self, state: Dict[str, Any]) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=self.base_dir, prefix=".gate-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            # A flush/replace failure is deliberately not swallowed: the caller
            # must not assume "nothing was written" and it must never erase a
            # possibly committed claim in order to retry.
            self._fsync_directory(self.base_dir)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    # -- coordination -----------------------------------------------------
    @staticmethod
    def _lock_handle(handle: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _transaction(self, wait_deadline: Optional[float] = None):
        """Serialize one read-check-write transaction across processes.

        The wait is bounded by the caller's existing budget: a slow or stuck
        holder becomes a refusal, never an unbounded delay. The process-local
        and operating-system acquisitions share **one** absolute deadline, so a
        contended transaction can never restart a fresh allowance for its
        second stage. ``wait_deadline`` is an optional tighter absolute limit
        (monotonic seconds); the effective wait is the smaller of it and
        :data:`LOCK_WAIT_SECONDS`. The lock is never held across a child process
        or a provider request.
        """
        os.makedirs(self.base_dir, exist_ok=True)
        entry = time.monotonic()
        limit = LOCK_WAIT_SECONDS
        if wait_deadline is not None:
            limit = min(limit, float(wait_deadline) - entry)
        if limit <= 0.0:
            raise GateRefused(REASON_BUSY)
        deadline = entry + limit
        remaining = deadline - time.monotonic()
        if remaining <= 0.0 or not _PROCESS_LOCK.acquire(timeout=remaining):
            raise GateRefused(REASON_BUSY)
        handle = None
        try:
            handle = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(handle).st_size == 0:
                # Windows byte-range locking needs a byte to lock; the byte is
                # not state and is written before taking the lock.
                os.write(handle, b"\0")
                os.fsync(handle)
            while True:
                try:
                    self._lock_handle(handle)
                    break
                except OSError:
                    now = time.monotonic()
                    if now >= deadline:
                        raise GateRefused(REASON_BUSY)
                    time.sleep(min(LOCK_POLL_SECONDS, deadline - now))
            try:
                yield
            finally:
                try:
                    self._unlock_handle(handle)
                except OSError:
                    pass
        finally:
            if handle is not None:
                os.close(handle)
            _PROCESS_LOCK.release()

    # -- admission --------------------------------------------------------
    def reserve(
        self,
        *,
        input_upper: int,
        output_cap: int,
        label: str,
        run_id: str = "",
        case_id: str = "",
        request_index: int = 0,
        wait_deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Admit one request, or refuse it with a bounded reason.

        The worst case is written durably **before** the caller dispatches
        anything, so an interrupted batch cannot lose its accounting.

        The transaction makes the read-check-write atomic among cooperating
        writers, but it does not by itself keep inference sequential. The
        in-flight rule does: if an earlier reservation is still open, or a
        dispatched request's outcome was never observed, no new reservation is
        admitted. A known terminal settlement permits the next call.

        ``wait_deadline`` is an optional absolute monotonic limit for lock
        acquisition. The provider passes the turn deadline so admission can
        never wait past the turn; the post-admission allowance is recomputed
        against the same absolute deadline before dispatch.
        """
        if input_upper > MAX_INPUT_TOKENS_PER_REQUEST:
            raise GateRefused(REASON_PER_REQUEST)
        with self._transaction(wait_deadline=wait_deadline):
            state = self.read()
            ceiling = self._validate_reservation_identity(
                state, run_id, case_id, request_index
            )
            self._require_no_uncertainty(state)
            if int(state["requests_reserved"]) + 1 > ceiling:
                raise GateRefused(REASON_REQUESTS)
            if (
                int(state["input_tokens_charged"]) + input_upper
                > self._max_input_tokens
            ):
                raise GateRefused(REASON_INPUT)
            if (
                int(state["output_tokens_charged"]) + output_cap
                > self._max_output_tokens
            ):
                raise GateRefused(REASON_OUTPUT)
            projected = float(state["spend_usd_charged"]) + price_usd(input_upper, output_cap)
            if projected > self._max_spend_usd:
                raise GateRefused(REASON_SPEND)
            sequence = int(state["requests_reserved"]) + 1
            state["requests_reserved"] = sequence
            state["input_tokens_charged"] = int(state["input_tokens_charged"]) + input_upper
            state["output_tokens_charged"] = int(state["output_tokens_charged"]) + output_cap
            state["spend_usd_charged"] = projected
            state["log"].append(
                {
                    "seq": sequence,
                    "label": str(label),
                    "phase": "reserved",
                    "input_upper": int(input_upper),
                    "output_cap": int(output_cap),
                    "projected_usd": round(price_usd(input_upper, output_cap), 8),
                    "run_id": run_id,
                    "case_id": case_id,
                    "request_index": int(request_index),
                }
            )
            if run_id:
                claim = self._claim_for(state, run_id)
                if claim is not None and claim["status"] == "claimed":
                    claim["status"] = "in_progress"
            self._write(state)
        return {
            "seq": sequence,
            "input_upper": int(input_upper),
            "output_cap": int(output_cap),
            "run_id": run_id,
            "case_id": case_id,
            "request_index": int(request_index),
        }

    def _validate_reservation_identity(
        self,
        state: Dict[str, Any],
        run_id: str,
        case_id: str,
        request_index: int,
    ) -> int:
        """Return the ceiling this reservation is bound to, or refuse.

        The mode is derived from persisted owner state, never from a caller
        flag: only a reservation carrying the run id of an open supplement
        claim may use the cumulative ten-request ceiling. A reservation without
        a run id is ordinary and stays at nine.
        """
        if not isinstance(request_index, int) or isinstance(request_index, bool):
            raise GateRefused(REASON_CLAIM_ORDER)
        if self._supplement_max_requests is None and (
            run_id or case_id or request_index != 0
        ):
            raise GateRefused(REASON_SUPPLEMENT_UNSUPPORTED)
        if not run_id:
            if case_id or request_index != 0:
                raise GateRefused(REASON_CLAIM)
            if any(
                claim.get("status") in OPEN_CLAIM_STATUSES
                for claim in state.get("claims", [])
            ):
                raise GateRefused(REASON_CLAIM_ACTIVE)
            return self._max_requests
        if not _is_run_id(run_id) or case_id not in SUPPLEMENT_CASES:
            raise GateRefused(REASON_CLAIM)
        claim = self._claim_for(state, run_id)
        if claim is None:
            raise GateRefused(REASON_CLAIM)
        if claim["status"] not in OPEN_CLAIM_STATUSES:
            raise GateRefused(REASON_CLAIM_CLOSED)
        recorded = self._recorded_cases(state, run_id)
        expected = next(
            (key for key in SUPPLEMENT_CASES if key not in recorded), ""
        )
        if case_id != expected:
            raise GateRefused(REASON_CLAIM_ORDER)
        indices = [
            int(entry.get("request_index") or 0)
            for entry in state["log"]
            if isinstance(entry, dict)
            and entry.get("phase") == "reserved"
            and entry.get("run_id") == run_id
            and entry.get("case_id") == case_id
        ]
        expected_index = (max(indices) + 1) if indices else 1
        if request_index != expected_index:
            raise GateRefused(REASON_CLAIM_ORDER)
        if self._supplement_max_requests is None:  # defensive; checked above
            raise GateRefused(REASON_SUPPLEMENT_UNSUPPORTED)
        return self._supplement_max_requests

    def _require_no_uncertainty(self, state: Dict[str, Any]) -> None:
        settled_known = 0
        settled_unknown = 0
        uncertain = False
        for entry in state["log"]:
            if not isinstance(entry, dict) or entry.get("phase") != "settled":
                continue
            if entry.get("usage_reported"):
                settled_known += 1
            else:
                settled_unknown += 1
            if entry.get("observed", True) is False:
                uncertain = True
        open_count = max(
            0,
            int(state["requests_reserved"]) - settled_known - settled_unknown,
        )
        if open_count:
            raise GateRefused(REASON_IN_FLIGHT)
        if uncertain:
            raise GateRefused(REASON_UNCERTAIN)

    @staticmethod
    def _claim_for(state: Dict[str, Any], run_id: str) -> Optional[Dict[str, Any]]:
        for claim in state.get("claims", []):
            if claim.get("run_id") == run_id:
                return claim
        return None

    @staticmethod
    def _recorded_cases(state: Dict[str, Any], run_id: str) -> List[str]:
        return [
            str(entry.get("case_id"))
            for entry in state["log"]
            if isinstance(entry, dict)
            and entry.get("phase") == "case_result"
            and entry.get("run_id") == run_id
        ]

    # -- settlement -------------------------------------------------------
    def settle(
        self,
        reservation: Dict[str, Any],
        *,
        usage: Optional[Dict[str, Any]],
        outcome: str,
        dispatched: bool = True,
        observed: bool = True,
    ) -> Dict[str, Any]:
        """Replace one reservation with what actually happened.

        With reported usage the difference is released — never more than was
        reserved, so the charge can only go down. A report that is not a
        complete pair of non-negative integer counters is treated as
        unreported: it releases nothing and the worst case stands. Without any
        usage (a failure, a lost response, a crash, an unmeasurable answer) the
        worst case **stands** too: an unknown outcome is charged, not waived.

        ``observed=False`` is the explicit statement that no answer or outcome
        was observed after dispatch; that reservation blocks further dispatch.
        An answered request with unreported usage is ``observed=True``: the
        effect is known even though its counters are not.

        Settling the same reservation twice with the same facts is a no-op and
        never releases a charge twice. A conflicting second settlement refuses
        and changes nothing. A legitimate late settlement of an existing
        reservation is accepted after its claim closed; it never reopens the
        claim.
        """
        with self._transaction():
            state = self.read()
            try:
                sequence = int(reservation["seq"])
            except (KeyError, TypeError, ValueError):
                raise GateRefused(REASON_SETTLE)
            reserved = None
            existing = None
            for entry in state["log"]:
                if not isinstance(entry, dict):
                    continue
                if entry.get("phase") == "reserved" and entry.get("seq") == sequence:
                    reserved = entry
                elif entry.get("phase") == "settled" and entry.get("seq") == sequence:
                    existing = entry
            if reserved is None:
                raise GateRefused(REASON_SETTLE)
            reserved_input = int(reserved["input_upper"])
            reserved_output = int(reserved["output_cap"])
            charged = _usable_usage(usage, reserved_input, reserved_output)
            reported = charged is not None
            charged_input, charged_output = (
                charged if charged is not None else (reserved_input, reserved_output)
            )
            if existing is not None:
                same = (
                    existing.get("outcome") == str(outcome)
                    and bool(existing.get("usage_reported")) == reported
                    and int(existing.get("input_charged", -1)) == charged_input
                    and int(existing.get("output_charged", -1)) == charged_output
                    and bool(existing.get("dispatched", True)) == bool(dispatched)
                    and bool(existing.get("observed", True)) == bool(observed)
                )
                if same:
                    return state
                raise GateRefused(REASON_SETTLE)
            state["input_tokens_charged"] = max(
                0,
                int(state["input_tokens_charged"])
                - reserved_input
                + charged_input,
            )
            state["output_tokens_charged"] = max(
                0,
                int(state["output_tokens_charged"])
                - reserved_output
                + charged_output,
            )
            state["spend_usd_charged"] = max(
                0.0,
                float(state["spend_usd_charged"])
                - price_usd(reserved_input, reserved_output)
                + price_usd(charged_input, charged_output),
            )
            state["requests_committed"] = int(state["requests_committed"]) + 1
            state["log"].append(
                {
                    "seq": sequence,
                    "phase": "settled",
                    "run_id": reserved.get("run_id", ""),
                    "case_id": reserved.get("case_id", ""),
                    "outcome": str(outcome),
                    "usage_reported": reported,
                    "dispatched": bool(dispatched),
                    "observed": bool(observed),
                    "input_charged": charged_input,
                    "output_charged": charged_output,
                    "spend_usd": round(price_usd(charged_input, charged_output), 8),
                }
            )
            self._write(state)
            return state

    # -- supplement claim -------------------------------------------------
    def claim_supplement(
        self,
        *,
        ledger_digest: str,
        report_digest: str,
        attestation: str,
    ) -> Dict[str, Any]:
        """Persist the one C–F supplement claim, before any backend exists.

        This is the only operation allowed to upgrade a legacy document. It
        validates the exact prior record (counters, structural sequence,
        labels, charged totals and a replay of the stored spend), compares the
        canonical digest the operator supplies, checks that the seven planned
        requests fit the unchanged aggregate caps, and only then writes one
        claim. Any earlier claim of any status blocks a second attempt.

        ``attestation`` selects the meaning of the historical evidence and is
        required explicitly: ``operator-legacy-ab-v1`` (operator-attested) or
        ``supplied-chat-transcript-ab-v1`` (client-approved transcript copy with
        unverified historical assertions). There is no default, and an existing
        claim is never relabelled.
        """
        if self._supplement_max_requests is None:
            raise GateRefused(REASON_SUPPLEMENT_UNSUPPORTED)
        if attestation not in SUPPLEMENT_ATTESTATIONS:
            raise GateRefused(REASON_ATTESTATION)
        if not _is_digest(ledger_digest) or not _is_digest(report_digest):
            raise GateRefused(REASON_IDENTITY)
        with self._transaction():
            state = self.read()
            if state.get("claims"):
                raise GateRefused(REASON_CLAIM_EXISTS)
            prior = self._validate_legacy_prior(state)
            current_digest = hashlib.sha256(canonical_bytes(state)).hexdigest()
            if current_digest != ledger_digest:
                raise GateRefused(REASON_IDENTITY)
            self._validate_supplement_plan(state)
            run_id = "run:" + uuid.uuid4().hex
            claim = {
                "run_id": run_id,
                "kind": SUPPLEMENT_KIND,
                "status": "claimed",
                "cases": list(SUPPLEMENT_CASES),
                "planned_max": dict(SUPPLEMENT_PLANNED),
                "prior": prior,
                "ledger_digest": ledger_digest,
                "report_digest": report_digest,
                "attestation": attestation,
                "stop_reason": "",
            }
            state.setdefault("claims", []).append(claim)
            state["format_version"] = FORMAT_VERSION
            self._write(state)
            return dict(claim)

    def _validate_legacy_prior(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Validate the supplied historical record structurally.

        What this proves: the ledger holds exactly three reservations, in the
        expected label sequence, each settled once with known usage, with the
        expected charged token totals and an internally consistent stored
        spend. What it cannot prove: that those reservations belong to A and B.
        That is the operator's explicit attestation.
        """
        reserved = [
            entry
            for entry in state["log"]
            if isinstance(entry, dict) and entry.get("phase") == "reserved"
        ]
        settled = [
            entry
            for entry in state["log"]
            if isinstance(entry, dict) and entry.get("phase") == "settled"
        ]
        case_results = [
            entry
            for entry in state["log"]
            if isinstance(entry, dict) and entry.get("phase") == "case_result"
        ]
        if case_results:
            raise GateRefused(REASON_IDENTITY)
        if len(reserved) != LEGACY_RESERVED or len(settled) != LEGACY_RESERVED:
            raise GateRefused(REASON_IDENTITY)
        if [int(entry.get("seq") or 0) for entry in reserved] != [1, 2, 3]:
            raise GateRefused(REASON_IDENTITY)
        if tuple(entry.get("label") for entry in reserved) != LEGACY_LABELS:
            raise GateRefused(REASON_IDENTITY)
        if sorted(int(entry.get("seq") or 0) for entry in settled) != [1, 2, 3]:
            raise GateRefused(REASON_IDENTITY)
        if any(not entry.get("usage_reported") for entry in settled):
            raise GateRefused(REASON_IDENTITY)
        if any(entry.get("observed", True) is False for entry in settled):
            raise GateRefused(REASON_IDENTITY)
        if int(state["requests_reserved"]) != LEGACY_RESERVED:
            raise GateRefused(REASON_IDENTITY)
        if int(state["requests_committed"]) != LEGACY_RESERVED:
            raise GateRefused(REASON_IDENTITY)
        if int(state["input_tokens_charged"]) != LEGACY_INPUT_CHARGED:
            raise GateRefused(REASON_IDENTITY)
        if int(state["output_tokens_charged"]) != LEGACY_OUTPUT_CHARGED:
            raise GateRefused(REASON_IDENTITY)
        if sum(int(entry.get("input_charged") or 0) for entry in settled) != LEGACY_INPUT_CHARGED:
            raise GateRefused(REASON_IDENTITY)
        if sum(int(entry.get("output_charged") or 0) for entry in settled) != LEGACY_OUTPUT_CHARGED:
            raise GateRefused(REASON_IDENTITY)
        replayed = self._replay_spend(state)
        if replayed is None:
            raise GateRefused(REASON_IDENTITY)
        stored = float(state["spend_usd_charged"])
        if abs(replayed - stored) > LEGACY_SPEND_TOLERANCE:
            raise GateRefused(REASON_IDENTITY)
        settled_known = sum(1 for entry in settled if entry.get("usage_reported"))
        settled_unknown = len(settled) - settled_known
        return {
            "requests_reserved": int(state["requests_reserved"]),
            "requests_settled_known_usage": settled_known,
            "requests_settled_unknown_usage": settled_unknown,
            "requests_open": max(
                0, int(state["requests_reserved"]) - settled_known - settled_unknown
            ),
            "input_tokens_charged": int(state["input_tokens_charged"]),
            "output_tokens_charged": int(state["output_tokens_charged"]),
            "spend_usd_charged": stored,
        }

    @staticmethod
    def _replay_spend(state: Dict[str, Any]) -> Optional[float]:
        spend = 0.0
        reservations: Dict[int, Dict[str, Any]] = {}
        for entry in state["log"]:
            if not isinstance(entry, dict):
                return None
            phase = entry.get("phase")
            if phase == "reserved":
                try:
                    sequence = int(entry["seq"])
                    input_upper = int(entry["input_upper"])
                    output_cap = int(entry["output_cap"])
                except (KeyError, TypeError, ValueError):
                    return None
                reservations[sequence] = entry
                spend += price_usd(input_upper, output_cap)
            elif phase == "settled":
                try:
                    sequence = int(entry["seq"])
                    charged_input = int(entry["input_charged"])
                    charged_output = int(entry["output_charged"])
                except (KeyError, TypeError, ValueError):
                    return None
                reserved = reservations.get(sequence)
                if reserved is None:
                    return None
                spend = max(
                    0.0,
                    spend
                    - price_usd(int(reserved["input_upper"]), int(reserved["output_cap"]))
                    + price_usd(charged_input, charged_output),
                )
        return spend

    @staticmethod
    def _validate_supplement_plan(state: Dict[str, Any]) -> None:
        planned_requests = sum(SUPPLEMENT_PLANNED.values())
        planned_input = planned_requests * MAX_INPUT_TOKENS_PER_REQUEST
        planned_output = planned_requests * MAX_OUTPUT_TOKENS_PER_REQUEST
        if int(state["requests_reserved"]) + planned_requests > SUPPLEMENT_MAX_REQUESTS:
            raise GateRefused(REASON_PLAN)
        if int(state["input_tokens_charged"]) + planned_input > MAX_INPUT_TOKENS:
            raise GateRefused(REASON_PLAN)
        if int(state["output_tokens_charged"]) + planned_output > MAX_OUTPUT_TOKENS:
            raise GateRefused(REASON_PLAN)
        if (
            float(state["spend_usd_charged"])
            + price_usd(planned_input, planned_output)
            > MAX_SPEND_USD
        ):
            raise GateRefused(REASON_PLAN)

    def record_case(self, run_id: str, case_id: str, outcome: str) -> Dict[str, Any]:
        """Append one bounded case result to the claim's log.

        A case result is not a reservation or a settlement: it changes no
        counter. It must arrive for the first case without a result, in the
        fixed C–F order, so a duplicate or out-of-order call refuses.
        """
        if not _is_case_outcome(outcome):
            raise GateRefused(REASON_CLAIM_ORDER)
        with self._transaction():
            state = self.read()
            claim = self._claim_for(state, run_id) if _is_run_id(run_id) else None
            if claim is None:
                raise GateRefused(REASON_CLAIM)
            if claim["status"] not in OPEN_CLAIM_STATUSES:
                raise GateRefused(REASON_CLAIM_CLOSED)
            if case_id not in SUPPLEMENT_CASES:
                raise GateRefused(REASON_CLAIM_ORDER)
            recorded = self._recorded_cases(state, run_id)
            expected = next(
                (key for key in SUPPLEMENT_CASES if key not in recorded), ""
            )
            if case_id != expected:
                raise GateRefused(REASON_CLAIM_ORDER)
            state["log"].append(
                {
                    "phase": "case_result",
                    "seq": int(state["requests_reserved"]),
                    "run_id": run_id,
                    "case_id": case_id,
                    "outcome": outcome,
                }
            )
            if claim["status"] == "claimed":
                claim["status"] = "in_progress"
            self._write(state)
            return dict(claim)

    def close_claim(self, run_id: str, status: str, reason: str = "") -> Dict[str, Any]:
        """Close the one supplement claim. Terminal states never reopen."""
        if status not in ("completed", "stopped"):
            raise GateRefused(REASON_CLAIM)
        if status == "stopped" and not _is_stop_reason(reason):
            raise GateRefused(REASON_CLAIM)
        if status == "completed" and reason:
            raise GateRefused(REASON_CLAIM)
        with self._transaction():
            state = self.read()
            claim = self._claim_for(state, run_id) if _is_run_id(run_id) else None
            if claim is None:
                raise GateRefused(REASON_CLAIM)
            if claim["status"] not in OPEN_CLAIM_STATUSES:
                raise GateRefused(REASON_CLAIM_CLOSED)
            if status == "completed":
                recorded = self._recorded_cases(state, run_id)
                if recorded != list(SUPPLEMENT_CASES):
                    raise GateRefused(REASON_CLAIM_ORDER)
            claim["status"] = status
            claim["stop_reason"] = reason if status == "stopped" else ""
            self._write(state)
            return dict(claim)

    # -- reporting --------------------------------------------------------
    def canonical_digest(self) -> str:
        """Return the read-only canonical fingerprint of the current ledger."""
        return hashlib.sha256(canonical_bytes(self.read())).hexdigest()

    def remaining(self) -> Dict[str, Any]:
        """Return what is left, computed from the authoritative ledger.

        Read-only and additive: every key this summary has always returned is
        still returned unchanged, so an existing caller is unaffected. The
        distinction it adds is the one an operator cannot otherwise make —
        between reservations whose outcome the provider's own usage settled and
        reservations which stand at their **conservative worst case**.

        What the numbers mean, exactly:

        * ``requests_settled_known_usage`` — a reservation replaced by usage the
          provider reported. The *response* may still have been unusable (empty
          content, a non-complete finish reason); only the accounting is known.
        * ``requests_settled_unknown_usage`` — settled with no usage. The worst
          case is retained, and it stays retained.
        * ``requests_open`` — reserved and never settled: an in-flight request,
          a crash, or a failure before the child was spawned.
        * ``requests_uncertain`` — settled after dispatch with no observed
          outcome. The worst case is retained and further dispatch is refused
          until a human resolves the state outside this module.
        * ``*_charged`` are the conservative totals the gate is holding. They are
          **not** evidence that the provider dispatched, completed or billed
          anything: a reservation bounds what this side may have spent, and the
          actual cost is whatever the provider bills, which this side cannot see.

        ``requests_remaining`` is the selected profile's request headroom
        (``profile.max_requests - requests_reserved``); it is not the supplement
        ceiling.
        ``claims_open`` and ``claims_total`` expose the persisted claim state,
        and ``requests_uncertain`` counts dispatched requests whose outcome was
        never observed.
        """
        state = self.read()
        spend = float(state["spend_usd_charged"])
        log = state.get("log") if isinstance(state.get("log"), list) else []
        settled_known = 0
        settled_unknown = 0
        uncertain = 0
        for entry in log:
            if not isinstance(entry, dict) or entry.get("phase") != "settled":
                continue
            if entry.get("usage_reported"):
                settled_known += 1
            else:
                settled_unknown += 1
            if entry.get("observed", True) is False:
                uncertain += 1
        reserved = int(state["requests_reserved"])
        claims = state.get("claims") if isinstance(state.get("claims"), list) else []
        version = state.get("format_version", LEGACY_FORMAT_VERSION)
        return {
            "gate_id": self.profile.gate_id,
            "format_version": version,
            "requests_reserved": reserved,
            "requests_committed": int(state["requests_committed"]),
            "requests_remaining": self._max_requests - reserved,
            "requests_settled_known_usage": settled_known,
            "requests_settled_unknown_usage": settled_unknown,
            "requests_open": max(0, reserved - settled_known - settled_unknown),
            "requests_uncertain": uncertain,
            "claims_total": len(claims),
            "claims_open": sum(
                1 for claim in claims if claim.get("status") in OPEN_CLAIM_STATUSES
            ),
            "input_tokens_charged": int(state["input_tokens_charged"]),
            "input_tokens_remaining": (
                self._max_input_tokens - int(state["input_tokens_charged"])
            ),
            "output_tokens_charged": int(state["output_tokens_charged"]),
            "output_tokens_remaining": (
                self._max_output_tokens - int(state["output_tokens_charged"])
            ),
            "spend_usd_charged": round(spend, 8),
            "spend_usd_remaining": round(self._max_spend_usd - spend, 8),
        }


__all__ = [
    "GATE_ID",
    "M5_L0_GATE_ID",
    "M5_L0_RG_GATE_ID",
    "PROFILE_LEGACY",
    "PROFILE_M5_L0",
    "PROFILE_M5_L0_RG",
    "GATE_PROFILE_ENV",
    "REASON_SUPPLEMENT_UNSUPPORTED",
    "GateProfile",
    "LEGACY_PROFILE",
    "M5_L0_PROFILE",
    "M5_L0_RG_PROFILE",
    "PROFILES",
    "default_base_dir",
    "m5_l0_base_dir",
    "m5_l0_rg_base_dir",
    "MAX_REQUESTS",
    "SUPPLEMENT_MAX_REQUESTS",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_SPEND_USD",
    "MAX_INPUT_TOKENS_PER_REQUEST",
    "MAX_OUTPUT_TOKENS_PER_REQUEST",
    "INPUT_USD_PER_MILLION",
    "OUTPUT_USD_PER_MILLION",
    "FORMAT_VERSION",
    "LEGACY_FORMAT_VERSION",
    "LOCK_NAME",
    "LOCK_WAIT_SECONDS",
    "RUN_ID_ENV",
    "CASE_ID_ENV",
    "SUPPLEMENT_KIND",
    "SUPPLEMENT_CASES",
    "SUPPLEMENT_PLANNED",
    "ATTESTATION_OPERATOR",
    "ATTESTATION_SUPPLIED_TRANSCRIPT",
    "SUPPLEMENT_ATTESTATIONS",
    "SUPPLEMENT_ATTESTATION",
    "CLAIM_STATUSES",
    "OPEN_CLAIM_STATUSES",
    "LEGACY_RESERVED",
    "LEGACY_LABELS",
    "LEGACY_INPUT_CHARGED",
    "LEGACY_OUTPUT_CHARGED",
    "LEGACY_SPEND_TOLERANCE",
    "REASON_REQUESTS",
    "REASON_INPUT",
    "REASON_OUTPUT",
    "REASON_SPEND",
    "REASON_PER_REQUEST",
    "REASON_LEDGER",
    "REASON_IN_FLIGHT",
    "REASON_UNCERTAIN",
    "REASON_BUSY",
    "REASON_CLAIM",
    "REASON_CLAIM_EXISTS",
    "REASON_CLAIM_CLOSED",
    "REASON_CLAIM_ORDER",
    "REASON_CLAIM_ACTIVE",
    "REASON_IDENTITY",
    "REASON_ATTESTATION",
    "REASON_PLAN",
    "REASON_SETTLE",
    "GateRefused",
    "LiveGate",
    "canonical_bytes",
    "default_base_dir",
    "price_usd",
]
