"""The deterministic advisory impact proposal (P5.3).

Two things are held here.

First, the **oracle**: ``fixtures/intent/manifest.json`` states, by hand and
independently of the renderer, what the contract must answer for a supported
case and for every named negative case — a missing or invalid required fact, an
empty versus an unknown impact, an exact source/Twin binding, a stale revision,
an artifact mismatch, an ambiguity, a cross-workspace reference, a changed
baseline, and the absence of side effects. Each expectation names *why* a fact
or a suggested test is present, and the case list is executed exactly as
written.

Second, the **boundaries**: the two P5.3 modules are read-only by construction,
not by convention. The tests below prove that directly — an import audit, a
runtime audit of the inputs and the working directory, and an assertion that the
protocol action set is exactly what it was before this work.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest

from hrca import twin
from hrca.authoring import impact_proposal, intent_delta
from hrca.core import contract
from hrca.source import scanner

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
FIXTURES = os.path.join(REPO, "fixtures")
MANIFEST = os.path.join(FIXTURES, "intent", "manifest.json")

_PY_SUFFIXES = (".py", ".pyi")

# Keys an ``expect`` block may carry. A typo is a test failure, not a silently
# unchecked expectation.
_EXPECT_KEYS = frozenset(
    {
        "result",
        "error",
        "state",
        "reason_code",
        "confidence",
        "prose_present",
        "target_artifact_ids",
        "unresolved_references",
        "affected_facts",
        "risks",
        "suggested_tests",
        "unchanged_constraints",
        "unresolved_questions",
        "evidence_bindings",
    }
)

_FACT_KEYS = ("fact_kind", "id", "role", "twin_artifact_id", "target")

# Modules that would grant an authority the P5.3 contract does not have, plus
# the project's own write-side seams. Neither new module may import any of them.
_FORBIDDEN_IMPORTS = frozenset(
    {
        "os",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "tempfile",
        "urllib",
        "http",
        "ssl",
        "ctypes",
        "runpy",
        "winreg",
        "platform",
        "multiprocessing",
        "threading",
        "asyncio",
        "sqlite3",
        "pickle",
        "hrca.boundary",
        "hrca.client",
        "hrca.client_core",
        "hrca.workspace",
        "hrca.codemap",
        "hrca.codemap_draft",
        "hrca.proposal",
        "hrca.advisory",
        "hrca.delta_candidate",
        "hrca.delta_transport",
        "hrca.delta_verifier",
        "hrca.candidate_package",
        "hrca.rule_delta",
        "hrca.rule_delta_interpret",
        "hrca.runner_broker",
        "hrca.container_runner",
        "hrca.runtime_handlers",
        "hrca.provider",
        "hrca.provider_config",
        "hrca.provider_cli",
        "hrca.deepseek",
        "hrca.deepseek_transport",
        "hrca.credential_host",
        "hrca.credential_store",
        "hrca.credential_store_win",
        "hrca.credential_sheet_win",
        "hrca.app_package",
        "hrca.library",
        "hrca.library_store",
        "hrca.document",
        "hrca.version_store",
        "hrca.twin_store",
        "hrca.memory",
        "hrca.memory_store",
        "hrca.memory_package",
        "hrca.hook_capture",
    }
)

_FORBIDDEN_CALLS = frozenset({"open", "exec", "eval", "compile", "__import__"})


# -- accepted evidence -----------------------------------------------------


def _fingerprints(root: str, doc: dict) -> dict:
    out = {}
    for rec in doc["files"]:
        path = rec["path"]
        if not path.endswith(_PY_SUFFIXES):
            continue
        try:
            with open(os.path.join(root, path), "rb") as fh:
                out[path] = twin.fingerprint_bytes(fh.read())
        except OSError:  # pragma: no cover - the corpus is readable
            out[path] = None
    return out


def _accepted() -> tuple:
    """Return ``(scanner document, Twin store)`` for the frozen corpus."""
    doc = scanner.scan_directory(FIXTURES)
    store = twin.build_store(
        doc, _fingerprints(FIXTURES, doc), twin.workspace_id_for(doc["root"]), 1, "T"
    )
    return doc, store


def _baseline_of(doc: dict, store: dict) -> dict:
    """The baseline an intent authored against this evidence would declare."""
    revision = store["workspace_revision"]
    return {
        "workspace_id": revision["workspace_id"],
        "scan_generation": revision["scan_generation"],
        "baseline_fingerprint": revision["baseline_fingerprint"],
        "scanner_schema_version": doc["schema_version"],
        "grammar": dict(doc["grammar"]),
    }


# -- declarative mutations -------------------------------------------------

_DUPLICATE_RANGE = {
    "lineno": 99,
    "col_offset": 0,
    "end_lineno": 99,
    "end_col_offset": 1,
}


def _mutate_scanner(doc: dict, ops: dict, case: str) -> dict:
    doc = copy.deepcopy(doc)
    if "remove_file" in ops:
        before = len(doc["files"])
        doc["files"] = [f for f in doc["files"] if f["path"] != ops["remove_file"]]
        assert len(doc["files"]) == before - 1, f"{case}: no such file to remove"
    if "set_grammar_version" in ops:
        assert doc["grammar"]["version"] != ops["set_grammar_version"], (
            f"{case}: the mutation changes nothing"
        )
        doc["grammar"]["version"] = ops["set_grammar_version"]
    if "set_schema_version" in ops:
        assert doc["schema_version"] != ops["set_schema_version"], (
            f"{case}: the mutation changes nothing"
        )
        doc["schema_version"] = ops["set_schema_version"]
    if "duplicate_symbol" in ops:
        victim = next(
            (s for s in doc["symbols"] if s["id"] == ops["duplicate_symbol"]), None
        )
        assert victim is not None, f"{case}: no such symbol to duplicate"
        duplicate = copy.deepcopy(victim)
        duplicate["source_range"] = dict(_DUPLICATE_RANGE)
        assert duplicate != victim, f"{case}: the duplicate is not a contradiction"
        doc["symbols"].append(duplicate)
    return doc


def _mutate_twin(store: dict, ops: dict, case: str) -> dict:
    store = copy.deepcopy(store)
    revision = store["workspace_revision"]
    if "set_workspace_id" in ops:
        assert revision["workspace_id"] != ops["set_workspace_id"], f"{case}: no change"
        revision["workspace_id"] = ops["set_workspace_id"]
    if "set_scan_generation" in ops:
        assert revision["scan_generation"] != ops["set_scan_generation"], f"{case}: no change"
        revision["scan_generation"] = ops["set_scan_generation"]
    if "set_baseline_fingerprint" in ops:
        assert revision["baseline_fingerprint"] != ops["set_baseline_fingerprint"], (
            f"{case}: no change"
        )
        revision["baseline_fingerprint"] = ops["set_baseline_fingerprint"]
    if "add_artifact" in ops:
        store["artifacts"].append(copy.deepcopy(ops["add_artifact"]))
    return store


# -- manifest execution ----------------------------------------------------


_HRCA_ROOT = os.path.join(SRC, "hrca")

def _hrca_module(name):
    """Return the path of an ``hrca`` module wherever it now lives.

    The package is organised by responsibility, so a module is no longer a
    fixed number of directories below ``src``; it is resolved by name. A
    compatibility shim is skipped in favour of the implementation it aliases,
    because these tests are about what a module *does* and a shim does
    nothing but point at another module.
    """
    stem = name[:-3] if name.endswith(".py") else name
    matches = []
    for dirpath, dirnames, filenames in os.walk(_HRCA_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        if stem + ".py" in filenames:
            matches.append(os.path.join(dirpath, stem + ".py"))
        # A responsibility package's front door is its ``__init__``, so a name
        # that used to be a module may now be a package.
        if os.path.basename(dirpath) == stem and "__init__.py" in filenames:
            matches.append(os.path.join(dirpath, "__init__.py"))
    for path in sorted(matches, key=len, reverse=True):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        alias = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not node.targets:
                continue
            t = node.targets[0]
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Attribute)
                    and t.value.attr == "modules"
                    and isinstance(t.value.value, ast.Name)
                    and t.value.value.id == "sys"
                    and isinstance(t.slice, ast.Name)
                    and t.slice.id == "__name__"):
                alias = True
        if not alias:
            return path
    if matches:
        return matches[0]
    raise AssertionError("no module named %r in the hrca package" % stem)


class ManifestOracleTests(unittest.TestCase):
    """Every case in the hand-authored manifest, run exactly as written."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, encoding="utf-8") as fh:
            cls.manifest = json.load(fh)
        cls.accepted_doc, cls.accepted_store = _accepted()
        cls.cases = {case["case"]: case for case in cls.manifest["cases"]}
        assert len(cls.cases) == len(cls.manifest["cases"]), "duplicate case name"

    def test_the_manifest_states_why_a_supported_signature_exists(self):
        # Every affected fact and suggested test in the supported case must say
        # why it is present; an oracle that only lists ids proves nothing.
        for name in ("supported_class_scope", "supported_module_scope"):
            case = self.cases[name]
            for fact in case["expect"]["affected_facts"]:
                with self.subTest(case=name, fact=fact["id"]):
                    self.assertTrue(fact["why"].strip())
            for test in case["expect"]["suggested_tests"]:
                with self.subTest(case=name, test=test["test_id"]):
                    self.assertTrue(test["why"].strip())

    def test_the_manifest_covers_every_required_case_kind(self):
        expected = {
            "supported_class_scope",
            "supported_module_scope",
            "missing_required_intent_field",
            "invalid_scan_generation",
            "oversized_requested_outcome",
            "no_impact_empty_module",
            "unknown_source_facts",
            "supported_class_scope",
            "stale_scan_generation",
            "stale_baseline_fingerprint",
            "unbound_origin_evidence",
            "ambiguous_reference",
            "ambiguous_impact",
            "cross_workspace_reference",
            "grammar_context_changed",
            "schema_context_changed",
            "missing_twin_evidence",
        }
        self.assertTrue(expected <= set(self.cases))

    def test_every_expectation_uses_a_known_key(self):
        for name, case in self.cases.items():
            with self.subTest(case=name):
                self.assertTrue(set(case["expect"]) <= _EXPECT_KEYS)

    def test_every_case(self):
        for case in self.manifest["cases"]:
            with self.subTest(case=case["case"]):
                self._run_case(case)

    # -- the runner --------------------------------------------------------

    def _base_evidence(self, spec):
        """The evidence as the intent's author saw it, before any mutation."""
        if isinstance(spec, dict) and "literal" in spec:
            literal = spec["literal"]
            return copy.deepcopy(literal["scanner"]), copy.deepcopy(literal["twin"])
        return copy.deepcopy(self.accepted_doc), copy.deepcopy(self.accepted_store)

    def _resolve_evidence(self, spec):
        if spec == "accepted":
            return {"scanner": self.accepted_doc, "twin": self.accepted_store}
        if spec == "not_a_mapping":
            return "not a mapping"
        if "literal" in spec:
            literal = copy.deepcopy(spec["literal"])
            return {"scanner": literal["scanner"], "twin": literal["twin"]}
        if "drop" in spec:
            bundle = {"scanner": self.accepted_doc, "twin": self.accepted_store}
            bundle.pop(spec["drop"])
            return bundle
        ops = spec["mutate"]
        doc = self.accepted_doc
        store = self.accepted_store
        if "scanner" in ops:
            doc = _mutate_scanner(doc, ops["scanner"], "case")
        if "twin" in ops:
            store = _mutate_twin(store, ops["twin"], "case")
        return {"scanner": doc, "twin": store}

    def _intent_input(self, case):
        intent = dict(self.manifest["intent_base"])
        override = dict(case["intent"])
        drop = override.pop("drop", [])
        intent.update(override)
        for key in drop:
            intent.pop(key, None)

        base_doc, base_store = self._base_evidence(case["evidence"])
        baseline = _baseline_of(base_doc, base_store)
        spec = case.get("baseline", "evidence")
        if isinstance(spec, dict):
            baseline.update(spec["override"])
        intent["baseline"] = baseline

        if intent.get("requested_outcome") == "OVERSIZED":
            intent["requested_outcome"] = "x" * (intent_delta.MAX_OUTCOME_CHARS + 1)
        return intent

    def _run_case(self, case):
        name = case["case"]
        expect = case["expect"]
        intent_input = self._intent_input(case)

        delta, error = intent_delta.build_intent_delta(intent_input)
        if expect["result"] == "delta_refused":
            self.assertIsNone(delta, name)
            self.assertEqual(expect["error"], error, name)
            return
        self.assertIsNone(error, f"{name}: {error}")

        bundle = self._resolve_evidence(case["evidence"])
        proposal, error = impact_proposal.build_impact_proposal(delta, bundle)

        if expect["result"] == "refused":
            self.assertIsNone(proposal, name)
            self.assertEqual(expect["error"], error, name)
            return

        self.assertIsNone(error, f"{name}: {error}")
        self.assertIsNone(impact_proposal.validate_impact_proposal(proposal), name)

        # The document is bound to this intent and to nothing else.
        self.assertEqual(delta["intent_delta_id"], proposal["intent_delta_id"], name)
        self.assertIs(True, proposal["advisory"], name)
        self.assertIs(False, proposal["executable"], name)
        self.assertIs(False, proposal["applied"], name)

        self.assertEqual(expect["state"], proposal["state"], name)
        self.assertEqual(expect["reason_code"], proposal["reason_code"], name)
        if "confidence" in expect:
            self.assertEqual(expect["confidence"], proposal["confidence"], name)
        if "prose_present" in expect:
            self.assertEqual(expect["prose_present"], proposal["prose_present"], name)

        if "target_artifact_ids" in expect:
            self.assertEqual(
                expect["target_artifact_ids"],
                [t["artifact_id"] for t in proposal["target_scope"]["targets"]],
                name,
            )
        if "unresolved_references" in expect:
            self.assertEqual(
                expect["unresolved_references"],
                proposal["target_scope"]["unresolved_references"],
                name,
            )

        if "affected_facts" in expect:
            actual = [
                {k: fact.get(k) for k in _FACT_KEYS if k in expected}
                for fact, expected in zip(
                    proposal["affected_facts"], expect["affected_facts"]
                )
            ]
            expected = [
                {k: fact.get(k) for k in _FACT_KEYS if k in fact}
                for fact in expect["affected_facts"]
            ]
            self.assertEqual(expected, actual, name)
            self.assertEqual(
                sorted((f["fact_kind"], f["id"]) for f in proposal["affected_facts"]),
                [(f["fact_kind"], f["id"]) for f in proposal["affected_facts"]],
                f"{name}: affected facts are not in canonical order",
            )

        if "risks" in expect:
            self.assertEqual(
                expect["risks"], [r["risk_id"] for r in proposal["risks"]], name
            )
        if "suggested_tests" in expect:
            self.assertEqual(
                [t["test_id"] for t in expect["suggested_tests"]],
                [t["test_id"] for t in proposal["suggested_tests"]],
                name,
            )
        if "evidence_bindings" in expect:
            self.assertEqual(
                expect["evidence_bindings"], proposal["evidence_bindings"], name
            )
        for field, key in (
            ("unchanged_constraints", "unchanged_constraints"),
            ("unresolved_questions", "unresolved_questions"),
        ):
            if key not in expect:
                continue
            counts = {"fixed": 0, "intent_delta": 0}
            for entry in proposal[field]:
                counts[entry["origin"]] += 1
            self.assertEqual(expect[key], counts, name)

        # Every produced proposal declares the same, wholly absent, surface.
        self.assertEqual(
            set(impact_proposal._MUTATION_SURFACE_KEYS),
            set(proposal["mutation_surface"]),
            name,
        )
        self.assertTrue(
            all(value is False for value in proposal["mutation_surface"].values()),
            name,
        )


# -- determinism -----------------------------------------------------------


class DeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc, cls.store = _accepted()
        cls.raw = {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(cls.doc, cls.store),
            "requested_outcome": "Record that the service reports version 2.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["the recorded version is 2"],
            "assumptions": [],
            "unresolved_questions": [],
        }
        cls.delta, error = intent_delta.build_intent_delta(cls.raw)
        assert error is None, error

    def _proposal(self, delta=None, bundle=None):
        proposal, error = impact_proposal.build_impact_proposal(
            delta or self.delta,
            bundle or {"scanner": self.doc, "twin": self.store},
        )
        assert error is None, error
        return proposal

    def test_the_same_intent_and_evidence_are_byte_identical(self):
        first = impact_proposal.dumps(self._proposal())
        second = impact_proposal.dumps(self._proposal())
        self.assertEqual(first, second)

    def test_an_identical_intent_built_twice_has_one_identity(self):
        other, error = intent_delta.build_intent_delta(dict(self.raw))
        self.assertIsNone(error)
        self.assertEqual(
            self._proposal()["proposal_id"], self._proposal(delta=other)["proposal_id"]
        )

    def test_the_identity_matches_the_content(self):
        proposal = self._proposal()
        self.assertEqual(
            proposal["proposal_id"], impact_proposal.impact_proposal_id_for(proposal)
        )

    def test_a_changed_baseline_is_refused_rather_than_re_bound(self):
        # One byte of Twin state moves; the old delta must not silently follow.
        store = copy.deepcopy(self.store)
        store["workspace_revision"]["baseline_fingerprint"] = "f" * 64
        proposal, error = impact_proposal.build_impact_proposal(
            self.delta, {"scanner": self.doc, "twin": store}
        )
        self.assertIsNone(proposal)
        self.assertEqual(impact_proposal.REASON_STALE_BASELINE, error)

    def test_a_changed_grammar_context_is_refused(self):
        doc = copy.deepcopy(self.doc)
        doc["grammar"] = {"implementation": "cpython", "version": "0.0"}
        proposal, error = impact_proposal.build_impact_proposal(
            self.delta, {"scanner": doc, "twin": self.store}
        )
        self.assertIsNone(proposal)
        self.assertEqual(impact_proposal.REASON_GRAMMAR_CHANGED, error)

    def test_a_changed_scope_yields_a_distinctly_bound_proposal(self):
        raw = dict(self.raw)
        raw["scope"] = {"entities": ["app.service.Service.handle"], "artifacts": []}
        other, error = intent_delta.build_intent_delta(raw)
        self.assertIsNone(error)
        narrow = self._proposal(delta=other)
        wide = self._proposal()
        self.assertNotEqual(narrow["proposal_id"], wide["proposal_id"])
        self.assertNotEqual(narrow["intent_delta_id"], wide["intent_delta_id"])
        # The binding to the workspace, revision and schema is the same; what
        # differs is the exact identity the proposal is bound to, which shows up
        # in the target scope rather than in the binding fingerprint.
        self.assertEqual(narrow["binding"], wide["binding"])
        self.assertEqual(
            ["artifact:method:app.service.Service.handle"],
            [t["artifact_id"] for t in narrow["target_scope"]["targets"]],
        )


# -- no fallback -----------------------------------------------------------


class NoFallbackTests(unittest.TestCase):
    """Binding is by exact identity only: no name, path, order or prose route."""

    @classmethod
    def setUpClass(cls):
        cls.doc, cls.store = _accepted()

    def _delta(self, **overrides):
        raw = {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(self.doc, self.store),
            "requested_outcome": "Record a change.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["something is true afterwards"],
            "assumptions": [],
            "unresolved_questions": [],
        }
        raw.update(overrides)
        delta, error = intent_delta.build_intent_delta(raw)
        assert error is None, error
        return delta

    def _proposal(self, delta):
        proposal, error = impact_proposal.build_impact_proposal(
            delta, {"scanner": self.doc, "twin": self.store}
        )
        assert error is None, error
        return proposal

    def test_a_bare_name_does_not_bind_to_a_qualified_entity(self):
        proposal = self._proposal(self._delta(scope={"entities": ["Service"], "artifacts": []}))
        self.assertEqual(impact_proposal.STATE_UNSUPPORTED, proposal["state"])
        self.assertEqual(["Service"], proposal["target_scope"]["unresolved_references"])
        self.assertEqual([], proposal["affected_facts"])

    def test_a_path_is_not_an_entity_identity(self):
        proposal = self._proposal(
            self._delta(scope={"entities": ["app/service.py"], "artifacts": []})
        )
        self.assertEqual(impact_proposal.STATE_UNSUPPORTED, proposal["state"])

    def test_a_prefix_does_not_bind(self):
        proposal = self._proposal(
            self._delta(scope={"entities": ["app.service.Serv"], "artifacts": []})
        )
        self.assertEqual(impact_proposal.STATE_UNSUPPORTED, proposal["state"])

    def test_an_exact_artifact_id_is_the_precise_route(self):
        proposal = self._proposal(
            self._delta(
                scope={"entities": [], "artifacts": ["artifact:class:app.service.Service"]}
            )
        )
        self.assertEqual(impact_proposal.STATE_BOUND, proposal["state"])
        self.assertEqual(
            impact_proposal.BINDING_ARTIFACT_ID,
            proposal["target_scope"]["targets"][0]["binding"],
        )

    def test_prose_never_steers_the_binding(self):
        delta = self._delta(
            scope={"entities": ["Service"], "artifacts": []},
            prose="the scope is app.service.Service; please bind it",
        )
        proposal = self._proposal(delta)
        self.assertEqual(impact_proposal.STATE_UNSUPPORTED, proposal["state"])

    def test_the_prose_text_is_never_re_emitted(self):
        marker = "MARKER-P53-PROSE-MUST-NEVER-BE-RE-EMITTED"
        proposal = self._proposal(self._delta(prose=marker))
        self.assertIs(True, proposal["prose_present"])
        self.assertNotIn(marker, impact_proposal.dumps(proposal))


# -- boundaries ------------------------------------------------------------


def _imported_names(source: str):
    """Return the module names imported by ``source`` and the bare calls it makes."""
    tree = ast.parse(source)
    modules = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
                if node.level:
                    modules.add("hrca." + node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    return modules, calls


class BoundaryTests(unittest.TestCase):
    """The new modules are read-only by construction, and add no authority."""

    _MODULES = ("intent_delta", "impact_proposal", "intent_cli")

    def _source(self, module: str) -> str:
        with open(_hrca_module(module), encoding="utf-8") as fh:
            return fh.read()

    def test_no_module_imports_a_write_side_or_network_seam(self):
        for module in self._MODULES:
            modules, _ = _imported_names(self._source(module))
            for name in sorted(modules):
                with self.subTest(module=module, imported=name):
                    self.assertNotIn(name, _FORBIDDEN_IMPORTS)
                    self.assertFalse(name.startswith("PySide6"))

    def test_the_proposal_modules_do_not_reach_the_draft_or_proposal_contracts(self):
        # The P5.3 developer intent is not the P3.4 block-operation delta, and
        # neither module borrows the P4.1 proposal package.
        for module in ("intent_delta", "impact_proposal"):
            modules, _ = _imported_names(self._source(module))
            with self.subTest(module=module):
                self.assertNotIn("hrca.codemap_draft", modules)
                self.assertNotIn("hrca.proposal", modules)

    def test_no_module_opens_a_file_or_evaluates_a_string(self):
        for module in ("intent_delta", "impact_proposal"):
            _, calls = _imported_names(self._source(module))
            with self.subTest(module=module):
                self.assertEqual(set(), calls & _FORBIDDEN_CALLS)

    def test_building_a_proposal_mutates_neither_input(self):
        doc, store = _accepted()
        raw = {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(doc, store),
            "requested_outcome": "Record that the service reports version 2.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["the recorded version is 2"],
            "assumptions": [],
            "unresolved_questions": [],
        }
        delta, error = intent_delta.build_intent_delta(raw)
        self.assertIsNone(error)
        before_delta = intent_delta.dumps(delta)
        before_doc = json.dumps(doc, sort_keys=True)
        before_store = json.dumps(store, sort_keys=True)
        before_raw = json.dumps(raw, sort_keys=True)

        impact_proposal.build_impact_proposal(delta, {"scanner": doc, "twin": store})

        self.assertEqual(before_delta, intent_delta.dumps(delta))
        self.assertEqual(before_doc, json.dumps(doc, sort_keys=True))
        self.assertEqual(before_store, json.dumps(store, sort_keys=True))
        self.assertEqual(before_raw, json.dumps(raw, sort_keys=True))

    def test_building_a_proposal_writes_nothing_to_the_working_directory(self):
        doc, store = _accepted()
        raw = {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(doc, store),
            "requested_outcome": "Record that the service reports version 2.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["the recorded version is 2"],
            "assumptions": [],
            "unresolved_questions": [],
        }
        with tempfile.TemporaryDirectory() as sandbox:
            previous = os.getcwd()
            os.chdir(sandbox)
            try:
                delta, error = intent_delta.build_intent_delta(raw)
                self.assertIsNone(error)
                impact_proposal.build_impact_proposal(
                    delta, {"scanner": doc, "twin": store}
                )
                self.assertEqual([], sorted(os.listdir(sandbox)))
            finally:
                os.chdir(previous)

    def test_no_protocol_action_was_added(self):
        # P5.3 adds no desktop route at all: no existing read-only action takes a
        # developer intent, so nothing was widened and nothing was invented.
        self.assertEqual("3.9.0", contract.CONTRACT_VERSION)
        self.assertEqual(61, len(contract.ALLOWED_ACTIONS))
        self.assertFalse([a for a in contract.ALLOWED_ACTIONS if "impact" in a])

    def test_the_evidence_schemas_are_unchanged(self):
        self.assertEqual("1.1.0", scanner.SCHEMA_VERSION)
        self.assertEqual("1.0.0", intent_delta.INTENT_DELTA_SCHEMA_VERSION)
        self.assertEqual("1.0.0", impact_proposal.IMPACT_SCHEMA_VERSION)
        self.assertEqual("1.0.0", twin.TWIN_SCHEMA_VERSION)


class PrivacyTests(unittest.TestCase):
    """No proposal may carry an environment fact, a scan root or a source body."""

    @classmethod
    def setUpClass(cls):
        cls.doc, cls.store = _accepted()

    def _proposal(self):
        raw = {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(self.doc, self.store),
            "requested_outcome": "Record that the service reports version 2.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["the recorded version is 2"],
            "assumptions": [],
            "unresolved_questions": [],
        }
        delta, error = intent_delta.build_intent_delta(raw)
        assert error is None, error
        proposal, error = impact_proposal.build_impact_proposal(
            delta, {"scanner": self.doc, "twin": self.store}
        )
        assert error is None, error
        return proposal

    def test_the_scan_root_never_reaches_the_proposal(self):
        rendered = impact_proposal.dumps(self._proposal())
        self.assertNotIn(self.doc["root"], rendered)

    def test_no_environment_value_reaches_the_proposal(self):
        rendered = impact_proposal.dumps(self._proposal())
        for forbidden in (
            sys.executable,
            sys.prefix,
            os.path.expanduser("~"),
            os.path.abspath(FIXTURES),
        ):
            if not forbidden:
                continue
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_no_source_body_reaches_the_proposal(self):
        rendered = impact_proposal.dumps(self._proposal())
        with open(os.path.join(FIXTURES, "app", "service.py"), encoding="utf-8") as fh:
            body = fh.read()
        for line in body.splitlines():
            line = line.strip()
            if len(line) < 12:
                continue
            with self.subTest(line=line):
                self.assertNotIn(line, rendered)

    def test_every_refusal_reason_is_bounded_and_path_free(self):
        reasons = [
            impact_proposal.REASON_INVALID_INTENT,
            impact_proposal.REASON_EVIDENCE_NOT_MAPPING,
            impact_proposal.REASON_MISSING_SCANNER,
            impact_proposal.REASON_MISSING_TWIN,
            impact_proposal.REASON_UNSUPPORTED_EVIDENCE,
            impact_proposal.REASON_CROSS_WORKSPACE,
            impact_proposal.REASON_STALE_BASELINE,
            impact_proposal.REASON_SCHEMA_CHANGED,
            impact_proposal.REASON_GRAMMAR_CHANGED,
            impact_proposal.REASON_AMBIGUOUS_REFERENCE,
            impact_proposal.REASON_UNBOUND_EVIDENCE,
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)
                self.assertLess(len(reason), 200)


class ValidationTests(unittest.TestCase):
    def _proposal(self):
        doc, store = _accepted()
        raw = {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(doc, store),
            "requested_outcome": "Record that the service reports version 2.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["the recorded version is 2"],
            "assumptions": [],
            "unresolved_questions": [],
        }
        delta, _ = intent_delta.build_intent_delta(raw)
        proposal, _ = impact_proposal.build_impact_proposal(
            delta, {"scanner": doc, "twin": store}
        )
        return proposal

    def test_a_built_proposal_validates(self):
        self.assertIsNone(impact_proposal.validate_impact_proposal(self._proposal()))

    def test_a_non_mapping_is_refused(self):
        self.assertEqual(
            "proposal is not a mapping",
            impact_proposal.validate_impact_proposal(None),
        )

    def test_an_executable_proposal_is_refused(self):
        proposal = self._proposal()
        proposal["executable"] = True
        self.assertEqual(
            "proposal must be non-executable",
            impact_proposal.validate_impact_proposal(proposal),
        )

    def test_a_non_advisory_proposal_is_refused(self):
        proposal = self._proposal()
        proposal["advisory"] = False
        self.assertEqual(
            "proposal must be advisory",
            impact_proposal.validate_impact_proposal(proposal),
        )

    def test_a_widened_mutation_surface_is_refused(self):
        proposal = self._proposal()
        proposal["mutation_surface"]["commit"] = True
        self.assertEqual(
            "proposal_id does not match the proposal content",
            impact_proposal.validate_impact_proposal(proposal),
        )
        # Re-deriving the identity still refuses: the surface itself is checked.
        proposal["proposal_id"] = impact_proposal.impact_proposal_id_for(proposal)
        self.assertEqual(
            "mutation_surface declares a non-false commit",
            impact_proposal.validate_impact_proposal(proposal),
        )

    def test_an_unknown_state_is_refused(self):
        proposal = self._proposal()
        proposal["state"] = "probably_fine"
        proposal["proposal_id"] = impact_proposal.impact_proposal_id_for(proposal)
        self.assertEqual(
            "unknown impact state",
            impact_proposal.validate_impact_proposal(proposal),
        )


class CliTests(unittest.TestCase):
    def _write(self, directory: str, name: str, payload) -> str:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def _intent(self, doc, store):
        return {
            "origin": {
                "kind": "developer_authored",
                "evidence": [{"kind": "scanner_file", "id": "app/service.py"}],
            },
            "baseline": _baseline_of(doc, store),
            "requested_outcome": "Record that the service reports version 2.",
            "scope": {"entities": ["app.service.Service"], "artifacts": []},
            "constraints": [],
            "acceptance_criteria": ["the recorded version is 2"],
            "assumptions": [],
            "unresolved_questions": [],
        }

    def test_verify_reports_a_valid_intent(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hrca.cli import intent_cli

        doc, store = _accepted()
        with tempfile.TemporaryDirectory() as sandbox:
            path = self._write(sandbox, "intent.json", self._intent(doc, store))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = intent_cli.main(["verify", path])
        self.assertEqual(0, code)
        self.assertEqual("", err.getvalue())
        self.assertTrue(json.loads(out.getvalue())["intent_delta_id"].startswith("intent:"))

    def test_propose_emits_a_canonical_proposal(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hrca.cli import intent_cli

        doc, store = _accepted()
        with tempfile.TemporaryDirectory() as sandbox:
            intent_path = self._write(sandbox, "intent.json", self._intent(doc, store))
            doc_path = self._write(sandbox, "scan.json", doc)
            store_path = self._write(sandbox, "twin.json", store)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = intent_cli.main(
                    ["propose", "--intent", intent_path, "--scanner", doc_path, "--twin", store_path]
                )
        self.assertEqual(0, code)
        proposal = json.loads(out.getvalue())
        self.assertEqual(impact_proposal.STATE_BOUND, proposal["state"])
        self.assertIsNone(impact_proposal.validate_impact_proposal(proposal))

    def test_propose_reports_a_refusal_as_a_bounded_reason(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hrca.cli import intent_cli

        doc, store = _accepted()
        # The intent is authored against the evidence as it was; the evidence is
        # then moved to another workspace, which the binding must catch.
        intent_payload = self._intent(doc, store)
        store = copy.deepcopy(store)
        store["workspace_revision"]["workspace_id"] = "ws:elsewhere"
        with tempfile.TemporaryDirectory() as sandbox:
            intent_path = self._write(sandbox, "intent.json", intent_payload)
            doc_path = self._write(sandbox, "scan.json", doc)
            store_path = self._write(sandbox, "twin.json", store)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = intent_cli.main(
                    ["propose", "--intent", intent_path, "--scanner", doc_path, "--twin", store_path]
                )
        self.assertEqual(intent_cli.EXIT_REFUSED, code)
        self.assertEqual("", out.getvalue())
        self.assertIn(impact_proposal.REASON_CROSS_WORKSPACE, err.getvalue())

    def test_a_missing_file_is_a_usage_failure_not_a_refusal(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from hrca.cli import intent_cli

        with tempfile.TemporaryDirectory() as sandbox:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = intent_cli.main(["verify", os.path.join(sandbox, "absent.json")])
        self.assertEqual(intent_cli.EXIT_USAGE, code)
        self.assertNotIn(sandbox, err.getvalue())


# -- envelope equivalence (B-T2A) -----------------------------------------
#
# The source-evidence read seam changed *how* this proposal reaches the facts it
# publishes, and nothing about what it publishes. The table below is the whole
# envelope for every case in the hand-authored manifest, plus the twin-store
# version family, reduced to one value per case and **frozen from the
# implementation as it stood before the seam existed**. A reader that changed a
# fact, reordered one, or reached a different refusal cannot leave these values
# unchanged.
#
# Five fields are masked, because they are derived from the machine and the
# interpreter rather than from the evidence: ``binding.workspace_id`` hashes the
# scan root's canonical path, ``binding.scanner_grammar`` follows the running
# CPython, and ``intent_delta_id``, ``binding.binding_fingerprint`` and
# ``proposal_id`` are hashes over those. Masking keeps the table portable across
# checkouts and interpreters while still pinning every fact, binding, risk,
# suggested test, question, constraint, assumption and refusal it carries.
#
# A refused case is pinned by its bounded reason and a refused intent delta by
# its own, so "the refusal did not change" is stated as exactly as "the envelope
# did not change".

_VOLATILE_ENVELOPE_FIELDS = (
    ("proposal_id",),
    ("intent_delta_id",),
    ("binding", "workspace_id"),
    ("binding", "scanner_grammar"),
    ("binding", "binding_fingerprint"),
)

_FROZEN_ENVELOPES = {
    "ambiguous_impact": "f570f15a02d6bc7e8db474cfcd07a5c026d84f319dca512af6182de324aac5ba",
    "ambiguous_reference": "refused:a scope reference does not bind to exactly one artifact",
    "cross_workspace_reference": "refused:evidence belongs to a different workspace",
    "empty_scope_entities": "delta_refused:scope must name at least one entity or artifact",
    "evidence_not_a_mapping": "refused:evidence is not a mapping",
    "future_evidence_schema": "refused:an evidence document is not a supported document",
    "grammar_context_changed": "refused:the scanner grammar context does not match the intent baseline",
    "invalid_scan_generation": "delta_refused:baseline.scan_generation must be an integer",
    "missing_required_intent_field": "delta_refused:missing required section acceptance_criteria",
    "missing_scanner_evidence": "refused:scanner evidence is missing",
    "missing_twin_evidence": "refused:twin evidence is missing",
    "no_impact_empty_module": "97d63620be3301ae8f7198a455db3fd1d8b43308a9bd1d6c0bc6768ecb856065",
    "oversized_requested_outcome": "delta_refused:requested_outcome is oversized",
    "prose_cannot_fill_a_required_field": "delta_refused:missing required section acceptance_criteria",
    "schema_context_changed": "refused:the scanner schema context does not match the intent baseline",
    "stale_baseline_fingerprint": "refused:evidence baseline does not match the intent baseline",
    "stale_scan_generation": "refused:evidence baseline does not match the intent baseline",
    "supported_class_scope": "4f236f616980eb47a45cd138676c90a0ebe908f103803daed361c63f87098c2c",
    "supported_module_scope": "caccedd796246e9f0a90c626aa0ac32fbd0978a3f98d50457cc58cbf0a87c666",
    "twin_future_schema_version": "refused:an evidence document is not a supported document",
    "twin_is_a_list": "refused:an evidence document is not a supported document",
    "twin_is_not_a_mapping": "refused:an evidence document is not a supported document",
    "twin_missing_artifacts": "refused:an origin evidence reference does not resolve exactly",
    "twin_missing_schema_version": "refused:an evidence document is not a supported document",
    "twin_missing_workspace_revision": "refused:an evidence document is not a supported document",
    "twin_mutated_artifact_added": "4f236f616980eb47a45cd138676c90a0ebe908f103803daed361c63f87098c2c",
    "twin_older_schema_version": "refused:an evidence document is not a supported document",
    "twin_workspace_revision_not_a_mapping": "refused:an evidence document is not a supported document",
    "unavailable_scanner_evidence": "548cc24e4eadebd4d1f29a851779244626378df977ac7a0627186cb80a2403b8",
    "unbound_origin_evidence": "refused:an origin evidence reference does not resolve exactly",
    "unknown_evidence_reference_kind": "delta_refused:an origin.evidence entry names an unknown kind",
    "unknown_source_facts": "7173b546957aa465e9f66da77ecbafe884d25392b788634721e5c4d1c6ab2709",
    "unsupported_identity": "47f7170d50a64d65cd29c9d377182d0cfc5a83959fa412c506238843951bf444",
}


def _stable_envelope(proposal: dict) -> str:
    """Return the canonical envelope with its machine-derived fields removed."""
    stable = copy.deepcopy(proposal)
    for path in _VOLATILE_ENVELOPE_FIELDS:
        node = stable
        for key in path[:-1]:
            node = node.get(key) or {}
        node.pop(path[-1], None)
    return impact_proposal.dumps(stable)


def _envelope_value(proposal):
    """Return the one value a built proposal is pinned by."""
    canon = _stable_envelope(proposal)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _twin_variants(accepted_store: dict) -> dict:
    """Return the twin-store mutations the seam's reads must answer for.

    The manifest's own twin mutations cover a well-formed store whose baseline
    moved. These cover the store the version gate cannot vouch for at all — a
    non-mapping, a missing or future schema version, a missing or malformed
    workspace baseline — plus a well-formed store carrying an extra artifact.
    """
    variants = {
        "twin_is_not_a_mapping": "not a mapping",
        "twin_is_a_list": [1, 2, 3],
        "twin_missing_schema_version": {
            k: v for k, v in accepted_store.items() if k != "schema_version"
        },
        "twin_missing_workspace_revision": {
            k: v for k, v in accepted_store.items() if k != "workspace_revision"
        },
        "twin_missing_artifacts": {
            k: v for k, v in accepted_store.items() if k != "artifacts"
        },
    }
    future = copy.deepcopy(accepted_store)
    future["schema_version"] = "9.9.9"
    variants["twin_future_schema_version"] = future
    older = copy.deepcopy(accepted_store)
    older["schema_version"] = "0.0.1"
    variants["twin_older_schema_version"] = older
    malformed = copy.deepcopy(accepted_store)
    malformed["workspace_revision"] = "not a mapping"
    variants["twin_workspace_revision_not_a_mapping"] = malformed
    mutated = copy.deepcopy(accepted_store)
    mutated["artifacts"] = mutated["artifacts"] + [
        {
            "record_type": "artifact",
            "id": "artifact:class:app.service",
            "kind": "class",
            "path": "app/service.py",
            "module": "app.service",
            "name": "Service",
            "locator": "app.service.Service2",
        }
    ]
    variants["twin_mutated_artifact_added"] = mutated
    return variants


class EnvelopeEquivalenceTests(unittest.TestCase):
    """Every envelope and every refusal is what it was before the seam."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        ManifestOracleTests.setUpClass()
        cls.runner = ManifestOracleTests("test_every_case")

    def _accepted(self):
        return ManifestOracleTests.accepted_doc, ManifestOracleTests.accepted_store

    def _observe_manifest_case(self, case):
        intent_input = self.runner._intent_input(case)
        delta, error = intent_delta.build_intent_delta(intent_input)
        if error is not None:
            return "delta_refused:" + error
        bundle = self.runner._resolve_evidence(case["evidence"])
        proposal, error = impact_proposal.build_impact_proposal(delta, bundle)
        if error is not None:
            return "refused:" + error
        return _envelope_value(proposal)

    def _observe_twin_variant(self, store):
        doc, _accepted_store = self._accepted()
        intent_input = self.runner._intent_input(
            {"case": "twin_version_family", "intent": {}, "evidence": "accepted"}
        )
        delta, error = intent_delta.build_intent_delta(intent_input)
        self.assertIsNone(error, error)
        proposal, error = impact_proposal.build_impact_proposal(
            delta, {"scanner": doc, "twin": store}
        )
        if error is not None:
            return "refused:" + error
        return _envelope_value(proposal)

    def test_the_table_pins_every_manifest_case_and_the_twin_version_family(self):
        expected = {case["case"] for case in ManifestOracleTests.manifest["cases"]}
        expected |= set(_twin_variants(ManifestOracleTests.accepted_store))
        self.assertEqual(sorted(expected), sorted(_FROZEN_ENVELOPES))

    def test_every_manifest_case_keeps_its_frozen_envelope(self):
        for case in ManifestOracleTests.manifest["cases"]:
            with self.subTest(case=case["case"]):
                self.assertEqual(
                    _FROZEN_ENVELOPES[case["case"]], self._observe_manifest_case(case)
                )

    def test_every_twin_version_case_keeps_its_frozen_answer(self):
        _doc, accepted_store = self._accepted()
        for name, store in sorted(_twin_variants(accepted_store).items()):
            with self.subTest(case=name):
                self.assertEqual(_FROZEN_ENVELOPES[name], self._observe_twin_variant(store))


class TwinEvidenceRefusalTests(unittest.TestCase):
    """A store the version gate cannot vouch for is refused, never half-read."""

    @classmethod
    def setUpClass(cls):
        ManifestOracleTests.setUpClass()
        cls.runner = ManifestOracleTests("test_every_case")

    def _delta(self):
        intent_input = self.runner._intent_input(
            {"case": "twin_version_family", "intent": {}, "evidence": "accepted"}
        )
        delta, error = intent_delta.build_intent_delta(intent_input)
        self.assertIsNone(error, error)
        return delta

    def _build(self, store):
        return impact_proposal.build_impact_proposal(
            self._delta(),
            {"scanner": ManifestOracleTests.accepted_doc, "twin": store},
        )

    def test_a_store_that_is_not_a_mapping_is_refused(self):
        for store in ("not a mapping", [1, 2, 3], 5, None):
            with self.subTest(store=store):
                self.assertEqual(
                    (None, impact_proposal.REASON_UNSUPPORTED_EVIDENCE),
                    self._build(store),
                )

    def test_a_store_with_no_schema_version_is_refused(self):
        store = {
            k: v
            for k, v in ManifestOracleTests.accepted_store.items()
            if k != "schema_version"
        }
        self.assertEqual(
            (None, impact_proposal.REASON_UNSUPPORTED_EVIDENCE), self._build(store)
        )

    def test_a_store_from_the_future_is_refused_rather_than_read(self):
        # 9.9.9 is *newer* than the schema this module understands. It is
        # refused, never partially read as if it were the version it is not.
        for version in ("9.9.9", "0.0.1"):
            with self.subTest(schema_version=version):
                store = copy.deepcopy(ManifestOracleTests.accepted_store)
                store["schema_version"] = version
                self.assertEqual(
                    (None, impact_proposal.REASON_UNSUPPORTED_EVIDENCE),
                    self._build(store),
                )

    def test_a_store_with_no_workspace_baseline_is_refused(self):
        store = {
            k: v
            for k, v in ManifestOracleTests.accepted_store.items()
            if k != "workspace_revision"
        }
        self.assertEqual(
            (None, impact_proposal.REASON_UNSUPPORTED_EVIDENCE), self._build(store)
        )

    def test_a_malformed_workspace_baseline_is_refused(self):
        for revision in ("not a mapping", None, [], 5):
            with self.subTest(workspace_revision=revision):
                store = copy.deepcopy(ManifestOracleTests.accepted_store)
                store["workspace_revision"] = revision
                self.assertEqual(
                    (None, impact_proposal.REASON_UNSUPPORTED_EVIDENCE),
                    self._build(store),
                )

    def test_a_store_with_no_artifacts_is_refused_by_its_evidence_bindings(self):
        # A store the gate accepts but that holds no artifact is not
        # unsupported *evidence*: it is valid evidence that does not hold the
        # identity the intent names.
        store = {
            k: v
            for k, v in ManifestOracleTests.accepted_store.items()
            if k != "artifacts"
        }
        self.assertEqual(
            (None, impact_proposal.REASON_UNBOUND_EVIDENCE), self._build(store)
        )

    def test_a_well_formed_mutated_store_still_builds(self):
        store = copy.deepcopy(ManifestOracleTests.accepted_store)
        store["artifacts"] = store["artifacts"] + [
            {
                "record_type": "artifact",
                "id": "artifact:class:app.service",
                "kind": "class",
                "path": "app/service.py",
                "module": "app.service",
                "name": "Service",
                "locator": "app.service.Service2",
            }
        ]
        proposal, error = self._build(store)
        self.assertIsNone(error, error)
        self.assertIsNone(impact_proposal.validate_impact_proposal(proposal))


if __name__ == "__main__":
    unittest.main()
