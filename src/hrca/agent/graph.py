"""The bounded turn as a LangGraph ``StateGraph`` (LANGGRAPH-LOOP-1).

One agent turn is a small sequential graph. The **model's typed, validated,
policy-permitted choice selects the next node** — that conditional routing is
what LangGraph owns here, and the ``read_record`` observation edge loops back
into context assembly, so the reason → act → observe cycle is the graph's own
structure rather than a custom ``while`` loop with a dispatch chain.

Ownership is deliberately split:

* the graph owns **ephemeral in-turn control state** only (this module's
  :class:`TurnState`), never a durable checkpoint;
* :mod:`hrca.agent.session_store` stays authoritative for session/turn identity,
  accounting, idempotency and budget;
* the orchestration store alone owns Plan / Run / Evidence / Decision.

Nothing here is durable: the graph is compiled per turn, invoked once, and
discarded. There is no checkpointer, no checkpoint database and no automatic
resume — a turn that did not settle stays ``unknown`` in the store and is never
replayed. ``recursion_limit`` is a secondary superstep safeguard; the primary
bounds are the code-owned budgets below.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypedDict

from ..integrations import provider as provider_mod
from . import budget as budget_mod
from . import choice as agent_contract
from . import context_builder as context_mod
from . import policy as policy_mod

# -- code-owned limits (the loop's budgets live with the loop) -------------

MAX_STEPS_PER_TURN = 4
MAX_MODEL_CALLS_PER_TURN = 2
MAX_READS_PER_TURN = 2
MAX_MODEL_CALLS_PER_SESSION = 12

#: The code-owned diagnostics switch for the RG transcript. It is set only in
#: the child environment the RG runner constructs, so a legacy/m5-l0 turn never
#: retains the per-call input/payload record. It is a diagnostics switch, never
#: authority: the model's context, routing and validation are unchanged.
TRANSCRIPT_CAPTURE_ENV = "HRCA_AGENT_TRANSCRIPT_CAPTURE"
TRANSCRIPT_CAPTURE_VALUE = "m5-l0-rg"


def transcript_capture_enabled() -> bool:
    """Return whether the RG diagnostics capture is enabled in this child."""
    return os.environ.get(TRANSCRIPT_CAPTURE_ENV, "") == TRANSCRIPT_CAPTURE_VALUE

#: Secondary safeguard: the number of *supersteps* the graph may take. It is
#: NOT the model-call or session budget — those are the constants above, checked
#: in-node, and they are what a caller observes. This only stops a routing bug
#: from spinning.
TURN_RECURSION_LIMIT = 12

#: The bounded system task handed to the adapter. It states the closed choice
#: vocabulary; the validator, never this text, is what enforces it.
TASK_PROMPT = (
    "You are a bounded coding assistant inside PrimaAgent. Reply with one JSON "
    "object and nothing else. It must have keys: schema_version (exactly "
    '"' + agent_contract.AGENT_CHOICE_SCHEMA_VERSION + '"'
    "), choice (one of "
    + ", ".join(agent_contract.CHOICES)
    + "), rationale (short text), and arguments (an object). Arguments per "
    "choice: clarify={question}; answer={answer, references}; "
    "read_record={record} with record='workflow' (the same-turn observation is "
    "authoritative; answer from it; don't repeat a redundant read; don't cite "
    "context-only IDs; if insufficient, clarify/stop); propose_scan={goal}; "
    "stop={limitation}. You may not run commands, read files, change scope or "
    "approve anything."
)

# -- bounded refusals (code-owned sentences) ------------------------------

REFUSAL_NO_PROJECT = "no project is open"
REFUSAL_NO_ADAPTER = "no agent model adapter is configured"
REFUSAL_BUDGET = "the agent session has reached its model-call budget"
REFUSAL_MALFORMED = "the agent model returned an answer this slice does not accept"
REFUSAL_UNOBSERVED = "the agent model call did not return an observed outcome"
REFUSAL_READ = "the requested record could not be read"
#: The whole-turn budget ran out *before* an effect was dispatched. A refusal,
#: never an uncertain outcome: nothing was started.
REFUSAL_TURN_BUDGET = "the turn's time budget is exhausted"
#: The graph itself did not settle. Deliberately generic: it is not a claim of
#: step/call exhaustion, an owner failure or any other cause, and it never
#: carries an exception message, a class name or provider data.
REFUSAL_GRAPH_FAILURE = "the agent turn failed before it could settle"

#: Terminal turn states.
STATE_CLARIFY = "clarify"
STATE_ANSWERED = "answered"
STATE_STOPPED = "stopped"
STATE_PROPOSED = "proposed"
STATE_REFUSED = "refused"
STATE_UNKNOWN = "unknown"

#: Graph node names.
NODE_CONTEXT = "assemble_context"
NODE_MODEL = "model_choice"
NODE_POLICY = "validate_and_policy"
NODE_READ = "read_record"
NODE_PROPOSE = "propose_scan"
NODE_CLARIFY = "clarify"
NODE_ANSWER = "answer"
NODE_STOP = "stop"
NODE_FINISH = "finish"


class TurnState(TypedDict, total=False):
    """Ephemeral control state for one turn. Never persisted."""

    goal: str
    plan: Optional[Dict[str, Any]]
    scan_recorded: bool
    observation: Optional[str]
    retrieved_evidence: List[str]
    context_trace: List[Dict[str, Any]]
    #: Diagnostics-only per-call record. It carries the bounded synthetic input
    #: and the accepted choice so a developer-evidence projection can be built
    #: later. It is never read back into context and grants no authority.
    call_records: List[Dict[str, Any]]
    steps: List[Dict[str, Any]]
    calls: int
    reads: int
    payload: Optional[Dict[str, Any]]
    arguments: Dict[str, Any]
    choice: str
    turn_state: str
    text: str
    refusal: Optional[str]
    verified: List[str]
    unsupported: List[str]
    plan_id: str
    plan_phase: str
    done: bool


class TurnContext:
    """The collaborators one turn graph needs. Supplied by the executor."""

    def __init__(
        self,
        *,
        adapter: Any,
        goal: str,
        workflow: Mapping[str, Any],
        save_draft,
        read_workflow,
        turn_deadline: Optional[float] = None,
    ) -> None:
        self.adapter = adapter
        self.goal = goal
        self.workflow = workflow
        self.save_draft = save_draft
        self.read_workflow = read_workflow
        #: The turn's single monotonic deadline. ``None`` means the caller owns
        #: no time budget (a direct graph run in a unit test).
        self.turn_deadline = turn_deadline

    def allowance(self) -> Optional[float]:
        """Return the seconds a call may take now, or ``None`` when unbounded."""
        if self.turn_deadline is None:
            return None
        import time

        return budget_mod.call_allowance(self.turn_deadline, time.monotonic())


def _scan_recorded(workflow: Mapping[str, Any]) -> bool:
    review = workflow.get("review")
    review = review if isinstance(review, Mapping) else {}
    return bool(review.get("run_outcome"))


def _observation_text(plan: Optional[Mapping[str, Any]], scan_recorded: bool) -> str:
    if plan is None:
        return "the project has no recorded plan"
    if not scan_recorded:
        return (
            "plan phase={phase} revision={revision}; no scan has been run".format(
                phase=plan.get("plan_phase"), revision=plan.get("plan_revision")
            )
        )
    return (
        "plan phase={phase} revision={revision}; scan outcome={outcome} freshness={freshness}; "
        "{count} evidence record(s)".format(
            phase=plan.get("plan_phase"),
            revision=plan.get("plan_revision"),
            outcome=plan.get("run_outcome"),
            freshness=plan.get("freshness"),
            count=len(plan.get("evidence") or []),
        )
    )


#: The bounded shape of one context-trace record. Rebuilt from these keys and
#: only these, so the record cannot grow an undeclared field.
CONTEXT_TRACE_KEYS: Tuple[str, ...] = ("call", "observation", "evidence", "items")


def _bounded_trace_text(value: Any) -> str:
    """Return the already-bounded observation text, or ``""``."""
    if not isinstance(value, str):
        return ""
    return value[: context_mod.MAX_FACT_CHARS]


def _trace_evidence_ids(items: Sequence[str]) -> List[str]:
    """Return the evidence ids the context actually carried, bounded in place."""
    ids: List[str] = []
    for item in items:
        if not isinstance(item, str) or not item.startswith("EVIDENCE: "):
            continue
        parts = item.split()
        if len(parts) > 1 and len(ids) < context_mod.MAX_EVIDENCE_REFS:
            ids.append(parts[1][:120])
    return ids


def _terminal(state: TurnState, **fields: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"done": True}
    out.update(fields)
    # The context trace is a record of what was handed to inference, so it
    # travels with the terminal state rather than being dropped at the end of
    # the turn — otherwise only a refused turn would be able to report it.
    if "context_trace" not in out:
        out["context_trace"] = list(state.get("context_trace", []))
    if "call_records" not in out:
        out["call_records"] = list(state.get("call_records", []))
    return out


def _decided_call_records(
    state: TurnState,
    *,
    validation: str,
    policy: str,
    reason: str,
    accepted: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return call records with the last call's decision attached.

    Diagnostics only: the record is copied, never mutated in place, and the
    routing decision is returned by the caller exactly as before. If the input
    record is unexpectedly absent (a direct graph caller), a minimal empty
    record is appended rather than inventing a context. When the RG capture
    switch is off, nothing is added and the existing (empty) list is returned.
    """
    if not transcript_capture_enabled():
        return list(state.get("call_records", []))
    call = int(state.get("calls", 0))
    records = [
        dict(record)
        for record in state.get("call_records", [])
        if isinstance(record, dict)
    ]
    decision = {
        "validation": validation,
        "policy": policy,
        "reason": reason,
        "accepted": accepted,
    }
    for index in range(len(records) - 1, -1, -1):
        if records[index].get("call") == call:
            records[index].update(decision)
            return records
    records.append(
        {
            "call": call,
            "task_id": "",
            "task": "",
            "context": [],
            "context_bytes": 0,
            "dispatched": False,
            **decision,
        }
    )
    return records


#: The terminal fields a completed node may have decided before the graph
#: failed. Rebuilt from this closed set and nothing else.
_TERMINAL_FIELDS: Tuple[str, ...] = (
    "turn_state",
    "choice",
    "text",
    "refusal",
    "verified",
    "unsupported",
    "plan_id",
    "plan_phase",
)


class _TurnProgress:
    """What each completed node recorded, kept outside LangGraph's state.

    LangGraph returns its final state only when ``invoke`` settles; an exception
    discards it. The nodes already count what they spent and decide their
    terminal outcome, so those exact values are observed after each node
    returns. The record is only a fallback for a turn that did not settle: on a
    normal turn the graph's own final state remains authoritative. Nothing here
    is reconstructed after the fact, and no choice or observation is invented.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.reads = 0
        self.steps: List[Dict[str, Any]] = []
        self.context_trace: List[Dict[str, Any]] = []
        self.call_records: List[Dict[str, Any]] = []
        #: The last bounded terminal a node actually returned, if one did. A
        #: graph failure after that point must not erase a decision that was
        #: already made (for example: the call budget was exhausted) and
        #: misreport it as an unknown failure.
        self.terminal: Optional[Dict[str, Any]] = None

    def observe(self, update: Mapping[str, Any]) -> None:
        if "calls" in update:
            self.calls = int(update.get("calls") or 0)
        if "reads" in update:
            self.reads = int(update.get("reads") or 0)
        steps = update.get("steps")
        if isinstance(steps, list):
            self.steps = list(steps)
        trace = update.get("context_trace")
        if isinstance(trace, list):
            self.context_trace = list(trace)
        records = update.get("call_records")
        if isinstance(records, list):
            self.call_records = list(records)
        if update.get("done"):
            self.terminal = {
                key: update.get(key) for key in _TERMINAL_FIELDS
            }


def graph_available() -> bool:
    """Return whether the optional LangGraph runtime is installed.

    Uses :func:`importlib.util.find_spec`, which *locates* the package without
    importing it — so asking the question never loads LangGraph, never loads a
    transitive transport and never enables tracing. The runtime itself is
    imported only inside :func:`build_turn_graph`. It is an optional extra
    precisely so the core stays dependency-free; when it is absent the executor
    refuses honestly instead of failing to import.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("langgraph") is not None
    except (ImportError, ValueError):
        return False


def load_runtime() -> bool:
    """Load the LangGraph runtime into **this** process, and only that.

    This is the exact import :func:`build_turn_graph` performs, exposed so the
    initialization step can pay it *outside* a turn. A child process can compile
    the bytecode to disk, but it can never populate the parent's ``sys.modules``:
    the parent's own import is real interpreter work (measured at ~4 s on an idle
    Windows host) and, left inside the first turn, it is charged against the
    turn's 12 s budget — where a loaded host can push it past the budget and the
    turn then refuses honestly, which is a refusal a developer should never have
    been able to reach.

    It imports nothing else: no adapter, no credential, no socket, no tracing
    switch. ``ImportError`` and ``ValueError`` — the two failures an absent or
    unlocatable package raises — are reported as a plain ``False``.
    """
    try:
        from langgraph.graph import END, START, StateGraph  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def build_turn_graph(
    ctx: TurnContext, progress: Optional[_TurnProgress] = None
):
    """Compile the turn graph for one turn's context.

    Imported lazily by the executor so ``import hrca.agent.executor`` never
    requires LangGraph. Importing this module pulls in LangGraph only.

    ``progress`` is supplied by :func:`run_turn_graph` so a graph that does not
    settle can still report the work its completed nodes counted. A caller that
    compiles the graph directly (a unit test) gets its own private recorder.
    """
    from langgraph.graph import END, START, StateGraph

    progress = progress if progress is not None else _TurnProgress()

    def _observed(node):
        """Record a node's counted update, then hand it back unchanged."""

        def run_node(state: TurnState) -> Dict[str, Any]:
            update = node(state)
            progress.observe(update)
            return update

        return run_node

    def assemble_context(state: TurnState) -> Dict[str, Any]:
        """Pure: nothing here retrieves or decides."""
        return {}

    def model_choice(state: TurnState) -> Dict[str, Any]:
        calls = int(state.get("calls", 0))
        steps = list(state.get("steps", []))
        if calls >= MAX_MODEL_CALLS_PER_TURN or len(steps) >= MAX_STEPS_PER_TURN:
            return _terminal(
                state,
                turn_state=STATE_REFUSED,
                choice="",
                refusal=policy_mod.REFUSAL_STEP_BUDGET,
            )
        # Hand the adapter the turn's single deadline (duck-typed; the Provider
        # interface is unchanged), then check the remaining allowance BEFORE
        # building or dispatching anything.
        setter = getattr(ctx.adapter, "set_turn_deadline", None)
        if callable(setter) and ctx.turn_deadline is not None:
            setter(ctx.turn_deadline)
        allowance = ctx.allowance()
        if allowance is not None and allowance <= 0:
            return _terminal(
                state,
                turn_state=STATE_REFUSED,
                choice="",
                refusal=REFUSAL_TURN_BUDGET,
            )
        items, context_bytes = context_mod.build_context(
            goal=ctx.goal, plan=state.get("plan"), observation=state.get("observation")
        )
        # A bounded, diagnostics-only record of what this call was *actually*
        # handed at inference time: the observation the turn had retrieved, the
        # evidence identifiers its context carried, and how many items it saw.
        # Every field is already bounded and sanitized by the context builder or
        # by the read that set it, so this record cannot carry raw source, a
        # secret, a header or any provider payload — and it changes nothing the
        # model receives or may cite.
        trace = list(state.get("context_trace", []))
        trace.append(
            {
                "call": calls + 1,
                "observation": _bounded_trace_text(state.get("observation")),
                "evidence": _trace_evidence_ids(items),
                "items": len(items),
            }
        )
        # The input half of the transcript record. It is built before dispatch
        # so a local refusal can still show exactly what would have been sent,
        # with ``dispatched`` false. The accepted payload is attached later by
        # ``validate_and_policy``. Nothing here is read back into the model's
        # context and nothing here grants authority.
        capture = transcript_capture_enabled()
        call_records = list(state.get("call_records", []))
        if capture:
            call_records.append(
                {
                    "call": calls + 1,
                    "task_id": f"agent-turn-{calls + 1}",
                    "task": TASK_PROMPT,
                    "context": list(items),
                    "context_bytes": int(context_bytes),
                    "dispatched": False,
                    "validation": "none",
                    "policy": "not-run",
                    "reason": "",
                    "accepted": None,
                }
            )
        request = provider_mod.ProviderRequest(
            task_id=f"agent-turn-{calls + 1}", task=TASK_PROMPT, context=items
        )
        try:
            result = ctx.adapter.generate(request)
        except budget_mod.DispatchRefused as refusal:
            # A local admission rule refused *before* dispatching — an exhausted
            # live gate, an over-bound payload, a missing credential. Nothing
            # left the machine, so this is a refusal, never an uncertain attempt.
            if capture:
                call_records[-1]["reason"] = refusal.reason
            return _terminal(
                state,
                turn_state=STATE_REFUSED,
                choice="",
                refusal=refusal.reason,
                call_records=call_records,
            )
        except budget_mod.TurnBudgetExhausted:
            # Nothing was dispatched, so nothing can have happened remotely:
            # a refusal, never an uncertain outcome, and never a retry.
            if capture:
                call_records[-1]["reason"] = REFUSAL_TURN_BUDGET
            return _terminal(
                state,
                turn_state=STATE_REFUSED,
                choice="",
                refusal=REFUSAL_TURN_BUDGET,
                call_records=call_records,
            )
        except Exception:
            # A call that raised without an observed outcome is *unknown*: it is
            # never retried, at any layer.
            if capture:
                call_records[-1]["dispatched"] = True
            return _terminal(
                state,
                calls=calls + 1,
                steps=steps + [{"kind": "unknown"}],
                turn_state=STATE_UNKNOWN,
                choice="",
                refusal=REFUSAL_UNOBSERVED,
                call_records=call_records,
            )
        if capture:
            call_records[-1]["dispatched"] = True
        return {
            "calls": calls + 1,
            "payload": result.structured_payload,
            "context_trace": trace,
            "call_records": call_records,
            "done": False,
        }

    def validate_and_policy(state: TurnState) -> Dict[str, Any]:
        if state.get("done"):
            return {}
        payload = state.get("payload")
        reason = agent_contract.validate_choice_payload(payload)
        if reason is not None:
            return {
                "steps": list(state.get("steps", [])) + [{"kind": "rejected", "reason": reason}],
                "turn_state": STATE_REFUSED,
                "choice": "",
                "refusal": reason,
                "done": True,
                "call_records": _decided_call_records(
                    state,
                    validation="refused",
                    policy="not-run",
                    reason=reason,
                    accepted=None,
                ),
            }
        selected = agent_contract.normalize_choice_payload(payload)
        kind = selected["choice"]
        arguments = selected["arguments"]
        refusal = policy_mod.decide(
            kind,
            arguments,
            reads_remaining=max(0, MAX_READS_PER_TURN - int(state.get("reads", 0))),
            steps_remaining=max(0, MAX_STEPS_PER_TURN - len(state.get("steps", []))),
            scan_recorded=bool(state.get("scan_recorded")),
        )
        if refusal is not None:
            return {
                "steps": list(state.get("steps", [])) + [
                    {"kind": "refused", "choice": kind, "reason": refusal}
                ],
                "turn_state": STATE_REFUSED,
                "choice": kind,
                "refusal": refusal,
                "done": True,
                "call_records": _decided_call_records(
                    state,
                    validation="accepted",
                    policy="refused",
                    reason=refusal,
                    accepted=selected,
                ),
            }
        return {
            "choice": kind,
            "arguments": arguments,
            "done": False,
            "call_records": _decided_call_records(
                state,
                validation="accepted",
                policy="permitted",
                reason="",
                accepted=selected,
            ),
        }

    def route_choice(state: TurnState) -> str:
        if state.get("done"):
            return NODE_FINISH
        return {
            agent_contract.CHOICE_CLARIFY: NODE_CLARIFY,
            agent_contract.CHOICE_ANSWER: NODE_ANSWER,
            agent_contract.CHOICE_STOP: NODE_STOP,
            agent_contract.CHOICE_READ_RECORD: NODE_READ,
            agent_contract.CHOICE_PROPOSE_SCAN: NODE_PROPOSE,
        }.get(state.get("choice", ""), NODE_FINISH)

    def clarify(state: TurnState) -> Dict[str, Any]:
        question = state["arguments"]["question"]
        return _terminal(
            state,
            steps=list(state.get("steps", [])) + [{"kind": "clarify", "question": question}],
            turn_state=STATE_CLARIFY,
            text=question,
        )

    def stop(state: TurnState) -> Dict[str, Any]:
        limitation = state["arguments"].get("limitation") or "the goal is outside this slice"
        return _terminal(
            state,
            steps=list(state.get("steps", [])) + [{"kind": "stop", "limitation": limitation}],
            turn_state=STATE_STOPPED,
            text=limitation,
        )

    def answer(state: TurnState) -> Dict[str, Any]:
        text = state["arguments"]["answer"]
        references = list(state["arguments"].get("references") or [])
        retrieved = list(state.get("retrieved_evidence", []))
        verified = [ref for ref in references if ref in retrieved]
        unsupported = [ref for ref in references if ref not in retrieved]
        return _terminal(
            state,
            steps=list(state.get("steps", [])) + [
                {"kind": "answer", "verified": verified, "unsupported": unsupported}
            ],
            turn_state=STATE_ANSWERED,
            text=text,
            verified=verified,
            unsupported=unsupported,
        )

    def read_record(state: TurnState) -> Dict[str, Any]:
        reads = int(state.get("reads", 0))
        if reads >= MAX_READS_PER_TURN:
            return _terminal(
                state,
                steps=list(state.get("steps", [])) + [
                    {"kind": "refused", "choice": agent_contract.CHOICE_READ_RECORD,
                     "reason": policy_mod.REFUSAL_READ_BUDGET}
                ],
                turn_state=STATE_REFUSED,
                choice=agent_contract.CHOICE_READ_RECORD,
                refusal=policy_mod.REFUSAL_READ_BUDGET,
            )
        allowance = ctx.allowance()
        if allowance is not None and allowance <= 0:
            return _terminal(
                state,
                turn_state=STATE_REFUSED,
                choice=agent_contract.CHOICE_READ_RECORD,
                refusal=REFUSAL_TURN_BUDGET,
            )
        plan = context_mod.summarize_workflow(ctx.workflow)
        evidence = [row["evidence_id"] for row in (plan or {}).get("evidence", [])]
        return {
            "reads": reads + 1,
            "plan": plan,
            "retrieved_evidence": evidence,
            "observation": _observation_text(plan, bool(state.get("scan_recorded"))),
            "steps": list(state.get("steps", [])) + [
                {"kind": "read_record", "record": state["arguments"]["record"]}
            ],
            "done": False,
        }

    def propose_scan(state: TurnState) -> Dict[str, Any]:
        # An effect: the turn must still have allowance before it is written.
        allowance = ctx.allowance()
        if allowance is not None and allowance <= 0:
            return _terminal(
                state,
                turn_state=STATE_REFUSED,
                choice=agent_contract.CHOICE_PROPOSE_SCAN,
                refusal=REFUSAL_TURN_BUDGET,
            )
        draft_goal = (state["arguments"].get("goal") or ctx.goal).strip() or ctx.goal
        saved = ctx.save_draft(draft_goal)
        fresh = ctx.read_workflow()
        return _terminal(
            state,
            steps=list(state.get("steps", [])) + [
                {"kind": "propose_scan", "plan_id": saved.get("plan_id")}
            ],
            turn_state=STATE_PROPOSED,
            text="A draft plan is ready. Confirm it, then run the scan separately.",
            plan_id=str(saved.get("plan_id") or ""),
            plan_phase=str(fresh.get("plan_phase") or "draft"),
        )

    def finish(state: TurnState) -> Dict[str, Any]:
        return {}

    graph = StateGraph(TurnState)
    graph.add_node(NODE_CONTEXT, _observed(assemble_context))
    graph.add_node(NODE_MODEL, _observed(model_choice))
    graph.add_node(NODE_POLICY, _observed(validate_and_policy))
    graph.add_node(NODE_READ, _observed(read_record))
    graph.add_node(NODE_PROPOSE, _observed(propose_scan))
    graph.add_node(NODE_CLARIFY, _observed(clarify))
    graph.add_node(NODE_ANSWER, _observed(answer))
    graph.add_node(NODE_STOP, _observed(stop))
    graph.add_node(NODE_FINISH, _observed(finish))

    graph.add_edge(START, NODE_CONTEXT)
    graph.add_edge(NODE_CONTEXT, NODE_MODEL)
    graph.add_edge(NODE_MODEL, NODE_POLICY)
    graph.add_conditional_edges(
        NODE_POLICY,
        route_choice,
        {
            NODE_CLARIFY: NODE_CLARIFY,
            NODE_ANSWER: NODE_ANSWER,
            NODE_STOP: NODE_STOP,
            NODE_READ: NODE_READ,
            NODE_PROPOSE: NODE_PROPOSE,
            NODE_FINISH: NODE_FINISH,
        },
    )
    # The bounded observation loop: a record read feeds the next choice.
    graph.add_edge(NODE_READ, NODE_CONTEXT)
    for terminal in (NODE_CLARIFY, NODE_ANSWER, NODE_STOP, NODE_PROPOSE, NODE_FINISH):
        graph.add_edge(terminal, END)
    return graph.compile()


def run_turn_graph(ctx: TurnContext) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, int]]:
    """Invoke one turn graph and return ``(outcome, steps, budget_delta)``."""
    progress = _TurnProgress()
    app = build_turn_graph(ctx, progress=progress)
    initial: TurnState = {
        "goal": ctx.goal,
        "plan": context_mod.summarize_workflow(ctx.workflow),
        "scan_recorded": _scan_recorded(ctx.workflow),
        "observation": None,
        "retrieved_evidence": [],
        #: One bounded record per call of what that call was actually handed.
        #: Diagnostics only: it is never read back into the model's context and
        #: it grants no authority.
        "context_trace": [],
        "call_records": [],
        "steps": [],
        "calls": 0,
        "reads": 0,
        "payload": None,
        "choice": "",
        "turn_state": STATE_REFUSED,
        "text": "",
        "refusal": None,
        "verified": [],
        "unsupported": [],
        "plan_id": "",
        "plan_phase": "",
        "done": False,
    }
    try:
        final = app.invoke(initial, {"recursion_limit": TURN_RECURSION_LIMIT})
    except Exception:
        # The graph did not settle. Its final state is gone, so the counts come
        # from what the completed nodes recorded, not from the provider slice:
        # an answered-call record is not exact usage, and a dispatched call with
        # no answer would be missing from it.
        delta = {
            "model_calls": progress.calls,
            "steps": len(progress.steps),
            "reads": progress.reads,
        }
        decided = progress.terminal
        if decided is not None and decided.get("turn_state") in (
            STATE_REFUSED,
            STATE_UNKNOWN,
        ):
            # Only a demonstrated **failure** terminal is recovered: a refusal
            # or an unknown outcome. The node had already decided it, so the
            # graph failing while finishing does not erase it.
            outcome = {
                "turn_state": str(decided.get("turn_state") or STATE_REFUSED),
                "choice": str(decided.get("choice") or ""),
                "text": str(decided.get("text") or ""),
                "refusal": decided.get("refusal"),
                "verified": list(decided.get("verified") or []),
                "unsupported": list(decided.get("unsupported") or []),
                "plan_id": str(decided.get("plan_id") or ""),
                "plan_phase": str(decided.get("plan_phase") or ""),
                "context_trace": list(progress.context_trace),
                "call_records": list(progress.call_records),
            }
        else:
            # No failure terminal was decided — or a success-like one (answer,
            # proposed, stop, clarify) was. A success-like terminal is NOT
            # returned after this boundary error: the graph did not settle, so
            # presenting it would be indistinguishable from a normal answer or
            # plan. The observed counts, step records and available trace are
            # retained for diagnosis; the exception message or class is not
            # carried. A refusal here does not prove that no owner effect
            # occurred: a node may have completed an owner write before raising
            # (for example, ``propose_scan`` saves a draft before its read).
            outcome = {
                "turn_state": STATE_REFUSED,
                "choice": "",
                "text": "",
                "refusal": REFUSAL_GRAPH_FAILURE,
                "verified": [],
                "unsupported": [],
                "plan_id": "",
                "plan_phase": "",
                "context_trace": list(progress.context_trace),
                "call_records": list(progress.call_records),
            }
        return outcome, list(progress.steps), delta

    steps = list(final.get("steps", []))
    outcome = {
        "turn_state": str(final.get("turn_state") or STATE_REFUSED),
        "choice": str(final.get("choice") or ""),
        "text": str(final.get("text") or ""),
        "refusal": final.get("refusal"),
        "verified": list(final.get("verified", [])),
        "unsupported": list(final.get("unsupported", [])),
        "plan_id": str(final.get("plan_id") or ""),
        "plan_phase": str(final.get("plan_phase") or ""),
        "context_trace": list(final.get("context_trace", [])),
        "call_records": list(final.get("call_records", [])),
    }
    delta = {
        "model_calls": int(final.get("calls", 0)),
        "steps": len(steps),
        "reads": int(final.get("reads", 0)),
    }
    return outcome, steps, delta


__all__ = [
    "MAX_STEPS_PER_TURN",
    "MAX_MODEL_CALLS_PER_TURN",
    "MAX_READS_PER_TURN",
    "MAX_MODEL_CALLS_PER_SESSION",
    "TURN_RECURSION_LIMIT",
    "TRANSCRIPT_CAPTURE_ENV",
    "TRANSCRIPT_CAPTURE_VALUE",
    "transcript_capture_enabled",
    "TASK_PROMPT",
    "REFUSAL_NO_PROJECT",
    "REFUSAL_NO_ADAPTER",
    "REFUSAL_BUDGET",
    "REFUSAL_MALFORMED",
    "REFUSAL_UNOBSERVED",
    "REFUSAL_READ",
    "REFUSAL_TURN_BUDGET",
    "REFUSAL_GRAPH_FAILURE",
    "STATE_CLARIFY",
    "STATE_ANSWERED",
    "STATE_STOPPED",
    "STATE_PROPOSED",
    "STATE_REFUSED",
    "STATE_UNKNOWN",
    "TurnState",
    "TurnContext",
    "graph_available",
    "load_runtime",
    "build_turn_graph",
    "run_turn_graph",
]
