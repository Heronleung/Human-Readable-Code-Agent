"""Command-line interface for the hrca scanner."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .scanner import scan_directory


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the scanner and write canonical JSON to stdout. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="hrca-scan",
        description="Scan a Python source tree and emit canonical JSON records.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="directory to scan (default: current directory)",
    )
    args = parser.parse_args(argv)

    doc = scan_directory(args.root)
    json.dump(doc, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0
