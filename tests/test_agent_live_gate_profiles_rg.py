"""m5-l0-rg profile identity/isolation, offline only.

The legacy and consumed m5-l0 profiles must remain exactly as they were. The RG
profile is an independent fifth-request C/D gate with its own ledger and no
supplement. These tests use temporary directories only; they never create or
open the real RG path, the m5-l0 ledger or the legacy ledger.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from hrca.cli import live_batch_cli as runner
from hrca.integrations import live_gate


class RGProfileIdentityTests(unittest.TestCase):
    def test_the_rg_profile_is_exact_and_isolated(self):
        profile = live_gate.M5_L0_RG_PROFILE
        self.assertEqual(profile.key, "m5-l0-rg")
        self.assertEqual(profile.gate_id, "M5-L0-RG-EVIDENCE-1")
        self.assertEqual(profile.max_requests, 5)
        self.assertEqual(profile.max_input_tokens, 30_000)
        self.assertEqual(profile.max_output_tokens, 6_000)
        self.assertAlmostEqual(profile.max_spend_usd, 0.05)
        self.assertIsNone(profile.supplement_max_requests)
        self.assertNotEqual(profile.base_dir, live_gate.LEGACY_PROFILE.base_dir)
        self.assertNotEqual(profile.base_dir, live_gate.M5_L0_PROFILE.base_dir)
        self.assertIs(live_gate.PROFILES["m5-l0-rg"], profile)

    def test_the_rg_gate_admits_five_and_refuses_the_sixth(self):
        with tempfile.TemporaryDirectory() as base:
            gate = live_gate.LiveGate(
                profile=live_gate.M5_L0_RG_PROFILE, base_dir=base
            )
            for index in range(5):
                reservation = gate.reserve(
                    input_upper=100, output_cap=10, label="rg%d" % index
                )
                gate.settle(reservation, usage=None, outcome="assumed")
            with self.assertRaises(live_gate.GateRefused) as caught:
                gate.reserve(input_upper=100, output_cap=10, label="sixth")
            self.assertEqual(caught.exception.reason, live_gate.REASON_REQUESTS)
            self.assertTrue(os.path.exists(gate.path))
            self.assertTrue(gate.path.endswith("M5-L0-RG-EVIDENCE-1.json"))

    def test_the_rg_gate_has_no_supplement(self):
        with tempfile.TemporaryDirectory() as base:
            gate = live_gate.LiveGate(
                profile=live_gate.M5_L0_RG_PROFILE, base_dir=base
            )
            with self.assertRaises(live_gate.GateRefused) as caught:
                gate.claim_supplement(
                    ledger_digest="0" * 64,
                    report_digest="0" * 64,
                    attestation=live_gate.ATTESTATION_OPERATOR,
                )
            self.assertEqual(
                caught.exception.reason, live_gate.REASON_SUPPLEMENT_UNSUPPORTED
            )
            with self.assertRaises(live_gate.GateRefused) as caught:
                gate.reserve(
                    input_upper=100,
                    output_cap=10,
                    label="supplement-identity",
                    run_id="run:" + "0" * 32,
                    case_id="C",
                    request_index=1,
                )
            self.assertEqual(
                caught.exception.reason, live_gate.REASON_SUPPLEMENT_UNSUPPORTED
            )
            self.assertFalse(os.path.exists(gate.path))

    def test_the_rg_gate_never_shares_a_ledger(self):
        with tempfile.TemporaryDirectory() as base:
            rg = live_gate.LiveGate(
                profile=live_gate.M5_L0_RG_PROFILE, base_dir=base
            )
            reservation = rg.reserve(input_upper=100, output_cap=10, label="rg")
            rg.settle(reservation, usage=None, outcome="assumed")
            legacy = live_gate.LiveGate(
                profile=live_gate.LEGACY_PROFILE, base_dir=base
            )
            m5 = live_gate.LiveGate(
                profile=live_gate.M5_L0_PROFILE, base_dir=base
            )
            self.assertEqual(rg.remaining()["requests_reserved"], 1)
            self.assertEqual(legacy.remaining()["requests_reserved"], 0)
            self.assertEqual(m5.remaining()["requests_reserved"], 0)
            self.assertEqual(rg.remaining()["gate_id"], "M5-L0-RG-EVIDENCE-1")


class RGSelectionTests(unittest.TestCase):
    def test_the_rg_case_set_is_exactly_c_then_d(self):
        self.assertEqual(
            [spec.key for spec in runner._resolve_rg_cases(None)], ["C", "D"]
        )

    def test_any_case_selection_is_refused(self):
        for selected in (["C"], ["D"], ["C", "D"], ["A"]):
            with self.subTest(selected=selected):
                with self.assertRaises(runner._Stop):
                    runner._resolve_rg_cases(selected)

    def test_an_unknown_or_partial_profile_key_is_refused(self):
        for key in ("m5-l0-r", "m5-l0-rg ", "M5-L0-RG", "", "unknown"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    runner._gate_profile(key)

    def test_the_cli_refuses_rg_with_case_before_any_run(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = runner.main(
                ["--engine", "offline", "--gate", "m5-l0-rg", "--case", "C"]
            )
        self.assertEqual(status, runner.EXIT_REFUSED)
        self.assertIn("exactly cases C and D", stderr.getvalue())

    def test_the_cli_refuses_transcript_without_rg(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = runner.main(["--engine", "offline", "--transcript"])
        self.assertEqual(status, runner.EXIT_REFUSED)
        self.assertIn("--transcript requires --gate m5-l0-rg", stderr.getvalue())

    def test_the_rg_manifest_lists_only_c_and_d(self):
        output = io.StringIO()
        runner._manifest(output, "m5-l0-rg")
        text = output.getvalue()
        self.assertIn("case C", text)
        self.assertIn("case D", text)
        self.assertNotIn("case A", text)
        self.assertNotIn("case B", text)
        self.assertNotIn("case E", text)
        self.assertNotIn("case F", text)
        self.assertIn("NO SUPPLEMENT", text)
        self.assertIn("m5-l0-rg", text)

    def test_the_legacy_manifest_is_unchanged(self):
        output = io.StringIO()
        runner._manifest(output, "legacy")
        text = output.getvalue()
        for key in "ABCDEF":
            self.assertIn("case %s" % key, text)
        self.assertIn("C-F SUPPLEMENT", text)
        self.assertIn("AGENT-LOOP-LIVE-1", text)
        self.assertIn("m5-l0", text)
        self.assertIn("m5-l0-rg", text)


class PreservationTests(unittest.TestCase):
    def test_the_legacy_profile_is_unchanged(self):
        profile = live_gate.LEGACY_PROFILE
        self.assertEqual(profile.key, "legacy")
        self.assertEqual(profile.gate_id, "AGENT-LOOP-LIVE-1")
        self.assertEqual(profile.base_dir, live_gate.default_base_dir())
        self.assertEqual(profile.max_requests, 9)
        self.assertEqual(profile.max_input_tokens, 20_000)
        self.assertEqual(profile.max_output_tokens, 4_000)
        self.assertAlmostEqual(profile.max_spend_usd, 0.05)
        self.assertEqual(profile.supplement_max_requests, 10)

    def test_the_m5_l0_profile_is_unchanged(self):
        profile = live_gate.M5_L0_PROFILE
        self.assertEqual(profile.key, "m5-l0")
        self.assertEqual(profile.gate_id, "M5-L0-EVIDENCE-1")
        self.assertEqual(profile.base_dir, live_gate.m5_l0_base_dir())
        self.assertEqual(profile.max_requests, 9)
        self.assertEqual(profile.max_input_tokens, 50_000)
        self.assertEqual(profile.max_output_tokens, 10_000)
        self.assertAlmostEqual(profile.max_spend_usd, 0.10)
        self.assertIsNone(profile.supplement_max_requests)

    def test_the_default_and_existing_profile_map_are_unchanged(self):
        self.assertEqual(runner.DEFAULT_GATE_PROFILE, "legacy")
        self.assertIs(live_gate.PROFILES["legacy"], live_gate.LEGACY_PROFILE)
        self.assertIs(live_gate.PROFILES["m5-l0"], live_gate.M5_L0_PROFILE)
        self.assertEqual(
            live_gate.MAX_REQUESTS,
            9,
        )
        self.assertEqual(live_gate.SUPPLEMENT_MAX_REQUESTS, 10)
        self.assertEqual(live_gate.MAX_INPUT_TOKENS, 20_000)
        self.assertEqual(live_gate.MAX_OUTPUT_TOKENS, 4_000)
        self.assertAlmostEqual(live_gate.MAX_SPEND_USD, 0.05)

    def test_the_a_f_manifest_is_unchanged(self):
        self.assertEqual([spec.key for spec in runner.CASES], list("ABCDEF"))
        self.assertEqual([spec.max_calls for spec in runner.CASES], [1, 1, 2, 3, 1, 1])
        self.assertEqual(
            [spec.scenario for spec in runner.CASES],
            [
                "clarify_once",
                "answer_once",
                "read_then_cite",
                "journey_full",
                "forged_tool_once",
                "stop_once",
            ],
        )

    def test_the_retrieval_guidance_is_unchanged(self):
        from hrca.agent import graph as agent_graph

        prompt = agent_graph.TASK_PROMPT
        for phrase in (
            "same-turn observation is",
            "authoritative",
            "answer from it",
            "don't repeat a redundant read",
            "don't cite context-only IDs",
            "if insufficient, clarify/stop",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)


if __name__ == "__main__":
    unittest.main()
