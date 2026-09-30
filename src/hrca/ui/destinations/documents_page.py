"""Documents: the durable project documents, their library and their versions.

This destination hosts the document library, the working-document editor and
the accepted-version list — the surfaces the product already owns, kept whole
and reached from one place. It is a *hosted* destination: the workspace host
builds those widgets once, mounts them here, and keeps driving them through
the same request path as before.

Nothing here writes anything itself. Saving a document, organizing the library
and restoring an accepted version all go through the host, which sends the
boundary request.
"""

from __future__ import annotations

from .base import Destination


class DocumentsDestination(Destination):
    """The library, the editor and the accepted versions."""

    hosted = True
    title = "Documents"
    subtitle = (
        "The project's working documents, their revisions and their accepted versions. "
        "Saving stores a revision; it adopts nothing."
    )


__all__ = ["DocumentsDestination"]
