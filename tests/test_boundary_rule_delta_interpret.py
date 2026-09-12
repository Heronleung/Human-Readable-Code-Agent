"""Tests for the P4.8 provider-to-rule-delta boundary actions.

Deterministic: the delta transport and the runner are injected fakes, so no real
credential, socket or Docker is used. They prove the offline disclosure, the
one-attempt confirmation gate, staleness protection, the bounded provider-output
contract, and that a valid quotation delta becomes a reviewable Candidate only
after isolated execution and independent verification.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import (
    app_package,
    boundary,
    contract,
    delta_verifier,
    provider,
    rule_delta,
    rule_delta_interpret,
    runtime_handlers,
)

_REQUIREMENT = "Members receive a 10% discount on quotations."
_INSUFFICIENT = "Change the pricing rules."


def _req(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-p48",
        "action": action,
    }
    req.update(overrides)
    return req


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


class RealisticFakeRunner:
    """A runner double that delegates to the trusted handlers (correct output)."""

    def __init__(self, available=True):
        self._available = available
        self.run_calls = 0

    def preflight(self):
        return {
            "available": self._available,
            "reason": None if self._available else "runtime_unavailable",
            "checks": {"docker": self._available, "daemon": self._available, "image": self._available},
        }

    def run(self, *, handler, input_payload, parameters=None):
        self.run_calls += 1
        function = runtime_handlers.resolve_handler(handler)
        result, error = function(input_payload, parameters)
        return result, error


class WrongRunner:
    """A runner double that returns a wrong result (verification must fail)."""

    def __init__(self):
        self.run_calls = 0

    def preflight(self):
        return {"available": True, "reason": None,
                "checks": {"docker": True, "daemon": True, "image": True}}

    def run(self, *, handler, input_payload, parameters=None):
        self.run_calls += 1
        return {
            "discount": "0.01", "shipping_fee": "0.00",
            "regional_fee": "0.00", "total": "999.00",
        }, None


class FakeDeltaTransport:
    """A deterministic transport double that counts its ``generate`` calls."""

    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result
        self.error = error

    def generate(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _usage(prompt=10, completion=5):
    return provider.ProviderUsage(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def _delta_result(payload, usage=None):
    return provider.ProviderResult(
        task_id="delta:abc",
        content=rule_delta_interpret.dumps(payload),
        provider="deepseek",
        model="deepseek-flash",
        structured_payload=payload,
        usage=usage if usage is not None else _usage(),
    )


def _delta_payload():
    return {"outcome": rule_delta_interpret.OUTCOME_DELTA, "delta": _quotation_delta()}


class BoundaryRuleDeltaInterpretTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)
        self.session.runner = RealisticFakeRunner(available=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(_req(action, **overrides), self.session)

    def _saved(self, content=_REQUIREMENT):
        created = self._do(contract.ACTION_DOCUMENT_CREATE, name="requirements.md")
        document_id = created["result"]["document"]["document_id"]
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=document_id,
                 content=content, base_revision_id=None)
        return document_id

    def _prepare(self, document_id):
        return self._do(contract.ACTION_PREPARE_RULE_DELTA, document_id=document_id)

    def _interpret(self, document_id, token, confirmed=True):
        return self._do(
            contract.ACTION_INTERPRET_RULE_DELTA,
            document_id=document_id,
            task={"token": token, "confirmed": confirmed},
        )

    # -- prepare ----------------------------------------------------------

    def test_prepare_requires_document_id(self):
        env = self._do(contract.ACTION_PREPARE_RULE_DELTA)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_prepare_requires_saved_revision(self):
        created = self._do(contract.ACTION_DOCUMENT_CREATE, name="empty.md")
        document_id = created["result"]["document"]["document_id"]
        env = self._prepare(document_id)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_not_saved")

    def test_prepare_is_offline_and_returns_disclosure(self):
        document_id = self._saved()
        env = self._prepare(document_id)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertTrue(result["available"])
        self.assertEqual(result["provider_id"], "deepseek")
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertTrue(result["token"].startswith("delta:"))
        disclosure = result["disclosure"]
        self.assertTrue(disclosure["one_attempt"])
        self.assertIn("policy_warning", disclosure)
        self.assertIn("reservation", disclosure)
        self.assertTrue(disclosure["reservation"]["sufficient"])

    def test_prepare_never_calls_transport(self):
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        document_id = self._saved()
        self._prepare(document_id)
        self.assertEqual(transport.calls, 0)

    # -- interpret gate ---------------------------------------------------

    def test_interpret_requires_confirmed_bool(self):
        document_id = self._saved()
        env = self._do(
            contract.ACTION_INTERPRET_RULE_DELTA,
            document_id=document_id,
            task={"token": "delta:abc", "confirmed": "yes"},
        )
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_interpret_requires_token(self):
        document_id = self._saved()
        env = self._do(
            contract.ACTION_INTERPRET_RULE_DELTA,
            document_id=document_id,
            task={"confirmed": True},
        )
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_cancel_sends_nothing(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=False)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_CANCEL_REQUESTED)
        self.assertFalse(result["sent"])
        self.assertIsNone(result["candidate"])
        self.assertEqual(transport.calls, 0)

    def test_stale_token_sends_nothing(self):
        document_id = self._saved()
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        env = self._interpret(document_id, "delta:wrong", confirmed=True)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_STALE)
        self.assertFalse(result["sent"])
        self.assertEqual(transport.calls, 0)

    def test_document_change_makes_token_stale(self):
        created = self._do(contract.ACTION_DOCUMENT_CREATE, name="requirements.md")
        document_id = created["result"]["document"]["document_id"]
        first = self._do(contract.ACTION_DOCUMENT_SAVE, document_id=document_id,
                         content=_REQUIREMENT, base_revision_id=None)
        rev_id = first["result"]["revision"]["revision_id"]
        token = self._prepare(document_id)["result"]["token"]
        # The document changes after prepare, so the token no longer matches.
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=document_id,
                 content=_INSUFFICIENT, base_revision_id=rev_id)
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        self.assertEqual(env["result"]["state"], rule_delta_interpret.STATE_STALE)
        self.assertEqual(transport.calls, 0)

    # -- success ----------------------------------------------------------

    def test_success_creates_reviewable_candidate(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(
            result["state"], rule_delta_interpret.STATE_REVIEWABLE_CANDIDATE
        )
        self.assertTrue(result["sent"])
        self.assertEqual(transport.calls, 1)
        candidate = result["candidate"]
        self.assertEqual(candidate["provenance"], rule_delta.PROVENANCE_PROVIDER_DELTA)
        self.assertEqual(candidate["binding"]["runner_identity"], app_package.RUNNER_IDENTITY)
        self.assertEqual(
            candidate["binding"]["verifier_identity"],
            delta_verifier.DELTA_VERIFIER_IDENTITY,
        )
        # The canonical protected case ran through the isolated runner and
        # matched the independent oracle: 200 / member / west -> 20.00 / 180.00.
        canonical = candidate["evidence"][0]
        self.assertEqual(canonical["input"], {"subtotal": "200.00", "member": True, "region": "west"})
        self.assertEqual(canonical["output"]["discount"], "20.00")
        self.assertEqual(canonical["output"]["total"], "180.00")
        # The protected inputs were actually executed (never merely claimed).
        self.assertGreaterEqual(self.session.runner.run_calls, 1)

    # -- bounded outcomes -------------------------------------------------

    def test_clarification_required_runs_nothing(self):
        document_id = self._saved(content=_INSUFFICIENT)
        token = self._prepare(document_id)["result"]["token"]
        payload = {
            "outcome": rule_delta_interpret.OUTCOME_CLARIFICATION_REQUIRED,
            "clarification_questions": ["which rule should change?"],
        }
        transport = FakeDeltaTransport(result=_delta_result(payload))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(
            result["state"], rule_delta_interpret.STATE_CLARIFICATION_REQUIRED
        )
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_unsupported_runs_nothing(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        payload = {
            "outcome": rule_delta_interpret.OUTCOME_UNSUPPORTED,
            "unsupported_requirements": ["a new rule family is required"],
        }
        transport = FakeDeltaTransport(result=_delta_result(payload))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_UNSUPPORTED)
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_invalid_output_runs_nothing(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        transport = FakeDeltaTransport(
            result=_delta_result({"outcome": "code", "delta": _quotation_delta()})
        )
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_INVALID_OUTPUT)
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_malformed_delta_runs_nothing(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        bad = _quotation_delta()
        bad["code"] = "import os"
        transport = FakeDeltaTransport(
            result=_delta_result({"outcome": rule_delta_interpret.OUTCOME_DELTA, "delta": bad})
        )
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        self.assertEqual(env["result"]["state"], rule_delta_interpret.STATE_INVALID_OUTPUT)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_usage_unknown_fails_closed(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        no_usage = provider.ProviderResult(
            task_id="delta:abc",
            content=rule_delta_interpret.dumps(_delta_payload()),
            provider="deepseek",
            model="deepseek-flash",
            structured_payload=_delta_payload(),
            usage=None,
        )
        transport = FakeDeltaTransport(result=no_usage)
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_USAGE_UNKNOWN)
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_runner_unavailable_blocks(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        self.session.runner = RealisticFakeRunner(available=False)
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_RUNNER_UNAVAILABLE)
        self.assertIsNone(result["candidate"])

    def test_verification_failed_blocks(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        self.session.runner = WrongRunner()
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(
            result["state"], rule_delta_interpret.STATE_VERIFICATION_FAILED
        )
        self.assertIsNone(result["candidate"])

    def test_transport_error_maps_to_bounded_state(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        from hrca import delta_transport

        transport = FakeDeltaTransport(error=delta_transport.TransportError("timeout"))
        self.session.delta_transport = transport
        env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_TIMEOUT)
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.session.runner.run_calls, 0)

    # -- no-egress --------------------------------------------------------

    def test_provider_request_never_carries_protected_oracle_data(self):
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        captured = {}

        class CapturingTransport:
            def generate(self, request):
                captured["task"] = request.task
                captured["context"] = list(request.context)
                return _delta_result(_delta_payload())

        self.session.delta_transport = CapturingTransport()
        env = self._interpret(document_id, token, confirmed=True)
        self.assertEqual(env["result"]["state"], rule_delta_interpret.STATE_REVIEWABLE_CANDIDATE)
        combined = captured["task"] + "\n".join(captured["context"])
        for forbidden in ("subtotal", "200.00", "180.00", "west", "days_late"):
            self.assertNotIn(forbidden, combined)

    # -- unresolved snapshot blocks before transport ------------------------

    def test_unknown_pricing_blocks_before_transport(self):
        from unittest import mock

        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]
        transport = FakeDeltaTransport(result=_delta_result(_delta_payload()))
        self.session.delta_transport = transport
        # Remove the model from the pricing table so the reservation cannot be
        # established; the confirmed interpret must fail closed *before* the
        # transport is ever constructed/called.
        with mock.patch.object(rule_delta_interpret, "PRICING", {}):
            env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_PRICING_UNKNOWN)
        self.assertFalse(result["sent"])
        self.assertIsNone(result["candidate"])
        self.assertEqual(transport.calls, 0)
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_model_facts_are_internally_consistent(self):
        # The snapshot is only dispatchable when requested id, canonical id and
        # effective version agree with an available peak-rate entry.
        self.assertEqual(
            rule_delta_interpret.MODEL_ID, rule_delta_interpret.CANONICAL_MODEL_ID
        )
        self.assertEqual(rule_delta_interpret.MODEL_ID, "deepseek-flash")
        self.assertEqual(
            rule_delta_interpret.EFFECTIVE_MODEL_VERSION, "DeepSeek-V4.1-Flash"
        )
        self.assertIn("deepseek-v4-flash", rule_delta_interpret.COMPATIBILITY_ALIASES)
        self.assertEqual(
            rule_delta_interpret.COMPATIBILITY_ALIAS_STATUS,
            "retired_routes_to_v4_1_flash",
        )
        self.assertIsNotNone(
            rule_delta_interpret.pricing_for(rule_delta_interpret.MODEL_ID)
        )

    # -- transport-before-credential ordering (P4.8b) ----------------------

    def test_missing_credential_fails_before_transport_construction(self):
        from unittest import mock

        from hrca import credential_store, delta_transport

        self.session.credential_store = credential_store.FakeCredentialStore()
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]

        def _boom(*args, **kwargs):
            raise AssertionError(
                "transport must not be constructed without a retrievable credential"
            )

        with mock.patch.object(
            delta_transport, "DeltaInterpretProvider", side_effect=_boom
        ):
            env = self._interpret(document_id, token, confirmed=True)

        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_CREDENTIAL_MISSING)
        self.assertFalse(result["sent"])
        self.assertIsNone(result["candidate"])
        self.assertEqual(self.session.runner.run_calls, 0)
        # A clear, bounded recovery instruction is returned — never a secret.
        self.assertTrue(result["limitations"])
        self.assertIn("Settings", result["limitations"][0])

    def test_orphaned_active_profile_fails_before_transport(self):
        # A profile is present and active in the config but its credential is
        # absent (metadata-only): the confirmed interpret fails before any
        # transport with ``sent: false`` and a recovery instruction.
        from unittest import mock

        from hrca import credential_store, delta_transport, provider_config

        profile_id = "a" * 32
        self.session.credential_store = credential_store.FakeCredentialStore()
        os.makedirs(self._tmp.name, exist_ok=True)
        with open(
            provider_config.config_path(self._tmp.name), "w", encoding="utf-8"
        ) as fh:
            json.dump(
                {
                    "schema_version": provider_config.CONFIG_SCHEMA_VERSION,
                    "provider_id": "deepseek",
                    "model": "deepseek-flash",
                    "profiles": [
                        {
                            "profile_id": profile_id,
                            "provider_id": "deepseek",
                            "display_name": "Work",
                        }
                    ],
                    "active_profile_id": profile_id,
                },
                fh,
            )
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]

        def _boom(*args, **kwargs):
            raise AssertionError("transport must not be constructed")

        with mock.patch.object(
            delta_transport, "DeltaInterpretProvider", side_effect=_boom
        ):
            env = self._interpret(document_id, token, confirmed=True)
        result = env["result"]
        self.assertEqual(result["state"], rule_delta_interpret.STATE_CREDENTIAL_MISSING)
        self.assertFalse(result["sent"])

    def test_retrievable_credential_passes_precheck_and_dispatches(self):
        # With a retrievable credential the pre-check passes, the transport is
        # constructed with a working credential getter (resolving the same
        # opaque target) and the single provider request proceeds.
        from unittest import mock

        from hrca import credential_store, delta_transport

        store = credential_store.FakeCredentialStore()
        store.store(credential_store.TARGET_NAME, "sk-test-secret")
        self.session.credential_store = store
        document_id = self._saved()
        token = self._prepare(document_id)["result"]["token"]

        captured = {}

        class FakeProvider:
            def __init__(self, credential_getter=None, **kwargs):
                captured["credential_getter"] = credential_getter

            def generate(self, request):
                captured["generate_called"] = True
                return _delta_result(_delta_payload())

        with mock.patch.object(delta_transport, "DeltaInterpretProvider", FakeProvider):
            env = self._interpret(document_id, token, confirmed=True)

        result = env["result"]
        self.assertEqual(
            result["state"], rule_delta_interpret.STATE_REVIEWABLE_CANDIDATE
        )
        self.assertTrue(result["sent"])
        self.assertTrue(captured.get("generate_called"))
        self.assertEqual(captured["credential_getter"](), "sk-test-secret")


if __name__ == "__main__":
    unittest.main()
