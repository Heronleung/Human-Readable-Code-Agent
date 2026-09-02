"""Tests for the deterministic Twin Draft and Intent Delta domain (P3.4).

These tests exercise :mod:`hrca.twin_draft` — the Qt-free, dependency-free
editable Code Map domain — against a synthetic baseline Twin store so draft
creation, read-only-field rejection, no-op handling, conflict/stale detection
and Intent Delta determinism can be driven precisely without touching the
filesystem or a real repository.
"""

from __future__ import annotations

import json
import unittest

from hrca import twin, twin_draft
from hrca.twin import ARTIFACT_FUNCTION, BEHAVIOR_CONDITIONS

_WS = twin.workspace_id_for("/tmp/workspace")


def _sr(lineno: int) -> dict:
    return {"lineno": lineno, "col_offset": 0, "end_lineno": lineno, "end_col_offset": 0}


def _fn(path: str, name: str, **kw) -> dict:
    mod = path.replace("/", ".").replace(".py", "")
    sym = {
        "id": f"{mod}.{name}",
        "kind": "function",
        "name": name,
        "parent_id": mod,
        "file": path,
        "confidence": "high",
        "source_range": _sr(2),
    }
    sym.update(kw)
    return sym


def _doc(specs) -> dict:
    files, symbols, relations = [], [], []
    for spec in specs:
        path = spec[0]
        syms = spec[1]
        rels = spec[2] if len(spec) > 2 else []
        mod = path.replace("/", ".").replace(".py", "")
        files.append({"path": path, "module": mod, "size_bytes": 1, "syntax_status": "ok"})
        symbols.append(
            {"id": mod, "kind": "module", "name": mod, "file": path, "source_range": _sr(1)}
        )
        for sym in syms:
            sym = dict(sym)
            sym.setdefault("file", path)
            symbols.append(sym)
        relations.extend(rels)
    return {"files": files, "symbols": symbols, "relations": relations,
            "parse_errors": [], "confidence": "high"}


def _fingerprints(doc: dict) -> dict:
    return {f["path"]: f"fp:{f['path']}" for f in doc["files"]}


def _baseline_store():
    doc = _doc([("a.py", [_fn("a.py", "f")], [])])
    return twin.build_store(doc, _fingerprints(doc), _WS, 1, "T")


def _file_target() -> str:
    return twin.file_artifact_id("a.py")


def _fn_target() -> str:
    return twin.symbol_artifact_id("a.f", ARTIFACT_FUNCTION)


def _behavior_target() -> str:
    return twin.behavior_node_id("a.f", BEHAVIOR_CONDITIONS, 0)


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------
class NormalizationTests(unittest.TestCase):
    def test_single_field_strips_and_returns_none_when_empty(self):
        self.assertEqual(twin_draft.normalize_value("purpose", "  do a thing  "), "do a thing")
        self.assertIsNone(twin_draft.normalize_value("purpose", "   "))
        self.assertIsNone(twin_draft.normalize_value("purpose", None))

    def test_single_field_rejects_non_string(self):
        with self.assertRaises(ValueError):
            twin_draft.normalize_value("purpose", 5)

    def test_list_field_strips_and_drops_blanks(self):
        self.assertEqual(
            twin_draft.normalize_value("workflow_steps", ["  a  ", "", "b"]),
            ["a", "b"],
        )
        self.assertIsNone(twin_draft.normalize_value("workflow_steps", ["  ", ""]))
        self.assertIsNone(twin_draft.normalize_value("workflow_steps", []))

    def test_list_field_rejects_non_string_items(self):
        with self.assertRaises(ValueError):
            twin_draft.normalize_value("workflow_steps", ["a", 1])

    def test_oversized_value_is_rejected(self):
        with self.assertRaises(ValueError):
            twin_draft.normalize_value("purpose", "x" * (twin_draft.MAX_FIELD_VALUE_CHARS + 1))


# ---------------------------------------------------------------------------
# Draft building and read-only rejection
# ---------------------------------------------------------------------------
class BuildDraftTests(unittest.TestCase):
    def test_draft_builds_with_changes_sorted_and_original_none(self):
        store = _baseline_store()
        draft, err = twin_draft.build_draft(
            _WS, store, [
                {"target_id": _file_target(), "field": "purpose", "proposed": "entry point"},
            ], "C", "U",
        )
        self.assertIsNone(err)
        self.assertEqual(draft["origin"], "user_authored")
        self.assertEqual(draft["workspace_id"], _WS)
        self.assertEqual(draft["changes"], [
            {"target_id": _file_target(), "field": "purpose",
             "original": None, "proposed": "entry point"},
        ])
        self.assertEqual(draft["validation"]["state"], "valid")
        self.assertEqual(draft["conflict"]["state"], "none")

    def test_draft_is_deterministic(self):
        store = _baseline_store()
        edits = [{"target_id": _file_target(), "field": "purpose", "proposed": "p"}]
        a, _ = twin_draft.build_draft(_WS, store, edits, "C", "U")
        b, _ = twin_draft.build_draft(_WS, store, edits, "C", "U")
        self.assertEqual(twin_draft.dumps(a), twin_draft.dumps(b))
        self.assertNotIn("\n", twin_draft.dumps(a))

    def test_content_addressed_draft_id(self):
        store = _baseline_store()
        edits = [{"target_id": _file_target(), "field": "purpose", "proposed": "p"}]
        a, _ = twin_draft.build_draft(_WS, store, edits, "C", "U")
        b, _ = twin_draft.build_draft(_WS, store, edits, "C", "U")
        self.assertEqual(a["draft_id"], b["draft_id"])
        c, _ = twin_draft.build_draft(_WS, store, [
            {"target_id": _file_target(), "field": "purpose", "proposed": "q"},
        ], "C", "U")
        self.assertNotEqual(a["draft_id"], c["draft_id"])

    def test_read_only_field_is_rejected(self):
        store = _baseline_store()
        for field in ("path", "locator", "source_range", "provenance", "sync_state", "id"):
            draft, err = twin_draft.build_draft(
                _WS, store, [{"target_id": _file_target(), "field": field, "proposed": "x"}],
                "C", "U",
            )
            self.assertIsNone(draft, field)
            self.assertEqual(err, "field is a read-only source fact", field)

    def test_unknown_field_is_rejected(self):
        store = _baseline_store()
        draft, err = twin_draft.build_draft(
            _WS, store, [{"target_id": _file_target(), "field": "not_a_field", "proposed": "x"}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, "field is not editable for this target")

    def test_unknown_target_is_rejected(self):
        store = _baseline_store()
        draft, err = twin_draft.build_draft(
            _WS, store, [{"target_id": "artifact:file:missing.py", "field": "purpose",
                          "proposed": "x"}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, "unknown target")

    def test_field_unsupported_for_target_kind_is_rejected(self):
        store = _baseline_store()
        # ``workflow_steps`` is behavior-only; a file artifact cannot carry it.
        draft, err = twin_draft.build_draft(
            _WS, store, [{"target_id": _file_target(), "field": "workflow_steps",
                          "proposed": ["a"]}],
            "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, "field is not editable for this target")

    def test_duplicate_edit_is_rejected(self):
        store = _baseline_store()
        draft, err = twin_draft.build_draft(
            _WS, store, [
                {"target_id": _file_target(), "field": "purpose", "proposed": "a"},
                {"target_id": _file_target(), "field": "purpose", "proposed": "b"},
            ], "C", "U",
        )
        self.assertIsNone(draft)
        self.assertEqual(err, "duplicate edit for the same target field")

    def test_behavior_target_resolves_to_symbol_artifact(self):
        store = _baseline_store()
        draft, err = twin_draft.build_draft(
            _WS, store, [{"target_id": _behavior_target(), "field": "purpose",
                          "proposed": "branch"}],
            "C", "U",
        )
        self.assertIsNone(err)
        target = draft["targets"][0]
        self.assertEqual(target["target_kind"], "behavior")
        self.assertEqual(target["source_artifact"]["id"], _fn_target())

    def test_noop_draft(self):
        store = _baseline_store()
        draft, err = twin_draft.build_draft(_WS, store, [], "C", "U")
        self.assertIsNone(err)
        self.assertTrue(twin_draft.is_noop(draft))
        self.assertEqual(draft["changes"], [])


# ---------------------------------------------------------------------------
# Conflict / stale detection
# ---------------------------------------------------------------------------
class ConflictTests(unittest.TestCase):
    def test_current_baseline_is_not_stale(self):
        store = _baseline_store()
        draft, _ = twin_draft.build_draft(
            _WS, store, [{"target_id": _file_target(), "field": "purpose", "proposed": "p"}],
            "C", "U",
        )
        self.assertEqual(twin_draft.conflict_for(draft, store)["state"], "none")

    def test_changed_baseline_marks_stale(self):
        store = _baseline_store()
        draft, _ = twin_draft.build_draft(
            _WS, store, [{"target_id": _file_target(), "field": "purpose", "proposed": "p"}],
            "C", "U",
        )
        # Simulate a re-sync that changed the baseline fingerprint.
        changed = json.loads(json.dumps(store))
        changed["workspace_revision"]["baseline_fingerprint"] = "fp:changed"
        conflict = twin_draft.conflict_for(draft, changed)
        self.assertEqual(conflict["state"], "stale")
        self.assertEqual(conflict["old_baseline"], store["workspace_revision"]["baseline_fingerprint"])
        self.assertEqual(conflict["current_baseline"], "fp:changed")
        self.assertIn(_file_target(), conflict["affected_targets"])
        self.assertEqual(conflict["safe_actions"], ["discard", "reset", "compare"])


# ---------------------------------------------------------------------------
# Intent Delta
# ---------------------------------------------------------------------------
class IntentDeltaTests(unittest.TestCase):
    def _draft(self, store, edits):
        return twin_draft.build_draft(_WS, store, edits, "C", "U")[0]

    def test_noop_draft_yields_no_change(self):
        store = _baseline_store()
        draft = self._draft(store, [])
        delta, err = twin_draft.generate_intent_delta(draft, store)
        self.assertIsNone(delta)
        self.assertEqual(err, "no_change")

    def test_stale_draft_blocks_intent_delta(self):
        store = _baseline_store()
        draft = self._draft(store, [{"target_id": _file_target(), "field": "purpose",
                                     "proposed": "p"}])
        changed = json.loads(json.dumps(store))
        changed["workspace_revision"]["baseline_fingerprint"] = "fp:changed"
        delta, err = twin_draft.generate_intent_delta(draft, changed)
        self.assertIsNone(delta)
        self.assertEqual(err, "stale")

    def test_intent_delta_is_deterministic_and_not_executable(self):
        store = _baseline_store()
        edits = [{"target_id": _file_target(), "field": "purpose", "proposed": "p"},
                 {"target_id": _file_target(), "field": "invariants", "proposed": ["x > 0"]}]
        draft = self._draft(store, edits)
        a, err = twin_draft.generate_intent_delta(draft, store)
        self.assertIsNone(err)
        b, _ = twin_draft.generate_intent_delta(draft, store)
        self.assertEqual(twin_draft.dumps(a), twin_draft.dumps(b))
        self.assertFalse(a["executable"])
        self.assertEqual(a["intent"], "user_authored")
        self.assertEqual(a["draft_id"], draft["draft_id"])
        self.assertEqual(a["conflict_state"], "none")

    def test_intent_delta_shape_and_derived_fields(self):
        store = _baseline_store()
        edits = [
            {"target_id": _file_target(), "field": "invariants", "proposed": ["x > 0"]},
            {"target_id": _file_target(), "field": "limitations", "proposed": ["see ticket"]},
            {"target_id": _behavior_target(), "field": "purpose", "proposed": "branch"},
        ]
        draft = self._draft(store, edits)
        delta, err = twin_draft.generate_intent_delta(draft, store)
        self.assertIsNone(err)
        self.assertEqual(delta["constraints"], ["x > 0"])
        self.assertIn("see ticket", delta["unresolved"])
        self.assertIn(_behavior_target(), delta["affected_behavior_nodes"])
        self.assertIn(_fn_target(), delta["affected_sources"])
        # One acceptance criterion per changed field.
        self.assertEqual(len(delta["acceptance_criteria"]), 3)
        # ``before``/``after`` mirror original/proposed.
        self.assertEqual(
            {c["target_id"] for c in delta["changes"]},
            {_file_target(), _behavior_target()},
        )

    def test_intent_delta_is_ascii_single_line(self):
        store = _baseline_store()
        draft = self._draft(store, [{"target_id": _file_target(), "field": "purpose",
                                     "proposed": "p"}])
        delta, _ = twin_draft.generate_intent_delta(draft, store)
        text = twin_draft.dumps(delta)
        self.assertNotIn("\n", text)
        self.assertTrue(all(ord(ch) < 128 for ch in text))


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------
class MigrationTests(unittest.TestCase):
    def test_current_version_passes_through(self):
        draft, err = twin_draft.migrate_draft(
            {"schema_version": twin_draft.DRAFT_SCHEMA_VERSION, "x": 1}
        )
        self.assertIsNone(err)
        self.assertEqual(draft["x"], 1)

    def test_future_version_rejected(self):
        draft, err = twin_draft.migrate_draft({"schema_version": "99.0.0"})
        self.assertIsNone(draft)
        self.assertIsNotNone(err)

    def test_malformed_rejected(self):
        for bad in ([], "x", None, {}, {"schema_version": 5}):
            draft, err = twin_draft.migrate_draft(bad)
            self.assertIsNone(draft, bad)
            self.assertIsNotNone(err, bad)


if __name__ == "__main__":
    unittest.main()
