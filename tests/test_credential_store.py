"""Tests for the credential-store port (P4.2a)."""

from __future__ import annotations

import json
import os
import unittest

from hrca.credential_store import (
    TARGET_NAME,
    CredentialStoreError,
    FakeCredentialStore,
    UnavailableCredentialStore,
    make_credential_store,
)


class FakeCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeCredentialStore()

    def test_store_and_read_round_trip(self):
        self.store.store(TARGET_NAME, "test-value-xyz")
        self.assertTrue(self.store.has(TARGET_NAME))
        self.assertEqual(self.store.read(TARGET_NAME), "test-value-xyz")

    def test_store_replaces(self):
        self.store.store(TARGET_NAME, "first")
        self.store.store(TARGET_NAME, "second")
        self.assertEqual(self.store.read(TARGET_NAME), "second")

    def test_delete_is_idempotent(self):
        self.store.store(TARGET_NAME, "first")
        self.store.delete(TARGET_NAME)
        self.assertFalse(self.store.has(TARGET_NAME))
        self.assertIsNone(self.store.read(TARGET_NAME))
        self.store.delete(TARGET_NAME)  # no error on an absent target
        self.assertFalse(self.store.has(TARGET_NAME))

    def test_missing_read_returns_none(self):
        self.assertIsNone(self.store.read(TARGET_NAME))

    def test_missing_has_is_false(self):
        self.assertFalse(self.store.has(TARGET_NAME))

    def test_rejects_invalid_target(self):
        for bad in ("", None, "x" * 300, "bad\ntarget"):
            with self.subTest(bad=bad):
                with self.assertRaises(CredentialStoreError) as ctx:
                    self.store.store(bad, "value")
                self.assertEqual(ctx.exception.code, "invalid_target")

    def test_rejects_empty_secret(self):
        with self.assertRaises(CredentialStoreError) as ctx:
            self.store.store(TARGET_NAME, "")
        self.assertEqual(ctx.exception.code, "invalid_secret")


class UnavailableCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = UnavailableCredentialStore()

    def test_not_available(self):
        self.assertFalse(self.store.available())

    def test_has_is_false(self):
        self.assertFalse(self.store.has(TARGET_NAME))

    def test_mutations_raise_unavailable(self):
        for call in (
            lambda: self.store.store(TARGET_NAME, "value"),
            lambda: self.store.delete(TARGET_NAME),
            lambda: self.store.read(TARGET_NAME),
        ):
            with self.assertRaises(CredentialStoreError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "unavailable")


class CredentialStoreErrorTests(unittest.TestCase):
    def test_messages_are_bounded(self):
        self.assertEqual(
            CredentialStoreError("store_failed").message,
            "the credential store operation failed",
        )
        self.assertEqual(
            CredentialStoreError("unavailable").message,
            "credential storage is unavailable on this platform",
        )

    def test_rejects_unknown_code(self):
        with self.assertRaises(ValueError):
            CredentialStoreError("not-a-real-code")

    def test_to_dict_is_bounded(self):
        err = CredentialStoreError("store_failed")
        self.assertEqual(
            err.to_dict(),
            {"code": "store_failed", "message": "the credential store operation failed"},
        )
        self.assertNotIn("secret", json.dumps(err.to_dict()))


class MakeCredentialStoreTests(unittest.TestCase):
    def test_matches_platform(self):
        store = make_credential_store()
        if os.name == "nt":
            from hrca.credential_store_win import WindowsCredentialStore

            self.assertIsInstance(store, WindowsCredentialStore)
        else:
            self.assertIsInstance(store, UnavailableCredentialStore)


if __name__ == "__main__":
    unittest.main()
