"""The parent side of the dedicated provider worker (LANGGRAPH-LOOP-1D).

This module owns two things: a **reusable bounded child runner** (used here and
by the runtime initializer) and the provider that runs one model call in a child
process the parent can stop.

The deadline a call receives is not its own: it is whatever the shared turn
budget has left (:mod:`hrca.agent.budget`). A call with no positive allowance is
**not dispatched** — that is a refusal, raised as
:class:`~hrca.agent.budget.TurnBudgetExhausted`, not an uncertain outcome. A
call that was dispatched and then did not return in time is a
:class:`WorkerError` with :data:`CODE_DEADLINE`, which the executor records as
``unknown``.

What is guaranteed: bounded startup, bounded request, bounded wait, bounded
cleanup, and no late output accepted. What is **not** claimed: that a remote
effect was undone, or that a request was billed exactly once.

The argv is a fixed code-owned list. No shell, no caller-supplied command, no
project root, no credential and no executable payload crosses this boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from ..integrations import deepseek_live
from ..integrations import live_gate as live_gate_mod
from ..integrations import provider as provider_mod
from . import budget as budget_mod

#: Hard, code-owned bounds. Never caller-tunable wider than these.
WORKER_DEADLINE_SECONDS = budget_mod.WORKER_CALL_CAP_SECONDS
WORKER_CLEANUP_SECONDS = budget_mod.WORKER_CLEANUP_SECONDS
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 256 * 1024

#: The environment values a Python child needs to start. Everything else —
#: API keys, tokens, tracing flags — is dropped rather than inherited.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SystemRoot",
    "SYSTEMROOT",
    "PATHEXT",
    "COMSPEC",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "NUMBER_OF_PROCESSORS",
    # A task-owned bytecode prefix, when the caller sets one, must reach the
    # child: it is how a fresh-install-like import is reproduced without
    # touching any installed cache.
    "PYTHONPYCACHEPREFIX",
)

CODE_DEADLINE = "worker_deadline_exceeded"
CODE_CRASHED = "worker_failed"
CODE_BAD_OUTPUT = "worker_output_invalid"
CODE_OVERSIZED = "worker_request_oversized"
#: A reservation that admission time made undispatachable. It is a local
#: non-dispatch, never an observed attempt; the reservation keeps its worst
#: case and is settled with ``dispatched=False``.
CODE_TURN_BUDGET = "turn_budget_exhausted"


class WorkerError(Exception):
    """A bounded worker failure. ``code`` is safe to surface verbatim."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def safe_environment() -> Dict[str, str]:
    """Return the scrubbed child environment.

    An allowlist, not a denylist: a secret this process happens to hold is
    absent unless its variable name is one a Python interpreter needs. Every
    child except the live one gets exactly this.
    """
    env = {name: value for name, value in os.environ.items() if name in _ENV_ALLOWLIST}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
        env.pop(name, None)
    return env


def live_environment() -> Dict[str, str]:
    """Return the scrubbed child environment **plus** the one live credential.

    Narrow on purpose: the live call happens in this child, so this child is the
    only one that gets the variable, and it is added to the same allowlist
    everything else uses rather than to a widened one. The value is never
    returned to a caller, never written, never logged and never placed in a
    request or an answer.
    """
    env = safe_environment()
    value = os.environ.get(deepseek_live.CREDENTIAL_ENV)
    if value and value.strip():
        env[deepseek_live.CREDENTIAL_ENV] = value
    return env


# ---------------------------------------------------------------------------
# The reusable bounded child runner
# ---------------------------------------------------------------------------
def spawn_child(
    argv: List[str],
    *,
    workdir: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    write_bytecode: bool = False,
) -> subprocess.Popen:
    """Start a fixed-argv child. Raises :class:`WorkerError` if it cannot start."""
    child_env = dict(env) if env is not None else safe_environment()
    if write_bytecode:
        # The runtime initializer deliberately lets the interpreter populate the
        # bytecode cache, which is what makes the *parent's* later import cheap.
        child_env.pop("PYTHONDONTWRITEBYTECODE", None)
    try:
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            env=child_env,
        )
    except OSError:
        raise WorkerError(CODE_CRASHED)


def terminate_child(proc: subprocess.Popen, cleanup: float) -> None:
    """Bounded termination and reaping. Late output is never used."""
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=cleanup)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=cleanup)
        except subprocess.TimeoutExpired:
            pass
    try:
        proc.communicate(timeout=cleanup)
    except Exception:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class WorkerProvider(provider_mod.Provider):
    """A ``Provider`` whose one generation runs in a bounded child process.

    ``scenario`` names a closed, code-owned offline fixture in
    :mod:`hrca.agent.worker`. There is no live provider path here.
    """

    def __init__(
        self,
        scenario: str,
        *,
        deadline: float = WORKER_DEADLINE_SECONDS,
        cleanup: float = WORKER_CLEANUP_SECONDS,
        workdir: Optional[str] = None,
        python: Optional[str] = None,
        env_factory: Any = None,
    ) -> None:
        self._scenario = str(scenario)
        self._cap = float(deadline)
        self._cleanup = float(cleanup)
        self._python = python or sys.executable
        self._workdir = workdir
        self._owns_workdir = workdir is None
        #: The child environment. The scrubbed allowlist for every scripted
        #: worker; the live provider passes :func:`live_environment` instead, so
        #: the one credential reaches exactly one child at exactly one step.
        self._env_factory = env_factory if env_factory is not None else safe_environment
        self._call_index = 0
        self._turn_deadline: Optional[float] = None
        self.terminated = 0
        self.last_allowance: Optional[float] = None

    # -- Provider -----------------------------------------------------------
    def capabilities(self) -> provider_mod.ProviderCapabilities:
        return provider_mod.ProviderCapabilities(
            structured_output=True,
            streaming=False,
            cancellation=True,
            tool_calling=False,
            context_limit=None,
            readiness_available=False,
        )

    def readiness(self) -> provider_mod.ProviderReadiness:
        return provider_mod.ProviderReadiness(
            ready=False, detail="offline scripted worker; no live provider"
        )

    def set_turn_deadline(self, deadline_monotonic: float) -> None:
        """Bind this provider to the turn's single monotonic deadline.

        Duck-typed on purpose: the executor offers this to any adapter that
        supports it, and the ``Provider`` interface is unchanged.
        """
        self._turn_deadline = float(deadline_monotonic)

    # -- one call -----------------------------------------------------------
    def generate(self, request: provider_mod.ProviderRequest) -> provider_mod.ProviderResult:
        if not isinstance(request, provider_mod.ProviderRequest):
            raise WorkerError(CODE_BAD_OUTPUT)

        allowance: Optional[float] = None
        if self._turn_deadline is not None:
            allowance = budget_mod.call_allowance(
                self._turn_deadline, _monotonic(), cap=self._cap, cleanup=self._cleanup
            )
            self.last_allowance = allowance
            if allowance <= 0:
                # Not dispatched. Nothing was started, so nothing can have
                # happened remotely: a refusal, never an uncertain outcome.
                raise budget_mod.TurnBudgetExhausted()
        effective = min(self._cap, allowance) if allowance is not None else self._cap

        payload = {
            "scenario": self._scenario,
            "call_index": self._call_index,
            "request": {
                "task_id": request.task_id,
                "task": request.task,
                "context": list(request.context),
            },
        }
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise WorkerError(CODE_OVERSIZED)
        self._call_index += 1

        answer = self._run_once(body, effective)
        if not isinstance(answer, dict) or not answer.get("ok"):
            raise WorkerError(CODE_BAD_OUTPUT)
        return provider_mod.ProviderResult(
            task_id=request.task_id,
            content="worker",
            provider="scripted-worker",
            model=self._scenario,
            structured_payload=answer.get("payload"),
        )

    def _argv(self) -> List[str]:
        # Isolated mode: no user site-packages, no PYTHON* environment entries.
        # A single fixed module name — never a path, a shell string or a payload.
        return [self._python, "-I", "-m", "hrca.agent.worker"]

    def _run_once(self, body: bytes, deadline: float) -> Optional[Dict[str, Any]]:
        workdir = self._workdir or tempfile.mkdtemp(prefix="hrca-agent-worker-")
        proc = None
        try:
            proc = spawn_child(self._argv(), workdir=workdir, env=self._env_factory())
            try:
                out, _err = proc.communicate(input=body, timeout=deadline)
            except subprocess.TimeoutExpired:
                # NOTE: ``subprocess.TimeoutExpired`` is a ``SubprocessError``,
                # NOT a ``TimeoutError``. Catching only the latter would skip the
                # kill, the reap and the deadline token (P5.5r1).
                self._terminate(proc)
                raise WorkerError(CODE_DEADLINE)
            if proc.returncode != 0:
                raise WorkerError(CODE_CRASHED)
            if len(out or b"") > MAX_RESPONSE_BYTES:
                raise WorkerError(CODE_BAD_OUTPUT)
            try:
                return json.loads((out or b"").decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise WorkerError(CODE_BAD_OUTPUT)
        finally:
            if proc is not None and proc.poll() is None:
                self._terminate(proc)
            if self._owns_workdir:
                shutil.rmtree(workdir, ignore_errors=True)

    def _terminate(self, proc: subprocess.Popen) -> None:
        self.terminated += 1
        terminate_child(proc, self._cleanup)


#: The bounded length of one provider-reported metadata string. Anything longer
#: is not truncated into a plausible-looking model id; it is reported
#: unavailable, because a clipped identifier is worse than an absent one.
MAX_METADATA_CHARS = 64


def _bounded_text(value: Any) -> str:
    """Return a bounded, safe provider string, or ``""`` when unusable."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > MAX_METADATA_CHARS:
        return ""
    if any(char < " " or char == "\x7f" for char in text):
        return ""
    return text


def _bounded_call_record(answer: Dict[str, Any]) -> Dict[str, Any]:
    """Return the safe, bounded metadata for one answered call.

    Only declared fields are carried: the code-owned **requested** model, the
    **returned** model and fingerprint if the provider stated them, the outcome
    code, and the usage counters. No raw payload, header, error body or
    reasoning text can reach a caller through this record.
    """
    usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
    return {
        "requested_model": deepseek_live.MODEL_ID,
        "returned_model": _bounded_text(answer.get("model")),
        "fingerprint": _bounded_text(answer.get("fingerprint")),
        "outcome": _bounded_text(answer.get("code")) or "ok",
        "usage_reported": bool(usage.get("reported")),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
    }


class LiveWorkerProvider(WorkerProvider):
    """One bounded **live** model call, in the owned child, through the gate.

    The child does the talking; this class owns the *accounting*. The worst case
    is reserved on the shared ledger **before** the child is spawned, so no
    dispatched request can be unaccounted for, and the reservation is settled
    with what actually happened when the child answers. A dispatched call whose
    outcome is unknown — a transport failure, a timeout, a reaped child, an
    unreadable answer — keeps its worst-case reservation: an unmeasurable call
    is charged, never waived.
    """

    def __init__(
        self,
        *,
        gate: Any,
        python: Optional[str] = None,
        cleanup: float = WORKER_CLEANUP_SECONDS,
        workdir: Optional[str] = None,
        label: str = "",
        run_id: str = "",
        case_id: str = "",
    ) -> None:
        super().__init__(
            deepseek_live.LIVE_SCENARIO,
            cleanup=cleanup,
            workdir=workdir,
            python=python,
            env_factory=live_environment,
        )
        self.gate = gate
        self.label = str(label)
        #: Persisted supplement identity, transported from the runner through
        #: code-owned environment names. It is not authority: the gate looks the
        #: run id up in its own ledger and refuses an unknown one.
        self.run_id = str(run_id)[:64]
        self.case_id = str(case_id)[:8]
        #: Test-side observation of the last exchange; neither holds the
        #: credential, the raw body or anything the child did not already bound.
        self.last_answer: Dict[str, Any] = {}
        self.last_reservation: Optional[Dict[str, Any]] = None
        #: One bounded record per *call*, in order, never overwriting an earlier
        #: one: a turn may make two calls, and the second must not be reported as
        #: if it were the first. Each record carries the code-owned requested
        #: model separately from whatever the provider actually returned, so an
        #: absent returned field stays visibly unavailable instead of being
        #: back-filled with the request's own alias.
        self.calls: List[Dict[str, Any]] = []

    def capabilities(self) -> provider_mod.ProviderCapabilities:
        return provider_mod.ProviderCapabilities(
            structured_output=True,
            streaming=False,
            cancellation=True,
            tool_calling=False,
            context_limit=deepseek_live.MAX_INPUT_TOKENS,
            readiness_available=False,
        )

    def readiness(self) -> provider_mod.ProviderReadiness:
        """Never probes.

        The first planned case *is* the first request: a separate availability
        call would be an unplanned, unbudgeted billable request, and there is no
        endpoint here whose whole purpose is to be pinged.
        """
        return provider_mod.ProviderReadiness(
            ready=False,
            detail="the live model is dispatched per turn; no separate readiness probe is made",
        )

    def _max_tokens(self) -> int:
        """Return the output ceiling: the model cap, lowered by what is left."""
        remaining = int(self.gate.remaining()["output_tokens_remaining"])
        return max(0, min(deepseek_live.MAX_OUTPUT_TOKENS, remaining))

    def generate(self, request: provider_mod.ProviderRequest) -> provider_mod.ProviderResult:
        if not isinstance(request, provider_mod.ProviderRequest):
            raise WorkerError(CODE_BAD_OUTPUT)

        if self._turn_deadline is not None:
            allowance = budget_mod.call_allowance(
                self._turn_deadline, _monotonic(), cap=self._cap, cleanup=self._cleanup
            )
            self.last_allowance = allowance
            if allowance <= 0:
                raise budget_mod.TurnBudgetExhausted()

        max_tokens = self._max_tokens()
        context = tuple(request.context)
        # The body is built here only to measure it. The child rebuilds the same
        # body from the same code-owned builder and is the only side that sends.
        _body_text, bound = deepseek_live.build_body(
            request.task, context, max_tokens=max_tokens
        )
        #: One-based ordinal within the case, so a duplicate identity can never
        #: spend a second effect. Ordinary runs carry no run id and use the
        #: ordinary ceiling.
        request_index = (self._call_index + 1) if self.run_id else 0
        try:
            reservation = self.gate.reserve(
                input_upper=bound,
                output_cap=max_tokens,
                label=self.label or request.task_id,
                run_id=self.run_id,
                case_id=self.case_id,
                request_index=request_index,
                wait_deadline=self._turn_deadline,
            )
        except live_gate_mod.GateRefused as refusal:
            # A refusal, not an uncertain outcome: nothing was dispatched, so
            # nothing can have been billed and nothing can have happened.
            raise budget_mod.DispatchRefused(refusal.reason)
        self.last_reservation = reservation

        payload = {
            "scenario": deepseek_live.LIVE_SCENARIO,
            "call_index": self._call_index,
            "max_tokens": max_tokens,
            "request": {
                "task_id": request.task_id,
                "task": request.task,
                "context": list(context),
            },
        }
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            # Reserved but never dispatched: the worst case stands, and the
            # effect is known to be nothing.
            self.gate.settle(
                reservation,
                usage=None,
                outcome=CODE_OVERSIZED,
                dispatched=False,
                observed=True,
            )
            raise WorkerError(CODE_OVERSIZED)

        # Admission (including a bounded lock wait) and the local body assembly
        # are time the turn has spent. Recompute against the absolute deadline
        # immediately before dispatch so the call never receives the
        # pre-admission allowance plus that time. If the remaining window no
        # longer fits the cleanup and response reserves, nothing is dispatched:
        # the reservation is settled as a local non-dispatch, so its
        # conservative charge stands but the in-flight and uncertainty guards
        # are not falsely tripped.
        if self._turn_deadline is not None:
            effective = budget_mod.call_allowance(
                self._turn_deadline, _monotonic(), cap=self._cap, cleanup=self._cleanup
            )
            self.last_allowance = effective
            if effective <= 0:
                self.gate.settle(
                    reservation,
                    usage=None,
                    outcome=CODE_TURN_BUDGET,
                    dispatched=False,
                    observed=True,
                )
                raise budget_mod.TurnBudgetExhausted()
        else:
            effective = self._cap
        self._call_index += 1

        try:
            answer = self._run_once(body, effective)
        except WorkerError as error:
            # Dispatched and not observed: the worst case stands, and the
            # gate refuses further dispatch until a human resolves the state.
            self.gate.settle(
                reservation, usage=None, outcome=error.code, observed=False
            )
            raise
        self.last_answer = answer if isinstance(answer, dict) else {}
        # Recorded whenever the child *answered* — including a bounded
        # non-success, because that answer may still have been billed. A call
        # that produced no answer at all is recorded nowhere: its uncertainty
        # belongs to the gate's reservation, not to a fabricated record.
        self.calls.append(_bounded_call_record(self.last_answer))
        if not self.last_answer.get("ok"):
            self.gate.settle(
                reservation,
                usage=self.last_answer.get("usage"),
                outcome=str(self.last_answer.get("code") or "unknown"),
            )
            raise WorkerError(CODE_BAD_OUTPUT)
        usage = self.last_answer.get("usage") or {}
        self.gate.settle(reservation, usage=usage, outcome="ok")
        reported = bool(usage.get("reported"))
        return provider_mod.ProviderResult(
            task_id=request.task_id,
            content="live",
            provider="deepseek",
            model=deepseek_live.MODEL_ID,
            structured_payload=self.last_answer.get("payload"),
            usage=provider_mod.ProviderUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0) if reported else None,
                completion_tokens=(
                    int(usage.get("completion_tokens") or 0) if reported else None
                ),
                total_tokens=int(usage.get("total_tokens") or 0) if reported else None,
            ),
        )

def _monotonic() -> float:
    import time

    return time.monotonic()


__all__ = [
    "WORKER_DEADLINE_SECONDS",
    "WORKER_CLEANUP_SECONDS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "CODE_DEADLINE",
    "CODE_CRASHED",
    "CODE_BAD_OUTPUT",
    "CODE_OVERSIZED",
    "CODE_TURN_BUDGET",
    "WorkerError",
    "WorkerProvider",
    "LiveWorkerProvider",
    "MAX_METADATA_CHARS",
    "safe_environment",
    "live_environment",
    "spawn_child",
    "terminate_child",
]
