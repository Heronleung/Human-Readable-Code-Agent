"""Backend tool policy for the bounded agent loop (AGENT-LOOP-1).

Policy is immutable application code. A model's accepted choice *passes
through* this module; it can never widen, bypass or rewrite it. The policy
answers one question — may this validated choice act at all? — and returns
either ``None`` (permitted) or one of the module's fixed reason sentences.

The reasons are code-owned constants, so no model text, path, id or stored
content can reach a client through a refusal. Policy never performs an effect
and never touches a store: it is a pure predicate over facts the executor
already holds.

Two of the five choices have no effect and are always permitted: ``clarify``
asks the developer a question and ``stop`` records a limitation. ``answer``
returns text the executor must still ground against retrieved records.
``read_record`` and ``propose_scan`` are the two choices that touch backend
state, and each is bounded here.
"""

from __future__ import annotations

from typing import Optional

from . import choice as agent_contract

#: The single record read this slice permits. The name is a backend reader
#: identity, resolved by the executor against the owner's own read interface —
#: never a path, a store handle or a client-supplied locator. The closed
#: *vocabulary* of record names is owned by the payload contract
#: (:data:`hrca.agent.choice.ALLOWED_RECORDS`); policy owns the
#: state-dependent *permission* to use them.
RECORD_WORKFLOW = "workflow"

REFUSAL_UNKNOWN_CHOICE = "the requested agent choice is not supported"
REFUSAL_READ_BUDGET = "the agent turn has reached its record-read limit"
REFUSAL_SCAN_ALREADY_RECORDED = "a scan is already recorded for the confirmed plan"
REFUSAL_STEP_BUDGET = "the agent turn has reached its step limit"


def decide(
    choice: str,
    arguments: dict,
    *,
    reads_remaining: int,
    steps_remaining: int,
    scan_recorded: bool,
) -> Optional[str]:
    """Return a bounded refusal reason, or ``None`` when the choice may act.

    ``scan_recorded`` is true when the project already holds a terminal
    execution for its confirmed plan. It is the agent-side expression of "at
    most one fresh scan per confirmed Plan"; the orchestration service enforces
    the same rule authoritatively, and this predicate refuses the *choice*
    before any effect is attempted.
    """
    if choice not in agent_contract.CHOICES:
        return REFUSAL_UNKNOWN_CHOICE
    if steps_remaining <= 0:
        return REFUSAL_STEP_BUDGET
    if choice in (agent_contract.CHOICE_CLARIFY, agent_contract.CHOICE_STOP):
        return None
    if choice == agent_contract.CHOICE_ANSWER:
        return None
    if choice == agent_contract.CHOICE_READ_RECORD:
        # The record *name* is already bounded to the contract's closed
        # vocabulary before policy runs; what policy decides here is whether the
        # turn still has budget to read at all.
        if reads_remaining <= 0:
            return REFUSAL_READ_BUDGET
        return None
    if choice == agent_contract.CHOICE_PROPOSE_SCAN:
        if scan_recorded:
            return REFUSAL_SCAN_ALREADY_RECORDED
        return None
    return REFUSAL_UNKNOWN_CHOICE


__all__ = [
    "RECORD_WORKFLOW",
    "REFUSAL_UNKNOWN_CHOICE",
    "REFUSAL_READ_BUDGET",
    "REFUSAL_SCAN_ALREADY_RECORDED",
    "REFUSAL_STEP_BUDGET",
    "decide",
]
