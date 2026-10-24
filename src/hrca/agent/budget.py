"""The shared whole-turn time budget (LANGGRAPH-LOOP-1D).

A turn is not a fixed number of independent calls whose individual caps happen
to sum to something. It is **one monotonic parent-owned deadline**, inside the
desktop's existing supervised request envelope, out of which every step pays:

```
|<-- TURN_BUDGET_SECONDS (12.0) ------------------------------------>|
| graph work | worker call 1 | worker call 2 | settle/reserve (1.5)  |  margin
|<--------------------------- BACKEND_DEADLINE ----------------------->|
|<----------------- supervisor request envelope (15 s) ----------------|
```

The worker never gets a fresh cap per call: each call receives
``min(cap, remaining turn time - cleanup)``, and a call whose allowance is not
positive is **not dispatched at all** — a *refusal*, distinct from a dispatched
call whose outcome was not observed (*unknown*). That distinction matters: a
call that never left cannot have had a remote effect, and a caller must not
report the two the same way.

Nothing here is caller- or model-tunable, and the supervisor's 15 s envelope is
never widened.
"""

from __future__ import annotations

#: The whole-turn budget, in seconds, inside the supervised request envelope.
TURN_BUDGET_SECONDS = 12.0

#: Reserved at the end of the turn for the terminal persistence and for carrying
#: the response back over the supervised pipe. Neither may be spent on a call.
TURN_RESPONSE_RESERVE_SECONDS = 1.5

#: The per-call ceiling. A call may never exceed this even when the turn has
#: more time left, and it never gets a *second* full allowance.
WORKER_CALL_CAP_SECONDS = 8.0

#: Bounded terminate/reap window after a call's deadline.
WORKER_CLEANUP_SECONDS = 3.0

#: The ceiling on a single SQLite busy wait. It matches the stores' own default
#: so a caller that sets no deadline keeps exactly the behaviour it had.
STORE_WAIT_CAP_SECONDS = 10.0


class TurnBudgetExhausted(Exception):
    """Raised *before* dispatch when the turn has no allowance left for a call.

    This is a **refusal**, not an uncertain outcome: nothing was started, so
    nothing can have happened remotely.
    """


class DispatchRefused(Exception):
    """Raised *before* dispatch by a local admission rule.

    The sibling of :class:`TurnBudgetExhausted` for refusals that are not about
    time: a live gate that has spent its allowance, a per-request input bound
    that the payload exceeds, a missing credential. All of them happen before a
    byte leaves the process, so all of them are refusals — never an observed
    attempt, never retried, never rounded to ``unknown``. ``reason`` is bounded
    and safe to surface verbatim.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


def call_allowance(
    turn_deadline: float,
    now: float,
    *,
    cap: float = WORKER_CALL_CAP_SECONDS,
    cleanup: float = WORKER_CLEANUP_SECONDS,
    reserve: float = TURN_RESPONSE_RESERVE_SECONDS,
) -> float:
    """Return the seconds a single call may take right now.

    A non-positive result means the call must not be dispatched. ``cleanup`` is
    subtracted because the parent has to be able to stop and reap the child
    inside the turn, not after it.
    """
    remaining = (turn_deadline - now) - reserve
    return min(cap, remaining - cleanup)


def store_deadline(turn_deadline: float) -> float:
    """Return the instant store operations may use: the turn deadline less the
    reserve. A store that honours this instant never consumes the time the turn
    still needs for its terminal commit and its response.
    """
    return turn_deadline - TURN_RESPONSE_RESERVE_SECONDS


def store_wait_allowance(
    deadline: float,
    now: float,
    *,
    cap: float = STORE_WAIT_CAP_SECONDS,
) -> float:
    """Return the seconds a store operation may wait on a lock right now.

    ``deadline`` is the already-reserved instant (:func:`store_deadline`).
    Recomputed at **every** connection, so successive operations consume the
    same budget rather than each getting a fresh full wait. ``0.0`` means "do
    not wait at all": the SQLite busy handler is disabled and a contended
    database fails immediately as a bounded store fault instead of blocking past
    the turn.
    """
    return max(0.0, min(cap, deadline - now))


__all__ = [
    "TURN_BUDGET_SECONDS",
    "TURN_RESPONSE_RESERVE_SECONDS",
    "WORKER_CALL_CAP_SECONDS",
    "WORKER_CLEANUP_SECONDS",
    "STORE_WAIT_CAP_SECONDS",
    "TurnBudgetExhausted",
    "DispatchRefused",
    "call_allowance",
    "store_deadline",
    "store_wait_allowance",
]
