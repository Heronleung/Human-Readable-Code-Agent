"""Tests for the P4.6 library persistence (:mod:`hrca.library_store`)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import document, library, library_store, version_store

_NOW = "2026-09-10T00:00:00+00:00"


def _save_document(base, document_id, name="requirements.md", content="hello"):
    store = document.new_document_store(document_id, name, "md", _NOW)
    store, _ = document.save_revision(store, content, None, _NOW)
    version_store.save(base, document_id, store)
    return store


class _Base:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class SaveLoadTests(_Base, unittest.TestCase):
    def test_round_trip(self):
        store = library.new_library_store(_NOW)
        store["folders"].append(library.new_folder_record("dir:a", "Plans", None, _NOW))
        self.assertIsNone(library_store.save(self.base, store))
        loaded, err = library_store.load(self.base)
        self.assertIsNone(err)
        self.assertEqual(loaded, store)

    def test_absent_loads_to_none(self):
        loaded, err = library_store.load(self.base)
        self.assertIsNone(loaded)
        self.assertIsNone(err)

    def test_corrupt_store_loads_to_error(self):
        library_store.save(self.base, library.new_library_store(_NOW))
        path = library_store.library_store_path(self.base)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        loaded, err = library_store.load(self.base)
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)

    def test_future_version_loads_to_error_and_keeps_store(self):
        store = library.new_library_store(_NOW)
        store["schema_version"] = "99.0.0"
        library_store.save(self.base, store)
        loaded, err = library_store.load(self.base)
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)


class MigrationTests(_Base, unittest.TestCase):
    def test_flat_documents_are_adopted_to_root(self):
        _save_document(self.base, "doc:a", "alpha.md")
        _save_document(self.base, "doc:b", "beta.md")
        store, adopted = library_store.ensure_library(self.base, _NOW)
        self.assertEqual(adopted, 2)
        ids = {d["document_id"] for d in store["documents"]}
        self.assertEqual(ids, {"doc:a", "doc:b"})
        for ref in store["documents"]:
            self.assertIsNone(ref["parent_id"])
            self.assertFalse(ref["trashed"])

    def test_migration_is_idempotent(self):
        _save_document(self.base, "doc:a", "alpha.md")
        first, adopted1 = library_store.ensure_library(self.base, _NOW)
        self.assertEqual(adopted1, 1)
        second, adopted2 = library_store.ensure_library(self.base, _NOW)
        self.assertEqual(adopted2, 0)
        self.assertEqual(len(second["documents"]), 1)

    def test_migration_preserves_ids_content_and_revisions(self):
        original = _save_document(self.base, "doc:a", "alpha.md", "exact content")
        library_store.ensure_library(self.base, _NOW)
        loaded, err = version_store.load(self.base, "doc:a")
        self.assertIsNone(err)
        self.assertEqual(loaded["document_id"], "doc:a")
        self.assertEqual(loaded["name"], "alpha.md")
        self.assertEqual(loaded["head_revision_id"], original["head_revision_id"])
        self.assertEqual(loaded["revisions"], original["revisions"])

    def test_legacy_duplicate_names_are_preserved(self):
        _save_document(self.base, "doc:a", "notes.md", "first")
        _save_document(self.base, "doc:b", "notes.md", "second")
        store, _ = library_store.ensure_library(self.base, _NOW)
        self.assertEqual(len(store["documents"]), 2)
        # Neither document is renamed, merged or dropped.
        tree = library_store.get_tree(self.base, _NOW)
        names = [d["name"] for d in tree["documents"]]
        self.assertEqual(sorted(names), ["notes.md", "notes.md"])

    def test_corrupt_library_is_not_silently_replaced(self):
        library_store.save(self.base, library.new_library_store(_NOW))
        path = library_store.library_store_path(self.base)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        store, adopted = library_store.ensure_library(self.base, _NOW)
        self.assertIsNone(store)
        self.assertEqual(adopted, 0)


class GetTreeTests(_Base, unittest.TestCase):
    def test_tree_is_joined_and_sorted(self):
        _save_document(self.base, "doc:b", "beta.md")
        _save_document(self.base, "doc:a", "alpha.md")
        library_store.ensure_library(self.base, _NOW)
        # Move doc:b under a new folder to exercise the parent join.
        store, _ = library_store.ensure_library(self.base, _NOW)
        store, _ = library.add_folder(store, "dir:f", "Folder", None, _NOW, set())
        store, _ = library.move_document(store, "doc:b", "dir:f", set(), "beta.md")
        library_store.save(self.base, store)

        tree = library_store.get_tree(self.base, _NOW)
        self.assertEqual([f["name"] for f in tree["folders"]], ["Folder"])
        docs = {d["name"]: d for d in tree["documents"]}
        self.assertEqual(docs["alpha.md"]["parent_id"], None)
        self.assertEqual(docs["beta.md"]["parent_id"], "dir:f")
        self.assertEqual(docs["alpha.md"]["head_revision_number"], 1)

    def test_dangling_reference_is_dropped(self):
        store = library.new_library_store(_NOW)
        store["documents"].append(library.new_document_record("doc:ghost", None))
        library_store.save(self.base, store)
        tree = library_store.get_tree(self.base, _NOW)
        self.assertEqual(tree["documents"], [])


if __name__ == "__main__":
    unittest.main()
