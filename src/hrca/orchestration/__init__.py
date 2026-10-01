"""A minimal persisted orchestration backbone for the read-only scan slice.

Five authoritative record families — Plan revision, Job, AgentRun, Evidence and
Decision — live here, owned by the backend, in a dedicated app-data namespace
outside any selected repository. Review and Resume are **projections** over
those records, never stores of their own, so there is no second copy of
"completed" or "verified" to disagree with the first.

The package is split by responsibility:

* :mod:`~hrca.orchestration.domain` is pure: the record shapes, the bounded
  state vocabularies, the code-owned acceptance predicates and the review and
  resume projections. It performs no I/O and imports no store.
* :mod:`~hrca.orchestration.store` is the sole storage owner. It owns the
  SQLite schema, its migration registry and every transaction.
* :mod:`~hrca.orchestration.manifest` binds the *actual bytes* of a selected
  scope — including dirty and untracked files — to a bounded content digest.
* :mod:`~hrca.orchestration.service` composes the three into the operations the
  boundary exposes.

Nothing here dispatches a provider, opens a network connection, reads a
credential, executes a command, runs a container or writes to the selected
repository or to Git metadata. The one execution this slice performs is the
existing deterministic scanner, reached through its existing seam.
"""

from __future__ import annotations

__all__: list = []
