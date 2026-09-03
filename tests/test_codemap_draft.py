"""Tests for the deterministic Code Map Draft and Intent Delta (P3.4).

These tests exercise :mod:`hrca.codemap_draft` — the typed-operation draft model
that replaces the field-based Twin Draft — against a synthetic Code Map
baseline so documentation vs. behavior intent, before/proposed typed payloads,
no-op handling, draft-scoped identity, conflict/stale detection and Intent
Delta determinism can be driven precisely without touching a repository.
"""

from __future__ import annotations

import os
import unittest

from hrca import codemap, codemap_draft, scanner

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "codemap_fixtures"
)

_WS = "ws:test"


def _build(name: str, rev: str = "rev-1"):
    path = os.path.join(_FIXTURE_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    module = scanner.module_name_for(name)
    return codemap.build_codemap(source, name, module, rev)


def _baseline(rev: str = "rev-1"):
    return codemap_draft.baseline_document(_build("calculator.py", rev), rev)


def _block_id(name: str, block_type: str, substr: str = "", rev: str = "rev-1"):
    for block in _build(name, rev):
        if block["block_type"] == block_type and (not substr or substr in block["block_id"]):
            return block["block_id"]
    raise AssertionError(f"no {block_type} block matching {substr!r}")


def _purpose_add(rev: str = "rev-1"):
    return _block_id("calculator.py", codemap.BT_PURPOSE, "calculator.add", rev)


def _decision_divide(rev: str = "rev-1"):
    return _block_id("calculator.py", codemap.BT_DECISION, "calculator.divide", rev)


def _draft(operations, baseline=None, ws=_WS):
    baseline = baseline or _baseline()
    return codemap_draft.build_draft(ws, baseline, operations, "C", "U")[0]


# ---------------------------------------------------------------------------
# Group 5 — typed operations and intent classification
# ---------------------------------------------------------------------------
class OperationTests(unittest.TestCase):
    def test_replace_description_is_documentation_intent(self):
        draft = _draft([
            {"op": "replace_description", "target_block_id": _purpose_add(),
             "proposed_text": "Add two floats and return their sum."},
        ])
        op = draft["operations"][0]
        self.assertEqual(op["intent_class"], "documentation_intent")
        self.assertEqual(op["proposed"]["payload"]["text"], "Add two floats and return their sum.")
        self.assertEqual(op["proposed"]["display_text"], "Add two floats and return their sum.")
        self.assertIn("Return the sum", op["before"]["display_text"])
        self.assertIsNotNone(op["before_fingerprint"])
        self.assertEqual(op["owning_entity_id"], "calculator.add")

    def test_replace_condition_intent_is_behavior_intent(self):
        draft = _draft([
            {"op": "replace_condition_intent", "target_block_id": _decision_divide(),
             "proposed_condition": "right != 0"},
        ])
        op = draft["operations"][0]
        self.assertEqual(op["intent_class"], "behavior_change_intent")
        self.assertEqual(op["proposed"]["payload"]["condition"], "right != 0")
        self.assertEqual(op["proposed"]["display_text"], "If right != 0 is true, the following runs:")
        self.assertEqual(op["owning_entity_id"], "calculator.divide")

    def test_insert_block_has_draft_scoped_id_and_no_anchor(self):
        draft = _draft([
            {"op": "insert_block", "owning_entity_id": "calculator.add",
             "block_type": "note", "proposed_text": "a draft note"},
        ])
        op = draft["operations"][0]
        self.assertTrue(op["target_block_id"].startswith("codemap:draft:"))
        self.assertIsNone(op["before"])
        self.assertIsNone(op["before_fingerprint"])
        self.assertEqual(op["proposed"]["display_text"], "Note: a draft note")
        self.assertEqual(op["intent_class"], "documentation_intent")
        # No source anchor is fabricated for a draft-inserted block.
        self.assertNotIn("source_anchors", op["proposed"])

    def test_insert_step_is_behavior_intent(self):
        draft = _draft([
            {"op": "insert_block", "owning_entity_id": "calculator.add",
             "block_type": "step", "proposed_payload": {"operation": "assign"},
             "proposed_text": "total is assigned 0"},
        ])
        self.assertEqual(draft["operations"][0]["intent_class"], "behavior_change_intent")

    def test_read_only_block_rejects_replace_description(self):
        entity_id = _block_id("calculator.py", codemap.BT_ENTITY, "calculator.add")
        draft, err = codemap_draft.build_draft(
            _WS, _baseline(),
            [{"op": "replace_description", "target_block_id": entity_id,
              "proposed_text": "x"}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, codemap_draft.REASON_UNSUPPORTED_OP)

    def test_replace_condition_on_non_decision_is_rejected(self):
        draft, err = codemap_draft.build_draft(
            _WS, _baseline(),
            [{"op": "replace_condition_intent", "target_block_id": _purpose_add(),
              "proposed_condition": "x"}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, codemap_draft.REASON_UNSUPPORTED_OP)

    def test_unknown_target_is_rejected(self):
        draft, err = codemap_draft.build_draft(
            _WS, _baseline(),
            [{"op": "replace_description", "target_block_id": "codemap:nope:purpose:0",
              "proposed_text": "x"}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, codemap_draft.REASON_UNKNOWN_TARGET)

    def test_duplicate_operation_is_rejected(self):
        draft, err = codemap_draft.build_draft(
            _WS, _baseline(),
            [
                {"op": "replace_description", "target_block_id": _purpose_add(), "proposed_text": "a"},
                {"op": "replace_description", "target_block_id": _purpose_add(), "proposed_text": "b"},
            ],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, codemap_draft.REASON_DUPLICATE)

    def test_delete_draft_block_requires_a_draft_target(self):
        draft, err = codemap_draft.build_draft(
            _WS, _baseline(),
            [{"op": "delete_draft_block", "target_block_id": _purpose_add()}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, codemap_draft.REASON_NOT_A_DRAFT_BLOCK)

    def test_mark_unresolved_and_restore_toggle_state(self):
        draft = _draft([
            {"op": "mark_unresolved", "target_block_id": _purpose_add(), "reason": "review"},
        ])
        op = draft["operations"][0]
        self.assertEqual(op["proposed"]["state"], "unsupported")
        self.assertEqual(op["proposed"]["reason"], "review")
        self.assertIsNone(op["proposed_fingerprint"])

    def test_draft_does_not_modify_source_blocks(self):
        baseline = _baseline()
        before = codemap.dumps(baseline["blocks"])
        _draft([{"op": "replace_description", "target_block_id": _purpose_add(),
                 "proposed_text": "changed"}], baseline=baseline)
        self.assertEqual(codemap.dumps(baseline["blocks"]), before)

    def test_noop_draft(self):
        draft = _draft([])
        self.assertTrue(codemap_draft.is_noop(draft))
        self.assertEqual(draft["operations"], [])

    def test_draft_is_deterministic_and_ascii_single_line(self):
        ops = [{"op": "replace_description", "target_block_id": _purpose_add(),
                "proposed_text": "p"}]
        a = _draft(ops)
        b = _draft(ops)
        self.assertEqual(codemap_draft.dumps(a), codemap_draft.dumps(b))
        self.assertNotIn("\n", codemap_draft.dumps(a))
        self.assertTrue(all(ord(ch) < 128 for ch in codemap_draft.dumps(a)))


# ---------------------------------------------------------------------------
# Group 5/6 — Intent Delta
# ---------------------------------------------------------------------------
class IntentDeltaTests(unittest.TestCase):
    def test_noop_draft_yields_no_change(self):
        delta, err = codemap_draft.generate_intent_delta(_draft([]), _baseline())
        self.assertIsNone(delta)
        self.assertEqual(err, "no_change")

    def test_stale_draft_blocks_delta(self):
        draft = _draft([{"op": "replace_description", "target_block_id": _purpose_add(),
                         "proposed_text": "p"}])
        delta, err = codemap_draft.generate_intent_delta(draft, _baseline("rev-2"))
        self.assertIsNone(delta)
        self.assertEqual(err, "stale")

    def test_delta_is_deterministic_and_not_executable(self):
        ops = [
            {"op": "replace_description", "target_block_id": _purpose_add(), "proposed_text": "p"},
            {"op": "replace_condition_intent", "target_block_id": _decision_divide(),
             "proposed_condition": "right != 0"},
        ]
        draft = _draft(ops)
        a, err = codemap_draft.generate_intent_delta(draft, _baseline())
        self.assertIsNone(err)
        b, _ = codemap_draft.generate_intent_delta(draft, _baseline())
        self.assertEqual(codemap_draft.dumps(a), codemap_draft.dumps(b))
        self.assertFalse(a["executable"])
        self.assertEqual(a["intent"], "user_authored")
        self.assertEqual(a["draft_id"], draft["draft_id"])
        self.assertEqual(a["conflict_state"], "none")

    def test_delta_entry_shape_and_approval_level(self):
        ops = [
            {"op": "replace_description", "target_block_id": _purpose_add(), "proposed_text": "p"},
            {"op": "replace_condition_intent", "target_block_id": _decision_divide(),
             "proposed_condition": "right != 0"},
        ]
        delta, err = codemap_draft.generate_intent_delta(_draft(ops), _baseline())
        self.assertIsNone(err)
        by_op = {e["operation"]: e for e in delta["entries"]}
        self.assertEqual(by_op["replace_description"]["required_approval_level"], "low")
        self.assertEqual(by_op["replace_condition_intent"]["required_approval_level"], "high")
        for entry in delta["entries"]:
            for key in (
                "operation", "target_block_id", "owning_entity_id", "baseline_revision",
                "before_fingerprint", "proposed_fingerprint", "before", "proposed",
                "intent_class", "affected_source_artifacts", "acceptance_criteria",
                "required_approval_level",
            ):
                self.assertIn(key, entry)
        self.assertEqual(by_op["replace_description"]["owning_entity_id"], "calculator.add")

    def test_delta_reports_static_callers(self):
        # The ``handle`` method calls ``self._run`` and ``self._cleanup``.
        draft = _draft([
            {"op": "replace_description",
             "target_block_id": _block_id("procedural.py", codemap.BT_PURPOSE, "Service.handle"),
             "proposed_text": "new"},
        ], baseline=codemap_draft.baseline_document(_build("procedural.py"), "rev-1"))
        delta, err = codemap_draft.generate_intent_delta(draft, codemap_draft.baseline_document(_build("procedural.py"), "rev-1"))
        self.assertIsNone(err)
        entry = delta["entries"][0]
        self.assertTrue(entry["known_callers"])
        self.assertEqual(entry["owning_entity_id"], "procedural.Service.handle")


# ---------------------------------------------------------------------------
# Group 6 — stale/conflict
# ---------------------------------------------------------------------------
class ConflictTests(unittest.TestCase):
    def test_current_baseline_is_not_stale(self):
        draft = _draft([{"op": "replace_description", "target_block_id": _purpose_add(),
                         "proposed_text": "p"}])
        self.assertEqual(codemap_draft.conflict_for(draft, _baseline())["state"], "none")

    def test_changed_baseline_marks_stale(self):
        draft = _draft([{"op": "replace_description", "target_block_id": _purpose_add(),
                         "proposed_text": "p"}])
        conflict = codemap_draft.conflict_for(draft, _baseline("rev-2"))
        self.assertEqual(conflict["state"], "stale")
        self.assertEqual(conflict["old_baseline"], "rev-1")
        self.assertEqual(conflict["current_baseline"], "rev-2")
        self.assertIn(_purpose_add(), conflict["affected_targets"])
        self.assertEqual(conflict["safe_actions"], ["discard", "reset", "compare"])


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------
class MigrationTests(unittest.TestCase):
    def test_current_version_passes_through(self):
        draft, err = codemap_draft.migrate_draft(
            {"schema_version": codemap_draft.CODEMAP_DRAFT_SCHEMA_VERSION, "x": 1}
        )
        self.assertIsNone(err)
        self.assertEqual(draft["x"], 1)

    def test_future_version_rejected(self):
        draft, err = codemap_draft.migrate_draft({"schema_version": "99.0.0"})
        self.assertIsNone(draft)
        self.assertIsNotNone(err)

    def test_malformed_rejected(self):
        for bad in ([], "x", None, {}, {"schema_version": 5}):
            draft, err = codemap_draft.migrate_draft(bad)
            self.assertIsNone(draft, bad)
            self.assertIsNotNone(err, bad)


if __name__ == "__main__":
    unittest.main()
