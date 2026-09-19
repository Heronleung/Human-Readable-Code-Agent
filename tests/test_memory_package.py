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
                run_id=None, created_at=None, base=None):
        """Write a package. A backup is always built from a verified snapshot."""
        target = self.path(name)
        if profile == package.PROFILE_EXPORT:
            entries, error = package.export_entries(
                store if store is not None else self.store, run_id or self.run_id
            )
            self.assertIsNone(error)
            self.assertIsNone(package.write_package(target, profile, entries, created_at))
            return target
        snapshot, error = package.snapshot_stores(base or self.base)
        self.assertIsNone(error)
        entries, error = package.backup_entries(snapshot)
        self.assertIsNone(error)
        self.assertIsNone(
            package.write_package(target, profile, entries, created_at,
                                  snapshot=snapshot)
        )
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
        self.assertIsNone(memory_store.save(self.base, self.run_id, self.store))
        path = self.package("b.zip", package.PROFILE_BACKUP)
        members = self.members_of(path)
        name = [n for n in members if n.startswith(package.BACKUP_STORES_PREFIX)][0]
        restored = json.loads(members[name].decode("utf-8"))
        self.assertEqual(1, len(restored["corrections"]))
        self.assertEqual(1, len(restored["generated_documents"]))
        self.assertEqual(len(self.store["events"]), len(restored["events"]))


class RefusedRecordTests(PackageTestCase):
    """What the contract *refused* still backs up, and still round-trips.

    A rejection names the event the contract refused, an unsupported run names a
    terminal event that was never stored, and a quarantine names the event whose
    identity was redelivered. None of those is a reference the store owes, so a
    snapshot that demanded them would refuse to back up exactly the runs whose
    history is most worth keeping.
    """

    def refusing_store(self, session_id="s-refused"):
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": session_id, "events": [
                {"event_type": "run_started", "source_event_id": "q1", "payload": {}},
                {"event_type": "run_progress", "source_event_id": "q2",
                 "payload": {"a": 1}},
                # The same identity again with different content: quarantined.
                {"event_type": "run_progress", "source_event_id": "q2",
                 "payload": {"a": 2}},
                # A type the contract does not know: rejected, and the run goes
                # unsupported with a terminal event that was never stored.
                {"event_type": "teleported", "source_event_id": "q3", "payload": {}},
                {"event_type": "run_terminated", "source_event_id": "q4",
                 "outcome": "completed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "q5", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        return store

    def test_a_store_with_refused_records_backs_up_and_round_trips(self):
        store = self.refusing_store()
        run_id = store["agent_run"]["id"]
        base = self.path("refused-base")
        os.makedirs(base, exist_ok=True)
        self.assertIsNone(memory_store.save(base, run_id, store))

        # The store owes nothing to what it refused ...
        self.assertTrue(store["rejections"])
        self.assertTrue(store["quarantines"])
        self.assertNotIn(
            store["agent_run"]["terminal_event_id"],
            {record["id"] for record in store["events"]},
        )
        self.assertIsNone(package.verify_store(store))

        # ... so the snapshot accepts it, and the package carries it whole.
        snapshot, error = package.snapshot_stores(base)
        self.assertIsNone(error)
        self.assertEqual([run_id], [r["run_id"] for r in snapshot["runs"]])

        staging_dir, _manifest, stage_error = package.stage_package(
            self.package("refused.zip", package.PROFILE_BACKUP, base=base),
            self.path("refused-stage"),
        )
        self.assertIsNone(stage_error)
        empty = self.path("empty-refused")
        os.makedirs(empty, exist_ok=True)
        plan, plan_error = package.plan_restore(staging_dir, empty)
        self.assertIsNone(plan_error)
        self.assertEqual("create", plan["runs"][0]["action"])
        result, apply_error = package.apply_restore(
            staging_dir, empty, plan["runs"][0]["active_identity"], run_id
        )
        self.assertIsNone(apply_error)
        self.assertTrue(result["verified"])

        restored, _load = memory_store.load(empty, run_id)
        for array in ("events", "rejections", "quarantines", "decisions"):
            with self.subTest(array=array):
                self.assertEqual(
                    [record["id"] for record in store[array]],
                    [record["id"] for record in restored[array]],
                )
        self.assertEqual(package.canonical(store), package.canonical(restored))


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
        # A store written by an older schema migrates on the *staged* copy: the
        # active store it was read from is never rewritten.
        legacy = json.loads(memory.dumps(self.store))
        legacy["schema_version"] = "0.9.0"
        del legacy["code_entity_links"]
        del legacy["agent_run"]["privacy"]
        path = memory_store.run_store_path(self.base, self.run_id)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(legacy, handle, sort_keys=True)
        with open(path, "rb") as handle:
            before = handle.read()

        staging_dir, _manifest = self.staging(self.backup())
        manifest, stores, error = package.verify_staged_snapshot(staging_dir)
        self.assertIsNone(error)
        self.assertEqual(package.PROFILE_BACKUP, manifest["profile"])
        store = stores[self.run_id]
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, store["schema_version"])
        self.assertIsNone(package.verify_store(store))
        with open(path, "rb") as handle:
            self.assertEqual(before, handle.read(), "the source store was rewritten")

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
        other_base = self.path("other-base")
        os.makedirs(other_base, exist_ok=True)
        self.assertIsNone(memory_store.save(other_base, self.run_id, other))
        path = self.package("other.zip", package.PROFILE_BACKUP, base=other_base)
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


class CrossRunIndependenceTests(PackageTestCase):
    """Stores are independent documents, and that is proven rather than assumed.

    Two runs of one project share a project and work-package *id* — the id is
    derived from the adapter and the source's own identifier — but each store
    carries its own descriptor and resolves the reference locally, so sharing a
    spelling is not a dependency. Independence is what makes a per-store read a
    meaningful unit at all: a store that referenced another could not be captured
    on its own. Proving *when* the whole set was read is the snapshot's job — see
    :class:`InterleavingTests`.
    """

    def second_run(self, session_id="s-2", path="pkg/b.py"):
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": session_id, "events": [
                {"event_type": "run_started", "source_event_id": "f1", "payload": {}},
                {"event_type": "run_progress", "source_event_id": "f2", "payload": {},
                 "paths": [path]},
                {"event_type": "run_terminated", "source_event_id": "f3",
                 "outcome": "completed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "f4", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        self.assertIsNone(
            memory_store.save(self.base, store["agent_run"]["id"], store)
        )
        return store

    def test_two_runs_of_one_project_share_ids_but_not_dependencies(self):
        other = self.second_run()
        first, _error = memory_store.load(self.base, self.run_id)
        second, _error2 = memory_store.load(self.base, other["agent_run"]["id"])
        shared_projects = (
            {r["id"] for r in first["projects"]} & {r["id"] for r in second["projects"]}
        )
        shared_packages = (
            {r["id"] for r in first["work_packages"]}
            & {r["id"] for r in second["work_packages"]}
        )
        # The ids coincide ...
        self.assertTrue(shared_projects)
        self.assertTrue(shared_packages)
        # ... and each store still resolves them inside itself.
        for label, store in (("first", first), ("second", second)):
            with self.subTest(store=label):
                self.assertIsNone(package.verify_store(store))
                own = {r["id"] for r in store["projects"]} | {
                    r["id"] for r in store["work_packages"]
                }
                self.assertIn(store["agent_run"]["project_id"], own)
                self.assertIn(store["agent_run"]["work_package_id"], own)

    def test_neither_store_references_the_other_run(self):
        other = self.second_run()
        first, _error = memory_store.load(self.base, self.run_id)
        second, _error2 = memory_store.load(self.base, other["agent_run"]["id"])
        foreign = {
            record.get("id")
            for array in memory.STORE_ARRAYS
            for record in second.get(array, [])
            if isinstance(record, dict)
        } | {other["agent_run"]["id"]}
        local = {
            record.get("id")
            for array in memory.STORE_ARRAYS
            for record in first.get(array, [])
            if isinstance(record, dict)
        } | {self.run_id}
        # Nothing that only the second run owns appears anywhere in the first.
        for record in first.get("events", []):
            for key in ("evidence_ids", "decision_ids", "code_entity_link_ids"):
                for reference in record.get(key) or []:
                    self.assertNotIn(reference, foreign - local)
        self.assertFalse((foreign - local) & local)

    def test_an_escaping_reference_fails_the_independence_check(self):
        store, _error = memory_store.load(self.base, self.run_id)
        store["work_packages"][0]["project_id"] = "project:elsewhere"
        self.assertEqual(package.REASON_NOT_A_STORE, package.verify_store(store))
        self.assertEqual(
            package.REASON_CROSS_STORE,
            package.verify_independence({self.run_id: store}),
        )

    def test_a_dangling_run_scoped_reference_is_caught(self):
        for array, field in (("evidence", "run_id"), ("decisions", "run_id"),
                             ("change_sets", "run_id"),
                             ("generated_documents", "run_id")):
            with self.subTest(array=array):
                store, _error = memory_store.load(self.base, self.run_id)
                if not store.get(array):
                    continue
                store[array][0][field] = "run:elsewhere:x:run"
                self.assertEqual(package.REASON_NOT_A_STORE, package.verify_store(store))

    def test_a_dangling_quarantine_event_is_caught(self):
        store, _error = memory_store.load(self.base, self.run_id)
        store["quarantines"] = [
            {"id": "quarantine:x:1", "run_id": self.run_id, "event_id": "event:absent"}
        ]
        self.assertEqual(package.REASON_NOT_A_STORE, package.verify_store(store))

    def test_a_rejection_reference_is_not_required_to_resolve(self):
        # ``event_ref`` names the event the contract *refused*, which was by
        # definition never stored, so it is not a reference the store owes.
        store, _error = memory_store.load(self.base, self.run_id)
        store["rejections"] = [
            {"id": "rejection:x:1", "run_id": self.run_id, "reason": "x",
             "event_ref": "event:never-stored"}
        ]
        self.assertIsNone(package.verify_store(store))


class SnapshotTests(PackageTestCase):
    def second_run(self, session_id="s-2"):
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": session_id, "events": [
                {"event_type": "run_started", "source_event_id": "f1", "payload": {}},
                {"event_type": "run_terminated", "source_event_id": "f2",
                 "outcome": "completed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "f3", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        self.assertIsNone(
            memory_store.save(self.base, store["agent_run"]["id"], store)
        )
        return store["agent_run"]["id"]

    def test_one_snapshot_covers_every_run(self):
        second = self.second_run()
        snapshot, error = package.snapshot_stores(self.base)
        self.assertIsNone(error)
        self.assertEqual(package.SNAPSHOT_MODEL, snapshot["model"])
        self.assertEqual([self.run_id, second], [r["run_id"] for r in snapshot["runs"]])
        self.assertTrue(snapshot["identity"].startswith("snap:"))

    def test_the_snapshot_binds_to_the_store_bytes(self):
        snapshot, _error = package.snapshot_stores(self.base)
        store, _load = memory_store.load(self.base, self.run_id)
        run = snapshot["runs"][0]
        self.assertEqual(package.checksum_of(
            package.canonical(store).encode("utf-8")), run["identity"])

    def test_a_multi_run_backup_declares_its_snapshot(self):
        self.second_run()
        path = self.package("multi.zip", package.PROFILE_BACKUP)
        manifest = self.manifest_of(path)
        self.assertEqual(package.SNAPSHOT_MODEL, manifest["snapshot"]["model"])
        self.assertEqual(2, len(manifest["snapshot"]["runs"]))
        members = self.members_of(path)
        for run in manifest["snapshot"]["runs"]:
            with self.subTest(run=run["run_id"]):
                self.assertIn(run["entry"], members)
                self.assertEqual(
                    package.checksum_of(members[run["entry"]]), run["identity"]
                )

    def test_a_backup_without_a_snapshot_is_refused(self):
        path = self.package()
        manifest = self.manifest_of(path)
        manifest["profile"] = package.PROFILE_BACKUP
        self.assert_refused_message(
            self.rebuilt("n.zip", manifest, self.members_of(path)),
            package.REASON_SNAPSHOT_MISSING,
        )

    def assert_refused_message(self, path, reason):
        manifest, entries, error = package.read_package(path)
        self.assertIsNone(manifest)
        self.assertIsNone(entries)
        self.assertEqual(reason, error)

    def test_a_tampered_snapshot_identity_is_refused(self):
        path = self.package("b.zip", package.PROFILE_BACKUP)
        manifest = self.manifest_of(path)
        manifest["snapshot"]["identity"] = "snap:0000000000000000000000000000000"
        self.assert_refused_message(
            self.rebuilt("t.zip", manifest, self.members_of(path)),
            package.REASON_SNAPSHOT_MISMATCH,
        )

    def test_a_snapshot_that_does_not_cover_its_entries_is_refused(self):
        path = self.package("b.zip", package.PROFILE_BACKUP)
        manifest = self.manifest_of(path)
        manifest["snapshot"]["runs"][0]["entry"] = "stores/absent.json"
        self.assert_refused_message(
            self.rebuilt("u.zip", manifest, self.members_of(path)),
            package.REASON_SNAPSHOT_MISMATCH,
        )

    def test_an_export_never_declares_a_snapshot(self):
        path = self.package()
        manifest = self.manifest_of(path)
        self.assertNotIn("snapshot", manifest)
        # ...and one that does is malformed for the profile.
        manifest["snapshot"] = {"model": package.SNAPSHOT_MODEL,
                                "identity": "snap:x", "runs": []}
        self.assert_refused_message(
            self.rebuilt("e.zip", manifest, self.members_of(path)),
            package.REASON_MANIFEST_MALFORMED,
        )

    def test_a_store_that_vanished_between_reads_refuses_the_snapshot(self):
        capture, error = package.capture_stores(self.base)
        self.assertIsNone(error)
        os.remove(memory_store.run_store_path(self.base, self.run_id))
        snapshot, reason = package.verify_capture(capture)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_SNAPSHOT_UNSTABLE, reason)

    def test_an_empty_base_has_nothing_to_snapshot(self):
        empty = self.path("nothing")
        os.makedirs(empty, exist_ok=True)
        snapshot, error = package.snapshot_stores(empty)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_NO_RUNS, error)

    def test_a_store_that_cannot_be_read_refuses_the_snapshot(self):
        """An undeclared scope is never quietly narrowed to what it could read.

        A snapshot that skipped an unreadable run would be a *partial* backup
        wearing the manifest of a complete one, so the enumeration refuses
        instead — while a caller that names its runs still gets those runs.
        """
        broken = os.path.join(memory_store.memory_dir(self.base), "run_broken")
        os.makedirs(broken, exist_ok=True)
        with open(os.path.join(broken, memory_store.RUN_STORE_FILENAME), "w",
                  encoding="utf-8") as handle:
            handle.write("{ not json")
        snapshot, error = package.snapshot_stores(self.base)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_ACTIVE_UNREADABLE, error)

        named, named_error = package.snapshot_stores(self.base, [self.run_id])
        self.assertIsNone(named_error)
        self.assertEqual([self.run_id], [r["run_id"] for r in named["runs"]])

    def test_a_named_run_without_a_store_is_refused(self):
        snapshot, error = package.snapshot_stores(self.base, ["run:absent:x:run"])
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_RUN_MISSING, error)

    def test_store_content_the_snapshot_does_not_cover_is_refused(self):
        # A run dropped from the snapshot block, with the block re-signed so the
        # only thing wrong is its coverage: the package would otherwise carry a
        # store its own snapshot never vouched for.
        self.second_run()
        path = self.package("multi.zip", package.PROFILE_BACKUP)
        manifest = self.manifest_of(path)
        block = manifest["snapshot"]
        block["runs"] = block["runs"][:1]
        block["identity"] = package._snapshot_identity(block["runs"])
        self.assert_refused_message(
            self.rebuilt("uncovered.zip", manifest, self.members_of(path)),
            package.REASON_SNAPSHOT_MISMATCH,
        )

    def test_inspect_the_manifest_and_the_plan_agree_on_one_identity(self):
        # The identity a reader sees, the identity the package declares and the
        # identity recovery re-derives from the staged bytes are the same one.
        second = self.second_run()
        path = self.package("multi.zip", package.PROFILE_BACKUP)
        summary = package.inspect(path)
        self.assertEqual(package.SNAPSHOT_MODEL, summary["snapshot"]["model"])
        self.assertEqual([self.run_id, second], summary["snapshot"]["runs"])

        staging_dir, _manifest, error = package.stage_package(
            path, self.path("identity-stage")
        )
        self.assertIsNone(error)
        plan, plan_error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(plan_error)
        self.assertEqual(summary["snapshot"]["identity"], plan["snapshot"]["identity"])

        # An export never declares one, so it never reports one either.
        self.assertNotIn("snapshot", package.inspect(self.package("e.zip")))

    def test_the_snapshot_block_exposes_package_bytes_only(self):
        # The block is an allowlist, not a summary: a field naming a stored
        # source, an evidence digest or a path could not be added without this
        # test failing first.
        self.second_run()
        block = self.manifest_of(
            self.package("multi.zip", package.PROFILE_BACKUP)
        )["snapshot"]
        self.assertEqual({"model", "identity", "runs"}, set(block))
        self.assertEqual(package.SNAPSHOT_MODEL, block["model"])
        for run in block["runs"]:
            with self.subTest(run=run["run_id"]):
                self.assertEqual({"run_id", "entry", "identity"}, set(run))
                self.assertTrue(run["entry"].startswith(package.BACKUP_STORES_PREFIX))
                self.assertNotIn("source", run["identity"])
                self.assertNotIn("fingerprint", run["identity"])

    def test_the_snapshot_is_deterministic(self):
        first, _error = package.snapshot_stores(self.base)
        second, _error2 = package.snapshot_stores(self.base)
        self.assertEqual(first["identity"], second["identity"])


class InterleavingTests(PackageTestCase):
    """A write between capture and verification must refuse the snapshot.

    The interleaving is deterministic: the capture is taken through the public
    two-step seam a caller would use, a writer then runs, and verification is
    asked to confirm the capture. No thread or sleep is involved.
    """

    def second_run(self):
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": "s-2", "events": [
                {"event_type": "run_started", "source_event_id": "f1", "payload": {}},
                {"event_type": "run_terminated", "source_event_id": "f2",
                 "outcome": "completed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "f3", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        self.assertIsNone(
            memory_store.save(self.base, store["agent_run"]["id"], store)
        )
        return store["agent_run"]["id"]

    def _concurrent_write(self):
        """A writer lands a new event into the first run."""
        store, _error = memory_store.load(self.base, self.run_id)
        store["agent_run"]["state"] = memory.RUN_COMPLETED
        store["events"].append({
            "id": "event:late", "record_kind": memory.RECORD_EVENT,
            "run_id": self.run_id, "event_type": memory.EVENT_RUN_PROGRESS,
            "ingest_ordinal": 99, "payload": {},
        })
        self.assertIsNone(memory_store.save(self.base, self.run_id, store))

    def test_a_write_between_capture_and_verification_refuses_the_snapshot(self):
        self.second_run()
        capture, error = package.capture_stores(self.base)
        self.assertIsNone(error)
        self._concurrent_write()
        snapshot, reason = package.verify_capture(capture)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_SNAPSHOT_UNSTABLE, reason)

    def test_a_store_rewritten_to_the_same_bytes_still_refuses_the_snapshot(self):
        """A store that moved and came back is still a store that moved.

        This is what the storage owner's change-detector buys: content alone
        cannot tell a store that was never written from one that was replaced by
        a byte-identical earlier state, and a re-import or a restore can
        legitimately write exactly that back. Without the stamp the capture below
        would be accepted as coherent.
        """
        capture, error = package.capture_stores(self.base)
        self.assertIsNone(error)

        trimmed, _load = memory_store.load(self.base, self.run_id)
        trimmed["events"] = trimmed["events"][:1]
        self.assertIsNone(memory_store.save(self.base, self.run_id, trimmed))
        # ... and straight back to precisely what the capture read.
        self.assertIsNone(memory_store.save(self.base, self.run_id, self.store))

        current, _reload = memory_store.load(self.base, self.run_id)
        self.assertEqual(
            package.canonical(self.store), package.canonical(current),
            "the store did not actually return to its captured content",
        )
        snapshot, reason = package.verify_capture(capture)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_SNAPSHOT_UNSTABLE, reason)

    def test_a_refused_snapshot_writes_no_package(self):
        capture, _error = package.capture_stores(self.base)
        self._concurrent_write()
        snapshot, _reason = package.verify_capture(capture)
        entries, entry_error = package.backup_entries(snapshot)
        self.assertIsNone(entries)
        self.assertEqual(package.REASON_SNAPSHOT_MALFORMED, entry_error)

    def test_a_write_before_the_capture_is_simply_the_new_snapshot(self):
        self.second_run()
        self._concurrent_write()
        snapshot, error = package.snapshot_stores(self.base)
        self.assertIsNone(error)
        store, _load = memory_store.load(self.base, self.run_id)
        self.assertEqual(
            package.checksum_of(package.canonical(store).encode("utf-8")),
            [r for r in snapshot["runs"] if r["run_id"] == self.run_id][0]["identity"],
        )

    def test_a_write_between_two_runs_capture_is_detected(self):
        self.second_run()
        capture, _error = package.capture_stores(self.base)
        self._concurrent_write()
        self._concurrent_write()
        snapshot, reason = package.verify_capture(capture)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_SNAPSHOT_UNSTABLE, reason)

    def test_a_second_run_appearing_mid_capture_is_refused(self):
        # An undeclared scope *claims the store root*, so a run that appeared
        # while the capture ran makes the capture an incomplete answer to the
        # question it was asked: it is refused, never quietly narrowed to the
        # runs that happened to be read.
        capture, _error = package.capture_stores(self.base)
        self.second_run()
        snapshot, error = package.verify_capture(capture)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_RUN_SET_CHANGED, error)

    def test_a_declared_scope_ignores_runs_it_did_not_name(self):
        # A caller that names its runs has fixed the scope, so another run
        # appearing is outside it — the capture still describes the runs asked
        # for, and exactly those.
        declared, error = package.capture_stores(self.base, [self.run_id])
        self.assertIsNone(error)
        self.second_run()
        snapshot, verify_error = package.verify_capture(declared)
        self.assertIsNone(verify_error)
        self.assertEqual([self.run_id], [r["run_id"] for r in snapshot["runs"]])


class SnapshotRecoveryTests(PackageTestCase):
    def staging(self, path, name="snapstage"):
        staging_dir, manifest, error = package.stage_package(path, self.path(name))
        self.assertIsNone(error)
        return staging_dir, manifest

    def test_recovery_reverifies_the_declared_snapshot(self):
        staging_dir, manifest = self.staging(
            self.package("b.zip", package.PROFILE_BACKUP)
        )
        self.assertIn("snapshot", manifest)
        verified, stores, error = package.verify_staged_snapshot(staging_dir)
        self.assertIsNone(error)
        self.assertEqual(manifest["snapshot"]["identity"],
                         verified["snapshot"]["identity"])
        # The stores a caller may apply come from the very read that was verified.
        self.assertEqual([self.run_id], sorted(stores))

    def test_an_altered_staged_store_is_refused_before_any_plan(self):
        staging_dir, manifest = self.staging(
            self.package("b.zip", package.PROFILE_BACKUP)
        )
        entry = manifest["snapshot"]["runs"][0]["entry"]
        target = os.path.join(staging_dir, *entry.split("/"))
        with open(target, "r", encoding="utf-8") as handle:
            store = json.load(handle)
        store["agent_run"]["state"] = memory.RUN_FAILED
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(store, handle)
        before = package.canonical(memory_store.load(self.base, self.run_id)[0])
        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(plan)
        self.assertEqual(package.REASON_SNAPSHOT_MISMATCH, error)
        after = package.canonical(memory_store.load(self.base, self.run_id)[0])
        self.assertEqual(before, after)

    def test_a_removed_staged_store_is_refused(self):
        staging_dir, manifest = self.staging(
            self.package("b.zip", package.PROFILE_BACKUP)
        )
        entry = manifest["snapshot"]["runs"][0]["entry"]
        os.remove(os.path.join(staging_dir, *entry.split("/")))
        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(plan)
        self.assertEqual(package.REASON_SNAPSHOT_MISMATCH, error)

    def test_a_staged_store_the_snapshot_does_not_declare_is_refused(self):
        # Recovery restores exactly the declared snapshot, never "whatever else
        # happens to be sitting in the staging root".
        staging_dir, manifest = self.staging(
            self.package("b.zip", package.PROFILE_BACKUP)
        )
        entry = manifest["snapshot"]["runs"][0]["entry"]
        source = os.path.join(staging_dir, *entry.split("/"))
        with open(source, "rb") as handle:
            data = handle.read()
        with open(os.path.join(os.path.dirname(source), "run_extra.json"), "wb") as handle:
            handle.write(data)
        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(plan)
        self.assertEqual(package.REASON_SNAPSHOT_MISMATCH, error)

    def test_a_staged_store_naming_another_run_is_refused(self):
        # The staged store is re-bound to the *bytes* so the only thing wrong is
        # which run they name: the entry holds a store, at the identity the
        # snapshot declares, that is not the run the snapshot says it is.
        staging_dir, manifest = self.staging(
            self.package("b.zip", package.PROFILE_BACKUP)
        )
        entry = manifest["snapshot"]["runs"][0]["entry"]
        target = os.path.join(staging_dir, *entry.split("/"))
        with open(target, "r", encoding="utf-8") as handle:
            store = json.load(handle)
        store["agent_run"]["id"] = "run:elsewhere:x:run"
        payload = package.canonical(store).encode("utf-8")
        with open(target, "wb") as handle:
            handle.write(payload)

        staged = json.loads(json.dumps(manifest))
        run = staged["snapshot"]["runs"][0]
        run["identity"] = package.checksum_of(payload)
        staged["snapshot"]["identity"] = package._snapshot_identity(
            staged["snapshot"]["runs"]
        )
        with open(os.path.join(staging_dir, package.STAGED_MANIFEST_NAME), "w",
                  encoding="utf-8") as handle:
            json.dump(staged, handle)

        plan, error = package.plan_restore(staging_dir, self.base)
        self.assertIsNone(plan)
        self.assertEqual(package.REASON_SNAPSHOT_MISMATCH, error)

    def test_an_unverifiable_staging_directory_is_refused(self):
        bare = self.path("bare")
        os.makedirs(bare, exist_ok=True)
        plan, error = package.plan_restore(bare, self.base)
        self.assertIsNone(plan)
        self.assertEqual(package.REASON_SNAPSHOT_MALFORMED, error)

    def test_an_interrupted_capture_yields_no_package_to_stage(self):
        capture, _error = package.capture_stores(self.base)
        store, _load = memory_store.load(self.base, self.run_id)
        store["events"] = []
        self.assertIsNone(memory_store.save(self.base, self.run_id, store))
        snapshot, reason = package.verify_capture(capture)
        self.assertIsNone(snapshot)
        self.assertEqual(package.REASON_SNAPSHOT_UNSTABLE, reason)
        self.assertIsNone(package.backup_entries(snapshot)[0])

    def test_a_multi_run_backup_round_trips_both_runs(self):
        other, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": "s-2", "events": [
                {"event_type": "run_started", "source_event_id": "f1", "payload": {}},
                {"event_type": "run_progress", "source_event_id": "f2", "payload": {},
                 "paths": ["pkg/b.py"]},
                {"event_type": "run_terminated", "source_event_id": "f3",
                 "outcome": "failed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "f4", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        other_run = other["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, other_run, other))

        staging_dir, manifest = self.staging(
            self.package("multi.zip", package.PROFILE_BACKUP), "multistage"
        )
        empty = self.path("empty-multi")
        os.makedirs(empty, exist_ok=True)
        plan, plan_error = package.plan_restore(staging_dir, empty)
        self.assertIsNone(plan_error)
        self.assertEqual(2, len(plan["runs"]))
        for entry in plan["runs"]:
            result, apply_error = package.apply_restore(
                staging_dir, empty, entry["active_identity"], entry["run_id"]
            )
            self.assertIsNone(apply_error)
            self.assertTrue(result["verified"])
        first, _load = memory_store.load(empty, self.run_id)
        second, _load2 = memory_store.load(empty, other_run)
        self.assertEqual("completed", memory.run_state(first))
        self.assertEqual("failed", memory.run_state(second))
        self.assertEqual(
            len(self.store["events"]), len(first["events"]),
            "no duplication across the cross-run restore",
        )
        self.assertEqual(2, len(memory_store.list_runs(empty)))


    def _with_history(self, store, run_id, text):
        """Give one run its own immutable generated version and correction."""
        document = memory_docs.project_store(store)[0]["documents"]["session_summary"]
        claim = {c["id"]: c for c in document["claims"]}[CLAIM]
        revisions.record_generated_version(store, run_id, "session_summary", document)
        revisions.append_correction(
            store, run_id,
            {"document_type": "session_summary", "operation": "merge",
             "target": {"claim_id": CLAIM}, "text": text},
            revisions.claim_fingerprint(claim))
        return store

    def test_a_multi_run_round_trip_keeps_each_runs_history_apart(self):
        # Two runs, each with its own generated version and correction, restored
        # from one snapshot: every revision must come back to the run that owns
        # it, with no drift and no duplication across the pair.
        self._with_history(self.store, self.run_id, "REVIEWED-ONE")
        self.assertIsNone(memory_store.save(self.base, self.run_id, self.store))

        other, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": "s-2", "events": [
                {"event_type": "run_started", "source_event_id": "f1", "payload": {}},
                {"event_type": "run_progress", "source_event_id": "f2", "payload": {},
                 "evidence": [{"kind": "diff", "artifact_ref": "artifacts/change.patch",
                               "bytes": 4}]},
                {"event_type": "run_terminated", "source_event_id": "f3",
                 "outcome": "completed", "payload": {}},
                {"event_type": "stream_ended", "source_event_id": "f4", "payload": {}}],
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Packaging work"}})
        self.assertIsNone(error)
        other_run = other["agent_run"]["id"]
        self._with_history(other, other_run, "REVIEWED-TWO")
        self.assertIsNone(memory_store.save(self.base, other_run, other))

        staging_dir, _manifest = self.staging(
            self.package("multi.zip", package.PROFILE_BACKUP), "history-stage"
        )
        empty = self.path("empty-history")
        os.makedirs(empty, exist_ok=True)
        plan, plan_error = package.plan_restore(staging_dir, empty)
        self.assertIsNone(plan_error)
        self.assertEqual(
            [self.run_id, other_run], sorted(e["run_id"] for e in plan["runs"])
        )
        for entry in plan["runs"]:
            with self.subTest(run=entry["run_id"]):
                result, apply_error = package.apply_restore(
                    staging_dir, empty, entry["active_identity"], entry["run_id"]
                )
                self.assertIsNone(apply_error)
                self.assertTrue(result["verified"])

        restored_first, _load = memory_store.load(empty, self.run_id)
        restored_other, _load2 = memory_store.load(empty, other_run)
        # Each correction landed in its own run, and only there.
        self.assertEqual(["REVIEWED-ONE"],
                         [r["text"] for r in restored_first["corrections"]])
        self.assertEqual(["REVIEWED-TWO"],
                         [r["text"] for r in restored_other["corrections"]])
        for label, restored in (("first", restored_first), ("other", restored_other)):
            with self.subTest(run=label):
                self.assertEqual(1, len(restored["generated_documents"]))
                self.assertIsNone(package.verify_store(restored))
                # ... and each run kept every evidence link it recorded.
                recorded = [
                    reference
                    for record in restored["events"]
                    for reference in (record.get("evidence_ids") or [])
                ]
                for reference in recorded:
                    self.assertIn(
                        reference, {r["id"] for r in restored["evidence"]}
                    )


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
        # The v2b Code Twin link pair moved the protocol to 3.9.0 additively;
        # the Memory schema and this boundary's offline, actionless surface are
        # exactly as v2a left them.
        self.assertEqual("3.9.0", contract.CONTRACT_VERSION)
        self.assertEqual("1.1.0", memory.MEMORY_SCHEMA_VERSION)
        self.assertNotIn("memory_package", contract.ALLOWED_ACTIONS)


if __name__ == "__main__":
    unittest.main()
