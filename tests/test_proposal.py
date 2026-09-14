"""Tests for the deterministic Proposal-Package domain (P4.1).

These tests exercise :mod:`hrca.proposal` — the first Phase 4 bridge from a
validated Intent Delta to a typed, non-applied proposal package — against a
synthetic Code Map baseline and a synthetic Twin store, so the state machine
(``ready`` / ``clarification_required`` / ``unsupported`` / ``no_change`` /
``blocked``), source grounding, determinism and no-side-effect guarantees can be
driven precisely without touching a repository.
"""

from __future__ import annotations

import os
import unittest

from hrca import codemap, codemap_draft, proposal, scanner, twin

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "codemap_fixtures"
)

_WS = "ws:test"


def _build(name: str, rev: str = "rev-1"):
    path = os.path.join(_FIXTURE_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    module = scanner.module_name_for(name)
    return codemap.build_codemap(source, name, module, rev)


def _baseline(name: str = "calculator.py", rev: str = "rev-1"):
    return codemap_draft.baseline_document(_build(name, rev), rev)


def _block_id(name: str, block_type: str, substr: str = "", rev: str = "rev-1"):
    for block in _build(name, rev):
        if block["block_type"] == block_type and (not substr or substr in block["block_id"]):
            return block["block_id"]
    raise AssertionError(f"no {block_type} block matching {substr!r}")


def _purpose_add(rev: str = "rev-1"):
    return _block_id("calculator.py", codemap.BT_PURPOSE, "calculator.add", rev)


def _store(gen: int = 1):
    return {
        "workspace_revision": {"scan_generation": gen},
        "artifacts": [
            {"locator": "calculator.add", "path": "calculator.py", "kind": "function"},
            {"locator": "calculator.divide", "path": "calculator.py", "kind": "function"},
            {
                "locator": "procedural.Service.handle",
                "path": "procedural.py",
                "kind": "method",
            },
        ],
    }


def _draft(operations, name="calculator.py", ws=_WS):
    baseline = _baseline(name)
    return codemap_draft.build_draft(ws, baseline, operations, "C", "U")[0]


def _proposal_for(operations, name="calculator.py", store=None):
    draft = _draft(operations, name)
    baseline = _baseline(name)
    delta, err = codemap_draft.generate_intent_delta(draft, baseline)
    assert err is None, err
    return proposal.build_proposal(delta, baseline, store or _store())


class BuildProposalTests(unittest.TestCase):
    def test_documentation_intent_is_ready(self):
        package = _proposal_for(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "Add two floats and return their sum."}]
        )
        self.assertEqual(package["state"], "ready")
        self.assertIsNone(package["reason"])
        self.assertFalse(package["executable"])
        self.assertFalse(package["applied"])
        self.assertTrue(package["proposal_id"].startswith("proposal:"))
        self.assertEqual(package["confidence"], "high")
        self.assertEqual(package["target_scope"]["entities"], ["calculator.add"])
        self.assertEqual(package["target_scope"]["artifacts"], ["calculator.py"])
        self.assertEqual(len(package["plan_steps"]), 1)
        self.assertFalse(package["plan_steps"][0]["requires_approval"])

    def test_ready_package_is_fully_structured(self):
        package = _proposal_for(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "p"}]
        )
        for field in (
            "schema_version",
            "generator",
            "state",
            "executable",
            "applied",
            "intent_delta_id",
            "draft_id",
            "workspace_id",
            "baseline",
            "target_scope",
            "affected_artifacts",
            "preserved_constraints",
            "assumptions",
            "clarifications",
            "plan_steps",
            "risks",
            "validation_plan",
            "confidence",
            "proposal_id",
        ):
            self.assertIn(field, package, field)

    def test_ready_package_validates_clean(self):
        package = _proposal_for(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "p"}]
        )
        self.assertIsNone(proposal.validate_proposal(package))

    def test_behavior_with_outgoing_calls_needs_clarification(self):
        # ``procedural.Service.handle`` calls other methods, so a behavior
        # intent on it cannot be bounded without a human answer.
        handle_purpose = _block_id("procedural.py", codemap.BT_PURPOSE, "Service.handle")
        package = _proposal_for(
            [{"op": "mark_unresolved", "target_block_id": handle_purpose, "reason": "review"}],
            name="procedural.py",
        )
        self.assertEqual(package["state"], "clarification_required")
        self.assertEqual(package["reason"], "ambiguous behavior intent")
        self.assertEqual(package["plan_steps"], [])
        self.assertTrue(package["clarifications"])

    def test_leaf_behavior_is_ready(self):
        # ``calculator.divide`` is a leaf with no outgoing calls, so a behavior
        # intent on it is bounded and yields a ready plan.
        divide_decision = _block_id("calculator.py", codemap.BT_DECISION, "calculator.divide")
        package = _proposal_for(
            [{"op": "replace_condition_intent", "target_block_id": divide_decision,
              "proposed_condition": "right != 0"}]
        )
        self.assertEqual(package["state"], "ready")
        self.assertEqual(package["plan_steps"][0]["requires_approval"], True)

    def test_unknown_owning_entity_is_unsupported(self):
        package = _proposal_for(
            [{"op": "insert_block", "owning_entity_id": "does.not.exist",
              "block_type": "note", "proposed_text": "a note"}]
        )
        self.assertEqual(package["state"], "unsupported")
        self.assertEqual(package["reason"], "unsupported target scope")
        self.assertEqual(package["target_scope"], {"entities": [], "artifacts": []})
        self.assertEqual(package["plan_steps"], [])

    def test_package_is_deterministic(self):
        operations = [
            {"op": "replace_description", "target_block_id": _purpose_add(), "proposed_text": "p"}
        ]
        a = _proposal_for(operations)
        b = _proposal_for(operations)
        self.assertEqual(proposal.dumps(a), proposal.dumps(b))
        self.assertEqual(a["proposal_id"], b["proposal_id"])


class PlanProposalTests(unittest.TestCase):
    def test_noop_draft_is_no_change(self):
        draft = _draft([])
        result, err = proposal.plan_proposal(draft, _baseline(), _store())
        self.assertIsNone(result)
        self.assertEqual(err, proposal.REASON_NO_CHANGE)

    def test_stale_draft_is_blocked(self):
        draft = _draft(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "p"}]
        )
        result, err = proposal.plan_proposal(draft, _baseline("calculator.py", "rev-2"), _store())
        self.assertIsNone(result)
        self.assertEqual(err, proposal.REASON_STALE)

    def test_valid_draft_yields_ready_package(self):
        draft = _draft(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "p"}]
        )
        result, err = proposal.plan_proposal(draft, _baseline(), _store())
        self.assertIsNone(err)
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "ready")


class ValidateProposalTests(unittest.TestCase):
    def _ready(self):
        return _proposal_for(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "p"}]
        )

    def test_non_mapping_rejected(self):
        self.assertIsNotNone(proposal.validate_proposal([]))

    def test_unsupported_schema_version_rejected(self):
        package = self._ready()
        package["schema_version"] = "0.0.0"
        self.assertEqual(proposal.validate_proposal(package), "unsupported schema_version")

    def test_unknown_state_rejected(self):
        package = self._ready()
        package["state"] = "not-a-state"
        self.assertEqual(proposal.validate_proposal(package), "unknown proposal state")

    def test_executable_package_rejected(self):
        package = self._ready()
        package["executable"] = True
        self.assertEqual(proposal.validate_proposal(package), "proposal must be non-executable")

    def test_applied_package_rejected(self):
        package = self._ready()
        package["applied"] = True
        self.assertEqual(proposal.validate_proposal(package), "proposal must be non-applied")

    def test_missing_proposal_id_rejected(self):
        package = self._ready()
        del package["proposal_id"]
        self.assertEqual(
            proposal.validate_proposal(package), "missing or malformed proposal_id"
        )

    def test_unordered_steps_rejected(self):
        package = self._ready()
        # Duplicate the step so its index no longer matches its position.
        package["plan_steps"].append(dict(package["plan_steps"][0]))
        self.assertEqual(proposal.validate_proposal(package), "plan steps are not ordered")


class NoSideEffectTests(unittest.TestCase):
    def test_planning_does_not_mutate_inputs(self):
        draft = _draft(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "p"}]
        )
        baseline = _baseline()
        store = _store()
        draft_before = codemap_draft.dumps(draft)
        baseline_before = codemap.dumps(baseline)
        store_before = proposal.dumps(store)
        proposal.plan_proposal(draft, baseline, store)
        self.assertEqual(codemap_draft.dumps(draft), draft_before)
        self.assertEqual(codemap.dumps(baseline), baseline_before)
        self.assertEqual(proposal.dumps(store), store_before)

    def test_proposal_never_references_source_text(self):
        package = _proposal_for(
            [{"op": "replace_description", "target_block_id": _purpose_add(),
              "proposed_text": "Add two floats"}]
        )
        serialized = proposal.dumps(package)
        # The package never embeds verified source body, only bounded identifiers.
        self.assertNotIn("return", serialized)
        self.assertIsNotNone(package["proposal_id"])


if __name__ == "__main__":
    unittest.main()
