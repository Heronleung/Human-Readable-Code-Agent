# UI-TRANSITION-2 — workspace renders

Deterministic viewport evidence for the chat-first workspace.

Regenerate with:

```bash
uv run python evidence/ui-transition-2/capture.py
```

The script builds the desktop offscreen from fixed, hand-built state and
writes one PNG per scenario per viewport. It touches no network, provider,
credential or store: the window is constructed, fed recorded values through its
own host methods, laid out and grabbed. Given the same Qt build the output is
identical, so **this script — not the images — is the durable evidence.**

## Scenarios

| Prefix | State |
| --- | --- |
| `first-use-*` | No project bound. Resume shows the product's purpose and one primary action. |
| `project-open-*` | A repository root is bound; the context bar states the repository, state and baseline. |
| `plan-*` | A goal has produced an editable Plan card, at 1024×640. |
| `blocked-*` | The plan is confirmed and its job has reported a blocker, at 1024×640. |

Every prefix is rendered for all seven destinations at **1024×640** and
**1920×1080** (the two target viewports), except `plan-*` and `blocked-*`,
which cover the affected destinations at the smaller viewport.

## What the renders show

* The thin rail carries exactly seven destinations, each with a glyph and a
  word, and none of them is hidden or collapsed.
* The context bar elides its text rather than clipping it, and always carries
  one primary action that fits at 1024×640.
* The composer states the bound context, the action and the authority before
  anything is dispatched.
* A blocked job shows its recorded blocker, a non-colour `⚠ Blocked` chip and
  only the controls its capability actually supports.
* The hosted Memory reader sits *below* the Resume summary, not above it.

## Known limitation

These renders are produced with Qt's `offscreen` platform plugin, so they
exercise real widget layout and painting but not the native window frame or a
compositor's font hinting. A Windows-hosted capture is the recommended
integrated validation step.
