"""Tests for the deterministic Structured Code Twin domain (P3.3).

These tests exercise :mod:`hrca.twin` — the Qt-free, dependency-free Twin
domain — against synthetic scanner documents so the incremental-synchronization
state machine (no-change, modification, removal, rename, ambiguous move,
syntax-error staleness and human-draft conflict) can be driven precisely
without touching the filesystem.
"""

from __future__ import annotations

import json
import unittest

from hrca import twin
from hrca.twin import (
    ARTIFACT_CLASS,
    ARTIFACT_FILE,
    ARTIFACT_FUNCTION,
    ARTIFACT_METHOD,
    BEHAVIOR_CALLS,
    BEHAVIOR_CONDITIONS,
    BEHAVIOR_DEPENDENCIES,
    BEHAVIOR_EXCEPTIONS,
    BEHAVIOR_INPUTS,
    BEHAVIOR_INVARIANTS,
    BEHAVIOR_LOOPS,
    BEHAVIOR_OUTPUTS,
    BEHAVIOR_SIDE_EFFECTS,
    CONF_HIGH,
    CONF_LOW,
    PROVENANCE_UNRESOLVED,
    PROVENANCE_VERIFIED,
    PROVENANCES,
    SYNC_CONFLICT,
    SYNC_NEEDS_REVIEW,
    SYNC_NO_CHANGE,
    SYNC_STALE,
    SYNC_SYNCHRONIZED,
    TWIN_SCHEMA_VERSION,
    baseline_fingerprint,
    build_store,
    dumps,
    migrate_store,
    sync_twin,
    workspace_id_for,
)

_WS = workspace_id_for("/tmp/workspace")


def _sr(lineno: int) -> dict:
    return {"lineno": lineno, "col_offset": 0, "end_lineno": lineno, "end_col_offset": 0}


def _fn(path: str, name: str, **kw) -> dict:
    """Return a function symbol in ``path`` with qualified id ``<mod>.<name>``."""
    mod = path.replace("/", ".").replace(".py", "")
    sym = {
        "id": f"{mod}.{name}",
        "kind": "function",
        "name": name,
        "parent_id": mod,
        "file": path,
        "confidence": CONF_HIGH,
        "source_range": _sr(2),
    }
    sym.update(kw)
    return sym


def _rel(source: str, kind: str, target: str, **kw) -> dict:
    rel = {"source": source, "kind": kind, "target": target, "source_range": _sr(3)}
    rel.update(kw)
    return rel


def _doc(specs) -> dict:
    """Build a scanner document from ``(path, symbols[, relations])`` tuples."""
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
    """Fingerprints derived from a fixed per-path token (deterministic, stable)."""
    return {f["path"]: f"fp:{f['path']}" for f in doc["files"]}


def _artifacts(store: dict, kind: str) -> list:
    return [r for r in store["artifacts"] if r["kind"] == kind]


def _behaviors(store: dict, category: str) -> list:
    return [r for r in store["behavior_nodes"] if r["category"] == category]


def _record_index(store: dict) -> dict:
    return {r["id"]: r for key in ("artifacts", "behavior_nodes", "correspondences",
                                   "projections") for r in store[key]}


# ---------------------------------------------------------------------------
# Deterministic identifiers and fingerprints
# ---------------------------------------------------------------------------
class IdentifierTests(unittest.TestCase):
    def test_workspace_id_is_deterministic_and_distinct(self):
        self.assertEqual(workspace_id_for("/a"), workspace_id_for("/a"))
        self.assertNotEqual(workspace_id_for("/a"), workspace_id_for("/b"))

    def test_artifact_ids_are_stable_and_prefixed(self):
        self.assertEqual(twin.file_artifact_id("a/b.py"), "artifact:file:a/b.py")
        self.assertEqual(
            twin.symbol_artifact_id("pkg.mod.Func", ARTIFACT_FUNCTION),
            "artifact:function:pkg.mod.Func",
        )

    def test_baseline_fingerprint_changes_only_with_content(self):
        fps = {"a.py": "x", "b.py": "y"}
        self.assertEqual(baseline_fingerprint(fps), baseline_fingerprint(dict(fps)))
        self.assertNotEqual(baseline_fingerprint(fps), baseline_fingerprint({"a.py": "x2", "b.py": "y"}))
        self.assertNotEqual(baseline_fingerprint(fps), baseline_fingerprint({"a.py": "x"}))  # removal
        self.assertNotEqual(baseline_fingerprint(fps), baseline_fingerprint({**fps, "c.py": "z"}))  # add


class MigrationTests(unittest.TestCase):
    def test_current_version_passes_through(self):
        store, err = migrate_store({"schema_version": TWIN_SCHEMA_VERSION, "x": 1})
        self.assertIsNone(err)
        self.assertEqual(store, {"schema_version": TWIN_SCHEMA_VERSION, "x": 1})

    def test_future_version_is_rejected(self):
        store, err = migrate_store({"schema_version": "99.0.0"})
        self.assertIsNone(store)
        self.assertIsNotNone(err)

    def test_unknown_old_version_is_rejected(self):
        store, err = migrate_store({"schema_version": "0.0.1"})
        self.assertIsNone(store)
        self.assertIsNotNone(err)

    def test_malformed_store_is_rejected(self):
        for bad in ([], "x", None, {}, {"schema_version": 5}):
            store, err = migrate_store(bad)
            self.assertIsNone(store, bad)
            self.assertIsNotNone(err, bad)


# ---------------------------------------------------------------------------
# Full build
# ---------------------------------------------------------------------------
class BuildStoreTests(unittest.TestCase):
    def test_build_is_deterministic(self):
        doc = _doc([
            ("app.py", [_fn("app.py", "run", is_method=False)], []),
            ("mod.py", [_fn("mod.py", "helper")], []),
        ])
        fps = _fingerprints(doc)
        a = build_store(doc, fps, _WS, 1, "T")
        b = build_store(doc, fps, _WS, 1, "T")
        self.assertEqual(dumps(a), dumps(b))

    def test_build_store_models_pyi_stub_files(self):
        # ``.pyi`` type stubs are modelled like ``.py`` modules: a file artifact
        # plus symbol artifacts with stable, suffix-stripped qualified IDs.
        doc = {
            "files": [{"path": "app/stubs.pyi", "module": "app.stubs",
                       "size_bytes": 1, "syntax_status": "ok"}],
            "symbols": [
                {"id": "app.stubs", "kind": "module", "name": "app.stubs",
                 "file": "app/stubs.pyi", "source_range": _sr(1)},
                {"id": "app.stubs.parse", "kind": "function", "name": "parse",
                 "parent_id": "app.stubs", "file": "app/stubs.pyi",
                 "confidence": CONF_HIGH, "source_range": _sr(2)},
            ],
            "relations": [],
            "parse_errors": [],
            "confidence": "high",
        }
        store = build_store(doc, {"app/stubs.pyi": "fp:stub"}, _WS, 1, "T")
        file_artifacts = _artifacts(store, ARTIFACT_FILE)
        self.assertEqual([r["path"] for r in file_artifacts], ["app/stubs.pyi"])
        fn = [r for r in store["artifacts"] if r["kind"] == ARTIFACT_FUNCTION]
        self.assertEqual([r["locator"] for r in fn], ["app.stubs.parse"])

    def test_dumps_is_ascii_and_single_line(self):
        doc = _doc([("a.py", [_fn("a.py", "f", return_annotation="dict")], [])])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        text = dumps(store)
        self.assertNotIn("\n", text)
        self.assertTrue(all(ord(ch) < 128 for ch in text))

    def test_artifact_kinds_map_scanner_symbols(self):
        doc = _doc([
            ("a.py", [
                _fn("a.py", "f"),
                _fn("a.py", "g", async_=True),
                _fn("a.py", "m", is_method=True),
                {"id": "a.Cls", "kind": "class", "name": "Cls", "parent_id": "a",
                 "confidence": CONF_HIGH, "source_range": _sr(4)},
            ], []),
        ])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        kinds = {r["locator"]: r["kind"] for r in store["artifacts"] if r["kind"] != ARTIFACT_FILE}
        self.assertEqual(kinds["a.f"], ARTIFACT_FUNCTION)
        self.assertEqual(kinds["a.g"], ARTIFACT_FUNCTION)
        self.assertEqual(kinds["a.m"], ARTIFACT_METHOD)
        self.assertEqual(kinds["a.Cls"], ARTIFACT_CLASS)

    def test_only_verified_and_unresolved_provenance_emitted(self):
        doc = _doc([
            ("a.py", [_fn("a.py", "f")], [_rel("a.f", "calls", "builtins.print")]),
        ])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        for key in ("artifacts", "behavior_nodes", "projections"):
            for rec in store[key]:
                self.assertIn(rec["provenance"], PROVENANCES, rec)
        self.assertTrue(all(r["provenance"] in {PROVENANCE_VERIFIED, PROVENANCE_UNRESOLVED}
                            for r in store["artifacts"]))
        self.assertTrue(all(r["provenance"] in {PROVENANCE_VERIFIED, PROVENANCE_UNRESOLVED}
                            for r in store["behavior_nodes"]))

    def test_supported_behavior_is_verified(self):
        doc = _doc([
            ("a.py", [_fn("a.py", "f")], [
                _rel("a.f", "returns", "int"),
                _rel("a.f", "calls", "print"),
                _rel("a.f", "raises", "ValueError"),
                _rel("a.f", "imports", "os"),
            ]),
        ])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        cats = {b["category"]: b for b in store["behavior_nodes"]}
        self.assertIn(BEHAVIOR_OUTPUTS, cats)
        self.assertEqual(cats[BEHAVIOR_OUTPUTS]["items"], ["int"])
        self.assertEqual(cats[BEHAVIOR_OUTPUTS]["provenance"], PROVENANCE_VERIFIED)
        self.assertIn(BEHAVIOR_CALLS, cats)
        self.assertIn(BEHAVIOR_EXCEPTIONS, cats)
        self.assertIn(BEHAVIOR_DEPENDENCIES, cats)
        self.assertEqual(cats[BEHAVIOR_DEPENDENCIES]["items"], ["os"])

    def test_unsupported_behavior_is_explicitly_unresolved(self):
        doc = _doc([("a.py", [_fn("a.py", "f")], [])])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        for category in (BEHAVIOR_CONDITIONS, BEHAVIOR_LOOPS, BEHAVIOR_SIDE_EFFECTS, BEHAVIOR_INVARIANTS):
            nodes = _behaviors(store, category)
            self.assertEqual(len(nodes), 1, category)
            self.assertEqual(nodes[0]["provenance"], PROVENANCE_UNRESOLVED)
            self.assertEqual(nodes[0]["confidence"], CONF_LOW)
            self.assertEqual(nodes[0]["items"], [])
            self.assertIn("reason", nodes[0])

    def test_unresolved_dependency_is_low_confidence(self):
        doc = _doc([
            ("a.py", [_fn("a.py", "f")], [_rel("a.f", "imports", None, status="unresolved")]),
        ])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        deps = _behaviors(store, BEHAVIOR_DEPENDENCIES)
        self.assertEqual(len(deps), 1)
        self.assertEqual(deps[0]["confidence"], CONF_LOW)
        self.assertEqual(deps[0]["items"], ["<unresolved>"])

    def test_inputs_come_from_parameters(self):
        doc = _doc([
            ("a.py", [_fn("a.py", "f")], []),
        ])
        doc["symbols"].append(
            {"id": "a.f.x", "kind": "parameter", "name": "x", "parent_id": "a.f",
             "file": "a.py", "type_annotation": "int", "source_range": _sr(2)}
        )
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        inputs = _behaviors(store, BEHAVIOR_INPUTS)
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["items"], ["x: int"])
        self.assertEqual(inputs[0]["provenance"], PROVENANCE_VERIFIED)

    def test_classes_get_projection_but_no_behavior_nodes(self):
        doc = _doc([
            ("a.py", [{"id": "a.Cls", "kind": "class", "name": "Cls", "parent_id": "a",
                       "confidence": CONF_HIGH, "source_range": _sr(4)}], []),
        ])
        store = build_store(doc, _fingerprints(doc), _WS, 1, "T")
        cls_artifact = _artifacts(store, ARTIFACT_CLASS)[0]
        self.assertFalse(any(b["symbol"] == cls_artifact["locator"] for b in store["behavior_nodes"]))


# ---------------------------------------------------------------------------
# Incremental synchronization
# ---------------------------------------------------------------------------
class SyncTwinTests(unittest.TestCase):
    def test_no_change_returns_previous_byte_identical(self):
        doc = _doc([("a.py", [_fn("a.py", "f")], [])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")
        store2, result = sync_twin(doc, fps, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_NO_CHANGE)
        self.assertIs(store2, store)
        self.assertEqual(dumps(store2), dumps(store))

    def test_modified_file_recomputes_only_that_scope(self):
        doc = _doc([
            ("a.py", [_fn("a.py", "f")], []),
            ("b.py", [_fn("b.py", "g")], []),
        ])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")
        fps2 = dict(fps, **{"a.py": "fp:changed"})
        store2, result = sync_twin(doc, fps2, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_SYNCHRONIZED)
        self.assertEqual(result["changed_paths"], ["a.py"])
        old = _record_index(store)
        new = _record_index(store2)
        # b.py records are carried verbatim (field-identical).
        for rid, rec in old.items():
            if rec.get("path") == "b.py":
                self.assertEqual(json.dumps(new[rid], sort_keys=True),
                                 json.dumps(rec, sort_keys=True), rid)

    def test_removed_file_drops_its_records(self):
        doc = _doc([("a.py", [_fn("a.py", "f")]), ("b.py", [_fn("b.py", "g")])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")
        doc2 = _doc([("a.py", [_fn("a.py", "f")])])
        fps2 = _fingerprints(doc2)
        store2, result = sync_twin(doc2, fps2, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_SYNCHRONIZED)
        self.assertFalse(any(r["path"] == "b.py" for r in store2["artifacts"]))

    def test_rename_annotates_moved_from(self):
        doc = _doc([("a.py", [_fn("a.py", "f")])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")
        doc2 = _doc([("c.py", [_fn("c.py", "f")])])
        fps2 = {"c.py": fps["a.py"]}
        store2, result = sync_twin(doc2, fps2, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_SYNCHRONIZED)
        new_file = _artifacts(store2, ARTIFACT_FILE)[0]
        self.assertEqual(new_file["path"], "c.py")
        self.assertEqual(new_file["moved_from"], "a.py")
        self.assertFalse(any(r["path"] == "a.py" for r in store2["artifacts"]))

    def test_ambiguous_move_marks_needs_review(self):
        doc = _doc([("a.py", [_fn("a.py", "f")]), ("b.py", [_fn("b.py", "g")])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")
        # a.py removed; x.py and y.py both carry its exact fingerprint.
        doc2 = _doc([("b.py", [_fn("b.py", "g")]), ("x.py", [_fn("x.py", "f")]), ("y.py", [_fn("y.py", "f")])])
        fps2 = {"b.py": fps["b.py"], "x.py": fps["a.py"], "y.py": fps["a.py"]}
        store2, result = sync_twin(doc2, fps2, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_NEEDS_REVIEW)
        retained = [r for r in store2["artifacts"] if r["path"] == "a.py"]
        self.assertTrue(retained)
        self.assertTrue(all(r["sync_state"] == SYNC_NEEDS_REVIEW for r in retained))

    def test_syntax_error_retains_last_valid_and_marks_stale(self):
        doc = _doc([("a.py", [_fn("a.py", "f")], [])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")

        bad = _doc([("a.py", [], [])])
        bad["files"][0]["syntax_status"] = "error"
        fps2 = {"a.py": "fp:broken"}
        store2, result = sync_twin(bad, fps2, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_STALE)
        self.assertIn("reason", result)

        # The previous symbol artifact is retained and marked stale.
        sym = [r for r in store2["artifacts"] if r.get("locator") == "a.f"]
        self.assertEqual(len(sym), 1)
        self.assertEqual(sym[0]["sync_state"], SYNC_STALE)
        # The file artifact reflects the parse error.
        file_art = _artifacts(store2, ARTIFACT_FILE)[0]
        self.assertEqual(file_art["syntax_status"], "error")
        self.assertEqual(file_art["sync_state"], SYNC_STALE)

    def test_restored_syntax_reconciles_affected_scope(self):
        doc = _doc([("a.py", [_fn("a.py", "f")], [])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")

        bad = _doc([("a.py", [], [])])
        bad["files"][0]["syntax_status"] = "error"
        store2, _ = sync_twin(bad, {"a.py": "fp:broken"}, store, _WS, 2, "T2")

        # Restored: valid content again.
        doc3 = _doc([("a.py", [_fn("a.py", "f")], [])])
        store3, result = sync_twin(doc3, {"a.py": "fp:restored"}, store2, _WS, 3, "T3")
        self.assertEqual(result["state"], SYNC_SYNCHRONIZED)
        sym = [r for r in store3["artifacts"] if r.get("locator") == "a.f"]
        self.assertEqual(sym[0]["sync_state"], SYNC_SYNCHRONIZED)

    def test_human_draft_conflict_preserves_both(self):
        doc = _doc([("a.py", [_fn("a.py", "f")], [])])
        fps = _fingerprints(doc)
        store, _ = sync_twin(doc, fps, None, _WS, 1, "T1")
        store["human_draft"] = {"draft_version": 1}
        store2, result = sync_twin(doc, {"a.py": "fp:changed"}, store, _WS, 2, "T2")
        self.assertEqual(result["state"], SYNC_CONFLICT)
        self.assertEqual(store2["human_draft"], {"draft_version": 1})
        self.assertEqual(store2["workspace_revision"]["sync_state"], SYNC_CONFLICT)


if __name__ == "__main__":
    unittest.main()
