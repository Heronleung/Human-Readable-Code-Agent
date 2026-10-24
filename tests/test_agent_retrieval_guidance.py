"""Generic same-turn retrieval guidance and a held-out retrieval fixture.

The guidance lives in the model-facing ``TASK_PROMPT``. It is deliberately
generic: it names no case, goal, plan, evidence id or fixture. The tests below
prove its presence and, separately, that the graph still requires an actual
same-turn ``read_record`` before a citation resolves. No model is called.
"""

from __future__ import annotations

import unittest

from hrca.agent import choice as agent_contract
from hrca.agent import graph as agent_graph
from hrca.agent import policy as policy_mod
from hrca.integrations import provider as provider_mod

HAS_LANGGRAPH = agent_graph.graph_available()

#: A held-out synthetic workflow, structurally distinct from C/D: different
#: goal, plan identity, evidence ids and predicates.
HELD_OUT_GOAL = "which evidence is recorded for the reviewed plan?"
HELD_OUT_WORKFLOW = {
    "has_plan": True,
    "plan_id": "plan:heldout-9",
    "plan_revision": 4,
    "plan_phase": "confirmed",
    "plan_digest": "digest-heldout-9",
    "job_state": "needs_review",
    "review": {
        "run_outcome": "succeeded",
        "freshness": "current",
        "evidence": [
            {
                "evidence_id": "ev:heldout-a1",
                "predicate_id": "lint",
                "result": "pass",
            },
            {
                "evidence_id": "ev:heldout-b2",
                "predicate_id": "types",
                "result": "pass",
            },
        ],
    },
}


def payload(kind, arguments):
    return {
        "schema_version": agent_contract.AGENT_CHOICE_SCHEMA_VERSION,
        "choice": kind,
        "rationale": "held-out scripted",
        "arguments": arguments,
    }


class _ScriptedAdapter(provider_mod.Provider):
    """A deterministic in-process adapter; no process, network or credential."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    def capabilities(self):
        return provider_mod.ProviderCapabilities(
            structured_output=True,
            streaming=False,
            cancellation=False,
            tool_calling=False,
            context_limit=None,
            readiness_available=False,
        )

    def readiness(self):
        return provider_mod.ProviderReadiness(ready=True, detail="scripted")

    def generate(self, request):
        self.calls.append(request)
        if not self._payloads:
            raise provider_mod.ProviderError("provider_failure")
        return provider_mod.ProviderResult(
            task_id=request.task_id,
            content="scripted",
            provider="scripted",
            model="scripted",
            structured_payload=self._payloads.pop(0),
        )


def _run(payloads, *, effect_counter=None):
    adapter = _ScriptedAdapter(payloads)
    effects = effect_counter if effect_counter is not None else {"save": 0, "read": 0}

    def save_draft(goal):
        effects["save"] += 1
        return {"plan_id": "plan:heldout-draft", "plan_phase": "draft"}

    def read_workflow():
        effects["read"] += 1
        return dict(HELD_OUT_WORKFLOW)

    ctx = agent_graph.TurnContext(
        adapter=adapter,
        goal=HELD_OUT_GOAL,
        workflow=HELD_OUT_WORKFLOW,
        save_draft=save_draft,
        read_workflow=read_workflow,
    )
    outcome, steps, delta = agent_graph.run_turn_graph(ctx)
    return adapter, outcome, steps, delta, effects


class GuidanceTextTests(unittest.TestCase):
    def test_the_task_prompt_states_the_generic_same_turn_rule(self):
        prompt = agent_graph.TASK_PROMPT
        for phrase in (
            "read_record={record} with record='workflow'",
            "same-turn observation is",
            "authoritative",
            "answer from it",
            "don't repeat a redundant read",
            "don't cite context-only IDs",
            "if insufficient, clarify/stop",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_the_guidance_is_not_keyed_to_the_c_or_d_fixture(self):
        prompt = agent_graph.TASK_PROMPT
        for fixture_text in (
            "what did the recorded workflow last record?",
            "scan the project and draft a plan",
            "delete every Python file",
            "batch-C",
            "batch-D",
            "permitted scan request",
        ):
            with self.subTest(fixture_text=fixture_text):
                self.assertNotIn(fixture_text, prompt)


@unittest.skipUnless(HAS_LANGGRAPH, "the optional agent graph runtime is not installed")
class HeldOutRetrievalTests(unittest.TestCase):
    def test_read_then_answer_resolves_only_the_same_turn_evidence(self):
        adapter, outcome, steps, delta, effects = _run(
            [
                payload("read_record", {"record": "workflow"}),
                payload(
                    "answer",
                    {
                        "answer": "The reviewed plan records a lint pass.",
                        "references": ["ev:heldout-a1"],
                    },
                ),
            ]
        )

        self.assertEqual(outcome["turn_state"], agent_graph.STATE_ANSWERED)
        self.assertEqual(outcome["verified"], ["ev:heldout-a1"])
        self.assertEqual(outcome["unsupported"], [])
        self.assertEqual(delta["model_calls"], 2)
        self.assertEqual([step["kind"] for step in steps], ["read_record", "answer"])
        # The second model input carried both the actual observation and the
        # evidence id returned by the same-turn read.
        second_context = "\n".join(adapter.calls[1].context)
        self.assertIn("OBSERVED", second_context)
        self.assertIn("ev:heldout-a1", second_context)
        # The first input had the plan evidence as context only; this test's
        # resolution comes from the read, not from that context.
        self.assertIn("ev:heldout-a1", "\n".join(adapter.calls[0].context))
        self.assertEqual(effects, {"save": 0, "read": 0})

    def test_context_present_alone_is_not_same_turn_support(self):
        adapter, outcome, _steps, delta, _effects = _run(
            [
                payload(
                    "answer",
                    {
                        "answer": "The plan records a lint pass.",
                        "references": ["ev:heldout-a1"],
                    },
                )
            ]
        )

        self.assertEqual(outcome["turn_state"], agent_graph.STATE_ANSWERED)
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(outcome["unsupported"], ["ev:heldout-a1"])
        self.assertEqual(delta["model_calls"], 1)
        # The same id was present in the first call's context; context presence
        # alone did not make it a same-turn retrieved reference.
        self.assertIn("ev:heldout-a1", "\n".join(adapter.calls[0].context))

    def test_a_repeated_redundant_read_is_not_converted_to_an_answer(self):
        adapter, outcome, steps, delta, _effects = _run(
            [
                payload("read_record", {"record": "workflow"}),
                payload("read_record", {"record": "workflow"}),
                payload(
                    "answer",
                    {
                        "answer": "This third call should never be dispatched.",
                        "references": ["ev:heldout-a1"],
                    },
                ),
            ]
        )

        self.assertEqual(outcome["turn_state"], agent_graph.STATE_REFUSED)
        self.assertEqual(outcome["refusal"], policy_mod.REFUSAL_STEP_BUDGET)
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(delta["model_calls"], 2)
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(
            [step["kind"] for step in steps], ["read_record", "read_record"]
        )
        # Guidance discourages the repeat; the system refuses rather than
        # inventing an answer or passing the turn.
        self.assertNotIn("third call", outcome["text"])

    def test_clarify_and_stop_remain_no_effect(self):
        for kind, arguments, expected in (
            ("clarify", {"question": "Which plan?"}, agent_graph.STATE_CLARIFY),
            ("stop", {"limitation": "outside the read-only slice"}, agent_graph.STATE_STOPPED),
        ):
            with self.subTest(choice=kind):
                effects = {"save": 0, "read": 0}
                _adapter, outcome, _steps, delta, effects = _run(
                    [payload(kind, arguments)], effect_counter=effects
                )
                self.assertEqual(outcome["turn_state"], expected)
                self.assertEqual(outcome["verified"], [])
                self.assertEqual(delta["model_calls"], 1)
                self.assertEqual(effects, {"save": 0, "read": 0})


if __name__ == "__main__":
    unittest.main()
