"""Exact canonical unified-style diff (P5.4).

A pure renderer: two pieces of UTF-8 text in, one bounded list of unified-style
lines out. It performs no filesystem access, no network call and no write, and
it imports only :mod:`difflib` from the standard library.

The rendering is deliberately the plain, documented unified form, because a
review artifact is only useful if a reader can predict it:

* ``--- a/<path>`` / ``+++ b/<path>`` with the exact repository-relative path
  the edit names, never an absolute location;
* ``@@ -<start>,<count> +<start>,<count> @@`` hunk headers, with
  :data:`CONTEXT_LINES` lines of context;
* one added (``+``), removed (``-``) or context (`` ``) line per source line.

Lines carry no line terminator. A file's final-newline state is not expressible
in a line list, so it is recorded *explicitly* on the operation the diff belongs
to (:func:`final_newline`) rather than being left for a reader to infer from a
convention marker. Content is therefore recoverable exactly from
``splitlines()`` plus that one flag, which is what makes the review artifact
checkable rather than merely readable.

The diff is bounded. A replacement whose rendering exceeds
:data:`MAX_DIFF_LINES` is refused rather than truncated: half a diff is not a
smaller review, it is a wrong one.
"""

from __future__ import annotations

import difflib
from typing import List, Optional, Tuple

CANDIDATE_DIFF_SCHEMA_VERSION = "1.0.0"
CANDIDATE_DIFF_GENERATOR = "hrca-candidate-diff"

CONTEXT_LINES = 3
MAX_DIFF_LINES = 2000

REASON_DIFF_TOO_LARGE = "the diff is larger than the accepted bound"

PREFIX_CONTEXT = " "
PREFIX_ADD = "+"
PREFIX_REMOVE = "-"


def final_newline(text: str) -> bool:
    """Return whether ``text`` ends with a line terminator (or is empty)."""
    return text == "" or text.endswith("\n")


def line_count(text: str) -> int:
    """Return the number of lines in ``text`` as the diff renderer counts them."""
    return len(text.splitlines())


def render_unified(
    path: str,
    before_text: str,
    after_text: str,
    max_lines: int = MAX_DIFF_LINES,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return ``(lines, reason)`` for the canonical diff of one exact path.

    ``lines`` is the unified rendering as a list of terminator-free strings, or
    ``None`` with a bounded reason when the rendering exceeds ``max_lines``. An
    unchanged pair renders as an empty list, which is how a no-op is recognised
    without a second comparison rule.
    """
    if before_text == after_text:
        return [], None
    lines = list(
        difflib.unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile="a/" + path,
            tofile="b/" + path,
            n=CONTEXT_LINES,
            lineterm="",
        )
    )
    if len(lines) > max_lines:
        return None, REASON_DIFF_TOO_LARGE
    return lines, None


__all__ = [
    "CANDIDATE_DIFF_SCHEMA_VERSION",
    "CANDIDATE_DIFF_GENERATOR",
    "CONTEXT_LINES",
    "MAX_DIFF_LINES",
    "REASON_DIFF_TOO_LARGE",
    "PREFIX_CONTEXT",
    "PREFIX_ADD",
    "PREFIX_REMOVE",
    "final_newline",
    "line_count",
    "render_unified",
]
