"""M5-L0-EVIDENCE-1 gate-profile parameterization, offline only.

The legacy AGENT-LOOP-LIVE-1 profile must remain byte-for-byte compatible for
every existing caller. The M5-L0 profile has its own gate id, ledger directory,
nine-request ceiling, 50k/10k/US$0.10 aggregate envelope and no supplement
claim. These tests use temporary directories only; they never read or write the
real ledger.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from hrca.integrations import live_gate


class GateProfileTests(unittest.TestCase):
    def test_the_legacy_profile_is_unchanged(self):
        profile = live_gate.LEGACY_PROFILE
        self.assertEqual(profile.key, live_gate.PROFILE_LEGACY)
        self.assertEqual(profile.gate_id, live_gate.GATE_ID)
        self.assertEqual(profile.max_requests, 9)
        self.assertEqual(profile.max_input_tokens, 20_000)
        self.assertEqual(profile.max_output_tokens, 4_000)
        self.assertAlmostEqual(profile.max_spend_usd, 0.05)
        self.assertEqual(profile.supplement_max_requests, 10)

    def test_the_m5_l0_profile_is_independent_with_the_approved_caps(self):
        profile = live_gate.M5_L0_PROFILE
        self.assertEqual(profile.key, live_gate.PROFILE_M5_L0)
        self.assertEqual(profile.gate_id, "M5-L0-EVIDENCE-1")
        self.assertNotEqual(profile.base_dir, live_gate.LEGACY_PROFILE.base_dir)
        self.assertEqual(profile.max_requests, 9)
        self.assertEqual(profile.max_input_tokens, 50_000)
        self.assertEqual(profile.max_output_tokens, 10_000)
        self.assertAlmostEqual(profile.max_spend_usd, 0.10)
        self.assertIsNone(profile.supplement_max_requests)

    def test_the_profiles_use_distinct_ledger_files(self):
        with tempfile.TemporaryDirectory() as base:
            legacy = live_gate.LiveGate(
                profile=live_gate.LEGACY_PROFILE, base_dir=base
            )
            m5 = live_gate.LiveGate(profile=live_gate.M5_L0_PROFILE, base_dir=base)

            self.assertNotEqual(legacy.path, m5.path)
            self.assertTrue(legacy.path.endswith("AGENT-LOOP-LIVE-1.json"))
            self.assertTrue(m5.path.endswith("M5-L0-EVIDENCE-1.json"))
            self.assertEqual(legacy.remaining()["gate_id"], "AGENT-LOOP-LIVE-1")
            self.assertEqual(m5.remaining()["gate_id"], "M5-L0-EVIDENCE-1")

    def test_the_m5_l0_gate_admits_nine_and_refuses_the_tenth(self):
        with tempfile.TemporaryDirectory() as base:
            gate = live_gate.LiveGate(
                profile=live_gate.M5_L0_PROFILE, base_dir=base
            )
            for index in range(9):
                reservation = gate.reserve(
                    input_upper=live_gate.MAX_INPUT_TOKENS_PER_REQUEST,
                    output_cap=live_gate.MAX_OUTPUT_TOKENS_PER_REQUEST,
                    label="call%d" % index,
                )
                gate.settle(reservation, usage=None, outcome="assumed")

            remaining = gate.remaining()
            self.assertEqual(remaining["requests_reserved"], 9)
            self.assertEqual(remaining["requests_remaining"], 0)
            self.assertEqual(remaining["input_tokens_charged"], 22_500)
            self.assertEqual(remaining["output_tokens_charged"], 4_500)
            self.assertLessEqual(
                remaining["spend_usd_charged"],
                live_gate.M5_L0_PROFILE.max_spend_usd,
            )
            with self.assertRaises(live_gate.GateRefused) as caught:
                gate.reserve(input_upper=100, output_cap=10, label="tenth")
            self.assertEqual(caught.exception.reason, live_gate.REASON_REQUESTS)

            self.assertTrue(
                os.path.exists(os.path.join(base, "M5-L0-EVIDENCE-1.json"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(base, "AGENT-LOOP-LIVE-1.json"))
            )

    def test_the_m5_l0_gate_has_no_supplement_claim(self):
        with tempfile.TemporaryDirectory() as base:
            gate = live_gate.LiveGate(
                profile=live_gate.M5_L0_PROFILE, base_dir=base
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

    def test_the_two_profiles_do_not_share_a_ledger(self):
        with tempfile.TemporaryDirectory() as base:
            m5 = live_gate.LiveGate(profile=live_gate.M5_L0_PROFILE, base_dir=base)
            reservation = m5.reserve(input_upper=100, output_cap=10, label="m5")
            m5.settle(reservation, usage=None, outcome="assumed")

            legacy = live_gate.LiveGate(
                profile=live_gate.LEGACY_PROFILE, base_dir=base
            )
            self.assertEqual(legacy.remaining()["gate_id"], "AGENT-LOOP-LIVE-1")
            self.assertEqual(legacy.remaining()["requests_reserved"], 0)
            self.assertEqual(m5.remaining()["gate_id"], "M5-L0-EVIDENCE-1")
            self.assertEqual(m5.remaining()["requests_reserved"], 1)


if __name__ == "__main__":
    unittest.main()
