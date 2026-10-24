"""The dedicated provider worker: a fixed, code-owned child entry point.

The worker exists so the **parent** can own a hard deadline over a blocking
synchronous model call. It is deliberately *not* a general command runner: there
is no shell, no argv from a model or a user, no arbitrary import or factory path
and no executable payload. The parent spawns exactly this module with exactly
this argument list, sends one bounded JSON request on stdin and reads at most one
bounded JSON answer from stdout.

What the worker may receive: a **closed scripted-scenario name** plus one
bounded :class:`~hrca.integrations.provider.ProviderRequest` shape (task id,
task text, context items) — all already validated, allowlisted and bounded by
the parent. What it never receives: a project root, a credential, a transcript,
raw source, a Memory document, or any path it could read.

The scripted scenarios below are offline deterministic fixtures for acceptance,
and they are the only names ``SCENARIOS`` admits. Exactly one further path
exists: ``deepseek_live.LIVE_SCENARIO``, which makes **one** bounded call to the
one approved model over the fixed official HTTPS endpoint. It is reachable only
from a parent that was explicitly configured for it — the name is outside
``SCENARIOS``, so no ``HRCA_AGENT_OFFLINE_SCENARIO`` value selects it, and the
credential it needs is injected into this child's private environment by that
parent alone and never crosses the request or the answer.

**A local worker is not an OS sandbox.** It is a separate process with a scrubbed
environment and a synthetic working directory, which bounds *lifetime* and
*I/O shape*; it is not an isolation or security boundary against malicious code.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional

#: Bounded reads. A request larger than this is refused rather than buffered.
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CONTEXT_ITEMS = 16

#: The closed scenario registry. A name outside this mapping is refused by name;
#: there is no dynamic import, no dotted path and no callable lookup.
SCENARIOS = (
    "clarify_once",
    "stop_once",
    "answer_once",
    "read_then_answer",
    "propose_scan",
    "journey_full",
    "malformed_once",
    "forged_tool_once",
    "crash",
    "hang",
    "slow_once",
    "huge_once",
    "truncated_once",
    "slow_second",
    "slow_success",
    "slow_settle",
    "read_then_cite",
    "journey_cite",
    "cite_foreign_once",
)

REASON_UNKNOWN_SCENARIO = "the requested worker scenario is not supported"
REASON_BAD_REQUEST = "the worker request is not a bounded JSON object"
REASON_OVERSIZED = "the worker request exceeds the bounded size"
REASON_RESPONSE_OVERSIZED = "the worker answer exceeds the bounded size"

_SCHEMA = "1.0.0"


def _choice(kind: str, arguments: Dict[str, Any], rationale: str = "scripted") -> Dict[str, Any]:
    return {
        "schema_version": _SCHEMA,
        "choice": kind,
        "rationale": rationale,
        "arguments": arguments,
    }


def _payloads(scenario: str, request: Dict[str, Any]) -> List[Any]:
    """Return the ordered scripted answers for one scenario.

    Deterministic and offline: the sequence is fixed by the scenario name, never
    by observed content, so a test can pin the exact model calls a turn makes.
    """
    goal = ""
    arguments = request.get("arguments")
    if isinstance(arguments, dict):
        goal = str(arguments.get("goal") or "")
    if scenario == "clarify_once":
        return [_choice("clarify", {"question": "Which package should I inspect?"})]
    if scenario == "stop_once":
        return [_choice("stop", {"limitation": "this goal is outside the read-only slice"})]
    if scenario == "answer_once":
        return [_choice("answer", {"answer": "Nothing has been observed yet.", "references": []})]
    if scenario == "read_then_answer":
        return [
            _choice("read_record", {"record": "workflow"}),
            _choice(
                "answer",
                {"answer": "The scoped workflow was read.", "references": []},
            ),
        ]
    if scenario == "propose_scan":
        return [_choice("propose_scan", {"goal": goal or "inspect the project"})]
    if scenario == "journey_full":
        # The whole bounded journey in one closed sequence, so a supervised
        # session needs exactly one adapter for both turns: turn one proposes a
        # draft, turn two reads the workflow and answers citing the first
        # evidence id its own context disclosed — so the citation RESOLVES
        # against what the turn retrieved.
        evidence = ""
        for item in request.get("context") or []:
            text = str(item)
            if text.startswith("EVIDENCE: "):
                parts = text.split()
                if len(parts) > 1:
                    evidence = parts[1]
                break
        return [
            _choice("propose_scan", {"goal": goal or "inspect the project"}),
            _choice("read_record", {"record": "workflow"}),
            _choice(
                "answer",
                {
                    "answer": "The scoped workflow was read; see the resolved reference.",
                    "references": [evidence] if evidence else [],
                },
            ),
        ]
    if scenario == "read_then_cite":
        # One turn, two calls: read the scoped workflow, then answer citing the
        # first evidence id the *observation* put in front of the second call.
        # The citation can only resolve if the observation really reached the
        # next model context, which is what makes it an observation oracle
        # rather than a reference-membership check.
        evidence = ""
        for item in request.get("context") or []:
            text = str(item)
            if text.startswith("EVIDENCE: "):
                parts = text.split()
                if len(parts) > 1:
                    evidence = parts[1]
                break
        return [
            _choice("read_record", {"record": "workflow"}),
            _choice(
                "answer",
                {
                    "answer": "The scoped workflow was read; see the resolved reference.",
                    "references": [evidence] if evidence else [],
                },
            ),
        ]
    if scenario == "journey_cite":
        # The two-call journey: propose a draft, then — after the human
        # confirmed and the separate run recorded its evidence — answer citing
        # the evidence id the *scan* put in front of the second call. It answers
        # directly rather than reading first, so the case costs exactly the two
        # calls its allocation allows.
        evidence = ""
        for item in request.get("context") or []:
            text = str(item)
            if text.startswith("EVIDENCE: "):
                parts = text.split()
                if len(parts) > 1:
                    evidence = parts[1]
                break
        return [
            _choice("propose_scan", {"goal": goal or "inspect the project"}),
            _choice(
                "answer",
                {
                    "answer": "The recorded scan produced the cited evidence.",
                    "references": [evidence] if evidence else [],
                },
            ),
        ]
    if scenario == "cite_foreign_once":
        # The negative oracle for citation provenance: the turn *does* read the
        # record, and then answers citing an id no record could hold. Resolution
        # must refuse it — a read in the turn is not a licence to cite anything.
        return [
            _choice("read_record", {"record": "workflow"}),
            _choice(
                "answer",
                {
                    "answer": "A claim resting on a record that does not exist.",
                    "references": ["ev:00000000000000000000000000000000"],
                },
            ),
        ]
    if scenario == "malformed_once":
        return [{"not": "a choice"}]
    if scenario == "forged_tool_once":
        return [_choice("run_shell", {"command": "rm -rf /"})]
    return []


def _write(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(line.encode("utf-8")) > MAX_RESPONSE_BYTES:
        # The worker bounds its own answer. The parent bounds it again on read;
        # neither side ever emits or accepts an unbounded line.
        line = json.dumps(
            {"ok": False, "code": REASON_RESPONSE_OVERSIZED},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    """Read one bounded request, emit one bounded answer, exit."""
    argv = list(sys.argv[1:] if argv is None else argv)
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        _write({"ok": False, "code": REASON_OVERSIZED})
        return 0
    try:
        request = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _write({"ok": False, "code": REASON_BAD_REQUEST})
        return 0
    if not isinstance(request, dict):
        _write({"ok": False, "code": REASON_BAD_REQUEST})
        return 0

    scenario = request.get("scenario")
    scenario = scenario if isinstance(scenario, str) else ""

    # The one live path. It is deliberately outside the offline ``SCENARIOS``
    # registry, so no ``HRCA_AGENT_OFFLINE_SCENARIO`` value can select it: only
    # a parent that was explicitly configured for a live call sends it, and only
    # that parent injects the credential into this child's private environment.
    # The import sits *inside* this branch, so a scripted worker never loads the
    # HTTP/TLS stack at all.
    if scenario == "live":
        from ..integrations import deepseek_live

        task = request.get("request") or {}
        context = tuple(
            str(item) for item in (task.get("context") or []) if isinstance(item, str)
        )
        answer = deepseek_live.live_call(
            str(task.get("task") or ""),
            context,
            max_tokens=int(request.get("max_tokens") or 0),
        )
        _write(answer)
        return 0

    if scenario not in SCENARIOS:
        _write({"ok": False, "code": REASON_UNKNOWN_SCENARIO})
        return 0

    # A scripted hang: the parent's deadline is what terminates this.
    if scenario == "hang":
        while True:
            time.sleep(3600)

    # A scripted *late* answer: it does produce output, but only after the
    # deadline, so the parent must already have stopped reading and reaped it.
    if scenario == "slow_once":
        time.sleep(4.0)

    if scenario == "crash":
        sys.stderr.write("worker: scripted crash\n")
        return 3

    # A portable malformed answer: the child exits 0 but writes bytes that are
    # not the bounded JSON answer, so the parent's real decode branch — not the
    # size branch, and not a host-specific /bin/echo — is exercised on any host.
    if scenario == "truncated_once":
        sys.stdout.write('{"ok": true, "payl')
        sys.stdout.flush()
        return 0

    if scenario == "slow_once":
        sequence = [_choice("clarify", {"question": "A late answer."})]
    elif scenario == "slow_success":
        # A slow but *successful* answer: it spends most of the turn's model
        # allowance, so the terminal commit has to settle out of what is left.
        if int(request.get("call_index") or 0) == 0:
            time.sleep(5.0)
            sequence = [_choice("clarify", {"question": "Answered slowly."})]
        else:
            sequence = [_choice("stop", {"limitation": "done"})]
    elif scenario == "slow_second":
        # A two-call turn whose SECOND call is slow: the point is that the second
        # call's allowance is whatever the shared turn budget has left, not a
        # fresh cap. Call 0 is a fast read; call 1 sleeps past any remaining
        # allowance, so the parent must stop it inside the turn.
        if int(request.get("call_index") or 0) == 0:
            sequence = [_choice("read_record", {"record": "workflow"})]
        else:
            time.sleep(11.0)
            sequence = [_choice("answer", {"answer": "late", "references": []})]
    elif scenario == "slow_settle":
        # A *successful* call that spends nearly the whole model allowance while
        # staying under the per-call cap, so almost nothing is left when the turn
        # reaches its terminal commit. That is the only shape in which the
        # terminal phase, rather than admission, is the contended one.
        if int(request.get("call_index") or 0) == 0:
            time.sleep(7.0)
            sequence = [_choice("clarify", {"question": "Settled late."})]
        else:
            sequence = [_choice("stop", {"limitation": "done"})]
    elif scenario == "huge_once":
        # An answer far past the bounded response size: the worker refuses it
        # itself rather than emitting an unbounded line.
        sequence = [_choice("answer", {"answer": "x" * (MAX_RESPONSE_BYTES + 4096), "references": []})]
    else:
        sequence = _payloads(scenario, request.get("request") or {})
    call_index = int(request.get("call_index") or 0)
    payload = sequence[call_index] if 0 <= call_index < len(sequence) else None
    _write({"ok": True, "payload": payload})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
