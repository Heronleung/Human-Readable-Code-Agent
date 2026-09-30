"""Local presentation state for the chat-first workspace (UI-TRANSITION-2).

This subpackage is the client-side orchestration model behind the redesigned
desktop. It is **pure Python**: it imports no Qt, no boundary host, no store,
no provider, no credential primitive and no scanner, and it performs no I/O.
The desktop renders it; tests exercise it directly.

Nothing here is a persisted product record. The accepted backend owns every
durable record — documents, revisions, accepted versions, Memory runs,
validation evidence — and this model never restates or overrides one. A goal,
a plan, a job, an agent role and a review decision are *local presentation
state*: they exist so a developer can shape work, see bounded progress and
recover context. They are never written to a store the boundary owns, and no
value here can advance an accepted baseline. Only the boundary's own actions,
reached through the desktop's existing request path, can do that.

The honest-state rule is the spine of the package: a job reports only what was
actually observed. An agent's reported completion is a *claim* that leaves a
job ``completed`` in this model and changes no accepted state; an outcome that
could not be observed is ``unknown`` and is never rounded up to success; a
capability the desktop cannot reach is ``unavailable`` with a stated reason
rather than a control that silently does nothing.
"""

from __future__ import annotations

__all__: list = []
