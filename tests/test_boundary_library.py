"""Tests for the P4.6 app-owned document-library boundary actions."""

from __future__ import annotations

import tempfile
import unittest

from hrca import app_package, boundary, contract


def _req(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-lib",
        "action": action,
    }
    req.update(overrides)
    return req


class FakeRunner:
    def __init__(self):
        self.run_calls = 0
        self.preflight_calls = 0

    def preflight(self):
        self.preflight_calls += 1
        return {"available": True, "reason": None}

    def run(self, *, handler, input_payload):
        self.run_calls += 1
        return {"discount": "0.00"}, None


class BoundaryLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)
        self.session.runner = FakeRunner()

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(_req(action, **overrides), self.session)

    def _create(self, name="requirements.md", parent_id=None):
        return self._do(contract.ACTION_DOCUMENT_CREATE, name=name, parent_id=parent_id)

    def _doc_id(self, envelope):
        return envelope["result"]["document"]["document_id"]

    def _folder_id(self, envelope):
        return envelope["result"]["folders"][0]["folder_id"]

    def _folders(self, envelope):
        return {f["name"]: f for f in envelope["result"]["folders"]}

    # -- tree + migration -------------------------------------------------

    def test_get_library_lists_documents(self):
        self._create("alpha.md")
        self._create("beta.txt")
        env = self._do(contract.ACTION_LIBRARY_GET)
        self.assertTrue(env["ok"])
        names = [d["name"] for d in env["result"]["documents"]]
        self.assertEqual(names, ["alpha.md", "beta.txt"])

    def test_flat_documents_migrate_to_root_without_library_write(self):
        # Two documents created before any folder exists are both at the root.
        a = self._doc_id(self._create("alpha.md"))
        self._create("beta.md")
        env = self._do(contract.ACTION_LIBRARY_GET)
        parents = {d["document_id"]: d["parent_id"] for d in env["result"]["documents"]}
        self.assertIsNone(parents[a])

    # -- folder create + collisions ----------------------------------------

    def test_create_folder_and_document_under_it(self):
        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="Plans"))
        env = self._create("roadmap.md", parent_id=fid)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["tree"]["documents"][0]["parent_id"], fid)

    def test_document_vs_folder_sibling_collision(self):
        self._create("notes.md")
        # A folder with the exact same (normalized) name is refused.
        env = self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="notes.md")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "name_in_use")

    def test_folder_vs_folder_sibling_collision(self):
        self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="Plans")
        env = self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="plans")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "name_in_use")

    def test_same_name_in_different_folders_is_allowed(self):
        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="A"))
        self._create("notes.md", parent_id=fid)
        # The same name at the root is fine (different parent scope).
        env = self._create("notes.md")
        self.assertTrue(env["ok"])

    def test_create_folder_invalid_name(self):
        for name in ("", "a/b", "con", ".."):
            with self.subTest(name=name):
                env = self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name=name)
                self.assertFalse(env["ok"])
                self.assertEqual(env["error"]["code"], "folder_name_invalid")

    def test_create_document_under_missing_folder(self):
        env = self._create("notes.md", parent_id="dir:missing")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "folder_not_found")

    def test_create_document_under_trashed_folder(self):
        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="A"))
        self._do(contract.ACTION_LIBRARY_TRASH, item_id=fid)
        env = self._create("notes.md", parent_id=fid)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "parent_trashed")

    # -- rename -----------------------------------------------------------

    def test_rename_document_keeps_identity_and_revisions(self):
        doc_id = self._doc_id(self._create("old.md"))
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        before = self._do(contract.ACTION_DOCUMENT_OPEN, document_id=doc_id)["result"]

        env = self._do(contract.ACTION_LIBRARY_RENAME, item_id=doc_id, name="new.md")
        self.assertTrue(env["ok"])
        self.assertEqual(
            [d["name"] for d in env["result"]["documents"] if d["document_id"] == doc_id],
            ["new.md"],
        )
        after = self._do(contract.ACTION_DOCUMENT_OPEN, document_id=doc_id)["result"]
        self.assertEqual(after["head_revision"]["revision_id"], before["head_revision"]["revision_id"])
        self.assertEqual(after["head_revision"]["content"], "v1")

    def test_rename_document_collision(self):
        self._create("a.md")
        b = self._doc_id(self._create("b.md"))
        env = self._do(contract.ACTION_LIBRARY_RENAME, item_id=b, name="a.md")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "name_in_use")

    def test_rename_folder(self):
        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="Old"))
        env = self._do(contract.ACTION_LIBRARY_RENAME, item_id=fid, name="New")
        self.assertTrue(env["ok"])
        self.assertEqual(self._folders(env)["New"]["folder_id"], fid)

    # -- move -------------------------------------------------------------

    def test_move_document_between_folders(self):
        a = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="A"))
        b = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="B"))
        doc_id = self._doc_id(self._create("notes.md", parent_id=a))
        env = self._do(contract.ACTION_LIBRARY_MOVE, item_id=doc_id, parent_id=b)
        self.assertTrue(env["ok"])
        parents = {d["document_id"]: d["parent_id"] for d in env["result"]["documents"]}
        self.assertEqual(parents[doc_id], b)

    def test_move_folder_into_descendant_is_cyclic(self):
        a = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="A"))
        b = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="B", parent_id=a))
        env = self._do(contract.ACTION_LIBRARY_MOVE, item_id=a, parent_id=b)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "cyclic_move")

    def test_move_document_name_collision(self):
        a = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="A"))
        self._create("notes.md", parent_id=a)
        other = self._doc_id(self._create("notes.md"))
        env = self._do(contract.ACTION_LIBRARY_MOVE, item_id=other, parent_id=a)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "name_in_use")

    # -- trash / restore ---------------------------------------------------

    def test_trash_and_restore_document(self):
        doc_id = self._doc_id(self._create("notes.md"))
        self._do(contract.ACTION_LIBRARY_TRASH, item_id=doc_id)
        tree = self._do(contract.ACTION_LIBRARY_GET)["result"]
        self.assertEqual(tree["documents"][0]["trashed"], True)
        self._do(contract.ACTION_LIBRARY_RESTORE, item_id=doc_id)
        tree = self._do(contract.ACTION_LIBRARY_GET)["result"]
        self.assertEqual(tree["documents"][0]["trashed"], False)

    def test_trash_non_empty_folder_then_restore_keeps_children(self):
        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="F"))
        self._create("child.md", parent_id=fid)
        self._do(contract.ACTION_LIBRARY_TRASH, item_id=fid)
        # After restore the child reappears under F.
        self._do(contract.ACTION_LIBRARY_RESTORE, item_id=fid)
        tree = self._do(contract.ACTION_LIBRARY_GET)["result"]
        child = [d for d in tree["documents"] if d["name"] == "child.md"][0]
        self.assertEqual(child["parent_id"], fid)
        self.assertFalse(child["trashed"])

    def test_restore_collision_refused_without_overwriting(self):
        doc_id = self._doc_id(self._create("notes.md"))
        self._do(contract.ACTION_LIBRARY_TRASH, item_id=doc_id)
        # A new live document takes the name while the original is trashed.
        self._create("notes.md")
        env = self._do(contract.ACTION_LIBRARY_RESTORE, item_id=doc_id)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "restore_collision")
        # The live document is untouched.
        tree = self._do(contract.ACTION_LIBRARY_GET)["result"]
        self.assertEqual(len([d for d in tree["documents"] if d["name"] == "notes.md" and not d["trashed"]]), 1)

    def test_restore_non_trashed_refused(self):
        doc_id = self._doc_id(self._create("notes.md"))
        env = self._do(contract.ACTION_LIBRARY_RESTORE, item_id=doc_id)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "item_not_trashed")

    # -- restart / reopen --------------------------------------------------

    def test_tree_survives_restart(self):
        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="F"))
        self._create("notes.md", parent_id=fid)
        # A fresh session over the same store base re-reads the same tree.
        fresh = boundary.WorkspaceSession(store_base=self._tmp.name)
        fresh.runner = FakeRunner()
        env = boundary.handle_request(
            _req(contract.ACTION_LIBRARY_GET), fresh
        )
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["folders"][0]["name"], "F")
        self.assertEqual(env["result"]["documents"][0]["parent_id"], fid)

    # -- preview / versions isolation --------------------------------------

    def test_rename_move_trash_do_not_alter_candidate_or_accepted(self):
        doc_id = self._doc_id(self._create("req.md"))
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        candidate_id = created["result"]["candidate"]["candidate_id"]
        adopted = self._do(contract.ACTION_DOCUMENT_ADOPT, document_id=doc_id, candidate_id=candidate_id)
        version_id = adopted["result"]["accepted_version"]["version_id"]

        fid = self._folder_id(self._do(contract.ACTION_LIBRARY_CREATE_FOLDER, name="F"))
        self._do(contract.ACTION_LIBRARY_RENAME, item_id=doc_id, name="renamed.md")
        self._do(contract.ACTION_LIBRARY_MOVE, item_id=doc_id, parent_id=fid)
        self._do(contract.ACTION_LIBRARY_TRASH, item_id=doc_id)
        self._do(contract.ACTION_LIBRARY_RESTORE, item_id=doc_id)

        preview = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)["result"]
        self.assertEqual(preview["state"], "current")
        self.assertEqual(preview["binding"]["kind"], "accepted")
        versions = self._do(contract.ACTION_DOCUMENT_LIST_VERSIONS, document_id=doc_id)["result"]
        self.assertEqual(versions["current_accepted_version_id"], version_id)
        self.assertEqual(versions["versions"][0]["version_id"], version_id)
        # No runner/package was executed.
        self.assertEqual(self.session.runner.run_calls, 0)

    def test_trashed_document_preview_is_unchanged(self):
        doc_id = self._doc_id(self._create("req.md"))
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        self._do(contract.ACTION_LIBRARY_TRASH, item_id=doc_id)
        # Preview still resolves by id (trash is a library concern only).
        preview = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)["result"]
        self.assertEqual(preview["state"], "no_candidate")


if __name__ == "__main__":
    unittest.main()
