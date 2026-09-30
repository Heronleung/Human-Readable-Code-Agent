"""The Agent Chat transcript — local, ordered, and never a record of truth.

The transcript holds what the developer said, what the coordinator proposed and
what the workspace observed. It is deliberately *not* an evidence store: a
message can point at a record with ``reference``, but the record it points at
is owned by the accepted backend, and the message is only a pointer. Nothing
in a transcript can be mistaken for a stored fact.

Message keys are assigned in arrival order by the workspace, so the transcript
is deterministic and a message can be addressed by key without relying on its
position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

# Who wrote a message. ``system`` is the workspace itself speaking — an honest
# notice about state, never a claim on a person's behalf.
AUTHOR_DEVELOPER = "developer"
AUTHOR_COORDINATOR = "coordinator"
AUTHOR_SYSTEM = "system"

AUTHORS: Tuple[str, ...] = (AUTHOR_DEVELOPER, AUTHOR_COORDINATOR, AUTHOR_SYSTEM)

AUTHOR_LABELS = {
    AUTHOR_DEVELOPER: "You",
    AUTHOR_COORDINATOR: "Coordinator",
    AUTHOR_SYSTEM: "Workspace",
}

# What kind of thing a message is, which decides how it renders.
KIND_MESSAGE = "message"
KIND_PLAN = "plan"
KIND_NOTICE = "notice"
KIND_EVIDENCE = "evidence"
KIND_DECISION = "decision"

KINDS: Tuple[str, ...] = (
    KIND_MESSAGE,
    KIND_PLAN,
    KIND_NOTICE,
    KIND_EVIDENCE,
    KIND_DECISION,
)


@dataclass(frozen=True)
class ChatMessage:
    """One entry in the transcript."""

    key: str
    author: str
    kind: str
    text: str
    reference: str = ""
    detail: str = ""

    @property
    def author_label(self) -> str:
        """Return the display name of the author."""
        return AUTHOR_LABELS.get(self.author, self.author)

    @property
    def is_plan(self) -> bool:
        """Whether this message is the position of the live plan card."""
        return self.kind == KIND_PLAN

    @property
    def is_notice(self) -> bool:
        """Whether this message is a workspace notice."""
        return self.kind == KIND_NOTICE


__all__ = [
    "AUTHOR_DEVELOPER",
    "AUTHOR_COORDINATOR",
    "AUTHOR_SYSTEM",
    "AUTHORS",
    "AUTHOR_LABELS",
    "KIND_MESSAGE",
    "KIND_PLAN",
    "KIND_NOTICE",
    "KIND_EVIDENCE",
    "KIND_DECISION",
    "KINDS",
    "ChatMessage",
]
