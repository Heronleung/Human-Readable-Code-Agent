# Human-Readable Code Agent

A deterministic static scanner for Python source trees — the **Phase 1
baseline** for a "code twin" project. It parses Python with the standard-library
[`ast`](https://docs.python.org/3/library/ast.html) module and emits canonical
JSON records that later phases can turn into a navigable, twin-like
representation of the code.

This is **not** an IDE, an LLM agent, or a general-purpose analysis tool. It is
a small, bounded foundation: it extracts structure and emits only what the
source actually says.

## Setup (uv)

Requires Python 3.9+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

`uv sync` creates or refreshes the project `.venv` and installs the project
(editable) with its dependencies from `uv.lock`.

> **Why not `pip`?** On many managed Python distributions, `pip install` into
> the system interpreter is blocked by
> [PEP 668](https://peps.python.org/pep-0668/) ("externally-managed-
> environment"). Use `uv` (or an explicit virtualenv) instead — never pass
> `--break-system-packages`.

There are no runtime dependencies — the parser baseline is the standard
library.

## Usage

```bash
uv run python -m hrca fixtures      # scan the fixture corpus
uv run hrca-scan fixtures           # equivalent, via console script
```

Output is canonical JSON on stdout (indented, keys sorted, records sorted).

## Test

```bash
uv run python -m unittest discover -s tests -v
```

The suite runs with the standard-library `unittest` runner only (no pytest
required).

## Record schema

The scanner emits a single JSON document with these top-level arrays:

| Key             | Contents                                                        |
| --------------- | --------------------------------------------------------------- |
| `files`         | one record per scanned `.py` file (`path`, `module`, `syntax_status`) |
| `symbols`       | modules, classes, functions, async functions, parameters, variables |
| `relations`     | `imports`, `calls`, `returns`, `raises`, `inherits`             |
| `parse_errors`  | per-file `SyntaxError` records (the scan continues)             |
| `confidence`    | explicit states for items below `high` confidence               |

Symbols carry a `source_range` (`lineno`/`col_offset`/`end_lineno`/
`end_col_offset`) and are identified by stable dotted IDs such as
`app.service.Service.handle`. Relations carry the literal `target` name as
written in source, plus a `status` (`resolved` / `unresolved` / `recorded`).

## Desktop client (P3.1)

A minimal, read-only PySide6 desktop window submits one bounded scan task to a
headless **application boundary** over newline-delimited JSON on stdin/stdout,
and renders the deterministic plan, scanner evidence, limitations, validation
status and explicit no-change result. The boundary is the only place that
imports the deterministic core (`scanner`, `planning`, `report`); the client
consumes only the versioned contract in `hrca.contract`.

The contract (`hrca/contract.py`) defines:

- `CONTRACT_VERSION` (`3.1.0`) — any other version is rejected,
- the request/result envelopes and the client-generated `correlation_id`
  echoed verbatim in every response,
- the allowed read-only action names (`scan`, `read`, `analyze`, `inspect`,
  `plan`) — every write/Git/command/network/provider action is rejected,
- the bounded error codes (`malformed_request`, `invalid_request`,
  `unknown_contract_version`, `action_not_allowed`, `message_too_large`,
  `internal_error`) whose messages never echo caller text,
- `MAX_MESSAGE_BYTES` (1 MiB) for inbound and outbound messages.

The boundary writes exactly one JSON line per request, reserves stdout for
protocol messages only, and keeps `ensure_ascii=True` so the wire is pure
ASCII while non-ASCII content still round-trips losslessly.

### Launching the backend

The client resolves and launches the headless backend through the **same entry
executable** using the `--serve` argument sentinel, resolved via
`sys.executable` and `sys.argv` rather than assuming an installed interpreter:

| Context           | Launch command                                         |
| ----------------- | ------------------------------------------------------ |
| Source (venv)     | `[sys.executable, "-m", "hrca.boundary", "--serve"]`   |
| Frozen (PyInstaller) | `[sys.executable, "--serve"]`                        |

The frozen build uses `hrca/app.py` as its entry point: it runs the desktop
client by default, and the headless boundary when invoked with `--serve`.

### Running from source

```bash
uv sync --extra desktop                 # installs PySide6 (optional)
uv run python -m hrca.client            # launch the desktop window (defaults to repo fixtures)
uv run python -m hrca.client --scan-once   # headless supervised scan (defaults to repo fixtures)
```

The default scan root is resolved by `hrca.client_core.default_fixture_root`:
in source mode it is the repository `fixtures/` directory (found relative to the
module, not the current working directory); in a frozen build it is the bundled
fixture data under `sys._MEIPASS`. An explicit path argument overrides it.

To exercise the boundary directly, without the graphical interface, pipe one
request per line:

```bash
printf '%s\n' \
  '{"contract_version":"3.1.0","correlation_id":"demo","action":"scan","path":"fixtures","task":{"task_id":"P3.1","title":"t","request":"r","repository_context":{"status":"Unverified"},"allowed_actions":["read","analyze","scan"],"constraints":["Read-only"],"acceptance_criteria":["no-change"],"risk_level":"low","approval_required":false}}' \
  | uv run python -m hrca.boundary --serve
```

### Frozen build (Windows)

Build a one-folder distribution that bundles the fixture corpus. PyInstaller's
`--add-data` separator is `;` on Windows (`:` on Linux/macOS). The launcher
(`packaging/launcher.py`) is needed because `src/hrca/app.py` uses
package-relative imports:

```bash
uv sync --extra desktop --extra packaging   # installs PySide6 + PyInstaller
uv run pyinstaller --noconfirm --clean --name hrca-app \
  --paths src \
  --add-data "fixtures;fixtures" \
  packaging/launcher.py
```

The result is a complete, self-contained folder; the executable and its bundled
fixtures must stay together:

- executable: `dist\hrca-app\hrca-app.exe`
- bundled fixtures: `dist\hrca-app\_internal\fixtures\`

Run the frozen artifact from its folder:

```bash
dist\hrca-app\hrca-app.exe --scan-once   # supervised scan (non-empty evidence)
dist\hrca-app\hrca-app.exe --serve       # frozen headless boundary
```

Do not copy `hrca-app.exe` out of `dist\hrca-app` and run it standalone: the
one-folder build relies on `_internal\` (including the bundled fixtures) being
present next to the executable. Double-click `dist\hrca-app\hrca-app.exe` to
open the GUI, then click **Run read-only scan**.

## Scope and limitations

Determinism and no-fabrication are the core guarantees:

- **No name resolution.** A `target` is the literal name in the source; the
  scanner never resolves it to a definition or a file path, so it cannot
  invent call edges or import targets.
- **Dynamic imports stay unresolved.** `importlib.import_module(...)` and
  `__import__(...)` are emitted as `imports` relations with `status:
  "unresolved"` and `confidence: "low"` (target is `null` unless the argument
  is a string literal). Reflection, dependency injection, and monkey-patching
  are similarly out of scope and are never guessed.
- **Syntax errors do not stop the scan.** A file that fails to parse yields a
  `parse_error` record and no symbols; other files are still scanned.
- **Assignment scope.** Only `Name` assignment targets become `variable`
  symbols; attribute assignments (e.g. `self.x = ...`) and function-local
  defaults are not modeled as named symbols.
- **Cross-version note.** Identifiers are stable across identical rescans in
  the same environment. Expression rendering uses `ast.unparse`, whose exact
  spelling can vary slightly between Python minor versions.

Out of scope entirely: LLM providers, semantic editing, UI, remote code
execution, multi-language support, and automated merges.
