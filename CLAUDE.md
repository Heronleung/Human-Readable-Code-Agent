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
