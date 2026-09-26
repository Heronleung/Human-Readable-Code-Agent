"""The mutation matrix: each check the coordinator must not be able to skip.

The oracle in ``test_source_apply`` states what a correct coordinator answers.
This module states the other half: that each of the eight ways a coordinator
could be made *unsafe* is detected, and that the detection changes the answer in
the required direction while leaving the source byte-unchanged.

A "detection" here means the coordinator's own verdict moves — the case fails if
the check is removed, because the answer would then be a write where a refusal
is required, or an applied state where the bytes say otherwise. Each case also
asserts the target's bytes, which is the property that actually matters.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

from hrca.authoring import source_apply

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


class MutationTests(unittest.TestCase):
    maxDiff = None

    def _world(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return WORLD.build(root)

    def _plan(self, world):
        return source_apply.plan_application(world.evidence, world.apply_root)

    _ABSENT = object()

    def _apply(self, world, **kwargs):
        """Apply with the world's own receipt unless a caller says otherwise.

        ``receipt=None`` means *no receipt at all*, which is why the default is
        a sentinel rather than ``None``: the difference between "an absent
        receipt" and "the caller forgot to pass one" is the whole point of the
        missing-approval case.
        """
        receipt = kwargs.pop("receipt", self._ABSENT)
        receipt_bytes = kwargs.pop("receipt_bytes", self._ABSENT)
        base = kwargs.pop("base", world.custody)
        if receipt is self._ABSENT:
            receipt = world.receipt
        if receipt_bytes is self._ABSENT:
            receipt_bytes = world.receipt_bytes if receipt is not None else None
        return source_apply.apply_application(
            world.evidence, world.apply_root, base, receipt, receipt_bytes
        )

    def _patch_replace(self, mode):
        patcher = mock.patch.object(
            source_apply, "_replace_target", new=self._stand_in(mode)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stand_in(self, mode):
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

    def _assert_no_writes(self, world):
        self.assertFalse(os.path.exists(world.pre_image_path()))
        self.assertEqual(sorted(os.listdir(world.custody)), [])
        self.assertFalse(os.path.isdir(world.record_dir()))

    # 1 — a skipped predecessor verification
    def test_a_drifted_predecessor_is_detected_and_nothing_is_written(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "rewrite_target"})
        drifted = WORLD.target_sha256(world)
        record, reason = self._plan(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(record["reason_code"], source_apply.STALE_PREDECESSOR)
        self.assertEqual(WORLD.target_sha256(world), drifted)
        applied, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(applied["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(applied["reason_code"], source_apply.STALE_PREDECESSOR)
        self.assertEqual(WORLD.target_sha256(world), drifted)
        self._assert_no_writes(world)

    # 2 — a substituted candidate hash
    def test_a_substituted_candidate_hash_is_detected(self):
        world = self._world()
        WORLD.apply_mutation(
            world,
            {
                "kind": "set_evidence",
                "path": "target.candidate_sha256",
                "value": "ab" * 32,
            },
        )
        record, reason = self._plan(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(record["reason_code"], source_apply.STALE_CANDIDATE)
        self.assertEqual(WORLD.target_sha256(world), world.predecessor_sha256)
        self._assert_no_writes(world)

    # 3 — an ignored approval requirement
    def test_an_ignored_approval_check_leaves_the_source_alone(self):
        world = self._world()
        before = WORLD.target_sha256(world)
        record, reason = self._apply(world, receipt=None)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_MISSING_APPROVAL)
        self.assertEqual(WORLD.target_sha256(world), before)
        self._assert_no_writes(world)

    def test_a_receipt_for_other_bytes_does_not_authorize_this_one(self):
        world = self._world()
        before = WORLD.target_sha256(world)
        world.receipt["candidate_sha256"] = "cd" * 32
        record, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(record["reason_code"], source_apply.STALE_RECEIPT)
        self.assertEqual(WORLD.target_sha256(world), before)
        self._assert_no_writes(world)

    # 4 — a widened target path
    def test_a_widened_target_path_is_refused(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "widen_path_absolute"})
        _record, reason = self._plan(world)
        self.assertEqual(reason, "absolute paths are not accepted")
        self.assertEqual(WORLD.target_sha256(world), world.predecessor_sha256)
        self._assert_no_writes(world)

    def test_a_path_the_proposal_never_scoped_is_refused(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "unauthorized_path"})
        _record, reason = self._plan(world)
        self.assertEqual(
            reason, "the bound proposal does not authorize that exact path"
        )
        self.assertEqual(WORLD.target_sha256(world), world.predecessor_sha256)
        self._assert_no_writes(world)

    # 5 — an inverted post-write report
    def test_a_replace_that_reports_success_but_wrote_the_predecessor_is_not_a_success(self):
        world = self._world()
        self._patch_replace("predecessor")
        record, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_NOT_EFFECTIVE)
        self.assertIs(record["applied"], False)
        # The observation is the bytes on disk, not the replacement's claim.
        self.assertEqual(
            record["observation"]["outcome"], source_apply.OUTCOME_PREDECESSOR
        )
        self.assertEqual(
            record["observation"]["sha256"], world.predecessor_sha256
        )
        self.assertEqual(WORLD.target_sha256(world), world.predecessor_sha256)

    # 6 — an unsafe action over unknown bytes
    def test_unknown_post_write_bytes_require_recovery_and_restore_nothing(self):
        world = self._world()
        self._patch_replace("neither")
        record, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_RECOVERY_REQUIRED)
        self.assertIs(record["applied"], False)
        self.assertEqual(
            record["observation"]["outcome"], source_apply.OUTCOME_NEITHER
        )
        self.assertEqual(record["recovery"]["action"], "none")
        # Nothing was restored: the unknown bytes are exactly as observed.
        observed = WORLD.target_sha256(world)
        self.assertNotEqual(observed, world.predecessor_sha256)
        self.assertNotEqual(observed, world.candidate_sha256)
        self.assertEqual(observed, record["observation"]["sha256"])
        self.assertTrue(os.path.exists(world.pre_image_path()))

    def test_a_failed_replacement_is_unknown_and_the_target_is_untouched(self):
        world = self._world()
        before = WORLD.target_sha256(world)
        self._patch_replace("fail")
        record, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_UNKNOWN)
        self.assertIs(record["write_surface"]["target_written"], False)
        self.assertEqual(WORLD.target_sha256(world), before)

    # 7 — a root identity substitution
    def test_a_substituted_root_is_refused_and_its_tree_is_untouched(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "point_root_at_other"})
        other_before = WORLD.target_sha256(world, world.other)
        self.assertEqual(other_before, world.predecessor_sha256)
        record, reason = self._plan(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(record["reason_code"], source_apply.STALE_ROOT_IDENTITY)
        applied, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(applied["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(applied["reason_code"], source_apply.STALE_ROOT_IDENTITY)
        # The other directory holds the same relative file with the same bytes,
        # and it was not touched: the root identity is what refused it.
        self.assertEqual(WORLD.target_sha256(world, world.other), other_before)
        self.assertFalse(os.path.exists(world.pre_image_path()))

    def test_a_moved_root_is_refused(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "move_project_root"})
        record, reason = self._plan(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(record["reason_code"], source_apply.STALE_ROOT_IDENTITY)

    # 8 — a receipt replay
    def test_a_replayed_receipt_is_refused_and_the_first_result_stands(self):
        """Two independent refusals stand between a receipt and a second apply.

        After a landed application the world has moved, so a replay is refused
        as stale before anything else is considered. And when the world has *not*
        moved — an attempt that created its pre-image and then failed before the
        replacement — the recovery path is taken, and the coordinator refuses
        rather than adopting or overwriting it. Neither path reaches a second
        target write.
        """
        world = self._world()
        first, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(first["state"], source_apply.STATE_APPLIED)
        self.assertEqual(WORLD.target_sha256(world), world.candidate_sha256)

        record, reason = self._apply(world)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_REFUSED_STALE)
        self.assertEqual(record["reason_code"], source_apply.STALE_PREDECESSOR)
        # The first application's artifacts are untouched and the target still
        # holds the candidate, not a second write.
        self.assertEqual(WORLD.target_sha256(world), world.candidate_sha256)
        self.assertEqual(
            WORLD.sha256_hex(world.read(world.pre_image_path())),
            world.predecessor_sha256,
        )
        self.assertEqual(len(os.listdir(world.record_dir())), 1)

    def test_a_replay_of_an_already_recorded_application_is_refused(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "record_claims_application"})
        before = WORLD.target_sha256(world)
        record, reason = self._apply(world)
        self.assertIsNone(record)
        self.assertEqual(reason, source_apply.REASON_APPLY_ALREADY_RECORDED)
        self.assertEqual(WORLD.target_sha256(world), before)
        self.assertFalse(os.path.exists(world.pre_image_path()))

    def test_an_existing_recovery_artifact_is_never_adopted_or_overwritten(self):
        world = self._world()
        WORLD.apply_mutation(world, {"kind": "pre_create_recovery_path"})
        standing = world.read(world.pre_image_path())
        before = WORLD.target_sha256(world)
        record, reason = self._apply(world)
        self.assertIsNone(record)
        self.assertEqual(reason, source_apply.REASON_RECOVERY_PATH_TAKEN)
        self.assertEqual(WORLD.target_sha256(world), before)
        self.assertEqual(world.read(world.pre_image_path()), standing)


if __name__ == "__main__":
    unittest.main()
