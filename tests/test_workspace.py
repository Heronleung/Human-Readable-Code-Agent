"""Tests for the read-only workspace filesystem policy (P3.2).

The workspace module is the boundary-side authority that filters, canonicalizes
and bounds the project tree and document surface. These tests build synthetic
trees under a temporary directory and assert the filter, containment, symlink,
extension, size and determinism guarantees without touching real project files.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from hrca import contract, workspace


def _write(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_project(base: str) -> str:
    """Build a small synthetic project under ``base``; return the project root."""
    proj = os.path.join(base, "proj")
    os.makedirs(os.path.join(proj, "pkg"))
    os.makedirs(os.path.join(proj, ".git"))
    os.makedirs(os.path.join(proj, "__pycache__"))
    _write(os.path.join(proj, "main.py"), "print('hi')\n")
    _write(os.path.join(proj, "README.md"), "# Readme\n")
    _write(os.path.join(proj, "pyproject.toml"), "[project]\n")
    _write(os.path.join(proj, "notes.txt"), "ignored by filter\n")
    _write(os.path.join(proj, "pkg", "util.py"), "x = 1\n")
    _write(os.path.join(proj, ".git", "config"), "excluded\n")
    _write(os.path.join(proj, "__pycache__", "main.cpython-311.pyc"), "junk")
    return proj


class FilterTests(unittest.TestCase):
    def test_included_files(self):
        for name in ("main.py", "mod.pyi", "pyproject.toml", "README.md", "README.rst"):
            self.assertTrue(workspace.is_included_file(name), name)

    def test_excluded_files(self):
        for name in ("notes.txt", ".gitignore", "makefile", "main.pyc", "Dockerfile"):
            self.assertFalse(workspace.is_included_file(name), name)

    def test_suffix_matching_is_case_insensitive(self):
        self.assertTrue(workspace.is_included_file("Main.PY"))


class ResolveRootTests(unittest.TestCase):
    def test_existing_directory_canonicalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            resolved = workspace.resolve_root(os.path.join(proj, "pkg"))
            self.assertEqual(resolved, os.path.realpath(os.path.join(proj, "pkg")))

    def test_missing_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.resolve_root(os.path.join(tmp, "nope"))
            self.assertEqual(ctx.exception.code, "path_not_found")

    def test_non_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "file.py")
            _write(f, "x = 1\n")
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.resolve_root(f)
            self.assertEqual(ctx.exception.code, "path_not_found")


class BuildTreeTests(unittest.TestCase):
    def test_filters_and_sorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            tree = workspace.build_tree(proj)
            self.assertEqual(tree["root"], os.path.realpath(proj))
            self.assertFalse(tree["truncated"])
            names = [n["name"] for n in tree["children"]]
            self.assertEqual(names, ["pkg", "README.md", "main.py", "pyproject.toml"])
            pkg = tree["children"][0]
            self.assertEqual(pkg["type"], "dir")
            self.assertEqual(pkg["path"], "pkg")
            self.assertEqual([n["name"] for n in pkg["children"]], ["util.py"])

    def test_excluded_directories_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            tree = workspace.build_tree(proj)
            names = {n["name"] for n in tree["children"]}
            self.assertNotIn(".git", names)
            self.assertNotIn("__pycache__", names)

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            self.assertEqual(workspace.build_tree(proj), workspace.build_tree(proj))

    def test_truncates_at_entry_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with mock.patch("hrca.contract.MAX_TREE_ENTRIES", 2):
                tree = workspace.build_tree(proj)
            self.assertTrue(tree["truncated"])

    def test_respects_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with mock.patch("hrca.contract.MAX_TREE_DEPTH", 1):
                tree = workspace.build_tree(proj)
            pkg = next(n for n in tree["children"] if n["name"] == "pkg")
            self.assertEqual(pkg["children"], [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinks_are_skipped_in_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            os.symlink(os.path.join(proj, "main.py"), os.path.join(proj, "link.py"))
            tree = workspace.build_tree(proj)
            names = [n["name"] for n in tree["children"]]
            self.assertNotIn("link.py", names)

    def test_oversized_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            _write(os.path.join(proj, "big.py"), "x" * (contract.MAX_FILE_BYTES + 1))
            tree = workspace.build_tree(proj)
            names = [n["name"] for n in tree["children"]]
            self.assertNotIn("big.py", names)


class ReadDocumentTests(unittest.TestCase):
    def test_reads_permitted_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "pkg/util.py")
            self.assertEqual(doc["path"], "pkg/util.py")
            self.assertEqual(doc["name"], "util.py")
            self.assertEqual(doc["content"], "x = 1\n")

    def test_absolute_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "/etc/passwd")
            self.assertEqual(ctx.exception.code, "path_not_allowed")

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "../secret.py")
            self.assertEqual(ctx.exception.code, "path_not_allowed")

    def test_excluded_directory_component_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, ".git/config")
            self.assertEqual(ctx.exception.code, "path_not_allowed")

    def test_missing_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "nope.py")
            self.assertEqual(ctx.exception.code, "path_not_found")

    def test_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "pkg")
            self.assertEqual(ctx.exception.code, "path_not_readable")

    def test_unsupported_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "notes.txt")
            self.assertEqual(ctx.exception.code, "unsupported_type")

    def test_oversized_document_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            _write(os.path.join(proj, "big.py"), "x" * (contract.MAX_DOCUMENT_BYTES + 1))
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "big.py")
            self.assertEqual(ctx.exception.code, "file_too_large")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.join(tmp, "proj")
            outside = os.path.join(tmp, "outside")
            os.makedirs(proj)
            os.makedirs(outside)
            _write(os.path.join(outside, "secret.py"), "secret\n")
            os.symlink(os.path.join(outside, "secret.py"), os.path.join(proj, "escape.py"))
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "escape.py")
            self.assertEqual(ctx.exception.code, "path_not_allowed")

    def test_error_never_echoes_requested_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            with self.assertRaises(contract.ContractError) as ctx:
                workspace.read_document(proj, "../very-secret-path.py")
            self.assertNotIn("very-secret-path", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
