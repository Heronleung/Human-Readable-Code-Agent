# The PrimaAgent workspace

PrimaAgent helps you manage coding agents. You state a goal, it proposes an
editable plan, you confirm what may run, you watch the work honestly, you
review the evidence, and you decide. It reads your repository. It changes
nothing without your explicit confirmation.

This page covers every screen and every option. You should not need it to get
started — the app explains itself — but it is the complete reference.

## First run

With no project open, PrimaAgent opens on **Resume** and states its purpose in
one line, with one primary action: **Open project**. Choose the repository
root you want the workspace bound to.

After it opens, the **context bar** across the top shows:

* the repository name and its reported state;
* the accepted baseline (or `No accepted baseline yet`);
* the open document and its revision;
* the **safest next action** as a single button, which changes as your work
  advances.

The composer at the bottom always states, before you send anything:

```
Context: …   ·   Action: propose an editable plan   ·   Authority: read only — nothing is sent
```

## The seven destinations

The thin rail on the left has exactly seven destinations. Every surface the
product keeps is reachable from one of them.

| Destination | What it is for |
| --- | --- |
| **Resume** | Where the work stands: the baseline, what changed, active/blocked/failed jobs, pending decisions, unverified claims, and one recommended next action. Also hosts the recorded Memory runs — the records behind the resume. |
| **Agent Chat** | The conversation with the coordinator, and the editable **Plan card**. |
| **Jobs** | Every job from the confirmed plan: owner, baseline, dependencies, progress, blocker and terminal state, with the controls it actually supports. |
| **Agents** | The bounded roles a plan can assign work to, what each may touch, and why an unavailable role is unavailable. |
| **Review** | Changed artifacts, evidence, missing proof, conflicts and acceptance coverage, then Approve / Request changes / Reject / Escalate. Also hosts the document's candidate preview and the project's scan evidence. |
| **Documents** | The document library, the working-document editor, and the accepted versions. |
| **Settings** | Provider readiness and credentials, appearance, workspace, privacy and about. |

## The workflow

1. **State a goal** in the composer. Nothing is sent. PrimaAgent proposes an
   editable plan and opens **Agent Chat**.
2. **Shape the plan.** The Plan card lists every job with its owner, capability,
   requested authority, risk and acceptance criteria. Untick a job to leave it
   out. The card tells you exactly which signals produced the proposal, and it
   refuses to be confirmed while anything about it is incoherent.
3. **Confirm the plan.** Confirming makes the jobs dispatchable. It runs
   nothing and sends nothing by itself.
4. **Dispatch jobs** from **Jobs**. A job whose authority is protected — a
   provider request, a credential use, a repository write, a destructive action
   — asks you to confirm that *specific effect* first, in its own dialog, every
   time. Cancelling dispatches nothing.
5. **Watch honestly.** A job can be draft, ready, running, paused, blocked,
   completed, failed, cancelled, stale or unknown. `unknown` means the outcome
   could not be observed; it is never rounded up to success. If the accepted
   baseline moves, jobs planned against the old one become `stale` rather than
   being silently re-bound.
6. **Review** the evidence and record a decision. Approving is disabled — with
   the reason stated — while proof is missing or a conflict stands.
7. **Return** any time. **Resume** reconstructs where you are and names the one
   next action.

## What each state means

| State | Meaning |
| --- | --- |
| Draft | In the plan, not yet confirmed. |
| Ready | Confirmed; waiting for you to dispatch it, and its dependencies are done. |
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

## Every option, and where it lives

* **Open project** — context bar (first run) or Resume.
* **Run read-only scan** — context bar, once a project is open.
* **Propose a plan** — the composer, from any destination.
* **Confirm plan** — the Plan card in Agent Chat, or the plan summary in Jobs.
* **Dispatch / Cancel** — a job row in Jobs. Dispatch appears only before a job
  has run; Cancel only where the capability can be interrupted.
* **Pause / Resume / Reassign** — a job row, **only** where the capability
  supports the control. A capability that cannot be paused offers no pause
  button at all rather than one that does nothing.
* **Review decisions** — the Decision card in Review.
* **Document actions** — Save, the contextual preview action, and the accepted
  version list, in Documents.
* **Library actions** — New document, New folder, Rename, Move, Trash and
  Restore, in the Documents library. Trash stays recoverable.
* **Provider, credentials, appearance, workspace, privacy** — Settings.

## Accessibility

* Every control has a visible label or an accessible name, and a disabled
  control always states why it is disabled.
* State never depends on colour: every state chip shows a glyph and a word.
* The rail is keyboard reachable with a visible focus ring, and the tab order
  follows the visual order (rail, context bar, content, composer, status).
* The window is usable at 1024×640 and 1920×1080 without clipping a primary
  action.

## What PrimaAgent will not do

* It does not run autonomous agents. A role may use only the capabilities
  listed under **Agents**, and nothing reaches beyond the authority those
  capabilities imply.
* It does not write to your repository, adopt a version, merge, deploy, or use
  a credential without its own explicit confirmation.
* It never shows an API key. The key is entered only in the native secure
  prompt and is stored in your operating system's credential store.
