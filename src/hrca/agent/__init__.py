"""The bounded agent loop: typed model choices over durable current-project facts.

PrimaAgent is the agent. This package is the smallest honest expression of that:
one bounded turn in which a model adapter chooses one typed next action —
``clarify``, ``answer``, ``read_record``, ``propose_scan`` or ``stop`` — over the
project's own durable context, and the backend validates, policed and applies
only what is permitted.

The package is split by responsibility:

* :mod:`~hrca.agent.contract` is a stdlib-only leaf: the closed payload shape a
  model answer must take, its bounds and its validator.
* :mod:`~hrca.agent.policy` is the immutable backend tool policy: a validated
  choice passes through it and can never widen it.
* :mod:`~hrca.agent.context` assembles the minimum structured context from a
  field allowlist; no raw source, diff, log or secret ever enters it.
* :mod:`~hrca.agent.session_store` is the sole owner of the agent session
  namespace. It references orchestration records by exact id and never re-owns
  the scan workflow.
* :mod:`~hrca.agent.service` composes the four into one bounded turn.

Nothing here dispatches a scanner, runs a command, opens a socket, reads a
credential, or writes to the selected repository. A ``propose_scan`` choice
creates a **draft** plan and nothing more: confirmation and the scan itself stay
separate, human actions on the existing owner's lifecycle.
"""

from __future__ import annotations

__all__: list = []
