"""Source application coordinator (P5-X A2).

The missing step in the Phase 5 chain, and nothing else. The product can bind a
typed intent, propose an impact, materialise a candidate and its diff, record
validation evidence, reconcile a controlled change — but every one of those
reports on a change and none of them *performs* it, so the human was left to
assemble pre-image, temporary bytes, hash checks and an atomic rename by hand.
This module owns that one step.

Two operations, deliberately separate
-------------------------------------

:func:`plan_application` binds the evidence and revalidates it against the live
apply root, and returns ``ready_for_approval`` when every **non-approval** fact
is exact. It takes no receipt and **no custody base**, so it has no argument
through which it could write a file even by mistake, and an absent, malformed or
stale receipt cannot influence a plan verdict.

:func:`apply_application` takes a receipt and a custody base, and **re-runs the
entire plan sequence** rather than trusting a prior verdict: a caller cannot
hand it a computed ``ready_for_approval`` and have it believed. Only then does
it write, and what it may write is exactly four things — one coordinator-created
pre-image, one same-directory temporary file, one atomic replacement of the one
bound target, and one bounded record.

Refusal versus state
--------------------

The split this package uses everywhere. A **refusal** is ``(None, reason)``: the
input cannot be *bound* — evidence that is not a mapping, a missing or malformed
group, an identity without its own prefix, an unknown decision token, an
unusable custody base, a target path the grammar rejects, a path the bound
proposal does not authorize, a symlinked component, a non-regular or hardlinked
or oversized or non-UTF-8 target, a candidate root that does not re-read to its
own manifest. A **state** is a record: everything bound, and the answer is a
determination. An absent approval is a state, not a refusal — ``work_reconciliation``
already establishes that rule for an absent acceptance, "because everything else
can still be bound and reported" — and so is a stale world.

The reason code carries the detail the state name cannot: every non-success
returned here is accompanied by a bounded token naming *which* fact disagreed,
so ``refused_stale_or_mismatched`` never has to be read as a shrug.

One consequence is stated rather than left to inference: a validation result
that is not ``passed`` and evidence-complete is reported as
``refused_stale_or_mismatched`` with a reason code naming it exactly, rather
than as a state of its own. The state vocabulary here is fixed and small, and
the evidence genuinely does not match what an application requires.

Root identity is derived, never asserted
---------------------------------------

The apply root is not trusted because a caller supplied it. It is canonicalized
with the accepted ``realpath``/``abspath`` semantics and its workspace identity
is **derived** with :func:`hrca.core.identity.workspace_id_for`, then required to
equal ``accepted_revision.workspace_id`` byte for byte. Because that identity is
the SHA-256 of the canonical root string, a different directory holding the same
relative ``greeting.py`` has a different identity and is refused before any
other check — and the relative path plays no part in the root identity at all.

What this module can never do
-----------------------------

It executes nothing: no process, shell, command, provider, network, credential,
Docker, Git, scan, Twin, Memory, validation run, protocol action or UI action.
Its import closure is the standard library plus ``core.identity``,
``candidate_edit`` and ``candidate`` — all pure leaves — so no dispatching,
storing or networked capability is present even transitively. It is not a
runner, a launcher or a command framework, and it takes no argv: the only
authority it holds is the four writes named above.

The owner's verdicts are supplied, not re-derived
------------------------------------------------

This module does not import ``validation``. It cannot: that module owns the
container-gated runner, and the package's seam is that a pure ``authoring``
composition binds verdicts while the *adapter* invokes the owners' gates — the
rule ``work_reconciliation`` states for itself, and the reason
``work_reconciliation_cli`` exists. So the candidate's verified *binding* is
supplied with the evidence, produced by the adapter calling the owner's
verifier.

What this module does instead is hold that binding to the facts it can check
itself: it re-validates the manifest against the P5.4 schema, re-derives the
candidate identity, requires the root's name to be the one that identity owns,
requires the root to hold exactly the manifest and the files directory, and
**re-reads the candidate bytes under its own guarded read**, requiring their
hash to match the binding, the manifest, the bound edit's replacement text and
the bound target. The bytes that reach the write are therefore pinned by this
module's own read; what it does not independently reproduce is the owner's
review-envelope verdict, and its limitations say so.

What a record proves, and what it does not
------------------------------------------

The receipt is gated on two shape facts before anything else in it is read: it
must declare the supported receipt schema version **exactly**, and its decision
timestamp must be a strict RFC 3339 date-time with an explicit zone. Neither gate
authenticates anyone. They make the receipt's *meaning* checkable, so a version
nobody understands is never read as though it were this one, and a decision is
always placeable on a timeline. The value supplied is the value bound — nothing
is normalized, defaulted or migrated — and the calendar is checked
arithmetically rather than by the interpreter's own date parser, so the gate does
not change behaviour with the Python version.

The receipt is **client-declared provenance**. Its digest makes it tamper-evident
after the fact and bound to this one application; it does **not** authenticate
the author, prove a human wrote it, or resist any process running as the same
user — which is how this module itself runs. No signing model is implemented or
implied, and the record says so in its own limitations.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from typing import Any, Dict, Optional, Tuple

from . import candidate
from . import candidate_edit
from ..core.identity import sha256_hex, workspace_id_for

SCHEMA_VERSION = "1.0.0"
GENERATOR = "hrca-source-apply"
APPLY_ID_PREFIX = "apply:"
RECEIPT_SCHEMA_VERSION = "1.0.0"

# The two fixed subdirectories of a caller-supplied custody base. A base is
# never written to directly, which is the rule ``work_reconciliation_cli``
# already uses for its own records.
RECORD_DIRNAME = "apply-records"
RECOVERY_DIRNAME = "source-recovery"

# The staging prefix every temporary this module creates must carry, so a
# cleanup can only ever reach a file this module made.
TEMP_PREFIX = ".hrca-source-apply-"

# Bounds. The target bound is read from its owner so the limit stays that
# contract's own.
MAX_TARGET_BYTES = candidate_edit.MAX_FILE_BYTES
MAX_RECEIPT_BYTES = 8 * 1024
MAX_RECORD_BYTES = 256 * 1024

MODE_PLAN = "plan"
MODE_APPLY = "apply"
MODES = frozenset({MODE_PLAN, MODE_APPLY})

# The terminal states. ``recovery_failed`` is declared but unreachable from
# :func:`apply_application`: that operation performs no recovery, so it can
# never restore and can never fail to restore. The state belongs to a future,
# separately authorized recovery operation.
STATE_READY = "ready_for_approval"
STATE_REFUSED_MISSING_APPROVAL = "refused_missing_approval"
STATE_REFUSED_STALE = "refused_stale_or_mismatched"
STATE_APPLIED = "applied_observed_bytes"
STATE_NOT_EFFECTIVE = "application_not_effective"
STATE_UNKNOWN = "application_unknown"
STATE_RECOVERY_REQUIRED = "recovery_required"
STATE_RECOVERY_FAILED = "recovery_failed"
APPLY_STATES = frozenset(
    {
        STATE_READY,
        STATE_REFUSED_MISSING_APPROVAL,
        STATE_REFUSED_STALE,
        STATE_APPLIED,
        STATE_NOT_EFFECTIVE,
        STATE_UNKNOWN,
        STATE_RECOVERY_REQUIRED,
        STATE_RECOVERY_FAILED,
    }
)

# The states that must never be preceded by a write of any kind. A record in
# one of these declares the whole write surface false, and the validator holds
# it there.
_PRE_WRITE_STATES = frozenset(
    {STATE_READY, STATE_REFUSED_MISSING_APPROVAL, STATE_REFUSED_STALE}
)

# The two decisions a receipt may record. Nothing here infers one.
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISIONS = frozenset({DECISION_ACCEPTED, DECISION_REJECTED})

# What the observation found. Derived from the bytes on disk, never supplied.
OUTCOME_PREDECESSOR = "predecessor"
OUTCOME_CANDIDATE = "candidate"
OUTCOME_NEITHER = "neither"

# The fixed sentence each state is reported with. Only code-owned names are ever
# interpolated, and a reason *code* always accompanies it to name the fact.
REASON_READY = "every_non_approval_fact_is_exact"
REASON_REFUSED_MISSING_APPROVAL = "no_accepted_receipt_was_supplied"
REASON_REFUSED_STALE = "live_or_bound_evidence_disagrees"
REASON_APPLIED = "observed_bytes_equal_the_approved_candidate"
REASON_NOT_EFFECTIVE = (
    "a_write_was_attempted_and_observed_bytes_equal_the_predecessor"
)
REASON_UNKNOWN = "observed_bytes_equal_neither_the_predecessor_nor_the_candidate"
REASON_RECOVERY_REQUIRED = (
    "observed_bytes_equal_neither_and_a_verified_pre_image_is_available"
)

# Specific stale codes: which fact disagreed. The state is one of a fixed few;
# these are bounded tokens, never caller content.
STALE_ROOT_IDENTITY = "the_apply_root_is_not_the_accepted_workspace"
STALE_IDENTITY_AGREEMENT = "the_supplied_identities_do_not_describe_one_change"
STALE_EDIT_BINDING = "the_bound_edit_does_not_agree_with_the_change"
STALE_PREDECESSOR = "the_live_predecessor_is_not_the_expected_one"
STALE_CANDIDATE = "the_candidate_bytes_do_not_agree_between_their_two_sources"
STALE_BASELINE = "the_bound_baseline_is_not_the_accepted_revision"
STALE_VALIDATION = "the_validation_result_is_not_a_passing_complete_one"
STALE_RECEIPT = "the_receipt_does_not_authorize_these_exact_facts"

# Bounded refusal reasons. Fixed tokens, never caller content.
REASON_EVIDENCE_NOT_MAPPING = "apply evidence is not a mapping"
REASON_GROUP_MISSING = "a required evidence group is missing or malformed: %s"
REASON_IDENTITY_INVALID = "an identity is missing or malformed: %s"
REASON_MODE_UNKNOWN = "the requested mode is not recognised"
REASON_RECEIPT_NOT_MAPPING = "the approval receipt is not a mapping"
REASON_RECEIPT_INVALID = "the approval receipt is missing or malformed: %s"
REASON_RECEIPT_VERSION_UNSUPPORTED = (
    "the approval receipt does not declare the supported schema version"
)
REASON_RECEIPT_TIMESTAMP_INVALID = (
    "the decision timestamp is not an RFC 3339 date-time with an explicit zone"
)
REASON_DECISION_UNKNOWN = "the receipt decision is not recognised"
REASON_EDIT_UNSOUND = "the bound edit request is not a valid one"
REASON_EDIT_MISSING = "the bound edit request carries no replacement operation"
REASON_ROOT_UNUSABLE = "the apply root is not a usable directory"
REASON_ROOT_INSIDE_GIT = "the apply root is inside a Git working tree"
REASON_CUSTODY_UNUSABLE = "the custody base is not a usable directory"
REASON_CUSTODY_INSIDE_TARGET = "the custody base is inside the apply root"
REASON_TARGET_NOT_AUTHORIZED = "the bound proposal does not authorize that exact path"
REASON_TARGET_ABSENT = "the target file is absent"
REASON_TARGET_NOT_REGULAR = "the target is not a regular file"
REASON_TARGET_SYMLINK = "the target is or passes through a symbolic link"
REASON_TARGET_HARDLINK = "the target has more than one hard link"
REASON_TARGET_ESCAPES = "the target resolves outside the apply root"
REASON_TARGET_UNREADABLE = "the target could not be read"
REASON_TARGET_OVERSIZED = "the target exceeds the accepted size bound"
REASON_TARGET_NOT_UTF8 = "the target is not UTF-8 text"
REASON_TARGET_UNREVIEWABLE = "the target is not reviewable text"
REASON_CANDIDATE_UNREADABLE = "the candidate could not be re-read under its identity"
REASON_RECOVERY_PATH_TAKEN = "the recovery path already exists"
REASON_APPLY_ALREADY_RECORDED = "this application has already been recorded"
REASON_RECORD_TAMPERED = "the recorded apply evidence does not match its identity"
REASON_CUSTODY_UNWRITABLE = "the custody base could not be written"
REASON_PREIMAGE_FAILED = "the pre-image could not be written"
REASON_PREIMAGE_STALE = "the pre-image does not hold the expected predecessor"
REASON_TEMP_FAILED = "the temporary file could not be written"
REASON_TEMP_STALE = "the temporary file does not hold the expected candidate"
REASON_REPLACE_FAILED = "the atomic replacement failed"

# The groups this contract binds. ``acceptance`` is deliberately absent: it is
# supplied separately by :func:`apply_application` and is not bound by the plan.
_REQUIRED_GROUPS = (
    "work_package",
    "change",
    "validation",
    "accepted_revision",
    "target",
    "candidate",
    "edit",
    "proposal",
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
    "accepted_revision": ("workspace_id", "scan_generation", "baseline_fingerprint"),
    "target": (
        "path",
        "predecessor_sha256",
        "candidate_sha256",
        "manifest_sha256",
        "review_sha256",
    ),
    "candidate": ("root", "manifest", "review", "binding"),
    "edit": (),
    "proposal": (),
}

# Fields that must carry their owner's own prefix, so a value from some other
# contract cannot stand in for one of these.
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

_PLAIN_STRINGS = ("record_id", "policy_version", "state")
_MAPPING_FIELDS = ("manifest", "review", "binding")
_HEX_FIELDS = ("predecessor_sha256", "candidate_sha256", "manifest_sha256",
               "review_sha256")

_WORKSPACE_PREFIX = "ws:"
_VALIDATION_PASSED = "passed"
_HEX64_LENGTH = 64

# The receipt fields, and the exact shape each must have. Every one of them is
# an identity this application is bound to; a receipt that agrees with none of
# them authorizes nothing.
_RECEIPT_FIELDS = (
    "actor",
    "decision",
    "decided_at",
    "candidate_id",
    "target_path",
    "predecessor_sha256",
    "candidate_sha256",
    "manifest_sha256",
    "review_sha256",
    "binding_fingerprint",
    "workspace_id",
    "scan_generation",
    "baseline_fingerprint",
    "validation_result_id",
)
_RECEIPT_HEX_FIELDS = (
    "predecessor_sha256",
    "candidate_sha256",
    "manifest_sha256",
    "review_sha256",
    "baseline_fingerprint",
)

# What this record declares about the world. Everything here is always false:
# this module has no provider, credential, network, container, Git, Twin,
# Memory, validation-run, scan, protocol or UI authority, launches no process,
# and the block exists so a reviewer, a test and a later phase can assert the
# whole surface at once.
_MUTATION_SURFACE_KEYS = (
    "accepted_repository",
    "git_index",
    "git_ref",
    "branch",
    "commit",
    "worktree",
    "provider_request",
    "credential",
    "network",
    "remote",
    "docker",
    "twin_state",
    "memory",
    "validation_run",
    "scan",
    "protocol_action",
    "ui",
    "approval_inferred",
    "process_launched",
)

# The write surface, declared per record: what this particular run wrote.
_WRITE_KEYS = (
    "pre_image_written",
    "temporary_written",
    "target_written",
    "record_written",
)

_LIMITATIONS = (
    "the receipt is client-declared provenance: its digest makes it "
    "tamper-evident and bound to this application, but it does not authenticate "
    "the author, prove a human wrote it, or resist any process running as the "
    "same user",
    "this record observes one file's bytes immediately after a replacement; it "
    "does not rescan, so it derives no baseline fingerprint and makes no "
    "freshness, reconciliation or accepted-state claim",
    "a passing validation result is evidence and never approval: the receipt is "
    "a separate object and no combination of validation facts stands in for it",
    "the pre-image is the only recovery artifact this operation creates, and it "
    "takes no recovery action: restoring the predecessor requires a separately "
    "authorized operation",
    "the candidate binding was produced by the adapter that invoked the owner's "
    "verifier: this module re-reads the candidate bytes and re-derives the "
    "identity, the root name and the manifest agreement, but it does not "
    "independently reproduce the owner's review-envelope verdict",
)


class _Refusal(Exception):
    """Internal: the input cannot be bound. Carries a bounded reason."""


class _Stale(Exception):
    """Internal: everything bound, and the live or bound evidence disagrees."""


def dumps(obj: Any) -> str:
    """Serialize a record canonically (sorted keys, compact, ASCII-safe)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _mutation_surface() -> Dict[str, bool]:
    return {key: False for key in _MUTATION_SURFACE_KEYS}


def _write_surface(
    pre_image: bool = False,
    temporary: bool = False,
    target: bool = False,
    record: bool = False,
) -> Dict[str, bool]:
    return {
        "pre_image_written": pre_image,
        "temporary_written": temporary,
        "target_written": target,
        "record_written": record,
    }


def _is_hex64(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != _HEX64_LENGTH:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _generation(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


# A strict RFC 3339 date-time: four-digit year, month and day, a case-insensitive
# T, a mandatory hour, minute and second, optional fractional seconds, and an
# explicit zone that is either Z or a numeric offset. Nothing here is optional:
# a date alone, a naive local time, or any other shape is not a decision
# timestamp a reviewer can place on a timeline.
_RFC3339_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"([Zz]|[+-]\d{2}:\d{2})$"
)

# Leap years are handled separately, so February is the only length that moves.
_MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_rfc3339(value: Any) -> bool:
    """Return whether a value is a strict RFC 3339 date-time with an explicit zone.

    The calendar is checked arithmetically rather than by the interpreter's own
    date parser, because that parser's strictness has changed between Python
    versions and a receipt gate whose behaviour depends on the interpreter is
    not a gate. Nothing here reads a clock, and nothing normalizes the value:
    the text supplied is the text that is bound, so a caller cannot have one
    timestamp validated and a different one recorded.
    """
    if not isinstance(value, str):
        return False
    match = _RFC3339_PATTERN.match(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = (
        int(group) for group in match.groups()[:6]
    )
    zone = match.group(7)

    if not 1 <= month <= 12:
        return False
    length = _MONTH_LENGTHS[month - 1]
    if month == 2 and year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
        length = 29
    if not 1 <= day <= length:
        return False
    if hour > 23 or minute > 59 or second > 60:
        return False
    if zone not in ("Z", "z"):
        if int(zone[1:3]) > 23 or int(zone[4:6]) > 59:
            return False
    return True


# -- root identity ---------------------------------------------------------


def normalize_root(path: Any) -> Tuple[Optional[str], Optional[str]]:
    """Canonicalize an apply root the way the accepted workspace policy does.

    This restates the rule ``hrca.core.workspace.resolve_root`` implements —
    reject a blank or non-string path, then ``realpath(abspath(path))``, then
    require a directory — as a bounded ``(canonical, reason)`` pair. It is
    restated rather than imported because the workspace policy is boundary-side
    and no ``authoring`` module imports it; the focused tests hold the two
    implementations to identical output over a shared matrix of inputs.
    """
    if not isinstance(path, str) or not path.strip():
        return None, REASON_ROOT_UNUSABLE
    canonical = os.path.realpath(os.path.abspath(path))
    if not os.path.isdir(canonical):
        return None, REASON_ROOT_UNUSABLE
    return canonical, None


def _inside_git_tree(canonical: str) -> bool:
    """Return whether a canonical root is, or is inside, a Git working tree.

    A path-only rule: no repository location is hardcoded, and ``.git`` as
    either a component of the root or an entry directly beneath it excludes it.
    """
    parts = canonical.replace(os.sep, "/").split("/")
    if ".git" in parts:
        return True
    return os.path.isdir(os.path.join(canonical, ".git"))


def _contained(root_real: str, target_real: str) -> bool:
    """Return whether ``target_real`` lies strictly beneath ``root_real``."""
    return target_real.startswith(root_real + os.sep)


# -- binding ---------------------------------------------------------------


def _require_groups(evidence: Any) -> Dict[str, Any]:
    if not isinstance(evidence, dict):
        raise _Refusal(REASON_EVIDENCE_NOT_MAPPING)
    for group in _REQUIRED_GROUPS:
        if not isinstance(evidence.get(group), dict):
            raise _Refusal(REASON_GROUP_MISSING % group)
    return evidence


def _bind_identity_shapes(evidence: Dict[str, Any]) -> None:
    """Refuse any identity that is absent or does not carry its own prefix."""
    for group, fields in sorted(_GROUP_FIELDS.items()):
        record = evidence[group]
        for field in fields:
            value = record.get(field)
            if field in _MAPPING_FIELDS:
                if not isinstance(value, dict):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field == "root":
                if not _text(value):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            prefix = _IDENTITY_PREFIXES.get(field)
            if prefix is not None:
                if not _text(value) or not value.startswith(prefix):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field in _PLAIN_STRINGS:
                if not _text(value):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field in _HEX_FIELDS:
                if not _is_hex64(value):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field == "baseline_fingerprint":
                if not _is_hex64(value):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field == "workspace_id":
                if (
                    not _text(value)
                    or not value.startswith(_WORKSPACE_PREFIX)
                    or not _is_hex64(value[len(_WORKSPACE_PREFIX):])
                ):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field == "scan_generation":
                if not _generation(value):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue
            if field == "evidence_complete":
                if not isinstance(value, bool):
                    raise _Refusal(REASON_IDENTITY_INVALID % ("%s.%s" % (group, field)))
                continue


def _bound_edit(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Return the bound edit's replacement operation, or refuse it.

    The owner's version gate and its content-addressed identity are invoked
    rather than re-implemented, so an edit this contract does not understand, or
    one whose id does not match its own content, is refused by the module that
    owns the schema.
    """
    edit = evidence["edit"]
    migrated, reason = candidate_edit.migrate_edit(edit)
    if reason is not None:
        raise _Refusal(REASON_EDIT_UNSOUND)
    if migrated.get("edit_id") != candidate_edit.edit_id_for(migrated):
        raise _Refusal(REASON_EDIT_UNSOUND)
    operations = migrated.get("operations")
    if not isinstance(operations, list) or not operations:
        raise _Refusal(REASON_EDIT_MISSING)
    operation = operations[0]
    if not isinstance(operation, dict):
        raise _Refusal(REASON_EDIT_MISSING)
    for field in ("path", "expected_sha256", "text"):
        if not _text(operation.get(field)):
            raise _Refusal(REASON_EDIT_MISSING)
    if operation["path"] != evidence["target"]["path"]:
        raise _Stale(STALE_EDIT_BINDING)
    if migrated.get("binding_fingerprint") != evidence["change"]["binding_fingerprint"]:
        raise _Stale(STALE_EDIT_BINDING)
    baseline = migrated.get("baseline") or {}
    if baseline.get("workspace_id") != evidence["accepted_revision"]["workspace_id"]:
        raise _Stale(STALE_EDIT_BINDING)
    return operation


def _authorized_path(evidence: Dict[str, Any], path: str) -> None:
    """Refuse a path the bound proposal does not both scope and carry as evidence."""
    authorized = candidate.authorized_paths(evidence["proposal"])
    entry = authorized.get(path)
    if not entry or not entry.get("in_scope") or not entry.get("in_evidence"):
        raise _Refusal(REASON_TARGET_NOT_AUTHORIZED)


def _read_target(root_real: str, path: str) -> bytes:
    """Read the target under a checked identity, or refuse it.

    Every check is fail-closed: a link anywhere in the chain, a special file, a
    second hard link, an escape from the root, an unreadable file, an oversized
    file or content that is not reviewable UTF-8 text is a refusal, never a
    best-effort read.
    """
    components = path.split("/")
    for index in range(len(components)):
        prefix = os.path.join(root_real, *components[: index + 1])
        if os.path.islink(prefix):
            raise _Refusal(REASON_TARGET_SYMLINK)

    target = os.path.join(root_real, *components)
    real = os.path.realpath(target)
    if not _contained(root_real, real):
        raise _Refusal(REASON_TARGET_ESCAPES)

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        raise _Refusal(REASON_TARGET_ABSENT)
    except OSError:
        raise _Refusal(REASON_TARGET_UNREADABLE)

    if stat.S_ISLNK(info.st_mode):
        raise _Refusal(REASON_TARGET_SYMLINK)
    if not stat.S_ISREG(info.st_mode):
        raise _Refusal(REASON_TARGET_NOT_REGULAR)
    if info.st_nlink != 1:
        raise _Refusal(REASON_TARGET_HARDLINK)
    if info.st_size > MAX_TARGET_BYTES:
        raise _Refusal(REASON_TARGET_OVERSIZED)

    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_TARGET_BYTES + 1)
    except OSError:
        raise _Refusal(REASON_TARGET_UNREADABLE)
    if len(data) > MAX_TARGET_BYTES:
        raise _Refusal(REASON_TARGET_OVERSIZED)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refusal(REASON_TARGET_NOT_UTF8)
    if not candidate.reviewable_text(text):
        raise _Refusal(REASON_TARGET_UNREVIEWABLE)
    return data


def _read_candidate_file(path: str) -> bytes:
    """Read one candidate file under a checked identity, or refuse it."""
    try:
        info = os.lstat(path)
    except OSError:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if info.st_nlink != 1:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if info.st_size > MAX_TARGET_BYTES:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_TARGET_BYTES + 1)
    except OSError:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if len(data) > MAX_TARGET_BYTES:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    return data


def _candidate(evidence: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any], Dict[str, Any]]:
    """Re-read the isolated candidate and hold the owner's binding to it.

    The binding is the verdict of the owner's verifier, produced by the adapter
    that assembled this evidence; this function does not re-derive it — it
    cannot, without importing the module that owns the container-gated runner.
    What it does instead is check every property it can check for itself and
    re-read the bytes, so the value that reaches the write is pinned by this
    module's own read rather than by the binding it was handed.
    """
    group = evidence["candidate"]
    root = group["root"]
    manifest = group["manifest"]
    binding = group["binding"]
    if not os.path.isdir(root):
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if candidate.validate_candidate_manifest(manifest) is not None:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    identity = candidate.candidate_id_for(manifest)
    if manifest.get("candidate_id") != identity:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if os.path.basename(os.path.normpath(root)) != candidate.candidate_root_name(identity):
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if sorted(os.listdir(root)) != sorted([candidate.MANIFEST_NAME, candidate.FILES_DIR]):
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    if binding.get("candidate_id") != identity:
        raise _Stale(STALE_IDENTITY_AGREEMENT)

    files = binding.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise _Refusal(REASON_CANDIDATE_UNREADABLE)
    entry = files[0]
    if not isinstance(entry, dict) or entry.get("path") != evidence["target"]["path"]:
        raise _Stale(STALE_IDENTITY_AGREEMENT)
    declared = {record["path"]: record for record in manifest["files"]}
    if declared.get(entry["path"], {}).get("sha256") != entry.get("sha256"):
        raise _Stale(STALE_IDENTITY_AGREEMENT)

    full = os.path.join(root, candidate.FILES_DIR, *entry["path"].split("/"))
    data = _read_candidate_file(full)
    if sha256_hex(data) != entry.get("sha256"):
        raise _Stale(STALE_CANDIDATE)
    return data, manifest, binding


def _live_facts(evidence: Any, apply_root: Any) -> Dict[str, Any]:
    """Bind and revalidate every non-approval fact against the live root.

    Raises :class:`_Refusal` when the input cannot be bound and :class:`_Stale`
    when it binds but disagrees with the bound or live facts.
    """
    evidence = _require_groups(evidence)
    _bind_identity_shapes(evidence)

    change = evidence["change"]
    validation_group = evidence["validation"]
    accepted = evidence["accepted_revision"]
    target_group = evidence["target"]

    for field, groups in (
        ("candidate_id", ("change", "validation")),
        ("plan_id", ("change", "validation")),
        ("policy_version", ("change", "validation")),
    ):
        if len({evidence[group][field] for group in groups}) != 1:
            raise _Stale(STALE_IDENTITY_AGREEMENT)

    operation = _bound_edit(evidence)
    path = operation["path"]

    canonical, reason = normalize_root(apply_root)
    if reason is not None:
        raise _Refusal(reason)
    if _inside_git_tree(canonical):
        raise _Refusal(REASON_ROOT_INSIDE_GIT)
    if workspace_id_for(canonical) != accepted["workspace_id"]:
        raise _Stale(STALE_ROOT_IDENTITY)

    normalized, reason = candidate_edit.normalize_edit_path(path)
    if reason is not None or normalized != path:
        raise _Refusal(reason or candidate_edit.REASON_PATH_INVALID)
    _authorized_path(evidence, path)

    predecessor = sha256_hex(_read_target(canonical, path))
    if predecessor != operation["expected_sha256"]:
        raise _Stale(STALE_PREDECESSOR)
    if predecessor != target_group["predecessor_sha256"]:
        raise _Stale(STALE_PREDECESSOR)

    candidate_bytes, manifest, binding = _candidate(evidence)
    if change["candidate_id"] != manifest.get("candidate_id"):
        raise _Stale(STALE_IDENTITY_AGREEMENT)
    for field in ("proposal_id", "edit_id", "binding_fingerprint"):
        if change[field] != manifest.get(field):
            raise _Stale(STALE_IDENTITY_AGREEMENT)

    candidate_sha = sha256_hex(candidate_bytes)
    if candidate_sha != sha256_hex(operation["text"].encode("utf-8")):
        raise _Stale(STALE_CANDIDATE)
    if candidate_sha != target_group["candidate_sha256"]:
        raise _Stale(STALE_CANDIDATE)

    if binding["manifest_sha256"] != target_group["manifest_sha256"]:
        raise _Stale(STALE_IDENTITY_AGREEMENT)
    if binding["review_sha256"] != target_group["review_sha256"]:
        raise _Stale(STALE_IDENTITY_AGREEMENT)
    for field in ("binding_fingerprint", "edit_id", "proposal_id"):
        if binding[field] != change[field]:
            raise _Stale(STALE_IDENTITY_AGREEMENT)

    baseline = binding.get("baseline") or {}
    for field in ("workspace_id", "scan_generation", "baseline_fingerprint"):
        if baseline.get(field) != accepted[field]:
            raise _Stale(STALE_BASELINE)

    if validation_group["state"] != _VALIDATION_PASSED:
        raise _Stale(STALE_VALIDATION)
    if validation_group["evidence_complete"] is not True:
        raise _Stale(STALE_VALIDATION)

    return {
        "path": path,
        "canonical_root": canonical,
        "predecessor_sha256": predecessor,
        "candidate_sha256": candidate_sha,
        "candidate_bytes": candidate_bytes,
    }


# -- receipt ---------------------------------------------------------------


def _bind_receipt(receipt: Any) -> Dict[str, Any]:
    """Bind the receipt's shape, or refuse it. Presence is the caller's business."""
    if not isinstance(receipt, dict):
        raise _Refusal(REASON_RECEIPT_NOT_MAPPING)

    # The version gate comes first, before any other field is interpreted. An
    # absent, non-string, blank, older or future version is refused rather than
    # assumed to mean this contract: a receipt is read by the schema it
    # declares, and a version nobody understands is not a version 1.0.0.
    version = receipt.get("receipt_schema_version")
    if not isinstance(version, str) or version != RECEIPT_SCHEMA_VERSION:
        raise _Refusal(REASON_RECEIPT_VERSION_UNSUPPORTED)

    for field in _RECEIPT_FIELDS:
        if field == "scan_generation":
            # A generation is an integer, and is checked as one below.
            continue
        if not _text(receipt.get(field)):
            raise _Refusal(REASON_RECEIPT_INVALID % field)

    # A decision timestamp is an audit fact: it has to be placeable on a
    # timeline, so it carries seconds and an explicit zone. The value is bound
    # exactly as supplied and is never normalized.
    if not _is_rfc3339(receipt["decided_at"]):
        raise _Refusal(REASON_RECEIPT_TIMESTAMP_INVALID)

    if receipt.get("decision") not in DECISIONS:
        raise _Refusal(REASON_DECISION_UNKNOWN)
    for field in _RECEIPT_HEX_FIELDS:
        if not _is_hex64(receipt.get(field)):
            raise _Refusal(REASON_RECEIPT_INVALID % field)
    if not _generation(receipt.get("scan_generation")):
        raise _Refusal(REASON_RECEIPT_INVALID % "scan_generation")
    workspace = receipt["workspace_id"]
    if not workspace.startswith(_WORKSPACE_PREFIX) or not _is_hex64(
        workspace[len(_WORKSPACE_PREFIX):]
    ):
        raise _Refusal(REASON_RECEIPT_INVALID % "workspace_id")
    return dict(receipt)


def receipt_digests(receipt: Any, raw_bytes: Any = None) -> Tuple[str, Optional[str]]:
    """Return ``(canonical, raw)`` digests of a receipt.

    The canonical digest is taken over the deterministic serialization, so it is
    reproducible from the mapping alone. The raw digest is taken over the
    content exactly as read, so the caller must supply those bytes: a mapping
    that arrived in process has no raw form, and reporting a re-serialization as
    though it were the raw file would be a false claim. When none is supplied
    the raw digest is ``None`` and the record says so.
    """
    canonical = sha256_hex(dumps(receipt).encode("utf-8"))
    if isinstance(raw_bytes, (bytes, bytearray)):
        return canonical, sha256_hex(bytes(raw_bytes))
    return canonical, None


def _receipt_agrees(
    receipt: Dict[str, Any], evidence: Dict[str, Any], facts: Dict[str, Any]
) -> bool:
    """Return whether every receipt fact matches the bound and live facts."""
    accepted = evidence["accepted_revision"]
    target_group = evidence["target"]
    change = evidence["change"]
    validation_group = evidence["validation"]
    baseline = evidence["candidate"]["manifest"].get("baseline") or {}
    return (
        receipt["candidate_id"] == change["candidate_id"]
        and receipt["target_path"] == facts["path"]
        and receipt["predecessor_sha256"] == facts["predecessor_sha256"]
        and receipt["candidate_sha256"] == facts["candidate_sha256"]
        and receipt["manifest_sha256"] == target_group["manifest_sha256"]
        and receipt["review_sha256"] == target_group["review_sha256"]
        and receipt["binding_fingerprint"] == change["binding_fingerprint"]
        and receipt["workspace_id"] == accepted["workspace_id"]
        and receipt["scan_generation"] == accepted["scan_generation"]
        and receipt["baseline_fingerprint"] == accepted["baseline_fingerprint"]
        and receipt["baseline_fingerprint"] == baseline.get("baseline_fingerprint")
        and receipt["validation_result_id"] == validation_group["result_id"]
    )


# -- identity and records --------------------------------------------------


def apply_id_for(evidence: Dict[str, Any], receipt_digest: Optional[str]) -> str:
    """Return the content-addressed identity of one application attempt.

    Identity is the bound change, the bound target, the accepted revision and
    the receipt's canonical digest — or a fixed marker when no receipt was
    supplied, so an unapproved attempt and an approved one are never the same
    identity.
    """
    canon = dumps(
        {
            "change": evidence["change"],
            "target": dict(evidence["target"]),
            "accepted_revision": evidence["accepted_revision"],
            "receipt": receipt_digest or "absent",
        }
    )
    return APPLY_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def _record(
    state: str,
    reason_code: str,
    mode: str,
    evidence: Dict[str, Any],
    apply_id: str,
    digest_pair: Tuple[str, Optional[str]],
    receipt: Optional[Dict[str, Any]],
    write_surface: Dict[str, bool],
    observation: Optional[Dict[str, Any]],
    recovery: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble one record. Every field is either supplied or declared false."""
    canonical_digest, raw_digest = digest_pair
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "apply_id": apply_id,
        "mode": mode,
        "state": state,
        "reason_code": reason_code,
        "reason": {
            STATE_READY: REASON_READY,
            STATE_REFUSED_MISSING_APPROVAL: REASON_REFUSED_MISSING_APPROVAL,
            STATE_REFUSED_STALE: REASON_REFUSED_STALE,
            STATE_APPLIED: REASON_APPLIED,
            STATE_NOT_EFFECTIVE: REASON_NOT_EFFECTIVE,
            STATE_UNKNOWN: REASON_UNKNOWN,
            STATE_RECOVERY_REQUIRED: REASON_RECOVERY_REQUIRED,
        }.get(state, REASON_UNKNOWN),
        "applied": state == STATE_APPLIED,
        "approval_inferred": False,
        # A record's self-description is derived from its own write surface, so
        # the two cannot contradict each other: a record that says it was
        # persisted is one whose write surface says its bytes were written.
        # Keeping them independent is exactly how the persisted copy came to
        # claim ``record_persisted`` false while the record sat on disk.
        "record_persisted": write_surface["record_written"],
        "persistence_reason": None,
        "work_package": dict(evidence["work_package"]),
        "change": dict(evidence["change"]),
        "validation": dict(evidence["validation"]),
        "accepted_revision": dict(evidence["accepted_revision"]),
        "target": dict(evidence["target"]),
        "acceptance": (
            {
                "actor": receipt["actor"],
                "decision": receipt["decision"],
                "decided_at": receipt["decided_at"],
                "candidate_id": receipt["candidate_id"],
                "receipt_canonical_sha256": canonical_digest,
                "receipt_raw_sha256": raw_digest,
                "receipt_provenance": "client_declared",
            }
            if receipt is not None
            else None
        ),
        "write_scope": {
            "apply_root_only": True,
            "single_target": True,
            "accepted_repository": False,
            "git_metadata": False,
        },
        "write_surface": dict(write_surface),
        "observation": observation,
        "recovery": recovery,
        "mutation_surface": _mutation_surface(),
        "limitations": list(_LIMITATIONS),
    }


def validate_apply_record(record: Any) -> Optional[str]:
    """Validate a record against this contract; return a reason or ``None``."""
    if not isinstance(record, dict):
        return "record is not a mapping"
    if record.get("schema_version") != SCHEMA_VERSION:
        return "unsupported schema_version"
    if record.get("generator") != GENERATOR:
        return "unknown record generator"
    if record.get("mode") not in MODES:
        return "unknown mode"
    if record.get("state") not in APPLY_STATES:
        return "unknown apply state"
    if record.get("applied") is not (record.get("state") == STATE_APPLIED):
        return "applied does not match the terminal state"
    if record.get("approval_inferred") is not False:
        return "record must not infer an approval"
    identity = record.get("apply_id")
    if not isinstance(identity, str) or not identity.startswith(APPLY_ID_PREFIX):
        return "missing or malformed apply_id"
    surface = record.get("mutation_surface")
    if not isinstance(surface, dict):
        return "missing or malformed mutation_surface"
    if set(surface) != set(_MUTATION_SURFACE_KEYS):
        return "mutation_surface does not declare every named boundary"
    for key, value in sorted(surface.items()):
        if value is not False:
            return "mutation_surface declares a non-false %s" % key
    writes = record.get("write_surface")
    if not isinstance(writes, dict) or set(writes) != set(_WRITE_KEYS):
        return "missing or malformed write_surface"
    # The invariant the whole design turns on: a state that precedes or refuses
    # a write must declare that nothing was written, and a plan must declare the
    # whole write surface false whatever its state.
    if record.get("mode") == MODE_PLAN or record.get("state") in _PRE_WRITE_STATES:
        for key, value in sorted(writes.items()):
            if value is not False:
                return "a non-writing state must not declare a write: %s" % key
    if not isinstance(record.get("limitations"), list) or not record["limitations"]:
        return "missing or malformed limitations"
    for group in ("work_package", "change", "validation", "accepted_revision",
                  "target", "write_scope"):
        if not isinstance(record.get(group), dict):
            return "missing or malformed %s" % group
    return None


# -- custody and the write set ---------------------------------------------


def _validate_custody_base(
    apply_real: str, custody_base: Any
) -> Tuple[str, Optional[str]]:
    """Return ``(base, reason)`` for a custody base outside the apply root."""
    if not isinstance(custody_base, str) or not custody_base.strip():
        return "", REASON_CUSTODY_UNUSABLE
    base = os.path.realpath(os.path.abspath(custody_base))
    if not os.path.isdir(base):
        return "", REASON_CUSTODY_UNUSABLE
    if base == apply_real or _contained(apply_real, base):
        return "", REASON_CUSTODY_INSIDE_TARGET
    return base, None


def _write_new(path: str, data: bytes) -> Optional[str]:
    """Write ``data`` at ``path``, refusing to overwrite different bytes.

    Directories are created as needed; an existing file holding exactly these
    bytes is idempotent, and one holding anything else is a different artifact
    under the same name, which is refused rather than overwritten.
    """
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return REASON_CUSTODY_UNWRITABLE
    if os.path.exists(path):
        try:
            with open(path, "rb") as handle:
                if handle.read() == data:
                    return None
        except OSError:
            return REASON_CUSTODY_UNWRITABLE
        return REASON_RECORD_TAMPERED
    handle = None
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".hrca-apply-record-", suffix=".tmp", dir=directory
        )
        handle = os.fdopen(descriptor, "wb")
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(temp_path, path)
        temp_path = None
    except OSError:
        return REASON_CUSTODY_UNWRITABLE
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if temp_path is not None and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return None


def _create_pre_image(path: str, data: bytes) -> Optional[str]:
    """Create the pre-image exclusively. A taken path is refused, never reused."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return REASON_CUSTODY_UNWRITABLE
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return REASON_RECOVERY_PATH_TAKEN
    except OSError:
        return REASON_CUSTODY_UNWRITABLE
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return REASON_PREIMAGE_FAILED
    try:
        with open(path, "rb") as handle:
            written = handle.read(MAX_TARGET_BYTES + 1)
    except OSError:
        return REASON_PREIMAGE_FAILED
    if written != data:
        return REASON_PREIMAGE_STALE
    return None


def _remove_temp(path: str) -> None:
    """Remove one temporary this operation created, and nothing else.

    The guard is what keeps cleanup honest: the name must carry this module's
    own staging prefix and the parent must be a directory this call wrote into.
    Anything else is left completely alone.
    """
    if not os.path.basename(path).startswith(TEMP_PREFIX):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _replace_target(target: str, data: bytes) -> Tuple[bool, Optional[str]]:
    """Replace the target atomically from a verified same-directory temporary.

    The bytes are written, flushed and fsynced, **read back and hashed**, and
    only then renamed over the target — so the bytes that land are exactly the
    bytes verified. Returns ``(replaced, reason)``.
    """
    directory = os.path.dirname(target)
    temp_path = None
    staged = False
    try:
        descriptor, temp_path = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=directory)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged = True
        with open(temp_path, "rb") as handle:
            if handle.read(MAX_TARGET_BYTES + 1) != data:
                return False, REASON_TEMP_STALE
        os.replace(temp_path, target)
        temp_path = None
        return True, None
    except OSError:
        return False, (REASON_REPLACE_FAILED if staged else REASON_TEMP_FAILED)
    finally:
        if temp_path is not None:
            _remove_temp(temp_path)


def _observe(path: str, target: str, predecessor: str, candidate_sha: str) -> Dict[str, Any]:
    """Re-read the target and classify the observation from the bytes on disk."""
    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_TARGET_BYTES + 1)
    except OSError:
        return {
            "path": path,
            "sha256": None,
            "size": None,
            "outcome": OUTCOME_NEITHER,
            "readable": False,
        }
    observed = sha256_hex(data)
    if observed == candidate_sha:
        outcome = OUTCOME_CANDIDATE
    elif observed == predecessor:
        outcome = OUTCOME_PREDECESSOR
    else:
        outcome = OUTCOME_NEITHER
    return {
        "path": path,
        "sha256": observed,
        "size": len(data),
        "outcome": outcome,
        "readable": True,
    }


# -- the two operations ----------------------------------------------------


def plan_application(
    evidence: Any, apply_root: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Revalidate every non-approval fact against the live root.

    Takes no receipt and no custody base, and writes nothing. Returns a record
    whose state is ``ready_for_approval`` or ``refused_stale_or_mismatched``, or
    ``(None, reason)`` when the input cannot be bound.
    """
    try:
        _live_facts(evidence, apply_root)
    except _Refusal as exc:
        return None, str(exc)
    except _Stale as exc:
        return _record(
            STATE_REFUSED_STALE, str(exc), MODE_PLAN, evidence,
            apply_id_for(evidence, None), ("", None), None, _write_surface(),
            None, None,
        ), None
    return _record(
        STATE_READY, REASON_READY, MODE_PLAN, evidence,
        apply_id_for(evidence, None), ("", None), None, _write_surface(),
        None, None,
    ), None


def apply_application(
    evidence: Any,
    apply_root: Any,
    custody_base: Any,
    receipt: Any = None,
    receipt_bytes: Any = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Apply the bound candidate to the one bound target, or refuse.

    Re-runs the whole live validation sequence rather than trusting any prior
    verdict, requires a byte-bound receipt, and writes only the pre-image, the
    temporary file, the target replacement and the record. ``receipt_bytes``,
    when supplied, are the receipt exactly as read, so the record can carry the
    raw digest as well as the canonical one.
    """
    try:
        facts = _live_facts(evidence, apply_root)
    except _Refusal as exc:
        return None, str(exc)
    except _Stale as exc:
        # A stale world is a *state*, not a refusal: everything bound, and the
        # answer is that it disagrees. It is reached before the receipt is even
        # examined, which is the documented precedence — an approval about
        # moved bytes would be approval of the wrong thing.
        return _record(
            STATE_REFUSED_STALE, str(exc), MODE_APPLY, evidence,
            apply_id_for(evidence, None), ("", None), None, _write_surface(),
            None, None,
        ), None

    if receipt is None:
        return _record(
            STATE_REFUSED_MISSING_APPROVAL, REASON_REFUSED_MISSING_APPROVAL,
            MODE_APPLY, evidence, apply_id_for(evidence, None), ("", None), None,
            _write_surface(), None, None,
        ), None

    try:
        bound = _bind_receipt(receipt)
    except _Refusal as exc:
        return None, str(exc)

    digests = receipt_digests(bound, receipt_bytes)
    apply_id = apply_id_for(evidence, digests[0])

    if not _receipt_agrees(bound, evidence, facts):
        return _record(
            STATE_REFUSED_STALE, STALE_RECEIPT, MODE_APPLY, evidence, apply_id,
            digests, bound, _write_surface(), None, None,
        ), None

    if bound["decision"] != DECISION_ACCEPTED:
        return _record(
            STATE_REFUSED_MISSING_APPROVAL, REASON_REFUSED_MISSING_APPROVAL,
            MODE_APPLY, evidence, apply_id, digests, bound, _write_surface(),
            None, None,
        ), None

    canonical = facts["canonical_root"]
    base, reason = _validate_custody_base(canonical, custody_base)
    if reason is not None:
        return None, reason

    record_path = os.path.join(
        base, RECORD_DIRNAME, apply_id.split(":", 1)[1] + ".json"
    )
    # A durable replay of a completed application is refused before any write,
    # by the record that claims it and by the pre-image that would name it.
    if os.path.exists(record_path):
        try:
            with open(record_path, "rb") as handle:
                prior = json.loads(handle.read(MAX_RECORD_BYTES + 1).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None, REASON_RECORD_TAMPERED
        if isinstance(prior, dict) and (prior.get("write_surface") or {}).get(
            "target_written"
        ):
            return None, REASON_APPLY_ALREADY_RECORDED

    target = os.path.join(canonical, *facts["path"].split("/"))
    pre_image = os.path.join(
        base, RECOVERY_DIRNAME,
        "%s.%s" % (os.path.basename(facts["path"]), facts["predecessor_sha256"]),
    )
    if os.path.exists(pre_image):
        return None, REASON_RECOVERY_PATH_TAKEN

    # The predecessor is read once more, immediately before the write, so the
    # window between the verdict and the replacement is closed as far as one
    # process can close it.
    reason = _create_pre_image(pre_image, _read_target(canonical, facts["path"]))
    if reason is not None:
        return None, reason

    replaced, replace_reason = _replace_target(target, facts["candidate_bytes"])
    observation = _observe(
        facts["path"], target, facts["predecessor_sha256"], facts["candidate_sha256"]
    )
    recovery = {
        "pre_image_path": os.path.relpath(pre_image, base),
        "pre_image_sha256": facts["predecessor_sha256"],
        "origin": "created",
        "action": "none",
    }

    if not replaced:
        state, code = STATE_UNKNOWN, replace_reason or REASON_UNKNOWN
        if replace_reason == REASON_TEMP_STALE:
            state, code = STATE_NOT_EFFECTIVE, REASON_NOT_EFFECTIVE
    elif observation["outcome"] == OUTCOME_CANDIDATE:
        state, code = STATE_APPLIED, REASON_APPLIED
    elif observation["outcome"] == OUTCOME_PREDECESSOR:
        state, code = STATE_NOT_EFFECTIVE, REASON_NOT_EFFECTIVE
    else:
        state, code = STATE_RECOVERY_REQUIRED, REASON_RECOVERY_REQUIRED

    # The record is assembled with what will be true of it once the write lands,
    # and those are the exact bytes written. That is not optimism: the write is
    # atomic, so a record that exists carries precisely this content, and a write
    # that fails leaves no record at all. A failure is therefore reported as an
    # *outcome* — only the returned copy is amended — never by persisting a file
    # that says it was not persisted, which is a different claim entirely. The
    # target observation stands as observed either way, and nothing is retried,
    # repaired or restored.
    record = _record(
        state, code, MODE_APPLY, evidence, apply_id, digests, bound,
        _write_surface(True, True, replaced, True), observation, recovery,
    )
    write_reason = _write_new(record_path, (dumps(record) + "\n").encode("utf-8"))
    if write_reason is not None:
        record["write_surface"] = _write_surface(True, True, replaced, False)
        record["record_persisted"] = record["write_surface"]["record_written"]
        record["persistence_reason"] = write_reason
    return record, None


__all__ = [
    "SCHEMA_VERSION",
    "GENERATOR",
    "APPLY_ID_PREFIX",
    "RECEIPT_SCHEMA_VERSION",
    "RECORD_DIRNAME",
    "RECOVERY_DIRNAME",
    "TEMP_PREFIX",
    "MAX_TARGET_BYTES",
    "MAX_RECEIPT_BYTES",
    "MAX_RECORD_BYTES",
    "MODE_PLAN",
    "MODE_APPLY",
    "MODES",
    "STATE_READY",
    "STATE_REFUSED_MISSING_APPROVAL",
    "STATE_REFUSED_STALE",
    "STATE_APPLIED",
    "STATE_NOT_EFFECTIVE",
    "STATE_UNKNOWN",
    "STATE_RECOVERY_REQUIRED",
    "STATE_RECOVERY_FAILED",
    "APPLY_STATES",
    "DECISION_ACCEPTED",
    "DECISION_REJECTED",
    "DECISIONS",
    "OUTCOME_PREDECESSOR",
    "OUTCOME_CANDIDATE",
    "OUTCOME_NEITHER",
    "REASON_READY",
    "REASON_REFUSED_MISSING_APPROVAL",
    "REASON_REFUSED_STALE",
    "REASON_APPLIED",
    "REASON_NOT_EFFECTIVE",
    "REASON_UNKNOWN",
    "REASON_RECOVERY_REQUIRED",
    "STALE_ROOT_IDENTITY",
    "STALE_IDENTITY_AGREEMENT",
    "STALE_EDIT_BINDING",
    "STALE_PREDECESSOR",
    "STALE_CANDIDATE",
    "STALE_BASELINE",
    "STALE_VALIDATION",
    "STALE_RECEIPT",
    "REASON_EVIDENCE_NOT_MAPPING",
    "REASON_GROUP_MISSING",
    "REASON_IDENTITY_INVALID",
    "REASON_MODE_UNKNOWN",
    "REASON_RECEIPT_NOT_MAPPING",
    "REASON_RECEIPT_INVALID",
    "REASON_RECEIPT_VERSION_UNSUPPORTED",
    "REASON_RECEIPT_TIMESTAMP_INVALID",
    "REASON_DECISION_UNKNOWN",
    "REASON_EDIT_UNSOUND",
    "REASON_EDIT_MISSING",
    "REASON_ROOT_UNUSABLE",
    "REASON_ROOT_INSIDE_GIT",
    "REASON_CUSTODY_UNUSABLE",
    "REASON_CUSTODY_INSIDE_TARGET",
    "REASON_TARGET_NOT_AUTHORIZED",
    "REASON_TARGET_ABSENT",
    "REASON_TARGET_NOT_REGULAR",
    "REASON_TARGET_SYMLINK",
    "REASON_TARGET_HARDLINK",
    "REASON_TARGET_ESCAPES",
    "REASON_TARGET_UNREADABLE",
    "REASON_TARGET_OVERSIZED",
    "REASON_TARGET_NOT_UTF8",
    "REASON_TARGET_UNREVIEWABLE",
    "REASON_CANDIDATE_UNREADABLE",
    "REASON_RECOVERY_PATH_TAKEN",
    "REASON_APPLY_ALREADY_RECORDED",
    "REASON_RECORD_TAMPERED",
    "REASON_CUSTODY_UNWRITABLE",
    "REASON_PREIMAGE_FAILED",
    "REASON_PREIMAGE_STALE",
    "REASON_TEMP_FAILED",
    "REASON_TEMP_STALE",
    "REASON_REPLACE_FAILED",
    "dumps",
    "normalize_root",
    "receipt_digests",
    "apply_id_for",
    "validate_apply_record",
    "plan_application",
    "apply_application",
]
