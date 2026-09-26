"""Explicit, fail-closed test routes (B0).

Ordinary work on this repository needs a test command that *cannot* reach a
container, a provider, a credential store or a network call. The repository's
own discovery command does not give that: ``unittest discover -s tests``
selects every module, and several of them construct the isolated runner and ask
a real Docker daemon a question. That is the same implicit, unbounded selection
:mod:`hrca.setup_verification` was written to remove for setup-only work
(P5.5r3c1); this module generalizes it into named routes.

What a route is
---------------

A route is a **code-owned allowlist** plus the statement of what the route is
safe from. There is no discovery, no pattern and no directory walk inside the
mechanism: a module is outside every route until someone adds it here on
purpose, and a module that is not on the selected route's allowlist is refused
*by name*, before it is imported.

The routes
----------

``core``
    The deterministic PrimaAgent core: structural invariants, the scanner and
    its shared foundation, Developer Memory, intent/impact, candidate identity
    and diffs, validation planning and policy, rule-delta interpretation, the
    read-only protocol, and the Qt-free client supervision logic. Safe by
    construction: no container, no process, no network, no credential, no
    optional dependency.
``twin``
    The optional Code Twin / Code Map capability and the Memory-to-Twin bridge.
``provider``
    The provider and credential seam, exercised through doubles only.
``container``
    The isolated runner, image policy and the real-container integrations.
    **Never selectable without the explicit acknowledgement flag**, and never
    reachable from ``core``, ``twin`` or ``provider``.
``setup``
    The pre-existing setup-verification surface, delegated to
    :mod:`hrca.setup_verification` so its rules stay in one place.
``full``
    The historical whole-suite route. Requires ``--acknowledge-container-risk``.

Why "safe" is a structural property here and not a promise
----------------------------------------------------------

Every safe route runs inside :func:`hrca.setup_verification.dispatch_guard`,
the mechanism already proven for setup verification. While it is armed an audit
hook turns **any** attempt to start a process into a failure, so a module that
quietly acquired a ``subprocess`` call cannot pass this route by accident — it
fails, loudly, and the run cannot report success. The guard is armed by the
existing module rather than reimplemented here, so there is exactly one
definition of "a spawn" in the repository.

A consequence worth stating plainly: a module that *legitimately* starts a
process is refused by these routes, not because it is dangerous but because a
guard that made exceptions would not be a guard. Ten modules are in that
position today and are listed in :data:`UNROUTED_MODULES` with the reason.

What this module is not
-----------------------

It is not a sandbox and it makes no unsafe command safe. It guards *this
process* while it runs *this selection*, exactly as the setup guard does. It
imports no product module — only the standard library and the setup guard — so
it cannot itself become a route into the surface it is partitioning.
"""

from __future__ import annotations

import contextlib
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import setup_verification

VERIFICATION_VERSION = "1.0.0"

ROUTE_CORE = "core"
ROUTE_TWIN = "twin"
ROUTE_PROVIDER = "provider"
ROUTE_CONTAINER = "container"
ROUTE_SETUP = "setup"
ROUTE_FULL = "full"

ROUTES: Tuple[str, ...] = (
    ROUTE_CORE,
    ROUTE_TWIN,
    ROUTE_PROVIDER,
    ROUTE_CONTAINER,
    ROUTE_SETUP,
    ROUTE_FULL,
)

# Routes that must never be able to reach a container-reaching module. The
# partition is enforced in :func:`validate_policy`, which every run consults
# before it selects anything.
SAFE_ROUTES: Tuple[str, ...] = (
    ROUTE_CORE,
    ROUTE_TWIN,
    ROUTE_PROVIDER,
    ROUTE_SETUP,
)

# The only route that may run without the acknowledgement flag. ``full`` is the
# one route whose whole purpose is to select everything.
ACKNOWLEDGED_ROUTES: Tuple[str, ...] = (ROUTE_CONTAINER, ROUTE_FULL)

# The modules that construct the isolated runner against its **real** default
# ``spawn=subprocess.run``, or that otherwise dispatch. Each was confirmed by
# static inspection of the module in this repository; the reason is recorded
# here so a reader does not have to re-derive it, and so an edit that adds one
# of them to a safe route fails :func:`validate_policy` and
# ``tests/test_architecture.py`` rather than passing quietly.
#
# ``ContainerRunner.__init__`` itself spawns nothing — it only stores its
# arguments. What dispatches is ``preflight()`` / ``image_digest()`` /
# ``run_package()`` / ``run_candidate()`` on an instance built with the default
# ``spawn``, and each of these modules does exactly that.
CONTAINER_MODULES: Tuple[str, ...] = (
    "test_candidate_syntax_integration",
    "test_rule_delta_docker_integration",
    "test_candidate_syntax",
    "test_container_runner",
    "test_validation",
)

CONTAINER_REASONS: Dict[str, str] = {
    "test_candidate_syntax_integration": (
        "constructs ContainerRunner() with the default spawn and calls "
        "preflight()/image_digest()/run_candidate() against a real daemon"
    ),
    "test_rule_delta_docker_integration": (
        "constructs ContainerRunner() with the default spawn and calls "
        "preflight() against a real daemon"
    ),
    "test_candidate_syntax": (
        "constructs ContainerRunner() with the default spawn and calls "
        "run_candidate()"
    ),
    "test_container_runner": (
        "constructs ContainerRunner() with the default spawn; the fake-daemon "
        "cases inject spawn, but the module is not spawn-free"
    ),
    "test_validation": (
        "constructs ContainerRunner() with the default spawn and calls "
        "preflight(), and its CLI case builds a real runner"
    ),
}

# Modules that are deliberately outside every safe route. Each is recorded with
# the reason, because "this test is not on the safe route" is a statement about
# the route's construction and a reader is entitled to see it.
UNROUTED_MODULES: Dict[str, str] = {
    "test_candidate": (
        "runs real ``git`` subprocesses against a working tree, so the armed "
        "dispatch guard would fail it; it is not a container test"
    ),
    "test_scanner_grammar": (
        "runs real interpreter subprocesses to compare grammar behaviour "
        "across interpreters"
    ),
    "test_style": (
        "requires the optional PySide6 desktop extra, which the core route "
        "must not depend on"
    ),
    "test_client_gui": "requires the optional PySide6 desktop extra",
    "test_client_gui_document": "requires the optional PySide6 desktop extra",
    "test_client_gui_memory": "requires the optional PySide6 desktop extra",
    "test_client_gui_memory_query": "requires the optional PySide6 desktop extra",
    "test_client_gui_memory_review": "requires the optional PySide6 desktop extra",
    "test_client_gui_memory_twin": "requires the optional PySide6 desktop extra",
    "test_client_gui_rule_delta": "requires the optional PySide6 desktop extra",
}

# The route surface. Every entry is a test *module* name (no package prefix, no
# file extension). Nothing here is derived at run time: a module that is not
# written down is not selectable.
ROUTE_MODULES: Dict[str, Tuple[str, ...]] = {
    ROUTE_CORE: (
        # structural invariants
        "test_architecture",
        "test_app_entry",
        "test_provider_seam",
        "test_test_routes",
        # shared identity primitives (B1)
        "test_identity",
        # shared storage concerns (B2)
        "test_storage",
        # shared source-evidence reads (B-T2A)
        "test_source_evidence",
        # deterministic scanner and its shared foundation
        "test_scanner",
        "test_contract",
        "test_workspace",
        "test_planning",
        "test_report",
        # packages, documents and accepted-version identity
        "test_app_package",
        "test_candidate_package",
        "test_verifier",
        "test_document",
        "test_version_store",
        "test_library",
        "test_library_store",
        # Developer Memory: replay, projection, query, corrections, packages
        "test_memory",
        "test_memory_docs",
        "test_memory_query",
        "test_memory_read",
        "test_memory_replay",
        "test_memory_revisions",
        "test_memory_store",
        "test_memory_fixtures",
        "test_memory_package",
        "test_claude_code_hooks",
        "test_hook_capture",
        # typed intent, advisory impact, candidate identity and diffs
        "test_intent_delta",
        "test_impact_proposal",
        "test_candidate_diff",
        "test_candidate_edit",
        # validation planning and policy (neither invokes the runner)
        "test_validation_plan",
        "test_validation_policy",
        # the controlled-change reconciliation record (pure composition)
        "test_work_reconciliation",
        # the source application coordinator: plan, apply, and the mutation
        # matrix that proves each check cannot be skipped
        "test_source_apply",
        "test_source_apply_cli",
        "test_source_apply_mutations",
        # rule-delta contract, interpretation and independent verification
        "test_rule_delta",
        "test_rule_delta_interpret",
        "test_delta_candidate",
        "test_delta_verifier",
        # Qt-free client supervision
        "test_client_core",
        "test_client_document",
        # the read-only protocol host
        "test_boundary",
        "test_boundary_candidate_package",
        "test_boundary_document",
        "test_boundary_library",
        "test_boundary_package",
        "test_boundary_rule_delta",
    ),
    ROUTE_TWIN: (
        "test_twin",
        "test_twin_store",
        "test_storage",
        "test_codemap",
        "test_codemap_draft",
        "test_proposal",
        "test_advisory",
        "test_memory_code_link",
    ),
    ROUTE_PROVIDER: (
        "test_provider",
        "test_provider_config",
        "test_provider_cli",
        "test_deepseek",
        "test_deepseek_transport",
        "test_delta_transport",
        "test_credential_store",
        "test_credential_store_win",
        "test_credential_host",
        "test_boundary_advisory",
        "test_boundary_credential",
        "test_boundary_profiles",
        "test_boundary_readiness",
        "test_boundary_rule_delta_interpret",
        "test_visual_tokens",
    ),
    ROUTE_CONTAINER: (
        "test_candidate_syntax_integration",
        "test_rule_delta_docker_integration",
        "test_candidate_syntax",
        "test_container_runner",
        "test_validation",
        "test_runner_broker",
        "test_runner_image_policy",
        "test_runner_image_setup",
        "test_runtime_handlers",
        "test_setup_verification_guard",
    ),
    # Owned by :mod:`hrca.setup_verification`; mirrored here so the route has an
    # explicit allowlist of its own and so a drift between the two is a failing
    # policy check rather than a silent difference. ``validate_policy`` asserts
    # they are equal.
    ROUTE_SETUP: tuple(setup_verification.ALLOWED_MODULES),
    ROUTE_FULL: (
        "test_advisory",
        "test_app_entry",
        "test_app_package",
        "test_architecture",
        "test_boundary",
        "test_boundary_advisory",
        "test_boundary_candidate_package",
        "test_boundary_credential",
        "test_boundary_document",
        "test_boundary_library",
        "test_boundary_package",
        "test_boundary_profiles",
        "test_boundary_readiness",
        "test_boundary_rule_delta",
        "test_boundary_rule_delta_interpret",
        "test_candidate",
        "test_candidate_diff",
        "test_candidate_edit",
        "test_candidate_package",
        "test_candidate_syntax",
        "test_candidate_syntax_integration",
        "test_claude_code_hooks",
        "test_client_core",
        "test_client_document",
        "test_client_gui",
        "test_client_gui_document",
        "test_client_gui_memory",
        "test_client_gui_memory_query",
        "test_client_gui_memory_review",
        "test_client_gui_memory_twin",
        "test_client_gui_rule_delta",
        "test_codemap",
        "test_codemap_draft",
        "test_container_runner",
        "test_contract",
        "test_credential_host",
        "test_credential_store",
        "test_credential_store_win",
        "test_deepseek",
        "test_deepseek_transport",
        "test_delta_candidate",
        "test_delta_transport",
        "test_delta_verifier",
        "test_document",
        "test_hook_capture",
        "test_identity",
        "test_impact_proposal",
        "test_intent_delta",
        "test_library",
        "test_library_store",
        "test_memory",
        "test_memory_code_link",
        "test_memory_docs",
        "test_memory_fixtures",
        "test_memory_package",
        "test_memory_query",
        "test_memory_read",
        "test_memory_replay",
        "test_memory_revisions",
        "test_memory_store",
        "test_planning",
        "test_proposal",
        "test_provider",
        "test_provider_cli",
        "test_provider_config",
        "test_provider_seam",
        "test_report",
        "test_rule_delta",
        "test_rule_delta_docker_integration",
        "test_rule_delta_interpret",
        "test_runner_broker",
        "test_runner_image_policy",
        "test_runner_image_setup",
        "test_runtime_handlers",
        "test_scanner",
        "test_scanner_grammar",
        "test_setup_verification",
        "test_setup_verification_guard",
        "test_source_apply",
        "test_source_apply_cli",
        "test_source_apply_mutations",
        "test_source_evidence",
        "test_storage",
        "test_style",
        "test_test_routes",
        "test_twin",
        "test_twin_store",
        "test_validation",
        "test_validation_plan",
        "test_validation_policy",
        "test_verifier",
        "test_version_store",
        "test_visual_tokens",
        "test_work_reconciliation",
        "test_workspace",
    ),
}

EXIT_VERIFIED = setup_verification.EXIT_VERIFIED
EXIT_FAILED = setup_verification.EXIT_FAILED
EXIT_REFUSED = setup_verification.EXIT_REFUSED

TESTS_PACKAGE = setup_verification.TESTS_PACKAGE

REASON_UNKNOWN_ROUTE = "the requested route is not one of this module's routes"
REASON_MODULE_NOT_LISTED = "the requested module is not on this route's allowlist"
REASON_MODULE_UNROUTED = "the requested module is deliberately outside every safe route"
REASON_MODULE_CONTAINER = "the requested module dispatches containers and is not on a safe route"
REASON_ROUTE_EMPTY = "a route has an empty allowlist"
REASON_ROUTE_DUPLICATE = "a route lists the same module twice"
REASON_SAFE_ROUTE_REACHES_CONTAINER = "a safe route reaches a container module"
REASON_CONTAINER_LEAK = "a container module appears on a route other than the container and full routes"
REASON_POLICY_MISSING = "an allowlisted module has no test file"
REASON_SETUP_DRIFT = "the setup route does not mirror the setup-verification allowlist"
REASON_FULL_INCOMPLETE = "the full route does not cover the whole test surface"
REASON_ACKNOWLEDGEMENT_REQUIRED = "this route requires an explicit container-risk acknowledgement"
REASON_VACUOUS = "the selection ran no tests, so it verified nothing"
REASON_VACUOUS_MODULE = "an allowlisted module contributed no tests"
REASON_SPAWN = setup_verification.REASON_SPAWN
REASON_LOAD = "an allowlisted module could not be loaded"


def route_names() -> Tuple[str, ...]:
    """Return every route name, in declaration order."""
    return ROUTES


def route_allowlist(route: str) -> Tuple[str, ...]:
    """Return the code-owned allowlist for ``route``.

    An unknown route returns an empty tuple; callers that need the bounded
    reason use :func:`resolve_selection`.
    """
    return ROUTE_MODULES.get(route, ())


def requires_acknowledgement(route: str) -> bool:
    """Return whether ``route`` needs the explicit container-risk flag."""
    return route in ACKNOWLEDGED_ROUTES


def repository_root() -> str:
    """Return the repository root this module lives in."""
    return setup_verification.repository_root()


def tests_root() -> Optional[str]:
    """Return the tests directory, or ``None`` when it is not there."""
    return setup_verification.tests_root()


def validate_policy() -> Optional[str]:
    """Return a bounded reason when the route table is not trustworthy.

    This is the tripwire every run consults before it selects anything: a safe
    route that somehow reaches a container module, a duplicated entry, a route
    with no allowlist, a missing test file, or a setup route that has drifted
    from the setup-verification allowlist all mean the selection cannot be
    believed, so the run refuses instead of running something it cannot vouch
    for.
    """
    for route in ROUTES:
        modules = ROUTE_MODULES.get(route)
        if not modules:
            return REASON_ROUTE_EMPTY
        if len(set(modules)) != len(modules):
            return REASON_ROUTE_DUPLICATE

    for route in SAFE_ROUTES:
        if set(ROUTE_MODULES[route]) & set(CONTAINER_MODULES):
            return REASON_SAFE_ROUTE_REACHES_CONTAINER

    for module in CONTAINER_MODULES:
        for route in ROUTES:
            if route in ACKNOWLEDGED_ROUTES:
                continue
            if module in ROUTE_MODULES[route]:
                return REASON_CONTAINER_LEAK

    if tuple(ROUTE_MODULES[ROUTE_SETUP]) != tuple(setup_verification.ALLOWED_MODULES):
        return REASON_SETUP_DRIFT

    # The full route must cover every module any other route can select. It is
    # not required to be *correct* here (``full_coverage_reason`` owns that);
    # it is required not to be a subset that silently drops a module.
    covered = set()
    for route in ROUTES:
        if route == ROUTE_FULL:
            continue
        covered |= set(ROUTE_MODULES[route])
    if not covered <= set(ROUTE_MODULES[ROUTE_FULL]):
        return REASON_FULL_INCOMPLETE

    root = tests_root()
    if root is None:
        return setup_verification.REASON_TESTS_ROOT
    for route in ROUTES:
        for name in ROUTE_MODULES[route]:
            if not os.path.isfile(os.path.join(root, name + ".py")):
                return REASON_POLICY_MISSING
    return None


def full_coverage_reason() -> Optional[str]:
    """Return a bounded reason when the full route is not the whole surface.

    Kept out of :func:`validate_policy` on purpose. ``full`` is the historical
    whole-suite route, so a newly added test module must appear on it; but
    making *every* route refuse until someone edits this table would turn a
    routine addition into a broken core route. The check is exercised by
    ``tests/test_test_routes.py`` instead, where it is a failing test rather
    than a blocked run.
    """
    root = tests_root()
    if root is None:
        return setup_verification.REASON_TESTS_ROOT
    actual = {
        name[:-3]
        for name in os.listdir(root)
        if name.endswith(".py") and name != "__init__.py"
    }
    declared = set(ROUTE_MODULES[ROUTE_FULL])
    if actual != declared:
        return REASON_FULL_INCOMPLETE
    return None


def resolve_selection(
    route: str,
    requested: Optional[Sequence[str]] = None,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return ``(modules, reason)`` for a requested selection on ``route``.

    ``None`` requested means the whole allowlist. Anything else must be a
    non-empty list of names on this route's allowlist. A module that belongs to
    another route, to no route, or to the container set is refused with the
    reason that fits it, so the refusal says *why* that name is not selectable
    here — and the refused name is never imported.
    """
    if route not in ROUTE_MODULES:
        return None, REASON_UNKNOWN_ROUTE
    allowlist = set(ROUTE_MODULES[route])

    if requested is None:
        return list(ROUTE_MODULES[route]), None
    if not isinstance(requested, (list, tuple)):
        return None, setup_verification.REASON_SELECTION_NOT_LIST
    if not requested:
        return None, setup_verification.REASON_EMPTY_SELECTION
    for name in requested:
        if not isinstance(name, str):
            return None, setup_verification.REASON_SELECTION_NOT_LIST
        if name in allowlist:
            continue
        if name in CONTAINER_MODULES:
            return None, REASON_MODULE_CONTAINER
        if name in UNROUTED_MODULES:
            return None, REASON_MODULE_UNROUTED
        return None, REASON_MODULE_NOT_LISTED
    return sorted(set(requested)), None


def _flatten(suite: Any):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def build_selection(
    modules: Sequence[str],
) -> Tuple[Optional[unittest.TestSuite], Optional[Dict[str, int]], Optional[str]]:
    """Load the named modules and return ``(suite, counts_per_module, reason)``.

    Modules are loaded by name from the allowlist that already refused anything
    else, so nothing outside the route is reachable here.
    """
    loader = unittest.TestLoader()
    counts: Dict[str, int] = {name: 0 for name in modules}
    cases: List[Any] = []
    for name in modules:
        try:
            suite = loader.loadTestsFromName(TESTS_PACKAGE + "." + name)
        except Exception:
            return None, None, REASON_LOAD
        for case in _flatten(suite):
            cases.append(case)
            key = case.__class__.__module__.split(".")[-1]
            counts[key] = counts.get(key, 0) + 1
    return unittest.TestSuite(cases), counts, None


def vacuity_reason(counts: Dict[str, int]) -> Optional[str]:
    """Return a bounded reason when a selection would verify nothing."""
    if not counts or sum(counts.values()) == 0:
        return REASON_VACUOUS
    for name in sorted(counts):
        if counts[name] == 0:
            return REASON_VACUOUS_MODULE
    return None


def run(
    route: str,
    requested: Optional[Sequence[str]] = None,
    *,
    acknowledge_container_risk: bool = False,
    stream: Any = None,
) -> int:
    """Run one route's selection and return an exit code.

    Order matters and is deliberate: the policy is validated, the selection is
    resolved (refusing unknown names *before* any import), the acknowledgement
    is required for the dispatching routes, and only then does anything load.
    """
    out = stream if stream is not None else sys.stderr

    if route not in ROUTE_MODULES:
        out.write("test route refused: %s\n" % REASON_UNKNOWN_ROUTE)
        return EXIT_REFUSED

    if requires_acknowledgement(route) and not acknowledge_container_risk:
        out.write("test route refused: %s\n" % REASON_ACKNOWLEDGEMENT_REQUIRED)
        return EXIT_REFUSED

    # The setup route owns its own rules; delegating keeps them in one place and
    # keeps the setup surface exactly as strong as it was before this module
    # existed.
    if route == ROUTE_SETUP:
        return setup_verification.run(requested)

    reason = validate_policy()
    if reason is not None:
        out.write("test route refused: %s\n" % reason)
        return EXIT_REFUSED

    # ``full`` is the only route that claims to be the whole suite, so it is the
    # only one that has to prove the claim. The check reads the directory, but
    # the *selection* still comes from the literal allowlist above: this only
    # refuses when the literal has fallen behind the tree.
    if route == ROUTE_FULL:
        reason = full_coverage_reason()
        if reason is not None:
            out.write("test route refused: %s\n" % reason)
            return EXIT_REFUSED

    modules, reason = resolve_selection(route, requested)
    if reason is not None:
        out.write("test route refused: %s\n" % reason)
        return EXIT_REFUSED
    assert modules is not None

    if repository_root() not in sys.path:
        sys.path.insert(0, repository_root())

    # The container and full routes are the two that may legitimately dispatch.
    # Arming the guard on them would make their whole purpose a failure, which
    # is why they are also the two that require the acknowledgement flag.
    guard: Optional[Any] = None
    with contextlib.ExitStack() as stack:
        if route not in ACKNOWLEDGED_ROUTES:
            guard = setup_verification.dispatch_guard()
            stack.enter_context(guard)
            setup_verification.reset_attempts()
        suite, counts, reason = build_selection(modules)
        if reason is not None:
            out.write("test route refused: %s\n" % reason)
            return EXIT_REFUSED
        reason = vacuity_reason(counts)
        if reason is not None:
            out.write("test route refused: %s\n" % reason)
            return EXIT_REFUSED
        result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
        attempts = guard.attempts if guard is not None else []

    if attempts:
        # Belt and braces, exactly as the setup guard does it: an attempt is a
        # refusal even if a test swallowed the exception and the run looked
        # green.
        out.write("test route failed: %s (%s)\n" % (REASON_SPAWN, attempts[0]))
        return EXIT_FAILED

    out.write(
        "test route %s: %d modules, %d tests, %s\n"
        % (
            route,
            len(counts),
            sum(counts.values()),
            "guard armed, nothing attempted" if guard is not None else "guard not applicable",
        )
    )
    return EXIT_VERIFIED if result.wasSuccessful() else EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - a refusal, asserted by source
    # Running *this* module as a script would put a second copy of it in the
    # process, and the guard it delegates to has the same objection
    # hrca.setup_verification documents. Refuse rather than run something
    # weaker than it appears to be.
    sys.stderr.write(
        "test routes refused: run hrca.test_routes_cli instead, "
        "so exactly one copy of the guard is live\n"
    )
    raise SystemExit(EXIT_REFUSED)
