"""The bounded agent turn executor (LANGGRAPH-LOOP-1D).

One call to :func:`run_turn` runs **at most one** bounded agent turn. The turn
itself is a LangGraph ``StateGraph`` (:mod:`hrca.agent.graph`): the model's
typed, validated, policy-permitted choice routes the graph, and a
``read_record`` observation loops back into context assembly. This module owns
everything *around* the graph:

* a single **monotonic whole-turn deadline** (:mod:`hrca.agent.budget`), inside
  the desktop's supervised request envelope, out of which every call, read and
  effect pays;
* **readiness gating** — a turn is refused, not started, until the local graph
  runtime has been initialized (:mod:`hrca.agent.runtime`);
* the durable claim/open/**commit** protocol: intent is written before any
  invocation, and the terminal result *and its idempotency record* commit in
  one transaction before the response is returned.

Nothing here is model-directed beyond the choice itself: the model never names a
root, a command, a capability, a provider, a credential or a policy. It cannot
rescan, cannot widen scope and cannot mint authority. A ``propose_scan`` choice
creates a **draft** plan only; confirmation and the actual scan remain separate
human actions on the existing owner's lifecycle.

When no adapter is configured — the normal runtime today — the executor returns
an honest refusal *without invoking anything*. A scripted adapter is injected
only by tests, and is reached through the Provider interface.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..integrations import provider as provider_mod
from . import budget as budget_mod
from . import choice as choice_mod
from . import context_builder as context_builder_mod
from . import graph as graph_mod
from . import session_store as store_mod
from .graph import (
    MAX_MODEL_CALLS_PER_SESSION,
    MAX_MODEL_CALLS_PER_TURN,
    MAX_READS_PER_TURN,
    MAX_STEPS_PER_TURN,
    STATE_ANSWERED,
    STATE_CLARIFY,
    STATE_PROPOSED,
    STATE_REFUSED,
    STATE_STOPPED,
    STATE_UNKNOWN,
    TASK_PROMPT,
    TURN_RECURSION_LIMIT,
    REFUSAL_BUDGET,
    REFUSAL_NO_ADAPTER,
    REFUSAL_NO_PROJECT,
    REFUSAL_MALFORMED,
    REFUSAL_READ,
    REFUSAL_TURN_BUDGET,
    REFUSAL_UNOBSERVED,
    TurnContext,
    TurnState,
    build_turn_graph,
    graph_available,
    run_turn_graph,
)

#: Raised when the optional graph runtime is not installed in this environment.
REFUSAL_NO_GRAPH = "the agent graph runtime is not installed"
#: The runtime is installed but has not finished initializing. Not a turn.
REFUSAL_NOT_READY = "the agent runtime is not ready"
#: The initialization expired while this process's own load was still running,
#: so the process has to be replaced. Distinct from ``REFUSAL_NOT_READY`` on
#: purpose: this one is not fixed by waiting or by initializing again here.
REFUSAL_RUNTIME_DISPOSED = (
    "the agent runtime initialization expired; this backend must be replaced"
)


class Refused(Exception):
    """A bounded refusal. ``reason`` is safe to surface verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _store(store_base: str, root: Optional[str]) -> store_mod.AgentSessionStore:
    store_mod.assert_outside_project(store_base, root)
    return store_mod.AgentSessionStore(store_base)


def _workflow(
    store_base: str, root: str, deadline: Optional[float] = None
) -> Dict[str, Any]:
    """Read the scoped workflow through its owner's own read interface."""
    from ..orchestration import service as orchestration

    return orchestration.read_workflow(store_base=store_base, root=root, deadline=deadline)


def _session_budget(store: store_mod.AgentSessionStore, project_id: str) -> Dict[str, int]:
    return store.read_state(project_id)["budget"]


def _run_graph(
    *,
    store_base: str,
    root: str,
    project_id: str,
    goal: str,
    adapter: Any,
    workflow: Mapping[str, Any],
    turn_deadline: Optional[float],
    store_deadline: Optional[float] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, int]]:
    """Invoke the turn graph once, given its collaborators and its deadline."""
    from ..orchestration import domain as orchestration_domain
    from ..orchestration import service as orchestration

    def save_draft(draft_goal: str) -> Dict[str, Any]:
        expected = int(workflow.get("plan_revision") or 0) if workflow.get("has_plan") else 0
        return orchestration.save_plan(
            store_base=store_base,
            root=root,
            goal=draft_goal,
            scope=orchestration_domain.Scope.from_payload({}),
            accepted_baseline_ref=None,
            expected_revision=expected,
            idempotency_key=f"agent-draft:{project_id}:{draft_goal[:60]}",
            deadline=store_deadline,
        )

    def read_workflow() -> Dict[str, Any]:
        return _workflow(store_base, root, store_deadline)

    ctx = TurnContext(
        adapter=adapter,
        goal=goal,
        workflow=workflow,
        save_draft=save_draft,
        read_workflow=read_workflow,
        turn_deadline=turn_deadline,
    )
    return run_turn_graph(ctx)


def run_turn(
    *,
    store_base: str,
    root: Optional[str],
    goal: str,
    idempotency_key: str,
    adapter: Optional[provider_mod.Provider] = None,
    runtime_ready: bool = True,
    runtime_disposed: bool = False,
) -> Dict[str, Any]:
    """Run one bounded agent turn and return its recorded outcome."""
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    if not isinstance(goal, str) or not goal.strip():
        raise Refused(REFUSAL_NO_PROJECT)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise Refused(REFUSAL_MALFORMED)

    from ..orchestration import service as orchestration

    # The turn's single monotonic clock starts at ADMISSION, before the claim,
    # the accounting reads and the turn row — so admission is measured work, not
    # an unmeasured prefix. Every store operation below honours the reserved
    # instant derived from it, and so does every model call.
    turn_started = time.monotonic()
    turn_deadline = turn_started + budget_mod.TURN_BUDGET_SECONDS
    store_deadline = budget_mod.store_deadline(turn_deadline)

    store = _store(store_base, root)
    store.set_deadline(store_deadline)
    store.ensure_schema()
    project_id = orchestration.project_id_for(root)
    now = orchestration._now()  # owner's clock, reused; never invented here
    store.ensure_session(project_id, goal.strip(), now)

    request_payload = {"goal": goal.strip(), "adapter": "configured" if adapter is not None else "none"}
    digest = store_mod.payload_digest(request_payload)
    disposition, stored = store.claim(project_id, idempotency_key.strip(), digest, now)
    if disposition == "replay":
        return json.loads(stored)
    if disposition == "pending":
        # A durably in-flight identical turn: never run a second effect.
        raise Refused(REFUSAL_UNOBSERVED)

    # Every claimed turn is durably opened *before* any invocation, so a refusal
    # or an unobserved outcome is as visible in the session history as a
    # completed answer, and a crash mid-turn leaves a turn that reads `unknown`.
    turn_id = store_mod.new_id("turn")
    store.open_turn(project_id, turn_id, idempotency_key.strip(), request_payload)

    steps: List[Dict[str, Any]] = []
    delta: Dict[str, int] = {}
    if adapter is None:
        result = _refusal_result(REFUSAL_NO_ADAPTER)
        runtime_ready = False
    elif not graph_mod.graph_available():
        result = _refusal_result(REFUSAL_NO_GRAPH)
        runtime_ready = False
    elif runtime_disposed:
        # The process itself is written off: waiting here cannot fix it, and
        # initializing again in this process is refused too. Recorded durably
        # like every other refusal, and no effect is attempted.
        result = _refusal_result(REFUSAL_RUNTIME_DISPOSED)
        runtime_ready = False
    elif not runtime_ready:
        # Initialization is explicit and separately owned; a turn is refused,
        # not started, until it has settled. No sync import happens in a turn.
        result = _refusal_result(REFUSAL_NOT_READY)
        runtime_ready = False
    elif _session_budget(store, project_id)["model_calls"] >= MAX_MODEL_CALLS_PER_SESSION:
        result = _refusal_result(REFUSAL_BUDGET)
    else:
        # A turn may make two calls, and each keeps its own identity: the slice
        # is taken from where the adapter stood before the graph ran, so a
        # second call is never reported as if it were the first, and a call that
        # produced no answer is reported as nothing rather than as a guess.
        calls_before = _adapter_calls_len(adapter)
        try:
            workflow = _workflow(store_base, root, store_deadline)
            outcome, steps, delta = _run_graph(
                store_base=store_base,
                root=root,
                project_id=project_id,
                goal=goal.strip(),
                adapter=adapter,
                workflow=workflow,
                turn_deadline=turn_deadline,
                store_deadline=store_deadline,
            )
            result = _bounded_result(
                outcome["turn_state"],
                outcome["choice"],
                outcome["text"],
                outcome["refusal"],
                verified_references=outcome["verified"],
                unsupported_references=outcome["unsupported"],
                plan_id=outcome["plan_id"],
                plan_phase=outcome["plan_phase"],
                provider_calls=_adapter_calls_since(adapter, calls_before),
                context_trace=outcome.get("context_trace"),
                call_records=outcome.get("call_records"),
            )
        except Refused as error:
            result = _refusal_result(error.reason)
        except Exception:
            # Anything the graph boundary could not settle is *unknown*:
            # recorded honestly, never retried, never rounded up to success.
            # The graph returns its own delta when ``invoke`` fails, so reaching
            # this handler means no turn work was counted here. Charging one
            # call would fabricate usage; the answered-record slice is not exact
            # usage either, so it stays a separately reported diagnostic.
            result = _unknown_result()
            result["provider_calls"] = _adapter_calls_since(adapter, calls_before)
            delta = {"model_calls": 0, "steps": 0}

    # Freeze the reply: the terminal result AND its idempotency record commit in
    # one transaction, so a lost response can never lose a committed result and
    # a replay returns exactly what the first call returned.
    state = store.read_state(project_id)
    result = {
        **result,
        "turn_id": turn_id,
        # The exact interval this one request occupied, and its outcome — the
        # measurement the shared budget is enforced against.
        "elapsed_seconds": round(time.monotonic() - turn_started, 3),
        "session": _session_view_from(state, delta, str(result.get("state") or "")),
    }
    store.close_turn(
        project_id,
        turn_id,
        choice=str(result.get("choice") or ""),
        payload={"state": result.get("state"), "text": result.get("text", "")},
        steps=steps,
        budget_delta=delta,
        idempotency_key=idempotency_key.strip(),
        result_json=json.dumps(result, sort_keys=True),
    )
    return result


MAX_PROVIDER_CALLS_PER_TURN = MAX_MODEL_CALLS_PER_TURN

#: The declared keys of one bounded call record. A record is rebuilt from these
#: and only these, so an adapter cannot smuggle an undeclared field — a raw
#: payload, a header, an error body — into the turn result.
_BOUNDED_CALL_KEYS: Tuple[str, ...] = (
    "requested_model",
    "returned_model",
    "fingerprint",
    "outcome",
    "usage_reported",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
)


def _adapter_calls_len(adapter: Any) -> int:
    """Return how many entries the adapter's call list holds, if it keeps one.

    Duck-typed exactly like ``set_turn_deadline``. The count is used only as an
    index, so an adapter whose ``calls`` means something else entirely (a scripted
    double records the *requests* it saw) is harmless: its entries fail the shape
    check below and contribute nothing.
    """
    calls = getattr(adapter, "calls", None)
    return len(calls) if isinstance(calls, list) else 0


#: How many trace records a turn may report, and the bounds on each field. A
#: record is rebuilt from the declared keys only, so nothing undeclared — a raw
#: payload, a header, an error body — can reach a caller through it.
MAX_TRACE_RECORDS = MAX_MODEL_CALLS_PER_TURN
MAX_TRACE_EVIDENCE_IDS = 12
MAX_TRACE_ID_CHARS = 120


def _bounded_context_trace(
    trace: Optional[Sequence[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Return the bounded, diagnostics-only context trace for one turn.

    Type- and size-bounded here as well as at the source, so a caller cannot
    widen it: an observation is truncated to the graph's own fact bound, an id
    list to ``MAX_TRACE_EVIDENCE_IDS`` of ``MAX_TRACE_ID_CHARS`` each, and the
    record count to the turn's model-call cap.
    """
    records: List[Dict[str, Any]] = []
    if not isinstance(trace, (list, tuple)):
        return records
    for record in list(trace)[:MAX_TRACE_RECORDS]:
        if not isinstance(record, Mapping):
            continue
        observation = record.get("observation")
        evidence = record.get("evidence")
        ids: List[str] = []
        if isinstance(evidence, (list, tuple)):
            for item in evidence:
                if isinstance(item, str) and item:
                    ids.append(item[:MAX_TRACE_ID_CHARS])
        records.append(
            {
                "call": record.get("call"),
                "observation": (
                    observation[:context_builder_mod.MAX_FACT_CHARS]
                    if isinstance(observation, str)
                    else ""
                ),
                "evidence": ids[:MAX_TRACE_EVIDENCE_IDS],
                "items": record.get("items"),
            }
        )
    return records


def _adapter_calls_since(adapter: Any, calls_before: int) -> List[Dict[str, Any]]:
    """Return this turn's bounded call records, in order, at most the turn cap.

    Only records carrying the declared metadata marker are carried, and each is
    rebuilt from the declared keys, so nothing outside that closed set can reach
    a caller through the turn result.
    """
    calls = getattr(adapter, "calls", None)
    if not isinstance(calls, list):
        return []
    records: List[Dict[str, Any]] = []
    for record in calls[calls_before:]:
        if not isinstance(record, dict) or "requested_model" not in record:
            continue
        records.append({key: record.get(key) for key in _BOUNDED_CALL_KEYS})
    return records[:MAX_PROVIDER_CALLS_PER_TURN]


#: Bounds for the diagnostics-only per-call records. They are stricter than the
#: transport's own and are re-applied here so an adapter can never smuggle an
#: undeclared field into the turn result.
MAX_CALL_TASK_CHARS = 2_000
MAX_CALL_ID_CHARS = 64
MAX_CALL_REASON_CHARS = 200


def _bounded_str(value: Any, limit: int) -> str:
    """Return a bounded string, or ``""`` when it is not a usable string."""
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else ""


def _bounded_accepted(value: Any) -> Optional[Dict[str, Any]]:
    """Rebuild a validated choice payload from the contract's closed shape.

    This is a defensive re-bound, not a second validator: the graph has already
    validated and normalised the payload. Anything outside the closed shape is
    returned as ``None`` rather than copied.
    """
    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != choice_mod.AGENT_CHOICE_SCHEMA_VERSION:
        return None
    choice = value.get("choice")
    if choice not in choice_mod.CHOICES:
        return None
    rationale = value.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale) > choice_mod.MAX_RATIONALE_CHARS:
        return None
    arguments = value.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    allowed = choice_mod.ARGUMENT_KEYS[choice]
    if set(arguments) - set(allowed):
        return None
    rebuilt: Dict[str, Any] = {}
    for key in allowed:
        if key not in arguments:
            continue
        item = arguments[key]
        if key == "references":
            if not isinstance(item, (list, tuple)):
                return None
            if len(item) > choice_mod.MAX_REFERENCES:
                return None
            references: List[str] = []
            for reference in item:
                if not isinstance(reference, str):
                    return None
                if len(reference) > choice_mod.MAX_REFERENCE_CHARS:
                    return None
                references.append(reference)
            rebuilt[key] = references
        else:
            limit = {
                "answer": choice_mod.MAX_TEXT_CHARS,
                "question": choice_mod.MAX_TEXT_CHARS,
                "limitation": choice_mod.MAX_TEXT_CHARS,
                "goal": choice_mod.MAX_GOAL_CHARS,
                "record": choice_mod.MAX_REFERENCE_CHARS,
            }[key]
            if not isinstance(item, str) or len(item) > limit:
                return None
            rebuilt[key] = item
    if choice == choice_mod.CHOICE_CLARIFY:
        if not rebuilt.get("question", "").strip():
            return None
    elif choice == choice_mod.CHOICE_ANSWER:
        if not rebuilt.get("answer", "").strip():
            return None
        rebuilt.setdefault("references", [])
    elif choice == choice_mod.CHOICE_READ_RECORD:
        record = rebuilt.get("record", "")
        if not record.strip() or record not in choice_mod.ALLOWED_RECORDS:
            return None
    elif choice == choice_mod.CHOICE_PROPOSE_SCAN:
        rebuilt.setdefault("goal", "")
    elif choice == choice_mod.CHOICE_STOP:
        rebuilt.setdefault("limitation", "")
    return {
        "schema_version": choice_mod.AGENT_CHOICE_SCHEMA_VERSION,
        "choice": choice,
        "rationale": rationale,
        "arguments": rebuilt,
    }


def _bounded_call_records(
    records: Optional[Sequence[Mapping[str, Any]]],
) -> List[Dict[str, Any]]:
    """Return the diagnostics-only per-call records for one turn.

    The record is rebuilt from declared keys only. Context is bounded to the
    context builder's own limits; if anything had to be shortened or dropped,
    ``context_truncated`` says so rather than presenting a partial fact as
    complete.
    """
    result: List[Dict[str, Any]] = []
    if not isinstance(records, (list, tuple)):
        return result
    for record in list(records)[:MAX_PROVIDER_CALLS_PER_TURN]:
        if not isinstance(record, Mapping):
            continue
        call = record.get("call")
        if not isinstance(call, int) or isinstance(call, bool):
            call = 0
        context: List[str] = []
        truncated = False
        total = 0
        raw_context = record.get("context")
        if isinstance(raw_context, (list, tuple)):
            if len(raw_context) > context_builder_mod.MAX_CONTEXT_ITEMS:
                truncated = True
            for item in list(raw_context)[: context_builder_mod.MAX_CONTEXT_ITEMS]:
                if not isinstance(item, str):
                    truncated = True
                    continue
                if len(item) > context_builder_mod.MAX_FACT_CHARS:
                    truncated = True
                item = item[: context_builder_mod.MAX_FACT_CHARS]
                encoded = len(item.encode("utf-8"))
                if total + encoded > context_builder_mod.MAX_CONTEXT_BYTES:
                    truncated = True
                    break
                context.append(item)
                total += encoded
        else:
            truncated = True
        result.append(
            {
                "call": call,
                "task_id": _bounded_str(record.get("task_id"), MAX_CALL_ID_CHARS),
                "task": _bounded_str(record.get("task"), MAX_CALL_TASK_CHARS),
                "context": context,
                "context_bytes": total,
                "context_truncated": truncated,
                "dispatched": bool(record.get("dispatched", False)),
                "validation": _bounded_str(record.get("validation"), 16),
                "policy": _bounded_str(record.get("policy"), 16),
                "reason": _bounded_str(record.get("reason"), MAX_CALL_REASON_CHARS),
                "accepted": _bounded_accepted(record.get("accepted")),
            }
        )
    return result


def _bounded_result(
    state: str,
    choice: str,
    text: str,
    refusal: Optional[str],
    *,
    verified_references: Optional[Sequence[str]] = None,
    unsupported_references: Optional[Sequence[str]] = None,
    plan_id: str = "",
    plan_phase: str = "",
    provider_calls: Optional[Sequence[Mapping[str, Any]]] = None,
    context_trace: Optional[Sequence[Mapping[str, Any]]] = None,
    call_records: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "state": state,
        "choice": choice,
        "text": text,
        "refusal": refusal,
        # Bounded, safe metadata about the model calls this turn made, in order:
        # the code-owned requested model, whatever the provider actually
        # returned, and the usage counters. It carries no raw payload, header or
        # reasoning text, and it is diagnostics only — nothing here is a
        # model-facing fact and nothing here grants authority.
        "provider_calls": [dict(call) for call in (provider_calls or ())],
        # What each of this turn's calls was actually handed at inference time:
        # the observation the turn had retrieved and the evidence identifiers
        # its bounded context carried. Diagnostics only — it is rebuilt from the
        # graph's declared keys, it carries no raw source, secret, header or
        # provider payload, and it is never read back into the model's context.
        "context_trace": _bounded_context_trace(context_trace),
        # Diagnostics-only per-call records: the bounded synthetic input and the
        # accepted choice, for the RG developer-evidence projection. They are
        # rebuilt from a closed key set, carry no raw provider payload, header,
        # secret or reasoning text, and are never read back into the model's
        # context. This field grants nothing and changes no oracle.
        "call_records": _bounded_call_records(call_records),
        # ``verified_references`` names only the citations that RESOLVED against
        # a record this turn actually retrieved (see ``graph.answer``). It is
        # reference *resolution*, never a verification of the claim the answer
        # makes: the wording is legible but the answer itself is model-inferred.
        "verified_references": list(verified_references or []),
        "unsupported_references": list(unsupported_references or []),
        # Stated on the DTO so a surface cannot read resolution as semantic
        # success. Both are constant: nothing here can ever verify an answer.
        "reference_resolution": "resolved against this turn's retrieved records",
        "answer_verified": False,
        "plan_id": plan_id,
        "plan_phase": plan_phase,
    }


def _refusal_result(reason: str) -> Dict[str, Any]:
    return _bounded_result(STATE_REFUSED, "", reason, reason)


def _unknown_result() -> Dict[str, Any]:
    return _bounded_result(STATE_UNKNOWN, "", "", REFUSAL_UNOBSERVED)


def _session_view_from(
    state: Mapping[str, Any], delta: Mapping[str, int], outcome_state: str
) -> Dict[str, Any]:
    """Account the session *after* this turn, from the pre-commit snapshot."""
    budget = state.get("budget") or {}
    return {
        "goal": state.get("goal", ""),
        "turn_count": len(state.get("turns") or []),
        "last_turn_state": outcome_state,
        "model_calls": int(budget.get("model_calls", 0)) + int(delta.get("model_calls", 0)),
        "model_call_limit": MAX_MODEL_CALLS_PER_SESSION,
    }


def read_session(*, store_base: str, root: Optional[str]) -> Dict[str, Any]:
    """Return the durable agent-session projection for the scoped project.

    A read: it invokes no graph, no provider and no scanner, and it never
    rewrites a live or uncertain turn.
    """
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    from ..orchestration import service as orchestration

    store = _store(store_base, root)
    store.ensure_schema()
    project_id = orchestration.project_id_for(root)
    state = store.read_state(project_id)
    return {
        "has_session": state["has_session"],
        "goal": state["goal"],
        "budget": state["budget"],
        "model_call_limit": MAX_MODEL_CALLS_PER_SESSION,
        "turns": [
            {
                "turn_id": turn["turn_id"],
                "state": turn["outcome"] or turn["state"],
                "choice": turn["choice"],
                "steps": turn["steps"],
            }
            for turn in state["turns"]
        ],
    }


__all__ = [
    "MAX_STEPS_PER_TURN",
    "MAX_MODEL_CALLS_PER_TURN",
    "MAX_READS_PER_TURN",
    "MAX_MODEL_CALLS_PER_SESSION",
    "TURN_RECURSION_LIMIT",
    "TASK_PROMPT",
    "REFUSAL_NO_PROJECT",
    "REFUSAL_NO_ADAPTER",
    "REFUSAL_NO_GRAPH",
    "REFUSAL_NOT_READY",
    "REFUSAL_BUDGET",
    "REFUSAL_MALFORMED",
    "REFUSAL_UNOBSERVED",
    "REFUSAL_READ",
    "REFUSAL_TURN_BUDGET",
    "STATE_CLARIFY",
    "STATE_ANSWERED",
    "STATE_STOPPED",
    "STATE_PROPOSED",
    "STATE_REFUSED",
    "STATE_UNKNOWN",
    "TurnState",
    "TurnContext",
    "build_turn_graph",
    "graph_available",
    "run_turn_graph",
    "Refused",
    "run_turn",
    "read_session",
]
