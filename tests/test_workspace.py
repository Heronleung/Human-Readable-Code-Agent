"""Tests for the read-only workspace filesystem policy (P3.2).

The workspace module is the boundary-side authority that filters, canonicalizes
and bounds the project tree and document surface. These tests build synthetic
trees under a temporary directory and assert the classification, full-tree
inclusion, containment, symlink, kind, size and determinism guarantees without
touching real project files.

Every ordinary file and folder under an accepted root is listed with a render
``kind`` (``source`` / ``preview`` / ``binary`` / ``unsupported``); only the
excluded directory names and symlinks are omitted. ``read_document`` returns a
``source`` / ``preview`` result for readable text and a bounded ``unavailable``
result for binary, unsupported, missing, unreadable or oversized files, while a
path that escapes the root remains a bounded ``ContractError``.
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
    os.makedirs(os.path.join(proj, "pkg", "sub"))
    os.makedirs(os.path.join(proj, ".git"))
    os.makedirs(os.path.join(proj, "__pycache__"))
    os.makedirs(os.path.join(proj, "node_modules"))
    _write(os.path.join(proj, "main.py"), "print('hi')\n")
    _write(os.path.join(proj, "README.md"), "# Readme\n")
    _write(os.path.join(proj, "pyproject.toml"), "[project]\n")
    _write(os.path.join(proj, "notes.txt"), "plain text\n")
    _write(os.path.join(proj, "data.json"), '{"k": 1}\n')
    _write(os.path.join(proj, "image.png"), "not really png")
    _write(os.path.join(proj, "blob.bin"), "\x00\x01\x02")
    _write(os.path.join(proj, "unknown.xyz"), "mystery")
    _write(os.path.join(proj, "pkg", "util.py"), "x = 1\n")
    _write(os.path.join(proj, "pkg", "sub", "helper.py"), "y = 2\n")
    _write(os.path.join(proj, ".git", "config"), "excluded\n")
    _write(os.path.join(proj, "__pycache__", "main.cpython-311.pyc"), "junk")
    _write(os.path.join(proj, "node_modules", "dep.js"), "excluded\n")
    return proj


class ClassifyFileTests(unittest.TestCase):
    def test_source_kinds(self):
        for name in ("main.py", "mod.pyi", "pyproject.toml", "README.md", "README.rst"):
            self.assertEqual(workspace.classify_file(name), "source", name)

    def test_preview_kinds(self):
        for name in (
            "notes.txt", "data.json", "README.markdown", "settings.yaml",
            "Dockerfile", "Makefile", ".gitignore", "conf.ini",
        ):
            self.assertEqual(workspace.classify_file(name), "preview", name)

    def test_binary_kinds(self):
        for name in ("image.png", "blob.bin", "main.pyc", "app.exe", "lib.so"):
            self.assertEqual(workspace.classify_file(name), "binary", name)

    def test_unsupported_kinds(self):
        for name in ("unknown.xyz", "noext", "data.qqq"):
            self.assertEqual(workspace.classify_file(name), "unsupported", name)

    def test_suffix_matching_is_case_insensitive(self):
        self.assertEqual(workspace.classify_file("Main.PY"), "source")


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
    def test_tree_lists_all_ordinary_entries_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            tree = workspace.build_tree(proj)
            self.assertEqual(tree["root"], os.path.realpath(proj))
            self.assertFalse(tree["truncated"])
            names = [n["name"] for n in tree["children"]]
            self.assertEqual(
                names,
                ["pkg", "README.md", "blob.bin", "data.json", "image.png",
                 "main.py", "notes.txt", "pyproject.toml", "unknown.xyz"],
            )
            pkg = tree["children"][0]
            self.assertEqual(pkg["type"], "dir")
            self.assertEqual(pkg["path"], "pkg")
            self.assertEqual([n["name"] for n in pkg["children"]], ["sub", "util.py"])

    def test_file_entries_carry_kind_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            tree = workspace.build_tree(proj)
            by_name = {n["name"]: n for n in tree["children"]}
            self.assertEqual(by_name["main.py"]["kind"], "source")
            self.assertEqual(by_name["data.json"]["kind"], "preview")
            self.assertEqual(by_name["image.png"]["kind"], "binary")
            self.assertEqual(by_name["unknown.xyz"]["kind"], "unsupported")
            self.assertEqual(by_name["main.py"]["type"], "file")
            self.assertGreater(by_name["main.py"]["size"], 0)

    def test_excluded_directories_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            tree = workspace.build_tree(proj)
            names = {n["name"] for n in tree["children"]}
            self.assertNotIn(".git", names)
            self.assertNotIn("__pycache__", names)
            self.assertNotIn("node_modules", names)

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

    def test_oversized_files_are_listed_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            _write(os.path.join(proj, "big.py"), "x" * (contract.MAX_DOCUMENT_BYTES + 1))
            tree = workspace.build_tree(proj)
            names = [n["name"] for n in tree["children"]]
            self.assertIn("big.py", names)
            big = next(n for n in tree["children"] if n["name"] == "big.py")
            self.assertEqual(big["kind"], "source")
            self.assertGreater(big["size"], contract.MAX_DOCUMENT_BYTES)


class ReadDocumentTests(unittest.TestCase):
    def test_reads_permitted_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "pkg/util.py")
            self.assertEqual(doc["path"], "pkg/util.py")
            self.assertEqual(doc["name"], "util.py")
            self.assertEqual(doc["kind"], "source")
            self.assertEqual(doc["content"], "x = 1\n")

    def test_reads_preview_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "data.json")
            self.assertEqual(doc["kind"], "preview")
            self.assertEqual(doc["content"], '{"k": 1}\n')

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

    def test_missing_path_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "nope.py")
            self.assertEqual(doc["kind"], "unavailable")
            self.assertEqual(doc["reason"], "path_not_found")

    def test_directory_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "pkg")
            self.assertEqual(doc["kind"], "unavailable")
            self.assertEqual(doc["reason"], "path_not_readable")

    def test_unsupported_type_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "unknown.xyz")
            self.assertEqual(doc["kind"], "unavailable")
            self.assertEqual(doc["reason"], "unsupported_type")

    def test_binary_file_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            doc = workspace.read_document(proj, "image.png")
            self.assertEqual(doc["kind"], "unavailable")
            self.assertEqual(doc["reason"], "binary")

    def test_text_file_with_nul_byte_returns_binary_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            _write(os.path.join(proj, "weird.txt"), "has\x00nul\n")
            doc = workspace.read_document(proj, "weird.txt")
            self.assertEqual(doc["kind"], "unavailable")
            self.assertEqual(doc["reason"], "binary")

    def test_oversized_document_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = _make_project(tmp)
            _write(os.path.join(proj, "big.py"), "x" * (contract.MAX_DOCUMENT_BYTES + 1))
            doc = workspace.read_document(proj, "big.py")
            self.assertEqual(doc["kind"], "unavailable")
            self.assertEqual(doc["reason"], "file_too_large")

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
