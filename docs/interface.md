# The PrimaAgent workspace

PrimaAgent helps you manage coding agents. You open a project, state a goal,
get an editable plan, confirm what may run, watch the work honestly, review the
evidence, and decide. It reads your repository. It changes nothing without your
explicit confirmation.

The interface shows you only the next useful decision. Advanced records,
capability detail and diagnostics stay available, but they appear when they are
relevant rather than all at once.

## First run

With no project open, PrimaAgent shows exactly one path:

* a short statement of what the product is for;
* any projects you opened earlier in this session;
* **one** primary action: **Open project**;
* **Settings**, anchored at the foot of the rail for provider setup.

Nothing else is on screen. There is no goal field, no scan action, no provider
warning, no status footer and no inactive navigation: a project-dependent
surface appears only once there is a project to act on.

## After you open a project

The rail carries four primary destinations, with Settings anchored separately:

| Destination | What it is for |
| --- | --- |
| **Home** | Continue, Needs attention, Recent changes — and Project history. |
| **Agent Chat** | The conversation, the editable Plan card, and the one composer. |
| **Work** | Jobs, Agents and Review, shown when they have something to say. |
| **Documents** | The document library, the editor and the accepted versions. |
| **Settings** | Provider readiness and credentials, appearance, workspace, privacy. |

Jobs, Agents and Review are **Work's contextual views**, so every functional
surface is at most two interactions away: one click to Work, one to the view.

Across the top, the **context bar** shows the repository and its reported
state, the accepted baseline and the open document, one concise **authority**
indicator, and the single safest next action. A **New task** action on each
non-Chat page returns you to Agent Chat.

## The workflow

1. **State a goal** in Agent Chat's composer. Nothing is sent. PrimaAgent
   proposes an editable plan.
2. **Shape the plan.** The Plan card lists every job with its owner,
   capability, requested authority, risk and acceptance criteria. Untick a job
   to leave it out. This is the one place the full authority is shown.
3. **Confirm the plan.** Confirming makes the jobs dispatchable. It runs
   nothing and sends nothing by itself.
4. **Dispatch jobs** from **Work → Jobs**. A job whose authority is protected —
   a provider request, a credential use, a repository write, a destructive
   action — asks you to confirm that *specific effect* first, every time.
   Cancelling dispatches nothing.
5. **Watch honestly.** A job can be draft, ready, running, paused, blocked,
   completed, failed, cancelled, stale or unknown. `unknown` means the outcome
   could not be observed and is never rounded up to success. If the accepted
   baseline moves, jobs planned against the old one become `stale` rather than
   being silently re-bound.
6. **Review** in **Work → Review** once a job has produced evidence. Approval
   is disabled — with the reason stated — while proof is missing or a conflict
   stands.
7. **Return** any time. **Home** reconstructs where you are and names the one
   next action.

## What each state means

| State | Meaning |
| --- | --- |
| Draft | In the plan, not yet confirmed. |
| Ready | Confirmed; waiting for you to dispatch it. |
| Running | Dispatched. |
| Paused | Stopped by you. Only offered where the capability supports it. |
| Blocked | Cannot proceed; the recorded reason is shown. |
| Completed | Reported finished. **A claim** — it changes no accepted state. |
| Failed | Ended without success. The evidence is retained. |
| Cancelled | Abandoned by you. The evidence is retained. |
| Stale | Planned against a baseline that has since moved. Re-plan it. |
| Unknown | The outcome could not be observed. Never assumed to be success. |

## Decisions

| Decision | What it does |
| --- | --- |
| Approve | Records that a named human accepts the evidence. It does **not** adopt, merge, deploy or write anything. |
| Request changes | Returns the work for revision and records why. Nothing is accepted. |
| Reject | Declines the work as presented. Evidence is retained, not deleted. |
| Escalate | Hands the decision to someone else and records the open question. |

Moving the accepted baseline is always a separate, explicit step in
**Documents**. No decision you record here does it for you.

## What is deliberately not on screen

These are available, not removed — they appear when relevant:

| Surface | Where it lives |
| --- | --- |
| The Developer Memory reader (Documents, Search, Resume, Corrections) | **Home → Project history**, read-only |
| The capability catalogue | **Work → Agents**, once a plan assigns a role |
| Review detail | **Work → Review**, once a job has produced evidence |
| Diagnostics and the recorded status | The **Activity** action in the context bar |
| Provider readiness | The context bar, as a warning, only when a dispatch needs a provider that is not configured |
| The scan action | The context bar, once a project is open |

**Needs attention** on Home is never hidden. A blocking risk, a failed or
blocked job, a pending approval and an unverified claim always appear there
regardless of anything else on the page. Progressive disclosure reduces
irrelevant controls, never safety information.

## Documents

One primary action per state, and never the same action twice:

* no document selected → **Create document**;
* a document selected → Save, and its one contextual preview action;
* the versions list carries no action of its own — the editor's footer already
  owns it, and opening a project belongs to the context bar.

## Accessibility

* Every control has a visible label or an accessible name, and a disabled
  control states why it is disabled.
* State never depends on colour: every state chip shows a glyph and a word.
* The rail is keyboard reachable with a visible focus ring, and the tab order
  follows the visual order.
* The window is usable at 1024×640 and 1920×1080 without clipping a primary
  action.

## What PrimaAgent will not do

* It does not run autonomous agents. A role may use only the capabilities
  listed under **Work → Agents**, and nothing reaches beyond the authority
  those capabilities imply.
* It does not write to your repository, adopt a version, merge, deploy, or use
  a credential without its own explicit confirmation.
* It never shows an API key. The key is entered only in the native secure
  prompt and is stored in your operating system's credential store.
