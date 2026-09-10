"""Tests for the P4.4 document/version-authority boundary actions."""

from __future__ import annotations

import tempfile
import unittest

from hrca import app_package, boundary, contract


def _req(action, **overrides):
    req = {
        "contract_version": contract.CONTRACT_VERSION,
        "correlation_id": "cid-doc",
        "action": action,
    }
    req.update(overrides)
    return req


class FakeRunner:
    def __init__(self):
        self.preflight_calls = 0
        self.run_calls = 0

    def preflight(self):
        self.preflight_calls += 1
        return {"available": True, "reason": None}

    def run(self, *, handler, input_payload):
        self.run_calls += 1
        return {"discount": "0.00"}, None


class FakeTransport:
    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raise AssertionError("transport must not be called")


class BoundaryDocumentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = boundary.WorkspaceSession(store_base=self._tmp.name)
        self.session.runner = FakeRunner()
        self.session.advisory_transport = FakeTransport()

    def tearDown(self):
        self._tmp.cleanup()

    def _do(self, action, **overrides):
        return boundary.handle_request(_req(action, **overrides), self.session)

    def _create(self, name="requirements.md"):
        return self._do(contract.ACTION_DOCUMENT_CREATE, name=name)

    def _doc_id(self, envelope):
        return envelope["result"]["document"]["document_id"]

    def test_create_document(self):
        env = self._create()
        self.assertTrue(env["ok"])
        doc = env["result"]["document"]
        self.assertEqual(doc["name"], "requirements.md")
        self.assertEqual(doc["kind"], "md")
        self.assertEqual(doc["head_revision_number"], 0)
        self.assertIsNone(env["result"]["head_revision"])

    def test_create_document_invalid_name(self):
        env = self._create(name="requirements.py")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_name_invalid")

    def test_create_duplicate_name_rejected_case_insensitive(self):
        self._create("requirements.md")
        env = self._create("REQUIREMENTS.MD")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_name_in_use")

    def test_create_duplicate_name_rejected_after_trim(self):
        self._create("requirements.md")
        env = self._create("  requirements.md  ")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_name_in_use")

    def test_create_invalid_names_rejected(self):
        for name in ("", "notes.py", "dir/notes.md", "CON.md", "..", "nul.txt"):
            with self.subTest(name=name):
                env = self._create(name=name)
                self.assertFalse(env["ok"])
                self.assertEqual(env["error"]["code"], "document_name_invalid")

    def test_create_collision_leaves_existing_unchanged(self):
        self._create("requirements.md")
        self._create("REQUIREMENTS.md")
        env = self._do(contract.ACTION_DOCUMENT_LIST)
        self.assertTrue(env["ok"])
        names = [d["name"] for d in env["result"]["documents"]]
        self.assertEqual(names, ["requirements.md"])

    def test_open_absent_document(self):
        env = self._do(contract.ACTION_DOCUMENT_OPEN, document_id="doc:missing")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_not_found")

    def test_save_then_reopen_round_trips_exact_content(self):
        doc_id = self._doc_id(self._create())
        content = "# 需求\n繁體中文 — 已儲存。\nEnglish text.\n"
        env = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content=content,
            base_revision_id=None,
        )
        self.assertTrue(env["ok"])
        revision = env["result"]["revision"]
        self.assertEqual(revision["content"], content)

        reopened = self._do(contract.ACTION_DOCUMENT_OPEN, document_id=doc_id)
        self.assertTrue(reopened["ok"])
        self.assertEqual(reopened["result"]["head_revision"]["content"], content)
        self.assertEqual(reopened["result"]["head_revision"]["revision_id"], revision["revision_id"])

    def test_save_stale_base_refused(self):
        doc_id = self._doc_id(self._create())
        env1 = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content="first",
            base_revision_id=None,
        )
        self.assertTrue(env1["ok"])
        # The same base (now stale) is refused.
        env2 = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content="second",
            base_revision_id=None,
        )
        self.assertFalse(env2["ok"])
        self.assertEqual(env2["error"]["code"], "document_stale")

    def test_save_oversized_refused(self):
        doc_id = self._doc_id(self._create())
        env = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content="x" * (contract.MAX_WORKING_DOCUMENT_BYTES + 1),
            base_revision_id=None,
        )
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_oversized")

    def test_save_has_zero_side_effects(self):
        # Save must not touch candidate/accepted state, run a package, or call a
        # provider/transport.
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        self.assertTrue(created["ok"])
        candidate_before = created["result"]["candidate"]
        accepted_before = created["result"]["accepted"]
        self.assertIsNone(accepted_before)

        # Save a second revision with the correct base.
        head = created["result"]["head_revision"]["revision_id"]
        env = self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content="v2",
            base_revision_id=head,
        )
        self.assertTrue(env["ok"])

        # Candidate and accepted state are unchanged by the save.
        reopened = self._do(contract.ACTION_DOCUMENT_OPEN, document_id=doc_id)
        self.assertEqual(reopened["result"]["candidate"]["candidate_id"], candidate_before["candidate_id"])
        self.assertEqual(reopened["result"]["candidate"]["document_revision_id"], candidate_before["document_revision_id"])
        self.assertIsNone(reopened["result"]["accepted"])
        # No package execution and no provider call.
        self.assertEqual(self.session.runner.run_calls, 0)
        self.assertEqual(self.session.runner.preflight_calls, 0)
        self.assertEqual(self.session.advisory_transport.calls, 0)

    def test_create_candidate_binds_fixture(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        env = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        self.assertTrue(env["ok"])
        candidate = env["result"]["candidate"]
        self.assertEqual(candidate["package_id"], "quotation-rules")
        self.assertEqual(candidate["runtime_identity"], app_package.RUNNER_IDENTITY)
        self.assertEqual(candidate["generation_source"], "deterministic_fixture")

    def test_create_candidate_requires_saved_revision(self):
        doc_id = self._doc_id(self._create())
        env = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_not_saved")

    def test_adopt_flow_and_versions(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        candidate_id = created["result"]["candidate"]["candidate_id"]

        adopted = self._do(contract.ACTION_DOCUMENT_ADOPT, document_id=doc_id, candidate_id=candidate_id)
        self.assertTrue(adopted["ok"])
        self.assertEqual(adopted["result"]["state"], "adopted")
        version_id = adopted["result"]["accepted_version"]["version_id"]

        versions = self._do(contract.ACTION_DOCUMENT_LIST_VERSIONS, document_id=doc_id)
        self.assertTrue(versions["ok"])
        self.assertEqual(versions["result"]["current_accepted_version_id"], version_id)
        self.assertEqual(len(versions["result"]["versions"]), 1)

        # Repeated adoption is refused.
        again = self._do(contract.ACTION_DOCUMENT_ADOPT, document_id=doc_id, candidate_id=candidate_id)
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"]["code"], "already_adopted")

    def test_adopt_stale_after_edit_refused(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        candidate_id = created["result"]["candidate"]["candidate_id"]
        # Edit the document after the candidate was created.
        self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content="v2",
            base_revision_id=created["result"]["head_revision"]["revision_id"],
        )
        env = self._do(contract.ACTION_DOCUMENT_ADOPT, document_id=doc_id, candidate_id=candidate_id)
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "candidate_stale")

    def test_get_candidate_state(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        candidate_id = created["result"]["candidate"]["candidate_id"]
        env = self._do(contract.ACTION_DOCUMENT_GET_CANDIDATE, document_id=doc_id, candidate_id=candidate_id)
        self.assertTrue(env["ok"])
        self.assertTrue(env["result"]["candidate"]["current"])
        self.assertFalse(env["result"]["candidate"]["adopted"])

    def test_restore_preserves_working_document(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created1 = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        adopted1 = self._do(
            contract.ACTION_DOCUMENT_ADOPT,
            document_id=doc_id,
            candidate_id=created1["result"]["candidate"]["candidate_id"],
        )
        v1 = adopted1["result"]["accepted_version"]["version_id"]

        # Adopt a second candidate so v1 becomes a *prior* accepted version.
        created2 = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        adopted2 = self._do(
            contract.ACTION_DOCUMENT_ADOPT,
            document_id=doc_id,
            candidate_id=created2["result"]["candidate"]["candidate_id"],
        )
        self.assertTrue(adopted2["ok"])

        # A newer working document exists; restoring v1 must not discard it.
        self._do(
            contract.ACTION_DOCUMENT_SAVE,
            document_id=doc_id,
            content="newer working text",
            base_revision_id=created2["result"]["head_revision"]["revision_id"],
        )
        restored = self._do(contract.ACTION_DOCUMENT_RESTORE, document_id=doc_id, version_id=v1)
        self.assertTrue(restored["ok"])
        self.assertEqual(restored["result"]["state"], "restored")
        self.assertEqual(restored["result"]["accepted_version"]["restore_of"], v1)
        # The newer working document is preserved.
        self.assertEqual(restored["result"]["head_revision"]["content"], "newer working text")

    def test_restore_missing_refused(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        env = self._do(contract.ACTION_DOCUMENT_RESTORE, document_id=doc_id, version_id="ver:missing")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "version_not_found")

    def test_list_documents(self):
        self._create("alpha.md")
        self._create("beta.txt")
        env = self._do(contract.ACTION_DOCUMENT_LIST)
        self.assertTrue(env["ok"])
        names = [d["name"] for d in env["result"]["documents"]]
        self.assertEqual(names, ["alpha.md", "beta.txt"])

    def test_preview_current_candidate(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)
        self.assertTrue(env["ok"])
        preview = env["result"]
        self.assertEqual(preview["state"], "current")
        self.assertEqual(preview["binding"]["kind"], "candidate")
        self.assertEqual(preview["document"]["revision_number"], 1)
        self.assertIn("subtotal", [f["name"] for f in preview["package"]["form"]])

    def test_preview_no_candidate(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)
        self.assertTrue(env["ok"])
        self.assertEqual(env["result"]["state"], "no_candidate")

    def test_preview_no_candidate_has_no_fixture_detail(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)
        preview = env["result"]
        self.assertIsNone(preview["package"])
        self.assertIsNone(preview["evidence"])
        self.assertIsNone(preview["provenance"])

    def test_preview_accepted_with_newer_requirements_is_stale(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        created = self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        candidate_id = created["result"]["candidate"]["candidate_id"]
        self._do(contract.ACTION_DOCUMENT_ADOPT, document_id=doc_id, candidate_id=candidate_id)
        head = self._do(contract.ACTION_DOCUMENT_OPEN, document_id=doc_id)["result"]["head_revision"]["revision_id"]
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v2", base_revision_id=head)
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)
        preview = env["result"]
        self.assertEqual(preview["state"], "stale")
        self.assertEqual(preview["binding"]["kind"], "accepted")
        self.assertIsNotNone(preview["package"])

    def test_preview_missing_document(self):
        env = self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id="doc:missing")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "document_not_found")

    def test_preview_has_zero_side_effects(self):
        doc_id = self._doc_id(self._create())
        self._do(contract.ACTION_DOCUMENT_SAVE, document_id=doc_id, content="v1", base_revision_id=None)
        self._do(contract.ACTION_DOCUMENT_CREATE_CANDIDATE, document_id=doc_id)
        self._do(contract.ACTION_DOCUMENT_PREVIEW, document_id=doc_id)
        self.assertEqual(self.session.runner.run_calls, 0)
        self.assertEqual(self.session.runner.preflight_calls, 0)
        self.assertEqual(self.session.advisory_transport.calls, 0)


if __name__ == "__main__":
    unittest.main()
