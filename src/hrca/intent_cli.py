"""Compatibility shim: ``hrca.intent_cli`` -> ``hrca.cli.intent_cli``.

Retained because documented `python -m hrca.intent_cli` operator entrypoint. This module holds no logic, no state and no I/O.

Importing it makes it *be* the implementation, so ``from hrca import intent_cli``
and ``mock.patch.object`` reach the real object rather than a copy of its
names. Running it with ``-m`` re-runs the implementation as ``__main__``,
because ``runpy`` executes this shim itself as ``__main__`` and an alias
performed at that point would leave nothing to run.
"""

from __future__ import annotations

import runpy
import sys

_IMPL = "hrca.cli.intent_cli"

if __name__ == "__main__":
    runpy.run_module(_IMPL, run_name="__main__")
else:
    from .cli import intent_cli as _impl

    sys.modules[__name__] = _impl
