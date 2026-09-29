"""Controlled-change reconciliation record (P5-E2).

The missing link in the Phase 5 chain. The product can already produce a typed
intent and an advisory impact proposal, materialise a candidate and its
canonical diff, record validation evidence, and accept a version — but nothing
joins those P5.x identities to the two facts that make a change *controlled*: an
explicit human acceptance, and an independently observed post-application
repository baseline. This module is that join, and nothing else.

A pure function from supplied documents to one record
-----------------------------------------------------

It takes **mappings** and returns a canonical record, or ``(None, reason)`` when
the supplied evidence cannot be bound at all. It has no store, no filesystem, no
clock, no process, no network, no provider, no container and no credential, and
it imports no Twin, Memory, validation, execution, integrations, boundary or UI
module. It reads only :func:`hrca.core.identity.sha256_hex`, for its own
content-addressed identity.

That constraint has a deliberate cost, stated so nobody has to infer it: this
module does **not** re-derive the Twin freshness verdict and does **not**
re-validate the validation result. Deriving them here would either import the
modules that own them or create a second implementation of their contracts, so
the *adapter* invokes those owners' functions and supplies their verdicts, and
this module binds what it is given. What it adds is Phase 5's **acceptance
policy**, which is a different claim from either schema: only an explicit
acceptance, a passing and evidence-complete validation, a current freshness
verdict, agreeing identities and a declared outcome that matches the observed
rescan may produce ``complete``.

Refusal versus state
--------------------

Two different failures, kept apart as everywhere else in this package.

A **refusal** is ``(None, reason)``: the input cannot be bound — a group is
missing or malformed, an identity is absent or does not carry its own prefix,
or a decision or declared outcome is not one of the recognised tokens.

A **non-success state** is a record: everything bound, and the answer is "not
complete". ``complete`` is never reachable from an operator's assertion. The
precedence is fixed and is the order of the Phase 5 chain: ``identity_conflict``
outranks ``validation_not_accepted``, which outranks ``awaiting_acceptance`` and
``acceptance_refused``, which outrank ``freshness_lost``, which outranks
``application_unconfirmed``.

What this record is not
-----------------------

It approves nothing, applies nothing, accepts nothing on anyone's behalf and
runs nothing. It declares that whole surface false. It is a statement about
supplied evidence and observed facts, and it says so in its own limitations.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from ..core.identity import sha256_hex

RECONCILIATION_SCHEMA_VERSION = "1.0.0"
RECONCILIATION_GENERATOR = "hrca-work-reconciliation"
RECONCILIATION_ID_PREFIX = "reconcile:"

# Terminal states. ``complete`` is the only success; every other value is a
# bounded non-success a reviewer must be able to see.
STATE_COMPLETE = "complete"
STATE_IDENTITY_CONFLICT = "identity_conflict"
STATE_VALIDATION_NOT_ACCEPTED = "validation_not_accepted"
STATE_AWAITING_ACCEPTANCE = "awaiting_acceptance"
STATE_ACCEPTANCE_REFUSED = "acceptance_refused"
STATE_FRESHNESS_LOST = "freshness_lost"
STATE_APPLICATION_UNCONFIRMED = "application_unconfirmed"
RECONCILIATION_STATES = frozenset(
    {
        STATE_COMPLETE,
        STATE_IDENTITY_CONFLICT,
        STATE_VALIDATION_NOT_ACCEPTED,
        STATE_AWAITING_ACCEPTANCE,
        STATE_ACCEPTANCE_REFUSED,
        STATE_FRESHNESS_LOST,
        STATE_APPLICATION_UNCONFIRMED,
    }
)

# The validation state that means the checks passed. Every other value — a
# refusal, an unknown, a failure, a cancellation — is not an acceptance path, so
# the state vocabulary is *not* enumerated here: anything that is not this token
# fails closed, and the supplied value is reported verbatim.
STATE_PASSED = "passed"

# The freshness verdict that means the held Memory link still describes current
# Twin authority. Anything else is a loss, and is reported verbatim.
FRESHNESS_CURRENT = "current"

# The two decisions a human may record. Nothing here infers one.
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISIONS = frozenset({DECISION_ACCEPTED, DECISION_REJECTED})

# What the operator declares happened. Kept distinct from what was observed.
APPLICATION_APPLIED = "applied"
APPLICATION_NOT_APPLIED = "not_applied"
APPLICATION_OUTCOMES = frozenset({APPLICATION_APPLIED, APPLICATION_NOT_APPLIED})

# Bounded state reasons. Fixed tokens, never caller content.
REASON_COMPLETE = "acceptance_and_observed_baseline_agree"
REASON_IDENTITY_CONFLICT = "supplied_identities_do_not_agree"
REASON_VALIDATION_NOT_ACCEPTED = "validation_is_not_a_passing_complete_result"
REASON_AWAITING_ACCEPTANCE = "no_explicit_acceptance_has_been_recorded"
REASON_ACCEPTANCE_REFUSED = "the_recorded_decision_is_not_an_acceptance"
REASON_FRESHNESS_LOST = "the_memory_link_no_longer_describes_current_authority"
REASON_APPLICATION_UNCONFIRMED = "declared_outcome_contradicts_the_observed_baseline"

_STATE_REASONS = {
    STATE_COMPLETE: REASON_COMPLETE,
    STATE_IDENTITY_CONFLICT: REASON_IDENTITY_CONFLICT,
    STATE_VALIDATION_NOT_ACCEPTED: REASON_VALIDATION_NOT_ACCEPTED,
    STATE_AWAITING_ACCEPTANCE: REASON_AWAITING_ACCEPTANCE,
    STATE_ACCEPTANCE_REFUSED: REASON_ACCEPTANCE_REFUSED,
    STATE_FRESHNESS_LOST: REASON_FRESHNESS_LOST,
    STATE_APPLICATION_UNCONFIRMED: REASON_APPLICATION_UNCONFIRMED,
}

# Fixed, source-grounded sentences. Each is a fixed phrase; only code-owned
# names are ever interpolated.
_STATE_SENTENCES = {
    STATE_COMPLETE: (
        "every supplied identity agrees, the validation result is a passing and "
        "evidence-complete one, an explicit acceptance is recorded, the Memory "
        "link still describes current authority, and the declared application "
        "outcome matches the baseline observed after the rescan"
    ),
    STATE_IDENTITY_CONFLICT: (
        "the supplied identities do not describe one change: the intent, "
        "proposal, edit, candidate, plan or workspace identities disagree, so "
        "this record would be about more than one thing"
    ),
    STATE_VALIDATION_NOT_ACCEPTED: (
        "the supplied validation result is not a passing, evidence-complete one, "
        "so it cannot support an accepted change"
    ),
    STATE_AWAITING_ACCEPTANCE: (
        "no explicit acceptance has been recorded: nothing here infers one from "
        "a candidate existing, a validation passing or an operator expecting it"
    ),
    STATE_ACCEPTANCE_REFUSED: (
        "the recorded decision is not an acceptance, so the change was not "
        "accepted however complete the rest of the evidence is"
    ),
    STATE_FRESHNESS_LOST: (
        "the Memory link no longer describes current authority, so the evidence "
        "this change was bound to may not describe what was validated"
    ),
    STATE_APPLICATION_UNCONFIRMED: (
        "the declared application outcome contradicts the baseline observed after "
        "the rescan: the declaration is the operator's and the observation is "
        "independent, and they do not agree"
    ),
}

# Bounded refusal reasons: the evidence could not be bound.
REASON_EVIDENCE_NOT_MAPPING = "reconciliation evidence is not a mapping"
REASON_GROUP_MISSING = "a required evidence group is missing or malformed: %s"
REASON_IDENTITY_INVALID = "an identity is missing or malformed: %s"
REASON_ACCEPTANCE_MALFORMED = "the acceptance is not a mapping"
REASON_DECISION_UNKNOWN = "the acceptance decision is not recognised"
REASON_APPLICATION_UNKNOWN = "the declared application outcome is not recognised"

# The groups this record binds, and the fields each must carry. The acceptance
# group is deliberately *not* in this table: an absent acceptance is a state
# (``awaiting_acceptance``), not a refusal, because everything else can still be
# bound and reported.
_REQUIRED_GROUPS = (
    "work_package",
    "change",
    "validation",
    "application",
    "accepted_revision",
    "observed",
    "freshness",
)

_GROUP_FIELDS = {
    "work_package": ("run_id", "record_id"),
    "change": (
        "intent_delta_id",
        "proposal_id",
        "edit_id",
        "candidate_id",
        "binding_fingerprint",
        "plan_id",
        "policy_version",
    ),
    "validation": (
        "result_id",
        "plan_id",
        "candidate_id",
        "policy_version",
        "state",
        "evidence_complete",
    ),
    "application": ("declared",),
    "accepted_revision": ("workspace_id", "scan_generation", "baseline_fingerprint"),
    "observed": ("workspace_id", "scan_generation", "baseline_fingerprint"),
    "freshness": ("verdict", "entity_id"),
}

# Fields that must be non-empty strings carrying their owner's own prefix, so a
# value from some other contract cannot stand in for one of these.
_IDENTITY_PREFIXES = {
    "run_id": "run:",
    "intent_delta_id": "intent:",
    "proposal_id": "impact:",
    "edit_id": "edit:",
    "candidate_id": "candidate:",
    "binding_fingerprint": "bind:",
    "plan_id": "plan:",
    "result_id": "result:",
}

# Fields that must be non-empty strings without a prefix claim of their own.
_PLAIN_STRINGS = (
    "record_id",
    "policy_version",
    "state",
    "verdict",
    "entity_id",
)

# The workspace identity carries its own prefix and a digest; a baseline
# fingerprint is a bare digest. Both are shaped, because either can be handed
# over as a plausible-looking substring.
_WORKSPACE_PREFIX = "ws:"

# Identities that appear in more than one group. They are the cross-bindings
# this record exists to check: the same value must appear in every group that
# claims it, or the evidence is about more than one change. A disagreement is a
# *state* (``identity_conflict``), not a refusal: every value bound, and the
# answer is that they do not describe one change.
_CROSS_BINDINGS = (
    ("candidate_id", ("change", "validation")),
    ("plan_id", ("change", "validation")),
    ("policy_version", ("change", "validation")),
)

_HEX64_LENGTH = 64

# The whole surface this record declares false. It states what it did not do.
_MUTATION_SURFACE_KEYS = (
    "repository_source",
    "accepted_baseline",
    "candidate",
    "git_state",
    "validation_run",
    "provider_request",
    "credential",
    "remote",
)

# Fixed statements of what this record cannot tell a reader.
_LIMITATIONS = (
    "this record attests supplied identities and supplied verdicts; it re-derives "
    "neither, so only the adapter that built it can show where they came from",
    "the declared application outcome is the operator's word; only the observed "
    "post-rescan baseline is independent of it",
    "no validation run was performed for this record: its validation facts are "
    "supplied, and container-gated evidence is not claimed here",
    "the record applies nothing: this product never writes accepted repository "
    "source, so an applied change is always reported, never performed",
)


def dumps(obj: Any) -> str:
    """Serialize a record canonically (sorted keys, compact, ASCII-safe)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _mutation_surface() -> Dict[str, bool]:
    return {key: False for key in _MUTATION_SURFACE_KEYS}


def _is_hex64(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != _HEX64_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _generation(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def _bind_identities(evidence: Dict[str, Any]) -> Optional[str]:
    """Return a bounded reason when any identity cannot be bound, else None."""
    for group, fields in sorted(_GROUP_FIELDS.items()):
        record = evidence[group]
        for field in fields:
            value = record.get(field)
            prefix = _IDENTITY_PREFIXES.get(field)
            if prefix is not None:
                if not _text(value) or not value.startswith(prefix):
                    return REASON_IDENTITY_INVALID % ("%s.%s" % (group, field))
                continue
            if field in _PLAIN_STRINGS:
                if not _text(value):
                    return REASON_IDENTITY_INVALID % ("%s.%s" % (group, field))
                continue
            if field == "baseline_fingerprint":
                if not _is_hex64(value):
                    return REASON_IDENTITY_INVALID % ("%s.%s" % (group, field))
                continue
            if field == "workspace_id":
                if (
                    not _text(value)
                    or not value.startswith(_WORKSPACE_PREFIX)
                    or not _is_hex64(value[len(_WORKSPACE_PREFIX):])
                ):
                    return REASON_IDENTITY_INVALID % ("%s.%s" % (group, field))
                continue
            if field == "scan_generation":
                if not _generation(value):
                    return REASON_IDENTITY_INVALID % ("%s.%s" % (group, field))
                continue
            if field == "evidence_complete":
                if not isinstance(value, bool):
                    return REASON_IDENTITY_INVALID % ("%s.%s" % (group, field))
                continue
    return None


def _identities_agree(evidence: Dict[str, Any]) -> bool:
    """Return whether every supplied identity describes one change.

    A value that appears in more than one group must carry the same value in
    each, and an observation must be of the workspace the change was accepted
    against — otherwise the record would be about more than one thing.
    """
    for field, groups in _CROSS_BINDINGS:
        if len({evidence[group][field] for group in groups}) != 1:
            return False
    acceptance = evidence.get("acceptance")
    if acceptance is not None:
        if acceptance["candidate_id"] != evidence["change"]["candidate_id"]:
            return False
    return (
        evidence["observed"]["workspace_id"]
        == evidence["accepted_revision"]["workspace_id"]
    )


def _state_for(evidence: Dict[str, Any]) -> str:
    """Return the terminal state, in the documented precedence order."""
    if not _identities_agree(evidence):
        return STATE_IDENTITY_CONFLICT
    validation = evidence["validation"]
    if validation["state"] != STATE_PASSED or validation["evidence_complete"] is not True:
        return STATE_VALIDATION_NOT_ACCEPTED
    acceptance = evidence.get("acceptance")
    if acceptance is None:
        return STATE_AWAITING_ACCEPTANCE
    if acceptance["decision"] != DECISION_ACCEPTED:
        return STATE_ACCEPTANCE_REFUSED
    if evidence["freshness"]["verdict"] != FRESHNESS_CURRENT:
        return STATE_FRESHNESS_LOST
    if not _application_is_confirmed(evidence):
        return STATE_APPLICATION_UNCONFIRMED
    return STATE_COMPLETE


def _application_is_confirmed(evidence: Dict[str, Any]) -> bool:
    """Return whether the declared outcome agrees with the observed baseline.

    The baseline fingerprint is the decisive observation: it captures which
    supported files exist and what content each holds, so it moves exactly when
    the accepted source moved. A declaration that the change was applied beside
    an unchanged fingerprint — or that it was not applied beside a changed one —
    is a contradiction, and this record reports it rather than resolving it.
    """
    moved = (
        evidence["observed"]["baseline_fingerprint"]
        != evidence["accepted_revision"]["baseline_fingerprint"]
    )
    declared = evidence["application"]["declared"]
    if declared == APPLICATION_APPLIED:
        return moved
    return not moved


def reconciliation_id_for(record: Dict[str, Any]) -> str:
    """Return the content-addressed identity of a record (never time-derived)."""
    canon = dumps({key: value for key, value in record.items() if key != "reconcile_id"})
    return RECONCILIATION_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def build_reconciliation(
    evidence: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Derive the controlled-change reconciliation record, or refuse a binding.

    ``evidence`` carries eight groups: ``work_package``, ``change``,
    ``validation``, ``acceptance``, ``application``, ``accepted_revision``,
    ``observed`` and ``freshness``. Returns ``(record, error)``: a refusal is
    ``(None, bounded reason)``, and a bound analysis is ``(record, None)`` with a
    terminal ``state``.
    """
    if not isinstance(evidence, dict):
        return None, REASON_EVIDENCE_NOT_MAPPING
    for group in _REQUIRED_GROUPS:
        if not isinstance(evidence.get(group), dict):
            return None, REASON_GROUP_MISSING % group

    acceptance = evidence.get("acceptance")
    if acceptance is not None:
        if not isinstance(acceptance, dict):
            return None, REASON_ACCEPTANCE_MALFORMED
        for field in ("actor", "decision", "decided_at"):
            if not _text(acceptance.get(field)):
                return None, REASON_IDENTITY_INVALID % ("acceptance.%s" % field)
        candidate = acceptance.get("candidate_id")
        if not _text(candidate) or not candidate.startswith(
            _IDENTITY_PREFIXES["candidate_id"]
        ):
            return None, REASON_IDENTITY_INVALID % "acceptance.candidate_id"
        if acceptance["decision"] not in DECISIONS:
            return None, REASON_DECISION_UNKNOWN

    if evidence["application"]["declared"] not in APPLICATION_OUTCOMES:
        return None, REASON_APPLICATION_UNKNOWN

    reason = _bind_identities(evidence)
    if reason is not None:
        return None, reason

    state = _state_for(evidence)
    record: Dict[str, Any] = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "generator": RECONCILIATION_GENERATOR,
        "reconcile_id": "",
        "state": state,
        "reason_code": _STATE_REASONS[state],
        "reason": _STATE_SENTENCES[state],
        "complete": state == STATE_COMPLETE,
        # This artifact reports on a decision. It takes none, and it performs
        # nothing whatever the state says.
        "approved": False,
        "applied": False,
        "execution_performed": False,
        "work_package": dict(evidence["work_package"]),
        "change": dict(evidence["change"]),
        "validation": dict(evidence["validation"]),
        "acceptance": dict(acceptance) if acceptance is not None else None,
        "application": dict(evidence["application"]),
        "accepted_revision": dict(evidence["accepted_revision"]),
        "observed": dict(evidence["observed"]),
        "freshness": dict(evidence["freshness"]),
        "baseline_moved": (
            evidence["observed"]["baseline_fingerprint"]
            != evidence["accepted_revision"]["baseline_fingerprint"]
        ),
        "mutation_surface": _mutation_surface(),
        "limitations": list(_LIMITATIONS),
    }
    record["reconcile_id"] = reconciliation_id_for(record)
    return record, None


def validate_reconciliation(record: Any) -> Optional[str]:
    """Validate a record against the P5-E2 schema; return a reason or ``None``.

    A valid record declares the current schema and generator, carries a
    ``reconcile:``-prefixed id that matches its own content, holds a known state,
    is neither approved nor applied nor executed, and declares every mutation
    surface false.
    """
    if not isinstance(record, dict):
        return "record is not a mapping"
    if record.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        return "unsupported schema_version"
    if record.get("generator") != RECONCILIATION_GENERATOR:
        return "unknown record generator"
    if record.get("state") not in RECONCILIATION_STATES:
        return "unknown reconciliation state"
    if record.get("approved") is not False:
        return "record must not approve"
    if record.get("applied") is not False:
        return "record must not apply"
    if record.get("execution_performed") is not False:
        return "record must not have performed execution"
    if record.get("complete") is not (record.get("state") == STATE_COMPLETE):
        return "complete does not match the terminal state"
    identity = record.get("reconcile_id")
    if not isinstance(identity, str) or not identity.startswith(
        RECONCILIATION_ID_PREFIX
    ):
        return "missing or malformed reconcile_id"
    if identity != reconciliation_id_for(record):
        return "reconcile_id does not match the record content"
    surface = record.get("mutation_surface")
    if not isinstance(surface, dict):
        return "missing or malformed mutation_surface"
    if set(surface) != set(_MUTATION_SURFACE_KEYS):
        return "mutation_surface does not declare every named boundary"
    for key, value in sorted(surface.items()):
        if value is not False:
            return "mutation_surface declares a non-false %s" % key
    if not isinstance(record.get("limitations"), list) or not record["limitations"]:
        return "missing or malformed limitations"
    for group in _REQUIRED_GROUPS:
        if not isinstance(record.get(group), dict):
            return "missing or malformed %s" % group
    return None


__all__ = [
    "RECONCILIATION_SCHEMA_VERSION",
    "RECONCILIATION_GENERATOR",
    "RECONCILIATION_ID_PREFIX",
    "STATE_COMPLETE",
    "STATE_IDENTITY_CONFLICT",
    "STATE_VALIDATION_NOT_ACCEPTED",
    "STATE_AWAITING_ACCEPTANCE",
    "STATE_ACCEPTANCE_REFUSED",
    "STATE_FRESHNESS_LOST",
    "STATE_APPLICATION_UNCONFIRMED",
    "RECONCILIATION_STATES",
    "DECISION_ACCEPTED",
    "DECISION_REJECTED",
    "DECISIONS",
    "APPLICATION_APPLIED",
    "APPLICATION_NOT_APPLIED",
    "APPLICATION_OUTCOMES",
    "REASON_COMPLETE",
    "REASON_IDENTITY_CONFLICT",
    "REASON_VALIDATION_NOT_ACCEPTED",
    "REASON_AWAITING_ACCEPTANCE",
    "REASON_ACCEPTANCE_REFUSED",
    "REASON_FRESHNESS_LOST",
    "REASON_APPLICATION_UNCONFIRMED",
    "REASON_EVIDENCE_NOT_MAPPING",
    "REASON_GROUP_MISSING",
    "REASON_IDENTITY_INVALID",
    "REASON_ACCEPTANCE_MALFORMED",
    "REASON_DECISION_UNKNOWN",
    "REASON_APPLICATION_UNKNOWN",
    "dumps",
    "build_reconciliation",
    "validate_reconciliation",
    "reconciliation_id_for",
]
