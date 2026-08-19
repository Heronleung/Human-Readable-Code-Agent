"""Demonstrates a dynamic import that cannot be statically resolved."""

import importlib


def load_module(module_name: str):
    return importlib.import_module(module_name)
