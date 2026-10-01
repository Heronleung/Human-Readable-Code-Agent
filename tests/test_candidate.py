"""The isolated content-addressed candidate (P5.4).

Three things are held here.

The **oracle**: ``candidate_fixtures/manifest.json`` states, by hand and
independently of the materializer and the diff renderer, what the contract must
answer for a supported case and for every named negative one. Its two file
hashes and byte counts were taken from ``sha256sum`` and ``wc -c`` over the
frozen fixture, not from this package.

The **independent reconstruction**: rather than trusting the manifest it
produced, the build is checked by applying the recorded replacement to the
frozen baseline with nothing but the standard library, recomputing every hash
and the candidate identity with :mod:`hashlib`, and requiring the result to be
byte-identical to what was written.

The **boundaries**: the accepted repository and its Git state are snapshotted
before and after every outcome — success, refusal, injected failure and the
read-only cancellation path — and required to be unchanged, down to the
untracked bundle's metadata. No candidate-driven test is executed, no runner is
invoked, and no protocol action is added.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hrca import twin
from hrca.authoring import candidate, candidate_diff, candidate_edit, impact_proposal, intent_delta
from hrca.cli import candidate_cli
from hrca.core import contract
from hrca.source import scanner

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
FIXTURE_REPO = os.path.join(REPO, "candidate_fixtures", "repo")
MANIFEST = os.path.join(REPO, "candidate_fixtures", "manifest.json")
BUNDLE = os.path.join(REPO, "hrca-df8baf7.bundle")

# The fixture repository has one canonical identity. A Twin workspace is derived
# from a canonical root, so a copy of the tree in a temporary directory would
# otherwise be a different workspace on every run and the candidate identity
# with it. Pinning the workspace here pins an *input*; it does not paper over a
# nondeterministic output.
FIXED_WORKSPACE = twin.workspace_id_for("hrca-p54-fixture")

PREDECESSOR_PATH = "pkg/service.py"

_WRITE_SCOPE = {
    "candidate_root_only": True,
    "accepted_repository": False,
    "git_metadata": False,
}

# Modules that would grant an authority the P5.4 contract does not have, plus
# the project's own write-side and approval seams. ``candidate.py`` is allowed
# ``os``, ``tempfile``, ``stat`` and ``json`` because it must read a repository
# and create one isolated directory; it is allowed nothing that could reach a
# provider, a runner, Git, a credential, the network or another store.
_FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
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
        "hrca.contract",
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
        "hrca.app_package",
        "hrca.document",
        "hrca.version_store",
        "hrca.twin_store",
        "hrca.library",
        "hrca.library_store",
        "hrca.memory",
        "hrca.memory_store",
        "hrca.memory_package",
        "hrca.hook_capture",
    }
)


# -- fixture plumbing ------------------------------------------------------


def _sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _frozen_text(path: str) -> str:
    with open(os.path.join(FIXTURE_REPO, *path.split("/")), encoding="utf-8") as fh:
        return fh.read()


def _manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _copy_repo() -> str:
    work = tempfile.mkdtemp(prefix="p54-work-")
    root = os.path.join(work, "repo")
    shutil.copytree(FIXTURE_REPO, root)
    return root


def _fingerprints(root: str, doc: dict) -> dict:
    out = {}
    for rec in doc["files"]:
        path = rec["path"]
        if not path.endswith((".py", ".pyi")):
            continue
        try:
            with open(os.path.join(root, *path.split("/")), "rb") as fh:
                out[path] = twin.fingerprint_bytes(fh.read())
        except OSError:  # pragma: no cover - the fixture is readable
            out[path] = None
    return out


def _evidence(root: str) -> dict:
    doc = scanner.scan_directory(root)
    return {
        "scanner": doc,
        "twin": twin.build_store(doc, _fingerprints(root, doc), FIXED_WORKSPACE, 1, "T"),
    }


def _baseline_of(evidence: dict) -> dict:
    doc, store = evidence["scanner"], evidence["twin"]
    return {
        "workspace_id": store["workspace_revision"]["workspace_id"],
        "scan_generation": store["workspace_revision"]["scan_generation"],
        "baseline_fingerprint": store["workspace_revision"]["baseline_fingerprint"],
        "scanner_schema_version": doc["schema_version"],
        "grammar": dict(doc["grammar"]),
    }


def _mutate_repo(root: str, ops: dict) -> None:
    for op, spec in sorted(ops.items()):
        if op == "remove":
            os.remove(os.path.join(root, *spec.split("/")))
            continue
        if op not in ("write", "symlink", "hardlink", "fifo"):  # pragma: no cover
            raise AssertionError("unknown repo mutation %r" % op)
        target = os.path.join(root, *spec["path"].split("/"))
        if op == "write":
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(spec["text"])
        elif op == "symlink":
            os.remove(target)
            os.symlink(spec["to"], target)
        elif op == "hardlink":
            keep = target + ".keep"
            os.rename(target, keep)
            os.link(keep, target)
        else:
            os.remove(target)
            os.mkfifo(target)


def _mutate_evidence(evidence: dict, ops: dict, case: str) -> dict:
    evidence = copy.deepcopy(evidence)
    revision = evidence["twin"]["workspace_revision"]
    if "set_grammar_version" in ops:
        assert evidence["scanner"]["grammar"]["version"] != ops["set_grammar_version"], case
        evidence["scanner"]["grammar"]["version"] = ops["set_grammar_version"]
    if "set_scan_generation" in ops:
        assert revision["scan_generation"] != ops["set_scan_generation"], case
        revision["scan_generation"] = ops["set_scan_generation"]
    if "set_baseline_fingerprint" in ops:
        assert revision["baseline_fingerprint"] != ops["set_baseline_fingerprint"], case
        revision["baseline_fingerprint"] = ops["set_baseline_fingerprint"]
    if "set_workspace_id" in ops:
        assert revision["workspace_id"] != ops["set_workspace_id"], case
        revision["workspace_id"] = ops["set_workspace_id"]
    if "zero_artifact_fingerprint" in ops:
        wanted = ops["zero_artifact_fingerprint"]
        found = False
        for artifact in evidence["twin"]["artifacts"]:
            if artifact["id"] == wanted:
                assert artifact.get("fingerprint") != "0" * 64, case
                artifact["fingerprint"] = "0" * 64
                found = True
        assert found, case
    return evidence


def _resolve_text(text: str, corpus: dict) -> str:
    """Resolve the oracle's content tokens against the frozen fixture."""
    if text == "@@PREDECESSOR@@":
        return _frozen_text(PREDECESSOR_PATH)
    if text == "RESULT_TEXT":
        return corpus["result"]["text"]
    return text


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


class _Case:
    """One manifest case, resolved against the frozen fixture."""

    def __init__(self, spec: dict, corpus: dict):
        self.spec = spec
        self.case = spec["case"]
        self.corpus = corpus
        self.root = _copy_repo()
        if isinstance(spec.get("repo"), dict):
            _mutate_repo(self.root, spec["repo"]["mutate"])

        evidence_root = self.root if spec.get("evidence") == "from_repo" else _copy_repo()
        self.pristine = _evidence(evidence_root)
        # The intent is authored against the evidence as it stood *before* any
        # mutation, so a case that changes the evidence is a case where the
        # accepted evidence has since moved. The proposal the caller holds is
        # derived from that pristine evidence; the mutated evidence is what the
        # build is then handed.
        baseline = _baseline_of(self.pristine)
        self.evidence = (
            _mutate_evidence(self.pristine, spec["evidence"]["mutate"], self.case)
            if isinstance(spec.get("evidence"), dict)
            else self.pristine
        )
        intent = dict(corpus["intent_base"])
        intent.update({k: v for k, v in spec.get("intent", {}).items()})
        intent["baseline"] = baseline
        self.delta, error = intent_delta.build_intent_delta(intent)
        assert error is None, "%s: %s" % (self.case, error)
        self.proposal, error = impact_proposal.build_impact_proposal(
            self.delta, self.pristine
        )
        assert error is None, "%s: %s" % (self.case, error)

        edit_baseline = dict(baseline)
        edit_baseline.update(spec.get("edit", {}).get("baseline_override", {}))
        operations = spec.get("edit", {}).get("operations", corpus["edit_base"]["operations"])
        raw_edit = {
            "intent_delta_id": self.delta["intent_delta_id"],
            "proposal_id": self.proposal["proposal_id"],
            "binding_fingerprint": self.proposal["binding"]["binding_fingerprint"],
            "baseline": edit_baseline,
            "operations": [self._resolve(record) for record in operations],
        }
        raw_edit.update(spec.get("edit", {}).get("override", {}))
        self.edit, error = candidate_edit.build_edit(raw_edit)
        assert error is None, "%s: %s" % (self.case, error)

        self.base = tempfile.mkdtemp(prefix="p54-base-")
        if spec.get("output_base") == "repository":
            self.base = self.root

    def _resolve(self, record: dict) -> dict:
        """Resolve the oracle's sentinels against the tree this build will read.

        ``@@CURRENT_SHA@@`` is for a case whose predecessor is *supposed* to be
        the one on disk: the pin has to be the real hash, because the case is
        about a different rule and a deliberately wrong pin would refuse for the
        wrong reason.
        """
        record = dict(record)
        record["text"] = _resolve_text(record["text"], self.corpus)
        if record.get("expected_sha256") == "@@CURRENT_SHA@@":
            record["expected_sha256"] = _sha256_file(
                os.path.join(self.root, *record["path"].split("/"))
            )
        return record

    def run(self):
        spec = self.spec
        materialize = spec.get("materialize", True)
        patcher = mock.patch.object
        stack = contextlib.ExitStack()
        for name, value in sorted(spec.get("patch", {}).items()):
            stack.enter_context(patcher(candidate, name, value))
        if spec.get("inject_failure") == "rename":
            stack.enter_context(patcher(os, "rename", side_effect=OSError("injected")))
        with stack:
            result = candidate.build_candidate(
                self.edit,
                self.delta,
                self.proposal,
                self.evidence,
                self.root,
                self.base if materialize else None,
                materialize=materialize,
            )
            if spec.get("output_base") == "reuse":
                assert result == (result[0], None)
                result = candidate.build_candidate(
                    self.edit,
                    self.delta,
                    self.proposal,
                    self.evidence,
                    self.root,
                    self.base,
                    materialize=True,
                )
        return result


# -- the oracle ------------------------------------------------------------


class ManifestOracleTests(unittest.TestCase):
    """Every case in the hand-authored manifest, run exactly as written."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.corpus = _manifest()
        cls.cases = {case["case"]: case for case in cls.corpus["cases"]}
        assert len(cls.cases) == len(cls.corpus["cases"]), "duplicate case name"

    def test_the_manifest_covers_every_required_case_kind(self):
        required = {
            "supported_replace",
            "no_change",
            "unsupported_content",
            "oversized_predecessor",
            "oversized_diff",
            "out_of_scope_target",
            "prose_cannot_widen_scope",
            "twin_identity_mismatch",
            "stale_predecessor",
            "predecessor_absent",
            "predecessor_symlink",
            "predecessor_hardlink",
            "predecessor_special_file",
            "output_inside_repository",
            "output_root_reused",
            "injected_staging_failure",
            "edit_not_bound_to_intent",
            "edit_not_bound_to_proposal",
            "edit_binding_fingerprint_mismatch",
            "edit_baseline_mismatch",
            "changed_baseline_evidence",
            "changed_scan_generation",
            "changed_grammar_context",
            "changed_workspace",
            "review_only_creates_nothing",
        }
        self.assertTrue(required <= set(self.cases))

    def test_the_pinned_hashes_match_the_frozen_fixture(self):
        # The oracle's own numbers, re-derived from the fixture with the standard
        # library alone rather than trusted.
        predecessor = self.corpus["predecessor"]
        path = os.path.join(FIXTURE_REPO, *PREDECESSOR_PATH.split("/"))
        self.assertEqual(predecessor["sha256"], _sha256_file(path))
        self.assertEqual(predecessor["bytes"], os.path.getsize(path))

        other = self.corpus["out_of_scope"]
        other_path = os.path.join(FIXTURE_REPO, *self.corpus["out_of_scope_path"].split("/"))
        self.assertEqual(other["sha256"], _sha256_file(other_path))
        self.assertEqual(other["bytes"], os.path.getsize(other_path))

        result = self.corpus["result"]
        encoded = result["text"].encode("utf-8")
        self.assertEqual(result["sha256"], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(result["bytes"], len(encoded))

    def test_the_pinned_diff_is_the_diff_of_the_pinned_bytes(self):
        # The oracle's diff lines, re-derived with difflib alone.
        import difflib

        lines = list(
            difflib.unified_diff(
                _frozen_text(PREDECESSOR_PATH).splitlines(),
                self.corpus["result"]["text"].splitlines(),
                fromfile="a/" + PREDECESSOR_PATH,
                tofile="b/" + PREDECESSOR_PATH,
                n=3,
                lineterm="",
            )
        )
        self.assertEqual(self.corpus["diff_lines"], lines)

    def test_every_case(self):
        for spec in self.corpus["cases"]:
            with self.subTest(case=spec["case"]):
                self._run_case(spec)

    def _run_case(self, spec):
        case = spec["case"]
        expect = spec["expect"]
        built = _Case(spec, self.corpus)
        envelope, error = built.run()

        if expect["result"] == "refused":
            self.assertIsNone(envelope, case)
            self.assertEqual(expect["error"], error, case)
            return

        self.assertIsNone(error, case)
        self.assertIsNotNone(envelope, case)
        self.assertEqual(expect["state"], envelope["state"], case)
        self.assertEqual(expect["reason_code"], envelope["reason_code"], case)

        # A candidate is never an outcome, whatever the state.
        self.assertIs(True, envelope["advisory"], case)
        self.assertIs(False, envelope["executable"], case)
        self.assertIs(False, envelope["applied"], case)
        self.assertIs(False, envelope["validated"], case)
        self.assertIs(False, envelope["approved"], case)
        self.assertIs(False, envelope["adopted"], case)
        self.assertEqual(_WRITE_SCOPE, envelope["write_scope"], case)
        self.assertTrue(
            all(value is False for value in envelope["mutation_surface"].values()), case
        )

        root_name = envelope["candidate_root_name"]
        if expect.get("root_created"):
            self.assertTrue(root_name.startswith(candidate.ROOT_PREFIX), case)
            self.assertTrue(
                os.path.isdir(os.path.join(built.base, root_name)), case
            )
        else:
            self.assertIsNone(root_name, case)
            # Nothing was created anywhere in the base. The reuse case is exempt
            # because its whole point is that a first root already exists.
            if built.base != built.root and "output_base" not in spec:
                self.assertEqual([], sorted(os.listdir(built.base)), case)

        if "candidate_id" in expect:
            self.assertEqual(expect["candidate_id"], envelope["candidate_id"], case)

        if "operations" in expect:
            for expected, record in zip(expect["operations"], envelope["operations"]):
                for key, value in sorted(expected.items()):
                    if value == "PINNED_DIFF":
                        value = self.corpus["diff_lines"]
                    self.assertEqual(value, record[key], "%s: %s" % (case, key))

        if "risks" in expect:
            self.assertEqual(expect["risks"], [r["risk_id"] for r in envelope["risks"]], case)
        if "unresolved_questions" in expect:
            self.assertEqual(
                expect["unresolved_questions"],
                [q["question_id"] for q in envelope["unresolved_questions"]],
                case,
            )

        if "staged_files" in expect:
            root = os.path.join(built.base, root_name) if root_name else None
            staged = []
            if root:
                content = os.path.join(root, candidate.FILES_DIR)
                for dirpath, _dirnames, filenames in os.walk(content):
                    for name in filenames:
                        rel = os.path.relpath(os.path.join(dirpath, name), content)
                        staged.append(rel.replace(os.sep, "/"))
            self.assertEqual(sorted(expect["staged_files"]), sorted(staged), case)


# -- independent reconstruction -------------------------------------------


class IndependentReconstructionTests(unittest.TestCase):
    """Rebuild the candidate with the standard library alone and compare bytes."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = _manifest()
        cls.spec = cls.corpus["cases"][0]
        assert cls.spec["case"] == "supported_replace"
        cls.built = _Case(cls.spec, cls.corpus)
        cls.envelope, cls.error = cls.built.run()
        assert cls.error is None, cls.error
        cls.root = os.path.join(cls.built.base, cls.envelope["candidate_root_name"])

    def _staged_bytes(self, path: str) -> bytes:
        with open(
            os.path.join(self.root, candidate.FILES_DIR, *path.split("/")), "rb"
        ) as handle:
            return handle.read()

    def _manifest_bytes(self) -> bytes:
        with open(os.path.join(self.root, candidate.MANIFEST_NAME), "rb") as handle:
            return handle.read()

    def test_applying_the_recorded_replacement_reconstructs_the_candidate_bytes(self):
        corpus = self.corpus
        baseline = os.path.join(FIXTURE_REPO, *PREDECESSOR_PATH.split("/"))
        with open(baseline, "rb") as handle:
            before = handle.read()

        # The oracle's own arithmetic, with hashlib alone.
        self.assertEqual(corpus["predecessor"]["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(corpus["predecessor"]["bytes"], len(before))

        replacement = corpus["result"]["text"].encode("utf-8")
        self.assertEqual(corpus["result"]["sha256"], hashlib.sha256(replacement).hexdigest())

        staged = self._staged_bytes(PREDECESSOR_PATH)
        self.assertEqual(replacement, staged)
        self.assertEqual(hashlib.sha256(staged).hexdigest(), corpus["result"]["sha256"])
        self.assertEqual(len(staged), corpus["result"]["bytes"])

        # And the build recorded the same identities it was handed.
        record = self.envelope["operations"][0]
        self.assertEqual(corpus["predecessor"]["sha256"], record["before_sha256"])
        self.assertEqual(corpus["predecessor"]["bytes"], record["before_bytes"])
        self.assertEqual(corpus["result"]["sha256"], record["after_sha256"])
        self.assertEqual(corpus["result"]["bytes"], record["after_bytes"])
        self.assertEqual(corpus["diff_lines"], record["diff"])

    def test_the_on_disk_manifest_is_byte_identical_to_the_reconstruction(self):
        corpus = self.corpus
        with open(os.path.join(FIXTURE_REPO, *PREDECESSOR_PATH.split("/")), "rb") as fh:
            before = fh.read()
        after = corpus["result"]["text"].encode("utf-8")

        assembled = {
            "schema_version": candidate.CANDIDATE_SCHEMA_VERSION,
            "generator": candidate.CANDIDATE_GENERATOR,
            "candidate_id": "",
            "state": candidate.STATE_CANDIDATE_READY,
            "edit_id": self.built.edit["edit_id"],
            "intent_delta_id": self.built.delta["intent_delta_id"],
            "proposal_id": self.built.proposal["proposal_id"],
            "binding_fingerprint": self.built.proposal["binding"]["binding_fingerprint"],
            "baseline": dict(self.built.edit["baseline"]),
            "operations": [
                {
                    "op": "replace_file",
                    "path": PREDECESSOR_PATH,
                    "before_sha256": hashlib.sha256(before).hexdigest(),
                    "before_bytes": len(before),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                    "after_bytes": len(after),
                    "before_final_newline": before.decode("utf-8").endswith("\n"),
                    "after_final_newline": after.decode("utf-8").endswith("\n"),
                    "diff": corpus["diff_lines"],
                }
            ],
            "files": [
                {
                    "path": PREDECESSOR_PATH,
                    "sha256": hashlib.sha256(after).hexdigest(),
                    "bytes": len(after),
                }
            ],
            "write_scope": dict(_WRITE_SCOPE),
            "limitations": list(self.envelope["limitations"]),
        }
        canon = json.dumps(
            {k: v for k, v in assembled.items() if k != "candidate_id"},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assembled["candidate_id"] = "candidate:" + hashlib.sha256(canon).hexdigest()

        self.assertEqual(self.envelope["candidate_id"], assembled["candidate_id"])
        self.assertEqual(
            json.dumps(assembled, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            self._manifest_bytes().decode("utf-8"),
        )
        self.assertIsNone(candidate.validate_candidate_manifest(assembled))

    def test_the_manifest_file_is_the_canonical_serialization(self):
        # The bytes on disk are exactly the canonical rendering of the manifest,
        # which is what makes the reconstruction above a byte comparison.
        manifest = json.loads(self._manifest_bytes())
        reencoded = json.dumps(
            manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(reencoded, self._manifest_bytes())


# -- determinism and identity ---------------------------------------------


class DeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = _manifest()
        cls.spec = cls.corpus["cases"][0]

    def _build(self):
        built = _Case(self.spec, self.corpus)
        envelope, error = built.run()
        assert error is None, error
        return built, envelope

    def test_identical_inputs_produce_one_identity_and_one_envelope(self):
        first_build, first = self._build()
        second_build, second = self._build()
        self.assertEqual(first["candidate_id"], second["candidate_id"])
        self.assertEqual(candidate.dumps(first), candidate.dumps(second))
        self.assertEqual(first["candidate_root_name"], second["candidate_root_name"])

        with open(os.path.join(first_build.base, first["candidate_root_name"],
                              candidate.MANIFEST_NAME), "rb") as fh:
            first_manifest = fh.read()
        with open(os.path.join(second_build.base, second["candidate_root_name"],
                              candidate.MANIFEST_NAME), "rb") as fh:
            second_manifest = fh.read()
        self.assertEqual(first_manifest, second_manifest)

    def test_a_read_only_build_agrees_with_the_materialized_identity(self):
        _built, materialized = self._build()
        spec = dict(self.spec, materialize=False)
        read_only, error = _Case(spec, self.corpus).run()
        self.assertIsNone(error)
        self.assertEqual(materialized["candidate_id"], read_only["candidate_id"])
        self.assertIsNone(read_only["candidate_root_name"])

    def test_a_changed_replacement_changes_the_identity(self):
        _built, baseline = self._build()
        spec = copy.deepcopy(self.spec)
        spec["edit"] = {
            "operations": [
                {
                    "op": "replace_file",
                    "path": PREDECESSOR_PATH,
                    "expected_sha256": self.corpus["predecessor"]["sha256"],
                    "text": 'VERSION = "3"\n',
                }
            ]
        }
        changed, error = _Case(spec, self.corpus).run()
        self.assertIsNone(error)
        self.assertNotEqual(baseline["candidate_id"], changed["candidate_id"])
        self.assertNotEqual(
            baseline["binding"]["edit_id"], changed["binding"]["edit_id"]
        )

    def test_a_changed_scope_yields_a_distinctly_bound_candidate(self):
        _built, baseline = self._build()
        spec = copy.deepcopy(self.spec)
        spec["intent"] = {
            "scope": {"entities": ["pkg.service.Service"], "artifacts": []},
            "origin": {
                "kind": "developer_authored",
                "evidence": [
                    {"kind": "twin_artifact", "id": "artifact:class:pkg.service.Service"}
                ],
            },
        }
        changed, error = _Case(spec, self.corpus).run()
        self.assertIsNone(error)
        self.assertEqual(candidate.STATE_CANDIDATE_READY, changed["state"])
        self.assertNotEqual(baseline["candidate_id"], changed["candidate_id"])
        self.assertNotEqual(
            baseline["binding"]["proposal_id"], changed["binding"]["proposal_id"]
        )
        # The binding fingerprint is over the *evidence* — workspace, revision,
        # schema and grammar — so two intents bound to one evidence share it.
        # What the scope changes is which identity the candidate is bound to,
        # and that shows up in the candidate identity above.
        self.assertEqual(
            baseline["binding"]["binding_fingerprint"],
            changed["binding"]["binding_fingerprint"],
        )

    def test_two_output_bases_get_the_same_candidate_under_the_same_name(self):
        first_build, first = self._build()
        second_build, second = self._build()
        self.assertNotEqual(first_build.base, second_build.base)
        self.assertEqual(first["candidate_root_name"], second["candidate_root_name"])
        self.assertTrue(os.path.isdir(os.path.join(first_build.base, first["candidate_root_name"])))
        self.assertTrue(os.path.isdir(os.path.join(second_build.base, second["candidate_root_name"])))

    def test_the_same_candidate_cannot_be_written_twice_into_one_base(self):
        built = _Case(self.spec, self.corpus)
        first, error = built.run()
        self.assertIsNone(error)
        second, error = candidate.build_candidate(
            built.edit, built.delta, built.proposal, built.evidence, built.root,
            built.base, materialize=True,
        )
        self.assertIsNone(error)
        self.assertEqual(candidate.STATE_REFUSED, second["state"])
        self.assertEqual(candidate.REASON_OUTPUT_ROOT_TAKEN, second["reason_code"])
        # The first root is still there and untouched.
        self.assertTrue(os.path.isdir(os.path.join(built.base, first["candidate_root_name"])))


# -- containment and cleanup ----------------------------------------------


class ContainmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = _manifest()
        cls.spec = cls.corpus["cases"][0]

    def test_the_root_is_a_fresh_directory_directly_under_the_base(self):
        built = _Case(self.spec, self.corpus)
        before = sorted(os.listdir(built.base))
        envelope, error = built.run()
        self.assertIsNone(error)
        root = os.path.join(built.base, envelope["candidate_root_name"])
        self.assertEqual([], before)
        self.assertEqual(
            [envelope["candidate_root_name"]], sorted(os.listdir(built.base))
        )
        self.assertTrue(os.path.isdir(root))
        self.assertEqual(0o700, os.stat(root).st_mode & 0o777)

    def test_the_candidate_holds_the_manifest_and_exactly_the_changed_files(self):
        built = _Case(self.spec, self.corpus)
        envelope, _error = built.run()
        root = os.path.join(built.base, envelope["candidate_root_name"])
        self.assertEqual(
            sorted([candidate.MANIFEST_NAME, candidate.FILES_DIR]),
            sorted(os.listdir(root)),
        )
        self.assertEqual(["pkg"], sorted(os.listdir(os.path.join(root, candidate.FILES_DIR))))
        self.assertEqual(
            ["service.py"],
            sorted(os.listdir(os.path.join(root, candidate.FILES_DIR, "pkg"))),
        )

    def test_nothing_is_created_in_the_repository(self):
        built = _Case(self.spec, self.corpus)
        before = sorted(os.listdir(built.root))
        _envelope, _error = built.run()
        self.assertEqual(before, sorted(os.listdir(built.root)))
        self.assertFalse(os.path.exists(os.path.join(built.root, ".git")))

    def test_failure_cleanup_removes_only_this_build_s_own_tree(self):
        # A neighbour already in the base, and an unrelated root, must survive.
        built = _Case(dict(self.spec, inject_failure="rename"), self.corpus)
        neighbour = os.path.join(built.base, "candidate-someone-elses")
        other = os.path.join(built.base, "unrelated")
        os.mkdir(neighbour)
        os.mkdir(other)
        with open(os.path.join(neighbour, "keep.txt"), "w", encoding="utf-8") as fh:
            fh.write("keep")
        before = sorted(os.listdir(built.base))
        envelope, error = built.run()
        self.assertIsNone(error)
        self.assertEqual(candidate.STATE_REFUSED, envelope["state"])
        self.assertEqual(before, sorted(os.listdir(built.base)))
        with open(os.path.join(neighbour, "keep.txt"), encoding="utf-8") as fh:
            self.assertEqual("keep", fh.read())

    def test_the_cleanup_guard_refuses_to_remove_anything_it_did_not_make(self):
        base = tempfile.mkdtemp(prefix="p54-base-")
        outsider = tempfile.mkdtemp(prefix="p54-elsewhere-")
        target = os.path.join(outsider, "candidate-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        os.mkdir(target)
        with open(os.path.join(target, "keep.txt"), "w", encoding="utf-8") as fh:
            fh.write("keep")
        # Wrong parent.
        candidate._discard_tree(base, target)
        self.assertTrue(os.path.isdir(target))
        # Right parent, wrong name.
        wrong_name = os.path.join(base, "not-ours")
        os.mkdir(wrong_name)
        candidate._discard_tree(base, wrong_name)
        self.assertTrue(os.path.isdir(wrong_name))
        # Right parent, right prefix.
        ours = os.path.join(base, candidate.ROOT_PREFIX + "b" * 32)
        os.mkdir(ours)
        candidate._discard_tree(base, ours)
        self.assertFalse(os.path.exists(ours))


# -- hostile input, directly ------------------------------------------------


class HostilePathTests(unittest.TestCase):
    """The filesystem guards, exercised beyond what a request can express."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = _manifest()
        cls.built = _Case(cls.corpus["cases"][0], cls.corpus)

    def test_a_symlinked_directory_component_is_refused(self):
        root = _copy_repo()
        os.mkdir(os.path.join(root, "link"))
        os.symlink(os.path.join(root, "pkg"), os.path.join(root, "link", "pkg"))
        _record, reason = candidate._read_predecessor(
            os.path.realpath(root), "link/pkg/service.py"
        )
        self.assertEqual(candidate.REASON_PREDECESSOR_SYMLINK, reason)

    def test_a_target_outside_the_root_is_refused(self):
        root = _copy_repo()
        outside = tempfile.mkdtemp(prefix="p54-outside-")
        real = os.path.realpath(root)
        _record, reason = candidate._read_predecessor(real, "../outside/service.py")
        self.assertIn(
            reason,
            {
                candidate.REASON_PREDECESSOR_ESCAPES,
                candidate.REASON_PREDECESSOR_ABSENT,
                candidate.REASON_PREDECESSOR_NOT_REGULAR,
            },
        )
        self.assertTrue(os.path.isdir(outside))

    def test_the_predecessor_guard_rejects_unreviewable_content(self):
        # Reached directly: with a bound proposal every scoped file parses, so
        # this guard is defence in depth rather than a reachable end state.
        root = _copy_repo()
        target = os.path.join(root, "pkg", "service.py")
        with open(target, "wb") as fh:
            fh.write(b"VERSION = '1'\x00\n")
        _record, reason = candidate._read_predecessor(os.path.realpath(root), PREDECESSOR_PATH)
        self.assertEqual(candidate.REASON_UNSUPPORTED_CONTENT, reason)

        with open(target, "wb") as fh:
            fh.write(b"VERSION = '\xff'\n")
        _record, reason = candidate._read_predecessor(os.path.realpath(root), PREDECESSOR_PATH)
        self.assertEqual(candidate.REASON_UNSUPPORTED_CONTENT, reason)

    def test_the_reviewability_predicate(self):
        self.assertTrue(candidate.reviewable_text('VERSION = "1"\n'))
        self.assertTrue(candidate.reviewable_text(""))
        self.assertFalse(candidate.reviewable_text("a\x00b"))
        self.assertFalse(candidate.reviewable_text("﻿VERSION = 1\n"))

    def test_an_unusable_output_base_is_refused(self):
        real = os.path.realpath(_copy_repo())
        for value in (None, "", 7, os.path.join(real, "missing", "deeper")):
            with self.subTest(value=value):
                base, reason = candidate._validate_output_base(real, value)
                self.assertIsNone(base)
                self.assertEqual(candidate.REASON_OUTPUT_UNUSABLE, reason)

    def test_git_metadata_is_not_a_usable_output_base(self):
        real = os.path.realpath(_copy_repo())
        git_dir = tempfile.mkdtemp(prefix="p54-git-")
        os.makedirs(os.path.join(git_dir, ".git", "objects"), exist_ok=True)
        base, reason = candidate._validate_output_base(
            real, os.path.join(git_dir, ".git", "objects")
        )
        self.assertIsNone(base)
        self.assertEqual(candidate.REASON_OUTPUT_INSIDE_REPOSITORY, reason)

    def test_an_authorized_path_requires_scope_and_evidence(self):
        proposal = self.built.proposal
        authorized = candidate.authorized_paths(proposal)
        self.assertTrue(authorized[PREDECESSOR_PATH]["in_scope"])
        self.assertTrue(authorized[PREDECESSOR_PATH]["in_evidence"])

        # The two conditions are separate: a path in scope whose evidence binding
        # is missing is not authorized, and neither is the reverse.
        scope_only = copy.deepcopy(proposal)
        scope_only["evidence_bindings"] = [
            binding
            for binding in scope_only["evidence_bindings"]
            if binding["kind"] != "scanner_file"
        ]
        scope_only["affected_facts"] = []
        self.assertTrue(candidate.authorized_paths(scope_only)[PREDECESSOR_PATH]["in_scope"])
        self.assertFalse(candidate.authorized_paths(scope_only)[PREDECESSOR_PATH]["in_evidence"])

        evidence_only = copy.deepcopy(proposal)
        evidence_only["target_scope"]["targets"] = []
        self.assertEqual({}, candidate.authorized_paths(evidence_only))

    def test_a_manifest_with_a_stale_identity_is_refused(self):
        built = _Case(self.corpus["cases"][0], self.corpus)
        envelope, _error = built.run()
        root = os.path.join(built.base, envelope["candidate_root_name"])
        with open(os.path.join(root, candidate.MANIFEST_NAME), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertIsNone(candidate.validate_candidate_manifest(manifest))
        manifest["operations"][0]["diff"] = ["-tampered"]
        self.assertEqual(
            "candidate_id does not match the candidate content",
            candidate.validate_candidate_manifest(manifest),
        )


# -- the accepted repository and Git state ---------------------------------


def _git(root: str, *args: str):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0"),
    )


def _git_state(root: str) -> dict:
    hooks = os.path.join(root, ".git", "hooks")
    return {
        "head": _git(root, "rev-parse", "HEAD").stdout,
        "refs": _git(root, "show-ref").stdout,
        "index": _git(root, "ls-files", "-s").stdout,
        "status": _git(root, "--no-optional-locks", "status", "--porcelain").stdout,
        "stash": _git(root, "stash", "list").stdout,
        "config": _git(root, "config", "--list", "--local").stdout,
        "hooks": sorted(os.listdir(hooks)) if os.path.isdir(hooks) else [],
    }


def _tracked_bytes(root: str) -> dict:
    listing = _git(root, "ls-files", "-z").stdout
    out = {}
    for rel in listing.split("\0"):
        if not rel:
            continue
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        out[rel] = _sha256_file(full)
    return out


def _fixture_bytes() -> dict:
    out = {}
    for dirpath, _dirnames, filenames in os.walk(FIXTURE_REPO):
        for name in filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, FIXTURE_REPO)] = _sha256_file(full)
    return out


class RepositoryNonMutationTests(unittest.TestCase):
    """Success, refusal, injected failure and cancellation leave nothing moved."""

    @classmethod
    def setUpClass(cls):
        if _git(REPO, "rev-parse", "--git-dir").returncode != 0:  # pragma: no cover
            raise unittest.SkipTest("the project is not a Git working tree")
        cls.corpus = _manifest()
        cls.spec = cls.corpus["cases"][0]
        cls.git_before = _git_state(REPO)
        cls.tracked_before = _tracked_bytes(REPO)
        cls.fixture_before = _fixture_bytes()
        bundle = os.path.join(REPO, "hrca-df8baf7.bundle")
        cls.bundle_before = (
            os.stat(bundle).st_size,
            os.stat(bundle).st_mtime_ns,
        ) if os.path.exists(bundle) else None

    def test_every_outcome_leaves_the_repository_and_its_git_state_unchanged(self):
        outcomes = []

        # Success.
        built = _Case(self.spec, self.corpus)
        outcomes.append(built.run()[0]["state"])

        # Refusal: a stale predecessor.
        stale = copy.deepcopy(self.spec)
        stale["repo"] = {"mutate": {"write": {"path": PREDECESSOR_PATH, "text": "VERSION = '9'\n"}}}
        stale["evidence"] = "from_frozen"
        outcomes.append(_Case(stale, self.corpus).run()[0]["state"])

        # Oversized.
        big = copy.deepcopy(self.spec)
        big["patch"] = {"MAX_FILE_BYTES": 64}
        outcomes.append(_Case(big, self.corpus).run()[0]["state"])

        # Injected mid-build failure.
        injected = copy.deepcopy(self.spec)
        injected["inject_failure"] = "rename"
        outcomes.append(_Case(injected, self.corpus).run()[0]["state"])

        # Cancellation: the read-only path.
        cancelled = dict(self.spec, materialize=False)
        outcomes.append(_Case(cancelled, self.corpus).run()[0]["state"])

        self.assertEqual(
            ["candidate_ready", "refused", "oversized", "refused", "candidate_ready"],
            outcomes,
        )

        self.assertEqual(self.git_before, _git_state(REPO))
        self.assertEqual(self.tracked_before, _tracked_bytes(REPO))
        self.assertEqual(self.fixture_before, _fixture_bytes())
        if self.bundle_before is not None:
            bundle = os.path.join(REPO, "hrca-df8baf7.bundle")
            self.assertEqual(
                self.bundle_before, (os.stat(bundle).st_size, os.stat(bundle).st_mtime_ns)
            )

    def test_an_untracked_entry_is_not_added_by_a_build(self):
        before = _git(REPO, "--no-optional-locks", "status", "--porcelain").stdout
        self.assertIn("?? hrca-df8baf7.bundle", before)
        _Case(self.spec, self.corpus).run()
        self.assertEqual(
            before, _git(REPO, "--no-optional-locks", "status", "--porcelain").stdout
        )


class TempGitRepositoryTests(unittest.TestCase):
    """The same proof against a repository the test can freely inspect."""

    def test_a_build_does_not_touch_a_real_working_tree(self):
        root = _copy_repo()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, env=env, check=True)
        ident = ["-c", "user.email=p54@example.com", "-c", "user.name=P54"]
        subprocess.run(["git", *ident, "add", "-A"], cwd=root, env=env, check=True)
        subprocess.run(
            ["git", *ident, "commit", "-q", "-m", "fixture"], cwd=root, env=env, check=True
        )

        spec = dict(_manifest()["cases"][0])
        built = _Case(spec, _manifest())
        built.root = root  # build against the Git working tree itself
        built.evidence = _evidence(root)
        baseline = _baseline_of(built.evidence)
        intent = dict(_manifest()["intent_base"])
        intent["baseline"] = baseline
        built.delta, _error = intent_delta.build_intent_delta(intent)
        built.proposal, _error = impact_proposal.build_impact_proposal(
            built.delta, built.evidence
        )
        built.edit, _error = candidate_edit.build_edit(
            {
                "intent_delta_id": built.delta["intent_delta_id"],
                "proposal_id": built.proposal["proposal_id"],
                "binding_fingerprint": built.proposal["binding"]["binding_fingerprint"],
                "baseline": baseline,
                "operations": [
                    {
                        "op": "replace_file",
                        "path": PREDECESSOR_PATH,
                        "expected_sha256": _manifest()["predecessor"]["sha256"],
                        "text": _manifest()["result"]["text"],
                    }
                ],
            }
        )

        before_state = _git_state(root)
        before_bytes = _tracked_bytes(root)
        envelope, error = built.run()
        self.assertIsNone(error)
        self.assertEqual(candidate.STATE_CANDIDATE_READY, envelope["state"])
        self.assertEqual(before_state, _git_state(root))
        self.assertEqual(before_bytes, _tracked_bytes(root))
        # The candidate landed outside the working tree.
        self.assertNotIn(envelope["candidate_root_name"], os.listdir(root))


# -- boundaries ------------------------------------------------------------


def _imported_names(source: str):
    tree = ast.parse(source)
    modules = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(("hrca." if node.level else "") + node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    return modules, calls


class BoundaryTests(unittest.TestCase):
    _MODULES = ("candidate_edit", "candidate_diff", "candidate", "candidate_cli")

    def _source(self, module: str) -> str:
        with open(_hrca_module(module), encoding="utf-8") as fh:
            return fh.read()

    def test_no_module_imports_a_write_side_or_network_seam(self):
        for module in self._MODULES:
            modules, _calls = _imported_names(self._source(module))
            for name in sorted(modules):
                with self.subTest(module=module, imported=name):
                    self.assertNotIn(name, _FORBIDDEN_IMPORTS)
                    self.assertFalse(name.startswith("PySide6"))

    def test_no_module_can_spawn_a_process_or_open_a_socket(self):
        for module in self._MODULES:
            modules, calls = _imported_names(self._source(module))
            with self.subTest(module=module):
                self.assertNotIn("subprocess", modules)
                self.assertNotIn("socket", modules)
                self.assertNotIn("exec", calls)
                self.assertNotIn("eval", calls)

    def test_only_the_candidate_module_touches_a_filesystem(self):
        for module in ("candidate_edit", "candidate_diff"):
            modules, calls = _imported_names(self._source(module))
            with self.subTest(module=module):
                self.assertNotIn("os", modules)
                self.assertNotIn("tempfile", modules)
                self.assertNotIn("open", calls)

    def test_no_protocol_action_was_added(self):
        # The four candidate-named actions all predate this work, none carries
        # an edit grammar, and none is reachable from this contract. The count
        # grew by ORCH-BACKBONE-1's five-action family, which is a separate
        # increment that adds no candidate-named action.
        self.assertEqual("3.10.0", contract.CONTRACT_VERSION)
        self.assertEqual(66, len(contract.ALLOWED_ACTIONS))
        self.assertEqual(
            {
                "create_candidate",
                "get_candidate",
                "adopt_candidate",
                "stage_candidate_package",
            },
            {action for action in contract.ALLOWED_ACTIONS if "candidate" in action},
        )

    def test_every_schema_is_the_one_the_task_froze(self):
        self.assertEqual("1.1.0", scanner.SCHEMA_VERSION)
        self.assertEqual("1.0.0", twin.TWIN_SCHEMA_VERSION)
        self.assertEqual("1.0.0", intent_delta.INTENT_DELTA_SCHEMA_VERSION)
        self.assertEqual("1.0.0", impact_proposal.IMPACT_SCHEMA_VERSION)
        self.assertEqual("1.0.0", candidate_edit.CANDIDATE_EDIT_SCHEMA_VERSION)
        self.assertEqual("1.0.0", candidate.CANDIDATE_SCHEMA_VERSION)
        self.assertEqual("1.0.0", candidate_diff.CANDIDATE_DIFF_SCHEMA_VERSION)

    def test_the_mutation_surface_names_every_boundary_the_contract_names(self):
        built = _Case(_manifest()["cases"][0], _manifest())
        envelope, _error = built.run()
        self.assertEqual(
            {
                "accepted_source",
                "git_index",
                "git_ref",
                "branch",
                "commit",
                "worktree",
                "runner_job",
                "provider_request",
                "credential",
                "network",
                "remote",
                "twin_state",
                "memory",
                "validation",
                "approval",
                "adoption",
                "protocol_action",
                "ui",
                "package_state",
                "recovery_state",
            },
            set(envelope["mutation_surface"]),
        )
        self.assertTrue(all(v is False for v in envelope["mutation_surface"].values()))
        self.assertEqual(
            {"candidate_root_only": True, "accepted_repository": False, "git_metadata": False},
            envelope["write_scope"],
        )

    def test_a_candidate_is_never_validated_approved_or_adopted(self):
        built = _Case(_manifest()["cases"][0], _manifest())
        envelope, _error = built.run()
        for field in ("validated", "approved", "adopted", "executable", "applied"):
            with self.subTest(field=field):
                self.assertIs(False, envelope[field])


# -- privacy ---------------------------------------------------------------


class PrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.built = _Case(_manifest()["cases"][0], _manifest())
        cls.envelope, _error = cls.built.run()

    def test_no_envelope_carries_an_environment_fact_or_an_absolute_path(self):
        rendered = candidate.dumps(self.envelope)
        for forbidden in (
            sys.executable,
            sys.prefix,
            os.path.expanduser("~"),
            os.path.abspath(REPO),
            os.path.abspath(self.built.base),
            os.path.abspath(self.built.root),
        ):
            if not forbidden:
                continue
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_the_envelope_names_the_root_but_not_where_it_lives(self):
        self.assertTrue(self.envelope["candidate_root_name"].startswith(candidate.ROOT_PREFIX))
        self.assertNotIn(os.sep, self.envelope["candidate_root_name"])

    def test_no_source_body_reaches_the_envelope_outside_the_diff(self):
        # The diff legitimately carries the changed source lines. Strip it, and
        # then no line of the predecessor may appear anywhere in the envelope.
        stripped = copy.deepcopy(self.envelope)
        for record in stripped["operations"]:
            record["diff"] = []
        rendered = candidate.dumps(stripped)
        for line in _frozen_text(PREDECESSOR_PATH).splitlines():
            line = line.strip()
            if len(line) < 12:
                continue
            with self.subTest(line=line):
                self.assertNotIn(line, rendered)

    def test_every_refusal_reason_is_bounded_and_path_free(self):
        reasons = [
            candidate.REASON_EDIT_NOT_VALID,
            candidate.REASON_DELTA_NOT_VALID,
            candidate.REASON_PROPOSAL_NOT_VALID,
            candidate.REASON_EVIDENCE_UNBOUND,
            candidate.REASON_PROPOSAL_NOT_REBOUND,
            candidate.REASON_DELTA_MISMATCH,
            candidate.REASON_PROPOSAL_MISMATCH,
            candidate.REASON_BINDING_MISMATCH,
            candidate.REASON_BASELINE_MISMATCH,
            candidate.REASON_TARGET_NOT_IN_SCOPE,
            candidate.REASON_TARGET_NOT_IN_EVIDENCE,
            candidate.REASON_PROPOSAL_NOT_AUTHORIZING,
            candidate.REASON_TWIN_IDENTITY_ABSENT,
            candidate.REASON_TWIN_IDENTITY_MISMATCH,
            candidate.REASON_TWIN_IDENTITY_UNBOUND,
            candidate.REASON_PREDECESSOR_ABSENT,
            candidate.REASON_PREDECESSOR_NOT_REGULAR,
            candidate.REASON_PREDECESSOR_SYMLINK,
            candidate.REASON_PREDECESSOR_HARDLINK,
            candidate.REASON_PREDECESSOR_ESCAPES,
            candidate.REASON_PREDECESSOR_UNREADABLE,
            candidate.REASON_PREDECESSOR_MOVED,
            candidate.REASON_PREDECESSOR_STALE,
            candidate.REASON_OUTPUT_UNUSABLE,
            candidate.REASON_OUTPUT_INSIDE_REPOSITORY,
            candidate.REASON_OUTPUT_ROOT_TAKEN,
            candidate.REASON_STAGING_FAILED,
            candidate.REASON_VERIFY_FAILED,
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)
                self.assertLess(len(reason), 200)

    def test_no_result_leaks_a_caller_value(self):
        marker = "AKIA-EXAMPLE-NOT-A-REAL-KEY"
        built = _Case(_manifest()["cases"][0], _manifest())
        raw = {
            "intent_delta_id": built.delta["intent_delta_id"],
            "proposal_id": built.proposal["proposal_id"],
            "binding_fingerprint": built.proposal["binding"]["binding_fingerprint"],
            "baseline": built.edit["baseline"],
            "operations": [
                {
                    "op": "replace_file",
                    "path": PREDECESSOR_PATH,
                    "expected_sha256": marker,
                    "text": "x\n",
                }
            ],
        }
        edit, error = candidate_edit.build_edit(raw)
        self.assertIsNone(edit)
        self.assertNotIn(marker, error)
        envelope, error = candidate.build_candidate(
            built.edit, built.delta, built.proposal, built.evidence, built.root,
            built.base, materialize=True,
        )
        self.assertIsNone(error)
        self.assertNotIn(marker, candidate.dumps(envelope))


# -- the offline CLI --------------------------------------------------------


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = _manifest()

    def _write(self, directory: str, name: str, payload) -> str:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def _files(self, sandbox: str):
        built = _Case(self.corpus["cases"][0], self.corpus)
        intent = dict(self.corpus["intent_base"])
        intent["baseline"] = _baseline_of(built.evidence)
        return {
            "intent": self._write(sandbox, "intent.json", intent),
            "scanner": self._write(sandbox, "scan.json", built.evidence["scanner"]),
            "twin": self._write(sandbox, "twin.json", built.evidence["twin"]),
            "repo": built.root,
        }

    def test_derive_prints_the_identities_an_edit_must_declare(self):
        with tempfile.TemporaryDirectory() as sandbox:
            files = self._files(sandbox)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = candidate_cli.main(
                    ["derive", "--intent", files["intent"], "--scanner", files["scanner"],
                     "--twin", files["twin"]]
                )
        self.assertEqual(0, code)
        summary = json.loads(out.getvalue())
        self.assertTrue(summary["intent_delta_id"].startswith("intent:"))
        self.assertTrue(summary["proposal_id"].startswith("impact:"))
        self.assertTrue(summary["binding_fingerprint"].startswith("bind:"))
        self.assertIs(True, summary["authorizes_an_edit"])

    def test_review_renders_the_envelope_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as sandbox:
            files = self._files(sandbox)
            built = _Case(self.corpus["cases"][0], self.corpus)
            edit = self._write(
                sandbox,
                "edit.json",
                {
                    "intent_delta_id": built.delta["intent_delta_id"],
                    "proposal_id": built.proposal["proposal_id"],
                    "binding_fingerprint": built.proposal["binding"]["binding_fingerprint"],
                    "baseline": built.delta["baseline"],
                    "operations": [
                        {
                            "op": "replace_file",
                            "path": PREDECESSOR_PATH,
                            "expected_sha256": self.corpus["predecessor"]["sha256"],
                            "text": self.corpus["result"]["text"],
                        }
                    ],
                },
            )
            base = tempfile.mkdtemp(prefix="p54-base-")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = candidate_cli.main(
                    ["review", "--edit", edit, "--intent", files["intent"],
                     "--scanner", files["scanner"], "--twin", files["twin"],
                     "--repo", files["repo"]]
                )
            self.assertEqual(0, code)
            self.assertEqual([], sorted(os.listdir(base)))
        envelope = json.loads(out.getvalue())
        self.assertEqual(candidate.STATE_CANDIDATE_READY, envelope["state"])
        self.assertIsNone(envelope["candidate_root_name"])

    def test_build_materializes_and_reports_the_container_name(self):
        with tempfile.TemporaryDirectory() as sandbox:
            files = self._files(sandbox)
            built = _Case(self.corpus["cases"][0], self.corpus)
            edit = self._write(
                sandbox,
                "edit.json",
                {
                    "intent_delta_id": built.delta["intent_delta_id"],
                    "proposal_id": built.proposal["proposal_id"],
                    "binding_fingerprint": built.proposal["binding"]["binding_fingerprint"],
                    "baseline": built.delta["baseline"],
                    "operations": [
                        {
                            "op": "replace_file",
                            "path": PREDECESSOR_PATH,
                            "expected_sha256": self.corpus["predecessor"]["sha256"],
                            "text": self.corpus["result"]["text"],
                        }
                    ],
                },
            )
            base = tempfile.mkdtemp(prefix="p54-base-")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = candidate_cli.main(
                    ["build", "--edit", edit, "--intent", files["intent"],
                     "--scanner", files["scanner"], "--twin", files["twin"],
                     "--repo", files["repo"], "--output-base", base]
                )
            self.assertEqual(0, code)
            envelope = json.loads(out.getvalue())
            self.assertTrue(
                os.path.isdir(os.path.join(base, envelope["candidate_root_name"]))
            )
            self.assertIn(envelope["candidate_id"], err.getvalue())
            self.assertIn(envelope["candidate_root_name"], err.getvalue())
            # The container name travels; where it lives does not.
            self.assertNotIn(base, err.getvalue())
            self.assertNotIn(base, out.getvalue())

    def test_a_non_ready_state_is_a_refusal_exit(self):
        with tempfile.TemporaryDirectory() as sandbox:
            files = self._files(sandbox)
            built = _Case(self.corpus["cases"][0], self.corpus)
            edit = self._write(
                sandbox,
                "edit.json",
                {
                    "intent_delta_id": built.delta["intent_delta_id"],
                    "proposal_id": built.proposal["proposal_id"],
                    "binding_fingerprint": built.proposal["binding"]["binding_fingerprint"],
                    "baseline": built.delta["baseline"],
                    "operations": [
                        {
                            "op": "replace_file",
                            "path": PREDECESSOR_PATH,
                            "expected_sha256": self.corpus["predecessor"]["sha256"],
                            "text": "VERSION = '1'\x00\n",
                        }
                    ],
                },
            )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = candidate_cli.main(
                    ["review", "--edit", edit, "--intent", files["intent"],
                     "--scanner", files["scanner"], "--twin", files["twin"],
                     "--repo", files["repo"]]
                )
        self.assertEqual(candidate_cli.EXIT_REFUSED, code)
        self.assertEqual(
            candidate.STATE_UNSUPPORTED_CONTENT, json.loads(out.getvalue())["state"]
        )

    def test_a_missing_file_is_a_usage_failure_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as sandbox:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = candidate_cli.main(
                    ["derive", "--intent", os.path.join(sandbox, "absent.json"),
                     "--scanner", os.path.join(sandbox, "absent.json"),
                     "--twin", os.path.join(sandbox, "absent.json")]
                )
        self.assertEqual(candidate_cli.EXIT_USAGE, code)
        self.assertNotIn(sandbox, err.getvalue())


if __name__ == "__main__":
    unittest.main()
