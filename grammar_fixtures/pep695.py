"""A module only a Python 3.12-or-later grammar can parse.

PEP 695 introduced type-parameter syntax (``def f[T]``, ``class C[T]``) and the
``type`` statement. A grammar that predates it cannot read this file at all, so
it is a genuine ``SyntaxError`` there — not a defect in this source and not a
defect in the scanner. The grammar context in the scan document is what tells
the two apart.
"""

type Alias = int


def identity[T](value: T) -> T:
    return value


class Box[T]:
    def get(self) -> T:
        ...
