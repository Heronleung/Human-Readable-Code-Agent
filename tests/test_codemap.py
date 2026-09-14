"""Tests for the Code Map Procedural Language Standard 0.1 (P3.4).

These tests exercise :mod:`hrca.codemap` — the Qt-free, dependency-free
procedural block model, extractor and renderer — against the synthetic
``codemap_fixtures/`` corpus so block serialization, wording-independent
identity, the supported Python mapping and the explicit unsupported
(``limitation``) set can be driven precisely without a real repository.
"""

from __future__ import annotations

import os
import unittest

from hrca import codemap, scanner

_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "codemap_fixtures"
)

_REQUIRED_KEYS = {
    "block_id",
    "block_type",
    "parent_id",
    "order",
    "subject",
    "payload",
    "display_text",
    "source_anchors",
    "baseline_revision",
    "source_fingerprint",
    "provenance",
    "confidence",
    "confidence_reason",
    "editability",
    "state",
    "language_version",
}


def _build(name: str, rev: str = "rev-1"):
    path = os.path.join(_FIXTURE_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    module = scanner.module_name_for(name)
    return codemap.build_codemap(source, name, module, rev)


def _blocks_for(name: str, locator: str, rev: str = "rev-1"):
    return codemap.blocks_for_entity(_build(name, rev), locator)


# ---------------------------------------------------------------------------
# Group 1 — block model and deterministic rendering
# ---------------------------------------------------------------------------
class BlockModelTests(unittest.TestCase):
    def test_every_block_carries_the_required_keys(self):
        for name in ("calculator.py", "procedural.py"):
            for block in _build(name):
                self.assertTrue(_REQUIRED_KEYS <= set(block), block.get("block_id"))
                self.assertEqual(block["language_version"], codemap.CODEMAP_LANGUAGE_VERSION)

    def test_block_ids_are_stable_and_unique(self):
        blocks = _build("procedural.py")
        ids = [b["block_id"] for b in blocks]
        self.assertEqual(len(ids), len(set(ids)))
        for bid in ids:
            self.assertTrue(bid.startswith("codemap:"), bid)

    def test_rescan_is_byte_identical(self):
        a = _build("procedural.py")
        b = _build("procedural.py")
        self.assertEqual(codemap.dumps(a), codemap.dumps(b))

    def test_wording_change_does_not_change_fingerprint_or_id(self):
        block = _build("calculator.py")[0]
        fingerprint = block["source_fingerprint"]
        block_id = block["block_id"]
        altered = dict(block)
        altered["display_text"] = "completely different wording"
        altered["subject"] = "changed"
        self.assertEqual(codemap.fingerprint_block(altered), fingerprint)
        self.assertEqual(altered["block_id"], block_id)

    def test_all_non_note_block_types_are_produced(self):
        types = {b["block_type"] for b in _build("procedural.py")}
        for block_type in codemap.BLOCK_TYPES:
            if block_type == codemap.BT_NOTE:  # note is draft-only
                continue
            self.assertIn(block_type, types, block_type)

    def test_entity_list_is_compact_and_ordered(self):
        entities = codemap.entity_list(_build("procedural.py"))
        locators = [e["locator"] for e in entities]
        self.assertEqual(locators[0], "procedural")
        self.assertIn("procedural.Service.handle", locators)
        for entity in entities:
            for key in ("block_id", "locator", "kind", "name", "subject"):
                self.assertIn(key, entity)

    def test_entity_kinds_are_classified(self):
        entities = {e["locator"]: e for e in codemap.entity_list(_build("procedural.py"))}
        self.assertEqual(entities["procedural"]["kind"], "module")
        self.assertEqual(entities["procedural.Service"]["kind"], "class")
        self.assertEqual(entities["procedural.Service.handle"]["kind"], "method")
        self.assertEqual(entities["procedural.nothing"]["kind"], "async_function")
        self.assertEqual(entities["procedural.process"]["kind"], "function")

    def test_purpose_and_decision_editability(self):
        blocks = _build("calculator.py")
        purposes = [b for b in blocks if b["block_type"] == codemap.BT_PURPOSE]
        decisions = [b for b in blocks if b["block_type"] == codemap.BT_DECISION]
        self.assertTrue(purposes)
        self.assertTrue(decisions)
        self.assertTrue(all(p["editability"] == "replace_description" for p in purposes))
        self.assertTrue(all(d["editability"] == "replace_condition_intent" for d in decisions))

    def test_documented_purpose_is_source_authored(self):
        blocks = _build("calculator.py")
        add_purpose = next(
            b for b in blocks
            if b["block_type"] == codemap.BT_PURPOSE and "calculator.add" in b["block_id"]
        )
        self.assertEqual(add_purpose["provenance"], "source_authored")
        self.assertEqual(add_purpose["confidence"], "high")

    def test_module_entity_has_full_file_anchor(self):
        module = _build("calculator.py")[0]
        self.assertEqual(module["payload"]["kind"], "module")
        self.assertTrue(module["source_anchors"])
        self.assertEqual(module["source_anchors"][0]["lineno"], 1)

    def test_blocks_for_unknown_entity_is_empty(self):
        self.assertEqual(_blocks_for("calculator.py", "calculator.missing"), [])

    def test_dependency_and_call_targets(self):
        blocks = _build("procedural.py")
        self.assertIn("os", codemap.dependency_targets(blocks))
        self.assertIn("math", codemap.dependency_targets(blocks))
        self.assertTrue(codemap.call_targets(blocks))


# ---------------------------------------------------------------------------
# Group 2 — supported Python mapping over the procedural corpus
# ---------------------------------------------------------------------------
class MappingCorpusTests(unittest.TestCase):
    def test_procedural_corpus_renders_its_constructs(self):
        blocks = _build("procedural.py")
        text = codemap.render_blocks(blocks)
        for expected in (
            "Module procedural",
            "declares Class Service",
            "declares Async function nothing() -> None",
            "Imports os.",
            "Imports math.",
            "Imports typing.Optional.",
        ):
            self.assertIn(expected, text)

    def test_unsupported_construct_is_a_visible_limitation(self):
        sub = _blocks_for("procedural.py", "procedural.squared")
        text = codemap.render_blocks(sub)
        self.assertIn(
            "comprehension is not modeled and is reported as unresolved.", text
        )
        limitations = [b for b in sub if b["block_type"] == codemap.BT_LIMITATION]
        self.assertTrue(limitations)
        self.assertEqual(limitations[0]["confidence"], "low")
        self.assertEqual(limitations[0]["state"], "unsupported")
        self.assertIsNotNone(limitations[0]["confidence_reason"])

    def test_try_except_else_finally_and_with_are_rendered(self):
        sub = _blocks_for("procedural.py", "procedural.Service.handle")
        text = codemap.render_blocks(sub)
        self.assertIn("Handles KeyError:", text)
        self.assertIn("If no exception occurred:", text)
        self.assertIn("Always after the try:", text)
        self.assertIn("Calls self._cleanup.", text)
        self.assertIn("With open(os.path.join('tmp', 'log'), 'a'):", text)

    def test_loop_break_continue_and_augassign(self):
        sub = _blocks_for("procedural.py", "procedural.Service.handle")
        text = codemap.render_blocks(sub)
        self.assertIn("While attempts < retries:", text)
        self.assertIn("attempts is incremented by 1", text)
        self.assertIn("Breaks out of the loop.", text)
        self.assertIn("Continues to the next iteration.", text)

    def test_assert_is_an_invariant(self):
        sub = _blocks_for("procedural.py", "procedural.Service._run")
        invariants = [b for b in sub if b["block_type"] == codemap.BT_INVARIANT]
        self.assertTrue(invariants)
        self.assertIn("Asserts", codemap.render_blocks(sub))

    def test_async_for_renders_as_loop(self):
        sub = _blocks_for("procedural.py", "procedural.Service.refresh")
        text = codemap.render_blocks(sub)
        self.assertIn("For each item in self._items()", text)
        self.assertIn("(asynchronous)", text)

    def test_side_effect_mutation_renders_as_a_procedure_step(self):
        sub = _blocks_for("procedural.py", "procedural.Service._cleanup")
        text = codemap.render_blocks(sub)
        self.assertIn("Mutates self.max_items.", text)
        self.assertIn("Returns None.", text)

    def test_if_elif_else_renders_every_branch(self):
        source = (
            "def classify(n):\n"
            "    if n < 0:\n"
            "        return 'negative'\n"
            "    elif n == 0:\n"
            "        return 'zero'\n"
            "    else:\n"
            "        return 'positive'\n"
        )
        blocks = codemap.build_codemap(source, "classify.py", "classify", "rev-1")
        text = codemap.render_blocks(codemap.blocks_for_entity(blocks, "classify.classify"))
        self.assertIn("If n < 0 is true, the following runs:", text)
        self.assertIn("Otherwise, if n == 0:", text)
        self.assertIn("Otherwise:", text)
        self.assertIn("Returns 'negative'.", text)
        self.assertIn("Returns 'zero'.", text)
        self.assertIn("Returns 'positive'.", text)
        # An explicit ``else`` means there is no implicit fall-through step.
        self.assertNotIn("Otherwise, continue to the next step.", text)


# ---------------------------------------------------------------------------
# Group 3 — calculator regression
# ---------------------------------------------------------------------------
class CalculatorRegressionTests(unittest.TestCase):
    def test_add_renders_ordered_addition_and_return(self):
        sub = _blocks_for("calculator.py", "calculator.add")
        text = codemap.render_blocks(sub)
        self.assertIn("Function add(left: float, right: float) -> float", text)
        self.assertIn("total is assigned left + right", text)
        self.assertIn("Returns total.", text)

    def test_divide_renders_zero_check_value_error_and_division(self):
        sub = _blocks_for("calculator.py", "calculator.divide")
        text = codemap.render_blocks(sub)
        self.assertIn("Function divide(left: float, right: float) -> float", text)
        self.assertIn("If right == 0 is true, the following runs:", text)
        self.assertIn("Raises ValueError('division by zero') when right == 0.", text)
        self.assertIn("Otherwise, continue to the next step.", text)
        self.assertIn("result is assigned left / right", text)
        self.assertIn("Returns result.", text)

    def test_calculator_document_is_deterministic(self):
        a = codemap.render_blocks(_build("calculator.py"))
        b = codemap.render_blocks(_build("calculator.py"))
        self.assertEqual(a, b)

    def test_render_blocks_of_empty_is_empty(self):
        self.assertEqual(codemap.render_blocks([]), "")


if __name__ == "__main__":
    unittest.main()
