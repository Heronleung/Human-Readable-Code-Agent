# PrimaAgent workspace — developer notes

The chat-first workspace is a presentation layer over the accepted
source-evidence core. It adds **no** boundary action, store, schema or
migration. Everything it holds is local presentation state; everything durable
stays owned by the boundary.

## Layers

```
src/hrca/ui/
  appmodel/       pure Python, Qt-free, no I/O — the whole orchestration model
  components.py   reusable widgets built from the token contract (incl. Composer)
  widgets.py      low-level Qt primitives (CodeView, ElidedLabel, splitters, trees)
  shell.py        the frame: four rail groups, context bar, drawer, conditional footer
  destinations/   base + home_page, chat_page, work_page, documents_page,
                  settings_page, history_page, and the embedded views
                  jobs_page / agents_page / review_page
  client.py       MainWindow: backend routing + the destinations' host
  style.py        the stylesheet and palette
```

`Destination` has two class flags that shape the frame:

* `embedded = True` — the page is shown *inside* another page (Jobs, Agents and
  Review live inside Work), so it renders no header of its own. Its container
  supplies the title and the one "New task" action; repeating them would
  duplicate both.
* `show_new_task = False` — the page owns the composer (Agent Chat), so it must
  not also offer a shortcut to itself.

`appmodel` must stay importable without PySide6 and without the boundary. It
imports only the standard library and, for its constants, nothing at all —
capability action names are held as literals and cross-checked against
`hrca.core.contract` by `tests/test_ui_appmodel.py`, which is what keeps the
catalogue from drifting without coupling the package to the contract module.

## The honest-state rules

These are enforced in `appmodel`, not in the widgets, so no screen can restate
them differently:

* `Workspace.confirm_plan` refuses while `validate_plan` reports any problem.
* `Workspace.dispatch` refuses unless the plan is confirmed, the dependencies
  have completed, the capability is available, the binding is not stale, and —
  for a protected authority — **every** protected effect the authority implies
  has been confirmed for that job.
* `Workspace.pause/resume_job/cancel/reassign` refuse unless the capability's
  `Control` says it supports the operation. `Workspace.supports` is what a
  destination asks before rendering the control, so no button is shown that
  cannot be honoured.
* `Workspace.report_job` records an observed state. A completion is a claim:
  `review.advances_acceptance` returns `False` unconditionally, because a
  decision recorded here never adopts, writes or moves a baseline.
* `Workspace.set_context` marks non-terminal, non-running, non-draft jobs
  `stale` when the accepted baseline no longer matches the one the plan was
  proposed under. It never re-binds a job.

## Authority and capabilities

`appmodel/capabilities.py` is the catalogue of what the desktop can actually
reach. Each entry names the one boundary action string it dispatches (or
`None`, with a reason, when the product genuinely cannot do it). The Validator
and Repository Writer roles exist precisely to be shown unavailable with a
stated reason rather than faked.

`appmodel/authority.py` defines protection by the *protected effects* an
authority implies — provider dispatch, repository write, credential use, cost,
destructive action, adoption, merge, deployment. A local write inside
PrimaAgent's own stores implies none of these, so it is not protected. Defining
it this way keeps the rule identical to the product's own approval boundaries.

## Adding a capability or a role

1. Add a `Capability` to `CAPABILITIES` naming a real boundary action.
2. Add it to exactly one role's `capability_keys`, or to none if it is a
   human-only action (adoption and credential management are).
3. `tests/test_ui_appmodel.py` will fail if the action name is not in
   `contract.ALLOWED_ACTIONS`, if a role claims more authority than its
   capabilities imply, or if a capability is owned by two roles.

## Adding a destination

Subclass `destinations.base.Destination`. A *rendered* destination implements
`render()` and rebuilds its body from `self.workspace` on every `refresh()`. A
*hosted* destination (`hosted = True`) is a container for widgets the client
builds and keeps; a refresh never clears its `hosted_body`.
`client._build_destinations` mounts the host-owned surfaces and registers each
page with the shell.

**Progressive disclosure is a rule, not a style.** A destination reveals a
section only when it has data, and never presents the same action twice in one
region. `WorkDestination.refresh` is the reference implementation: it computes
relevance from the workspace (`has_plan`, `assigns_roles`, `has_evidence`) and
hides the views that would otherwise show empty internals. `Shell.set_started`
does the same for the rail. The one thing that is never hidden is safety state
— `HomeDestination` always renders its **Needs attention** section when a job is
blocked or failed, a decision is pending or a claim is unverified.

**Surfaces are natural height; surplus belongs to the canvas.** Every card and
state view follows one rule, defined in `components.py` as `SURFACE_PADDING`
(24 px), `SURFACE_GAP` (12 px) and `natural_height()`:

* a surface is pinned to `QSizePolicy.Maximum` vertically, so a layout that
  respects size policy gives it exactly the height its content needs;
* it also carries a trailing stretch *after* its content, because a
  `QStackedLayout` — which is what a destination inside Work uses — sets its
  page's geometry to the full rect regardless of size policy. That stretch is
  what keeps a heading, its explanation and its action one group instead of
  three items flung apart.

The regression this prevents: a `QFrame`'s default vertical policy is
`Preferred`, so in a scroll area the card grew to the viewport and its labels
absorbed the slack — a one-line heading measured 93 px and its action sat
200 px below it. `tests/test_ui_layout.py` pins the rule by measuring geometry.

Two shells needed an explicit surplus consumer rather than a bare stretch,
because their two states have different owners of the page height:
`WorkDestination._tail` (visible with the empty panel, hidden with the stack)
and `MainWindow._document_tail` (visible with the empty panel, hidden with the
editor). Both greedy at once would split the page in half.

The host protocol a destination may call is documented at the top of
`destinations/base.py`. A destination never imports the request builders and
never sends anything itself — that keeps exactly one module talking to the
boundary.

## The client

`MainWindow` still owns the single request path: the two `BackendSupervisor`
processes, the `_pending` correlation map, the `_send`/`_send_credential`
methods and every `_on_*` / `_apply_*` response handler. The redesign replaced
its *presentation*, not its routing: `_build_ui` now builds a `Shell` and
registers seven destinations, and the surfaces that survived the redesign
(the library explorer, the document workspace, the versions drawer, the
candidate preview, the Memory reader, the Settings pages, the scan evidence
views) are built by their original `_build_*` methods and mounted into the
destination that now owns them.

`_sync_workspace_context` is the one bridge from backend state into the model:
it derives the bound root, the open document and the accepted baseline from
what the boundary already reported and pushes them into `Workspace`.

## Tests

| Module | Covers |
| --- | --- |
| `tests/test_ui_appmodel.py` | the model: states, authority, capabilities, roles, plan validation, the composer, the full workflow, honesty rules |
| `tests/test_ui_components.py` | accessible names, disabled reasons, non-colour state cues, component styling |
| `tests/test_ui_workspace.py` | the rail, first run, goal → plan, review decisions, accessible controls, and the *removal* of the Advanced/Memory/Twin surfaces |
| `tests/test_client_gui_*.py` | the retained backend behaviours through the new shell |

Run them with `uv run python -m unittest tests.test_ui_appmodel
tests.test_ui_components tests.test_ui_workspace`.

For a setup-only change, use `uv run python -m hrca.setup_verification_cli`;
never `unittest discover`, which also selects the modules that dispatch real
containers when a daemon is reachable.
