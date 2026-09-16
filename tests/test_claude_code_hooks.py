"""Tests for the Claude Code hook -> M4.1 adapter (M4.2).

The adapter is the only module allowed to know provider vocabulary. These
tests pin the documented mapping, the content-dropping posture, the path
policy and the terminal-resolution rules, and they prove that an absent or
unrecognized field is disclosed rather than invented.
"""

from __future__ import annotations

import ast
import json
import os
import unittest

from hrca import claude_code_hooks as hooks
from hrca import memory

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

_ROOT = "C:/synthetic/project"
_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

# A value that must never be reproduced by a durable record.
_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"


def payload(event, **extra):
    body = {
        "session_id": _SESSION,
        "transcript_path": "C:/Users/someone/.claude/projects/-x/t.jsonl",
        "cwd": _ROOT,
        "hook_event_name": event,
    }
    body.update(extra)
    return body


def translate(event, root=_ROOT, **extra):
    return hooks.translate_hook_event(payload(event, **extra), root)


def only(event, root=_ROOT, **extra):
    events = translate(event, root, **extra)
    assert len(events) == 1, events
    return events[0]


class MappingTests(unittest.TestCase):
    def test_every_modelled_event_has_a_documented_event_type(self):
        for name in hooks.MODELLED_HOOK_EVENTS:
            with self.subTest(event=name):
                self.assertIn(name, hooks.HOOK_EVENT_TYPES)

    def test_the_modelled_surface_is_bounded(self):
        self.assertEqual(8, len(hooks.MODELLED_HOOK_EVENTS))
        self.assertTrue(
            set(hooks.MODELLED_HOOK_EVENTS).isdisjoint(hooks.UNMODELLED_HOOK_EVENTS)
        )

    def test_session_start_opens_the_run(self):
        event = only("SessionStart", source="startup")
        self.assertEqual(memory.EVENT_RUN_STARTED, event["event_type"])

    def test_a_tool_event_is_progress(self):
        event = only("PreToolUse", tool_name="Read", tool_use_id="tu-1")
        self.assertEqual(memory.EVENT_RUN_PROGRESS, event["event_type"])

    def test_session_end_closes_the_stream(self):
        event = only("SessionEnd", reason="other")
        self.assertEqual(memory.EVENT_STREAM_ENDED, event["event_type"])

    def test_no_timestamp_is_ever_invented(self):
        # Documented hook input carries no timestamp; the adapter must not
        # manufacture one from wall-clock time or from event order.
        for name in hooks.MODELLED_HOOK_EVENTS:
            with self.subTest(event=name):
                for event in translate(name):
                    self.assertNotIn("source_timestamp", event)

    def test_the_run_is_bound_to_the_session_identity(self):
        event = only("SessionStart")
        self.assertEqual(_SESSION, event["payload"]["session_id"])


class TerminalStateTests(unittest.TestCase):
    def test_a_stop_without_an_active_hook_concludes_the_run(self):
        event = only("Stop", stop_hook_active=False, last_assistant_message="done")
        self.assertEqual(memory.EVENT_RUN_TERMINATED, event["event_type"])
        self.assertEqual(memory.OUTCOME_COMPLETED, event["outcome"])

    def test_a_continuation_stop_is_not_a_conclusion(self):
        # ``stop_hook_active`` true means this Stop is itself the product of an
        # earlier blocking hook, so it is not the agent concluding.
        event = only("Stop", stop_hook_active=True)
        self.assertEqual(memory.EVENT_RUN_PROGRESS, event["event_type"])
        self.assertNotIn("outcome", event)

    def test_a_stop_failure_is_a_failed_conclusion(self):
        event = only("StopFailure", error="rate limited")
        self.assertEqual(memory.EVENT_RUN_TERMINATED, event["event_type"])
        self.assertEqual(memory.OUTCOME_FAILED, event["outcome"])

    def test_cancellation_reasons_are_exactly_the_documented_teardowns(self):
        for reason in ("clear", "resume", "logout", "prompt_input_exit"):
            with self.subTest(reason=reason):
                self.assertEqual(
                    memory.OUTCOME_CANCELLED, hooks.outcome_from_session_end(reason)
                )

    def test_the_catch_all_reason_is_not_interpreted(self):
        # ``other`` is documented as a catch-all: it is reported, never mapped.
        self.assertIsNone(hooks.outcome_from_session_end("other"))
        self.assertIsNone(hooks.outcome_from_session_end(None))

    def test_a_session_end_is_not_a_conclusion_on_its_own(self):
        self.assertEqual(memory.EVENT_STREAM_ENDED, only("SessionEnd")["event_type"])

    def test_an_interrupt_observation_is_never_a_terminal_state(self):
        event = only(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_use_id="tu-9",
            error="interrupted",
            is_interrupt=True,
        )
        self.assertEqual(memory.EVENT_RUN_PROGRESS, event["event_type"])
        self.assertTrue(event["payload"]["is_interrupt"])


class AssemblyTests(unittest.TestCase):
    def _assemble(self, items):
        events = [
            e
            for item in items
            for e in hooks.translate_hook_event(item, _ROOT)
        ]
        return hooks.assemble_session(events, project={"source_id": "p", "name": "P"})

    def test_a_concluded_stream_replays_to_completed(self):
        session, report = self._assemble(
            [
                payload("SessionStart", source="startup"),
                payload("Stop", stop_hook_active=False),
                payload("SessionEnd", reason="prompt_input_exit"),
            ]
        )
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_COMPLETED, memory.run_state(store))
        self.assertTrue(memory.is_success(store))
        self.assertTrue(report["concluded_by_agent"])

    def test_a_stream_without_a_conclusion_is_cancelled(self):
        session, report = self._assemble(
            [
                payload("SessionStart", source="startup"),
                payload("UserPromptSubmit", prompt="do the thing"),
                payload("SessionEnd", reason="prompt_input_exit"),
            ]
        )
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_CANCELLED, memory.run_state(store))
        self.assertFalse(memory.is_success(store))
        self.assertTrue(report["terminated_from_session_end"])

    def test_an_unrecognized_teardown_reason_is_unknown_never_success(self):
        session, _ = self._assemble(
            [
                payload("SessionStart", source="startup"),
                payload("SessionEnd", reason="other"),
            ]
        )
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_UNKNOWN_OUTCOME, memory.run_state(store))
        self.assertFalse(memory.is_success(store))

    def test_a_missing_teardown_reason_is_unknown_never_success(self):
        session, _ = self._assemble([payload("SessionStart", source="startup")])
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_MISSING_TERMINAL, memory.run_state(store))
        self.assertFalse(memory.is_success(store))

    def test_an_unknown_event_is_fail_closed(self):
        session, report = self._assemble(
            [
                payload("SessionStart", source="startup"),
                payload("SomethingBrandNew"),
            ]
        )
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_UNSUPPORTED, memory.run_state(store))
        self.assertFalse(memory.is_success(store))
        self.assertEqual(["SomethingBrandNew"], report["unsupported_events"])

    def test_a_documented_but_unmodelled_event_is_disclosed_not_fatal(self):
        session, report = self._assemble(
            [
                payload("SessionStart", source="startup"),
                payload("Notification", message="hello"),
                payload("Stop", stop_hook_active=False),
                payload("SessionEnd", reason="prompt_input_exit"),
            ]
        )
        store, error, _ = memory.ingest_session(session)
        self.assertIsNone(error)
        self.assertEqual(memory.RUN_COMPLETED, memory.run_state(store))
        self.assertEqual(["Notification"], report["unmodelled_events"])
        self.assertEqual([], report["unsupported_events"])

    def test_a_stream_without_identity_is_refused(self):
        events = [hooks.translate_hook_event({"hook_event_name": "Stop"}, _ROOT)[0]]
        session, _ = hooks.assemble_session(events)
        self.assertIsNone(session)


class IdentityTests(unittest.TestCase):
    def test_tool_identity_comes_from_the_clients_own_identifier(self):
        self.assertEqual(
            "PreToolUse:tu-42", hooks.source_event_id_for("PreToolUse", {"tool_use_id": "tu-42"})
        )

    def test_identity_does_not_depend_on_observed_content(self):
        # A redelivery whose content changed must keep the same identity, or a
        # conflict could never be recognized as one.
        first = only("PreToolUse", tool_name="Read", tool_use_id="tu-1",
                     tool_input={"file_path": _ROOT + "/a.txt"})
        second = only("PreToolUse", tool_name="Read", tool_use_id="tu-1",
                      tool_input={"file_path": _ROOT + "/b.txt"})
        self.assertEqual(first["source_event_id"], second["source_event_id"])

    def test_a_continuation_stop_and_a_concluding_stop_never_collide(self):
        final = hooks.source_event_id_for("Stop", {"stop_hook_active": False})
        active = hooks.source_event_id_for("Stop", {"stop_hook_active": True})
        self.assertNotEqual(final, active)


class OmissionTests(unittest.TestCase):
    def test_missing_documented_fields_are_reported(self):
        event = only("PreToolUse", tool_name="Read", tool_use_id="tu-1")
        missing = event["payload"]["missing_fields"]
        for name in ("agent_id", "agent_type", "permission_mode", "prompt_id"):
            self.assertIn(name, missing)

    def test_present_documented_fields_are_reported(self):
        event = only("PreToolUse", tool_name="Read", tool_use_id="tu-1")
        self.assertIn("tool_name", event["payload"]["present_fields"])

    def test_an_unknown_field_is_reported_by_name_only(self):
        event = only("PreToolUse", tool_use_id="tu-1", tool_name="Read",
                     surprise="do-not-keep-me")
        self.assertIn("surprise", event["payload"]["unknown_fields"])
        self.assertNotIn("do-not-keep-me", json.dumps(event))

    def test_an_unrecognized_tool_argument_is_disclosed_by_name(self):
        event = only(
            "PreToolUse",
            tool_use_id="tu-1",
            tool_name="Bash",
            tool_input={"command": "echo hi", "mystery": _SECRET},
        )
        projection = event["payload"]
        self.assertIn("mystery", projection["omitted_fields"])
        self.assertEqual("echo hi", projection["tool_input"]["command"])
        self.assertNotIn(_SECRET, json.dumps(event))

    def test_an_unrecognized_session_end_reason_is_preserved_verbatim(self):
        event = only("SessionEnd", reason="something-unheard-of")
        self.assertEqual("something-unheard-of", event["payload"]["reason"])
        self.assertFalse(event["payload"]["reason_documented"])


class PrivacyTests(unittest.TestCase):
    def test_prompt_content_is_never_persisted(self):
        event = only("UserPromptSubmit", prompt=_SECRET, source="user")
        encoded = json.dumps(event)
        self.assertNotIn(_SECRET, encoded)
        reference = event["payload"]["prompt_content"]
        self.assertEqual(len(_SECRET), reference["chars"])
        self.assertTrue(reference["digest"].startswith("sha256:"))
        self.assertIn("prompt", event["payload"]["omitted_fields"])

    def test_assistant_message_content_is_never_persisted(self):
        event = only("Stop", stop_hook_active=False, last_assistant_message=_SECRET)
        self.assertNotIn(_SECRET, json.dumps(event))
        self.assertIn("last_assistant_message", event["payload"]["omitted_fields"])

    def test_tool_response_content_is_never_persisted(self):
        event = only(
            "PostToolUse",
            tool_name="Read",
            tool_use_id="tu-1",
            tool_response={"content": _SECRET},
        )
        self.assertNotIn(_SECRET, json.dumps(event))
        self.assertIn("keys", event["payload"]["tool_response"])

    def test_error_text_is_never_persisted(self):
        event = only("StopFailure", error=_SECRET)
        self.assertNotIn(_SECRET, json.dumps(event))

    def test_a_secret_in_kept_tool_input_is_redacted(self):
        event = only(
            "PreToolUse",
            tool_use_id="tu-1",
            tool_name="Bash",
            tool_input={"command": "curl -H 'Authorization: Bearer " + _SECRET + "'"},
        )
        self.assertNotIn(_SECRET, json.dumps(event))

    def test_an_absolute_path_outside_the_root_is_refused(self):
        event = only(
            "PreToolUse",
            tool_use_id="tu-1",
            tool_name="Read",
            tool_input={"file_path": "C:/Users/someone/.ssh/id_rsa"},
        )
        self.assertIsNone(event["payload"]["tool_input"]["file_path"])
        self.assertNotIn("someone", json.dumps(event))

    def test_a_path_inside_the_root_is_relativized(self):
        event = only(
            "PreToolUse",
            tool_use_id="tu-1",
            tool_name="Read",
            tool_input={"file_path": _ROOT + "/pkg/mod.py"},
        )
        self.assertEqual("pkg/mod.py", event["payload"]["tool_input"]["file_path"])
        self.assertNotIn(_ROOT, json.dumps(event))

    def test_the_session_root_is_never_stored_absolutely(self):
        event = only("SessionStart", source="startup")
        self.assertEqual(".", event["payload"]["cwd"])
        self.assertNotIn(_ROOT, json.dumps(event))

    def test_the_transcript_is_a_reference_and_never_a_source(self):
        # The transcript sits outside the session root, so the reference is
        # recorded with no location at all: the record proves an artifact was
        # named without preserving where it lives.
        event = only("SessionStart", source="startup")
        transcript = [e for e in event["evidence"] if e["kind"] == "transcript"]
        self.assertEqual(1, len(transcript))
        self.assertIsNone(transcript[0]["artifact_ref"])
        self.assertNotIn("t.jsonl", json.dumps(event))

    def test_the_adapter_never_opens_a_file(self):
        with open(os.path.join(_SRC, "claude_code_hooks.py"), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", called)


class ChangeSetTests(unittest.TestCase):
    def test_a_write_tool_use_names_the_changed_path(self):
        event = only(
            "PostToolUse",
            tool_name="Write",
            tool_use_id="tu-7",
            tool_input={"file_path": _ROOT + "/out.txt", "content": "x"},
        )
        self.assertEqual(["out.txt"], event["paths"])
        self.assertEqual(
            [{"path": "out.txt", "entity_kind": "file"}], event["code_entities"]
        )

    def test_a_read_tool_use_changes_nothing(self):
        event = only(
            "PostToolUse",
            tool_name="Read",
            tool_use_id="tu-8",
            tool_input={"file_path": _ROOT + "/out.txt"},
        )
        self.assertEqual([], event["paths"])


if __name__ == "__main__":
    unittest.main()
