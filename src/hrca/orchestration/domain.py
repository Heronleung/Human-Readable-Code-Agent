"""Pure domain for the persisted orchestration backbone.

Everything here is a value: frozen dataclasses, bounded vocabularies, code-owned
acceptance predicates and the two projections (Review and Resume). There is no
I/O, no clock, no store and no scanner import, so the rules a reviewer cares
about can be read — and tested — in one file without a database or a filesystem.

Three rules are enforced here rather than in the store or the UI, because they
are what the slice is *for*:

* **Completion is not acceptance.** An ``AgentRun`` with a terminal outcome is
  an execution fact. It never satisfies a requirement, never adopts source and
  never moves an accepted baseline.
* **A criterion is covered only by the evidence of its own predicate.** The four
  code-owned predicates below are the only things that can satisfy a check. A
  developer's own prose is a first-class requirement that stays ``uncovered``
  however the scan turns out, and a scan that parsed every file proves nothing
  about a claim that the application works.
* **Freshness is derived, never stored as a boolean.** Evidence is ``current``
  only while the manifest it is bound to is still the manifest of the bound
  scope; storing ``fresh=true`` would let a stale record keep asserting its own
  freshness after the source moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from hrca.core import identity

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------
PLAN_DRAFT = "draft"
PLAN_CONFIRMED = "confirmed"
PLAN_SUPERSEDED = "superseded"
PLAN_PHASES: Tuple[str, ...] = (PLAN_DRAFT, PLAN_CONFIRMED, PLAN_SUPERSEDED)

JOB_READY = "ready"
JOB_RUNNING = "running"
JOB_NEEDS_REVIEW = "needs_review"
JOB_REVIEWED = "reviewed"
JOB_STATES: Tuple[str, ...] = (JOB_READY, JOB_RUNNING, JOB_NEEDS_REVIEW, JOB_REVIEWED)

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_INTERRUPTED_UNKNOWN = "interrupted_unknown"
RUN_OUTCOMES: Tuple[str, ...] = (
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_FAILED,
    RUN_INTERRUPTED_UNKNOWN,
)
#: Outcomes an execution can end in. ``running`` is the only non-terminal one.
TERMINAL_RUN_OUTCOMES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_INTERRUPTED_UNKNOWN})

FRESH_CURRENT = "current"
FRESH_STALE = "stale"
FRESH_UNKNOWN = "unknown"
FRESH_MISSING = "missing"
FRESHNESS_STATES: Tuple[str, ...] = (FRESH_CURRENT, FRESH_STALE, FRESH_UNKNOWN, FRESH_MISSING)

DECISION_PLAN_CONFIRMATION = "plan_confirmation"
DECISION_SCAN_REVIEW = "scan_review"
DECISION_KINDS: Tuple[str, ...] = (DECISION_PLAN_CONFIRMATION, DECISION_SCAN_REVIEW)

OUTCOME_ACKNOWLEDGED = "acknowledged"
OUTCOME_REQUEST_CHANGES = "request_changes"
OUTCOME_REJECTED = "rejected"
OUTCOME_ESCALATED = "escalated"
REVIEW_OUTCOMES: Tuple[str, ...] = (
    OUTCOME_ACKNOWLEDGED,
    OUTCOME_REQUEST_CHANGES,
    OUTCOME_REJECTED,
    OUTCOME_ESCALATED,
)

RESULT_SATISFIED = "satisfied"
RESULT_UNSATISFIED = "unsatisfied"
RESULT_UNKNOWN = "unknown"
EVIDENCE_RESULTS: Tuple[str, ...] = (RESULT_SATISFIED, RESULT_UNSATISFIED, RESULT_UNKNOWN)

PROVENANCE_SCAFFOLD = "deterministic scaffold"
PROVENANCE_USER_EDITED = "user edited"

#: The only capability this slice can execute, and the only executor.
CAPABILITY_SOURCE_SCAN = "source.scan"
EXECUTOR_LOCAL_SCANNER = "local_scanner"

BASELINE_UNKNOWN = "unknown"

PLAN_LABELS = {PLAN_DRAFT: "Draft", PLAN_CONFIRMED: "Confirmed", PLAN_SUPERSEDED: "Superseded"}
JOB_LABELS = {
    JOB_READY: "Ready",
    JOB_RUNNING: "Running",
    JOB_NEEDS_REVIEW: "Needs review",
    JOB_REVIEWED: "Reviewed",
}
RUN_LABELS = {
    RUN_RUNNING: "Running",
    RUN_SUCCEEDED: "Succeeded",
    RUN_FAILED: "Failed",
    RUN_INTERRUPTED_UNKNOWN: "Interrupted — outcome unknown",
}
FRESHNESS_LABELS = {
    FRESH_CURRENT: "Current",
    FRESH_STALE: "Stale",
    FRESH_UNKNOWN: "Unknown",
    FRESH_MISSING: "Missing",
}
OUTCOME_LABELS = {
    OUTCOME_ACKNOWLEDGED: "Evidence acknowledged",
    OUTCOME_REQUEST_CHANGES: "Changes requested",
    OUTCOME_REJECTED: "Rejected",
    OUTCOME_ESCALATED: "Escalated",
}


def plan_label(phase: str) -> str:
    """Return the human label for a plan phase."""
    return PLAN_LABELS.get(phase, phase)


def job_label(state: str) -> str:
    """Return the human label for a job state."""
    return JOB_LABELS.get(state, state)


def run_label(outcome: str) -> str:
    """Return the human label for a run outcome."""
    return RUN_LABELS.get(outcome, outcome)


def freshness_label(state: str) -> str:
    """Return the human label for a freshness state."""
    return FRESHNESS_LABELS.get(state, state)


def outcome_label(outcome: str) -> str:
    """Return the human label for a review outcome."""
    return OUTCOME_LABELS.get(outcome, outcome)


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------
def canonical_bytes(payload: Any) -> bytes:
    """Return the canonical JSON encoding used for every digest here."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def digest(payload: Any) -> str:
    """Return the SHA-256 of the canonical encoding of ``payload``."""
    return identity.sha256_hex(canonical_bytes(payload))


# ---------------------------------------------------------------------------
# Code-owned acceptance predicates
# ---------------------------------------------------------------------------
CRITERION_SCAN_EVIDENCE_BOUND = "scan_evidence_bound"
CRITERION_SOURCE_BINDING_STABLE = "source_binding_stable"
CRITERION_LIMITATIONS_PRESERVED = "limitations_preserved"
CRITERION_TERMINAL_OUTCOME_KNOWN = "terminal_outcome_known"

#: The complete set of checks this slice can establish. A criterion naming a
#: predicate outside this set is prose for our purposes and can never be
#: covered — the table is code-owned so a caller cannot widen it by asking.
PREDICATE_LABELS: Dict[str, str] = {
    CRITERION_SCAN_EVIDENCE_BOUND: "The scoped scan produced readable bound evidence",
    CRITERION_SOURCE_BINDING_STABLE: "The observation has a stable source binding",
    CRITERION_LIMITATIONS_PRESERVED: "Scanner limitations and unresolved facts are preserved",
    CRITERION_TERMINAL_OUTCOME_KNOWN: "Execution reached a known terminal outcome",
}

#: The scaffold's criteria, in order. Deterministic: the same goal always
#: produces the same checks.
SCAFFOLD_PREDICATES: Tuple[str, ...] = (
    CRITERION_SCAN_EVIDENCE_BOUND,
    CRITERION_SOURCE_BINDING_STABLE,
    CRITERION_LIMITATIONS_PRESERVED,
    CRITERION_TERMINAL_OUTCOME_KNOWN,
)


def is_known_predicate(predicate_id: Optional[str]) -> bool:
    """Return whether ``predicate_id`` is one this slice can establish."""
    return bool(predicate_id) and predicate_id in PREDICATE_LABELS


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Criterion:
    """One acceptance check on a plan revision.

    ``predicate_id`` is ``None`` for a requirement the developer wrote. Such a
    criterion is displayed and is never covered: no supported check establishes
    it, and the scan finishing is not evidence about it.
    """

    criterion_id: str
    text: str
    predicate_id: Optional[str] = None

    @property
    def is_supported(self) -> bool:
        """Whether a code-owned predicate could ever establish this check."""
        return is_known_predicate(self.predicate_id)

    @property
    def label(self) -> str:
        """Return the display text for the check."""
        return self.text or PREDICATE_LABELS.get(self.predicate_id or "", "")

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this criterion."""
        return {
            "criterion_id": self.criterion_id,
            "text": self.text,
            "predicate_id": self.predicate_id,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, Any]) -> "Criterion":
        """Rebuild a criterion from its persisted mapping."""
        return Criterion(
            criterion_id=str(payload.get("criterion_id", "")),
            text=str(payload.get("text", "")),
            predicate_id=payload.get("predicate_id"),
        )


@dataclass(frozen=True)
class Scope:
    """The exact part of the project a scan job is allowed to read."""

    include_paths: Tuple[str, ...] = ()
    exclusions: Tuple[str, ...] = ()

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this scope."""
        return {
            "include_paths": list(self.include_paths),
            "exclusions": list(self.exclusions),
        }

    @staticmethod
    def from_payload(payload: Mapping[str, Any]) -> "Scope":
        """Rebuild a scope from its persisted mapping."""
        return Scope(
            include_paths=tuple(str(p) for p in payload.get("include_paths", ())),
            exclusions=tuple(str(p) for p in payload.get("exclusions", ())),
        )


@dataclass(frozen=True)
class ManifestEntry:
    """One file bound into a source manifest."""

    path: str
    digest: str
    size_bytes: int

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this entry."""
        return {"path": self.path, "digest": self.digest, "size_bytes": self.size_bytes}


@dataclass(frozen=True)
class SourceManifest:
    """The actual bytes a run observed, bound by content.

    ``complete`` is the honest bit: it is false when the scope could not be
    observed consistently (a file changed under the walk, a read failed, a size
    was refused). An incomplete manifest still names what *was* seen, and no
    run against it can produce current evidence.
    """

    manifest_id: str
    entries: Tuple[ManifestEntry, ...] = ()
    exclusions: Tuple[str, ...] = ()
    scanner_schema: str = ""
    grammar: Mapping[str, str] = field(default_factory=dict)
    complete: bool = False
    unreadable: Tuple[str, ...] = ()
    observed_at: str = ""

    @property
    def file_count(self) -> int:
        """Return how many files the manifest binds."""
        return len(self.entries)

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this manifest.

        Only counts, paths and digests travel: the bounded artifact is a
        manifest, never a copy of the source.
        """
        return {
            "manifest_id": self.manifest_id,
            "entries": [entry.as_payload() for entry in self.entries],
            "exclusions": list(self.exclusions),
            "scanner_schema": self.scanner_schema,
            "grammar": dict(self.grammar),
            "complete": self.complete,
            "unreadable": list(self.unreadable),
            "observed_at": self.observed_at,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, Any]) -> "SourceManifest":
        """Rebuild a manifest from its persisted mapping."""
        return SourceManifest(
            manifest_id=str(payload.get("manifest_id", "")),
            entries=tuple(
                ManifestEntry(
                    path=str(entry.get("path", "")),
                    digest=str(entry.get("digest", "")),
                    size_bytes=int(entry.get("size_bytes") or 0),
                )
                for entry in payload.get("entries", ())
            ),
            exclusions=tuple(str(item) for item in payload.get("exclusions", ())),
            scanner_schema=str(payload.get("scanner_schema", "")),
            grammar=dict(payload.get("grammar") or {}),
            complete=bool(payload.get("complete")),
            unreadable=tuple(str(item) for item in payload.get("unreadable", ())),
            observed_at=str(payload.get("observed_at", "")),
        )

    def with_completeness(self, complete: bool, unreadable: Sequence[str]) -> "SourceManifest":
        """Return a copy carrying the observation's consistency verdict."""
        return replace(self, complete=complete, unreadable=tuple(unreadable))


def build_manifest(
    entries: Sequence[ManifestEntry],
    *,
    exclusions: Sequence[str],
    scanner_schema: str,
    grammar: Mapping[str, str],
    complete: bool,
    unreadable: Sequence[str],
    observed_at: str,
) -> SourceManifest:
    """Return a manifest whose identity is derived from its own content.

    The id covers the bound bytes, not the moment they were read, so an
    unchanged scope re-observes to the same id and a changed scope cannot.
    """
    ordered = tuple(sorted(entries, key=lambda entry: entry.path))
    manifest_id = "manifest:" + digest(
        {
            "entries": [entry.as_payload() for entry in ordered],
            "exclusions": sorted(exclusions),
            "scanner_schema": scanner_schema,
            "grammar": dict(grammar),
        }
    )
    return SourceManifest(
        manifest_id=manifest_id,
        entries=ordered,
        exclusions=tuple(exclusions),
        scanner_schema=scanner_schema,
        grammar=dict(grammar),
        complete=complete,
        unreadable=tuple(unreadable),
        observed_at=observed_at,
    )


@dataclass(frozen=True)
class JobSpec:
    """The single executable job a plan revision declares."""

    capability: str
    executor: str
    scope: Scope
    criterion_ids: Tuple[str, ...]

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this spec."""
        return {
            "capability": self.capability,
            "executor": self.executor,
            "scope": self.scope.as_payload(),
            "criterion_ids": list(self.criterion_ids),
        }

    @staticmethod
    def from_payload(payload: Mapping[str, Any]) -> "JobSpec":
        """Rebuild a job spec from its persisted mapping."""
        return JobSpec(
            capability=str(payload.get("capability", "")),
            executor=str(payload.get("executor", "")),
            scope=Scope.from_payload(payload.get("scope") or {}),
            criterion_ids=tuple(str(c) for c in payload.get("criterion_ids", ())),
        )


@dataclass(frozen=True)
class PlanRevision:
    """One immutable revision of a project's plan."""

    plan_id: str
    project_id: str
    revision: int
    goal: str
    provenance: str
    scope: Scope
    manifest_id: str
    criteria: Tuple[Criterion, ...]
    job_spec: JobSpec
    phase: str = PLAN_DRAFT
    accepted_baseline_ref: Optional[str] = None
    created_at: str = ""
    digest: str = ""

    @property
    def criterion_ids(self) -> Tuple[str, ...]:
        """Return every criterion id on this revision, in order."""
        return tuple(criterion.criterion_id for criterion in self.criteria)

    @property
    def supported_criteria(self) -> Tuple[Criterion, ...]:
        """Return the criteria a code-owned predicate could establish."""
        return tuple(c for c in self.criteria if c.is_supported)

    def as_payload(self) -> Dict[str, Any]:
        """Return the canonical payload the revision digest is taken over."""
        return {
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "revision": self.revision,
            "goal": self.goal,
            "provenance": self.provenance,
            "scope": self.scope.as_payload(),
            "manifest_id": self.manifest_id,
            "criteria": [c.as_payload() for c in self.criteria],
            "job_spec": self.job_spec.as_payload(),
            "accepted_baseline_ref": self.accepted_baseline_ref,
        }

    @property
    def content_digest(self) -> str:
        """Return the digest that pins this revision's exact content."""
        return digest(self.as_payload())

    def with_phase(self, phase: str, created_at: str = "") -> "PlanRevision":
        """Return a copy in ``phase``."""
        return replace(self, phase=phase, created_at=created_at or self.created_at)

    def with_digest(self) -> "PlanRevision":
        """Return a copy carrying its own content digest."""
        return replace(self, digest=self.content_digest)


@dataclass(frozen=True)
class JobRecord:
    """The persisted state of the plan's one job."""

    job_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    capability: str
    executor: str
    scope: Scope
    manifest_id: str
    criterion_ids: Tuple[str, ...]
    state: str = JOB_READY
    reason: str = ""

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this job."""
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "capability": self.capability,
            "executor": self.executor,
            "scope": self.scope.as_payload(),
            "manifest_id": self.manifest_id,
            "criterion_ids": list(self.criterion_ids),
            "state": self.state,
            "reason": self.reason,
        }

    def with_state(self, state: str, reason: str = "") -> "JobRecord":
        """Return a copy in ``state``."""
        return replace(self, state=state, reason=reason)


@dataclass(frozen=True)
class AgentRun:
    """One attempt at executing a job. Terminal outcomes are immutable."""

    run_id: str
    project_id: str
    job_id: str
    plan_id: str
    plan_revision: int
    attempt: int
    idempotency_key: str
    executor: str
    executor_version: str
    manifest_id: str
    outcome: str
    reason: str = ""
    started_at: str = ""
    ended_at: str = ""
    #: The process that claimed this run. Recovery may only mark an unfinished
    #: run ``interrupted_unknown`` once this process is demonstrably gone; a
    #: live executor's run is never rewritten by a reader.
    executor_pid: int = 0

    @property
    def is_terminal(self) -> bool:
        """Whether this run has ended."""
        return self.outcome in TERMINAL_RUN_OUTCOMES

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this run."""
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "job_id": self.job_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "executor": self.executor,
            "executor_version": self.executor_version,
            "manifest_id": self.manifest_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "executor_pid": self.executor_pid,
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """One backend-produced, immutable observation about a bound scope."""

    evidence_id: str
    project_id: str
    plan_id: str
    job_id: str
    run_id: str
    kind: str
    predicate_id: str
    result: str
    manifest_id: str
    limitation: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)
    grammar: Mapping[str, str] = field(default_factory=dict)
    scanner_schema: str = ""
    artifact_ref: str = ""
    artifact_digest: str = ""
    created_at: str = ""

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this evidence."""
        return {
            "evidence_id": self.evidence_id,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "predicate_id": self.predicate_id,
            "result": self.result,
            "manifest_id": self.manifest_id,
            "limitation": self.limitation,
            "counts": dict(self.counts),
            "grammar": dict(self.grammar),
            "scanner_schema": self.scanner_schema,
            "artifact_ref": self.artifact_ref,
            "artifact_digest": self.artifact_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class DecisionRecord:
    """An append-only human decision."""

    decision_id: str
    project_id: str
    kind: str
    outcome: str
    actor: str
    target_digest: str
    idempotency_key: str
    run_id: str = ""
    evidence_set_digest: str = ""
    reason: str = ""
    supersedes: str = ""
    created_at: str = ""

    def as_payload(self) -> Dict[str, Any]:
        """Return the plain mapping persisted for this decision."""
        return {
            "decision_id": self.decision_id,
            "project_id": self.project_id,
            "kind": self.kind,
            "outcome": self.outcome,
            "actor": self.actor,
            "target_digest": self.target_digest,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "evidence_set_digest": self.evidence_set_digest,
            "reason": self.reason,
            "supersedes": self.supersedes,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CriterionCoverage:
    """Whether one criterion is covered, and by what."""

    criterion_id: str
    label: str
    covered: bool
    supported: bool
    note: str = ""


@dataclass(frozen=True)
class EvidenceView:
    """One evidence row as Review shows it."""

    evidence_id: str
    predicate_id: str
    label: str
    result: str
    limitation: str
    counts: Mapping[str, int]


@dataclass(frozen=True)
class ReviewProjection:
    """Everything a developer needs to decide, derived from records only."""

    project_id: str
    plan_id: str
    plan_revision: int
    goal: str
    job_id: str
    job_state: str
    run_id: str
    run_outcome: str
    run_reason: str
    freshness: str
    evidence: Tuple[EvidenceView, ...] = ()
    coverage: Tuple[CriterionCoverage, ...] = ()
    blocking: Tuple[str, ...] = ()
    limitations: Tuple[str, ...] = ()
    decision: Optional[DecisionRecord] = None

    @property
    def is_reviewable(self) -> bool:
        """Whether there is a terminal run to review."""
        return bool(self.run_id) and self.run_outcome in TERMINAL_RUN_OUTCOMES

    @property
    def has_decision(self) -> bool:
        """Whether a scan-review decision is already recorded."""
        return self.decision is not None

    @property
    def coverage_counts(self) -> Tuple[int, int]:
        """Return ``(covered, total)`` supported criteria."""
        supported = [row for row in self.coverage if row.supported]
        return sum(1 for row in supported if row.covered), len(supported)

    @property
    def can_acknowledge(self) -> bool:
        """Whether the scan evidence may be acknowledged right now.

        Acknowledging means "I have seen this evidence", so it requires a
        terminal run, current evidence and no blocking reason. It never
        requires the source to be clean: a run that preserved parse errors is
        exactly the run a developer may acknowledge.
        """
        return self.is_reviewable and not self.blocking and self.freshness == FRESH_CURRENT

    def as_payload(self) -> Dict[str, Any]:
        """Return a deterministic, JSON-safe mapping of this projection."""
        return {
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "goal": self.goal,
            "job_id": self.job_id,
            "job_state": self.job_state,
            "run_id": self.run_id,
            "run_outcome": self.run_outcome,
            "run_reason": self.run_reason,
            "freshness": self.freshness,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "predicate_id": item.predicate_id,
                    "label": item.label,
                    "result": item.result,
                    "limitation": item.limitation,
                    "counts": dict(item.counts),
                }
                for item in self.evidence
            ],
            "coverage": [
                {
                    "criterion_id": row.criterion_id,
                    "label": row.label,
                    "covered": row.covered,
                    "supported": row.supported,
                    "note": row.note,
                }
                for row in self.coverage
            ],
            "blocking": list(self.blocking),
            "limitations": list(self.limitations),
            "decision": self.decision.as_payload() if self.decision else None,
        }


@dataclass(frozen=True)
class ResumeItem:
    """One resume line, with the record that supports it."""

    key: str
    label: str
    detail: str = ""
    reference: str = ""


@dataclass(frozen=True)
class ResumeProjection:
    """The deterministic resume over persisted facts."""

    project_id: str
    plan_id: str
    plan_revision: int
    phase: str
    job_id: str
    job_state: str
    run_id: str
    run_outcome: str
    freshness: str
    accepted_baseline_ref: Optional[str]
    observed_manifest_id: str
    observed_file_count: int
    pending_decision: bool
    decision_outcome: str
    blockers: Tuple[str, ...] = ()
    unverified: Tuple[ResumeItem, ...] = ()
    changes: Tuple[ResumeItem, ...] = ()
    next_action: str = ""
    next_action_reason: str = ""
    next_action_reference: str = ""

    @property
    def accepted_baseline_text(self) -> str:
        """Return the accepted baseline, or an explicit unknown."""
        return self.accepted_baseline_ref or BASELINE_UNKNOWN

    def as_payload(self) -> Dict[str, Any]:
        """Return a deterministic, JSON-safe mapping of this projection."""
        return {
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "phase": self.phase,
            "job_id": self.job_id,
            "job_state": self.job_state,
            "run_id": self.run_id,
            "run_outcome": self.run_outcome,
            "freshness": self.freshness,
            "accepted_baseline_ref": self.accepted_baseline_ref,
            "accepted_baseline": self.accepted_baseline_text,
            "observed_manifest_id": self.observed_manifest_id,
            "observed_file_count": self.observed_file_count,
            "pending_decision": self.pending_decision,
            "decision_outcome": self.decision_outcome,
            "blockers": list(self.blockers),
            "unverified": [
                {"key": i.key, "label": i.label, "detail": i.detail, "reference": i.reference}
                for i in self.unverified
            ],
            "changes": [
                {"key": i.key, "label": i.label, "detail": i.detail, "reference": i.reference}
                for i in self.changes
            ],
            "next_action": self.next_action,
            "next_action_reason": self.next_action_reason,
            "next_action_reference": self.next_action_reference,
        }


# ---------------------------------------------------------------------------
# Projection construction
# ---------------------------------------------------------------------------
def build_coverage(
    criteria: Sequence[Criterion],
    evidence: Sequence[EvidenceRecord],
    *,
    run_outcome: str,
    freshness: str,
) -> Tuple[CriterionCoverage, ...]:
    """Derive per-criterion coverage from actual evidence.

    A supported criterion is covered only when a terminal, current run produced
    evidence for its own predicate with a satisfied result. An unsupported
    criterion — the developer's prose — is never covered, whatever the scan
    found.
    """
    by_predicate: Dict[str, EvidenceRecord] = {}
    for record in evidence:
        by_predicate[record.predicate_id] = record

    rows: List[CriterionCoverage] = []
    for criterion in criteria:
        if not criterion.is_supported:
            rows.append(
                CriterionCoverage(
                    criterion_id=criterion.criterion_id,
                    label=criterion.label,
                    covered=False,
                    supported=False,
                    note="No supported check establishes this requirement.",
                )
            )
            continue
        record = by_predicate.get(criterion.predicate_id or "")
        if record is None:
            rows.append(
                CriterionCoverage(
                    criterion_id=criterion.criterion_id,
                    label=criterion.label,
                    covered=False,
                    supported=True,
                    note="No evidence recorded for this check.",
                )
            )
            continue
        if freshness != FRESH_CURRENT:
            rows.append(
                CriterionCoverage(
                    criterion_id=criterion.criterion_id,
                    label=criterion.label,
                    covered=False,
                    supported=True,
                    note=f"Evidence is {freshness_label(freshness).lower()}.",
                )
            )
            continue
        covered = record.result == RESULT_SATISFIED
        rows.append(
            CriterionCoverage(
                criterion_id=criterion.criterion_id,
                label=criterion.label,
                covered=covered,
                supported=True,
                note="" if covered else (record.limitation or "The check did not pass."),
            )
        )
    return tuple(rows)


def build_review(
    revision: PlanRevision,
    job: JobRecord,
    run: Optional[AgentRun],
    evidence: Sequence[EvidenceRecord],
    *,
    freshness: str,
    decision: Optional[DecisionRecord] = None,
) -> ReviewProjection:
    """Return the review projection for one plan revision."""
    views = tuple(
        EvidenceView(
            evidence_id=record.evidence_id,
            predicate_id=record.predicate_id,
            label=PREDICATE_LABELS.get(record.predicate_id, record.predicate_id),
            result=record.result,
            limitation=record.limitation,
            counts=dict(record.counts),
        )
        for record in evidence
    )
    coverage = build_coverage(
        revision.criteria,
        evidence,
        run_outcome=run.outcome if run else RUN_RUNNING,
        freshness=freshness,
    )

    blocking: List[str] = []
    if run is None:
        blocking.append("The scan has not been run.")
    elif run.outcome == RUN_RUNNING:
        blocking.append("The scan is still running.")
    elif run.outcome == RUN_FAILED:
        blocking.append(f"The scan failed. {run.reason}".strip())
    elif run.outcome == RUN_INTERRUPTED_UNKNOWN:
        blocking.append(
            "The scan was interrupted and its outcome was not observed. "
            "It is recorded as unknown rather than failed."
        )
    if freshness == FRESH_STALE:
        blocking.append("The bound source changed after this scan; the evidence is stale.")
    elif freshness == FRESH_UNKNOWN:
        blocking.append("The bound source could not be re-observed; freshness is unknown.")
    elif freshness == FRESH_MISSING:
        blocking.append("No source manifest is bound to this scan.")

    limitations: List[str] = []
    for record in evidence:
        if record.limitation and record.limitation not in limitations:
            limitations.append(record.limitation)

    return ReviewProjection(
        project_id=revision.project_id,
        plan_id=revision.plan_id,
        plan_revision=revision.revision,
        goal=revision.goal,
        job_id=job.job_id,
        job_state=job.state,
        run_id=run.run_id if run else "",
        run_outcome=run.outcome if run else "",
        run_reason=run.reason if run else "",
        freshness=freshness,
        evidence=views,
        coverage=coverage,
        blocking=tuple(blocking),
        limitations=tuple(limitations),
        decision=decision,
    )


def build_resume(
    revision: PlanRevision,
    job: JobRecord,
    run: Optional[AgentRun],
    review: ReviewProjection,
    *,
    freshness: str,
    decision: Optional[DecisionRecord] = None,
    observed_file_count: int = 0,
) -> ResumeProjection:
    """Return the resume projection for one plan revision.

    The review projection is passed in rather than rebuilt: a resume that
    recomputed coverage from no evidence would report every criterion
    unverified beside a review that had just shown them covered.

    The next action never recommends adopting source: a read-only scan has no
    candidate to adopt, and this slice cannot move an accepted baseline.
    """
    unverified: List[ResumeItem] = []
    for row in review.coverage:
        if not row.covered:
            unverified.append(
                ResumeItem(
                    key=row.criterion_id,
                    label=row.label,
                    detail=row.note,
                    reference=row.criterion_id,
                )
            )

    changes: List[ResumeItem] = []
    if revision.manifest_id:
        changes.append(
            ResumeItem(
                key="observed-source",
                label="Observed source",
                detail=f"{observed_file_count} file(s) bound to {revision.manifest_id}.",
                reference=revision.manifest_id,
            )
        )
    if revision.accepted_baseline_ref:
        changes.append(
            ResumeItem(
                key="accepted-baseline",
                label="Accepted baseline",
                detail=revision.accepted_baseline_ref,
                reference=revision.accepted_baseline_ref,
            )
        )

    action, reason, reference = _recommend(revision, job, run, freshness, decision, review)
    return ResumeProjection(
        project_id=revision.project_id,
        plan_id=revision.plan_id,
        plan_revision=revision.revision,
        phase=revision.phase,
        job_id=job.job_id,
        job_state=job.state,
        run_id=run.run_id if run else "",
        run_outcome=run.outcome if run else "",
        freshness=freshness,
        accepted_baseline_ref=revision.accepted_baseline_ref,
        observed_manifest_id=revision.manifest_id,
        observed_file_count=observed_file_count,
        pending_decision=review.is_reviewable and decision is None,
        decision_outcome=decision.outcome if decision else "",
        blockers=review.blocking,
        unverified=tuple(unverified),
        changes=tuple(changes),
        next_action=action,
        next_action_reason=reason,
        next_action_reference=reference,
    )


def _recommend(
    revision: PlanRevision,
    job: JobRecord,
    run: Optional[AgentRun],
    freshness: str,
    decision: Optional[DecisionRecord],
    review: ReviewProjection,
) -> Tuple[str, str, str]:
    """Return the single safe next action, chosen by the first matching rule."""
    if revision.phase == PLAN_DRAFT:
        return (
            "Confirm the plan",
            "The plan is a draft. Confirming it pins this exact revision and "
            "its scope; it dispatches nothing.",
            revision.plan_id,
        )
    if run is None:
        return (
            "Run the read-only scan",
            "The plan is confirmed and its one scan job has not run yet.",
            job.job_id,
        )
    if run.outcome == RUN_RUNNING:
        return (
            "Wait for the scan to finish",
            "An execution is in progress. A normal read does not interrupt it.",
            run.run_id,
        )
    if freshness in (FRESH_STALE, FRESH_UNKNOWN, FRESH_MISSING):
        return (
            "Confirm a new scan job",
            "The bound source no longer matches this scan, so its evidence is not "
            "current. A repeat is a new job, never a replay of the old request.",
            job.job_id,
        )
    if run.outcome in (RUN_FAILED, RUN_INTERRUPTED_UNKNOWN):
        return (
            "Review the interrupted or failed execution",
            run.reason or "The execution did not reach a successful outcome.",
            run.run_id,
        )
    if decision is None:
        return (
            "Review the scan evidence",
            "The scan reached a terminal outcome. Acknowledging it records that "
            "you have seen this evidence; it adopts nothing.",
            run.run_id,
        )
    if decision.outcome == OUTCOME_ACKNOWLEDGED:
        return (
            "No further action for this scan",
            "The scan evidence is acknowledged. This slice cannot adopt source, "
            "so nothing is recommended beyond it.",
            decision.decision_id,
        )
    return (
        "State a new goal",
        f"The scan review ended as {outcome_label(decision.outcome).lower()}.",
        decision.decision_id,
    )


def freshness_of(
    stored_manifest_id: str,
    current_manifest_id: Optional[str],
    *,
    observation_failed: bool = False,
) -> str:
    """Derive freshness by comparing the bound manifest with the current one.

    ``current_manifest_id`` is ``None`` when the scope could not be re-observed
    at all — which is ``unknown``, never silently ``current``.
    """
    if not stored_manifest_id:
        return FRESH_MISSING
    if observation_failed or current_manifest_id is None:
        return FRESH_UNKNOWN
    return FRESH_CURRENT if stored_manifest_id == current_manifest_id else FRESH_STALE


def validate_revision(revision: PlanRevision) -> Tuple[str, ...]:
    """Return every problem that makes a revision unfit to save or confirm."""
    problems: List[str] = []
    if not revision.goal.strip():
        problems.append("The plan has no stated goal.")
    if revision.revision < 1:
        problems.append("A plan revision is numbered from one.")
    if revision.job_spec.capability != CAPABILITY_SOURCE_SCAN:
        problems.append(
            f"This slice executes only {CAPABILITY_SOURCE_SCAN!r}, not "
            f"{revision.job_spec.capability!r}."
        )
    if revision.job_spec.executor != EXECUTOR_LOCAL_SCANNER:
        problems.append(
            f"This slice executes only the {EXECUTOR_LOCAL_SCANNER!r} executor, not "
            f"{revision.job_spec.executor!r}."
        )
    if not revision.criteria:
        problems.append("The plan states no acceptance criteria.")
    seen: set = set()
    for criterion in revision.criteria:
        if not criterion.criterion_id:
            problems.append("A criterion has no id.")
        elif criterion.criterion_id in seen:
            problems.append(f"Two criteria share the id {criterion.criterion_id!r}.")
        seen.add(criterion.criterion_id)
    return tuple(problems)
