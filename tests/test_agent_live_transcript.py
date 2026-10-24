"""The RG diagnostics transcript contract, offline only.

These tests use scripted/fake choices and synthetic in-memory case results. They
touch no provider, credential, backend, real ledger, app-data or database. The
graph-level negatives prove the C/D grounding oracle is unchanged; the renderer
tests prove the projection cannot copy excluded data or upgrade a non-pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from hrca.agent import choice as agent_choice
from hrca.agent import graph as agent_graph
from hrca.agent import policy as agent_policy
from hrca.cli import live_batch_cli as runner
from hrca.integrations import live_gate
from hrca.integrations import provider as provider_mod

HAS_LANGGRAPH = agent_graph.graph_available()
RG = live_gate.M5_L0_RG_PROFILE


def _payload(kind, arguments):
    return {
        "schema_version": agent_choice.AGENT_CHOICE_SCHEMA_VERSION,
        "choice": kind,
        "rationale": "scripted",
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


WORKFLOW = {
    "has_plan": True,
    "plan_id": "plan:transcript-1",
    "plan_revision": 1,
    "plan_phase": "confirmed",
    "plan_digest": "d" * 64,
    "job_state": "needs_review",
    "revision": {"goal": "synthetic recorded plan"},
    "review": {
        "run_outcome": "succeeded",
        "freshness": "current",
        "evidence": [
            {
                "evidence_id": "ev:current",
                "predicate_id": "scan_evidence_bound",
                "result": "satisfied",
            }
        ],
    },
}


def _run(payloads):
    adapter = _ScriptedAdapter(payloads)
    effects = {"save": 0, "read": 0}

    def save_draft(goal):
        effects["save"] += 1
        return {"plan_id": "plan:transcript-draft", "plan_phase": "draft"}

    def read_workflow():
        effects["read"] += 1
        return dict(WORKFLOW)

    ctx = agent_graph.TurnContext(
        adapter=adapter,
        goal="what did the recorded workflow last record?",
        workflow=WORKFLOW,
        save_draft=save_draft,
        read_workflow=read_workflow,
    )
    outcome, steps, delta = agent_graph.run_turn_graph(ctx)
    return outcome, steps, delta, effects


@unittest.skipUnless(HAS_LANGGRAPH, "the optional agent graph runtime is not installed")
class OracleRetentionTests(unittest.TestCase):
    """Retention never changes a non-passing C/D oracle."""

    def test_context_only_reference_is_unsupported(self):
        outcome, _steps, delta, _effects = _run(
            [
                _payload(
                    "answer",
                    {"answer": "claims from context", "references": ["ev:current"]},
                )
            ]
        )
        self.assertEqual(outcome["turn_state"], agent_graph.STATE_ANSWERED)
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(outcome["unsupported"], ["ev:current"])
        self.assertEqual(delta["model_calls"], 1)

    def test_foreign_reference_is_unsupported_after_a_read(self):
        outcome, _steps, _delta, _effects = _run(
            [
                _payload("read_record", {"record": "workflow"}),
                _payload(
                    "answer",
                    {"answer": "foreign", "references": ["ev:foreign"]},
                ),
            ]
        )
        self.assertEqual(outcome["turn_state"], agent_graph.STATE_ANSWERED)
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(outcome["unsupported"], ["ev:foreign"])

    def test_stale_reference_is_unsupported_after_a_read(self):
        outcome, _steps, _delta, _effects = _run(
            [
                _payload("read_record", {"record": "workflow"}),
                _payload(
                    "answer",
                    {"answer": "stale", "references": ["ev:stale"]},
                ),
            ]
        )
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(outcome["unsupported"], ["ev:stale"])

    def test_a_redundant_read_is_not_converted_to_an_answer(self):
        outcome, steps, delta, _effects = _run(
            [
                _payload("read_record", {"record": "workflow"}),
                _payload("read_record", {"record": "workflow"}),
                _payload("answer", {"answer": "third", "references": []}),
            ]
        )
        self.assertEqual(outcome["turn_state"], agent_graph.STATE_REFUSED)
        self.assertEqual(outcome["refusal"], agent_policy.REFUSAL_STEP_BUDGET)
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(delta["model_calls"], 2)
        self.assertEqual([step["kind"] for step in steps], ["read_record", "read_record"])

    def test_a_refused_choice_is_non_passing(self):
        outcome, _steps, _delta, _effects = _run(
            [_payload("run_shell", {"command": "rm -rf /"})]
        )
        self.assertEqual(outcome["turn_state"], agent_graph.STATE_REFUSED)
        self.assertEqual(outcome["verified"], [])

    def test_a_cap_stop_is_non_passing(self):
        outcome, _steps, delta, _effects = _run(
            [
                _payload("read_record", {"record": "workflow"}),
                _payload("read_record", {"record": "workflow"}),
                _payload("read_record", {"record": "workflow"}),
            ]
        )
        self.assertEqual(outcome["turn_state"], agent_graph.STATE_REFUSED)
        self.assertEqual(outcome["refusal"], agent_policy.REFUSAL_STEP_BUDGET)
        self.assertEqual(delta["model_calls"], 2)
        self.assertEqual(outcome["verified"], [])

    def test_a_no_effect_clarify_has_no_effect(self):
        outcome, _steps, _delta, effects = _run(
            [_payload("clarify", {"question": "Which package?"})]
        )
        self.assertEqual(outcome["turn_state"], agent_graph.STATE_CLARIFY)
        self.assertEqual(outcome["verified"], [])
        self.assertEqual(effects, {"save": 0, "read": 0})

    def test_the_diagnostics_capture_is_off_by_default(self):
        outcome, _steps, _delta, _effects = _run(
            [_payload("clarify", {"question": "Which package?"})]
        )
        self.assertEqual(outcome.get("call_records"), [])


class CaptureSwitchTests(unittest.TestCase):
    def test_the_switch_requires_the_rg_value(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(agent_graph.TRANSCRIPT_CAPTURE_ENV, None)
            self.assertFalse(agent_graph.transcript_capture_enabled())
        with mock.patch.dict(
            os.environ,
            {agent_graph.TRANSCRIPT_CAPTURE_ENV: agent_graph.TRANSCRIPT_CAPTURE_VALUE},
        ):
            self.assertTrue(agent_graph.transcript_capture_enabled())
        with mock.patch.dict(
            os.environ, {agent_graph.TRANSCRIPT_CAPTURE_ENV: "legacy"}
        ):
            self.assertFalse(agent_graph.transcript_capture_enabled())

    def test_the_runner_sets_the_switch_only_for_rg(self):
        spec = runner._CASE_INDEX["C"]
        with tempfile.TemporaryDirectory() as base:
            for profile, expected in (
                ("legacy", None),
                ("m5-l0", None),
                ("m5-l0-rg", agent_graph.TRANSCRIPT_CAPTURE_VALUE),
            ):
                with self.subTest(profile=profile):
                    case = runner._Case(
                        spec, runner.ENGINE_OFFLINE, base, gate_profile=profile
                    )
                    try:
                        env = case.backend._env
                        if expected is None:
                            self.assertNotIn(
                                agent_graph.TRANSCRIPT_CAPTURE_ENV, env
                            )
                        else:
                            self.assertEqual(
                                env[agent_graph.TRANSCRIPT_CAPTURE_ENV], expected
                            )
                    finally:
                        shutil.rmtree(case.root, ignore_errors=True)

    def test_an_ambient_switch_is_scrubbed_for_a_legacy_case(self):
        spec = runner._CASE_INDEX["C"]
        with tempfile.TemporaryDirectory() as base:
            with mock.patch.dict(
                os.environ,
                {agent_graph.TRANSCRIPT_CAPTURE_ENV: agent_graph.TRANSCRIPT_CAPTURE_VALUE},
            ):
                case = runner._Case(
                    spec, runner.ENGINE_OFFLINE, base, gate_profile="legacy"
                )
                try:
                    self.assertNotIn(
                        agent_graph.TRANSCRIPT_CAPTURE_ENV, case.backend._env
                    )
                finally:
                    shutil.rmtree(case.root, ignore_errors=True)


def _call(index, choice, arguments, *, observation="", evidence_ids=(), provider=None):
    return {
        "call": index,
        "task_id": "agent-turn-%d" % index,
        "context_items": ["GOAL: synthetic", "PLAN: phase=confirmed revision=1"],
        "context_bytes": 48,
        "context_truncated": False,
        "dispatched": True,
        "validation": "accepted",
        "policy": "permitted",
        "reason": "",
        "accepted": {
            "schema_version": agent_choice.AGENT_CHOICE_SCHEMA_VERSION,
            "choice": choice,
            "rationale": "scripted",
            "arguments": arguments,
        },
        "observation": observation,
        "evidence_ids": list(evidence_ids),
        "provider": provider,
    }


def _turn(label, goal, calls, steps, verified=(), unsupported=()):
    return {
        "label": label,
        "turn_id": "turn:%s" % label,
        "goal": goal,
        "state": "answered",
        "choice": "answer",
        "refusal": None,
        "text": "bounded synthetic answer",
        "elapsed_seconds": 0.1,
        "session_model_calls": len(calls),
        "turn_model_calls": len(calls),
        "calls": list(calls),
        "steps": list(steps),
        "verified_references": list(verified),
        "unsupported_references": list(unsupported),
        "answer_verified": False,
        "response_interval": {"seconds": 0.1, "delivered": True},
    }


def _c_result():
    result = runner.CaseResult(
        key="C",
        title="fact absent from the initial context",
        expectation="read then cite",
        verdict="human-review",
        detail="synthetic",
        calls=2,
        scans=0,
        setup_scans=1,
        elapsed=1.0,
    )
    result.transcript = {
        "synthetic": True,
        "setup": {
            "performed": True,
            "plan_id": "plan:transcript-1",
            "plan_revision": 1,
            "plan_digest": "d" * 64,
            "scans_recorded": 1,
        },
        "turns": [
            _turn(
                "batch-C",
                "what did the recorded workflow last record?",
                [
                    _call(1, "read_record", {"record": "workflow"}),
                    _call(
                        2,
                        "answer",
                        {"answer": "synthetic", "references": ["ev:current"]},
                        observation="plan phase=confirmed revision=1",
                        evidence_ids=["ev:current"],
                        provider={
                            "requested_model": "deepseek-flash",
                            "returned_model": "deepseek-v4.1-flash-1017",
                            "fingerprint": "fp:1",
                            "outcome": "ok",
                            "usage_reported": True,
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "reasoning_tokens": 0,
                        },
                    ),
                ],
                [
                    {"ordinal": 0, "kind": "read_record", "record": "workflow"},
                    {"ordinal": 1, "kind": "answer", "verified": ["ev:current"], "unsupported": []},
                ],
                verified=["ev:current"],
            )
        ],
        "effects": {
            "synthetic_root_sha256_before": "a" * 64,
            "synthetic_root_sha256_after": "a" * 64,
            "source_unchanged": True,
        },
    }
    result.provider_calls = [
        {
            "requested_model": "deepseek-flash",
            "returned_model": "deepseek-v4.1-flash-1017",
            "fingerprint": "fp:1",
            "outcome": "ok",
            "usage_reported": True,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "reasoning_tokens": 0,
        }
    ]
    return result


def _d_result():
    result = runner.CaseResult(
        key="D",
        title="permitted scan request",
        expectation="draft, confirm, run, continue",
        verdict="human-review",
        detail="synthetic",
        calls=3,
        scans=1,
        setup_scans=0,
        elapsed=2.0,
    )
    result.transcript = {
        "synthetic": True,
        "turns": [
            _turn(
                "batch-D-1",
                "scan the project and draft a plan",
                [_call(1, "propose_scan", {"goal": "inspect the project"})],
                [{"ordinal": 0, "kind": "propose_scan", "plan_id": "plan:transcript-1"}],
            ),
            _turn(
                "batch-D-2",
                "what did the scan record?",
                [
                    _call(1, "read_record", {"record": "workflow"}),
                    _call(
                        2,
                        "answer",
                        {"answer": "synthetic", "references": ["ev:current"]},
                        observation="plan phase=confirmed revision=1",
                        evidence_ids=["ev:current"],
                    ),
                ],
                [
                    {"ordinal": 0, "kind": "read_record", "record": "workflow"},
                    {"ordinal": 1, "kind": "answer", "verified": ["ev:current"], "unsupported": []},
                ],
                verified=["ev:current"],
            ),
        ],
        "d_journey": {
            "draft": {
                "plan_id": "plan:transcript-1",
                "plan_revision": 1,
                "plan_digest": "d" * 64,
                "goal": "inspect the project",
                "state": "proposed",
                "turn_label": "batch-D-1",
                "turn_id": "turn:batch-D-1",
            },
            "confirmation": {
                "actor": "runner-explicit-synthetic-human",
                "plan_id": "plan:transcript-1",
                "expected_digest": "d" * 64,
                "plan_revision": 1,
                "idempotency_key": "batch-D-confirm-1",
                "result": {
                    "available": True,
                    "revision": 1,
                    "digest": "d" * 64,
                    "job_id": "job:1",
                    "decision_id": "decision:1",
                },
            },
            "run": {
                "plan_id": "plan:transcript-1",
                "idempotency_key": "batch-D-run-1",
                "result": {
                    "available": True,
                    "run_id": "run:1",
                    "outcome": "succeeded",
                    "job_id": "job:1",
                    "attempt": 1,
                    "replayed": None,
                },
                "scans_after": 1,
                "case_scans": 1,
            },
            "continue": {
                "goal": "what did the scan record?",
                "turn_label": "batch-D-2",
                "turn_id": "turn:batch-D-2",
                "same_turn_read": True,
                "steps": [
                    {"ordinal": 0, "kind": "read_record", "record": "workflow"},
                    {"ordinal": 1, "kind": "answer", "verified": ["ev:current"], "unsupported": []},
                ],
                "verified_references": ["ev:current"],
                "unsupported_references": [],
                "observation_reaching_inference": "plan phase=confirmed revision=1",
                "context_evidence_ids": ["ev:current"],
                "answer_verified": False,
            },
            "record_before_continue": {
                "available": True,
                "run_id": "run:1",
                "run_outcome": "succeeded",
                "job_state": "needs_review",
                "evidence_count": 1,
                "evidence": [{"evidence_id": "ev:current", "predicate": "scan_evidence_bound", "result": "satisfied", "counts": {"files": 2}}],
            },
            "record_after_continue": {
                "available": True,
                "run_id": "run:1",
                "run_outcome": "succeeded",
                "job_state": "needs_review",
                "evidence_count": 1,
                "evidence": [{"evidence_id": "ev:current", "predicate": "scan_evidence_bound", "result": "satisfied", "counts": {"files": 2}}],
            },
        },
        "effects": {
            "synthetic_root_sha256_before": "b" * 64,
            "synthetic_root_sha256_after": "b" * 64,
            "source_unchanged": True,
        },
    }
    return result


def _render(result, specs):
    return runner._transcript(
        [result],
        profile=RG,
        gate_before=None,
        gate_after=None,
        exit_code=0,
        specs=specs,
    )


class TranscriptRendererTests(unittest.TestCase):
    def test_c_transcript_retains_inputs_payloads_and_steps(self):
        document = _render(_c_result(), [runner._CASE_INDEX["C"]])
        case = document["cases"][0]
        self.assertEqual(case["key"], "C")
        self.assertEqual(case["verdict"], "human-review")
        self.assertEqual(case["max_calls"], 2)
        self.assertEqual(case["setup"]["scans_recorded"], 1)
        turn = case["turns"][0]
        self.assertEqual(turn["turn_model_calls"], 2)
        self.assertEqual([step["kind"] for step in turn["steps"]], ["read_record", "answer"])
        self.assertEqual(len(turn["calls"]), 2)
        self.assertEqual(turn["calls"][0]["context_items"][0], "GOAL: synthetic")
        self.assertEqual(turn["calls"][0]["accepted"]["choice"], "read_record")
        self.assertEqual(turn["calls"][1]["accepted"]["choice"], "answer")
        self.assertEqual(turn["calls"][1]["observation"], "plan phase=confirmed revision=1")
        self.assertEqual(turn["calls"][1]["evidence_ids"], ["ev:current"])
        self.assertEqual(turn["calls"][1]["provider"]["returned_model"], "deepseek-v4.1-flash-1017")
        self.assertFalse(turn["answer_verified"])
        self.assertEqual(turn["verified_references"], ["ev:current"])

    def test_d_transcript_retains_the_complete_journey(self):
        document = _render(_d_result(), [runner._CASE_INDEX["D"]])
        case = document["cases"][0]
        self.assertEqual(case["key"], "D")
        self.assertEqual(case["max_calls"], 3)
        self.assertEqual(case["effects"]["case_scans"], 1)
        journey = case["d_journey"]
        self.assertEqual(journey["draft"]["plan_id"], journey["confirmation"]["plan_id"])
        self.assertEqual(
            journey["draft"]["plan_digest"], journey["confirmation"]["expected_digest"]
        )
        self.assertEqual(journey["confirmation"]["result"]["decision_id"], "decision:1")
        self.assertEqual(journey["run"]["result"]["run_id"], "run:1")
        self.assertEqual(journey["run"]["scans_after"], 1)
        self.assertTrue(journey["continue"]["same_turn_read"])
        self.assertEqual(
            [step["kind"] for step in journey["continue"]["steps"]],
            ["read_record", "answer"],
        )
        self.assertEqual(
            journey["record_after_continue"]["evidence"][0]["evidence_id"],
            "ev:current",
        )
        self.assertEqual(len(case["turns"]), 2)

    def test_unknown_keys_canary_headers_and_paths_are_not_copied(self):
        result = _c_result()
        canary = "CANARY_SECRET_VALUE_123"
        result.observations = {"canary": canary, "path": "/etc/passwd"}
        result.transcript["canary"] = canary
        result.transcript["path"] = "/etc/passwd"
        result.transcript["turns"][0]["Authorization"] = "Bearer " + canary
        result.transcript["turns"][0]["headers"] = {"authorization": canary}
        result.transcript["turns"][0]["calls"][0]["raw_content"] = canary
        document = _render(result, [runner._CASE_INDEX["C"]])
        text = json.dumps(document, sort_keys=True)
        self.assertNotIn(canary, text)
        self.assertNotIn("/etc/passwd", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("raw_content", text)

    def test_wrong_profile_is_refused(self):
        with self.assertRaises(runner._TranscriptError):
            runner._transcript(
                [_c_result()],
                profile=live_gate.LEGACY_PROFILE,
                gate_before=None,
                gate_after=None,
                exit_code=0,
                specs=[runner._CASE_INDEX["C"]],
            )

    def test_non_synthetic_result_is_refused(self):
        result = _c_result()
        result.transcript["synthetic"] = False
        with self.assertRaises(runner._TranscriptError):
            _render(result, [runner._CASE_INDEX["C"]])

    def test_over_bound_text_is_refused(self):
        result = _c_result()
        result.transcript["turns"][0]["text"] = "x" * 1201
        with self.assertRaises(runner._TranscriptError):
            _render(result, [runner._CASE_INDEX["C"]])

    def test_answer_verified_true_is_refused(self):
        result = _c_result()
        result.transcript["turns"][0]["answer_verified"] = True
        with self.assertRaises(runner._TranscriptError):
            _render(result, [runner._CASE_INDEX["C"]])

    def test_accounting_uses_reported_usage_and_marks_unknowns(self):
        result = _c_result()
        result.provider_calls = [
            {
                "requested_model": "deepseek-flash",
                "returned_model": "deepseek-v4.1-flash-1017",
                "fingerprint": "fp:1",
                "outcome": "ok",
                "usage_reported": True,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "reasoning_tokens": 0,
            },
            {
                "requested_model": "deepseek-flash",
                "returned_model": "",
                "fingerprint": "",
                "outcome": "http",
                "usage_reported": False,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
            },
        ]
        document = _render(result, [runner._CASE_INDEX["C"]])
        accounting = document["cases"][0]["accounting"]
        self.assertEqual(accounting["provider_records"], 2)
        self.assertEqual(accounting["usage_known_calls"], 1)
        self.assertEqual(accounting["usage_unknown_calls"], 1)
        self.assertEqual(accounting["prompt_tokens_total"], 10)
        self.assertEqual(accounting["completion_tokens_total"], 5)

    def test_an_unrun_case_has_no_fabricated_zeroes(self):
        result = runner.CaseResult(
            key="D",
            title="permitted scan request",
            expectation="never ran",
            verdict="pending",
            detail="not run: an earlier case stopped the batch",
        )
        result.transcript = {"synthetic": True}
        document = _render(result, [runner._CASE_INDEX["D"]])
        case = document["cases"][0]
        self.assertFalse(case["setup"]["performed"])
        self.assertEqual(case["turns"], [])
        self.assertIsNone(case["effects"]["case_scans"])
        self.assertIsNone(case["accounting"]["case_calls"])
        self.assertIsNone(case["accounting"]["prompt_tokens_total"])
        self.assertEqual(document["batch"]["gate_mode"], "offline")
        self.assertIsNone(document["batch"]["gate_before"])
        self.assertIsNone(document["batch"]["gate_after"])

    def test_a_non_passing_verdict_and_its_problem_are_preserved(self):
        result = runner.CaseResult(
            key="C",
            title="fact absent from the initial context",
            expectation="read then cite",
            verdict="not-established",
            detail="the answer cited nothing this turn retrieved",
            calls=2,
        )
        result.problems = ["spent 3 model calls against an allocation of 2"]
        result.transcript = {
            "synthetic": True,
            "turns": [
                _turn(
                    "batch-C",
                    "what did the recorded workflow last record?",
                    [_call(1, "read_record", {"record": "workflow"})],
                    [{"ordinal": 0, "kind": "read_record", "record": "workflow"}],
                    unsupported=["ev:foreign"],
                )
            ],
            "effects": {"source_unchanged": True},
        }
        document = _render(result, [runner._CASE_INDEX["C"]])
        case = document["cases"][0]
        self.assertEqual(case["verdict"], "not-established")
        self.assertIn("spent 3 model calls", case["problems"][0])
        self.assertEqual(case["turns"][0]["unsupported_references"], ["ev:foreign"])
        self.assertFalse(case["turns"][0]["answer_verified"])

    def test_the_model_input_identity_is_hashed(self):
        document = _render(_c_result(), [runner._CASE_INDEX["C"]])
        model = document["model"]
        self.assertEqual(
            model["task_prompt_sha256"],
            hashlib.sha256(agent_graph.TASK_PROMPT.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(model["requested_id"], "deepseek-flash")
        self.assertEqual(model["thinking"], "disabled")
        self.assertEqual(model["response_format"], "json_object")
        self.assertEqual(model["max_input_tokens_per_request"], 2_500)
        self.assertEqual(model["max_output_tokens_per_request"], 500)


if __name__ == "__main__":
    unittest.main()
