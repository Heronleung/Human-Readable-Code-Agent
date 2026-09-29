"""Compatibility shim: ``hrca.setup_verification`` -> ``hrca.cli.setup_verification``.

Retained because documented `hrca.setup_verification` surface and its `-m` refusal. This module holds no logic, no state and no I/O.

Importing it makes it *be* the implementation, so ``from hrca import setup_verification``
and ``mock.patch.object`` reach the real object rather than a copy of its
names. Running it with ``-m`` re-runs the implementation as ``__main__``,
because ``runpy`` executes this shim itself as ``__main__`` and an alias
performed at that point would leave nothing to run.
"""

from __future__ import annotations

import runpy
import sys

_IMPL = "hrca.cli.setup_verification"

if __name__ == "__main__":
    runpy.run_module(_IMPL, run_name="__main__")
else:
    from .cli import setup_verification as _impl

    sys.modules[__name__] = _impl
