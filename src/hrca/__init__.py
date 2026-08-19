"""hrca — Human-Readable Code Agent.

Phase 1 baseline: a deterministic static scanner for Python source trees,
built on the standard-library :mod:`ast` module. It emits canonical JSON
records for files, symbols, relations, parse errors, and confidence states.
"""

from __future__ import annotations

from .scanner import SCHEMA_VERSION, Scanner, scan_directory

__version__ = "0.1.0"

__all__ = ["SCHEMA_VERSION", "Scanner", "scan_directory", "__version__"]
