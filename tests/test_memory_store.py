"""Tests for deterministic Developer Memory persistence (M4.1).

These tests exercise :mod:`hrca.memory_store` — the single owner of memory
storage access — against a temporary base directory so the real per-user
app-data directory is never touched. They verify the atomic write path,
fail-closed loads, per-run isolation and migration recovery without any Qt or
filesystem access outside the temp directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from hrca import memory, memory_store


def _session(events, session_id="s-1"):
    return {"adapter": "fixture", "session_id": session_id, "events": events}


def _store(session_id="s-1"):
    store, err, _ = memory.ingest_session(
        _session(
            [
                {"event_type": "run_started", "source_event_id": "e-1"},
                {"event_type": "run_terminated", "source_event_id": "e-2",
                 "outcome": "completed"},
            ],
            session_id,
        )
    )
    assert err is None, err
    return store


def _run_id(store):
    return store["agent_run"]["id"]


class _Base:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class SaveLoadTests(_Base, unittest.TestCase):
    def test_round_trip_is_identical(self):
        store = _store()
        self.assertIsNone(memory_store.save(self.base, _run_id(store), store))
        loaded, err = memory_store.load(self.base, _run_id(store))
        self.assertIsNone(err)
        self.assertEqual(loaded, store)

    def test_absent_store_loads_to_none(self):
        loaded, err = memory_store.load(self.base, "run:fixture:nope:run")
        self.assertIsNone(loaded)
        self.assertIsNone(err)

    def test_store_lives_under_the_memory_namespace(self):
        store = _store()
        memory_store.save(self.base, _run_id(store), store)
        path = memory_store.run_store_path(self.base, _run_id(store))
        self.assertTrue(path.startswith(self.base))
        self.assertTrue(os.path.isfile(path))
        self.assertIn(memory_store._namespace(_run_id(store)), path)
        self.assertTrue(path.endswith(memory_store.RUN_STORE_FILENAME))

    def test_save_is_deterministic(self):
        store = _store()
        memory_store.save(self.base, _run_id(store), store)
        with open(memory_store.run_store_path(self.base, _run_id(store)), "rb") as fh:
            first = fh.read()
        memory_store.save(self.base, _run_id(store), store)
        with open(memory_store.run_store_path(self.base, _run_id(store)), "rb") as fh:
            second = fh.read()
        self.assertEqual(first, second)

    def test_the_store_namespace_is_distinct_from_twin_and_documents(self):
        from hrca import twin_store, version_store

        self.assertEqual(memory_store.memory_dir(self.base), os.path.join(self.base, "memory"))
        self.assertNotEqual(memory_store.memory_dir(self.base), self.base)
        self.assertNotIn("documents", memory_store.memory_dir(self.base))
        self.assertNotEqual(
            memory_store.memory_dir(self.base), version_store.documents_dir(self.base)
        )
        # The Twin store is keyed per workspace and is a different namespace.
        self.assertNotEqual(
            memory_store.memory_dir(self.base),
            os.path.dirname(twin_store.workspace_store_path(self.base, "ws:abc")),
        )


class IsolationTests(_Base, unittest.TestCase):
    def test_two_runs_never_collide(self):
        left = _store("s-1")
        right = _store("s-2")
        memory_store.save(self.base, _run_id(left), left)
        memory_store.save(self.base, _run_id(right), right)
        self.assertNotEqual(_run_id(left), _run_id(right))
        loaded_left, _ = memory_store.load(self.base, _run_id(left))
        loaded_right, _ = memory_store.load(self.base, _run_id(right))
        self.assertEqual(loaded_left["agent_run"]["session_id"], "s-1")
        self.assertEqual(loaded_right["agent_run"]["session_id"], "s-2")

    def test_repository_content_is_never_written(self):
        store = _store()
        memory_store.save(self.base, _run_id(store), store)
        self.assertEqual(sorted(os.listdir(self.base)), ["memory"])


class FailClosedLoadTests(_Base, unittest.TestCase):
    def _write_raw(self, run_id, text):
        path = memory_store.run_store_path(self.base, run_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_invalid_json_is_an_explicit_failure(self):
        run_id = "run:fixture:broken:run"
        self._write_raw(run_id, "{not json")
        loaded, err = memory_store.load(self.base, run_id)
        self.assertIsNone(loaded)
        self.assertIn("not valid JSON", err)

    def test_a_future_version_is_refused_and_left_untouched(self):
        run_id = "run:fixture:future:run"
        path = self._write_raw(run_id, json.dumps({"schema_version": "9.9.9"}))
        with open(path, "rb") as fh:
            before = fh.read()
        loaded, err = memory_store.load(self.base, run_id)
        self.assertIsNone(loaded)
        self.assertEqual(err, memory.REASON_FUTURE_VERSION)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_a_missing_version_is_refused(self):
        run_id = "run:fixture:noversion:run"
        self._write_raw(run_id, json.dumps({"agent_run": {}}))
        loaded, err = memory_store.load(self.base, run_id)
        self.assertIsNone(loaded)
        self.assertEqual(err, memory.REASON_MISSING_VERSION)

    def test_an_unreadable_store_is_an_explicit_failure(self):
        run_id = "run:fixture:dir:run"
        path = memory_store.run_store_path(self.base, run_id)
        os.makedirs(path, exist_ok=True)  # a directory where a file is expected
        loaded, err = memory_store.load(self.base, run_id)
        self.assertIsNone(loaded)
        self.assertIsNotNone(err)


class MigrationTests(_Base, unittest.TestCase):
    def _legacy_on_disk(self, run_id):
        store = _store()
        legacy = json.loads(memory.dumps(store))
        legacy["schema_version"] = "0.9.0"
        del legacy["code_entity_links"]
        del legacy["agent_run"]["privacy"]
        path = memory_store.run_store_path(self.base, run_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(legacy, fh, sort_keys=True)
        return store, path

    def test_a_legacy_store_migrates_on_load(self):
        store = _store()
        run_id = _run_id(store)
        fresh, _ = self._legacy_on_disk(run_id)
        loaded, err = memory_store.load(self.base, run_id)
        self.assertIsNone(err)
        self.assertEqual(loaded["schema_version"], memory.MEMORY_SCHEMA_VERSION)
        self.assertEqual(memory.dumps(loaded), memory.dumps(fresh))

    def test_loading_a_legacy_store_does_not_rewrite_it(self):
        store = _store()
        run_id = _run_id(store)
        _, path = self._legacy_on_disk(run_id)
        with open(path, "rb") as fh:
            before = fh.read()
        memory_store.load(self.base, run_id)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)


class AtomicWriteTests(_Base, unittest.TestCase):
    def test_an_interrupted_write_preserves_the_previous_state(self):
        store = _store()
        run_id = _run_id(store)
        memory_store.save(self.base, run_id, store)
        path = memory_store.run_store_path(self.base, run_id)
        with open(path, "rb") as fh:
            before = fh.read()

        original_replace = os.replace

        def _fail(src, dst):
            raise OSError("interrupted")

        os.replace = _fail
        try:
            reason = memory_store.save(self.base, run_id, {"schema_version": "1.0.0"})
        finally:
            os.replace = original_replace

        self.assertIsNotNone(reason)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        loaded, err = memory_store.load(self.base, run_id)
        self.assertIsNone(err)
        self.assertEqual(loaded, store)

    def test_a_failed_write_leaves_no_temporary_file_behind(self):
        store = _store()
        run_id = _run_id(store)
        memory_store.save(self.base, run_id, store)
        directory = os.path.dirname(memory_store.run_store_path(self.base, run_id))
        self.assertEqual(sorted(os.listdir(directory)), [memory_store.RUN_STORE_FILENAME])


class ListRunsTests(_Base, unittest.TestCase):
    def test_runs_are_summarized_deterministically(self):
        for session_id, outcome in (("s-1", "completed"), ("s-2", "failed")):
            store, err, _ = memory.ingest_session(
                _session(
                    [
                        {"event_type": "run_started", "source_event_id": "e-1"},
                        {"event_type": "run_terminated", "source_event_id": "e-2",
                         "outcome": outcome},
                    ],
                    session_id,
                )
            )
            memory_store.save(self.base, _run_id(store), store)
        summaries = memory_store.list_runs(self.base)
        self.assertEqual(len(summaries), 2)
        self.assertEqual([s["run_id"] for s in summaries], sorted(s["run_id"] for s in summaries))
        self.assertEqual({s["state"] for s in summaries}, {"completed", "failed"})
        for summary in summaries:
            self.assertEqual(summary["event_count"], 2)

    def test_an_empty_base_has_no_runs(self):
        self.assertEqual(memory_store.list_runs(self.base), [])

    def test_a_corrupt_store_is_skipped_never_treated_as_a_run(self):
        store = _store()
        memory_store.save(self.base, _run_id(store), store)
        broken = memory_store.run_store_path(self.base, "run:fixture:broken:run")
        os.makedirs(os.path.dirname(broken), exist_ok=True)
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        summaries = memory_store.list_runs(self.base)
        self.assertEqual([s["run_id"] for s in summaries], [_run_id(store)])


class PersistencePrivacyTests(_Base, unittest.TestCase):
    """Prove the privacy policy holds at the durable-write boundary.

    The store is written to disk, then the *raw bytes* are inspected: no secret
    value, no excluded path and no artifact content may appear anywhere in the
    persisted artefact.
    """

    def _save_privacy_fixture(self):
        import os as _os

        root = _os.path.normpath(
            _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "fixtures", "memory")
        )
        with open(_os.path.join(root, "sessions", "privacy.json"), "r", encoding="utf-8") as fh:
            session = json.load(fh)
        store, err, _ = memory.ingest_session(session)
        self.assertIsNone(err)
        self.assertIsNone(memory_store.save(self.base, _run_id(store), store))
        path = memory_store.run_store_path(self.base, _run_id(store))
        with open(path, "rb") as fh:
            return store, fh.read()

    def test_no_secret_value_reaches_the_durable_store(self):
        _, raw = self._save_privacy_fixture()
        for secret in (
            b"EXAMPLESYNTHETICKEY",
            b"SYNTHETICBEARERTOKEN",
            b"SYNTHETICVALUE",
            b"SYNTHETICPASSWORD",
            b"SYNTHETICTOKEN",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, raw)
        self.assertIn(memory.REDACTION_MARKER.encode("utf-8"), raw)

    def test_no_excluded_path_reaches_the_durable_store(self):
        _, raw = self._save_privacy_fixture()
        for excluded in (b".env", b"secrets/", b"server.pem", b".git/"):
            with self.subTest(excluded=excluded):
                self.assertNotIn(excluded, raw)
        self.assertIn(b"src/ok.py", raw)

    def test_no_artifact_content_reaches_the_durable_store(self):
        _, raw = self._save_privacy_fixture()
        self.assertNotIn(b"raw artifact content", raw)
        self.assertNotIn(b"raw transcript content", raw)
        # The artifact *reference* is retained.
        self.assertIn(b"logs/sess-privacy-001.jsonl", raw)

    def test_only_normalized_records_are_persisted(self):
        store, raw = self._save_privacy_fixture()
        self.assertEqual(json.loads(raw.decode("utf-8")), store)
        for key in memory.STORE_ARRAYS:
            self.assertIn(key, json.loads(raw.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
