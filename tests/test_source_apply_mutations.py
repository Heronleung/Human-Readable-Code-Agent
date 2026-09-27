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
import json
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

# A marker meaning "this key is absent", so a test can remove a field rather
# than set it to a value that merely looks empty.
_MISSING = object()


def _conform_receipt(world):
    """Complete the fixture's default receipt with the version it predates.

    The fixture world predates the receipt version gate, so its default receipt
    carries fourteen fields and no ``receipt_schema_version``. The harness
    completes it here rather than the fixture doing so, because a receipt is an
    operator-authored artifact and the harness stands in for the operator.
    Correcting the fixture default is one line, needs its own authorization, and
    is recorded as an outstanding item rather than taken silently.
    """
    world.receipt["receipt_schema_version"] = source_apply.RECEIPT_SCHEMA_VERSION
    world.receipt_bytes = (json.dumps(world.receipt, indent=1) + "\n").encode("utf-8")
    return world


def _receipt(world, **overrides):
    """Return a conformant receipt with the named fields overridden or removed."""
    receipt = dict(world.receipt)
    for key, value in overrides.items():
        if value is _MISSING:
            receipt.pop(key, None)
        else:
            receipt[key] = value
    return receipt


class MutationTests(unittest.TestCase):
    maxDiff = None

    def _world(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return _conform_receipt(WORLD.build(root))

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


class ReceiptBindingTests(unittest.TestCase):
    """The receipt's two shape gates, and the no-write property of each refusal.

    A receipt must declare the supported receipt schema version exactly, and its
    decision timestamp must be a strict RFC 3339 date-time with an explicit zone.
    Every failure is a *binding refusal*, raised before any write-capable path,
    that leaves the source and the custody base untouched — never a state, and
    never something the coordinator can round to missing approval or to a stale
    world. Neither gate authenticates anyone: they make the receipt's meaning
    checkable, and what is bound is the text supplied, unnormalized.
    """

    maxDiff = None

    def _world(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return _conform_receipt(WORLD.build(root))

    def _invoke(self, world, receipt):
        return source_apply.apply_application(
            world.evidence,
            world.apply_root,
            world.custody,
            receipt,
            json.dumps(receipt, indent=1).encode("utf-8"),
        )

    def _assert_untouched(self, world, before):
        """A refusal proves it wrote nothing, anywhere."""
        self.assertEqual(WORLD.target_sha256(world), before, "the target moved")
        self.assertFalse(os.path.exists(world.pre_image_path()), "a pre-image exists")
        self.assertFalse(os.path.isdir(world.record_dir()), "a record directory exists")
        self.assertEqual(sorted(os.listdir(world.custody)), [], "custody is not empty")
        for name in sorted(os.listdir(world.project)):
            self.assertFalse(
                name.startswith(source_apply.TEMP_PREFIX), "a temporary was left"
            )

    def _expect_refusal(self, world, receipt, token):
        before = WORLD.target_sha256(world)
        record, reason = self._invoke(world, receipt)
        self.assertIsNone(record, "expected a refusal, got a record")
        self.assertEqual(reason, token)
        self._assert_untouched(world, before)

    def _expect_applied(self, world, receipt):
        record, reason = self._invoke(world, receipt)
        self.assertIsNone(reason)
        self.assertEqual(record["state"], source_apply.STATE_APPLIED)
        self.assertEqual(WORLD.target_sha256(world), world.candidate_sha256)
        return record

    # -- success paths ------------------------------------------------------

    def test_an_exact_version_and_a_utc_timestamp_bind_and_apply(self):
        world = self._world()
        receipt = _receipt(
            world,
            receipt_schema_version="1.0.0",
            decided_at="2026-09-27T00:00:00Z",
        )
        record = self._expect_applied(world, receipt)
        acceptance = record["acceptance"]
        self.assertEqual(acceptance["decided_at"], "2026-09-27T00:00:00Z")
        self.assertEqual(acceptance["receipt_provenance"], "client_declared")
        self.assertTrue(acceptance["receipt_canonical_sha256"])
        self.assertTrue(acceptance["receipt_raw_sha256"])

    def test_an_explicit_offset_timestamp_binds_and_applies(self):
        for stamp in (
            "2026-09-27T08:30:00+08:00",
            "2026-09-27T00:00:00-05:30",
            "2026-09-27T00:00:00.500Z",
            "2026-09-27t00:00:00z",
        ):
            with self.subTest(stamp=stamp):
                world = self._world()
                record = self._expect_applied(world, _receipt(world, decided_at=stamp))
                self.assertEqual(record["acceptance"]["decided_at"], stamp)

    def test_a_valid_timestamp_is_bound_verbatim_and_never_normalized(self):
        """Two spellings of one instant are two receipts, not one.

        If the coordinator normalized the timestamp before hashing, these would
        share a canonical digest. They must not: the text validated is the text
        recorded, and the text hashed.
        """
        digests = []
        for stamp in ("2026-09-27T00:00:00Z", "2026-09-27T00:00:00+00:00"):
            world = self._world()
            record = self._expect_applied(world, _receipt(world, decided_at=stamp))
            self.assertEqual(record["acceptance"]["decided_at"], stamp)
            digests.append(record["acceptance"]["receipt_canonical_sha256"])
        self.assertNotEqual(digests[0], digests[1])

    # -- version refusals --------------------------------------------------

    def test_a_missing_version_refuses(self):
        world = self._world()
        self._expect_refusal(
            world,
            _receipt(world, receipt_schema_version=_MISSING),
            source_apply.REASON_RECEIPT_VERSION_UNSUPPORTED,
        )

    def test_a_non_string_or_blank_version_refuses(self):
        for value in (1, 1.0, True, None, [], {"version": "1.0.0"}, "", "   "):
            with self.subTest(value=value):
                world = self._world()
                self._expect_refusal(
                    world,
                    _receipt(world, receipt_schema_version=value),
                    source_apply.REASON_RECEIPT_VERSION_UNSUPPORTED,
                )

    def test_an_unsupported_or_future_version_refuses(self):
        for value in ("1.0.1", "1.1.0", "2.0.0", "9.9.9", "1.0", "1.0.0.0",
                      "1.0.0 ", " 1.0.0", "V1.0.0"):
            with self.subTest(value=value):
                world = self._world()
                self._expect_refusal(
                    world,
                    _receipt(world, receipt_schema_version=value),
                    source_apply.REASON_RECEIPT_VERSION_UNSUPPORTED,
                )

    # -- timestamp refusals ------------------------------------------------

    def test_a_naive_timestamp_refuses(self):
        for stamp in ("2026-09-27T00:00:00", "2026-09-27T00:00:00.500"):
            with self.subTest(stamp=stamp):
                world = self._world()
                self._expect_refusal(
                    world,
                    _receipt(world, decided_at=stamp),
                    source_apply.REASON_RECEIPT_TIMESTAMP_INVALID,
                )

    def test_a_date_only_timestamp_refuses(self):
        for stamp in ("2026-09-27", "2026-09-27Z", "20260927"):
            with self.subTest(stamp=stamp):
                world = self._world()
                self._expect_refusal(
                    world,
                    _receipt(world, decided_at=stamp),
                    source_apply.REASON_RECEIPT_TIMESTAMP_INVALID,
                )

    def test_malformed_calendar_offset_and_blank_timestamps_refuse(self):
        for stamp in (
            "2026-02-30T00:00:00Z",        # an impossible calendar date
            "2025-02-29T00:00:00Z",        # not a leap year
            "2026-13-01T00:00:00Z",        # an impossible month
            "2026-09-31T00:00:00Z",        # a month with no such day
            "2026-09-27T24:00:00Z",        # an impossible hour
            "2026-09-27T00:60:00Z",        # an impossible minute
            "2026-09-27T00:00:61Z",        # an impossible second
            "2026-09-27T00:00:00+25:00",   # an invalid offset hour
            "2026-09-27T00:00:00+00:60",   # an invalid offset minute
            "2026-09-27T00:00:00+0800",    # an offset with no separator
            "2026-09-27T00:00:00",         # naive, repeated for the subtest name
            "2026-09-27 00:00:00Z",        # a space where the T belongs
            "2026-09-27T00:00Z",           # no seconds
            "2026-09-27T00:00:00 UTC",     # a zone name is not an offset
            "   ",                         # whitespace only
            "not-a-timestamp",
        ):
            with self.subTest(stamp=stamp):
                world = self._world()
                self._expect_refusal(
                    world,
                    _receipt(world, decided_at=stamp),
                    source_apply.REASON_RECEIPT_TIMESTAMP_INVALID,
                )

    def test_an_absent_or_empty_timestamp_is_a_field_failure(self):
        """An absent field is a presence failure, not a format one.

        The distinction matters to a reader: nothing was supplied to parse, so
        the reason names the missing field rather than judging its shape.
        """
        world = self._world()
        before = WORLD.target_sha256(world)
        for value in (_MISSING, ""):
            with self.subTest(value=value):
                record, reason = self._invoke(
                    world, _receipt(world, decided_at=value)
                )
                self.assertIsNone(record)
                self.assertEqual(
                    reason, source_apply.REASON_RECEIPT_INVALID % "decided_at"
                )
        self._assert_untouched(world, before)

    def test_a_refusal_is_never_a_state(self):
        """Requirement C: never missing approval, never a stale world."""
        world = self._world()
        before = WORLD.target_sha256(world)
        record, reason = self._invoke(
            world, _receipt(world, decided_at="2026-09-27")
        )
        self.assertIsNone(record)
        self.assertNotIn(
            reason,
            (
                source_apply.REASON_REFUSED_MISSING_APPROVAL,
                source_apply.REASON_REFUSED_STALE,
            ),
        )
        self._assert_untouched(world, before)


if __name__ == "__main__":
    unittest.main()
