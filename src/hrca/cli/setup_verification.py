"""Explicit, fail-closed test selection for setup verification (P5.5r3c1).

A setup-only task must be able to verify itself **without** being able to reach
candidate or container integration behaviour. Running the repository's discovery
command does not give that: ``unittest discover -s tests`` selects every module,
and two of them dispatch real containers when a daemon happens to be reachable —
``tests/test_candidate_syntax_integration.py`` mounts a candidate, and
``tests/test_rule_delta_docker_integration.py`` runs the package handlers. That
is exactly how a "quick baseline check" turned into a candidate container running
during P5.5a-r3c. The dangerous property was not intent; it was an *implicit,
unbounded selection*.

This module replaces that with an explicit one.

What it does
------------

1. **selects nothing implicitly.** The setup-verification surface is a
   code-owned allowlist. There is no discovery, no pattern and no directory walk,
   so a new test module is outside the surface until someone adds it here on
   purpose.
2. **refuses everything else by name.** A requested module that is not on the
   allowlist is refused with a bounded reason, and the refused name is never
   imported: a module that dispatches containers cannot be reached by asking for
   it.
3. **makes the run try to fail.** While verification runs, an audit hook refuses
   any attempt to start a process, and an import hook refuses the excluded
   modules. A spawn attempt is a *failure*, not a note — the run cannot report
   success if anything tried.
4. **refuses to be vacuous.** A selection that runs no tests, or that leaves an
   allowed module contributing none, is refused: "verified nothing" must not look
   like "verified".

Why an audit hook rather than a patched ``subprocess``
------------------------------------------------------

The obvious guard — replace ``subprocess.run`` with something that refuses — does
not work here, and knowing why matters. :class:`hrca.container_runner.
ContainerRunner` binds ``spawn=subprocess.run`` as a **default argument**, which
is evaluated when the function is defined. Replacing the ``subprocess.run``
attribute afterwards is invisible to it: a run would dispatch with the original
function while the guard reported nothing. The ``subprocess.Popen`` audit event
is raised at the C level, before any process is created, whichever reference the
caller holds — so it covers the runner's own primitive rather than one spelling
of it.

What this is not
----------------

It is not a sandbox, and it does not make an unsafe command safe. It guards *this
process* while it runs *this selection*. It also does not touch
candidate-validation behaviour: the integration modules stay exactly as they are
and stay reachable through their own explicit route. The correction is a
partition — setup verification on one side, candidate validation on the other —
not a weakening of either.

Usage, and why the entrypoint is a separate module
--------------------------------------------------

``python -m hrca.setup_verification_cli`` runs the allowlist. ``--module``
narrows it to a subset of the allowlist and refuses anything outside it. Exit
codes: ``0`` verified, ``1`` the selection ran and failed, ``2`` the run was
refused before it could verify anything.

The entrypoint is deliberately *not* in this module. ``python -m hrca.x``
executes ``x`` as ``__main__``, so a self-entrypoint here would exist twice in
one process: once as ``__main__`` and once as ``hrca.setup_verification`` when
the tests under it import the canonical name. The two copies would hold separate
guard state and *separate exception classes*, so a refusal raised by the copy
that installed the hook would not be the class the canonical copy documents —
and, worse, the copy the tests inspect would look inactive while a hook was
armed. Keeping the CLI in its own module means exactly one copy of this module is
ever live, which is what :attr:`_GuardState.installed_by` exists to catch if that
ever stops being true.
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERIFICATION_VERSION = "1.0.0"

# The whole setup-verification surface. Nothing else is selectable, and nothing
# here dispatches a container. `test_setup_verification` is included because the
# selector is part of what a setup-only task must be able to verify.
ALLOWED_MODULES = (
    "test_architecture",
    "test_runner_image_policy",
    "test_runner_image_setup",
    "test_setup_verification",
)

# Modules that must never be selected, each with the reason it cannot be. The
# allowlist above is what enforces this; the map exists so the reason is stated
# where a reader looks, and so a future edit that adds one of these to the
# allowlist fails a test and the policy check rather than passing quietly.
EXCLUDED_MODULES = {
    "test_candidate_syntax_integration": "it mounts a candidate in a real container",
    "test_rule_delta_docker_integration": "it runs the package handlers in a real container",
}

TESTS_PACKAGE = "tests"
TESTS_DIRNAME = "tests"
_MAX_DESCRIPTION_CHARS = 64

REASON_SELECTION_NOT_LIST = "the requested selection is not a list of module names"
REASON_EMPTY_SELECTION = "the selection is empty"
REASON_UNKNOWN_MODULE = "the requested module is not on the setup-verification allowlist"
REASON_EXCLUDED_MODULE = "the requested module dispatches containers and is never selectable here"
REASON_POLICY_OVERLAP = "a module is both allowed and excluded, so this selection is not trustworthy"
REASON_POLICY_MISSING = "an allowed module has no test file"
REASON_TESTS_ROOT = "the tests package could not be found"
REASON_LOAD = "an allowed module could not be loaded"
REASON_VACUOUS = "the selection ran no tests, so it verified nothing"
REASON_VACUOUS_MODULE = "an allowed module contributed no tests"
REASON_SPAWN = "setup verification attempted to start a process"
REASON_IMPORT = "setup verification attempted to import an excluded module"
REASON_ALREADY_RUNNING = "setup verification is already running in this process"

EXIT_VERIFIED = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

# Every way a process can be started. ``subprocess`` is the only one this
# repository uses, but a guard that watched only the spelling in use today would
# be a guard against history rather than against dispatch.
def _is_spawn_event(event: str) -> bool:
    if event == "subprocess.Popen":
        return True
    if event in ("os.system", "os.posix_spawn", "os.fork", "os.forkpty"):
        return True
    return event.startswith("os.exec") or event.startswith("os.spawn")


class SetupRefused(RuntimeError):
    """Raised when guarded setup verification tries to spawn or import.

    ``reason`` is one of this module's bounded sentences. The message never
    carries a caller value; it may name the *event*, which is structure rather
    than input.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail[:_MAX_DESCRIPTION_CHARS]
        super().__init__(reason + (": " + self.detail if self.detail else ""))


class _GuardState:
    """Process-wide guard state.

    An audit hook cannot be removed once added, so the hook stays installed and
    this state decides whether it acts. That keeps the hook inert outside a
    verification run — a later test in the same process that legitimately spawns
    (a Git working-tree test, say) is unaffected.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.attempts: List[str] = []
        self.running = False
        self.hook_installed = False
        # Which module installed the hook. It must be the canonical name: if a
        # second copy of this module ever arms a hook, this records it and a
        # test fails, because a duplicated guard is a guard nobody can reason
        # about.
        self.installed_by: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.depth > 0


_STATE = _GuardState()


def _audit(event: str, args: Tuple[Any, ...]) -> None:
    if not _STATE.active or not _is_spawn_event(event):
        return
    detail = " ".join(str(part) for part in args[:1]) if args else ""
    detail = os.path.basename(detail)[:_MAX_DESCRIPTION_CHARS]
    _STATE.attempts.append(event + (":" + detail if detail else ""))
    raise SetupRefused(REASON_SPAWN, event)


class _ImportGuard:
    """Refuse the excluded modules by name while the guard is active."""

    def __init__(self, names: Sequence[str]) -> None:
        self._names = frozenset(names)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if _STATE.active and fullname in self._names:
            _STATE.attempts.append("import:" + fullname)
            raise SetupRefused(REASON_IMPORT, fullname.split(".")[-1])
        return None


def _install() -> None:
    if not _STATE.hook_installed:
        _STATE.installed_by = __name__
        sys.addaudithook(_audit)
        sys.meta_path.insert(0, _ImportGuard(_watched_import_names()))
        _STATE.hook_installed = True


def _watched_import_names() -> Tuple[str, ...]:
    names: List[str] = []
    for name in EXCLUDED_MODULES:
        names.append(name)
        names.append(TESTS_PACKAGE + "." + name)
    return tuple(names)


class DispatchGuard:
    """Context manager that makes a spawn or an excluded import a refusal."""

    def __enter__(self) -> "DispatchGuard":
        _install()
        _STATE.depth += 1
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        _STATE.depth = max(0, _STATE.depth - 1)
        return False

    @property
    def attempts(self) -> List[str]:
        """Return what was refused, in the order it was attempted."""
        return list(_STATE.attempts)


def dispatch_guard() -> DispatchGuard:
    """Return a guard for use as a context manager."""
    return DispatchGuard()


def reset_attempts() -> None:
    """Forget what has been refused so far."""
    _STATE.attempts = []


def repository_root() -> str:
    """Return the repository root this module lives in."""
    # Three levels up: this module's package, ``hrca``, then ``src``. The
    # repository reorganisation moved it one package deeper, and a root that
    # stopped at ``src`` would put the tests directory out of reach.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def tests_root() -> Optional[str]:
    """Return the tests directory, or ``None`` when it is not there."""
    path = os.path.join(repository_root(), TESTS_DIRNAME)
    return path if os.path.isdir(path) else None


def validate_policy() -> Optional[str]:
    """Return a bounded reason when the allowlist itself is not trustworthy.

    This is the tripwire a future edit trips: an excluded module that somehow
    appears on the allowlist, a duplicated entry, a missing test file. Any of
    them means the selection cannot be believed, so verification refuses instead
    of running something it cannot vouch for.
    """
    if not ALLOWED_MODULES:
        return REASON_EMPTY_SELECTION
    if len(set(ALLOWED_MODULES)) != len(ALLOWED_MODULES):
        return REASON_POLICY_OVERLAP
    if set(ALLOWED_MODULES) & set(EXCLUDED_MODULES):
        return REASON_POLICY_OVERLAP
    root = tests_root()
    if root is None:
        return REASON_TESTS_ROOT
    for name in ALLOWED_MODULES:
        if not os.path.isfile(os.path.join(root, name + ".py")):
            return REASON_POLICY_MISSING
    return None


def resolve_selection(
    requested: Optional[Sequence[str]] = None,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return ``(modules, reason)`` for a requested selection.

    ``None`` means the whole allowlist. Anything else must be a non-empty list of
    allowlist names; an excluded module is refused by its own reason so the
    refusal says *why* that name can never be selected here.
    """
    if requested is None:
        return list(ALLOWED_MODULES), None
    if not isinstance(requested, (list, tuple)):
        return None, REASON_SELECTION_NOT_LIST
    if not requested:
        return None, REASON_EMPTY_SELECTION
    for name in requested:
        if not isinstance(name, str):
            return None, REASON_SELECTION_NOT_LIST
        if name in EXCLUDED_MODULES:
            return None, REASON_EXCLUDED_MODULE
        if name not in ALLOWED_MODULES:
            return None, REASON_UNKNOWN_MODULE
    return sorted(set(requested)), None


def vacuity_reason(counts: Dict[str, int]) -> Optional[str]:
    """Return a bounded reason when a selection would verify nothing."""
    if not counts:
        return REASON_VACUOUS
    if sum(counts.values()) == 0:
        return REASON_VACUOUS
    for name in sorted(counts):
        if counts[name] == 0:
            return REASON_VACUOUS_MODULE
    return None


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

    The count is taken from the loaded cases rather than assumed, so a module
    that stops producing tests is visible instead of silently shrinking the
    surface.
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


def run(
    requested: Optional[Sequence[str]] = None,
    *,
    stream: Any = None,
) -> int:
    """Run the setup-verification selection and return an exit code."""
    out = stream if stream is not None else sys.stderr
    if _STATE.running:
        out.write("setup verification refused: %s\n" % REASON_ALREADY_RUNNING)
        return EXIT_REFUSED

    reason = validate_policy()
    if reason is not None:
        out.write("setup verification refused: %s\n" % reason)
        return EXIT_REFUSED

    modules, reason = resolve_selection(requested)
    if reason is not None:
        out.write("setup verification refused: %s\n" % reason)
        return EXIT_REFUSED

    if repository_root() not in sys.path:
        sys.path.insert(0, repository_root())

    _STATE.running = True
    reset_attempts()
    try:
        with dispatch_guard() as guard:
            suite, counts, reason = build_selection(modules)
            if reason is not None:
                out.write("setup verification refused: %s\n" % reason)
                return EXIT_REFUSED
            reason = vacuity_reason(counts)
            if reason is not None:
                out.write("setup verification refused: %s\n" % reason)
                return EXIT_REFUSED
            result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
            attempts = guard.attempts
    finally:
        _STATE.running = False

    if attempts:
        # Belt and braces: a guard attempt is a refusal even if a test swallowed
        # the exception and the run otherwise looked green.
        out.write("setup verification refused: %s (%s)\n" % (REASON_SPAWN, attempts[0]))
        return EXIT_FAILED

    out.write(
        "setup verification: %d modules, %d tests, guard armed by %s, nothing attempted\n"
        % (len(counts), sum(counts.values()), _STATE.installed_by)
    )
    return EXIT_VERIFIED if result.wasSuccessful() else EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - a refusal, asserted by source
    # Running *this* module as a script would put a second copy of it in the
    # process: one as __main__, one as hrca.setup_verification once the tests
    # under it import the canonical name. Those copies would hold separate guard
    # state and separate exception classes, so the guard the tests inspect would
    # not be the guard that was armed. Refuse rather than run something weaker
    # than it appears to be.
    sys.stderr.write(
        "setup verification refused: run hrca.setup_verification_cli instead, "
        "so exactly one copy of the guard is live\n"
    )
    raise SystemExit(EXIT_REFUSED)
