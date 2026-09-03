"""A synthetic module exercising the Code Map Procedural Language Standard 0.1.

Every supported block type and Python construct the Code Map models is present:
module/class/function/async-function/method definitions, parameters with
annotations and defaults, assignment and augmented assignment, ``return`` and
``raise``, ``if``/``elif``/``else``, ``for``/``async for``/``while``, calls,
``try``/``except``/``else``/``finally``, ``with``/``async with``, imports and
dependencies, ``assert``, and ``break``/``continue``/``pass``. One deliberately
unsupported construct (a list comprehension) is also present so the Code Map
renders a visible ``limitation`` block rather than guessing.
"""

import os
import math
from typing import Optional


def squared(values):
    """Return the same-length list of squared values via a comprehension.

    The comprehension is intentionally unsupported: the Code Map must report it
    as a limitation, never attempt to model it.
    """
    return [v * v for v in values]


class Service:
    """A small service class with one synchronous and one async method."""

    max_items = 10

    def handle(self, request: str, retries: int = 3) -> Optional[str]:
        """Process ``request``, retrying up to ``retries`` times."""
        attempts = 0
        while attempts < retries:
            attempts += 1
            if attempts >= self.max_items:
                break
            else:
                continue
        with open(os.path.join("tmp", "log"), "a") as handle_:
            handle_.write(request)
        try:
            return self._run(request)
        except KeyError:
            return None
        else:
            pass
        finally:
            self._cleanup()
        return None

    def _run(self, request: str) -> str:
        assert request != "", "request must not be empty"
        return request.strip()

    def _cleanup(self) -> None:
        self.max_items = 0
        return None

    async def refresh(self) -> None:
        """Refresh asynchronously over every item."""
        async for item in self._items():
            print(item)


def process(items, *, verbose: bool = False) -> int:
    """Walk ``items`` and report a bounded count."""
    count = 0
    for item in items:
        if verbose:
            print(item)
        count += 1
        pass
    if count == 0:
        raise RuntimeError("no items")
    return count


class Derived(Service):
    """A subclass that overrides one method."""

    def handle(self, request: str, retries: int = 3) -> Optional[str]:
        return super().handle(request, retries)


async def nothing() -> None:
    """An async function that does nothing."""
    return None
