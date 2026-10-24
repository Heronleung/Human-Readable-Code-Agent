"""Bounded, explicit local graph-runtime initialization (LANGGRAPH-LOOP-1D).

The LangGraph runtime is a large import — measured at ~25 s the first time after
an install and ~2.8 s once the interpreter's bytecode cache is warm. Doing that
import inside a turn means the first turn can exceed the desktop's whole request
envelope. So it is moved *out* of the turn, into an explicit, bounded,
separately-owned step:

* :meth:`RuntimeInitializer.start` launches a **code-owned child** that imports
  the runtime (which populates the shared bytecode cache) and returns
  immediately with ``initializing`` — no request ever blocks on the import;
* in parallel it loads the runtime in **this** process on an owned worker
  thread, because a child can compile bytecode but can never populate the
  parent's ``sys.modules``;
* :meth:`RuntimeInitializer.poll` reports progress, enforces the code-owned
  deadline by terminating and reaping the owned child, and settles to ``ready``
  / ``error``;
* if the deadline expires while **this process's** load is still in flight, the
  process itself is written off (:func:`process_disposed`): no further loader or
  turn may run here, because Python cannot cancel the import and a late
  completion must never be read as recovery. The supervisor replaces the
  process; a direct in-process caller must refuse;
* A turn is **not accepted** until the state is ``ready`` — and ``ready`` means
  the runtime is genuinely loaded *here*, so a turn never pays the runtime's
  load inside its own budget.

Initialization performs **zero** model, worker or scanner inference, reads no
credential, opens no socket and writes nothing into the selected source. It only
compiles and loads installed dependency bytecode. It also does **not** mean a
live adapter exists: readiness and adapter availability are separate facts.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import time
from typing import Dict, Optional

from . import worker_host as worker_host_mod

RUNTIME_UNINITIALIZED = "uninitialized"
RUNTIME_INITIALIZING = "initializing"
RUNTIME_READY = "ready"
RUNTIME_UNAVAILABLE = "unavailable"
RUNTIME_ERROR = "error"

#: The code-owned initialization ceiling. It is deliberately larger than the
#: measured first-install-like import (~25 s) and is *not* the turn envelope:
#: initialization is asynchronous, so nothing waits on it inside a request.
RUNTIME_INIT_DEADLINE_SECONDS = 45.0
RUNTIME_INIT_CLEANUP_SECONDS = 5.0

#: The closed set of initialization scenarios. ``import_runtime`` is the real
#: one; the rest are code-owned offline fixtures so a slow, failing, hung or
#: stalled initialization can be exercised without changing the product path.
RUNTIME_SCENARIOS = ("import_runtime", "slow", "fail", "hang", "stalled_parent")

#: The only scenario that has a parent-side load to do. A fixture that models
#: the *child* lifecycle leaves the parent flag already satisfied — otherwise it
#: would silently depend on whether the host had already loaded the runtime.
REAL_SCENARIO = "import_runtime"

#: A fixture whose **parent** half deliberately never finishes. It is the only
#: shape in which the process itself, rather than the child, is the outstanding
#: work, so it is what the process-disposal contract is proved against.
STALLED_SCENARIO = "stalled_parent"

#: The code-owned ceiling a scenario runs under. Only the stalled fixture
#: shortens it, so its acceptance run is bounded; nothing a caller sends can
#: change this table.
SCENARIO_DEADLINES: Dict[str, float] = {STALLED_SCENARIO: 1.0}

#: The child program, per scenario. Code-owned literals only: there is no
#: caller-supplied import, module path or command anywhere on this path.
_INIT_BODY: Dict[str, str] = {
    "import_runtime": "import langgraph.graph",
    "slow": "import time; time.sleep(3.0); import langgraph.graph",
    "fail": "import sys; sys.exit(3)",
    "hang": "import time; time.sleep(3600)",
    "stalled_parent": "pass",
}

#: Every child carries its own code-owned bound: a watchdog thread that ends the
#: process at the initializer's deadline even if nobody ever reaps it. Without
#: it, killing the backend would orphan the child until its import finished on
#: its own — bounded in practice, but not *guaranteed* to be, which is the
#: difference this closes. ``9`` is a bounded, non-zero exit: exceeding the
#: child's own bound is a failure, never a readiness.
_CHILD_BOUND_TEMPLATE = (
    "import os, threading\n"
    "_bound = threading.Timer({bound!r}, lambda: os._exit(9))\n"
    # Daemon, so the guard itself is never what keeps the child alive: a
    # non-daemon timer would hold the interpreter open for the whole bound and
    # turn every successful import into a hang.
    "_bound.daemon = True\n"
    "_bound.start()\n"
    "{body}\n"
    "_bound.cancel()\n"
)


def child_source(scenario: str, deadline: float) -> str:
    """Return the code-owned child program for one scenario and bound."""
    body = _INIT_BODY.get(scenario, _INIT_BODY[REAL_SCENARIO])
    return _CHILD_BOUND_TEMPLATE.format(bound=float(deadline), body=body)


def deadline_for(scenario: str) -> float:
    """Return the code-owned initialization ceiling for one scenario."""
    return float(SCENARIO_DEADLINES.get(scenario, RUNTIME_INIT_DEADLINE_SECONDS))


DETAIL_READY = "the local graph runtime is loaded in this process"
DETAIL_INITIALIZING = "the local graph runtime is initializing"
DETAIL_UNAVAILABLE = "the agent graph runtime is not installed"
DETAIL_TIMEOUT = "the local graph runtime did not initialize inside its deadline"
DETAIL_FAILED = "the local graph runtime failed to initialize"
DETAIL_UNKNOWN_SCENARIO = "the requested runtime scenario is not supported"
DETAIL_CLOSED = "the initializer was closed before it finished"
DETAIL_DISPOSED = "this backend process must be replaced before the agent can run"


def runtime_installed() -> bool:
    """Return whether the LangGraph runtime is *locatable*, without importing it."""
    try:
        return importlib.util.find_spec("langgraph") is not None
    except (ImportError, ValueError):
        return False


#: Readiness is a property of *this interpreter*, not of one session: once the
#: runtime has been loaded here, a later initializer starts already ready,
#: because there is nothing left to load. The flag is process-global and never
#: persisted.
_WARMED = False


def runtime_warmed() -> bool:
    """Return whether this process has already loaded the graph runtime."""
    return _WARMED


def _mark_warmed() -> None:
    global _WARMED
    _WARMED = True


#: A process whose runtime load was **still outstanding** when the initialization
#: deadline fired is written off entirely. Python cannot cancel an import, and a
#: late completion must never be read as recovery, so this cannot be cleared by
#: anything that happens inside the process. The supervisor replaces the process;
#: a direct in-process caller has no such path and must refuse rather than claim
#: a bound it cannot enforce.
_DISPOSED = False


def process_disposed() -> bool:
    """Return whether an expired initialization has written this process off."""
    return _DISPOSED


def _mark_disposed() -> None:
    global _DISPOSED
    _DISPOSED = True


def _stall_forever() -> None:
    """The code-owned stalled-parent fixture: a load that never finishes.

    Deliberately unbounded — that is the point. It is what makes the *process*
    the only thing that can end the work, so the acceptance run proves process
    replacement rather than a cooperative stop. Idle, not spinning.
    """
    while True:
        time.sleep(3600.0)


class RuntimeInitializer:
    """Owns one bounded initialization and its state.

    Two things have to be true before ``ready``: the code-owned child must have
    imported the runtime (so the dependency really is importable here and its
    bytecode is on disk), **and** this process must have loaded it itself — a
    child cannot populate the parent's ``sys.modules``, and the parent's own
    import would otherwise be charged to the first turn's budget.

    The deadline is enforced by an **owned timer**, not by whoever happens to
    poll next: a hung child is terminated and reaped at the ceiling even if no
    caller ever asks again. Every transition runs under one lock and only ever
    leaves ``initializing`` once, so a late child exit — or a late parent load —
    cannot promote a timed-out, errored or closed initialization to ``ready``,
    and no second child is ever spawned.
    """

    def __init__(
        self,
        *,
        python: Optional[str] = None,
        deadline: float = RUNTIME_INIT_DEADLINE_SECONDS,
        cleanup: float = RUNTIME_INIT_CLEANUP_SECONDS,
        scenario: str = "import_runtime",
    ) -> None:
        self._python = python or sys.executable
        self._deadline = float(deadline)
        self._cleanup = float(cleanup)
        self._scenario = scenario if scenario in RUNTIME_SCENARIOS else "import_runtime"
        # A scenario that carries its own code-owned ceiling keeps it, whether it
        # was named here or at ``start`` — so no argument can widen it.
        if self._scenario in SCENARIO_DEADLINES:
            self._deadline = deadline_for(self._scenario)
        self._proc = None
        self._started_at: Optional[float] = None
        self._workdir: Optional[str] = None
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._loader: Optional[threading.Thread] = None
        self._closed = False
        #: The child's half of readiness: the dependency imported cleanly.
        self._child_ok = False
        #: This process's half: the runtime is in *our* ``sys.modules``.
        self._parent_loaded = False
        if not runtime_installed():
            self.state, self.detail = RUNTIME_UNAVAILABLE, DETAIL_UNAVAILABLE
        elif process_disposed():
            # An earlier initialization in this process expired with its parent
            # load outstanding. Nothing here may start a loader or a turn.
            self.state, self.detail = RUNTIME_ERROR, DETAIL_DISPOSED
        elif self._scenario == REAL_SCENARIO and runtime_warmed():
            # This process has already loaded the runtime, so there is nothing
            # left for a turn to pay. Only the real scenario short-circuits: a
            # code-owned fixture always really runs, so a slow/failing/hung
            # initialization stays reproducible.
            self.state, self.detail = RUNTIME_READY, DETAIL_READY
        else:
            self.state, self.detail = RUNTIME_UNINITIALIZED, ""
        self.reaped = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self, scenario: Optional[str] = None) -> str:
        """Ensure initialization has begun; return the (possibly unchanged) state."""
        with self._lock:
            if self._closed:
                return self.state
            if process_disposed():
                self.state, self.detail = RUNTIME_ERROR, DETAIL_DISPOSED
                return self.state
            if scenario is not None:
                if scenario not in RUNTIME_SCENARIOS:
                    self.state, self.detail = RUNTIME_ERROR, DETAIL_UNKNOWN_SCENARIO
                    return self.state
                self._scenario = scenario
                # Only a fixture that carries its own code-owned ceiling sets
                # the deadline here; every other scenario keeps the caller's or
                # the constructor's, so nothing a request sends can widen the
                # real one.
                if scenario in SCENARIO_DEADLINES:
                    self._deadline = deadline_for(scenario)
            if self.state in (RUNTIME_READY, RUNTIME_UNAVAILABLE, RUNTIME_ERROR):
                return self.state
            if self.state == RUNTIME_INITIALIZING:
                return self.poll()

            # A fixture that models only the child lifecycle has no parent load
            # of its own to do, so that half is satisfied from the start; the
            # real and stalled scenarios have to earn it. Resolved here rather
            # than in ``__init__`` so an explicit ``start(scenario)`` decides it,
            # not the default.
            self._parent_loaded = self._scenario not in (REAL_SCENARIO, STALLED_SCENARIO)

            argv = [
                self._python,
                "-I",
                "-c",
                child_source(self._scenario, self._deadline),
            ]
            # ``-I`` is isolated mode, which also ignores every ``PYTHON*``
            # variable — including ``PYTHONPYCACHEPREFIX``. The initializer
            # therefore always uses the interpreter's own bytecode cache; a
            # fresh-install-like measurement is made separately, with a plain
            # subprocess, and is reported as such rather than attributed to this
            # path.
            env = worker_host_mod.safe_environment()
            self._workdir = tempfile.mkdtemp(prefix="hrca-agent-init-")
            try:
                self._proc = worker_host_mod.spawn_child(
                    argv, workdir=self._workdir, env=env, write_bytecode=True
                )
            except worker_host_mod.WorkerError:
                self._cleanup_workdir()
                self.state, self.detail = RUNTIME_ERROR, DETAIL_FAILED
                return self.state
            self._started_at = time.monotonic()
            self.state, self.detail = RUNTIME_INITIALIZING, DETAIL_INITIALIZING
            self._arm_timer()
            # The parent's own load runs beside the child, on a thread this
            # object owns. It is bounded by the same timer: whichever half is
            # late, the deadline settles the initialization to ``error`` and a
            # load that lands afterwards cannot promote it.
            if self._scenario in (REAL_SCENARIO, STALLED_SCENARIO):
                self._start_parent_load()
            return self.state

    def poll(self) -> str:
        """Observe and advance the state. Observation only — never the sole
        enforcement: the owned timer settles the deadline on its own.
        """
        with self._lock:
            if self.state != RUNTIME_INITIALIZING or self._proc is None:
                return self.state
            code = self._proc.poll()
            if code is not None and code != 0:
                self._settle(RUNTIME_ERROR, DETAIL_FAILED)
                return self.state
            if code == 0:
                self._child_ok = True
            if self._child_ok and self._parent_loaded:
                _mark_warmed()
                self._settle(RUNTIME_READY, DETAIL_READY)
                return self.state
            if self._started_at is not None and (
                time.monotonic() - self._started_at
            ) > self._deadline:
                self._settle(RUNTIME_ERROR, DETAIL_TIMEOUT)
            return self.state

    def close(self) -> None:
        """Release everything this object owns; idempotent and terminal."""
        with self._lock:
            self._closed = True
            self._cancel_timer()
            if self.state == RUNTIME_INITIALIZING:
                self._settle(RUNTIME_ERROR, DETAIL_CLOSED)
            else:
                self._reap()

    # -- internals ----------------------------------------------------------
    def _start_parent_load(self) -> None:
        """Load the runtime in this process, on an owned daemon thread.

        Daemon, so it can never keep the process alive; owned, so ``close`` and
        the deadline both survive a load that is still running.
        """
        loader = threading.Thread(
            target=self._load_in_parent,
            name="hrca-agent-runtime-load",
            daemon=True,
        )
        self._loader = loader
        loader.start()

    def _load_in_parent(self) -> None:
        """The parent's half of readiness. Runs off the request path."""
        if self._scenario == STALLED_SCENARIO:
            # The code-owned fixture: the parent half never completes. Only the
            # deadline ends the *initialization*; only the owning process being
            # replaced ends this thread.
            _stall_forever()
            return

        from . import graph as graph_loader

        loaded = graph_loader.load_runtime()
        with self._lock:
            # A late load never promotes a settled initialization: if the timer
            # fired, ``close`` ran, or the child already failed, the state is no
            # longer ``initializing`` and this half is simply discarded.
            if self._closed or self.state != RUNTIME_INITIALIZING:
                return
            if not loaded:
                self._settle(RUNTIME_ERROR, DETAIL_FAILED)
                return
            self._parent_loaded = True
            self.poll()

    def _parent_load_outstanding(self) -> bool:
        """Return whether this process's own load was started and never finished.

        Caller holds the lock. A fixture that never started a parent load has
        nothing outstanding — only a started-and-unfinished one can leave work
        running behind a settled state.
        """
        return self._loader is not None and not self._parent_loaded

    @property
    def deadline_seconds(self) -> float:
        """The code-owned ceiling this initialization runs under.

        Reported on the wire so the *supervisor* can enforce the same instant at
        the process boundary, independently of anything this process does.
        """
        return float(self._deadline)

    def _disposal_required(self) -> bool:
        """Return whether the process must be replaced. Caller holds the lock.

        Only an *expired* initialization with this process's own load still in
        flight qualifies. A child-side failure leaves the parent healthy, and a
        deliberate ``close`` is the caller's own decision — neither writes the
        process off.
        """
        return (
            self.state == RUNTIME_ERROR
            and self.detail == DETAIL_TIMEOUT
            and self._parent_load_outstanding()
        )

    @property
    def must_dispose(self) -> bool:
        """Return whether the **process** has to be replaced, not just the state.

        True only when the initialization expired with this process's own
        runtime load still in flight. Then no in-process recovery is honest: the
        load cannot be cancelled, a late completion must not be read as
        recovery, and the backend process can no longer be trusted to serve a
        turn. The supervisor replaces the process; a direct caller must refuse.
        """
        with self._lock:
            return self._disposal_required()

    def _arm_timer(self) -> None:
        """Arm the owned deadline. Daemon: it never keeps the process alive."""
        timer = threading.Timer(self._deadline, self._on_deadline)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _on_deadline(self) -> None:
        """Autonomous expiry: no caller has to poll for this to happen."""
        with self._lock:
            if self.state != RUNTIME_INITIALIZING or self._closed:
                return
            self._settle(RUNTIME_ERROR, DETAIL_TIMEOUT)

    def _settle(self, state: str, detail: str) -> None:
        """Settle exactly once. Caller holds the lock."""
        if self.state != RUNTIME_INITIALIZING:
            return
        outstanding = self._parent_load_outstanding()
        self._cancel_timer()
        self._reap()
        self.state, self.detail = state, detail
        if (
            state == RUNTIME_ERROR
            and detail == DETAIL_TIMEOUT
            and outstanding
        ):
            # The process, not just the initialization, is now written off: the
            # load is still running and nothing inside this process may start
            # another one or serve a turn.
            _mark_disposed()

    def _reap(self) -> None:
        """Stop and reap only the child this object owns. Caller holds the lock."""
        if self._proc is not None:
            self.reaped += 1
            worker_host_mod.terminate_child(self._proc, self._cleanup)
            self._proc = None
        self._cleanup_workdir()

    def _cleanup_workdir(self) -> None:
        if self._workdir is not None:
            import shutil

            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None


__all__ = [
    "RUNTIME_UNINITIALIZED",
    "RUNTIME_INITIALIZING",
    "RUNTIME_READY",
    "RUNTIME_UNAVAILABLE",
    "RUNTIME_ERROR",
    "RUNTIME_INIT_DEADLINE_SECONDS",
    "RUNTIME_INIT_CLEANUP_SECONDS",
    "RUNTIME_SCENARIOS",
    "REAL_SCENARIO",
    "STALLED_SCENARIO",
    "SCENARIO_DEADLINES",
    "child_source",
    "deadline_for",
    "process_disposed",
    "DETAIL_DISPOSED",
    "DETAIL_READY",
    "DETAIL_INITIALIZING",
    "DETAIL_UNAVAILABLE",
    "DETAIL_TIMEOUT",
    "DETAIL_FAILED",
    "DETAIL_UNKNOWN_SCENARIO",
    "DETAIL_CLOSED",
    "runtime_installed",
    "runtime_warmed",
    "RuntimeInitializer",
]
