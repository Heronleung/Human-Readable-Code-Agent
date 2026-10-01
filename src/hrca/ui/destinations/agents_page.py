"""Agents: the bounded roles a plan can assign work to.

A role is shown with its purpose, the exact capabilities it owns, the widest
authority those capabilities imply, and — when it cannot be used — the reason.
Two roles are listed precisely because the desktop cannot use them: the
Validator and the Repository Writer. Showing them with a stated reason is
honest; hiding them would imply the work is possible, and inventing an agent
behind them would be worse.
"""

from __future__ import annotations


from PySide6.QtWidgets import QWidget

from ..appmodel import agents as _agents
from ..appmodel import authority as _authority
from ..components import Card, make_button
from .base import Destination

NO_AGENT_NOTE = (
    "PrimaAgent does not run autonomous agents. Each role below is a bounded "
    "worker profile: it may use only the named capabilities, and no role can "
    "reach beyond the authority those capabilities imply."
)


class AgentsDestination(Destination):
    """Every role, its capabilities and its authority."""

    #: Shown inside Work, which supplies the title and the "New task" action.
    embedded = True
    title = "Agents"
    subtitle = "The bounded roles a plan can assign work to, and what each may touch."

    def render(self) -> None:
        """Re-render the role list."""
        self.add_state_view("empty", "How agents work here", NO_AGENT_NOTE)

        for role in _agents.ROLES:
            card = Card(role.label, role.purpose, self)
            authority_line = _authority.describe_authority(role.max_authority)
            card.body.addWidget(self._field("Authority", authority_line))

            capability_text = "\n".join(
                f"• {capability.label}"
                + ("" if capability.available else f" — unavailable: {capability.disabled_reason}")
                for capability in role.capabilities
            )
            card.body.addWidget(self._field("Capabilities", capability_text))

            if not role.available:
                card.body.addWidget(self._field("Availability", role.unavailable_reason))
                card.body.addWidget(
                    make_button(
                        "Unavailable",
                        "secondary",
                        accessible=f"{role.label} is unavailable",
                        enabled=False,
                        disabled_reason=role.unavailable_reason,
                    )
                )
            self.body.addWidget(card)

        self.body.addStretch(1)

    def _field(self, label: str, value: str) -> QWidget:
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QLabel(label, container)
        heading.setObjectName("sectionLabel")
        layout.addWidget(heading)
        text = QLabel(value, container)
        text.setObjectName("secondary")
        text.setWordWrap(True)
        layout.addWidget(text)
        return container


__all__ = ["AgentsDestination", "NO_AGENT_NOTE"]
