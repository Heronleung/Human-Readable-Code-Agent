"""Tests for the P4.4 document/version persistence (:mod:`hrca.version_store`)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import document, version_store

_NOW = "2026-09-09T00:00:00+00:00"


def _store(document_id="doc:d1", content="hello"):
    store = document.new_document_store(document_id, "requirements.md", "md", _NOW)
    new_store, _ = document.save_revision(store, content, None, _NOW)
    return new_store


class _Base:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class SaveLoadTests(_Base, unittest.TestCase):
    def test_round_trip_is_identical(self):
        store = _store()
        self.assertIsNone(version_store.save(self.base, "doc:d1", store))
        loaded, err = version_store.load(self.base, "doc:d1")
        self.assertIsNone(err)
        self.assertEqual(loaded, store)

    def test_absent_loads_to_none(self):
        loaded, err = version_store.load(self.base, "doc:missing")
        self.assertIsNone(loaded)
        self.assertIsNone(err)

    def test_lives_under_documents_namespace(self):
        version_store.save(self.base, "doc:d1", _store())
        path = version_store.document_store_path(self.base, "doc:d1")
        self.assertIn("documents", path)
        self.assertTrue(path.startswith(self.base))
        self.assertTrue(os.path.isfile(path))

    def test_corrupt_json_loads_to_error(self):
        version_store.save(self.base, "doc:d1", _store())
        path = version_store.document_store_path(self.base, "doc:d1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        loaded, err = version_store.load(self.base, "doc:d1")
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)

    def test_future_version_loads_to_error_and_keeps_store(self):
        store = _store()
        store["schema_version"] = "99.0.0"
        version_store.save(self.base, "doc:d1", store)
        loaded, err = version_store.load(self.base, "doc:d1")
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)
        # The on-disk store is untouched.
        path = version_store.document_store_path(self.base, "doc:d1")
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.loads(fh.read())
        self.assertEqual(raw["schema_version"], "99.0.0")


class ListDocumentsTests(_Base, unittest.TestCase):
    def test_list_is_sorted_and_summarized(self):
        version_store.save(self.base, "doc:b", _store("doc:b", "bee"))
        version_store.save(self.base, "doc:a", _store("doc:a", "aye"))
        summaries = version_store.list_documents(self.base)
        ids = [s["document_id"] for s in summaries]
        self.assertEqual(ids, ["doc:a", "doc:b"])
        self.assertEqual(summaries[0]["name"], "requirements.md")
        self.assertEqual(summaries[0]["head_revision_number"], 1)
        self.assertEqual(summaries[0]["revision_count"], 1)
        self.assertEqual(summaries[0]["has_candidate"], False)

    def test_corrupt_store_is_skipped(self):
        version_store.save(self.base, "doc:good", _store("doc:good", "ok"))
        version_store.save(self.base, "doc:bad", _store("doc:bad", "bad"))
        path = version_store.document_store_path(self.base, "doc:bad")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        summaries = version_store.list_documents(self.base)
        self.assertEqual([s["document_id"] for s in summaries], ["doc:good"])

    def test_empty_dir_lists_nothing(self):
        self.assertEqual(version_store.list_documents(self.base), [])


class LegacyIsolationTests(_Base, unittest.TestCase):
    def test_legacy_twin_store_is_untouched(self):
        # A twin.json under a workspace namespace must not be listed as a
        # document, and saving documents must not create/alter twin files.
        from hrca import twin, twin_store

        wsid = twin.workspace_id_for("/tmp/legacy-workspace")
        twin_store.save(self.base, wsid, {"schema_version": twin.TWIN_SCHEMA_VERSION, "artifacts": []})
        version_store.save(self.base, "doc:d1", _store())
        # The twin store is still present and unchanged.
        loaded, err = twin_store.load(self.base, wsid)
        self.assertIsNone(err)
        self.assertEqual(loaded["schema_version"], twin.TWIN_SCHEMA_VERSION)
        # The document is not confused with the twin.
        summaries = version_store.list_documents(self.base)
        self.assertEqual([s["document_id"] for s in summaries], ["doc:d1"])


if __name__ == "__main__":
    unittest.main()
