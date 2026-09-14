"""Tests for the P4.6 app-owned library domain (:mod:`hrca.library`)."""

from __future__ import annotations

import unittest

from hrca import document, library

_NOW = "2026-09-10T00:00:00+00:00"


def _store(folders=None, documents=None):
    store = library.new_library_store(_NOW)
    store["folders"] = folders or []
    store["documents"] = documents or []
    return store


def _folder(folder_id, name, parent_id=None, trashed=False):
    rec = library.new_folder_record(folder_id, name, parent_id, _NOW)
    rec["trashed"] = trashed
    return rec


def _doc(document_id, parent_id=None, trashed=False):
    rec = library.new_document_record(document_id, parent_id)
    rec["trashed"] = trashed
    return rec


class FolderNameTests(unittest.TestCase):
    def test_valid_names_normalize(self):
        self.assertEqual(library.normalize_folder_name("  Budget  "), "Budget")
        self.assertEqual(library.normalize_folder_name("Archive-2026"), "Archive-2026")

    def test_invalid_names_rejected(self):
        for name in ("", "   ", "a/b", "a\\b", ".", "..", "con", "NUL.txt",
                     "x" * (library.MAX_FOLDER_NAME_CHARS + 1)):
            with self.subTest(name=name):
                self.assertIsNone(library.normalize_folder_name(name))

    def test_name_key_is_case_insensitive(self):
        self.assertEqual(
            library.folder_name_key("Budget"), library.folder_name_key("budget")
        )


class SiblingNameKeyTests(unittest.TestCase):
    def test_folders_and_documents_share_a_namespace(self):
        folders = [_folder("dir:a", "Plans")]
        documents = [{"document_id": "doc:b", "name": "plans.md",
                      "parent_id": None, "trashed": False}]
        keys = library.sibling_name_keys(folders, documents, None)
        # "Plans" (folder) and "plans.md" (document) are distinct names, so both
        # keys are present and they do not collide with each other.
        self.assertEqual(keys, {"plans", "plans.md"})

    def test_document_vs_folder_collision_on_exact_name(self):
        folders = [_folder("dir:a", "Notes")]
        documents = [{"document_id": "doc:b", "name": "Notes",
                      "parent_id": None, "trashed": False}]
        keys = library.sibling_name_keys(folders, documents, None)
        self.assertIn("notes", keys)

    def test_trashed_and_other_parent_are_excluded(self):
        folders = [
            _folder("dir:a", "Live"),
            _folder("dir:b", "Trashed", trashed=True),
            _folder("dir:c", "Elsewhere", parent_id="dir:x"),
        ]
        documents = [{"document_id": "doc:d", "name": "trash.md",
                      "parent_id": "dir:x", "trashed": False}]
        keys = library.sibling_name_keys(folders, documents, None)
        self.assertEqual(keys, {"live"})

    def test_exclude_id_drops_self(self):
        folders = [_folder("dir:a", "Plans")]
        keys = library.sibling_name_keys(folders, [], None, exclude_id="dir:a")
        self.assertEqual(keys, set())


class AddFolderTests(unittest.TestCase):
    def test_adds_folder_at_root(self):
        store = _store()
        new_store, err = library.add_folder(
            store, "dir:a", "Plans", None, _NOW, set()
        )
        self.assertIsNone(err)
        self.assertEqual(len(new_store["folders"]), 1)
        self.assertEqual(new_store["folders"][0]["name"], "Plans")
        self.assertEqual(new_store["folders"][0]["parent_id"], None)
        self.assertFalse(new_store["folders"][0]["trashed"])

    def test_rejects_invalid_name(self):
        new_store, err = library.add_folder(_store(), "dir:a", "a/b", None, _NOW, set())
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_NAME_INVALID)

    def test_rejects_name_in_use(self):
        new_store, err = library.add_folder(_store(), "dir:a", "Plans", None, _NOW, {"plans"})
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_NAME_IN_USE)

    def test_rejects_missing_parent(self):
        new_store, err = library.add_folder(_store(), "dir:a", "Plans", "dir:missing", _NOW, set())
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_FOLDER_NOT_FOUND)

    def test_rejects_trashed_parent(self):
        store = _store(folders=[_folder("dir:p", "Parent", trashed=True)])
        new_store, err = library.add_folder(store, "dir:a", "Plans", "dir:p", _NOW, set())
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_PARENT_TRASHED)


class RenameFolderTests(unittest.TestCase):
    def test_rename_changes_only_name(self):
        store = _store(folders=[_folder("dir:a", "Old", None)])
        new_store, err = library.rename_folder(store, "dir:a", "New", set())
        self.assertIsNone(err)
        folder = new_store["folders"][0]
        self.assertEqual(folder["name"], "New")
        self.assertEqual(folder["folder_id"], "dir:a")
        self.assertFalse(folder["trashed"])

    def test_rename_does_not_mutate_input(self):
        store = _store(folders=[_folder("dir:a", "Old", None)])
        library.rename_folder(store, "dir:a", "New", set())
        self.assertEqual(store["folders"][0]["name"], "Old")

    def test_rename_collision_rejected(self):
        store = _store(folders=[_folder("dir:a", "Old", None)])
        new_store, err = library.rename_folder(store, "dir:a", "Taken", {"taken"})
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_NAME_IN_USE)


class MoveFolderTests(unittest.TestCase):
    def test_move_updates_parent_only(self):
        store = _store(folders=[_folder("dir:a", "A"), _folder("dir:b", "B")])
        new_store, err = library.move_folder(store, "dir:b", "dir:a", set())
        self.assertIsNone(err)
        moved = [f for f in new_store["folders"] if f["folder_id"] == "dir:b"][0]
        self.assertEqual(moved["parent_id"], "dir:a")
        self.assertEqual(moved["name"], "B")

    def test_move_into_self_rejected(self):
        store = _store(folders=[_folder("dir:a", "A")])
        new_store, err = library.move_folder(store, "dir:a", "dir:a", set())
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_CYCLIC_MOVE)

    def test_move_into_descendant_rejected(self):
        store = _store(folders=[
            _folder("dir:a", "A"),
            _folder("dir:b", "B", parent_id="dir:a"),
        ])
        new_store, err = library.move_folder(store, "dir:a", "dir:b", set())
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_CYCLIC_MOVE)

    def test_move_name_collision_rejected(self):
        store = _store(folders=[
            _folder("dir:a", "A"),
            _folder("dir:b", "B"),
            _folder("dir:c", "B", parent_id="dir:a"),  # "b" already under a
        ])
        new_store, err = library.move_folder(store, "dir:b", "dir:a", {"b"})
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_NAME_IN_USE)


class TrashRestoreTests(unittest.TestCase):
    def test_trash_is_idempotent(self):
        store = _store(folders=[_folder("dir:a", "A")])
        once, err = library.trash_folder(store, "dir:a")
        self.assertIsNone(err)
        self.assertTrue(once["folders"][0]["trashed"])
        twice, err = library.trash_folder(once, "dir:a")
        self.assertIsNone(err)
        self.assertTrue(twice["folders"][0]["trashed"])

    def test_restore_clears_trash_and_keeps_parent(self):
        store = _store(folders=[_folder("dir:a", "A", parent_id=None, trashed=True)])
        new_store, err = library.restore_folder(store, "dir:a", None, set())
        self.assertIsNone(err)
        self.assertFalse(new_store["folders"][0]["trashed"])

    def test_restore_collision_rejected(self):
        store = _store(folders=[_folder("dir:a", "A", trashed=True)])
        new_store, err = library.restore_folder(store, "dir:a", None, {"a"})
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_RESTORE_COLLISION)

    def test_restore_parent_drops_to_root_when_parent_trashed(self):
        store = _store(folders=[
            _folder("dir:p", "P", trashed=True),
            _folder("dir:a", "A", parent_id="dir:p", trashed=True),
        ])
        self.assertIsNone(library.restore_parent(store, store["folders"][1]))

    def test_restore_parent_kept_when_live(self):
        store = _store(folders=[
            _folder("dir:p", "P"),
            _folder("dir:a", "A", parent_id="dir:p", trashed=True),
        ])
        self.assertEqual(library.restore_parent(store, store["folders"][1]), "dir:p")


class DocumentRefTests(unittest.TestCase):
    def test_move_document_updates_parent(self):
        store = _store(
            folders=[_folder("dir:a", "A")],
            documents=[_doc("doc:d", None)],
        )
        new_store, err = library.move_document(store, "doc:d", "dir:a", set(), "d.md")
        self.assertIsNone(err)
        ref = [d for d in new_store["documents"] if d["document_id"] == "doc:d"][0]
        self.assertEqual(ref["parent_id"], "dir:a")

    def test_move_document_collision_rejected(self):
        store = _store(
            folders=[_folder("dir:a", "A")],
            documents=[_doc("doc:d", None)],
        )
        new_store, err = library.move_document(store, "doc:d", "dir:a", {"d.md"}, "d.md")
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_NAME_IN_USE)

    def test_trash_and_restore_document(self):
        store = _store(documents=[_doc("doc:d", None)])
        trashed, err = library.trash_document(store, "doc:d")
        self.assertIsNone(err)
        self.assertTrue(trashed["documents"][0]["trashed"])
        restored, err = library.restore_document(trashed, "doc:d", None, set(), "d.md")
        self.assertIsNone(err)
        self.assertFalse(restored["documents"][0]["trashed"])

    def test_restore_document_collision_rejected(self):
        store = _store(documents=[_doc("doc:d", None, trashed=True)])
        new_store, err = library.restore_document(store, "doc:d", None, {"d.md"}, "d.md")
        self.assertIsNone(new_store)
        self.assertEqual(err, library.REASON_RESTORE_COLLISION)


class CycleHelperTests(unittest.TestCase):
    def test_is_descendant(self):
        store = _store(folders=[
            _folder("dir:a", "A"),
            _folder("dir:b", "B", parent_id="dir:a"),
            _folder("dir:c", "C", parent_id="dir:b"),
        ])
        self.assertTrue(library.is_descendant(store, "dir:a", "dir:c"))
        self.assertTrue(library.is_descendant(store, "dir:b", "dir:b"))
        self.assertFalse(library.is_descendant(store, "dir:c", "dir:a"))

    def test_folder_effectively_trashed(self):
        store = _store(folders=[
            _folder("dir:a", "A", trashed=True),
            _folder("dir:b", "B", parent_id="dir:a"),
        ])
        self.assertTrue(library.folder_effectively_trashed(store, "dir:b"))
        self.assertTrue(library.folder_effectively_trashed(store, "dir:a"))


class MigrateLibraryTests(unittest.TestCase):
    def test_current_version_passes_through(self):
        store = library.new_library_store(_NOW)
        migrated, err = library.migrate_library(store)
        self.assertIsNone(err)
        self.assertEqual(migrated, store)

    def test_future_version_rejected(self):
        store = library.new_library_store(_NOW)
        store["schema_version"] = "99.0.0"
        migrated, err = library.migrate_library(store)
        self.assertIsNone(migrated)
        self.assertEqual(err, library.REASON_FUTURE_VERSION)

    def test_non_mapping_rejected(self):
        migrated, err = library.migrate_library("not a dict")
        self.assertIsNone(migrated)
        self.assertEqual(err, library.REASON_NOT_MAPPING)


class RenameDocumentTests(unittest.TestCase):
    def test_rename_document_keeps_revisions(self):
        store = document.new_document_store("doc:d1", "old.md", "md", _NOW)
        store, _ = document.save_revision(store, "hello", None, _NOW)
        revision_before = store["head_revision_id"]
        renamed, err = document.rename_document(store, "new.md")
        self.assertIsNone(err)
        self.assertEqual(renamed["name"], "new.md")
        self.assertEqual(renamed["head_revision_id"], revision_before)
        self.assertEqual(renamed["revisions"], store["revisions"])

    def test_rename_document_invalid_name_rejected(self):
        store = document.new_document_store("doc:d1", "old.md", "md", _NOW)
        renamed, err = document.rename_document(store, "notes.py")
        self.assertIsNone(renamed)
        self.assertEqual(err, document.REASON_NAME_INVALID)


if __name__ == "__main__":
    unittest.main()
