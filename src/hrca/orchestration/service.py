"""The bounded operations the boundary exposes for the one-scan slice.

Each function here is one operation from the Grill's list — save a plan
revision, confirm a plan, run the scan job, read the scoped workflow, append a
review decision — and each is written so the rules survive a restart:

* a Run is claimed in the store *before* the scanner is touched, so a duplicate
  dispatch or a dropped response cannot produce a second scan;
* the scanner is reached only after the project, confirmation, exact revision,
  source binding and capability allowlist have all been checked;
* terminal facts and evidence are published in one transaction, so a reported
  success always has the records behind it;
* reading never rewrites a live run, and recovery marks an unfinished run
  unknown only once its executor process is demonstrably gone.

Nothing here reads a credential, opens a network connection, dispatches a
provider, executes a command, runs a container or writes to the project.
"""

from __future__ import annotations

import datetime as _datetime
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from hrca.core import identity, workspace

from . import domain, manifest as manifest_mod, store as store_mod

#: The bounded reasons this slice refuses with. Fixed sentences; no caller text.
REFUSAL_NO_PROJECT = "no project is open"
REFUSAL_NO_PLAN = "no plan has been saved for this project"
REFUSAL_NOT_CONFIRMED = "the plan revision is not confirmed"
REFUSAL_REVISION_MISMATCH = "the confirmed revision is not the one requested"
REFUSAL_UNSUPPORTED_CAPABILITY = "this slice executes only the read-only source scan"
REFUSAL_SOURCE_CHANGED = "the bound source changed since the plan was made"
REFUSAL_SOURCE_UNOBSERVABLE = "the scoped source could not be observed consistently"
REFUSAL_EVIDENCE_NOT_CURRENT = "the evidence is not current for the bound source"
REFUSAL_DECISION_OUTCOME = "the requested review outcome is not supported"
REFUSAL_ALREADY_DECIDED = "a review decision is already recorded for this run"
REFUSAL_ACTOR_REQUIRED = "a review decision must name who made it"
REFUSAL_NO_TERMINAL_RUN = "there is no terminal execution to review"
REFUSAL_STORE_LOCATION = store_mod.REASON_STORE_LOCATION


class Refused(Exception):
    """A bounded refusal. ``reason`` is safe to surface verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> str:
    """Return the current instant as an ISO-8601 UTC string."""
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def project_id_for(root: str) -> str:
    """Return the project identity for a canonical root.

    Reuses the product's existing workspace identity rather than inventing a
    second one, so orchestration records and Twin records name a project the
    same way.
    """
    canonical = os.path.realpath(os.path.abspath(root))
    return identity.workspace_id_for(canonical)


def _scanner_identity() -> Tuple[str, Dict[str, str]]:
    return manifest_mod.scanner_identity()


def _store(store_base: str, root: Optional[str]) -> store_mod.OrchestrationStore:
    try:
        store_mod.assert_outside_project(store_base, root)
    except store_mod.StoreError as error:
        raise Refused(error.reason) from error
    return store_mod.OrchestrationStore(store_base)


def _observe(root: str, scope: domain.Scope) -> domain.SourceManifest:
    schema, grammar = _scanner_identity()
    return manifest_mod.observe_scope(
        root, scope, scanner_schema=schema, grammar=grammar, observed_at=_now()
    )


def _current_manifest_id(root: str, scope: domain.Scope) -> Tuple[Optional[str], Optional[str]]:
    schema, grammar = _scanner_identity()
    return manifest_mod.observe_current(
        root, scope, scanner_schema=schema, grammar=grammar, observed_at=_now()
    )


# ---------------------------------------------------------------------------
# Scaffold
# ---------------------------------------------------------------------------
def scaffold_revision(
    *,
    project_id: str,
    plan_id: str,
    revision: int,
    goal: str,
    scope: domain.Scope,
    manifest_id: str,
    accepted_baseline_ref: Optional[str],
    created_at: str,
    provenance: str = domain.PROVENANCE_SCAFFOLD,
    extra_requirements: Sequence[str] = (),
) -> domain.PlanRevision:
    """Return the deterministic one-scan scaffold for a goal.

    Deterministic: the same goal, scope and manifest always produce the same
    criteria. It proposes inspection only — it does not claim to implement or
    to understand an arbitrary coding request.
    """
    criteria: List[domain.Criterion] = []
    for index, predicate_id in enumerate(domain.SCAFFOLD_PREDICATES, start=1):
        criteria.append(
            domain.Criterion(
                criterion_id=f"c{index}",
                text=domain.PREDICATE_LABELS[predicate_id],
                predicate_id=predicate_id,
            )
        )
    for offset, text in enumerate(extra_requirements, start=len(criteria) + 1):
        # A developer's own requirement. It has no predicate, so no supported
        # check can ever cover it — the scan finishing is not evidence about it.
        criteria.append(
            domain.Criterion(criterion_id=f"c{offset}", text=str(text), predicate_id=None)
        )

    job_spec = domain.JobSpec(
        capability=domain.CAPABILITY_SOURCE_SCAN,
        executor=domain.EXECUTOR_LOCAL_SCANNER,
        scope=scope,
        criterion_ids=tuple(c.criterion_id for c in criteria),
    )
    return domain.PlanRevision(
        plan_id=plan_id,
        project_id=project_id,
        revision=revision,
        goal=goal,
        provenance=provenance,
        scope=scope,
        manifest_id=manifest_id,
        criteria=tuple(criteria),
        job_spec=job_spec,
        phase=domain.PLAN_DRAFT,
        accepted_baseline_ref=accepted_baseline_ref,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def save_plan(
    *,
    store_base: str,
    root: Optional[str],
    goal: str,
    scope: domain.Scope,
    accepted_baseline_ref: Optional[str],
    expected_revision: int,
    idempotency_key: str,
    extra_requirements: Sequence[str] = (),
    provenance: str = domain.PROVENANCE_SCAFFOLD,
) -> Dict[str, Any]:
    """Observe the scope and append one draft plan revision."""
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    store = _store(store_base, root)
    store.ensure_schema()

    observed = _observe(root, scope)
    project_id = project_id_for(root)
    latest = store.latest_revision(project_id)
    plan_id = latest.plan_id if latest else store_mod.new_id("plan")
    revision_number = (latest.revision if latest else 0) + 1

    revision = scaffold_revision(
        project_id=project_id,
        plan_id=plan_id,
        revision=revision_number,
        goal=goal.strip(),
        scope=scope,
        manifest_id=observed.manifest_id,
        accepted_baseline_ref=accepted_baseline_ref,
        created_at=_now(),
        provenance=provenance,
        extra_requirements=extra_requirements,
    )
    problems = domain.validate_revision(revision)
    if problems:
        raise Refused(problems[0])

    result = store.save_revision(
        revision,
        expected_revision=int(expected_revision or 0),
        idempotency_key=idempotency_key,
        now=_now(),
    )
    return {
        **result,
        "plan_id": plan_id,
        "project_id": project_id,
        "manifest_id": observed.manifest_id,
        "manifest_complete": observed.complete,
        "file_count": observed.file_count,
    }


def confirm_plan(
    *,
    store_base: str,
    root: Optional[str],
    plan_id: str,
    expected_digest: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    """Confirm one exact revision and create its job. Dispatches nothing."""
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    store = _store(store_base, root)
    store.ensure_schema()
    project_id = project_id_for(root)

    revisions = store.list_revisions(project_id, plan_id)
    if not revisions:
        raise Refused(REFUSAL_NO_PLAN)
    revision = revisions[-1]
    if revision.content_digest != expected_digest:
        raise Refused(REFUSAL_REVISION_MISMATCH)
    if revision.job_spec.capability != domain.CAPABILITY_SOURCE_SCAN:
        raise Refused(REFUSAL_UNSUPPORTED_CAPABILITY)

    # The source must still be the source this revision was written against:
    # confirming a plan whose scope has already moved would pin a stale binding.
    current_id, _failure = _current_manifest_id(root, revision.scope)
    if current_id is not None and current_id != revision.manifest_id:
        raise Refused(REFUSAL_SOURCE_CHANGED)

    job = domain.JobRecord(
        job_id=store_mod.new_id("job"),
        project_id=project_id,
        plan_id=plan_id,
        plan_revision=revision.revision,
        capability=revision.job_spec.capability,
        executor=revision.job_spec.executor,
        scope=revision.scope,
        manifest_id=revision.manifest_id,
        criterion_ids=revision.criterion_ids,
        state=domain.JOB_READY,
    )
    result = store.confirm_revision(
        revision,
        job,
        expected_digest=expected_digest,
        idempotency_key=idempotency_key,
        now=_now(),
        decision_id=store_mod.new_id("decision"),
    )
    return {**result, "project_id": project_id}


def run_scan(
    *,
    store_base: str,
    root: Optional[str],
    plan_id: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    """Validate, claim one execution, then run the existing deterministic scanner."""
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    store = _store(store_base, root)
    store.ensure_schema()
    project_id = project_id_for(root)

    state = store.read_state(project_id)
    revision = _confirmed_revision(state, plan_id)
    if revision is None:
        raise Refused(REFUSAL_NOT_CONFIRMED)
    job = state["job"]
    if job is None or job.plan_id != plan_id:
        raise Refused(REFUSAL_NOT_CONFIRMED)

    # A repeated dispatch is one effect. If this key already claimed an
    # execution, that execution *is* the answer: the scanner is not reached
    # again, and the run is not restarted. The same key against a different
    # plan is a conflict rather than a silent reuse.
    existing = store.run_by_idempotency_key(project_id, idempotency_key)
    if existing is not None:
        if existing.plan_id != plan_id:
            raise Refused(store_mod.REASON_CONFLICT)
        return {
            "run_id": existing.run_id,
            "outcome": existing.outcome,
            "project_id": project_id,
            "job_id": existing.job_id,
            "attempt": existing.attempt,
            "replayed": True,
        }

    if job.state == domain.JOB_RUNNING and _active_run(state) is not None:
        # A live execution is not interrupted by a second request. The store's
        # unique index would refuse anyway; refusing here is the bounded reason.
        raise Refused(store_mod.REASON_ACTIVE_RUN)
    if job.capability != domain.CAPABILITY_SOURCE_SCAN:
        raise Refused(REFUSAL_UNSUPPORTED_CAPABILITY)

    # Re-check the source before dispatch: a scope that moved since the plan was
    # confirmed must not be dispatched against the old binding.
    current_id, failure = _current_manifest_id(root, revision.scope)
    if failure is not None or current_id is None:
        raise Refused(REFUSAL_SOURCE_UNOBSERVABLE)
    if current_id != revision.manifest_id:
        raise Refused(REFUSAL_SOURCE_CHANGED)

    claimed = store.claim_run(
        domain.AgentRun(
            run_id=store_mod.new_id("run"),
            project_id=project_id,
            job_id=job.job_id,
            plan_id=plan_id,
            plan_revision=revision.revision,
            attempt=0,
            idempotency_key=idempotency_key,
            executor=domain.EXECUTOR_LOCAL_SCANNER,
            executor_version=_scanner_identity()[0],
            manifest_id=revision.manifest_id,
            outcome=domain.RUN_RUNNING,
            started_at=_now(),
            executor_pid=os.getpid(),
        ),
        idempotency_key=idempotency_key,
        now=_now(),
    )
    run_id = str(claimed["run_id"])

    outcome, reason, evidence, artifacts = _execute_scan(
        root=root, project_id=project_id, revision=revision, job=job, run_id=run_id
    )
    job_state = (
        domain.JOB_NEEDS_REVIEW
        if outcome in (domain.RUN_SUCCEEDED, domain.RUN_INTERRUPTED_UNKNOWN)
        else domain.JOB_READY
    )
    published = store.publish_terminal(
        run_id,
        project_id,
        outcome=outcome,
        reason=reason,
        evidence=evidence,
        artifacts=artifacts,
        job_state=job_state,
        now=_now(),
    )
    return {**published, "project_id": project_id, "job_id": job.job_id, "attempt": claimed["attempt"]}


def _execute_scan(
    *,
    root: str,
    project_id: str,
    revision: domain.PlanRevision,
    job: domain.JobRecord,
    run_id: str,
) -> Tuple[str, str, List[domain.EvidenceRecord], List[Tuple[str, bytes]]]:
    """Run the deterministic scanner and turn its document into evidence rows."""
    from hrca.source import scanner

    try:
        document = scanner.scan_directory(root)
    except Exception:  # pragma: no cover - the scanner is pure and does not raise in practice
        return domain.RUN_FAILED, "the deterministic scan could not be completed", [], []

    observed = _observe(root, revision.scope)
    files = list(document.get("files") or [])
    parse_errors = list(document.get("parse_errors") or [])
    relations = list(document.get("relations") or [])
    unresolved = [r for r in relations if str(r.get("status")) == "unresolved"]
    syntax_errors = [f for f in files if str(f.get("syntax_status")) == "error"]
    grammar = dict(document.get("grammar") or {})
    schema = str(document.get("schema_version", ""))

    counts = {
        "files": len(files),
        "symbols": len(document.get("symbols") or []),
        "relations": len(relations),
        "parse_errors": len(parse_errors),
        "unresolved": len(unresolved),
        "bound_files": observed.file_count,
    }

    # The bounded artifact is the scanner's own document — a derived record of
    # the scan, never a copy of the source. Nothing here persists file content.
    artifact_name = f"{run_id.replace(':', '_')}.json"
    artifact_bytes = domain.canonical_bytes(
        {
            "counts": counts,
            "grammar": grammar,
            "scanner_schema": schema,
            "manifest_id": observed.manifest_id,
            "parse_errors": [
                {
                    "file": str(item.get("file", "")),
                    "message": str(item.get("message", "")),
                    "lineno": item.get("lineno"),
                }
                for item in parse_errors
            ],
        }
    )
    artifact_digest = identity.sha256_hex(artifact_bytes)
    artifact_ref = os.path.join(store_mod.EVIDENCE_DIR_NAME, artifact_name)

    now = _now()

    def _evidence(predicate_id: str, result: str, limitation: str) -> domain.EvidenceRecord:
        return domain.EvidenceRecord(
            evidence_id=store_mod.new_id("ev"),
            project_id=project_id,
            plan_id=revision.plan_id,
            job_id=job.job_id,
            run_id=run_id,
            kind="scan_observation",
            predicate_id=predicate_id,
            result=result,
            manifest_id=observed.manifest_id,
            limitation=limitation,
            counts=counts,
            grammar=grammar,
            scanner_schema=schema,
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            created_at=now,
        )

    limitation = (
        f"{len(parse_errors)} file(s) could not be parsed and "
        f"{len(unresolved)} relation(s) are unresolved; the scan records these, "
        "it does not resolve them."
        if (parse_errors or unresolved)
        else ""
    )
    binding_limitation = "" if observed.complete else "the scope was not observed consistently"

    evidence = [
        _evidence(
            domain.CRITERION_SCAN_EVIDENCE_BOUND,
            domain.RESULT_SATISFIED if observed.complete else domain.RESULT_UNSATISFIED,
            binding_limitation,
        ),
        _evidence(
            domain.CRITERION_SOURCE_BINDING_STABLE,
            domain.RESULT_SATISFIED if observed.complete else domain.RESULT_UNSATISFIED,
            binding_limitation,
        ),
        _evidence(
            domain.CRITERION_LIMITATIONS_PRESERVED,
            domain.RESULT_SATISFIED,
            limitation,
        ),
        _evidence(
            domain.CRITERION_TERMINAL_OUTCOME_KNOWN,
            domain.RESULT_SATISFIED,
            "",
        ),
    ]
    return domain.RUN_SUCCEEDED, "", evidence, [(artifact_name, artifact_bytes)]


def append_decision(
    *,
    store_base: str,
    root: Optional[str],
    run_id: str,
    outcome: str,
    actor: str,
    reason: str,
    idempotency_key: str,
) -> Dict[str, Any]:
    """Append one human scan-review decision. It adopts nothing."""
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    if not str(actor).strip():
        raise Refused(REFUSAL_ACTOR_REQUIRED)
    if outcome not in domain.REVIEW_OUTCOMES:
        raise Refused(REFUSAL_DECISION_OUTCOME)

    store = _store(store_base, root)
    store.ensure_schema()
    project_id = project_id_for(root)
    state = store.read_state(project_id)

    run = _run_by_id(state, run_id)
    if run is None or not run.is_terminal:
        raise Refused(REFUSAL_NO_TERMINAL_RUN)

    # A repeated request is one effect: the same key against the same run and
    # outcome returns the decision it already recorded. A *different* decision
    # for a run that already has one is refused rather than appended silently.
    for existing in state["decisions"]:
        if existing.idempotency_key != idempotency_key:
            continue
        if (
            existing.kind == domain.DECISION_SCAN_REVIEW
            and existing.run_id == run_id
            and existing.outcome == outcome
        ):
            return {
                "decision_id": existing.decision_id,
                "outcome": existing.outcome,
                "project_id": project_id,
                "replayed": True,
            }
        raise Refused(store_mod.REASON_CONFLICT)

    if _scan_decision(state, run_id) is not None:
        raise Refused(REFUSAL_ALREADY_DECIDED)

    revision = _revision_of(state, run.plan_id)
    if revision is None:
        raise Refused(REFUSAL_NO_PLAN)
    evidence = [e for e in state["evidence"] if e.run_id == run_id]
    freshness = _freshness(root, revision, run, evidence)

    if outcome == domain.OUTCOME_ACKNOWLEDGED:
        review = domain.build_review(revision, state["job"], run, evidence, freshness=freshness)
        if not review.can_acknowledge:
            raise Refused(review.blocking[0] if review.blocking else REFUSAL_EVIDENCE_NOT_CURRENT)

    decision = domain.DecisionRecord(
        decision_id=store_mod.new_id("decision"),
        project_id=project_id,
        kind=domain.DECISION_SCAN_REVIEW,
        outcome=outcome,
        actor=str(actor).strip(),
        target_digest=_evidence_set_digest(evidence),
        idempotency_key=idempotency_key,
        run_id=run_id,
        evidence_set_digest=_evidence_set_digest(evidence),
        reason=str(reason or ""),
        created_at=_now(),
    )
    result = store.append_decision(decision)
    return {**result, "project_id": project_id}


def read_workflow(
    *,
    store_base: str,
    root: Optional[str],
    recover_orphans: bool = True,
) -> Dict[str, Any]:
    """Return the scoped workflow, its review projection and its resume.

    Reading is a query: it never dispatches, never rewrites a live run and
    never refreshes a binding. Freshness is computed now, against the source as
    it is now, and reported — never read back from a stored boolean.
    """
    if not root:
        raise Refused(REFUSAL_NO_PROJECT)
    store = _store(store_base, root)
    store.ensure_schema()
    project_id = project_id_for(root)

    if recover_orphans:
        _recover_orphans(store, project_id)

    state = store.read_state(project_id)
    revision = state["revisions"][-1] if state["revisions"] else None
    if revision is None:
        return {
            "project_id": project_id,
            "has_plan": False,
            "review": None,
            "resume": None,
        }

    job = state["job"]
    if job is None:
        job = domain.JobRecord(
            job_id="",
            project_id=project_id,
            plan_id=revision.plan_id,
            plan_revision=revision.revision,
            capability=revision.job_spec.capability,
            executor=revision.job_spec.executor,
            scope=revision.scope,
            manifest_id=revision.manifest_id,
            criterion_ids=revision.criterion_ids,
        )

    run = _latest_run(state)
    evidence = [e for e in state["evidence"] if run is not None and e.run_id == run.run_id]
    freshness = _freshness(root, revision, run, evidence)
    decision = _scan_decision(state, run.run_id) if run is not None else None

    review = domain.build_review(revision, job, run, evidence, freshness=freshness, decision=decision)
    resume = domain.build_resume(
        revision,
        job,
        run,
        review,
        freshness=freshness,
        decision=decision,
        observed_file_count=_manifest_file_count(state, revision),
    )
    return {
        "project_id": project_id,
        "has_plan": True,
        "plan_id": revision.plan_id,
        "plan_revision": revision.revision,
        "plan_digest": revision.content_digest,
        "plan_phase": revision.phase,
        "job_id": job.job_id,
        "job_state": job.state,
        "run_id": run.run_id if run else "",
        "review": review.as_payload(),
        "resume": resume.as_payload(),
        "revision": revision.as_payload(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _confirmed_revision(state: Mapping[str, Any], plan_id: str) -> Optional[domain.PlanRevision]:
    for revision in reversed(list(state.get("revisions", ()))):
        if revision.plan_id == plan_id and revision.phase == domain.PLAN_CONFIRMED:
            return revision
    return None


def _revision_of(state: Mapping[str, Any], plan_id: str) -> Optional[domain.PlanRevision]:
    for revision in reversed(list(state.get("revisions", ()))):
        if revision.plan_id == plan_id:
            return revision
    return None


def _latest_run(state: Mapping[str, Any]) -> Optional[domain.AgentRun]:
    runs = list(state.get("runs", ()))
    return runs[-1] if runs else None


def _run_by_id(state: Mapping[str, Any], run_id: str) -> Optional[domain.AgentRun]:
    for run in state.get("runs", ()):
        if run.run_id == run_id:
            return run
    return None


def _active_run(state: Mapping[str, Any]) -> Optional[domain.AgentRun]:
    for run in state.get("runs", ()):
        if run.outcome == domain.RUN_RUNNING:
            return run
    return None


def _scan_decision(state: Mapping[str, Any], run_id: str) -> Optional[domain.DecisionRecord]:
    for decision in state.get("decisions", ()):
        if decision.kind == domain.DECISION_SCAN_REVIEW and decision.run_id == run_id:
            return decision
    return None


def _evidence_set_digest(evidence: Sequence[domain.EvidenceRecord]) -> str:
    return domain.digest(
        sorted(
            ({"id": e.evidence_id, "predicate": e.predicate_id, "result": e.result} for e in evidence),
            key=lambda item: item["id"],
        )
    )


def _manifest_file_count(state: Mapping[str, Any], revision: domain.PlanRevision) -> int:
    for run in reversed(list(state.get("runs", ()))):
        if run.plan_id == revision.plan_id and run.manifest_id == revision.manifest_id:
            for evidence in state.get("evidence", ()):
                if evidence.run_id == run.run_id:
                    return int(evidence.counts.get("bound_files") or 0)
    return 0


def _freshness(
    root: str,
    revision: domain.PlanRevision,
    run: Optional[domain.AgentRun],
    evidence: Sequence[domain.EvidenceRecord],
) -> str:
    """Derive freshness against the source as it is now."""
    if run is None:
        return domain.FRESH_MISSING
    current_id, failure = _current_manifest_id(root, revision.scope)
    if failure is not None:
        return domain.FRESH_UNKNOWN
    return domain.freshness_of(run.manifest_id, current_id, observation_failed=False)


def _recover_orphans(store: store_mod.OrchestrationStore, project_id: str) -> None:
    """Mark an unfinished run unknown, but only once its executor is gone.

    A live executor's run is left exactly as it is: a reader must not rewrite a
    run that is still running. This never retries and never reports success.
    """
    state = store.read_state(project_id)
    active = _active_run(state)
    if active is None or active.executor_pid <= 0:
        return
    if _process_alive(active.executor_pid):
        return
    store.publish_terminal(
        active.run_id,
        project_id,
        outcome=domain.RUN_INTERRUPTED_UNKNOWN,
        reason="the previous execution did not finish and its executor is gone",
        evidence=[],
        artifacts=[],
        job_state=domain.JOB_NEEDS_REVIEW,
        now=_now(),
    )


def _process_alive(pid: int) -> bool:
    """Return whether ``pid`` is still running. Unknown counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


__all__ = [
    "Refused",
    "project_id_for",
    "scaffold_revision",
    "save_plan",
    "confirm_plan",
    "run_scan",
    "append_decision",
    "read_workflow",
    "REFUSAL_NO_PROJECT",
    "REFUSAL_NO_PLAN",
    "REFUSAL_NOT_CONFIRMED",
    "REFUSAL_REVISION_MISMATCH",
    "REFUSAL_UNSUPPORTED_CAPABILITY",
    "REFUSAL_SOURCE_CHANGED",
    "REFUSAL_SOURCE_UNOBSERVABLE",
    "REFUSAL_EVIDENCE_NOT_CURRENT",
    "REFUSAL_DECISION_OUTCOME",
    "REFUSAL_ALREADY_DECIDED",
    "REFUSAL_ACTOR_REQUIRED",
    "REFUSAL_NO_TERMINAL_RUN",
    "REFUSAL_STORE_LOCATION",
]
