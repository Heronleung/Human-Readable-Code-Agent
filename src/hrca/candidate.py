"""Isolated content-addressed candidate (P5.4).

Builds one immutable candidate from four things that must already agree:

1. a validated P5.3 Intent Delta;
2. its bound P5.3 Impact Proposal;
3. the accepted read-side evidence those two were bound to; and
4. an explicit typed edit request (:mod:`hrca.candidate_edit`).

The result is a fresh directory holding the exact replacement bytes plus a
canonical manifest, and a review envelope describing precisely what changed.

What this module is allowed to do
---------------------------------

It reads files under one accepted repository root, and it writes **only**
beneath a fresh output root it creates outside that repository and outside Git
metadata. Nothing else. It has no provider, credential, command, runner,
network, Twin-write or Memory-write authority, it never stages or commits, and
it never touches the accepted repository.

Materializing a candidate is not validating, approving or adopting one. The
envelope carries ``validated``, ``approved`` and ``adopted`` all ``False``, and
the accepted repository is unchanged by construction: the only writes are under
the new output root.

Refusal versus state
--------------------

Two different questions, kept apart:

* ``(None, reason)`` — the request or its binding cannot be *read*: an invalid
  edit, delta or proposal; evidence the proposal does not re-derive from; an
  edit whose declared delta, proposal, binding fingerprint or baseline is not
  the one supplied. There is nothing coherent to describe, so nothing is
  described. A refusal is also where an unbounded or undecodable *request* ends:
  too many operations, a payload that is not UTF-8, or a payload past the
  accepted byte bound.
* an **envelope** with a terminal ``state`` — the request *is* read, and the
  answer is a determination about content. The five states are
  ``candidate_ready``, ``no_change``, ``unsupported_content``, ``oversized`` and
  ``refused``, with the fixed precedence ``refused`` > ``oversized`` >
  ``unsupported_content`` > ``no_change`` > ``candidate_ready``.

``unsupported_content`` covers content this contract will not render as a text
diff — a NUL byte or a byte-order mark, in the replacement the developer
supplied or in the predecessor the repository holds. It is a state rather than a
refusal because a developer can legitimately hand this contract the wrong thing
and deserves to be told what was wrong with it, and because the same answer has
to cover a repository that simply contains such a file.

Authorization is evidence, never prose
--------------------------------------

A path may be replaced only when the bound proposal puts it in scope **and**
carries it as evidence: it must appear as a target path in
``target_scope.targets``, and it must appear either as a ``scanner_file``
binding or as a governed fact's ``file``. There is no basename, suffix,
case-folding, entity-name, prose, path-order or nearest-match route, and prose
cannot add a path or widen authority.

Two further exact identities gate every operation. The proposal must be in its
``bound`` state — ``no_impact``, ``unknown``, ``unavailable``, ``unsupported``
and ``ambiguous`` all mean the affected facts were *not* established, and an
edit that changed a file anyway would be changing facts nobody bound. And the
edit's ``expected_sha256`` must equal the Twin's own recorded fingerprint for
that exact file artifact, so the predecessor is pinned by two independent
sources before a byte is read.

Isolation and atomicity
-----------------------

The candidate root is created by :func:`tempfile.mkdtemp` under the validated
output base (the pattern :func:`hrca.memory_package.stage_package` already uses
to refuse reuse), written and ``fsync``-ed, verified against its own recorded
hashes, and only then claimed and renamed into place with :func:`os.rename` —
the atomic-rename contract :mod:`hrca.memory_store` and :mod:`hrca.library_store`
already rely on. The final name is derived from the candidate identity, so a
second build of the same candidate is refused rather than overwriting the first.

A failure at any point removes only the directory this build created, and only
after checking that its parent is the base this call validated and that its name
carries this module's staging prefix. Cleanup can therefore never reach an
unrelated output root, another candidate, the accepted repository, or the
untracked bundle.

Identical inputs produce an identical ``candidate_id``, an identical manifest
and an identical review envelope. Identity is logical content only: the manifest
records no path, no root name and no timestamp, and the root name is derived
from the identity rather than the other way round.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from . import candidate_diff, candidate_edit, impact_proposal, intent_delta
from .identity import file_artifact_id, sha256_hex

CANDIDATE_SCHEMA_VERSION = "1.0.0"
CANDIDATE_GENERATOR = "hrca-candidate"
CANDIDATE_REVIEW_GENERATOR = "hrca-candidate-review"
CANDIDATE_ID_PREFIX = "candidate:"

MANIFEST_NAME = "candidate.json"
FILES_DIR = "files"
STAGING_PREFIX = ".hrca-staging-"
ROOT_PREFIX = "candidate-"

# Read from this module at call time so the bounds stay the contract's own.
MAX_FILE_BYTES = candidate_edit.MAX_FILE_BYTES
MAX_DIFF_LINES = candidate_diff.MAX_DIFF_LINES

STATE_CANDIDATE_READY = "candidate_ready"
STATE_NO_CHANGE = "no_change"
STATE_UNSUPPORTED_CONTENT = "unsupported_content"
STATE_OVERSIZED = "oversized"
STATE_REFUSED = "refused"
CANDIDATE_STATES = frozenset(
    {
        STATE_CANDIDATE_READY,
        STATE_NO_CHANGE,
        STATE_UNSUPPORTED_CONTENT,
        STATE_OVERSIZED,
        STATE_REFUSED,
    }
)

# Only a ``bound`` proposal authorizes an edit: every other terminal state
# means the affected facts were not established, so there is nothing exact to
# change and any edit would be inventing the scope it claims to respect.
AUTHORIZING_PROPOSAL_STATES = frozenset({impact_proposal.STATE_BOUND})

# Bounded reasons: refusals first, then the envelope states.
REASON_EDIT_NOT_VALID = "edit request is not valid"
REASON_DELTA_NOT_VALID = "intent delta is not valid"
REASON_PROPOSAL_NOT_VALID = "impact proposal is not valid"
REASON_EVIDENCE_UNBOUND = "the accepted evidence does not bind to this intent"
REASON_PROPOSAL_NOT_REBOUND = "the impact proposal does not match the accepted evidence"
REASON_DELTA_MISMATCH = "the edit is not bound to this intent delta"
REASON_PROPOSAL_MISMATCH = "the edit is not bound to this impact proposal"
REASON_BINDING_MISMATCH = "the edit binding fingerprint does not match the proposal"
REASON_BASELINE_MISMATCH = "the edit baseline does not match the intent baseline"

REASON_TARGET_NOT_IN_SCOPE = "the target is not named by the intent scope"
REASON_TARGET_NOT_IN_EVIDENCE = "the target is not bound as evidence by the proposal"
REASON_PROPOSAL_NOT_AUTHORIZING = "the impact proposal does not authorize an edit"
REASON_TWIN_IDENTITY_ABSENT = "the Twin holds no file artifact for the target"
REASON_TWIN_IDENTITY_MISMATCH = "the edit does not match the Twin's recorded fingerprint"
REASON_TWIN_IDENTITY_UNBOUND = "the Twin artifact is not bound by the proposal"
REASON_PREDECESSOR_ABSENT = "the predecessor file is absent"
REASON_PREDECESSOR_NOT_REGULAR = "the predecessor is not a regular file"
REASON_PREDECESSOR_SYMLINK = "the predecessor is or passes through a symbolic link"
REASON_PREDECESSOR_HARDLINK = "the predecessor has more than one hard link"
REASON_PREDECESSOR_ESCAPES = "the target resolves outside the accepted repository"
REASON_PREDECESSOR_UNREADABLE = "the predecessor could not be read"
REASON_PREDECESSOR_MOVED = "the predecessor changed while the candidate was being built"
REASON_PREDECESSOR_STALE = "the predecessor does not have the expected content"

REASON_OUTPUT_UNUSABLE = "the output base is not a usable directory"
REASON_OUTPUT_INSIDE_REPOSITORY = "the output base is inside the accepted repository"
REASON_OUTPUT_ROOT_TAKEN = "the candidate output root is already in use"
REASON_STAGING_FAILED = "the candidate could not be staged"
REASON_VERIFY_FAILED = "the staged candidate does not match its recorded identities"

REASON_READY = "candidate_materialized"
REASON_NO_CHANGE = "the replacement equals the predecessor"
REASON_UNSUPPORTED_CONTENT = "a bound file is not reviewable UTF-8 text"
REASON_OVERSIZED = "the request exceeds an accepted size bound"

# The complete mutation surface this contract names. Every value is always
# ``False``; the block exists so a reviewer, a test and a later phase can assert
# the whole surface at once instead of trusting a docstring.
_MUTATION_SURFACE_KEYS = (
    "accepted_source",
    "git_index",
    "git_ref",
    "branch",
    "commit",
    "worktree",
    "runner_job",
    "provider_request",
    "credential",
    "network",
    "remote",
    "twin_state",
    "memory",
    "validation",
    "approval",
    "adoption",
    "protocol_action",
    "ui",
    "package_state",
    "recovery_state",
)

_WRITE_SCOPE = {
    "candidate_root_only": True,
    "accepted_repository": False,
    "git_metadata": False,
}

_LIMITATIONS = (
    "a candidate is materialized only: it is not validated, approved or adopted, "
    "and nothing has been applied to the accepted repository",
    "the diff and the recorded predecessor identities describe the bytes read "
    "during this build; a later change to the accepted repository is not reflected",
    "the grammar represents whole-file replacement of UTF-8 Python source only",
)

_RISK_NOT_VALIDATION = (
    "the candidate is materialized only; no validation, approval or adoption has "
    "occurred, and the accepted repository is unchanged"
)
_RISK_TEXT_ONLY = (
    "the diff is a line-level rendering of UTF-8 text, not a byte-level proof of "
    "equivalence"
)
_RISK_PREDECESSOR_MAY_MOVE = (
    "the predecessor was read once under a checked identity; a concurrent change "
    "to the accepted repository after this build is not reflected"
)
_RISK_WHOLE_FILE = (
    "the operation replaces the whole file, so content the replacement does not "
    "carry forward is removed rather than reviewed line by line"
)
_RISK_NO_CHANGE = (
    "the replacement equals the predecessor, so no candidate was produced; this "
    "is not a statement that the requested outcome is already satisfied"
)
_RISK_UNREVIEWABLE = (
    "a file the proposal binds is not reviewable UTF-8 text, so whether this "
    "intent affects it cannot be reviewed here"
)

_QUESTION_AUTHORITY = (
    "what authority would apply this candidate is not determined here; this "
    "workflow grants none and performs none"
)
_QUESTION_CORRECTNESS = (
    "whether the replacement is correct, complete and desirable is not "
    "determined here; that is a later phase"
)
_QUESTION_REFUSAL = (
    "why the accepted evidence and the requested change disagree, and which of "
    "the two is intended to move"
)


def dumps(obj: Any) -> str:
    """Serialize a manifest or envelope canonically (sorted keys, compact)."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _mutation_surface() -> Dict[str, bool]:
    return {key: False for key in _MUTATION_SURFACE_KEYS}


# -- identity --------------------------------------------------------------


def candidate_id_for(manifest: Dict[str, Any]) -> str:
    """Return the content-addressed identity of a candidate manifest."""
    canon = dumps({k: v for k, v in manifest.items() if k != "candidate_id"})
    return CANDIDATE_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def candidate_root_name(candidate_id: str) -> str:
    """Return the deterministic directory name a candidate identity owns."""
    return ROOT_PREFIX + candidate_id.split(":", 1)[-1][:32]


# -- read-side safety ------------------------------------------------------


def _contained(root_real: str, target_real: str) -> bool:
    """Return whether ``target_real`` lies strictly beneath ``root_real``."""
    return target_real.startswith(root_real + os.sep)


def reviewable_text(text: str) -> bool:
    """Return whether ``text`` is content this contract will render as a diff.

    A NUL byte has no line structure to review, and a byte-order mark is an
    encoding marker rather than source a reader wrote. Both are reported as the
    ``unsupported_content`` state — a statement about the content, not a
    malformed-request refusal — because a developer can legitimately hand this
    contract the wrong thing and deserves to be told what was wrong with it.
    """
    return "\x00" not in text and not text.startswith("﻿")


def _symlinked_component(root_real: str, components: List[str]) -> Optional[str]:
    """Return the first path component that is a symbolic link, if any.

    Every prefix of the target is checked, so a link anywhere in the chain — not
    only at the final component — is refused. A path that reaches its file
    through a link names something other than the bytes it appears to contain.
    """
    current = root_real
    for component in components:
        current = os.path.join(current, component)
        if os.path.islink(current):
            return component
    return None


def _read_predecessor(
    root_real: str, path: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read one predecessor under checked identity; return ``(record, reason)``.

    The record is ``{bytes, text, sha256, size, stat_key}``. Every check is
    fail-closed: a link, a special file, a second hard link, an escape from the
    root or an unreadable file is a refusal, never a best-effort read.
    """
    components = path.split("/")
    if _symlinked_component(root_real, components) is not None:
        return None, REASON_PREDECESSOR_SYMLINK

    target = os.path.join(root_real, *components)
    real = os.path.realpath(target)
    if not _contained(root_real, real):
        return None, REASON_PREDECESSOR_ESCAPES

    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None, REASON_PREDECESSOR_ABSENT
    except OSError:
        return None, REASON_PREDECESSOR_UNREADABLE

    if stat.S_ISLNK(info.st_mode):
        return None, REASON_PREDECESSOR_SYMLINK
    if not stat.S_ISREG(info.st_mode):
        return None, REASON_PREDECESSOR_NOT_REGULAR
    if info.st_nlink != 1:
        return None, REASON_PREDECESSOR_HARDLINK
    if info.st_size > MAX_FILE_BYTES:
        return None, REASON_OVERSIZED

    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None, REASON_PREDECESSOR_UNREADABLE
    if len(data) > MAX_FILE_BYTES:
        return None, REASON_OVERSIZED

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, REASON_UNSUPPORTED_CONTENT
    if not reviewable_text(text):
        return None, REASON_UNSUPPORTED_CONTENT

    return (
        {
            "bytes": data,
            "text": text,
            "sha256": sha256_hex(data),
            "size": len(data),
            "stat_key": (info.st_ino, info.st_size, info.st_mtime_ns),
        },
        None,
    )


def _predecessor_moved(root_real: str, path: str, record: Dict[str, Any]) -> bool:
    """Return whether a predecessor changed since it was read."""
    target = os.path.join(root_real, *path.split("/"))
    try:
        info = os.lstat(target)
    except OSError:
        return True
    if (info.st_ino, info.st_size, info.st_mtime_ns) != record["stat_key"]:
        return True
    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return True
    return sha256_hex(data) != record["sha256"]


# -- authorization ---------------------------------------------------------


def authorized_paths(proposal: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return the exact paths the bound proposal authorizes, and why.

    A path is authorized only when the proposal's target scope names it *and*
    the proposal carries it as evidence. The two conditions are checked
    separately because they answer different questions — what the intent scoped,
    and what the evidence actually bound — and a path that satisfies one but not
    the other is refused rather than merged into a single membership test.
    """
    out: Dict[str, Dict[str, Any]] = {}
    target_scope = proposal.get("target_scope") or {}
    for target in target_scope.get("targets") or []:
        path = target.get("path")
        if isinstance(path, str) and path:
            out[path] = {
                "artifact_id": target.get("artifact_id"),
                "in_scope": True,
                "in_evidence": False,
            }
    for binding in proposal.get("evidence_bindings") or []:
        if binding.get("kind") == "scanner_file" and binding.get("id") in out:
            out[binding["id"]]["in_evidence"] = True
    for fact in proposal.get("affected_facts") or []:
        path = fact.get("file")
        if isinstance(path, str) and path in out:
            out[path]["in_evidence"] = True
    return out


def _bound_artifact(
    evidence: Dict[str, Any], proposal: Dict[str, Any], path: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the Twin file artifact for ``path``, if the proposal binds it.

    The proposal must bind an artifact that *lives in this exact path* — the
    file artifact itself when the scope named the module, or the class, method
    or function artifact when the scope named a symbol. Both are exact: the
    path comes from the artifact the proposal recorded, not from the request.

    The predecessor identity comes from the file artifact, which is the only
    artifact that carries a whole-file fingerprint.
    """
    store = evidence.get("twin")
    if not isinstance(store, dict):
        return None, REASON_TWIN_IDENTITY_ABSENT

    bound = {
        target.get("artifact_id")
        for target in (proposal.get("target_scope") or {}).get("targets") or []
        if target.get("path") == path
    }
    if not bound:
        return None, REASON_TWIN_IDENTITY_UNBOUND

    artifact_id = file_artifact_id(path)
    for record in store.get("artifacts") or []:
        if isinstance(record, dict) and record.get("id") == artifact_id:
            return record, None
    return None, REASON_TWIN_IDENTITY_ABSENT


# -- output root -----------------------------------------------------------


def _validate_output_base(
    repo_real: str, output_base: Any
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(base, reason)`` for an output base outside the repository."""
    if not isinstance(output_base, str) or not output_base.strip():
        return None, REASON_OUTPUT_UNUSABLE
    base = os.path.realpath(os.path.abspath(output_base))
    if not os.path.isdir(base):
        return None, REASON_OUTPUT_UNUSABLE
    if base == repo_real or _contained(repo_real, base):
        return None, REASON_OUTPUT_INSIDE_REPOSITORY
    if ".git" in base.replace(os.sep, "/").split("/"):
        return None, REASON_OUTPUT_INSIDE_REPOSITORY
    return base, None


def _discard_tree(base_real: str, path: str) -> None:
    """Remove one tree this build created, and nothing else.

    The guard is what keeps cleanup honest: the parent must be exactly the base
    this call validated, and the name must carry this module's own prefix. A
    path that fails either test is left completely alone, so cleanup can never
    reach an unrelated output root, another candidate, or the repository.
    """
    if os.path.dirname(path) != base_real:
        return
    name = os.path.basename(path)
    if not (name.startswith(STAGING_PREFIX) or name.startswith(ROOT_PREFIX)):
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for filename in filenames:
            try:
                os.remove(os.path.join(dirpath, filename))
            except OSError:
                pass
        for dirname in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, dirname))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _write_staged(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _stage(
    base_real: str, manifest: Dict[str, Any], entries: List[Tuple[str, bytes]]
) -> Tuple[Optional[str], Optional[str]]:
    """Stage a candidate atomically and return ``(root_name, reason)``.

    The whole tree is written and verified before it is claimed, and the claim
    itself is an :func:`os.mkdir` — which fails rather than overwrites when the
    name is taken — followed by an :func:`os.rename` onto the now-empty claimed
    directory. A build that cannot complete removes exactly the two directories
    it made and reports a bounded reason.
    """
    staging = None
    claimed = None
    try:
        staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=base_real)
        for path, data in entries:
            _write_staged(os.path.join(staging, FILES_DIR, *path.split("/")), data)
        manifest_bytes = dumps(manifest).encode("utf-8")
        _write_staged(os.path.join(staging, MANIFEST_NAME), manifest_bytes)

        # Verify what is on disk against what was recorded, before claiming.
        for path, data in entries:
            staged = os.path.join(staging, FILES_DIR, *path.split("/"))
            with open(staged, "rb") as handle:
                if sha256_hex(handle.read()) != sha256_hex(data):
                    raise _Staging(REASON_VERIFY_FAILED)
        with open(os.path.join(staging, MANIFEST_NAME), "rb") as handle:
            if handle.read() != manifest_bytes:
                raise _Staging(REASON_VERIFY_FAILED)

        final = os.path.join(base_real, candidate_root_name(manifest["candidate_id"]))
        try:
            os.mkdir(final)
        except FileExistsError:
            return None, REASON_OUTPUT_ROOT_TAKEN
        except OSError:
            raise _Staging(REASON_STAGING_FAILED)
        claimed = final
        os.rename(staging, final)
        staging = None
        claimed = None
        return os.path.basename(final), None
    except _Staging as exc:
        return None, str(exc)
    except OSError:
        return None, REASON_STAGING_FAILED
    finally:
        if staging is not None:
            _discard_tree(base_real, staging)
        if claimed is not None:
            _discard_tree(base_real, claimed)


class _Staging(Exception):
    """Internal: a bounded staging failure with a safe reason."""


# -- envelope --------------------------------------------------------------


def _operation_record(operation: Dict[str, Any]) -> Dict[str, Any]:
    """Return the always-complete operation record used in a review envelope.

    Every key is present on every record. A field this build did not establish
    is ``None`` rather than absent, so a reader never has to infer from a
    missing key whether a value was unset or unchecked.
    """
    return {
        "op": candidate_edit.OP_REPLACE_FILE,
        "path": operation["path"],
        "expected_sha256": operation["expected_sha256"],
        "authorized": False,
        "reason_code": None,
        "reason": None,
        "before_sha256": None,
        "before_bytes": None,
        "after_sha256": None,
        "after_bytes": None,
        "before_final_newline": None,
        "after_final_newline": None,
        "diff": [],
    }


def _envelope(
    state: str,
    reason_code: str,
    reason: str,
    binding: Dict[str, Any],
    operations: List[Dict[str, Any]],
    *,
    candidate_id: Optional[str] = None,
    root_name: Optional[str] = None,
) -> Dict[str, Any]:
    risks: List[Dict[str, Any]] = [
        {"risk_id": "risk:not_validation", "statement": _RISK_NOT_VALIDATION},
        {"risk_id": "risk:text_only_diff", "statement": _RISK_TEXT_ONLY},
        {"risk_id": "risk:predecessor_may_move", "statement": _RISK_PREDECESSOR_MAY_MOVE},
    ]
    if state == STATE_NO_CHANGE:
        risks.append({"risk_id": "risk:no_change", "statement": _RISK_NO_CHANGE})
    if state == STATE_UNSUPPORTED_CONTENT:
        risks.append({"risk_id": "risk:unreviewable", "statement": _RISK_UNREVIEWABLE})
    for record in operations:
        if record.get("before_sha256") is not None and record["before_sha256"] != record.get(
            "after_sha256"
        ):
            risks.append(
                {
                    "risk_id": "risk:whole_file_replacement:" + record["path"],
                    "statement": _RISK_WHOLE_FILE,
                }
            )

    questions: List[Dict[str, Any]] = [
        {"question_id": "question:authority", "question": _QUESTION_AUTHORITY},
        {"question_id": "question:correctness", "question": _QUESTION_CORRECTNESS},
    ]
    if state == STATE_REFUSED:
        questions.append({"question_id": "question:refusal", "question": _QUESTION_REFUSAL})
    risks.sort(key=lambda entry: entry["risk_id"])
    questions.sort(key=lambda entry: entry["question_id"])

    return {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "generator": CANDIDATE_REVIEW_GENERATOR,
        "candidate_id": candidate_id,
        "state": state,
        "reason_code": reason_code,
        "reason": reason,
        # A candidate is a proposal of bytes, never an outcome.
        "advisory": True,
        "executable": False,
        "applied": False,
        "validated": False,
        "approved": False,
        "adopted": False,
        # Only the fresh root's own name, never the absolute base: the envelope
        # is the shareable review artifact, and the operator already knows the
        # base they chose.
        "candidate_root_name": root_name,
        "binding": binding,
        "operations": operations,
        "risks": risks,
        "unresolved_questions": questions,
        "mutation_surface": _mutation_surface(),
        "write_scope": dict(_WRITE_SCOPE),
        "limitations": list(_LIMITATIONS),
    }


def _assemble_manifest(
    records: List[Dict[str, Any]],
    edit: Dict[str, Any],
    intent_delta_doc: Dict[str, Any],
    proposal: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the canonical manifest — the one place a candidate is identified.

    The manifest records logical content only: no path, no root name and no
    timestamp, so the same request against the same evidence produces the same
    ``candidate_id`` and the same manifest bytes wherever it is materialized.
    """
    manifest: Dict[str, Any] = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "generator": CANDIDATE_GENERATOR,
        "candidate_id": "",
        "state": STATE_CANDIDATE_READY,
        "edit_id": edit["edit_id"],
        "intent_delta_id": intent_delta_doc["intent_delta_id"],
        "proposal_id": proposal["proposal_id"],
        "binding_fingerprint": proposal["binding"]["binding_fingerprint"],
        "baseline": dict(edit["baseline"]),
        "operations": [
            {
                key: record[key]
                for key in (
                    "op",
                    "path",
                    "before_sha256",
                    "before_bytes",
                    "after_sha256",
                    "after_bytes",
                    "before_final_newline",
                    "after_final_newline",
                    "diff",
                )
            }
            for record in records
        ],
        "files": sorted(
            (
                {
                    "path": record["path"],
                    "sha256": record["after_sha256"],
                    "bytes": record["after_bytes"],
                }
                for record in records
                if record["diff"]
            ),
            key=lambda entry: entry["path"],
        ),
        "write_scope": dict(_WRITE_SCOPE),
        "limitations": list(_LIMITATIONS),
    }
    manifest["candidate_id"] = candidate_id_for(manifest)
    return manifest


def _binding_record(
    edit: Dict[str, Any], intent_delta_doc: Dict[str, Any], proposal: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "edit_id": edit["edit_id"],
        "intent_delta_id": intent_delta_doc["intent_delta_id"],
        "proposal_id": proposal["proposal_id"],
        "binding_fingerprint": proposal["binding"]["binding_fingerprint"],
        "workspace_id": edit["baseline"]["workspace_id"],
        "scan_generation": edit["baseline"]["scan_generation"],
        "baseline_fingerprint": edit["baseline"]["baseline_fingerprint"],
        "scanner_schema_version": edit["baseline"]["scanner_schema_version"],
        "scanner_grammar": dict(edit["baseline"]["grammar"]),
    }


# -- the build -------------------------------------------------------------


def build_candidate(
    edit: Any,
    intent_delta_doc: Any,
    proposal: Any,
    evidence: Any,
    repository_root: Any,
    output_base: Any,
    materialize: bool = True,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build one candidate and return ``(review envelope, error)``.

    ``materialize=False`` performs the whole build read-only and returns the
    same envelope with no output root created — the cancellation path, and the
    only thing the CLI's ``review`` command does.
    """
    # -- readable request and binding -------------------------------------
    if candidate_edit.validate_edit(edit) is not None:
        return None, REASON_EDIT_NOT_VALID
    if intent_delta.validate_intent_delta(intent_delta_doc) is not None:
        return None, REASON_DELTA_NOT_VALID
    if impact_proposal.validate_impact_proposal(proposal) is not None:
        return None, REASON_PROPOSAL_NOT_VALID

    rebuilt, error = impact_proposal.build_impact_proposal(intent_delta_doc, evidence)
    if error is not None:
        return None, REASON_EVIDENCE_UNBOUND
    if (
        rebuilt["proposal_id"] != proposal["proposal_id"]
        or rebuilt["binding"]["binding_fingerprint"]
        != proposal["binding"]["binding_fingerprint"]
    ):
        return None, REASON_PROPOSAL_NOT_REBOUND

    if edit["intent_delta_id"] != intent_delta_doc["intent_delta_id"]:
        return None, REASON_DELTA_MISMATCH
    if edit["proposal_id"] != proposal["proposal_id"]:
        return None, REASON_PROPOSAL_MISMATCH
    if edit["binding_fingerprint"] != proposal["binding"]["binding_fingerprint"]:
        return None, REASON_BINDING_MISMATCH
    if edit["baseline"] != intent_delta_doc["baseline"]:
        return None, REASON_BASELINE_MISMATCH

    binding = _binding_record(edit, intent_delta_doc, proposal)
    records = [_operation_record(op) for op in edit["operations"]]

    if proposal["state"] not in AUTHORIZING_PROPOSAL_STATES:
        for record in records:
            record["reason_code"] = REASON_PROPOSAL_NOT_AUTHORIZING
            record["reason"] = "the proposal's state is not bound"
        return (
            _envelope(
                STATE_REFUSED,
                REASON_PROPOSAL_NOT_AUTHORIZING,
                "the impact proposal is in state '%s', which does not establish the "
                "affected facts an edit would change" % proposal["state"],
                binding,
                records,
            ),
            None,
        )

    authorizations = authorized_paths(proposal)

    def refuse_all(reason_code: str, message: str) -> Dict[str, Any]:
        for record in records:
            record["reason_code"] = reason_code
            record["reason"] = message
        return _envelope(STATE_REFUSED, reason_code, message, binding, records)

    # -- scope and evidence, per operation --------------------------------
    for record in records:
        authorization = authorizations.get(record["path"])
        if authorization is None or not authorization["in_scope"]:
            return refuse_all(
                REASON_TARGET_NOT_IN_SCOPE,
                "the intent's target scope does not name an artifact at this path",
            ), None
        if not authorization["in_evidence"]:
            return refuse_all(
                REASON_TARGET_NOT_IN_EVIDENCE,
                "the proposal does not bind this path as evidence",
            ), None
        artifact, reason = _bound_artifact(evidence, proposal, record["path"])
        if reason is not None:
            return refuse_all(
                reason,
                "the bound proposal carries no Twin file artifact for this path",
            ), None
        fingerprint = artifact.get("fingerprint")
        if not isinstance(fingerprint, str) or fingerprint != record["expected_sha256"]:
            return refuse_all(
                REASON_TWIN_IDENTITY_MISMATCH,
                "the edit pins a predecessor the Twin does not record for this artifact",
            ), None
        record["authorized"] = True

    # -- read the predecessors --------------------------------------------
    try:
        repository_real = os.path.realpath(os.path.abspath(repository_root))
    except (TypeError, ValueError):
        return None, REASON_PREDECESSOR_ESCAPES
    if not os.path.isdir(repository_real):
        return None, REASON_PREDECESSOR_ESCAPES

    predecessors: Dict[str, Dict[str, Any]] = {}
    texts = {op["path"]: op["text"] for op in edit["operations"]}
    oversized = False
    unsupported = False
    for record in records:
        predecessor, reason = _read_predecessor(repository_real, record["path"])
        if reason == REASON_OVERSIZED:
            oversized = True
            record["reason_code"] = REASON_OVERSIZED
            record["reason"] = "the predecessor is larger than the accepted bound"
            continue
        if reason == REASON_UNSUPPORTED_CONTENT:
            unsupported = True
            record["reason_code"] = REASON_UNSUPPORTED_CONTENT
            record["reason"] = "the predecessor is not content this contract reviews as text"
            continue
        if reason is not None:
            return refuse_all(reason, "the predecessor could not be read under its identity"), None
        if predecessor["sha256"] != record["expected_sha256"]:
            return refuse_all(
                REASON_PREDECESSOR_STALE,
                "the predecessor on disk does not have the expected content",
            ), None

        replacement_text = texts[record["path"]]
        if not reviewable_text(replacement_text):
            unsupported = True
            record["reason_code"] = REASON_UNSUPPORTED_CONTENT
            record["reason"] = "the replacement is not content this contract reviews as text"
            continue
        diff_lines, diff_reason = candidate_diff.render_unified(
            record["path"],
            predecessor["text"],
            replacement_text,
            max_lines=MAX_DIFF_LINES,
        )
        if diff_reason is not None:
            oversized = True
            record["reason_code"] = REASON_OVERSIZED
            record["reason"] = diff_reason
            continue

        record["before_sha256"] = predecessor["sha256"]
        record["before_bytes"] = predecessor["size"]
        record["after_sha256"] = sha256_hex(replacement_text.encode("utf-8"))
        record["after_bytes"] = len(replacement_text.encode("utf-8"))
        record["before_final_newline"] = candidate_diff.final_newline(predecessor["text"])
        record["after_final_newline"] = candidate_diff.final_newline(replacement_text)
        record["diff"] = diff_lines
        predecessors[record["path"]] = predecessor

    # -- the terminal state ------------------------------------------------
    if oversized:
        return (
            _envelope(
                STATE_OVERSIZED,
                REASON_OVERSIZED,
                "the request, a predecessor or a rendered diff exceeds an accepted bound",
                binding,
                records,
            ),
            None,
        )
    if unsupported:
        return (
            _envelope(
                STATE_UNSUPPORTED_CONTENT,
                REASON_UNSUPPORTED_CONTENT,
                "a file the proposal binds is not reviewable UTF-8 text",
                binding,
                records,
            ),
            None,
        )

    changed = [r for r in records if r["diff"]]
    if not changed:
        return (
            _envelope(
                STATE_NO_CHANGE,
                REASON_NO_CHANGE,
                "every replacement equals its predecessor, so no candidate was produced",
                binding,
                records,
            ),
            None,
        )

    # -- identity ----------------------------------------------------------
    manifest = _assemble_manifest(records, edit, intent_delta_doc, proposal)

    if not materialize:
        return (
            _envelope(
                STATE_CANDIDATE_READY,
                REASON_READY,
                "the candidate is ready; no output root was created",
                binding,
                records,
                candidate_id=manifest["candidate_id"],
            ),
            None,
        )

    # -- materialize -------------------------------------------------------
    base, reason = _validate_output_base(repository_real, output_base)
    if reason is not None:
        return refuse_all(reason, "the output base is not usable for this candidate"), None

    # The predecessor is re-checked after the whole candidate has been computed
    # and immediately before anything is claimed. This narrows, and does not
    # pretend to close, the window in which the repository can move under a
    # build; a candidate is a review artifact and is never applied.
    for path, predecessor in predecessors.items():
        if _predecessor_moved(repository_real, path, predecessor):
            return refuse_all(
                REASON_PREDECESSOR_MOVED,
                "a predecessor changed while the candidate was being built",
            ), None

    entries = [(record["path"], texts[record["path"]].encode("utf-8")) for record in changed]
    root_name, reason = _stage(base, manifest, entries)
    if reason is not None:
        return refuse_all(reason, "the candidate could not be staged and was removed"), None

    for path, predecessor in predecessors.items():
        if _predecessor_moved(repository_real, path, predecessor):
            _discard_tree(base, os.path.join(base, root_name))
            return refuse_all(
                REASON_PREDECESSOR_MOVED,
                "a predecessor changed while the candidate was being built",
            ), None

    return (
        _envelope(
            STATE_CANDIDATE_READY,
            REASON_READY,
            "the candidate is materialized under a fresh output root; it is not "
            "validated, approved or adopted",
            binding,
            records,
            candidate_id=manifest["candidate_id"],
            root_name=root_name,
        ),
        None,
    )


def validate_candidate_manifest(manifest: Any) -> Optional[str]:
    """Validate a candidate manifest against the P5.4 schema, or return a reason."""
    if not isinstance(manifest, dict):
        return "candidate manifest is not a mapping"
    if manifest.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        return "unsupported schema_version"
    if manifest.get("generator") != CANDIDATE_GENERATOR:
        return "unknown candidate generator"
    if manifest.get("state") != STATE_CANDIDATE_READY:
        return "candidate manifest is not for a ready candidate"
    for field in ("edit_id", "intent_delta_id", "proposal_id", "binding_fingerprint"):
        if not isinstance(manifest.get(field), str):
            return "missing or malformed %s" % field
    for field in ("operations", "files"):
        if not isinstance(manifest.get(field), list) or not manifest[field]:
            return "missing or malformed %s" % field
    staged = {entry["path"] for entry in manifest["files"]}
    changed = {op["path"] for op in manifest["operations"] if op["diff"]}
    if staged != changed:
        return "the staged files do not match the changed operations"
    identity = manifest.get("candidate_id")
    if not isinstance(identity, str) or not identity.startswith(CANDIDATE_ID_PREFIX):
        return "missing or malformed candidate_id"
    if identity != candidate_id_for(manifest):
        return "candidate_id does not match the candidate content"
    return None


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "CANDIDATE_GENERATOR",
    "CANDIDATE_REVIEW_GENERATOR",
    "CANDIDATE_ID_PREFIX",
    "MANIFEST_NAME",
    "FILES_DIR",
    "STATE_CANDIDATE_READY",
    "STATE_NO_CHANGE",
    "STATE_UNSUPPORTED_CONTENT",
    "STATE_OVERSIZED",
    "STATE_REFUSED",
    "CANDIDATE_STATES",
    "AUTHORIZING_PROPOSAL_STATES",
    "REASON_EDIT_NOT_VALID",
    "REASON_DELTA_NOT_VALID",
    "REASON_PROPOSAL_NOT_VALID",
    "REASON_EVIDENCE_UNBOUND",
    "REASON_PROPOSAL_NOT_REBOUND",
    "REASON_DELTA_MISMATCH",
    "REASON_PROPOSAL_MISMATCH",
    "REASON_BINDING_MISMATCH",
    "REASON_BASELINE_MISMATCH",
    "REASON_TARGET_NOT_IN_SCOPE",
    "REASON_TARGET_NOT_IN_EVIDENCE",
    "REASON_PROPOSAL_NOT_AUTHORIZING",
    "REASON_TWIN_IDENTITY_ABSENT",
    "REASON_TWIN_IDENTITY_MISMATCH",
    "REASON_TWIN_IDENTITY_UNBOUND",
    "REASON_PREDECESSOR_ABSENT",
    "REASON_PREDECESSOR_NOT_REGULAR",
    "REASON_PREDECESSOR_SYMLINK",
    "REASON_PREDECESSOR_HARDLINK",
    "REASON_PREDECESSOR_ESCAPES",
    "REASON_PREDECESSOR_UNREADABLE",
    "REASON_PREDECESSOR_MOVED",
    "REASON_PREDECESSOR_STALE",
    "REASON_OUTPUT_UNUSABLE",
    "REASON_OUTPUT_INSIDE_REPOSITORY",
    "REASON_OUTPUT_ROOT_TAKEN",
    "REASON_STAGING_FAILED",
    "REASON_VERIFY_FAILED",
    "REASON_READY",
    "REASON_NO_CHANGE",
    "REASON_UNSUPPORTED_CONTENT",
    "REASON_OVERSIZED",
    "dumps",
    "authorized_paths",
    "reviewable_text",
    "candidate_id_for",
    "candidate_root_name",
    "build_candidate",
    "validate_candidate_manifest",
]
