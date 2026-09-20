"""Tiny service the P5.4 candidate fixture replaces."""

VERSION = "1"


class Service:
    def version(self) -> str:
        return VERSION


def ping() -> str:
    return VERSION
