# P5.5r3c1 — setup verification that cannot dispatch a candidate container

The repair for the authority leak in P5.5a-r3c: a setup-only task launched
`unittest discover -s tests` as a baseline, discovery selected the live
integration modules, a daemon was reachable, and candidate containers ran. The
setup implementation never dispatched them — the *selection* did.

**Result: the setup-verification path is now an explicit allowlist with no
discovery, and while it runs, starting a process or importing an excluded module
is a refusal.** Running it performs no Docker client or daemon operation, starts
no container, creates no candidate root, plan, attempt or result, and makes no
network contact.

## The safe command

```bash
uv run python -m hrca.setup_verification_cli
```

```text
setup verification: 4 modules, 183 tests, guard armed by hrca.setup_verification, nothing attempted
----------------------------------------------------------------------
Ran 183 tests in 4.052s

OK
RC=0   seconds=4
```

`--module <name>` narrows the run to a subset of the allowlist; anything else is
refused. Exit codes: `0` verified, `1` the selection ran and failed, `2` refused
before it could verify anything.

| | |
|---|---|
| **Allowed surface** | `test_architecture`, `test_runner_image_policy`, `test_runner_image_setup`, `test_setup_verification` — a code-owned tuple in `src/hrca/setup_verification.py`. No discovery, no pattern, no directory walk. |
| **Excluded, by name** | `test_candidate_syntax_integration` (mounts a candidate in a real container) and `test_rule_delta_docker_integration` (runs the package handlers in a real container). Both are untouched and stay reachable through their own explicit route. |
| **Refused while running** | any attempt to start a process (audit hook) and any import of an excluded module (import hook). An attempt is a *failure*, not a note. |
| **Refused as vacuous** | a selection that runs no tests, or leaves an allowed module contributing none. |

## Fail-closed, demonstrated

| Command | Result |
|---|---|
| `--module test_candidate_syntax_integration` | `refused: the requested module dispatches containers and is never selectable here` · RC `2` |
| `--module test_candidate_syntax_integration --module test_rule_delta_docker_integration` | same refusal · RC `2` |
| `--module test_container_runner` (not on the allowlist) | `refused: the requested module is not on the setup-verification allowlist` · RC `2` |
| `python -m hrca.setup_verification` (the duplicate-copy form) | `refused: run hrca.setup_verification_cli instead, so exactly one copy of the guard is live` · RC `2` |
| `--module test_setup_verification` | `OK` · 29 tests · RC `0` |

A refused name is not imported: the tests assert `sys.modules` is unchanged
across the refusal.

## The non-dispatch proof, and what it rests on

Three independent pieces, none of which queries Docker:

1. **In-process, at run time.** The audit hook fires at the C level before any
   process exists, whoever holds the reference. The run reports
   `nothing attempted` — that is the guard's own record of what it refused, and
   it is empty.
2. **The blocked primitive is the one dispatch uses.** `ContainerRunner` binds
   `spawn=subprocess.run` as a *default argument*, so patching the module
   attribute afterwards would be invisible to it; the audit event is not. The
   guard tests prove the runner's `preflight()` is refused at that primitive, and
   a static check proves both dispatch paths (`run`, `run_candidate`) call
   `self._spawn` — so blocking it blocks dispatch rather than one spelling of it.
   Those attempts use the **host interpreter** as a fake `docker`, so even a
   broken guard runs `python info` and never the Docker client.
3. **Nothing changed outside the process.** `git rev-parse HEAD` and
   `git status --porcelain` are identical before and after, and the count of
   `/tmp/hrca-*` staged directories is `0` before and after.

**Limitation, stated plainly.** Docker state was *not* queried to corroborate
this, because querying it is itself a Docker client operation and this task
forbids one. The proof above is in-process and filesystem-based; it is not a
daemon-side observation, and it is not offered as one.

## The bug this task found in its own first draft

The first version of the entrypoint lived in the state module. Running
`python -m hrca.setup_verification` executes that file as `__main__`, so the
tests under it imported a **second copy** of the module — with its own guard
state and its own `SetupRefused` class. The guard still blocked the spawn, but
the exception type did not match the one the tests expected, and the copy the
tests inspected looked *inactive while a hook was armed*.

Two things follow, and both are in the code: the entrypoint now lives in
`setup_verification_cli.py` so exactly one copy of the state module is ever live,
and `_GuardState.installed_by` records which module armed the hook, so a
duplicate would be visible. A test asserts no copy of the state module is running
as `__main__`.

This is why the repair was not merely "add an allowlist": an allowlist alone
would have left the guard inspecting a copy of itself.

## Tests

```bash
uv run python -m unittest tests.test_setup_verification tests.test_setup_verification_guard
→ Ran 40 tests in 0.068s — OK
```

`tests/test_setup_verification.py` is on the allowlist and must never trigger a
refusal. `tests/test_setup_verification_guard.py` is deliberately **not**: the
tests that prove a spawn is refused have to attempt one, and the safe run's claim
is that it attempts none. Keeping them apart is what lets `nothing attempted` be
true rather than aspirational.

## What did not change

- Candidate validation and its integration tests: untouched, still runnable by
  their own explicit module names.
- Runner image identity, network policy, credentials, validation semantics,
  approval/adoption, repository application, desktop protocol/UI, Twin and
  Memory: untouched.
- `hrca-df8baf7.bundle`: not inspected, hashed, staged, copied or altered.

**P5.5a and P5.5b remain unauthorized, and the separate P5.5r3c2
network-observation gate has not been attempted.**
