"""A module every grammar this scanner supports can parse.

This is the control case for the grammar-context differential: its records must
be identical under every reported grammar, because nothing in it depends on the
grammar that reads it.
"""


def add(left, right):
    return left + right


class Adder:
    def run(self, value):
        return add(value, 1)
