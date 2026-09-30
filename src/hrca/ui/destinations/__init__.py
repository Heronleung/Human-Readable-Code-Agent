"""The seven destinations of the chat-first workspace.

Each module here renders one destination of the rail. A destination owns its
presentation and calls back to the workspace host through the small protocol
documented in :mod:`hrca.ui.destinations.base`; it holds no backend client and
issues no request directly, so the single request path stays in one place.
"""

from __future__ import annotations

__all__: list = []
