"""Tests for the non-secret DeepSeek credential-profile configuration (P4.2a)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import provider_config


def _profile_id() -> str:
    return "a" * 32


class ProviderConfigValidationTests(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfg = provider_config.default_config()
        self.assertIsNone(provider_config.validate_config(cfg))
        self.assertEqual(cfg["schema_version"], provider_config.CONFIG_SCHEMA_VERSION)
        self.assertEqual(cfg["provider_id"], "deepseek")
        self.assertEqual(cfg["model"], "deepseek-flash")
        self.assertEqual(cfg["profiles"], [])
        self.assertIsNone(cfg["active_profile_id"])

    def test_rejects_non_mapping(self):
        self.assertIsNotNone(provider_config.validate_config("not-a-mapping"))
        self.assertIsNotNone(provider_config.validate_config(None))

    def test_rejects_unknown_field(self):
        cfg = provider_config.default_config()
        cfg["endpoint"] = "https://evil.example"
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_unknown_profile_field(self):
        cfg = provider_config.default_config()
        cfg["profiles"] = [{"profile_id": _profile_id(), "secret": "x"}]
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

    def test_rejects_invalid_profile_id(self):
        cfg = provider_config.default_config()
        cfg["profiles"] = [
            {"profile_id": "not-hex", "provider_id": "deepseek", "display_name": "x"}
        ]
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_duplicate_profile_id(self):
        cfg = provider_config.default_config()
        cfg["profiles"] = [
            {"profile_id": _profile_id(), "provider_id": "deepseek", "display_name": "A"},
            {"profile_id": _profile_id(), "provider_id": "deepseek", "display_name": "B"},
        ]
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_duplicate_display_name(self):
        cfg = provider_config.default_config()
        cfg["profiles"] = [
            {"profile_id": _profile_id(), "provider_id": "deepseek", "display_name": "A"},
            {"profile_id": "b" * 32, "provider_id": "deepseek", "display_name": "a"},
        ]
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_long_display_name(self):
        cfg = provider_config.default_config()
        cfg["profiles"] = [
            {
                "profile_id": _profile_id(),
                "provider_id": "deepseek",
                "display_name": "x" * 65,
            }
        ]
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_rejects_active_profile_not_in_profiles(self):
        cfg = provider_config.default_config()
        cfg["active_profile_id"] = _profile_id()
        self.assertIsNotNone(provider_config.validate_config(cfg))

    def test_accepts_valid_profile_and_active(self):
        cfg = provider_config.default_config()
        cfg["profiles"] = [
            {"profile_id": _profile_id(), "provider_id": "deepseek", "display_name": "Work"}
        ]
        cfg["active_profile_id"] = _profile_id()
        self.assertIsNone(provider_config.validate_config(cfg))

    def test_config_is_secret_free(self):
        # A secret-like field is rejected outright; the config never stores key
        # material, a length, prefix, suffix, hash or reversible derivative.
        cfg = provider_config.default_config()
        cfg["profiles"] = [
            {
                "profile_id": _profile_id(),
                "provider_id": "deepseek",
                "display_name": "x",
                "api_key": "secret-token-abc123",
            }
        ]
        self.assertIsNotNone(provider_config.validate_config(cfg))


class ProfileHelperTests(unittest.TestCase):
    def test_add_profile_appends_and_does_not_mutate(self):
        cfg = provider_config.default_config()
        out, err = provider_config.add_profile(cfg, _profile_id(), "Work")
        self.assertIsNone(err)
        self.assertEqual(len(out["profiles"]), 1)
        self.assertEqual(out["profiles"][0]["display_name"], "Work")
        self.assertEqual(len(cfg["profiles"]), 0)  # input untouched

    def test_add_profile_rejects_duplicate_name(self):
        cfg = provider_config.default_config()
        cfg, _ = provider_config.add_profile(cfg, _profile_id(), "Work")
        out, err = provider_config.add_profile(cfg, "b" * 32, "work")
        self.assertIsNotNone(err)
        self.assertIsNone(out)

    def test_add_profile_rejects_invalid_name(self):
        cfg = provider_config.default_config()
        out, err = provider_config.add_profile(cfg, _profile_id(), "   ")
        self.assertIsNotNone(err)

    def test_rename_preserves_id(self):
        cfg = provider_config.default_config()
        cfg, _ = provider_config.add_profile(cfg, _profile_id(), "Old")
        out, err = provider_config.rename_profile(cfg, _profile_id(), "New")
        self.assertIsNone(err)
        self.assertEqual(out["profiles"][0]["profile_id"], _profile_id())
        self.assertEqual(out["profiles"][0]["display_name"], "New")

    def test_rename_missing_profile_errors(self):
        cfg = provider_config.default_config()
        out, err = provider_config.rename_profile(cfg, _profile_id(), "New")
        self.assertEqual(err, "profile not found")

    def test_remove_profile_sets_fallback_active(self):
        cfg = provider_config.default_config()
        cfg, _ = provider_config.add_profile(cfg, _profile_id(), "A")
        cfg, _ = provider_config.add_profile(cfg, "b" * 32, "B")
        cfg, _ = provider_config.set_active_profile(cfg, _profile_id())
        out, err = provider_config.remove_profile(cfg, _profile_id(), "b" * 32)
        self.assertIsNone(err)
        self.assertEqual(out["active_profile_id"], "b" * 32)
        self.assertEqual(len(out["profiles"]), 1)

    def test_remove_profile_requires_valid_fallback(self):
        cfg = provider_config.default_config()
        cfg, _ = provider_config.add_profile(cfg, _profile_id(), "A")
        out, err = provider_config.remove_profile(cfg, _profile_id(), "b" * 32)
        self.assertIsNotNone(err)

    def test_set_active_profile_requires_existing_id(self):
        cfg = provider_config.default_config()
        out, err = provider_config.set_active_profile(cfg, _profile_id())
        self.assertEqual(err, "profile not found")

    def test_set_active_profile_accepts_none(self):
        cfg = provider_config.default_config()
        cfg, _ = provider_config.add_profile(cfg, _profile_id(), "A")
        out, err = provider_config.set_active_profile(cfg, None)
        self.assertIsNone(err)
        self.assertIsNone(out["active_profile_id"])


class MigrationTests(unittest.TestCase):
    def test_migrate_structure_v1_to_v2(self):
        raw = {
            "schema_version": "1.0.0",
            "provider_id": "deepseek",
            "model": "deepseek-v4-flash",
            "label": "personal",
        }
        migrated = provider_config.migrate_structure(raw)
        self.assertEqual(migrated["schema_version"], provider_config.CONFIG_SCHEMA_VERSION)
        self.assertEqual(migrated["profiles"], [])
        self.assertIsNone(migrated["active_profile_id"])
        self.assertNotIn("label", migrated)

    def test_migrate_structure_keeps_v2(self):
        cfg = provider_config.default_config()
        self.assertIs(provider_config.migrate_structure(cfg), cfg)

    def test_migrate_structure_rejects_unknown(self):
        self.assertIsNone(provider_config.migrate_structure({"schema_version": "999.0.0"}))
        self.assertIsNone(provider_config.migrate_structure("not-a-dict"))

    def test_migrate_model_maps_alias_to_canonical_preserving_profiles(self):
        raw = {
            "schema_version": provider_config.CONFIG_SCHEMA_VERSION,
            "provider_id": "deepseek",
            "model": "deepseek-v4-flash",
            "profiles": [
                {"profile_id": _profile_id(), "provider_id": "deepseek",
                 "display_name": "Work"}
            ],
            "active_profile_id": _profile_id(),
        }
        migrated = provider_config.migrate_model(raw)
        self.assertEqual(migrated["model"], "deepseek-flash")
        # Profile/credential identity is preserved untouched.
        self.assertEqual(migrated["profiles"], raw["profiles"])
        self.assertEqual(migrated["active_profile_id"], raw["active_profile_id"])

    def test_migrate_model_is_idempotent(self):
        canonical = provider_config.default_config()
        self.assertIs(provider_config.migrate_model(canonical), canonical)
        unknown = provider_config.default_config()
        unknown["model"] = "deepseek-chat"
        self.assertIs(provider_config.migrate_model(unknown), unknown)

    def test_load_migrates_alias_model(self):
        # An on-disk v2 config predating the migration still carries the retired
        # alias; load must migrate it to canonical while preserving profiles.
        raw = {
            "schema_version": provider_config.CONFIG_SCHEMA_VERSION,
            "provider_id": "deepseek",
            "model": "deepseek-v4-flash",
            "profiles": [
                {"profile_id": _profile_id(), "provider_id": "deepseek",
                 "display_name": "dev"}
            ],
            "active_profile_id": _profile_id(),
        }
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(base, exist_ok=True)
            with open(provider_config.config_path(base), "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
            loaded, err = provider_config.load(base)
            self.assertIsNone(err)
            self.assertEqual(loaded["model"], "deepseek-flash")
            self.assertEqual(len(loaded["profiles"]), 1)
            self.assertEqual(loaded["active_profile_id"], _profile_id())


class ProviderConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _with_profile(self):
        cfg = provider_config.default_config()
        cfg, _ = provider_config.add_profile(cfg, _profile_id(), "dev")
        cfg, _ = provider_config.set_active_profile(cfg, _profile_id())
        return cfg

    def test_save_and_load_round_trip(self):
        cfg = self._with_profile()
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

    def test_load_v1_is_migrated_to_v2(self):
        os.makedirs(self.base, exist_ok=True)
        with open(provider_config.config_path(self.base), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "schema_version": "1.0.0",
                    "provider_id": "deepseek",
                    "model": "deepseek-v4-flash",
                    "label": "personal",
                },
                fh,
            )
        loaded, err = provider_config.load(self.base)
        self.assertIsNone(err)
        self.assertEqual(loaded["schema_version"], provider_config.CONFIG_SCHEMA_VERSION)
        self.assertEqual(loaded["profiles"], [])

    def test_serialized_config_is_secret_free(self):
        cfg = self._with_profile()
        provider_config.save(self.base, cfg)
        with open(provider_config.config_path(self.base), "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIsNone(provider_config.validate_config(json.loads(text)))
        for token in ("secret", "token", "api_key", "password", "authorization", "bearer"):
            self.assertNotIn(token, text)

    def test_config_file_lives_under_the_app_data_dir(self):
        cfg = self._with_profile()
        provider_config.save(self.base, cfg)
        path = provider_config.config_path(self.base)
        self.assertTrue(path.startswith(self.base))
        self.assertNotIn(".git", path)
        self.assertEqual(os.path.basename(path), provider_config.CONFIG_FILENAME)


class ActiveCredentialTargetTests(unittest.TestCase):
    """``active_credential_target`` resolves the same opaque target the provider
    action uses — never the editable display name, never the working directory."""

    _PID = "a" * 32

    def _config(self, *, profiles=(), active=None):
        return {
            "schema_version": provider_config.CONFIG_SCHEMA_VERSION,
            "provider_id": "deepseek",
            "model": "deepseek-flash",
            "profiles": [
                {
                    "profile_id": pid,
                    "provider_id": "deepseek",
                    "display_name": name,
                }
                for pid, name in profiles
            ],
            "active_profile_id": active,
        }

    def test_active_profile_target_is_profile_target(self):
        from hrca import credential_store

        cfg = self._config(profiles=[(self._PID, "Work")], active=self._PID)
        self.assertEqual(
            provider_config.active_credential_target(cfg),
            credential_store.profile_target(self._PID),
        )

    def test_legacy_target_when_no_profiles(self):
        from hrca import credential_store

        self.assertEqual(
            provider_config.active_credential_target(self._config()),
            credential_store.TARGET_NAME,
        )

    def test_no_target_when_profiles_but_none_active(self):
        cfg = self._config(profiles=[(self._PID, "Work")], active=None)
        self.assertIsNone(provider_config.active_credential_target(cfg))

    def test_no_target_when_config_is_none(self):
        self.assertIsNone(provider_config.active_credential_target(None))

    def test_target_is_independent_of_display_name(self):
        from hrca import credential_store

        before = self._config(profiles=[(self._PID, "Work")], active=self._PID)
        after = self._config(profiles=[(self._PID, "Renamed")], active=self._PID)
        self.assertEqual(
            provider_config.active_credential_target(before),
            provider_config.active_credential_target(after),
        )
        self.assertEqual(
            provider_config.active_credential_target(before),
            credential_store.profile_target(self._PID),
        )

    def test_target_never_contains_the_display_name(self):
        target = provider_config.active_credential_target(
            self._config(profiles=[(self._PID, "Top Secret Name")], active=self._PID)
        )
        self.assertNotIn("Top Secret Name", target)
        self.assertIn(self._PID, target)


if __name__ == "__main__":
    unittest.main()
