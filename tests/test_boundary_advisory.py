"""Tests for the P4.2b advisory hosted-planning boundary actions.

These are deterministic: the credential store and the advisory transport are
injected fakes, so no real Credential Manager, socket or network is used. They
prove the disclosure/confirmation flow, the one-attempt policy, cancellation
sending nothing, staleness/correlation protection, failure normalization, and
that the deterministic proposal is preserved in every state.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from hrca import advisory, boundary, contract, credential_store, deepseek_transport, provider

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures"))


def _req(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-adv",
        "action": action,
    }
    req.update(overrides)
    return req


class FakeAdvisoryTransport:
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


def _valid_result():
    return provider.ProviderResult(
        task_id="advisory:abc",
        content='{"impact":"documentation only"}',
        provider="deepseek",
        model="deepseek-flash",
        structured_payload={"impact": "documentation only"},
        usage=provider.ProviderUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
    )


class BoundaryAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_base = self._tmp.name
        self.session = boundary.WorkspaceSession(
            store_base=self.store_base,
            credential_store=credential_store.FakeCredentialStore(),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(_req(action, **overrides), self.session)

    def _open_sync_save(self):
        open_env = self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        self.assertTrue(open_env["ok"])
        sync_env = self._do(contract.ACTION_SYNC_TWIN, task={})
        self.assertTrue(sync_env["ok"])
        blocks = self._do(contract.ACTION_GET_CODE_MAP)["result"]["blocks"]
        module_id = next(
            b["block_id"]
            for b in blocks
            if b.get("block_type") == "entity"
            and (b.get("payload") or {}).get("kind") == "module"
            and (b.get("payload") or {}).get("locator") == "app.service"
        )
        purpose_id = next(
            b["block_id"]
            for b in blocks
            if b.get("block_type") == "purpose" and b.get("parent_id") == module_id
        )
        save_env = self._do(
            contract.ACTION_SAVE_DRAFT,
            task={
                "operations": [
                    {
                        "op": "replace_description",
                        "target_block_id": purpose_id,
                        "proposed_text": "entry point for the service",
                    }
                ]
            },
        )
        self.assertTrue(save_env["ok"])

    def _prepare(self):
        return self._do(contract.ACTION_PREPARE_ADVISORY)

    # -- prepare gate tests ----------------------------------------------

    def test_prepare_requires_open_project(self):
        env = self._prepare()
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "project_not_open")

    def test_prepare_requires_sync(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        env = self._prepare()
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "twin_not_synchronized")

    def test_prepare_requires_draft(self):
        self._do(contract.ACTION_OPEN_PROJECT, path=FIXTURES)
        self._do(contract.ACTION_SYNC_TWIN, task={})
        env = self._prepare()
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "draft_not_found")

    def test_prepare_returns_disclosure_and_token(self):
        self._open_sync_save()
        env = self._prepare()
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertTrue(result["advisory_available"])
        self.assertEqual(result["provider_id"], "deepseek")
        self.assertTrue(result["advisory_token"].startswith("advisory:"))
        self.assertTrue(result["disclosure"]["one_attempt"])
        self.assertGreater(result["disclosure"]["total_bytes"], 0)

    def test_prepare_preserves_deterministic_proposal(self):
        self._open_sync_save()
        result = self._prepare()["result"]
        proposal = result["proposal"]
        self.assertEqual(proposal["state"], "ready")
        self.assertFalse(proposal["executable"])
        self.assertFalse(proposal["applied"])

    def test_prepare_is_offline(self):
        # prepare never touches the transport; it needs none and none is set.
        self._open_sync_save()
        env = self._prepare()
        self.assertTrue(env["ok"])

    # -- plan gate tests --------------------------------------------------

    def test_plan_requires_confirmed_bool(self):
        self._open_sync_save()
        env = self._do(contract.ACTION_PLAN_ADVISORY, task={"advisory_token": "t", "confirmed": "yes"})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_plan_requires_token(self):
        self._open_sync_save()
        env = self._do(contract.ACTION_PLAN_ADVISORY, task={"confirmed": True})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_plan_cancel_sends_nothing(self):
        self._open_sync_save()
        token = self._prepare()["result"]["advisory_token"]
        transport = FakeAdvisoryTransport(result=_valid_result())
        self.session.advisory_transport = transport
        env = self._do(
            contract.ACTION_PLAN_ADVISORY,
            task={"advisory_token": token, "confirmed": False},
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], advisory.STATE_CANCEL_REQUESTED)
        self.assertFalse(result["sent"])
        self.assertIsNone(result["provider_suggested"])
        self.assertEqual(transport.calls, 0)

    def test_plan_stale_token_sends_nothing(self):
        self._open_sync_save()
        transport = FakeAdvisoryTransport(result=_valid_result())
        self.session.advisory_transport = transport
        env = self._do(
            contract.ACTION_PLAN_ADVISORY,
            task={"advisory_token": "advisory:wrong", "confirmed": True},
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], advisory.STATE_STALE_RESPONSE)
        self.assertFalse(result["sent"])
        self.assertEqual(transport.calls, 0)

    def test_plan_ready_success_makes_one_call(self):
        self._open_sync_save()
        token = self._prepare()["result"]["advisory_token"]
        transport = FakeAdvisoryTransport(result=_valid_result())
        self.session.advisory_transport = transport
        env = self._do(
            contract.ACTION_PLAN_ADVISORY,
            task={"advisory_token": token, "confirmed": True},
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], advisory.STATE_READY)
        self.assertTrue(result["sent"])
        self.assertEqual(result["provider_suggested"]["impact"], "documentation only")
        self.assertEqual(result["deterministic"]["proposal"]["state"], "ready")
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(transport.calls, 1)

    def test_plan_maps_transport_error(self):
        self._open_sync_save()
        token = self._prepare()["result"]["advisory_token"]
        transport = FakeAdvisoryTransport(
            error=deepseek_transport.TransportError("timeout")
        )
        self.session.advisory_transport = transport
        env = self._do(
            contract.ACTION_PLAN_ADVISORY,
            task={"advisory_token": token, "confirmed": True},
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], advisory.STATE_TIMEOUT)
        self.assertIsNone(result["provider_suggested"])
        self.assertEqual(result["deterministic"]["proposal"]["state"], "ready")

    def test_plan_maps_provider_failure(self):
        self._open_sync_save()
        token = self._prepare()["result"]["advisory_token"]
        transport = FakeAdvisoryTransport(
            error=provider.ProviderError("provider_failure")
        )
        self.session.advisory_transport = transport
        env = self._do(
            contract.ACTION_PLAN_ADVISORY,
            task={"advisory_token": token, "confirmed": True},
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], advisory.STATE_PROVIDER_FAILURE)
        self.assertIsNone(result["provider_suggested"])

    def test_result_never_carries_absolute_paths(self):
        self._open_sync_save()
        env = self._prepare()
        serialized = contract.dumps(env)
        self.assertNotIn(FIXTURES, serialized)
        self.assertNotIn("C:\\", serialized)


if __name__ == "__main__":
    unittest.main()
