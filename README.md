# PrimaAgent

A chat-first, document-based coding agent built on a deterministic
source-evidence core. The core parses Python with the standard-library
[`ast`](https://docs.python.org/3/library/ast.html) module and emits canonical
JSON records; the desktop presents working documents, their accepted versions,
the Developer Memory read model and the provider-backed review workflow.

Source evidence — the scanner, the Structured Code Twin store, its Code Map
projection, the advisory context builder and the Memory-to-Code-Twin evidence
link — is **internal support** for orchestration, review, validation and
migration. It is deliberately not a product surface: the desktop exposes no
code-twin view, no source-tree pane and no Code Map.

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
| `grammar`       | the bounded grammar context that read the source                |

Symbols carry a `source_range` (`lineno`/`col_offset`/`end_lineno`/
`end_col_offset`) and are identified by stable dotted IDs such as
`app.service.Service.handle`. Relations carry the literal `target` name as
written in source, plus a `status` (`resolved` / `unresolved` / `recorded`).

### The grammar context

Scanner schema `1.1.0` adds one block: the grammar that produced the scan.

```json
"grammar": { "implementation": "cpython", "version": "3.11" }
```

It carries exactly two facts — the implementation *family* and the
`major.minor` **grammar** version — and nothing else. Executable paths, full
`sys.version` text, build identifiers, platform and OS values, environment
details, arguments and unrestricted runtime metadata are never read, so they
cannot reach canonical output. A value that is missing, over-long or not
identifier-shaped is reported as `unknown` rather than echoed.

This is *context*, never a verdict. The same source can be a genuine
`SyntaxError` under one grammar and valid under a later one, and the scan
document is what lets a consumer tell those apart: a file that fails to parse
stays a `parse_error` with its own bounded details, and nothing claims another
interpreter would accept it. Both keys are always present, so contexts compare
without inferring a missing value, and the context is constant within a process,
so repeated scans stay byte-identical.

**Compatibility.** `1.0.0` → `1.1.0` is purely additive: the step supplies the
`unknown` context a `1.0.0` document never recorded and rewrites no record.
`scanner.migrate_document` returns `(document, error)`; a document whose version
is newer than the scanner supports, or is missing or malformed, is refused with
a bounded reason rather than half-read.

## Desktop client

A PySide6 desktop client supervises a headless **application boundary** over
newline-delimited JSON on stdin/stdout. It opens a project root through the
boundary and submits the deterministic read-only scan. The boundary is the only
place that imports the deterministic core (`scanner`, `planning`, `report`) and
the workspace filesystem policy; the client consumes only the versioned
contract in `hrca.contract`.

The client presents a document-first product surface (all presentation-only; no
semantics are invented):

- **Document workspace (primary)** — a full-height Working Document editor with a
  document header (selector, New, Open, saved/unsaved state) and a footer where
  Save is the primary action and the candidate actions (Create candidate /
  Review candidate / Adopt) are contextual. The app-owned **document library**
  explorer sits immediately right of the navigation rail and is the app's only
  tree.
- **Preview, Versions, Memory, Change Review and Validation Evidence** — the
  remaining destinations. **Preview** is a read-only, version-bound candidate
  review surface. **Versions** is the accepted-version drawer. **Memory** is the
  read-only Developer Memory read model (Documents, Search, Resume,
  Corrections). **Change Review** groups Agent Chat (a deliberately disabled
  composer and send action — no provider, credential, network or inference call
  is made), Plan, Diff and the raw candidate metadata; **Diff** stays explicitly
  unavailable in this read-only slice. **Validation Evidence** carries Problems,
  Tests and Evidence. **Agent Chat** is the seam the chat-first surface lands on.
- **A single-row status bar** — a transient message plus four persistent fields
  (root, repository, provider, validation state).

As of **P4.4a** the shell is document-first. A compact labelled navigation rail
presents **Document** and **Preview** as the only always-visible primary
destinations, then **Versions** (the accepted-version history with per-version
Restore) and a collapsed **Advanced** disclosure that groups the retained
technical surfaces as **Change Review** (Agent Chat, Plan, Diff and the raw
candidate metadata) and **Validation Evidence** (Problems, Tests, Evidence).

As of **UI-TRANSITION-1** the desktop no longer presents a code-twin surface.
The Project Explorer, the read-only Source Code tabs and the right-hand Code Map
pane — together with the Memory destination's **Code Twin** tab and the status
bar's Twin field — are removed. Their capability is retained headless and
unchanged: the scanner, the Structured Code Twin store, the Code Map projection
and its editable draft, the advisory context builder and the
Memory-to-Code-Twin evidence link all remain reachable through the boundary and
the offline operator CLIs. No protocol action, contract version, schema or store
was changed, and every persisted record stays readable.

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

- `CONTRACT_VERSION` (`3.8.0`) — any other version is rejected,
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
  `memory_record_not_found`, `memory_kind_not_supported`, `memory_not_readable`),
- the bounded Memory query actions (`search_memory`, `memory_resume`) added in
  3.7.0 with the single bounded code `memory_query_invalid`,
- the M4.5 human-revision actions (`get_memory_history`,
  `resolve_memory_effective`, `append_memory_correction`) added in 3.8.0, with
  the bounded codes `memory_revision_invalid` and `memory_correction_refused`.
  The increments are additive: every earlier action keeps its name, meaning and
  version rule, and `ALLOWED_ACTIONS` stays exactly the union of the declared
  action sets.

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
| Source (venv)     | `[sys.executable, "-m", "hrca.cli.app", "--serve"]`    |
| Frozen (PyInstaller) | `[sys.executable, "--serve"]`                        |

Both forms name the same unified entry, `hrca.cli.app`: it runs the desktop
client by default, and the headless boundary when invoked with `--serve`. The
frozen executable bundles that entry directly; the source command reaches it
with `-m`. It is named here rather than `-m hrca.boundary` because
`hrca.boundary` is a package with no `__main__.py`, which `-m` cannot execute.

### Running from source

```bash
uv sync --extra desktop                 # installs PySide6 (optional)
uv run python -m hrca.client            # launch the desktop client
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
  there is no host-Python fallback. A wall-time expiry runs its whole cleanup
  lifecycle exactly once — one kill/removal attempt, one staged-root removal,
  one bounded token — and the token distinguishes a completed stop (`timeout`)
  from one whose kill, removal or cleanup failed (`runner_failed`), so a
  half-finished stop is never reported as a clean one. Note that
  `subprocess.run(timeout=…)` raises `subprocess.TimeoutExpired`, which is a
  `SubprocessError` and *not* a `TimeoutError`; catching the latter alone is a
  silent-failure trap this runner no longer has.
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
caller explicitly configures hooks and points them at the collector. Correction
history, export, backup and the Memory-to-Code-Twin link boundary are delivered
(M4.5). The interactive Code Twin link review that read a link, rendered the
freshness the Twin reports and opened the exact entity shipped as the Memory
destination's Code Twin tab and was removed by UI-TRANSITION-1; the boundary it
drove is unchanged and remains headless.

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

## Bounded Search, Timeline and Resume (M4.4/v1)

`memory_query.py` is a **read model**, not an index: it answers questions across
stored runs from normalized records and the accepted projected claims, and adds
no durable artefact, no migration and no second storage authority. Two read-only
actions carry it on contract `3.7.0`:

| Action | Request | Result |
|---|---|---|
| `search_memory` | optional `filters`, `order`, `limit`, `runs` | ranked bounded hits, or a recorded-time timeline |
| `memory_resume` | optional `runs` | an evidence-linked Resume |

**Facets.** `project`, `work_package`, `date`, `run_state`, `file`, `symbol`,
`decision` and `text` read named canonical fields; terms are alternatives within
a facet and facets are combined with AND. `text` searches an explicit allowlist
(decision and change-set summaries, project names, work-package titles, and the
contract's own refusal reasons) — nothing else is searchable, because nothing
else is in a store. `test_result` is nameable but **has no typed data in schema
1.0.0**, so it is reported as an unsupported facet and the query returns nothing
rather than every record.

**Every hit is a target.** A hit carries the exact `(run_id, kind, record_id)`
the read boundary resolves, plus which facets and fields matched and whether the
matched field was `observed` or source-`reported`. Ranking is total: more matched
facets first, then facet precedence, then run/kind/record identity.

**Time is ordered only when it is comparable.** Only a strict uniform instant
(`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) is ordered; a zone suffix, an exotic
fraction or free text is reported as `incomparable`, an absent value as
`missing`. In `recorded_time` order the comparable items are sorted with stable
identity ties, and everything else is returned in a separate `unordered` bucket
with its status. Display order never claims causality, and it is stated as such.

**Resume composes, it never narrates.** It reports the covered runs, the named
(or ambiguous, or unsupported) goal, blockers, unverified claims and ordered next
actions — each a typed field, an accepted projected claim, or an explicit
statement that the schema cannot support the fact. **Acceptance is unsupported**:
schema `1.0.0` records no accepted/adopted decision, so "last accepted change" is
reported as absent and a completed run is reported separately as completion, never
as acceptance. The **current baseline is not verified** for the same reason. An
unverified claim is one whose reference did not resolve, or whose value is
source-reported text no record verifies.

### The Search, Timeline and Resume workflow (M4.4/v2)

The desktop **Memory** destination carries three read-only pages — **Documents**
(one run's claims and their exact records), **Search** and **Resume** — that
consume the 3.7.0 boundary and nothing else.

**Search** builds a query from an explicit filter list. Each filter names a facet
and a term, is listed with a `Remove` action, and the count is stated against the
bound. A facet schema `1.0.0` cannot satisfy is offered but **disabled and
labelled** `(unsupported)`, so the gap is visible and the surface can never build
a query it knows the model must refuse. An empty term, an over-full filter list
and a refused query each produce words, never silence.

**Timeline** is the recorded-time order, and its two buckets stay apart: *In
recorded-time order* and *Not in time order*, the latter stating that those
results carry no comparable instant and are therefore listed unordered rather
than placed in the sequence. Both buckets say the order claims nothing about
causality. Each result row shows its identity, the facets and fields that
matched, its provenance, its run state and its time status — all as words.

**Resume** renders every returned section: last accepted change, completed runs,
current goal, current baseline, blockers, unverified claims and next actions,
each with the returned labels. Acceptance reads `Unsupported` with its reason,
the baseline reads `Not verified`, and completion is stated as a separate fact
that is *not* acceptance.

**Navigation is typed.** A result or resume entry opens its exact
`(run_id, kind, record_id)` through the read boundary; Search and Resume each
have their own Evidence pane, so a record opened from one never renders into the
other. A result with no typed target renders a **disabled** `Unavailable` button.

**Stale actions are invalidated.** A query generation advances whenever a filter,
the order, the result set or the Resume context changes, and every action
captures the generation it was built under. A superseded action opens nothing and
a late response is discarded rather than shown against the current selection.

## Human corrections, confirmation and history (M4.5/v1a)

Schema **1.1.0** adds two human-facing record kinds, and with them the first
Memory authority a human owns rather than a source. The migration is additive:
`1.0.0` (and `0.9.0`, by chaining) gains two empty arrays, and no existing
identity, replay meaning, evidence link or privacy field is touched.

| Record | What it is |
|---|---|
| `generated_document` | an immutable snapshot of one projected document at one revision |
| `correction` | an append-only, human-owned revision over one exact typed target |

**A correction changes what a reader is shown, never what was recorded.** It
cannot alter a normalized record, cannot change a run's terminal state, cannot
imply repository adoption, and never claims the recorded baseline is current.
The three actions are `get_memory_history`, `resolve_memory_effective` and
`append_memory_correction` — the last one writes only the per-run store, through
the storage owner at the boundary-owned base.

### Operations, states and identity

`keep` affirms a claim, `merge` replaces its statement with human text,
`supersede` replaces an earlier revision (naming its parents), and `reject` marks
a claim rejected without deleting it. Every state says what it means: **draft**,
**archived** and **superseded** are retained in history and change nothing;
**confirmed** and **rejected** bind. `unresolved_conflict` is deliberately *not*
a stored state — writing a resolution outcome into append-only history would be
a rewrite — so it is a resolution result, distinct from every stored state.

Identity follows the M4.1 pattern: a caller-supplied `source_id` is preferred, so
an identical retry is observably a no-op while the same identity arriving with
different content is **refused** rather than silently overwriting a durable
revision. A correction that records no baseline is refused too, because it could
never bind and would sit in history looking like authority.

### Effective resolution

`resolve_memory_effective` returns the generated projection with the overlays
that bind, and every conflict it could not. The baseline a correction binds to is
computed by the boundary from the document **it** projects — never asserted by a
caller — and re-checked at resolution time. An overlay applies only while its
exact target is present **and** unchanged; a missing or changed target becomes an
explicit unresolved conflict, with the generated statement left intact. Nothing
is matched by prose similarity, order or a guessed replacement, and the result
states in words what a correction cannot do: change a record, verify a baseline
or imply acceptance.

The stored baseline fingerprint is the resolver's internal binding key and never
crosses the boundary — a reader is told only that a baseline was recorded.

### The correction and history workflow (M4.5/v1b)

The Memory destination carries a fourth read/write page, **Corrections**, that
consumes protocol 3.8.0 and nothing else. It loads a review — documents, then the
effective document, then its history — and presents:

**Generated and effective, together.** Every claim row shows `Generated: …` and,
when a correction binds, `Effective: …` beside it. The generated statement is
never hidden, a rejected claim says so while keeping the generated text, and
conflicts are listed first.

**An editor that a choice arms.** Selecting a claim, or choosing one of a
conflict's four options, arms the editor with that claim's exact identity and the
current generated version. `Save draft` appends an inert draft; `Confirm` appends
a confirmed revision that **names the draft as its parent**, so the draft is
superseded rather than mutated. The replacement text is carried forward to the
confirmation, and the editor is cleared once an append is accepted — what a
reader sees from then on is the redacted durable revision, not the raw words.

**History, honestly.** Generated revisions are labelled *immutable*, and every
correction carries its state and an **Authority** or **History only** label, so
confirmed and rejected revisions are visibly distinct from draft, archived and
superseded ones. Superseding shows its links. Nothing in the workflow deletes or
mutates an earlier revision, and a generation guard invalidates every outstanding
action when the run, document or context changes.

**Errors never become success.** A refusal — a stale expected version, an unknown
target, a conflicting identity, a save failure — leaves the prior screen readable,
reports the bounded code, and triggers a fresh bounded reload rather than an
optimistic update. The effective text only changes after a successful fresh read.

## Versioned export and local-sensitive backup (M4.5/v2a)

`memory_package.py` is the offline operator boundary for portability and
disaster recovery, with **two profiles that are deliberately kept apart**.

| Profile | Content | May be shared? |
|---|---|---|
| `export` | the projected documents, the effective resolution of each, and bounded evidence metadata | **yes** — least disclosure |
| `backup` | the whole normalized run store, so a restoration can be exact | **never** |

Each profile carries its own label, and the two labels are different sentences:
an export reads *"Shareable export — least disclosure, allowlisted projections
only"*, a backup reads *"Local-sensitive backup — never safe to share"*. There is
no single allowlist and no ambiguous label.

```bash
uv run python -m hrca.memory_package_cli export --base <dir> --run <id> --out pkg.zip
uv run python -m hrca.memory_package_cli backup --base <dir> --out pkg.zip
uv run python -m hrca.memory_package_cli backup --base <dir> --run <id> --out pkg.zip
uv run python -m hrca.memory_package_cli inspect pkg.zip
uv run python -m hrca.memory_package_cli recover --package pkg.zip --active <dir> --staging <dir>
```

### Packages are deterministic

Entries are sorted, every archive member is stamped with a fixed instant, and no
clock is consulted: a creation instant is recorded only when a caller supplies
one. Two packagings of the same snapshot are **byte-identical**, and supplying an
instant changes only that manifest field — proven by test.

The manifest declares the package schema version, the generator, the profile and
its label, the Memory schema it was written for, the entry catalogue with sizes
and checksums, and the profile's own limitations. **Checksums attest the package
bytes only** — no stored source or evidence fingerprint is exposed.

### A backup is one verified snapshot

A backup is a claim about a *cross-run state*, so it is built from one verified
snapshot rather than from independently timed reads. Every store in scope is
captured, then re-read and proven unmoved, before a single entry exists:

- **Content identity.** Each store's canonical bytes are hashed; a store whose
  bytes differ on re-read refuses the snapshot.
- **A change-detector.** The storage owner also reports a stamp derived from the
  store *file*, which moves on every replacement even when the content does not.
  Identity alone is not enough: a re-import or a restore can legitimately write
  an earlier state back, and content would call that "unchanged".
- **Independence.** Every store must resolve every reference it makes inside
  itself. Two runs of one project share a project and work-package *id*, but each
  store carries its own descriptor and resolves it locally, so a shared spelling
  is not a dependency.

Every capture read happens before every verification read, so the windows in
which each store is pinned to its captured content overlap: at that instant the
whole captured set really coexisted, which is exactly what a snapshot claims.

Naming no run means **the store root**, and that scope is enumerated without ever
skipping — a run store that cannot be read refuses the snapshot rather than being
quietly dropped from a package that would claim to cover the root, and a run that
appears or disappears mid-capture refuses it too. Naming runs fixes the scope, so
only those stores have to hold still.

The manifest then binds the snapshot to the package: each declared run names the
entry that carries it, and that entry's checksum *is* the run's identity, so the
declared snapshot and the package bytes cannot disagree. A `stores/` entry no run
declares is refused — a package may not carry content its own snapshot does not
attest.

### A package is hostile until validated

Nothing is read before the archive has been fully checked, and any of these is
refused with a bounded reason: a traversal, absolute or backslash name; a link or
other non-regular entry; a duplicate or case-colliding name; an entry the
manifest does not declare or a declared entry the archive lacks; a malformed
manifest; an unsupported package, Memory-schema or profile version; a size or
checksum mismatch; and an excessive entry count, total size or expansion ratio.

An archive written without file-type bits is ordinary content, not a link — the
check refuses only an entry that *declares* a non-regular type, so a package from
another tool is not rejected for the wrong reason.

### Recovery stages, verifies, then asks

`recover` validates the package, extracts it into a **fresh isolated staging
root** (never in place, never over existing content), migrates a *copy*, and
verifies identity, references, replay and revision integrity: every event must
belong to its run, every child reference must resolve, every correction parent
must exist, and every generated version must name a document. It then prints a
reviewable plan — `create`, `replace`, `identical` or `refused` per run — and
changes nothing.

Before a plan exists, the staged bytes are held to the snapshot the package
declared, re-derived in a **single read**: the staged store set must be exactly
the declared set, and each staged store must parse to the run its entry claims at
the identity the snapshot declares. The stores a restore applies come from that
same verified read, so nothing can be applied that was not proven, and an
altered, extended or interrupted staging directory is refused while no active
store is in scope at all.

Replacement happens only with `--apply` **and** the exact `--expected` active
identity the plan reported. A stale expectation, an unreadable active store or a
failed rollback write leaves the active store exactly as it was; a replacement
preserves the prior store as rollback material first, and the result is re-read
to prove the store reopens with the expected state. An **export cannot be
recovered** — it is shareable precisely because it is not a restore source.

## Memory-to-Code-Twin links and freshness (M4.5/v2b)

`memory_twin_link.py` is the typed bound between one stored Memory record and one
exact Code Twin entity, plus an honest answer to the only question that bound
raises: *does the source revision the link was taken against still hold?* Two
read-only boundary actions carry it, and neither writes anything.

| Action | Input | Returns |
|---|---|---|
| `get_memory_code_link` | a Memory run, record and record kind, plus one exact Twin entity identity | the typed link, with the revision read *from the authoritative Twin store* |
| `resolve_memory_code_freshness` | that link | one freshness verdict |

### Identity is exact or it is a miss

The entity identity is the Twin's own deterministic identifier —
`artifact:file:<root-relative-path>` or `artifact:<kind>:<locator>`, where the
kind is one of `class`/`function`/`method` and the locator is the scanner's
dotted `module.path.Class.method`. Resolution is a lookup **by that identifier**
in the authoritative store, and nothing else is consulted: not a display label,
not prose, not a path substring, not row order, not a digest, not the Memory
record's own `symbol` or `path`, and never a caller-supplied repository path. A
same-named function in another module is a *different* entity; an identifier one
character from a real one is a miss, and a miss is never filled by a
similarly-named entity.

A file identity must be a plain root-relative path: an absolute name, a drive or
colon, a backslash and a `..` segment are all refused, so a link cannot name a
location outside the workspace.

### The recorded revision is the Twin's revision number

A link records the Twin's own workspace revision — the monotone
`scan_generation`, which the Twin advances only when a scan really changes the
workspace. It is not a content digest, and it deliberately is not: elsewhere in
this program a fingerprint never crosses a boundary as a value (a digest is
reported as a present/absent boolean), so a revision *number* is the honest
representation of "the revision this link was taken against" that adds no new
disclosure class. The boundary reads the revision **from the store** and refuses
to take one from a caller, so a caller cannot assert its own currency, and a
fingerprint cannot be smuggled through the revision field.

### Five verdicts, and only one of them is current

`resolve_memory_code_freshness` recomputes the comparison on every call from
current authority: a stored link, a generated statement or a confirmation is
**never** allowed to establish currentness.

| Verdict | Meaning |
|---|---|
| `current` | the entity resolves as a current artifact **and** the recorded revision equals the authoritative one |
| `historical` | the Twin retains the entity as an earlier version (a last-valid symbol kept when its file stopped parsing, or a record held back from an ambiguous rename) |
| `stale` | the entity is a current artifact, but the source has moved past the recorded revision |
| `missing` | the identifier is not in the authoritative store |
| `unsupported` | no comparison is possible: no readable authority, an unsupported Twin schema, a link taken against another workspace, an artifact state this contract does not classify, or an unusable revision |

A retained earlier version is decided **before** the revision is compared, so
history can never read as current — and no link can be *taken* against a
retained version in the first place. `missing` and `unsupported` are returned as
visible, non-actionable results rather than errors, so a caller can render an
honest answer instead of inventing one. Freshness is returned, never persisted.

### Nothing is written

Both actions are reads. The link is a value; the verdict is computed. No store,
run state, correction, generated version or source fact is touched, so a link
can never accept a repository change or claim a verified current baseline.

## The Code Twin link workflow (M4.5/v2c) — headless only

*As of UI-TRANSITION-1 the desktop no longer surfaces this workflow.* The
**Code Twin** tab of the Memory destination was removed with the rest of the
code-twin product surface. The link boundary itself is unchanged: it remains
reachable through the two read-only actions `get_memory_code_link` /
`resolve_memory_code_freshness` and the existing `get_twin` read, and through
the offline operator CLI.

The workflow it implemented — read one stored source claim, bind it against the
authoritative Twin, and return the freshness the Twin itself reports, then
offer to open the exact entity the link names only while the boundary reported
the link actionable. What follows describes that retained headless capability.

### Two steps, because the protocol has two

**Bind link** records a link against the entity the Twin holds *now*. **Refresh
freshness** re-compares that held link with the Twin as it is *later*. They are
deliberately not one action.

Re-binding on every read would erase the drift the page exists to show: a link
taken a moment ago always agrees with itself, so `stale`, `historical` and
`missing` could never appear at all. And no link can be bound against a retained
or absent entity in the first place — the boundary refuses those outright — so
holding the earlier link is the only way the Twin's later answer about it can be
read.

### Every candidate identity is answered, never guessed

A Memory record states only whether its claim is a file or a symbol. Which of
the three symbol kinds an exact locator is (`class`, `function`, `method`) can
be said by the authoritative Twin alone, so the page offers each candidate as
its own complete identity and reports what the boundary answered for each.
Nothing is reordered, filtered or preferred, and a miss is reported as a miss.

Two exact identities resolving for one claim is an **ambiguity**: the page
reports it and refuses to open either, rather than choosing between them.

### Opening is an exact-identity claim

An actionable `current` or `stale` link may be opened. The selector is derived
from the *returned* identity alone — the identity's own body, split exactly once
— and the response is accepted only when the returned artifact carries that
exact id. A same-named entity from another module, a current artifact standing in
for a retained one, an absent artifact, or a bundle with no artifact at all is
refused, and the link and its verdict stay on screen.

The gate is what the boundary returned, narrowed and never widened: the verdict
must be actionable, its state must be one the protocol calls actionable, it must
be about *this* link (identity, kind, workspace, run, record and revision all
echoed), and exactly one candidate identity must have resolved.

### The desktop does not package

Export, backup and recovery are offline operator workflows with no protocol
route into this process. The Memory destination says so in place, rather than
leaving a reader to assume a capability that is not there: there is no package,
restore or replacement control in the desktop, and none is reachable from it.

### Retained scope, unchanged

A link is a typed reference, never source truth; freshness is returned, never
persisted; and a confirmation establishes nothing about whether the source still
holds. This workflow adds no store, no index, no durable artefact and no new
action: it consumes the two v2b reads and the existing `get_twin` read.

## A typed developer intent and a deterministic impact proposal (P5.3)

Two modules turn an explicitly supplied developer intent into a reviewable
statement of what it would affect — *before* any candidate, diff or patch
exists. `intent_delta.py` records what a developer asked for;
`impact_proposal.py` binds that record to exact read-side evidence and reports
the impact. `intent_cli.py` is the offline operator tool.

```
uv run python -m hrca.intent_cli verify intents/service-version.json
uv run python -m hrca.intent_cli propose --intent intents/service-version.json \
    --scanner scan.json --twin twin.json
```

### Why this is not the P4.1 Intent Delta

`codemap_draft.generate_intent_delta` already produces something called an
"Intent Delta" — but that one is *derived from typed Code Map block edits*, and
every field in it, including its acceptance criterion, is computed by the tool.
P5.3 is the other thing: a record of developer intent whose required facts are
**supplied**. Sharing the name would be misleading, so this contract declares
its own generator (`hrca-developer-intent`) and its own schema, and neither new
module imports `codemap_draft` or `proposal`.

### Explicit facts, never inferred ones

Every section must be **present**: `origin`, `baseline`, `requested_outcome`,
`scope`, `constraints`, `acceptance_criteria`, `assumptions` and
`unresolved_questions`. An absent section is refused even when the surrounding
text plainly implies it.

The distinction is between *absent* and *explicitly empty*. `constraints`,
`assumptions` and `unresolved_questions` accept `[]` — "I know of none" is a
real answer. `requested_outcome`, the scope, `acceptance_criteria` and
`origin.evidence` must name at least one entry, because an intent with no
outcome, no target, no acceptance criterion or no evidence reference is not a
bounded intent.

A scope may name entities, exact artifact ids, or both. The artifact-id route
matters: an entity reference can be ambiguous when one id denotes two artifacts,
and the exact artifact id is then the only way to say which one is meant.

`prose` is carried as data with a fixed `authority: "input_data_only"`. It is
never parsed, searched or substituted for a missing fact — an intent that
states its acceptance criteria in prose but omits the field is refused — and it
is never re-published by a proposal, which records only `prose_present`.

### Refusal is not a state

They answer different questions, and the contract keeps them apart.

A **refusal** is `(None, reason)`: the binding could not be established at all,
so nothing is proposed. The evidence belongs to another workspace; the scan
generation or baseline fingerprint moved; the scanner schema or grammar context
differs from the one the intent was authored against; a scope reference binds to
more than one artifact; an origin evidence reference does not resolve to exactly
one identity; an evidence document is malformed or newer than this build.

When the binding holds, the impact itself is reported as a terminal `state`,
because "we could not determine the impact" is a result a reviewer must see
rather than an error that hides it:

| state | meaning |
| --- | --- |
| `unsupported` | no evidence holds that identity; the architecture cannot represent it |
| `unavailable` | the Twin knows the target, the scanner evidence for it is absent |
| `unknown` | the source exists but did not parse, so the facts inside it are not known |
| `ambiguous` | every identity bound, but the evidence contradicts itself about a scoped id |
| `no_impact` | everything read; the bound evidence records no fact inside the scope |
| `bound` | everything read; the affected facts below are what it records |

Precedence is fixed and documented in that order. `no_impact` is a positive
finding about the evidence supplied — never a place to put unknown impact, and
never rendered as empty success: it carries its own risk entry saying exactly
that.

### Binding is exact, and a changed context invalidates it

Every identity in the output is one the evidence publishes verbatim: a Twin
artifact `id`, a scanner symbol `id`, a scanner relation `id`, a
workspace-relative scanner file `path`. The scope resolves by exact equality
only — no name, path, prefix, positional or prose matching exists in either
module, and a bare name like `Service` resolves to nothing rather than to
`app.service.Service`.

Facts are reached structurally, not by name. A bound class artifact contributes
the symbols whose `parent_id` is its locator; a bound file artifact contributes
the symbols and relations whose `file` is its path. A fact is only established
when the source that declares it is readable — when the scanner recorded a parse
error, the facts inside are omitted rather than asserted, so the `unknown`
state and the fact list never contradict each other. Relation `target` values
are the scanner's literal source names and are reported with `resolved: false`.

Reverse impact — facts that *depend on* a scoped entity — is deliberately not
computed: the scanner records relation targets as literal names and never
resolves them, so a dependent set could only be reached by name matching. The
proposal says so as an unresolved question instead of guessing.

### Advisory only, and provably so

`executable` and `applied` are always `False`, and every proposal carries a
`mutation_surface` block declaring each boundary the contract names — candidate,
diff, branch, commit, source file, Git index, runner job, provider request,
credential, package state, recovery state, Twin state, Memory — as `False`.
`tests/test_impact_proposal.py` asserts that block directly, audits both modules
for imports of any write-side or network seam, and builds a proposal inside an
empty temporary directory to show it creates no file. The protocol action set is
asserted unchanged: P5.3 adds no route, so nothing was widened.

### Fixtures

`fixtures/intent/manifest.json` states, by hand and independently of the
renderer, what the contract must answer for a supported case and for every named
negative case: missing and invalid intent fields, prose that must not fill one,
an oversized value, empty versus unknown impact, an exact source/Twin binding, a
stale revision, an artifact mismatch, an ambiguous reference and an ambiguous
impact, a cross-workspace reference, a changed baseline and a changed grammar
context, and a malformed or absent evidence document. Each supported expectation
says *why* the fact or suggested test is present, so the oracle is a statement
about the contract rather than a recording of the implementation.

## An isolated content-addressed candidate and its exact diff (P5.4)

Four modules turn an explicit typed edit into an immutable candidate *outside*
the accepted repository, plus a review envelope describing exactly what
changed. `candidate_edit.py` is the typed request, `candidate_diff.py` the
canonical diff, `candidate.py` the materializer (the only module in this
contract that touches a filesystem), and `candidate_cli.py` the offline
operator tool.

```
uv run python -m hrca.candidate_cli derive --intent i.json --scanner s.json --twin t.json
uv run python -m hrca.candidate_cli review --edit e.json --intent i.json \
    --scanner s.json --twin t.json --repo <dir>
uv run python -m hrca.candidate_cli build  --edit e.json --intent i.json \
    --scanner s.json --twin t.json --repo <dir> --output-base <dir>
```

Materializing a candidate is **not validation, approval or adoption**. The
envelope carries `validated`, `approved` and `adopted` all `False`, and the
accepted repository is untouched by construction: the only writes are under a
fresh output root this contract creates, outside the repository and outside Git
metadata.

### The narrowest grammar that can still express a change

One operation: **exact whole-file replacement of an existing UTF-8 Python
source file**, naming one exact repository-relative path, the SHA-256 the file
is expected to have now, and the complete text it should have instead.

Everything else is refused *by name*, so a caller learns which capability is
missing rather than guessing at a validation error:

- **Creation is unsupported**, not merely unimplemented. A new file has no
  predecessor artifact, so it has no exact identity to bind a scope, a
  fingerprint or an evidence reference to. Authorizing one would mean inventing
  the scope this contract is built to avoid inventing.
- **Deletion, rename, move, copy and mode changes are unsupported.** A
  withdrawal or a relocation is not a replacement; representing one as the other
  would misdescribe what happened.
- **Symlinks, hardlinks and submodules are unsupported.** They make a path name
  something other than the bytes it appears to contain.
- **Patch and unified-diff input is unsupported.** An arbitrary patch parser is
  a second, unbounded input language. Accepting complete content only means the
  desired bytes are fully known before anything is written.
- **Binary content is unsupported.** A payload that is not decodable UTF-8 is a
  refusal; a decodable payload carrying a NUL byte or a byte-order mark is the
  `unsupported_content` *state*.

A path is accepted only in one exact spelling — forward slashes, no leading or
trailing separator, no empty/`.`/`..` component, no drive or UNC prefix, no
control character, no padded component, no reserved device name, at most 32
components, already in Unicode NFC. Two operations differing only by case are
refused: on a case-insensitive filesystem they name one file twice.

### Authorization is evidence, never prose

A path may be replaced only when the bound proposal puts it in scope **and**
carries it as evidence: it must appear as a target path in
`target_scope.targets`, and as either a `scanner_file` binding or a governed
fact's `file`. There is no basename, suffix, case-folding, entity-name, prose,
path-order or nearest-match route — an intent whose prose asks for a
neighbouring file authorizes nothing, and the edit that follows the prose is
refused.

Two further exact identities gate every operation. The proposal must be in its
`bound` state — `no_impact`, `unknown`, `unavailable`, `unsupported` and
`ambiguous` all mean the affected facts were **not** established, and an edit
that changed a file anyway would be changing facts nobody bound. And the edit's
`expected_sha256` must equal the Twin's own recorded fingerprint for that exact
file artifact, so the predecessor is pinned by two independent sources before a
byte is read.

The proposal supplied is never trusted either: the build re-derives it from the
delta and the evidence and requires the identity to match. A changed baseline,
scan generation, grammar or schema therefore refuses in one exact test rather
than in a checklist of comparisons.

### Refusal versus state

`(None, reason)` means the request or its binding cannot be *read*: an invalid
edit, delta or proposal; evidence the proposal does not re-derive from; an edit
whose declared delta, proposal, binding fingerprint or baseline is not the one
supplied; a request past an accepted bound.

An **envelope** with a terminal `state` means the request *is* read and the
answer is a determination about content: `candidate_ready`, `no_change`,
`unsupported_content`, `oversized` and `refused`, with the fixed precedence
`refused` > `oversized` > `unsupported_content` > `no_change` >
`candidate_ready`. A replacement identical to its predecessor is `no_change`
and never becomes `candidate_ready`.

### Isolation, atomicity and cleanup

The candidate root is created by `tempfile.mkdtemp` under the validated output
base — the pattern `memory_package.stage_package` already uses to refuse reuse.
Content and manifest are written, `fsync`-ed, and verified against their own
recorded hashes; only then is the final name claimed with `os.mkdir` (which
fails rather than overwrites) and the staging tree renamed onto it with
`os.rename`, the atomic-rename contract `memory_store` and `library_store`
already rely on. The final name is derived from the candidate identity, so a
second build of the same candidate is refused.

Every predecessor is read under a checked identity: each path component is
checked for a link, the resolved target must stay inside the accepted root, and
the file must be a regular file with exactly one hard link. A failure at any
point removes only the directory this build created, and only after checking
that its parent is the base this call validated and that its name carries this
module's staging prefix — so cleanup can never reach an unrelated output root,
another candidate, the repository, or the untracked bundle.

### Identity

`candidate_id` is content-addressed over a manifest that records **logical
content only**: no path, no root name, no timestamp. The root name is derived
from the identity rather than the other way round, so identical inputs produce
an identical id, an identical manifest and an identical review envelope — and
the same candidate cannot be written twice into one base.

The manifest is written as its own canonical serialization, so its bytes are the
digest's preimage. `tests/test_candidate.py` proves the whole chain
independently: it applies the recorded replacement to the frozen baseline with
the standard library alone, recomputes every hash and the identity with
`hashlib`, and requires byte equality with what is on disk. Neither the
materializer nor the diff renderer is consulted to compute an expected value.

The frozen fixture's hashes and byte counts were pinned from `sha256sum` and
`wc -c`, not from the package. The miniature repository lives in its own root,
`candidate_fixtures/`, beside `grammar_fixtures/` and `codemap_fixtures/`: the
Phase 1 scanner tests measure `fixtures/` by exact file, symbol and relation
counts, so a new Python file inside it would change those numbers and quietly
rewrite what the baseline asserts. One input is pinned deliberately: the Twin
workspace identity. A workspace is derived from a canonical root, so a copy of
the tree in a temporary directory is a *different* workspace — the same bytes in
a different checkout are a different candidate, which is the accepted
architecture's own rule rather than a nondeterminism here.

### Boundaries

`tests/test_candidate.py` asserts the whole mutation surface — accepted source,
Git index, refs, branch, commit, worktree, runner job, provider request,
credential, network, remote, Twin, Memory, validation, approval, adoption,
protocol action, UI, package state, recovery state — as `False` on every
produced envelope. It audits all four modules for imports of any write-side or
network seam, and snapshots the accepted repository's bytes and its entire Git
state — HEAD, refs, index, status, stash, config, hooks — before and after a
success, a refusal, an oversized request, an injected mid-build failure and the
read-only cancellation path, requiring every one to be unchanged. The untracked
bundle is compared by size and modification time only, never read.

No protocol action was added: the accepted candidate-named actions carry no edit
grammar and no output root, so nothing fitted and nothing was invented. The
contract is reached offline through `candidate_cli.py` only. No repository test
or build is executed as candidate validation, and the controlled runner is never
invoked — candidate-driven validation is a later phase.

## Bounded validation evidence without adoption (P5.5a)

Three modules produce honest, bounded evidence about one exact P5.4 candidate
and never turn a green result into an approval. `validation_policy.py` is the
code-owned command table, `validation_plan.py` the plan document,
`validation.py` the dispatch, binding and append-only evidence store, and
`validation_cli.py` the offline operator tool.

```
uv run python -m hrca.validation_cli plan   --candidate <dir> --review <f>
uv run python -m hrca.validation_cli run    --candidate <dir> --review <f> --evidence-base <dir>
uv run python -m hrca.validation_cli verify --evidence-base <dir>
```

Exit code `0` means passing evidence. It does not mean approved, adopted or
applied — the contract has none of those to give.

### Why a check is not a command

The accepted runner does not take a command. `ContainerRunner.run` takes a
**handler name** and an input payload, and the container's entrypoint argv is a
module constant. So the strongest policy available here is not "a reviewed argv
per check" but "a reviewed *package* per check", from which the handler and the
form and result schemas all come. A caller names a check id and nothing else;
the command, the image, the mounts, the environment, the timeout, the resource
limits, the network policy and the working directory are all unreachable.

That is enforced, not merely intended: a plan request may carry `checks` and
nothing else. `command`, `argv`, `shell`, `image`, `mount`, `environment`,
`timeout`, `memory`, `network`, `credential`, `privileged`, `cwd` and
`handler` each get their own bounded refusal sentence, and any other key gets a
generic one. The fixed inputs are the code-owned protected inputs in
`delta_verifier` — the same inputs the delta verifier already treats as
protected from provider influence — and the packages are the accepted ones, so
`validate_policy()` fails if a check could ever name a package or handler the
allowlist does not hold.

### What a plan binds, and what it refuses

A plan binds the exact candidate: its identity, the hash and size of its
manifest and review envelope, the candidate root's own name, every staged
path/hash/size, and the P5.3 binding the candidate was built against — the
Intent id, the Proposal id, the binding fingerprint, and the workspace,
baseline, scan-generation, scanner schema and grammar context. Nothing runs
until all of that has been re-read from the candidate root and required to equal
what the plan names. A moved candidate, a review that disagrees with the
manifest, an extra file, a missing file, a link, a changed byte — each is a
refusal with its own reason. Nothing is rebased, refreshed, reconstructed or
regenerated.

A plan never carries where the candidate lives. The root appears as its own
name, so an absolute path in a plan is a malformed binding rather than a
location.

### Seven states, and only one of them passes

| state | meaning |
| --- | --- |
| `passed` | the check ran, exited zero, and its artifact validated |
| `failed` | the check ran and its artifact did not satisfy the contract |
| `timed_out` | the check exceeded the runner's own bound |
| `cancelled` | cancelled before dispatch, so nothing ran |
| `unavailable` | the runtime could not run it at all |
| `refused` | the check was not dispatched, for a reason this contract named |
| `unknown` | the check ran but its evidence is missing, unreadable or inconsistent |

The overall state is the most severe state any check reached, in that order, so
a failure can never be averaged away by passing neighbours. `evidence_complete`
is true only when every check passed. Missing, truncated, unreadable or
unverifiable evidence is visible and non-passing; it is never coerced to
`passed`.

Every attempt records the isolation facts read out of the **actual dispatched
argv** — network disabled, root filesystem read-only, non-root user,
capabilities dropped, no-new-privileges, resources bounded, exactly two mounts
with only the `/in` one read-only, no Docker socket, `--rm` and `--init`
present. The mounted source directories and the container name are redacted to
`<staged>` and `<container>`, because they are fresh, random and absolute and
carry no review value; the flags, the image and the mount destinations are kept
verbatim.

### Evidence is append-only

Each attempt is written as its own file named by its content-addressed
identity, plus one appended index line. Repeats take a new ordinal and therefore
a new identity: they create new attempts and cannot overwrite, merge into or
change the ones before them. A file that already holds different bytes under a
recorded name is refused, and re-reading the store re-derives every identity, so
a tampered attempt or a deleted one invalidates the whole store rather than
being quietly skipped.

### What the runtime actually does here

**No Docker daemon is reachable in this environment.** A `docker` client is on
`PATH`, but it is the Windows shim and it reaches no daemon, so the runner's own
preflight reports `runtime_unavailable` and every check is `unavailable`. That
is the honest result, not a skipped test, and no state is invented to fill the
gap. The pass, fail, timeout and unreadable-artifact cases are exercised against
a container double that reproduces the in-image wire contract exactly
(`packaging/runner/runner_main.py` resolves the handler and writes
`{"result": ...}` or `{"error": ...}`), and the tests say so.

Reading the runner turned up a pre-existing defect, since repaired under
P5.5r1: **`subprocess.TimeoutExpired` is not a `TimeoutError`**, so
`container_runner`'s `except TimeoutError:` clauses never fired on a real
timeout — the kill, the removal, the staged-directory cleanup and the bounded
token were all skipped with it, in the dispatch path and in both preflight
calls. The runner now catches both names, and a real timeout runs its whole
lifecycle exactly once. `timeout` means that lifecycle *completed*; if the
kill, the removal or the cleanup failed, `runner_failed` is returned instead, so
a half-finished stop is never reported as a clean one. This contract keeps a
defensive catch of its own so a future regression is still bounded rather than
escaping, and both exception spellings are tested through to the same outcome.

### No approval, no adoption, no application

`approved`, `adopted` and `applied` are `False` on every attempt and every
result, and a `mutation_surface` block declares accepted source, candidate, Git
index, refs, branch, commit, worktree, Twin, Memory, approval, adoption,
application, provider request, credential, network, remote, protocol action and
UI all `False`. The validators refuse a record that claims otherwise. A passing
run is evidence; `tests/test_validation.py` snapshots the accepted repository's
tracked bytes, its entire Git state and the candidate itself before and after a
pass, a failure, an unreadable artifact, a refusal and the absent-runtime
outcome, and requires every one unchanged.

No protocol action was added, so the desktop cannot reach this contract; it is
offline through `validation_cli.py` only.

### A note on the name

`validation_plan` here is **not** the `validation_plan` field inside a P4.1
proposal package, which is a list of check descriptions. This is a separate
P5.5a document with its own generator (`hrca-validation-plan`) and its own
`plan:` identity.

## A real candidate syntax check in a real container (P5.5a-r2)

The P5.5a gate was `mount_policy_missing` — the runner had no candidate mount and
no entrypoint that could exercise one. That is now closed, additively: a second
check family, one more read-only mount, one more literal entrypoint, and an
immutable image digest.

Everything the earlier gate found is still true of the **package** path. The
candidate path is separate, opt-in, and cannot be turned into it.

### One more mount, and one more literal entrypoint

`ContainerRunner.run_candidate` dispatches with exactly three mounts, in a fixed
order: the staged input (read-only), the staged output, and the candidate root's
`files` directory (read-only) at the fixed `/candidate`. The entrypoint is
`CANDIDATE_ENTRYPOINT`, a module constant — `python /app/runner_syntax.py
/in/input.json /out/output.json` — so no plan, candidate or prose value reaches
any element of the argv.

The in-image entrypoint does the least it could: it asks the interpreter whether
each **explicitly declared** file parses. It never imports or executes candidate
code (`compile` builds a code object; it does not run one — and `py_compile`
would *write*, which a read-only rootfs forbids), never discovers files, never
shells out, and never reports source text: a syntax error contributes its message
and position, never `exc.text`, so a bounded diagnostic cannot become a way to
read a file back out of the container.

Two choices there are worth naming.

**What is mounted is the root's `files` directory, not the root.** The candidate
root is created `0700` and the container runs as 65534, so mounting the root
would mean widening its mode — a mutation of the thing being validated. `files`
is already traversable, so the candidate is **not touched at all**, not one byte
and not one mode bit, and the container never sees the manifest or the review
envelope sitting beside it. The mount source is still verified as a contained
subdirectory of the verified candidate root before it becomes a mount.

**The image is bound by digest, not by tag.** `hrca-runner:v1` is a mutable tag;
content can move under it. Before dispatch the runner reads the local image's
immutable ID and refuses unless it equals the pinned
`sha256:0809a47a00fcce555500b02d6645b68a565ad2a8299416bd9aa02f459ebaf258`. A
tag-only, absent or mismatched digest refuses and nothing is dispatched. The
digest is pinned in code *and* as fixture data, so a rebuild that is not re-pinned
fails the tests instead of quietly reporting evidence about a different image.

### The rebuilt image, and how it was built

Rebuilding the reviewed `packaging/runner/Dockerfile` was blocked because its
base was a **floating tag** and the base image had been removed locally. It now
pins the base by manifest digest:

```
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
```

That is the manifest the previous image was actually built from, so the rebuild
is the same lineage rather than a base substitution — and the base is now
immutable and reviewable instead of a tag that can move.

The rebuild is now a **reviewable path** rather than a recollection:
`hrca.runner_image_policy` holds the pinned base, the pinned runner digest and
the decisions that bind them, and `hrca.runner_image_setup` carries the path out
— resolve the base index to one platform manifest and one config, verify the
Dockerfile builds from exactly that base, run one bounded `docker build`, and
read back the resulting identity, OnBuild state and layer lineage.

The boundary it works under is the ordinary coding-agent one: a pinned base,
anonymous access, and the minimum official registry contact the engine needs.
Every Docker command runs with `DOCKER_CONFIG` pointing at a fresh empty
directory and with every credential-bearing ambient variable dropped, so the run
has no stored auth entry and no credential helper to consult; an ambient helper
is disclosed as a boolean and never read or named. The record states plainly that
it is **networked setup evidence**: no destination was captured, the endpoint
classes are declared from the official client's documented behaviour, and no
domain list is treated as proof of purpose. See
[`evidence/p5.5a-r3c/`](evidence/p5.5a-r3c/) for the record and its limits.

The P5.5a-r3c rebuild ran `docker build` from that pinned base. `load metadata`
took `1.9s` on the first of them — the base manifest was revalidated against the
registry rather than answered from a local cache — while the build itself needed
no new content: the `FROM` step re-resolved the pinned digest, the `WORKDIR` and
the three `COPY` steps were all `CACHED`, no layer was acquired, and the export
produced a manifest list carrying an attestation manifest.

**What a rebuild does and does not reproduce.** Two rebuilds from that base
produced the same platform manifest (`sha256:0ba2c00a…`), the same config
(`sha256:89b3c9d9…`) and the same eight layer `diff_ids`, `Created` stamp,
`Cmd`, `User`, `WorkingDirectory` and OnBuild state — byte-identical to the
previously reviewed image. They produced a *different attestation manifest* each
time, so the tag's immutable identity, which `docker image inspect` reports as an
index digest, moved on every rebuild: `0ae0f7f5…` → `f6b3752c…` → `0809a47a…`.
The content is reproducible; that index identity is not. The pin therefore has to
be re-pinned on every rebuild — which is what the fixture note already requires —
and the candidate path was deliberately **not** exercised against any of these
identities.

### Verifying a setup-only change

Preparing an image is not validating one, and the two are verified by different
commands. Running the repository's discovery command for a setup-only change is
**not** safe here: `uv run python -m unittest discover -s tests` selects every
module, and two of them dispatch real containers whenever a daemon is reachable —
`test_candidate_syntax_integration` mounts a candidate, and
`test_rule_delta_docker_integration` runs the package handlers. That is exactly
how a "quick baseline check" once put a candidate inside a container.

Setup verification therefore has its own entrypoint:

```bash
uv run python -m hrca.setup_verification_cli          # the whole allowlist
uv run python -m hrca.setup_verification_cli --module test_architecture
```

| | |
|---|---|
| Selects | a **code-owned allowlist** — `test_architecture`, `test_runner_image_policy`, `test_runner_image_setup`, `test_setup_verification`. There is no discovery, no pattern and no directory walk, so a new test module is outside the surface until someone adds it on purpose. |
| Refuses | any other module, **by name, before importing it**. An excluded module gets its own bounded reason — `the requested module dispatches containers and is never selectable here` — and exit code `2`. |
| While it runs | an audit hook makes any attempt to **start a process** a failure, and an import hook refuses the excluded modules. An attempt is a refusal, not a note: the run cannot report success if anything tried. |
| Refuses to be vacuous | a selection that runs no tests, or leaves an allowed module contributing none, is refused. "Verified nothing" must not look like "verified". |
| Exit codes | `0` verified, `1` the selection ran and failed, `2` refused before it could verify anything. A usage error is `2`, so a typo is never mistaken for a pass. |

The guard is an audit hook rather than a patched `subprocess.run` for a concrete
reason: `ContainerRunner` binds `spawn=subprocess.run` as a **default argument**,
so replacing the module attribute afterwards is invisible to a run that would
dispatch with the original. The audit event fires at the C level, before any
process exists, whichever reference the caller holds.

The two live integration modules are untouched and stay reachable through their
own explicit route — the correction is a partition, not a weakening. The tests
that *prove* the guard refuses a spawn necessarily attempt one, so they live in
`tests/test_setup_verification_guard.py`, which is deliberately **not** on the
allowlist: that is what lets a passing setup run report "nothing attempted" and
mean it.

The entrypoint is a separate module on purpose. `python -m hrca.x` executes `x` as
`__main__`, so a self-entrypoint in the state module would exist twice in one
process — once as `__main__`, once as `hrca.setup_verification` when the tests
under it import the canonical name — with separate guard state and *separate
exception classes*. The wrong form refuses rather than running something weaker
than it appears to be, and a test asserts no duplicate is live.

### What a passing result does and does not mean

`passed` means the pinned files **compiled** under the bound image digest. It is
not behavioural correctness — nothing imports or runs the candidate — and it is
not approval, adoption or application. Every attempt records the entrypoint, the
verified image digest, the three-mount isolation facts read out of the real
argv, the artifact digest, and `approved`/`adopted`/`applied` all `False`.

A file that genuinely does not compile is `failed`, with a bounded limitation
naming the file and never the source line. An artifact that does not account for
exactly the declared files — missing one, answering for another, or with counts
that disagree with its own outcomes — is `unknown`, never passing.

### A late-created container is reconciled, not stranded

Killing the docker *client* is not the same as stopping the container. A timeout
can fire while the daemon is still creating the named container: the kill and the
removal then run **before it exists**, and it afterwards appears in `created`
state — where `--rm` never reaps it, because `--rm` only removes a container that
has run. The lifecycle used to report a clean timeout for that, because the
removal had "answered".

`timeout` no longer rests on what the client said. After the initial
kill/removal, the runner asks a bounded number of times whether **the exact
product-owned name** still resolves, and force-removes it if it does. `timeout`
is returned only when the container is conclusively absent *and* the staged roots
are gone; a container that will not go away, a daemon that cannot be asked, a
failed removal and a failed staged cleanup all remain `runner_failed`.

Three rules keep that honest:

- **the daemon must confirm it is reachable before an absence is believed** — a
  failed query is `unknown`, never absent;
- **nothing is enumerated**: only the one generated name is ever inspected or
  removed, never a list, a pattern or another container;
- **the bounds are fixed module constants**, not settings — at most five removal
  rounds and one more query than that, with fixed waits. No caller, plan,
  candidate or prose can tune them.

The live integration suite proves it on a real daemon: a bound far below
container creation time is used deliberately, and the test does **no reaping of
its own** — if production cleanup did not remove the late container, the test
fails. A companion live test creates a bystander container from the same image,
left in the same `created` state under a different name, and asserts the
lifecycle never touches it.

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
  spelling can vary slightly between Python minor versions — and the *grammar*
  that reads a tree is itself versioned, which is the sharper effect: source
  using syntax a grammar cannot express (PEP 695 type parameters, for one) is a
  genuine `SyntaxError` there and parses into entities under a later grammar.
  The document therefore reports its `grammar` context, so a consumer can
  attribute that difference rather than read it as a defect in the source.
  `grammar_fixtures/` is exactly this case, and `tests/test_scanner_grammar.py`
  holds it to expectations authored per grammar *capability*, not per release.

- **Developer Memory is offline (M4.1) with one explicit capture path (M4.2).**
  M4.1 replays bounded sessions from the fixture corpus; M4.2 additionally maps
  documented Claude Code hook JSON through a collector that a caller must
  configure explicitly. Neither performs a provider request, credential access
  or model egress, and neither parses a transcript. Raw hook payloads, prompt
  text, assistant text and artifact content are never durably stored. No
  summary generation, search, Resume or Memory UI exists yet.

Out of scope entirely: LLM providers, semantic editing, UI, remote code
execution, multi-language support, and automated merges.

- **The P5.3 impact proposal is advisory and statically bounded (P5.3).** It
  reports what the *supplied* evidence records inside an intent's scope and
  nothing beyond it: reverse impact is not computed (relation targets are
  literal names and are never resolved), no runtime or provider evidence
  verifies the affected set, and `no_impact` is a finding about the evidence
  rather than a safety claim. It is a pure function of two documents — no
  candidate, diff, branch, commit, runner job, provider request, package state,
  Twin write or Memory write is reachable from it — and it deliberately has no
  desktop route: the accepted read-only actions derive a proposal from a Code
  Map *draft*, which is a different input with different authority, so none fits
  this contract and none was invented. Reaching it requires a new protocol
  action, which is a separate decision and not part of this work.

- **A P5.4 candidate is bytes, never an outcome.** It is materialized outside the
  accepted repository and is not validated, approved or adopted; nothing applies
  it, and no check that would make it *correct* exists yet. It represents
  whole-file replacement of UTF-8 Python source only, so deletion, creation,
  rename, mode changes, links and binary content are refused rather than
  approximated. The diff is a line-level rendering, not a byte-level proof, and
  the predecessor is read once: a concurrent change to the repository after the
  build is not reflected, which is why the envelope says so as a risk. It has no
  desktop route and no protocol action was added.

- **P5.5a evidence is not correctness.** The checks are the product's own fixed
  policy, not a test suite derived from the candidate's content: they exercise
  the accepted runner and its handler family, and a green result says the
  machinery ran cleanly against this candidate's bound context, not that the
  candidate's code is right — nothing here executes the candidate. A real
  container runtime is unavailable in this environment, so every check reports
  `unavailable`; the passing and failing cases are exercised against a container
  double, and no state is invented to cover the gap. Cancellation is cooperative
  and pre-dispatch, since this contract has no way to interrupt a running
  container. The evidence store has no retention policy and no signature: it is
  a local append-only record, and an adversary who can rewrite it can also
  rewrite the index.

- **A syntax pass is the least interesting thing a candidate could pass
  (P5.5a-r2).** The candidate path compiles the declared files and nothing else:
  no import, no execution, no discovery, no tests. So a green result says the
  files parse under the bound image, and says nothing about behaviour, types,
  dependencies or correctness. The package checks still exercise only the
  product's own fixed handlers. The candidate path is opt-in, has three mounts
  where the package path has two, and is bound to an immutable image digest that
  must be re-pinned after every rebuild. A dispatch timeout returns `timeout`
  only after the exact product-owned container is conclusively absent and the
  staged roots are gone; every other outcome — including a daemon that cannot be
  asked — is `runner_failed`.
