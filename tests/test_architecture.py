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
    {"memory", "memory_store", "memory_cli", "memory_docs", "memory_query",
     "memory_revisions", "memory_package", "memory_package_cli",
     "memory_twin_link"}
)

# The M4.5/v2a package boundary. It reaches the network nowhere, spawns nothing
# and knows nothing about the desktop; it is an offline operator surface.
_PACKAGE_MODULES = ("memory_package", "memory_package_cli")

# M4.5 human-revision domain. Like the projector and the query model it is a
# pure model over normalized records: no store, no I/O, no Qt.
_REVISIONS_MODULE = "memory_revisions"

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
    "memory_revisions",
)

# The P5.5a-r3c runner-image setup seam. It is the only code that prepares the
# container image, it reaches the registry to do it, and it is an operator
# surface: a client must never import it, and it must not be able to reach a
# container, a candidate or a provider.
_SETUP_SEAM = frozenset(
    {"runner_image_setup", "runner_image_setup_cli", "runner_image_policy"}
)
_SETUP_MODULES = ("runner_image_setup", "runner_image_setup_cli", "runner_image_policy")


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
                     "memory_query", "memory_revisions", "memory_package",
                     "memory_package_cli"):
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

    def test_client_modules_do_not_import_setup_seam(self):
        # Preparing the runner image reaches the registry and the Docker client.
        # The desktop shell must not be able to trigger a rebuild, so it cannot
        # see the setup adapter, its policy or its CLI.
        for module, path in _CLIENT_MODULES.items():
            with self.subTest(module=module):
                imported = _imported_top_level_names(path)
                self.assertTrue(
                    imported.isdisjoint(_SETUP_SEAM),
                    f"{module} imports the runner-image setup seam: "
                    f"{sorted(imported & _SETUP_SEAM)}",
                )

    def test_the_setup_module_cannot_reach_a_container_a_candidate_or_a_provider(self):
        # The setup prepares an image and observes the host. It must not be able
        # to run a container, dispatch a plan, touch a candidate or reach a
        # provider: those are separate authorities with their own modules.
        forbidden = {
            "container_runner",
            "validation",
            "validation_plan",
            "validation_policy",
            "candidate",
            "candidate_cli",
            "candidate_diff",
            "candidate_edit",
            "provider",
            "deepseek",
            "boundary",
            "client",
            "verifier",
        }
        for name in _SETUP_MODULES:
            with self.subTest(module=name):
                imported = _imported_top_level_names(os.path.join(_SRC, name + ".py"))
                self.assertTrue(
                    imported.isdisjoint(forbidden),
                    f"hrca.{name} reaches beyond setup: {sorted(imported & forbidden)}",
                )

    def test_the_setup_policy_is_pure(self):
        # The policy decides; it never acts. It may not spawn, read the network
        # or open a file, and it imports nothing from this package at all.
        path = os.path.join(_SRC, "runner_image_policy.py")
        imported = _imported_top_level_names(path)
        self.assertTrue(
            imported.isdisjoint({"subprocess", "os", "socket", "http", "urllib"}),
            f"the setup policy is not pure: {sorted(imported)}",
        )
        siblings = {
            os.path.splitext(entry)[0]
            for entry in os.listdir(_SRC)
            if entry.endswith(".py") and entry != "__init__.py"
        }
        self.assertTrue(
            imported.isdisjoint(siblings),
            f"the setup policy imports from this package: {sorted(imported & siblings)}",
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
    # re-deriving them; the package module is included because an export *is* the
    # projector's allowlisted output, and rebuilding it any other way would be a
    # second, divergent projection.
    _PROJECTOR_CALLERS = frozenset(
        {"boundary", "memory_cli", _QUERY_MODULE, "memory_package"}
    )

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
        # The package boundary reads and writes stores for backups and staged
        # recovery; it is the second sanctioned owner, and it goes through the
        # storage owner for every read and write.
        allowed = {"boundary", "hook_capture", "memory_store", "memory_cli",
                   "memory_package", "memory_package_cli"}
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

    def test_the_package_boundary_stays_offline_and_desktop_free(self):
        # The package boundary is an offline operator surface: it may read and
        # write only the paths it is given, and it must not reach the desktop,
        # a process primitive, a credential or a provider.
        for name in _PACKAGE_MODULES:
            with self.subTest(module=name):
                imported = _imported_top_level_names(os.path.join(_SRC, name + ".py"))
                self.assertTrue(
                    imported.isdisjoint(
                        _NETWORK_MODULES
                        | {"subprocess", "multiprocessing", "client", "client_core",
                           "style", "boundary", "credential_store", "deepseek",
                           "provider", "workspace"}
                    ),
                    f"hrca.{name} reaches where it should not: {sorted(imported)}",
                )

    def test_the_revision_model_is_pure_and_has_no_clock(self):
        # A revision model that could open a file, reach a store or read a clock
        # could bind a correction to something the contract never recorded.
        path = os.path.join(_SRC, _REVISIONS_MODULE + ".py")
        imported = _imported_top_level_names(path)
        self.assertTrue(
            imported.isdisjoint(
                _CAPTURE_SEAM | {"memory_store", "os", "subprocess", "datetime",
                                 "time"}
            ),
            f"hrca.{_REVISIONS_MODULE} reaches beyond normalized records: "
            f"{sorted(imported)}",
        )
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("open", called)

    def test_only_the_boundary_reaches_the_revision_model(self):
        offenders = []
        for name in sorted(os.listdir(_SRC)):
            if not name.endswith(".py"):
                continue
            stem = name[:-3]
            if stem in ("boundary", _REVISIONS_MODULE, "memory_package"):
                continue
            if _REVISIONS_MODULE in _imported_top_level_names(
                os.path.join(_SRC, name)
            ):
                offenders.append(stem)
        self.assertEqual(
            [], offenders, "these modules import the revision model: %s" % offenders
        )

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
                ("_memory", "_get_memory", "_bounded_memory", "_load_memory",
                 "_resolve_memory")
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


# -- explicit test routes (B0) -------------------------------------------
#
# The route table lives in ``hrca.test_routes``, but this module imports
# nothing from ``hrca`` — that is the property that lets it police the package
# without depending on it, and it is asserted below. So the table is read the
# same way every other rule here is: by parsing the source.

_ROUTE_MODULE = os.path.join(_SRC, "test_routes.py")
_SETUP_MODULE = os.path.join(_SRC, "setup_verification.py")

# Import names a route mechanism must never carry. It is dev tooling that
# partitions the surface, so it cannot be part of it, and it must not be able
# to dispatch, reach a network, or touch a credential.
_ROUTE_FORBIDDEN_IMPORTS = frozenset(
    {
        "container_runner", "runner_broker", "runner_image_policy",
        "runner_image_setup", "runner_image_setup_cli", "runtime_handlers",
        "provider", "provider_cli", "provider_config", "deepseek",
        "deepseek_transport", "delta_transport", "credential_store",
        "credential_store_win", "credential_host", "credential_sheet_win",
        "boundary", "client", "client_core", "twin", "twin_store", "codemap",
        "codemap_draft", "proposal", "advisory", "scanner", "memory",
        "subprocess", "multiprocessing", "socket", "urllib", "ssl", "http",
        "requests", "docker",
    }
)

# Calls that would make module selection a discovery rather than a lookup.
_DYNAMIC_IMPORT_NAMES = frozenset(
    {
        "__import__", "import_module", "walk_packages", "iter_modules",
        "entry_points", "__subclasses__",
    }
)


def _module_constants_and_assignments(path):
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    consts = {}
    assignments = {}
    for node in tree.body:
        # Both plain and annotated assignments are read, because a route table
        # is naturally written with an annotation and a table that vanished
        # from this reader would silently disable every rule below.
        value = None
        name = None
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            value = node.value
        if name is None or value is None:
            continue
        assignments[name] = value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts[name] = value.value
    return tree, consts, assignments


def _literal_string_tuple(node, consts=None):
    """Return the tuple of strings a tuple/list/set resolves to, else None.

    Elements may be written as string literals or as names bound to string
    constants in the same module — a route list is naturally written the second
    way, and a reader that understood only literals would report an empty table
    rather than a broken one.
    """
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return None
    values = []
    for element in node.elts:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            values.append(element.value)
        elif isinstance(element, ast.Name) and consts and element.id in consts:
            values.append(consts[element.id])
        else:
            return None
    return tuple(values)


def _route_table():
    """Return ``(routes, allowlists, container, safe, acknowledged)``.

    ``setup``'s allowlist is written as a reference to the setup-verification
    module, so it is resolved from that module's own literal rather than
    duplicated here.
    """
    _, route_consts, route_assigns = _module_constants_and_assignments(_ROUTE_MODULE)
    _, setup_consts, setup_assigns = _module_constants_and_assignments(_SETUP_MODULE)
    setup_allowlist = _literal_string_tuple(
        setup_assigns.get("ALLOWED_MODULES"), setup_consts
    )

    def resolve_key(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return route_consts.get(node.id)
        return None

    table = {}
    routes_node = route_assigns.get("ROUTE_MODULES")
    if not isinstance(routes_node, ast.Dict):
        return None
    for key, value in zip(routes_node.keys, routes_node.values):
        route = resolve_key(key)
        if route is None:
            return None
        modules = _literal_string_tuple(value, route_consts)
        if modules is None and isinstance(value, ast.Call):
            modules = setup_allowlist
        if modules is None:
            return None
        table[route] = tuple(modules)

    def string_tuple(name):
        return _literal_string_tuple(route_assigns.get(name), route_consts)

    return (
        table,
        string_tuple("ROUTES"),
        string_tuple("CONTAINER_MODULES"),
        string_tuple("SAFE_ROUTES"),
        string_tuple("ACKNOWLEDGED_ROUTES"),
    )


class TestRouteIsolationTests(unittest.TestCase):
    """The B0 route mechanism stays an explicit lookup, and its routes stay
    partitioned. The rules are read from the source so this module keeps its
    no-``hrca``-import property.
    """

    def test_the_route_table_is_readable(self):
        parsed = _route_table()
        self.assertIsNotNone(parsed, "the route table could not be read statically")
        table, routes, container, safe, acknowledged = parsed
        self.assertTrue(table)
        self.assertIsNotNone(routes)
        self.assertIsNotNone(container)
        self.assertIsNotNone(safe)
        self.assertIsNotNone(acknowledged)
        self.assertEqual(set(routes), set(table))

    def test_every_route_has_an_explicit_non_empty_allowlist(self):
        table, _, _, _, _ = _route_table()
        for route, modules in sorted(table.items()):
            self.assertTrue(modules, route)
            self.assertEqual(len(set(modules)), len(modules), route)

    def test_safe_routes_are_disjoint_from_the_container_modules(self):
        table, _, container, safe, _ = _route_table()
        container = set(container)
        for route in safe:
            self.assertFalse(
                set(table[route]) & container, "route %s reaches a container" % route
            )

    def test_container_modules_appear_only_on_the_dispatching_routes(self):
        table, _, container, _, acknowledged = _route_table()
        for module in container:
            found = {route for route, mods in table.items() if module in mods}
            self.assertEqual(set(acknowledged), found, module)

    def test_the_setup_route_mirrors_the_setup_verification_allowlist(self):
        table, _, _, _, _ = _route_table()
        _, setup_consts, setup_assigns = _module_constants_and_assignments(_SETUP_MODULE)
        setup_allowlist = _literal_string_tuple(
            setup_assigns.get("ALLOWED_MODULES"), setup_consts
        )
        self.assertEqual(setup_allowlist, table["setup"])

    def test_the_route_module_imports_no_product_or_dispatch_module(self):
        imported = _imported_top_level_names(_ROUTE_MODULE)
        offending = sorted(imported & _ROUTE_FORBIDDEN_IMPORTS)
        self.assertEqual([], offending)

    def test_the_route_module_contains_no_dynamic_import(self):
        tree, _, _ = _module_constants_and_assignments(_ROUTE_MODULE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                self.assertNotIn(name, _DYNAMIC_IMPORT_NAMES, name)

    def test_this_module_imports_no_hrca_module(self):
        imported = _imported_top_level_names(os.path.join(_HERE, "test_architecture.py"))
        hrca_names = {
            name
            for name in imported
            if os.path.isfile(os.path.join(_SRC, name + ".py"))
        }
        self.assertEqual(set(), hrca_names)

    def test_the_provider_seam_guards_import_no_hrca_module(self):
        imported = _imported_top_level_names(os.path.join(_HERE, "test_provider_seam.py"))
        hrca_names = {
            name
            for name in imported
            if os.path.isfile(os.path.join(_SRC, name + ".py"))
        }
        self.assertEqual(set(), hrca_names)


# -- the identity seam (B1) ----------------------------------------------
#
# ``hrca.identity`` owns the pure digests, fingerprints and stable identifier
# constructors. ``hrca.twin`` re-exports them. These rules hold that seam:
# identity stays a dependency-light leaf, and twin keeps no second
# implementation that could drift from it.

_IDENTITY_MODULE = os.path.join(_SRC, "identity.py")
_TWIN_MODULE = os.path.join(_SRC, "twin.py")

# The primitives B1 relocated out of twin.py.
_MOVED_IDENTITY_SYMBOLS = (
    "sha256_hex",
    "fingerprint_bytes",
    "fingerprint_source",
    "workspace_id_for",
    "file_artifact_id",
    "symbol_artifact_id",
    "baseline_fingerprint",
    "ARTIFACT_FILE",
)

# identity.py is a leaf: standard library only, and not even all of that.
_IDENTITY_FORBIDDEN_IMPORTS = frozenset(
    {
        "os", "io", "pathlib", "subprocess", "socket", "time", "datetime",
        "shutil", "tempfile", "sys", "sqlite3", "pickle", "importlib",
        "multiprocessing", "threading", "asyncio", "random", "secrets",
        "uuid", "logging", "warnings", "atexit", "signal",
    }
)

# Attribute hosts that would mean I/O, a clock read, or a store read if this
# module ever touched them.
_IDENTITY_FORBIDDEN_ATTR_HOSTS = frozenset(
    {
        "os", "io", "subprocess", "socket", "time", "datetime", "shutil",
        "tempfile", "pathlib", "sqlite3", "pickle", "sys",
    }
)

# Modules that import ``hashlib``. These predate B1 and are *recorded* here,
# not endorsed: what this pins is that B1 created no new digest helper and that
# ``twin`` is no longer one of them. Folding the remaining callers onto
# ``identity.sha256_hex`` is a later, separately reviewed change — they are not
# identity primitives today, and moving them is not this seam's business.
_DIRECT_HASHLIB_MODULES = frozenset(
    {
        "identity",
        "client_core",
        "delta_candidate",
        "document",
        "memory",
        "memory_package",
        "rule_delta_interpret",
        "runner_image_setup",
    }
)


def _top_level_definitions(path):
    """Return the top-level function and assignment names a module defines."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            names.add(node.targets[0].id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_from(path, relative_module):
    """Return the names a module imports from one relative sibling module."""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module == relative_module:
                for alias in node.names:
                    names.add(alias.name)
    return names


class IdentitySeamTests(unittest.TestCase):
    """hrca.identity is a pure leaf; hrca.twin re-exports, never duplicates."""

    def test_the_identity_module_exists(self):
        self.assertTrue(os.path.isfile(_IDENTITY_MODULE))

    def test_the_identity_module_imports_no_hrca_module(self):
        imported = _imported_top_level_names(_IDENTITY_MODULE)
        hrca_names = {
            name for name in imported if os.path.isfile(os.path.join(_SRC, name + ".py"))
        }
        self.assertEqual(set(), hrca_names)

    def test_the_identity_module_imports_no_io_process_or_clock_host(self):
        imported = _imported_top_level_names(_IDENTITY_MODULE)
        offending = sorted(imported & _IDENTITY_FORBIDDEN_IMPORTS)
        self.assertEqual([], offending)

    def test_the_identity_module_performs_no_io_clock_or_store_access(self):
        with open(_IDENTITY_MODULE, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    violations.append(("open()", node.lineno))
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name):
                    if node.value.id in _IDENTITY_FORBIDDEN_ATTR_HOSTS:
                        violations.append(
                            ("%s.%s" % (node.value.id, node.attr), node.lineno)
                        )
        self.assertEqual([], violations)

    def test_the_identity_module_loads_no_json_only_dumps_it(self):
        # ``json.dumps`` is how a baseline is canonicalised; ``load``/``loads``
        # would mean reading a store, which this module must not do.
        with open(_IDENTITY_MODULE, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "json":
                    self.assertIn(node.attr, {"dumps"}, node.attr)

    def test_twin_re_exports_every_moved_symbol(self):
        reexported = _imported_from(_TWIN_MODULE, "identity")
        missing = sorted(set(_MOVED_IDENTITY_SYMBOLS) - reexported)
        self.assertEqual([], missing)

    def test_twin_defines_none_of_the_moved_symbols(self):
        defined = _top_level_definitions(_TWIN_MODULE)
        duplicated = sorted(defined & set(_MOVED_IDENTITY_SYMBOLS))
        self.assertEqual([], duplicated)

    def test_twin_still_owns_its_own_vocabulary(self):
        defined = _top_level_definitions(_TWIN_MODULE)
        for name in ("CONF_HIGH", "CONF_LOW", "ARTIFACT_CLASS", "ARTIFACT_KINDS",
                     "TWIN_SCHEMA_VERSION", "MIGRATIONS"):
            self.assertIn(name, defined, name)

    def test_no_module_added_a_duplicate_digest_helper(self):
        found = set()
        for name in sorted(os.listdir(_SRC)):
            if not name.endswith(".py"):
                continue
            module = name[:-3]
            if "hashlib" in _imported_top_level_names(os.path.join(_SRC, name)):
                found.add(module)
        self.assertEqual(set(_DIRECT_HASHLIB_MODULES), found)


# -- the storage seam (B2) -----------------------------------------------
#
# ``hrca.storage`` owns the application-data root, canonical serialization and
# the generic migration engine. It must stay generic: a storage module that
# names a schema, a capability or a platform privileged surface has stopped
# being one. ``hrca.twin`` and ``hrca.twin_store`` re-export what they used to
# own.

_STORAGE_MODULE = os.path.join(_SRC, "storage.py")
_TWIN_STORE_MODULE = os.path.join(_SRC, "twin_store.py")

# Privileged or non-generic hosts storage must never reach. ``os`` and ``json``
# are deliberately absent: resolving a per-user directory and serializing a
# mapping are the two things this module is *for*.
_STORAGE_FORBIDDEN_IMPORTS = frozenset(
    {
        "subprocess", "socket", "urllib", "http", "ssl", "requests",
        "shutil", "tempfile", "pathlib", "io", "sys", "sqlite3", "pickle",
        "importlib", "time", "datetime", "random", "secrets", "uuid",
        "logging", "warnings", "ctypes", "winreg", "PySide6",
        "PyQt5", "PyQt6",
    }
)

# Names that would mean storage had learned what it stores. Each is a schema
# version or a migration registry owned by some capability.
_STORAGE_FORBIDDEN_DEFINITIONS = frozenset(
    {
        "TWIN_SCHEMA_VERSION", "MIGRATIONS", "TWIN_GENERATOR",
        "MEMORY_SCHEMA_VERSION", "DOCUMENT_SCHEMA_VERSION",
        "LIBRARY_SCHEMA_VERSION", "CANDIDATE_EDIT_SCHEMA_VERSION",
        "VALIDATION_PLAN_SCHEMA_VERSION", "CODEMAP_DRAFT_SCHEMA_VERSION",
        "INTENT_DELTA_SCHEMA_VERSION", "SCHEMA_VERSION",
    }
)


class StorageSeamTests(unittest.TestCase):
    """hrca.storage is generic; Twin keeps its own schema and registry."""

    def test_the_storage_module_exists(self):
        self.assertTrue(os.path.isfile(_STORAGE_MODULE))

    def test_the_storage_module_imports_no_hrca_module(self):
        imported = _imported_top_level_names(_STORAGE_MODULE)
        hrca_names = {
            name for name in imported if os.path.isfile(os.path.join(_SRC, name + ".py"))
        }
        self.assertEqual(set(), hrca_names)

    def test_the_storage_module_reaches_no_privileged_or_ui_host(self):
        imported = _imported_top_level_names(_STORAGE_MODULE)
        offending = sorted(imported & _STORAGE_FORBIDDEN_IMPORTS)
        self.assertEqual([], offending)

    def test_the_storage_module_owns_no_schema_or_migration_registry(self):
        defined = _top_level_definitions(_STORAGE_MODULE)
        offending = sorted(defined & _STORAGE_FORBIDDEN_DEFINITIONS)
        self.assertEqual([], offending)

    def test_the_storage_module_names_no_store_or_capability(self):
        # A directory leaf for the application root is storage's business; a
        # name for a *particular* store is not.
        defined = _top_level_definitions(_STORAGE_MODULE)
        for name in sorted(defined):
            lowered = name.lower()
            for capability in ("twin", "memory", "candidate", "validation",
                               "provider", "credential", "library", "document",
                               "codemap", "runner", "client"):
                self.assertNotIn(capability, lowered, name)

    def test_twin_store_re_exports_the_application_data_root(self):
        reexported = _imported_from(_TWIN_STORE_MODULE, "storage")
        self.assertIn("app_data_dir", reexported)

    def test_twin_store_no_longer_defines_the_root(self):
        defined = _top_level_definitions(_TWIN_STORE_MODULE)
        self.assertNotIn("app_data_dir", defined)

    def test_twin_keeps_its_schema_version_and_registry(self):
        defined = _top_level_definitions(_TWIN_MODULE)
        self.assertIn("TWIN_SCHEMA_VERSION", defined)
        self.assertIn("MIGRATIONS", defined)

    def test_twin_re_exports_the_serializer_rather_than_redefining_it(self):
        reexported = _imported_from(_TWIN_MODULE, "storage")
        self.assertIn("dumps", reexported)
        defined = _top_level_definitions(_TWIN_MODULE)
        self.assertNotIn("dumps", defined)

    def test_no_module_but_storage_defines_the_application_root(self):
        offenders = []
        for name in sorted(os.listdir(_SRC)):
            if not name.endswith(".py"):
                continue
            module = name[:-3]
            if module in ("storage", "twin_store"):
                continue
            if "app_data_dir" in _top_level_definitions(os.path.join(_SRC, name)):
                offenders.append(module)
        self.assertEqual([], offenders)


# -- the boundary handler registry (B3a) ---------------------------------
#
# The registry is code-owned: an action is dispatchable through it only because
# someone wrote it down in a literal tuple. These rules hold that, and hold the
# registration to the contract's own constant rather than a string that merely
# spells the same thing.

_BOUNDARY_MODULE = os.path.join(_SRC, "boundary.py")
_REGISTRY_ENTRIES_NAME = "_BOUNDARY_HANDLER_ENTRIES"
_REGISTRY_NAME = "_BOUNDARY_HANDLERS"

# Modules that would make dispatch depend on what happens to be importable at
# run time rather than on what was written down.
_DISCOVERY_IMPORTS = frozenset(
    {"importlib", "pkgutil", "pkg_resources", "stevedore", "entrypoints", "pluggy"}
)

# Calls that would turn registration into a search.
_DISCOVERY_CALLS = frozenset(
    {
        "walk_packages", "iter_modules", "entry_points", "__subclasses__",
        "import_module", "__import__", "getmembers", "find_spec",
        "listdir", "scandir", "walk", "glob", "iglob",
    }
)


def _boundary_module_tree():
    with open(_BOUNDARY_MODULE, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _top_level_value(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == name:
                return node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node.value
    return None


def _contract_action_groups():
    """Return ``{group_name: {action strings}}`` read from contract.py.

    Resolved statically, because this module imports nothing from ``hrca`` —
    that is what lets it police the package without depending on it.
    """
    with open(_CONTRACT_MODULE, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    consts, group_nodes = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                consts[name] = node.value.value
            elif name.endswith("_ACTIONS"):
                group_nodes[name] = node.value

    def resolve(node, seen=frozenset()):
        if isinstance(node, ast.Constant):
            return {node.value}
        if isinstance(node, ast.Name):
            if node.id in group_nodes and node.id not in seen:
                return resolve(group_nodes[node.id], seen | {node.id})
            return {consts.get(node.id, node.id)}
        if isinstance(node, (ast.Set, ast.Tuple, ast.List)):
            found = set()
            for element in node.elts:
                found |= resolve(element, seen)
            return found
        if isinstance(node, ast.Call):
            found = set()
            for argument in node.args:
                found |= resolve(argument, seen)
            return found
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return resolve(node.left, seen) | resolve(node.right, seen)
        return set()

    return {name: resolve(node) for name, node in group_nodes.items()}, consts


def _registry_entry_nodes():
    """Return the ``(action, handler)`` AST nodes of the registry's entries."""
    entries = _top_level_value(_boundary_module_tree(), _REGISTRY_ENTRIES_NAME)
    if not isinstance(entries, ast.Tuple):
        return None
    pairs = []
    for pair in entries.elts:
        if not isinstance(pair, ast.Tuple) or len(pair.elts) != 2:
            return None
        pairs.append((pair.elts[0], pair.elts[1]))
    return pairs


def _registered_action_names():
    """Return the ``ACTION_*`` attribute names the registry registers."""
    pairs = _registry_entry_nodes()
    if pairs is None:
        return None
    return [getattr(action, "attr", None) for action, _ in pairs]


def _legacy_action_names():
    """Return the ``ACTION_*`` names still owned by the dispatch chain."""
    tree = _boundary_module_tree()
    process = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_process":
            process = node
    if process is None:
        return set()
    names = set()
    for node in ast.walk(process):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id != "action":
                continue
            for comparator in node.comparators:
                if isinstance(comparator, ast.Attribute):
                    names.add(comparator.attr)
    return names


class BoundaryRegistryIsolationTests(unittest.TestCase):
    """The registry searches for nothing and names its action exactly."""

    def test_the_registry_module_exists(self):
        self.assertTrue(os.path.isfile(_BOUNDARY_MODULE))

    def test_the_boundary_imports_no_discovery_module(self):
        imported = _imported_top_level_names(_BOUNDARY_MODULE)
        offending = sorted(imported & _DISCOVERY_IMPORTS)
        self.assertEqual([], offending)

    def test_the_boundary_contains_no_discovery_call(self):
        tree = _boundary_module_tree()
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in _DISCOVERY_CALLS:
                    found.append((name, node.lineno))
        self.assertEqual([], found)

    def test_the_registry_is_built_at_module_scope(self):
        # Constructed when the module loads, so a duplicate or a non-callable
        # entry is a startup failure rather than a request-time one.
        value = _top_level_value(_boundary_module_tree(), _REGISTRY_NAME)
        self.assertIsNotNone(value, "the registry is not built at module scope")
        self.assertIsInstance(value, ast.Call)
        self.assertEqual("_HandlerRegistry", getattr(value.func, "id", None))

    def test_the_registry_entries_are_a_literal_tuple(self):
        entries = _top_level_value(_boundary_module_tree(), _REGISTRY_ENTRIES_NAME)
        self.assertIsNotNone(entries, "the registry entry tuple was not found")
        self.assertIsInstance(entries, ast.Tuple)

    def test_every_registration_names_its_action_by_contract_constant(self):
        # ``contract.ACTION_X`` — an attribute on the contract module, never a
        # bare string literal that could drift from the real action name.
        pairs = _registry_entry_nodes()
        self.assertIsNotNone(pairs, "the registry entries could not be read")
        self.assertTrue(pairs)
        for action, _ in pairs:
            with self.subTest(action=ast.dump(action)):
                self.assertIsInstance(action, ast.Attribute)
                self.assertTrue(action.attr.startswith("ACTION_"), action.attr)
                self.assertIsInstance(action.value, ast.Name)
                self.assertEqual("contract", action.value.id)

    def test_every_registration_names_a_module_level_function(self):
        # A named function, never a lambda, a partial, or an attribute borrowed
        # from another module — so a registration can always be read, grepped
        # and pointed at.
        with open(_BOUNDARY_MODULE, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        defined = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        pairs = _registry_entry_nodes()
        self.assertIsNotNone(pairs)
        for _, handler in pairs:
            with self.subTest(handler=ast.dump(handler)):
                self.assertIsInstance(handler, ast.Name)
                self.assertIn(handler.id, defined)

    def test_registered_action_constants_are_distinct(self):
        names = _registered_action_names()
        self.assertIsNotNone(names)
        self.assertEqual(len(names), len(set(names)), names)

    def test_every_registered_action_is_allowed(self):
        groups, consts = _contract_action_groups()
        allowed = groups["ALLOWED_ACTIONS"]
        for name in _registered_action_names():
            with self.subTest(action=name):
                self.assertIn(name, consts, name)
                self.assertIn(consts[name], allowed)

    def test_no_registered_action_still_has_a_legacy_branch(self):
        # A migrated action has exactly one owner. Two would be a latent
        # disagreement about which handler answers a request.
        registered = set(_registered_action_names())
        still_chained = registered & _legacy_action_names()
        self.assertEqual(set(), still_chained)

    def test_no_registered_action_is_twin_owned(self):
        # The Twin family is held for B4. Nothing may drift into the registry
        # because it happened to look read-only.
        groups, consts = _contract_action_groups()
        twin_family = set()
        for group in (
            "TWIN_ACTIONS",
            "DRAFT_ACTIONS",
            "PROPOSAL_ACTIONS",
            "MEMORY_CODE_LINK_ACTIONS",
        ):
            twin_family |= groups[group]
        for name in _registered_action_names():
            with self.subTest(action=name):
                self.assertNotIn(consts[name], twin_family)

    def test_unregistered_actions_keep_their_legacy_branch(self):
        registered = set(_registered_action_names())
        chained = _legacy_action_names()
        for name in ("ACTION_OPEN_PROJECT", "ACTION_DOCUMENT_SAVE",
                     "ACTION_DOCUMENT_ADOPT", "ACTION_LIBRARY_GET"):
            with self.subTest(action=name):
                self.assertNotIn(name, registered)
                self.assertIn(name, chained)

    def test_the_registry_is_not_exported_as_public_api(self):
        # It is an internal implementation detail of this package, not a new
        # protocol action, plugin interface or capability surface.
        with open(_BOUNDARY_MODULE, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        exported = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == "__all__":
                    value = node.value
                    if isinstance(value, (ast.List, ast.Tuple)):
                        for element in value.elts:
                            if isinstance(element, ast.Constant):
                                exported.add(element.value)
        self.assertNotIn(_REGISTRY_NAME, exported)
        self.assertNotIn("_HandlerRegistry", exported)
        self.assertNotIn(_REGISTRY_ENTRIES_NAME, exported)


if __name__ == "__main__":
    unittest.main()
