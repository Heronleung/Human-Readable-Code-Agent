# ORCH-BACKBONE-1 — the persisted one-scan workflow

PrimaAgent's chat-first shell could describe work but not remember it: the plan,
its jobs and the review decision lived in `ui/appmodel/` and vanished with the
window. This package gives the read-only scan slice a durable backbone.

Five authoritative record families live in a backend-owned store outside any
selected repository. **Review and Resume are projections over those records**,
never stores of their own, so there is no second copy of "completed" or
"verified" that can drift from the first.

## What it does, and what it refuses to claim

A developer states a goal, edits a deterministic one-scan plan, confirms that
exact revision, separately runs one read-only scan job, reviews the evidence it
actually produced, and records a human decision — and a fresh backend and
desktop recover the same records by id, without scanning again.

It is **not** hosted-agent execution, arbitrary goal fulfilment, code
correctness, or source acceptance. The one thing this slice executes is the
existing deterministic scanner, reached through its existing seam.

## Records

| Family | Carries | Invariant |
| --- | --- | --- |
| `plan_revision` | id, project, monotonically versioned revision, goal, provenance, scope, bound manifest, criteria, one typed scan-job spec, phase | Editing appends a revision under optimistic concurrency. Confirmation binds the exact revision digest; a confirmed revision is immutable and its confirmation never covers a later edit. |
| `job` | id, owning plan revision, fixed capability `source.scan`, executor `local_scanner`, scope, source identity, criterion ids, state | Exactly one executable job. No arbitrary capability, command, provider, role, dependency or sub-agent can be dispatched. |
| `agent_run` | id, job, plan revision, attempt, idempotency key, executor + version + pid, bound manifest, start/end, outcome | Immutable terminal outcome. Completion is an execution fact, never requirement satisfaction and never source acceptance. |
| `evidence` | id, run/job/plan/project, code-owned predicate id, result, limitations, bounded counts, scanner schema and grammar, manifest id, artifact ref + digest | Backend-produced and immutable. A capability hint, UI text or a `completed` label cannot create evidence. |
| `decision` | id, project, kind, outcome, actor, target digest, evidence-set digest, reason, supersedes | Append-only. Review acknowledgement never adopts source and never widens authority. |

`Resume` and `Review` are deterministic queries over the above plus a freshness
observation taken now — never a mutable store and never a stored boolean.

## The one layout rule that matters here

**A criterion is covered only by the evidence of its own predicate.** Four
code-owned predicates exist:

```
scan_evidence_bound · source_binding_stable · limitations_preserved · terminal_outcome_known
```

A requirement the developer writes has no predicate. It is displayed, and it is
**never covered** — the scan finishing is not evidence that the application
works. A scan that parsed every file therefore cannot produce an all-green
review: the prose requirement stays open, and parse errors are preserved as
observations rather than converted into a clean bill of health.

## Source binding and freshness

Two identities, kept apart: `accepted_baseline_ref` is existing adoption
authority, and `observed_source_snapshot` is the bytes actually read. The
second never creates the first, and its absence is shown as `unknown` rather
than manufactured during project open or scan.

The manifest binds the **actual bytes** of the scope — relative paths, content
digests and sizes, including dirty and untracked files, because the accepted
local work is uncommitted. No atomic filesystem snapshot is claimed: every file
is stat-ed either side of its read and the scope is listed twice, so anything
that appeared, vanished or changed during the walk marks the manifest
`complete=False` and yields non-current evidence instead of a confident lie.

Freshness is re-derived on every read by re-observing the scope, which is why a
stored record can never keep asserting its own freshness after the source
moved. An addition, a deletion or a content change makes the evidence `stale`
before dispatch and before a current-evidence acknowledgement.

## Storage and boundary

* One SQLite database (standard library `sqlite3`) at
  `<app-data>/orchestration/orchestration.db`, with bounded evidence artifacts
  beneath the same namespace. A store configured inside the selected project or
  its Git metadata is refused by name.
* Schema version `1.0.0`, owned independently. An unreadable, malformed or
  newer schema is refused with a bounded reason and the existing data is left
  untouched — there is no destructive migration and no automatic repair.
* **One active execution per project**, enforced by a partial unique index
  rather than a race between two readers.
* **Idempotency binds key + payload.** The same key with the same payload
  returns the stored result; the same key with a different payload is a
  conflict. Correlation ids match responses to requests and are never used for
  deduplication.
* A run's terminal outcome and its evidence are published in one transaction,
  and the artifact is renamed into place before that transaction commits, so a
  committed evidence row cannot point at a partial artifact.
* Recovery marks an unfinished run `interrupted_unknown` only once its
  recorded executor pid is demonstrably gone. A normal read never rewrites a
  live run, and nothing is ever retried automatically.

## Protocol (contract 3.10.0)

Additive over 3.9.0: every 3.9.0 action keeps its name, request shape and
response shape. The scan family's `plan` synonym and `memory_resume` are **not**
repurposed. A client still sending 3.9.0 is refused by the existing
exact-version rule.

| Action | Purpose |
| --- | --- |
| `orchestration_save_plan` | Observe the scope and append a draft revision |
| `orchestration_confirm_plan` | Confirm one exact revision and create its job |
| `orchestration_run_scan` | Validate, claim one execution, run the scanner |
| `orchestration_read` | Read the scoped workflow, its review and its resume |
| `orchestration_decide` | Append one human review decision |

All five are chain-dispatched, like `open_project`, so the handler registry and
its authority ledger are unchanged.

Two negative shapes are kept apart. A **domain refusal** — an unconfirmed plan,
a stale binding, an already-claimed execution — is a *successful* response
carrying `state: "refused"` and one of the module's fixed reason sentences; no
caller text, path, id or stored content can reach the client through it. An
**infrastructure fault** is a contract error with a fixed catalogue message
(`orchestration_store_unavailable`, `orchestration_schema_unsupported`,
`orchestration_request_invalid`).

## The desktop binding (ORCH-BACKBONE-1B)

The desktop reaches the workflow only through the supervised boundary path.
`ui/client.py` imports **no** orchestration module — not the store, not the
service, not the scanner — and holds no second copy of any record.

What `MainWindow` keeps is `_orchestration_workflow`: the last projection the
backend returned. It is a **cache of a backend projection, not an authority**.
It is written only from a response, and it is re-read after every mutation, so
the displayed plan id, run id, coverage and freshness are always the backend's.

| Interaction | What is sent | What is shown |
| --- | --- | --- |
| State a goal | `orchestration_save_plan` | The card, then the persisted plan |
| Confirm the plan | `orchestration_confirm_plan` with the exact persisted digest | Confirmed only once the backend confirms that digest |
| Dispatch the scan job | `orchestration_run_scan` with the stored plan id | The persisted run outcome |
| Review / Home | — (a read) | `orchestration_read`'s review and resume projections |
| Record a decision | `orchestration_decide` against the exact run | The persisted decision |

Three rules the binding enforces:

* **The desktop names nothing it does not own.** No builder sends a root, a
  capability, an executor, a command or a source binding; the boundary resolves
  all of them from the confirmed job.
* **A bounded refusal is shown as a refusal.** A `state: "refused"` response
  reaches the status line in the backend's own words and triggers a re-read. It
  is never converted into a success card, and the UI never papers over it.
* **One key per attempt.** An idempotency key is minted when the developer
  initiates an action and cleared when it completes, so a repeated click while
  a request is in flight replays one effect instead of creating a second.

Only the `source.scan` branch of `_drive_job` was rewired. Every other job kind
keeps its previous behaviour and gains no implied execution support.

## Reproducing the integrated proof

```bash
cd <repo>
uv run python -m unittest tests.test_orchestration -v
uv run python -m unittest tests.test_orchestration_desktop -v
```

`tests/test_orchestration_desktop.py` drives a real `MainWindow` whose `_send`
is routed straight into `boundary.handle_request` over an isolated store — no
fake backend, no stubbed response — and performs goal → plan → confirm → run →
review → decision, then closes and recreates both the backend session and the
desktop and asserts the recovered ids are identical with the scanner dispatch
count unchanged. It also covers the refusals a developer can actually reach: a
run before confirmation, and a source that moved before an acknowledgement.

`tests/test_orchestration.py` drives the **real boundary request loop** over a
temporary store and a synthetic project, counting scanner dispatches so "no
rescan" is measured rather than assumed, and hashing the selected source before
and after so "the read-only flow wrote nothing" is checked in bytes.

It covers the acceptance oracle: goal → plan → confirm → run → evidence →
decision → resume with exact ids; a fresh process recovering the same records
without rescanning; a prose requirement staying uncovered; a parse error
preserved; duplicate dispatch, confirmation and decision each having one effect;
a changed payload conflicting; the store refusing to live inside the project;
staleness on content change, addition and deletion; an unacknowledgeable stale
binding; orphan recovery to `unknown` and never a rerun; a live executor not
rewritten; a newer schema and a corrupt row refused by name; and no source text
persisted.

## Deferred

Durable Chat/Message history, semantic or provider planning, hosted agents,
multi-job dependencies and concurrency, automatic repair, Memory schema
extension, risk ranking, candidate adoption, runtime validation, credential
work, source writes and frozen-build rebuilds. This package proves persistence
and honest recovery; it does not close all Phase 5 exit criteria.
