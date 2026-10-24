"""Compatibility shim: ``hrca.live_batch_cli`` -> ``hrca.cli.live_batch_cli``.

Retained because the documented operator entrypoint is
``python -m hrca.live_batch_cli``. This module holds no logic, no state and no
I/O.

Importing it makes it *be* the implementation, so ``from hrca import
live_batch_cli`` and ``mock.patch.object`` reach the real object rather than a
copy of its names. Running it with ``-m`` re-runs the implementation as
``__main__``, because ``runpy`` executes this shim itself as ``__main__`` and an
alias performed at that point would leave nothing to run.
"""

from __future__ import annotations

import runpy
import sys

_IMPL = "hrca.cli.live_batch_cli"

if __name__ == "__main__":
    runpy.run_module(_IMPL, run_name="__main__")
else:
    from .cli import live_batch_cli as _impl

    sys.modules[__name__] = _impl
