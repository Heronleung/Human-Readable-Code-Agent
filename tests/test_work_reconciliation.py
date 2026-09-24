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
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from hrca.authoring import validation, validation_plan, validation_policy
from hrca.authoring import work_reconciliation
from hrca.cli import work_reconciliation_cli as cli
from hrca.core import identity
from hrca.source import scanner
from hrca import twin
from hrca.twin import memory_twin_link, twin_store

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


# -- the offline operator adapter (P5-E2/B) -------------------------------
#
# The composition above is a pure leaf on purpose, which puts three
# responsibilities in exactly one other place: reading stores, invoking the
# *owners'* gates, and writing the record. These tests hold that adapter to
# them, and hold the composition's two literal verdict tokens to the modules
# that own them.

_ACCEPTED_FINGERPRINT = "2" * 64
# The corpus is published at generation 2, so the revision the change was
# accepted against is a real earlier one. A generation of 0 is not a revision
# this product ever produces, and the composition refuses it.
_OBSERVED_GENERATION = 2
_ACCEPTED_GENERATION = 1


def _attempt_document(candidate_id, state="passed"):
    attempt = {
        "schema_version": validation.VALIDATION_ATTEMPT_SCHEMA_VERSION,
        "generator": validation.VALIDATION_ATTEMPT_GENERATOR,
        "state": state,
        "approved": False,
        "adopted": False,
        "applied": False,
        "ordinal": 1,
        "candidate_id": candidate_id,
    }
    attempt["attempt_id"] = validation.attempt_id_for(attempt)
    return attempt


def _result_document(candidate_id, plan_id, state="passed"):
    """A schema-valid validation result, assembled without running anything."""
    result = {
        "schema_version": validation.VALIDATION_RESULT_SCHEMA_VERSION,
        "generator": validation.VALIDATION_RESULT_GENERATOR,
        "result_id": "",
        "plan_id": plan_id,
        "candidate_id": candidate_id,
        "policy_version": validation_policy.POLICY_VERSION,
        "state": state,
        "evidence_complete": state == validation.STATE_PASSED,
        "checks": [{"check_id": "check:quotation_reference"}],
        "attempts": [_attempt_document(candidate_id, state)],
        "approved": False,
        "adopted": False,
        "applied": False,
        "mutation_surface": validation._mutation_surface(),
    }
    result["result_id"] = validation.result_id_for(result)
    return result


def _plan_document(candidate_id, edit_id, intent_id, proposal_id, binding_fingerprint):
    plan = {
        "schema_version": validation_plan.VALIDATION_PLAN_SCHEMA_VERSION,
        "generator": validation_plan.VALIDATION_PLAN_GENERATOR,
        "plan_id": "",
        "policy_version": validation_policy.POLICY_VERSION,
        "candidate": {
            "candidate_id": candidate_id,
            "edit_id": edit_id,
            "intent_delta_id": intent_id,
            "proposal_id": proposal_id,
            "binding_fingerprint": binding_fingerprint,
        },
        "checks": [],
        "executable": False,
        "applied": False,
    }
    plan["plan_id"] = validation_plan.plan_id_for(plan)
    return plan


class AdapterCase(unittest.TestCase):
    """A scanned corpus, its authoritative Twin store, and the three documents."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.root = os.path.join(self.base, "corpus")
        os.makedirs(os.path.join(self.root, "pkg"))
        with open(os.path.join(self.root, "pkg", "mod.py"), "w", encoding="utf-8") as h:
            h.write("def f():\n    return 1\n")
        self.workspace_id, self.store = self._publish()
        self.entity_id, self.entity_kind = self._an_artifact()
        self.candidate_id = "candidate:" + "a" * 64
        self.plan = _plan_document(
            self.candidate_id, "edit:" + "b" * 64, "intent:" + "c" * 64,
            "impact:" + "d" * 64, "bind:" + "e" * 64,
        )
        self.result = _result_document(self.candidate_id, self.plan["plan_id"])
        self.link = self._link(self.store["workspace_revision"]["scan_generation"])

    def tearDown(self):
        self.tmp.cleanup()

    def _publish(self):
        document = scanner.scan_directory(self.root)
        fingerprints = {}
        for record in document.get("files", []):
            path = record.get("path")
            if isinstance(path, str) and path.endswith((".py", ".pyi")):
                with open(os.path.join(self.root, path), "rb") as handle:
                    fingerprints[path] = twin.fingerprint_bytes(handle.read())
        workspace_id = identity.workspace_id_for(self.root)
        store = twin.build_store(
            document, fingerprints, workspace_id, _OBSERVED_GENERATION, "T"
        )
        self.assertIsNone(twin_store.save(self.base, workspace_id, store))
        return workspace_id, store

    def _an_artifact(self):
        for artifact in self.store["artifacts"]:
            if artifact.get("locator"):
                return artifact["id"], artifact["kind"]
        raise AssertionError("the corpus produced no symbol artifact")

    def _link(self, recorded_revision):
        return {
            "link_schema_version": "1.0.0",
            "workspace_id": self.workspace_id,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "memory_run_id": "run:smoke:s-1:run",
            "memory_record_id": "evidence:run:smoke:s-1:run:" + "f" * 32,
            "recorded_revision": recorded_revision,
        }

    def _write(self, name, document):
        path = os.path.join(self.base, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path

    def _argv(self, declared="applied", accepted=None, result=None, link=None, **extra):
        accepted = accepted or (_ACCEPTED_GENERATION, _ACCEPTED_FINGERPRINT)
        argv = {
            "--result": self._write("result.json", result or self.result),
            "--plan": self._write("plan.json", self.plan),
            "--link": self._write("link.json", link or self.link),
            "--twin-store": self.base,
            "--workspace-id": self.workspace_id,
            "--accepted-generation": str(accepted[0]),
            "--accepted-fingerprint": accepted[1],
            "--run-id": "run:smoke:s-1:run",
            "--record-id": self.link["memory_record_id"],
            "--actor": "heron",
            "--decided-at": "2026-01-01T00:00:00Z",
            "--declared": declared,
            "--base": os.path.join(self.base, "out"),
        }
        argv.update(extra)
        return argv

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([item for pair in sorted(argv.items())
                             for item in pair])
        return code, out.getvalue(), err.getvalue()

    def _records(self):
        directory = os.path.join(self.base, "out", "reconciliations")
        if not os.path.isdir(directory):
            return []
        return sorted(os.listdir(directory))


class AdapterTests(AdapterCase):
    """The adapter reads stores, invokes the owners and writes one record."""

    def test_the_supplied_documents_are_sound_before_anything_is_recorded(self):
        # If these were not sound the adapter's refusals would be vacuous.
        self.assertIsNone(validation.validate_result(self.result))
        self.assertIsNone(validation_plan.migrate_plan(self.plan)[1])
        self.assertEqual(
            memory_twin_link.FRESHNESS_CURRENT,
            memory_twin_link.resolve_freshness(
                self.link, self.store, self.workspace_id
            )["freshness"],
        )

    def test_a_complete_change_is_recorded_and_exits_zero(self):
        code, out, err = self._run(self._argv())
        self.assertEqual(cli.EXIT_OK, code, err)
        record = json.loads(out)
        self.assertEqual(work_reconciliation.STATE_COMPLETE, record["state"])
        self.assertIs(True, record["complete"])
        self.assertIsNone(work_reconciliation.validate_reconciliation(record))
        self.assertEqual(
            [record["reconcile_id"].split(":", 1)[1] + ".json"], self._records()
        )

    def test_the_written_record_is_the_printed_one(self):
        code, out, _ = self._run(self._argv())
        self.assertEqual(cli.EXIT_OK, code)
        with open(
            os.path.join(self.base, "out", "reconciliations", self._records()[0]),
            encoding="utf-8",
        ) as handle:
            written = json.load(handle)
        self.assertEqual(json.loads(out), written)

    def test_the_record_binds_the_revision_the_store_actually_holds(self):
        code, out, _ = self._run(self._argv())
        self.assertEqual(cli.EXIT_OK, code)
        record = json.loads(out)
        revision = self.store["workspace_revision"]
        self.assertEqual(
            {
                "workspace_id": revision["workspace_id"],
                "scan_generation": revision["scan_generation"],
                "baseline_fingerprint": revision["baseline_fingerprint"],
            },
            record["observed"],
        )
        self.assertEqual(self.plan["candidate"]["candidate_id"],
                         record["acceptance"]["candidate_id"])

    def test_a_second_identical_run_writes_no_second_record(self):
        first = self._run(self._argv())
        second = self._run(self._argv())
        self.assertEqual(cli.EXIT_OK, first[0])
        self.assertEqual(cli.EXIT_OK, second[0])
        self.assertEqual(first[1], second[1])
        self.assertEqual(1, len(self._records()))

    def test_declaring_no_application_with_an_unmoved_baseline_completes(self):
        code, out, err = self._run(
            self._argv(
                declared="not_applied",
                accepted=(self.store["workspace_revision"]["scan_generation"],
                          self.store["workspace_revision"]["baseline_fingerprint"]),
            )
        )
        self.assertEqual(cli.EXIT_OK, code, err)
        self.assertEqual(work_reconciliation.STATE_COMPLETE, json.loads(out)["state"])

    def test_declaring_application_beside_an_unmoved_baseline_is_a_non_success(self):
        code, out, err = self._run(
            self._argv(
                declared="applied",
                accepted=(self.store["workspace_revision"]["scan_generation"],
                          self.store["workspace_revision"]["baseline_fingerprint"]),
            )
        )
        self.assertEqual(cli.EXIT_NOT_COMPLETE, code, err)
        record = json.loads(out)
        self.assertEqual(work_reconciliation.STATE_APPLICATION_UNCONFIRMED, record["state"])
        self.assertIs(False, record["complete"])

    def test_a_stale_link_is_a_non_success_the_owner_reported(self):
        generation = self.store["workspace_revision"]["scan_generation"]
        stale = self._link(generation - 1)
        self.assertEqual(
            memory_twin_link.FRESHNESS_STALE,
            memory_twin_link.resolve_freshness(
                stale, self.store, self.workspace_id
            )["freshness"],
        )
        code, out, err = self._run(self._argv(link=stale))
        self.assertEqual(cli.EXIT_NOT_COMPLETE, code, err)
        self.assertEqual(work_reconciliation.STATE_FRESHNESS_LOST, json.loads(out)["state"])

    def test_a_link_for_another_workspace_is_a_non_success(self):
        other = dict(self.link, workspace_id="ws:" + "9" * 64)
        code, out, err = self._run(self._argv(link=other))
        self.assertEqual(cli.EXIT_NOT_COMPLETE, code, err)
        self.assertEqual(work_reconciliation.STATE_FRESHNESS_LOST, json.loads(out)["state"])


class AdapterRefusalTests(AdapterCase):
    """An unsound or absent input is refused, and nothing is written."""

    def _refused(self, argv):
        code, out, err = self._run(argv)
        self.assertEqual(cli.EXIT_REFUSED, code)
        self.assertEqual("", out)
        self.assertTrue(err.startswith("refused: "), err)
        self.assertEqual([], self._records())
        return err.strip()

    def test_an_unsound_result_document_is_refused(self):
        broken = dict(self.result, evidence_complete=False)
        broken["result_id"] = validation.result_id_for(broken)
        self.assertIsNotNone(validation.validate_result(broken))
        self._refused(self._argv(result=broken))

    def test_a_result_that_never_passed_is_bound_rather_than_refused(self):
        # A sound result whose state is not a pass is *not* a refusal: the
        # record must be produced and must say why it is not complete.
        failed = _result_document(self.candidate_id, self.plan["plan_id"], "failed")
        self.assertIsNone(validation.validate_result(failed))
        code, out, err = self._run(self._argv(result=failed))
        self.assertEqual(cli.EXIT_NOT_COMPLETE, code, err)
        self.assertEqual(
            work_reconciliation.STATE_VALIDATION_NOT_ACCEPTED,
            json.loads(out)["state"],
        )

    def test_a_plan_that_is_not_a_mapping_is_refused(self):
        # Written under its own name: `_argv` writes the sound plan, so an
        # override that reused that path would be clobbered before the run.
        argv = self._argv()
        argv["--plan"] = self._write("bad-plan.json", [1, 2, 3])
        self._refused(argv)

    def test_a_missing_twin_store_is_refused(self):
        argv = self._argv()
        argv["--twin-store"] = os.path.join(self.base, "empty")
        self._refused(argv)

    def test_no_acceptance_is_ever_inferred(self):
        argv = self._argv()
        argv["--actor"] = ""
        self._refused(argv)

    def test_a_result_id_that_does_not_match_its_content_is_refused(self):
        tampered = dict(self.result, result_id="result:" + "0" * 64)
        self._refused(self._argv(result=tampered))

    def test_the_adapter_writes_nothing_outside_its_base(self):
        # The documents are written by `_argv` before the snapshot is taken, so
        # the only thing the run can add is its own output directory. The store
        # path comes from the store's own public constructor rather than a
        # guessed layout, and the corpus is read back byte for byte.
        argv = self._argv()
        store_path = twin_store.workspace_store_path(self.base, self.workspace_id)
        with open(store_path, "rb") as handle:
            before_store = handle.read()
        before_corpus = sorted(os.listdir(self.root))
        before_base = set(os.listdir(self.base))
        code, _, _ = self._run(argv)
        self.assertEqual(cli.EXIT_OK, code)
        self.assertEqual(before_corpus, sorted(os.listdir(self.root)))
        with open(store_path, "rb") as handle:
            self.assertEqual(before_store, handle.read())
        self.assertEqual({"out"}, set(os.listdir(self.base)) - before_base)


class OwnerVocabularyPinTests(unittest.TestCase):
    """The composition's two literal tokens are held to the modules that own them.

    It must not import those modules — that is what keeps it a leaf — so the two
    verdict tokens it compares against are literals here. Pinning them to their
    owners turns a silent duplication into a guarded one: a re-pin that moved
    either value fails here rather than quietly changing every record's answer.
    """

    def test_the_passing_validation_token_is_the_validations_own(self):
        self.assertEqual(validation.STATE_PASSED, work_reconciliation.STATE_PASSED)

    def test_the_current_freshness_token_is_the_links_own(self):
        self.assertEqual(
            memory_twin_link.FRESHNESS_CURRENT, work_reconciliation.FRESHNESS_CURRENT
        )

    def test_the_current_freshness_token_is_not_one_of_the_failure_verdicts(self):
        # A pin that only compared `current` to itself would not notice the
        # token drifting into the failure set.
        self.assertNotIn(
            work_reconciliation.FRESHNESS_CURRENT,
            {
                memory_twin_link.FRESHNESS_STALE,
                memory_twin_link.FRESHNESS_HISTORICAL,
                memory_twin_link.FRESHNESS_MISSING,
                memory_twin_link.FRESHNESS_UNSUPPORTED,
            },
        )


if __name__ == "__main__":
    unittest.main()

