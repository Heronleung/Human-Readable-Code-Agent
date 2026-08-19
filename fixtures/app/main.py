"""Application entry point."""

import os
from app.service import Service

APP_NAME = "human-readable-code-agent"


class MainService(Service):
    """Top-level service that wraps the base service."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self) -> str:
        service = Service()
        return service.handle(self.name)


def print_hi(name: str) -> None:
    print(f"Hi, {name}")


if __name__ == "__main__":
    print_hi(APP_NAME)
