"""Settings: provider readiness, credentials, appearance, workspace and about.

The destination hosts the settings pages the product already owns — the
provider and credential-profile manager, appearance, workspace and privacy
notes — reached from its own rail entry rather than a modal dialog, so it sits
in the same navigation model as everything else.

Credential handling is unchanged. The key is still entered only through the
native secure prompt and is never displayed; the pages show a masked presence
marker and the provider's readiness, exactly as before.
"""

from __future__ import annotations

from .base import Destination


class SettingsDestination(Destination):
    """Provider, credentials, appearance, workspace and about."""

    hosted = True
    title = "Settings"
    subtitle = (
        "Provider readiness and credentials, appearance, workspace and privacy. "
        "The API key is entered only in the native secure prompt and is never shown."
    )


__all__ = ["SettingsDestination"]
