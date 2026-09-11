"""Offline Docker integration for the P4.8 provider-to-rule-delta flow.

This is the *one* slice that executes the actual reviewed ``hrca-runner:v1``
image. A fake provider returns ``member_discount_rate = 0.10``; the normal P4.8
boundary handler then validates the delta, runs the code-owned protected inputs
inside the real isolated container (network none, non-root, read-only rootfs),
and the independent oracle verifies the output before a reviewable Candidate is
produced. No DeepSeek endpoint or live credential is used.

Skipped automatically when the Docker client, daemon or reviewed image is
unavailable — the failure is reported as a skip, never a host-Python fallback.
"""

from __future__ import annotations

import tempfile
import unittest

from hrca import (
    app_package,
    boundary,
    container_runner,
    contract,
    delta_verifier,
    provider,
    rule_delta,
    rule_delta_interpret,
)

_REQUIREMENT = "Members receive a 10% discount on quotations."


class _RecordingRunner(container_runner.ContainerRunner):
    """The real Docker runner, recording each hardened ``docker run`` argv."""

    def __init__(self):
        super().__init__()
        self.commands = []
        self.run_calls = 0

    def build_command(self, **kwargs):
        argv = super().build_command(**kwargs)
        self.commands.append(argv)
        return argv

    def run(self, **kwargs):
        self.run_calls += 1
        return super().run(**kwargs)


class _FakeDeltaTransport:
    """A deterministic provider double that returns one bounded result."""

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return self._result


def _quotation_delta():
    return {
        "schema_version": rule_delta.RULE_DELTA_SCHEMA_VERSION,
        "result_kind": rule_delta.RESULT_KIND_QUOTATION,
        "changes": [
            {
                "operation": rule_delta.OPERATION_SET_PARAMETER,
                "rule_id": rule_delta.RESULT_KIND_QUOTATION,
                "parameter_id": "member_discount_rate",
                "value": "0.10",
            }
        ],
    }


def _result_for(payload):
    return provider.ProviderResult(
        task_id="delta:integration",
        content=rule_delta_interpret.dumps(payload),
        provider="deepseek",
        model="deepseek-v4-flash",
        structured_payload=payload,
        usage=provider.ProviderUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _delta_payload():
    return {"outcome": rule_delta_interpret.OUTCOME_DELTA, "delta": _quotation_delta()}


def _clarification_payload():
    return {
        "outcome": rule_delta_interpret.OUTCOME_CLARIFICATION_REQUIRED,
        "clarification_questions": ["which rule should change?"],
    }


class DockerRuleDeltaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        preflight = container_runner.ContainerRunner().preflight()
        if not preflight.get("available"):
            raise unittest.SkipTest(
                f"Docker runner unavailable: {preflight.get('reason')} "
                f"(checks={preflight.get('checks')})"
            )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runner = _RecordingRunner()
        self.transport = _FakeDeltaTransport(_result_for(_delta_payload()))
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)
        self.session.runner = self.runner
        self.session.delta_transport = self.transport

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        req = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "cid-integration",
            "action": action,
        }
        req.update(overrides)
        return boundary.handle_request(req, self.session)

    def _saved(self, content=_REQUIREMENT):
        created = self._do(contract.ACTION_DOCUMENT_CREATE, name="requirements.md")
        document_id = created["result"]["document"]["document_id"]
        self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=document_id,
            content=content,
            base_revision_id=None,
        )
        return document_id

    def test_fake_provider_delta_runs_real_container_and_verifies(self):
        document_id = self._saved()
        prepared = self._do(contract.ACTION_PREPARE_RULE_DELTA, document_id=document_id)
        self.assertTrue(prepared["ok"])
        token = prepared["result"]["token"]

        env = self._do(
            contract.ACTION_INTERPRET_RULE_DELTA,
            document_id=document_id,
            task={"token": token, "confirmed": True},
        )
        self.assertTrue(env["ok"], env)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_REVIEWABLE_CANDIDATE)
        self.assertTrue(result["sent"])
        self.assertEqual(self.transport.calls, 1)

        # The actual container executed the protected inputs — never host Python.
        self.assertGreater(self.runner.run_calls, 0)
        self.assertGreater(len(self.runner.commands), 0)
        for argv in self.runner.commands:
            self.assertIn("hrca-runner:v1", argv)
            self.assertIn("--network", argv)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertIn("--read-only", argv)

        # Independent oracle: canonical protected case 200/member/west -> 20.00/180.00.
        canonical = result["candidate"]["evidence"][0]
        self.assertEqual(
            canonical["input"], {"subtotal": "200.00", "member": True, "region": "west"}
        )
        self.assertEqual(canonical["output"]["discount"], "20.00")
        self.assertEqual(canonical["output"]["total"], "180.00")

        # The candidate is bound to the reviewed runner and verifier identities.
        binding = result["candidate"]["binding"]
        self.assertEqual(binding["runner_identity"], app_package.RUNNER_IDENTITY)
        self.assertEqual(binding["verifier_identity"], delta_verifier.DELTA_VERIFIER_IDENTITY)
        # Never auto-adopted.
        self.assertEqual(result["candidate"]["provenance"], rule_delta.PROVENANCE_PROVIDER_DELTA)

    def test_clarification_never_starts_the_runner(self):
        document_id = self._saved(content="Change the pricing rules.")
        prepared = self._do(contract.ACTION_PREPARE_RULE_DELTA, document_id=document_id)
        token = prepared["result"]["token"]
        self.session.delta_transport = _FakeDeltaTransport(
            _result_for(_clarification_payload())
        )

        env = self._do(
            contract.ACTION_INTERPRET_RULE_DELTA,
            document_id=document_id,
            task={"token": token, "confirmed": True},
        )
        self.assertTrue(env["ok"], env)
        result = env["result"]
        self.assertEqual(
            result["state"], rule_delta_interpret.STATE_CLARIFICATION_REQUIRED
        )
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.runner.run_calls, 0)
        self.assertEqual(len(self.runner.commands), 0)


if __name__ == "__main__":
    unittest.main()
