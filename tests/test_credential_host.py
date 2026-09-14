"""Tests for the P4.2a dedicated native credential host.

The host owns the native dark entry sheet and the Credential Manager write for
one Add / Replace / Remove operation. These tests inject a fake store and a fake
sheet so the real Credential Manager, the native dialog and the per-user
app-data directory are never touched. Every result is asserted to be secret-free
and to carry only a bounded redacted state/reason (plus the non-secret profile
metadata on a successful add).
"""

from __future__ import annotations

import unittest
from unittest import mock

from hrca import contract, credential_host, credential_store

_SECRET_LIKE = "secret-token-abc123"


class FailingStore(credential_store.FakeCredentialStore):
    def store(self, target, secret):
        raise credential_store.CredentialStoreError("store_failed")


def _sheet_returning(value):
    """Build a fake sheet returning ``value`` (a tuple, ``None``, or a raising call)."""

    def sheet(replace_name=None, hwnd_parent=None, theme=None):
        if callable(value):
            return value()
        return value

    return sheet


def _sheet_raising(code):
    def sheet(replace_name=None, hwnd_parent=None, theme=None):
        raise credential_store.CredentialStoreError(code)

    return sheet


class CredentialHostRunTests(unittest.TestCase):
    def setUp(self):
        self.store = credential_store.FakeCredentialStore()

    def test_add_stores_and_returns_profile_metadata(self):
        result = credential_host.run(
            "enroll", store=self.store, sheet=_sheet_returning(("Work", _SECRET_LIKE))
        )
        self.assertEqual(result["state"], "stored")
        self.assertTrue(result["credential_present"])
        self.assertIn("profile_id", result)
        self.assertEqual(result["display_name"], "Work")
        self.assertTrue(
            contract.is_valid_profile_id(result["profile_id"])
        )
        self.assertTrue(
            self.store.has(credential_store.profile_target(result["profile_id"]))
        )

    def test_add_passes_no_replace_name_to_sheet(self):
        seen = []

        def sheet(replace_name=None, hwnd_parent=None, theme=None):
            seen.append((replace_name, hwnd_parent))
            return ("Work", _SECRET_LIKE)

        credential_host.run("enroll", hwnd_parent=123, store=self.store, sheet=sheet)
        self.assertEqual(seen, [(None, 123)])

    def test_run_passes_theme_to_sheet(self):
        seen = []

        def sheet(replace_name=None, hwnd_parent=None, theme=None):
            seen.append(theme)
            return ("Work", _SECRET_LIKE)

        credential_host.run("enroll", theme="light", store=self.store, sheet=sheet)
        self.assertEqual(seen, ["light"])

    def test_replace_stores_under_existing_target(self):
        profile_id = "a" * 32
        target = credential_store.profile_target(profile_id)
        self.store.store(target, "old-value")
        result = credential_host.run(
            "enroll",
            profile_id=profile_id,
            display_name="Work",
            store=self.store,
            sheet=_sheet_returning(("Work", _SECRET_LIKE)),
        )
        self.assertEqual(result["state"], "stored")
        self.assertNotIn("profile_id", result)
        self.assertNotIn("display_name", result)
        self.assertEqual(self.store.read(target), _SECRET_LIKE)

    def test_replace_passes_display_name_to_sheet(self):
        seen = []

        def sheet(replace_name=None, hwnd_parent=None, theme=None):
            seen.append(replace_name)
            return ("Work", _SECRET_LIKE)

        credential_host.run(
            "enroll",
            profile_id="a" * 32,
            display_name="Work",
            store=self.store,
            sheet=sheet,
        )
        self.assertEqual(seen, ["Work"])

    def test_cancel_is_reported(self):
        result = credential_host.run(
            "enroll", store=self.store, sheet=_sheet_returning(None)
        )
        self.assertEqual(result["state"], "cancelled")
        self.assertFalse(result["credential_present"])

    def test_replace_cancel_preserves_old_credential(self):
        profile_id = "a" * 32
        target = credential_store.profile_target(profile_id)
        self.store.store(target, "old-value")
        result = credential_host.run(
            "enroll", profile_id=profile_id, store=self.store, sheet=_sheet_returning(None)
        )
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["credential_present"])
        self.assertEqual(self.store.read(target), "old-value")

    def test_unavailable_without_store(self):
        result = credential_host.run(
            "enroll",
            store=credential_store.UnavailableCredentialStore(),
            sheet=_sheet_returning(("Work", _SECRET_LIKE)),
        )
        self.assertEqual(result["state"], "unavailable")

    def test_unavailable_without_sheet(self):
        # On a platform with no native sheet the host must report unavailable
        # rather than fall back to insecure input.
        with unittest.mock.patch.object(
            credential_store, "native_entry_sheet", return_value=None
        ):
            result = credential_host.run("enroll", store=self.store)
        self.assertEqual(result["state"], "unavailable")

    def test_maps_sheet_failure_reason(self):
        result = credential_host.run(
            "enroll", store=self.store, sheet=_sheet_raising("prompt_failed")
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "prompt_failed")

    def test_maps_store_failure_reason(self):
        result = credential_host.run(
            "enroll", store=FailingStore(), sheet=_sheet_returning(("Work", _SECRET_LIKE))
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "store_failed")

    def test_enroll_is_secret_free(self):
        result = credential_host.run(
            "enroll", store=self.store, sheet=_sheet_returning(("Work", _SECRET_LIKE))
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

    def test_delete_with_profile_id_removes_profile_target(self):
        profile_id = "a" * 32
        target = credential_store.profile_target(profile_id)
        self.store.store(target, "old")
        result = credential_host.run("delete", profile_id=profile_id, store=self.store)
        self.assertEqual(result["state"], "removed")
        self.assertFalse(self.store.has(target))

    def test_delete_is_idempotent(self):
        result = credential_host.run("delete", store=self.store)
        self.assertEqual(result["state"], "removed")


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

    def test_add_request_dispatches_to_sheet(self):
        envelope = credential_host.handle_request(
            self._manage_request(hwnd=999),
            store=self.store,
            sheet=_sheet_returning(("Work", _SECRET_LIKE)),
        )
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["result"]["state"], "stored")
        self.assertIn("profile_id", envelope["result"])
        self.assertEqual(envelope["correlation_id"], "cid-host")

    def test_replace_request_dispatches_with_profile_and_name(self):
        profile_id = "a" * 32
        envelope = credential_host.handle_request(
            self._manage_request(profile_id=profile_id, display_name="Work"),
            store=self.store,
            sheet=_sheet_returning(("Work", _SECRET_LIKE)),
        )
        self.assertTrue(envelope["ok"])
        self.assertTrue(
            self.store.has(credential_store.profile_target(profile_id))
        )

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
            {"contract_version": contract.CONTRACT_VERSION, "action": contract.ACTION_SCAN},
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

    def test_rejects_invalid_profile_id(self):
        envelope = credential_host.handle_request(
            self._manage_request(profile_id="not-a-profile-id"),
            store=self.store,
            sheet=_sheet_returning(("Work", _SECRET_LIKE)),
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_rejects_non_string_display_name(self):
        envelope = credential_host.handle_request(
            self._manage_request(profile_id="a" * 32, display_name=42),
            store=self.store,
            sheet=_sheet_returning(("Work", _SECRET_LIKE)),
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "invalid_request")

    def test_request_defaults_theme_to_dark(self):
        seen = []

        def sheet(replace_name=None, hwnd_parent=None, theme=None):
            seen.append(theme)
            return ("Work", _SECRET_LIKE)

        credential_host.handle_request(
            self._manage_request(), store=self.store, sheet=sheet
        )
        self.assertEqual(seen, ["dark"])

    def test_request_passes_theme(self):
        seen = []

        def sheet(replace_name=None, hwnd_parent=None, theme=None):
            seen.append(theme)
            return ("Work", _SECRET_LIKE)

        credential_host.handle_request(
            self._manage_request(theme="light"), store=self.store, sheet=sheet
        )
        self.assertEqual(seen, ["light"])

    def test_ignores_invalid_hwnd(self):
        seen = []

        def sheet(replace_name=None, hwnd_parent=None, theme=None):
            seen.append(hwnd_parent)
            return ("Work", _SECRET_LIKE)

        envelope = credential_host.handle_request(
            self._manage_request(hwnd="not-an-int"),
            store=self.store,
            sheet=sheet,
        )
        self.assertTrue(envelope["ok"])
        self.assertEqual(seen, [None])


if __name__ == "__main__":
    unittest.main()
