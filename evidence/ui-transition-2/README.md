# UI-TRANSITION-2 — workspace renders

Deterministic viewport evidence for the chat-first workspace, showing the
Form 2R progressive-disclosure states.

Regenerate with:

```bash
uv run python evidence/ui-transition-2/capture.py
```

The script builds the desktop offscreen from fixed, hand-built state and
writes one PNG per acceptance state per destination per viewport. It touches no
network, provider, credential or store: the window is constructed, fed recorded
values through its own host methods, laid out and grabbed. Given the same Qt
build the output is identical, so **this script — not the images — is the
durable evidence.**

## Acceptance states

| Prefix | State |
| --- | --- |
| `first-use-*` | No project bound. One path: the product's purpose and one primary action. |
| `project-open-*` | A repository root is bound; the four groups plus Settings are offered. |
| `plan-*` | A goal has produced an editable Plan card, unconfirmed. |
| `blocked-*` | The plan is confirmed and its job has reported a blocker. |
| `review-ready-*` | Every job completed with evidence; the decision is open. |
| `resumed-*` | A decision has been recorded; the post-decision resume. |

Each state is rendered for **home, chat, work, documents, settings and
history**, plus Work's contextual views (Jobs, Agents, Review) whenever they are
relevant, at **1024×640** and **1920×1080**.

## What the renders show

* **First use** presents the purpose and exactly one primary action. The
  project-dependent rail entries, the composer, the scan action, the authority
  chip, the provider warning and the status footer are all absent.
* After a project opens, the rail carries **four primary groups** — Home, Agent
  Chat, Work, Documents — with **Settings** anchored at the foot. Jobs, Agents
  and Review are Work's contextual views, so all seven functional surfaces are
  at most two interactions away.
* **Work** opens on Jobs and reveals Agents once a plan assigns a role, and
  Review once a job has produced evidence. Empty Work is one *Start in Agent
  Chat* action.
* **Home** shows Continue, Needs attention (never hidden) and Recent changes,
  with the raw Developer Memory reader behind one **Project history** entry.
* Only **Agent Chat** carries a composer; every other page offers one *New
  task* action.
* **Documents** shows one primary action per state and never the same action
  twice.
* The status footer appears only for a warning, failure or refusal; a
  successful operation does not leave a permanent message.

## Known limitation

These renders are produced with Qt's `offscreen` platform plugin, so they
exercise real widget layout and painting but not the native window frame or a
compositor's font hinting. A Windows-hosted capture is the recommended
integrated validation step.
