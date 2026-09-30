"""The workspace context bar's record: what the workspace is bound to.

Everything here is a fact the accepted boundary already reported — a root, a
repository state, an accepted baseline, the head of an open document. The
record exists so the context bar, the plan composer and the Resume projection
all read the same values instead of each re-deriving them, and so "the
baseline moved" is a comparison of two recorded facts rather than a guess.

Nothing here is inferred. ``baseline`` is empty until an accepted version
actually exists, and the workspace says so rather than inventing a baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class ProjectContext:
    """The immutable facts the workspace is bound to, as last reported."""

    root: Optional[str] = None
    repository_state: str = "Unverified"
    baseline: str = ""
    baseline_label: str = ""
    document_id: str = ""
    document_name: str = ""
    document_revision: int = 0
    document_dirty: bool = False

    @property
    def has_project(self) -> bool:
        """Whether a project root is bound."""
        return bool(self.root)

    @property
    def has_baseline(self) -> bool:
        """Whether an accepted baseline has been recorded."""
        return bool(self.baseline)

    @property
    def has_document(self) -> bool:
        """Whether a working document is open."""
        return bool(self.document_id)

    @property
    def baseline_text(self) -> str:
        """Return a plain-language baseline line for the context bar."""
        if not self.has_baseline:
            return "No accepted baseline yet"
        return self.baseline_label or self.baseline

    @property
    def document_text(self) -> str:
        """Return a plain-language document line for the context bar."""
        if not self.has_document:
            return "No document open"
        marker = " (unsaved changes)" if self.document_dirty else ""
        return f"{self.document_name or 'Untitled'} — revision {self.document_revision}{marker}"

    def with_root(self, root: Optional[str], repository_state: str) -> "ProjectContext":
        """Return a copy bound to ``root`` with its reported repository state."""
        return replace(self, root=root, repository_state=repository_state)

    def with_baseline(self, baseline: str, label: str = "") -> "ProjectContext":
        """Return a copy carrying a newly recorded accepted baseline."""
        return replace(self, baseline=baseline, baseline_label=label)

    def with_document(
        self,
        document_id: str,
        name: str,
        revision: int,
        dirty: bool = False,
    ) -> "ProjectContext":
        """Return a copy bound to an open document."""
        return replace(
            self,
            document_id=document_id,
            document_name=name,
            document_revision=revision,
            document_dirty=dirty,
        )

    def without_document(self) -> "ProjectContext":
        """Return a copy with no document open."""
        return replace(
            self,
            document_id="",
            document_name="",
            document_revision=0,
            document_dirty=False,
        )


__all__ = ["ProjectContext"]
