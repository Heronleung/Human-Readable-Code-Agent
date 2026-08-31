"""Deterministic static scanner for Python source trees (Phase 1 baseline).

The scanner parses Python files using the standard-library :mod:`ast` module
and emits canonical JSON records for:

* ``files``        — one record per scanned ``.py`` file.
* ``symbols``      — modules, classes, functions, async functions, parameters,
  and variables (assignments).
* ``relations``    — imports, calls, returns, raises, and inheritance bases.
* ``parse_errors`` — per-file ``SyntaxError`` records (the scan continues).
* ``confidence``   — explicit confidence states for non-high-confidence items.

Design invariants (see README for the full contract):

* **Deterministic** — records are sorted and key order is canonical, so
  identical rescans produce byte-identical output and stable identifiers.
* **No fabrication** — a relation is emitted only when source evidence exists,
  and its ``target`` is the literal name written in the source. Names are never
  resolved to definitions or file paths, so no call edge or import target is
  invented.
* **Explicitly unresolved** — dynamic imports (``importlib.import_module`` /
  ``__import__``) are emitted as ``imports`` relations with ``status``
  ``"unresolved"`` instead of being silently dropped or guessed.
"""

from __future__ import annotations

import ast
import os
from typing import Dict, Iterator, List, Optional, Set

SCHEMA_VERSION = "1.0.0"
GENERATOR = "hrca-scanner"

_CONF_HIGH = "high"
_CONF_LOW = "low"

# Directories that are never descended into.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".eggs",
        "htmlcov",
    }
)

# Python source suffixes the scanner models. ``.pyi`` type stubs are parsed
# with the same ``ast`` extractor and produce the same symbol/relation records.
_PY_SUFFIXES = (".py", ".pyi")


def module_name_for(rel_path: str) -> str:
    """Derive a dotted module name from a slash-separated relative path."""
    parts = rel_path.replace("\\", "/").split("/")
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts and parts[-1].endswith(".pyi"):
        parts[-1] = parts[-1][: -len(".pyi")]
    elif parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][: -len(".py")]
    return ".".join(part for part in parts if part)


def _src_range(node: Optional[ast.AST]) -> Optional[dict]:
    """Return a source range dict for an AST node, if the node has one."""
    if node is None or not hasattr(node, "lineno"):
        return None
    return {
        "lineno": node.lineno,
        "col_offset": node.col_offset,
        "end_lineno": getattr(node, "end_lineno", node.lineno),
        "end_col_offset": getattr(node, "end_col_offset", node.col_offset),
    }


def _render(node: Optional[ast.AST]) -> Optional[str]:
    """Return a canonical string for an expression node (via ``ast.unparse``)."""
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def _store_names(node: ast.AST) -> Iterator[ast.Name]:
    """Yield ``Name`` nodes used as assignment targets (unpacks tuples, etc.)."""
    if isinstance(node, ast.Name):
        yield node
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _store_names(elt)
    elif isinstance(node, ast.Starred):
        yield from _store_names(node.value)


class Scanner:
    """Scans a directory tree and produces a canonical record document."""

    def __init__(self, root: str):
        self._root_arg = root.replace("\\", "/")
        self._root = os.path.abspath(root)
        self._reset()

    # -- public API ------------------------------------------------------

    def scan(self) -> dict:
        """Scan ``root`` and return the canonical document as a dict."""
        self._reset()
        for rel_path in self._iter_python_files():
            self._scan_file(rel_path)
        return self.document()

    def document(self) -> dict:
        """Assemble the current state into a deterministically ordered dict."""
        files = sorted(self.files, key=lambda r: r["path"])
        symbols = sorted(self.symbols, key=lambda r: r["id"])
        relations = sorted(self.relations.values(), key=lambda r: r["id"])
        parse_errors = sorted(
            self.parse_errors,
            key=lambda r: (r["file"], r.get("lineno") or 0, r.get("col_offset") or 0),
        )
        confidence = sorted(
            self.confidence, key=lambda r: (r["item_type"], r["item_id"])
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "generator": GENERATOR,
            "root": self._root_arg,
            "files": files,
            "symbols": symbols,
            "relations": relations,
            "parse_errors": parse_errors,
            "confidence": confidence,
        }

    # -- internals -------------------------------------------------------

    def _reset(self) -> None:
        self.files: List[dict] = []
        self.symbols: List[dict] = []
        self.relations: Dict[str, dict] = {}
        self.parse_errors: List[dict] = []
        self.confidence: List[dict] = []
        self._symbol_ids: Set[str] = set()

    def _iter_python_files(self) -> List[str]:
        found: List[str] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(_PY_SUFFIXES):
                    full = os.path.join(dirpath, fn)
                    found.append(os.path.relpath(full, self._root).replace(os.sep, "/"))
        return sorted(found)

    def _scan_file(self, rel_path: str) -> None:
        full = os.path.join(self._root, rel_path)
        module_name = module_name_for(rel_path)
        try:
            size = os.path.getsize(full)
            with open(full, "r", encoding="utf-8") as fh:
                source = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            self.files.append(self._file(rel_path, module_name, None, "error"))
            self._parse_error(rel_path, None, None, f"could not read file: {exc}")
            return
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            self.files.append(self._file(rel_path, module_name, size, "error"))
            self._parse_error(rel_path, exc.lineno, exc.offset, exc.msg)
            return
        self.files.append(self._file(rel_path, module_name, size, "ok"))
        _Extractor(self, rel_path, module_name, source).extract(tree)

    # -- record builders -------------------------------------------------

    def _file(self, rel_path, module_name, size, syntax_status):
        return {
            "record_type": "file",
            "path": rel_path,
            "module": module_name,
            "size_bytes": size,
            "syntax_status": syntax_status,
        }

    def _parse_error(self, rel_path, lineno, col_offset, message):
        rec = {"record_type": "parse_error", "file": rel_path, "message": message}
        if lineno is not None:
            rec["lineno"] = lineno
        if col_offset is not None:
            rec["col_offset"] = col_offset
        self.parse_errors.append(rec)

    def _add_symbol(self, id_, kind, name, rel_path, parent_id, node=None, extra=None):
        if id_ in self._symbol_ids:
            return
        self._symbol_ids.add(id_)
        rec = {
            "record_type": "symbol",
            "id": id_,
            "kind": kind,
            "name": name,
            "file": rel_path,
            "confidence": _CONF_HIGH,
        }
        if parent_id is not None:
            rec["parent_id"] = parent_id
        rng = _src_range(node)
        if rng is not None:
            rec["source_range"] = rng
        if extra:
            for key, value in extra.items():
                if value not in (None, [], {}):
                    rec[key] = value
        self.symbols.append(rec)

    def _add_relation(
        self,
        kind,
        rel_path,
        source,
        target,
        node=None,
        *,
        status=None,
        confidence=_CONF_HIGH,
        **extra,
    ):
        rng = _src_range(node)
        lineno = rng["lineno"] if rng else 0
        col = rng["col_offset"] if rng else 0
        target_key = target if target is not None else "<unresolved>"
        id_ = f"{kind}:{source}->{target_key}@{lineno}:{col}"
        if id_ in self.relations:
            return None
        rec = {
            "record_type": "relation",
            "id": id_,
            "kind": kind,
            "file": rel_path,
            "source": source,
            "target": target,
            "confidence": confidence,
        }
        if status is not None:
            rec["status"] = status
        if rng is not None:
            rec["source_range"] = rng
        for key, value in extra.items():
            if value not in (None, [], {}):
                rec[key] = value
        self.relations[id_] = rec
        return id_

    def _note_confidence(self, item_type, item_id, confidence, reason):
        self.confidence.append(
            {
                "item_type": item_type,
                "item_id": item_id,
                "confidence": confidence,
                "reason": reason,
            }
        )


class _Extractor:
    """Walks one parsed module and emits symbols and relations."""

    def __init__(self, scanner: Scanner, rel_path: str, module_name: str, source: str):
        self.scanner = scanner
        self.rel_path = rel_path
        self.module_name = module_name
        self.source = source
        self.scope_stack: List[str] = [module_name]
        self.scope_kinds: List[str] = ["module"]
        self.func_stack: List[str] = []

    # -- scope helpers ---------------------------------------------------

    def _scope(self) -> str:
        return self.scope_stack[-1]

    def _current_function(self) -> Optional[str]:
        return self.func_stack[-1] if self.func_stack else None

    def _qname(self, parent_id: str, name: str) -> str:
        return f"{parent_id}.{name}"

    # -- entry point -----------------------------------------------------

    def extract(self, tree: ast.Module) -> None:
        lines = self.source.splitlines() or [""]
        end_lineno = len(lines)
        end_col_offset = len(lines[-1]) if lines else 0
        self.scanner._add_symbol(
            self.module_name,
            "module",
            self.module_name,
            self.rel_path,
            None,
            extra={
                "source_range": {
                    "lineno": 1,
                    "col_offset": 0,
                    "end_lineno": end_lineno,
                    "end_col_offset": end_col_offset,
                }
            },
        )
        for stmt in tree.body:
            self.visit(stmt)

    # -- visitor ---------------------------------------------------------

    def visit(self, node) -> None:
        if node is None:
            return
        node_type = type(node)
        if node_type is ast.ClassDef:
            self._class(node)
        elif node_type is ast.FunctionDef:
            self._function(node, is_async=False)
        elif node_type is ast.AsyncFunctionDef:
            self._function(node, is_async=True)
        elif node_type is ast.Assign:
            self._assign(node)
        elif node_type is ast.AnnAssign:
            self._annassign(node)
        elif node_type is ast.AugAssign:
            self._augassign(node)
        elif node_type is ast.Import:
            self._import(node)
        elif node_type is ast.ImportFrom:
            self._import_from(node)
        elif node_type is ast.Call:
            self._call(node)
        elif node_type is ast.Return:
            self._return(node)
        elif node_type is ast.Raise:
            self._raise(node)
        else:
            for child in ast.iter_child_nodes(node):
                self.visit(child)

    # -- definitions -----------------------------------------------------

    def _class(self, node: ast.ClassDef) -> None:
        class_id = self._qname(self._scope(), node.name)
        extra = {
            "decorators": [_render(d) for d in node.decorator_list] or None,
            "bases": [_render(b) for b in node.bases] or None,
        }
        self.scanner._add_symbol(
            class_id, "class", node.name, self.rel_path, self._scope(), node, extra
        )
        for base in node.bases:
            self.scanner._add_relation(
                "inherits", self.rel_path, class_id, _render(base), base, status="recorded"
            )
        self.scope_stack.append(class_id)
        self.scope_kinds.append("class")
        for stmt in node.body:
            self.visit(stmt)
        self.scope_kinds.pop()
        self.scope_stack.pop()

    def _function(self, node, is_async: bool) -> None:
        func_id = self._qname(self._scope(), node.name)
        kind = "async_function" if is_async else "function"
        is_method = self.scope_kinds[-1] == "class"
        extra = {
            "decorators": [_render(d) for d in node.decorator_list] or None,
            "async": True if is_async else None,
            "is_method": True if is_method else None,
            "return_annotation": _render(node.returns),
        }
        self.scanner._add_symbol(
            func_id, kind, node.name, self.rel_path, self._scope(), node, extra
        )
        self._parameters(func_id, node.args)
        self.scope_stack.append(func_id)
        self.scope_kinds.append("function")
        self.func_stack.append(func_id)
        for stmt in node.body:
            self.visit(stmt)
        self.func_stack.pop()
        self.scope_kinds.pop()
        self.scope_stack.pop()

    def _parameters(self, func_id: str, args: ast.arguments) -> None:
        groups = [
            (args.posonlyargs, "positional_only"),
            (args.args, "positional"),
            ([args.vararg] if args.vararg else [], "vararg"),
            (args.kwonlyargs, "keyword_only"),
            ([args.kwarg] if args.kwarg else [], "kwarg"),
        ]
        for params, param_kind in groups:
            for arg in params:
                param_id = self._qname(func_id, arg.arg)
                self.scanner._add_symbol(
                    param_id,
                    "parameter",
                    arg.arg,
                    self.rel_path,
                    func_id,
                    arg,
                    {"type_annotation": _render(arg.annotation), "param_kind": param_kind},
                )

    # -- statements ------------------------------------------------------

    def _assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for name_node in _store_names(target):
                self._variable(name_node)
        self.visit(node.value)

    def _annassign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._variable(node.target, _render(node.annotation))
        self.visit(node.value)

    def _augassign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Name):
            self._variable(node.target)
        self.visit(node.value)

    def _variable(self, name_node: ast.Name, annotation: Optional[str] = None) -> None:
        var_id = self._qname(self._scope(), name_node.id)
        extra = {"type_annotation": annotation} if annotation else None
        self.scanner._add_symbol(
            var_id, "variable", name_node.id, self.rel_path, self._scope(), name_node, extra
        )

    def _import(self, node: ast.Import) -> None:
        source = self._scope()
        for alias in node.names:
            self.scanner._add_relation(
                "imports",
                self.rel_path,
                source,
                alias.name,
                node,
                status="resolved",
                alias=alias.asname,
            )

    def _import_from(self, node: ast.ImportFrom) -> None:
        source = self._scope()
        module = self._resolve_import_from(node.level, node.module)
        if module is None:
            for alias in node.names:
                reason = f"relative import beyond top-level package (level={node.level})"
                rel_id = self.scanner._add_relation(
                    "imports",
                    self.rel_path,
                    source,
                    None,
                    node,
                    status="unresolved",
                    confidence=_CONF_LOW,
                    imported_name=alias.name,
                    alias=alias.asname,
                    reason=reason,
                )
                if rel_id:
                    self.scanner._note_confidence("relation", rel_id, _CONF_LOW, reason)
            return
        for alias in node.names:
            target = f"{module}.{alias.name}" if alias.name != "*" else f"{module}.*"
            self.scanner._add_relation(
                "imports",
                self.rel_path,
                source,
                target,
                node,
                status="resolved",
                imported_name=alias.name,
                alias=alias.asname,
            )

    def _resolve_import_from(self, level: int, module: Optional[str]) -> Optional[str]:
        if level == 0:
            return module
        parts = self.module_name.split(".")
        if level > len(parts) - 1:
            return None
        base = parts[: len(parts) - level]
        if module:
            base = base + module.split(".")
        return ".".join(base) if base else None

    # -- expressions -----------------------------------------------------

    def _call(self, node: ast.Call) -> None:
        source = self._current_function() or self._scope()
        target = _render(node.func)
        if self._is_dynamic_import(node):
            dynamic_target = None
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                node.args[0].value, str
            ):
                dynamic_target = node.args[0].value
            reason = "dynamic import: target module resolved at runtime"
            rel_id = self.scanner._add_relation(
                "imports",
                self.rel_path,
                source,
                dynamic_target,
                node,
                status="unresolved",
                confidence=_CONF_LOW,
                reason=reason,
            )
            if rel_id:
                self.scanner._note_confidence("relation", rel_id, _CONF_LOW, reason)
        self.scanner._add_relation("calls", self.rel_path, source, target, node, status="recorded")
        self.visit(node.func)
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _is_dynamic_import(self, node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Attribute):
            return (
                isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
                and func.attr == "import_module"
            )
        if isinstance(func, ast.Name):
            return func.id == "__import__"
        return False

    def _return(self, node: ast.Return) -> None:
        func = self._current_function()
        if func is not None:
            target = _render(node.value) if node.value is not None else None
            self.scanner._add_relation(
                "returns", self.rel_path, func, target, node, status="recorded"
            )
        self.visit(node.value)

    def _raise(self, node: ast.Raise) -> None:
        source = self._current_function() or self._scope()
        target = _render(node.exc) if node.exc is not None else None
        self.scanner._add_relation(
            "raises", self.rel_path, source, target, node, status="recorded"
        )
        self.visit(node.exc)
        self.visit(node.cause)


def scan_directory(root: str) -> dict:
    """Scan ``root`` and return the canonical record document as a dict."""
    return Scanner(root).scan()
