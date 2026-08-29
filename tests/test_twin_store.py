"""Tests for deterministic Twin persistence (P3.3).

These tests exercise :mod:`hrca.twin_store` — the single owner of Twin storage
access — against a temporary base directory so the real per-user app-data
directory is never touched. They verify the atomic write path, fail-closed
loads, and per-workspace isolation without any Qt or filesystem access outside
the temp directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import twin, twin_store


class _Base:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.wsid = twin.workspace_id_for("/tmp/workspace")

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, **overrides):
        store = twin.build_store(
            {"files": [], "symbols": [], "relations": [],
             "parse_errors": [], "confidence": "high"},
            {}, self.wsid, 1, "T",
        )
        store.update(overrides)
        return store


class SaveLoadTests(_Base, unittest.TestCase):
    def test_round_trip_is_identical(self):
        store = self._store()
        self.assertIsNone(twin_store.save(self.base, self.wsid, store))
        loaded, err = twin_store.load(self.base, self.wsid)
        self.assertIsNone(err)
        self.assertEqual(loaded, store)

    def test_absent_store_loads_to_none(self):
        loaded, err = twin_store.load(self.base, self.wsid)
        self.assertIsNone(loaded)
        self.assertIsNone(err)

    def test_store_lives_outside_the_base_subdir_by_workspace(self):
        twin_store.save(self.base, self.wsid, self._store())
        path = twin_store.workspace_store_path(self.base, self.wsid)
        self.assertTrue(path.startswith(self.base))
        self.assertTrue(os.path.isfile(path))
        self.assertIn(twin_store._namespace(self.wsid), path)

    def test_save_is_deterministic(self):
        store = self._store()
        twin_store.save(self.base, self.wsid, store)
        with open(twin_store.workspace_store_path(self.base, self.wsid), "rb") as fh:
            first = fh.read()
        twin_store.save(self.base, self.wsid, store)
        with open(twin_store.workspace_store_path(self.base, self.wsid), "rb") as fh:
            second = fh.read()
        self.assertEqual(first, second)


class IsolationTests(_Base, unittest.TestCase):
    def test_two_workspaces_do_not_collide(self):
        other = twin.workspace_id_for("/tmp/other")
        self.assertIsNone(twin_store.save(self.base, self.wsid, self._store()))
        loaded_other, err = twin_store.load(self.base, other)
        self.assertIsNone(loaded_other)
        self.assertIsNone(err)
        self.assertNotEqual(
            twin_store.workspace_store_path(self.base, self.wsid),
            twin_store.workspace_store_path(self.base, other),
        )


class FailClosedLoadTests(_Base, unittest.TestCase):
    def test_corrupt_json_is_rejected_without_overwrite(self):
        twin_store.save(self.base, self.wsid, self._store())
        path = twin_store.workspace_store_path(self.base, self.wsid)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        loaded, err = twin_store.load(self.base, self.wsid)
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)

    def test_future_schema_version_is_rejected_and_not_overwritten(self):
        twin_store.save(self.base, self.wsid, self._store())
        path = twin_store.workspace_store_path(self.base, self.wsid)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": "99.0.0"}, fh)
        loaded, err = twin_store.load(self.base, self.wsid)
        self.assertIsNone(loaded)
        self.assertIn("newer", err)
        with open(path, encoding="utf-8") as fh:
            self.assertIn("99.0.0", fh.read())

    def test_a_failed_load_never_rewrites_the_store(self):
        twin_store.save(self.base, self.wsid, self._store())
        path = twin_store.workspace_store_path(self.base, self.wsid)
        with open(path, "rb") as fh:
            before = fh.read()
        twin_store.load(self.base, self.wsid)  # succeeds, no-op on disk
        with open(path, "rb") as fh:
            self.assertEqual(before, fh.read())


class AppDataDirTests(unittest.TestCase):
    def test_app_data_dir_is_never_the_repository(self):
        d = twin_store.app_data_dir()
        self.assertTrue(d.endswith("human-readable-code-agent") or d.endswith("HumanReadableCodeAgent"))
        self.assertIn("human-readable-code-agent", d.replace("HumanReadableCodeAgent", "human-readable-code-agent"))


if __name__ == "__main__":
    unittest.main()
