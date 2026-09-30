"""Bounded agent roles — the assignment targets a plan can name.

A role here is not an autonomous entity. It is a **named, capability-scoped
worker profile**: a purpose, the exact capabilities it may use, and the widest
authority those capabilities imply, derived rather than declared so a role can
never claim more reach than its capabilities actually have. That derivation is
the point — the Agents destination shows what each role can touch because the
capability catalogue says so, not because a label asserts it.

Two roles exist on purpose to be shown as unavailable: the validator and the
repository writer. The product cannot run validation or write into the
repository from the desktop, so those roles are listed with a stated reason
and cannot be assigned work, rather than hidden (which would imply the work is
possible) or faked (which would be dishonest).

The human is a first-class owner, not an agent: ``OWNER_HUMAN`` names the
developer, who owns every job a plan assigns and who alone records a review
decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from . import authority as _authority
from . import capabilities as _capabilities

OWNER_HUMAN = "human"

# Role keys used by plans and jobs.
ROLE_COORDINATOR = "coordinator"
ROLE_SOURCE = "source"
ROLE_AUTHOR = "author"
ROLE_INTERPRETER = "interpreter"
ROLE_MEMORY = "memory"
ROLE_VALIDATOR = "validator"
ROLE_REPOSITORY = "repository"


@dataclass(frozen=True)
class AgentRole:
    """A named capability-scoped worker profile."""

    key: str
    label: str
    purpose: str
    capability_keys: Tuple[str, ...]

    @property
    def capabilities(self) -> Tuple[_capabilities.Capability, ...]:
        """Return the capabilities this role owns, in catalogue order."""
        return tuple(
            capability
            for capability in _capabilities.CAPABILITIES
            if capability.key in self.capability_keys
        )

    @property
    def available(self) -> bool:
        """Whether any capability this role owns can actually be used."""
        return any(capability.available for capability in self.capabilities)

    @property
    def unavailable_reason(self) -> str:
        """Return why this role cannot be used, or ``""`` if it can."""
        if self.available:
            return ""
        for capability in self.capabilities:
            if not capability.available:
                return capability.disabled_reason
        return "This role owns no capability the desktop can use."

    @property
    def max_authority(self) -> str:
        """Return the widest authority this role's capabilities imply."""
        widest = _authority.AUTHORITY_READ_ONLY
        for capability in self.capabilities:
            if _authority.authority_rank(capability.authority) > _authority.authority_rank(
                widest
            ):
                widest = capability.authority
        return widest

    @property
    def is_protected(self) -> bool:
        """Whether this role can reach a protected effect."""
        return _authority.is_protected(self.max_authority)


ROLES: Tuple[AgentRole, ...] = (
    AgentRole(
        key=ROLE_COORDINATOR,
        label="Coordinator",
        purpose=(
            "Turns a stated goal into an editable plan, tracks the jobs, and "
            "reports honestly. It executes nothing itself and holds no "
            "authority beyond reading what you have already recorded."
        ),
        capability_keys=(
            "project.open",
            "library.read",
            "document.read",
            "document.preview",
            "memory.read",
            "memory.search",
            "memory.resume",
            "provider.readiness",
        ),
    ),
    AgentRole(
        key=ROLE_SOURCE,
        label="Source Scanner",
        purpose=(
            "Parses the project's Python with the deterministic scanner and "
            "records what it found. Read-only: it changes no file and resolves "
            "no name."
        ),
        capability_keys=("source.scan",),
    ),
    AgentRole(
        key=ROLE_AUTHOR,
        label="Document Author",
        purpose=(
            "Creates and organizes working documents and appends their "
            "revisions. It writes only inside PrimaAgent's own stores."
        ),
        capability_keys=(
            "document.create",
            "document.save",
            "library.organize",
            "candidate.create",
            "version.restore",
        ),
    ),
    AgentRole(
        key=ROLE_INTERPRETER,
        label="Rule Interpreter",
        purpose=(
            "Prepares an offline disclosure, then — only when you confirm it — "
            "makes exactly one provider request that yields a reviewable "
            "candidate. Never an automatic change."
        ),
        capability_keys=("rule_delta.prepare", "rule_delta.interpret"),
    ),
    AgentRole(
        key=ROLE_MEMORY,
        label="Memory Reader",
        purpose=(
            "Reads the recorded runs and, when asked, appends an append-only "
            "human correction. A correction changes what is shown, never what "
            "was recorded."
        ),
        capability_keys=("memory.correction",),
    ),
    AgentRole(
        key=ROLE_VALIDATOR,
        label="Validator",
        purpose=(
            "Runs the code-owned check table through the accepted isolated "
            "runner and stores append-only evidence."
        ),
        capability_keys=("validation.run",),
    ),
    AgentRole(
        key=ROLE_REPOSITORY,
        label="Repository Writer",
        purpose="Applies accepted changes to the project repository under version control.",
        capability_keys=("repository.write",),
    ),
)

_ROLE_INDEX: Dict[str, AgentRole] = {role.key: role for role in ROLES}


def get(key: str) -> Optional[AgentRole]:
    """Return the role with ``key``, or ``None``."""
    return _ROLE_INDEX.get(key)


def require(key: str) -> AgentRole:
    """Return the role with ``key`` or raise ``KeyError``."""
    try:
        return _ROLE_INDEX[key]
    except KeyError as error:  # pragma: no cover - defensive
        raise KeyError(f"unknown role: {key}") from error


def role_label(key: str) -> str:
    """Return the human label for a role key, falling back to the key."""
    role = _ROLE_INDEX.get(key)
    return role.label if role is not None else key


def role_for_capability(capability_key: str) -> Optional[AgentRole]:
    """Return the role that owns ``capability_key``, if any.

    The catalogue is expected to be a partition: a capability belongs to at
    most one role. The first match wins so the answer is stable.
    """
    for role in ROLES:
        if capability_key in role.capability_keys:
            return role
    return None


def assignable_roles() -> Tuple[AgentRole, ...]:
    """Return the roles that can actually be assigned work."""
    return tuple(role for role in ROLES if role.available)


__all__ = [
    "OWNER_HUMAN",
    "ROLE_COORDINATOR",
    "ROLE_SOURCE",
    "ROLE_AUTHOR",
    "ROLE_INTERPRETER",
    "ROLE_MEMORY",
    "ROLE_VALIDATOR",
    "ROLE_REPOSITORY",
    "AgentRole",
    "ROLES",
    "get",
    "require",
    "role_label",
    "role_for_capability",
    "assignable_roles",
]
