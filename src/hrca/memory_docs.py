"""Evidence-linked document projection over normalized Developer Memory records.

M4.3. This module is a *projector*. It reads normalized schema 1.0.0 records and
renders bounded developer documents from them. It never reads a raw hook payload,
a transcript, a log or provider output, and it never generates free prose: every
statement is assembled from typed fields, and every material statement either
resolves to a stored record identity or is reported as missing or unsupported.

Authority
---------
A document is a **view of records, never a source of truth**. A claim can only be
as strong as the record it points at, and the projector may never upgrade a
source's own report into an established fact.

Provenance taxonomy
-------------------
``observed``
    The contract validated and stored this value itself: a typed transition, an
    identity it computed, a path or reference that passed its policy.
``reported``
    The value is source-supplied text the contract retained after redaction and
    bounding -- a decision summary, a descriptor name. It is what the source
    *said*, and it is not verified by anything.
``inferred``
    The projector derived it by counting or joining stored records. The
    derivation is deterministic and repeatable; it is still not a stored fact.
``user-confirmed``
    A correction a human confirmed. **No record type carries one in schema
    1.0.0**, so this label is declared and reported unsupported rather than
    silently omitted. The projector never emits it.

Redaction
---------
The projector renders only fields the contract already redacted, bounded and
path-policed. Content the contract dropped -- prompt text, assistant text, tool
responses, diagnostic text, transcript content -- is not present in a store, so
it cannot reappear here. Evidence records are rendered as metadata; a content
digest is reported only as *present*, never as a value, because a digest is a
fingerprint of content this layer must not expose.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import memory

DOCUMENT_SCHEMA_VERSION = "1.0.0"
DOCUMENT_GENERATOR = "hrca-memory-docs"

# -- document types ------------------------------------------------------

DOC_SESSION_SUMMARY = "session_summary"
DOC_CHANGE_RECORD = "change_record"
DOC_DECISION_RECORD = "decision_record"
DOC_ISSUES_AND_ACTIONS = "issues_and_actions"

DOCUMENT_TYPES = (
    DOC_SESSION_SUMMARY,
    DOC_CHANGE_RECORD,
    DOC_DECISION_RECORD,
    DOC_ISSUES_AND_ACTIONS,
)

# -- provenance ----------------------------------------------------------

PROV_OBSERVED = "observed"
PROV_REPORTED = "reported"
PROV_INFERRED = "inferred"
PROV_USER_CONFIRMED = "user-confirmed"

PROVENANCE_TAXONOMY = {
    PROV_OBSERVED: "a value the contract validated and stored itself",
    PROV_REPORTED: "source-supplied text the contract retained; not verified",
    PROV_INFERRED: "derived by the projector from stored records; not a stored fact",
    PROV_USER_CONFIRMED: "a correction a human confirmed",
}

# The labels no record type can carry in schema 1.0.0. Declared so a reader can
# see the gap rather than infer that the taxonomy is complete.
UNSUPPORTED_PROVENANCE = (PROV_USER_CONFIRMED,)

# -- evidence link kinds -------------------------------------------------

LINK_PROJECT = "project"
LINK_WORK_PACKAGE = "work_package"
LINK_RUN = "run"
LINK_EVENT = "event"
LINK_EVIDENCE = "evidence"
LINK_DECISION = "decision"
LINK_CHANGE_SET = "change_set"
LINK_CODE_ENTITY = "code_entity_link"
LINK_REJECTION = "rejection"
LINK_QUARANTINE = "quarantine"

# Store array -> link kind.
_ARRAY_LINKS = {
    "projects": LINK_PROJECT,
    "work_packages": LINK_WORK_PACKAGE,
    "events": LINK_EVENT,
    "evidence": LINK_EVIDENCE,
    "decisions": LINK_DECISION,
    "change_sets": LINK_CHANGE_SET,
    "code_entity_links": LINK_CODE_ENTITY,
    "rejections": LINK_REJECTION,
    "quarantines": LINK_QUARANTINE,
}

# -- bounded reasons -----------------------------------------------------

REASON_NOT_A_STORE = "projection input is not a store mapping"
REASON_UNSUPPORTED_VERSION = "store schema_version is not the supported version"
REASON_UNSUPPORTED_GENERATOR = "store generator is not the memory generator"
REASON_NO_RUN = "store carries no agent_run record"
REASON_DANGLING_LINK = "claim references a record that is not present in the store"
REASON_NOT_FINALIZED = "store is not a finalized snapshot"
REASON_NO_TERMINAL_EVENT = "run records no typed terminal event"
REASON_ORIGIN_UNRECORDED = "schema 1.0.0 records no capture origin"
REASON_ORIGIN_UNSUPPORTED = "capture origin must be 'live' or 'offline'"

# -- state semantics -----------------------------------------------------

# The reported limitation each non-success state carries into a document. A
# non-success state is never softened and never presented as a partial win.
STATE_LIMITATIONS = {
    memory.RUN_MISSING_TERMINAL: (
        "the bounded stream ended without a typed terminal transition; the run "
        "is explicitly unresolved and is never success"
    ),
    memory.RUN_UNKNOWN_OUTCOME: (
        "a terminal transition carried an outcome outside the contract's "
        "vocabulary; the run is explicitly unknown and is never success"
    ),
    memory.RUN_UNSUPPORTED: (
        "an event outside the contract's vocabulary was observed; the run is "
        "fail-closed and is never success"
    ),
    memory.RUN_FAILED: (
        "the run ended on a typed failure transition; no success may be inferred"
    ),
    memory.RUN_CANCELLED: (
        "the run was terminated without the agent concluding its work"
    ),
    memory.RUN_BLOCKED: (
        "the run was blocked before it could conclude"
    ),
}

# The bounded next action each terminal state justifies.
STATE_NEXT_ACTIONS = {
    memory.RUN_COMPLETED: (),
    memory.RUN_FAILED: (
        "inspect the failing transition and its evidence before re-running the work package",
    ),
    memory.RUN_CANCELLED: (
        "confirm whether the cancellation was intended before re-running",
    ),
    memory.RUN_BLOCKED: (
        "resolve the decision that blocked the run before re-running",
    ),
    memory.RUN_MISSING_TERMINAL: (
        "the capture is incomplete: re-import a complete session before trusting this run",
    ),
    memory.RUN_UNKNOWN_OUTCOME: (
        "the source emitted an unmodelled outcome: inspect the source's terminal event before trusting this run",
    ),
    memory.RUN_UNSUPPORTED: (
        "the source emitted an unmodelled event: extend the adapter or exclude the event before re-capturing",
    ),
}

# The bounded limitation every document carries about its own baseline.
BASELINE_UNSUPPORTED = {
    "status": "unsupported",
    "reason": "schema 1.0.0 records no baseline or revision identity",
}

# The bounded limitation every document carries about adapter payload detail.
PAYLOAD_NOT_PROJECTED = (
    "adapter payload detail is not projected: documents stay source-neutral and "
    "render only canonical record fields"
)

# The bounded limitation every document carries about verification.
VERIFICATION_UNSUPPORTED = (
    "schema 1.0.0 stores no verification result: an artifact reference proves an "
    "artifact was named, never that anything was verified"
)


# -- indexing and links --------------------------------------------------


def _index(store: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Index every stored record by link kind and id."""
    index: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for array, kind in _ARRAY_LINKS.items():
        records = store.get(array)
        if not isinstance(records, list):
            continue
        bucket = index.setdefault(kind, {})
        for record in records:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                bucket[record["id"]] = record
    run = store.get("agent_run")
    if isinstance(run, dict) and isinstance(run.get("id"), str):
        index.setdefault(LINK_RUN, {})[run["id"]] = run
    return index


def _resolve(
    index: Dict[str, Dict[str, Dict[str, Any]]],
    refs: Sequence[Tuple[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split ``(kind, id)`` references into resolved links and unresolved ones.

    A reference that does not resolve is never dropped and never guessed: it is
    reported with a bounded reason so a reader can see that the claim rests on
    something the store does not contain.
    """
    links: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for kind, ident in refs:
        if not isinstance(ident, str) or not ident:
            continue
        if ident in index.get(kind, {}):
            links.append({"kind": kind, "id": ident})
        else:
            unresolved.append({"kind": kind, "id": ident, "reason": REASON_DANGLING_LINK})
    return links, unresolved


def _claim(
    document_type: str,
    key: str,
    statement: str,
    provenance: str,
    index: Dict[str, Dict[str, Dict[str, Any]]],
    refs: Sequence[Tuple[str, Any]] = (),
    limitations: Sequence[str] = (),
) -> Dict[str, Any]:
    """Return one bounded, evidence-linked claim."""
    links, unresolved = _resolve(index, refs)
    return {
        "id": "claim:%s:%s" % (document_type, key),
        "statement": statement,
        "provenance": provenance,
        "links": links,
        "unresolved": unresolved,
        "limitations": list(limitations),
    }


def _count(store: Dict[str, Any], array: str) -> int:
    records = store.get(array)
    return len(records) if isinstance(records, list) else 0


# -- run context ---------------------------------------------------------


def _run_context(
    store: Dict[str, Any], run: Dict[str, Any]
) -> Dict[str, Any]:
    """Return the bounded run/work-package/baseline context every document carries.

    ``stale`` marks a store that is not a closed snapshot: a reader must be able
    to tell a finalized record from one that is still open before trusting it.
    """
    stale_reasons: List[str] = []
    if run.get("finalized") is not True:
        stale_reasons.append(REASON_NOT_FINALIZED)

    state = memory.run_state(store)
    return {
        "run_id": run.get("id"),
        "adapter": run.get("adapter"),
        "session_id": run.get("session_id"),
        "project_id": run.get("project_id"),
        "work_package_id": run.get("work_package_id"),
        "state": state,
        "success": memory.is_success(store),
        "finalized": run.get("finalized") is True,
        "stream_closed": run.get("stream_closed") is True,
        "ingest_sequence": run.get("ingest_sequence"),
        "rejection_sequence": run.get("rejection_sequence"),
        "quarantine_sequence": run.get("quarantine_sequence"),
        "terminal_event_id": run.get("terminal_event_id"),
        "stale": bool(stale_reasons),
        "stale_reasons": sorted(stale_reasons),
        "baseline": dict(BASELINE_UNSUPPORTED),
    }


def _origin_claim(
    document_type: str,
    index: Dict[str, Dict[str, Dict[str, Any]]],
    run: Dict[str, Any],
    evidence_origin: Optional[str],
) -> Dict[str, Any]:
    """Return the claim that states how this run's evidence was produced.

    The schema records no capture origin, so the projector may not assert that a
    terminal state was observed live. An operator may *declare* an origin; that
    declaration is then reported as the caller's word, never as a stored fact.
    """
    if isinstance(evidence_origin, str) and evidence_origin in ("live", "offline"):
        return _claim(
            document_type,
            "capture_origin",
            "Capture origin is declared by the caller as '%s'." % evidence_origin,
            PROV_REPORTED,
            index,
            [(LINK_RUN, run.get("id"))],
            limitations=[
                "caller-declared: " + REASON_ORIGIN_UNRECORDED,
                "a terminal state recorded from deterministic offline input is not "
                "live-session observation",
            ],
        )
    return _claim(
        document_type,
        "capture_origin",
        "The capture origin of this run is not recorded by the store.",
        PROV_INFERRED,
        index,
        [(LINK_RUN, run.get("id"))],
        limitations=[
            REASON_ORIGIN_UNRECORDED,
            "no claim in this document may be read as live-session observation",
        ],
    )


# -- document builders ---------------------------------------------------


def _session_summary(
    store: Dict[str, Any],
    index: Dict[str, Dict[str, Dict[str, Any]]],
    run: Dict[str, Any],
    evidence_origin: Optional[str],
) -> Dict[str, Any]:
    """Project the Session / Daily Summary document for one run."""
    dtype = DOC_SESSION_SUMMARY
    state = memory.run_state(store)
    claims: List[Dict[str, Any]] = []

    claims.append(
        _claim(
            dtype,
            "run_identity",
            "Run '%s' is namespaced by adapter '%s' and session '%s'."
            % (run.get("id"), run.get("adapter"), run.get("session_id")),
            PROV_OBSERVED,
            index,
            [(LINK_RUN, run.get("id"))],
        )
    )

    project = index.get(LINK_PROJECT, {}).get(run.get("project_id"))
    if isinstance(project, dict):
        claims.append(
            _claim(
                dtype,
                "project",
                "The run is bound to project '%s' (name: %s)."
                % (project.get("source_id"), project.get("name")),
                PROV_OBSERVED,
                index,
                [(LINK_PROJECT, project.get("id"))],
                limitations=["the project name is source-reported text"] if project.get("name") else [],
            )
        )
    else:
        claims.append(
            _claim(
                dtype,
                "project",
                "The run records no project binding.",
                PROV_INFERRED,
                index,
                [],
                limitations=["a project binding is optional in schema 1.0.0"],
            )
        )

    work_package = index.get(LINK_WORK_PACKAGE, {}).get(run.get("work_package_id"))
    if isinstance(work_package, dict):
        claims.append(
            _claim(
                dtype,
                "work_package",
                "The run is bound to work package '%s' (title: %s)."
                % (work_package.get("source_id"), work_package.get("title")),
                PROV_OBSERVED,
                index,
                [(LINK_WORK_PACKAGE, work_package.get("id"))],
                limitations=["the work-package title is source-reported text"] if work_package.get("title") else [],
            )
        )
    else:
        claims.append(
            _claim(
                dtype,
                "work_package",
                "The run records no work-package binding.",
                PROV_INFERRED,
                index,
                [],
                limitations=["a work-package binding is optional in schema 1.0.0"],
            )
        )

    state_limitations = []
    if state in STATE_LIMITATIONS:
        state_limitations.append(STATE_LIMITATIONS[state])
    if not run.get("terminal_event_id"):
        state_limitations.append(REASON_NO_TERMINAL_EVENT)
    claims.append(
        _claim(
            dtype,
            "terminal_state",
            "The run's reported terminal state is '%s'." % state,
            PROV_OBSERVED,
            index,
            [(LINK_RUN, run.get("id")), (LINK_EVENT, run.get("terminal_event_id"))],
            limitations=state_limitations,
        )
    )

    claims.append(
        _claim(
            dtype,
            "success",
            "The run is%s success." % ("" if memory.is_success(store) else " not"),
            PROV_OBSERVED,
            index,
            [(LINK_RUN, run.get("id"))],
            limitations=[]
            if memory.is_success(store)
            else ["only the validated terminal state 'completed' is success"],
        )
    )

    events = store.get("events") if isinstance(store.get("events"), list) else []
    by_type: Dict[str, int] = {}
    for event in events:
        if isinstance(event, dict):
            key = str(event.get("event_type"))
            by_type[key] = by_type.get(key, 0) + 1
    listing = ", ".join("%s=%d" % (k, by_type[k]) for k in sorted(by_type)) or "none"
    claims.append(
        _claim(
            dtype,
            "event_counts",
            "The run holds %d stored event(s): %s." % (len(events), listing),
            PROV_INFERRED,
            index,
            [(LINK_EVENT, e.get("id")) for e in events if isinstance(e, dict)],
        )
    )

    claims.append(
        _claim(
            dtype,
            "record_counts",
            "Stored child records: evidence=%d, decisions=%d, change_sets=%d, "
            "code_entity_links=%d."
            % (
                _count(store, "evidence"),
                _count(store, "decisions"),
                _count(store, "change_sets"),
                _count(store, "code_entity_links"),
            ),
            PROV_INFERRED,
            index,
            [],
        )
    )

    claims.append(
        _claim(
            dtype,
            "snapshot",
            "The store is%s a finalized snapshot and its stream is%s closed."
            % (
                "" if run.get("finalized") is True else " not",
                "" if run.get("stream_closed") is True else " not",
            ),
            PROV_OBSERVED,
            index,
            [(LINK_RUN, run.get("id"))],
        )
    )

    # A store that is not a closed snapshot is stale: its records may still be
    # appended to, so a reader must not treat this document as final.
    stale_reasons = _run_context(store, run)["stale_reasons"]
    claims.append(
        _claim(
            dtype,
            "staleness",
            "The store is%s stale." % ("" if stale_reasons else " not"),
            PROV_OBSERVED,
            index,
            [(LINK_RUN, run.get("id"))],
            limitations=[r for r in stale_reasons],
        )
    )

    claims.append(
        _claim(
            dtype,
            "baseline",
            "The code baseline this run executed against is not recorded.",
            PROV_INFERRED,
            index,
            [],
            limitations=[BASELINE_UNSUPPORTED["reason"]],
        )
    )

    claims.append(_origin_claim(dtype, index, run, evidence_origin))

    return {
        "document_type": dtype,
        "title": "Session Summary",
        "claims": claims,
    }


def _change_record(
    store: Dict[str, Any],
    index: Dict[str, Dict[str, Dict[str, Any]]],
    run: Dict[str, Any],
    evidence_origin: Optional[str],
) -> Dict[str, Any]:
    """Project the Change & Verification Record document."""
    dtype = DOC_CHANGE_RECORD
    claims: List[Dict[str, Any]] = []

    change_sets = store.get("change_sets") if isinstance(store.get("change_sets"), list) else []
    if change_sets:
        for position, change_set in enumerate(change_sets):
            if not isinstance(change_set, dict):
                continue
            paths = change_set.get("paths") if isinstance(change_set.get("paths"), list) else []
            claims.append(
                _claim(
                    dtype,
                    "change_set:%d" % position,
                    "Observed change set with %d path(s): %s."
                    % (len(paths), ", ".join(str(p) for p in paths) or "none"),
                    PROV_OBSERVED,
                    index,
                    [(LINK_CHANGE_SET, change_set.get("id"))],
                    limitations=["the change-set summary is source-reported text"]
                    if change_set.get("summary")
                    else [],
                )
            )
    else:
        claims.append(
            _claim(
                dtype,
                "change_set:none",
                "No change set was observed for this run.",
                PROV_INFERRED,
                index,
                [],
                limitations=[
                    "a run that changed nothing and a run whose changes were not "
                    "observed are indistinguishable here"
                ],
            )
        )

    links = (
        store.get("code_entity_links")
        if isinstance(store.get("code_entity_links"), list)
        else []
    )
    if links:
        rendered = ", ".join(
            "%s%s" % (r.get("path") or "", ("#" + str(r.get("symbol"))) if r.get("symbol") else "")
            for r in links
            if isinstance(r, dict)
        )
        claims.append(
            _claim(
                dtype,
                "code_entities",
                "Observed code entities: %s." % rendered,
                PROV_OBSERVED,
                index,
                [(LINK_CODE_ENTITY, r.get("id")) for r in links if isinstance(r, dict)],
            )
        )
    else:
        claims.append(
            _claim(
                dtype,
                "code_entities",
                "No code entity was observed for this run.",
                PROV_INFERRED,
                index,
                [],
            )
        )

    evidence = store.get("evidence") if isinstance(store.get("evidence"), list) else []
    if evidence:
        rendered = ", ".join(
            "%s:%s" % (r.get("kind"), r.get("artifact_ref") or "(no location)")
            for r in evidence
            if isinstance(r, dict)
        )
        claims.append(
            _claim(
                dtype,
                "verification_evidence",
                "Referenced artifacts: %s." % rendered,
                PROV_OBSERVED,
                index,
                [(LINK_EVIDENCE, r.get("id")) for r in evidence if isinstance(r, dict)],
                limitations=[
                    VERIFICATION_UNSUPPORTED,
                    "a content fingerprint is reported only as present, never as a value",
                ],
            )
        )
    else:
        claims.append(
            _claim(
                dtype,
                "verification_evidence",
                "No artifact reference was recorded for this run.",
                PROV_INFERRED,
                index,
                [],
                limitations=[VERIFICATION_UNSUPPORTED],
            )
        )

    claims.append(
        _claim(
            dtype,
            "verification_result",
            "No verification result is recorded for this run.",
            PROV_INFERRED,
            index,
            [],
            limitations=[VERIFICATION_UNSUPPORTED],
        )
    )

    return {
        "document_type": dtype,
        "title": "Change & Verification Record",
        "claims": claims,
    }


def _decision_record(
    store: Dict[str, Any],
    index: Dict[str, Dict[str, Dict[str, Any]]],
    run: Dict[str, Any],
    evidence_origin: Optional[str],
) -> Dict[str, Any]:
    """Project the Decision Record document."""
    dtype = DOC_DECISION_RECORD
    claims: List[Dict[str, Any]] = []

    decisions = store.get("decisions") if isinstance(store.get("decisions"), list) else []
    ordered = sorted(
        (d for d in decisions if isinstance(d, dict)),
        key=lambda d: str(d.get("id", "")),
    )
    if not ordered:
        claims.append(
            _claim(
                dtype,
                "decisions:none",
                "No decision was recorded for this run.",
                PROV_INFERRED,
                index,
                [],
                limitations=[
                    "a run that made no decision and a run whose decisions were not "
                    "captured are indistinguishable here"
                ],
            )
        )
    for position, decision in enumerate(ordered):
        claims.append(
            _claim(
                dtype,
                "decision:%d" % position,
                "Recorded decision: %s" % (decision.get("summary") or "(no summary)"),
                PROV_REPORTED,
                index,
                [(LINK_DECISION, decision.get("id"))],
                limitations=[
                    "source-reported rationale, retained after redaction and bounding; "
                    "it is not verified by any record"
                ],
            )
        )

    return {
        "document_type": dtype,
        "title": "Decision Record",
        "claims": claims,
    }


def _issues_and_actions(
    store: Dict[str, Any],
    index: Dict[str, Dict[str, Dict[str, Any]]],
    run: Dict[str, Any],
    evidence_origin: Optional[str],
) -> Dict[str, Any]:
    """Project the Known Issues & Next Actions document."""
    dtype = DOC_ISSUES_AND_ACTIONS
    state = memory.run_state(store)
    claims: List[Dict[str, Any]] = []

    rejections = store.get("rejections") if isinstance(store.get("rejections"), list) else []
    if rejections:
        for position, rejection in enumerate(sorted(
            (r for r in rejections if isinstance(r, dict)),
            key=lambda r: str(r.get("id", "")),
        )):
            claims.append(
                _claim(
                    dtype,
                    "rejection:%d" % position,
                    "The contract refused a record: %s." % rejection.get("reason"),
                    PROV_OBSERVED,
                    index,
                    [(LINK_REJECTION, rejection.get("id"))],
                )
            )
    else:
        claims.append(
            _claim(
                dtype,
                "rejections:none",
                "The contract refused no record for this run.",
                PROV_INFERRED,
                index,
                [],
            )
        )

    quarantines = (
        store.get("quarantines") if isinstance(store.get("quarantines"), list) else []
    )
    if quarantines:
        for position, quarantine in enumerate(sorted(
            (q for q in quarantines if isinstance(q, dict)),
            key=lambda q: str(q.get("id", "")),
        )):
            claims.append(
                _claim(
                    dtype,
                    "quarantine:%d" % position,
                    "A redelivery with a known identity carried different content and "
                    "was quarantined.",
                    PROV_OBSERVED,
                    index,
                    [(LINK_QUARANTINE, quarantine.get("id"))],
                    limitations=[
                        "conflicting content is deliberately not reproduced; only the "
                        "conflict is recorded"
                    ],
                )
            )
    else:
        claims.append(
            _claim(
                dtype,
                "quarantines:none",
                "No conflicting redelivery was quarantined for this run.",
                PROV_INFERRED,
                index,
                [],
            )
        )

    limitation = STATE_LIMITATIONS.get(state)
    claims.append(
        _claim(
            dtype,
            "terminal_limitation",
            "Terminal state '%s' is%s recorded as success."
            % (state, "" if memory.is_success(store) else " not"),
            PROV_OBSERVED,
            index,
            [(LINK_RUN, run.get("id"))],
            limitations=[limitation] if limitation else [],
        )
    )

    actions = STATE_NEXT_ACTIONS.get(state, ())
    claims.append(
        _claim(
            dtype,
            "next_actions",
            "Next actions: %s." % ("; ".join(actions) if actions else "none required"),
            PROV_INFERRED,
            index,
            [(LINK_RUN, run.get("id"))],
        )
    )

    claims.append(_origin_claim(dtype, index, run, evidence_origin))

    return {
        "document_type": dtype,
        "title": "Known Issues & Next Actions",
        "claims": claims,
    }


_BUILDERS = {
    DOC_SESSION_SUMMARY: _session_summary,
    DOC_CHANGE_RECORD: _change_record,
    DOC_DECISION_RECORD: _decision_record,
    DOC_ISSUES_AND_ACTIONS: _issues_and_actions,
}


# -- entry points --------------------------------------------------------


def project_store(
    store: Any,
    evidence_origin: Optional[str] = None,
    document_types: Optional[Sequence[str]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Dict[str, Any]]:
    """Project a bounded document set from one normalized store.

    Returns ``(document_set, error, report)``. Exactly one of ``document_set``
    and ``error`` is ``None``. A store whose version, generator or run record is
    not what this projector supports is refused outright rather than projected
    partially: a document built from a store it does not understand would be
    worse than no document.
    """
    report: Dict[str, Any] = {"documents": 0, "claims": 0}

    if evidence_origin is not None and evidence_origin not in ("live", "offline"):
        return None, REASON_ORIGIN_UNSUPPORTED, report
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE, report
    if store.get("schema_version") != memory.MEMORY_SCHEMA_VERSION:
        return None, REASON_UNSUPPORTED_VERSION, report
    if store.get("generator") != memory.MEMORY_GENERATOR:
        return None, REASON_UNSUPPORTED_GENERATOR, report

    run = store.get("agent_run")
    if not isinstance(run, dict) or not isinstance(run.get("id"), str):
        return None, REASON_NO_RUN, report

    wanted = list(document_types) if document_types else list(DOCUMENT_TYPES)
    for document_type in wanted:
        if document_type not in _BUILDERS:
            return None, "unknown document type: " + str(document_type), report

    index = _index(store)
    documents = {
        document_type: _BUILDERS[document_type](store, index, run, evidence_origin)
        for document_type in wanted
    }

    document_set = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "generator": DOCUMENT_GENERATOR,
        "memory_schema_version": store.get("schema_version"),
        "run": _run_context(store, run),
        "documents": documents,
        "provenance_taxonomy": dict(PROVENANCE_TAXONOMY),
        "unsupported_provenance": list(UNSUPPORTED_PROVENANCE),
        "limitations": sorted(
            {
                PAYLOAD_NOT_PROJECTED,
                VERIFICATION_UNSUPPORTED,
                REASON_ORIGIN_UNRECORDED,
            }
        ),
    }
    report["documents"] = len(documents)
    report["claims"] = sum(len(d.get("claims", [])) for d in documents.values())
    return document_set, None, report


def project_stores(
    stores: Sequence[Any],
    evidence_origin: Optional[str] = None,
    document_types: Optional[Sequence[str]] = None,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], Dict[str, Any]]:
    """Project an explicitly ordered, bounded set of stores.

    The set is caller-supplied and ordered by the caller. No date grouping is
    performed: schema 1.0.0 carries no reliable run clock, and inventing one
    would be exactly the inference this layer must not make.
    """
    sets: List[Dict[str, Any]] = []
    total_claims = 0
    for position, store in enumerate(stores):
        document_set, error, report = project_store(
            store, evidence_origin, document_types
        )
        if error is not None:
            return None, "run %d: %s" % (position, error), {"documents": 0, "claims": 0}
        sets.append(document_set)
        total_claims += int(report.get("claims", 0))
    return sets, None, {"documents": len(sets), "claims": total_claims}


def render(document_set: Dict[str, Any]) -> str:
    """Return the canonical serialization of a document set.

    Re-projecting identical input yields an identical string, so a document can
    be diffed, hashed or reviewed without a formatting step.
    """
    return memory.dumps(document_set)


def render_sets(document_sets: Sequence[Dict[str, Any]]) -> str:
    """Return the canonical serialization of an ordered set of document sets.

    The envelope is a plain list in the caller's order: this layer performs no
    grouping, sorting or bucketing of its own.
    """
    return memory.dumps(
        {
            "schema_version": DOCUMENT_SCHEMA_VERSION,
            "generator": DOCUMENT_GENERATOR,
            "set_count": len(list(document_sets)),
            "document_sets": list(document_sets),
        }
    )


# -- exact typed record view --------------------------------------------
#
# The read boundary resolves exactly one supporting record and returns it
# through this allowlist. The view is built by iterating the allowlist and
# reading named fields, never by iterating the record, so a field that is not
# named here cannot cross the boundary even if the contract later stores it.
# Nothing content-bearing is reachable: an event's hook payload, a content
# fingerprint, a quarantine fingerprint and an evidence digest are all absent.

# The record kinds a support target may name. These are the projector's link
# kinds, so a claim reference maps onto a record request without translation.
RECORD_VIEW_KINDS = (
    LINK_RUN,
    LINK_PROJECT,
    LINK_WORK_PACKAGE,
    LINK_EVENT,
    LINK_EVIDENCE,
    LINK_DECISION,
    LINK_CHANGE_SET,
    LINK_CODE_ENTITY,
    LINK_REJECTION,
    LINK_QUARANTINE,
)

# Link kind -> store array. ``run`` names the single ``agent_run`` record.
_KIND_TO_ARRAY = {
    LINK_PROJECT: "projects",
    LINK_WORK_PACKAGE: "work_packages",
    LINK_EVENT: "events",
    LINK_EVIDENCE: "evidence",
    LINK_DECISION: "decisions",
    LINK_CHANGE_SET: "change_sets",
    LINK_CODE_ENTITY: "code_entity_links",
    LINK_REJECTION: "rejections",
    LINK_QUARANTINE: "quarantines",
}

# The exact normalized fields each kind may expose. Every field is canonical and
# already redacted, bounded and path-policed by the contract.
RECORD_VIEW_FIELDS = {
    LINK_RUN: (
        "id", "record_kind", "adapter", "session_id", "project_id",
        "work_package_id", "state", "finalized", "stream_closed",
        "ingest_sequence", "terminal_event_id",
    ),
    LINK_PROJECT: ("id", "record_kind", "source_id", "name"),
    LINK_WORK_PACKAGE: ("id", "record_kind", "project_id", "source_id", "title"),
    LINK_EVENT: (
        "id", "record_kind", "run_id", "event_type", "outcome", "ingest_ordinal",
        "source_sequence", "transition", "change_set_id", "evidence_ids",
        "decision_ids", "code_entity_link_ids",
    ),
    LINK_EVIDENCE: (
        "id", "record_kind", "run_id", "kind", "source_id", "artifact_ref", "bytes",
    ),
    LINK_DECISION: ("id", "record_kind", "run_id", "source_id", "summary", "decided_at"),
    LINK_CHANGE_SET: (
        "id", "record_kind", "run_id", "source_id", "paths", "entity_refs", "summary",
    ),
    LINK_CODE_ENTITY: ("id", "record_kind", "run_id", "path", "symbol", "entity_kind"),
    LINK_REJECTION: (
        "id", "record_kind", "run_id", "reason", "event_ref", "rejection_ordinal",
        "ingest_sequence",
    ),
    LINK_QUARANTINE: (
        "id", "record_kind", "run_id", "event_id", "reason", "quarantine_ordinal",
        "ingest_sequence",
    ),
}

# Fields whose value is the source's own reported text rather than a fact the
# contract validated, so a reader is told which values are reported.
REPORTED_VIEW_FIELDS = {
    LINK_DECISION: ("summary",),
    LINK_CHANGE_SET: ("summary",),
    LINK_PROJECT: ("name",),
    LINK_WORK_PACKAGE: ("title",),
}

# Kinds that carry a content digest in the store. Only *presence* is exposed:
# a digest is a fingerprint of content this layer must not hand out.
_DIGEST_BEARING_KINDS = (LINK_EVIDENCE,)

# Bounded, content-free reasons for a record view that cannot be produced.
REASON_KIND_NOT_SUPPORTED = "the record kind is not supported by the read boundary"
REASON_RECORD_ID_INVALID = "the requested record identity is not usable"
REASON_RECORD_NOT_FOUND = "the requested record is not present in this run"


def _view_fields(record: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Return the allowlisted projection of ``record`` for ``kind``."""
    fields: Dict[str, Any] = {}
    for name in RECORD_VIEW_FIELDS[kind]:
        value = record.get(name)
        if name in ("paths", "entity_refs", "evidence_ids", "decision_ids",
                    "code_entity_link_ids"):
            value = list(value) if isinstance(value, list) else []
        elif isinstance(value, dict):
            value = dict(value)
        fields[name] = value
    return fields


def _owning_run_id(store: Dict[str, Any], record: Dict[str, Any]) -> Optional[str]:
    """Return the run a record belongs to.

    A record that carries its own ``run_id`` names its run. A run-scoped
    descriptor -- a project or a work package -- carries none, so its owning run
    is the store's own run rather than the descriptor's identity.
    """
    owned = record.get("run_id")
    if isinstance(owned, str) and owned:
        return owned
    run = store.get("agent_run")
    if isinstance(run, dict) and isinstance(run.get("id"), str):
        return run["id"]
    return None


def record_view(
    store: Any, kind: Any, record_id: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(view, error)`` for one exact typed record inside one run.

    Resolution is by the exact identity ``(kind, record_id)`` against *this*
    store only, so a record id belonging to another run cannot resolve here and
    no substitute record is ever returned. Exactly one of ``view`` and ``error``
    is ``None``.
    """
    if not isinstance(store, dict):
        return None, REASON_NOT_A_STORE
    if not isinstance(kind, str) or kind not in RECORD_VIEW_KINDS:
        return None, REASON_KIND_NOT_SUPPORTED
    if not isinstance(record_id, str) or not record_id.strip():
        return None, REASON_RECORD_ID_INVALID
    record_id = record_id.strip()

    if kind == LINK_RUN:
        run = store.get("agent_run")
        candidates = [run] if isinstance(run, dict) else []
    else:
        array = store.get(_KIND_TO_ARRAY[kind])
        candidates = array if isinstance(array, list) else []

    for record in candidates:
        if not isinstance(record, dict) or record.get("id") != record_id:
            continue
        view = {
            "kind": kind,
            "record_id": record_id,
            "run_id": _owning_run_id(store, record),
            "fields": _view_fields(record, kind),
            "reported_fields": list(REPORTED_VIEW_FIELDS.get(kind, ())),
            "digest_present": (
                isinstance(record.get("digest"), str) and bool(record.get("digest"))
                if kind in _DIGEST_BEARING_KINDS
                else None
            ),
        }
        return view, None

    return None, REASON_RECORD_NOT_FOUND
