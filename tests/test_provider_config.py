"""Tests for the non-secret DeepSeek configuration (P4.2a)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import provider_config


class ProviderConfigValidationTests(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfg = provider_config.default_config()
        self.assertIsNone(provider_config.validate_config(cfg))
        self.assertEqual(cfg["schema_version"], provider_config.CONFIG_SCHEMA_VERSION)
        self.assertEqual(cfg["provider_id"], "deepseek")
        self.assertEqual(cfg["model"], "deepseek-v4-flash")
        self.assertIsNone(cfg["label"])

    def test_rejects_non_mapping(self):
        self.assertIsNotNone(provider_config.validate_config("not-a-mapping"))
        self.assertIsNotNone(provider_config.validate_config(None))

    def test_rejects_unknown_field(self):
        cfg = provider_config.default_config()
        cfg["endpoint"] = "https://evil.example"
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_wrong_schema_version(self):
        cfg = provider_config.default_config()
        cfg["schema_version"] = "0.0.0"
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_wrong_provider(self):
        cfg = provider_config.default_config()
        cfg["provider_id"] = "other"
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_non_allowlisted_model(self):
        cfg = provider_config.default_config()
        cfg["model"] = "deepseek-chat"
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_long_label(self):
        cfg = provider_config.default_config()
        cfg["label"] = "x" * 201
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_accepts_short_label(self):
        cfg = provider_config.default_config()
        cfg["label"] = "personal"
        self.assertIsNone(provider_config.validate_config(cfg))


class ProviderConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_and_load_round_trip(self):
        cfg = provider_config.default_config()
        cfg["label"] = "dev"
        self.assertIsNone(provider_config.save(self.base, cfg))
        loaded, err = provider_config.load(self.base)
        self.assertIsNone(err)
        self.assertEqual(loaded, cfg)

    def test_load_absent_returns_none(self):
        loaded, err = provider_config.load(self.base)
        self.assertIsNone(loaded)
        self.assertIsNone(err)

    def test_save_rejects_invalid(self):
        cfg = provider_config.default_config()
        cfg["model"] = "deepseek-chat"
        self.assertIsNotNone(provider_config.save(self.base, cfg))
        self.assertFalse(os.path.exists(provider_config.config_path(self.base)))

    def test_load_malformed_json_is_fail_closed(self):
        os.makedirs(self.base, exist_ok=True)
        with open(provider_config.config_path(self.base), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        loaded, err = provider_config.load(self.base)
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)

    def test_load_future_schema_is_fail_closed(self):
        os.makedirs(self.base, exist_ok=True)
        with open(provider_config.config_path(self.base), "w", encoding="utf-8") as fh:
            json.dump({"schema_version": "999.0.0"}, fh)
        loaded, err = provider_config.load(self.base)
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)

    def test_serialized_config_is_secret_free(self):
        cfg = provider_config.default_config()
        cfg["label"] = "personal"
        provider_config.save(self.base, cfg)
        with open(provider_config.config_path(self.base), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIsNone(provider_config.validate_config(json.loads(text)))
        for token in ("secret", "token", "api_key", "password", "authorization", "bearer"):
            self.assertNotIn(token, text)

    def test_config_file_lives_under_the_app_data_dir(self):
        cfg = provider_config.default_config()
        provider_config.save(self.base, cfg)
        path = provider_config.config_path(self.base)
        self.assertTrue(path.startswith(self.base))
        self.assertNotIn(".git", path)
        self.assertEqual(os.path.basename(path), provider_config.CONFIG_FILENAME)


if __name__ == "__main__":
    unittest.main()
