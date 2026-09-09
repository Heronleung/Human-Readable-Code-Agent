"""Tests for the P4.4 document/version-authority domain (:mod:`hrca.document`)."""

from __future__ import annotations

import unittest

from hrca import app_package, document

_NOW = "2026-09-09T00:00:00+00:00"
_PACKAGE = app_package.quotation_reference_package()
_RUNTIME = app_package.RUNNER_IDENTITY

# Exact content that must round-trip byte-for-byte, including Traditional
# Chinese and English.
_MIXED = (
    "# 需求文件 (Requirements)\n\n"
    "Traditional Chinese: 繁體中文 — 已儲存。\n"
    "English: hello, world.\n"
)


def _store_with_revision(content: str = _MIXED, revision_id: str = "rev:r1") -> dict:
    store = document.new_document_store("doc:d1", "requirements.md", document.KIND_MARKDOWN, _NOW)
    new_store, revision = document.save_revision(store, content, None, _NOW)
    if revision_id != "rev:r1":
        new_store["head_revision_id"] = revision_id
        new_store["revisions"][0]["revision_id"] = revision_id
    return new_store


class MigrateTests(unittest.TestCase):
    def test_current_version_round_trips(self):
        store = _store_with_revision()
        migrated, err = document.migrate_document(store)
        self.assertIsNone(err)
        self.assertEqual(migrated, store)

    def test_future_version_rejected(self):
        store = _store_with_revision()
        store["schema_version"] = "99.0.0"
        migrated, err = document.migrate_document(store)
        self.assertIsNone(migrated)
        self.assertEqual(err, document.REASON_FUTURE_VERSION)

    def test_missing_version_rejected(self):
        store = _store_with_revision()
        del store["schema_version"]
        migrated, err = document.migrate_document(store)
        self.assertIsNone(migrated)
        self.assertEqual(err, document.REASON_MISSING_VERSION)

    def test_non_mapping_rejected(self):
        migrated, err = document.migrate_document(["not", "a", "mapping"])
        self.assertIsNone(migrated)
        self.assertEqual(err, document.REASON_NOT_MAPPING)


class NameKindTests(unittest.TestCase):
    def test_kind_md(self):
        self.assertEqual(document.kind_for_name("requirements.md"), document.KIND_MARKDOWN)

    def test_kind_txt(self):
        self.assertEqual(document.kind_for_name("notes.txt"), document.KIND_TEXT)

    def test_kind_none(self):
        self.assertIsNone(document.kind_for_name("notes.py"))

    def test_valid_name(self):
        self.assertTrue(document.valid_name("requirements.md"))
        self.assertTrue(document.valid_name("  notes.txt  "))
        self.assertFalse(document.valid_name("notes.py"))
        self.assertFalse(document.valid_name(""))
        self.assertFalse(document.valid_name("x" * (document.MAX_DOCUMENT_NAME_CHARS + 1) + ".md"))


class NormalizeNameTests(unittest.TestCase):
    """P4.5a: the Windows-safe, case-insensitive name policy."""

    def test_trims_surrounding_whitespace(self):
        self.assertEqual(document.normalize_name("  notes.txt  "), "notes.txt")
        self.assertEqual(document.normalize_name("req.md"), "req.md")

    def test_rejects_blank(self):
        self.assertIsNone(document.normalize_name(""))
        self.assertIsNone(document.normalize_name("   "))
        self.assertIsNone(document.normalize_name(None))

    def test_rejects_unsupported_extension(self):
        self.assertIsNone(document.normalize_name("notes.py"))
        self.assertIsNone(document.normalize_name("notes"))

    def test_rejects_path_separators(self):
        self.assertIsNone(document.normalize_name("dir/notes.md"))
        self.assertIsNone(document.normalize_name("dir\\notes.txt"))

    def test_rejects_traversal(self):
        self.assertIsNone(document.normalize_name(".."))
        self.assertIsNone(document.normalize_name("."))

    def test_rejects_reserved_windows_names(self):
        for name in ("CON.md", "con.txt", "LPT1.md", "nul.txt", "aux.md"):
            with self.subTest(name=name):
                self.assertIsNone(document.normalize_name(name))

    def test_rejects_overlong(self):
        self.assertIsNone(document.normalize_name("x" * 121 + ".md"))

    def test_name_key_is_case_insensitive(self):
        self.assertEqual(document.name_key("Requirement.md"), "requirement.md")
        self.assertEqual(document.name_key("requirement.MD"), "requirement.md")
        self.assertEqual(document.name_key("  Requirement.md  "), "requirement.md")

    def test_name_key_none_for_invalid(self):
        self.assertIsNone(document.name_key(""))
        self.assertIsNone(document.name_key(None))


class SaveRevisionTests(unittest.TestCase):
    def test_exact_content_round_trips(self):
        store = document.new_document_store("doc:d1", "requirements.md", "md", _NOW)
        new_store, revision = document.save_revision(store, _MIXED, None, _NOW)
        self.assertIsNotNone(new_store)
        self.assertEqual(revision["content"], _MIXED)
        self.assertEqual(
            revision["content_fingerprint"],
            document.sha256_hex(_MIXED.encode("utf-8")),
        )
        self.assertEqual(revision["byte_size"], len(_MIXED.encode("utf-8")))
        self.assertEqual(revision["revision_number"], 1)
        self.assertEqual(revision["origin"], document.ORIGIN_USER_SAVED)
        self.assertEqual(new_store["head_revision_id"], revision["revision_id"])

    def test_save_is_immutable_append(self):
        store = _store_with_revision()
        head_before = store["head_revision_id"]
        new_store, revision = document.save_revision(store, "second", head_before, _NOW)
        self.assertIsNotNone(new_store)
        self.assertEqual(len(new_store["revisions"]), 2)
        # The original store is unchanged (immutability at the caller boundary).
        self.assertEqual(store["head_revision_id"], head_before)
        self.assertEqual(len(store["revisions"]), 1)
        self.assertEqual(revision["parent_revision_id"], head_before)
        self.assertEqual(revision["revision_number"], 2)

    def test_save_stale_base_refused(self):
        store = _store_with_revision()
        new_store, revision = document.save_revision(store, "new", "rev:WRONG", _NOW)
        self.assertIsNone(new_store)
        self.assertEqual(revision, document.REASON_STALE)

    def test_save_non_string_content_refused(self):
        store = _store_with_revision()
        new_store, revision = document.save_revision(store, 123, store["head_revision_id"], _NOW)
        self.assertIsNone(new_store)
        self.assertEqual(revision, document.REASON_CONTENT_INVALID)


class CandidateTests(unittest.TestCase):
    def test_candidate_is_deterministic(self):
        store = _store_with_revision()
        c1, err1 = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        c2, err2 = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual(c1["candidate_id"], c2["candidate_id"])

    def test_candidate_binds_head_revision_and_fixture(self):
        store = _store_with_revision()
        candidate, err = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        self.assertIsNone(err)
        head = store["head_revision_id"]
        self.assertEqual(candidate["document_revision_id"], head)
        self.assertEqual(candidate["document_fingerprint"], document.sha256_hex(_MIXED.encode("utf-8")))
        self.assertEqual(candidate["package_id"], "quotation-rules")
        self.assertEqual(candidate["runtime_identity"], _RUNTIME)
        self.assertEqual(candidate["generation_source"], document.SOURCE_DETERMINISTIC_FIXTURE)
        self.assertIn("no document-to-code interpretation", candidate["limitation"])
        self.assertIsNone(candidate["accepted_predecessor_id"])
        self.assertFalse(candidate["adopted"])

    def test_candidate_requires_saved_revision(self):
        store = document.new_document_store("doc:d1", "requirements.md", "md", _NOW)
        candidate, err = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        self.assertIsNone(candidate)
        self.assertEqual(err, document.REASON_NO_REVISION)


class AdoptionTests(unittest.TestCase):
    def _adopt(self, store, candidate_id):
        return document.adopt_candidate(
            store, candidate_id, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW
        )

    def test_adopt_succeeds_and_moves_pointer(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(candidate)
        new_store, version = self._adopt(store, candidate["candidate_id"])
        self.assertIsNotNone(new_store)
        self.assertEqual(version["candidate_id"], candidate["candidate_id"])
        self.assertEqual(new_store["current_accepted_version_id"], version["version_id"])
        self.assertIsNone(version["restore_of"])
        # The candidate is marked adopted in the new store only.
        adopted = next(c for c in new_store["candidates"] if c["candidate_id"] == candidate["candidate_id"])
        self.assertTrue(adopted["adopted"])
        self.assertFalse(store["candidates"][0]["adopted"])

    def test_adopt_repeated_refused(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(candidate)
        new_store, _ = self._adopt(store, candidate["candidate_id"])
        self.assertIsNotNone(new_store)
        new_store, reason = self._adopt(new_store, candidate["candidate_id"])
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_ALREADY_ADOPTED)

    def test_adopt_stale_after_document_edit(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(candidate)
        # The document advances a revision; the candidate is now stale.
        edited, _ = document.save_revision(store, "edited content", store["head_revision_id"], _NOW)
        edited["candidates"].append(candidate)
        new_store, reason = self._adopt(edited, candidate["candidate_id"])
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_CANDIDATE_STALE)

    def test_adopt_baseline_mismatch_refused(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(candidate)
        # The accepted baseline moved since the candidate was created.
        store["current_accepted_version_id"] = "ver:someone-else"
        new_store, reason = self._adopt(store, candidate["candidate_id"])
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_BASELINE_MISMATCH)

    def test_adopt_corrupt_manifest_refused(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        # Tamper a binding field without recomputing the id.
        candidate["document_fingerprint"] = "0" * 64
        store["candidates"].append(candidate)
        new_store, reason = self._adopt(store, candidate["candidate_id"])
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_CORRUPT)

    def test_adopt_missing_candidate_refused(self):
        store = _store_with_revision()
        new_store, reason = self._adopt(store, "cand:missing")
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_CANDIDATE_NOT_FOUND)

    def test_adopt_failed_evidence_refused(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(candidate)
        # A changed (now-invalid) package snapshot fails the evidence check.
        bad_package = dict(_PACKAGE)
        bad_package["package_id"] = "quotation-rules"
        bad_package["runtime"] = "hrca-runner:v0"
        new_store, reason = document.adopt_candidate(
            store, candidate["candidate_id"], bad_package, _RUNTIME,
            document.VALIDATION_IDENTITY, _NOW,
        )
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_EVIDENCE_FAILED)


class RestoreTests(unittest.TestCase):
    def _adopt_sequence(self):
        """Adopt two candidates, yielding ``(store, v1, v2)`` with v2 current."""
        store = _store_with_revision()
        c1, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(c1)
        store, v1 = document.adopt_candidate(
            store, c1["candidate_id"], _PACKAGE, _RUNTIME,
            document.VALIDATION_IDENTITY, _NOW,
        )
        c2, _ = document.build_candidate(store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW)
        store["candidates"].append(c2)
        store, v2 = document.adopt_candidate(
            store, c2["candidate_id"], _PACKAGE, _RUNTIME,
            document.VALIDATION_IDENTITY, _NOW,
        )
        return store, v1, v2

    def test_restore_preserves_working_document(self):
        store, v1, _v2 = self._adopt_sequence()
        # A newer working revision exists (unsaved-to-accepted work).
        store, _ = document.save_revision(store, "newer working text", store["head_revision_id"], _NOW)
        head_before = store["head_revision_id"]
        revisions_before = len(store["revisions"])
        new_store, restored = document.restore_version(store, v1["version_id"], _NOW)
        self.assertIsNotNone(new_store)
        # Working document untouched.
        self.assertEqual(new_store["head_revision_id"], head_before)
        self.assertEqual(len(new_store["revisions"]), revisions_before)
        # Accepted pointer moved to a new restore record pointing at v1.
        self.assertEqual(restored["restore_of"], v1["version_id"])
        self.assertEqual(new_store["current_accepted_version_id"], restored["version_id"])

    def test_restore_current_is_noop(self):
        store, _v1, v2 = self._adopt_sequence()
        new_store, version = document.restore_version(store, v2["version_id"], _NOW)
        self.assertEqual(new_store["current_accepted_version_id"], v2["version_id"])
        self.assertEqual(version["version_id"], v2["version_id"])

    def test_restore_missing_refused(self):
        store = _store_with_revision()
        new_store, reason = document.restore_version(store, "ver:missing", _NOW)
        self.assertIsNone(new_store)
        self.assertEqual(reason, document.REASON_VERSION_NOT_FOUND)


class PreviewStateTests(unittest.TestCase):
    """P4.5: the version-bound preview derivation is bounded and honest."""

    def _preview(self, store):
        return document.preview_state(store, _PACKAGE)

    def _candidate_store(self):
        store = _store_with_revision()
        candidate, _ = document.build_candidate(
            store, _PACKAGE, _RUNTIME, document.VALIDATION_IDENTITY, _NOW
        )
        store["candidates"].append(candidate)
        return store, candidate

    def test_empty_store_is_no_document(self):
        store = document.new_document_store("doc:d1", "requirements.md", document.KIND_MARKDOWN, _NOW)
        preview = self._preview(store)
        self.assertEqual(preview["state"], document.PREVIEW_STATE_NO_DOCUMENT)
        self.assertIsNone(preview["binding"])
        self.assertIsNone(preview["provenance"])
        self.assertIsNone(preview["limitation"])
        self.assertIsNone(preview["document"]["revision_id"])

    def test_saved_without_candidate_is_no_candidate(self):
        preview = self._preview(_store_with_revision())
        self.assertEqual(preview["state"], document.PREVIEW_STATE_NO_CANDIDATE)
        self.assertIsNone(preview["binding"])

    def test_current_candidate(self):
        store, candidate = self._candidate_store()
        preview = self._preview(store)
        self.assertEqual(preview["state"], document.PREVIEW_STATE_CURRENT)
        self.assertEqual(preview["binding"]["kind"], document.PREVIEW_KIND_CANDIDATE)
        self.assertEqual(preview["binding"]["record_id"], candidate["candidate_id"])
        self.assertEqual(preview["provenance"], document.SOURCE_DETERMINISTIC_FIXTURE)
        self.assertIn("no document-to-code", preview["limitation"])

    def test_stale_after_edit(self):
        store, _ = self._candidate_store()
        store, _ = document.save_revision(store, "changed", store["head_revision_id"], _NOW)
        self.assertEqual(self._preview(store)["state"], document.PREVIEW_STATE_STALE)

    def test_invalid_corrupt_candidate(self):
        store, _ = self._candidate_store()
        store["candidates"][-1]["document_fingerprint"] = "0" * 64
        self.assertEqual(self._preview(store)["state"], document.PREVIEW_STATE_INVALID)

    def test_insufficient_evidence_mismatched_package(self):
        store, _ = self._candidate_store()
        other_package = dict(_PACKAGE, package_id="other-package")
        preview = document.preview_state(store, other_package)
        self.assertEqual(preview["state"], document.PREVIEW_STATE_INSUFFICIENT_EVIDENCE)

    def test_accepted_version_is_distinct(self):
        store, candidate = self._candidate_store()
        store, version = document.adopt_candidate(
            store, candidate["candidate_id"], _PACKAGE, _RUNTIME,
            document.VALIDATION_IDENTITY, _NOW,
        )
        preview = self._preview(store)
        self.assertEqual(preview["binding"]["kind"], document.PREVIEW_KIND_ACCEPTED)
        self.assertEqual(preview["binding"]["record_id"], version["version_id"])
        self.assertTrue(preview["binding"]["adopted"])
        self.assertEqual(preview["state"], document.PREVIEW_STATE_CURRENT)

    def test_accepted_version_stale_after_edit(self):
        store, candidate = self._candidate_store()
        store, _ = document.adopt_candidate(
            store, candidate["candidate_id"], _PACKAGE, _RUNTIME,
            document.VALIDATION_IDENTITY, _NOW,
        )
        store, _ = document.save_revision(store, "changed again", store["head_revision_id"], _NOW)
        preview = self._preview(store)
        self.assertEqual(preview["binding"]["kind"], document.PREVIEW_KIND_ACCEPTED)
        self.assertEqual(preview["state"], document.PREVIEW_STATE_STALE)

    def test_preview_never_carries_document_content(self):
        store, _ = self._candidate_store()
        preview = self._preview(store)
        self.assertNotIn("content", preview["document"])
        self.assertNotIn(_MIXED, document.dumps(preview))

    def test_package_form_and_result_are_bounded(self):
        store, _ = self._candidate_store()
        preview = self._preview(store)
        self.assertEqual(
            [f["name"] for f in preview["package"]["form"]],
            ["subtotal", "member", "region"],
        )
        self.assertEqual(
            [f["name"] for f in preview["package"]["result"]],
            ["discount", "shipping_fee", "regional_fee", "total"],
        )
        self.assertTrue(preview["evidence"]["package_validates"])
        self.assertFalse(preview["evidence"]["execution_performed"])


if __name__ == "__main__":
    unittest.main()
