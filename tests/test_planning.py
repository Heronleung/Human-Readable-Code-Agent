"""Tests for the deterministic task-intake and plan builder (P2.3)."""

from __future__ import annotations

import json
import os
import unittest

from hrca.planning import (
    PLAN_VERSION,
    TaskValidationError,
    build_plan,
    validate_task,
)
from hrca.report import build_report
from hrca.scanner import scan_directory

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures"))

BASE_SHA = "75967d5198126e42fddff99a9b9870ce53898613"

PLAN_ENTRY_KEYS = {"step", "action", "requires_approval", "expected_evidence"}

_REQUIRED_FIELDS = (
    "task_id",
    "title",
    "request",
    "repository_context",
    "allowed_actions",
    "constraints",
    "acceptance_criteria",
    "risk_level",
    "approval_required",
)


def _valid_task(**overrides) -> dict:
    """Return a valid read-only task, optionally overridden per-test."""
    task = {
        "task_id": "P2.3",
        "title": "Scan and analyze the fixture corpus",
        "request": "Read the fixture corpus and summarize its structure without modifying anything.",
        "repository_context": {
            "status": "Verified",
            "commit_sha": BASE_SHA,
            "branch": "main",
            "head_sha": BASE_SHA,
            "base_sha": BASE_SHA,
        },
        "allowed_actions": ["read", "analyze"],
        "constraints": ["Do not modify files", "Do not push"],
        "acceptance_criteria": [
            "Plan entries are deterministic",
            "No repository action is performed",
        ],
        "risk_level": "low",
        "approval_required": False,
    }
    task.update(overrides)
    return task


class PlanVersionTests(unittest.TestCase):
    def test_plan_version_is_one(self):
        self.assertEqual(PLAN_VERSION, 1)


class TaskValidationTests(unittest.TestCase):
    def test_missing_required_fields(self):
        for field in _REQUIRED_FIELDS:
            with self.subTest(field=field):
                task = _valid_task()
                del task[field]
                with self.assertRaises(TaskValidationError):
                    validate_task(task)

    def test_task_input_must_be_mapping(self):
        with self.assertRaises(TaskValidationError):
            validate_task(None)
        with self.assertRaises(TaskValidationError):
            validate_task(["not", "a", "mapping"])

    def test_invalid_repository_context(self):
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(repository_context="not-a-mapping"))

    def test_repository_context_missing_status(self):
        ctx = _valid_task()["repository_context"]
        del ctx["status"]
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(repository_context=ctx))

    def test_repository_context_invalid_status(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(repository_context={"status": "Confirmed", "commit_sha": BASE_SHA})
            )

    def test_valid_verified_repository_context(self):
        validate_task(
            _valid_task(
                repository_context={"status": "Verified", "branch": "main", "commit_sha": BASE_SHA}
            )
        )

    def test_verified_missing_branch_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(repository_context={"status": "Verified", "commit_sha": BASE_SHA})
            )

    def test_verified_missing_commit_sha_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(repository_context={"status": "Verified", "branch": "main"})
            )

    def test_verified_empty_commit_sha_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(
                    repository_context={"status": "Verified", "branch": "main", "commit_sha": "  "}
                )
            )

    def test_unverified_null_evidence_accepted(self):
        validate_task(
            _valid_task(
                repository_context={"status": "Unverified", "branch": None, "commit_sha": None}
            )
        )

    def test_unverified_omitted_evidence_accepted(self):
        validate_task(_valid_task(repository_context={"status": "Unverified"}))

    def test_unverified_supplied_evidence_valid(self):
        validate_task(
            _valid_task(
                repository_context={"status": "Unverified", "branch": "main", "commit_sha": BASE_SHA}
            )
        )

    def test_unverified_empty_evidence_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(
                    repository_context={"status": "Unverified", "branch": "", "commit_sha": None}
                )
            )

    def test_unverified_non_string_evidence_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(
                    repository_context={"status": "Unverified", "branch": None, "commit_sha": 42}
                )
            )

    def test_head_base_sha_do_not_substitute_verified_branch(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(
                    repository_context={
                        "status": "Verified",
                        "commit_sha": BASE_SHA,
                        "head_sha": BASE_SHA,
                        "base_sha": BASE_SHA,
                    }
                )
            )

    def test_head_base_sha_do_not_substitute_verified_commit_sha(self):
        with self.assertRaises(TaskValidationError):
            validate_task(
                _valid_task(
                    repository_context={
                        "status": "Verified",
                        "branch": "main",
                        "head_sha": BASE_SHA,
                        "base_sha": BASE_SHA,
                    }
                )
            )

    def test_invalid_action_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(allowed_actions=["read", "deploy"]))
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(allowed_actions=[]))
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(allowed_actions=["read", 3]))

    def test_invalid_risk_level_rejected(self):
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(risk_level="extreme"))

    def test_approval_required_must_be_boolean(self):
        with self.assertRaises(TaskValidationError):
            validate_task(_valid_task(approval_required="yes"))

    def test_non_string_required_fields_rejected(self):
        for field in ("task_id", "title", "request"):
            with self.subTest(field=field):
                with self.assertRaises(TaskValidationError):
                    validate_task(_valid_task(**{field: ""}))
                with self.assertRaises(TaskValidationError):
                    validate_task(_valid_task(**{field: 42}))


class PlanBuilderTests(unittest.TestCase):
    def test_read_only_plan_requires_no_approval(self):
        plan = build_plan(_valid_task())
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(entry["requires_approval"] is False for entry in plan))
        self.assertEqual([entry["step"] for entry in plan], [1, 2])
        self.assertEqual([entry["action"] for entry in plan], ["read", "analyze"])

    def test_plan_entries_have_p21_shape(self):
        for entry in build_plan(_valid_task()):
            self.assertEqual(set(entry), PLAN_ENTRY_KEYS)
            self.assertIsInstance(entry["step"], int)
            self.assertIsInstance(entry["action"], str)
            self.assertIsInstance(entry["requires_approval"], bool)
            self.assertIsInstance(entry["expected_evidence"], str)

    def test_approval_gated_actions(self):
        task = _valid_task(allowed_actions=["edit", "commit", "remote"])
        plan = build_plan(task)
        self.assertEqual(len(plan), 3)
        self.assertTrue(all(entry["requires_approval"] is True for entry in plan))

    def test_task_level_approval_gates_read_only_steps(self):
        task = _valid_task(approval_required=True)
        plan = build_plan(task)
        self.assertTrue(all(entry["requires_approval"] is True for entry in plan))

    def test_output_is_deterministic(self):
        task = _valid_task()
        first = json.dumps(build_plan(task), sort_keys=True)
        second = json.dumps(build_plan(task), sort_keys=True)
        self.assertEqual(first, second)


class FixtureBasedExampleTests(unittest.TestCase):
    def test_read_only_task_with_scanner_evidence(self):
        """A read-only task and scanner evidence yield a structured, no-op plan."""
        task = _valid_task()
        plan = build_plan(task)
        scanner_doc = scan_directory(FIXTURES)

        report = build_report(
            scanner_doc,
            {
                "task_id": task["task_id"],
                "plan": plan,
                "next_action": "Report only; no repository action performed.",
                "repository_context": task["repository_context"],
            },
        )

        self.assertEqual(report["plan"], plan)
        self.assertTrue(all(entry["requires_approval"] is False for entry in report["plan"]))
        # Scanner evidence is present and untouched by planning.
        self.assertEqual(report["validation"]["scanner_summary"]["files"], 6)
        self.assertEqual(report["outcome"]["status"], "no_change")
        self.assertEqual(report["outcome"]["changed_files"], [])


if __name__ == "__main__":
    unittest.main()
