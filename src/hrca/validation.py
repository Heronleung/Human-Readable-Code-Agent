"""Bounded validation evidence for one exact candidate (P5.5a).

Runs a product-owned plan (:mod:`hrca.validation_plan`) against one verified
P5.4 candidate, through the accepted isolated runner, and records what actually
happened — distinctly, immutably, and without ever turning a green result into
an approval.

What this module may do
-----------------------

It reads one candidate root and it writes **only** beneath an evidence base it
is given: one file per attempt, named by that attempt's content-addressed
identity, plus one append-only index line. It never writes to the candidate, to
the accepted repository, to Git, to the Twin or to Memory, and it never applies
anything. It has no provider, credential, network, approval or adoption
authority.

``hrca.candidate`` remains the only writer *in the candidate contract*; this
module is the validation contract's own writer, and its writes are confined to
the evidence base for exactly the same reason: evidence that could touch what it
describes would not be evidence.

Binding before dispatch
-----------------------

Nothing runs until the candidate, its manifest, its review envelope and every
staged byte have been re-read and re-verified, and the resulting binding has
been required to equal the one the plan names. A candidate that moved, a review
that disagrees with the manifest, an extra file in the root, a missing file, a
file whose bytes do not hash to what the manifest recorded — each is a refusal
with its own bounded reason. Nothing is rebased, refreshed, reconstructed or
regenerated: a plan describes one exact candidate or it describes nothing.

Terminal states
---------------

Seven, and only one of them is passing:

``passed``       the check ran, exited zero, and its artifact validated
``failed``       the check ran and its artifact did not satisfy the contract
``timed_out``    the check exceeded the runner's own bound and was killed
``cancelled``    the check was cancelled before dispatch, so nothing ran
``unavailable``  the runtime could not run it at all
``refused``      the check was not dispatched, for a reason this contract named
``unknown``      the check ran but its evidence is missing, unreadable or inconsistent

Missing, truncated, unreadable, inconsistent or unverified evidence is visible
and non-passing. It is never coerced to ``passed``. A result is ``passed`` only
when every check passed; anything else stays explicitly non-passing, and no
state — including ``passed`` — is an approval, an adoption or an application.

Truthfulness about the runtime
------------------------------

The attempt records the isolation facts derived from the **actual dispatched
argv**: that the network was disabled, the root filesystem read-only, the user
non-root, the capabilities dropped, the input mount read-only, and that there
were exactly two mounts, both to staged directories. When the runtime is
unavailable — which is what this environment reports, since no Docker daemon is
reachable — the check is ``unavailable`` and carries the runner's own bounded
preflight reason. No state is invented to fill the gap.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import (
    app_package,
    candidate,
    candidate_edit,
    container_runner,
    validation_plan,
    validation_policy,
)
from .identity import sha256_hex

VALIDATION_ATTEMPT_SCHEMA_VERSION = "1.0.0"
VALIDATION_ATTEMPT_GENERATOR = "hrca-validation-attempt"
VALIDATION_RESULT_SCHEMA_VERSION = "1.0.0"
VALIDATION_RESULT_GENERATOR = "hrca-validation-result"
ATTEMPT_ID_PREFIX = "attempt:"
RESULT_ID_PREFIX = "result:"

STATE_PASSED = "passed"
STATE_FAILED = "failed"
STATE_TIMED_OUT = "timed_out"
STATE_CANCELLED = "cancelled"
STATE_UNAVAILABLE = "unavailable"
STATE_REFUSED = "refused"
STATE_UNKNOWN = "unknown"
CHECK_STATES = frozenset(
    {
        STATE_PASSED,
        STATE_FAILED,
        STATE_TIMED_OUT,
        STATE_CANCELLED,
        STATE_UNAVAILABLE,
        STATE_REFUSED,
        STATE_UNKNOWN,
    }
)

# Severity order. The overall state is the first of these that any check holds,
# so a single failure can never be averaged away by passing neighbours.
_SEVERITY = (
    STATE_FAILED,
    STATE_TIMED_OUT,
    STATE_CANCELLED,
    STATE_REFUSED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    STATE_PASSED,
)

# The runner's own error tokens, mapped onto this contract's states. The runner
# is not modified and its vocabulary is not replaced; this is the one place the
# two are related.
_RUNNER_ERROR_STATES = {
    app_package.STATE_TIMEOUT: STATE_TIMED_OUT,
    app_package.STATE_RUNNER_FAILED: STATE_FAILED,
    app_package.STATE_INPUT_INVALID: STATE_FAILED,
    app_package.STATE_OUTPUT_INVALID: STATE_UNKNOWN,
}

# The candidate path shares the runner's run tokens and adds its own refusals:
# a digest that is absent or does not match, a candidate root that is not the
# verified shape, and a declaration that is not safe. All of them mean nothing
# was dispatched, so all of them are ``refused``.
_CANDIDATE_ERROR_STATES = {
    app_package.STATE_TIMEOUT: STATE_TIMED_OUT,
    app_package.STATE_RUNNER_FAILED: STATE_FAILED,
    app_package.STATE_OUTPUT_INVALID: STATE_UNKNOWN,
    app_package.STATE_INPUT_INVALID: STATE_REFUSED,
    container_runner.REASON_DIGEST_ABSENT: STATE_REFUSED,
    container_runner.REASON_DIGEST_MISMATCH: STATE_REFUSED,
    container_runner.REASON_CANDIDATE_ROOT_INVALID: STATE_REFUSED,
    container_runner.REASON_DECLARED_FILES_INVALID: STATE_REFUSED,
}

# The artifact the in-image syntax entrypoint returns, and the contract it must
# satisfy before any outcome can be called passing.
SYNTAX_ARTIFACT_SCHEMA = "hrca-syntax-check/1"

REASON_NO_CANDIDATE_ROOT = "the plan names no candidate root to check"
REASON_SYNTAX_ARTIFACT_INVALID = "the syntax artifact does not satisfy its contract"

# Bounded refusals.
REASON_PLAN_INVALID = "validation plan is not valid"
REASON_CANDIDATE_MISSING = "the candidate root could not be read"
REASON_CANDIDATE_MISMATCH = "the candidate does not match the plan binding"
REASON_MANIFEST_INVALID = "the candidate manifest is not valid"
REASON_REVIEW_INVALID = "the review envelope does not describe this candidate"
REASON_ROOT_HAS_EXTRA_ENTRIES = "the candidate root holds something the manifest does not"
REASON_FILE_MISSING = "a manifest file is absent from the candidate root"
REASON_FILE_NOT_REGULAR = "a manifest entry is not a regular file"
REASON_FILE_LINKED = "a manifest entry is a link"
REASON_FILE_SIZE = "a manifest file does not have the recorded size"
REASON_FILE_HASH = "a manifest file does not have the recorded content"
REASON_POLICY_INVALID = "the code-owned policy contradicts the accepted package set"
REASON_EVIDENCE_UNUSABLE = "the evidence base is not usable"
REASON_EVIDENCE_TAMPERED = "recorded evidence does not match its identity"
REASON_SHELL_STRING = "a shell string was supplied where a plan was expected"

MAX_ATTEMPTS = 64
MAX_EVIDENCE_BYTES = 256 * 1024

# The complete mutation surface this contract names. Every value is always
# ``False``; the block exists so a reviewer and a test can assert the whole
# surface at once rather than trusting a docstring.
_MUTATION_SURFACE_KEYS = (
    "accepted_source",
    "candidate",
    "git_index",
    "git_ref",
    "branch",
    "commit",
    "worktree",
    "twin_state",
    "memory",
    "approval",
    "adoption",
    "application",
    "provider_request",
    "credential",
    "network",
    "remote",
    "protocol_action",
    "ui",
)

_LIMITATIONS = (
    "validation evidence is not an approval, an adoption or an application; "
    "nothing here changes the accepted repository",
    "the checks are the product's own fixed policy, not a test suite derived "
    "from the candidate's content",
    "cancellation is cooperative and pre-dispatch: this contract has no way to "
    "interrupt a container already running, and the runner's own timeout kill "
    "is the only post-dispatch stop it has",
)

_OUTCOME_FILENAME = "output.json"
_MANIFEST_NAME = candidate.MANIFEST_NAME
_FILES_DIR = candidate.FILES_DIR


def dumps(obj: Any) -> str:
    """Serialize an attempt, result or binding canonically."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _mutation_surface() -> Dict[str, bool]:
    return {key: False for key in _MUTATION_SURFACE_KEYS}


def _bounded(text: Any, limit: int = 200) -> str:
    if not isinstance(text, str):
        return ""
    return text[:limit]


# -- candidate binding ------------------------------------------------------


def _read_manifest(
    candidate_root: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], Optional[str]]:
    path = os.path.join(candidate_root, _MANIFEST_NAME)
    try:
        info = os.lstat(path)
    except OSError:
        return None, None, REASON_CANDIDATE_MISSING
    if not stat.S_ISREG(info.st_mode) or os.path.islink(path):
        return None, None, REASON_CANDIDATE_MISSING
    if info.st_size > MAX_EVIDENCE_BYTES:
        return None, None, REASON_MANIFEST_INVALID
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_EVIDENCE_BYTES + 1)
    except OSError:
        return None, None, REASON_CANDIDATE_MISSING
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None, REASON_MANIFEST_INVALID
    return manifest, data, None


def _staged_entries(candidate_root: str) -> Optional[set]:
    """Return the repository-relative paths present under ``files/``, or None."""
    content = os.path.join(candidate_root, _FILES_DIR)
    if not os.path.isdir(content):
        return None
    found = set()
    for dirpath, dirnames, filenames in os.walk(content):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, content).replace(os.sep, "/")
            found.add(rel)
    return found


def verify_candidate(
    candidate_root: Any, review: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Re-read one candidate and return its exact binding, or a bounded refusal.

    Every check here is a refusal rather than a repair: the manifest is
    re-validated as a P5.4 manifest, the root is re-read byte for byte and
    required to hold exactly what the manifest records and nothing else, and the
    review envelope is required to describe this candidate and no other.
    """
    if not isinstance(candidate_root, str) or not candidate_root:
        return None, REASON_CANDIDATE_MISSING
    if not os.path.isdir(candidate_root):
        return None, REASON_CANDIDATE_MISSING

    manifest, manifest_bytes, reason = _read_manifest(candidate_root)
    if reason is not None:
        return None, reason
    if candidate.validate_candidate_manifest(manifest) is not None:
        return None, REASON_MANIFEST_INVALID

    # The root is *named from* the identity, so a directory whose name is not
    # the one this identity owns is not this candidate's root.
    root_name = os.path.basename(os.path.normpath(candidate_root))
    expected_name = candidate.candidate_root_name(manifest["candidate_id"])
    if root_name != expected_name:
        return None, REASON_CANDIDATE_MISMATCH

    declared = {entry["path"]: entry for entry in manifest["files"]}
    present = _staged_entries(candidate_root)
    if present is None:
        return None, REASON_CANDIDATE_MISSING
    if present - set(declared):
        return None, REASON_ROOT_HAS_EXTRA_ENTRIES
    if set(declared) - present:
        return None, REASON_FILE_MISSING
    top = sorted(os.listdir(candidate_root))
    if top != sorted([_FILES_DIR, _MANIFEST_NAME]):
        return None, REASON_ROOT_HAS_EXTRA_ENTRIES

    for path, entry in sorted(declared.items()):
        full = os.path.join(candidate_root, _FILES_DIR, *path.split("/"))
        try:
            info = os.lstat(full)
        except OSError:
            return None, REASON_FILE_MISSING
        if os.path.islink(full) or not stat.S_ISREG(info.st_mode):
            return None, REASON_FILE_NOT_REGULAR
        if info.st_nlink != 1:
            return None, REASON_FILE_LINKED
        if info.st_size != entry["bytes"]:
            return None, REASON_FILE_SIZE
        try:
            with open(full, "rb") as handle:
                data = handle.read(MAX_EVIDENCE_BYTES + 1)
        except OSError:
            return None, REASON_FILE_MISSING
        if sha256_hex(data) != entry["sha256"]:
            return None, REASON_FILE_HASH

    reason = _verify_review(review, manifest, root_name)
    if reason is not None:
        return None, reason

    binding = {
        "candidate_id": manifest["candidate_id"],
        "candidate_root_name": root_name,
        # The bytes on disk, not a re-serialization of them: the binding is to
        # what a reviewer can read, and a re-serialization could differ from it.
        "manifest_sha256": sha256_hex(manifest_bytes),
        "manifest_bytes": len(manifest_bytes),
        "review_sha256": sha256_hex(dumps(review).encode("utf-8")),
        "review_bytes": len(dumps(review)),
        "edit_id": manifest["edit_id"],
        "intent_delta_id": manifest["intent_delta_id"],
        "proposal_id": manifest["proposal_id"],
        "binding_fingerprint": manifest["binding_fingerprint"],
        "baseline": dict(manifest["baseline"]),
        "files": [
            {"path": entry["path"], "sha256": entry["sha256"], "bytes": entry["bytes"]}
            for entry in sorted(manifest["files"], key=lambda item: item["path"])
        ],
    }
    return binding, None


def _verify_review(review: Any, manifest: Dict[str, Any], root_name: str) -> Optional[str]:
    """Return a reason when the review envelope does not describe this candidate."""
    if not isinstance(review, dict):
        return REASON_REVIEW_INVALID
    if review.get("schema_version") != candidate.CANDIDATE_SCHEMA_VERSION:
        return REASON_REVIEW_INVALID
    if review.get("generator") != candidate.CANDIDATE_REVIEW_GENERATOR:
        return REASON_REVIEW_INVALID
    if review.get("state") != candidate.STATE_CANDIDATE_READY:
        return REASON_REVIEW_INVALID
    if review.get("candidate_id") != manifest["candidate_id"]:
        return REASON_REVIEW_INVALID
    if review.get("candidate_root_name") != root_name:
        return REASON_REVIEW_INVALID
    for field in ("executable", "applied", "advisory"):
        expected = True if field == "advisory" else False
        if review.get(field) is not expected:
            return REASON_REVIEW_INVALID
    for field in ("validated", "approved", "adopted"):
        if review.get(field) is not False:
            return REASON_REVIEW_INVALID
    surface = review.get("mutation_surface")
    if not isinstance(surface, dict) or any(value is not False for value in surface.values()):
        return REASON_REVIEW_INVALID
    if review.get("binding", {}).get("edit_id") != manifest["edit_id"]:
        return REASON_REVIEW_INVALID
    if review.get("binding", {}).get("proposal_id") != manifest["proposal_id"]:
        return REASON_REVIEW_INVALID

    declared = {entry["path"]: entry for entry in manifest["files"]}
    operations = review.get("operations")
    if not isinstance(operations, list):
        return REASON_REVIEW_INVALID
    for record in operations:
        if not isinstance(record, dict):
            return REASON_REVIEW_INVALID
        path = record.get("path")
        if path not in declared:
            return REASON_REVIEW_INVALID
        if record.get("op") != candidate_edit.OP_REPLACE_FILE:
            return REASON_REVIEW_INVALID
        if record.get("after_sha256") != declared[path]["sha256"]:
            return REASON_REVIEW_INVALID
        if record.get("after_bytes") != declared[path]["bytes"]:
            return REASON_REVIEW_INVALID
    return None


# -- isolation facts derived from the dispatched argv -----------------------


def _mount_values(argv: Sequence[str]) -> List[str]:
    return [
        argv[index + 1]
        for index, element in enumerate(argv)
        if element == "--mount" and index + 1 < len(argv)
    ]


def _flag_value(argv: Sequence[str], flag: str) -> Optional[str]:
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def isolation_facts(argv: Sequence[str]) -> Dict[str, Any]:
    """Return what the *actual* dispatched argv proves about isolation.

    Nothing here is a claim about what the runner would do: every value is read
    out of the argv that was handed to the container client, so a run that
    dispatched something else records what it actually dispatched.
    """
    mounts = _mount_values(argv)
    readonly_mounts = [mount for mount in mounts if "readonly" in mount]
    return {
        "network_disabled": _flag_value(argv, "--network") == container_runner.RUNNER_NETWORK,
        "read_only_rootfs": "--read-only" in argv,
        "non_root": _flag_value(argv, "--user") == container_runner.RUNNER_USER,
        "capabilities_dropped": _flag_value(argv, "--cap-drop") == "ALL",
        "no_new_privileges": "no-new-privileges" in argv,
        "resource_bounded": all(
            _flag_value(argv, flag) is not None
            for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit", "--tmpfs")
        ),
        "input_mount_read_only": any(
            "dst=/in" in mount and "readonly" in mount for mount in mounts
        ),
        "mount_count": len(mounts),
        "write_mounts_are_staged_only": len(mounts) == 2
        and len(readonly_mounts) == 1
        and all("dst=/in" in mount or "dst=/out" in mount for mount in mounts),
        "no_docker_socket_mount": not any("docker.sock" in mount for mount in mounts),
        "image_is_code_owned": list(argv).count(container_runner.RUNNER_IMAGE) == 1,
        "removed_after_run": "--rm" in argv,
        "init_reaps_children": "--init" in argv,
    }


def canonical_argv(argv: Sequence[str]) -> List[str]:
    """Return the argv in its reviewable form, with the variable parts redacted.

    The mounted source directories and the container name are fresh, random and
    absolute; recording them would put a location and a host path into a durable
    artifact for no review value. The flags, the image and the mount
    destinations are the reviewed surface, so those are kept verbatim.
    """
    out: List[str] = []
    index = 0
    while index < len(argv):
        element = argv[index]
        if element == "--mount" and index + 1 < len(argv):
            parts = argv[index + 1].split(",")
            redacted = [
                "src=<staged>" if part.startswith("src=") else part for part in parts
            ]
            out.extend(["--mount", ",".join(redacted)])
            index += 2
            continue
        if element == "--name" and index + 1 < len(argv):
            out.extend(["--name", "<container>"])
            index += 2
            continue
        if index == 0:
            out.append("docker")
            index += 1
            continue
        out.append(element)
        index += 1
    return out


class _Recorder:
    """Observe one dispatch through the runner's own injectable ``spawn``.

    The runner keeps ownership of staging, killing and cleanup; this only
    remembers what the runner handed to the container client. That is why the
    facts below are facts about the run rather than an assumption about it.
    """

    def __init__(self, spawn: Callable[..., Any]) -> None:
        self._spawn = spawn
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> Any:
        record: Dict[str, Any] = {"argv": list(argv), "returncode": None, "timed_out": False}
        self.calls.append(record)
        try:
            proc = self._spawn(argv, **kwargs)
        except (TimeoutError, subprocess.TimeoutExpired):
            # Both spellings, because a real ``subprocess.run(timeout=...)``
            # raises ``TimeoutExpired`` and that is *not* a ``TimeoutError``
            # (checked, not assumed). Recording the fact is all this does: the
            # exception is re-raised untouched so the runner still sees exactly
            # what it saw before, and runs its own timeout lifecycle for it.
            record["timed_out"] = True
            raise
        record["returncode"] = getattr(proc, "returncode", None)
        record["stdout"] = getattr(proc, "stdout", None)
        record["stderr"] = getattr(proc, "stderr", None)
        return proc

    def last_dispatch(self) -> Optional[Dict[str, Any]]:
        """Return the ``docker run`` call, not merely the last call.

        A timeout makes the runner call the client again — ``docker kill`` and
        ``docker rm -f`` — so the last call is the cleanup, not the run. Taking
        the last *dispatch* keeps the recorded facts about the thing that was
        measured rather than about what happened afterwards.
        """
        for record in reversed(self.calls):
            if record["argv"][1:2] == ["run"]:
                return record
        return None


def _stream_facts(data: Any) -> Dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        return {"sha256": None, "bytes": None}
    return {"sha256": sha256_hex(bytes(data)), "bytes": len(data)}


# -- one check --------------------------------------------------------------


def attempt_id_for(attempt: Dict[str, Any]) -> str:
    """Return the content-addressed identity of an attempt."""
    canon = dumps({k: v for k, v in attempt.items() if k != "attempt_id"})
    return ATTEMPT_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def _attempt(
    plan: Dict[str, Any],
    check: Dict[str, Any],
    ordinal: int,
    state: str,
    *,
    limitation: Optional[str] = None,
    dispatch: Optional[Dict[str, Any]] = None,
    artifact: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    argv = dispatch["argv"] if dispatch else None
    stdout = dispatch.get("stdout") if dispatch else None
    stderr = dispatch.get("stderr") if dispatch else None
    reviewable = canonical_argv(argv) if argv else None
    attempt: Dict[str, Any] = {
        "schema_version": VALIDATION_ATTEMPT_SCHEMA_VERSION,
        "generator": VALIDATION_ATTEMPT_GENERATOR,
        "attempt_id": "",
        "ordinal": ordinal,
        "plan_id": plan["plan_id"],
        "candidate_id": plan["candidate"]["candidate_id"],
        "policy_version": plan["policy_version"],
        "check_id": check["check_id"],
        "package_id": check.get("package_id"),
        # Which entrypoint ran, and the image digest it was required to observe
        # before it could run. Both are ``None`` for a package check.
        "entrypoint": check.get("entrypoint"),
        "image_digest": check.get("image_digest"),
        "state": state,
        "argv": reviewable,
        "argv_sha256": sha256_hex(dumps(reviewable).encode("utf-8")) if reviewable else None,
        "isolation": isolation_facts(argv) if argv else None,
        "returncode": dispatch.get("returncode") if dispatch else None,
        "timed_out": bool(dispatch and dispatch.get("timed_out")),
        "cancelled": state == STATE_CANCELLED,
        "stdout": _stream_facts(stdout),
        "stderr": _stream_facts(stderr),
        "artifact": artifact,
        # A validation attempt produces evidence. It approves nothing, adopts
        # nothing and applies nothing, whatever its state.
        "approved": False,
        "adopted": False,
        "applied": False,
        "limitations": [limitation] if limitation else [],
    }
    attempt["attempt_id"] = attempt_id_for(attempt)
    return attempt


def _validate_syntax_artifact(result: Any, declared: List[str]) -> Optional[str]:
    """Return a reason when the syntax artifact does not satisfy its contract.

    The artifact must answer for **exactly** the declared files — no more, no
    fewer, none twice — and its counts must agree with its own outcomes. An
    artifact that cannot be checked this way is ``unknown``, never passing.
    """
    if not isinstance(result, dict):
        return REASON_SYNTAX_ARTIFACT_INVALID
    if result.get("schema") != SYNTAX_ARTIFACT_SCHEMA:
        return REASON_SYNTAX_ARTIFACT_INVALID
    checked = result.get("checked")
    if not isinstance(checked, list) or not checked:
        return REASON_SYNTAX_ARTIFACT_INVALID
    paths: List[str] = []
    for entry in checked:
        if not isinstance(entry, dict):
            return REASON_SYNTAX_ARTIFACT_INVALID
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("ok"), bool):
            return REASON_SYNTAX_ARTIFACT_INVALID
        paths.append(entry["path"])
    if sorted(paths) != sorted(set(declared)):
        return REASON_SYNTAX_ARTIFACT_INVALID
    compiled, failed = result.get("compiled"), result.get("failed")
    for value in (compiled, failed):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return REASON_SYNTAX_ARTIFACT_INVALID
    if compiled + failed != len(checked):
        return REASON_SYNTAX_ARTIFACT_INVALID
    if compiled != sum(1 for entry in checked if entry["ok"]):
        return REASON_SYNTAX_ARTIFACT_INVALID
    return None


def _run_candidate_check(
    plan: Dict[str, Any],
    check: Dict[str, Any],
    ordinal: int,
    candidate_root: Any,
    spawn: Callable[..., Any],
    which: Optional[Callable[[str], Optional[str]]],
    cancelled: Optional[Callable[[], bool]],
) -> Dict[str, Any]:
    """Run the candidate syntax check and return its attempt record.

    The gates are the accepted ones, in the accepted order: the plan's binding
    supplied the declared files, the runner preflights, the runner refuses
    unless the local image's immutable ID equals the declared digest, the
    entrypoint compiles exactly those files, and the artifact is checked before
    any outcome is called passing.
    """
    if cancelled is not None and cancelled():
        return _attempt(
            plan, check, ordinal, STATE_CANCELLED,
            limitation="cancelled before dispatch",
        )
    if not isinstance(candidate_root, str) or not candidate_root:
        return _attempt(
            plan, check, ordinal, STATE_REFUSED,
            limitation=REASON_NO_CANDIDATE_ROOT,
        )
    declared = [entry["path"] for entry in plan["candidate"]["files"]]

    recorder = _Recorder(spawn)
    runner = (
        container_runner.ContainerRunner(spawn=recorder, which=which)
        if which is not None
        else container_runner.ContainerRunner(spawn=recorder)
    )
    preflight = runner.preflight()
    if not preflight.get("available"):
        return _attempt(
            plan, check, ordinal, STATE_UNAVAILABLE,
            limitation=preflight.get("reason") or "runtime unavailable",
        )

    try:
        result, error = runner.run_candidate(
            candidate_dir=candidate_root,
            declared_files=declared,
            expected_digest=check["image_digest"],
        )
    except subprocess.TimeoutExpired:
        return _attempt(
            plan, check, ordinal, STATE_TIMED_OUT,
            limitation=(
                "the runner raised a timeout outside its own lifecycle: its kill "
                "and cleanup did not run, so the container and the staged "
                "directories may remain"
            ),
            dispatch=recorder.last_dispatch(),
        )
    dispatch = recorder.last_dispatch()

    if error is not None:
        return _attempt(
            plan, check, ordinal,
            _CANDIDATE_ERROR_STATES.get(error, STATE_UNKNOWN),
            limitation=_bounded(error, 80),
            dispatch=dispatch,
        )
    reason = _validate_syntax_artifact(result, declared)
    if reason is not None:
        return _attempt(
            plan, check, ordinal, STATE_UNKNOWN,
            limitation=reason, dispatch=dispatch,
        )

    compiled = {
        "artifact": {
            "name": check["expected_artifact"],
            "sha256": sha256_hex(dumps(result).encode("utf-8")),
            "bytes": len(dumps(result)),
        }
    }
    if result["failed"]:
        failing = sorted(
            entry["path"] for entry in result["checked"] if not entry["ok"]
        )
        return _attempt(
            plan, check, ordinal, STATE_FAILED,
            limitation="a declared file did not compile: " + ", ".join(failing)[:80],
            dispatch=dispatch, **compiled
        )
    return _attempt(plan, check, ordinal, STATE_PASSED, dispatch=dispatch, **compiled)


def run_check(
    plan: Dict[str, Any],
    check: Dict[str, Any],
    ordinal: int,
    *,
    candidate_root: Any = None,
    spawn: Callable[..., Any] = subprocess.run,
    which: Callable[[str], Optional[str]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Run one plan check and return its attempt record.

    A package check and a candidate check are separate paths with separate
    gates; what they share is the attempt vocabulary and the append-only store,
    which is why one result can hold both and still be read as one document.
    """
    if check.get("kind") == validation_policy.KIND_CANDIDATE_SYNTAX:
        return _run_candidate_check(
            plan, check, ordinal, candidate_root, spawn, which, cancelled
        )

    package = validation_policy.package_for(check["check_id"])
    if package is None:  # pragma: no cover - the plan validator refuses these
        return _attempt(plan, check, ordinal, STATE_REFUSED,
                        limitation=REASON_POLICY_INVALID)

    if cancelled is not None and cancelled():
        return _attempt(plan, check, ordinal, STATE_CANCELLED,
                        limitation="cancelled before dispatch")

    reason = app_package.validate_form_input(package, check["form_input"])
    if reason is not None:
        return _attempt(plan, check, ordinal, STATE_REFUSED,
                        limitation=REASON_POLICY_INVALID)

    # One runner, wrapped in the recorder, so the argv that preflight and the
    # run actually dispatched is what gets recorded. The runner keeps ownership
    # of staging, killing and cleanup; this only remembers.
    recorder = _Recorder(spawn)
    runner = (
        container_runner.ContainerRunner(spawn=recorder, which=which)
        if which is not None
        else container_runner.ContainerRunner(spawn=recorder)
    )

    preflight = runner.preflight()
    if not preflight.get("available"):
        return _attempt(
            plan, check, ordinal, STATE_UNAVAILABLE,
            limitation=preflight.get("reason") or "runtime unavailable",
        )

    try:
        result, error = runner.run(
            handler=package["handler"],
            input_payload=check["form_input"],
            parameters=check["parameters"],
        )
    except subprocess.TimeoutExpired:
        # A guard, not a path. The accepted runner catches ``TimeoutExpired``
        # itself and runs its whole timeout lifecycle, so a real timeout arrives
        # here as the ``timeout`` token rather than as an exception. This branch
        # exists so that a regression in the runner's own handling still yields a
        # bounded, non-passing state instead of an escaping exception; reaching
        # it means the runner leaked a timeout, which is why the limitation says
        # the lifecycle did not run.
        return _attempt(
            plan, check, ordinal, STATE_TIMED_OUT,
            limitation=(
                "the runner raised a timeout outside its own lifecycle: its kill "
                "and cleanup did not run, so the container and the staged "
                "directories may remain"
            ),
            dispatch=recorder.last_dispatch(),
        )
    dispatch = recorder.last_dispatch()

    if error is not None:
        return _attempt(
            plan, check, ordinal, _RUNNER_ERROR_STATES.get(error, STATE_UNKNOWN),
            limitation=error, dispatch=dispatch,
        )

    reason = app_package.validate_result(package, result)
    if reason is not None:
        # The package's own bounded reason, never the artifact's content: an
        # `unknown` a reviewer cannot diagnose is not much better than a pass.
        return _attempt(
            plan, check, ordinal, STATE_UNKNOWN,
            limitation="the collected artifact does not satisfy its package schema: "
            + _bounded(reason, 80),
            dispatch=dispatch,
        )

    canonical = dumps(result)
    return _attempt(
        plan, check, ordinal, STATE_PASSED,
        dispatch=dispatch,
        artifact={
            "name": check["expected_artifact"],
            "sha256": sha256_hex(canonical.encode("utf-8")),
            "bytes": len(canonical),
        },
    )


# -- the whole plan ---------------------------------------------------------


def result_id_for(result: Dict[str, Any]) -> str:
    canon = dumps({k: v for k, v in result.items() if k != "result_id"})
    return RESULT_ID_PREFIX + sha256_hex(canon.encode("utf-8"))


def _overall_state(states: Sequence[str]) -> str:
    for state in _SEVERITY:
        if state in states:
            return state
    return STATE_UNKNOWN  # pragma: no cover - an empty check list is refused


def run_plan(
    plan: Any,
    candidate_root: Any,
    review: Any,
    *,
    ordinal: int = 1,
    spawn: Callable[..., Any] = subprocess.run,
    which: Optional[Callable[[str], Optional[str]]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Verify, dispatch every check, and return ``(result, error)``.

    A refusal is ``(None, reason)`` and means nothing was dispatched. Otherwise
    the result carries every attempt, and its state is the most severe state any
    check reached.
    """
    if isinstance(plan, str):
        # A plan is never a string. A string here is the clearest possible sign
        # that something upstream believed it could supply a command.
        return None, REASON_SHELL_STRING
    reason = validation_plan.validate_plan(plan)
    if reason is not None:
        return None, REASON_PLAN_INVALID
    reason = validation_policy.validate_policy()
    if reason is not None:
        return None, REASON_POLICY_INVALID

    binding, reason = verify_candidate(candidate_root, review)
    if reason is not None:
        return None, reason
    if binding != plan["candidate"]:
        return None, REASON_CANDIDATE_MISMATCH

    attempts = [
        run_check(
            plan, check, ordinal,
            candidate_root=candidate_root,
            spawn=spawn, which=which, cancelled=cancelled,
        )
        for check in plan["checks"]
    ]
    states = [attempt["state"] for attempt in attempts]
    overall = _overall_state(states)
    result: Dict[str, Any] = {
        "schema_version": VALIDATION_RESULT_SCHEMA_VERSION,
        "generator": VALIDATION_RESULT_GENERATOR,
        "result_id": "",
        "plan_id": plan["plan_id"],
        "candidate_id": binding["candidate_id"],
        "policy_version": plan["policy_version"],
        "state": overall,
        # Complete, valid passing evidence — and nothing less — is what makes a
        # result passing. It is still not an approval.
        "evidence_complete": overall == STATE_PASSED,
        "checks": [
            {
                "check_id": attempt["check_id"],
                "attempt_id": attempt["attempt_id"],
                "state": attempt["state"],
            }
            for attempt in attempts
        ],
        # The attempts themselves, so the result is a self-contained evidence
        # bundle rather than a summary that points at evidence it does not carry.
        "attempts": attempts,
        "approved": False,
        "adopted": False,
        "applied": False,
        "mutation_surface": _mutation_surface(),
        "limitations": list(_LIMITATIONS),
    }
    result["result_id"] = result_id_for(result)
    return result, None


# -- the append-only evidence store ----------------------------------------


def _evidence_dir(base: str) -> str:
    return os.path.join(base, "attempts")


def _append_line(path: str, line: str) -> Optional[str]:
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        return REASON_EVIDENCE_UNUSABLE
    return None


def _write_new(path: str, data: bytes) -> Optional[str]:
    """Write ``data`` at ``path`` with the repository's atomic pattern.

    An existing file is never overwritten: if it already holds exactly these
    bytes the write is idempotent, and if it holds anything else the caller is
    looking at a different attempt under the same name, which is refused.
    """
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return REASON_EVIDENCE_UNUSABLE
    if os.path.exists(path):
        try:
            with open(path, "rb") as handle:
                if handle.read() == data:
                    return None
        except OSError:
            return REASON_EVIDENCE_UNUSABLE
        return REASON_EVIDENCE_TAMPERED
    handle = None
    temp_path = None
    try:
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".hrca-attempt-", suffix=".tmp", dir=directory
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
        return REASON_EVIDENCE_UNUSABLE
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


def record_attempt(base: Any, attempt: Any) -> Tuple[Optional[str], Optional[str]]:
    """Append one attempt to the evidence base; return ``(attempt_id, reason)``."""
    if not isinstance(base, str) or not base:
        return None, REASON_EVIDENCE_UNUSABLE
    if not isinstance(attempt, dict):
        return None, REASON_EVIDENCE_TAMPERED
    identity = attempt.get("attempt_id")
    if not isinstance(identity, str) or not identity.startswith(ATTEMPT_ID_PREFIX):
        return None, REASON_EVIDENCE_TAMPERED
    if identity != attempt_id_for(attempt):
        return None, REASON_EVIDENCE_TAMPERED
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        return None, REASON_EVIDENCE_UNUSABLE

    path = os.path.join(_evidence_dir(base), identity.split(":", 1)[1] + ".json")
    reason = _write_new(path, dumps(attempt).encode("utf-8"))
    if reason is not None:
        return None, reason
    reason = _append_line(
        os.path.join(base, "index.jsonl"),
        dumps({"attempt_id": identity, "ordinal": attempt.get("ordinal"),
               "check_id": attempt.get("check_id"), "state": attempt.get("state"),
               "plan_id": attempt.get("plan_id")}),
    )
    if reason is not None:
        return None, reason
    return identity, None


def read_attempts(base: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Read the evidence base back, re-verifying every attempt's identity."""
    if not isinstance(base, str) or not base:
        return None, REASON_EVIDENCE_UNUSABLE
    index_path = os.path.join(base, "index.jsonl")
    try:
        with open(index_path, encoding="utf-8") as handle:
            lines = handle.read(MAX_EVIDENCE_BYTES + 1).splitlines()
    except OSError:
        return None, REASON_EVIDENCE_UNUSABLE
    if len(lines) > MAX_ATTEMPTS:
        return None, REASON_EVIDENCE_TAMPERED

    attempts: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            identity = entry["attempt_id"]
        except (ValueError, KeyError, TypeError):
            return None, REASON_EVIDENCE_TAMPERED
        path = os.path.join(_evidence_dir(base), identity.split(":", 1)[1] + ".json")
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_EVIDENCE_BYTES + 1)
            attempt = json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None, REASON_EVIDENCE_TAMPERED
        if attempt.get("attempt_id") != identity or attempt_id_for(attempt) != identity:
            return None, REASON_EVIDENCE_TAMPERED
        attempts.append(attempt)
    return attempts, None


def next_ordinal(base: Any) -> int:
    """Return the next ordinal for this evidence base, so repeats are distinct."""
    attempts, _reason = read_attempts(base)
    if not attempts:
        return 1
    return max(int(attempt.get("ordinal") or 0) for attempt in attempts) + 1


def run_and_record(
    plan: Any,
    candidate_root: Any,
    review: Any,
    base: Any,
    **kwargs: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run a plan and append each attempt to the evidence base.

    Every attempt is recorded whether it passed or not: a failure that leaves no
    evidence is exactly the outcome this contract exists to avoid.
    """
    ordinal = kwargs.pop("ordinal", None)
    if ordinal is None:
        ordinal = next_ordinal(base)
    result, error = run_plan(plan, candidate_root, review, ordinal=ordinal, **kwargs)
    if error is not None:
        return None, error

    for attempt in result["attempts"]:
        _identity, reason = record_attempt(base, attempt)
        if reason is not None:
            return None, reason
    return result, None


def validate_attempt(attempt: Any) -> Optional[str]:
    """Validate an attempt record against the P5.5a schema."""
    if not isinstance(attempt, dict):
        return "attempt is not a mapping"
    if attempt.get("schema_version") != VALIDATION_ATTEMPT_SCHEMA_VERSION:
        return "unsupported schema_version"
    if attempt.get("generator") != VALIDATION_ATTEMPT_GENERATOR:
        return "unknown attempt generator"
    if attempt.get("state") not in CHECK_STATES:
        return "unknown check state"
    for field in ("approved", "adopted", "applied"):
        if attempt.get(field) is not False:
            return "attempt must not approve, adopt or apply"
    ordinal = attempt.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        return "invalid ordinal"
    identity = attempt.get("attempt_id")
    if not isinstance(identity, str) or not identity.startswith(ATTEMPT_ID_PREFIX):
        return "missing or malformed attempt_id"
    if identity != attempt_id_for(attempt):
        return "attempt_id does not match the attempt content"
    return None


def validate_result(result: Any) -> Optional[str]:
    """Validate a result record against the P5.5a schema."""
    if not isinstance(result, dict):
        return "result is not a mapping"
    if result.get("schema_version") != VALIDATION_RESULT_SCHEMA_VERSION:
        return "unsupported schema_version"
    if result.get("generator") != VALIDATION_RESULT_GENERATOR:
        return "unknown result generator"
    if result.get("state") not in CHECK_STATES:
        return "unknown result state"
    for field in ("approved", "adopted", "applied"):
        if result.get(field) is not False:
            return "result must not approve, adopt or apply"
    if result.get("evidence_complete") is not (result.get("state") == STATE_PASSED):
        return "evidence_complete does not match the overall state"
    surface = result.get("mutation_surface")
    if not isinstance(surface, dict):
        return "missing or malformed mutation_surface"
    if set(surface) != set(_MUTATION_SURFACE_KEYS):
        return "mutation_surface does not declare every named boundary"
    for key, value in sorted(surface.items()):
        if value is not False:
            return "mutation_surface declares a non-false %s" % key
    checks = result.get("checks")
    if not isinstance(checks, list) or not checks:
        return "result carries no checks"
    attempts = result.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != len(checks):
        return "result does not carry one attempt per check"
    for attempt in attempts:
        reason = validate_attempt(attempt)
        if reason is not None:
            return "a result attempt is not valid"
    identity = result.get("result_id")
    if not isinstance(identity, str) or not identity.startswith(RESULT_ID_PREFIX):
        return "missing or malformed result_id"
    if identity != result_id_for(result):
        return "result_id does not match the result content"
    return None


__all__ = [
    "VALIDATION_ATTEMPT_SCHEMA_VERSION",
    "VALIDATION_ATTEMPT_GENERATOR",
    "VALIDATION_RESULT_SCHEMA_VERSION",
    "VALIDATION_RESULT_GENERATOR",
    "ATTEMPT_ID_PREFIX",
    "RESULT_ID_PREFIX",
    "STATE_PASSED",
    "STATE_FAILED",
    "STATE_TIMED_OUT",
    "STATE_CANCELLED",
    "STATE_UNAVAILABLE",
    "STATE_REFUSED",
    "STATE_UNKNOWN",
    "CHECK_STATES",
    "REASON_PLAN_INVALID",
    "REASON_CANDIDATE_MISSING",
    "REASON_CANDIDATE_MISMATCH",
    "REASON_MANIFEST_INVALID",
    "REASON_REVIEW_INVALID",
    "REASON_ROOT_HAS_EXTRA_ENTRIES",
    "REASON_FILE_MISSING",
    "REASON_FILE_NOT_REGULAR",
    "REASON_FILE_LINKED",
    "REASON_FILE_SIZE",
    "REASON_FILE_HASH",
    "REASON_POLICY_INVALID",
    "REASON_EVIDENCE_UNUSABLE",
    "REASON_EVIDENCE_TAMPERED",
    "REASON_SHELL_STRING",
    "SYNTAX_ARTIFACT_SCHEMA",
    "REASON_NO_CANDIDATE_ROOT",
    "REASON_SYNTAX_ARTIFACT_INVALID",
    "dumps",
    "verify_candidate",
    "isolation_facts",
    "canonical_argv",
    "attempt_id_for",
    "result_id_for",
    "run_check",
    "run_plan",
    "run_and_record",
    "record_attempt",
    "read_attempts",
    "next_ordinal",
    "validate_attempt",
    "validate_result",
]
