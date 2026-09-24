"""The controlled-change reconciliation record (P5-E2).

First, the **oracle**: ``fixtures/reconciliation/manifest.json`` states, by hand
and from the acceptance criteria, what a controlled-change record must answer for
a canonical success and for every bounded non-success and refusal. The runner
below holds the implementation to it; a case never re-derives its own expected
output from the builder.

Second, the record's own shape: it reports on a decision, so it must be provably
incapable of being read as one — it approves nothing, applies nothing and
performed no execution, whatever its terminal state says.

Third, the module's purity, checked over its source rather than asserted in
prose: it is a leaf over the standard library and ``core.identity``, and it
reaches no store, filesystem, clock, process, network, provider, credential,
runner or container host, and no Twin, Memory, validation, execution,
integrations or boundary module.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import unittest

from hrca.authoring import work_reconciliation

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
MANIFEST = os.path.join(REPO, "fixtures", "reconciliation", "manifest.json")
MODULE = os.path.join(REPO, "src", "hrca", "authoring", "work_reconciliation.py")

# Keys an ``expect`` block may carry. A typo is a failure, not a silently
# unchecked expectation.
_EXPECT_KEYS = frozenset({"result", "state", "complete", "reason_code", "error"})
_CASE_KEYS = frozenset({"case", "set", "drop", "literal", "expect"})


def _manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)


def _bundle(case: dict, base: dict) -> object:
    if "literal" in case:
        return case["literal"]
    bundle = copy.deepcopy(base)
    for key in case.get("drop", []):
        bundle.pop(key, None)
    for path, value in sorted(case.get("set", {}).items()):
        node = bundle
        parts = path.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    return bundle


class ManifestOracleTests(unittest.TestCase):
    """Every case in the hand-authored manifest, run exactly as written."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.manifest = _manifest()
        cls.base = cls.manifest["base"]
        cls.cases = {case["case"]: case for case in cls.manifest["cases"]}

    def test_the_manifest_covers_both_a_success_and_a_non_success(self):
        states = {
            case["expect"].get("state")
            for case in self.manifest["cases"]
            if case["expect"]["result"] == "record"
        }
        self.assertIn(work_reconciliation.STATE_COMPLETE, states)
        self.assertTrue(states - {work_reconciliation.STATE_COMPLETE})

    def test_the_manifest_names_every_terminal_state(self):
        named = {
            case["expect"].get("state")
            for case in self.manifest["cases"]
            if case["expect"]["result"] == "record"
        }
        self.assertEqual(set(work_reconciliation.RECONCILIATION_STATES), named)

    def test_every_case_name_is_unique(self):
        names = [case["case"] for case in self.manifest["cases"]]
        self.assertEqual(len(names), len(set(names)))

    def test_every_case_uses_only_known_keys(self):
        for case in self.manifest["cases"]:
            with self.subTest(case=case["case"]):
                self.assertTrue(set(case) <= _CASE_KEYS, sorted(case))
                self.assertTrue(
                    set(case["expect"]) <= _EXPECT_KEYS, sorted(case["expect"])
                )

    def test_the_base_bundle_succeeds_on_its_own(self):
        # The base is the canonical success shape; a case is a departure from it.
        record, error = work_reconciliation.build_reconciliation(
            copy.deepcopy(self.base)
        )
        self.assertIsNone(error, error)
        self.assertEqual(work_reconciliation.STATE_COMPLETE, record["state"])

    def test_every_case(self):
        for case in self.manifest["cases"]:
            with self.subTest(case=case["case"]):
                self._run_case(case)

    def _run_case(self, case: dict) -> None:
        name = case["case"]
        expect = case["expect"]
        bundle = _bundle(case, self.base)
        record, error = work_reconciliation.build_reconciliation(bundle)

        if expect["result"] == "refused":
            self.assertIsNone(record, name)
            self.assertEqual(expect["error"], error, name)
            return

        self.assertIsNone(error, "%s: %s" % (name, error))
        self.assertEqual(expect["state"], record["state"], name)
        self.assertEqual(expect["reason_code"], record["reason_code"], name)
        self.assertIs(expect["complete"], record["complete"], name)
        # The record is a valid P5-E2 record whatever it concluded.
        self.assertIsNone(work_reconciliation.validate_reconciliation(record), name)


def _strings(value):
    """Yield every string inside a nested record."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class RecordShapeTests(unittest.TestCase):
    """What the record claims about itself, on every terminal state.

    Driven from the manifest's own record cases rather than from a hand-built
    state list, so every one of the seven terminal states is genuinely
    exercised — the manifest is held to covering them all by
    :meth:`ManifestOracleTests.test_the_manifest_names_every_terminal_state`.
    """

    @classmethod
    def setUpClass(cls):
        manifest = _manifest()
        cls.base = manifest["base"]
        cls.records = {}
        for case in manifest["cases"]:
            if case["expect"]["result"] != "record":
                continue
            record, error = work_reconciliation.build_reconciliation(
                _bundle(case, cls.base)
            )
            assert error is None, error
            cls.records[case["case"]] = record

    def _record(self):
        record, error = work_reconciliation.build_reconciliation(
            copy.deepcopy(self.base)
        )
        self.assertIsNone(error, error)
        return record

    def test_every_terminal_state_is_represented_among_the_cases(self):
        self.assertEqual(
            set(work_reconciliation.RECONCILIATION_STATES),
            {record["state"] for record in self.records.values()},
        )

    def test_no_record_ever_approves_applies_or_executes(self):
        # On every state, including the success one, because this record reports
        # on a decision and must never be readable as having taken or performed
        # one.
        for name, record in sorted(self.records.items()):
            with self.subTest(case=name):
                self.assertIs(False, record["approved"])
                self.assertIs(False, record["applied"])
                self.assertIs(False, record["execution_performed"])

    def test_complete_is_true_only_for_the_complete_state(self):
        for name, record in sorted(self.records.items()):
            with self.subTest(case=name):
                self.assertIs(
                    record["state"] == work_reconciliation.STATE_COMPLETE,
                    record["complete"],
                )

    def test_only_the_success_case_completes(self):
        completing = [
            name
            for name, record in sorted(self.records.items())
            if record["complete"]
        ]
        self.assertEqual(["complete_applied", "complete_not_applied"], completing)

    def test_the_record_carries_every_bound_group_verbatim(self):
        record = self._record()
        for group in (
            "work_package",
            "change",
            "validation",
            "acceptance",
            "application",
            "accepted_revision",
            "observed",
            "freshness",
        ):
            with self.subTest(group=group):
                self.assertEqual(self.base[group], record[group])

    def test_the_record_declares_every_mutation_boundary_false(self):
        record = self._record()
        self.assertTrue(record["mutation_surface"])
        for key, value in sorted(record["mutation_surface"].items()):
            with self.subTest(key=key):
                self.assertIs(False, value)

    def test_the_record_states_its_own_limitations(self):
        record = self._record()
        self.assertTrue(record["limitations"])
        for limitation in record["limitations"]:
            with self.subTest(limitation=limitation[:40]):
                self.assertIsInstance(limitation, str)
                self.assertLess(len(limitation), 300)

    def test_no_record_carries_a_filesystem_path(self):
        for name, record in sorted(self.records.items()):
            for text in _strings(record):
                with self.subTest(case=name, text=text[:40]):
                    self.assertNotIn("/", text)
                    self.assertNotIn("\\", text)

    def test_a_bundle_missing_its_groups_is_refused_with_a_path_free_reason(self):
        for bundle in ({}, {"change": {}}, None, 5, "bundle"):
            with self.subTest(bundle=bundle):
                _, error = work_reconciliation.build_reconciliation(bundle)
                self.assertIsNotNone(error)
                for character in ("/", "\\"):
                    self.assertNotIn(character, error)
                self.assertLess(len(error), 200)


class DeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = _manifest()["base"]

    def test_the_same_bundle_is_byte_identical(self):
        first, _ = work_reconciliation.build_reconciliation(copy.deepcopy(self.base))
        second, _ = work_reconciliation.build_reconciliation(copy.deepcopy(self.base))
        self.assertEqual(
            work_reconciliation.dumps(first), work_reconciliation.dumps(second)
        )

    def test_the_identity_matches_the_content(self):
        record, _ = work_reconciliation.build_reconciliation(copy.deepcopy(self.base))
        self.assertTrue(
            record["reconcile_id"].startswith(
                work_reconciliation.RECONCILIATION_ID_PREFIX
            )
        )
        self.assertEqual(
            record["reconcile_id"], work_reconciliation.reconciliation_id_for(record)
        )

    def test_the_identity_is_excluded_from_its_own_derivation(self):
        record, _ = work_reconciliation.build_reconciliation(copy.deepcopy(self.base))
        tampered = dict(record, reconcile_id="reconcile:not-the-real-one")
        self.assertEqual(
            record["reconcile_id"], work_reconciliation.reconciliation_id_for(tampered)
        )

    def test_a_changed_baseline_yields_a_distinct_record(self):
        base_record, _ = work_reconciliation.build_reconciliation(
            copy.deepcopy(self.base)
        )
        bundle = copy.deepcopy(self.base)
        bundle["observed"]["baseline_fingerprint"] = "4" * 64
        other, _ = work_reconciliation.build_reconciliation(bundle)
        self.assertNotEqual(base_record["reconcile_id"], other["reconcile_id"])

    def test_the_only_time_like_value_is_the_one_supplied(self):
        # Nothing here reads a clock, so the sole timestamp in the record is the
        # caller's decision time, carried verbatim.
        record, _ = work_reconciliation.build_reconciliation(copy.deepcopy(self.base))
        stamps = [
            text
            for text in _strings(record)
            if "T" in text and ":" in text and text.endswith("Z")
        ]
        self.assertEqual([self.base["acceptance"]["decided_at"]], stamps)


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = _manifest()["base"]

    def _record(self):
        record, _ = work_reconciliation.build_reconciliation(copy.deepcopy(self.base))
        return record

    def test_a_built_record_validates(self):
        self.assertIsNone(work_reconciliation.validate_reconciliation(self._record()))

    def test_a_non_mapping_is_refused(self):
        for value in ("x", 5, [], None):
            with self.subTest(value=value):
                self.assertEqual(
                    "record is not a mapping",
                    work_reconciliation.validate_reconciliation(value),
                )

    def test_an_unsupported_schema_is_refused(self):
        self.assertEqual(
            "unsupported schema_version",
            work_reconciliation.validate_reconciliation(
                dict(self._record(), schema_version="9.9.9")
            ),
        )

    def test_an_unknown_state_is_refused(self):
        self.assertEqual(
            "unknown reconciliation state",
            work_reconciliation.validate_reconciliation(
                dict(self._record(), state="probably_fine")
            ),
        )

    def test_a_record_that_approves_or_applies_is_refused(self):
        for field in ("approved", "applied", "execution_performed"):
            with self.subTest(field=field):
                reason = work_reconciliation.validate_reconciliation(
                    dict(self._record(), **{field: True})
                )
                self.assertIn("must not", reason)

    def test_a_completion_claim_that_disagrees_with_the_state_is_refused(self):
        record = self._record()
        record["complete"] = not record["complete"]
        self.assertEqual(
            "complete does not match the terminal state",
            work_reconciliation.validate_reconciliation(record),
        )

    def test_a_missing_or_mismatched_identity_is_refused(self):
        for identity in (None, "", "no-prefix"):
            with self.subTest(identity=identity):
                self.assertEqual(
                    "missing or malformed reconcile_id",
                    work_reconciliation.validate_reconciliation(
                        dict(self._record(), reconcile_id=identity)
                    ),
                )
        record = self._record()
        record["observed"]["scan_generation"] = 99
        self.assertEqual(
            "reconcile_id does not match the record content",
            work_reconciliation.validate_reconciliation(record),
        )

    def test_a_widened_mutation_surface_is_refused(self):
        # The identity is re-derived after the tamper, so this asserts the
        # surface rule rather than tripping the identity rule first.
        record = self._record()
        record["mutation_surface"]["repository_source"] = True
        record["reconcile_id"] = work_reconciliation.reconciliation_id_for(record)
        self.assertEqual(
            "mutation_surface declares a non-false repository_source",
            work_reconciliation.validate_reconciliation(record),
        )

    def test_a_surface_that_omits_a_boundary_is_refused(self):
        record = self._record()
        record["mutation_surface"].pop("remote")
        record["reconcile_id"] = work_reconciliation.reconciliation_id_for(record)
        self.assertEqual(
            "mutation_surface does not declare every named boundary",
            work_reconciliation.validate_reconciliation(record),
        )

    def test_a_missing_limitations_block_is_refused(self):
        record = self._record()
        record["limitations"] = []
        record["reconcile_id"] = work_reconciliation.reconciliation_id_for(record)
        self.assertEqual(
            "missing or malformed limitations",
            work_reconciliation.validate_reconciliation(record),
        )


# The names this module may reach. Standard library plus the shared identity
# leaf, and nothing else: no capability, no seam, no host.
_ALLOWED_IMPORTS = frozenset({"__future__", "json", "typing", "identity"})
_FORBIDDEN_IMPORTS = frozenset(
    {
        "os", "io", "sys", "pathlib", "subprocess", "socket", "ssl", "urllib",
        "http", "requests", "shutil", "tempfile", "sqlite3", "pickle",
        "importlib", "time", "datetime", "random", "secrets", "uuid",
        "logging", "warnings", "ctypes", "winreg", "multiprocessing",
        "threading", "asyncio", "PySide6",
    }
)
_FORBIDDEN_MODULES = frozenset(
    {"twin", "memory", "validation", "validation_plan", "validation_policy",
     "container_runner", "execution", "integrations", "boundary", "ui",
     "candidate", "candidate_edit", "document", "library"}
)


def _imports(path):
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
    return names


class PurityTests(unittest.TestCase):
    """The composition is a leaf, checked over its source, not asserted in prose."""

    def test_the_module_imports_only_the_standard_library_and_identity(self):
        self.assertEqual(_ALLOWED_IMPORTS, _imports(MODULE))

    def test_the_module_reaches_no_host_or_capability(self):
        imported = _imports(MODULE)
        self.assertEqual(set(), imported & _FORBIDDEN_IMPORTS)
        self.assertEqual(set(), imported & _FORBIDDEN_MODULES)

    def test_the_module_opens_no_file_and_reads_no_clock(self):
        with open(MODULE, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    violations.append(("open()", node.lineno))
                elif isinstance(func, ast.Attribute) and isinstance(
                    func.value, ast.Name
                ):
                    if func.value.id in _FORBIDDEN_IMPORTS:
                        violations.append(
                            ("%s.%s" % (func.value.id, func.attr), node.lineno)
                        )
        self.assertEqual([], violations)

    def test_the_module_carries_no_module_level_store_or_capability_handle(self):
        # A module that cannot name a store, a runner or a client cannot reach
        # one: the names below are what such a reach would need.
        with open(MODULE, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        assigned = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for name in sorted(assigned):
            with self.subTest(name=name):
                for fragment in ("store", "runner", "client", "session", "socket"):
                    self.assertNotIn(fragment, name.lower(), name)


if __name__ == "__main__":
    unittest.main()
