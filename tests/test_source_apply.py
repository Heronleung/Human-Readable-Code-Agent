"""The source application coordinator (P5-X A2).

First, the **oracle**: ``apply_fixtures/manifest.json`` states, by hand, what the
coordinator must answer for a canonical success, for every bounded non-success
and for every refusal. The runner below holds the implementation to it and never
re-derives an expectation from the builder or from the world it just made.

Second, the two proofs the design turns on.

*Root identity is derived, never asserted.* The coordinator's canonicalization
is held to the accepted workspace policy over a shared matrix of inputs, and two
directories holding the same relative file are shown to have different
identities — which is why a valid candidate cannot be applied to another
directory that merely looks the same.

*The module is a closed leaf.* Its imports are named exactly, and its
**transitive local closure** is walked statically and asserted to contain no
dispatching, storing, networked or protocol module — in particular not
``validation``, which owns the container-gated runner, and not
``container_runner`` itself.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from hrca.authoring import candidate, candidate_edit, source_apply
from hrca.core import contract, identity, workspace

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
FIXTURES = os.path.join(REPO, "apply_fixtures")
MANIFEST = os.path.join(FIXTURES, "manifest.json")
MODULE = os.path.join(SRC, "hrca", "authoring", "source_apply.py")

# Leaf names that must not appear in the module's local import closure: a
# runner, a store, a provider, a credential, a network client, the desktop
# boundary or its UI, and the validation module that owns the container-gated
# runner.
#
# One group is subtracted below, and it is stated rather than hidden: the
# accepted P5.4 candidate contract imports ``impact_proposal``, which reaches
# ``twin``, ``scanner`` and ``source_evidence`` under the product's own
# documented and mechanically bounded Twin exception for the P5.3 proposal. Any
# module that binds the candidate contract inherits that edge. This module names
# none of it — asserted separately — and removing the edge would mean
# duplicating identity derivation, the manifest schema check and the path
# authorization rule, each of which must have one implementation.
_INHERITED_FROM_THE_P54_CONTRACT = frozenset(
    {"twin", "twin_store", "scanner", "source_evidence"}
)

_FORBIDDEN_LEAVES = frozenset(
    {
        "container_runner",
        "runner_broker",
        "runtime_handlers",
        "runner_image_policy",
        "runner_image_setup",
        "validation",
        "validation_cli",
        "provider",
        "provider_config",
        "provider_cli",
        "deepseek",
        "deepseek_transport",
        "credential_host",
        "credential_store",
        "credential_store_win",
        "client",
        "client_core",
        "style",
        "boundary",
        "twin",
        "twin_store",
        "memory",
        "memory_store",
        "memory_docs",
        "memory_query",
        "memory_revisions",
        "memory_twin_link",
        "scanner",
        "app_package",
        "library_store",
        "version_store",
        "hook_capture",
        "claude_code_hooks",
    }
)


def _world_module():
    """Load the fixture harness by path, so no package layout is assumed."""
    spec = importlib.util.spec_from_file_location(
        "apply_world", os.path.join(FIXTURES, "world.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORLD = _world_module()


def _target_matches(world, expectation):
    """Return whether the target's live state matches a named expectation."""
    target = world.target()
    if expectation == "absent":
        return not os.path.exists(target)
    if expectation == "directory":
        return os.path.isdir(target)
    if expectation == "link":
        return os.path.islink(target)
    digest = WORLD.target_sha256(world)
    if expectation == "predecessor":
        return digest == world.predecessor_sha256
    if expectation == "candidate":
        return digest == world.candidate_sha256
    if expectation in ("drifted", "binary", "neither"):
        return digest is not None and digest not in (
            world.predecessor_sha256,
            world.candidate_sha256,
        )
    raise AssertionError("unknown target expectation: %r" % (expectation,))


def _stand_in(mode):
    """Return a stand-in for the atomic replacement, for the patched cases."""

    def replacement(target, data):
        if mode == "fail":
            return False, source_apply.REASON_REPLACE_FAILED
        payload = (
            WORLD.PREDECESSOR_TEXT.encode("utf-8")
            if mode == "predecessor"
            else b"neither approved byte string\n"
        )
        with open(target, "wb") as handle:
            handle.write(payload)
        return True, None

    return replacement


class OracleTests(unittest.TestCase):
    """Every case in the hand-authored oracle, run exactly as written."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        with open(MANIFEST, encoding="utf-8") as handle:
            cls.manifest = json.load(handle)
        cls.cases = cls.manifest["cases"]
        cls.expect_keys = frozenset(cls.manifest["expect_keys"])

    def _run_case(self, case):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)

        stand_in = None
        drop_receipt = False
        for mutation in case["mutations"]:
            kind = mutation["kind"]
            if kind == "patch_replace":
                stand_in = mutation["mode"]
            elif kind == "drop_receipt":
                drop_receipt = True
            else:
                WORLD.apply_mutation(world, mutation)

        if stand_in is not None:
            patcher = mock.patch.object(
                source_apply, "_replace_target", new=_stand_in(stand_in)
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        receipt = None if drop_receipt else world.receipt
        if case["mode"] == "plan":
            record, reason = source_apply.plan_application(
                world.evidence, world.apply_root
            )
        else:
            record, reason = source_apply.apply_application(
                world.evidence,
                world.apply_root,
                world.custody,
                receipt,
                world.receipt_bytes,
            )
        return world, record, reason

    def _assert_case(self, case):
        self.assertTrue(
            set(case["expect"]) <= self.expect_keys,
            "a case states an expectation the runner does not check: %s"
            % sorted(set(case["expect"]) - self.expect_keys),
        )
        world, record, reason = self._run_case(case)
        expect = case["expect"]

        if "error" in expect:
            self.assertIsNone(record, "expected a refusal, got %r" % (record,))
            self.assertEqual(reason, expect["error"])
        else:
            self.assertIsNotNone(
                record, "expected a record, got the refusal %r" % (reason,)
            )
            self.assertIsNone(
                source_apply.validate_apply_record(record),
                source_apply.validate_apply_record(record),
            )
            self.assertEqual(record["state"], expect["state"])
            if "reason_code" in expect:
                self.assertEqual(record["reason_code"], expect["reason_code"])
            writes = record["write_surface"]
            for key in ("pre_image_written", "temporary_written", "target_written"):
                if key in expect:
                    self.assertIs(writes[key], expect[key], key)
            if "record_persisted" in expect:
                self.assertIs(record["record_persisted"], expect["record_persisted"])
            if "outcome" in expect:
                self.assertEqual(record["observation"]["outcome"], expect["outcome"])
            if record["state"] == source_apply.STATE_APPLIED:
                self.assertEqual(record["recovery"]["origin"], "created")
                self.assertEqual(
                    record["recovery"]["pre_image_sha256"], world.predecessor_sha256
                )
                self.assertTrue(os.path.exists(world.pre_image_path()))
                self.assertEqual(
                    WORLD.sha256_hex(world.read(world.pre_image_path())),
                    world.predecessor_sha256,
                )

        if "target_after" in expect:
            self.assertTrue(
                _target_matches(world, expect["target_after"]),
                "the target does not match %r" % (expect["target_after"],),
            )
        if expect.get("pre_image_written") is False:
            path = world.pre_image_path()
            if os.path.exists(path):
                self.assertNotEqual(
                    WORLD.sha256_hex(world.read(path)), world.predecessor_sha256
                )
        for name in sorted(os.listdir(world.project)):
            self.assertFalse(
                name.startswith(source_apply.TEMP_PREFIX),
                "a temporary file was left behind: %s" % name,
            )

    def test_every_oracle_case_holds(self):
        self.assertTrue(self.cases, "the oracle states no cases")
        for case in self.cases:
            with self.subTest(case=case["case"]):
                self._assert_case(case)

    def test_the_oracle_states_a_case_for_every_terminal_state(self):
        stated = {
            case["expect"]["state"]
            for case in self.cases
            if "state" in case["expect"]
        }
        for state in (
            source_apply.STATE_READY,
            source_apply.STATE_REFUSED_MISSING_APPROVAL,
            source_apply.STATE_REFUSED_STALE,
            source_apply.STATE_APPLIED,
            source_apply.STATE_NOT_EFFECTIVE,
            source_apply.STATE_UNKNOWN,
            source_apply.STATE_RECOVERY_REQUIRED,
        ):
            self.assertIn(state, stated)
        # ``recovery_failed`` is declared but unreachable from this operation.
        self.assertNotIn(source_apply.STATE_RECOVERY_FAILED, stated)

    def test_a_plan_needs_no_receipt_and_writes_nothing(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)
        before = WORLD.target_sha256(world)
        record, reason = source_apply.plan_application(world.evidence, world.apply_root)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_READY)
        self.assertEqual(WORLD.target_sha256(world), before)
        self.assertEqual(sorted(os.listdir(world.custody)), [])
        self.assertFalse(os.path.exists(world.pre_image_path()))

    def test_an_apply_that_never_reaches_a_write_leaves_custody_empty(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)
        before = WORLD.target_sha256(world)
        record, reason = source_apply.apply_application(
            world.evidence, world.apply_root, world.custody, None, None
        )
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_MISSING_APPROVAL)
        self.assertEqual(WORLD.target_sha256(world), before)
        self.assertEqual(sorted(os.listdir(world.custody)), [])


class RootIdentityTests(unittest.TestCase):
    """The apply root's identity is derived, and it is the accepted one."""

    def _matrix_root(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        inner = os.path.join(root, "project")
        os.makedirs(inner)
        alias = os.path.join(root, "alias")
        os.symlink(inner, alias)
        plain = os.path.join(root, "a-file")
        with open(plain, "w", encoding="utf-8") as handle:
            handle.write("x\n")
        return root, inner, alias, plain

    def test_normalization_agrees_with_the_accepted_workspace_policy(self):
        root, inner, alias, plain = self._matrix_root()
        matrix = [
            inner,
            inner + os.sep,
            inner + os.sep + ".",
            alias,
            os.path.join(root, "missing"),
            plain,
            "",
            "   ",
            7,
            None,
        ]
        for value in matrix:
            with self.subTest(value=repr(value)):
                mine, reason = source_apply.normalize_root(value)
                try:
                    theirs = workspace.resolve_root(value)
                except contract.ContractError:
                    theirs = None
                if theirs is None:
                    self.assertIsNone(mine)
                    self.assertEqual(reason, source_apply.REASON_ROOT_UNUSABLE)
                else:
                    self.assertEqual(mine, theirs)
                    self.assertIsNone(reason)

    def test_a_symlinked_alias_resolves_to_the_same_workspace_identity(self):
        _root, inner, alias, _plain = self._matrix_root()
        canonical, _reason = source_apply.normalize_root(alias)
        self.assertEqual(canonical, os.path.realpath(inner))
        self.assertEqual(
            identity.workspace_id_for(canonical),
            identity.workspace_id_for(workspace.resolve_root(inner)),
        )

    def test_two_directories_with_the_same_relative_file_have_different_identities(self):
        root, inner, _alias, _plain = self._matrix_root()
        other = os.path.join(root, "other")
        os.makedirs(other)
        for directory in (inner, other):
            with open(
                os.path.join(directory, "greeting.py"), "w", encoding="utf-8"
            ) as handle:
                handle.write('"""Identical bytes in both trees."""\n')
        first, _ = source_apply.normalize_root(inner)
        second, _ = source_apply.normalize_root(other)
        self.assertNotEqual(
            identity.workspace_id_for(first), identity.workspace_id_for(second)
        )


class LeafTests(unittest.TestCase):
    """The module is a closed leaf, checked over its source, not asserted."""

    def _imports_of(self, path):
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        plain: set = set()
        local: set = set()
        package = os.path.relpath(path, SRC).split(os.sep)[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    plain.add(alias.name.split(".")[0])
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                base = package[: len(package) - (node.level - 1)]
            elif (node.module or "").startswith("hrca"):
                base = []
            else:
                plain.add((node.module or "").split(".")[0])
                continue
            if node.module:
                # ``from X import y`` depends on X, and on X.y when y is itself
                # a submodule — the walker skips any name with no file, so both
                # candidates are offered and only the real edges are followed.
                root = ".".join(base + node.module.split("."))
                local.add(root)
                for alias in node.names:
                    local.add(root + "." + alias.name)
            else:
                for alias in node.names:
                    local.add(".".join(base + [alias.name]))
        return plain, local

    def _file_for(self, dotted):
        parts = dotted.split(".")
        base = os.path.join(SRC, *parts)
        for option in (base + ".py", os.path.join(base, "__init__.py")):
            if os.path.exists(option):
                return option
        return None

    def test_the_module_imports_only_its_named_leaves(self):
        plain, local = self._imports_of(MODULE)
        self.assertEqual(
            plain, {"__future__", "json", "os", "re", "stat", "tempfile", "typing"}
        )
        # Submodule probes name no file and are not dependencies; the modules
        # this file actually reaches are the ones that resolve.
        reached = {name for name in local if self._file_for(name) is not None}
        self.assertEqual(
            reached,
            {
                "hrca.authoring.candidate",
                "hrca.authoring.candidate_edit",
                "hrca.core.identity",
            },
        )

    def test_the_transitive_local_closure_names_no_forbidden_capability(self):
        _plain, local = self._imports_of(MODULE)
        pending = list(local)
        seen = set()
        while pending:
            dotted = pending.pop()
            if dotted in seen:
                continue
            seen.add(dotted)
            path = self._file_for(dotted)
            if path is None:
                continue
            _plain_of, further = self._imports_of(path)
            pending.extend(further)
        leaves = {name.split(".")[-1] for name in seen}
        self.assertEqual(
            sorted(leaves & (_FORBIDDEN_LEAVES - _INHERITED_FROM_THE_P54_CONTRACT)),
            [],
            "the closure reaches a capability this module must not have",
        )
        self.assertNotIn("hrca.authoring.validation", seen)
        self.assertNotIn("hrca.execution.container_runner", seen)

    def test_the_inherited_leaves_are_never_named_in_this_module(self):
        """The one documented exception, asserted rather than assumed.

        The inherited edge runs through the accepted P5.4 contract and not
        through anything this module says. So this module must name none of
        those capabilities anywhere in its own source — no import, no
        attribute, no bare name.
        """
        with open(MODULE, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        named |= {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertEqual(sorted(named & _INHERITED_FROM_THE_P54_CONTRACT), [])

    def test_the_source_names_no_process_launch(self):
        with open(MODULE, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "system",
                "popen",
                "spawn",
                "spawnv",
                "execv",
                "execve",
                "fork",
                "run",
                "check_output",
                "call",
            }:
                raise AssertionError("the source reaches %r" % (node.attr,))
            if isinstance(node, ast.Name) and node.id in {"eval", "exec", "compile"}:
                raise AssertionError("the source reaches %r" % (node.id,))


class RecordShapeTests(unittest.TestCase):
    """The record's own vocabulary, independent of any world."""

    def test_an_unknown_state_and_mode_are_refused_by_the_validator(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)
        record, _reason = source_apply.plan_application(world.evidence, world.apply_root)
        self.assertIsNone(source_apply.validate_apply_record(record))

        altered = dict(record)
        altered["state"] = "not_a_state"
        self.assertIsNotNone(source_apply.validate_apply_record(altered))

        altered = dict(record)
        altered["mode"] = "not_a_mode"
        self.assertIsNotNone(source_apply.validate_apply_record(altered))

        altered = dict(record)
        altered["approval_inferred"] = True
        self.assertIsNotNone(source_apply.validate_apply_record(altered))

        altered = dict(record)
        surface = dict(record["mutation_surface"])
        surface["network"] = True
        altered["mutation_surface"] = surface
        self.assertIsNotNone(source_apply.validate_apply_record(altered))

    def test_the_apply_identity_distinguishes_an_approved_from_an_unapproved_run(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)
        digest, raw = source_apply.receipt_digests(world.receipt, world.receipt_bytes)
        self.assertTrue(digest)
        self.assertTrue(raw)
        self.assertNotEqual(digest, raw)
        self.assertNotEqual(
            source_apply.apply_id_for(world.evidence, None),
            source_apply.apply_id_for(world.evidence, digest),
        )
        again = source_apply.receipt_digests(world.receipt, world.receipt_bytes)
        self.assertEqual((digest, raw), again)

    def test_a_mapping_only_receipt_records_no_raw_digest(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)
        canonical, raw = source_apply.receipt_digests(world.receipt)
        self.assertTrue(canonical)
        self.assertIsNone(raw)


if __name__ == "__main__":
    unittest.main()
