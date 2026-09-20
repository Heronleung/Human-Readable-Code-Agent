"""The exact canonical unified-style diff (P5.4).

A review artifact is only useful if a reader can predict it, so the renderer is
held to a plain, documented unified form: `a/` and `b/` headers carrying the
exact repository-relative path, three lines of context, and terminator-free
lines whose final-newline state is recorded separately rather than inferred.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

from hrca import candidate_diff

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))
FIXTURE = os.path.join(REPO, "candidate_fixtures", "repo")
MANIFEST = os.path.join(REPO, "candidate_fixtures", "manifest.json")


def _corpus() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def _before() -> str:
    with open(os.path.join(FIXTURE, "pkg", "service.py"), encoding="utf-8") as fh:
        return fh.read()


class RenderTests(unittest.TestCase):
    def test_an_unchanged_pair_renders_as_an_empty_diff(self):
        text = "a\nb\n"
        self.assertEqual(([], None), candidate_diff.render_unified("p.txt", text, text))

    def test_the_supported_case_matches_the_pinned_oracle(self):
        corpus = _corpus()
        lines, reason = candidate_diff.render_unified(
            "pkg/service.py", _before(), corpus["result"]["text"]
        )
        self.assertIsNone(reason)
        self.assertEqual(corpus["diff_lines"], lines)

    def test_the_headers_carry_the_exact_relative_path_and_nothing_else(self):
        lines, _reason = candidate_diff.render_unified("pkg/service.py", "a\n", "b\n")
        self.assertEqual("--- a/pkg/service.py", lines[0])
        self.assertEqual("+++ b/pkg/service.py", lines[1])
        for line in lines:
            with self.subTest(line=line):
                self.assertNotIn(os.sep + "home", line)
                self.assertNotIn(":", line.split("@@")[0][:3])

    def test_the_hunk_header_states_both_ranges(self):
        lines, _reason = candidate_diff.render_unified(
            "x.py", "one\ntwo\nthree\n", "one\nTWO\nthree\n"
        )
        headers = [line for line in lines if line.startswith("@@")]
        self.assertEqual(1, len(headers))
        self.assertEqual("@@ -1,3 +1,3 @@", headers[0])

    def _body(self, lines):
        return [line for line in lines if not line.startswith(("---", "+++", "@@"))]

    def test_an_empty_side_is_all_additions_or_all_removals(self):
        added, _reason = candidate_diff.render_unified("x.py", "", "a\nb\n")
        self.assertIn("+a", added)
        self.assertIn("+b", added)
        self.assertFalse([line for line in self._body(added) if line.startswith("-")])

        removed, _reason = candidate_diff.render_unified("x.py", "a\nb\n", "")
        self.assertIn("-a", removed)
        self.assertFalse([line for line in self._body(removed) if line.startswith("+")])

    def test_context_is_the_documented_three_lines(self):
        self.assertEqual(3, candidate_diff.CONTEXT_LINES)
        body = "".join("line%d\n" % index for index in range(12))
        changed = body.replace("line6\n", "CHANGED\n")
        lines, _reason = candidate_diff.render_unified("x.py", body, changed)
        # Three before, one removed, one added, three after.
        self.assertEqual(8, len(self._body(lines)))

    def test_a_diff_past_the_bound_is_refused_not_truncated(self):
        before = "".join("line%d\n" % index for index in range(20))
        after = "".join("other%d\n" % index for index in range(20))
        lines, reason = candidate_diff.render_unified("x.py", before, after, max_lines=5)
        self.assertIsNone(lines)
        self.assertEqual(candidate_diff.REASON_DIFF_TOO_LARGE, reason)

    def test_the_same_pair_renders_byte_for_byte(self):
        first, _reason = candidate_diff.render_unified("pkg/service.py", _before(), "VERSION = '2'\n")
        second, _reason = candidate_diff.render_unified("pkg/service.py", _before(), "VERSION = '2'\n")
        self.assertEqual(first, second)


class NewlineTests(unittest.TestCase):
    def test_a_missing_final_newline_is_recorded_rather_than_implied(self):
        self.assertTrue(candidate_diff.final_newline("a\n"))
        self.assertTrue(candidate_diff.final_newline(""))
        self.assertFalse(candidate_diff.final_newline("a"))
        # The diff of a missing terminator is otherwise identical, so the flag is
        # what makes the two cases distinguishable.
        with_flag, _reason = candidate_diff.render_unified("x.py", "a\n", "b\n")
        without, _reason = candidate_diff.render_unified("x.py", "a", "b")
        self.assertEqual(
            [line for line in with_flag if not line.startswith(("---", "+++"))],
            [line for line in without if not line.startswith(("---", "+++"))],
        )
        self.assertFalse(candidate_diff.final_newline("b"))

    def test_the_line_count_matches_the_renderer(self):
        self.assertEqual(0, candidate_diff.line_count(""))
        self.assertEqual(1, candidate_diff.line_count("a"))
        self.assertEqual(2, candidate_diff.line_count("a\nb\n"))
        self.assertEqual(2, candidate_diff.line_count("a\nb"))


class PrivacyTests(unittest.TestCase):
    def test_no_rendered_line_carries_an_environment_value(self):
        lines, _reason = candidate_diff.render_unified("pkg/service.py", _before(), "VERSION = '2'\n")
        rendered = "\n".join(lines)
        for forbidden in (sys.executable, sys.prefix, os.path.expanduser("~"), REPO):
            if not forbidden:
                continue
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, rendered)

    def test_the_bound_is_the_contract_s_own(self):
        self.assertEqual(2000, candidate_diff.MAX_DIFF_LINES)
        self.assertEqual("1.0.0", candidate_diff.CANDIDATE_DIFF_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
