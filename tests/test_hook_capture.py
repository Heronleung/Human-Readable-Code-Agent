"""Tests for the hook collector and importer (M4.2).

These tests exercise the capture boundary the way the client invokes it: one
hook payload at a time on stdin, appended to a transient spool, then replayed
into a persisted M4.1 store. They pin the properties the task depends on --
redaction before persistence, an idempotent re-import, an explicit quarantine
for a conflicting redelivery, a content-free rejection for malformed input,
and no raw hook payload anywhere on disk.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from hrca import claude_code_hooks as hooks
from hrca import hook_capture
from hrca import memory
from hrca import memory_store

_ROOT = "C:/synthetic/project"
_SESSION = "11111111-2222-3333-4444-555555555555"
_OTHER_SESSION = "99999999-8888-7777-6666-555555555555"
_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_HOME = "C:/Users/someone/.ssh/id_rsa"


def payload(event, session=_SESSION, **extra):
    body = {
        "session_id": session,
        "transcript_path": "C:/Users/someone/.claude/projects/-x/t.jsonl",
        "cwd": _ROOT,
        "hook_event_name": event,
    }
    body.update(extra)
    return body


def raw(item):
    return json.dumps(item).encode("utf-8")


def completed_stream():
    return [
        payload("SessionStart", source="startup"),
        payload("UserPromptSubmit", prompt_id="p-1", prompt="read " + _SECRET),
        payload(
            "PreToolUse",
            tool_name="Read",
            tool_use_id="tu-1",
            tool_input={"file_path": _ROOT + "/hello.txt"},
        ),
        payload(
            "PostToolUse",
            tool_name="Read",
            tool_use_id="tu-1",
            tool_input={"file_path": _ROOT + "/hello.txt"},
            tool_response={"content": _SECRET},
            duration_ms=7,
        ),
        payload("Stop", stop_hook_active=False, last_assistant_message=_SECRET),
        payload("SessionEnd", reason="prompt_input_exit"),
    ]


class CaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-m42-test-")
        self.spool = os.path.join(self.base, "spool")
        os.makedirs(self.spool)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def collect(self, item, spool=None, **kwargs):
        return hook_capture.collect_hook_input(
            raw(item), spool or self.spool, root=_ROOT, **kwargs
        )

    def collect_all(self, items, **kwargs):
        return [self.collect(item, **kwargs) for item in items]


class SpoolTests(CaptureTestCase):
    def test_one_payload_appends_one_event(self):
        self.assertEqual(0, self.collect(payload("SessionStart", source="startup")))
        header, events, rejections, error = hook_capture.read_spool(self.spool)
        self.assertIsNone(error)
        self.assertEqual(1, len(events))
        self.assertEqual([], rejections)
        self.assertEqual(_SESSION, header["session_id"])

    def test_the_spool_holds_no_raw_hook_payload(self):
        for item in completed_stream():
            self.collect(item)
        blob = self._spool_bytes()
        # The literal document the client delivered must not survive anywhere:
        # no content, no personal path, no transcript location. Documented
        # field *names* are disclosed, which is the point of the omission
        # account, but no documented field's value is reproduced.
        self.assertNotIn(_SECRET, blob)
        self.assertNotIn(_HOME, blob)
        self.assertNotIn(_ROOT, blob)
        self.assertNotIn("someone", blob)
        self.assertNotIn("t.jsonl", blob)
        self.assertNotIn(".claude", blob)
        self.assertNotIn('"transcript_path":"', blob)

    def test_the_header_records_a_root_digest_not_a_root(self):
        self.collect(payload("SessionStart", source="startup"))
        header, _, _, _ = hook_capture.read_spool(self.spool)
        self.assertTrue(header["root_digest"].startswith("sha256:"))
        self.assertNotIn(_ROOT, json.dumps(header))

    def test_a_payload_from_another_session_is_refused(self):
        self.collect(payload("SessionStart", source="startup"))
        self.collect(payload("Stop", session=_OTHER_SESSION, stop_hook_active=False))
        _, events, rejections, _ = hook_capture.read_spool(self.spool)
        self.assertEqual(1, len(events))
        self.assertEqual(
            [hook_capture.REASON_SESSION_MISMATCH], [r["reason"] for r in rejections]
        )

    def _spool_bytes(self):
        blob = b""
        for name in sorted(os.listdir(self.spool)):
            with open(os.path.join(self.spool, name), "rb") as handle:
                blob += handle.read()
        return blob.decode("utf-8", "replace")


class MalformedInputTests(CaptureTestCase):
    def test_invalid_json_is_rejected_without_storing_it(self):
        code = hook_capture.collect_hook_input(
            b"{not json at all " + _SECRET.encode(), self.spool, root=_ROOT
        )
        self.assertEqual(0, code)
        _, events, rejections, _ = hook_capture.read_spool(self.spool)
        self.assertEqual([], events)
        self.assertEqual(
            [hook_capture.REASON_INPUT_NOT_JSON], [r["reason"] for r in rejections]
        )
        self.assertNotIn(_SECRET, json.dumps(rejections))

    def test_a_non_mapping_payload_is_rejected(self):
        hook_capture.collect_hook_input(b"[1,2,3]", self.spool, root=_ROOT)
        _, _, rejections, _ = hook_capture.read_spool(self.spool)
        self.assertEqual(
            [hook_capture.REASON_INPUT_NOT_MAPPING], [r["reason"] for r in rejections]
        )

    def test_an_empty_payload_is_rejected(self):
        hook_capture.collect_hook_input(b"   \n", self.spool, root=_ROOT)
        _, _, rejections, _ = hook_capture.read_spool(self.spool)
        self.assertEqual(
            [hook_capture.REASON_INPUT_EMPTY], [r["reason"] for r in rejections]
        )

    def test_an_oversized_payload_is_rejected_before_parsing(self):
        hook_capture.collect_hook_input(
            b'{"big":"' + b"x" * (hooks.MAX_HOOK_INPUT_BYTES + 10) + b'"}',
            self.spool,
            root=_ROOT,
        )
        _, events, rejections, _ = hook_capture.read_spool(self.spool)
        self.assertEqual([], events)
        self.assertEqual(
            [hook_capture.REASON_INPUT_OVERSIZED], [r["reason"] for r in rejections]
        )

    def test_a_payload_without_a_session_identity_is_rejected(self):
        hook_capture.collect_hook_input(
            raw({"hook_event_name": "Stop"}), self.spool, root=_ROOT
        )
        _, _, rejections, _ = hook_capture.read_spool(self.spool)
        self.assertEqual(
            [hook_capture.REASON_INPUT_NO_SESSION], [r["reason"] for r in rejections]
        )


class ImportTests(CaptureTestCase):
    def test_a_concluded_stream_imports_as_completed(self):
        self.collect_all(completed_stream())
        store, error, report = hook_capture.import_spool(self.spool)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_COMPLETED, memory.run_state(store))
        self.assertEqual({"accepted": 6}, report["ingest_outcomes"])

    def test_a_stream_without_a_conclusion_imports_as_cancelled(self):
        self.collect_all(
            [
                payload("SessionStart", source="startup"),
                payload("UserPromptSubmit", prompt="do the thing"),
                payload("SessionEnd", reason="prompt_input_exit"),
            ]
        )
        store, error, _ = hook_capture.import_spool(self.spool)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_CANCELLED, memory.run_state(store))
        self.assertFalse(memory.is_success(store))

    def test_re_import_is_an_idempotent_no_op(self):
        self.collect_all(completed_stream())
        first, _, _ = hook_capture.import_spool(self.spool)
        second, _, report = hook_capture.import_spool(self.spool)
        self.assertEqual(memory.dumps(first), memory.dumps(second))
        self.assertEqual({"accepted": 6}, report["ingest_outcomes"])

    def test_redelivering_the_same_stream_into_a_store_is_a_no_op(self):
        # Re-import builds a fresh store, so the contract-level no-op is proved
        # by replaying the same events into the store they already populate.
        self.collect_all(completed_stream())
        store, error, _ = hook_capture.import_spool(self.spool)
        self.assertIsNone(error)
        _, events, _, _ = hook_capture.read_spool(self.spool)
        session, _ = hooks.assemble_session(events)
        outcomes = [memory.apply_event(store, event)[1]["status"]
                    for event in session["events"]]
        self.assertEqual(["duplicate"] * len(session["events"]), outcomes)
        self.assertEqual(memory.RUN_COMPLETED, memory.run_state(store))

    def test_a_conflicting_redelivery_is_quarantined(self):
        self.collect_all(
            [
                payload("SessionStart", source="startup"),
                payload("PreToolUse", tool_name="Read", tool_use_id="tu-1",
                        tool_input={"file_path": _ROOT + "/a.txt"}),
                payload("PreToolUse", tool_name="Read", tool_use_id="tu-1",
                        tool_input={"file_path": _ROOT + "/b.txt"}),
                payload("Stop", stop_hook_active=False),
                payload("SessionEnd", reason="prompt_input_exit"),
            ]
        )
        store, error, report = hook_capture.import_spool(self.spool)
        self.assertIsNone(error)
        self.assertEqual(1, report["ingest_outcomes"]["quarantined"])
        self.assertEqual(1, len(store["quarantines"]))
        # A quarantine is never success and never silently absorbed.
        self.assertEqual(memory.RUN_COMPLETED, memory.run_state(store))

    def test_replaying_the_same_stream_twice_is_byte_stable(self):
        self.collect_all(completed_stream())
        first, _, _ = hook_capture.import_spool(self.spool)
        again, _, _ = hook_capture.import_spool(self.spool)
        self.assertEqual(memory.dumps(first).encode(), memory.dumps(again).encode())

    def test_a_directory_with_no_records_is_an_explicit_failure(self):
        store, error, _ = hook_capture.import_spool(self.spool)
        self.assertIsNone(store)
        self.assertEqual(hook_capture.REASON_SPOOL_UNREADABLE, error)

    def test_a_header_with_no_events_is_an_explicit_failure(self):
        self.collect(payload("SessionStart", source="startup"))
        os.remove(os.path.join(self.spool, hook_capture.EVENTS_FILENAME))
        store, error, _ = hook_capture.import_spool(self.spool)
        self.assertIsNone(store)
        self.assertEqual(hook_capture.REASON_SPOOL_EMPTY, error)

    def test_a_missing_spool_is_an_explicit_failure(self):
        store, error, _ = hook_capture.import_spool(os.path.join(self.base, "nope"))
        self.assertIsNone(store)
        self.assertEqual(hook_capture.REASON_SPOOL_UNREADABLE, error)

    def test_capture_rejections_are_reported_alongside_the_run(self):
        self.collect(payload("SessionStart", source="startup"))
        hook_capture.collect_hook_input(b"junk", self.spool, root=_ROOT)
        self.collect(payload("Stop", stop_hook_active=False))
        self.collect(payload("SessionEnd", reason="prompt_input_exit"))
        store, error, report = hook_capture.import_spool(self.spool)
        self.assertIsNone(error)
        self.assertEqual(1, report["capture_rejections"])


class BlockTests(CaptureTestCase):
    def test_a_blocked_event_exits_two_and_is_recorded_as_a_decision(self):
        self.collect(payload("SessionStart", source="startup"))
        code = self.collect(
            payload("UserPromptSubmit", prompt_id="p-1", prompt="do it"),
            block_events=("UserPromptSubmit",),
        )
        self.assertEqual(2, code)
        self.collect(payload("SessionEnd", reason="prompt_input_exit"))
        store, error, _ = hook_capture.import_spool(self.spool)
        self.assertIsNone(error)
        self.assertEqual(1, len(store["decisions"]))
        self.assertIn("blocked", store["decisions"][0]["summary"])
        # The blocked run never concluded, so the session ends cancelled and
        # is never reported as success.
        self.assertEqual(memory.RUN_CANCELLED, memory.run_state(store))
        self.assertFalse(memory.is_success(store))

    def test_an_unblocked_event_exits_zero(self):
        self.assertEqual(0, self.collect(payload("UserPromptSubmit", prompt="do it")))


class PersistenceTests(CaptureTestCase):
    def _import_and_save(self, spool=None):
        spool = spool or self.spool
        store, error, _ = hook_capture.import_spool(spool)
        self.assertIsNone(error)
        run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        path = memory_store.run_store_path(self.base, run_id)
        with open(path, "rb") as handle:
            return run_id, handle.read().decode("utf-8")

    def test_no_content_or_personal_path_reaches_the_durable_store(self):
        self.collect_all(
            completed_stream()
            + [
                payload(
                    "PreToolUse",
                    tool_name="Read",
                    tool_use_id="tu-2",
                    tool_input={"file_path": _HOME, "mystery": _SECRET},
                )
            ]
        )
        _, blob = self._import_and_save()
        for forbidden in (_SECRET, _HOME, "someone", _ROOT, ".claude", ".ssh"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_the_store_survives_a_load_unchanged(self):
        self.collect_all(completed_stream())
        run_id, blob = self._import_and_save()
        loaded, error = memory_store.load(self.base, run_id)
        self.assertIsNone(error)
        self.assertEqual(memory.dumps(loaded), blob.rstrip("\n"))

    def test_two_captured_sessions_never_collide(self):
        other = os.path.join(self.base, "spool-other")
        os.makedirs(other)
        self.collect_all(completed_stream())
        self.collect_all(
            [payload("SessionStart", source="startup", session=_OTHER_SESSION),
             payload("SessionEnd", reason="clear", session=_OTHER_SESSION)],
            spool=other,
        )
        first_id, _ = self._import_and_save()
        second_id, _ = self._import_and_save(other)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(2, len(memory_store.list_runs(self.base)))

    def test_the_store_is_bound_to_the_synthetic_project(self):
        self.collect_all(
            completed_stream(),
            project={"source_id": "synth-repo", "name": "Synthetic Capture"},
            work_package={"source_id": "cap-01", "title": "M4.2 capture"},
        )
        run_id, blob = self._import_and_save()
        store = json.loads(blob)
        self.assertEqual("synth-repo", store["projects"][0]["source_id"])
        self.assertEqual("cap-01", store["work_packages"][0]["source_id"])
        self.assertTrue(run_id.startswith("run:claude-code:"))


class CleanupTests(CaptureTestCase):
    def test_cleanup_removes_the_spool(self):
        self.collect(payload("SessionStart", source="startup"))
        self.assertTrue(hook_capture.cleanup_spool(self.spool))
        self.assertFalse(os.path.exists(self.spool))

    def test_cleanup_refuses_a_directory_that_is_not_a_spool(self):
        victim = os.path.join(self.base, "not-a-spool")
        os.makedirs(victim)
        with open(os.path.join(victim, "precious.txt"), "w", encoding="utf-8") as fh:
            fh.write("keep me")
        self.assertFalse(hook_capture.cleanup_spool(victim))
        self.assertTrue(os.path.isfile(os.path.join(victim, "precious.txt")))

    def test_cleanup_of_a_missing_spool_is_a_no_op(self):
        self.assertTrue(hook_capture.cleanup_spool(os.path.join(self.base, "gone")))


if __name__ == "__main__":
    unittest.main()
