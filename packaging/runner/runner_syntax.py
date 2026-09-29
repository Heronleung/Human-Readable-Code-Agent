"""In-image candidate syntax-check entrypoint (P5.5a-r2).

Baked into the reviewed ``hrca-runner:v1`` image alongside ``runtime_handlers``
and ``runner_main``. It is the **only** thing in the image that reads the
candidate mount, and what it does with it is deliberately the least it could be:
it asks the interpreter whether each *explicitly declared* file parses.

What it will not do
-------------------

* **It never imports or executes candidate code.** ``compile`` builds a code
  object; it does not run one. There is no ``exec``, no ``eval``, no
  ``importlib``, no ``runpy``, no ``__import__`` and no ``py_compile`` here —
  ``py_compile`` would also *write* a ``.pyc``, and the root filesystem is
  read-only.
* **It never discovers files.** The list comes from the staged input and from
  nowhere else: no walking, no globbing, no extension sweep.
* **It never shells out.** No ``subprocess``, no ``os.system``, no command
  string from any source.
* **It never reports source text.** A syntax error contributes its message and
  position, never ``exc.text`` — a bounded diagnostic must not become a way to
  read a file back out of the container.
* **It never runs tests.** There is no test runner in this image and no path to
  one; the candidate's own tests are not executed here.

The entrypoint is listed literally in
:data:`hrca.container_runner.CANDIDATE_ENTRYPOINT`, so the argv a run actually
dispatches is a module constant rather than anything a plan, a candidate or
prose can reach.

This module performs no filesystem access beyond the two fixed paths given on
the command line and the fixed candidate mount below, no network access, and no
credential access.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Sequence

# The one fixed in-image location the candidate root is mounted at, read-only.
CANDIDATE_ROOT = "/candidate"

SCHEMA = "hrca-syntax-check/1"

# Bounded request and response. A check that cannot be bounded is refused, never
# truncated: half a syntax result is a wrong result.
MAX_FILES = 64
MAX_FILE_BYTES = 64 * 1024
MAX_PATH_CHARS = 512
MAX_ERROR_CHARS = 120

SUFFIXES = (".py", ".pyi")

# Bounded request reasons, mirroring the runner's ``error`` convention.
ERROR_NOT_MAPPING = "input is not a mapping"
ERROR_NO_FILES = "no files were declared"
ERROR_TOO_MANY_FILES = "too many files were declared"
ERROR_BAD_PATH = "a declared path is not an exact repository-relative path"
ERROR_UNREADABLE = "a declared file could not be read"
ERROR_OVERSIZED = "a declared file is larger than the accepted bound"


def _valid_path(value: Any) -> bool:
    """Return whether ``value`` is one exact repository-relative source path.

    Checked again here even though the host already checked it: the container
    must not depend on the host having been correct, and a mount that is ever
    wrong should still not let a path escape the fixed root.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        return False
    if value.startswith("/") or "\\" in value or ":" in value:
        return False
    if not value.endswith(SUFFIXES):
        return False
    for char in value:
        if ord(char) < 32 or ord(char) == 127:
            return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _read(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _check_one(rel_path: str) -> Dict[str, Any]:
    """Return one bounded outcome for one declared file."""
    outcome: Dict[str, Any] = {
        "path": rel_path,
        "ok": False,
        "error": None,
        "lineno": None,
        "offset": None,
    }
    target = CANDIDATE_ROOT + "/" + rel_path
    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        outcome["error"] = ERROR_UNREADABLE
        return outcome
    if len(data) > MAX_FILE_BYTES:
        outcome["error"] = ERROR_OVERSIZED
        return outcome
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        outcome["error"] = "a declared file is not valid UTF-8"
        return outcome

    try:
        # Compile only. This builds a code object and runs nothing.
        compile(source, rel_path, "exec")
    except SyntaxError as exc:
        outcome["error"] = str(exc.msg)[:MAX_ERROR_CHARS]
        outcome["lineno"] = exc.lineno if isinstance(exc.lineno, int) else None
        outcome["offset"] = exc.offset if isinstance(exc.offset, int) else None
        return outcome
    except ValueError:
        # ``compile`` rejects a NUL byte with ValueError rather than SyntaxError.
        outcome["error"] = "a declared file contains a NUL byte"
        return outcome
    outcome["ok"] = True
    return outcome


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        return 2
    input_path, output_path = argv[1], argv[2]

    # The staged shape is the runner's one staging convention, the same one
    # ``runner_main.py`` reads: ``{"handler": ..., "input": {...}}``. The
    # handler name is the entrypoint id and is ignored here; what matters is
    # the declared file list and nothing else.
    payload = _read(input_path)
    if not isinstance(payload, dict):
        result: Any = {"error": ERROR_NOT_MAPPING}
    else:
        body = payload.get("input")
        declared = body.get("files") if isinstance(body, dict) else None
        if not isinstance(declared, list) or not declared:
            result = {"error": ERROR_NO_FILES}
        elif len(declared) > MAX_FILES:
            result = {"error": ERROR_TOO_MANY_FILES}
        elif not all(_valid_path(item) for item in declared):
            result = {"error": ERROR_BAD_PATH}
        else:
            checked: List[Dict[str, Any]] = [
                _check_one(item) for item in sorted(set(declared))
            ]
            compiled = sum(1 for outcome in checked if outcome["ok"])
            result = {
                "result": {
                    "schema": SCHEMA,
                    "checked": checked,
                    "compiled": compiled,
                    "failed": len(checked) - compiled,
                }
            }
    try:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=True, separators=(",", ":"))
    except OSError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
