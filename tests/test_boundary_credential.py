"""Tests for the P4.2a backend-owned credential management boundary actions."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from hrca import boundary, contract, credential_store

_SECRET_LIKE = "secret-token-abc123"


def _request(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-cred",
        "action": action,
    }
    req.update(overrides)
    return req


class BoundaryCredentialTests(unittest.TestCase):
    """``manage_credential`` / ``remove_credential`` over a shared session.

    The store and the secure prompt are injected fakes, so the real Credential
    Manager, the native prompt and the per-user app-data directory are never
    touched. Every result is asserted to be secret-free.
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

    def _manage(self, prompt):
        self.session.credential_prompt = prompt
        return boundary.handle_request(
            _request(contract.ACTION_MANAGE_CREDENTIAL), self.session
        )

    def _remove(self):
        return boundary.handle_request(
            _request(contract.ACTION_REMOVE_CREDENTIAL), self.session
        )

    def test_manage_stores_secret_and_reports_presence(self):
        env = self._manage(lambda _message: _SECRET_LIKE)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "stored")
        self.assertTrue(env["result"]["credential_present"])
        self.assertTrue(self.store.has(credential_store.TARGET_NAME))

    def test_manage_returns_cancelled_when_prompt_is_none(self):
        env = self._manage(lambda _message: None)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "cancelled")
        self.assertFalse(env["result"]["credential_present"])

    def test_manage_returns_unavailable_without_a_native_prompt(self):
        # When no coherent native prompt exists the action must report
        # unavailable rather than fall back to insecure input. The platform
        # fallback is patched so this is deterministic on Windows too.
        self.session.credential_prompt = None
        with mock.patch.object(
            credential_store, "native_credential_prompt", return_value=None
        ):
            env = boundary.handle_request(
                _request(contract.ACTION_MANAGE_CREDENTIAL), self.session
            )
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "unavailable")
        self.assertFalse(env["result"]["credential_present"])

    def test_manage_returns_failed_on_prompt_error(self):
        def raise_prompt(_message):
            raise credential_store.CredentialStoreError("prompt_failed")

        env = self._manage(raise_prompt)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "failed")

    def test_manage_never_serializes_the_secret(self):
        env = self._manage(lambda _message: _SECRET_LIKE)
        serialized = contract.dumps(env)
        self.assertNotIn(_SECRET_LIKE, serialized)
        for token in ("secret", "token", "api_key", "password", "authorization", "bearer"):
            self.assertNotIn(token, serialized)

    def test_remove_deletes_and_reports_absence(self):
        self.store.store(credential_store.TARGET_NAME, "old-value")
        env = self._remove()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "removed")
        self.assertFalse(env["result"]["credential_present"])
        self.assertFalse(self.store.has(credential_store.TARGET_NAME))

    def test_remove_is_idempotent(self):
        env = self._remove()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "removed")
        self.assertFalse(env["result"]["credential_present"])

    def test_remove_returns_unavailable_without_a_store(self):
        self.session.credential_store = credential_store.UnavailableCredentialStore()
        env = self._remove()
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "unavailable")
        self.assertFalse(env["result"]["credential_present"])

    def test_manage_returns_unavailable_without_a_store(self):
        self.session.credential_store = credential_store.UnavailableCredentialStore()
        env = self._manage(lambda _message: _SECRET_LIKE)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "unavailable")

    def test_manage_reports_presence_without_materializing_the_key(self):
        # The handler must call ``store``/``has`` (presence) and never ``read``;
        # a store that fails on ``read`` still reports the credential as stored.
        class StoreOnlyStore(credential_store.FakeCredentialStore):
            def read(self, target):
                raise AssertionError("read() must not be called for manage")

        self.store = StoreOnlyStore()
        self.session.credential_store = self.store
        env = self._manage(lambda _message: _SECRET_LIKE)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "stored")
        self.assertTrue(env["result"]["credential_present"])


if __name__ == "__main__":
    unittest.main()
