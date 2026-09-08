"""Tests for the P4.4 document/version client vocabulary (:mod:`hrca.client_core`)."""

from __future__ import annotations

import unittest

from hrca import contract
from hrca.client_core import (
    build_adopt_candidate_request,
    build_create_candidate_request,
    build_create_document_request,
    build_list_documents_request,
    build_list_versions_request,
    build_open_document_request,
    build_restore_version_request,
    build_save_document_request,
    document_failure_message,
    document_kind_label,
    format_document_state,
    format_version_list,
)


class RequestBuilderTests(unittest.TestCase):
    def test_create_document(self):
        req = build_create_document_request("cid", "requirements.md")
        self.assertEqual(req["contract_version"], contract.CONTRACT_VERSION)
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_CREATE)
        self.assertEqual(req["name"], "requirements.md")

    def test_open_document(self):
        req = build_open_document_request("cid", "doc:d1")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_OPEN)
        self.assertEqual(req["document_id"], "doc:d1")

    def test_save_document_carries_base(self):
        req = build_save_document_request("cid", "doc:d1", "text", "rev:1")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_SAVE)
        self.assertEqual(req["content"], "text")
        self.assertEqual(req["base_revision_id"], "rev:1")

    def test_save_document_omits_none_base(self):
        req = build_save_document_request("cid", "doc:d1", "text", None)
        self.assertNotIn("base_revision_id", req)

    def test_create_candidate(self):
        req = build_create_candidate_request("cid", "doc:d1")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_CREATE_CANDIDATE)
        self.assertEqual(req["document_id"], "doc:d1")

    def test_adopt_candidate(self):
        req = build_adopt_candidate_request("cid", "doc:d1", "cand:c1")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_ADOPT)
        self.assertEqual(req["candidate_id"], "cand:c1")

    def test_list_and_restore(self):
        req = build_list_versions_request("cid", "doc:d1")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_LIST_VERSIONS)
        req = build_restore_version_request("cid", "doc:d1", "ver:v1")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_RESTORE)
        self.assertEqual(req["version_id"], "ver:v1")

    def test_list_documents(self):
        req = build_list_documents_request("cid")
        self.assertEqual(req["action"], contract.ACTION_DOCUMENT_LIST)


class LabelTests(unittest.TestCase):
    def test_kind_labels(self):
        self.assertEqual(document_kind_label("md"), "Markdown")
        self.assertEqual(document_kind_label("txt"), "Plain text")
        self.assertEqual(document_kind_label("unknown"), "unknown")

    def test_failure_messages_are_bounded(self):
        self.assertEqual(
            document_failure_message("document_stale"),
            "The document changed since it was opened.",
        )
        self.assertEqual(
            document_failure_message("already_adopted"),
            "This candidate has already been adopted.",
        )
        self.assertEqual(
            document_failure_message("totally_unknown_code"),
            "The operation could not be completed.",
        )


class FormatterTests(unittest.TestCase):
    def _state(self):
        return {
            "document": {"document_id": "doc:d1", "name": "requirements.md", "kind": "md",
                          "head_revision_number": 1, "revision_count": 1},
            "head_revision": {"revision_id": "rev:1", "content": "hi",
                              "content_fingerprint": "f" * 64},
            "candidate": {"candidate_id": "cand:c1", "document_revision_id": "rev:1",
                          "generation_source": "deterministic_fixture", "adopted": False,
                          "limitation": "no interpretation"},
            "accepted": None,
            "current_accepted_version_id": None,
            "versions": [],
        }

    def test_format_document_state_distinguishes_records(self):
        text = format_document_state(self._state())
        self.assertIn("Working Document", text)
        self.assertIn("Candidate (not accepted behavior)", text)
        self.assertIn("no interpretation", text)
        self.assertIn("Accepted Version: none", text)

    def test_format_document_state_empty(self):
        self.assertEqual(format_document_state({}), "")

    def test_format_version_list_marks_current(self):
        versions = [
            {"version_id": "ver:1", "restore_of": None},
            {"version_id": "ver:2", "restore_of": "ver:1"},
        ]
        text = format_version_list(versions, "ver:2")
        self.assertIn("ver:1", text)
        self.assertIn("ver:2 (current) (restored)", text)

    def test_format_version_list_empty(self):
        self.assertEqual(format_version_list([], None), "No accepted versions.")


if __name__ == "__main__":
    unittest.main()
