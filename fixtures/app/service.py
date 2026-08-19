"""Service layer."""


class Service:
    """Base service."""

    def __init__(self):
        self.running = False

    def handle(self, name: str) -> str:
        return name.upper()

    def start(self):
        self.running = True

    def fail(self):
        raise RuntimeError("boom")

    @staticmethod
    def version() -> str:
        return "1.0"
