"""Tests for the durable correction, confirmation and history boundary (M4.5/v1a).

Schema 1.1.0 adds immutable generated-document versions and append-only human
corrections. These tests pin what that authority may and may not do: history is
never rewritten, a correction never touches a normalized record or a run state,
resolution binds only by exact typed identity against an unchanged claim, and a
missing, changed or ambiguous target is reported rather than guessed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from hrca import boundary, client_core, contract, memory, memory_docs
from hrca import memory_revisions as rv
from hrca import memory_store

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

# Sentinel so a test can ask for "no baseline at all" rather than the default.
_DEFAULT_BASE = object()

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures", "memory"))

CLAIM = "claim:session_summary:project"

EVENTS = [
    {"event_type": "run_started", "source_event_id": "e1", "payload": {"prompt": _SECRET}},
    {"event_type": "run_progress", "source_event_id": "e2", "payload": {},
     "paths": ["pkg/mod.py"], "decisions": [{"summary": "chose the bounded path"}]},
    {"event_type": "run_terminated", "source_event_id": "e3", "outcome": "completed",
     "payload": {}},
    {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}},
]


def build_store():
    store, error, _ = memory.ingest_session({
        "adapter": "smoke", "session_id": "s-1", "events": EVENTS,
        "project": {"source_id": "p-1", "name": "Project One"},
        "work_package": {"source_id": "w-1", "title": "Correction work"},
    })
    assert error is None, error
    return store


def document_of(store, document_type="session_summary"):
    document_set, _err, _report = memory_docs.project_store(store)
    return document_set["documents"][document_type]


class RevisionTestCase(unittest.TestCase):
    def setUp(self):
        self.store = build_store()
        self.run_id = self.store["agent_run"]["id"]
        self.document = document_of(self.store)
        self.claim = {c["id"]: c for c in self.document["claims"]}[CLAIM]
        self.base = rv.claim_fingerprint(self.claim)
        self.version, _ = rv.record_generated_version(
            self.store, self.run_id, "session_summary", self.document
        )

    def append(self, payload, base=_DEFAULT_BASE):
        return rv.append_correction(
            self.store, self.run_id, payload,
            self.base if base is _DEFAULT_BASE else base,
        )

    def effective(self, document=None):
        result, _reason = rv.resolve_effective(
            self.store, self.run_id, "session_summary",
            document if document is not None else self.document, self.version,
        )
        return result

    def entry(self, result=None, claim_id=CLAIM):
        result = result if result is not None else self.effective()
        return {c["claim_id"]: c for c in result["claims"]}[claim_id]

    def payload(self, **overrides):
        body = {
            "document_type": "session_summary",
            "operation": "keep",
            "target": {"claim_id": CLAIM},
        }
        body.update(overrides)
        return body


class MigrationTests(unittest.TestCase):
    def test_a_1_0_0_store_migrates_additively(self):
        raw = json.loads(memory.dumps(self._legacy_store()))
        raw["schema_version"] = "1.0.0"
        migrated, error = memory.migrate_memory(raw)
        self.assertIsNone(error)
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, migrated["schema_version"])
        self.assertEqual([], migrated["generated_documents"])
        self.assertEqual([], migrated["corrections"])

    def _legacy_store(self):
        store = build_store()
        store["schema_version"] = "1.0.0"
        store.pop("generated_documents", None)
        store.pop("corrections", None)
        return store

    def test_migration_preserves_identity_and_replay_meaning(self):
        raw = self._legacy_store()
        migrated, error = memory.migrate_memory(raw)
        self.assertIsNone(error)
        self.assertEqual(
            memory._migration_snapshot(raw), memory._migration_snapshot(migrated)
        )
        self.assertEqual(raw["agent_run"]["state"], migrated["agent_run"]["state"])
        self.assertEqual(
            [e["id"] for e in raw["events"]], [e["id"] for e in migrated["events"]]
        )
        self.assertEqual(
            [ev["id"] for ev in raw["evidence"]],
            [ev["id"] for ev in migrated["evidence"]],
        )

    def test_a_0_9_0_store_chains_to_the_current_version(self):
        with open(os.path.join(_FIXTURES, "stores", "legacy_0_9_0.json"),
                  encoding="utf-8") as handle:
            raw = json.load(handle)
        migrated, error = memory.migrate_memory(raw)
        self.assertIsNone(error)
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, migrated["schema_version"])
        self.assertIn("generated_documents", migrated)
        self.assertIn("corrections", migrated)

    def test_a_future_version_is_an_explicit_blocker(self):
        store, error = memory.migrate_memory(
            {"schema_version": "9.9.9", "agent_run": {}}
        )
        self.assertIsNone(store)
        self.assertEqual(memory.REASON_FUTURE_VERSION, error)

    def test_an_unknown_version_is_an_explicit_blocker(self):
        store, error = memory.migrate_memory(
            {"schema_version": "0.1.0", "agent_run": {}}
        )
        self.assertIsNone(store)
        self.assertEqual(memory.REASON_NOT_MIGRATABLE, error)

    def test_a_failed_migration_leaves_the_prior_bytes_readable(self):
        base = tempfile.mkdtemp(prefix="hrca-m45-mig-")
        try:
            store = build_store()
            run_id = store["agent_run"]["id"]
            self.assertIsNone(memory_store.save(base, run_id, store))
            path = memory_store.run_store_path(base, run_id)
            with open(path, "rb") as handle:
                before = handle.read()
            # A store the loader must refuse leaves the file exactly as it was.
            broken = json.loads(before.decode("utf-8"))
            broken["schema_version"] = "9.9.9"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(broken, handle)
            with open(path, "rb") as handle:
                refused = handle.read()
            loaded, error = memory_store.load(base, run_id)
            self.assertIsNone(loaded)
            self.assertEqual(memory.REASON_FUTURE_VERSION, error)
            with open(path, "rb") as handle:
                self.assertEqual(refused, handle.read())
        finally:
            shutil.rmtree(base, ignore_errors=True)


class GeneratedVersionTests(RevisionTestCase):
    def test_a_version_is_recorded_once(self):
        again, _ = rv.record_generated_version(
            self.store, self.run_id, "session_summary", self.document
        )
        self.assertEqual(self.version["id"], again["id"])
        self.assertEqual(1, len(self.store["generated_documents"]))

    def test_regenerating_identical_content_does_not_add_a_revision(self):
        for _ in range(3):
            rv.record_generated_version(
                self.store, self.run_id, "session_summary", self.document
            )
        self.assertEqual(1, len(self.store["generated_documents"]))

    def test_a_new_version_is_appended_when_the_content_changes(self):
        changed = dict(self.document)
        changed["claims"] = list(self.document["claims"]) + [{"id": "claim:extra"}]
        second, error = rv.record_generated_version(
            self.store, self.run_id, "session_summary", changed
        )
        self.assertIsNone(error)
        self.assertNotEqual(self.version["id"], second["id"])
        self.assertEqual(2, second["revision"])
        self.assertEqual(2, len(self.store["generated_documents"]))

    def test_the_version_limit_fails_closed(self):
        for index in range(rv.MAX_GENERATED_VERSIONS):
            rv.record_generated_version(
                self.store, self.run_id, "session_summary",
                {"claims": [{"id": "c%d" % index}]},
            )
        record, error = rv.record_generated_version(
            self.store, self.run_id, "session_summary", {"claims": [{"id": "one-more"}]}
        )
        self.assertIsNone(record)
        self.assertEqual(rv.REASON_VERSION_LIMIT, error)


class IdentityTests(RevisionTestCase):
    def test_an_identical_retry_is_idempotent(self):
        payload = self.payload(source_id="c-1", actor="heron")
        first, error, created = self.append(payload)
        self.assertTrue(created)
        self.assertIsNone(error)
        second, error2, created2 = self.append(payload)
        self.assertFalse(created2)
        self.assertIsNone(error2)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, len(self.store["corrections"]))

    def test_the_same_identity_with_different_content_fails_closed(self):
        self.append(self.payload(source_id="c-1", actor="heron"))
        record, error, created = self.append(
            self.payload(source_id="c-1", actor="someone-else")
        )
        self.assertIsNone(record)
        self.assertFalse(created)
        self.assertEqual(rv.REASON_IDENTITY_CONFLICT, error)
        self.assertEqual(1, len(self.store["corrections"]))

    def test_content_identity_makes_an_unidentified_retry_idempotent(self):
        payload = self.payload(actor="heron")
        _first, _err, created = self.append(payload)
        _second, _err2, created2 = self.append(payload)
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(1, len(self.store["corrections"]))

    def test_a_correction_without_a_baseline_is_refused(self):
        record, error, created = self.append(self.payload(), base=None)
        self.assertIsNone(record)
        self.assertFalse(created)
        self.assertEqual(rv.REASON_BASE_UNKNOWN, error)


class ValidationTests(RevisionTestCase):
    def test_every_rejectable_shape_fails_closed(self):
        cases = [
            ("operation", rv.REASON_OPERATION_UNSUPPORTED, self.payload(operation="rewrite")),
            ("state", rv.REASON_STATE_UNSUPPORTED, self.payload(state="approved")),
            ("target", rv.REASON_TARGET_UNUSABLE, self.payload(target={})),
            ("target kind", rv.REASON_TARGET_UNUSABLE,
             self.payload(target={"claim_id": CLAIM, "kind": ""})),
            ("no text", rv.REASON_TEXT_UNUSABLE, self.payload(operation="merge")),
            ("text on keep", rv.REASON_TEXT_NOT_ALLOWED, self.payload(text="x")),
            ("long text", rv.REASON_TEXT_UNUSABLE,
             self.payload(operation="merge", text="x" * 5000)),
            ("long actor", rv.REASON_ACTOR_UNUSABLE, self.payload(actor="a" * 500)),
            ("bad supersedes", rv.REASON_SUPERSEDES_UNUSABLE,
             self.payload(supersedes=[""])),
            ("unknown supersede", rv.REASON_SUPERSEDES_UNKNOWN,
             self.payload(operation="merge", text="x", supersedes=["correction:nope"])),
            ("long source id", rv.REASON_SOURCE_ID_UNUSABLE,
             self.payload(source_id="s" * 500)),
        ]
        for label, expected, payload in cases:
            with self.subTest(case=label):
                record, error, created = self.append(payload)
                self.assertIsNone(record)
                self.assertFalse(created)
                self.assertEqual(expected, error)

    def test_a_non_mapping_command_is_refused(self):
        record, error, _created = rv.append_correction(self.store, self.run_id, "nope", self.base)
        self.assertIsNone(record)
        self.assertEqual(rv.REASON_OPERATION_UNSUPPORTED, error)

    def test_a_missing_run_is_refused(self):
        record, error, _created = self.append(self.payload())
        self.assertIsNone(error)
        other, other_error, _c = rv.append_correction(
            self.store, "run:nope:x:run", self.payload(), self.base
        )
        self.assertIsNone(other)
        self.assertEqual(rv.REASON_NOT_A_STORE, other_error)

    def test_a_secret_in_correction_text_is_redacted_before_storage(self):
        record, error, _created = self.append(
            self.payload(operation="merge", text="note " + _SECRET)
        )
        self.assertIsNone(error)
        self.assertNotIn(_SECRET, memory.dumps(self.store))
        self.assertIn(memory.REDACTION_MARKER, record["text"])

    def test_a_personal_path_in_text_survives_only_as_text(self):
        record, error, _created = self.append(
            self.payload(operation="merge", text=_PERSONAL)
        )
        self.assertIsNone(error)
        self.assertEqual(_PERSONAL, record["text"])

    def test_the_correction_limit_fails_closed(self):
        self.store["corrections"] = [
            {"id": "correction:filler:%d" % i, "run_id": self.run_id}
            for i in range(rv.MAX_CORRECTIONS_PER_RUN)
        ]
        record, error, created = self.append(self.payload(source_id="c-1"))
        self.assertIsNone(record)
        self.assertFalse(created)
        self.assertEqual(rv.REASON_CORRECTION_LIMIT, error)


class StateTests(RevisionTestCase):
    def test_a_confirmed_overlay_is_authority(self):
        self.append(self.payload(operation="merge", text="Human text", source_id="c-1"))
        entry = self.entry()
        self.assertEqual("Human text", entry["effective_statement"])
        self.assertEqual("source", entry["effective_provenance"])
        self.assertEqual(1, len(self.effective()["applied_correction_ids"]))

    def test_a_draft_changes_nothing(self):
        self.append(self.payload(operation="merge", text="DRAFT", state="draft",
                                 source_id="c-1"))
        entry = self.entry()
        self.assertEqual(self.claim["statement"], entry["effective_statement"])
        self.assertEqual([], self.effective()["applied_correction_ids"])
        # ...but it is retained and reviewable.
        self.assertEqual(1, len(self.store["corrections"]))

    def test_an_archived_revision_changes_nothing(self):
        self.append(self.payload(operation="merge", text="OLD", state="archived",
                                 source_id="c-1"))
        self.assertEqual([], self.effective()["applied_correction_ids"])
        self.assertEqual(1, len(self.store["corrections"]))

    def test_a_rejected_claim_is_visible_and_not_deleted(self):
        self.append(self.payload(operation="reject", source_id="c-1"))
        entry = self.entry()
        self.assertTrue(entry["rejected"])
        self.assertEqual("reject", entry["overlay"]["operation"])
        # The generated statement is still there to be read.
        self.assertEqual(self.claim["statement"], entry["generated_statement"])

    def test_a_reject_command_states_itself(self):
        record, error, _created = self.append(self.payload(operation="reject"))
        self.assertIsNone(error)
        self.assertEqual(rv.STATE_REJECTED, record["state"])

    def test_every_state_is_distinct_and_labelled(self):
        for state in rv.CORRECTION_STATES:
            with self.subTest(state=state):
                self.assertTrue(client_core.memory_correction_state_label(state))
        self.assertEqual(
            set(rv.CORRECTION_STATES), set(client_core.MEMORY_CORRECTION_STATES)
        )
        self.assertNotIn(rv.OUTCOME_UNRESOLVED_CONFLICT, rv.CORRECTION_STATES)


class SupersedeTests(RevisionTestCase):
    def test_supersede_keeps_the_parent_in_history(self):
        first, _err, _created = self.append(self.payload(source_id="c-1"))
        _second, error, _created2 = self.append(
            self.payload(operation="supersede", text="Successor",
                         supersedes=[first["id"]], source_id="c-2")
        )
        self.assertIsNone(error)
        history, _ = rv.history_for(self.store, self.run_id, "session_summary")
        self.assertIn(first["id"], history["superseded_ids"])
        self.assertEqual(2, len(history["corrections"]))
        ids = [c["id"] for c in history["corrections"]]
        self.assertIn(first["id"], ids)

    def test_the_superseded_parent_stops_being_authority(self):
        first, _e, _c = self.append(self.payload(operation="merge", text="FIRST",
                                                 source_id="c-1"))
        self.append(self.payload(operation="supersede", text="SECOND",
                                 supersedes=[first["id"]], source_id="c-2"))
        entry = self.entry()
        self.assertEqual("SECOND", entry["effective_statement"])
        self.assertEqual(1, len(self.effective()["applied_correction_ids"]))

    def test_merge_parents_are_preserved(self):
        first, _e, _c = self.append(self.payload(source_id="c-1"))
        second, _e2, _c2 = self.append(self.payload(source_id="c-2"))
        third, error, _c3 = self.append(
            self.payload(operation="merge", text="Merged",
                         supersedes=[first["id"], second["id"]], source_id="c-3")
        )
        self.assertIsNone(error)
        self.assertEqual(sorted([first["id"], second["id"]]), third["supersedes"])


class EffectiveTests(RevisionTestCase):
    def test_a_missing_target_is_a_visible_conflict(self):
        self.append(self.payload(target={"claim_id": "claim:absent"},
                                 source_id="c-1"))
        result = self.effective()
        self.assertEqual(1, len(result["conflicts"]))
        self.assertEqual(rv.OUTCOME_UNRESOLVED_CONFLICT, result["conflicts"][0]["outcome"])
        self.assertEqual(
            rv.REASON_TARGET_NOT_PRESENT, result["conflicts"][0]["reason"]
        )
        self.assertEqual([], result["applied_correction_ids"])

    def test_a_changed_target_is_a_visible_conflict(self):
        self.append(self.payload(operation="merge", text="HUMAN", source_id="c-1"))
        mutated = dict(self.document)
        mutated["claims"] = [dict(c) for c in self.document["claims"]]
        for claim in mutated["claims"]:
            if claim["id"] == CLAIM:
                claim["statement"] = "the claim moved on"
        result = self.effective(mutated)
        self.assertEqual(
            rv.REASON_TARGET_CHANGED, result["conflicts"][0]["reason"]
        )
        # The generated statement is shown unchanged, never replaced by a guess.
        entry = self.entry(result)
        self.assertEqual("the claim moved on", entry["effective_statement"])
        self.assertIsNone(entry["overlay"])

    def test_a_conflict_is_distinct_from_every_stored_state(self):
        self.append(self.payload(target={"claim_id": "claim:absent"}, source_id="c-1"))
        conflict = self.effective()["conflicts"][0]
        self.assertNotIn(conflict["outcome"], rv.CORRECTION_STATES)
        record = self.store["corrections"][0]
        # The durable record keeps its own state: the conflict is an outcome.
        self.assertEqual(rv.STATE_CONFIRMED, record["state"])

    def test_unrelated_claims_are_untouched(self):
        before = self.effective()
        self.append(self.payload(operation="merge", text="HUMAN", source_id="c-1"))
        after = self.effective()
        changed = [
            a["claim_id"]
            for a, b in zip(after["claims"], before["claims"])
            if a["effective_statement"] != b["effective_statement"]
        ]
        self.assertEqual([CLAIM], changed)

    def test_the_result_states_what_a_correction_cannot_do(self):
        self.append(self.payload(source_id="c-1"))
        blob = memory.dumps(self.effective())
        self.assertIn("never changes a normalized record", blob)
        self.assertIn("not verified", blob)
        self.assertIn("not acceptance", blob)
        self.assertIn("never guessed", blob)

    def test_resolution_is_byte_stable(self):
        self.append(self.payload(operation="merge", text="HUMAN", source_id="c-1"))
        self.assertEqual(
            rv.render(self.effective()), rv.render(self.effective())
        )


class HistoryTests(RevisionTestCase):
    def test_history_is_append_only_and_byte_stable(self):
        self.append(self.payload(source_id="c-1"))
        first, error = rv.history_for(self.store, self.run_id, "session_summary")
        self.assertIsNone(error)
        self.append(self.payload(operation="merge", text="X", source_id="c-2"))
        second, _err2 = rv.history_for(self.store, self.run_id, "session_summary")
        self.assertEqual(2, len(second["corrections"]))
        # The first history is a prefix of the second: nothing was rewritten.
        self.assertEqual(
            [c["id"] for c in first["corrections"]],
            [c["id"] for c in second["corrections"]][:1],
        )
        self.assertEqual(rv.render(second), rv.render(
            rv.history_for(self.store, self.run_id, "session_summary")[0]
        ))

    def test_history_states_its_own_limits(self):
        history, _ = rv.history_for(self.store, self.run_id)
        blob = memory.dumps(history)
        self.assertIn("append-only", blob)
        self.assertIn("never changes a normalized record or a run state", blob)

    def test_history_never_returns_a_fingerprint(self):
        self.append(self.payload(source_id="c-1"))
        history, _ = rv.history_for(self.store, self.run_id, "session_summary")
        blob = rv.render(history)
        self.assertNotIn("sha256:", blob)
        self.assertTrue(history["corrections"][0]["base_recorded"])

    def test_a_missing_run_is_refused(self):
        result, error = rv.history_for(self.store, "run:nope:x:run")
        self.assertIsNone(result)
        self.assertEqual(rv.REASON_NOT_A_STORE, error)


class ProtocolTests(unittest.TestCase):
    """The 3.8.0 actions through the real boundary, over a temp store base."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-m45-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        store = build_store()
        self.run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, self.run_id, store))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def call(self, payload, **overrides):
        request = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "c1",
        }
        request.update(payload)
        request.update(overrides)
        return boundary.handle_request(request, self.session)

    def correct(self, operation="keep", **overrides):
        return self.call(
            client_core.build_memory_correction_request(
                "c1", self.run_id, "session_summary", operation, CLAIM, **overrides
            )
        )

    def test_the_contract_is_3_8_0_and_the_actions_are_allowed(self):
        self.assertEqual("3.8.0", contract.CONTRACT_VERSION)
        for action in sorted(contract.MEMORY_REVISION_ACTIONS):
            with self.subTest(action=action):
                self.assertIn(action, contract.ALLOWED_ACTIONS)

    def test_history_and_effective_are_reachable(self):
        history = self.call({"action": contract.ACTION_MEMORY_HISTORY,
                             "run_id": self.run_id})
        self.assertTrue(history["ok"])
        effective = self.call({"action": contract.ACTION_MEMORY_EFFECTIVE,
                               "run_id": self.run_id,
                               "document_type": "session_summary"})
        self.assertTrue(effective["ok"])
        self.assertEqual(11, len(effective["result"]["claims"]))

    def test_an_append_persists_and_is_idempotent(self):
        first = self.correct(actor="heron", source_id="c-1")
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["result"]["created"])
        second = self.correct(actor="heron", source_id="c-1")
        self.assertFalse(second["result"]["created"])
        self.assertEqual(
            first["result"]["correction"]["id"], second["result"]["correction"]["id"]
        )
        reloaded, error = memory_store.load(self.base, self.run_id)
        self.assertIsNone(error)
        self.assertEqual(1, len(reloaded["corrections"]))
        self.assertEqual(memory.MEMORY_SCHEMA_VERSION, reloaded["schema_version"])

    def test_a_correction_never_changes_a_normalized_record_or_run_state(self):
        before, _ = memory_store.load(self.base, self.run_id)
        self.assertTrue(self.correct(operation="merge", text="HUMAN", source_id="c-1")["ok"])
        after, _ = memory_store.load(self.base, self.run_id)
        for array in ("events", "evidence", "decisions", "change_sets",
                      "code_entity_links", "projects", "work_packages"):
            with self.subTest(array=array):
                self.assertEqual(
                    memory.dumps(before[array]), memory.dumps(after[array])
                )
        self.assertEqual(
            before["agent_run"]["state"], after["agent_run"]["state"]
        )
        before_snapshot = memory._migration_snapshot(before)
        after_snapshot = memory._migration_snapshot(after)
        for field in ("run_id", "state", "ingest_sequence", "event_ids",
                      "evidence_ids", "decision_ids", "change_set_ids"):
            with self.subTest(field=field):
                self.assertEqual(before_snapshot[field], after_snapshot[field])
        # The human-revision arrays are the only thing that grew.
        self.assertEqual([], before_snapshot["correction_ids"])
        self.assertEqual(1, len(after_snapshot["correction_ids"]))

    def test_an_unknown_target_is_refused(self):
        result = self.call(client_core.build_memory_correction_request(
            "c1", self.run_id, "session_summary", "keep", "claim:nope"))
        self.assertFalse(result["ok"])
        self.assertEqual("memory_correction_refused", result["error"]["code"])

    def test_a_stale_expected_version_is_refused(self):
        self.assertTrue(self.correct(source_id="c-1")["ok"])
        version_id = self.call({"action": contract.ACTION_MEMORY_HISTORY,
                                "run_id": self.run_id})["result"]["generated_versions"][0]["id"]
        result = self.correct(source_id="c-2", expected_version_id="gendoc:stale:1")
        self.assertFalse(result["ok"])
        self.assertEqual("memory_correction_refused", result["error"]["code"])
        # The matching expectation is accepted.
        ok = self.correct(source_id="c-3", expected_version_id=version_id)
        self.assertTrue(ok["ok"], ok)

    def test_a_malformed_request_is_refused(self):
        for payload in (
            {"action": contract.ACTION_MEMORY_HISTORY},
            {"action": contract.ACTION_MEMORY_EFFECTIVE, "run_id": self.run_id},
            {"action": contract.ACTION_MEMORY_CORRECTION, "run_id": self.run_id,
             "document_type": "session_summary", "operation": "keep"},
            {"action": contract.ACTION_MEMORY_EFFECTIVE, "run_id": self.run_id,
             "document_type": "not_a_document"},
        ):
            with self.subTest(payload=payload.get("action")):
                result = self.call(payload)
                self.assertFalse(result["ok"])
                self.assertIn(
                    result["error"]["code"],
                    ("invalid_request", "memory_revision_invalid"),
                )

    def test_an_old_contract_version_is_rejected(self):
        for version in ("3.7.0", "3.6.0", "9.9.9"):
            with self.subTest(version=version):
                result = self.call(
                    {"action": contract.ACTION_MEMORY_HISTORY, "run_id": self.run_id},
                    contract_version=version,
                )
                self.assertEqual("unknown_contract_version", result["error"]["code"])

    def test_a_request_cannot_change_the_store_root(self):
        baseline = contract.dumps(self.call(
            {"action": contract.ACTION_MEMORY_HISTORY, "run_id": self.run_id}))
        for field, value in (("root", "/etc"), ("path", _PERSONAL),
                             ("base", "C:/Windows"), ("store_base", "/tmp"),
                             ("cwd", "/")):
            with self.subTest(field=field):
                injected = self.call(
                    {"action": contract.ACTION_MEMORY_HISTORY, "run_id": self.run_id,
                     field: value}
                )
                self.assertEqual(baseline, contract.dumps(injected))

    def test_the_client_builders_send_no_path(self):
        requests = [
            client_core.build_memory_history_request("c1", self.run_id),
            client_core.build_memory_effective_request("c1", self.run_id, "session_summary"),
            client_core.build_memory_correction_request(
                "c1", self.run_id, "session_summary", "keep", CLAIM),
        ]
        for request in requests:
            with self.subTest(action=request["action"]):
                self.assertEqual(contract.CONTRACT_VERSION, request["contract_version"])
                for forbidden in ("root", "path", "base", "store_base", "cwd"):
                    self.assertNotIn(forbidden, request)

    def test_no_prohibited_value_reaches_a_response(self):
        self.assertTrue(self.correct(operation="merge", text="HUMAN", source_id="c-1")["ok"])
        blobs = [
            contract.dumps(self.call({"action": contract.ACTION_MEMORY_HISTORY,
                                      "run_id": self.run_id})),
            contract.dumps(self.call({"action": contract.ACTION_MEMORY_EFFECTIVE,
                                      "run_id": self.run_id,
                                      "document_type": "session_summary"})),
        ]
        for blob in blobs:
            for forbidden in (_SECRET, _PERSONAL, "someone", ".ssh", "sha256:",
                              '"payload"', "content_fingerprint"):
                with self.subTest(forbidden=forbidden):
                    self.assertNotIn(forbidden, blob)

    def test_the_client_views_carry_no_prohibited_value(self):
        self.assertTrue(self.correct(operation="merge", text="HUMAN", source_id="c-1")["ok"])
        history = self.call({"action": contract.ACTION_MEMORY_HISTORY,
                             "run_id": self.run_id})["result"]
        effective = self.call({"action": contract.ACTION_MEMORY_EFFECTIVE,
                               "run_id": self.run_id,
                               "document_type": "session_summary"})["result"]
        blob = memory.dumps(client_core.memory_history_view(history)) + memory.dumps(
            client_core.memory_effective_view(effective)
        )
        self.assertTrue(blob)
        for forbidden in (_SECRET, _PERSONAL, "someone", "sha256:"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)

    def test_prior_memory_actions_still_work(self):
        for action, extra in (
            (contract.ACTION_MEMORY_DOCUMENTS, {}),
            (contract.ACTION_MEMORY_SEARCH, {}),
            (contract.ACTION_MEMORY_RESUME, {}),
        ):
            with self.subTest(action=action):
                result = self.call({"action": action, **extra})
                self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()
