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

## Desktop client (P3.2)

A read-only PySide6 **IDE workspace shell** supervises a headless **application
boundary** over newline-delimited JSON on stdin/stdout. It opens a project root
through the boundary, lists a filtered, bounded project tree, opens documents
read-only, and still submits the deterministic read-only scan. The boundary is
the only place that imports the deterministic core (`scanner`, `planning`,
`report`) and the workspace filesystem policy; the client consumes only the
versioned contract in `hrca.contract`.

The shell presents the approved IDE layout (all presentation-only; no
semantics are invented):

- **Three primary panes side by side** — a thin, collapsible **Project
  Explorer** (`QTreeView`, populated from the boundary's filtered `get_tree`
  response, never a direct directory walk or `QFileSystemModel`); a central
  **Source Code** area with flat, closable, read-only `QPlainTextEdit` +
  `QSyntaxHighlighter` tabs opened via `get_document` (the client never reads
  files itself); and an independent right-hand **Code Map** pane
  (never nested inside Source Code) that can display the bounded
  `empty` / `loading` / `available` / `stale` / `conflict` / `unsupported`
  states — in P3.2 no Twin entity exists, so the honest default is `empty`.
  In P3.3 the pane follows the active supported `.py`/`.pyi` tab by default;
  a single monochrome lock button on its header pins the displayed projection
  to its source path (tab switches no longer replace it), and unpinning
  immediately follows the active tab again. Closing a pinned tab keeps the last
  rendered projection until unpinned.
- **One bottom utility panel** — a single full-width surface directly beneath
  all three panes, with one flat tab bar (`Agent Chat | Plan | Diff | Problems
  | Tests | Evidence`, Agent Chat first) and one disclosure chevron (`▾`/`▴`)
  at the far right that collapses the panel to its header row and restores the
  last usable height. **Agent Chat** (the default tab) carries a disabled
  composer and send action labelled "provider-backed chat unavailable" — no
  provider, credential, network or inference call is ever made — while the
  other five tabs surface the P3.1 plan, raw result, validation, limitations
  and outcome data. **Diff** is explicitly unavailable in this read-only
  slice: no code-proposal capability exists, so there is nothing to diff and
  no way to apply changes.
- **A single-row status bar** — a transient message plus six persistent fields
  (root, repository, file, Twin, provider, validation state).

As of **P4.4a** the shell is document-first rather than IDE-shaped. A compact
labelled navigation rail presents **Document** and **Preview** as the only
always-visible primary destinations, then **Versions** (the accepted-version
history with per-version Restore) and a collapsed **Advanced** disclosure that
groups the retained technical surfaces as **Source & Code Map** (the three
panes above), **Change Review** (Agent Chat, Plan, Diff and the raw candidate
metadata) and **Validation Evidence** (Problems, Tests, Evidence). The Working
Document is promoted to a full-height primary workspace with a document header
(file selector, New, Open, saved/unsaved state), a large editor and a footer
where Save is the primary action and the candidate actions (Create candidate /
Review candidate / Adopt) are contextual.

As of **P4.5**, **Preview** is a read-only, version-bound candidate review
surface fed by a new `preview_document` boundary read-model (no package
execution, provider call or version-state change). It names the bound document
and exact revision, labels the record **Candidate** or **Accepted Version** and
its bounded state (current / stale / invalid / insufficient-evidence), states
the deterministic-fixture provenance (never AI-generated from the prose), and
lists the fixed quotation inputs (`subtotal`, `member`, `region`), result fields
(`discount`, `shipping_fee`, `regional_fee`, `total`), a business-rule summary
and a validation-evidence summary with its limits. The interactive Run button is
removed; Preview never executes a package or presents example output as a live
result.

As of **P4.5a**, document entry is repaired: a newly created document appears in
the selector and is selected immediately (no destination switch or manual
refresh); document names are normalized (surrounding whitespace trimmed,
compared case-insensitively) and uniqueness is enforced at the store boundary,
with blank/path/traversal/reserved-Windows names rejected and a collision refused
by name; and after a successful save a single contextual **Create preview**
action appears (hidden while dirty or pending) that runs only the existing
deterministic candidate path.

As of **P4.5b**, Preview and Versions are strictly document-bound and stateful.
A document with no Candidate and no Accepted Version shows a calm empty state —
no global quotation-fixture schema, package metadata or validation wording; the
fixture's form/result/business-rule detail appears only for a stored
Candidate/Accepted record that validates its binding to the quotation package.
Candidate, current app, accepted app with newer (unapplied) requirements, unsaved
text, out-of-date, invalid and insufficient-evidence are distinct states; the
badge and body use plain language, never raw ids. Versions distinguishes Save
(storing requirements) from adoption and shows each accepted app with the
document revision it accepted.

As of **P4.6**, the Working Document destination is replaced by a compact,
application-owned **document library explorer** for non-programmers. A narrow,
resizable, collapsible pane immediately right of the navigation rail shows an
expandable folder/document tree and a recoverable **Trash**, so opening a
document is one click (no dropdown-plus-Open step). It provides New document /
New folder / Rename / Move / Trash / Restore. This is an organiser only — not a
general file manager, a source tree, a filesystem browser, cloud sync or app
context. Every folder and document carries an opaque stable id; names are
normalized and must be unique among *siblings* (folders and documents together),
so identity never depends on title, order, path or tree position. Rename and
move change only display metadata / the parent relationship — they never rewrite
a document id, revision, candidate/accepted record, package identity or evidence
binding. Legacy flat documents are migrated to the root idempotently and
crash-safely, preserving ids, content, revisions and duplicate names; new
conflicting names are blocked until a legacy collision is resolved explicitly.
Trash is recoverable (no permanent deletion); trashing a non-empty folder asks
for confirmation, and restore never overwrites live data — a name collision is
refused with a clear message. Switching away from a document with unsaved edits
offers Save / Discard / Cancel. The three actions remain distinct: **Save**
stores requirements only, **Create preview** builds the deterministic fixture
Candidate (never from the prose), and **Use this version** (adoption) is the
separate, explicit, revalidated step that makes an Accepted Version — it is
never automatic.

All visual values live in the desktop-only design system `hrca/style.py` —
light and dark palettes (auto-selected from the operating-system appearance),
a 4 px spacing scale, corner radii, typography, component geometry, and a Qt
style sheet. State colours are always paired with a word; body and syntax text
meet the WCAG 4.5:1 contrast threshold in both palettes (checked by
`hrca.style.contrast_ratio`).

The contract (`hrca/contract.py`) defines:

- `CONTRACT_VERSION` (`3.6.0`) — any other version is rejected,
- the request/result envelopes and the client-generated `correlation_id`
  echoed verbatim in every response,
- the allowed read-only action names — the scan pipeline (`scan`, `read`,
  `analyze`, `inspect`, `plan`) plus the workspace actions (`open_project`,
  `get_tree`, `get_document`); every write/Git/command/network/provider action
  is rejected,
- the bounded error codes (`malformed_request`, `invalid_request`,
  `unknown_contract_version`, `action_not_allowed`, `message_too_large`,
  `internal_error`, `project_not_open`, `path_not_found`, `path_not_allowed`,
  `path_not_readable`, `unsupported_type`, `file_too_large`) whose messages
  never echo caller text, requested paths or file contents,
- `MAX_MESSAGE_BYTES` (1 MiB) plus the workspace limits `MAX_TREE_ENTRIES`,
  `MAX_TREE_DEPTH` and `MAX_DOCUMENT_BYTES` (64 KiB) that bound tree and
  document output,
- the bounded Memory read actions (`get_memory_documents`, `get_memory_record`)
  added in 3.6.0, with their own limits (`MAX_MEMORY_RUNS`,
  `MAX_MEMORY_ID_CHARS`) and error codes (`memory_run_not_found`,
  `memory_record_not_found`, `memory_kind_not_supported`, `memory_not_readable`).
  The increment is additive: every earlier action keeps its name, meaning and
  version rule.

The workspace policy (`hrca/workspace.py`) lists every ordinary file and folder
below the accepted root — not only Python files — while still excluding
`.git`, `.venv`, `__pycache__`, `node_modules`, `build` and `dist`, skipping
symlinks, and rejecting `..` traversal and symlink escape outside the accepted
root with bounded errors. Each file carries a render `kind` (`source` /
`preview` / `binary` / `unsupported`); the Project Explorer draws each folder's
disclosure indicator as a fixed 20 px chevron slot painted by a
`QProxyStyle` — a right-pointing chevron when collapsed and a down-pointing one
when expanded — so toggling a folder never shifts its label, child indentation
or row geometry (leaf folders show no indicator). `get_document` returns a
`source` result for Python files, a clearly labelled read-only `preview` for
common text/config formats, and a bounded `unavailable` result (with a fixed
`reason`) for binary, unsupported, missing, unreadable or oversized files.

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
uv run python -m hrca.client            # launch the IDE workspace shell
uv run python -m hrca.client --scan-once   # headless supervised scan (defaults to repo fixtures)
```

The GUI starts with no project open; click **Open Project** to choose a root
with a directory chooser. The boundary validates the root, returns the filtered
tree, and each tree click opens a read-only document via `get_document`. The
**Run read-only scan** button scans the opened root.

The default scan root for `--scan-once` is resolved by
`hrca.client_core.default_fixture_root`: in source mode it is the repository
`fixtures/` directory (found relative to the module, not the current working
directory); in a frozen build it is the bundled fixture data under
`sys._MEIPASS`. An explicit path argument overrides it.

To exercise the boundary directly, without the graphical interface, pipe one
request per line:

```bash
printf '%s\n' \
  '{"contract_version":"3.2.0","correlation_id":"demo","action":"scan","path":"fixtures","task":{"task_id":"P3.2","title":"t","request":"r","repository_context":{"status":"Unverified"},"allowed_actions":["read","analyze","scan"],"constraints":["Read-only"],"acceptance_criteria":["no-change"],"risk_level":"low","approval_required":false}}' \
  '{"contract_version":"3.2.0","correlation_id":"demo2","action":"open_project","path":"fixtures"}' \
  '{"contract_version":"3.2.0","correlation_id":"demo3","action":"get_tree"}' \
  | uv run python -m hrca.boundary --serve
```

The `open_project` accepts the root; the later `get_tree` in the same loop
lists it — the boundary keeps one accepted root per process.

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
open the GUI, then click **Open Project** to choose a project root (the bundled
`fixtures` folder, or any local project).

## Advisory hosted planning (P4.2b)

One deliberately constrained hosted-provider interaction: an explicit,
user-confirmed, low-budget DeepSeek request that returns a schema-validated
**advisory** planning result for the current Intent Delta. It is advisory only —
it never generates or applies source changes.

- **Two-step, user-confirmed.** `prepare_advisory` builds an itemized disclosure
  manifest offline (provider/model, every context item with its byte size, the
  request-byte/context-item/output-token/timeout caps, the one-attempt policy,
  and a data-egress statement). `plan_advisory` performs the single request only
  with an explicit `confirmed: true`; cancellation sends nothing.
- **Fixed, code-owned provider.** The official `https://api.deepseek.com`
  origin, bearer authentication, and the single allowlisted model
  (`deepseek-flash`) are constants in `hrca.deepseek`/`hrca.deepseek_transport` —
  never user-configurable, no arbitrary endpoint, model, proxy or fallback.
- **Authority.** The deterministic P4.1 proposal (target scope, source anchors,
  preserved constraints, baseline, stale/conflict decisions) stays authoritative.
  Only `clarification_needs`, `impact`, `assumptions`, `risks` and ordered
  `plan_suggestions` may come from the provider, always under
  `provider_suggested`.
- **Deny before network.** Context is rejected (before any socket is opened) when
  it is outside the project root, secret-like, binary, unsupported, over a
  byte/item/token limit, stale, or missing required anchors — and the limitation
  is reported truthfully rather than silently filled in.
- **Offline by default.** Normal launch, scan, `--serve` and readiness never
  import or touch the transport; the provider request is reachable only through
  the explicit confirmed action.

## Document-driven Builder foundation (P4.3)

The first executable foundation for Specification 2.0: a hand-written
**quotation-rules** reference package is validated, rendered through a fixed
form UI, and run only through an isolated container runner.

- **Versioned package contract** (`hrca.app_package`). A package is data only:
  it declares approved form/result fields, a fixed runtime identity, and a
  named handler resolved from a code-owned allowlist. It cannot carry a script,
  HTML, command, mount, environment or dependency-install field — those are
  rejected as unknown keys before any runner starts.
- **Hand-written fixture** (`hrca.runtime_handlers`). Member discount, free
  shipping at/above the 100.00 threshold, per-region fee, non-negative subtotal,
  and half-up two-decimal rounding — proven by acceptance examples.
- **Isolated runner** (`hrca.container_runner`). Executes the trusted handler
  inside a reviewed Linux container only after a fail-closed
  availability/isolation preflight: no network, non-root,
  `no-new-privileges`, `--cap-drop ALL`, read-only rootfs, bounded
  CPU/memory/PID/wall-time, staged input only, and validated output collection.
  No host home, credential, repository or Docker socket is ever mounted, and
  there is no host-Python fallback.
- **Boundary + Builder tab** (`get_package` / `run_package`). The desktop
  renders the fixed form, runs the named package, and shows bounded results or
  an understandable blocked/failed state. A missing runtime reports
  `runtime_unavailable` rather than falling back to the host.

The container image is defined (but not built here) at `packaging/runner/`;
building it requires a running Docker daemon.

## Candidate-package readiness (P4.7)

The offline safety and correctness seams needed before the first real
document-to-app generation flow — **not** a provider-generation task. It
validates, binds, stages and independently verifies a bounded candidate package
that a later, separately authorized flow could originate, and it fails closed on
arbitrary code, dangerous controls, evidence mismatch and verifier tampering.

- **Bounded candidate-package contract** (`hrca.candidate_package`). A versioned,
  data-only record that wraps a `package` manifest (which must be byte-identical
  to one of the code-owned variants), a `provenance` token, an exact `binding`
  (Working Document revision id + content fingerprint + runtime + protected
  verifier identity), and an `evidence` list. Any `script`/`code`/`html`/
  `import`/`path`/`command`/`url`/`network`/`install`/`dependencies`/`mount`/
  `env`/`dockerfile`/`image`/`entrypoint`/`args` field is rejected as an unknown
  key; the renderer schema is code-owned, so a candidate cannot alter form/result
  fields, handler or rules.
- **Protected verifier** (`hrca.verifier`). A code-owned offline oracle with a
  fixed identity and two reviewed, hand-authored benign rule variants (the
  reference `quotation-rules` and the alternative `quotation-rules-alt`), each
  pinned by frozen regression cases. `verify` compares a candidate's claimed
  evidence to the frozen expectations — missing, extra, malformed or mismatched
  evidence is refused — and it never executes a package or imports the in-image
  handler (no host fallback). The expectations cannot be edited by candidate
  input.
- **Staging seam** (`stage_candidate_package`). Validate → bind → verify →
  report a read-only evidence state (`deterministic_fixture` /
  `manually_validated_variant` / `valid_candidate` / `invalid` / `blocked` /
  `insufficient_evidence`). It never executes a package, adopts a candidate,
  persists anything, or makes a provider/credential/network/token call; a valid
  candidate whose runtime is unavailable is reported `blocked`.
- **Honest runner feasibility.** The isolated runner executes only the baked-in,
  code-owned handlers resolved from `hrca.runtime_handlers`; it cannot execute
  arbitrary or provider-produced code. The runner image is not built in this
  environment (no Docker daemon in the WSL distro), so validation/staging is
  exercised offline and the runtime is reported `runtime_unavailable`.

## Declarative rule-delta contract (P4.7a)

A bounded, parameterized, data-only **rule delta** replaces P4.7's byte-match
selection of prewritten variants, so a later provider can return a genuinely new
bounded rule change without ever supplying code.

- **Delta schema** (`hrca.rule_delta`). A versioned record of `result_kind` +
  `changes` (only `set_parameter` against an allowlisted `rule_id` +
  `parameter_id` with one exact typed decimal value) plus optional
  `clarification_questions` / `unsupported_requirements`. Anything else —
  code/script/import/command/dependency/path/mount/env/network/UI/runtime/
  verifier — is rejected as an unknown key. Duplicate/conflicting changes,
  unknown ids/operators, out-of-range, over-precise or malformed decimals and
  fingerprint mismatch are refused. `resolve_delta` returns a bounded
  `{rule_id, parameters}` mapping; raw code never enters the path.
- **Two bounded families, no byte-matched variants.** The quotation family
  supports changing only the member-discount parameter (5% → 10%:
  `{subtotal 200, member, west}` yields discount 20 / total 180, while
  non-member, shipping threshold, regional fees and invalid input stay
  unchanged). The held-out **late-return-fee** family (3/day capped at 30)
  supports changing only the cap (30 → 24); below-cap, boundary, above-cap and
  negative input are proven.
- **Binding + independent oracle** (`hrca.delta_candidate`, `hrca.delta_verifier`).
  The candidate binds the Working Document revision/fingerprint, the
  accepted-baseline fingerprint, the delta fingerprint, the runner identity and
  the protected verifier identity; evidence is verified against a code-owned
  oracle that recomputes expected results **independently** of the runner
  evaluator and candidate data. Stale/forged/mismatched states fail closed and
  cannot become adoptable.
- **Real isolated execution** (`stage_rule_delta` / `run_rule_delta`). Reviewed
  deltas execute only in the isolated `hrca-runner:v1` container (network none,
  non-root, read-only rootfs, resource-bounded); invalid deltas/inputs are
  rejected before any container starts. No provider, credential, token,
  document-interpretation, host-Python fallback, repository/Git or adoption
  side effect is introduced.

## Provider-to-rule-delta interpretation (P4.8)

The first honest provider-backed document-interpretation path: one explicitly
confirmed DeepSeek request may translate one current saved **synthetic**
quotation requirement into the existing `hrca.rule_delta` 1.0.0 data-only
contract — never into code, commands, dependencies, paths, environment, UI,
runtime or verifier settings.

- **Distinct capability** (`prepare_rule_delta` / `interpret_rule_delta`). A
  separate action pair, never a widening of the P4.2b advisory flow.
  `prepare_rule_delta` builds a visible preflight disclosure **offline** (no
  network, no credential); `interpret_rule_delta` performs exactly one
  user-confirmed request and never adopts or writes the repository.
- **Exact binding.** Every run binds the document id, head revision id, content
  fingerprint, accepted-baseline fingerprint, schema, allowlisted model
  (`deepseek-flash`, effective version DeepSeek-V4.1-Flash), disclosure manifest,
  delta fingerprint, runner identity (`hrca-runner:v1`) and verifier identity
  (`hrca-rule-delta-verifier:1`). A changed document or accepted predecessor
  makes any in-flight/returned result stale and non-adoptable.
- **Model routing and pricing snapshot** (`hrca.rule_delta_interpret`,
  `hrca.deepseek`). The requested API id (`deepseek-flash`), the canonical id
  (`deepseek-flash`) and the effective routed version (DeepSeek-V4.1-Flash) are
  kept distinct; the retired `deepseek-v4-flash` /
  `deepseek-v4-flash-vision-exp` aliases are recorded as compatibility aliases
  that only temporarily route to V4.1-Flash and are never dispatched. The fact
  snapshot records its verification date (2026-09-12) and source URLs.
- **Fail-closed pre-network limits** (`hrca.rule_delta_interpret`,
  `hrca.delta_transport`). One request, zero retries, zero paid repairs, a 12 KiB
  serialized body, ≤4,096 input tokens, ≤1,024 output tokens, a 45-second
  provider deadline, a 120-second workflow deadline, an atomic US$0.01 local
  reservation (from the verified peak $0.30 cache-miss input / $1.20 output per
  1M), and a fixed origin/model/auth with thinking explicitly disabled. Unknown
  pricing, insufficient reservation, unknown usage after dispatch, a stale scope
  or an unconfirmed disclosure all fail closed.
- **Retrievable-vs-metadata readiness (P4.8b).** The redacted local readiness/
  status surface now distinguishes `no_profile`, `missing_credential`
  (metadata-only / orphaned), `credential_unretrievable` (a bounded read
  failure) and `configured` (an actually-retrieved non-empty secret) — instead
  of a single presence-derived "configured". The readiness check reads the
  credential transiently inside the backend credential boundary and never
  surfaces it. A missing, orphaned or unreadable credential fails **before
  transport construction** with zero HTTP, `sent: false` and a recovery
  instruction, so profile metadata alone can never produce a misleading
  "configured" success.
- **Exactly one structured outcome.** The provider output is either a valid
  `hrca.rule_delta` 1.0.0, `clarification_required`, or `unsupported`. Prose-
  wrapped JSON, unknown fields, code/script/command/path/env/network/UI/runtime/
  verifier fields, unknown operations/families/parameters, duplicate changes and
  invalid decimals are rejected before staging or runner startup.
- **Code-owned verification path.** A valid quotation delta becomes a reviewable
  Candidate only after strict delta validation, **real isolated runner
  execution** over the code-owned protected inputs, and **independent-oracle**
  match — never merely on the provider's word. The protected oracle cases never
  enter the provider context. Adoption remains a separate explicit action.
- **Calm user-visible states** cover preflight, cancellation, sending, usage
  known/unknown, clarification, unsupported, invalid output, runner unavailable,
  verification failed, stale, over-limit, pricing/reservation failure and
  reviewable Candidate. The live request is separately gated and is never made
  by save/open/select/preview/cancel.
- **Desktop wiring.** After a save, the single contextual **Build preview**
  action (relabelled **Update preview** once a reviewable candidate exists for
  the current document) prepares the offline disclosure, shows it in a
  confirmation dialog that **defaults to Cancel**, and only on explicit
  confirmation dispatches one `interpret_rule_delta` request. The disclosure
  names the DeepSeek recipient, the requested API model (`deepseek-flash`), the
  effective routed model (DeepSeek-V4.1-Flash), the compatibility/retirement
  warning, the verification date, the exact outgoing item names and byte sizes,
  the policy/retention warning, the one-request/zero-retry/zero-repair policy,
  the token/byte/deadline caps, the US$0.01 reservation, and that **US$8 is not
  an enforced account cap**. Prepare, cancel, document switching and navigation
  retrieve no credential and make zero HTTP calls; duplicate clicks and
  late/stale confirmations cannot create a second request. A reviewable
  Candidate is never auto-adopted — **Use this version** remains the separate,
  backend-revalidated adoption step.

## Developer Memory — replayable offline contract (M4.1)

The durable foundation for bounded coding-agent runs. It is deliberately
**offline and read-side**: a bounded synthetic session in, a versioned,
normalized, evidence-linked store out, and the same terminal `AgentRun` state
on every replay. There is no hook installation, no real session capture, no
transcript parsing, no provider request and no model egress. The core is
**source-neutral**: Claude Code hook field mapping belongs to a later adapter
(M4.2) and must never define the canonical domain.

```bash
uv run python -m hrca.memory_cli replay fixtures/memory/sessions/completed.json
uv run python -m hrca.memory_cli summary fixtures/memory
uv run python -m hrca.memory_cli verify fixtures/memory
uv run python -m hrca.memory_cli migrate fixtures/memory/stores/legacy_0_9_0.json
```

### Schema and records

`hrca.memory` emits `MEMORY_SCHEMA_VERSION = 1.0.0`. One bounded source session
normalizes into one `AgentRun` aggregate:

| Record                     | Contents                                                        |
| -------------------------- | --------------------------------------------------------------- |
| `project` / `work_package` | adapter-namespaced descriptors of the repository and unit of work |
| `agent_run`                | identity, reported state, ingest ledger, source timestamps as evidence |
| `agent_run_event`          | typed, validated events in monotonic ingest order                |
| `change_set`               | the bounded paths/entities an event reports as touched           |
| `evidence`                 | bounded artifact **metadata** (kind, reference, byte size, digest) |
| `decision`                 | a recorded decision with a bounded, redacted rationale           |
| `code_entity_link`         | a link to a file path or a `module.path.Class.method` locator    |
| `rejection` / `quarantine` | fail-closed records; they never carry source content             |

### Identity, ordering and deduplication

- **Namespaced identity.** `run:<adapter>:<session>:<run-token>`; adapter and
  session fold to bounded tokens so a raw source string can never inject an id
  separator.
- **Identity preference.** A stable `source_event_id` owns identity; otherwise a
  stable `source_sequence`; otherwise a deterministic SHA-256 fingerprint over
  canonical **non-secret** fields. Redaction runs *before* fingerprinting, so a
  secret can never influence an identity.
- **Ordering.** A **per-run single writer** assigns the monotonic
  `ingest_ordinal`. Source timestamps are retained as evidence only and never
  decide order or state.
- **Idempotency.** An identical redelivery is a no-op: no new logical event, no
  ingest advance, no state change.
- **Quarantine.** The same identity with different canonical content is
  quarantined — it never overwrites evidence and never advances run state. The
  quarantine record stores only fingerprints, never the incoming content.

### Terminal state

Terminal state is owned **exclusively** by a typed, validated `run_terminated`
transition carrying an outcome from `completed` / `failed` / `cancelled` /
`blocked`. It is never inferred from the last message, command text, timestamp
order, page order or agent narrative.

| State              | Meaning                                                            |
| ------------------ | ------------------------------------------------------------------ |
| `completed`        | the only success state                                             |
| `failed` / `cancelled` / `blocked` | distinct typed terminal outcomes                    |
| `missing_terminal` | the bounded stream ended with no typed terminal transition         |
| `unknown_outcome`  | `run_terminated` carried an unrecognized outcome                   |
| `unsupported`      | an event type the contract does not model was encountered          |

Every terminal state is **absorbing**: a later event may not move a run out of
one, so a trailing "actually everything is fine" message cannot rescue a failed
run. On `fixtures/memory/sessions/invalid_transition.json` the two refused
events are recorded as `invalid_transition` rejections while the validated
terminal transition still stands.

### Unsupported cases and fail-closed behavior

- **Unsupported event types** are recorded explicitly and resolve a
  non-terminal run to `unsupported`, which is absorbing. The contract therefore
  refuses to certify a stream it could not fully interpret. An adapter must map
  every event type it emits onto one of the four contract types, or route it to
  `run_progress`.
- **Malformed events** (not a mapping, missing/non-string `event_type`) are
  rejected before they can influence identity, ordering or state. Each gets its
  own record via a dedicated rejection ledger.
- **Oversized payloads** are rejected rather than truncated, because truncating
  would silently alter evidence. Free-text fields are truncated to a bounded
  length and the truncation is counted.
- **A finalized store is a closed snapshot.** A genuinely new event is refused
  with `run store is finalized`; an identical redelivery stays a no-op and a
  conflicting one stays a quarantine.

### Privacy before durable write

Path policy, payload bounds and secret redaction are applied **before** any
record is assembled, so no raw payload and no secret-like value can reach a
durable write:

- **Excluded paths** are never persisted — `.env*`, `secrets/`, `private/`,
  `.git/`, `.ssh/`, `node_modules/`, `id_rsa`, `*.pem`, `*.key`, `*.p12` and
  peers. An evidence record whose artifact reference is excluded is dropped
  entirely, so an excluded artifact is not reachable even indirectly.
- **Secret redaction** covers PEM private-key blocks, provider token shapes
  (`sk-`, `sk-ant-`, `ghp_`/`gho_`/`ghs_`/`ghu_`, `github_pat_`, `xox[baprs]-`,
  `AKIA…`, `AIza…`), JWTs, `Bearer` values, URL userinfo, and `key: value` /
  `key = value` pairs for credential-bearing key names. It is deterministic and
  **idempotent**.
- **Artifact content is never stored.** Evidence carries a reference and bounded
  metadata only; a content-bearing field (`content`, `transcript`, `output`,
  `stdout`, …) is dropped and counted. A transcript path is a reference, not
  permission to ingest transcript content.
- Every record carries a bounded, content-free `privacy` accounting block
  (`redactions`, `truncations`, `exclusions`) and the run carries the aggregate.

### Persistence, migration and recovery

`hrca.memory_store` is the **only** code allowed to read, write or enumerate
memory storage. It lives outside the selected repository under a per-user
app-data `memory/` namespace keyed by the run id, so two runs never collide and
the selected repository is never written to. The rules mirror `hrca.twin_store`:

- **Atomic write** — temp file, flush, `fsync`, `os.replace`. A failed or
  interrupted write leaves the previous valid store intact and readable.
- **Fail-closed load** — an unreadable, unparsable, future-versioned or
  non-migratable store returns `(None, reason)` and never overwrites the
  on-disk store.
- **Verified additive migration** — `MIGRATIONS` maps an older
  `schema_version` to an upgrade. A migration is *verified* after the fact: it
  must preserve event identities, evidence identities and the terminal replay
  result of the store it came from. A raising migration returns
  `migration failed`; one that would change replay meaning returns
  `migration changed identity, evidence or replay result`. Both are explicit
  blockers, and both leave the prior readable state untouched.

`fixtures/memory/stores/legacy_0_9_0.json` is the pre-freeze draft of this
contract (it predates `code_entity_links` and run-level privacy accounting),
retained so the additive-migration path is proven by fixture rather than
asserted. It migrates to a store that is **byte-identical** to a fresh replay of
its source session.

### Fixture corpus

`fixtures/memory/manifest.json` states each case's expected terminal state,
counts, rejection reasons and quarantines; the corpus tests assert exactly those
values. `sessions/` covers completed, failed, cancelled, blocked,
missing-terminal, empty, unknown-outcome, unsupported-event, invalid-transition,
malformed, duplicate, conflicting, out-of-order and privacy sessions;
`stores/` covers a supported older schema and an unsupported one.

### Adapter port

An adapter's entire surface is one bounded `SESSION_KEYS` mapping —
`adapter`, `session_id`, `run_key`, `project`, `work_package`, `events` — whose
events use the bounded `SOURCE_EVENT_KEYS` contract. Everything outside that
shape is ignored and nothing outside it is reconstructed. M4.2 delivers the
first port of that surface: documented Claude Code hook JSON, mapped in an
adapter module the core never imports. A hook's `transcript_path` becomes an
`evidence` `artifact_ref` only, which is the whole of M4.1's permission to
reference a transcript.

## Claude Code hook capture and import (M4.2)

`claude_code_hooks.py` translates documented Claude Code hook payloads onto the
M4.1 boundary; `hook_capture.py` is the smallest local collector that feeds it.
The canonical domain stays source-neutral — no provider name reaches a canonical
record, and the core never imports the adapter. The capture seam is a new
architecture rule, enforced like the existing ones.

### Hook-event map

Eight events are installed and modelled: `SessionStart` → `run_started`;
`UserPromptSubmit`, `PreToolUse`, `PostToolUse` and `PostToolUseFailure` →
`run_progress`; `Stop` (with `stop_hook_active` false) → `run_terminated` /
`completed`; `StopFailure` → `run_terminated` / `failed`; `SessionEnd` →
`stream_ended`. A continuation `Stop` is progress, never a conclusion.

Terminal state is decided only by typed fields. A `SessionEnd` reached without a
conclusion is terminated from the documented `reason`: `clear`, `resume`,
`logout` and `prompt_input_exit` mean cancelled; anything else — including the
documented catch-all `other` — is carried through as an explicit **unrecognized**
outcome, which the contract reports as `unknown_outcome`. No message text,
command text, timestamp order or agent narrative can decide a terminal state.
Every other event the client documents is disclosed as an omission, and an event
outside the documented surface is fail-closed.

### Capture posture

Capture is offline and explicitly configured. The collector reads one hook
payload from stdin, validates it, translates and redacts it **in memory**, and
appends only the resulting bounded records to a spool. Raw hook JSON is never
written anywhere. Redaction and bounding happen before persistence, so a spool
and a store hold nothing that has not already been through the M4.1 policy.

Dropped before anything is written: prompt text, assistant message text, tool
responses and error text (a SHA-256 digest and a character count survive
instead); the transcript (a bounded reference only, and stored with no location
at all when it sits outside the session root); the session root as an absolute
path (the spool keeps a digest); every path outside the session root; and any
unrecognized tool argument, whose **name** is disclosed while its value is
dropped. Each event carries an explicit account of the documented fields present,
the documented fields missing, the fields dropped, and the field names this
adapter does not recognize.

**One field is retained, because it is a classification rather than text.**
`StopFailure` is the surface's only typed run-failure signal, and its `error`
field is the classification the client itself matches on. That class is kept as
typed failure evidence when — and only when — it is one of the documented
classes; any other value, including free text, is dropped rather than kept, so
arbitrary content can never survive merely because it arrived in an enum-shaped
field. `StopFailure.error_details` and `StopFailure.last_assistant_message` stay
under the content policy, and the identically named `PostToolUseFailure.error`
is documented as free text and stays redacted too. The rule is per event and per
field, not a global exception, and it never affects terminal state: an
unrecognized class is still reported as `failed`, never as success.

Tool identity comes from the client's own `tool_use_id` and prompt identity from
`prompt_id`, so identity is independent of observed content — which is what lets
a redelivery whose content changed be recognized as a conflict instead of being
accepted as a new event.

### Commands

```bash
uv run python -m hrca.hook_capture collect --spool <dir> --root <session-root>
uv run python -m hrca.hook_capture report  --spool <dir>
uv run python -m hrca.hook_capture import  --spool <dir> [--base <dir>]
uv run python -m hrca.hook_capture cleanup --spool <dir>
```

Re-importing a spool is deterministic and byte-stable; a spool belongs to
exactly one session and refuses a payload naming another. `cleanup` removes a
spool, and refuses any directory that is not one.

### Captured evidence and limits

`evidence/m4.2/` holds the bounded record of the two authorized capture sessions
and a set of offline controlled cases (conflicting, malformed, unmodelled,
unsupported). One session reached `completed`; the other reached a genuine
non-completion that the contract reports as `unknown_outcome`, so that work
package returned **Partial** rather than the requested `failed`/`cancelled` —
print mode emits only the catch-all `SessionEnd` reason, so the documented hook
surface cannot type a cancellation. See `evidence/m4.2/README.md` for the full
account.

**M4.2 capture is not a product feature.** No hook installation, no background
collector, no provider access and no Memory UI exist. Capture runs only when a
caller explicitly configures hooks and points them at the collector. Search,
Resume and timeline remain unbuilt (M4.4); correction history, export and Code
Twin linkage remain unbuilt (M4.5).

## Evidence-linked documents (M4.3)

`memory_docs.py` projects four developer documents from normalized records:
**Session / Daily Summary**, **Change & Verification Record**, **Decision
Record**, and **Known Issues & Next Actions**. It reads stores, never raw hook
JSON, transcripts, logs or provider output.

```bash
uv run python -m hrca.memory_cli project --base <store-dir> [--origin live|offline]
```

### Document authority

A document is a **view of records, never a source of truth**. Every material
claim carries a provenance label and either resolves to a stored record identity
or is reported as unresolved with a bounded reason. The projector assembles each
statement from typed fields — there is no free-text generator — so a document
cannot assert something no record supports.

| Provenance | Meaning |
|---|---|
| `observed` | a value the contract validated and stored itself |
| `reported` | source-supplied text the contract retained; not verified |
| `inferred` | derived by the projector from stored records; not a stored fact |
| `user-confirmed` | a correction a human confirmed |

`user-confirmed` is **declared and unreachable**: no record type in schema
`1.0.0` carries a human correction, so the projector reports the gap in
`unsupported_provenance` rather than minting a label nothing justifies. A
source's own report can never be upgraded to observed fact — a decision summary
is always `reported`, and a terminal state is always `observed`.

### Evidence links and fail-closed behaviour

Every claim lists `links` (`run`, `event`, `evidence`, `decision`, `change_set`,
`code_entity_link`, `rejection`, `quarantine`, `project`, `work_package`) and an
`unresolved` list. A reference the store does not contain is never dropped and
never guessed: it is reported with `claim references a record that is not present
in the store`. A store whose schema version, generator or run record the
projector does not recognise is refused outright rather than half-projected.

### Retained limitations

- **No baseline.** Schema `1.0.0` records no revision identity, so every document
  reports `baseline.status: "unsupported"` instead of implying one.
- **No capture origin.** The schema does not record whether a run came from a
  live session or deterministic offline input. An operator may *declare* an
  origin with `--origin`; that declaration is then `reported`, and the document
  states that it is caller-declared. Without it, no claim may be read as
  live-session observation — an offline typed `failed` run stays visibly offline.
- **No verification result.** An artifact reference proves an artifact was named,
  never that anything was verified.
- **No adapter payload.** Documents stay source-neutral and render only canonical
  record fields, so provider vocabulary cannot enter one.
- **No clock.** No date is derived or rendered, so no "daily" bucket is invented
  from timestamps; `project` takes an explicit, caller-ordered run set and M4.4
  owns any timeline.

### Redaction

A document renders only fields the contract already redacted, bounded and
path-policed. Content it dropped — prompt text, assistant text, tool responses,
diagnostic text, transcript content — is not in a store and cannot reappear. An
evidence digest is reported as *present*, never as a value, because a digest is a
fingerprint of content this layer must not expose.

Reprojecting identical input is byte-stable, so a document can be diffed or
hashed and its evidence references stay stable across runs.

### The Memory read boundary (M4.3/v2a)

The desktop is architecturally prohibited from importing the Memory seam, so it
reaches Developer Memory only through two read-only protocol actions added in
contract `3.6.0`:

| Action | Request | Result |
|---|---|---|
| `get_memory_documents` | optional `runs`, `documents`, `origin` | the bounded document sets of at most `MAX_MEMORY_RUNS` runs |
| `get_memory_record` | `run_id`, `kind`, `record_id` | exactly one typed supporting record, through the projector's allowlist |

**No request carries a path.** Every store is rooted at the boundary-owned
`session.store_base` — the same app-data root the Twin, version and library
stores use — so a caller cannot influence which directory is read; a supplied
`root`/`path`/`store_base` field is ignored, and an architecture test asserts the
Memory handlers never read one.

**Resolution is by exact typed identity** `(run_id, kind, record_id)` inside the
named run, so a record id belonging to another run cannot resolve and no
substitute record is ever returned. An unknown run, an unsupported kind and a
missing id each fail closed with a bounded, catalogue-drawn error that never
echoes caller text or stored content. A store the projector does not understand
is refused rather than half-projected.

**The returned view is an explicit allowlist.** It is built by iterating the
allowlist and reading named fields — never by iterating the record — so a field
not named there cannot cross even if the contract later stores it. An event's
hook payload, a content fingerprint, a quarantine fingerprint and an evidence
digest are all absent; evidence exposes a `digest_present` boolean only, so the
digest value never leaves the boundary.

`client_core` mirrors the vocabulary the desktop needs — document types, record
kinds, and textual state, provenance and origin labels — because the desktop
cannot import the seam. A boundary test asserts the mirror stays identical to the
projector's own vocabulary, so the two cannot drift apart silently.

### The read-only Evidence surface (M4.3/v2b)

The desktop **Memory** destination turns a claim into its supporting record. It
shows a run selector, a document selector and the bounded claim list of the
selected document, and a read-only Evidence pane beside it.

**Every claim row states its own provenance, run state and limitations in
words.** Colour is decoration: a provenance chip always carries the provenance
text, and each row repeats `Run state: …` and any `Limit: …` so nothing depends
on a tint. A claim's support references appear as explicit buttons — `Open Run`,
`Open Evidence` — and the run status line above the list states the selected
run's terminal state, whether the snapshot is `finalized` or `stale`, whether the
baseline is `unsupported`, and whether a capture origin was declared.

**Activation resolves by exact typed identity.** A button carries the
`(run_id, kind, record_id)` the claim named; the request goes to the boundary,
which resolves only that record in that run, and the detail pane names the record
and its owning run. Nothing is ever selected by prose, row order, path, digest or
payload.

**Unresolved support stays visible and inert.** A dangling reference renders as a
disabled `Unavailable: <kind>` button whose tooltip carries the requested
identity and the bounded reason. It cannot navigate, is never hidden, never
substituted, and never counts as verified support.

**Obsolete actions are invalidated by generation.** The surface holds a
generation that advances whenever the result set, the run or the document
changes. A button captures the generation it was built under, and a late response
carries its own; a mismatched generation opens nothing and renders nothing, so a
switch can never surface stale or unrelated evidence.

**Nothing unsupported is claimed.** `unknown_outcome` reads `Unknown outcome (not
success)`; a deterministic `failed` run reads `Failed (not success)` and its
origin label says `Offline deterministic (declared by caller)`; staleness and a
baseline gap are reported as separate facts; and the surface never says
`current`, `verified` or `fresh`. The strongest statement it makes about
verification is the projector's own disclaimer — *never that anything was
verified*.

Offscreen interaction tests drive the real protocol path (surface → boundary →
surface) and cover navigation, per-target resolution, state fidelity, unresolved
targets, generation invalidation, accessibility (names, focus, non-colour cues)
and privacy negatives over rendered text, tooltips and accessible names.

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

- **Developer Memory is offline (M4.1) with one explicit capture path (M4.2).**
  M4.1 replays bounded sessions from the fixture corpus; M4.2 additionally maps
  documented Claude Code hook JSON through a collector that a caller must
  configure explicitly. Neither performs a provider request, credential access
  or model egress, and neither parses a transcript. Raw hook payloads, prompt
  text, assistant text and artifact content are never durably stored. No
  summary generation, search, Resume or Memory UI exists yet.

Out of scope entirely: LLM providers, semantic editing, UI, remote code
execution, multi-language support, and automated merges.
