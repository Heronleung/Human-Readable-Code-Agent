"""Tests for the P4.2a redacted local readiness boundary action."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import boundary, contract, credential_store, provider_config

_SECRET_LIKE = "secret-token-abc123"


def _request(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-ready",
        "action": action,
    }
    req.update(overrides)
    return req


class BoundaryReadinessTests(unittest.TestCase):
    """The ``get_readiness`` action over a shared boundary session.

    The credential store is an injected fake and the config file lives under a
    temporary base directory, so the real Credential Manager and the per-user
    app-data directory are never touched during a test run.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_base = self._tmp.name
        self.store = credential_store.FakeCredentialStore()
        self.session = boundary.WorkspaceSession(
            store_base=self.store_base, credential_store=self.store
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action=contract.ACTION_GET_READINESS, **overrides):
        return boundary.handle_request(_request(action, **overrides), self.session)

    def _write_config(self, obj):
        os.makedirs(self.store_base, exist_ok=True)
        with open(provider_config.config_path(self.store_base), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_readiness_missing_credential_without_project(self):
        env = self._do()
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["state"], "missing_credential")
        self.assertEqual(result["provider_id"], "deepseek")
        self.assertEqual(result["model"], "deepseek-v4-flash")
        self.assertFalse(result["credential_present"])

    def test_readiness_configured_when_credential_present(self):
        self.store.store(credential_store.TARGET_NAME, "test-value")
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "configured")
        self.assertTrue(env["result"]["credential_present"])

    def test_readiness_invalid_config(self):
        self._write_config({"schema_version": "999.0.0"})
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "invalid_config")
        self.assertIsNone(env["result"]["model"])

    def test_readiness_unavailable_store(self):
        self.session.credential_store = credential_store.UnavailableCredentialStore()
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "unavailable")
        self.assertFalse(env["result"]["credential_present"])

    def test_readiness_never_claims_network(self):
        self.store.store(credential_store.TARGET_NAME, "test-value")
        result = self._do()["result"]
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["online"])
        self.assertFalse(result["executable"])

    def test_readiness_is_secret_free(self):
        self.store.store(credential_store.TARGET_NAME, _SECRET_LIKE)
        env = self._do()
        serialized = contract.dumps(env)
        self.assertNotIn(_SECRET_LIKE, serialized)
        for token in ("secret", "token", "api_key", "password", "authorization", "bearer"):
            self.assertNotIn(token, serialized)

    def test_readiness_uses_presence_not_read(self):
        # The readiness handler must use ``has`` (presence) and never
        # materialize the credential; a store that fails on ``read`` still
        # reports the credential as present.
        class PresenceOnlyStore(credential_store.FakeCredentialStore):
            def read(self, target):
                raise AssertionError("read() must not be called for readiness")

        store = PresenceOnlyStore()
        store.store(credential_store.TARGET_NAME, "test-value")
        self.session.credential_store = store
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "configured")


if __name__ == "__main__":
    unittest.main()
