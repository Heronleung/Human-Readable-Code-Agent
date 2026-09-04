"""Tests for the fixed DeepSeek adapter identity and redacted readiness (P4.2a)."""

from __future__ import annotations

import json
import unittest

from hrca import deepseek


class DeepSeekIdentityTests(unittest.TestCase):
    def test_provider_id_is_fixed(self):
        self.assertEqual(deepseek.PROVIDER_ID, "deepseek")

    def test_origin_is_fixed_https(self):
        self.assertEqual(deepseek.API_ORIGIN, "https://api.deepseek.com")
        self.assertTrue(deepseek.API_ORIGIN.startswith("https://"))

    def test_auth_scheme_is_bearer(self):
        self.assertEqual(deepseek.AUTH_SCHEME, "bearer")
        self.assertEqual(deepseek.AUTH_PREFIX, "Bearer")

    def test_allowlist_contains_exactly_one_model(self):
        self.assertEqual(deepseek.ALLOWED_MODELS, frozenset({"deepseek-v4-flash"}))

    def test_default_model_is_allowlisted(self):
        self.assertIn(deepseek.DEFAULT_MODEL, deepseek.ALLOWED_MODELS)

    def test_is_allowed_model(self):
        self.assertTrue(deepseek.is_allowed_model("deepseek-v4-flash"))
        self.assertFalse(deepseek.is_allowed_model("deepseek-chat"))
        self.assertFalse(deepseek.is_allowed_model("https://evil.example"))
        self.assertFalse(deepseek.is_allowed_model(None))
        self.assertFalse(deepseek.is_allowed_model(42))


class ReadinessStateTests(unittest.TestCase):
    def test_states_are_bounded(self):
        self.assertEqual(
            deepseek.READY_STATES,
            {"configured", "missing_credential", "unavailable", "invalid_config"},
        )

    def test_state_precedence(self):
        ready = deepseek.readiness_state
        self.assertEqual(
            ready(config_error=None, credential_present=False, store_available=False),
            "unavailable",
        )
        self.assertEqual(
            ready(config_error="bad", credential_present=True, store_available=True),
            "invalid_config",
        )
        self.assertEqual(
            ready(config_error=None, credential_present=False, store_available=True),
            "missing_credential",
        )
        self.assertEqual(
            ready(config_error=None, credential_present=True, store_available=True),
            "configured",
        )


class RedactedReadinessTests(unittest.TestCase):
    def _ready(self, **overrides):
        kwargs = dict(
            config={
                "schema_version": "1.0.0",
                "provider_id": "deepseek",
                "model": "deepseek-v4-flash",
                "label": None,
            },
            config_error=None,
            credential_present=False,
            store_available=True,
        )
        kwargs.update(overrides)
        return deepseek.redacted_readiness(**kwargs)

    def test_result_shape_is_bounded(self):
        result = self._ready()
        self.assertEqual(
            set(result),
            {
                "state",
                "provider_id",
                "model",
                "credential_present",
                "authenticated",
                "online",
                "executable",
            },
        )

    def test_configured_never_claims_network(self):
        result = self._ready(credential_present=True)
        self.assertEqual(result["state"], "configured")
        self.assertEqual(result["model"], "deepseek-v4-flash")
        self.assertFalse(result["authenticated"])
        self.assertFalse(result["online"])
        self.assertFalse(result["executable"])

    def test_invalid_config_drops_model(self):
        result = self._ready(config_error="unsupported model")
        self.assertEqual(result["state"], "invalid_config")
        self.assertIsNone(result["model"])

    def test_non_allowlisted_model_is_dropped_defensively(self):
        result = self._ready(
            config={
                "schema_version": "1.0.0",
                "provider_id": "deepseek",
                "model": "deepseek-chat",
                "label": None,
            }
        )
        self.assertIsNone(result["model"])

    def test_readiness_never_contains_a_secret(self):
        # A secret-like value smuggled into a bogus config field must never
        # surface in the serialized result.
        result = self._ready(
            config={
                "schema_version": "1.0.0",
                "provider_id": "deepseek",
                "model": "deepseek-v4-flash",
                "label": None,
                "api_key": "secret-token-abc123",
            }
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("secret-token-abc123", serialized)
        for token in ("api_key", "secret", "token", "authorization", "bearer"):
            self.assertNotIn(token, serialized)


if __name__ == "__main__":
    unittest.main()
