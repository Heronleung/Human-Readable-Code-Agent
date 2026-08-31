"""Deterministic Structured Code Twin domain (P3.3).

This module is the Qt-free, dependency-free *domain* for the read-side
Structured Code Twin. It turns a deterministic scanner document
(:mod:`hrca.scanner`) plus a set of content fingerprints into a versioned,
canonical Twin store, and reconciles a previous store against a new scan
incrementally.

The module is deliberately pure:

* it performs **no filesystem access** (the boundary supplies scanner facts and
  content fingerprints), so it can never read or write the selected repository;
* it performs **no model, provider, network, credential or telemetry** call and
  emits no model-generated explanation — every projection string is assembled
  deterministically from source-grounded facts only;
* it is **Qt-free** and imports only the standard library, so the desktop
  client can never import it (that rule is enforced by
  :mod:`tests.test_architecture`).

The Twin store is a single canonical JSON document with these top-level arrays:

* ``workspace_revision`` — workspace identity, scan generation, baseline
  fingerprint and the last persisted synchronization state;
* ``artifacts`` — one :class:`SourceArtifact` per supported file / class /
  function / method;
* ``behavior_nodes`` — deterministic ordered :class:`BehaviorNode` records for
  supported behavior categories;
* ``correspondences`` — links a behavior node or projection to its source
  anchor;
* ``projections`` — one :class:`HumanProjection` per artifact.

Synchronization states, provenance values, artifact kinds and behavior
categories are all fixed constants below.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

TWIN_SCHEMA_VERSION = "1.0.0"
TWIN_GENERATOR = "hrca-twin"

# -- provenance ----------------------------------------------------------

# A Twin record is ``verified`` when its content is derived directly from
# scanner evidence, and ``unresolved`` when the deterministic scanner cannot
# model it. P3.3 may only ever produce these two; ``inferred`` and
# ``user_authored`` are reserved for later phases and are never emitted.
PROVENANCE_VERIFIED = "verified"
PROVENANCE_UNRESOLVED = "unresolved"
PROVENANCE_INFERRED = "inferred"
PROVENANCE_USER_AUTHORED = "user_authored"
PROVENANCES = frozenset(
    {PROVENANCE_VERIFIED, PROVENANCE_UNRESOLVED, PROVENANCE_INFERRED,
     PROVENANCE_USER_AUTHORED}
)

# -- synchronization states ----------------------------------------------

SYNC_SYNCHRONIZED = "synchronized"
SYNC_NO_CHANGE = "no_change"
SYNC_NEEDS_REVIEW = "needs_review"
SYNC_STALE = "stale"
SYNC_BLOCKED = "blocked"
SYNC_CONFLICT = "conflict"
SYNC_UNSUPPORTED = "unsupported"
SYNC_STATES = frozenset(
    {
        SYNC_SYNCHRONIZED,
        SYNC_NO_CHANGE,
        SYNC_NEEDS_REVIEW,
        SYNC_STALE,
        SYNC_BLOCKED,
        SYNC_CONFLICT,
        SYNC_UNSUPPORTED,
    }
)

# -- artifact kinds ------------------------------------------------------

ARTIFACT_FILE = "file"
ARTIFACT_CLASS = "class"
ARTIFACT_FUNCTION = "function"
ARTIFACT_METHOD = "method"
ARTIFACT_KINDS = frozenset(
    {ARTIFACT_FILE, ARTIFACT_CLASS, ARTIFACT_FUNCTION, ARTIFACT_METHOD}
)

# -- behavior node categories --------------------------------------------

BEHAVIOR_INPUTS = "inputs"
BEHAVIOR_OUTPUTS = "outputs"
BEHAVIOR_CONDITIONS = "conditions"
BEHAVIOR_LOOPS = "loops"
BEHAVIOR_CALLS = "calls"
BEHAVIOR_EXCEPTIONS = "exceptions"
BEHAVIOR_SIDE_EFFECTS = "side_effects"
BEHAVIOR_DEPENDENCIES = "dependencies"
BEHAVIOR_INVARIANTS = "invariants"

# Behavior categories the deterministic scanner can derive from source evidence,
# mapped to the scanner relation ``kind`` that provides the evidence.
_RELATION_TO_BEHAVIOR = (
    ("returns", BEHAVIOR_OUTPUTS),
    ("calls", BEHAVIOR_CALLS),
    ("raises", BEHAVIOR_EXCEPTIONS),
    ("imports", BEHAVIOR_DEPENDENCIES),
)

# Behavior categories the P3.3 deterministic scanner does not model and must
# therefore be emitted as an *explicit* unresolved node, never guessed.
_UNSUPPORTED_BEHAVIOR = (
    BEHAVIOR_CONDITIONS,
    BEHAVIOR_LOOPS,
    BEHAVIOR_SIDE_EFFECTS,
    BEHAVIOR_INVARIANTS,
)

# Fixed bounded reason attached to every unsupported behavior node.
_UNSUPPORTED_BEHAVIOR_REASON = (
    "not modeled by the deterministic scanner (P3.3); no runtime or inference "
    "evidence is available"
)

# Confidence values carried through from the scanner.
CONF_HIGH = "high"
CONF_LOW = "low"

# Python source suffixes the Twin models as file artifacts/projections. ``.pyi``
# type stubs share the same deterministic treatment as ``.py`` modules.
_PY_SUFFIXES = (".py", ".pyi")


def sha256_hex(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def fingerprint_bytes(data: bytes) -> str:
    """Return a content fingerprint (SHA-256 hex) for raw source ``data``."""
    return sha256_hex(data)


def fingerprint_source(source: str) -> str:
    """Return a content fingerprint for ``source`` text (UTF-8 encoded)."""
    return fingerprint_bytes(source.encode("utf-8"))


# -- deterministic identifiers -------------------------------------------

# Identifiers are stable and deterministic. Symbol identifiers keep the
# scanner's ``module.path.Class.method`` locator; file identifiers use the
# portable root-relative path. A formatting-only change never changes any
# identifier because they are derived from the path and qualified name, never
# from content.


def workspace_id_for(canonical_root: str) -> str:
    """Return the canonical workspace identifier for a canonical root path."""
    return "ws:" + sha256_hex(canonical_root.encode("utf-8"))


def _portable(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


def file_artifact_id(rel_path: str) -> str:
    """Return the SourceArtifact id for a file (``artifact:file:<path>``)."""
    return f"artifact:file:{_portable(rel_path)}"


def symbol_artifact_id(qname: str, kind: str) -> str:
    """Return the SourceArtifact id for a symbol (``artifact:<kind>:<qname>``)."""
    return f"artifact:{kind}:{qname}"


def behavior_node_id(qname: str, category: str, ordinal: int) -> str:
    """Return the BehaviorNode id for a symbol's behavior category."""
    return f"behavior:{qname}:{category}:{ordinal}"


def correspondence_id(artifact_id: str, node_id: str) -> str:
    """Return the Correspondence id linking a projection/behavior to an artifact."""
    return f"correspondence:{artifact_id}:{node_id}"


def projection_id(artifact_id: str) -> str:
    """Return the HumanProjection id for a SourceArtifact."""
    return f"projection:{artifact_id}"


# -- schema migration ----------------------------------------------------

# Pure migrations map an *older* ``schema_version`` to a function that upgrades
# a store dict in place. There are no historical versions before 1.0.0, so the
# registry is empty; it exists so later phases can add migrations without
# changing the load path. A store with a *future* (or unknown) version is never
# migrated and never overwritten.
MIGRATIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def _version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split(".") if p.isdigit()) or (0,)


def migrate_store(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and migrate a raw store dict to the current schema version.

    Returns ``(store, error)``: on success ``error`` is ``None``; on a
    future/unknown version or a malformed store, ``store`` is ``None`` and
    ``error`` is a bounded reason. The caller must retain the last valid store
    whenever this returns an error.
    """
    if not isinstance(raw, dict):
        return None, "store is not a mapping"
    version = raw.get("schema_version")
    if not isinstance(version, str) or not version:
        return None, "missing schema_version"
    try:
        current = _version_tuple(TWIN_SCHEMA_VERSION)
        found = _version_tuple(version)
    except ValueError:
        return None, "invalid schema_version"

    if found == current:
        return raw, None
    if found > current:
        return None, "schema_version is newer than supported"
    if version not in MIGRATIONS:
        return None, "schema_version is not migratable"
    return MIGRATIONS[version](dict(raw)), None


# -- baseline fingerprint ------------------------------------------------

def baseline_fingerprint(file_fingerprints: Dict[str, Optional[str]]) -> str:
    """Return a deterministic fingerprint over ``{path: fingerprint}``.

    The baseline captures *which* supported files exist and *what* content each
    has, so a content change, an addition, or a removal always changes the
    fingerprint while a byte-identical rescan keeps it unchanged.
    """
    pairs = [(p, fp) for p, fp in sorted(file_fingerprints.items())]
    canon = json.dumps(pairs, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256_hex(canon.encode("utf-8"))


def dumps(store: Dict[str, Any]) -> str:
    """Serialize a store to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(store, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# -- scanner-fact indexing -----------------------------------------------

def _artifact_kind(symbol: Dict[str, Any]) -> Optional[str]:
    """Map a scanner symbol to a Twin artifact kind, or ``None`` if unsupported."""
    kind = symbol.get("kind")
    if kind == "class":
        return ARTIFACT_CLASS
    if kind in ("function", "async_function"):
        if symbol.get("is_method"):
            return ARTIFACT_METHOD
        return ARTIFACT_FUNCTION
    # modules, parameters and variables are not standalone Twin artifacts.
    return None


def _index(scanner_doc: Dict[str, Any]) -> Tuple[
    Dict[str, Dict[str, Any]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, Dict[str, Any]],
]:
    """Index a scanner document for fast lookup.

    Returns ``(symbols_by_id, symbols_by_file, relations_by_source, files_by_path)``.
    """
    symbols_by_id: Dict[str, Dict[str, Any]] = {}
    symbols_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for sym in scanner_doc.get("symbols", []):
        if isinstance(sym, dict) and "id" in sym:
            symbols_by_id[sym["id"]] = sym
            file = sym.get("file")
            if isinstance(file, str):
                symbols_by_file.setdefault(file, []).append(sym)

    relations_by_source: Dict[str, List[Dict[str, Any]]] = {}
    for rel in scanner_doc.get("relations", []):
        if isinstance(rel, dict):
            source = rel.get("source")
            if isinstance(source, str):
                relations_by_source.setdefault(source, []).append(rel)

    files_by_path: Dict[str, Dict[str, Any]] = {}
    for f in scanner_doc.get("files", []):
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            files_by_path[f["path"]] = f

    return symbols_by_id, symbols_by_file, relations_by_source, files_by_path


def _source_lineno(rec: Dict[str, Any]) -> Tuple[int, int, str]:
    rng = rec.get("source_range") or {}
    return (rng.get("lineno") or 0, rng.get("col_offset") or 0, rec.get("id", ""))


def _sort_symbols(symbols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(symbols, key=_source_lineno)


def _sort_relations(relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(relations, key=_source_lineno)


# -- record builders -----------------------------------------------------

def _file_artifact(
    file_rec: Dict[str, Any],
    fingerprint: Optional[str],
    sync_state: str,
    source_range: Optional[Dict[str, Any]],
    moved_from: Optional[str] = None,
) -> Dict[str, Any]:
    rel_path = _portable(file_rec["path"])
    rec: Dict[str, Any] = {
        "record_type": "artifact",
        "id": file_artifact_id(rel_path),
        "kind": ARTIFACT_FILE,
        "path": rel_path,
        "name": rel_path.rsplit("/", 1)[-1],
        "module": file_rec.get("module"),
        "source_range": source_range,
        "fingerprint": fingerprint,
        "syntax_status": file_rec.get("syntax_status"),
        "provenance": PROVENANCE_VERIFIED,
        "confidence": CONF_HIGH,
        "sync_state": sync_state,
    }
    if moved_from is not None:
        rec["moved_from"] = moved_from
    return rec


def _symbol_artifact(symbol: Dict[str, Any], kind: str, sync_state: str) -> Dict[str, Any]:
    qname = symbol["id"]
    return {
        "record_type": "artifact",
        "id": symbol_artifact_id(qname, kind),
        "kind": kind,
        "path": _portable(symbol.get("file", "")),
        "module": qname.rsplit(".", 1)[0],
        "name": symbol.get("name"),
        "locator": qname,
        "source_range": symbol.get("source_range"),
        "provenance": PROVENANCE_VERIFIED,
        "confidence": symbol.get("confidence", CONF_HIGH),
        "sync_state": sync_state,
    }


def _params_for(symbols_by_id: Dict[str, Dict[str, Any]], qname: str) -> List[str]:
    out: List[str] = []
    for sym in symbols_by_id.values():
        if sym.get("parent_id") == qname and sym.get("kind") == "parameter":
            name = sym.get("name", "")
            ann = sym.get("type_annotation")
            out.append(f"{name}: {ann}" if ann else name)
    return out


def _signature(symbol: Dict[str, Any], params: List[str]) -> str:
    name = symbol.get("name", "?")
    ret = symbol.get("return_annotation")
    sig = f"{name}({', '.join(params)})"
    if ret:
        sig += f" -> {ret}"
    return sig


def _kind_word(kind: str, symbol: Dict[str, Any]) -> str:
    if kind == ARTIFACT_METHOD:
        return "Method"
    if kind == ARTIFACT_FUNCTION:
        if symbol.get("async"):
            return "Async function"
        return "Function"
    return "Class"


def _projection_summary(kind: str, symbol: Dict[str, Any], params: List[str]) -> str:
    word = _kind_word(kind, symbol)
    if kind == ARTIFACT_CLASS:
        bases = symbol.get("bases") or []
        if bases:
            return f"{word} {symbol.get('name', '?')}({', '.join(bases)})"
        return f"{word} {symbol.get('name', '?')}"
    return f"{word} {_signature(symbol, params)}"


def _behavior_items(
    relations: List[Dict[str, Any]],
    kind_filter: str,
) -> Tuple[List[str], bool]:
    """Return ``(items, has_unresolved)`` for ``kind_filter`` relations."""
    items: List[str] = []
    has_unresolved = False
    for rel in _sort_relations(relations):
        if rel.get("kind") != kind_filter:
            continue
        if rel.get("status") == "unresolved" or rel.get("confidence") == CONF_LOW:
            has_unresolved = True
        target = rel.get("target")
        items.append("<unresolved>" if target is None else str(target))
    return items, has_unresolved


def _behavior_nodes(
    qname: str,
    rel_path: str,
    params: List[str],
    relations: List[Dict[str, Any]],
    source_range: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Build the deterministic ordered BehaviorNode list for a function/method.

    Returns ``(nodes, limitations)`` where ``limitations`` are bounded strings
    derived from unresolved behavior (dynamic imports / unmodeled categories).
    """
    nodes: List[Dict[str, Any]] = []
    limitations: List[str] = []
    unresolved_evidence = False

    def emit(category: str, items: List[str], confidence: str, reason: Optional[str]) -> None:
        node: Dict[str, Any] = {
            "record_type": "behavior",
            "id": behavior_node_id(qname, category, len(nodes)),
            "symbol": qname,
            "path": rel_path,
            "category": category,
            "provenance": PROVENANCE_VERIFIED,
            "confidence": confidence,
            "items": items,
        }
        if source_range is not None:
            node["source_range"] = source_range
        if reason is not None:
            node["reason"] = reason
        nodes.append(node)

    if params:
        emit(BEHAVIOR_INPUTS, params, CONF_HIGH, None)

    for rel_kind, category in _RELATION_TO_BEHAVIOR:
        items, has_unresolved = _behavior_items(relations, rel_kind)
        if not items:
            continue
        confidence = CONF_LOW if has_unresolved else CONF_HIGH
        emit(category, items, confidence, None)
        if has_unresolved:
            unresolved_evidence = True

    # Explicitly unresolved categories the scanner never models.
    for category in _UNSUPPORTED_BEHAVIOR:
        node: Dict[str, Any] = {
            "record_type": "behavior",
            "id": behavior_node_id(qname, category, len(nodes)),
            "symbol": qname,
            "path": rel_path,
            "category": category,
            "provenance": PROVENANCE_UNRESOLVED,
            "confidence": CONF_LOW,
            "items": [],
            "reason": _UNSUPPORTED_BEHAVIOR_REASON,
        }
        if source_range is not None:
            node["source_range"] = source_range
        nodes.append(node)

    if unresolved_evidence:
        limitations.append(
            "a dynamic or unresolved dependency cannot be statically resolved "
            "and is marked with low confidence"
        )

    return nodes, limitations


def _projection(
    artifact_id: str,
    kind: str,
    locator: str,
    path: str,
    name: str,
    summary: str,
    details: List[str],
    limitations: List[str],
    confidence: str,
    sync_state: str,
    source_range: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "record_type": "projection",
        "id": projection_id(artifact_id),
        "artifact_id": artifact_id,
        "kind": kind,
        "locator": locator,
        "path": path,
        "name": name,
        "summary": summary,
        "details": details,
        "provenance": PROVENANCE_VERIFIED,
        "confidence": confidence,
        "sync_state": sync_state,
        "limitations": limitations,
    }
    if source_range is not None:
        rec["source_anchor"] = {"file": path, "source_range": source_range}
    return rec


def _correspondence(
    artifact_id: str,
    node_id: str,
    node_kind: str,
    anchor: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "record_type": "correspondence",
        "id": correspondence_id(artifact_id, node_id),
        "artifact_id": artifact_id,
        "node_id": node_id,
        "node_kind": node_kind,
    }
    if anchor is not None:
        rec["source_anchor"] = anchor
    return rec


# -- full build ----------------------------------------------------------

def _module_source_range(
    symbols_by_file: Dict[str, List[Dict[str, Any]]], rel_path: str
) -> Optional[Dict[str, Any]]:
    for sym in symbols_by_file.get(rel_path, []):
        if sym.get("kind") == "module":
            return sym.get("source_range")
    return None


def build_store(
    scanner_doc: Dict[str, Any],
    fingerprints: Dict[str, Optional[str]],
    workspace_id: str,
    scan_generation: int,
    scan_timestamp: str,
    sync_state: str = SYNC_SYNCHRONIZED,
) -> Dict[str, Any]:
    """Build a full canonical Twin store from a scanner document.

    ``fingerprints`` maps every supported ``.py`` file path to its content
    fingerprint (or ``None`` when the content could not be read). The result is
    deterministic: arrays are sorted by identifier and keys are canonical.
    """
    symbols_by_id, symbols_by_file, relations_by_source, files_by_path = _index(scanner_doc)

    artifacts: Dict[str, Dict[str, Any]] = {}
    behavior_nodes: Dict[str, Dict[str, Any]] = {}
    correspondences: Dict[str, Dict[str, Any]] = {}
    projections: Dict[str, Dict[str, Any]] = {}

    for rel_path in sorted(p for p in files_by_path if p.endswith(_PY_SUFFIXES)):
        file_rec = files_by_path[rel_path]
        fp = fingerprints.get(rel_path)
        file_state = sync_state if file_rec.get("syntax_status") == "ok" else SYNC_STALE
        module_range = _module_source_range(symbols_by_file, rel_path)
        file_artifact = _file_artifact(file_rec, fp, file_state, module_range)
        artifacts[file_artifact["id"]] = file_artifact

        module = file_rec.get("module") or rel_path
        if file_rec.get("syntax_status") == "ok":
            summary = f"Python module {module}"
            file_limitations: List[str] = []
        else:
            summary = f"Python module {module} (parse error)"
            file_limitations = [
                "this file could not be parsed; no symbols or behavior are modeled"
            ]
        file_details = _file_projection_details(symbols_by_id, symbols_by_file, rel_path)
        file_proj = _projection(
            file_artifact["id"],
            ARTIFACT_FILE,
            rel_path,
            rel_path,
            file_artifact["name"],
            summary,
            file_details,
            file_limitations,
            CONF_HIGH,
            file_artifact["sync_state"],
            file_artifact["source_range"],
        )
        projections[file_proj["id"]] = file_proj
        correspondences[correspondence_id(file_artifact["id"], file_proj["id"])] = (
            _correspondence(file_artifact["id"], file_proj["id"], "projection",
                            file_proj.get("source_anchor"))
        )

        for sym in _sort_symbols(symbols_by_file.get(rel_path, [])):
            kind = _artifact_kind(sym)
            if kind is None:
                continue
            qname = sym["id"]
            symbol_artifact = _symbol_artifact(sym, kind, file_state)
            artifacts[symbol_artifact["id"]] = symbol_artifact

            params = _params_for(symbols_by_id, qname)
            relations = relations_by_source.get(qname, [])
            limitations: List[str] = []

            if kind in (ARTIFACT_FUNCTION, ARTIFACT_METHOD):
                nodes, limitations = _behavior_nodes(
                    qname, rel_path, params, relations, sym.get("source_range")
                )
                for node in nodes:
                    behavior_nodes[node["id"]] = node
                    anchor = (
                        {"file": rel_path, "source_range": node["source_range"]}
                        if node.get("source_range") is not None else None
                    )
                    correspondences[correspondence_id(symbol_artifact["id"], node["id"])] = (
                        _correspondence(symbol_artifact["id"], node["id"], "behavior", anchor)
                    )

            proj_summary = _projection_summary(kind, sym, params)
            details = _symbol_projection_details(
                kind, qname, params, relations_by_source.get(qname, []), symbols_by_file, rel_path
            )
            symbol_proj = _projection(
                symbol_artifact["id"],
                kind,
                qname,
                rel_path,
                sym.get("name", ""),
                proj_summary,
                details,
                limitations,
                sym.get("confidence", CONF_HIGH),
                file_state,
                sym.get("source_range"),
            )
            projections[symbol_proj["id"]] = symbol_proj
            correspondences[correspondence_id(symbol_artifact["id"], symbol_proj["id"])] = (
                _correspondence(symbol_artifact["id"], symbol_proj["id"], "projection",
                                symbol_proj.get("source_anchor"))
            )

    return _assemble(
        workspace_id,
        scan_generation,
        baseline_fingerprint(fingerprints),
        scan_timestamp,
        sync_state,
        artifacts,
        behavior_nodes,
        correspondences,
        projections,
    )


def _file_projection_details(
    symbols_by_id: Dict[str, Dict[str, Any]],
    symbols_by_file: Dict[str, List[Dict[str, Any]]],
    rel_path: str,
) -> List[str]:
    details: List[str] = []
    for sym in _sort_symbols(symbols_by_file.get(rel_path, [])):
        kind = _artifact_kind(sym)
        if kind is None:
            continue
        word = _kind_word(kind, sym)
        if kind == ARTIFACT_CLASS:
            details.append(f"{word} {sym.get('name', '?')}")
        else:
            params = _params_for(symbols_by_id, sym["id"])
            details.append(f"{word} {_signature(sym, params)}")
    return details


def _symbol_projection_details(
    kind: str,
    qname: str,
    params: List[str],
    relations: List[Dict[str, Any]],
    symbols_by_file: Dict[str, List[Dict[str, Any]]],
    rel_path: str,
) -> List[str]:
    details: List[str] = []
    if kind in (ARTIFACT_FUNCTION, ARTIFACT_METHOD):
        if params:
            details.append(f"Parameters: {', '.join(params)}")
        for rel_kind, label in (("calls", "Calls"), ("raises", "Raises"), ("imports", "Dependencies")):
            items, _ = _behavior_items(relations, rel_kind)
            if items:
                details.append(f"{label}: {', '.join(items)}")
    elif kind == ARTIFACT_CLASS:
        details = [
            f"Method {m.get('name', '?')}()"
            for m in _sort_symbols(symbols_by_file.get(rel_path, []))
            if m.get("parent_id") == qname and _artifact_kind(m) is not None
        ]
    return details


def _assemble(
    workspace_id: str,
    scan_generation: int,
    base_fp: str,
    scan_timestamp: str,
    sync_state: str,
    artifacts: Dict[str, Dict[str, Any]],
    behavior_nodes: Dict[str, Dict[str, Any]],
    correspondences: Dict[str, Dict[str, Any]],
    projections: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema_version": TWIN_SCHEMA_VERSION,
        "generator": TWIN_GENERATOR,
        "workspace_revision": {
            "workspace_id": workspace_id,
            "scan_generation": scan_generation,
            "baseline_fingerprint": base_fp,
            "scan_timestamp": scan_timestamp,
            "sync_state": sync_state,
        },
        "artifacts": [artifacts[k] for k in sorted(artifacts)],
        "behavior_nodes": [behavior_nodes[k] for k in sorted(behavior_nodes)],
        "correspondences": [correspondences[k] for k in sorted(correspondences)],
        "projections": [projections[k] for k in sorted(projections)],
    }


# -- incremental synchronization -----------------------------------------

def _records_index(store: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map record id -> record for every top-level array in ``store``."""
    index: Dict[str, Dict[str, Any]] = {}
    for key in ("artifacts", "behavior_nodes", "correspondences", "projections"):
        for rec in store.get(key, []):
            if isinstance(rec, dict) and isinstance(rec.get("id"), str):
                index[rec["id"]] = rec
    return index


def _file_fingerprints(store: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Recover ``{path: fingerprint}`` from a store's file artifacts."""
    out: Dict[str, Optional[str]] = {}
    for rec in store.get("artifacts", []):
        if rec.get("kind") == ARTIFACT_FILE and isinstance(rec.get("path"), str):
            out[rec["path"]] = rec.get("fingerprint")
    return out


def _mark_state(rec: Dict[str, Any], state: str) -> Dict[str, Any]:
    """Return a copy of ``rec`` with ``sync_state`` updated (if it carries one)."""
    if rec.get("record_type") in ("artifact", "projection"):
        out = dict(rec)
        out["sync_state"] = state
        return out
    return rec


def sync_twin(
    scanner_doc: Dict[str, Any],
    fingerprints: Dict[str, Optional[str]],
    previous: Optional[Dict[str, Any]],
    workspace_id: str,
    scan_generation: int,
    scan_timestamp: str,
    changed_paths: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Reconcile ``scanner_doc`` against ``previous`` and return ``(store, result)``.

    * ``fingerprints`` maps every supported ``.py`` path to its content
      fingerprint (or ``None`` when unreadable).
    * ``previous`` is the last valid store (``None`` on first sync).
    * ``changed_paths`` restricts reconciliation to the given root-relative
      paths; when ``None`` the full set of fingerprints is diffed.

    The returned ``result`` is the :class:`SynchronizationResult` record with a
    fixed ``state`` and a bounded ``reason``. A ``no_change`` scan returns the
    *previous* store byte-for-byte (never re-serialized) so two no-change scans
    remain byte-identical.
    """
    files_by_path = {f["path"]: f for f in scanner_doc.get("files", []) if isinstance(f.get("path"), str)}

    if previous is None:
        store = build_store(scanner_doc, fingerprints, workspace_id, scan_generation, scan_timestamp)
        return store, _result(SYNC_SYNCHRONIZED, workspace_id, scan_generation,
                              store, sorted(fingerprints), None)

    prev_fp = _file_fingerprints(previous)
    prev_base = previous.get("workspace_revision", {}).get("baseline_fingerprint")
    curr_base = baseline_fingerprint(fingerprints)

    # An outstanding human-side draft plus changed source is a conflict; both the
    # draft and the prior Twin are preserved, never silently overwritten.
    if previous.get("human_draft") and curr_base != prev_base:
        preserved = _with_preserved_draft(previous, SYNC_CONFLICT)
        return preserved, _result(
            SYNC_CONFLICT, workspace_id, scan_generation, preserved,
            _changed_paths(prev_fp, fingerprints, changed_paths),
            "an outstanding human draft conflicts with changed source",
        )

    if curr_base == prev_base:
        return previous, _result(SYNC_NO_CHANGE, workspace_id, scan_generation,
                                 previous, [], None)

    dirty = _changed_paths(prev_fp, fingerprints, changed_paths)

    removed = sorted(p for p in prev_fp if p not in fingerprints)
    added = sorted(p for p in fingerprints if p not in prev_fp)
    moves: Dict[str, str] = {}
    ambiguous: List[str] = []
    for old_path in removed:
        old_fp = prev_fp[old_path]
        if old_fp is None:
            continue
        candidates = [p for p in added if fingerprints[p] == old_fp]
        if len(candidates) == 1:
            moves[old_path] = candidates[0]
        elif len(candidates) > 1:
            ambiguous.append(old_path)

    store = _rebuild(
        scanner_doc, fingerprints, previous, workspace_id, scan_generation,
        scan_timestamp, dirty, moves, ambiguous, files_by_path,
    )

    if ambiguous:
        state = SYNC_NEEDS_REVIEW
        reason = "duplicate content fingerprint makes move/rename identity ambiguous"
    else:
        state = SYNC_SYNCHRONIZED
        reason = None
    stale_paths = [
        p for p in dirty
        if p in files_by_path and files_by_path[p].get("syntax_status") != "ok"
    ]
    if stale_paths:
        state = SYNC_STALE
        reason = "one or more files could not be parsed; last valid projection retained"

    return store, _result(state, workspace_id, scan_generation, store, dirty, reason)


def _changed_paths(
    prev_fp: Dict[str, Optional[str]],
    fingerprints: Dict[str, Optional[str]],
    restricted: Optional[List[str]],
) -> List[str]:
    if restricted:
        wanted = {p for p in restricted if p in fingerprints or p in prev_fp}
        return sorted(wanted)
    changed = set()
    for path in set(prev_fp) | set(fingerprints):
        if prev_fp.get(path) != fingerprints.get(path):
            changed.add(path)
    return sorted(changed)


def _with_preserved_draft(previous: Dict[str, Any], state: str) -> Dict[str, Any]:
    """Return a copy of ``previous`` with the sync state updated (draft preserved)."""
    store = json.loads(json.dumps(previous))
    store["workspace_revision"]["sync_state"] = state
    return store


def _rebuild(
    scanner_doc: Dict[str, Any],
    fingerprints: Dict[str, Optional[str]],
    previous: Dict[str, Any],
    workspace_id: str,
    scan_generation: int,
    scan_timestamp: str,
    dirty: List[str],
    moves: Dict[str, str],
    ambiguous: List[str],
    files_by_path: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Recompute only the changed paths and carry every other record verbatim."""
    full = build_store(scanner_doc, fingerprints, workspace_id, scan_generation, scan_timestamp)
    full_index = _records_index(full)
    prev_index = _records_index(previous)

    dirty_set = set(dirty)
    moved_old = set(moves)
    moved_new = set(moves.values())
    ambiguous_old = set(ambiguous)

    keep: Dict[str, Dict[str, Any]] = {}
    stale: Dict[str, Dict[str, Any]] = {}

    for rec_id, rec in prev_index.items():
        path = rec.get("path")
        if path is None:
            keep[rec_id] = rec
            continue
        if path in moved_old:
            continue  # dropped; the new path's records carry the identity
        if path in ambiguous_old:
            stale[rec_id] = _mark_state(rec, SYNC_NEEDS_REVIEW)
            continue
        if path not in dirty_set:
            keep[rec_id] = rec
            continue
        # A dirty path that is no longer in the current scan is a plain removal
        # (moves and ambiguous renames were already handled above): drop it.
        if path not in files_by_path:
            continue
        # Dirty path that still exists but no longer parses: keep last-valid
        # *symbol-level* records as stale; file-level records are replaced from
        # ``full`` (which carries the parse-error status).
        if files_by_path[path].get("syntax_status") != "ok":
            if rec.get("kind") == ARTIFACT_FILE or (
                rec.get("record_type") == "projection" and rec.get("kind") == ARTIFACT_FILE
            ):
                continue
            stale[rec_id] = _mark_state(rec, SYNC_STALE)
        # otherwise the record is recomputed from ``full`` below.

    recompute: Dict[str, Dict[str, Any]] = {}
    for rec_id, rec in full_index.items():
        path = rec.get("path")
        if path is None:
            continue
        if path in moved_new:
            recompute[rec_id] = rec
        elif path in dirty_set:
            status = files_by_path.get(path, {}).get("syntax_status")
            if status == "ok":
                recompute[rec_id] = rec
            elif rec.get("kind") == ARTIFACT_FILE or (
                rec.get("record_type") == "projection" and rec.get("kind") == ARTIFACT_FILE
            ):
                recompute[rec_id] = rec  # file-level error records

    # Annotate moved file artifacts with the source of continuity.
    for old_path, new_path in moves.items():
        new_file_id = file_artifact_id(new_path)
        if new_file_id in recompute:
            recompute[new_file_id]["moved_from"] = old_path
            recompute[new_file_id]["sync_state"] = SYNC_SYNCHRONIZED

    merged: Dict[str, Dict[str, Any]] = {}
    merged.update(keep)
    merged.update(stale)
    merged.update(recompute)

    artifacts = {k: v for k, v in merged.items() if v.get("record_type") == "artifact"}
    behavior_nodes = {k: v for k, v in merged.items() if v.get("record_type") == "behavior"}
    correspondences = {k: v for k, v in merged.items() if v.get("record_type") == "correspondence"}
    projections = {k: v for k, v in merged.items() if v.get("record_type") == "projection"}

    return _assemble(
        workspace_id,
        scan_generation,
        baseline_fingerprint(fingerprints),
        scan_timestamp,
        SYNC_SYNCHRONIZED,
        artifacts,
        behavior_nodes,
        correspondences,
        projections,
    )


def _result(
    state: str,
    workspace_id: str,
    scan_generation: int,
    store: Dict[str, Any],
    changed_paths: List[str],
    reason: Optional[str],
) -> Dict[str, Any]:
    counts = {
        "artifacts": len(store.get("artifacts", [])),
        "behavior_nodes": len(store.get("behavior_nodes", [])),
        "correspondences": len(store.get("correspondences", [])),
        "projections": len(store.get("projections", [])),
    }
    result: Dict[str, Any] = {
        "record_type": "synchronization_result",
        "state": state,
        "workspace_id": workspace_id,
        "scan_generation": scan_generation,
        "baseline_fingerprint": store.get("workspace_revision", {}).get("baseline_fingerprint"),
        "changed_paths": changed_paths,
        "counts": counts,
    }
    if reason is not None:
        result["reason"] = reason
    return result


# -- projection retrieval and anchor navigation -------------------------

def find_projection(store: Dict[str, Any], selector: str) -> Optional[Dict[str, Any]]:
    """Return the HumanProjection matching ``selector`` (a locator or file path).

    A symbol locator (``module.path.Class.method``) takes precedence over a file
    path so a symbol and a file with a coincidentally equal string never
    collide. Returns ``None`` when no projection matches.
    """
    for proj in store.get("projections", []):
        if proj.get("kind") != ARTIFACT_FILE and proj.get("locator") == selector:
            return proj
    for proj in store.get("projections", []):
        if proj.get("kind") == ARTIFACT_FILE and proj.get("path") == selector:
            return proj
    return None


def projection_bundle(store: Dict[str, Any], selector: str) -> Optional[Dict[str, Any]]:
    """Return the projection, artifact, behavior nodes and correspondences for a
    selector, or ``None`` when the selector is not represented in the store."""
    proj = find_projection(store, selector)
    if proj is None:
        return None
    artifact_id = proj.get("artifact_id")
    artifact = next(
        (a for a in store.get("artifacts", []) if a.get("id") == artifact_id), None
    )
    locator = proj.get("locator")
    behavior_nodes = [
        b for b in store.get("behavior_nodes", []) if b.get("symbol") == locator
    ]
    correspondences = [
        c for c in store.get("correspondences", []) if c.get("artifact_id") == artifact_id
    ]
    return {
        "projection": proj,
        "artifact": artifact,
        "behavior_nodes": behavior_nodes,
        "correspondences": correspondences,
    }


def anchor_for(store: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Return a bounded anchor-navigation result for a behavior ``node_id``.

    Returns ``None`` when no behavior node matches ``node_id`` (the caller maps
    that to a bounded ``twin_not_found``). Otherwise the result carries either a
    valid anchor (``available: true`` with ``file`` + ``source_range``) or a
    bounded, visibly-safe failure (``available: false`` with a fixed ``reason``)
    when the owning artifact is stale / needs-review / conflicted / blocked /
    unsupported, or the node carries no source anchor.
    """
    node = next((b for b in store.get("behavior_nodes", []) if b.get("id") == node_id), None)
    if node is None:
        return None
    corr = next(
        (c for c in store.get("correspondences", []) if c.get("node_id") == node_id), None
    )
    artifact_id = corr.get("artifact_id") if corr else None
    artifact = next(
        (a for a in store.get("artifacts", []) if a.get("id") == artifact_id), None
    )
    anchor = corr.get("source_anchor") if corr else None

    unsafe = (
        SYNC_STALE,
        SYNC_BLOCKED,
        SYNC_CONFLICT,
        SYNC_NEEDS_REVIEW,
        SYNC_UNSUPPORTED,
    )
    state = (artifact or {}).get("sync_state", SYNC_SYNCHRONIZED)

    if anchor is None:
        return {"node_id": node_id, "available": False, "reason": "no_anchor"}
    result: Dict[str, Any] = {
        "node_id": node_id,
        "available": True,
        "file": anchor.get("file"),
        "source_range": anchor.get("source_range"),
        "sync_state": state,
    }
    if state in unsafe:
        result["available"] = False
        result["reason"] = state
    return result


__all__ = [
    "TWIN_SCHEMA_VERSION",
    "TWIN_GENERATOR",
    "PROVENANCE_VERIFIED",
    "PROVENANCE_UNRESOLVED",
    "PROVENANCE_INFERRED",
    "PROVENANCE_USER_AUTHORED",
    "PROVENANCES",
    "SYNC_SYNCHRONIZED",
    "SYNC_NO_CHANGE",
    "SYNC_NEEDS_REVIEW",
    "SYNC_STALE",
    "SYNC_BLOCKED",
    "SYNC_CONFLICT",
    "SYNC_UNSUPPORTED",
    "SYNC_STATES",
    "ARTIFACT_FILE",
    "ARTIFACT_CLASS",
    "ARTIFACT_FUNCTION",
    "ARTIFACT_METHOD",
    "ARTIFACT_KINDS",
    "BEHAVIOR_INPUTS",
    "BEHAVIOR_OUTPUTS",
    "BEHAVIOR_CONDITIONS",
    "BEHAVIOR_LOOPS",
    "BEHAVIOR_CALLS",
    "BEHAVIOR_EXCEPTIONS",
    "BEHAVIOR_SIDE_EFFECTS",
    "BEHAVIOR_DEPENDENCIES",
    "BEHAVIOR_INVARIANTS",
    "CONF_HIGH",
    "CONF_LOW",
    "sha256_hex",
    "fingerprint_bytes",
    "fingerprint_source",
    "workspace_id_for",
    "file_artifact_id",
    "symbol_artifact_id",
    "behavior_node_id",
    "correspondence_id",
    "projection_id",
    "MIGRATIONS",
    "migrate_store",
    "baseline_fingerprint",
    "dumps",
    "build_store",
    "sync_twin",
    "find_projection",
    "projection_bundle",
    "anchor_for",
]
