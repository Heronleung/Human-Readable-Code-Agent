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
        self.assertEqual(result["model"], "deepseek-flash")
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

    def test_readiness_reads_for_retrievability_not_presence(self):
        # P4.8b: readiness must distinguish "metadata present" from "actually
        # retrievable". An empty-blob credential reports ``has`` true but reads
        # an empty secret, so it must be ``missing_credential`` — never the
        # misleading ``configured``.
        class EmptyBlobStore(credential_store.FakeCredentialStore):
            def read(self, target):
                _ = self._values.get(target)
                return ""  # present (has=True) but not retrievable

        store = EmptyBlobStore()
        store.store(credential_store.TARGET_NAME, "x")
        self.session.credential_store = store
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "missing_credential")
        self.assertFalse(env["result"]["credential_present"])

    def test_readiness_unretrievable_credential_is_distinct(self):
        # A bounded read failure is ``credential_unretrievable``, distinct from
        # a plain missing credential.
        class ReadFailsStore(credential_store.FakeCredentialStore):
            def read(self, target):
                raise credential_store.CredentialStoreError("store_failed")

        store = ReadFailsStore()
        store.store(credential_store.TARGET_NAME, "x")
        self.session.credential_store = store
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "credential_unretrievable")
        self.assertFalse(env["result"]["credential_present"])

    def test_readiness_active_profile_retrievable_is_configured(self):
        profile_id = "a" * 32
        target = credential_store.profile_target(profile_id)
        self.store.store(target, "secret-token-abc123")
        self._write_config(
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
            }
        )
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "configured")
        self.assertTrue(env["result"]["credential_present"])

    def test_readiness_orphaned_profile_is_missing_credential(self):
        # Profile metadata exists (and is active) but no credential is stored:
        # metadata-only must never produce a "configured" success.
        profile_id = "a" * 32
        self._write_config(
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
            }
        )
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "missing_credential")
        self.assertFalse(env["result"]["credential_present"])

    def test_readiness_no_active_profile_is_no_profile(self):
        # Profiles exist but none is active: no credential applies.
        profile_id = "a" * 32
        self.store.store(credential_store.profile_target(profile_id), "x")
        self._write_config(
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
                "active_profile_id": None,
            }
        )
        env = self._do()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "no_profile")
        self.assertFalse(env["result"]["credential_present"])


if __name__ == "__main__":
    unittest.main()
