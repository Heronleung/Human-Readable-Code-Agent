"""Architecture import-rule tests (P3.1).

A client module must never import the deterministic core (scanner, planner,
report builder), the provider protocol, Git tooling, or any command-execution
code. The client is a client only: it consumes the versioned contract, never
the core, and never decides that an action is permitted.

Import statements are inspected with :mod:`ast`, so prose references in
docstrings do not produce false positives.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src", "hrca"))

_CLIENT_PATH = os.path.join(_SRC, "client.py")

# Modules that are part of the client boundary and must therefore stay free of
# any core / provider / Git / command-execution import. ``hrca.style`` is
# included because it is the desktop-only visual layer and must own nothing but
# presentation tokens.
_CLIENT_MODULES = {
    "hrca.client": os.path.join(_SRC, "client.py"),
    "hrca.client_core": os.path.join(_SRC, "client_core.py"),
    "hrca.style": os.path.join(_SRC, "style.py"),
}

# The shared contract is Qt-free and must not import the core either.
_CONTRACT_MODULE = os.path.join(_SRC, "contract.py")

# The workspace policy is boundary-side: it may import stdlib and the contract,
# but never the deterministic core.
_WORKSPACE_MODULE = os.path.join(_SRC, "workspace.py")

# Top-level module names that a client or contract module must never import.
_FORBIDDEN_TOP_LEVEL = frozenset(
    {"scanner", "planning", "report", "provider", "subprocess", "git"}
)

# P4.2a provider/credential seam modules. A client must never import these:
# they own the fixed DeepSeek identity, the credential store (including the
# Win32 binding) and the non-secret configuration — backend infrastructure the
# desktop shell reaches only through the NDJSON boundary.
_PROVIDER_SEAM = frozenset(
    {"deepseek", "credential_store", "credential_store_win",
     "credential_sheet_win", "provider_config", "provider_cli", "credential_host",
     "deepseek_transport", "advisory",
     "app_package", "runner_broker", "container_runner", "runtime_handlers",
     "verifier", "candidate_package", "rule_delta", "delta_verifier", "delta_candidate",
     "rule_delta_interpret", "delta_transport"}
)

# Network primitives a client must never import: only the backend transport may
# open a socket. This enforces the P4.2b rule that the desktop is isolated from
# HTTP and socket code.
_NETWORK_MODULES = frozenset({"http", "socket", "urllib", "ssl", "requests"})

# P4.4 document/version-authority seam modules. A client must never import
# these: they own the Working Document domain and its persistence, which the
# desktop shell reaches only through the NDJSON boundary.
_DOCUMENT_SEAM = frozenset({"document", "version_store"})

# M4.1 Developer Memory seam modules. A client must never import these: the
# offline Developer Memory contract is the read/replay authority for bounded
# coding-agent runs, is not a capture path, and exposes no Memory UI. The
# desktop reaches it only through a later, explicitly designed boundary.
_MEMORY_SEAM = frozenset(
    {"memory", "memory_store", "memory_cli", "memory_docs", "memory_query"}
)

# M4.3 evidence-linked document projection. It reads normalized records only, so
# it may not reach the capture seam or the storage owner.
_DOCS_MODULE = "memory_docs"

# M4.4 bounded query read model. Like the projector it is a pure model over
# normalized records and accepted claims: no store, no index, no I/O, no Qt.
_QUERY_MODULE = "memory_query"

# M4.2 Claude Code capture seam modules. These own the only provider-specific
# mapping in the project and the only hook collector. A client must never
# import them: capture is configured and driven outside the desktop shell.
_CAPTURE_SEAM = frozenset({"claude_code_hooks", "hook_capture"})

# Modules that make up the offline Developer Memory product surface. The
# canonical domain must stay free of adapter vocabulary, so none of these may
# import the capture seam.
_MEMORY_PRODUCT_MODULES = (
    "memory", "memory_store", "memory_cli", "memory_docs", "memory_query",
)


def _imported_top_level_names(path: str) -> set:
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                names.add(node.module.split(".")[0])
            elif node.level:  # relative import such as ``from . import scanner``
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
    return names


def _inside_function(node: ast.AST, tree: ast.AST) -> bool:
    """Return True when ``node`` is nested within a function definition."""
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return True
    return False


class ClientArchitectureTests(unittest.TestCase):
    def test_client_modules_do_not_import_core(self):
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_FORBIDDEN_TOP_LEVEL),
                    f"{module} imports forbidden modules: "
                    f"{sorted(imported & _FORBIDDEN_TOP_LEVEL)}",
                )

    def test_contract_module_does_not_import_core(self):
        imported = _imported_top_level_names(_CONTRACT_MODULE)
        self.assertTrue(imported.isdisjoint(_FORBIDDEN_TOP_LEVEL))

    def test_workspace_module_does_not_import_core(self):
        imported = _imported_top_level_names(_WORKSPACE_MODULE)
        self.assertTrue(imported.isdisjoint(_FORBIDDEN_TOP_LEVEL))

    def test_client_modules_do_not_import_workspace(self):
        # The workspace policy owns path containment on the boundary side; a
        # client that imported it would gain direct filesystem access.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertNotIn(
                    "workspace",
                    imported,
                    f"{module} imports the boundary-side workspace policy",
                )

    def test_client_modules_do_not_import_codemap(self):
        # The desktop shell renders the procedural Code Map through its own
        # presentation vocabulary in ``client_core``; importing the Code Map or
        # Code Map Draft domain would couple the client to the deterministic
        # core (mirroring the existing workspace rule).
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint({"codemap", "codemap_draft"}),
                    f"{module} imports the Code Map domain: "
                    f"{sorted(imported & {'codemap', 'codemap_draft'})}",
                )

    def test_client_modules_do_not_import_proposal(self):
        # The proposal domain derives a plan from the deterministic core; a
        # client importing it would bypass the boundary. The desktop shell
        # renders the package through ``client_core``'s presentation vocabulary
        # only.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertNotIn(
                    "proposal",
                    imported,
                    f"{module} imports the proposal domain",
                )

    def test_client_modules_do_not_import_provider_seam(self):
        # The desktop shell reports provider readiness only through the
        # contract; importing the fixed DeepSeek identity, the credential
        # store, the advisory domain or the transport would couple it to backend
        # credential/network infrastructure.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_PROVIDER_SEAM),
                    f"{module} imports the provider/credential seam: "
                    f"{sorted(imported & _PROVIDER_SEAM)}",
                )

    def test_client_modules_do_not_import_network(self):
        # The desktop shell never opens a socket; only the backend transport
        # does. This isolates the client from HTTP and socket code (P4.2b).
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_NETWORK_MODULES),
                    f"{module} imports network primitives: "
                    f"{sorted(imported & _NETWORK_MODULES)}",
                )

    def test_client_modules_do_not_import_document_seam(self):
        # The desktop shell renders the Working Document / Candidate / Accepted
        # Version through its own presentation vocabulary in ``client_core``;
        # importing the document domain or store would couple the client to the
        # deterministic core (mirroring the proposal/workspace rules).
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_DOCUMENT_SEAM),
                    f"{module} imports the document/version seam: "
                    f"{sorted(imported & _DOCUMENT_SEAM)}",
                )

    def test_client_modules_do_not_import_memory_seam(self):
        # The Developer Memory contract is offline and read-side; the desktop
        # shell must not reach it directly, and no Memory UI exists yet.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_MEMORY_SEAM),
                    f"{module} imports the Developer Memory seam: "
                    f"{sorted(imported & _MEMORY_SEAM)}",
                )

    def test_client_modules_do_not_import_capture_seam(self):
        # Capture is a local, explicitly configured operation. The desktop
        # shell must not reach it, so no client can start a capture.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_CAPTURE_SEAM),
                    f"{module} imports the capture seam: "
                    f"{sorted(imported & _CAPTURE_SEAM)}",
                )

    def test_the_canonical_domain_does_not_import_the_adapter(self):
        # The M4.1 contract must stay source-neutral: if the canonical domain
        # could see the provider adapter, provider vocabulary would leak into
        # canonical records.
        for name in _MEMORY_PRODUCT_MODULES:
            with self.subTest(module=name):
                imported = _imported_top_level_names(os.path.join(_SRC, name + ".py"))
                self.assertTrue(
                    imported.isdisjoint(_CAPTURE_SEAM),
                    f"hrca.{name} imports the provider adapter: "
                    f"{sorted(imported & _CAPTURE_SEAM)}",
                )

    def test_the_adapter_does_not_import_the_storage_or_the_client(self):
        # The adapter is a pure translation: it may read the domain, never the
        # store, the shell or the boundary.
        imported = _imported_top_level_names(
            os.path.join(_SRC, "claude_code_hooks.py")
        )
        self.assertTrue(
            imported.isdisjoint(
                {"memory_store", "client", "client_core", "boundary", "style"}
            ),
            f"the adapter imports beyond the domain: {sorted(imported)}",
        )

    def test_memory_modules_do_not_import_network(self):
        # The memory domain and its store are offline: they never open a
        # socket, so an offline replay stays network-free.
        for name in ("memory", "memory_store", "memory_cli", "memory_docs",
                     "memory_query"):
            path = os.path.join(_SRC, name + ".py")
            imported = _imported_top_level_names(path)
            self.assertTrue(
                imported.isdisjoint(_NETWORK_MODULES),
                f"hrca.{name} imports network primitives: "
                f"{sorted(imported & _NETWORK_MODULES)}",
            )

    def test_the_projector_reads_only_normalized_records(self):
        # M4.3 projects documents from stores. If it could reach the capture
        # seam it could read raw hook JSON, and the document would stop being a
        # view of the contract's records.
        imported = _imported_top_level_names(os.path.join(_SRC, _DOCS_MODULE + ".py"))
        self.assertTrue(
            imported.isdisjoint(_CAPTURE_SEAM | {"memory_store", "os", "subprocess"}),
            f"hrca.{_DOCS_MODULE} reaches beyond normalized records: {sorted(imported)}",
        )

    def test_the_projector_does_no_io(self):
        # A projector that opened a file could read a transcript or a log, which
        # is exactly the source this layer must not have.
        with open(os.path.join(_SRC, _DOCS_MODULE + ".py"), "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", called)

    def test_capture_modules_do_not_import_network_or_spawn(self):
        # Capture reads one payload from stdin and writes bounded records. It
        # never opens a socket and never spawns a process, so no capture step
        # can reach a provider or run a command.
        for name in sorted(_CAPTURE_SEAM):
            with self.subTest(module=name):
                imported = _imported_top_level_names(os.path.join(_SRC, name + ".py"))
                self.assertTrue(
                    imported.isdisjoint(_NETWORK_MODULES),
                    f"hrca.{name} imports network primitives: "
                    f"{sorted(imported & _NETWORK_MODULES)}",
                )
                self.assertTrue(
                    imported.isdisjoint({"subprocess", "multiprocessing"}),
                    f"hrca.{name} can spawn a process: {sorted(imported)}",
                )

    def test_document_modules_do_not_import_network(self):
        # The document domain and its store are offline: they never open a
        # socket, so a frozen save/adopt loop stays network-free.
        for name in ("document", "version_store"):
            path = os.path.join(_SRC, name + ".py")
            imported = _imported_top_level_names(path)
            self.assertTrue(
                imported.isdisjoint(_NETWORK_MODULES),
                f"hrca.{name} imports network primitives: "
                f"{sorted(imported & _NETWORK_MODULES)}",
            )

    def test_candidate_package_modules_do_not_import_network(self):
        # The candidate-package contract and protected verifier are offline:
        # they never open a socket, so validation/staging stays network-free.
        for name in ("candidate_package", "verifier", "rule_delta", "delta_verifier",
                     "delta_candidate", "rule_delta_interpret"):
            path = os.path.join(_SRC, name + ".py")
            imported = _imported_top_level_names(path)
            self.assertTrue(
                imported.isdisjoint(_NETWORK_MODULES),
                f"hrca.{name} imports network primitives: "
                f"{sorted(imported & _NETWORK_MODULES)}",
            )

    def test_boundary_imports_transport_lazily(self):
        # ``deepseek_transport`` and ``delta_transport`` (the only
        # socket-opening modules) must be imported inside a function body, never
        # at module top level, so a frozen scan/serve/readiness loop never pulls
        # in HTTP/socket code.
        boundary_path = os.path.join(_SRC, "boundary.py")
        with open(boundary_path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and {"deepseek_transport", "delta_transport"} & {
                    a.name for a in node.names
                }:
                    if not _inside_function(node, tree):
                        violations.append(node.lineno)
        self.assertFalse(
            violations,
            "boundary.py imports a transport at module top level "
            f"(lines {violations}); import it lazily inside a function",
        )

    def test_client_modules_exist(self):
        for path in _CLIENT_MODULES.values():
            self.assertTrue(os.path.isfile(path), path)


class MemoryReadBoundaryTests(unittest.TestCase):
    """The Memory read path is boundary-owned, not desktop-owned (M4.3/v2a).

    The desktop reaches Developer Memory only through the two read-only
    protocol actions. These rules keep that true structurally: the shared
    contract module pulls in nothing from the memory seam, so a client cannot
    acquire memory access transitively, and the projector is reached only from
    the boundary and the offline CLI.
    """

    # Modules allowed to import the projector. The query model is included
    # because it composes Resume from the accepted projected claims rather than
    # re-deriving them.
    _PROJECTOR_CALLERS = frozenset({"boundary", "memory_cli", _QUERY_MODULE})

    def test_the_contract_module_does_not_reach_the_memory_seam(self):
        imported = _imported_top_level_names(os.path.join(_SRC, "contract.py"))
        self.assertTrue(
            imported.isdisjoint(_MEMORY_SEAM),
            f"hrca.contract reaches the memory seam: {sorted(imported & _MEMORY_SEAM)}",
        )

    def test_the_projector_is_reached_only_from_the_boundary_and_the_cli(self):
        offenders = []
        for name in sorted(os.listdir(_SRC)):
            if not name.endswith(".py"):
                continue
            stem = name[:-3]
            if stem in self._PROJECTOR_CALLERS or stem == _DOCS_MODULE:
                continue
            imported = _imported_top_level_names(os.path.join(_SRC, name))
            if _DOCS_MODULE in imported:
                offenders.append(stem)
        self.assertEqual(
            [], offenders, "these modules import the projector: %s" % offenders
        )

    def test_only_the_boundary_reads_memory_stores(self):
        # ``memory_store`` is imported by the boundary (the read path) and by
        # the offline capture importer. No other module may reach a store.
        allowed = {"boundary", "hook_capture", "memory_store", "memory_cli"}
        offenders = []
        for name in sorted(os.listdir(_SRC)):
            if not name.endswith(".py"):
                continue
            stem = name[:-3]
            if stem in allowed:
                continue
            imported = _imported_top_level_names(os.path.join(_SRC, name))
            if "memory_store" in imported:
                offenders.append(stem)
        self.assertEqual(
            [], offenders, "these modules import the memory store: %s" % offenders
        )

    def test_the_query_model_is_a_pure_read_model(self):
        # A query model that could open a file or reach a store could index
        # something the boundary never allowed it to see.
        path = os.path.join(_SRC, _QUERY_MODULE + ".py")
        imported = _imported_top_level_names(path)
        self.assertTrue(
            imported.isdisjoint(_CAPTURE_SEAM | {"memory_store", "os", "subprocess"}),
            f"hrca.{_QUERY_MODULE} reaches beyond normalized records: {sorted(imported)}",
        )
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", called)

    def test_only_the_boundary_reaches_the_query_model(self):
        offenders = []
        for name in sorted(os.listdir(_SRC)):
            if not name.endswith(".py"):
                continue
            stem = name[:-3]
            if stem in ("boundary", _QUERY_MODULE):
                continue
            if _QUERY_MODULE in _imported_top_level_names(os.path.join(_SRC, name)):
                offenders.append(stem)
        self.assertEqual(
            [], offenders, "these modules import the query model: %s" % offenders
        )

    def test_the_boundary_roots_memory_at_the_session_store_base(self):
        with open(os.path.join(_SRC, "boundary.py"), "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("memory_store.load(session.store_base", source)
        self.assertIn("memory_store.list_runs(session.store_base", source)

    def test_the_memory_handlers_never_take_a_path_from_the_request(self):
        # Scoped to the Memory handlers only: the workspace document handler
        # legitimately reads a request path, and conflating the two would make
        # this rule meaningless.
        path = os.path.join(_SRC, "boundary.py")
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name.startswith(
                ("_memory", "_get_memory", "_bounded_memory", "_load_memory")
            )
        ]
        self.assertTrue(functions, "the Memory handlers must exist")
        for node in functions:
            segment = ast.get_source_segment(source, node) or ""
            for field in ("root", "path", "base", "store_base", "cwd",
                          "transcript_path"):
                forbidden = 'request.get("%s"' % field
                with self.subTest(function=node.name, field=field):
                    self.assertNotIn(forbidden, segment)


def _stylesheet_calls(path: str) -> list:
    """Return every ``setStyleSheet(...)`` call in ``path`` (ast.Call nodes)."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setStyleSheet":
                calls.append(node)
    return calls


class StyleOwnershipTests(unittest.TestCase):
    """``hrca.style`` owns every visual value; ``hrca.client`` composes none.

    The client must contain no hex colour literal, no visual geometry literal
    (a pixel value controlling component size, margin, padding, spacing, radius,
    typography or splitter geometry), and every widget style-sheet must be
    produced by a ``style.*`` factory rather than assembled inline.
    """

    # Geometry methods where *every* numeric argument is a visual value (px).
    _ALL_ARG_GEOMETRY = frozenset(
        {
            "setContentsMargins",
            "setSpacing",
            "setFixedHeight",
            "setFixedWidth",
            "setFixedSize",
            "setMinimumWidth",
            "setMaximumWidth",
            "setMinimumHeight",
            "setMaximumHeight",
            "setMinimumSize",
            "setMaximumSize",
            "setBaseSize",
            "resize",
            "setIndentation",
            "setHandleWidth",
            "setGeometry",
        }
    )

    # Geometry methods where only certain *positional* arguments are visual
    # (the rest are widget indexes, enum modes, or booleans — not pixel values).
    _POSITIONAL_ARG_GEOMETRY = {
        "setStretchFactor": (1,),  # index, factor — only the factor is visual
        "setLineHeight": (0,),     # height, mode — only the height is visual
    }

    @staticmethod
    def _contains_numeric_literal(node: ast.AST) -> bool:
        """Return True if ``node`` or any descendant is an int/float literal."""
        return any(
            isinstance(child, ast.Constant) and isinstance(child.value, (int, float))
            for child in ast.walk(node)
        )

    def test_client_has_no_geometry_literal(self):
        with open(_CLIENT_PATH, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if name == "setSizes":
                # The sole argument is a list whose elements are pane widths.
                if node.args and isinstance(node.args[0], ast.List):
                    for element in node.args[0].elts:
                        if self._contains_numeric_literal(element):
                            violations.append((node.lineno, name, "pane width"))
            elif name in self._ALL_ARG_GEOMETRY:
                for arg in node.args:
                    if self._contains_numeric_literal(arg):
                        violations.append((node.lineno, name))
            elif name in self._POSITIONAL_ARG_GEOMETRY:
                for index in self._POSITIONAL_ARG_GEOMETRY[name]:
                    if index < len(node.args) and self._contains_numeric_literal(
                        node.args[index]
                    ):
                        violations.append((node.lineno, name))
            elif name == "_status_field":
                # The helper forwards ``max_width`` straight to setMaximumWidth,
                # so its keyword value is a fixed-widget-width constant.
                for kw in node.keywords:
                    if kw.arg == "max_width" and self._contains_numeric_literal(kw.value):
                        violations.append((node.lineno, name, "max_width"))

        self.assertFalse(
            violations,
            "client.py hard-codes a visual geometry literal; move it to hrca.style: "
            f"{violations}",
        )

    def test_client_has_no_hex_color_literal(self):
        with open(_CLIENT_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIsNone(
            re.search(r"#[0-9a-fA-F]{3,8}\b", source),
            "client.py hard-codes a colour literal; move it to hrca.style",
        )

    def test_client_stylesheets_use_style_factories(self):
        for call in _stylesheet_calls(_CLIENT_PATH):
            arg = call.args[0]
            self.assertIsInstance(arg, ast.Call)
            self.assertIsInstance(arg.func, ast.Attribute)
            self.assertIsInstance(arg.func.value, ast.Name)
            self.assertEqual(
                arg.func.value.id,
                "style",
                "client.py assembles a style-sheet inline; use a style factory",
            )


# Unicode ranges that qualify as emoji for the purposes of the product-UI
# audit. These deliberately exclude the non-emoji text chevrons used by the
# P3.2 remediation (U+25B4 ``▴`` / U+25BE ``▾`` — Geometric Shapes) and the
# punctuation marks (em dash, arrow, ellipsis) that remain in prose and elided
# text.
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # Supplemental Pictographs, Emoticons, Transport, Symbols
    (0x2600, 0x27BF),    # Miscellaneous Symbols, Dingbats
    (0x2B00, 0x2BFF),    # Miscellaneous Symbols and Arrows
    (0x23E9, 0x23F3),    # Media fast-forward/rewind arrows (e.g. U+23EB/U+23EC)
    (0x23F8, 0x23FA),    # Media control symbols (U+23F8..U+23FA)
    (0x231A, 0x231B),    # Watch / hourglass
)


def _is_emoji(char: str) -> bool:
    return any(start <= ord(char) <= end for start, end in _EMOJI_RANGES)


def _ui_string_literals(path: str) -> list:
    """Return every string literal in ``path`` that is not a docstring.

    Docstrings are developer documentation, not product UI, so a prose example
    that happens to quote an emoji must not fail the audit.
    """
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))

    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_nodes:
                literals.append(node)
    return literals


class EmojiAuditTests(unittest.TestCase):
    """No emoji may appear in any product-UI string the client or visual
    design system emits. The P3.2 remediation replaced the former up/down
    emoji (U+23EB / U+23EC) with non-emoji text chevrons (U+25B4 / U+25BE).
    """

    def test_no_emoji_in_ui_strings(self):
        violations = []
        for path in (_CLIENT_PATH, _CLIENT_MODULES["hrca.style"]):
            for node in _ui_string_literals(path):
                for char in node.value:
                    if _is_emoji(char):
                        violations.append(
                            (os.path.basename(path), node.lineno, char, node.value)
                        )
        self.assertFalse(
            violations,
            "emoji found in a product-UI string literal: " f"{violations}",
        )


if __name__ == "__main__":
    unittest.main()
