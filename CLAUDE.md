# Human-Readable Code Agent

A deterministic static scanner for Python source trees — the **Phase 1
baseline** for a "code twin" project. No LLM, no network, no runtime code
execution at scan time.

## Layout

- `src/hrca/` — the scanner package (`scanner.py` is the core; `cli.py` the CLI).
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
  write nothing, so Memory authority is untouched. The M4.5/v2a package boundary
  is `memory_package.py` (two
  profiles: a least-disclosure `export` and a local-sensitive `backup`) with the
  offline operator CLI `memory_package_cli.py`; both are outside the desktop and
  reach no network, process or credential primitive. A `backup` is built from
  one verified cross-run snapshot (`capture_stores` / `verify_capture`) and
  carries its identity in the manifest, which staged recovery re-derives from
  the staged bytes before an active store is in scope.
- `fixtures/` — synthetic Python corpus used by the tests; `fixtures/memory/`
  holds the M4.1 session/store corpus with its `manifest.json`.
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
```

## Contract (Phase 1)

- Parse with the stdlib `ast` module only; no runtime dependencies.
- Emit canonical JSON records under `files`, `symbols`, `relations`,
  `parse_errors`, and `confidence`.
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
- When extending the record schema, bump `SCHEMA_VERSION` and add a fixture +
  test that exercises the change.
- Never commit secrets or generated artefacts; respect `.gitignore`.
