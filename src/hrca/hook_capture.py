"""Compatibility shim: ``hrca.hook_capture`` -> ``hrca.memory.hook_capture``.

Retained because console script `hrca-hooks` and `python -m hrca.hook_capture`. This module holds no logic, no state and no I/O.

Importing it makes it *be* the implementation, so ``from hrca import hook_capture``
and ``mock.patch.object`` reach the real object rather than a copy of its
names. Running it with ``-m`` re-runs the implementation as ``__main__``,
because ``runpy`` executes this shim itself as ``__main__`` and an alias
performed at that point would leave nothing to run.
"""

from __future__ import annotations

import runpy
import sys

_IMPL = "hrca.memory.hook_capture"

if __name__ == "__main__":
    runpy.run_module(_IMPL, run_name="__main__")
else:
    from .memory import hook_capture as _impl

    sys.modules[__name__] = _impl
