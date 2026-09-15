"""Fixture-corpus acceptance tests for Developer Memory (M4.1).

Every session fixture is re-imported and replayed repeatedly and must produce
byte-stable normalized output, exactly one logical event per identity and the
same terminal ``AgentRun`` state every time. Expected values come from
``fixtures/memory/manifest.json``, so the corpus documents its own contract.

The final class proves the contract's authority boundaries hold: the domain
modules are offline, the core is source-neutral, and this work package installs
no hook, reads no real session and stores no raw payload.
"""

from __future__ import annotations

import ast
import json
import os
import unittest

from hrca import memory, memory_cli, memory_store

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures", "memory"))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))


def _load(rel_path):
    with open(os.path.join(FIXTURES, rel_path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _manifest():
    return _load("manifest.json")


def _read_source(name):
    """Return the source text of an ``hrca`` module."""
    with open(os.path.join(_SRC, name + ".py"), "r", encoding="utf-8") as fh:
        return fh.read()


def _replay(rel_path):
    store, err, outcomes = memory.ingest_session(_load(rel_path))
    return store, err, outcomes


class ManifestTests(unittest.TestCase):
    def test_the_manifest_names_every_session_fixture(self):
        on_disk = {
            name
            for name in os.listdir(os.path.join(FIXTURES, "sessions"))
            if name.endswith(".json")
        }
        listed = {os.path.basename(case["file"]) for case in _manifest()["sessions"]}
        self.assertEqual(on_disk, listed)

    def test_the_manifest_names_every_store_fixture(self):
        on_disk = {
            name
            for name in os.listdir(os.path.join(FIXTURES, "stores"))
            if name.endswith(".json")
        }
        listed = {os.path.basename(case["file"]) for case in _manifest()["stores"]}
        self.assertEqual(on_disk, listed)

    def test_every_declared_file_exists(self):
        manifest = _manifest()
        self.assertEqual(manifest["schema_version"], memory.MEMORY_SCHEMA_VERSION)
        self.assertEqual(manifest["generator"], memory.MEMORY_GENERATOR)
        for case in manifest["sessions"] + manifest["stores"]:
            with self.subTest(case=case["name"]):
                self.assertTrue(os.path.isfile(os.path.join(FIXTURES, case["file"])))

    def test_the_corpus_covers_every_required_case(self):
        names = {case["name"] for case in _manifest()["sessions"]}
        required = {
            "completed", "failed", "cancelled", "blocked", "malformed", "duplicate",
            "conflicting", "out_of_order", "missing_terminal", "unsupported_event",
            "unknown_outcome", "invalid_transition", "privacy", "empty",
        }
        self.assertTrue(required <= names, sorted(required - names))


class SessionFixtureTests(unittest.TestCase):
    """Each manifest expectation is an acceptance condition for that case."""

    def test_every_session_matches_its_declared_expectations(self):
        for case in _manifest()["sessions"]:
            with self.subTest(case=case["name"]):
                store, err, _ = _replay(case["file"])
                self.assertIsNone(err)
                result = memory.replay_result(store)
                self.assertEqual(result["state"], case["expect_state"])
                self.assertEqual(memory.is_success(store), case["expect_success"])
                self.assertEqual(len(result["event_ids"]), case["expect_events"])
                self.assertEqual(len(result["evidence_ids"]), case["expect_evidence"])
                self.assertEqual(len(result["decision_ids"]), case["expect_decisions"])
                self.assertEqual(
                    len(result["change_set_ids"]), case["expect_change_sets"]
                )
                self.assertEqual(
                    len(result["code_entity_link_ids"]), case["expect_code_entity_links"]
                )
                self.assertEqual(
                    len(result["rejection_reasons"]), case["expect_reason_count"]
                )
                self.assertEqual(
                    len(result["quarantine_ids"]), case["expect_quarantines"]
                )

    def test_replay_is_byte_stable_across_repeated_imports(self):
        for case in _manifest()["sessions"]:
            with self.subTest(case=case["name"]):
                session = _load(case["file"])
                dumps = [
                    memory.dumps(memory.ingest_session(session)[0]) for _ in range(4)
                ]
                self.assertEqual(len(set(dumps)), 1)

    def test_there_is_exactly_one_logical_event_per_identity(self):
        for case in _manifest()["sessions"]:
            with self.subTest(case=case["name"]):
                store, _, _ = _replay(case["file"])
                ids = memory.event_ids(store)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(sorted(ids), sorted(set(ids)))

    def test_the_terminal_state_never_drifts(self):
        for case in _manifest()["sessions"]:
            with self.subTest(case=case["name"]):
                session = _load(case["file"])
                states = {
                    memory.run_state(memory.ingest_session(session)[0]) for _ in range(3)
                }
                self.assertEqual(len(states), 1)

    def test_every_persisted_run_state_is_a_reported_state(self):
        for case in _manifest()["sessions"]:
            with self.subTest(case=case["name"]):
                store, _, _ = _replay(case["file"])
                self.assertIn(memory.run_state(store), memory.REPORTED_RUN_STATES)

    def test_only_the_completed_fixtures_are_success(self):
        successes = {
            case["name"]
            for case in _manifest()["sessions"]
            if memory.is_success(_replay(case["file"])[0])
        }
        self.assertEqual(successes, {"completed", "conflicting", "duplicate",
                                     "invalid_transition", "malformed", "out_of_order",
                                     "privacy"})

    def test_the_distinct_outcomes_stay_distinct(self):
        states = {
            name: memory.run_state(_replay("sessions/%s.json" % name)[0])
            for name in ("completed", "failed", "cancelled", "blocked",
                         "missing_terminal", "unknown_outcome", "unsupported_event")
        }
        self.assertEqual(len(set(states.values())), len(states))

    def test_fail_closed_cases_produce_explicit_records(self):
        expected = {
            "invalid_transition": memory.REASON_INVALID_TRANSITION,
            "malformed": memory.REASON_MALFORMED_EVENT,
            "unknown_outcome": memory.REASON_UNKNOWN_OUTCOME,
            "unsupported_event": memory.REASON_UNSUPPORTED_EVENT_TYPE,
        }
        for name, reason in expected.items():
            with self.subTest(case=name):
                store, _, _ = _replay("sessions/%s.json" % name)
                self.assertIn(
                    reason, [r["reason"] for r in store["rejections"]]
                )
        store, _, _ = _replay("sessions/conflicting.json")
        self.assertEqual(store["quarantines"][0]["reason"],
                         memory.REASON_CONFLICTING_REDELIVERY)

    def test_the_out_of_order_fixture_keeps_arrival_order(self):
        store, _, _ = _replay("sessions/out_of_order.json")
        ordered = sorted(store["events"], key=lambda e: e["ingest_ordinal"])
        self.assertEqual(
            [e["source_event_id"] for e in ordered], ["ev-005", "ev-002", "ev-009"]
        )
        self.assertEqual([e["ingest_ordinal"] for e in ordered], [1, 2, 3])

    def test_the_duplicate_fixture_collapses_to_one_event_per_identity(self):
        store, _, _ = _replay("sessions/duplicate.json")
        self.assertEqual(len(store["events"]), 3)
        self.assertEqual(len(memory.event_ids(store)), 3)

    def test_the_conflicting_fixture_retains_the_first_content(self):
        store, _, _ = _replay("sessions/conflicting.json")
        kept = [e for e in store["events"] if e["source_event_id"] == "ev-002"][0]
        self.assertIn("first content", kept["payload"]["message"])
        self.assertEqual(len(store["quarantines"]), 1)

    def test_the_unsupported_fixture_never_certifies_success(self):
        store, _, _ = _replay("sessions/unsupported_event.json")
        self.assertEqual(memory.run_state(store), memory.RUN_UNSUPPORTED)
        self.assertFalse(memory.is_success(store))


class StoreFixtureTests(unittest.TestCase):
    def test_an_older_schema_migrates_without_drift(self):
        case = next(
            c for c in _manifest()["stores"] if c["expect"] == "migrates"
        )
        migrated, err = memory.migrate_memory(_load(case["file"]))
        self.assertIsNone(err)
        self.assertEqual(migrated["schema_version"], case["expect_schema_version"])
        self.assertEqual(memory.run_state(migrated), case["expect_state"])
        # Migration is verified against a fresh replay of the source session:
        # identical event count, identity, evidence link and replay result.
        fresh, err, _ = _replay(case["equivalent_to_session"])
        self.assertIsNone(err)
        self.assertEqual(memory.dumps(migrated), memory.dumps(fresh))
        self.assertEqual(memory.replay_result(migrated), memory.replay_result(fresh))

    def test_an_unsupported_schema_is_an_explicit_blocker(self):
        case = next(c for c in _manifest()["stores"] if c["expect"] == "blocked")
        migrated, err = memory.migrate_memory(_load(case["file"]))
        self.assertIsNone(migrated)
        self.assertEqual(err, case["expect_reason"])


class CliEvidenceTests(unittest.TestCase):
    """The CLI is the reviewable local evidence path, not a capture path."""

    def test_summary_covers_the_whole_corpus(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = memory_cli.main(["summary", FIXTURES])
        self.assertEqual(code, 0)
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), len(_manifest()["sessions"]))

    def test_verify_proves_determinism_and_reports_success(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = memory_cli.main(["verify", FIXTURES])
        self.assertEqual(code, 0)
        self.assertIn("ok", buffer.getvalue())

    def test_migrate_reports_the_blocker_for_an_unsupported_schema(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = memory_cli.main(["migrate", os.path.join(FIXTURES, "stores",
                                                            "unknown_version.json")])
        self.assertEqual(code, 1)
        self.assertIn("blocked", buffer.getvalue())


class OfflineBoundaryTests(unittest.TestCase):
    """The contract's authority boundaries, proved by inspection."""

    _MEMORY_MODULES = ("memory", "memory_store", "memory_cli")
    _NETWORK = frozenset({"http", "socket", "urllib", "ssl", "requests", "ftplib",
                          "smtplib", "asyncio"})

    def _imports(self, name):
        tree = ast.parse(_read_source(name))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found.add(node.module.split(".")[0])
                elif node.level:
                    found.update(alias.name.split(".")[0] for alias in node.names)
        return found

    def test_the_memory_modules_are_offline(self):
        for name in self._MEMORY_MODULES:
            with self.subTest(module=name):
                self.assertTrue(
                    self._imports(name).isdisjoint(self._NETWORK),
                    "%s imports a network primitive" % name,
                )

    def test_the_domain_imports_only_the_standard_library(self):
        imports = self._imports("memory")
        self.assertTrue(
            imports <= {"__future__", "hashlib", "json", "re", "typing"}, sorted(imports)
        )

    def test_the_domain_has_no_filesystem_or_process_access(self):
        """The pure domain performs no I/O and runs nothing."""
        for name in ("memory",):
            with self.subTest(module=name):
                tree = ast.parse(_read_source(name))
                self.assertNotIn("open", {
                    node.func.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                })

    def test_the_core_stays_source_neutral(self):
        """No provider or hook vocabulary may enter the canonical domain.

        Docstrings are developer documentation and are excluded: the design
        note explaining that the core knows nothing about Claude Code hooks is
        prose, not a source-specific mapping. Every other string literal and
        identifier in the domain must be provider-neutral.
        """
        tree = ast.parse(_read_source("memory"))

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = node.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))

        literals = {
            node.value.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings
        }
        imports = self._imports("memory")
        self.assertTrue(
            imports.isdisjoint({"memory_store", "twin", "twin_store", "document",
                                "version_store", "provider", "scanner", "boundary"})
        )
        # These are Claude Code hook field names. Their presence would mean the
        # adapter's mapping had leaked into the canonical domain.
        for forbidden in ("claude", "transcript_path", "pretooluse", "posttooluse",
                          "sessionstart", "hook_event_name"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, literals)

    def test_the_pure_domain_has_no_filesystem_or_process_access(self):
        imports = self._imports("memory")
        self.assertTrue(imports.isdisjoint({"os", "subprocess", "shutil", "pathlib"}))

    def test_no_memory_module_can_spawn_a_process(self):
        """No hook installation, no capture command, no runner."""
        for name in self._MEMORY_MODULES:
            with self.subTest(module=name):
                self.assertTrue(
                    self._imports(name).isdisjoint({"subprocess", "shutil",
                                                    "multiprocessing"}),
                    "%s can reach a process primitive" % name,
                )

    def test_the_store_touches_only_its_own_namespace(self):
        source = _read_source("memory_store")
        self.assertIn('_MEMORY_DIR = "memory"', source)
        for foreign in ("twin.json", "draft.json", "document.json"):
            with self.subTest(name=foreign):
                self.assertNotIn(foreign, source)


if __name__ == "__main__":
    unittest.main()
