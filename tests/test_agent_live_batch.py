"""AGENT-LOOP-LIVE-1R: the controlled batch runner, proved offline.

Two kinds of test live here.

* **Pure** tests pin the frozen manifest, the configuration refusals, the
  environment scrubbing and the allocation guard. They start nothing.
* **Choreography** tests run the runner's own ``--engine offline`` binding,
  which spawns the *real* supervised backend against the code-owned scripted
  worker scenarios. That exercises the supervised path end to end with no
  network, no credential and no gate: the live variables are stripped from the
  child environment, so an ambient credential cannot turn an offline case into
  a remote call.

The live engine is never run here. Its refusals are asserted; its dispatches are
not, because a dispatch is a real, billable request and this module must be
incapable of making one.

This module **spawns child processes**, so it is on no guarded safe route: it is
run by name, like the other supervised-process suites.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hrca.agent import executor as executor_mod
from hrca.cli import live_batch_cli as runner
from hrca.core import contract

#: A synthetic secret. Proves the scrubbing rules without ever being real, and
#: never reaches a child that could use it.
CANARY_KEY = "sk-canary-not-a-real-key-0123456789"

_OFFLINE_ENGINE_TIMEOUT_SECONDS = 600


class ManifestTests(unittest.TestCase):
    """The frozen cases are code-owned, bounded and answer-free."""

    def test_the_manifest_lists_every_case_without_executing_anything(self):
        stream = io.StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            status = runner.main(["--list"])
        self.assertEqual(status, runner.EXIT_OK)
        text = stream.getvalue()
        for spec in runner.CASES:
            with self.subTest(case=spec.key):
                self.assertIn("case %s" % spec.key, text)
        self.assertIn(runner.GATE_ID, text)

    def test_the_goals_never_name_the_choice_they_are_measured_on(self):
        # A goal that contains its own expected choice measures the fixture, not
        # the agent. Case D may legitimately *ask* for a scan; what it must
        # never do is name ``propose_scan``.
        words = ("clarify", "answer", "read_record", "propose_scan", "stop")
        for spec in runner.CASES:
            with self.subTest(case=spec.key):
                lowered = spec.goal.lower()
                for word in words:
                    self.assertNotIn(word, lowered)

    def test_the_allocation_is_exactly_the_approved_ceiling(self):
        self.assertEqual(len(runner.CASES), 6)
        self.assertEqual(sum(spec.max_calls for spec in runner.CASES), 9)
        self.assertEqual(
            {spec.key: spec.max_calls for spec in runner.CASES},
            {"A": 1, "B": 1, "C": 2, "D": 3, "E": 1, "F": 1},
        )

    def test_an_unknown_case_is_refused(self):
        stream = io.StringIO()
        with redirect_stderr(stream):
            status = runner.main(["--engine", "offline", "--case", "Z"])
        self.assertEqual(status, runner.EXIT_REFUSED)

    def test_an_unknown_engine_is_refused(self):
        stream = io.StringIO()
        with redirect_stderr(stream):
            status = runner.main(["--engine", "whatever"])
        self.assertEqual(status, runner.EXIT_REFUSED)

    def test_the_m5_l0_profile_is_listed_and_selectable(self):
        stream = io.StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            status = runner.main(["--list"])
        self.assertEqual(status, runner.EXIT_OK)
        text = stream.getvalue()
        self.assertIn("M5-L0-EVIDENCE-1", text)
        self.assertIn("m5-l0", text)
        self.assertIn("input=50000", text)
        self.assertIn("output=10000", text)
        self.assertIn("spend=0.1000", text)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                runner.main(["--gate", "m5-l0", "--list"]),
                runner.EXIT_OK,
            )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                runner.main(["--gate", "unknown", "--list"]),
                runner.EXIT_REFUSED,
            )

    def test_the_m5_l0_profile_cannot_use_the_supplement(self):
        stream = io.StringIO()
        with redirect_stderr(stream):
            status = runner.main(["--gate", "m5-l0", "--supplement-cf"])
        self.assertEqual(status, runner.EXIT_REFUSED)
        self.assertIn("legacy-gate-only", stream.getvalue())


class ConfigurationTests(unittest.TestCase):
    """Configuration is two approved names, and nothing a caller can widen."""

    def test_the_offline_child_environment_is_scrubbed_of_the_live_variables(self):
        with mock.patch.dict(
            os.environ,
            {
                runner.CREDENTIAL_ENV: CANARY_KEY,
                runner.LIVE_MODEL_ENV: "deepseek-flash",
                "XDG_DATA_HOME": "/tmp/ignored",
            },
            clear=False,
        ):
            env = runner._offline_env(runner.CASES[0], "/tmp/appdata")
        self.assertNotIn(runner.CREDENTIAL_ENV, env)
        self.assertNotIn(runner.LIVE_MODEL_ENV, env)
        self.assertNotIn(CANARY_KEY, json.dumps(env))
        self.assertEqual(env[runner.OFFLINE_SCENARIO_ENV], runner.CASES[0].scenario)
        self.assertEqual(env["XDG_DATA_HOME"], "/tmp/appdata")
        self.assertEqual(env["LOCALAPPDATA"], "/tmp/appdata")

    def test_the_live_child_environment_names_the_model_and_drops_the_offline_hook(self):
        with mock.patch.dict(
            os.environ,
            {
                runner.OFFLINE_SCENARIO_ENV: "journey_full",
                runner.CREDENTIAL_ENV: CANARY_KEY,
            },
            clear=False,
        ):
            env = runner._live_env("/tmp/appdata")
        self.assertEqual(env[runner.LIVE_MODEL_ENV], runner.LIVE_MODEL)
        self.assertNotIn(runner.OFFLINE_SCENARIO_ENV, env)
        # The credential is *inherited* into this one child, never re-encoded.
        self.assertEqual(env[runner.CREDENTIAL_ENV], CANARY_KEY)
        # The legacy profile stays the default; the M5-L0 name is explicit.
        self.assertEqual(env[runner.GATE_PROFILE_ENV], runner.DEFAULT_GATE_PROFILE)
        with mock.patch.dict(
            os.environ, {runner.CREDENTIAL_ENV: CANARY_KEY}, clear=False
        ):
            m5 = runner._live_env("/tmp/appdata", gate_profile="m5-l0")
        self.assertEqual(m5[runner.GATE_PROFILE_ENV], "m5-l0")
        self.assertEqual(m5[runner.CREDENTIAL_ENV], CANARY_KEY)
        self.assertEqual(m5[runner.LIVE_MODEL_ENV], runner.LIVE_MODEL)

    def test_live_refuses_when_the_credential_is_absent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(runner.CREDENTIAL_ENV, None)
            os.environ[runner.LIVE_MODEL_ENV] = runner.LIVE_MODEL
            os.environ.pop(runner.OFFLINE_SCENARIO_ENV, None)
            self.assertEqual(runner._live_preflight(), runner.REASON_CREDENTIAL)

    def test_live_refuses_when_the_model_is_not_the_approved_one(self):
        with mock.patch.dict(
            os.environ,
            {runner.LIVE_MODEL_ENV: "some-other-model", runner.CREDENTIAL_ENV: CANARY_KEY},
            clear=False,
        ):
            os.environ.pop(runner.OFFLINE_SCENARIO_ENV, None)
            self.assertEqual(runner._live_preflight(), runner.REASON_MODEL)

    def test_live_refuses_when_the_offline_hook_is_set(self):
        # Scripted intelligence must never be mistaken for a live call.
        with mock.patch.dict(
            os.environ,
            {
                runner.OFFLINE_SCENARIO_ENV: "journey_full",
                runner.LIVE_MODEL_ENV: runner.LIVE_MODEL,
                runner.CREDENTIAL_ENV: CANARY_KEY,
            },
            clear=False,
        ):
            self.assertEqual(runner._live_preflight(), runner.REASON_OFFLINE_SET)

    def test_live_admits_only_both_approved_names(self):
        with mock.patch.dict(
            os.environ,
            {runner.LIVE_MODEL_ENV: runner.LIVE_MODEL, runner.CREDENTIAL_ENV: CANARY_KEY},
            clear=False,
        ):
            os.environ.pop(runner.OFFLINE_SCENARIO_ENV, None)
            self.assertIsNone(runner._live_preflight())

    def test_live_refuses_outright_without_a_usable_configuration(self):
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(runner.CREDENTIAL_ENV, None)
            with redirect_stdout(stream):
                status = runner.run_batch(runner.ENGINE_LIVE)
        self.assertEqual(status, runner.EXIT_REFUSED)
        self.assertIn("refused", stream.getvalue())

    def test_a_partially_consumed_ledger_is_refused_rather_than_interpreted(self):
        # The smallest safe rule: a spent allowance needs a decision, not a
        # recovery framework. The real ledger is never touched — the reading is
        # supplied.
        consumed = {
            "gate_id": runner.GATE_ID,
            "requests_reserved": 2,
            "requests_remaining": 6,
            "input_tokens_charged": 120,
            "input_tokens_remaining": 19_880,
            "output_tokens_charged": 40,
            "output_tokens_remaining": 3_960,
            "spend_usd_charged": 0.0001,
            "spend_usd_remaining": 0.0499,
        }
        fresh = dict(consumed, requests_reserved=0, spend_usd_charged=0.0)
        self.assertFalse(runner._ledger_continuable(consumed))
        self.assertTrue(runner._ledger_continuable(fresh))

    def test_the_runner_drives_the_boundary_and_never_the_adapter(self):
        # A structural guard: the runner must go through the supervised
        # boundary, not call the live adapter directly as a shortcut. Checked on
        # the parsed imports and calls, so prose about the adapter is allowed
        # while an actual dependency on it is not.
        import ast

        path = os.path.join("src", "hrca", "cli", "live_batch_cli.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for forbidden in ("deepseek_live", "live_gate", "http.client", "ssl"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(
                    [name for name in imported if forbidden in name], sorted(imported)
                )
        # Nothing here constructs a model call of its own.
        self.assertNotIn("live_call(", source)


class OfflineChoreographyTests(unittest.TestCase):
    """The runner's own offline binding, over the real supervised path."""

    @classmethod
    def setUpClass(cls):
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(runner.CREDENTIAL_ENV, None)
            os.environ.pop(runner.LIVE_MODEL_ENV, None)
            with redirect_stdout(stream):
                cls.status = runner.run_batch(runner.ENGINE_OFFLINE)
        cls.text = stream.getvalue()

    def test_the_whole_batch_stays_inside_the_frozen_allocation(self):
        # Nine model calls is the whole approved ceiling: the batch is exactly
        # sized for it, and no case may quietly borrow another's share.
        total = 0
        for key, allocation in (("A", 1), ("B", 1), ("C", 2), ("D", 3), ("E", 1), ("F", 1)):
            spent = _calls_for(self.text, key)
            with self.subTest(case=key):
                self.assertLessEqual(spent, allocation)
            total += spent
        self.assertLessEqual(total, 9)

    def test_no_case_failed_or_was_left_pending(self):
        self.assertNotIn("verdict=not-established", self.text)
        self.assertNotIn("verdict=refused-or-unsafe", self.text)
        self.assertNotIn("verdict=pending", self.text)
        self.assertEqual(self.status, runner.EXIT_OK)

    def test_every_case_reports_the_verdict_its_oracle_supports(self):
        expected = {
            "A": runner.VERDICT_ESTABLISHED,
            "B": runner.VERDICT_HUMAN_REVIEW,
            "C": runner.VERDICT_HUMAN_REVIEW,
            "D": runner.VERDICT_HUMAN_REVIEW,
            "E": runner.VERDICT_SAFETY_ONLY,
            # F's scripted stop carries a limitation, but whether it is honest
            # and complete is a human question: the maximum automatic verdict
            # is review, never a pass.
            "F": runner.VERDICT_HUMAN_REVIEW,
        }
        for key, verdict in expected.items():
            with self.subTest(case=key):
                self.assertIn("case %s " % key, self.text)
                self.assertIn("verdict=%s" % verdict, _line_for(self.text, key))

    def test_only_case_d_executes_a_scan_and_only_once(self):
        for key in ("A", "B", "C", "E", "F"):
            with self.subTest(case=key):
                self.assertIn("scans=0 ", _line_for(self.text, key))
        self.assertIn("scans=1 ", _line_for(self.text, "D"))
        self.assertIn("setup_scans=0", _line_for(self.text, "D"))

    def test_the_recorded_fact_case_reads_before_it_answers(self):
        line = _line_for(self.text, "C")
        self.assertIn("calls=2", line)
        # The setup scan is reported separately, so a case's own effect count
        # is never inflated by the record it was given.
        self.assertIn("setup_scans=1", line)

    def test_a_resolved_citation_is_never_reported_as_verification(self):
        self.assertIn('"answer_verified": false', self.text)

    def test_case_e_produced_no_forged_effect(self):
        line = _line_for(self.text, "E")
        self.assertIn("safety-only", line)
        self.assertNotIn("THE ANSWER", line)

    def test_the_synthetic_source_is_untouched_by_every_case(self):
        self.assertNotIn("problem  : the synthetic source changed", self.text)

    def test_the_live_engine_was_never_reached_by_this_module(self):
        self.assertNotIn("gate %s after" % runner.GATE_ID, self.text)

    def test_every_case_reports_a_delivered_response_interval(self):
        intervals = [
            line for line in self.text.splitlines() if line.strip().startswith("response :")
        ]
        self.assertEqual(len(intervals), 8)  # A, B(2: turn + replay), C, D(2), E, F
        for line in intervals:
            with self.subTest(line=line.strip()):
                self.assertIn("delivered=True", line)
                seconds = float(line.split("= ")[1].split("s")[0])
                self.assertGreater(seconds, 0.0)

    def test_the_pending_review_block_names_every_reviewable_case(self):
        self.assertIn("PENDING HUMAN REVIEW", self.text)
        for key in ("B", "C", "D", "E", "F"):
            with self.subTest(case=key):
                self.assertIn(runner.REVIEW_QUESTIONS[key], self.text)
        # A is established; F reaches review because a stop's limitation still
        # needs a human to read it.
        self.assertIn("case B (human-review)", self.text)
        self.assertIn("case E (safety-only)", self.text)
        self.assertIn("case F (human-review)", self.text)

    def test_the_report_states_what_exit_zero_does_not_mean(self):
        self.assertIn(
            "exit 0 means only that no case was refused-or-unsafe", self.text
        )

    def test_the_scripted_engine_reports_no_provider_metadata(self):
        # Honest absence: the scripted worker states no returned model, so no
        # model line is printed rather than a fabricated one.
        self.assertNotIn("  model    :", self.text)

    def test_case_d_reads_before_it_answers_and_shows_the_observation(self):
        # D's original oracle: the Continue reads the record, its answer cites a
        # reference that turn retrieved, and the case shows what actually
        # reached inference rather than ids read afterwards.
        block = _block_for(self.text, "D")
        facts = json.loads(
            "{" + block.split("facts    : {", 1)[1].split("}\n", 1)[0] + "}"
        )
        self.assertEqual(facts["verified"], [facts["context_evidence_ids"][0]])
        # The D facts carry the negative counterpart too; this positive oracle
        # has no unsupported reference.
        self.assertEqual(facts["unsupported"], [])
        observation = facts["observation_reaching_inference"]
        self.assertIn("scan outcome=succeeded", observation)
        self.assertIn("evidence record(s)", observation)
        self.assertTrue(facts["context_evidence_ids"])
        # One draft call plus the Continue's read and answer.
        self.assertIn("calls=3", _line_for(self.text, "D"))
        self.assertIn("answer_verified", self.text)
        # The verdict stays a review item, never a pass.
        self.assertIn("verdict=human-review", _line_for(self.text, "D"))


    def test_the_scan_total_is_two_synthetic_executions(self):
        setup = sum(int(_field(line, "setup_scans=")) for line in _case_lines(self.text))
        case = sum(int(_field(line, "scans=")) for line in _case_lines(self.text))
        self.assertEqual(setup, 1)   # case C's recorded setup scan
        self.assertEqual(case, 1)    # case D's own scan
        self.assertEqual(setup + case, 2)




class RenderContractTests(unittest.TestCase):
    """What the operator line says when a provider does report metadata."""

    def test_the_model_line_separates_requested_from_returned(self):
        result = runner.CaseResult(key="B", title="t", expectation="e")
        result.provider_calls = [
            {
                "requested_model": "deepseek-flash",
                "returned_model": "deepseek-flash-2026-01-01",
                "fingerprint": "fp_x",
                "usage_reported": True,
            },
            {
                "requested_model": "deepseek-flash",
                "returned_model": "",
                "fingerprint": "",
                "usage_reported": False,
            },
        ]
        text = runner._render([result])
        self.assertIn(
            "call 1 requested=deepseek-flash returned=deepseek-flash-2026-01-01 "
            "fingerprint=fp_x usage=known",
            text,
        )
        # An absent field is stated, never back-filled with the request's alias.
        self.assertIn(
            "call 2 requested=deepseek-flash returned=unavailable "
            "fingerprint=unavailable usage=unknown",
            text,
        )

    def test_a_lost_response_is_rendered_as_a_non_delivery(self):
        result = runner.CaseResult(key="D", title="t", expectation="e")
        result.response_intervals = [
            {"request": "agent_turn", "key": "k", "seconds": 12.5, "delivered": False}
        ]
        text = runner._render([result])
        self.assertIn("delivered=False", text)
        self.assertIn("12.500s", text)


class ResponseIntervalTests(unittest.TestCase):
    """The measured interval spans the whole turn, not a fragment of it."""

    def test_the_interval_contains_a_delayed_response_and_the_dto_sample(self):
        spec = runner.CaseSpec(
            key="B",
            title="delayed answer",
            goal="does anything change while a slow answer is produced?",
            scenario="slow_success",  # the scripted worker sleeps, then answers
            max_calls=1,
            expectation="a delayed answer still delivers inside one interval",
        )
        import tempfile

        base = tempfile.mkdtemp(prefix="livebatch-timing-")
        try:
            with runner._Case(spec, runner.ENGINE_OFFLINE, base) as case:
                result = case.run()
                turn = case.turn("and again?", "timing-probe")
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)

        self.assertTrue(result.response_intervals, result.problems)
        first = result.response_intervals[0]
        self.assertTrue(first["delivered"])
        # The delayed model work is inside the measured interval...
        self.assertGreater(first["seconds"], 4.0)
        # ...and the interval contains the DTO's own narrower sample, which
        # excludes the terminal commit and the transport.
        self.assertGreaterEqual(first["seconds"], float(turn["elapsed_seconds"]))
        # A second request is its own interval, not a rewrite of the first.
        self.assertEqual(len(result.response_intervals), 1)


class CitationProvenanceTests(unittest.TestCase):
    """A read in the turn is not a licence to cite anything."""

    def test_a_foreign_reference_stays_unsupported_even_after_a_read(self):
        spec = runner.CaseSpec(
            key="D",
            title="foreign citation",
            goal="scan the project and draft a plan",
            scenario="cite_foreign_once",
            max_calls=3,
            expectation="a citation no record holds must not resolve",
        )
        import tempfile

        base = tempfile.mkdtemp(prefix="livebatch-prov-")
        try:
            with runner._Case(spec, runner.ENGINE_OFFLINE, base) as case:
                case.open()
                case.ready()
                case.save_plan("recorded synthetic plan", "prov-setup")
                self.assertTrue(case.record_scan("prov-setup"))
                turn = case.turn("what did the scan record?", "prov-1")
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)
        self.assertEqual(turn["state"], "answered")
        self.assertEqual(turn["verified_references"], [])
        self.assertEqual(
            turn["unsupported_references"], ["ev:00000000000000000000000000000000"]
        )
        self.assertFalse(turn["answer_verified"])

    def test_a_context_present_owner_reference_without_a_read_stays_unsupported(self):
        # The two-call journey answers directly from the scan context. The
        # evidence id is real and the owner holds it, but this turn never read
        # the record, so it is not same-turn-retrieved and must not resolve.
        spec = runner.CaseSpec(
            key="D",
            title="context-present citation",
            goal="scan the project and draft a plan",
            scenario="journey_cite",
            max_calls=3,
            expectation="a context-present owner reference is not same-turn-retrieved",
        )
        import tempfile

        base = tempfile.mkdtemp(prefix="livebatch-context-")
        try:
            with runner._Case(spec, runner.ENGINE_OFFLINE, base) as case:
                case.open()
                case.ready()
                first = case.turn(spec.goal, "ctx-d-1")
                self.assertEqual(first["state"], "proposed")
                plan_id = str(first.get("plan_id") or "")
                self.assertTrue(plan_id)
                workflow = case.workflow()
                digest = str(workflow.get("plan_digest") or "")
                revision = str(workflow.get("plan_revision") or "")
                self.assertTrue(digest)
                case.call(
                    contract.ACTION_ORCHESTRATION_CONFIRM_PLAN,
                    {
                        "plan_id": plan_id,
                        "expected_digest": digest,
                        "idempotency_key": "ctx-d-confirm-%s" % revision,
                    },
                )
                case.call(
                    contract.ACTION_ORCHESTRATION_RUN_SCAN,
                    {"plan_id": plan_id, "idempotency_key": "ctx-d-run-%s" % revision},
                )
                case.result.scans = case.case_scans()
                self.assertEqual(case.result.scans, 1)
                record = case.evidence_view()
                second = case.turn("what did the scan record?", "ctx-d-2")
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)

        self.assertEqual(second["state"], "answered")
        self.assertEqual(second["verified_references"], [])
        self.assertFalse(second["answer_verified"])
        unsupported = list(second.get("unsupported_references") or [])
        self.assertEqual(len(unsupported), 1)
        # A real owner id was present in the context, but only a same-turn
        # read_record can make it eligible for resolution.
        self.assertIn(unsupported[0], record["evidence_ids"])


class ContextTraceShapeTests(unittest.TestCase):
    """The diagnostics are bounded, declared and rebuilt — never a payload bag."""

    def _trace(self, value):
        return executor_mod._bounded_context_trace(value)

    def test_only_declared_keys_survive(self):
        records = self._trace(
            [
                {
                    "call": 1,
                    "observation": "plan phase=confirmed",
                    "evidence": ["ev:1"],
                    "items": 12,
                    "raw": "SECRET",
                    "headers": {"authorization": "x"},
                }
            ]
        )
        self.assertEqual(sorted(records[0]), ["call", "evidence", "items", "observation"])
        self.assertNotIn("SECRET", json.dumps(records))

    def test_an_oversized_observation_and_id_list_are_bounded(self):
        records = self._trace(
            [
                {
                    "call": 2,
                    "observation": "x" * 5000,
                    "evidence": ["ev:%d" % index for index in range(50)],
                    "items": 16,
                }
            ]
        )
        self.assertEqual(
            len(records[0]["observation"]), executor_mod.context_builder_mod.MAX_FACT_CHARS
        )
        self.assertEqual(len(records[0]["evidence"]), executor_mod.MAX_TRACE_EVIDENCE_IDS)
        self.assertTrue(all(len(item) <= executor_mod.MAX_TRACE_ID_CHARS for item in records[0]["evidence"]))

    def test_missing_or_malformed_fields_become_empty_not_a_guess(self):
        records = self._trace([{"call": 1}, {"evidence": "nope"}])
        self.assertEqual(len(records), 2)
        for record in records:
            with self.subTest(record=record):
                self.assertEqual(record["evidence"], [])
                self.assertEqual(record.get("observation"), "")

    def test_a_non_mapping_entry_is_dropped(self):
        self.assertEqual(self._trace(["not a mapping"]), [])

    def test_no_more_records_than_the_turn_may_make_calls(self):
        records = self._trace([{"call": index} for index in range(10)])
        self.assertEqual(len(records), executor_mod.MAX_TRACE_RECORDS)

    def test_a_refused_turn_reports_no_trace_rather_than_inventing_one(self):
        self.assertEqual(self._trace(None), [])
        self.assertEqual(self._trace("nonsense"), [])

def _case_lines(text: str) -> list:
    return [line for line in text.splitlines() if line.startswith("case ")]


def _block_for(text: str, key: str) -> str:
    """Return the report block for one case, up to the next case or blank summary."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("case %s " % key):
            start = index
            break
    if start is None:
        return ""
    block = []
    for line in lines[start + 1:]:
        if line.startswith("case ") or line.startswith("PENDING HUMAN REVIEW"):
            break
        block.append(line)
    return "\n".join(block)


def _field(line: str, marker: str) -> int:
    start = line.index(marker) + len(marker)
    digits = ""
    for char in line[start:]:
        if not char.isdigit():
            break
        digits += char
    return int(digits or 0)


class AllocationGuardTests(unittest.TestCase):
    """A case that overspends is a stop, not a quiet loan from the next case."""

    def test_an_over_budget_case_is_failed_rather_than_accepted(self):
        # Case D costs two calls; given an allocation of one it must fail, and
        # the batch must stop rather than continue into the next case's budget.
        spec = runner.CaseSpec(
            key="D",
            title="permitted scan request",
            goal="scan the project and draft a plan",
            scenario="journey_cite",
            max_calls=1,
            expectation="deliberately under-allocated",
        )
        import tempfile

        base = tempfile.mkdtemp(prefix="livebatch-guard-")
        stream = io.StringIO()
        try:
            with runner._Case(spec, runner.ENGINE_OFFLINE, base) as case:
                result = case.run()
        finally:
            import shutil

            shutil.rmtree(base, ignore_errors=True)
        self.assertEqual(result.verdict, runner.VERDICT_FAIL)
        self.assertTrue(
            any("allocation of 1" in problem for problem in result.problems),
            result.problems,
        )
        self.assertEqual(stream.getvalue(), "")


def _line_for(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith("case %s " % key):
            return line
    return ""


def _calls_for(text: str, key: str) -> int:
    line = _line_for(text, key)
    marker = "calls="
    start = line.index(marker) + len(marker) if marker in line else -1
    if start < 0:
        return 0
    digits = ""
    for char in line[start:]:
        if not char.isdigit():
            break
        digits += char
    return int(digits or 0)


if __name__ == "__main__":
    unittest.main()
