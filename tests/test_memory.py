"""Contract tests for the offline Developer Memory domain (M4.1).

These tests exercise :mod:`hrca.memory` — the Qt-free, dependency-free domain —
with no filesystem, network, provider, credential or model access. They cover
the entity/identity contract, deterministic normalization, the pre-storage
privacy boundary and additive schema migration.
"""

from __future__ import annotations

import json
import unittest

from hrca import memory as m


def _session(session_id: str, events, **overrides):
    session = {"adapter": "fixture", "session_id": session_id, "events": events}
    session.update(overrides)
    return session


def _run(session_id: str, events, **overrides):
    store, err, _ = m.ingest_session(_session(session_id, events, **overrides))
    assert err is None, err
    return store


class IdentityTests(unittest.TestCase):
    """Identity is namespaced by adapter and session, and is stable."""

    def test_run_id_namespaces_adapter_and_session(self):
        self.assertEqual(
            m.run_id_for("Fixture", "Sess-1", "Run-A"),
            "run:fixture:sess-1:run-a",
        )

    def test_run_id_refuses_an_unusable_adapter_or_session(self):
        self.assertIsNone(m.run_id_for("", "s", None))
        self.assertIsNone(m.run_id_for("a", "   ", None))
        self.assertIsNone(m.run_id_for(None, "s", None))

    def test_two_adapters_never_collide_on_the_same_session_id(self):
        left = _run("s-1", [{"event_type": "run_started", "source_event_id": "e"}])
        other = m.ingest_session(
            _session("s-1", [{"event_type": "run_started", "source_event_id": "e"}])
        )[0]
        other["agent_run"]["adapter"] = "other"
        self.assertNotEqual(m.run_id_for("a", "s-1"), m.run_id_for("b", "s-1"))
        self.assertEqual(m.run_state(left), "missing_terminal")

    def test_a_raw_adapter_string_cannot_inject_an_id_separator(self):
        self.assertEqual(m.run_id_for("a/b", "c:d", "e"), "run:a-b:c-d:e")

    def test_event_identity_prefers_the_stable_source_id(self):
        """A redelivery carrying corrected content keeps the same identity."""
        first = m.event_id_for("run:x", {"source_event_id": "e-1"}, {"message": "one"})
        second = m.event_id_for("run:x", {"source_event_id": "e-1"}, {"message": "two"})
        self.assertEqual(first, second)

    def test_event_identity_falls_back_to_the_source_sequence(self):
        first = m.event_id_for("run:x", {"source_sequence": 7}, {"message": "one"})
        second = m.event_id_for("run:x", {"source_sequence": 7}, {"message": "two"})
        other = m.event_id_for("run:x", {"source_sequence": 8}, {"message": "one"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_event_identity_falls_back_to_a_non_secret_fingerprint(self):
        first = m.event_id_for("run:x", {"event_type": "run_progress"}, {"message": "one"})
        same = m.event_id_for("run:x", {"event_type": "run_progress"}, {"message": "one"})
        other = m.event_id_for("run:x", {"event_type": "run_progress"}, {"message": "two"})
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)

    def test_a_secret_cannot_influence_identity(self):
        """Identity is computed over the already redacted payload."""
        secret = "token=sk-EXAMPLESYNTHETICKEY0123456789"
        first = m.event_id_for("run:x", {"event_type": "run_progress"}, {"message": secret})
        second = m.event_id_for("run:x", {"event_type": "run_progress"}, {"message": secret})
        self.assertEqual(first, second)
        self.assertNotIn("EXAMPLESYNTHETICKEY", first)

    def test_identifiers_are_stable_across_recomputation(self):
        first = _run("s-1", [{"event_type": "run_started", "source_event_id": "e-1"}])
        second = _run("s-1", [{"event_type": "run_started", "source_event_id": "e-1"}])
        self.assertEqual(m.event_ids(first), m.event_ids(second))
        self.assertEqual(m.evidence_ids(first), m.evidence_ids(second))


class SerializationTests(unittest.TestCase):
    def test_dumps_is_canonical_and_ascii_safe(self):
        store = _run("s-1", [{"event_type": "run_started", "source_event_id": "e",
                              "message": "café"}])
        text = m.dumps(store)
        self.assertNotIn("\n", text)
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text), store)

    def test_identical_reimports_are_byte_identical(self):
        session = _session(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2", "message": "x"},
                {"event_type": "run_terminated", "source_event_id": "e-3",
                 "outcome": "completed"},
            ],
        )
        first = m.dumps(m.ingest_session(session)[0])
        second = m.dumps(m.ingest_session(session)[0])
        third = m.dumps(m.ingest_session(session)[0])
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_arrays_are_sorted_for_determinism(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2",
                 "code_entities": [{"path": "src/z.py"}, {"path": "src/a.py"}]},
                {"event_type": "run_terminated", "source_event_id": "e-3",
                 "outcome": "completed"},
            ],
        )
        ids = [r["id"] for r in store["code_entity_links"]]
        self.assertEqual(ids, sorted(ids))

    def test_directory_of_record_kinds_is_complete(self):
        store = _run("s-1", [{"event_type": "run_started", "source_event_id": "e"}])
        kinds = {store["agent_run"]["record_kind"]}
        for array in m.STORE_ARRAYS:
            kinds.update(r["record_kind"] for r in store[array])
        self.assertTrue(kinds <= m.RECORD_KINDS)


class RedactionTests(unittest.TestCase):
    """Positive and negative privacy controls on secret-like values."""

    POSITIVE = (
        "sk-EXAMPLESYNTHETICKEY0123456789",
        "sk-ant-EXAMPLESYNTHETICKEY0123456789",
        "ghp_EXAMPLESYNTHETICTOKEN0123456789",
        "github_pat_EXAMPLESYNTHETICTOKEN0123456789",
        "xoxb-EXAMPLE-SYNTHETIC-TOKEN",
        "AKIAIOSFODNN7EXAMPLE",
        "token=EXAMPLESYNTHETICVALUE",
        "password: EXAMPLESYNTHETICVALUE",
        "api_key = EXAMPLESYNTHETICVALUE",
        "Authorization: Bearer EXAMPLESYNTHETICTOKEN",
        "https://user:EXAMPLESYNTHETIC@example.invalid/x",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEXAMPLESYNTHETIC\n"
        "-----END RSA PRIVATE KEY-----",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU",
    )

    NEGATIVE = (
        "the build succeeded and the tests passed",
        "src/app/main.py",
        "sha256:0000000000000000000000000000000000000000000000000000000000000001",
        "2026-01-01T09:00:00Z",
        "",
    )

    def test_secret_like_values_are_redacted(self):
        for value in self.POSITIVE:
            with self.subTest(value=value):
                self.assertIn(m.REDACTION_MARKER, m.redact_text(value))

    def test_ordinary_values_are_not_redacted(self):
        for value in self.NEGATIVE:
            with self.subTest(value=value):
                self.assertNotIn(m.REDACTION_MARKER, m.redact_text(value))

    def test_redaction_is_idempotent(self):
        for value in self.POSITIVE:
            with self.subTest(value=value):
                once = m.redact_text(value)
                self.assertEqual(once, m.redact_text(once))

    def test_redaction_preserves_the_key_name_only(self):
        self.assertEqual(
            m.redact_text("password: hunter2"), "password: " + m.REDACTION_MARKER
        )

    def test_free_text_is_redacted_before_it_reaches_the_store(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1",
                 "message": "using token=sk-EXAMPLESYNTHETICKEY0123456789"},
                {"event_type": "run_terminated", "source_event_id": "e-2",
                 "outcome": "completed"},
            ],
        )
        blob = m.dumps(store)
        self.assertNotIn("EXAMPLESYNTHETICKEY", blob)
        self.assertIn(m.REDACTION_MARKER, blob)


class PathPolicyTests(unittest.TestCase):
    EXCLUDED = (
        ".env",
        ".env.local",
        "secrets/prod.yaml",
        "config/server.pem",
        ".git/config",
        "node_modules/pkg/index.js",
        "keys/id_rsa",
        ".ssh/config",
        "app/credentials.json",
        "src/__pycache__/x.pyc",
        "private/notes.txt",
    )
    PERMITTED = (
        "src/app/main.py",
        "README.md",
        "logs/session.jsonl",
        "app/service.py",
        "tests/test_memory.py",
    )

    def test_excluded_paths_are_refused(self):
        for path in self.EXCLUDED:
            with self.subTest(path=path):
                self.assertTrue(m.path_excluded(path))

    def test_permitted_paths_are_kept(self):
        for path in self.PERMITTED:
            with self.subTest(path=path):
                self.assertFalse(m.path_excluded(path))

    def test_policy_is_case_and_separator_agnostic(self):
        self.assertTrue(m.path_excluded("Secrets\\Prod.YAML"))
        self.assertTrue(m.path_excluded("./.ENV"))
        self.assertFalse(m.path_excluded("SRC\\App\\Main.py"))

    def test_empty_or_non_string_paths_are_refused(self):
        for value in ("", "   ", None, 7, []):
            with self.subTest(value=value):
                self.assertTrue(m.path_excluded(value))

    def test_excluded_references_are_dropped_from_the_store(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2",
                 "change_set": {"paths": [".env", "src/ok.py", "secrets/x.yaml"]},
                 "evidence": [{"artifact_ref": ".env"}, {"artifact_ref": "logs/ok.jsonl"}],
                 "code_entities": [{"path": ".env", "symbol": "a.b"},
                                   {"path": "src/ok.py", "symbol": "a.b"}]},
                {"event_type": "run_terminated", "source_event_id": "e-3",
                 "outcome": "completed"},
            ],
        )
        blob = m.dumps(store)
        self.assertNotIn(".env", blob)
        self.assertNotIn("secrets/", blob)
        self.assertEqual(store["change_sets"][0]["paths"], ["src/ok.py"])
        self.assertEqual([e["artifact_ref"] for e in store["evidence"]], ["logs/ok.jsonl"])
        self.assertEqual([l["path"] for l in store["code_entity_links"]], ["src/ok.py"])


class PayloadBoundTests(unittest.TestCase):
    def test_content_bearing_keys_are_never_stored(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2",
                 "evidence": [{"artifact_ref": "logs/ok.jsonl",
                               "content": "RAWTRANSCRIPTCONTENT"},
                              {"artifact_ref": "logs/two.jsonl",
                               "transcript": "RAWTRANSCRIPTCONTENT"}]},
                {"event_type": "run_terminated", "source_event_id": "e-3",
                 "outcome": "completed"},
            ],
        )
        self.assertNotIn("RAWTRANSCRIPTCONTENT", m.dumps(store))
        # The references survive; only the content is dropped. The evidence
        # array is id-sorted, so compare as a set.
        self.assertEqual(
            {e["artifact_ref"] for e in store["evidence"]},
            {"logs/ok.jsonl", "logs/two.jsonl"},
        )
        self.assertGreaterEqual(store["agent_run"]["privacy"]["exclusions"], 2)

    def test_oversized_text_is_truncated_and_counted(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2",
                 "message": "x" * (m.MAX_TEXT_CHARS + 500)},
            ],
        )
        event = [e for e in store["events"] if e["source_event_id"] == "e-2"][0]
        self.assertEqual(len(event["payload"]["message"]), m.MAX_TEXT_CHARS)
        self.assertEqual(event["privacy"]["truncations"], 1)

    def test_an_oversized_payload_is_rejected_before_it_is_stored(self):
        big = {"k%03d" % i: "v" * 1024 for i in range(32)}
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2", "payload": big},
                {"event_type": "run_terminated", "source_event_id": "e-3",
                 "outcome": "completed"},
            ],
        )
        self.assertEqual(len(store["events"]), 2)
        self.assertIn(
            m.REASON_PAYLOAD_OVERSIZED,
            [r["reason"] for r in store["rejections"]],
        )
        self.assertNotIn("vvvvvvvvvv", m.dumps(store))
        # The rejection does not change the run's validated terminal state.
        self.assertEqual(m.run_state(store), "completed")

    def test_a_tightened_policy_bound_is_respected(self):
        policy = m.PrivacyPolicy(max_payload_bytes=256)
        store, err, _ = m.ingest_session(
            _session(
                "s-1",
                [
                    {"event_type": "run_started", "source_event_id": "e-1",
                     "payload": {"k": "v" * 400}},
                ],
            ),
            policy,
        )
        self.assertIsNone(err)
        self.assertEqual(len(store["events"]), 0)
        self.assertEqual(store["rejections"][0]["reason"], m.REASON_PAYLOAD_OVERSIZED)

    def test_a_non_mapping_payload_is_preserved_not_discarded(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1",
                 "payload": ["a", "b"], "message": "text"},
            ],
        )
        event = store["events"][0]
        self.assertEqual(event["payload"]["payload"], ["a", "b"])
        self.assertEqual(event["payload"]["message"], "text")

    def test_payload_depth_is_bounded(self):
        nested = current = {}
        for _ in range(m.MAX_PAYLOAD_DEPTH + 4):
            current["n"] = {}
            current = current["n"]
        current["leaf"] = "DEEPVALUE"
        store = _run(
            "s-1",
            [{"event_type": "run_started", "source_event_id": "e-1", "payload": nested}],
        )
        self.assertNotIn("DEEPVALUE", m.dumps(store))


class SessionValidationTests(unittest.TestCase):
    def test_non_mapping_sessions_are_refused(self):
        for value in ([], "x", 7, None):
            with self.subTest(value=value):
                store, err, _ = m.ingest_session(value)
                self.assertIsNone(store)
                self.assertEqual(err, m.REASON_NOT_A_SESSION)

    def test_missing_adapter_or_session_is_refused(self):
        self.assertEqual(
            m.ingest_session({"session_id": "s", "events": []})[1],
            m.REASON_SESSION_MISSING_ADAPTER,
        )
        self.assertEqual(
            m.ingest_session({"adapter": "a", "events": []})[1],
            m.REASON_SESSION_MISSING_ID,
        )

    def test_non_list_events_are_refused(self):
        self.assertEqual(
            m.ingest_session({"adapter": "a", "session_id": "s", "events": {}})[1],
            m.REASON_SESSION_EVENTS_NOT_LIST,
        )

    def test_the_bounded_event_count_is_enforced(self):
        policy = m.PrivacyPolicy(max_events=2)
        session = _session("s-1", [{"event_type": "run_started"}] * 3)
        store, err, _ = m.ingest_session(session, policy)
        self.assertIsNone(store)
        self.assertEqual(err, m.REASON_SESSION_EVENT_LIMIT)

    def test_reasons_never_interpolate_source_content(self):
        secrets = "sk-EXAMPLESYNTHETICKEY0123456789"
        store = _run("s-1", [{"event_type": "tool_use", "message": secrets}])
        for record in store["rejections"] + store["quarantines"]:
            self.assertNotIn("EXAMPLESYNTHETIC", m.dumps(record))


class MigrationTests(unittest.TestCase):
    """Additive migration and fail-closed recovery."""

    def _legacy(self):
        store = _run(
            "s-1",
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_progress", "source_event_id": "e-2",
                 "evidence": [{"artifact_ref": "logs/ok.jsonl"}],
                 "decisions": [{"summary": "s"}]},
                {"event_type": "run_terminated", "source_event_id": "e-3",
                 "outcome": "cancelled"},
            ],
        )
        legacy = json.loads(m.dumps(store))
        legacy["schema_version"] = "0.9.0"
        del legacy["code_entity_links"]
        del legacy["agent_run"]["privacy"]
        return legacy, store

    def test_the_current_version_is_returned_unchanged(self):
        legacy, store = self._legacy()
        store = json.loads(m.dumps(store))
        migrated, err = m.migrate_memory(store)
        self.assertIsNone(err)
        self.assertEqual(migrated, store)

    def test_an_older_version_migrates_without_drift(self):
        legacy, fresh = self._legacy()
        migrated, err = m.migrate_memory(legacy)
        self.assertIsNone(err)
        self.assertEqual(migrated["schema_version"], m.MEMORY_SCHEMA_VERSION)
        self.assertEqual(migrated["code_entity_links"], [])
        # Identical event count, identity, evidence link and replay result.
        self.assertEqual(m.dumps(migrated), m.dumps(fresh))

    def test_a_future_version_is_an_explicit_blocker(self):
        migrated, err = m.migrate_memory({"schema_version": "9.9.9"})
        self.assertIsNone(migrated)
        self.assertEqual(err, m.REASON_FUTURE_VERSION)

    def test_an_unknown_version_is_an_explicit_blocker(self):
        migrated, err = m.migrate_memory({"schema_version": "0.1.0"})
        self.assertIsNone(migrated)
        self.assertEqual(err, m.REASON_NOT_MIGRATABLE)

    def test_missing_or_invalid_versions_are_explicit_blockers(self):
        for raw, reason in (
            ({}, m.REASON_MISSING_VERSION),
            ({"schema_version": ""}, m.REASON_MISSING_VERSION),
            ({"schema_version": 1}, m.REASON_MISSING_VERSION),
            ("not-a-store", m.REASON_NOT_MAPPING),
        ):
            with self.subTest(raw=raw):
                migrated, err = m.migrate_memory(raw)
                self.assertIsNone(migrated)
                self.assertEqual(err, reason)

    def test_a_raising_migration_is_an_explicit_blocker(self):
        def _boom(raw):
            raise RuntimeError("interrupted")

        original = dict(m.MIGRATIONS)
        m.MIGRATIONS["0.9.0"] = _boom
        try:
            migrated, err = m.migrate_memory({"schema_version": "0.9.0"})
        finally:
            m.MIGRATIONS.clear()
            m.MIGRATIONS.update(original)
        self.assertIsNone(migrated)
        self.assertEqual(err, m.REASON_MIGRATION_FAILED)

    def test_a_drifting_migration_is_an_explicit_blocker(self):
        """A migration that would change replay meaning is refused."""

        def _drift(raw):
            store = dict(raw)
            store["events"] = []
            store["schema_version"] = m.MEMORY_SCHEMA_VERSION
            return store

        original = dict(m.MIGRATIONS)
        m.MIGRATIONS["0.9.0"] = _drift
        try:
            legacy, _ = self._legacy()
            migrated, err = m.migrate_memory(legacy)
        finally:
            m.MIGRATIONS.clear()
            m.MIGRATIONS.update(original)
        self.assertIsNone(migrated)
        self.assertEqual(err, m.REASON_MIGRATION_DRIFT)

    def test_migration_never_mutates_the_store_it_was_given(self):
        legacy, _ = self._legacy()
        before = json.loads(m.dumps(legacy))
        m.migrate_memory(legacy)
        self.assertEqual(legacy, before)


if __name__ == "__main__":
    unittest.main()
