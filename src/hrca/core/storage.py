"""Generic storage concerns (B2).

This module owns the three things every store in this package needs and none
of them owns: *where* the per-user application data lives, how a mapping is
serialized canonically, and how a versioned document is validated and migrated.

**Admission rule.** A symbol belongs here only when it is *generic to storing
something* — it must be impossible to tell from this module what is being
stored. It must not name a schema, a schema version, a migration step, a store
file, a directory beneath the root, a repository, a capability or a policy.
A caller supplies its own version and its own migration chain; this module
executes them and owns neither.

Why it exists
-------------

The application-data root was resolved by :mod:`hrca.twin_store`, so every
store in the package — Memory, documents, versions, the library, the provider
profile — received its location from a Twin-owned module. The migration engine
and the canonical serializer were likewise reached through :mod:`hrca.twin`.
That is a dependency on a *capability* where only storage was meant. B2 moves
the generic parts here and leaves both Twin modules re-exporting them, so no
existing physical path moves and no import path breaks.

The refusal vocabulary below is deliberately the one the package already uses,
verbatim. It was never Twin-specific: the same five sentences are written out
in nine modules that each re-implement this engine locally (``scanner``,
``memory``, ``document``, ``library``, ``codemap_draft``, ``candidate_edit``,
``intent_delta``, ``validation_plan`` and ``twin``). Naming them here makes the
shared vocabulary explicit; folding those nine copies onto this engine is a
separate change and is *not* part of this seam.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

# -- the application-data root -------------------------------------------

# The per-user directory leaf each platform has always used. These are a
# compatibility requirement, not a preference: renaming either one would orphan
# every store already sitting beneath it on a user's disk, so neither is
# normalised, unified or "improved" here. Note that the two names genuinely
# differ — `HumanReadableCodeAgent` on Windows, `human-readable-code-agent` on
# POSIX — and that difference is preserved exactly as found.
_APP_DIR_NAME_WINDOWS = "HumanReadableCodeAgent"
_APP_DIR_NAME_POSIX = "human-readable-code-agent"


def app_data_dir() -> str:
    """Return the per-user application-data directory.

    Uses the platform convention and never consults a selected repository, so
    the value cannot be influenced by which tree a caller happens to have
    open:

    * Windows — ``%LOCALAPPDATA%`` (falling back to the home directory);
    * POSIX  — ``$XDG_DATA_HOME`` (falling back to ``~/.local/share``).
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, _APP_DIR_NAME_WINDOWS)
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, _APP_DIR_NAME_POSIX)


# -- canonical serialization ---------------------------------------------

def dumps(store: Dict[str, Any]) -> str:
    """Serialize a store to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(store, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# -- schema version comparison and migration -----------------------------

def version_tuple(version: str) -> Tuple[int, ...]:
    """Return the numeric components of a ``major.minor.patch`` version string.

    Non-numeric components are dropped, so ``"1.0.0-beta"`` compares as
    ``(1, 0, 0)``. A string with no digits at all compares as ``(0,)``, which
    keeps a malformed version on the low side rather than raising here.
    """
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


def migrate(
    raw: Any,
    *,
    current_version: str,
    migrations: Mapping[str, Callable[[Dict[str, Any]], Dict[str, Any]]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and migrate a raw store mapping to ``current_version``.

    Returns ``(store, error)``: on success ``error`` is ``None``; on a
    future, unknown or malformed version, ``store`` is ``None`` and ``error``
    is one of this module's bounded reasons. The caller must retain the last
    valid store whenever this returns an error.

    ``current_version`` is the version the caller's schema is at, and
    ``migrations`` maps an *older* version string to a function that upgrades a
    store mapping. Both are the caller's: this engine runs a migration chain, it
    does not own one, and it never guesses at a version it was not told about.
    A store carrying a *future* version is refused rather than migrated down or
    half-read, and an unlisted older version is refused rather than skipped.
    """
    if not isinstance(raw, dict):
        return None, "store is not a mapping"
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, "missing schema_version"
    try:
        current = version_tuple(current_version)
        found = version_tuple(version)
    except ValueError:
        return None, "invalid schema_version"

    if found == current:
        return raw, None
    if found > current:
        return None, "schema_version is newer than supported"
    if version not in migrations:
        return None, "schema_version is not migratable"
    return migrations[version](dict(raw)), None
