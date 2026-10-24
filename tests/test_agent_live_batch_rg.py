"""The m5-l0-rg C/D offline choreography and transcript, offline only.

The run uses the existing scripted worker scenarios over the real supervised
backend, with a temporary synthetic root and a temporary app-data directory.
It never touches a provider, credential, real ledger, real app-data or database.
"""

from __future__ import annotations

import io
import json
import unittest

from hrca.cli import live_batch_cli as runner

MARKER = "TRANSCRIPT JSON:"
RG = "m5-l0-rg"


class RGBatchOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stream = io.StringIO()
        cls.exit_code = runner.run_batch(
            runner.ENGINE_OFFLINE,
            None,
            stream=cls.stream,
            gate_profile=RG,
            transcript=True,
        )
        cls.text = cls.stream.getvalue()
        cls.human_report, cls.json_text = cls.text.split(MARKER, 1)
        cls.transcript = json.loads(cls.json_text)

    def test_the_batch_runs_only_c_and_d_and_exits_cleanly(self):
        self.assertEqual(self.exit_code, runner.EXIT_OK)
        self.assertEqual(
            self.transcript["batch"]["cases"], ["C", "D"]
        )
        self.assertEqual(
            self.transcript["batch"]["allocation"], {"C": 2, "D": 3, "total": 5}
        )
        self.assertEqual(
            self.transcript["batch"]["declared_envelope"]["max_requests"], 5
        )
        self.assertEqual(
            self.transcript["batch"]["declared_envelope"]["max_input_tokens"], 30_000
        )
        self.assertEqual(
            self.transcript["batch"]["declared_envelope"]["max_output_tokens"], 6_000
        )
        self.assertAlmostEqual(
            self.transcript["batch"]["declared_envelope"]["max_spend_usd"], 0.05
        )
        self.assertEqual(
            self.transcript["batch"]["declared_envelope"]["concurrency"], 1
        )
        self.assertFalse(
            self.transcript["batch"]["declared_envelope"]["supplement"]
        )

    def test_the_transcript_is_synthetic_and_offline(self):
        self.assertTrue(self.transcript["synthetic"])
        self.assertEqual(self.transcript["profile"], RG)
        self.assertEqual(self.transcript["gate_id"], "M5-L0-RG-EVIDENCE-1")
        self.assertEqual(self.transcript["batch"]["gate_mode"], "offline")
        self.assertIsNone(self.transcript["batch"]["gate_before"])
        self.assertIsNone(self.transcript["batch"]["gate_after"])
        self.assertFalse(self.transcript["batch"]["declared_envelope"]["supplement"])
        self.assertLessEqual(
            len(self.json_text.encode("utf-8")), runner.TRANSCRIPT_MAX_BYTES
        )

    def test_c_spends_two_calls_and_reads_before_it_answers(self):
        case = self.transcript["cases"][0]
        self.assertEqual(case["key"], "C")
        self.assertEqual(case["verdict"], runner.VERDICT_HUMAN_REVIEW)
        self.assertEqual(len(case["turns"]), 1)
        turn = case["turns"][0]
        self.assertEqual(turn["turn_model_calls"], 2)
        self.assertEqual(
            [step["kind"] for step in turn["steps"]], ["read_record", "answer"]
        )
        self.assertEqual(len(turn["calls"]), 2)
        for call in turn["calls"]:
            self.assertTrue(call["dispatched"])
            self.assertEqual(call["validation"], "accepted")
            self.assertEqual(call["policy"], "permitted")
            self.assertTrue(call["context_items"])
            self.assertIsNotNone(call["accepted"])
        self.assertTrue(turn["verified_references"])
        self.assertEqual(turn["unsupported_references"], [])
        self.assertFalse(turn["answer_verified"])
        self.assertEqual(case["setup"]["scans_recorded"], 1)
        self.assertEqual(case["effects"]["case_scans"], 0)
        self.assertEqual(case["effects"]["setup_scans"], 1)

    def test_d_spends_three_calls_and_retains_the_whole_journey(self):
        case = self.transcript["cases"][1]
        self.assertEqual(case["key"], "D")
        self.assertEqual(case["verdict"], runner.VERDICT_HUMAN_REVIEW)
        self.assertEqual(len(case["turns"]), 2)
        self.assertEqual(
            [turn["label"] for turn in case["turns"]],
            ["batch-D-1", "batch-D-2"],
        )
        self.assertEqual(
            [turn["turn_model_calls"] for turn in case["turns"]], [1, 2]
        )
        journey = case["d_journey"]
        self.assertEqual(journey["draft"]["plan_id"], journey["confirmation"]["plan_id"])
        self.assertEqual(
            journey["draft"]["plan_digest"],
            journey["confirmation"]["expected_digest"],
        )
        self.assertEqual(
            journey["confirmation"]["result"]["digest"],
            journey["confirmation"]["expected_digest"],
        )
        self.assertTrue(journey["confirmation"]["result"]["job_id"])
        self.assertTrue(journey["confirmation"]["result"]["decision_id"])
        self.assertTrue(journey["run"]["result"]["run_id"])
        self.assertEqual(journey["run"]["result"]["outcome"], "succeeded")
        self.assertEqual(journey["run"]["scans_after"], 1)
        self.assertEqual(journey["run"]["case_scans"], 1)
        self.assertTrue(journey["continue"]["same_turn_read"])
        self.assertEqual(
            [step["kind"] for step in journey["continue"]["steps"]],
            ["read_record", "answer"],
        )
        self.assertTrue(journey["continue"]["verified_references"])
        self.assertFalse(journey["continue"]["answer_verified"])
        evidence_ids = {
            row["evidence_id"] for row in journey["record_after_continue"]["evidence"]
        }
        self.assertTrue(
            set(journey["continue"]["verified_references"]) <= evidence_ids
        )
        self.assertEqual(case["effects"]["case_scans"], 1)

    def test_the_whole_batch_spends_exactly_five_calls(self):
        total = sum(
            turn["turn_model_calls"]
            for case in self.transcript["cases"]
            for turn in case["turns"]
        )
        self.assertEqual(total, 5)
        self.assertEqual(
            [case["accounting"]["case_calls"] for case in self.transcript["cases"]],
            [2, 3],
        )

    def test_offline_provider_metadata_is_unavailable_not_invented(self):
        for case in self.transcript["cases"]:
            for turn in case["turns"]:
                for call in turn["calls"]:
                    self.assertIsNone(call["provider"])
            # The scripted worker reports no provider usage. The transcript must
            # say so rather than hardcode a zero.
            self.assertIsNone(case["accounting"]["prompt_tokens_total"])
            self.assertIsNone(case["accounting"]["completion_tokens_total"])

    def test_the_synthetic_source_is_unchanged(self):
        for case in self.transcript["cases"]:
            self.assertTrue(case["effects"]["source_unchanged"])
            self.assertEqual(
                case["effects"]["synthetic_root_sha256_before"],
                case["effects"]["synthetic_root_sha256_after"],
            )

    def test_no_real_ledger_or_database_path_is_retained(self):
        self.assertNotIn("agent.db", self.json_text)
        self.assertNotIn("orchestration.db", self.json_text)
        self.assertNotIn("hrca-m5-l0-evidence-1", self.json_text)
        self.assertNotIn("hrca-m5-l0-rg-evidence-1", self.json_text)
        self.assertNotIn("M5-L0-EVIDENCE-1", self.json_text)
        self.assertNotIn("DEEPSEEK_API_KEY", self.json_text)
        self.assertNotIn("HRCA_AGENT_LIVE_MODEL", self.json_text)
        self.assertNotIn(runner.agent_graph.TRANSCRIPT_CAPTURE_ENV, self.json_text)
        self.assertNotIn("Authorization", self.json_text)

    def test_the_human_report_is_still_printed(self):
        self.assertIn("case C", self.human_report)
        self.assertIn("case D", self.human_report)
        self.assertIn("PENDING HUMAN REVIEW", self.human_report)
        self.assertIn("exit 0 means only", self.human_report)


if __name__ == "__main__":
    unittest.main()
