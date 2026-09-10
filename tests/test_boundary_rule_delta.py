"""Tests for the P4.7a rule-delta boundary actions (stage_rule_delta / run_rule_delta)."""

from __future__ import annotations

import tempfile
import unittest

from hrca import app_package, boundary, contract, delta_candidate, delta_verifier, rule_delta


def _req(action, **overrides):
    req = {"contract_version": contract.CONTRACT_VERSION, "correlation_id": "cid-rd", "action": action}
    req.update(overrides)
    return req


class FakeRunner:
    """A deterministic runner double; ``run`` returns a fixed valid quotation result."""

    def __init__(self, available=True):
        self._available = available
        self.run_calls = 0

    def preflight(self):
        return {"available": self._available,
                "reason": None if self._available else "runtime_unavailable",
                "checks": {"docker": self._available, "daemon": self._available, "image": self._available}}

    def run(self, *, handler, input_payload, parameters=None):
        self.run_calls += 1
        return {
            "discount": "20.00", "shipping_fee": "0.00",
            "regional_fee": "0.00", "total": "180.00",
        }, None


_QUOTATION_RESULT_FIELDS = ["discount", "shipping_fee", "regional_fee", "total"]


class BoundaryRuleDeltaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)
        self.session.runner = FakeRunner(available=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(_req(action, **overrides), self.session)

    def _saved(self, content="v1"):
        created = self._do(contract.ACTION_DOCUMENT_CREATE, name="requirements.md")
        document_id = created["result"]["document"]["document_id"]
        saved = self._do(contract.ACTION_DOCUMENT_SAVE, document_id=document_id,
                         content=content, base_revision_id=None)
        rev = saved["result"]["revision"]
        return document_id, rev["revision_id"], rev["content_fingerprint"]

    def _delta(self):
        return {
            "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
            "result_kind": rule_delta.RESULT_KIND_QUOTATION,
            "changes": [{"operation": "set_parameter", "rule_id": "quotation",
                         "parameter_id": "member_discount_rate", "value": "0.10"}],
        }

    def _candidate(self, rev_id, fp, **overrides):
        delta = self._delta()
        binding = {
            "document_revision_id": rev_id,
            "document_fingerprint": fp,
            "baseline_fingerprint": None,
            "delta_fingerprint": delta_candidate.fingerprint(delta),
            "runner_identity": app_package.RUNNER_IDENTITY,
            "verifier_identity": delta_verifier.DELTA_VERIFIER_IDENTITY,
        }
        evidence = delta_verifier.expected_cases("quotation", {"member_discount_rate": "0.10"})
        record = {
            "schema_version": delta_candidate.DELTA_CANDIDATE_SCHEMA_VERSION,
            "provenance": rule_delta.PROVENANCE_MANUAL_DELTA,
            "delta": delta,
            "binding": binding,
            "evidence": evidence,
        }
        record.update(overrides)
        return record

    def _stage(self, document_id, candidate):
        return self._do(contract.ACTION_RULE_DELTA_STAGE, document_id=document_id, candidate=candidate)

    def test_stage_manual_delta(self):
        doc_id, rev, fp = self._saved()
        env = self._stage(doc_id, self._candidate(rev, fp))
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_MANUAL_DELTA)
        self.assertEqual(env["result"]["runtime"]["available"], True)

    def test_stage_provider_delta_is_valid_candidate(self):
        doc_id, rev, fp = self._saved()
        env = self._stage(doc_id, self._candidate(rev, fp, provenance=rule_delta.PROVENANCE_PROVIDER_DELTA))
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_VALID_CANDIDATE)

    def test_stage_deterministic_fixture(self):
        doc_id, rev, fp = self._saved()
        env = self._stage(doc_id, self._candidate(rev, fp, provenance=rule_delta.PROVENANCE_DETERMINISTIC_FIXTURE))
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_DETERMINISTIC_FIXTURE)

    def test_stage_invalid_delta(self):
        doc_id, rev, fp = self._saved()
        delta = self._delta()
        delta["code"] = "import os"
        env = self._stage(doc_id, self._candidate(rev, fp, delta=delta))
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_INVALID)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_stage_stale_revision(self):
        doc_id, _rev, fp = self._saved()
        env = self._stage(doc_id, self._candidate("rev:stale", fp))
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_INVALID)
        self.assertIn("stale", env["result"]["limitations"])

    def test_stage_baseline_mismatch(self):
        doc_id, rev, fp = self._saved()
        candidate = self._candidate(rev, fp)
        candidate["binding"]["baseline_fingerprint"] = "b" * 64
        env = self._stage(doc_id, candidate)
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_INVALID)
        self.assertIn("baseline mismatch", env["result"]["limitations"])

    def test_stage_forged_evidence(self):
        doc_id, rev, fp = self._saved()
        evidence = delta_verifier.expected_cases("quotation", {"member_discount_rate": "0.10"})
        forged = [dict(e) for e in evidence]
        forged[0] = dict(forged[0])
        forged[0]["output"] = dict(forged[0]["output"])
        forged[0]["output"]["total"] = "0.01"
        env = self._stage(doc_id, self._candidate(rev, fp, evidence=forged))
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_INSUFFICIENT_EVIDENCE)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_stage_blocked_when_runtime_unavailable(self):
        self.session.runner = FakeRunner(available=False)
        doc_id, rev, fp = self._saved()
        env = self._stage(doc_id, self._candidate(rev, fp))
        self.assertEqual(env["result"]["state"], delta_candidate.STATE_BLOCKED)
        self.assertEqual(env["result"]["runtime"]["available"], False)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_stage_missing_document(self):
        env = self._stage("doc:missing", self._candidate("rev:1", "a" * 64))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_not_found")

    def test_stage_missing_document_id(self):
        self._saved()
        env = self._do(contract.ACTION_RULE_DELTA_STAGE, candidate={})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_run_rule_delta_ok(self):
        env = self._do(contract.ACTION_RULE_DELTA_RUN, delta=self._delta(),
                       input={"subtotal": "200.00", "member": True, "region": "west"})
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], app_package.STATE_OK)
        self.assertEqual(env["result"]["result"]["total"], "180.00")
        self.assertEqual(self.session.runner.run_calls, 1)

    def test_run_rule_delta_invalid_delta(self):
        delta = self._delta()
        delta["code"] = "import os"
        env = self._do(contract.ACTION_RULE_DELTA_RUN, delta=delta,
                       input={"subtotal": "200.00", "member": True, "region": "west"})
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], app_package.STATE_PACKAGE_INVALID)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_run_rule_delta_input_invalid(self):
        env = self._do(contract.ACTION_RULE_DELTA_RUN, delta=self._delta(),
                       input={"subtotal": "-1.00", "member": True, "region": "west"})
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], app_package.STATE_INPUT_INVALID)
        self.assertEqual(self.session.runner.run_calls, 0)


if __name__ == "__main__":
    unittest.main()
