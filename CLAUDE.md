# Human-Readable Code Agent

A deterministic static scanner for Python source trees — the **Phase 1
baseline** for a "code twin" project. No LLM, no network, no runtime code
execution at scan time.

## Layout

- `src/hrca/` — the package, organised into **responsibility packages**:
  `core/` (the contract, identity, storage, visual tokens and workspace policy),
  `source/` (the deterministic scanner), `authoring/` (the P5.x product surface:
  intent, impact, candidate edit and diff, the document and version authority,
  validation, and the controlled-change reconciliation record), `execution/` (the
  accepted runner and its packages), `integrations/` (the provider and credential
  seam), `memory/`, `twin/`, `boundary/`, `cli/` and `ui/`. A few modules stay at
  the package root: `source_evidence.py`, `__main__.py`, and the compatibility
  shims that keep the documented `python -m hrca.X` entrypoints and the console
  scripts resolving after the reorganisation. **A module name mentioned below is
  a module name, never a path relative to `src/hrca/`: it is a leaf inside one of
  those packages, a package's own front door, or one of those root modules.** The
  minimum agent working
  surface is `source/` plus `core/identity`: the scan entrypoint `hrca-scan`
  reaches exactly one module, and every other capability is reached only through
  the backend host, the desktop client, or an offline operator CLI.
  The M4.1 Developer Memory contract is `memory.py` (pure domain),
  `memory_store.py` (sole storage owner) and `memory_cli.py` (offline replay).
  The M4.2 Claude Code port is `claude_code_hooks.py` (adapter: the only module
  allowed provider vocabulary) and `hook_capture.py` (collector and importer).
  The M4.3 evidence-linked document projector is `memory_docs.py`: a pure
  projection over normalized records that never reads raw hook JSON, a
  transcript or a log. The desktop reaches Memory only through the two
  read-only `boundary.py` actions `get_memory_documents` / `get_memory_record`
  (contract 3.6.0); it must never import the Memory seam. The M4.3 read-only
  Evidence surface is the `memory` nav destination in `client.py`. The M4.4
  bounded Search/Timeline/Resume read model is `memory_query.py`, reached
  through the `search_memory` / `memory_resume` actions (contract 3.7.0); it is
  a pure model over records with no index and no storage. The M4.4 workflow is
  the Documents / Search / Resume tabs of that same `memory` destination. Schema
  1.1.0 (M4.5/v1a) adds append-only human corrections and immutable
  generated-document versions in `memory_revisions.py`, reached through the
  `get_memory_history` / `resolve_memory_effective` / `append_memory_correction`
  actions (contract 3.8.0); a correction changes what is shown, never what was
  recorded. The M4.5 workflow is the Corrections tab of that same `memory`
  destination. The M4.5/v2b Code Twin linkage is `memory_twin_link.py`: a pure
  domain that binds one Memory record to one exact Twin entity identity plus the
  Twin's own workspace revision number, reached through the read-only
  `get_memory_code_link` / `resolve_memory_code_freshness` actions (contract
  3.9.0, additive over 3.8.0). Freshness is a returned comparison against
  authoritative Twin state — never a persisted assertion — and the two actions
  write nothing, so Memory authority is untouched. The M4.5/v2c Code Twin link
  workflow is the `Code Twin` tab of that same `memory` destination: it binds a
  link, re-reads the freshness the Twin reports against the held link, and opens
  the exact entity only after the existing `get_twin` read returns the artifact
  whose id equals the link's `entity_id`. Binding and comparing are separate
  actions on purpose — a fresh bind always agrees with itself, so only a held
  link can show `stale`, `historical` or `missing`. The desktop narrows the
  returned `actionable` and never widens it. The M4.5/v2a package boundary
  is `memory_package.py` (two
  profiles: a least-disclosure `export` and a local-sensitive `backup`) with the
  offline operator CLI `memory_package_cli.py`; both are outside the desktop and
  reach no network, process or credential primitive. A `backup` is built from
  one verified cross-run snapshot (`capture_stores` / `verify_capture`) and
  carries its identity in the manifest, which staged recovery re-derives from
  the staged bytes before an active store is in scope. The P5.3 typed developer
  intent is `intent_delta.py` and its deterministic advisory impact proposal is
  `impact_proposal.py`, with the offline operator CLI `intent_cli.py`. They are
  pure: no filesystem, no network, no provider, no credential, no command, no
  Git and no store of their own, and they import neither `codemap_draft` nor
  `proposal` — the P4.1 "Intent Delta" is *derived* from typed Code Map block
  edits, whereas this one records facts a developer *supplied*, so the two share
  a name and nothing else. The source-evidence facts a proposal consumes — a
  Twin-produced document's workspace baseline and its source artifacts — are read
  through `source_evidence.py`, a pure read model over a mapping handed to it
  (standard library plus `hrca.core.identity` only: no schema, migration, store,
  clock, I/O or Twin import). Twin stays the producer and owner of its store,
  its schema and its migrations; the proposal's only Twin reach is
  `twin.migrate_store`, an explicit and mechanically bounded *temporary*
  exception that ends only under a separate decision giving a source-evidence
  document schema an explicit owner. A proposal binds by **exact identity only**
  (a Twin artifact id, a scanner symbol/relation id, a workspace-relative
  scanner file path) and refuses rather than falling back to a name, a path, an
  order or prose; a changed workspace, scan generation, baseline fingerprint or
  scanner schema/grammar context invalidates the binding instead of silently
  re-binding it. Neither module is reachable from the desktop and no protocol
  action was added, so the desktop's authority is unchanged. The P5.4 typed edit
  request is
  `candidate_edit.py`, the canonical diff is `candidate_diff.py`, and the
  isolated content-addressed candidate is `candidate.py` with the offline
  operator CLI `candidate_cli.py`. `candidate.py` is the **only** module in this
  package allowed to write anything, and what it may write is exactly one fresh
  directory it creates itself beneath a caller-supplied output base *outside*
  the accepted repository and outside Git metadata. The grammar is one
  operation — exact whole-file replacement of an existing UTF-8 Python source
  file — because creation has no predecessor identity to bind and deletion,
  rename, mode changes, links, binary content and arbitrary patch input are all
  refused by name. A path is authorized only when the bound P5.3 proposal both
  scopes it and carries it as evidence, and the edit's `expected_sha256` must
  equal the Twin's own recorded fingerprint for that file artifact, so the
  predecessor is pinned by two independent sources before a byte is read.
  Materializing a candidate is not validating, approving or adopting one:
  `candidate_edit`, `candidate_diff` and `candidate` import no provider,
  credential, runner, contract, workspace or store seam, and no repository test
  or build is ever executed as candidate validation. The P5.5a validation
  contract is `validation_policy.py` (the code-owned check table),
  `validation_plan.py` (the plan document), `validation.py` (binding, dispatch
  through the accepted runner, and the append-only evidence store) and
  `validation_cli.py`. A check is **not** a command: the accepted runner takes a
  handler name and a payload, never an argv, so a caller names a check id and
  the product supplies the package, the handler, the fixed input, the timeout,
  the resource profile and the network and credential policy. A plan request
  carrying a command, an image, a mount, an environment value, a timeout, a
  resource limit, a credential, a privilege setting or a working directory is
  refused by name. The seven terminal states are `passed`, `failed`,
  `timed_out`, `cancelled`, `unavailable`, `refused` and `unknown`, and the
  overall state is the most severe any check reached; `evidence_complete` is
  true only when every check passed. **Passing evidence is not approval**: every
  attempt and result pins `approved`, `adopted` and `applied` false and
  declares the whole mutation surface false. Attempts are written one file per
  content-addressed identity under a caller-supplied evidence base, plus one
  appended index line — repeats take a new ordinal and cannot overwrite or merge
  into earlier evidence, and re-reading re-derives every identity. `validation.py`
  is the only module here that writes, and only beneath that base. Note the name
  collision with the P4.1 proposal package's `validation_plan` *field*, which is
  a list of check descriptions: this is a separate document with its own
  `hrca-validation-plan` generator and `plan:` identity. One runner rule is
  worth stating where a future change would see it: `subprocess.run(timeout=…)`
  raises `subprocess.TimeoutExpired`, which is a `SubprocessError` and **not** a
  `TimeoutError`. Every bounded client call in `container_runner.py` therefore
  catches both names; catching only `TimeoutError` silently skips the kill, the
  removal, the staged-root cleanup and the timeout token (P5.5r1). A real
  timeout must run that lifecycle exactly once, and `timeout` means it
  *completed* — if any substep failed, `runner_failed` is returned instead.
  Killing the docker *client* is not stopping the container: a timeout can fire
  while the daemon is still creating it, so the kill and removal run before it
  exists and it then appears in `created` state, where `--rm` never reaps it
  (P5.5r2). `timeout` is therefore returned only after
  `_reconcile_after_timeout` has confirmed the **exact product-owned name** is
  absent — bounded by the fixed `RECONCILE_*` constants, which no caller, plan
  or candidate can reach or tune. Only that one name is ever inspected or
  removed; nothing is enumerated. A failed query is `unknown` and is never
  rounded up to absence.
  `ContainerRunner.run_candidate` is a **second, separate path** (P5.5a-r2) for
  the candidate check family: exactly three mounts (staged input read-only,
  staged output, and the candidate root's `files` directory read-only at
  `/candidate`) and one more literal entrypoint,
  `CANDIDATE_ENTRYPOINT = python /app/runner_syntax.py …`, which compiles the
  declared files and never imports or runs them. It mounts the root's `files`
  directory rather than the root itself, because the root is `0700` and the
  container is 65534: mounting the root would mean widening its mode, and the
  candidate must not be mutated at all. It also refuses unless the local image's
  immutable **ID** equals `RUNNER_IMAGE_DIGEST`, since `hrca-runner:v1` is a
  mutable tag; the digest is pinned in code *and* in
  `fixtures/validation/manifest.json`, so a rebuild that is not re-pinned fails
  the tests. `packaging/runner/Dockerfile` pins its base by manifest digest for
  the same reason — a floating `python:3.12-slim` tag let a rebuild silently
  swap the interpreter.
  The P5.5r3c1 setup-verification partition is `setup_verification.py` (the
  selector and its guard) with the entrypoint in `setup_verification_cli.py`.
  **A setup-only change is verified with
  `uv run python -m hrca.setup_verification_cli` and never with
  `unittest discover`**: discovery selects every module, and
  `test_candidate_syntax_integration` and `test_rule_delta_docker_integration`
  mount a
  candidate or run a handler in a real container whenever a daemon is
  reachable, which is how a "quick baseline check" once put a candidate in a
  container. The selector has no discovery: it runs a code-owned allowlist,
  refuses any other module *by name* before importing it, and while it runs an
  audit hook makes any attempt to start a process a failure and an import hook
  refuses the excluded modules. Those two integration modules stay exactly as
  they are and stay reachable through their own explicit route. The entrypoint
  is a separate module on purpose: `python -m hrca.setup_verification` would
  duplicate the state module and its guard in one process, so that form refuses
  instead of running something weaker than it appears to be.
- `fixtures/` — synthetic Python corpus used by the tests; `fixtures/memory/`
  holds the M4.1 session/store corpus with its `manifest.json`, and
  `fixtures/intent/manifest.json` the P5.3 hand-authored intent oracle.
  `candidate_fixtures/` holds the P5.4 frozen miniature repository with its
  oracle. It is a **separate root**, like `grammar_fixtures/` and
  `codemap_fixtures/`, because the Phase 1 scanner tests measure `fixtures/` by
  exact file, symbol and relation counts: a new Python file inside it would
  change those numbers and quietly rewrite what the baseline asserts.
- `evidence/m4.2/` — the bounded record of the two authorized capture sessions
  plus offline controlled cases; see its `README.md`.
- `tests/` — stdlib `unittest` tests (no third-party test deps).

## Commands

Use [uv](https://docs.astral.sh/uv/) for the environment. System `pip` may be
blocked on managed Python distributions by PEP 668 ("externally-managed-
environment"); never pass `--break-system-packages`.

```bash
uv sync                                        # create/refresh .venv, install the project
uv run python -m unittest discover -s tests -v  # run the test suite
uv run python -m hrca fixtures                  # scan the corpus, JSON to stdout
uv run python -m hrca.memory_cli verify fixtures/memory  # replay the M4.1 corpus
uv run python -m hrca.hook_capture report --spool evidence/m4.2/live/session-1  # M4.2 capture
uv run python -m hrca.memory_cli project --base <store-dir>  # M4.3 documents
uv run python -m hrca.intent_cli propose --intent <f> --scanner <f> --twin <f>  # P5.3 proposal
uv run python -m hrca.candidate_cli review --edit <f> --intent <f> --scanner <f> --twin <f> --repo <dir>  # P5.4 diff
uv run python -m hrca.validation_cli run --candidate <dir> --review <f> --evidence-base <dir>  # P5.5a evidence
uv run python -m hrca.setup_verification_cli  # P5.5r3c1 setup verification — no discovery, no container
```

## Contract (Phase 1)

- Parse with the stdlib `ast` module only; no runtime dependencies.
- Emit canonical JSON records under `files`, `symbols`, `relations`,
  `parse_errors`, `confidence`, and `grammar`. `grammar` is the bounded context
  of the grammar that read the tree — implementation family and `major.minor`
  only, never a path, platform, build or environment value — so a consumer can
  attribute a version-dependent `SyntaxError` instead of reading it as a source
  defect. It is context, never a verdict: an unparseable file stays a
  `parse_error`, and nothing claims another grammar would accept it.
- **Deterministic**: sorted records, canonical key order, stable IDs of the
  form `module.path.Class.method`; identical rescans are byte-identical.
- **No fabrication**: relation `target`s are the literal names in source —
  never resolved to definitions or file paths. A relation is emitted only when
  source evidence exists.
- **Explicitly unresolved**: dynamic imports (`importlib.import_module` /
  `__import__`) are emitted as `imports` relations with `status: "unresolved"`
  and a `confidence: "low"` state, not guessed.
- A `SyntaxError` in one file is recorded as a `parse_error`; scanning continues.

## Out of scope

LLM providers, semantic editing, UI, remote execution, multi-language support,
automated merges, and any guesswork about dynamic imports, reflection,
dependency injection, or runtime monkey-patching.

## Conventions

- Keep the scanner dependency-free (stdlib only).
- Use `uv` for the environment (see Commands); do not use system `pip` or
  `--break-system-packages`.
- When extending the record schema, bump `SCHEMA_VERSION`, register the step in
  the module's `MIGRATIONS` map, and add a fixture + test that exercises the
  change. An older version the registry can upgrade is migrated; a missing,
  malformed or *newer* version is refused with a bounded reason, never guessed
  at or half-read.
- Never commit secrets or generated artefacts; respect `.gitignore`.
