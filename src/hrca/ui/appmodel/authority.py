"""Authority levels and the protected effects that stay separate approvals.

The workspace states, before dispatch, exactly what a job will be allowed to
do. Two vocabularies do that work:

* an **authority level** — the widest reach a job's capability needs; and
* a **protected effect** — a specific side effect that the accepted product
  keeps behind its own explicit, per-occurrence approval.

A level above ``read_only`` is *protected*: the workspace will not dispatch it
until the developer confirms that specific effect. The protected-effect
vocabulary is deliberately the one the product already commits to — provider
dispatch, repository writes, credential use, cost, destructive action,
adoption, merge and deployment — so a job cannot quietly widen what it asked
for after confirmation.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Authority levels, ordered by reach.
# ---------------------------------------------------------------------------
AUTHORITY_READ_ONLY = "read_only"
AUTHORITY_LOCAL_WRITE = "local_write"
AUTHORITY_PROVIDER_CALL = "provider_call"
AUTHORITY_CREDENTIAL_USE = "credential_use"
AUTHORITY_REPOSITORY_WRITE = "repository_write"
AUTHORITY_DESTRUCTIVE = "destructive"

AUTHORITY_LEVELS: Tuple[str, ...] = (
    AUTHORITY_READ_ONLY,
    AUTHORITY_LOCAL_WRITE,
    AUTHORITY_PROVIDER_CALL,
    AUTHORITY_CREDENTIAL_USE,
    AUTHORITY_REPOSITORY_WRITE,
    AUTHORITY_DESTRUCTIVE,
)

AUTHORITY_LABELS: Dict[str, str] = {
    AUTHORITY_READ_ONLY: "Read only",
    AUTHORITY_LOCAL_WRITE: "Write inside PrimaAgent only",
    AUTHORITY_PROVIDER_CALL: "Send one confirmed provider request",
    AUTHORITY_CREDENTIAL_USE: "Use a stored credential",
    AUTHORITY_REPOSITORY_WRITE: "Write into the project repository",
    AUTHORITY_DESTRUCTIVE: "Destructive action",
}

AUTHORITY_RANK: Dict[str, int] = {name: index for index, name in enumerate(AUTHORITY_LEVELS)}

# ---------------------------------------------------------------------------
# Protected effects — each remains its own approval boundary.
# ---------------------------------------------------------------------------
EFFECT_PROVIDER_DISPATCH = "provider_dispatch"
EFFECT_REPOSITORY_WRITE = "repository_write"
EFFECT_CREDENTIAL_USE = "credential_use"
EFFECT_COST = "cost"
EFFECT_DESTRUCTIVE = "destructive_action"
EFFECT_ADOPTION = "adoption"
EFFECT_MERGE = "merge"
EFFECT_DEPLOYMENT = "deployment"

PROTECTED_EFFECTS: Tuple[str, ...] = (
    EFFECT_PROVIDER_DISPATCH,
    EFFECT_REPOSITORY_WRITE,
    EFFECT_CREDENTIAL_USE,
    EFFECT_COST,
    EFFECT_DESTRUCTIVE,
    EFFECT_ADOPTION,
    EFFECT_MERGE,
    EFFECT_DEPLOYMENT,
)

PROTECTED_EFFECT_LABELS: Dict[str, str] = {
    EFFECT_PROVIDER_DISPATCH: "Provider dispatch",
    EFFECT_REPOSITORY_WRITE: "Repository write",
    EFFECT_CREDENTIAL_USE: "Credential use",
    EFFECT_COST: "Cost",
    EFFECT_DESTRUCTIVE: "Destructive action",
    EFFECT_ADOPTION: "Adoption",
    EFFECT_MERGE: "Merge",
    EFFECT_DEPLOYMENT: "Deployment",
}

# The effects an authority level implies. A level implies every effect below
# it, so a job requesting ``provider_call`` is understood to send one request
# and to spend.
AUTHORITY_EFFECTS: Dict[str, Tuple[str, ...]] = {
    AUTHORITY_READ_ONLY: (),
    AUTHORITY_LOCAL_WRITE: (),
    AUTHORITY_PROVIDER_CALL: (EFFECT_PROVIDER_DISPATCH, EFFECT_COST),
    AUTHORITY_CREDENTIAL_USE: (EFFECT_CREDENTIAL_USE,),
    AUTHORITY_REPOSITORY_WRITE: (EFFECT_REPOSITORY_WRITE,),
    AUTHORITY_DESTRUCTIVE: (EFFECT_DESTRUCTIVE,),
}


def authority_label(authority: str) -> str:
    """Return the human label for an authority level."""
    return AUTHORITY_LABELS.get(authority, authority)


def authority_rank(authority: str) -> int:
    """Return the reach rank of ``authority`` (unknown levels rank highest)."""
    return AUTHORITY_RANK.get(authority, 99)


def is_protected(authority: str) -> bool:
    """Return whether ``authority`` needs an explicit per-job confirmation.

    Protection is defined by the *protected effects* an authority implies, not
    by its rank: writing inside PrimaAgent's own document store is a local
    write with no entry in the protected list, so it is not protected, while a
    provider call, a credential use, a repository write or a destructive action
    is. Defining it this way keeps the rule identical to the product's own
    approval boundaries instead of an approximation of them.
    """
    return bool(effects_for(authority))


def effects_for(authority: str) -> Tuple[str, ...]:
    """Return the protected effects an authority level implies."""
    return AUTHORITY_EFFECTS.get(authority, ())


def exceeds(claimed: str, granted: str) -> bool:
    """Return whether ``claimed`` reaches further than ``granted``.

    Used to refuse a job that asks for more authority than the plan was
    confirmed with, rather than silently widening it.
    """
    return authority_rank(claimed) > authority_rank(granted)


def effect_label(effect: str) -> str:
    """Return the human label for a protected effect."""
    return PROTECTED_EFFECT_LABELS.get(effect, effect)


def describe_authority(authority: str) -> str:
    """Return a one-line, plain-language description of what a level allows."""
    label = authority_label(authority)
    effects = effects_for(authority)
    if not effects:
        return label
    named = ", ".join(effect_label(effect).lower() for effect in effects)
    return f"{label} ({named})"


def protected_effect(authority: str) -> Optional[str]:
    """Return the single protected effect to confirm for ``authority``.

    Returns ``None`` for a read-only level, so a caller can gate confirmation
    on this one call rather than re-deriving the rule.
    """
    effects = effects_for(authority)
    return effects[0] if effects else None


__all__ = [
    "AUTHORITY_READ_ONLY",
    "AUTHORITY_LOCAL_WRITE",
    "AUTHORITY_PROVIDER_CALL",
    "AUTHORITY_CREDENTIAL_USE",
    "AUTHORITY_REPOSITORY_WRITE",
    "AUTHORITY_DESTRUCTIVE",
    "AUTHORITY_LEVELS",
    "AUTHORITY_LABELS",
    "AUTHORITY_RANK",
    "EFFECT_PROVIDER_DISPATCH",
    "EFFECT_REPOSITORY_WRITE",
    "EFFECT_CREDENTIAL_USE",
    "EFFECT_COST",
    "EFFECT_DESTRUCTIVE",
    "EFFECT_ADOPTION",
    "EFFECT_MERGE",
    "EFFECT_DEPLOYMENT",
    "PROTECTED_EFFECTS",
    "PROTECTED_EFFECT_LABELS",
    "AUTHORITY_EFFECTS",
    "authority_label",
    "authority_rank",
    "is_protected",
    "effects_for",
    "exceeds",
    "effect_label",
    "describe_authority",
    "protected_effect",
]
