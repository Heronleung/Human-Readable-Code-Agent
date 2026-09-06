"""Tests for the P4.2a dedicated native credential host.

The host owns the secure native prompt and the Credential Manager write for a
single Manage / Remove operation. These tests inject a fake store and a fake
prompt so the real Credential Manager, the native dialog and the per-user
app-data directory are never touched. Every result is asserted to be secret-free
and to carry only a bounded redacted state/reason.
"""

from __future__ import annotations

import unittest
from unittest import mock

from hrca import contract, credential_host, credential_store

_SECRET_LIKE = "secret-token-abc123"


class FailingStore(credential_store.FakeCredentialStore):
    def store(self, target, secret):
        raise credential_store.CredentialStoreError("store_failed")


def _prompt_returning(value):
    def prompt(_message, hwnd_parent=None):
        return value

    return prompt


def _prompt_raising(code):
    def prompt(_message, hwnd_parent=None):
        raise credential_store.CredentialStoreError(code)

    return prompt


class CredentialHostRunTests(unittest.TestCase):
    def setUp(self):
        self.store = credential_store.FakeCredentialStore()

    def test_enroll_stores_and_reports_presence(self):
        result = credential_host.run(
            "enroll", store=self.store, prompt=_prompt_returning(_SECRET_LIKE)
        )
        self.assertEqual(result["state"], "stored")
        self.assertTrue(result["credential_present"])
        self.assertTrue(self.store.has(credential_store.TARGET_NAME))

    def test_enroll_passes_the_parent_handle_to_the_prompt(self):
        seen = []

        def prompt(_message, hwnd_parent=None):
            seen.append(hwnd_parent)
            return _SECRET_LIKE

        credential_host.run("enroll", hwnd_parent=12345, store=self.store, prompt=prompt)
        self.assertEqual(seen, [12345])

    def test_enroll_passes_no_parent_when_absent(self):
        seen = []

        def prompt(_message, hwnd_parent=None):
            seen.append(hwnd_parent)
            return _SECRET_LIKE

        credential_host.run("enroll", store=self.store, prompt=prompt)
        self.assertEqual(seen, [None])

    def test_enroll_cancel_is_reported(self):
        result = credential_host.run(
            "enroll", store=self.store, prompt=_prompt_returning(None)
        )
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(result["credential_present"])

    def test_enroll_unavailable_without_store(self):
        result = credential_host.run(
            "enroll",
            store=credential_store.UnavailableCredentialStore(),
            prompt=_prompt_returning(_SECRET_LIKE),
        )
        self.assertEqual(result["state"], "unavailable")

    def test_enroll_unavailable_without_prompt(self):
        # On a platform with no coherent native prompt the host must report
        # unavailable rather than fall back to insecure input. The platform
        # fallback is patched so this is deterministic on Windows too.
        with mock.patch.object(
            credential_store, "native_credential_prompt", return_value=None
        ):
            result = credential_host.run("enroll", store=self.store, prompt=None)
        self.assertEqual(result["state"], "unavailable")

    def test_enroll_maps_prompt_failure_reason(self):
        result = credential_host.run(
            "enroll", store=self.store, prompt=_prompt_raising("prompt_failed")
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "prompt_failed")

    def test_enroll_maps_session_unavailable_reason(self):
        result = credential_host.run(
            "enroll",
            store=self.store,
            prompt=_prompt_raising("prompt_session_unavailable"),
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "prompt_session_unavailable")

    def test_enroll_maps_store_failure_reason(self):
        result = credential_host.run(
            "enroll", store=FailingStore(), prompt=_prompt_returning(_SECRET_LIKE)
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "store_failed")

    def test_enroll_is_secret_free(self):
        result = credential_host.run(
            "enroll", store=self.store, prompt=_prompt_returning(_SECRET_LIKE)
        )
        serialized = contract.dumps(result)
        self.assertNotIn(_SECRET_LIKE, serialized)
        for token in ("secret", "token", "api_key", "password", "authorization", "bearer"):
            self.assertNotIn(token, serialized)

    def test_delete_removes_and_reports_absence(self):
        self.store.store(credential_store.TARGET_NAME, "old-value")
        result = credential_host.run("delete", store=self.store)
        self.assertEqual(result["state"], "removed")
        self.assertFalse(result["credential_present"])
        self.assertFalse(self.store.has(credential_store.TARGET_NAME))

    def test_delete_is_idempotent(self):
        result = credential_host.run("delete", store=self.store)
        self.assertEqual(result["state"], "removed")

    def test_delete_unavailable_without_store(self):
        result = credential_host.run(
            "delete", store=credential_store.UnavailableCredentialStore()
        )
        self.assertEqual(result["state"], "unavailable")


class CredentialHostRequestTests(unittest.TestCase):
    def setUp(self):
        self.store = credential_store.FakeCredentialStore()

    def _manage_request(self, **overrides):
        req = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "cid-host",
            "action": contract.ACTION_MANAGE_CREDENTIAL,
        }
        req.update(overrides)
        return req

    def test_manage_request_dispatches_to_enroll(self):
        envelope = credential_host.handle_request(
            self._manage_request(hwnd=999),
            store=self.store,
            prompt=_prompt_returning(_SECRET_LIKE),
        )
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"]["state"], "stored")
        self.assertEqual(envelope["correlation_id"], "cid-host")

    def test_remove_request_dispatches_to_delete(self):
        req = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "cid-host",
            "action": contract.ACTION_REMOVE_CREDENTIAL,
        }
        envelope = credential_host.handle_request(req, store=self.store)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"]["state"], "removed")

    def test_rejects_non_credential_action(self):
        envelope = credential_host.handle_request(
            {
                "contract_version": contract.CONTRACT_VERSION,
                "action": contract.ACTION_SCAN,
            },
            store=self.store,
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "action_not_allowed")

    def test_rejects_unknown_contract_version(self):
        envelope = credential_host.handle_request(
            {
                "contract_version": "0.0.0",
                "action": contract.ACTION_MANAGE_CREDENTIAL,
            },
            store=self.store,
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "unknown_contract_version")

    def test_rejects_non_dict(self):
        envelope = credential_host.handle_request("not-a-dict", store=self.store)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_ignores_invalid_hwnd(self):
        # A malformed (non-int) hwnd is treated as absent, never passed through.
        seen = []

        def prompt(_message, hwnd_parent=None):
            seen.append(hwnd_parent)
            return _SECRET_LIKE

        envelope = credential_host.handle_request(
            self._manage_request(hwnd="not-an-int"),
            store=self.store,
            prompt=prompt,
        )
        self.assertTrue(envelope["ok"])
        self.assertEqual(seen, [None])


class CredentialHostProfileTests(unittest.TestCase):
    """The host stores/removes a secret under a profile-derived target (P4.2a)."""

    def setUp(self):
        self.store = credential_store.FakeCredentialStore()

    def test_enroll_with_profile_id_uses_profile_target(self):
        profile_id = "a" * 32
        result = credential_host.run(
            "enroll",
            profile_id=profile_id,
            store=self.store,
            prompt=_prompt_returning(_SECRET_LIKE),
        )
        self.assertEqual(result["state"], "stored")
        self.assertTrue(self.store.has(credential_store.profile_target(profile_id)))
        self.assertFalse(self.store.has(credential_store.TARGET_NAME))

    def test_delete_with_profile_id_removes_profile_target(self):
        profile_id = "a" * 32
        target = credential_store.profile_target(profile_id)
        self.store.store(target, "old")
        result = credential_host.run("delete", profile_id=profile_id, store=self.store)
        self.assertEqual(result["state"], "removed")
        self.assertFalse(self.store.has(target))

    def test_request_with_profile_id_dispatches_to_profile_target(self):
        profile_id = "a" * 32
        envelope = credential_host.handle_request(
            {
                "contract_version": contract.CONTRACT_VERSION,
                "correlation_id": "cid-host",
                "action": contract.ACTION_MANAGE_CREDENTIAL,
                "profile_id": profile_id,
            },
            store=self.store,
            prompt=_prompt_returning(_SECRET_LIKE),
        )
        self.assertTrue(envelope["ok"])
        self.assertTrue(self.store.has(credential_store.profile_target(profile_id)))

    def test_request_rejects_invalid_profile_id(self):
        envelope = credential_host.handle_request(
            {
                "contract_version": contract.CONTRACT_VERSION,
                "correlation_id": "cid-host",
                "action": contract.ACTION_MANAGE_CREDENTIAL,
                "profile_id": "not-a-profile-id",
            },
            store=self.store,
            prompt=_prompt_returning(_SECRET_LIKE),
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_profile_result_is_secret_free(self):
        profile_id = "a" * 32
        envelope = credential_host.handle_request(
            {
                "contract_version": contract.CONTRACT_VERSION,
                "correlation_id": "cid-host",
                "action": contract.ACTION_MANAGE_CREDENTIAL,
                "profile_id": profile_id,
            },
            store=self.store,
            prompt=_prompt_returning(_SECRET_LIKE),
        )
        self.assertNotIn(_SECRET_LIKE, contract.dumps(envelope))


if __name__ == "__main__":
    unittest.main()
