"""Tests for the P4.7 candidate-package staging seam (boundary action)."""

from __future__ import annotations

import tempfile
import unittest

from hrca import app_package, boundary, candidate_package, contract, verifier


def _req(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-cpkg",
        "action": action,
    }
    req.update(overrides)
    return req


class FakeRunner:
    """A deterministic runner double whose ``run`` must never be called."""

    def __init__(self, available=True):
        self._available = available
        self.run_calls = 0

    def preflight(self):
        return {
            "available": self._available,
            "reason": None if self._available else "runtime_unavailable",
            "checks": {"docker": self._available, "daemon": self._available, "image": self._available},
        }

    def run(self, *, handler, input_payload):
        self.run_calls += 1
        raise AssertionError("the staging seam must never execute a package")


class BoundaryCandidatePackageTests(unittest.TestCase):
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
        saved = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=document_id,
            content=content,
            base_revision_id=None,
        )
        revision = saved["result"]["revision"]
        return document_id, revision["revision_id"], revision["content_fingerprint"]

    def _candidate(self, revision_id, fingerprint, **overrides):
        record = {
            "schema_version": candidate_package.CANDIDATE_PACKAGE_SCHEMA_VERSION,
            "candidate_package_id": candidate_package.new_candidate_package_id(),
            "provenance": candidate_package.PROVENANCE_DETERMINISTIC_FIXTURE,
            "variant_id": verifier.VARIANT_REFERENCE,
            "package": verifier.variant_package(verifier.VARIANT_REFERENCE),
            "binding": {
                "document_revision_id": revision_id,
                "document_fingerprint": fingerprint,
                "runtime": app_package.RUNNER_IDENTITY,
                "verifier_identity": verifier.VERIFIER_IDENTITY,
            },
            "evidence": verifier.regression_cases(verifier.VARIANT_REFERENCE),
        }
        record.update(overrides)
        return record

    def _stage(self, document_id, candidate):
        return self._do(
            contract.ACTION_CANDIDATE_PACKAGE_STAGE,
            document_id=document_id,
            candidate_package=candidate,
        )

    def test_stage_deterministic_fixture(self):
        document_id, rev, fp = self._saved()
        env = self._stage(document_id, self._candidate(rev, fp))
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_DETERMINISTIC_FIXTURE)
        self.assertEqual(env["result"]["runtime"]["available"], True)

    def test_stage_manual_variant(self):
        document_id, rev, fp = self._saved()
        alt = self._candidate(
            rev, fp,
            provenance=candidate_package.PROVENANCE_MANUAL_VARIANT,
            variant_id=verifier.VARIANT_ALT,
            package=verifier.variant_package(verifier.VARIANT_ALT),
            evidence=verifier.regression_cases(verifier.VARIANT_ALT),
        )
        env = self._stage(document_id, alt)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_MANUAL_VARIANT)

    def test_stage_provider_produced_is_valid_candidate(self):
        document_id, rev, fp = self._saved()
        candidate = self._candidate(rev, fp, provenance=candidate_package.PROVENANCE_PROVIDER)
        env = self._stage(document_id, candidate)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_VALID_CANDIDATE)

    def test_stage_invalid_package_never_executes(self):
        document_id, rev, fp = self._saved()
        bad = self._candidate(rev, fp)
        bad["script"] = "import os"
        env = self._stage(document_id, bad)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_INVALID)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_stage_stale_revision_is_invalid(self):
        document_id, rev, fp = self._saved()
        env = self._stage(document_id, self._candidate("rev:stale", fp))
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_INVALID)
        self.assertIn("stale", env["result"]["limitations"])

    def test_stage_stale_fingerprint_is_invalid(self):
        document_id, rev, _fp = self._saved()
        env = self._stage(document_id, self._candidate(rev, "0" * 64))
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_INVALID)

    def test_stage_forged_evidence_is_insufficient(self):
        document_id, rev, fp = self._saved()
        evidence = verifier.regression_cases(verifier.VARIANT_REFERENCE)
        forged = [dict(e) for e in evidence]
        forged[0] = dict(forged[0])
        forged[0]["output"] = dict(forged[0]["output"])
        forged[0]["output"]["total"] = "0.01"
        env = self._stage(document_id, self._candidate(rev, fp, evidence=forged))
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_INSUFFICIENT_EVIDENCE)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_stage_blocked_when_runtime_unavailable(self):
        self.session.runner = FakeRunner(available=False)
        document_id, rev, fp = self._saved()
        env = self._stage(document_id, self._candidate(rev, fp))
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], candidate_package.STATE_BLOCKED)
        self.assertEqual(env["result"]["runtime"]["available"], False)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_stage_missing_document_is_error(self):
        env = self._stage("doc:missing", self._candidate("rev:1", "a" * 64))
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_not_found")

    def test_stage_missing_document_id_is_error(self):
        self._saved()
        env = self._do(contract.ACTION_CANDIDATE_PACKAGE_STAGE, candidate_package={})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_stage_has_no_runner_or_provider_side_effect(self):
        document_id, rev, fp = self._saved()
        candidate = self._candidate(rev, fp)
        # The session's runner (FakeRunner.run) and transport are never touched.
        self.session.advisory_transport = _NoTransport()
        env = self._stage(document_id, candidate)
        self.assertTrue(env["ok"])
        self.assertEqual(self.session.runner.run_calls, 0)


class _NoTransport:
    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raise AssertionError("provider transport must not be called")


if __name__ == "__main__":
    unittest.main()
