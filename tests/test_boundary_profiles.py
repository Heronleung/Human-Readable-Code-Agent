"""Tests for the P4.2a credential-profile boundary actions and legacy migration."""

from __future__ import annotations

import tempfile
import unittest

from hrca import boundary, contract, credential_store

_SECRET_LIKE = "secret-token-abc123"
_PID = "a" * 32


def _request(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-profile",
        "action": action,
    }
    req.update(overrides)
    return req


class BoundaryProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_base = self._tmp.name
        self.store = credential_store.FakeCredentialStore()
        self.session = boundary.WorkspaceSession(
            store_base=self.store_base, credential_store=self.store
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(_request(action, **overrides), self.session)

    def _add(self, profile_id, name):
        self.store.store(credential_store.profile_target(profile_id), _SECRET_LIKE)
        return self._do(contract.ACTION_ADD_PROFILE, profile_id=profile_id, display_name=name)

    def test_get_profiles_empty(self):
        env = self._do(contract.ACTION_GET_PROFILES)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["profiles"], [])
        self.assertIsNone(result["active_profile_id"])
        self.assertEqual(result["state"], "missing_credential")
        self.assertFalse(result["credential_present"])

    def test_migration_creates_default_profile(self):
        self.store.store(credential_store.TARGET_NAME, _SECRET_LIKE)
        env = self._do(contract.ACTION_GET_PROFILES)
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertTrue(result["migrated"])
        self.assertEqual(len(result["profiles"]), 1)
        profile = result["profiles"][0]
        self.assertEqual(profile["display_name"], "Default")
        self.assertTrue(profile["credential_present"])
        self.assertEqual(result["active_profile_id"], profile["profile_id"])
        # Legacy entry removed only after the new target's presence was confirmed.
        self.assertFalse(self.store.has(credential_store.TARGET_NAME))
        self.assertTrue(
            self.store.has(credential_store.profile_target(profile["profile_id"]))
        )

    def test_migration_preserves_legacy_when_store_read_fails(self):
        class ReadFailsStore(credential_store.FakeCredentialStore):
            def read(self, target):
                raise credential_store.CredentialStoreError("store_failed")

        self.store = ReadFailsStore()
        self.store.store(credential_store.TARGET_NAME, _SECRET_LIKE)
        self.session.credential_store = self.store
        env = self._do(contract.ACTION_GET_PROFILES)
        result = env["result"]
        self.assertFalse(result["migrated"])
        self.assertEqual(result["profiles"], [])
        self.assertTrue(self.store.has(credential_store.TARGET_NAME))

    def test_migration_is_idempotent(self):
        self.store.store(credential_store.TARGET_NAME, _SECRET_LIKE)
        first = self._do(contract.ACTION_GET_PROFILES)["result"]
        second = self._do(contract.ACTION_GET_PROFILES)["result"]
        self.assertEqual(first["profiles"], second["profiles"])
        self.assertFalse(second["migrated"])

    def test_add_profile_persists_and_selects_first_active(self):
        env = self._add(_PID, "Work")
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(len(result["profiles"]), 1)
        self.assertEqual(result["active_profile_id"], _PID)
        self.assertTrue(result["credential_present"])

    def test_add_profile_rejects_missing_credential(self):
        env = self._do(contract.ACTION_ADD_PROFILE, profile_id=_PID, display_name="Work")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "profile_credential_missing")

    def test_add_profile_rejects_duplicate_name(self):
        self._add(_PID, "Work")
        pid2 = "b" * 32
        self.store.store(credential_store.profile_target(pid2), _SECRET_LIKE)
        env = self._do(contract.ACTION_ADD_PROFILE, profile_id=pid2, display_name="work")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "profile_name_invalid")

    def test_rename_profile_preserves_id_and_secret(self):
        target = credential_store.profile_target(_PID)
        self.store.store(target, _SECRET_LIKE)
        self._do(contract.ACTION_ADD_PROFILE, profile_id=_PID, display_name="Work")
        env = self._do(
            contract.ACTION_RENAME_PROFILE, profile_id=_PID, display_name="Home"
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["profiles"][0]["display_name"], "Home")
        self.assertEqual(result["profiles"][0]["profile_id"], _PID)
        # The secret is unchanged and still under the same opaque-id target.
        self.assertEqual(self.store.read(target), _SECRET_LIKE)

    def test_delete_profile_removes_credential_and_metadata(self):
        self._add(_PID, "Work")
        env = self._do(
            contract.ACTION_DELETE_PROFILE, profile_id=_PID, active_profile_id=None
        )
        self.assertTrue(env["ok"])
        result = env["result"]
        self.assertEqual(result["profiles"], [])
        self.assertIsNone(result["active_profile_id"])
        self.assertFalse(self.store.has(credential_store.profile_target(_PID)))

    def test_delete_active_with_explicit_fallback(self):
        pid2 = "b" * 32
        self._add(_PID, "A")
        self._add(pid2, "B")
        # _PID is active (first added); deleting it with an explicit fallback works.
        env = self._do(
            contract.ACTION_DELETE_PROFILE, profile_id=_PID, active_profile_id=pid2
        )
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["active_profile_id"], pid2)

    def test_delete_active_accepts_explicit_no_active(self):
        self._add(_PID, "A")
        env = self._do(
            contract.ACTION_DELETE_PROFILE, profile_id=_PID, active_profile_id=None
        )
        self.assertTrue(env["ok"])
        self.assertIsNone(env["result"]["active_profile_id"])

    def test_delete_rejects_invalid_fallback(self):
        pid2 = "b" * 32
        self._add(_PID, "A")
        self._add(pid2, "B")
        env = self._do(
            contract.ACTION_DELETE_PROFILE, profile_id=_PID, active_profile_id="c" * 32
        )
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "invalid_request")

    def test_delete_missing_profile_is_bounded(self):
        env = self._do(
            contract.ACTION_DELETE_PROFILE, profile_id=_PID, active_profile_id=None
        )
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "profile_not_found")

    def test_set_active_profile(self):
        pid2 = "b" * 32
        self._add(_PID, "A")
        self._add(pid2, "B")
        env = self._do(contract.ACTION_SET_ACTIVE_PROFILE, profile_id=pid2)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["active_profile_id"], pid2)
        self.assertTrue(env["result"]["credential_present"])

    def test_set_active_profile_rejects_missing(self):
        env = self._do(contract.ACTION_SET_ACTIVE_PROFILE, profile_id=_PID)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "profile_not_found")

    def test_profile_results_are_secret_free(self):
        self.store.store(credential_store.TARGET_NAME, _SECRET_LIKE)
        env = self._do(contract.ACTION_GET_PROFILES)
        serialized = contract.dumps(env)
        self.assertNotIn(_SECRET_LIKE, serialized)
        for token in ("secret", "api_key", "password", "authorization", "bearer"):
            self.assertNotIn(token, serialized)


if __name__ == "__main__":
    unittest.main()
