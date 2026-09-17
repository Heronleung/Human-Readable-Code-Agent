"""Bounded cross-run Search, Timeline and Resume over normalized records.

M4.4/v1. This module is a *read model*: it answers questions across stored runs
from normalized schema 1.0.0 records and the accepted projected claims. It adds
no durable index, no migration and no second storage authority — every answer is
computed from the stores it is handed, so an answer can never outlive or
disagree with the records it came from.

Authority rules
---------------
* **Only allowlisted content is searchable.** The facets read named canonical
  fields. A hook payload, a transcript, prompt/assistant/tool-response text, a
  dropped field, a fingerprint or digest value, a secret or a personal absolute
  path is not reachable from this module, because it is not reachable from a
  store.
* **A hit is a target.** Every hit carries the exact ``(run_id, kind,
  record_id)`` the read boundary resolves, so a result can always be opened.
* **Ranking is total and deterministic.** Hits are ordered by how many facets
  matched, then by facet precedence, then by identity. Identical input yields
  byte-identical output.
* **Time is ordered only when it is comparable.** Only a strict uniform instant
  shape is ordered; anything else is reported as missing or incomparable, in a
  separate bucket, and never silently sorted. Display order is never causality.
* **Resume composes, it never narrates.** Every statement is drawn from a typed
  field or an accepted projected claim, and a fact the schema cannot support —
  acceptance, current baseline, verification — is reported as unsupported rather
  than asserted.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import memory
from . import memory_docs

QUERY_SCHEMA_VERSION = "1.0.0"
QUERY_GENERATOR = "hrca-memory-query"

# -- facets --------------------------------------------------------------

FACET_PROJECT = "project"
FACET_WORK_PACKAGE = "work_package"
FACET_DATE = "date"
FACET_RUN_STATE = "run_state"
FACET_FILE = "file"
FACET_SYMBOL = "symbol"
FACET_DECISION = "decision"
FACET_TEST_RESULT = "test_result"
FACET_TEXT = "text"

# Declaration order is the precedence order used for ranking: an earlier facet
# is a stronger signal than a later one.
FACET_ORDER = (
    FACET_PROJECT,
    FACET_WORK_PACKAGE,
    FACET_RUN_STATE,
    FACET_DATE,
    FACET_FILE,
    FACET_SYMBOL,
    FACET_DECISION,
    FACET_TEXT,
    FACET_TEST_RESULT,
)

# Facets a caller may name. ``test_result`` is nameable but has no typed data in
# schema 1.0.0, so it is reported as unsupported instead of silently matching
# nothing.
QUERY_FACETS = frozenset(FACET_ORDER)
UNSUPPORTED_FACETS = (FACET_TEST_RESULT,)

# -- ordering ------------------------------------------------------------

ORDER_RELEVANCE = "relevance"
ORDER_RECORDED_TIME = "recorded_time"
QUERY_ORDERS = (ORDER_RELEVANCE, ORDER_RECORDED_TIME)

# -- bounds --------------------------------------------------------------

MAX_QUERY_RESULTS = 100
MAX_QUERY_TERM_CHARS = 128
MAX_QUERY_TERMS_PER_FACET = 8
MAX_QUERY_FACETS = 16
MAX_TIMELINE_ITEMS = 200
MAX_RESUME_ITEMS = 64
MAX_RESUME_ACTION_CHARS = 256

# -- bounded reasons -----------------------------------------------------

REASON_NOT_STORES = "query input is not a list of stores"
REASON_UNKNOWN_FACET = "facet is not supported by the query model"
REASON_TERM_UNUSABLE = "facet term is empty or exceeds the bounded length"
REASON_TOO_MANY_TERMS = "facet carries more terms than the query model allows"
REASON_ORDER_UNSUPPORTED = "order is not supported by the query model"
REASON_LIMIT_UNUSABLE = "limit is not a usable result count"
REASON_DATE_UNUSABLE = "date facet term is not a usable calendar date"

# -- recorded time -------------------------------------------------------

# The only comparable recorded-time shape: a strict, uniform instant or date.
# Anything else — a zone suffix, an exotic fraction, free text — is *incomparable*
# rather than guessed at, because mixing shapes would make a lexical sort lie.
_COMPARABLE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2})?$")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TIME_COMPARABLE = "comparable"
TIME_MISSING = "missing"
TIME_INCOMPARABLE = "incomparable"

# Fields whose value is source-reported text rather than a value the contract
# validated, so a hit that matched one is labelled accordingly.
_REPORTED_FIELDS = frozenset({"summary", "name", "title", "decided_at"})

# The allowlisted free-text sources. Nothing outside this map is searched.
TEXT_SOURCES = {
    memory_docs.LINK_DECISION: ("summary",),
    memory_docs.LINK_CHANGE_SET: ("summary",),
    memory_docs.LINK_PROJECT: ("name",),
    memory_docs.LINK_WORK_PACKAGE: ("title",),
}

# The field a hit's recorded time is read from, per kind. A kind with no entry
# has no recorded time at all.
TIME_FIELDS = {
    memory_docs.LINK_RUN: ("first_source_timestamp",),
    memory_docs.LINK_EVENT: ("source_timestamp",),
    memory_docs.LINK_DECISION: ("decided_at",),
}

TARGET_LIMITATIONS = (
    "a hit is a bounded reference: the record itself is read through the "
    "exact-target action",
)


# -- small helpers -------------------------------------------------------


def recorded_time(value: Any) -> Tuple[Optional[str], str]:
    """Return ``(value, status)`` for one candidate recorded time.

    A comparable value is returned unchanged so it sorts as a string; a value
    that is absent is ``missing`` and one that is present but of a shape that
    cannot be compared is ``incomparable``.
    """
    if value is None or value == "":
        return None, TIME_MISSING
    if isinstance(value, str) and _COMPARABLE_TIME_RE.match(value):
        return value, TIME_COMPARABLE
    return None, TIME_INCOMPARABLE


def _lower(value: Any) -> Optional[str]:
    return value.lower() if isinstance(value, str) else None


def _matches(term: str, value: Any) -> bool:
    """Case-insensitive bounded substring match."""
    text = _lower(value)
    return text is not None and term in text


def _any_match(terms: Sequence[str], value: Any) -> bool:
    return any(_matches(term, value) for term in terms)


def _strings(value: Any) -> List[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


class _RunIndex:
    """The descriptors and run facts one search needs, built once per store."""

    __slots__ = ("store", "run", "run_id", "state", "success", "projects",
                 "work_packages", "report")

    def __init__(self, store: Dict[str, Any]) -> None:
        self.store = store
        run = store.get("agent_run") if isinstance(store, dict) else None
        self.run = run if isinstance(run, dict) else {}
        self.run_id = self.run.get("id")
        self.state = memory.run_state(store) if isinstance(store, dict) else None
        self.success = memory.is_success(store) if isinstance(store, dict) else False
        self.projects = {}
        self.work_packages = {}
        for record in store.get("projects", []) if isinstance(store, dict) else []:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                self.projects[record["id"]] = record
        for record in store.get("work_packages", []) if isinstance(store, dict) else []:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                self.work_packages[record["id"]] = record

    @property
    def project(self) -> Optional[Dict[str, Any]]:
        return self.projects.get(self.run.get("project_id"))

    @property
    def work_package(self) -> Optional[Dict[str, Any]]:
        return self.work_packages.get(self.run.get("work_package_id"))


# -- facet evaluation ----------------------------------------------------


def _facet_values(
    index: _RunIndex, kind: str, record: Dict[str, Any], facet: str
) -> List[Tuple[str, Any]]:
    """Return ``(field, value)`` candidates for one facet of one record.

    Only named canonical fields are offered, so a facet cannot reach content the
    contract dropped.
    """
    run, state = index.run, index.state
    if facet == FACET_PROJECT:
        project = index.project
        fields = []
        if project is not None:
            fields.append(("source_id", project.get("source_id")))
            fields.append(("name", project.get("name")))
        if kind == memory_docs.LINK_PROJECT:
            fields.extend([("source_id", record.get("source_id")),
                           ("name", record.get("name"))])
        return fields
    if facet == FACET_WORK_PACKAGE:
        package = index.work_package
        fields = []
        if package is not None:
            fields.append(("source_id", package.get("source_id")))
            fields.append(("title", package.get("title")))
        if kind == memory_docs.LINK_WORK_PACKAGE:
            fields.extend([("source_id", record.get("source_id")),
                           ("title", record.get("title"))])
        return fields
    if facet == FACET_RUN_STATE:
        return [("state", state)]
    if facet == FACET_DATE:
        value, status = _recorded_time_of(kind, record, run)
        return [("recorded_time", value)] if status == TIME_COMPARABLE else []
    if facet == FACET_FILE:
        fields = []
        if kind == memory_docs.LINK_CHANGE_SET:
            for path in _strings(record.get("paths")):
                fields.append(("paths", path))
        if kind == memory_docs.LINK_CODE_ENTITY:
            fields.append(("path", record.get("path")))
        if kind == memory_docs.LINK_EVIDENCE:
            fields.append(("artifact_ref", record.get("artifact_ref")))
        return fields
    if facet == FACET_SYMBOL:
        if kind == memory_docs.LINK_CODE_ENTITY:
            return [("symbol", record.get("symbol"))]
        return []
    if facet == FACET_DECISION:
        if kind == memory_docs.LINK_DECISION:
            return [("summary", record.get("summary"))]
        return []
    if facet == FACET_TEXT:
        fields = []
        for name in TEXT_SOURCES.get(kind, ()):
            fields.append((name, record.get(name)))
        if kind in (memory_docs.LINK_REJECTION, memory_docs.LINK_QUARANTINE):
            fields.append(("reason", record.get("reason")))
        return fields
    return []


def _recorded_time_of(
    kind: str, record: Dict[str, Any], run: Dict[str, Any]
) -> Tuple[Optional[str], str]:
    for field in TIME_FIELDS.get(kind, ()):
        source = run if kind == memory_docs.LINK_RUN else record
        value = source.get(field)
        if value:
            return recorded_time(value)
    return None, TIME_MISSING


# -- search --------------------------------------------------------------


def _normalize_filters(filters: Any) -> Tuple[Dict[str, List[str]], List[str]]:
    """Return ``(filters, unsupported)`` or raise ``_QueryError``."""
    if filters is None:
        return {}, []
    if not isinstance(filters, dict):
        raise ValueError(REASON_UNKNOWN_FACET)
    if len(filters) > MAX_QUERY_FACETS:
        raise ValueError(REASON_UNKNOWN_FACET)
    normalized: Dict[str, List[str]] = {}
    unsupported: List[str] = []
    for facet in sorted(filters):
        if facet not in QUERY_FACETS:
            raise ValueError(REASON_UNKNOWN_FACET)
        raw = filters[facet]
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ValueError(REASON_TERM_UNUSABLE)
        if len(raw) > MAX_QUERY_TERMS_PER_FACET:
            raise ValueError(REASON_TOO_MANY_TERMS)
        terms = []
        for term in raw:
            if not isinstance(term, str):
                raise ValueError(REASON_TERM_UNUSABLE)
            text = term.strip().lower()
            if not text or len(text) > MAX_QUERY_TERM_CHARS:
                raise ValueError(REASON_TERM_UNUSABLE)
            if facet == FACET_DATE and not _DATE_ONLY_RE.match(text):
                raise ValueError(REASON_DATE_UNUSABLE)
            terms.append(text)
        if facet in UNSUPPORTED_FACETS:
            # A nameable facet with no typed data is reported, never silently
            # treated as a facet that simply matched nothing.
            unsupported.append(facet)
            continue
        normalized[facet] = sorted(set(terms))
    return normalized, sorted(set(unsupported))


def search(
    stores: Any,
    filters: Any = None,
    order: str = ORDER_RELEVANCE,
    limit: Optional[int] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str]]:
    """Return ``(result, error_reason, unsupported_facet)``.

    Exactly one of ``result`` and ``error_reason`` is set. A term the model
    cannot use fails closed rather than matching loosely, and a facet with no
    typed data is reported through the third value so the caller can surface it.
    """
    if not isinstance(stores, list):
        return None, REASON_NOT_STORES, None
    if order not in QUERY_ORDERS:
        return None, REASON_ORDER_UNSUPPORTED, None
    if limit is None:
        limit = MAX_QUERY_RESULTS
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return None, REASON_LIMIT_UNUSABLE, None
    limit = min(limit, MAX_QUERY_RESULTS)

    try:
        normalized, unsupported = _normalize_filters(filters)
    except ValueError as exc:
        return None, str(exc), None

    hits: List[Dict[str, Any]] = []
    # A facet the schema cannot satisfy makes the whole query unanswerable: the
    # honest answer is an empty, explicitly-labelled result, never every record.
    if not unsupported:
        for store in stores:
            if not isinstance(store, dict):
                continue
            index = _RunIndex(store)
            if index.run_id is None:
                continue
            for kind, record in memory_docs.iter_records(store):
                hit = _evaluate(index, kind, record, normalized)
                if hit is not None:
                    hits.append(hit)

    if order == ORDER_RECORDED_TIME:
        # Only a recorded-time order has an "unordered" bucket: nothing is out of
        # order in a relevance ranking, and every hit still reports its own time
        # status so a surface can mark the ones that carry no instant.
        ranked, unordered = _split_by_time(hits)
    else:
        ranked = sorted(hits, key=_relevance_key)
        unordered = []

    truncated = len(ranked) > limit or len(unordered) > limit
    result = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "generator": QUERY_GENERATOR,
        "memory_schema_version": memory.MEMORY_SCHEMA_VERSION,
        "order": order,
        "limit": limit,
        "filters": {facet: list(terms) for facet, terms in normalized.items()},
        "run_count": len([s for s in stores if isinstance(s, dict)]),
        "hit_count": len(ranked) + len(unordered),
        "truncated": truncated,
        "results": ranked[:limit],
        "unordered": unordered[:limit],
        "unsupported_facets": unsupported,
        "facets": list(FACET_ORDER),
        "unsupported_facet_names": list(UNSUPPORTED_FACETS),
        "limitations": sorted(
            _search_limitations(order, unsupported, bool(unordered))
        ),
    }
    return result, None, unsupported


def _search_limitations(
    order: str, unsupported: Sequence[str], has_unordered: bool
) -> List[str]:
    limits = [
        "results are bounded references, never content",
        "a facet with no typed data in schema 1.0.0 is reported as unsupported "
        "rather than as a facet that matched nothing",
    ]
    for facet in unsupported:
        limits.append(
            "facet '%s' has no typed data in schema 1.0.0, so no record can "
            "satisfy this query and no result is returned" % facet
        )
    if order == ORDER_RECORDED_TIME:
        limits.append(
            "recorded-time order is a display order over comparable instants "
            "only; it makes no claim about causality"
        )
    if has_unordered:
        limits.append(
            "items whose recorded time is missing or incomparable are listed "
            "separately and are explicitly unordered"
        )
    return limits


def _relevance_key(hit: Dict[str, Any]) -> Tuple:
    """Return the total, deterministic ranking key for a hit."""
    facets = hit["matched_facets"]
    strongest = min(
        (FACET_ORDER.index(f) for f in facets if f in FACET_ORDER), default=len(FACET_ORDER)
    )
    return (
        -len(facets),
        strongest,
        str(hit["run_id"]),
        str(hit["kind"]),
        str(hit["record_id"]),
    )


def _evaluate(
    index: _RunIndex,
    kind: str,
    record: Dict[str, Any],
    filters: Dict[str, List[str]],
) -> Optional[Dict[str, Any]]:
    """Return a hit for one record, or ``None`` when it does not satisfy the filters.

    Every named facet must match at least one term; within a facet, terms are
    alternatives. With no filters every record is a hit, which is what makes an
    unfiltered ``recorded_time`` query a timeline.
    """
    matched_facets: List[str] = []
    matched_fields: List[str] = []
    for facet in FACET_ORDER:
        terms = filters.get(facet)
        if not terms:
            continue
        candidates = _facet_values(index, kind, record, facet)
        if facet == FACET_DATE:
            if not any(
                _matches(term, value[:10])
                for term in terms
                for _field, value in candidates
                if isinstance(value, str)
            ):
                return None
            matched_facets.append(facet)
            matched_fields.extend(field for field, _v in candidates)
            continue
        hit_fields = [
            field
            for field, value in candidates
            if _any_match(terms, value)
        ]
        if not hit_fields:
            return None
        matched_facets.append(facet)
        matched_fields.extend(hit_fields)

    value, status = _recorded_time_of(kind, record, index.run)
    return {
        "hit_id": "hit:%s:%s" % (kind, record.get("id")),
        "kind": kind,
        "record_id": record.get("id"),
        "run_id": index.run_id,
        "run_state": index.state,
        "run_success": index.success,
        "matched_facets": matched_facets,
        "matched_fields": sorted(set(matched_fields)),
        "provenance": (
            "reported"
            if _REPORTED_FIELDS.intersection(matched_fields)
            else "observed"
        ),
        "rank": {
            "matched_facet_count": len(matched_facets),
            "strongest_facet": matched_facets[0] if matched_facets else None,
        },
        "recorded_time": value,
        "time_status": status,
        "target": {
            "run_id": index.run_id,
            "kind": kind,
            "record_id": record.get("id"),
        },
        "limitations": list(TARGET_LIMITATIONS),
    }


def _split_by_time(
    hits: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split hits into comparable-time order and an explicitly unordered bucket."""
    comparable = [h for h in hits if h["time_status"] == TIME_COMPARABLE]
    unordered = [h for h in hits if h["time_status"] != TIME_COMPARABLE]
    comparable.sort(
        key=lambda h: (
            str(h["recorded_time"]),
            str(h["run_id"]),
            str(h["kind"]),
            str(h["record_id"]),
        )
    )
    unordered.sort(
        key=lambda h: (str(h["run_id"]), str(h["kind"]), str(h["record_id"]))
    )
    return comparable, unordered


# -- resume --------------------------------------------------------------

REASON_NO_ACCEPTANCE = (
    "schema 1.0.0 records no acceptance or adoption decision, so a last accepted "
    "change cannot be identified"
)
REASON_NO_BASELINE = (
    "schema 1.0.0 records no baseline or revision identity, and no boundary-owned "
    "current-revision source exists, so the current baseline is not verified"
)
REASON_GOAL_AMBIGUOUS = (
    "the covered runs do not share one work package, so no single current goal "
    "can be named"
)
REASON_NO_EVIDENCE_LINK = "claim references a record that is not present in the store"
REASON_REPORTED_UNVERIFIED = "source-reported text; no record verifies it"

# Run states that are blockers rather than ordinary unfinished work.
_BLOCKING_STATES = (
    memory.RUN_BLOCKED,
    memory.RUN_UNSUPPORTED,
    memory.RUN_MISSING_TERMINAL,
    memory.RUN_UNKNOWN_OUTCOME,
)


def resume(stores: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Compose an evidence-linked Resume over a bounded set of runs.

    Returns ``(result, error_reason)``. The composition is deterministic and
    narrates nothing: every entry is a typed field, a projected claim, or an
    explicit statement that the schema cannot support the fact.
    """
    if not isinstance(stores, list):
        return None, REASON_NOT_STORES

    runs: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    unverified: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    goals: List[Dict[str, Any]] = []

    for store in stores:
        if not isinstance(store, dict):
            continue
        index = _RunIndex(store)
        run_id = index.run_id
        if run_id is None:
            continue

        stale = index.run.get("finalized") is not True
        runs.append(
            {
                "run_id": run_id,
                "state": index.state,
                "success": index.success,
                "stale": stale,
                "stale_reasons": (
                    [memory_docs.REASON_NOT_FINALIZED] if stale else []
                ),
                "baseline_status": "unsupported",
                "project_id": index.run.get("project_id"),
                "work_package_id": index.run.get("work_package_id"),
            }
        )

        package = index.work_package
        if isinstance(package, dict):
            goals.append(
                {
                    "work_package_id": index.run.get("work_package_id"),
                    "title": package.get("title"),
                    "run_id": run_id,
                    "provenance": "reported",
                    "target": {
                        "run_id": run_id,
                        "kind": memory_docs.LINK_WORK_PACKAGE,
                        "record_id": index.run.get("work_package_id"),
                    },
                }
            )

        limitation = memory_docs.STATE_LIMITATIONS.get(index.state or "")
        if limitation is not None or not index.success:
            blockers.append(
                {
                    "kind": "run_state",
                    "run_id": run_id,
                    "state": index.state,
                    "limitations": [limitation] if limitation else [],
                    "target": {
                        "run_id": run_id,
                        "kind": memory_docs.LINK_RUN,
                        "record_id": run_id,
                    },
                }
            )
        for kind in (memory_docs.LINK_REJECTION, memory_docs.LINK_QUARANTINE):
            for record in _records_of(store, kind):
                blockers.append(
                    {
                        "kind": kind,
                        "run_id": run_id,
                        "reason": record.get("reason"),
                        "limitations": [],
                        "target": {
                            "run_id": run_id,
                            "kind": kind,
                            "record_id": record.get("id"),
                        },
                    }
                )
        for text in memory_docs.STATE_NEXT_ACTIONS.get(index.state or "", ()):
            actions.append(
                {
                    "text": text[:MAX_RESUME_ACTION_CHARS],
                    "provenance": "inferred",
                    "run_id": run_id,
                    "target": {
                        "run_id": run_id,
                        "kind": memory_docs.LINK_RUN,
                        "record_id": run_id,
                    },
                }
            )

        document_set, _err, _report = memory_docs.project_store(store)
        if isinstance(document_set, dict):
            for claim in _claims(document_set):
                reason = _unverified_reason(claim)
                if reason is None:
                    continue
                unverified.append(
                    {
                        "claim_id": claim.get("id"),
                        "statement": claim.get("statement"),
                        "provenance": claim.get("provenance"),
                        "run_id": run_id,
                        "reason": reason,
                        "target": _first_link_target(run_id, claim),
                    }
                )

    unique_actions = {a["text"]: a for a in actions}
    ordered_actions = [unique_actions[k] for k in sorted(unique_actions)]

    # A goal is a work package, not a run: several runs of one work package
    # describe one goal, and only genuinely different packages are ambiguous.
    distinct: Dict[Any, Dict[str, Any]] = {}
    for goal in goals:
        distinct.setdefault(goal.get("work_package_id"), goal)
    unique_goals = [distinct[key] for key in sorted(distinct, key=str)]

    if len(unique_goals) == 1:
        current_goal: Dict[str, Any] = dict(unique_goals[0])
        current_goal["status"] = "named"
    elif len(unique_goals) > 1:
        current_goal = {
            "status": "ambiguous",
            "limitations": [REASON_GOAL_AMBIGUOUS],
            "candidates": sorted(
                unique_goals, key=lambda g: str(g["work_package_id"])
            )[:MAX_RESUME_ITEMS],
        }
    else:
        current_goal = {
            "status": "unsupported",
            "limitations": ["no covered run records a work package"],
        }

    blockers.sort(key=lambda b: (str(b["run_id"]), str(b["kind"]), str(b["target"])))
    unverified.sort(key=lambda u: (str(u["run_id"]), str(u["claim_id"])))

    result = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "generator": QUERY_GENERATOR,
        "memory_schema_version": memory.MEMORY_SCHEMA_VERSION,
        "run_count": len(runs),
        "runs": sorted(runs, key=lambda r: str(r["run_id"])),
        # Acceptance requires a typed accepted/adopted record, which schema
        # 1.0.0 does not have. Completion is a different fact and is reported
        # separately below, never as acceptance.
        "last_accepted_change": {
            "status": "unsupported",
            "reason": REASON_NO_ACCEPTANCE,
        },
        "completed_runs": [
            {
                "run_id": r["run_id"],
                "state": r["state"],
                "target": {
                    "run_id": r["run_id"],
                    "kind": memory_docs.LINK_RUN,
                    "record_id": r["run_id"],
                },
            }
            for r in sorted(runs, key=lambda r: str(r["run_id"]))
            if r["success"]
        ],
        "current_goal": current_goal,
        "current_baseline": {"status": "not_verified", "reason": REASON_NO_BASELINE},
        "blockers": blockers[:MAX_RESUME_ITEMS],
        "unverified_claims": unverified[:MAX_RESUME_ITEMS],
        "next_actions": ordered_actions[:MAX_RESUME_ITEMS],
        "truncated": {
            "blockers": len(blockers) > MAX_RESUME_ITEMS,
            "unverified_claims": len(unverified) > MAX_RESUME_ITEMS,
            "next_actions": len(ordered_actions) > MAX_RESUME_ITEMS,
        },
        "limitations": sorted(
            {
                "acceptance is unsupported and is never inferred from completion",
                REASON_NO_ACCEPTANCE,
                REASON_NO_BASELINE,
                "an unverified claim is one whose evidence link did not resolve; "
                "it is reported, never repaired",
                "nothing here is generated prose: every entry is a typed field or "
                "an accepted projected claim",
            }
        ),
    }
    return result, None


def _unverified_reason(claim: Dict[str, Any]) -> Optional[str]:
    """Return why a claim is unverified, or ``None`` when it is supported.

    A claim is unverified when a reference it makes does not resolve, or when the
    value is the source's own reported text rather than a fact the contract
    validated. An aggregate claim that names no record at all is *not* reported
    here: it asserts nothing a record could support, and listing it would
    bury the real gaps in noise.
    """
    unresolved = claim.get("unresolved")
    if isinstance(unresolved, list) and unresolved:
        for entry in unresolved:
            if isinstance(entry, dict) and entry.get("reason"):
                return str(entry["reason"])
        return REASON_NO_EVIDENCE_LINK
    if claim.get("provenance") == "reported":
        return REASON_REPORTED_UNVERIFIED
    return None


def _first_link_target(run_id: Any, claim: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the first resolved support target a claim names, if any."""
    links = claim.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and isinstance(link.get("id"), str):
                return {
                    "run_id": run_id,
                    "kind": link.get("kind"),
                    "record_id": link["id"],
                }
    return None


def _records_of(store: Dict[str, Any], kind: str) -> List[Dict[str, Any]]:
    return [record for k, record in memory_docs.iter_records(store) if k == kind]


def _claims(document_set: Dict[str, Any]) -> List[Dict[str, Any]]:
    documents = document_set.get("documents")
    if not isinstance(documents, dict):
        return []
    claims: List[Dict[str, Any]] = []
    for document_type in sorted(documents):
        document = documents[document_type]
        if not isinstance(document, dict):
            continue
        for claim in document.get("claims") or []:
            if isinstance(claim, dict):
                claims.append(claim)
    return claims


# -- canonical rendering -------------------------------------------------


def render(payload: Dict[str, Any]) -> str:
    """Return the canonical serialization of a query result."""
    return memory.dumps(payload)
