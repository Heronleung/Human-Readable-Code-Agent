"""Tests for the backend-owned credential configuration CLI (P4.2a)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from hrca import credential_store, provider_cli

_SECRET_LIKE = "secret-token-abc123"


class ProviderCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.store = credential_store.FakeCredentialStore()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, subcommand, prompt=None, store=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = provider_cli.run(
                subcommand,
                store=store if store is not None else self.store,
                base_dir=self.base,
                prompt=prompt,
            )
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_enroll_stores_and_prints_no_secret(self):
        rc, out, err = self._run("enroll", prompt=lambda _p: _SECRET_LIKE)
        self.assertEqual(rc, 0)
        self.assertIn("credential stored", out)
        self.assertNotIn(_SECRET_LIKE, out)
        self.assertNotIn(_SECRET_LIKE, err)
        self.assertEqual(self.store.read(credential_store.TARGET_NAME), _SECRET_LIKE)

    def test_enroll_replaces_existing(self):
        self.store.store(credential_store.TARGET_NAME, "old-value")
        rc, _, _ = self._run("enroll", prompt=lambda _p: "new-value")
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.read(credential_store.TARGET_NAME), "new-value")

    def test_enroll_empty_prompt_fails(self):
        rc, out, err = self._run("enroll", prompt=lambda _p: "")
        self.assertEqual(rc, 1)
        self.assertIn("no key provided", err)
        self.assertNotIn(_SECRET_LIKE, err)

    def test_enroll_unavailable_store_fails(self):
        rc, out, err = self._run(
            "enroll",
            prompt=lambda _p: "value",
            store=credential_store.UnavailableCredentialStore(),
        )
        self.assertEqual(rc, 1)
        self.assertIn("unavailable", err)

    def test_delete_removes_and_is_idempotent(self):
        self.store.store(credential_store.TARGET_NAME, "value")
        rc, out, err = self._run("delete")
        self.assertEqual(rc, 0)
        self.assertIn("credential deleted", out)
        self.assertFalse(self.store.has(credential_store.TARGET_NAME))
        rc, _, _ = self._run("delete")
        self.assertEqual(rc, 0)

    def test_delete_unavailable_store_fails(self):
        rc, _, err = self._run(
            "delete", store=credential_store.UnavailableCredentialStore()
        )
        self.assertEqual(rc, 1)
        self.assertIn("unavailable", err)

    def test_readiness_missing_credential(self):
        rc, out, err = self._run("readiness")
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(result["state"], "missing_credential")
        self.assertEqual(result["provider_id"], "deepseek")
        self.assertFalse(result["credential_present"])
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["online"])

    def test_readiness_configured_and_secret_free(self):
        self.store.store(credential_store.TARGET_NAME, _SECRET_LIKE)
        rc, out, err = self._run("readiness")
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertEqual(result["state"], "configured")
        self.assertTrue(result["credential_present"])
        self.assertNotIn(_SECRET_LIKE, out)
        self.assertNotIn(_SECRET_LIKE, err)

    def test_unknown_subcommand_prints_usage(self):
        rc, out, err = self._run("bogus")
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)


if __name__ == "__main__":
    unittest.main()
