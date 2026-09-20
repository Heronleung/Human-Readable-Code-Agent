"""Bounded validation evidence for one exact candidate (P5.5a).

Three things are held here.

The **oracle**: ``fixtures/validation/manifest.json`` states, by hand and
independently of :mod:`hrca.validation_policy`, every check record and the
expected terminal state for each way the runtime can behave — a pass, a
command failure, an unreadable artifact, both spellings of a timeout,
cancellation and an absent runtime.

The **honesty**: ``unavailable`` is what this host can actually produce, because
no Docker daemon is reachable here. The pass, fail and timeout cases run against
a container double that reproduces the in-image wire contract exactly
(``packaging/runner/runner_main.py``) and is labelled as a double. No state is
invented to fill the gap left by the missing runtime, and no state — including
``passed`` — is an approval.

The **boundaries**: the accepted repository, its Git state and the candidate
itself are snapshotted before and after success, failure and refusal and
required to be unchanged; the evidence store is append-only and must refuse to
rewrite what it already holds.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from hrca import (
    app_package,
    candidate,
    candidate_edit,
    container_runner,
    contract,
    delta_verifier,
    impact_proposal,
    intent_delta,
    rule_delta,
    runtime_handlers,
    scanner,
    twin,
    validation,
    validation_cli,
    validation_plan,
    validation_policy,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
SRC = os.path.join(REPO, "src")
FIXTURE_REPO = os.path.join(REPO, "candidate_fixtures", "repo")
CANDIDATE_CORPUS = os.path.join(REPO, "candidate_fixtures", "manifest.json")
MANIFEST = os.path.join(REPO, "fixtures", "validation", "manifest.json")
BUNDLE = os.path.join(REPO, "hrca-df8baf7.bundle")

FIXED_WORKSPACE = twin.workspace_id_for("hrca-p55-fixture")

_FORBIDDEN_IMPORTS = frozenset(
    {
        "socket",
        "urllib",
        "http",
        "ssl",
        "ctypes",
        "runpy",
        "platform",
        "uuid",
        "hrca.boundary",
        "hrca.client",
        "hrca.client_core",
        "hrca.workspace",
        "hrca.provider",
        "hrca.provider_config",
        "hrca.deepseek",
        "hrca.credential_store",
        "hrca.credential_host",
        "hrca.document",
        "hrca.version_store",
        "hrca.library",
        "hrca.memory",
        "hrca.memory_store",
        "hrca.twin_store",
        "hrca.hook_capture",
        "hrca.advisory",
        "hrca.rule_delta_interpret",
    }
)


# -- the container double ---------------------------------------------------


class FakeDocker:
    """A container-client double that reproduces the in-image wire contract.

    ``docker info`` and ``docker image inspect`` succeed, and ``docker run``
    resolves the handler through the same code-owned registry the image bakes in
    and writes ``{"result": ...}`` or ``{"error": ...}`` exactly as
    ``packaging/runner/runner_main.py`` does. This is a double, not a claim about
    a real container: the isolation of a real one is the runner's own, asserted
    by ``tests/test_container_runner.py``.
    """

    def __init__(self, modes: dict) -> None:
        self.modes = dict(modes)
        self.runs: list = []
        self.kills: list = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if argv[1:2] == ["info"] or argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[1:3] == ["container", "inspect"]:
            # The timeout reconciliation asks whether the exact container name
            # still resolves. "No such container" is what lets it conclude; a
            # double that did not answer would leave the query unknown, which is
            # deliberately not the same as absent.
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        if argv[1:2] in (["kill"], ["rm"]):
            # The runner's own timeout path kills and removes the container; a
            # double that did not answer these would hide whether that path ran.
            self.kills.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        input_dir, output_dir = None, None
        for index, element in enumerate(argv):
            if element != "--mount" or index + 1 >= len(argv):
                continue
            mount = argv[index + 1]
            for part in mount.split(","):
                if not part.startswith("src="):
                    continue
                source = part[len("src="):]
                if "dst=/out" in mount:
                    output_dir = source
                elif "dst=/in" in mount:
                    input_dir = source
        with open(os.path.join(input_dir, "input.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        handler_id = payload["handler"]
        mode = self._mode_for(handler_id)
        self.runs.append((handler_id, mode))

        if mode == "timeout_injected":
            raise TimeoutError("simulated timeout")
        if mode == "timeout_real":
            raise subprocess.TimeoutExpired(argv, 10.0)
        if mode == "nonzero":
            return subprocess.CompletedProcess(argv, 1, b"boom", b"worse")
        if mode == "garbage":
            with open(os.path.join(output_dir, "output.json"), "w", encoding="utf-8") as fh:
                fh.write("{not json")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        function = runtime_handlers.resolve_handler(handler_id)
        result, error = function(payload["input"], payload.get("parameters"))
        body = {"result": result} if error is None else {"error": error}
        with open(os.path.join(output_dir, "output.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=True, separators=(",", ":"))
        return subprocess.CompletedProcess(argv, 0, b"ran", b"")

    def _mode_for(self, handler_id: str) -> str:
        for check_id, mode in self.modes.items():
            package = validation_policy.package_for(check_id)
            if package is not None and package["handler"] == handler_id:
                return mode
        return "ok"


def _corpus() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _candidate_corpus() -> dict:
    with open(CANDIDATE_CORPUS, encoding="utf-8") as fh:
        return json.load(fh)


# -- building the frozen candidate -----------------------------------------


def _build_candidate():
    """Return ``(candidate_root, review)`` for the frozen P5.4 fixture."""
    corpus = _candidate_corpus()
    work = tempfile.mkdtemp(prefix="p55-work-")
    root = os.path.join(work, "repo")
    shutil.copytree(FIXTURE_REPO, root)

    doc = scanner.scan_directory(root)
    fingerprints = {}
    for record in doc["files"]:
        if record["path"].endswith((".py", ".pyi")):
            with open(os.path.join(root, *record["path"].split("/")), "rb") as fh:
                fingerprints[record["path"]] = twin.fingerprint_bytes(fh.read())
    store = twin.build_store(doc, fingerprints, FIXED_WORKSPACE, 1, "T")
    evidence = {"scanner": doc, "twin": store}
    baseline = {
        "workspace_id": store["workspace_revision"]["workspace_id"],
        "scan_generation": store["workspace_revision"]["scan_generation"],
        "baseline_fingerprint": store["workspace_revision"]["baseline_fingerprint"],
        "scanner_schema_version": doc["schema_version"],
        "grammar": doc["grammar"],
    }
    intent = dict(corpus["intent_base"])
    intent["baseline"] = baseline
    delta, error = intent_delta.build_intent_delta(intent)
    assert error is None, error
    proposal, error = impact_proposal.build_impact_proposal(delta, evidence)
    assert error is None, error
    edit, error = candidate_edit.build_edit(
        {
            "intent_delta_id": delta["intent_delta_id"],
            "proposal_id": proposal["proposal_id"],
            "binding_fingerprint": proposal["binding"]["binding_fingerprint"],
            "baseline": baseline,
            "operations": [
                {
                    "op": "replace_file",
                    "path": "pkg/service.py",
                    "expected_sha256": corpus["predecessor"]["sha256"],
                    "text": corpus["result"]["text"],
                }
            ],
        }
    )
    assert error is None, error
    base = tempfile.mkdtemp(prefix="p55-out-")
    review, error = candidate.build_candidate(edit, delta, proposal, evidence, root, base)
    assert error is None, error
    assert review["state"] == candidate.STATE_CANDIDATE_READY, review["state"]
    return os.path.join(base, review["candidate_root_name"]), review


class _Fixture:
    """One candidate, verified once, shared by the read-only tests."""

    @classmethod
    def setUpClass(cls):
        cls.root, cls.review = _build_candidate()
        cls.binding, cls.error = validation.verify_candidate(cls.root, cls.review)
        assert cls.error is None, cls.error
        cls.plan, cls.plan_error = validation_plan.build_plan(cls.binding)
        assert cls.plan_error is None, cls.plan_error
        cls.corpus = _corpus()


# -- scenarios --------------------------------------------------------------


class ScenarioTests(_Fixture, unittest.TestCase):
    """Every way the runtime can behave, run against the hand-authored oracle."""

    maxDiff = None

    def _run(self, scenario, docker=None):
        modes = scenario["modes"]
        runtime = scenario.get("runtime", "available")
        which = (lambda name: "/usr/bin/docker") if runtime == "available" else (lambda name: None)
        cancelled = (lambda: True) if scenario.get("cancel") else None
        docker = docker or FakeDocker(modes)
        result = validation.run_plan(
            self.plan,
            self.root,
            self.review,
            spawn=docker,
            which=which,
            cancelled=cancelled,
        )
        return result[0], result[1], docker

    def test_every_scenario(self):
        for scenario in self.corpus["scenarios"]:
            with self.subTest(case=scenario["case"]):
                result, error, _docker = self._run(scenario)
                self.assertIsNone(error, scenario["case"])
                self.assertIsNone(validation.validate_result(result))
                self.assertEqual(scenario["expect_state"], result["state"])
                self.assertEqual(
                    scenario["expect_check_states"],
                    [record["state"] for record in result["checks"]],
                )
                self.assertEqual(
                    scenario["evidence_complete"], result["evidence_complete"]
                )
                if "expect_limitations" in scenario:
                    self.assertEqual(
                        scenario["expect_limitations"],
                        result["attempts"][0]["limitations"],
                    )
                if scenario.get("dispatched") is False:
                    self.assertEqual([], _docker.runs)

    def test_the_check_order_is_the_canonical_one(self):
        result, _error, _docker = self._run(self.corpus["scenarios"][0])
        self.assertEqual(
            self.corpus["check_order"],
            [record["check_id"] for record in result["checks"]],
        )

    def test_a_passing_run_records_the_declared_isolation_facts(self):
        result, _error, _docker = self._run(
            {"modes": {check_id: "ok" for check_id in self.corpus["check_order"]}}
        )
        self.assertEqual("passed", result["state"])
        for attempt in result["attempts"]:
            with self.subTest(check=attempt["check_id"]):
                self.assertEqual(self.corpus["expected_isolation"], attempt["isolation"])
                self.assertEqual(
                    self.corpus["expected_argv_prefix"], attempt["argv"][: len(
                        self.corpus["expected_argv_prefix"]
                    )]
                )
                self.assertEqual(0, attempt["returncode"])
                self.assertIs(False, attempt["timed_out"])
                self.assertIs(False, attempt["cancelled"])
                self.assertEqual(
                    self.corpus["checks"][attempt["check_id"]]["expected_artifact"],
                    attempt["artifact"]["name"],
                )
                self.assertGreater(attempt["artifact"]["bytes"], 0)

    def test_the_artifact_digest_is_of_the_collected_result(self):
        result, _error, _docker = self._run(
            {"modes": {check_id: "ok" for check_id in self.corpus["check_order"]}}
        )
        for attempt in result["attempts"]:
            package = validation_policy.package_for(attempt["check_id"])
            form = validation_policy.POLICY[attempt["check_id"]]["form_input"]
            expected, error = runtime_handlers.resolve_handler(package["handler"])(
                form, None
            )
            self.assertIsNone(error)
            canonical = validation.dumps(expected)
            with self.subTest(check=attempt["check_id"]):
                self.assertEqual(
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    attempt["artifact"]["sha256"],
                )
                self.assertEqual(len(canonical), attempt["artifact"]["bytes"])

    def test_stdout_and_stderr_are_recorded_as_bounded_digests(self):
        result, _error, _docker = self._run(
            {"modes": {check_id: "ok" for check_id in self.corpus["check_order"]}}
        )
        for attempt in result["attempts"]:
            with self.subTest(check=attempt["check_id"]):
                self.assertEqual(hashlib.sha256(b"ran").hexdigest(),
                                 attempt["stdout"]["sha256"])
                self.assertEqual(3, attempt["stdout"]["bytes"])
                self.assertEqual(hashlib.sha256(b"").hexdigest(),
                                 attempt["stderr"]["sha256"])
                self.assertEqual(0, attempt["stderr"]["bytes"])

    def test_a_failing_check_records_the_container_exit_code(self):
        modes = {check_id: "ok" for check_id in self.corpus["check_order"]}
        modes["check:late_return_fee"] = "nonzero"
        result, _error, _docker = self._run({"modes": modes})
        self.assertEqual("failed", result["state"])
        failed = result["attempts"][0]
        self.assertEqual(1, failed["returncode"])
        self.assertIsNone(failed["artifact"])

    def test_the_injected_timeout_reaches_the_runner_s_cleanup(self):
        modes = {check_id: "ok" for check_id in self.corpus["check_order"]}
        modes["check:late_return_fee"] = "timeout_injected"
        result, _error, docker = self._run({"modes": modes})
        attempt = result["attempts"][0]
        self.assertEqual("timed_out", attempt["state"])
        self.assertIs(True, attempt["timed_out"])
        # The runner's own bounded token, because this shape of timeout is the
        # one it actually handles.
        self.assertEqual([app_package.STATE_TIMEOUT], attempt["limitations"])
        # And its kill-and-remove path really ran, which is why this shape is
        # the clean one.
        self.assertEqual(["kill", "rm"], docker.kills)

    def test_the_real_timeout_also_reaches_the_runner_s_cleanup(self):
        # A real subprocess.TimeoutExpired now follows the runner's whole
        # timeout lifecycle, exactly like the injected TimeoutError does: the
        # kill and the removal run, the staged roots are cleaned, and the
        # bounded token is returned. Both spellings therefore reach the same
        # outcome, which is the point of the repair.
        modes = {check_id: "ok" for check_id in self.corpus["check_order"]}
        modes["check:late_return_fee"] = "timeout_real"
        result, _error, docker = self._run({"modes": modes})
        attempt = result["attempts"][0]
        self.assertEqual("timed_out", attempt["state"])
        self.assertIs(True, attempt["timed_out"])
        self.assertEqual([app_package.STATE_TIMEOUT], attempt["limitations"])
        self.assertEqual(["kill", "rm"], docker.kills)

    def test_the_validator_guard_still_reports_a_leaked_timeout(self):
        # The contract keeps its defensive catch so that a regression in the
        # runner's own handling is still bounded rather than escaping.
        modes = {check_id: "ok" for check_id in self.corpus["check_order"]}
        with mock.patch.object(
            container_runner.ContainerRunner,
            "run",
            side_effect=subprocess.TimeoutExpired(["docker", "run"], 10.0),
        ):
            result, _error, _docker = self._run({"modes": modes})
        self.assertEqual("timed_out", result["state"])
        attempt = result["attempts"][0]
        self.assertEqual(1, len(attempt["limitations"]))
        self.assertIn("outside its own lifecycle", attempt["limitations"][0])

    def test_cancellation_dispatches_nothing(self):
        modes = {check_id: "ok" for check_id in self.corpus["check_order"]}
        docker = FakeDocker(modes)
        result, _error = validation.run_plan(
            self.plan, self.root, self.review, spawn=docker,
            which=lambda name: "/usr/bin/docker", cancelled=lambda: True,
        )
        self.assertEqual("cancelled", result["state"])
        self.assertEqual([], docker.runs)
        for attempt in result["attempts"]:
            with self.subTest(check=attempt["check_id"]):
                self.assertIsNone(attempt["argv"])
                self.assertIsNone(attempt["isolation"])
                self.assertIs(True, attempt["cancelled"])

    def test_an_absent_runtime_dispatches_nothing_and_says_why(self):
        result, _error = validation.run_plan(
            self.plan, self.root, self.review,
            spawn=FakeDocker({}), which=lambda name: None,
        )
        self.assertEqual("unavailable", result["state"])
        for attempt in result["attempts"]:
            with self.subTest(check=attempt["check_id"]):
                self.assertEqual(
                    [container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE],
                    attempt["limitations"],
                )
                self.assertIsNone(attempt["argv"])


class UnavailableRuntimeTests(_Fixture, unittest.TestCase):
    """The unavailable-runtime outcome, with nothing dispatched.

    This class is live-Docker safe by construction: every runner it builds has
    an injected ``which`` or ``spawn``, so no real container can start. Real
    dispatch lives in ``tests/test_candidate_syntax_integration.py``, which is
    dedicated to it and skip-guarded; a unit module must not begin executing
    containers simply because a daemon appeared on the host.
    """

    def test_an_absent_client_makes_every_check_unavailable(self):
        modes = {check_id: "ok" for check_id in self.corpus["check_order"]}
        docker = FakeDocker(modes)
        result, error = validation.run_plan(
            self.plan, self.root, self.review,
            spawn=docker, which=lambda name: None,
        )
        self.assertIsNone(error)
        self.assertEqual("unavailable", result["state"])
        self.assertIs(False, result["evidence_complete"])
        self.assertEqual([], docker.runs)
        for attempt in result["attempts"]:
            with self.subTest(check=attempt["check_id"]):
                self.assertEqual("unavailable", attempt["state"])
                self.assertIsNone(attempt["argv"])
                self.assertIsNone(attempt["artifact"])
                self.assertEqual(
                    [container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE],
                    attempt["limitations"],
                )

    def test_the_preflight_report_is_internally_consistent_whatever_the_host(self):
        # Availability is not asserted, because it is an environment fact and
        # asserting it either way would break on the other kind of host. What is
        # asserted is that the three checks agree with the verdict.
        preflight = container_runner.ContainerRunner().preflight()
        checks = preflight["checks"]
        self.assertEqual({"docker", "daemon", "image"}, set(checks))
        if preflight["available"]:
            self.assertIsNone(preflight["reason"])
            self.assertTrue(all(checks.values()))
        else:
            self.assertIn(
                preflight["reason"],
                {
                    container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE,
                    container_runner.PREFLIGHT_RUNTIME_BLOCKED,
                },
            )
            self.assertFalse(all(checks.values()))
            if preflight["reason"] == container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE:
                self.assertFalse(checks["daemon"])


# -- candidate binding ------------------------------------------------------


class CandidateBindingTests(_Fixture, unittest.TestCase):
    def _copy_root(self) -> str:
        work = tempfile.mkdtemp(prefix="p55-bind-")
        root = os.path.join(work, os.path.basename(self.root))
        shutil.copytree(self.root, root)
        return root

    def test_the_binding_describes_the_candidate_exactly(self):
        self.assertEqual(self.review["candidate_id"], self.binding["candidate_id"])
        self.assertEqual(
            os.path.basename(self.root), self.binding["candidate_root_name"]
        )
        self.assertEqual(
            self.corpus["checks"]["check:late_return_fee"]["expected_artifact"]
            is not None,
            True,
        )
        manifest = json.loads(
            open(os.path.join(self.root, candidate.MANIFEST_NAME), encoding="utf-8").read()
        )
        self.assertEqual(
            hashlib.sha256(
                open(os.path.join(self.root, candidate.MANIFEST_NAME), "rb").read()
            ).hexdigest(),
            self.binding["manifest_sha256"],
        )
        self.assertEqual(manifest["files"], self.binding["files"])

    def test_a_changed_staged_byte_is_refused(self):
        root = self._copy_root()
        target = os.path.join(root, candidate.FILES_DIR, "pkg", "service.py")
        with open(target, "ab") as fh:
            fh.write(b"# tampered\n")
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertIn(
            reason, {validation.REASON_FILE_SIZE, validation.REASON_FILE_HASH}
        )

    def test_a_changed_manifest_is_refused(self):
        root = self._copy_root()
        path = os.path.join(root, candidate.MANIFEST_NAME)
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        manifest["files"][0]["bytes"] = 999
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, sort_keys=True, separators=(",", ":"))
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_MANIFEST_INVALID, reason)

    def test_a_missing_staged_file_is_refused(self):
        root = self._copy_root()
        os.remove(os.path.join(root, candidate.FILES_DIR, "pkg", "service.py"))
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_FILE_MISSING, reason)

    def test_an_extra_file_in_the_root_is_refused(self):
        root = self._copy_root()
        with open(os.path.join(root, candidate.FILES_DIR, "pkg", "extra.py"), "w") as fh:
            fh.write("X = 1\n")
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_ROOT_HAS_EXTRA_ENTRIES, reason)

    def test_an_extra_top_level_entry_is_refused(self):
        root = self._copy_root()
        with open(os.path.join(root, "notes.txt"), "w") as fh:
            fh.write("hi\n")
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_ROOT_HAS_EXTRA_ENTRIES, reason)

    def test_a_symlinked_staged_file_is_refused(self):
        root = self._copy_root()
        target = os.path.join(root, candidate.FILES_DIR, "pkg", "service.py")
        # The link points outside the root, so nothing extra appears beside it:
        # the refusal is about the link itself, not about a stray file.
        os.remove(target)
        os.symlink("/etc/hostname", target)
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_FILE_NOT_REGULAR, reason)

    def test_a_hardlinked_staged_file_is_refused(self):
        root = self._copy_root()
        target = os.path.join(root, candidate.FILES_DIR, "pkg", "service.py")
        keep = target + ".keep"
        os.rename(target, keep)
        os.link(keep, target)
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertIn(
            reason,
            {validation.REASON_FILE_LINKED, validation.REASON_ROOT_HAS_EXTRA_ENTRIES},
        )

    def test_a_root_named_for_a_different_candidate_is_refused(self):
        work = tempfile.mkdtemp(prefix="p55-bind-")
        root = os.path.join(work, "candidate-" + "0" * 32)
        shutil.copytree(self.root, root)
        binding, reason = validation.verify_candidate(root, self.review)
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_CANDIDATE_MISMATCH, reason)

    def test_a_missing_root_is_refused(self):
        binding, reason = validation.verify_candidate(
            os.path.join(tempfile.mkdtemp(prefix="p55-none-"), "absent"), self.review
        )
        self.assertIsNone(binding)
        self.assertEqual(validation.REASON_CANDIDATE_MISSING, reason)

    def test_a_review_that_describes_another_candidate_is_refused(self):
        review = copy.deepcopy(self.review)
        review["candidate_id"] = "candidate:" + "0" * 64
        _binding, reason = validation.verify_candidate(self.root, review)
        self.assertEqual(validation.REASON_REVIEW_INVALID, reason)

    def test_a_review_that_claims_approval_is_refused(self):
        for field in ("approved", "adopted", "validated", "executable", "applied"):
            review = copy.deepcopy(self.review)
            review[field] = True
            with self.subTest(field=field):
                _binding, reason = validation.verify_candidate(self.root, review)
                self.assertEqual(validation.REASON_REVIEW_INVALID, reason)

    def test_a_review_with_a_widened_mutation_surface_is_refused(self):
        review = copy.deepcopy(self.review)
        review["mutation_surface"]["commit"] = True
        _binding, reason = validation.verify_candidate(self.root, review)
        self.assertEqual(validation.REASON_REVIEW_INVALID, reason)

    def test_a_review_operation_disagreeing_with_the_manifest_is_refused(self):
        review = copy.deepcopy(self.review)
        review["operations"][0]["after_sha256"] = "0" * 64
        _binding, reason = validation.verify_candidate(self.root, review)
        self.assertEqual(validation.REASON_REVIEW_INVALID, reason)

    def test_a_non_mapping_review_is_refused(self):
        for review in (None, "review", 7):
            with self.subTest(review=review):
                _binding, reason = validation.verify_candidate(self.root, review)
                self.assertEqual(validation.REASON_REVIEW_INVALID, reason)

    def test_a_plan_for_a_different_candidate_is_refused_at_dispatch(self):
        binding = copy.deepcopy(self.binding)
        binding["files"][0]["bytes"] = binding["files"][0]["bytes"] + 1
        plan, error = validation_plan.build_plan(binding)
        self.assertIsNone(error)
        result, error = validation.run_plan(
            plan, self.root, self.review,
            spawn=FakeDocker({}), which=lambda name: "/usr/bin/docker",
        )
        self.assertIsNone(result)
        self.assertEqual(validation.REASON_CANDIDATE_MISMATCH, error)

    def test_a_shell_string_where_a_plan_belongs_is_refused(self):
        for plan in ("run the checks", "/bin/sh -c 'x'", "x"):
            with self.subTest(plan=plan):
                result, error = validation.run_plan(plan, self.root, self.review)
                self.assertIsNone(result)
                self.assertEqual(validation.REASON_SHELL_STRING, error)


# -- the evidence store -----------------------------------------------------


class EvidenceStoreTests(_Fixture, unittest.TestCase):
    def _base(self) -> str:
        return tempfile.mkdtemp(prefix="p55-evidence-")

    def _record(self, base, ordinal, modes=None):
        modes = modes or {check_id: "ok" for check_id in self.corpus["check_order"]}
        return validation.run_and_record(
            self.plan, self.root, self.review, base, ordinal=ordinal,
            spawn=FakeDocker(modes), which=lambda name: "/usr/bin/docker",
        )

    def test_a_first_record_starts_at_ordinal_one(self):
        self.assertEqual(1, validation.next_ordinal(self._base()))

    def test_the_evidence_root_holds_only_the_store(self):
        base = self._base()
        self._record(base, 1)
        self.assertEqual(
            ["attempts", "index.jsonl"], sorted(os.listdir(base))
        )

    def test_repeats_create_distinct_attempts_without_touching_the_first(self):
        base = self._base()
        ids = []
        for ordinal in (1, 2, 3):
            result, error = self._record(base, ordinal)
            self.assertIsNone(error)
            self.assertEqual("passed", result["state"])
            ids.append([record["attempt_id"] for record in result["attempts"]])
        self.assertEqual(3, len({tuple(group) for group in ids}))
        attempts, error = validation.read_attempts(base)
        self.assertIsNone(error)
        self.assertEqual(9, len(attempts))
        self.assertEqual(
            [1, 1, 1, 2, 2, 2, 3, 3, 3],
            [attempt["ordinal"] for attempt in attempts],
        )
        self.assertEqual(4, validation.next_ordinal(base))

    def test_the_index_is_append_only(self):
        base = self._base()
        self._record(base, 1)
        index = os.path.join(base, "index.jsonl")
        with open(index, encoding="utf-8") as fh:
            first = fh.read()
        self._record(base, 2)
        with open(index, encoding="utf-8") as fh:
            second = fh.read()
        self.assertTrue(second.startswith(first))
        # Three checks per run, so two runs leave six lines and the first three
        # are untouched.
        self.assertEqual(3, len(first.strip().splitlines()))
        self.assertEqual(6, len(second.strip().splitlines()))
        self.assertLess(len(first), len(second))

    def test_rewriting_an_attempt_with_different_bytes_is_refused(self):
        base = self._base()
        result, error = self._record(base, 1)
        self.assertIsNone(error)
        attempt = result["attempts"][0]
        tampered = copy.deepcopy(attempt)
        tampered["state"] = validation.STATE_PASSED
        tampered["limitations"] = []
        tampered["ordinal"] = attempt["ordinal"]
        tampered["check_id"] = attempt["check_id"]
        # Same identity, different content is impossible by construction; the
        # same *name* with different bytes is what the store must refuse.
        path = os.path.join(
            base, "attempts", attempt["attempt_id"].split(":", 1)[1] + ".json"
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(validation.dumps({**attempt, "state": "failed"}))
        _identity, reason = validation.record_attempt(base, attempt)
        self.assertEqual(validation.REASON_EVIDENCE_TAMPERED, reason)

    def test_a_tampered_attempt_file_invalidates_the_store(self):
        base = self._base()
        result, _error = self._record(base, 1)
        attempt = result["attempts"][0]
        path = os.path.join(
            base, "attempts", attempt["attempt_id"].split(":", 1)[1] + ".json"
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(validation.dumps({**attempt, "state": "failed"}))
        attempts, reason = validation.read_attempts(base)
        self.assertIsNone(attempts)
        self.assertEqual(validation.REASON_EVIDENCE_TAMPERED, reason)

    def test_a_missing_attempt_file_invalidates_the_store(self):
        base = self._base()
        result, _error = self._record(base, 1)
        os.remove(
            os.path.join(
                base,
                "attempts",
                result["attempts"][0]["attempt_id"].split(":", 1)[1] + ".json",
            )
        )
        attempts, reason = validation.read_attempts(base)
        self.assertIsNone(attempts)
        self.assertEqual(validation.REASON_EVIDENCE_TAMPERED, reason)

    def test_an_attempt_with_a_stale_identity_is_refused(self):
        base = self._base()
        result, _error = self._record(base, 1)
        attempt = dict(result["attempts"][0])
        attempt["state"] = validation.STATE_FAILED
        _identity, reason = validation.record_attempt(base, attempt)
        self.assertEqual(validation.REASON_EVIDENCE_TAMPERED, reason)

    def test_recording_the_same_attempt_twice_is_idempotent(self):
        base = self._base()
        result, _error = self._record(base, 1)
        attempt = result["attempts"][0]
        identity, reason = validation.record_attempt(base, attempt)
        self.assertIsNone(reason)
        self.assertEqual(attempt["attempt_id"], identity)

    def test_a_failing_run_still_records_its_evidence(self):
        base = self._base()
        modes = {check_id: "nonzero" for check_id in self.corpus["check_order"]}
        result, error = self._record(base, 1, modes)
        self.assertIsNone(error)
        self.assertEqual("failed", result["state"])
        attempts, error = validation.read_attempts(base)
        self.assertIsNone(error)
        self.assertEqual(3, len(attempts))
        self.assertTrue(all(a["state"] == "failed" for a in attempts))

    def test_the_store_needs_a_base(self):
        for base in (None, "", 7):
            with self.subTest(base=base):
                _identity, reason = validation.record_attempt(base, {"attempt_id": "attempt:x"})
                self.assertEqual(validation.REASON_EVIDENCE_UNUSABLE, reason)

    def test_reading_an_absent_store_is_refused(self):
        attempts, reason = validation.read_attempts(
            os.path.join(tempfile.mkdtemp(prefix="p55-none-"), "absent")
        )
        self.assertIsNone(attempts)
        self.assertEqual(validation.REASON_EVIDENCE_UNUSABLE, reason)


class StoreContainmentTests(_Fixture, unittest.TestCase):
    def test_evidence_lands_only_under_its_own_base(self):
        base = tempfile.mkdtemp(prefix="p55-evidence-")
        before = sorted(os.listdir(base))
        result, error = validation.run_and_record(
            self.plan, self.root, self.review, base, ordinal=1,
            spawn=FakeDocker({check_id: "ok" for check_id in self.corpus["check_order"]}),
            which=lambda name: "/usr/bin/docker",
        )
        self.assertIsNone(error)
        self.assertEqual([], before)
        self.assertEqual(
            ["attempts", "index.jsonl"], sorted(os.listdir(base))
        )
        self.assertEqual(3, len(os.listdir(os.path.join(base, "attempts"))))

    def test_the_candidate_is_untouched_by_a_run(self):
        before = _tree_digest(self.root)
        base = tempfile.mkdtemp(prefix="p55-evidence-")
        validation.run_and_record(
            self.plan, self.root, self.review, base, ordinal=1,
            spawn=FakeDocker({check_id: "ok" for check_id in self.corpus["check_order"]}),
            which=lambda name: "/usr/bin/docker",
        )
        self.assertEqual(before, _tree_digest(self.root))


def _tree_digest(root: str) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


# -- no authority -----------------------------------------------------------


class NoAuthorityTests(_Fixture, unittest.TestCase):
    """A green result is evidence, and only evidence."""

    def _passing(self):
        base = tempfile.mkdtemp(prefix="p55-evidence-")
        result, error = validation.run_and_record(
            self.plan, self.root, self.review, base, ordinal=1,
            spawn=FakeDocker({check_id: "ok" for check_id in self.corpus["check_order"]}),
            which=lambda name: "/usr/bin/docker",
        )
        assert error is None, error
        return result

    def test_a_passing_result_sets_no_approval_adoption_or_application(self):
        result = self._passing()
        self.assertEqual("passed", result["state"])
        self.assertIs(True, result["evidence_complete"])
        for field in ("approved", "adopted", "applied"):
            with self.subTest(field=field):
                self.assertIs(False, result[field])
        for attempt in result["attempts"]:
            for field in ("approved", "adopted", "applied"):
                with self.subTest(attempt=attempt["check_id"], field=field):
                    self.assertIs(False, attempt[field])

    def test_the_mutation_surface_is_wholly_absent(self):
        result = self._passing()
        self.assertEqual(
            {
                "accepted_source", "candidate", "git_index", "git_ref", "branch",
                "commit", "worktree", "twin_state", "memory", "approval",
                "adoption", "application", "provider_request", "credential",
                "network", "remote", "protocol_action", "ui",
            },
            set(result["mutation_surface"]),
        )
        self.assertTrue(all(v is False for v in result["mutation_surface"].values()))

    def test_a_result_claiming_approval_is_refused_by_its_validator(self):
        result = self._passing()
        for field in ("approved", "adopted", "applied"):
            tampered = copy.deepcopy(result)
            tampered[field] = True
            with self.subTest(field=field):
                self.assertEqual(
                    "result must not approve, adopt or apply",
                    validation.validate_result(tampered),
                )

    def test_an_attempt_claiming_adoption_is_refused_by_its_validator(self):
        attempt = self._passing()["attempts"][0]
        attempt["adopted"] = True
        self.assertEqual(
            "attempt must not approve, adopt or apply",
            validation.validate_attempt(attempt),
        )

    def test_evidence_complete_cannot_be_claimed_without_a_passing_state(self):
        result = self._passing()
        result["state"] = "failed"
        self.assertEqual(
            "evidence_complete does not match the overall state",
            validation.validate_result(result),
        )

    def test_a_tampered_result_identity_is_refused(self):
        result = self._passing()
        result["checks"][0]["state"] = "failed"
        self.assertEqual(
            "result_id does not match the result content",
            validation.validate_result(result),
        )

    def test_a_command_string_cannot_be_expressed_anywhere(self):
        # The plan carries no argv; the attempt's argv is the one the runner
        # built. There is nowhere for a caller to put a command.
        plan = self.plan
        for check in plan["checks"]:
            for field in check:
                with self.subTest(check=check["check_id"], field=field):
                    self.assertNotIn(field, {"argv", "command", "cmd", "shell", "entrypoint"})
        attempt = self._passing()["attempts"][0]
        self.assertEqual("docker", attempt["argv"][0])
        self.assertIn("--network", attempt["argv"])


# -- non-mutation of the accepted repository --------------------------------


def _git(root: str, *args: str):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
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
    out = {}
    for rel in _git(root, "ls-files", "-z").stdout.split("\0"):
        if not rel:
            continue
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


class RepositoryNonMutationTests(_Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if _git(REPO, "rev-parse", "--git-dir").returncode != 0:  # pragma: no cover
            raise unittest.SkipTest("the project is not a Git working tree")
        cls.git_before = _git_state(REPO)
        cls.tracked_before = _tracked_bytes(REPO)
        cls.fixture_before = _tree_digest(FIXTURE_REPO)
        cls.bundle_before = (
            (os.stat(BUNDLE).st_size, os.stat(BUNDLE).st_mtime_ns)
            if os.path.exists(BUNDLE) else None
        )

    def test_every_outcome_leaves_the_repository_and_its_git_state_unchanged(self):
        outcomes = []
        ok = {check_id: "ok" for check_id in self.corpus["check_order"]}
        for label, modes in (
            ("pass", ok),
            ("fail", {check_id: "nonzero" for check_id in self.corpus["check_order"]}),
            ("unreadable", {check_id: "garbage" for check_id in self.corpus["check_order"]}),
        ):
            with self.subTest(outcome=label):
                result, error = validation.run_and_record(
                    self.plan, self.root, self.review,
                    tempfile.mkdtemp(prefix="p55-evidence-"), ordinal=1,
                    spawn=FakeDocker(modes), which=lambda name: "/usr/bin/docker",
                )
                self.assertIsNone(error)
                outcomes.append(result["state"])

        # A refusal, and the honest absent-runtime outcome.
        _result, error = validation.run_plan(
            self.plan, self.root, copy.deepcopy(self.review) | {"state": "refused"}
        )
        self.assertIsNotNone(error)
        result, error = validation.run_and_record(
            self.plan, self.root, self.review,
            tempfile.mkdtemp(prefix="p55-evidence-"), ordinal=1,
            which=lambda name: None,
        )
        self.assertIsNone(error)
        outcomes.append(result["state"])

        self.assertEqual(["passed", "failed", "unknown", "unavailable"], outcomes)
        self.assertEqual(self.git_before, _git_state(REPO))
        self.assertEqual(self.tracked_before, _tracked_bytes(REPO))
        self.assertEqual(self.fixture_before, _tree_digest(FIXTURE_REPO))
        if self.bundle_before is not None:
            self.assertEqual(
                self.bundle_before, (os.stat(BUNDLE).st_size, os.stat(BUNDLE).st_mtime_ns)
            )

    def test_an_untracked_entry_is_not_added(self):
        before = _git(REPO, "--no-optional-locks", "status", "--porcelain").stdout
        validation.run_and_record(
            self.plan, self.root, self.review,
            tempfile.mkdtemp(prefix="p55-evidence-"), ordinal=1,
            which=lambda name: None,
        )
        self.assertEqual(
            before, _git(REPO, "--no-optional-locks", "status", "--porcelain").stdout
        )


# -- boundaries -------------------------------------------------------------


def _imported(source: str):
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
    _MODULES = ("validation_policy", "validation_plan", "validation", "validation_cli")

    def _source(self, module: str) -> str:
        with open(os.path.join(SRC, "hrca", module + ".py"), encoding="utf-8") as fh:
            return fh.read()

    def test_no_module_imports_a_forbidden_seam(self):
        for module in self._MODULES:
            modules, _calls = _imported(self._source(module))
            for name in sorted(modules):
                with self.subTest(module=module, imported=name):
                    self.assertNotIn(name, _FORBIDDEN_IMPORTS)
                    self.assertFalse(name.startswith("PySide6"))

    def test_no_module_can_start_a_process_except_the_dispatch_seam(self):
        # Only the dispatch module may reach subprocess, and only to hand it to
        # the accepted runner's own injectable spawn.
        for module in self._MODULES:
            modules, _calls = _imported(self._source(module))
            if module == "validation":
                self.assertIn("subprocess", modules)
            else:
                with self.subTest(module=module):
                    self.assertNotIn("subprocess", modules)

    def test_no_module_evaluates_a_string(self):
        for module in self._MODULES:
            _modules, calls = _imported(self._source(module))
            for forbidden in ("exec", "eval", "compile", "__import__"):
                with self.subTest(module=module, call=forbidden):
                    self.assertNotIn(forbidden, calls)

    def test_only_the_dispatch_module_touches_a_filesystem(self):
        for module in ("validation_policy", "validation_plan"):
            modules, _calls = _imported(self._source(module))
            with self.subTest(module=module):
                self.assertNotIn("os", modules)
                self.assertNotIn("tempfile", modules)

    def test_no_protocol_action_was_added(self):
        self.assertEqual("3.9.0", contract.CONTRACT_VERSION)
        self.assertEqual(61, len(contract.ALLOWED_ACTIONS))
        self.assertFalse([a for a in contract.ALLOWED_ACTIONS if "validation" in a])

    def test_every_schema_is_the_one_the_task_froze(self):
        self.assertEqual("1.1.0", scanner.SCHEMA_VERSION)
        self.assertEqual("1.0.0", twin.TWIN_SCHEMA_VERSION)
        self.assertEqual("1.0.0", intent_delta.INTENT_DELTA_SCHEMA_VERSION)
        self.assertEqual("1.0.0", impact_proposal.IMPACT_SCHEMA_VERSION)
        self.assertEqual("1.0.0", candidate.CANDIDATE_SCHEMA_VERSION)
        self.assertEqual("1.0.0", validation_policy.POLICY_VERSION)
        self.assertEqual("1.0.0", validation_plan.VALIDATION_PLAN_SCHEMA_VERSION)
        self.assertEqual("1.0.0", validation.VALIDATION_ATTEMPT_SCHEMA_VERSION)
        self.assertEqual("1.0.0", validation.VALIDATION_RESULT_SCHEMA_VERSION)

    def test_the_runner_is_used_through_its_own_public_seam(self):
        # The contract constructs the accepted runner with its documented
        # injectable spawn; it adds no runner abstraction of its own.
        with open(os.path.join(SRC, "hrca", "validation.py"), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("container_runner.ContainerRunner(", source)
        self.assertIn("runner.preflight()", source)
        self.assertIn("runner.run(", source)


# -- privacy ---------------------------------------------------------------


class PrivacyTests(_Fixture, unittest.TestCase):
    def _passing(self):
        base = tempfile.mkdtemp(prefix="p55-evidence-")
        result, error = validation.run_and_record(
            self.plan, self.root, self.review, base, ordinal=1,
            spawn=FakeDocker({check_id: "ok" for check_id in self.corpus["check_order"]}),
            which=lambda name: "/usr/bin/docker",
        )
        assert error is None, error
        return result, base

    def test_no_attempt_carries_an_absolute_path_or_environment_fact(self):
        result, base = self._passing()
        rendered = validation.dumps(result)
        for forbidden in (
            sys.executable,
            sys.prefix,
            os.path.expanduser("~"),
            os.path.abspath(self.root),
            os.path.abspath(base),
            os.path.abspath(REPO),
        ):
            if not forbidden:
                continue
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_the_reviewable_argv_redacts_the_staged_locations(self):
        result, _base = self._passing()
        for attempt in result["attempts"]:
            with self.subTest(check=attempt["check_id"]):
                self.assertEqual("docker", attempt["argv"][0])
                self.assertEqual("<container>", attempt["argv"][3])
                mounts = [
                    attempt["argv"][index + 1]
                    for index, element in enumerate(attempt["argv"])
                    if element == "--mount"
                ]
                self.assertEqual(2, len(mounts))
                for mount in mounts:
                    self.assertIn("src=<staged>", mount)
                    self.assertNotIn("/tmp", mount)

    def test_no_stored_evidence_carries_a_staged_location(self):
        _result, base = self._passing()
        attempts, _error = validation.read_attempts(base)
        rendered = validation.dumps(attempts)
        self.assertNotIn(os.path.abspath(self.root), rendered)
        self.assertNotIn("/tmp/hrca-", rendered)

    def test_no_source_body_reaches_the_evidence(self):
        result, _base = self._passing()
        rendered = validation.dumps(result)
        with open(os.path.join(FIXTURE_REPO, "pkg", "service.py"), encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                line = line.strip()
                if len(line) < 12:
                    continue
                with self.subTest(line=line):
                    self.assertNotIn(line, rendered)

    def test_every_refusal_reason_is_bounded_and_path_free(self):
        reasons = [
            validation.REASON_PLAN_INVALID,
            validation.REASON_CANDIDATE_MISSING,
            validation.REASON_CANDIDATE_MISMATCH,
            validation.REASON_MANIFEST_INVALID,
            validation.REASON_REVIEW_INVALID,
            validation.REASON_ROOT_HAS_EXTRA_ENTRIES,
            validation.REASON_FILE_MISSING,
            validation.REASON_FILE_NOT_REGULAR,
            validation.REASON_FILE_LINKED,
            validation.REASON_FILE_SIZE,
            validation.REASON_FILE_HASH,
            validation.REASON_POLICY_INVALID,
            validation.REASON_EVIDENCE_UNUSABLE,
            validation.REASON_EVIDENCE_TAMPERED,
            validation.REASON_SHELL_STRING,
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertNotIn("/", reason)
                self.assertNotIn("\\", reason)
                self.assertLess(len(reason), 200)

    def test_a_shell_string_is_not_echoed_back(self):
        marker = "AKIA-EXAMPLE rm -rf /"
        _result, error = validation.run_plan(marker, self.root, self.review)
        self.assertEqual(validation.REASON_SHELL_STRING, error)
        self.assertNotIn(marker, error)


# -- the offline CLI --------------------------------------------------------


class CliTests(_Fixture, unittest.TestCase):
    """The CLI, exercised with no runtime.

    The CLI builds a real runner, so on a host with a live daemon these tests
    would really dispatch containers. They therefore assume the unavailable
    runtime explicitly: a unit module must not begin executing containers
    because a daemon appeared. Real dispatch belongs to the dedicated,
    skip-guarded integration module.
    """

    def _no_runtime(self):
        return mock.patch.object(
            container_runner.ContainerRunner,
            "preflight",
            return_value={
                "available": False,
                "reason": container_runner.PREFLIGHT_RUNTIME_UNAVAILABLE,
                "checks": {"docker": False, "daemon": False, "image": False},
            },
        )

    def _files(self, sandbox: str):
        review_path = os.path.join(sandbox, "review.json")
        with open(review_path, "w", encoding="utf-8") as fh:
            json.dump(self.review, fh)
        return review_path

    def test_plan_prints_the_product_owned_plan(self):
        with tempfile.TemporaryDirectory() as sandbox:
            review = self._files(sandbox)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = validation_cli.main(
                    ["plan", "--candidate", self.root, "--review", review]
                )
        self.assertEqual(0, code)
        plan = json.loads(out.getvalue())
        self.assertIsNone(validation_plan.validate_plan(plan))
        self.assertEqual(
            self.corpus["check_order"],
            [record["check_id"] for record in plan["checks"]],
        )

    def test_run_appends_the_evidence_and_reports_non_passing_without_a_runtime(self):
        with tempfile.TemporaryDirectory() as sandbox:
            review = self._files(sandbox)
            base = os.path.join(sandbox, "evidence")
            out, err = io.StringIO(), io.StringIO()
            with self._no_runtime():
                with redirect_stdout(out), redirect_stderr(err):
                    code = validation_cli.main(
                        ["run", "--candidate", self.root, "--review", review,
                         "--evidence-base", base]
                    )
            self.assertEqual(validation_cli.EXIT_NOT_PASSING, code)
            result = json.loads(out.getvalue())
            self.assertEqual("unavailable", result["state"])
            attempts, error = validation.read_attempts(base)
            self.assertIsNone(error)
            self.assertEqual(3, len(attempts))
            self.assertEqual(
                ["attempts", "index.jsonl"], sorted(os.listdir(base))
            )

    def test_verify_summarizes_the_store(self):
        with tempfile.TemporaryDirectory() as sandbox:
            review = self._files(sandbox)
            base = os.path.join(sandbox, "evidence")
            out = io.StringIO()
            with self._no_runtime():
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    validation_cli.main(
                        ["run", "--candidate", self.root, "--review", review,
                         "--evidence-base", base]
                    )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = validation_cli.main(["verify", "--evidence-base", base])
        self.assertEqual(0, code)
        summary = json.loads(out.getvalue())
        self.assertEqual(3, summary["attempts"])
        self.assertIs(False, summary["all_passed"])

    def test_a_supplied_plan_that_names_another_candidate_is_refused(self):
        with tempfile.TemporaryDirectory() as sandbox:
            review = self._files(sandbox)
            other = copy.deepcopy(self.binding)
            other["candidate_id"] = "candidate:" + "0" * 64
            plan, error = validation_plan.build_plan(other)
            self.assertIsNone(error)
            plan_path = os.path.join(sandbox, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(plan, fh)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = validation_cli.main(
                    ["run", "--candidate", self.root, "--review", review,
                     "--evidence-base", os.path.join(sandbox, "ev"),
                     "--plan", plan_path]
                )
        self.assertEqual(validation_cli.EXIT_NOT_PASSING, code)
        self.assertIn("different candidate", err.getvalue())

    def test_a_supplied_plan_matching_the_candidate_is_accepted(self):
        with tempfile.TemporaryDirectory() as sandbox:
            review = self._files(sandbox)
            plan_path = os.path.join(sandbox, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump(self.plan, fh)
            out, err = io.StringIO(), io.StringIO()
            with self._no_runtime(), redirect_stdout(out), redirect_stderr(err):
                code = validation_cli.main(
                    ["run", "--candidate", self.root, "--review", review,
                     "--evidence-base", os.path.join(sandbox, "ev"),
                     "--plan", plan_path]
                )
        self.assertEqual(validation_cli.EXIT_NOT_PASSING, code)
        self.assertEqual("unavailable", json.loads(out.getvalue())["state"])

    def test_a_missing_file_is_a_usage_failure(self):
        with tempfile.TemporaryDirectory() as sandbox:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = validation_cli.main(
                    ["plan", "--candidate", self.root,
                     "--review", os.path.join(sandbox, "absent.json")]
                )
        self.assertEqual(validation_cli.EXIT_USAGE, code)
        self.assertNotIn(sandbox, err.getvalue())

    def test_an_unknown_check_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as sandbox:
            review = self._files(sandbox)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = validation_cli.main(
                    ["plan", "--candidate", self.root, "--review", review,
                     "--check", "check:not_a_check"]
                )
        self.assertEqual(validation_cli.EXIT_NOT_PASSING, code)
        self.assertIn(validation_policy.REASON_UNKNOWN_CHECK, err.getvalue())


if __name__ == "__main__":
    unittest.main()
