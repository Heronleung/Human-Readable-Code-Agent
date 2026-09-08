"""Headless local application boundary (P3.2).

The boundary is the single place that turns a versioned desktop-to-core request
into the deterministic read-only result. It:

* reads newline-delimited JSON requests from stdin,
* validates each request and rejects unknown contract versions and
  non-allowlisted actions with bounded, sanitized errors,
* dispatches scan-pipeline actions (``scan`` / ``read`` / ``analyze`` /
  ``inspect`` / ``plan``) to the task intake (:mod:`hrca.planning`), planner,
  scanner (:mod:`hrca.scanner`) and report builder (:mod:`hrca.report`),
* dispatches workspace actions (``open_project`` / ``get_tree`` /
  ``get_document``) to the read-only filesystem policy in
  :mod:`hrca.workspace`,
* writes exactly one JSON response line per request to stdout.

The boundary is a review and safety *workflow* boundary, not an
operating-system privilege boundary: it owns contract validation, action
allowlisting, the accepted-project-root session, orchestration of the
deterministic core, path containment for workspace access, and bounded error
mapping — but it performs no repository write, Git operation, command
execution, network access, or provider call.

Stdio discipline: stdin and stdout are reconfigured to UTF-8 with newline
``"\\n"``, messages are serialized with ``ensure_ascii=True``, stdout is
reserved exclusively for protocol messages, and every log or diagnostic (there
are none in normal operation) goes to stderr.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TextIO, Sequence

from . import (
    advisory,
    app_package,
    codemap,
    codemap_draft,
    contract,
    credential_store,
    deepseek,
    document,
    proposal,
    provider,
    provider_config,
    twin,
    twin_store,
    version_store,
    workspace,
)
from .planning import TaskValidationError, build_plan, validate_task
from .report import build_report
from .scanner import module_name_for, scan_directory

# Fixed, read-only next action reported by this slice: the boundary never
# performs a repository action.
_NEXT_ACTION = "Report only; no repository action performed."


class WorkspaceSession:
    """In-memory accepted-project state owned by one boundary loop (P3.2/P3.3).

    ``open_project`` sets the accepted root; ``get_tree`` / ``get_document``
    operate relative to it, and the P3.3 Twin actions (``sync_twin`` /
    ``get_twin`` / ``get_anchor``) persist and read the workspace's Twin under
    ``store_base`` (per-workspace app-data outside the accepted root). The
    session is per-``run_loop`` invocation, so a fresh boundary process (or a
    fresh test loop) always starts with no project accepted.
    """

    def __init__(
        self,
        store_base: Optional[str] = None,
        credential_store: Any = None,
        credential_prompt: Any = None,
        advisory_transport: Any = None,
        runner: Any = None,
    ) -> None:
        self.root: Optional[str] = None
        self.store_base: str = store_base or twin_store.app_data_dir()
        # Backend-owned credential store, injected for tests; resolved lazily to
        # the platform store by the readiness handler so a ``--serve`` loop that
        # never queries readiness never constructs (or touches) a credential
        # store.
        self.credential_store = credential_store
        # Backend-owned secure credential prompt, injected for tests; resolved
        # lazily to the platform native prompt by the manage-credential handler.
        self.credential_prompt = credential_prompt
        # Backend-owned advisory transport, injected for tests (a deterministic
        # Provider double). When absent the confirmed plan-advisory handler
        # constructs the real DeepSeek transport lazily.
        self.advisory_transport = advisory_transport
        # Backend-owned isolated runner, injected for tests (a deterministic
        # runner double). When absent the run-package handler constructs the
        # real container runner lazily.
        self.runner = runner

    def open(self, root: str) -> None:
        self.root = root

    def close(self) -> None:
        self.root = None


def _configure_stdio(stream: TextIO) -> TextIO:
    """Reconfigure a standard stream to UTF-8 with ``"\\n"`` newlines.

    ``reconfigure`` exists on real standard streams (not on ``io.StringIO``
    test doubles), so it is applied only when available.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", newline="\n")
    return stream


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the headless boundary loop over stdin/stdout; returns an exit code."""
    _configure_stdio(sys.stdin)
    _configure_stdio(sys.stdout)
    _configure_stdio(sys.stderr)
    # ``argv`` is accepted for parity with other entry points; the ``--serve``
    # sentinel requires no further handling because this module *is* the serve
    # loop whenever it is invoked.
    return run_loop(sys.stdin, sys.stdout, sys.stderr)


def run_loop(
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    store_base: Optional[str] = None,
) -> int:
    """Read requests from ``stdin``, write one response line per request.

    ``stderr`` is accepted for interface parity and reserved for diagnostics;
    the boundary emits none in normal operation. One :class:`WorkspaceSession`
    is shared across the whole loop so ``open_project`` establishes the root
    that later ``get_tree`` / ``get_document`` / Twin requests use.
    ``store_base`` overrides the Twin app-data directory (tests use a temp dir;
    normal operation uses the per-user app-data location).
    """
    session = WorkspaceSession(store_base)
    for raw in stdin:
        line = raw[:-1] if raw.endswith("\n") else raw
        if line == "":
            continue
        if len(line.encode("utf-8")) > contract.MAX_MESSAGE_BYTES:
            _emit(stdout, contract.build_error(None, "message_too_large"))
            continue
        try:
            request = contract.loads(line)
        except (ValueError, UnicodeDecodeError):
            _emit(stdout, contract.build_error(None, "malformed_request"))
            continue
        _emit(stdout, handle_request(request, session))
    return 0


def _emit(stdout: TextIO, payload: Dict[str, Any]) -> None:
    """Write exactly one response line to stdout, then flush.

    A response that would itself exceed the maximum size is replaced with a
    bounded ``message_too_large`` error so stdout never carries an oversized
    line.
    """
    text = contract.dumps(payload)
    if len(text.encode("utf-8")) > contract.MAX_MESSAGE_BYTES:
        text = contract.dumps(contract.build_error(None, "message_too_large"))
    stdout.write(text + "\n")
    stdout.flush()


def _correlation_id(request: Any) -> Optional[str]:
    """Return a valid correlation identifier from ``request``, else ``None``."""
    if isinstance(request, dict):
        cid = request.get("correlation_id")
        if (
            isinstance(cid, str)
            and cid
            and len(cid) <= contract.CORRELATION_ID_MAX_CHARS
        ):
            return cid
    return None


def handle_request(request: Any, session: Optional[WorkspaceSession] = None) -> Dict[str, Any]:
    """Validate and process one request, returning a result or error envelope.

    This is the boundary's single, testable core: it never raises, and always
    returns exactly one envelope. Failures are mapped to bounded codes with
    messages drawn from the contract catalogue, so no caller text or file
    content leaks. ``session`` carries the accepted project root; when omitted
    a fresh session is used so the function remains usable standalone.
    """
    if session is None:
        session = WorkspaceSession()
    correlation_id = _correlation_id(request)
    try:
        return _process(request, session)
    except contract.ContractError as exc:
        return contract.build_error(correlation_id, exc.code)
    except Exception:
        # Internal failures are mapped to a bounded code; the original
        # exception object and its message are never serialized.
        return contract.build_error(correlation_id, "internal_error")


def _process(request: Any, session: WorkspaceSession) -> Dict[str, Any]:
    """Validate and dispatch one request envelope to its read-only handler."""
    if not isinstance(request, dict):
        raise contract.ContractError("invalid_request")

    if request.get("contract_version") != contract.CONTRACT_VERSION:
        raise contract.ContractError("unknown_contract_version")

    action = request.get("action")
    if action not in contract.ALLOWED_ACTIONS:
        raise contract.ContractError("action_not_allowed")

    correlation_id = _correlation_id(request)

    if action in contract.SCAN_ACTIONS:
        result = _scan_result(request)
    elif action == contract.ACTION_OPEN_PROJECT:
        result = _open_project_result(request, session)
    elif action == contract.ACTION_GET_TREE:
        result = _get_tree_result(request, session)
    elif action == contract.ACTION_GET_DOCUMENT:
        result = _get_document_result(request, session)
    elif action == contract.ACTION_SYNC_TWIN:
        result = _sync_twin_result(request, session)
    elif action == contract.ACTION_GET_TWIN:
        result = _get_twin_result(request, session)
    elif action == contract.ACTION_GET_ANCHOR:
        result = _get_anchor_result(request, session)
    elif action == contract.ACTION_GET_CODE_MAP:
        result = _get_code_map_result(request, session)
    elif action == contract.ACTION_SAVE_DRAFT:
        result = _save_draft_result(request, session)
    elif action == contract.ACTION_GET_DRAFT:
        result = _get_draft_result(request, session)
    elif action == contract.ACTION_DISCARD_DRAFT:
        result = _discard_draft_result(request, session)
    elif action == contract.ACTION_RESET_DRAFT:
        result = _reset_draft_result(request, session)
    elif action == contract.ACTION_COMPARE_DRAFT:
        result = _compare_draft_result(request, session)
    elif action == contract.ACTION_GENERATE_INTENT_DELTA:
        result = _generate_intent_delta_result(request, session)
    elif action == contract.ACTION_PLAN_PROPOSAL:
        result = _plan_proposal_result(request, session)
    elif action == contract.ACTION_GET_READINESS:
        result = _get_readiness_result(request, session)
    elif action == contract.ACTION_MANAGE_CREDENTIAL:
        result = _manage_credential_result(request, session)
    elif action == contract.ACTION_REMOVE_CREDENTIAL:
        result = _remove_credential_result(request, session)
    elif action == contract.ACTION_GET_PROFILES:
        result = _get_profiles_result(request, session)
    elif action == contract.ACTION_ADD_PROFILE:
        result = _add_profile_result(request, session)
    elif action == contract.ACTION_RENAME_PROFILE:
        result = _rename_profile_result(request, session)
    elif action == contract.ACTION_DELETE_PROFILE:
        result = _delete_profile_result(request, session)
    elif action == contract.ACTION_SET_ACTIVE_PROFILE:
        result = _set_active_profile_result(request, session)
    elif action == contract.ACTION_PREPARE_ADVISORY:
        result = _prepare_advisory_result(request, session)
    elif action == contract.ACTION_PLAN_ADVISORY:
        result = _plan_advisory_result(request, session)
    elif action == contract.ACTION_GET_PACKAGE:
        result = _get_package_result(request, session)
    elif action == contract.ACTION_RUN_PACKAGE:
        result = _run_package_result(request, session)
    elif action == contract.ACTION_DOCUMENT_CREATE:
        result = _create_document_result(request, session)
    elif action == contract.ACTION_DOCUMENT_OPEN:
        result = _open_document_result(request, session)
    elif action == contract.ACTION_DOCUMENT_SAVE:
        result = _save_document_result(request, session)
    elif action == contract.ACTION_DOCUMENT_LIST:
        result = _list_documents_result(request, session)
    elif action == contract.ACTION_DOCUMENT_CREATE_CANDIDATE:
        result = _create_candidate_result(request, session)
    elif action == contract.ACTION_DOCUMENT_GET_CANDIDATE:
        result = _get_candidate_result(request, session)
    elif action == contract.ACTION_DOCUMENT_ADOPT:
        result = _adopt_candidate_result(request, session)
    elif action == contract.ACTION_DOCUMENT_LIST_VERSIONS:
        result = _list_versions_result(request, session)
    elif action == contract.ACTION_DOCUMENT_RESTORE:
        result = _restore_version_result(request, session)
    else:  # pragma: no cover - guarded by the allowlist above
        raise contract.ContractError("action_not_allowed")

    return contract.build_success(correlation_id, result)


def _scan_result(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run the deterministic scan -> plan -> report pipeline (P3.1)."""
    path = request.get("path")
    if not isinstance(path, str) or not path.strip():
        raise contract.ContractError("invalid_request")

    task = request.get("task")
    if not isinstance(task, dict):
        raise contract.ContractError("invalid_request")

    # Task intake: validate against the P2.3 shape. The specific validation
    # reason is not propagated; only the bounded code is serialized.
    try:
        validate_task(task)
    except TaskValidationError:
        raise contract.ContractError("invalid_request")

    # The boundary owns the allowlist decision: reject any task that names a
    # mutating action, even though the planner would accept it.
    if any(a not in contract.READ_ONLY_TASK_ACTIONS for a in task["allowed_actions"]):
        raise contract.ContractError("action_not_allowed")

    scanner_doc = scan_directory(path)
    plan = build_plan(task)
    report = build_report(
        scanner_doc,
        {
            "task_id": task["task_id"],
            "plan": plan,
            "next_action": _NEXT_ACTION,
            "repository_context": task["repository_context"],
        },
    )

    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "report": report,
        "evidence": scanner_doc,
    }


def _open_project_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Validate and accept a project root."""
    path = request.get("path")
    if not isinstance(path, str) or not path.strip():
        raise contract.ContractError("invalid_request")
    root = workspace.resolve_root(path)
    session.open(root)
    return {"root": root, "repository_state": "Unverified"}


def _get_tree_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the filtered tree for the accepted project root."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    return workspace.build_tree(session.root)


def _get_document_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return one permitted document below the accepted project root."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    rel_path = request.get("path")
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise contract.ContractError("invalid_request")
    return workspace.read_document(session.root, rel_path)


# -- Twin handlers (P3.3) ------------------------------------------------

def _compute_fingerprints(root: str, scanner_doc: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Return ``{path: fingerprint}`` for every ``.py``/``.pyi`` file in ``scanner_doc``.

    Fingerprints are read from disk below ``root`` (the accepted project, which
    the scanner already walked); an unreadable file yields ``None`` so its
    fingerprint never invents content. The result is deterministic and
    path-ordered.
    """
    fingerprints: Dict[str, Optional[str]] = {}
    for file_rec in scanner_doc.get("files", []):
        rel_path = file_rec.get("path")
        if not isinstance(rel_path, str) or not rel_path.endswith((".py", ".pyi")):
            continue
        try:
            with open(os.path.join(root, rel_path), "rb") as fh:
                fingerprints[rel_path] = twin.fingerprint_bytes(fh.read())
        except OSError:
            fingerprints[rel_path] = None
    return fingerprints


def _twin_store(session: WorkspaceSession) -> Dict[str, Any]:
    """Load the workspace Twin store, raising ``twin_not_synchronized`` if absent."""
    workspace_id = twin.workspace_id_for(session.root)
    store, err = twin_store.load(session.store_base, workspace_id)
    if err is not None or store is None:
        raise contract.ContractError("twin_not_synchronized")
    return store


def _next_generation(previous: Optional[Dict[str, Any]]) -> int:
    """Return the next scan generation (one past the previous store's)."""
    if previous is None:
        return 1
    prev_gen = (previous.get("workspace_revision") or {}).get("scan_generation")
    return prev_gen + 1 if isinstance(prev_gen, int) else 1


def _now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _sync_twin_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Synchronize the Structured Twin for the accepted workspace.

    A full sync (``changed_paths`` absent) or a changed-path sync (a list of
    root-relative ``.py`` paths) reconciles the scanner facts against the last
    valid store, persists atomically, and returns the SynchronizationResult.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")

    task = request.get("task")
    changed_paths: Optional[List[str]] = None
    if isinstance(task, dict) and "changed_paths" in task:
        changed_paths = task["changed_paths"]
        if not isinstance(changed_paths, list) or not all(
            isinstance(p, str) for p in changed_paths
        ):
            raise contract.ContractError("invalid_request")

    scanner_doc = scan_directory(session.root)
    fingerprints = _compute_fingerprints(session.root, scanner_doc)
    workspace_id = twin.workspace_id_for(session.root)
    previous, _ = twin_store.load(session.store_base, workspace_id)
    generation = _next_generation(previous)

    store, result = twin.sync_twin(
        scanner_doc,
        fingerprints,
        previous,
        workspace_id,
        generation,
        _now_iso(),
        changed_paths,
    )

    # Persist atomically; a failed write retains the last valid store and the
    # result still carries the reconciled state, flagged ``persisted`` false.
    result["persisted"] = twin_store.save(session.store_base, workspace_id, store) is None
    return result


def _get_twin_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the source-linked Twin projection bundle for a selector."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    task = request.get("task")
    selector = task.get("selector") if isinstance(task, dict) else None
    if not isinstance(selector, str) or not selector.strip():
        raise contract.ContractError("invalid_request")

    store = _twin_store(session)
    bundle = twin.projection_bundle(store, selector)
    if bundle is None:
        raise contract.ContractError("twin_not_found")
    return bundle


def _get_anchor_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return a bounded source-anchor navigation result for a behavior node."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    task = request.get("task")
    node_id = task.get("node_id") if isinstance(task, dict) else None
    if not isinstance(node_id, str) or not node_id.strip():
        raise contract.ContractError("invalid_request")

    store = _twin_store(session)
    anchor = twin.anchor_for(store, node_id)
    if anchor is None:
        raise contract.ContractError("twin_not_found")
    return anchor


# -- Editable Code Map handlers (P3.4) -----------------------------------

def _workspace_id(session: WorkspaceSession) -> str:
    """Return the canonical workspace identifier for the accepted root."""
    return twin.workspace_id_for(session.root)


def _load_draft_or_raise(session: WorkspaceSession) -> Dict[str, Any]:
    """Load the saved draft, raising a bounded ``draft_not_found`` when absent.

    A corrupt or future-version draft also maps to ``draft_not_found`` (the
    fail-closed load returns an error reason), so no draft content ever leaks.
    """
    draft, err = twin_store.load_draft(session.store_base, _workspace_id(session))
    if err is not None or draft is None:
        raise contract.ContractError("draft_not_found")
    return draft


def _code_map_baseline(session: WorkspaceSession, store: Dict[str, Any]) -> Dict[str, Any]:
    """Build the full Code Map block list for the synchronized workspace.

    Every modeled ``.py``/``.pyi`` file is read from disk below the accepted
    root and parsed into procedural blocks (the source facts behind the Code
    Map document). The result is the baseline envelope a draft validates
    against, carrying the workspace's baseline revision.
    """
    revision = store.get("workspace_revision") or {}
    baseline_revision = revision.get("baseline_fingerprint")
    blocks: List[Dict[str, Any]] = []
    for artifact in store.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if artifact.get("kind") != twin.ARTIFACT_FILE:
            continue
        rel_path = artifact.get("path")
        if not isinstance(rel_path, str) or not rel_path.endswith((".py", ".pyi")):
            continue
        try:
            with open(os.path.join(session.root, rel_path), "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        module = artifact.get("module") or module_name_for(rel_path)
        # A file the scanner already recorded as a ``parse_error`` (e.g. invalid
        # syntax) has no procedural model; skip it rather than fail the whole
        # Code Map. Valid files are unaffected, mirroring the scanner's
        # continue-on-error rule.
        try:
            blocks.extend(codemap.build_codemap(source, rel_path, module, baseline_revision))
        except (SyntaxError, ValueError):
            continue
    return codemap_draft.baseline_document(blocks, baseline_revision)


def _code_map_scope(
    request: Dict[str, Any], baseline: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the Code Map blocks and document scoped to the request's selector.

    When no ``selector`` is present the whole module-level document is returned;
    otherwise the blocks are narrowed to the named entity and a missing entity
    is a bounded ``twin_not_found``.
    """
    blocks = baseline.get("blocks") or []
    task = request.get("task")
    selector = task.get("selector") if isinstance(task, dict) else None
    if isinstance(selector, str) and selector.strip():
        scoped = codemap.blocks_for_entity(blocks, selector)
        if not scoped:
            raise contract.ContractError("twin_not_found")
        return {"entity": selector, "blocks": scoped, "document": codemap.render_blocks(scoped)}
    return {"entity": None, "blocks": blocks, "document": codemap.render_blocks(blocks)}


def _get_code_map_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the procedural Code Map document, entity list and saved draft state."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    workspace_id = _workspace_id(session)
    baseline = _code_map_baseline(session, store)
    scope = _code_map_scope(request, baseline)
    draft, _ = twin_store.load_draft(session.store_base, workspace_id)
    revision = store.get("workspace_revision") or {}
    return {
        "language_version": codemap.CODEMAP_LANGUAGE_VERSION,
        "generator": codemap.GENERATOR,
        "entity": scope["entity"],
        "entities": codemap.entity_list(baseline.get("blocks") or []),
        "blocks": scope["blocks"],
        "document": scope["document"],
        "baseline": {
            "workspace_id": revision.get("workspace_id"),
            "baseline_revision": revision.get("baseline_fingerprint"),
            "scan_generation": revision.get("scan_generation"),
            "sync_state": revision.get("sync_state"),
        },
        "draft": draft,
        "conflict": (
            codemap_draft.conflict_for(draft, baseline)
            if draft is not None
            else {"state": codemap_draft.CONFLICT_NONE, "reason": None}
        ),
    }


def _save_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Validate and atomically persist a Code Map Draft against the baseline.

    The draft is built from a list of typed ``operations`` (SCOPE G) validated
    against the current Code Map baseline. A read-only-block edit, an unknown
    target, an unsupported operation, or an oversized value is rejected with a
    bounded error — never persisted.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)

    task = request.get("task")
    if not isinstance(task, dict):
        raise contract.ContractError("invalid_request")
    operations = task.get("operations")
    if not isinstance(operations, list):
        raise contract.ContractError("invalid_request")
    if len(contract.dumps(operations).encode("utf-8")) > contract.MAX_DRAFT_BYTES:
        raise contract.ContractError("draft_oversized")

    workspace_id = _workspace_id(session)
    baseline = _code_map_baseline(session, store)
    now = _now_iso()
    draft, err = codemap_draft.build_draft(workspace_id, baseline, operations, now, now)
    if err is not None:
        # Only the oversized reason maps to ``draft_oversized``; every other
        # domain rejection (read-only, unknown target, unsupported, duplicate)
        # maps to the fixed ``draft_invalid`` code. The reason never leaks.
        if err == codemap_draft.REASON_OVERSIZED:
            raise contract.ContractError("draft_oversized")
        raise contract.ContractError("draft_invalid")

    persisted = twin_store.save_draft(session.store_base, workspace_id, draft) is None
    return {"draft": draft, "persisted": persisted}


def _get_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the saved Code Map Draft plus its conflict state against the baseline."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    baseline = _code_map_baseline(session, store)
    draft = _load_draft_or_raise(session)
    return {"draft": draft, "conflict": codemap_draft.conflict_for(draft, baseline)}


def _discard_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Discard (delete) the saved Code Map Draft. Idempotent; never touches source."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    twin_store.discard_draft(session.store_base, _workspace_id(session))
    return {"discarded": True, "draft": None}


def _reset_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Reset the draft to the baseline by removing every saved operation."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    twin_store.discard_draft(session.store_base, _workspace_id(session))
    return {"reset": True, "draft": None}


def _compare_draft_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the draft's typed operations relative to the baseline."""
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    baseline = _code_map_baseline(session, store)
    draft = _load_draft_or_raise(session)
    return {
        "draft_id": draft.get("draft_id"),
        "operations": draft.get("operations", []),
        "conflict": codemap_draft.conflict_for(draft, baseline),
    }


def _generate_intent_delta_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Generate (or retrieve) the deterministic, non-executable Intent Delta.

    A no-op draft yields an honest ``no_change`` result; a stale draft is
    blocked with a bounded ``draft_stale`` error. The delta is never claimed to
    be executable and never contains source content.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    baseline = _code_map_baseline(session, store)
    draft = _load_draft_or_raise(session)
    if codemap_draft.is_noop(draft):
        return {"intent_delta": None, "no_change": True}
    if codemap_draft.conflict_for(draft, baseline)["state"] != codemap_draft.CONFLICT_NONE:
        raise contract.ContractError("draft_stale")
    delta, err = codemap_draft.generate_intent_delta(draft, baseline)
    if err is not None:  # pragma: no cover - guarded by the checks above
        raise contract.ContractError("draft_invalid")
    return {"intent_delta": delta, "no_change": False}


def _plan_proposal_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Derive a deterministic, non-applied Proposal Package from the Intent Delta.

    A no-op draft yields an honest ``no_change`` result; a missing draft maps to
    ``draft_not_found``; a stale draft is blocked with ``draft_stale``. The
    package is never claimed to be a patch, diff, approval or execution outcome
    and never contains source content beyond bounded identifiers.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store = _twin_store(session)
    baseline = _code_map_baseline(session, store)
    draft = _load_draft_or_raise(session)
    package, err = proposal.plan_proposal(draft, baseline, store)
    if err == proposal.REASON_NO_CHANGE:
        return {"proposal": None, "state": proposal.STATE_NO_CHANGE, "no_change": True}
    if err == proposal.REASON_STALE:
        raise contract.ContractError("draft_stale")
    if err is not None:  # pragma: no cover - guarded by the reasons above
        raise contract.ContractError("draft_invalid")
    return {"proposal": package, "state": package["state"], "no_change": False}


# -- Provider readiness handler (P4.2a) ----------------------------------


def _get_readiness_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Return the redacted local readiness result (P4.2a).

    Only non-secret facts are reported: the fixed provider id, the allowlisted
    model (or ``None`` when the config is invalid) and a bounded state derived
    from the *active* profile's credential presence — or the legacy credential
    presence when no profile exists yet. The credential value is never read or
    surfaced, and the result never claims network authentication or
    availability. This path performs no migration (it must stay read-free).
    """
    store = _resolve_store(session)
    store_available = store.available()
    config, config_error = provider_config.load(session.store_base)
    if config is None and config_error is None:
        config = provider_config.default_config()
    credential_present = False
    if config is not None:
        active = config.get("active_profile_id")
        if active is not None:
            credential_present = store_available and store.has(
                credential_store.profile_target(active)
            )
        elif not config.get("profiles") and store_available:
            credential_present = store.has(credential_store.TARGET_NAME)
    return deepseek.redacted_readiness(
        config=config,
        config_error=config_error,
        credential_present=credential_present,
        store_available=store_available,
    )


# -- Credential management handlers (P4.2a) -------------------------------


def _resolve_store(session: WorkspaceSession):
    """Resolve the backend-owned credential store, lazily constructing it.

    The store is constructed lazily so a boundary loop that never touches a
    credential never constructs one. Unlike :func:`_credential_store` this
    returns the raw store (including an unavailable one) so presence checks can
    still run safely against it.
    """
    store = session.credential_store
    if store is None:
        store = credential_store.make_credential_store()
    return store


def _credential_store(session: WorkspaceSession):
    """Resolve the backend-owned credential store, or ``None`` when unavailable.

    The store is constructed lazily so a boundary loop that never touches a
    credential never constructs one. A store that reports itself unavailable is
    returned as ``None`` so the handlers report ``unavailable`` rather than
    attempting a mutating call.
    """
    store = session.credential_store
    if store is None:
        store = credential_store.make_credential_store()
    return store if store.available() else None


def _credential_prompt(session: WorkspaceSession):
    """Resolve the backend-owned secure prompt, or ``None`` when unavailable."""
    prompt = session.credential_prompt
    if prompt is None:
        prompt = credential_store.native_credential_prompt()
    return prompt


def _manage_credential_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Enroll or replace the DeepSeek API key through the secure local prompt.

    The key is collected by the operating system's own credential dialog and
    written straight to the backend-owned platform credential store; the secret
    never passes through, and is never held/logged/serialized by, the boundary
    result. The returned mapping carries only a bounded state token plus the
    redacted presence fact.
    """
    store = _credential_store(session)
    prompt = _credential_prompt(session)
    if store is None or prompt is None:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_UNAVAILABLE, store is not None and store.has(credential_store.TARGET_NAME)
        )
    try:
        secret = prompt("Enter the DeepSeek API key")
    except (EOFError, KeyboardInterrupt):
        secret = None
    except credential_store.CredentialStoreError as exc:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_FAILED,
            store.has(credential_store.TARGET_NAME),
            reason=exc.code,
        )
    if secret is None:
        # User cancelled the native prompt; the store is left untouched.
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_CANCELLED, store.has(credential_store.TARGET_NAME)
        )
    try:
        store.store(credential_store.TARGET_NAME, secret)
    except credential_store.CredentialStoreError as exc:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_FAILED,
            store.has(credential_store.TARGET_NAME),
            reason=exc.code,
        )
    finally:
        # The secret is dropped from the local frame as soon as the store call
        # returns, success or failure.
        secret = None
    return credential_store.redacted_credential_result(
        credential_store.CREDENTIAL_STATE_STORED, store.has(credential_store.TARGET_NAME)
    )


def _remove_credential_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Delete the DeepSeek API key from the platform credential store.

    Only the backend delete is performed; the key is never read or surfaced. The
    result carries a bounded state token and the redacted presence fact after
    deletion (always ``False`` on success).
    """
    store = _credential_store(session)
    if store is None:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_UNAVAILABLE, False
        )
    try:
        store.delete(credential_store.TARGET_NAME)
    except credential_store.CredentialStoreError as exc:
        return credential_store.redacted_credential_result(
            credential_store.CREDENTIAL_STATE_FAILED,
            store.has(credential_store.TARGET_NAME),
            reason=exc.code,
        )
    return credential_store.redacted_credential_result(
        credential_store.CREDENTIAL_STATE_REMOVED, False
    )


# -- Credential-profile handlers (P4.2a) ----------------------------------


def _migrate_legacy_credential(
    session: WorkspaceSession, store: Any, config: Dict[str, Any]
) -> tuple:
    """Migrate the legacy single DeepSeek credential into a default profile.

    Creates one opaque default profile, copies the legacy secret from
    ``TARGET_NAME`` to the profile's own target, verifies the new target's
    presence, persists the profile (active) atomically, and only then deletes
    the legacy entry. Any failure before that commit returns ``(config, False)``
    and leaves the legacy credential and configuration untouched, so migration
    is never lossy.
    """
    profile_id = contract.new_profile_id()
    display_name = "Default"
    target = credential_store.profile_target(profile_id)
    try:
        secret = store.read(credential_store.TARGET_NAME)
    except credential_store.CredentialStoreError:
        return config, False
    if not secret:
        return config, False
    try:
        store.store(target, secret)
    except credential_store.CredentialStoreError:
        return config, False
    finally:
        secret = None
    if not store.has(target):
        return config, False
    new_config, err = provider_config.add_profile(config, profile_id, display_name)
    if err is not None:
        return config, False
    new_config, err = provider_config.set_active_profile(new_config, profile_id)
    if err is not None:
        return config, False
    if provider_config.save(session.store_base, new_config) is not None:
        return config, False
    try:
        store.delete(credential_store.TARGET_NAME)
    except credential_store.CredentialStoreError:
        pass  # the legacy entry lingers harmlessly; the profile is authoritative
    return new_config, True


def _profiles_state(session: WorkspaceSession, *, migrate: bool = True):
    """Load the profile configuration and return its state tuple.

    Returns ``(config, config_error, migrated, store_available)``. When
    ``migrate`` is true and no profiles exist while a legacy credential is
    present, the legacy credential is migrated into a default profile (which
    reads the secret and is therefore only performed on the profile-management
    path, never the read-free readiness path).
    """
    store = _resolve_store(session)
    store_available = store.available()
    config, config_error = provider_config.load(session.store_base)
    migrated = False
    if config is None and config_error is None:
        config = provider_config.default_config()
    if (
        migrate
        and config is not None
        and config_error is None
        and not config.get("profiles")
        and store_available
        and store.has(credential_store.TARGET_NAME)
    ):
        config, migrated = _migrate_legacy_credential(session, store, config)
    return config, config_error, migrated, store_available


def _profiles_result(
    session: WorkspaceSession,
    config: Optional[Dict[str, Any]],
    config_error: Optional[str],
    migrated: bool,
    store_available: bool,
) -> Dict[str, Any]:
    """Assemble the bounded, secret-free profile list result (P4.2a)."""
    store = _resolve_store(session)
    profiles = []
    if config is not None:
        for profile in config.get("profiles", []):
            profile_id = profile.get("profile_id")
            profiles.append(
                {
                    "profile_id": profile_id,
                    "provider_id": profile.get("provider_id"),
                    "display_name": profile.get("display_name"),
                    "credential_present": (
                        store_available
                        and store.has(credential_store.profile_target(profile_id))
                    ),
                }
            )
    active = config.get("active_profile_id") if config is not None else None
    active_present = any(
        p["profile_id"] == active and p["credential_present"] for p in profiles
    )
    model = None
    if config_error is None and config is not None:
        candidate = config.get("model")
        if deepseek.is_allowed_model(candidate):
            model = candidate
    state = deepseek.readiness_state(
        config_error=config_error,
        credential_present=active_present,
        store_available=store_available,
    )
    return {
        "provider_id": deepseek.PROVIDER_ID,
        "model": model,
        "state": state,
        "profiles": profiles,
        "active_profile_id": active,
        "credential_present": active_present,
        "authenticated": False,
        "online": False,
        "executable": False,
        "migrated": migrated,
        "store_available": store_available,
    }


def _profile_config_or_raise(session: WorkspaceSession) -> Dict[str, Any]:
    """Load the migrated config, raising a bounded error when it is invalid."""
    config, config_error, _migrated, _store_available = _profiles_state(session)
    if config_error is not None:
        raise contract.ContractError("invalid_request")
    return config


def _get_profiles_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Return the saved credential profiles with redacted presence (P4.2a)."""
    config, config_error, migrated, store_available = _profiles_state(session)
    return _profiles_result(session, config, config_error, migrated, store_available)


def _add_profile_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Persist one profile whose secret was already stored by the native host.

    The opaque profile id and display name arrive as metadata only; the secret
    was collected and stored by the dedicated credential host under a target
    derived from the id. The boundary verifies the credential is actually
    present (so no orphaned metadata is created) and, on the first profile,
    selects it as active.
    """
    profile_id = request.get("profile_id")
    display_name = request.get("display_name")
    if not contract.is_valid_profile_id(profile_id) or not isinstance(display_name, str):
        raise contract.ContractError("invalid_request")
    config = _profile_config_or_raise(session)
    store = _resolve_store(session)
    if store.available() and not store.has(credential_store.profile_target(profile_id)):
        raise contract.ContractError("profile_credential_missing")
    new_config, err = provider_config.add_profile(config, profile_id, display_name)
    if err is not None:
        raise contract.ContractError("profile_name_invalid")
    if not config.get("profiles"):
        new_config, err = provider_config.set_active_profile(new_config, profile_id)
        if err is not None:  # pragma: no cover - first profile is always valid
            raise contract.ContractError("invalid_request")
    if provider_config.save(session.store_base, new_config) is not None:
        raise contract.ContractError("profile_persist_failed")
    return _profiles_result(session, new_config, None, False, store.available())


def _rename_profile_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Change a profile's display name (metadata only, id and secret unchanged)."""
    profile_id = request.get("profile_id")
    display_name = request.get("display_name")
    if not contract.is_valid_profile_id(profile_id) or not isinstance(display_name, str):
        raise contract.ContractError("invalid_request")
    config = _profile_config_or_raise(session)
    new_config, err = provider_config.rename_profile(config, profile_id, display_name)
    if err == "profile not found":
        raise contract.ContractError("profile_not_found")
    if err is not None:
        raise contract.ContractError("profile_name_invalid")
    if provider_config.save(session.store_base, new_config) is not None:
        raise contract.ContractError("profile_persist_failed")
    return _profiles_result(session, new_config, None, False, _resolve_store(session).available())


def _delete_profile_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Delete one profile's credential and metadata (failure-safe, P4.2a).

    The caller supplies the fallback ``active_profile_id`` (an existing
    remaining profile id, or ``None`` to leave no active profile) so the
    boundary never silently chooses another credential. The credential is
    deleted first; if it cannot be removed the metadata is left untouched. Then
    the metadata is removed with a single atomic write.
    """
    profile_id = request.get("profile_id")
    active_profile_id = request.get("active_profile_id")
    if not contract.is_valid_profile_id(profile_id):
        raise contract.ContractError("invalid_request")
    if active_profile_id is not None and not contract.is_valid_profile_id(active_profile_id):
        raise contract.ContractError("invalid_request")
    config = _profile_config_or_raise(session)
    new_config, err = provider_config.remove_profile(config, profile_id, active_profile_id)
    if err == "profile not found":
        raise contract.ContractError("profile_not_found")
    if err is not None:
        raise contract.ContractError("invalid_request")
    store = _resolve_store(session)
    if store.available():
        try:
            store.delete(credential_store.profile_target(profile_id))
        except credential_store.CredentialStoreError:
            raise contract.ContractError("profile_persist_failed")
    if provider_config.save(session.store_base, new_config) is not None:
        raise contract.ContractError("profile_persist_failed")
    return _profiles_result(session, new_config, None, False, store.available())


def _set_active_profile_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Select exactly one active profile (a non-secret metadata write only)."""
    profile_id = request.get("profile_id")
    if profile_id is not None and not contract.is_valid_profile_id(profile_id):
        raise contract.ContractError("invalid_request")
    config = _profile_config_or_raise(session)
    new_config, err = provider_config.set_active_profile(config, profile_id)
    if err == "profile not found":
        raise contract.ContractError("profile_not_found")
    if err is not None:
        raise contract.ContractError("invalid_request")
    if provider_config.save(session.store_base, new_config) is not None:
        raise contract.ContractError("profile_persist_failed")
    return _profiles_result(session, new_config, None, False, _resolve_store(session).available())


# -- Advisory hosted-planning handlers (P4.2b) ----------------------------
#
# ``prepare_advisory`` builds the bounded disclosure context (offline, no
# network); ``plan_advisory`` performs exactly one confirmed provider request.
# Both preserve the deterministic P4.1 proposal; only ``provider_suggested``
# fields may come from the provider, and they never overwrite the deterministic
# authority. No credential, source excerpt, raw prompt or raw response is ever
# placed in a result, and the transport module is imported lazily so frozen
# scan/serve/readiness remain offline.


def _advisory_state(session: WorkspaceSession) -> tuple:
    """Re-derive the current Intent Delta and Proposal Package for the session.

    Returns ``(store, baseline, draft, package, delta, err)``. ``package`` and
    ``delta`` are ``None`` when the draft is a no-op or stale (``err`` carries
    the bounded reason). The derivation is identical to ``plan_proposal`` but
    also retains the Intent Delta needed to build the advisory context.
    """
    store = _twin_store(session)
    baseline = _code_map_baseline(session, store)
    draft = _load_draft_or_raise(session)
    if codemap_draft.is_noop(draft):
        return store, baseline, draft, None, None, proposal.REASON_NO_CHANGE
    if codemap_draft.conflict_for(draft, baseline)["state"] != codemap_draft.CONFLICT_NONE:
        return store, baseline, draft, None, None, proposal.REASON_STALE
    delta, delta_err = codemap_draft.generate_intent_delta(draft, baseline)
    if delta_err is not None:  # pragma: no cover - guarded by the checks above
        return store, baseline, draft, None, None, delta_err
    package = proposal.build_proposal(delta, baseline, store)
    return store, baseline, draft, package, delta, None


def _advisory_authority(package: Any, store: Dict[str, Any]) -> Dict[str, Any]:
    """Return the authoritative deterministic slice for a result."""
    return advisory.deterministic_authority(
        package if isinstance(package, dict) else {}, store
    )


def _denial_for_document(reason: str) -> str:
    """Map a ``workspace.read_document`` unavailable reason to a bounded denial."""
    if reason == "binary":
        return advisory.DENY_BINARY
    if reason == "unsupported_type":
        return advisory.DENY_UNSUPPORTED_PATH
    if reason == "file_too_large":
        return advisory.DENY_OVER_LIMIT
    if reason == "path_not_found":
        return advisory.DENY_MISSING_ANCHOR
    return advisory.DENY_OUTSIDE_ROOT


def _add_denial(denials: List[str], reason: str) -> None:
    """Append ``reason`` once, keeping the list sorted and deduplicated."""
    if reason not in denials:
        denials.append(reason)
        denials.sort()


def _build_advisory_excerpts(
    session: WorkspaceSession, blocks: List[Dict[str, Any]], entities: List[str]
) -> tuple:
    """Return ``(excerpts, denials)`` for the target entities' source anchors.

    Only ``.py``/``.pyi`` files below the accepted root are read (via the
    read-only workspace policy, which already enforces containment, binary and
    size limits). A denied, missing or unreadable anchor is recorded as a
    bounded denial so the disclosure reports the limitation truthfully rather
    than fabricating a replacement excerpt.
    """
    anchors = advisory.entity_anchors(blocks, entities)
    excerpts: Dict[str, List[str]] = {}
    denials: List[str] = []
    covered: set = set()
    for anchor in anchors:
        locator = anchor["locator"]
        file = anchor["file"]
        lineno = anchor["lineno"]
        end_lineno = anchor["end_lineno"]
        if not isinstance(lineno, int) or not isinstance(end_lineno, int):
            continue
        covered.add(locator)
        try:
            doc = workspace.read_document(session.root, file)
        except contract.ContractError:
            _add_denial(denials, advisory.DENY_OUTSIDE_ROOT)
            continue
        if doc.get("kind") != "source":
            _add_denial(denials, _denial_for_document(doc.get("reason") or ""))
            continue
        excerpt = advisory.slice_excerpt(
            file=file, lineno=lineno, end_lineno=end_lineno,
            content=doc.get("content", ""),
        )
        excerpts.setdefault(file, []).append(excerpt)

    missing = sorted(set(entities) - covered)
    if missing:
        _add_denial(denials, advisory.DENY_MISSING_ANCHOR)
    for file in excerpts:
        excerpts[file] = sorted(set(excerpts[file]))
    return excerpts, denials


def _active_credential_target(session: WorkspaceSession) -> Optional[str]:
    """Return the credential-store target for the active profile, or the legacy
    target when no profile exists yet, or ``None`` when no credential applies."""
    config, config_error = provider_config.load(session.store_base)
    if config is None and config_error is None:
        config = provider_config.default_config()
    if config is None:
        return None
    active = config.get("active_profile_id")
    if active is not None:
        return credential_store.profile_target(active)
    if not config.get("profiles"):
        return credential_store.TARGET_NAME
    return None


def _advisory_result(
    *,
    state: str,
    token: Optional[str],
    sent: bool,
    authority: Dict[str, Any],
    provider_suggested: Optional[Dict[str, Any]],
    usage: Optional[Dict[str, Optional[int]]],
    limitations: List[str],
) -> Dict[str, Any]:
    """Assemble one versioned advisory result through the shared domain."""
    return advisory.assemble_result(
        state=state,
        provider_id=deepseek.PROVIDER_ID,
        model=deepseek.DEFAULT_MODEL,
        advisory_token=token,
        sent=sent,
        deterministic=authority,
        provider_suggested=provider_suggested,
        usage=usage,
        limitations=limitations,
    )


def _prepare_advisory_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Build the deterministic disclosure context for the current proposal.

    Performs no network, credential or provider access. Returns
    ``advisory_available`` plus, when available, the itemized disclosure
    manifest and the content-addressed advisory token; otherwise a bounded
    ``reason`` (no_change / stale / unsupported / clarification_required / a
    denial reason). The deterministic proposal is always preserved.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    store, baseline, _draft, package, delta, err = _advisory_state(session)
    base = {
        "provider_id": deepseek.PROVIDER_ID,
        "model": deepseek.DEFAULT_MODEL,
        "proposal": package,
        "disclosure": None,
        "advisory_token": None,
    }
    if err == proposal.REASON_NO_CHANGE:
        return {**base, "advisory_available": False, "reason": proposal.REASON_NO_CHANGE}
    if err == proposal.REASON_STALE:
        return {**base, "advisory_available": False, "reason": proposal.REASON_STALE}
    if package is None:  # pragma: no cover - guarded by the reasons above
        raise contract.ContractError("draft_invalid")
    if package["state"] != proposal.STATE_READY:
        return {**base, "advisory_available": False, "reason": package["state"]}

    blocks = baseline.get("blocks") or []
    entities = (package.get("target_scope") or {}).get("entities") or []
    excerpts, denials = _build_advisory_excerpts(session, blocks, entities)
    context, context_err = advisory.build_context(
        delta=delta or {},
        proposal=package,
        store=store,
        excerpts=excerpts,
        denials=denials,
    )
    if context is None:
        return {**base, "advisory_available": False, "reason": context_err}
    return {
        **base,
        "advisory_available": True,
        "reason": None,
        "disclosure": context["disclosure"],
        "advisory_token": context["advisory_token"],
    }


def _plan_advisory_result(
    request: Dict[str, Any], session: WorkspaceSession
) -> Dict[str, Any]:
    """Perform one user-confirmed advisory planning request.

    ``task`` must carry a boolean ``confirmed`` and the ``advisory_token`` from a
    prior ``prepare_advisory``. An absent/false ``confirmed`` sends nothing and
    returns ``cancel_requested``. The context is re-derived deterministically
    and must match the token (else ``stale_response``) before exactly one
    backend-owned network attempt is made. Every failure state preserves the
    deterministic proposal.
    """
    if session.root is None:
        raise contract.ContractError("project_not_open")
    task = request.get("task")
    if not isinstance(task, dict):
        raise contract.ContractError("invalid_request")
    confirmed = task.get("confirmed")
    if not isinstance(confirmed, bool):
        raise contract.ContractError("invalid_request")
    token = task.get("advisory_token")
    if not isinstance(token, str) or not token:
        raise contract.ContractError("invalid_request")

    store, baseline, _draft, package, delta, err = _advisory_state(session)
    authority = _advisory_authority(package, store)

    if not confirmed:
        return _advisory_result(
            state=advisory.STATE_CANCEL_REQUESTED,
            token=token,
            sent=False,
            authority=authority,
            provider_suggested=None,
            usage=None,
            limitations=["cancelled by the user; nothing was sent"],
        )

    if package is None or package["state"] != proposal.STATE_READY:
        return _advisory_result(
            state=advisory.STATE_STALE_RESPONSE,
            token=token,
            sent=False,
            authority=authority,
            provider_suggested=None,
            usage=None,
            limitations=["the deterministic proposal is no longer ready"],
        )

    blocks = baseline.get("blocks") or []
    entities = (package.get("target_scope") or {}).get("entities") or []
    excerpts, denials = _build_advisory_excerpts(session, blocks, entities)
    context, context_err = advisory.build_context(
        delta=delta or {},
        proposal=package,
        store=store,
        excerpts=excerpts,
        denials=denials,
    )
    if context is None:
        state = (
            advisory.STATE_OVER_LIMIT
            if context_err == advisory.DENY_OVER_LIMIT
            else advisory.STATE_CONTEXT_REJECTED
        )
        return _advisory_result(
            state=state,
            token=token,
            sent=False,
            authority=authority,
            provider_suggested=None,
            usage=None,
            limitations=[context_err],
        )
    if context["advisory_token"] != token:
        return _advisory_result(
            state=advisory.STATE_STALE_RESPONSE,
            token=token,
            sent=False,
            authority=authority,
            provider_suggested=None,
            usage=None,
            limitations=["prepared context no longer matches the current proposal"],
        )

    # Exactly one backend-owned network attempt. The transport is imported
    # lazily (inside this handler only) so frozen scan/serve/readiness never
    # pull in HTTP/socket code.
    from . import deepseek_transport

    if session.advisory_transport is not None:
        transport = session.advisory_transport
    else:
        credential_store_impl = _resolve_store(session)
        target = _active_credential_target(session)

        def get_credential() -> Optional[str]:
            if target is None or credential_store_impl is None or not credential_store_impl.available():
                return None
            try:
                return credential_store_impl.read(target)
            except credential_store.CredentialStoreError:
                return None

        transport = deepseek_transport.DeepSeekProvider(credential_getter=get_credential)

    provider_request = advisory.build_provider_request(context)
    try:
        result = transport.generate(
            provider.ProviderRequest(
                task_id=provider_request["task_id"],
                task=provider_request["task"],
                context=tuple(provider_request["context"]),
            )
        )
    except deepseek_transport.TransportError as exc:
        return _advisory_result(
            state=exc.code,
            token=token,
            sent=True,
            authority=authority,
            provider_suggested=None,
            usage=None,
            limitations=[exc.code],
        )
    except (provider.ProviderError, Exception):
        return _advisory_result(
            state=advisory.STATE_PROVIDER_FAILURE,
            token=token,
            sent=True,
            authority=authority,
            provider_suggested=None,
            usage=None,
            limitations=["the provider request failed"],
        )

    usage = result.usage.to_dict() if result.usage is not None else None
    return _advisory_result(
        state=advisory.STATE_READY,
        token=token,
        sent=True,
        authority=authority,
        provider_suggested=result.structured_payload,
        usage=usage,
        limitations=[],
    )


# -- Document-driven app-package handlers (P4.3) --------------------------


def _known_package(package_id: str):
    """Return the code-owned reference package for ``package_id``, or ``None``."""
    if package_id == "quotation-rules":
        return app_package.quotation_reference_package()
    return None


def _get_package_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the validated reference package (its form/result schema).

    Performs no runner or network access. The default package id is the single
    hand-written quotation-rules fixture.
    """
    task = request.get("task")
    package_id = task.get("package_id") if isinstance(task, dict) else None
    if package_id is None:
        package_id = "quotation-rules"
    if not isinstance(package_id, str) or not package_id:
        raise contract.ContractError("invalid_request")
    package = _known_package(package_id)
    if package is None:
        raise contract.ContractError("package_not_found")
    return {"package": package}


def _run_package_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Execute one named package against one form input through the runner.

    The package manifest and the form input are untrusted; the broker validates
    both before the isolated runner starts, and the runner never executes on the
    host. The broker (and its container runner) is imported lazily so the frozen
    scan/serve/readiness loop never pulls in the runner subprocess.
    """
    task = request.get("task")
    if not isinstance(task, dict):
        raise contract.ContractError("invalid_request")
    package_id = task.get("package_id")
    if not isinstance(package_id, str) or not package_id:
        raise contract.ContractError("invalid_request")
    package = _known_package(package_id)
    if package is None:
        raise contract.ContractError("package_not_found")
    form_input = task.get("input")
    from . import runner_broker

    return runner_broker.run_package(package, form_input, session.runner)


# -- Document/version-authority handlers (P4.4) ----------------------------
#
# The Working Document is user intent; a Candidate is a deterministic,
# fixture-bound record that is *not* accepted behavior; an Accepted Version is a
# version-bound record produced only by explicit, revalidated adoption. Saving a
# document appends an immutable revision and updates the Working Document only —
# it never calls a provider, inspects/sends project context, executes a package,
# or alters Candidate/Accepted state. Adoption revalidates the candidate against
# the current store before atomically moving the accepted pointer.


def _document_id(request: Dict[str, Any]) -> str:
    """Return and validate the opaque ``document_id`` from ``request``."""
    document_id = request.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise contract.ContractError("invalid_request")
    return document_id


def _load_document_store(session: WorkspaceSession, document_id: str) -> Dict[str, Any]:
    """Load a document store, raising ``document_not_found`` when absent/corrupt."""
    store, err = version_store.load(session.store_base, document_id)
    if err is not None or store is None:
        raise contract.ContractError("document_not_found")
    return store


def _persist_document(session: WorkspaceSession, document_id: str, store: Dict[str, Any]) -> None:
    """Persist a document store atomically, raising ``document_persist_failed``."""
    if version_store.save(session.store_base, document_id, store) is not None:
        raise contract.ContractError("document_persist_failed")


def _create_document_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Create an empty Working Document from a validated md/txt name.

    Performs no provider, package or runner access. The returned state has no
    revision and no candidate; the first save creates revision 1.
    """
    name = request.get("name")
    if not document.valid_name(name):
        raise contract.ContractError("document_name_invalid")
    document_id = document.new_document_id()
    store = document.new_document_store(
        document_id, name.strip(), document.kind_for_name(name), _now_iso()
    )
    _persist_document(session, document_id, store)
    return document.document_state(store)


def _open_document_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the reopenable state of one Working Document."""
    document_id = _document_id(request)
    store = _load_document_store(session, document_id)
    return document.document_state(store)


def _save_document_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Append an immutable revision and update the Working Document only.

    The content is validated as bounded UTF-8 text; the caller's ``base_revision_id``
    must match the current head or the save is refused with ``document_stale``
    (an external change happened between load and save). This handler performs no
    provider, package, runner, project-context or candidate/accepted mutation.
    """
    document_id = _document_id(request)
    content = request.get("content")
    if not isinstance(content, str):
        raise contract.ContractError("invalid_request")
    if len(content.encode("utf-8")) > contract.MAX_WORKING_DOCUMENT_BYTES:
        raise contract.ContractError("document_oversized")

    store = _load_document_store(session, document_id)
    base_revision_id = request.get("base_revision_id")
    if base_revision_id is not None and not isinstance(base_revision_id, str):
        raise contract.ContractError("invalid_request")

    new_store, revision = document.save_revision(store, content, base_revision_id, _now_iso())
    if new_store is None:
        # Only a stale base maps to ``document_stale``; other domain refusals are
        # guarded above (invalid content is already bounded as ``invalid_request``).
        raise contract.ContractError("document_stale")

    _persist_document(session, document_id, new_store)
    return {
        "revision": revision,
        "head_revision_number": revision["revision_number"],
        "content_fingerprint": revision["content_fingerprint"],
    }


def _list_documents_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the bounded summary of every readable document store."""
    return {"documents": version_store.list_documents(session.store_base)}


def _create_candidate_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Create a deterministic candidate bound to the current head revision.

    The candidate is bound to the hand-written quotation fixture (the code-owned
    reference package), its runtime identity and the fixed validation identity. It
    is never generated from the document prose.
    """
    document_id = _document_id(request)
    store = _load_document_store(session, document_id)
    package = _known_package("quotation-rules")
    candidate, err = document.build_candidate(
        store, package, app_package.RUNNER_IDENTITY, document.VALIDATION_IDENTITY, _now_iso()
    )
    if err is not None:
        raise contract.ContractError("document_not_saved")

    # Idempotent: an existing, not-yet-adopted candidate with the same
    # deterministic id is returned rather than appended twice.
    existing = next(
        (c for c in store.get("candidates", []) if c.get("candidate_id") == candidate["candidate_id"]),
        None,
    )
    if existing is None:
        new_store = _copy_store(store)
        new_store["candidates"].append(candidate)
        _persist_document(session, document_id, new_store)
        store = new_store
    return document.document_state(store)


def _copy_store(store: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of ``store`` (via the domain's deterministic dumps)."""
    import json as _json

    return _json.loads(document.dumps(store))


def _get_candidate_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return one candidate's state plus its current/adopted status."""
    document_id = _document_id(request)
    candidate_id = request.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise contract.ContractError("invalid_request")
    store = _load_document_store(session, document_id)
    state = document.candidate_state(store, candidate_id)
    if state is None:
        raise contract.ContractError("candidate_not_found")
    return {"candidate": state}


def _adopt_candidate_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Explicitly adopt a candidate into an Accepted Version, after revalidation.

    The candidate is revalidated against the *current* store (document head,
    package/runtime/validation identities, accepted predecessor) before the
    pointer moves. A stale candidate, changed document, missing/failed evidence,
    corrupt manifest, unexpected baseline or repeated/concurrent adoption is a
    bounded refusal that leaves the previous Accepted Version intact. This handler
    performs no runner or provider access.
    """
    document_id = _document_id(request)
    candidate_id = request.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise contract.ContractError("invalid_request")
    store = _load_document_store(session, document_id)
    package = _known_package("quotation-rules")
    new_store, version = document.adopt_candidate(
        store, candidate_id, package, app_package.RUNNER_IDENTITY,
        document.VALIDATION_IDENTITY, _now_iso(),
    )
    if new_store is None:
        raise contract.ContractError(_adopt_error_code(version))
    _persist_document(session, document_id, new_store)
    result = document.document_state(new_store)
    result["state"] = "adopted"
    result["accepted_version"] = version
    return result


def _adopt_error_code(reason: Optional[str]) -> str:
    """Map a bounded adoption refusal to its contract error code."""
    if reason == document.REASON_CANDIDATE_NOT_FOUND:
        return "candidate_not_found"
    if reason == document.REASON_ALREADY_ADOPTED:
        return "already_adopted"
    if reason == document.REASON_CORRUPT:
        return "candidate_invalid"
    if reason == document.REASON_CANDIDATE_STALE:
        return "candidate_stale"
    # evidence_failed / baseline_mismatch → bounded, safe refusal
    return "adopt_not_allowed"


def _list_versions_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Return the accepted-version history and the current pointer."""
    document_id = _document_id(request)
    store = _load_document_store(session, document_id)
    return {
        "versions": [v for v in store.get("accepted_versions", []) if isinstance(v, dict)],
        "current_accepted_version_id": store.get("current_accepted_version_id"),
    }


def _restore_version_result(request: Dict[str, Any], session: WorkspaceSession) -> Dict[str, Any]:
    """Re-point the Accepted Version to a prior accepted snapshot.

    The Working Document (head revision and history) and every non-adopted
    candidate are preserved. Restoring the current version is a no-op.
    """
    document_id = _document_id(request)
    version_id = request.get("version_id")
    if not isinstance(version_id, str) or not version_id.strip():
        raise contract.ContractError("invalid_request")
    store = _load_document_store(session, document_id)
    new_store, version = document.restore_version(store, version_id, _now_iso())
    if new_store is None:
        raise contract.ContractError("version_not_found")
    _persist_document(session, document_id, new_store)
    result = document.document_state(new_store)
    result["state"] = "restored"
    result["accepted_version"] = version
    return result


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_loop", "handle_request", "WorkspaceSession"]
