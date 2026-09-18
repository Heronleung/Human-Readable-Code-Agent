"""Tests for the versioned export, backup and recovery boundary (M4.5/v2a).

Two profiles, deliberately kept apart: a least-disclosure shareable export built
only from allowlisted projections, and a lossless local-sensitive backup built
from the store itself. These tests pin determinism, the privacy of each profile,
hostile-archive refusal, and a recovery that cannot touch an active store until
a caller proves it is looking at the one the plan was built against.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile

from hrca import contract, memory, memory_docs
from hrca import memory_package as package
from hrca import memory_revisions as revisions
from hrca import memory_store

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"
CLAIM = "claim:session_summary:project"


def build_store(session_id="s-1"):
    store, error, _ = memory.ingest_session({
        "adapter": "smoke", "session_id": session_id, "events": [
            {"event_type": "run_started", "source_event_id": "e1",
             "payload": {"prompt": _SECRET}, "paths": [_PERSONAL]},
            {"event_type": "run_progress", "source_event_id": "e2", "payload": {},
             "decisions": [{"summary": "chose the bounded path"}]},
            {"event_type": "run_terminated", "source_event_id": "e3",
             "outcome": "completed", "payload": {}},
            {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}}],
        "project": {"source_id": "p-1", "name": "Project One"},
        "work_package": {"source_id": "w-1", "title": "Packaging work"}})
    assert error is None, error
    return store


class PackageTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="hrca-v2a-")
        self.base = os.path.join(self.work, "base")
        os.makedirs(self.base, exist_ok=True)
        self.store = build_store()
        self.run_id = self.store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, self.run_id, self.store))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.work, name)

    def package(self, name="pkg.zip", profile=package.PROFILE_EXPORT, store=None,
                run_id=None, created_at=None):
        store = store if store is not None else self.store
        run_id = run_id or self.run_id
        builder = (
            package.export_entries if profile == package.PROFILE_EXPORT
            else package.backup_entries
        )
        entries, error = builder(store, run_id)
        self.assertIsNone(error)
        target = self.path(name)
        self.assertIsNone(package.write_package(target, profile, entries, created_at))
        return target

    def export_blob(self, path):
        blob = ""
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                blob += archive.read(name).decode("utf-8", "replace")
        return blob

    def raw_archive(self, name, members, mode=None, compress=zipfile.ZIP_DEFLATED):
        """Write a hand-built archive so a hostile shape can be exercised."""
        target = self.path(name)
        with zipfile.ZipFile(target, "w", compress) as archive:
            for entry in members:
                if len(entry) == 2:
                    archive.writestr(entry[0], entry[1])
                else:
                    info = zipfile.ZipInfo(entry[0], date_time=(1980, 1, 1, 0, 0, 0))
                    if mode is not None:
                        info.external_attr = mode << 16
                    archive.writestr(info, entry[1])
        return target

    def members_of(self, path):
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def manifest_of(self, path):
        return json.loads(self.members_of(path)[package.MANIFEST_NAME].decode("utf-8"))

    def rebuilt(self, name, manifest, members):
        """Write an archive from a manifest and a member map."""
        ordered = [(package.MANIFEST_NAME, package.canonical(manifest).encode("utf-8"))]
        ordered += [(key, value) for key, value in members.items()
                    if key != package.MANIFEST_NAME]
        return self.raw_archive(name, ordered)


class NamePolicyTests(unittest.TestCase):
    def test_a_plain_relative_name_is_accepted(self):
        for name in ("PACKAGE.txt", "stores/a.json", "documents/run_x/session.json"):
            with self.subTest(name=name):
                self.assertIsNone(package.name_error(name))

    def test_a_name_outside_the_package_is_refused(self):
        for name in ("../escape.txt", "/etc/passwd", "a/../../b", "a\\b.txt",
                     "C:/x", "a//b", "./a", "", None, "x" * 300):
            with self.subTest(name=str(name)[:20]):
                self.assertEqual(package.REASON_UNSAFE_NAME, package.name_error(name))


class ExportProfileTests(PackageTestCase):
    def test_an_export_holds_allowlisted_projections(self):
        path = self.package()
        names = set(self.members_of(path))
        self.assertIn(package.NOTE_NAME, names)
        self.assertTrue(any(n.startswith(package.EXPORT_DOCUMENTS_PREFIX) for n in names))
        self.assertTrue(any(n.startswith(package.EXPORT_EFFECTIVE_PREFIX) for n in names))
        self.assertTrue(any(n.startswith(package.EXPORT_EVIDENCE_PREFIX) for n in names))

    def test_an_export_carries_no_prohibited_class(self):
        path = self.package()
        blob = self.export_blob(path)
        # A projected claim states a count ("...run_started=1..."), which is a
        # human-facing sentence, not a raw record: what must be absent is the
        # stored record itself, its payload and its internal fingerprints.
        for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh",
                          "content_fingerprint", "source_timestamp",
                          "first_source_timestamp", '"events"', '"payload"',
                          "target_hash", '"privacy"'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_an_export_keeps_what_a_reader_needs(self):
        document = memory_docs.project_store(self.store)[0]["documents"]["session_summary"]
        claim = {c["id"]: c for c in document["claims"]}[CLAIM]
        revisions.append_correction(
            self.store, self.run_id,
            {"document_type": "session_summary", "operation": "merge",
             "target": {"claim_id": CLAIM}, "text": "Reviewed by hand"},
            revisions.claim_fingerprint(claim))
        path = self.package()
        blob = self.export_blob(path)
        self.assertIn("Reviewed by hand", blob)
        self.assertIn("limitations", blob)
        self.assertIn("provenance", blob)

    def test_the_export_label_is_not_the_backup_label(self):
        export = self.manifest_of(self.package("e.zip"))
        backup = self.manifest_of(self.package("b.zip", package.PROFILE_BACKUP))
        self.assertEqual(package.PROFILE_EXPORT, export["profile"])
        self.assertEqual(package.PROFILE_BACKUP, backup["profile"])
        self.assertNotEqual(export["label"], backup["label"])
        self.assertIn("never safe to share", backup["label"])
        self.assertNotIn("never safe to share", export["label"])


class BackupProfileTests(PackageTestCase):
    def test_a_backup_holds_the_whole_store(self):
        path = self.package("b.zip", package.PROFILE_BACKUP)
        members = self.members_of(path)
        store_names = [n for n in members if n.startswith(package.BACKUP_STORES_PREFIX)]
        self.assertEqual(1, len(store_names))
        restored = json.loads(members[store_names[0]].decode("utf-8"))
        self.assertEqual(package.canonical(self.store), package.canonical(restored))

    def test_a_backup_preserves_every_record_class(self):
        document = memory_docs.project_store(self.store)[0]["documents"]["session_summary"]
        claim = {c["id"]: c for c in document["claims"]}[CLAIM]
        revisions.record_generated_version(
            self.store, self.run_id, "session_summary", document
        )
        revisions.append_correction(
            self.store, self.run_id,
            {"document_type": "session_summary", "operation": "keep",
             "target": {"claim_id": CLAIM}},
            revisions.claim_fingerprint(claim))
        path = self.package("b.zip", package.PROFILE_BACKUP)
        members = self.members_of(path)
        name = [n for n in members if n.startswith(package.BACKUP_STORES_PREFIX)][0]
        restored = json.loads(members[name].decode("utf-8"))
        self.assertEqual(1, len(restored["corrections"]))
        self.assertEqual(1, len(restored["generated_documents"]))
        self.assertEqual(len(self.store["events"]), len(restored["events"]))


class ManifestTests(PackageTestCase):
    def test_the_manifest_declares_its_own_contract(self):
        manifest = self.manifest_of(self.package())
        self.assertEqual(package.PACKAGE_SCHEMA_VERSION, manifest["package_schema_version"])
        self.assertEqual(package.PACKAGE_GENERATOR, manifest["generator"])
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, manifest["memory_schema_version"])
        self.assertEqual(manifest["entry_count"], len(manifest["entries"]))
        self.assertEqual(
            manifest["total_bytes"], sum(e["bytes"] for e in manifest["entries"])
        )
        for record in manifest["entries"]:
            with self.subTest(entry=record["name"]):
                self.assertTrue(record["checksum"].startswith("sha256:"))
                self.assertEqual(package.name_error(record["name"]), None)

    def test_checksums_attest_package_bytes_only(self):
        path = self.package()
        manifest = self.manifest_of(path)
        members = self.members_of(path)
        for record in manifest["entries"]:
            with self.subTest(entry=record["name"]):
                self.assertEqual(
                    package.checksum_of(members[record["name"]]), record["checksum"]
                )
        # No stored fingerprint from the run appears anywhere in the manifest.
        blob = package.canonical(manifest)
        for record in self.store["events"]:
            fingerprint = record.get("content_fingerprint")
            if fingerprint:
                self.assertNotIn(fingerprint, blob)

    def test_no_creation_instant_is_invented(self):
        manifest = self.manifest_of(self.package())
        self.assertIsNone(manifest["created_at"])


class DeterminismTests(PackageTestCase):
    def test_two_packagings_of_one_snapshot_are_identical(self):
        first = self.package("a.zip")
        second = self.package("b.zip")
        with open(first, "rb") as left, open(second, "rb") as right:
            self.assertEqual(left.read(), right.read())

    def test_a_declared_instant_changes_only_that_field(self):
        plain = self.manifest_of(self.package("a.zip"))
        stamped = self.manifest_of(
            self.package("b.zip", created_at="2026-09-18T00:00:00")
        )
        self.assertEqual(
            [e["checksum"] for e in plain["entries"]],
            [e["checksum"] for e in stamped["entries"]],
        )
        differing = {key for key in stamped if plain.get(key) != stamped.get(key)}
        self.assertEqual({"created_at"}, differing)

    def test_an_unusable_instant_is_refused(self):
        entries, _error = package.export_entries(self.store, self.run_id)
        for value in (7, "", "   "):
            with self.subTest(value=value):
                manifest, error = package.build_manifest(
                    package.PROFILE_EXPORT, entries, value
                )
                self.assertIsNone(manifest)
                self.assertEqual(package.REASON_MANIFEST_MALFORMED, error)

    def test_an_unknown_profile_is_refused(self):
        entries, _error = package.export_entries(self.store, self.run_id)
        manifest, error = package.build_manifest("shareable", entries)
        self.assertIsNone(manifest)
        self.assertEqual(package.REASON_PROFILE_UNSUPPORTED, error)


class HostileArchiveTests(PackageTestCase):
    def good_members(self):
        return self.members_of(self.package("good.zip"))

    def assert_refused(self, path, reason):
        manifest, entries, error = package.read_package(path)
        self.assertIsNone(manifest)
        self.assertIsNone(entries)
        self.assertEqual(reason, error)

    def test_a_traversal_name_is_refused(self):
        manifest = {"package_schema_version": package.PACKAGE_SCHEMA_VERSION}
        path = self.raw_archive("t.zip", [
            (package.MANIFEST_NAME, package.canonical(manifest).encode()),
            ("../escape.txt", b"x")])
        self.assert_refused(path, package.REASON_UNSAFE_NAME)

    def test_an_absolute_name_is_refused(self):
        path = self.raw_archive("a.zip", [
            (package.MANIFEST_NAME, b"{}"), ("/etc/passwd", b"x")])
        self.assert_refused(path, package.REASON_UNSAFE_NAME)

    def test_a_duplicate_name_is_refused(self):
        path = self.raw_archive("d.zip", [
            (package.MANIFEST_NAME, b"{}"), ("a.txt", b"x"), ("a.txt", b"y")])
        self.assert_refused(path, package.REASON_DUPLICATE_NAME)

    def test_a_case_collision_is_refused(self):
        path = self.raw_archive("c.zip", [
            (package.MANIFEST_NAME, b"{}"), ("a.txt", b"x"), ("A.TXT", b"y")])
        self.assert_refused(path, package.REASON_CASE_COLLISION)

    def test_an_undeclared_entry_is_refused(self):
        members = self.good_members()
        path = self.raw_archive("u.zip", [(k, v) for k, v in members.items()] +
                                [("extra.txt", b"x")])
        self.assert_refused(path, package.REASON_UNDECLARED_ENTRY)

    def test_a_missing_entry_is_refused(self):
        members = self.good_members()
        kept = [(k, v) for k, v in members.items()
                if k in (package.MANIFEST_NAME, package.NOTE_NAME)][:2]
        self.assert_refused(self.raw_archive("m.zip", kept), package.REASON_MISSING_ENTRY)

    def test_a_checksum_mismatch_is_refused(self):
        members = self.good_members()
        tampered = []
        for name, data in members.items():
            if name == package.NOTE_NAME:
                tampered.append((name, bytes(len(data))))  # same size, new content
            else:
                tampered.append((name, data))
        self.assert_refused(
            self.raw_archive("x.zip", tampered), package.REASON_CHECKSUM_MISMATCH
        )

    def test_a_size_mismatch_is_refused(self):
        members = self.good_members()
        tampered = [
            (name, data + b"extra" if name == package.NOTE_NAME else data)
            for name, data in members.items()
        ]
        self.assert_refused(
            self.raw_archive("s.zip", tampered), package.REASON_SIZE_MISMATCH
        )

    def test_an_unsupported_package_version_is_refused(self):
        manifest = self.manifest_of(self.package("good.zip"))
        manifest["package_schema_version"] = "9.9.9"
        self.assert_refused(
            self.rebuilt("v.zip", manifest, self.good_members()),
            package.REASON_PACKAGE_VERSION,
        )

    def test_an_unsupported_memory_schema_is_refused(self):
        manifest = self.manifest_of(self.package("good.zip"))
        manifest["memory_schema_version"] = "9.9.9"
        self.assert_refused(
            self.rebuilt("ms.zip", manifest, self.good_members()),
            package.REASON_MEMORY_SCHEMA,
        )

    def test_an_unsupported_profile_is_refused(self):
        manifest = self.manifest_of(self.package("good.zip"))
        manifest["profile"] = "shareable"
        self.assert_refused(
            self.rebuilt("p.zip", manifest, self.good_members()),
            package.REASON_PROFILE_UNSUPPORTED,
        )

    def test_a_malformed_manifest_is_refused(self):
        path = self.raw_archive("mm.zip", [(package.MANIFEST_NAME, b"{not json")])
        self.assert_refused(path, package.REASON_MANIFEST_MALFORMED)

    def test_a_package_without_a_manifest_is_refused(self):
        path = self.raw_archive("nm.zip", [("a.txt", b"x")])
        self.assert_refused(path, package.REASON_MANIFEST_MISSING)

    def test_a_link_entry_is_refused(self):
        manifest = self.manifest_of(self.package("good.zip"))
        path = self.path("l.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(package.MANIFEST_NAME, package.canonical(manifest).encode())
            info = zipfile.ZipInfo("link.txt", date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"/etc/passwd")
        self.assert_refused(path, package.REASON_LINK_ENTRY)

    def test_an_expanding_archive_is_refused(self):
        path = self.raw_archive(
            "b.zip", [(package.MANIFEST_NAME, package.canonical({}).encode()),
                      ("big.txt", b"\0" * (4 * 1024 * 1024))])
        self.assert_refused(path, package.REASON_EXPANSION)

    def test_too_many_entries_is_refused(self):
        members = [(package.MANIFEST_NAME, b"{}")]
        members += [("f%03d.txt" % index, b"x") for index in range(package.MAX_ENTRIES + 2)]
        self.assert_refused(self.raw_archive("n.zip", members), package.REASON_ENTRY_COUNT)

    def test_a_non_archive_is_refused(self):
        target = self.path("plain.txt")
        with open(target, "wb") as handle:
            handle.write(b"not an archive")
        self.assert_refused(target, package.REASON_NOT_ZIP)

    def test_a_missing_file_is_refused(self):
        self.assert_refused(self.path("absent.zip"), package.REASON_UNREADABLE)

    def test_the_boundary_refuses_a_package_neither_profile_wrote(self):
        # Our own archives always carry regular-file entries, so the link check
        # must not be what a plain archive trips over.
        members = self.good_members()
        self.assertIsNone(package.read_package(self.package("good.zip"))[2])
        path = self.raw_archive("plain.zip", list(members.items()))
        self.assertIsNone(package.read_package(path)[2])


class RecoveryTests(PackageTestCase):
    def backup(self, name="b.zip"):
        return self.package(name, package.PROFILE_BACKUP)

    def staging(self, path, name="staging"):
        root = self.path(name)
        staging_dir, manifest, error = package.stage_package(path, root)
        self.assertIsNone(error)
        return staging_dir, manifest

    def test_a_plan_changes_nothing(self):
        staging_dir, _manifest = self.staging(self.backup())
        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(error)
        self.assertEqual("identical", plan["runs"][0]["action"])
        self.assertTrue(plan["restorable"])

    def test_staging_an_export_is_possible_but_it_is_not_a_backup(self):
        staging_dir, manifest = self.staging(self.package("e.zip"))
        self.assertEqual(package.PROFILE_EXPORT, manifest["profile"])
        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(plan)
        self.assertEqual(package.REASON_NOT_A_STORE, error)

    def test_a_staged_backup_verifies_and_migrates_a_copy(self):
        staging_dir, _manifest = self.staging(self.backup())
        stores, error = package._load_backup_stores(staging_dir)
        self.assertIsNone(error)
        store = stores[self.run_id]
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, store["schema_version"])
        self.assertIsNone(package.verify_store(store))

    def test_a_backup_round_trips_into_an_empty_active_store(self):
        staging_dir, _manifest = self.staging(self.backup())
        empty = self.path("empty")
        os.makedirs(empty, exist_ok=True)
        plan, _error = package.plan_restore(staging_dir, empty)
        self.assertEqual("create", plan["runs"][0]["action"])
        result, error = package.apply_restore(
            staging_dir, empty, plan["runs"][0]["active_identity"], self.run_id
        )
        self.assertIsNone(error)
        self.assertTrue(result["verified"])
        restored, _load_error = memory_store.load(empty, self.run_id)
        self.assertEqual(package.canonical(self.store), package.canonical(restored))

    def test_a_round_trip_preserves_history_without_duplication(self):
        document = memory_docs.project_store(self.store)[0]["documents"]["session_summary"]
        claim = {c["id"]: c for c in document["claims"]}[CLAIM]
        revisions.record_generated_version(
            self.store, self.run_id, "session_summary", document
        )
        revisions.append_correction(
            self.store, self.run_id,
            {"document_type": "session_summary", "operation": "merge",
             "target": {"claim_id": CLAIM}, "text": "REVIEWED"},
            revisions.claim_fingerprint(claim))
        memory_store.save(self.base, self.run_id, self.store)

        staging_dir, _manifest = self.staging(self.backup())
        empty = self.path("empty2")
        os.makedirs(empty, exist_ok=True)
        plan, _error = package.plan_restore(staging_dir, empty)
        package.apply_restore(
            staging_dir, empty, plan["runs"][0]["active_identity"], self.run_id
        )
        restored, _load = memory_store.load(empty, self.run_id)
        self.assertEqual(1, len(restored["corrections"]))
        self.assertEqual(1, len(restored["generated_documents"]))
        self.assertEqual(
            [r["id"] for r in self.store["events"]], [r["id"] for r in restored["events"]]
        )
        self.assertEqual("completed", memory.run_state(restored))

    def test_a_stale_active_identity_is_refused_and_changes_nothing(self):
        staging_dir, _manifest = self.staging(self.backup())
        before = package.canonical(memory_store.load(self.base, self.run_id)[0])
        result, error = package.apply_restore(
            staging_dir, self.base, "not-the-identity", self.run_id
        )
        self.assertIsNone(result)
        self.assertEqual(package.REASON_ACTIVE_MISMATCH, error)
        after = package.canonical(memory_store.load(self.base, self.run_id)[0])
        self.assertEqual(before, after)

    def test_a_replacement_preserves_rollback_material(self):
        # The same run, with more in it: same identity, different content, so a
        # restore genuinely replaces rather than creating or matching.
        other, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": "s-1", "events": [
                {"event_type": "run_started", "source_event_id": "e1", "payload": {}},
                {"event_type": "run_progress", "source_event_id": "e2", "payload": {},
                 "paths": ["pkg/later.py"]},
                {"event_type": "run_terminated", "source_event_id": "e3",
                 "outcome": "completed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        self.assertEqual(self.run_id, other["agent_run"]["id"])
        path = self.package("other.zip", package.PROFILE_BACKUP, store=other,
                            run_id=self.run_id)
        staging_dir, _manifest = self.staging(path, "staging-other")
        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(error)
        self.assertEqual("replace", plan["runs"][0]["action"])
        rollback = self.path("rollback-root")
        result, apply_error = package.apply_restore(
            staging_dir, self.base, plan["runs"][0]["active_identity"], self.run_id,
            rollback,
        )
        self.assertIsNone(apply_error)
        self.assertIsNotNone(result["rollback_path"])
        self.assertTrue(os.path.isfile(result["rollback_path"]))
        with open(result["rollback_path"], "r", encoding="utf-8") as handle:
            prior = json.load(handle)
        self.assertEqual(self.run_id, prior["agent_run"]["id"])

    def test_a_run_outside_the_plan_is_refused(self):
        staging_dir, _manifest = self.staging(self.backup())
        result, error = package.apply_restore(
            staging_dir, self.base, "absent", "run:other:x:run"
        )
        self.assertIsNone(result)
        self.assertEqual(package.REASON_NOT_PLANNED, error)

    def test_verification_catches_a_dangling_reference(self):
        store = build_store("s-3")
        store["events"][0]["evidence_ids"] = ["evidence:absent"]
        error = package.verify_store(store)
        self.assertEqual(package.REASON_NOT_A_STORE, error)

    def test_verification_catches_a_dangling_correction_parent(self):
        store = build_store("s-4")
        document = memory_docs.project_store(store)[0]["documents"]["session_summary"]
        claim = {c["id"]: c for c in document["claims"]}[CLAIM]
        _record, _error, _created = revisions.append_correction(
            store, store["agent_run"]["id"],
            {"document_type": "session_summary", "operation": "keep",
             "target": {"claim_id": CLAIM}},
            revisions.claim_fingerprint(claim))
        store["corrections"][0]["supersedes"] = ["correction:absent"]
        self.assertEqual(package.REASON_NOT_A_STORE, package.verify_store(store))

    def test_a_refused_package_leaves_the_active_store_alone(self):
        before = package.canonical(memory_store.load(self.base, self.run_id)[0])
        staging_dir, _manifest, error = package.stage_package(
            self.path("absent.zip"), self.path("staging-x")
        )
        self.assertIsNone(staging_dir)
        self.assertEqual(package.REASON_UNREADABLE, error)
        after = package.canonical(memory_store.load(self.base, self.run_id)[0])
        self.assertEqual(before, after)


class CliTests(PackageTestCase):
    def run_cli(self, argv):
        from hrca import memory_package_cli as cli
        return cli.main(argv)

    def test_export_backup_inspect_and_recover(self):
        export = self.path("cli-export.zip")
        self.assertEqual(0, self.run_cli([
            "export", "--base", self.base, "--run", self.run_id, "--out", export]))
        backup = self.path("cli-backup.zip")
        self.assertEqual(0, self.run_cli([
            "backup", "--base", self.base, "--run", self.run_id, "--out", backup]))
        self.assertEqual(0, self.run_cli(["inspect", backup]))
        self.assertEqual(0, self.run_cli([
            "recover", "--package", backup, "--active", self.base,
            "--staging", self.path("cli-staging")]))

    def test_a_refused_package_exits_non_zero(self):
        bad = self.path("bad.zip")
        with open(bad, "wb") as handle:
            handle.write(b"nope")
        self.assertEqual(1, self.run_cli(["inspect", bad]))

    def test_an_export_cannot_be_recovered(self):
        export = self.path("cli-export2.zip")
        self.run_cli(["export", "--base", self.base, "--run", self.run_id,
                      "--out", export])
        self.assertEqual(1, self.run_cli([
            "recover", "--package", export, "--active", self.base,
            "--staging", self.path("cli-staging2")]))

    def test_applying_without_the_plan_identity_is_refused(self):
        backup = self.path("cli-backup2.zip")
        self.run_cli(["backup", "--base", self.base, "--run", self.run_id,
                      "--out", backup])
        hard = self.path("cli-fresh")
        os.makedirs(hard, exist_ok=True)
        self.assertEqual(1, self.run_cli([
            "recover", "--package", backup, "--active", hard,
            "--staging", self.path("cli-staging3"), "--apply",
            "--expected", "wrong", "--run", self.run_id]))


class BoundaryTests(PackageTestCase):
    def test_the_package_module_reaches_nothing_it_should_not(self):
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        source = os.path.normpath(
            os.path.join(here, "..", "src", "hrca", "memory_package.py")
        )
        with open(source, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
        for forbidden in ("socket", "http", "urllib", "ssl", "requests",
                          "subprocess", "client", "style", "credential_store",
                          "deepseek", "boundary"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imported)

    def test_no_clock_is_consulted(self):
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        source = os.path.normpath(
            os.path.join(here, "..", "src", "hrca", "memory_package.py")
        )
        with open(source, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
        self.assertNotIn("datetime", imported)
        self.assertNotIn("time", imported)

    def test_the_contract_and_schema_are_unchanged(self):
        self.assertEqual("3.8.0", contract.CONTRACT_VERSION)
        self.assertEqual("1.1.0", memory.MEMORY_SCHEMA_VERSION)
        self.assertNotIn("memory_package", contract.ALLOWED_ACTIONS)


if __name__ == "__main__":
    unittest.main()
