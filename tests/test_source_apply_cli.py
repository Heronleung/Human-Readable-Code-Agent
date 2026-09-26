"""The offline operator adapter for the source application coordinator.

The adapter's whole job is to read two documents and hand the caller's roots
through, plus one thing only it may do: invoke the owner's candidate verifier,
whose verdict the coordinator binds and never re-derives. These tests hold it to
that, and to the exit codes an operator reads: ``0`` ready or applied, ``2``
refused, ``3`` a bounded non-success, and ``4`` for the one case an operator must
never miss — a write that landed but could not be recorded.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from hrca.authoring import source_apply
from hrca.cli import source_apply_cli as cli

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
FIXTURES = os.path.join(REPO, "apply_fixtures")


def _world_module():
    spec = importlib.util.spec_from_file_location(
        "apply_world", os.path.join(FIXTURES, "world.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WORLD = _world_module()


class CliTests(unittest.TestCase):
    maxDiff = None

    def _world(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        world = WORLD.build(root)
        evidence = os.path.join(root, "evidence.json")
        with open(evidence, "w", encoding="utf-8") as handle:
            json.dump(world.evidence, handle)
        receipt = os.path.join(root, "receipt.json")
        with open(receipt, "wb") as handle:
            handle.write(world.receipt_bytes)
        return world, root, evidence, receipt

    def _invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _plan(self, world, evidence):
        return self._invoke(
            ["--mode", "plan", "--evidence", evidence, "--apply-root", world.apply_root]
        )

    def _apply(self, world, evidence, receipt=None, base=None):
        argv = [
            "--mode", "apply",
            "--evidence", evidence,
            "--apply-root", world.apply_root,
        ]
        if receipt is not None:
            argv += ["--receipt", receipt]
        if base is not None:
            argv += ["--base", base]
        return self._invoke(argv)

    def test_a_plan_prints_a_ready_record_and_writes_nothing(self):
        world, root, evidence, _receipt = self._world()
        before = WORLD.target_sha256(world)
        code, out, err = self._plan(world, evidence)
        self.assertEqual(code, cli.EXIT_OK, err)
        record = json.loads(out)
        self.assertEqual(record["state"], source_apply.STATE_READY)
        self.assertIsNone(source_apply.validate_apply_record(record))
        self.assertEqual(WORLD.target_sha256(world), before)
        self.assertEqual(sorted(os.listdir(world.custody)), [])

    def test_a_plan_that_is_given_a_base_still_writes_nothing(self):
        world, root, evidence, _receipt = self._world()
        spare = os.path.join(root, "spare-base")
        os.makedirs(spare)
        code, _out, err = self._invoke(
            [
                "--mode", "plan",
                "--evidence", evidence,
                "--apply-root", world.apply_root,
                "--base", spare,
            ]
        )
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(sorted(os.listdir(spare)), [])

    def test_an_apply_without_a_base_is_refused(self):
        world, _root, evidence, receipt = self._world()
        code, _out, err = self._apply(world, evidence, receipt, None)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn(cli.REASON_BASE_REQUIRED, err)

    def test_an_apply_without_a_receipt_is_a_non_success_with_no_write(self):
        world, _root, evidence, _receipt = self._world()
        before = WORLD.target_sha256(world)
        code, out, err = self._apply(world, evidence, None, world.custody)
        self.assertEqual(code, cli.EXIT_NOT_SUCCESS, err)
        record = json.loads(out)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_MISSING_APPROVAL)
        self.assertEqual(WORLD.target_sha256(world), before)
        self.assertEqual(sorted(os.listdir(world.custody)), [])

    def test_an_apply_with_a_receipt_lands_the_bytes_and_records_them(self):
        world, _root, evidence, receipt = self._world()
        code, out, err = self._apply(world, evidence, receipt, world.custody)
        self.assertEqual(code, cli.EXIT_OK, err)
        record = json.loads(out)
        self.assertEqual(record["state"], source_apply.STATE_APPLIED)
        self.assertEqual(WORLD.target_sha256(world), world.candidate_sha256)
        self.assertTrue(os.path.exists(world.pre_image_path()))
        self.assertEqual(
            WORLD.sha256_hex(world.read(world.pre_image_path())),
            world.predecessor_sha256,
        )
        self.assertIs(record["record_persisted"], True)
        self.assertTrue(os.path.isdir(world.record_dir()))
        self.assertEqual(len(os.listdir(world.record_dir())), 1)
        acceptance = record["acceptance"]
        self.assertEqual(acceptance["receipt_provenance"], "client_declared")
        self.assertTrue(acceptance["receipt_canonical_sha256"])
        self.assertTrue(acceptance["receipt_raw_sha256"])
        self.assertNotEqual(
            acceptance["receipt_canonical_sha256"], acceptance["receipt_raw_sha256"]
        )

    def test_the_record_on_disk_matches_the_record_on_stdout(self):
        world, _root, evidence, receipt = self._world()
        code, out, err = self._apply(world, evidence, receipt, world.custody)
        self.assertEqual(code, cli.EXIT_OK, err)
        printed = json.loads(out)
        files = os.listdir(world.record_dir())
        self.assertEqual(len(files), 1)
        with open(os.path.join(world.record_dir(), files[0]), encoding="utf-8") as handle:
            stored = json.load(handle)
        body = printed["apply_id"].split(":", 1)[1]
        self.assertEqual(files[0], body + ".json")
        self.assertEqual(stored["apply_id"], printed["apply_id"])
        self.assertEqual(stored["state"], printed["state"])

    def test_an_unreadable_evidence_document_is_refused(self):
        world, root, _evidence, receipt = self._world()
        code, _out, err = self._plan(world, os.path.join(root, "absent.json"))
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn(cli.REASON_EVIDENCE_UNREADABLE, err)

    def test_an_unreadable_receipt_is_refused(self):
        world, root, evidence, _receipt = self._world()
        code, _out, err = self._apply(
            world, evidence, os.path.join(root, "absent-receipt.json"), world.custody
        )
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn(cli.REASON_RECEIPT_UNREADABLE, err)

    def test_the_adapter_verifies_the_candidate_and_refuses_a_tampered_one(self):
        world, _root, evidence, receipt = self._world()
        WORLD.apply_mutation(world, {"kind": "rewrite_candidate_file"})
        # The evidence the adapter reads still describes the untampered
        # candidate; only the bytes on disk moved.
        before = WORLD.target_sha256(world)
        code, _out, err = self._apply(world, evidence, receipt, world.custody)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn(cli.REASON_CANDIDATE_UNVERIFIED, err)
        self.assertEqual(WORLD.target_sha256(world), before)

    def test_the_adapter_replaces_any_supplied_binding(self):
        world, _root, evidence, receipt = self._world()
        with open(evidence, encoding="utf-8") as handle:
            document = json.load(handle)
        document["candidate"]["binding"] = {
            "candidate_id": "candidate:" + "0" * 64,
            "files": [],
        }
        with open(evidence, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        code, out, err = self._apply(world, evidence, receipt, world.custody)
        self.assertEqual(code, cli.EXIT_OK, err)
        record = json.loads(out)
        self.assertEqual(record["state"], source_apply.STATE_APPLIED)
        self.assertEqual(WORLD.target_sha256(world), world.candidate_sha256)

    def test_a_write_that_landed_but_could_not_be_recorded_reports_uncertainty(self):
        world, _root, evidence, receipt = self._world()
        # Make the record directory uncreatable by occupying the name with a file.
        os.makedirs(world.custody, exist_ok=True)
        with open(world.record_dir(), "w", encoding="utf-8") as handle:
            handle.write("not a directory\n")
        code, out, err = self._apply(world, evidence, receipt, world.custody)
        self.assertEqual(code, cli.EXIT_UNPERSISTED, err)
        record = json.loads(out)
        # The write did land, and the record says so while flagging that it was
        # not persisted: it must never fabricate success and never hide it.
        self.assertEqual(WORLD.target_sha256(world), world.candidate_sha256)
        self.assertIs(record["write_surface"]["target_written"], True)
        self.assertIs(record["record_persisted"], False)
        self.assertTrue(record["persistence_reason"])

    def test_the_cli_runs_no_process_and_imports_no_dispatching_module(self):
        with open(os.path.join(REPO, "src", "hrca", "cli", "source_apply_cli.py"), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("subprocess", "os.system", "Popen", "container_runner"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
