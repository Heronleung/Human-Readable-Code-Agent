"""A tiny arithmetic module used as a Code Map regression corpus.

``add`` demonstrates the ordered, deterministic rendering of a simple
computation; ``divide`` demonstrates a decision (a zero-check guard), an
exception and a return value.
"""


def add(left: float, right: float) -> float:
    """Return the sum of ``left`` and ``right``."""
    total = left + right
    return total


def divide(left: float, right: float) -> float:
    """Return the quotient of ``left`` divided by ``right``.

    Raises a ``ValueError`` when ``right`` is zero.
    """
    if right == 0:
        raise ValueError("division by zero")
    result = left / right
    return result
