# M4.2 — Claude Code hook capture and import

This directory is the bounded evidence for the first real ingestion slice of the
M4.1 Developer Memory contract: two authorized, non-sensitive Claude Code
sessions captured through documented hook JSON and replayed into memory schema
`1.0.0`.

**Result: `Partial`.** One session reached `completed`; the second reached a
genuine non-completion that the contract reports as `unknown_outcome`, not
`failed`/`cancelled`. See [Outcome](#outcome) for why, and why no retry was run.

## Authorization and scope

Two Claude Code session starts were authorized, using the existing
authenticated client only. No direct API call, API-key read or change, model
switch, billing change, retry or third session occurred. Both sessions ran in a
purpose-built synthetic repository containing no secrets, personal files or
production data.

## Capture environment and isolation

| | |
|---|---|
| Entrypoint | `claude -p --session-id <pinned uuid> --settings <temp file>` |
| Hook configuration | the `--settings` **flag tier** — a throwaway file |
| User settings written | none |
| Project settings written | none |
| Session identities | pinned with `--session-id`, so exactly two are provable |
| Working directory | a purpose-built synthetic repository in the system temp directory |
| Spool | a transient directory outside the repository |

The flag tier overrides user, project and local settings and persists nothing,
so the global `~/.claude/settings.json` was never opened for writing. Its
SHA-256 was identical before and after the run, and it still contains no `hooks`
key. No `settings.local.json` was created at global or project level.

### Hook transport

A hook command runs under the client's shell — bash where available, otherwise
PowerShell. The command is therefore written to survive both:

```
wsl.exe -d Ubuntu -e bash -c "cd <repo> && exec .venv/bin/python -m hrca.hook_capture collect ..."
```

The whole invocation is one quoted argument that begins with a word. A bare
absolute POSIX path as a direct argument is rewritten by MSYS/Git Bash into
`C:/Program Files/Git/...` before `wsl.exe` sees it; that failure mode was found
and fixed before the first session start, not during it.

`SessionEnd` has a much shorter default hook budget than other events, so every
installed hook sets an explicit `timeout`.

## Supported hook-event map

Eight events are installed and modelled. Everything else the client documents is
disclosed as an omission rather than silently dropped.

| Hook event | Canonical event | Terminal? |
|---|---|---|
| `SessionStart` | `run_started` | no |
| `UserPromptSubmit` | `run_progress` | no |
| `PreToolUse` | `run_progress` | no |
| `PostToolUse` | `run_progress` | no |
| `PostToolUseFailure` | `run_progress` | no |
| `Stop` (`stop_hook_active` false) | `run_terminated` / `completed` | **yes** |
| `Stop` (`stop_hook_active` true) | `run_progress` | no |
| `StopFailure` | `run_terminated` / `failed` | **yes** |
| `SessionEnd` | `stream_ended` (+ a derived termination) | see below |

Terminal state is decided only by typed fields:

* `Stop` with `stop_hook_active` false is the agent concluding its response.
* `StopFailure` is the turn ending on an error instead of a conclusion.
* A `SessionEnd` reached **without** either is a session torn down before the
  agent concluded. The documented `reason` decides the rest: `clear`, `resume`,
  `logout` and `prompt_input_exit` are cancellation reasons; anything else —
  including the documented catch-all `other` — is carried through as an
  explicit **unrecognized** outcome.

No message text, command text, timestamp order or agent narrative can decide a
terminal state. Source timestamps are never invented: documented hook input
carries none, so the adapter emits none.

Tool identity comes from the client's own `tool_use_id` and prompt identity from
`prompt_id`, so identity is independent of observed content. That is what lets a
redelivery whose content changed be recognized as a *conflict* rather than
accepted as a new event.

## Content and omission policy

Never persisted, at any point:

* prompt text, assistant message text, tool responses, error text — only a
  SHA-256 digest and a character count survive, proving content existed without
  being able to reproduce it;
* the transcript — `transcript_path` is normalized as a bounded artifact
  reference and is never opened. It sits outside the session root, so the
  reference is stored with no location at all and the contract records it as an
  unresolved artifact;
* the session root as an absolute path — the spool keeps a digest only;
* any path outside the session root, and every path the M4.1 exclusion policy
  refuses;
* an unrecognized tool argument — its **name** is disclosed, its value dropped.

Every event carries an explicit account: the documented fields observed present,
the documented fields missing, the fields whose value was dropped, and the field
names this adapter does not recognize.

## The two sessions

| | Session 1 | Session 2 |
|---|---|---|
| Session id | `4d3f9a10-6b21-4c8e-9f01-2a7b5c8d9e10` | `7c1e2b30-4a52-4d9f-8b31-6e2f8a0c4d21` |
| Run id | `run:claude-code:4d3f9a10-…:run` | `run:claude-code:7c1e2b30-…:run` |
| Prompt | *Read hello.txt in the working directory, then reply with only the single word it contains.* | *Summarize this repository in one line.* |
| Actions | one `Read` tool call | none — the prompt was blocked before the model ran |
| Observed hooks | `SessionStart`, `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SessionEnd` | `SessionStart`, `UserPromptSubmit`, `SessionEnd` |
| Client exit code | 0 | 0 |
| Client `terminal_reason` | `completed` | *(absent)* |
| Client `num_turns` | 2 | 0 |
| **Normalized run state** | **`completed`** (success) | **`unknown_outcome`** (not success) |

Session 2's non-completion is a deliberate, documented capture configuration: a
`UserPromptSubmit` hook blocking the prompt with exit code 2, which the hook
contract defines as "block processing, erase original prompt". The block is
recorded as an explicit decision on the event, so the run's evidence chain
explains itself. `collect --block-event` is the whole mechanism; it is not part
of a default capture.

Two further observations from the live data:

* **The client delivered `SessionStart` twice in session 1.** Both deliveries
  carried identical content, so the second was stored as a M4.1 *duplicate*
  no-op. Idempotency is therefore demonstrated on live traffic, not only on a
  fixture.
* **The client's own success signal is not a completion signal.** Session 2
  reported `subtype: "success"` and `is_error: false` while running zero turns
  and emitting no `terminal_reason`. Trusting the client's exit-level status
  would have produced a false success. The adapter refuses it because terminal
  state comes only from typed hook transitions.

### Why no retry

`SessionEnd.reason` is the only teardown signal the surface offers, and
**both** sessions emitted the catch-all `other`. Print mode never emits a named
reason, so no `-p` session can produce a typed cancellation reason, and there is
no interrupt event at all. Mapping the catch-all to `cancelled` would be exactly
the inference M4.1 forbids, so the contract reports the honest state instead.

This is the escalation condition the task anticipated — the documented hook
surface cannot reliably distinguish a cancelled run from an ordinary exit. Per
the task's instruction, the run stopped after the second session with the
observed evidence and no third start.

## Offline controlled cases

Built from documented payloads, kept separate from the live sessions so the two
can never be confused. Their spools live under `offline/`.

| Case | Exercise | Result |
|---|---|---|
| `conflicting` | same `tool_use_id`, different observed content | 1 quarantine; run still `completed` |
| `malformed` | invalid JSON, a JSON array, empty input, oversized input | 4 content-free capture rejections; run `completed` |
| `unmodelled` | a documented event the adapter does not model (`Notification`) | disclosed as an omission; run `completed` |
| `unsupported` | an event outside the documented surface | run `unsupported`, not success |

## Outcome

* Session 1: **`completed`**, success, byte-stable replay.
* Session 2: genuine non-completion, reported **`unknown_outcome`** — explicitly
  not success, never a false success. The termination is stored *and* refused:
  the contract keeps the event and records a bounded rejection
  (`run_terminated carries an unrecognized outcome`), so the store shows both
  the observation and the explicit refusal to interpret it.

Acceptance asked for one `completed` and one `failed`/`cancelled`. Only the
first was achieved, so this work package returns **Partial** with the observed
evidence and no retry, as instructed.

## Verification

Every file in this tree was byte-scanned for the username, the capture
directories, the session prompts, the fixture content word, transcript paths,
and secret-like tokens. **Zero hits.** The only durable capture outputs are
normalized, redacted, bounded records.

Reproduce, from the repository root:

```
uv run python -m hrca.hook_capture import --spool evidence/m4.2/live/session-1
uv run python -m hrca.hook_capture import --spool evidence/m4.2/live/session-2
uv run python -m hrca.hook_capture report --spool evidence/m4.2/offline/conflicting
```

Re-importing a spool is deterministic and byte-stable.

## Cleanup and retention

Removed after import: the temporary hook settings, the client's raw JSON output,
the transient spools, and the synthetic repository. Verified afterwards: the
global settings file is byte-identical and still holds no hooks; no global or
project `settings.local.json` was created; no capture process remained.

Retained: this evidence tree only — normalized redacted records and bounded
metadata. Raw hook payloads, transcript content and prompt text are retained
nowhere.

Recovery: a spool is plain JSON Lines. A partially written spool still imports,
because the reducer refuses an incomplete stream explicitly (`missing_terminal`)
rather than guessing; a malformed line is skipped by the reader and counted. If
capture is lost entirely, re-running `collect` for the same session reproduces
identical event identities, so a recovered spool de-duplicates against an
already-imported run instead of duplicating it.
