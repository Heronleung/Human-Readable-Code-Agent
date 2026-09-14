"""In-image runner entrypoint (P4.3).

Baked into the reviewed ``hrca-runner:v1`` image alongside
``runtime_handlers.py``. It reads the staged input (a single JSON object with a
``handler`` name and a ``form`` mapping), resolves the handler against the
code-owned registry, and writes either ``{"result": ...}`` or ``{"error": ...}``
to the output path.

This module performs no filesystem access beyond the two fixed paths given on
the command line, no network access, and no credential access. It is trusted,
code-owned, and never derived from a package manifest.
"""

from __future__ import annotations

import json
import sys
from typing import Sequence

import runtime_handlers  # type: ignore  # baked into the image


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        return 2
    input_path, output_path = argv[1], argv[2]
    try:
        with open(input_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return 2
    if not isinstance(payload, dict):
        return 2
    handler_id = payload.get("handler")
    form = payload.get("input")
    if not isinstance(handler_id, str) or not isinstance(form, dict):
        return 2
    parameters = payload.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        return 2
    function = runtime_handlers.resolve_handler(handler_id)
    if function is None:
        return 2
    result, error = function(form, parameters)
    out = {"result": result} if error is None else {"error": error}
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=True, separators=(",", ":"))
    except OSError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
