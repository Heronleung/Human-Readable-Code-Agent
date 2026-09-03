"""Code Map Procedural Language Standard 0.1 (Qt-free domain).

This module is the deterministic, dependency-free *domain* for the Code Map: a
source-grounded procedural document that explains what a supported Python
module, class, function or method does, in ordered, readable human language,
while preserving typed structure, source correspondence, provenance,
confidence and editability.

It is deliberately pure in the same sense as :mod:`hrca.twin`:

* it performs **no filesystem access** (the caller supplies the source text);
* it performs **no model, provider, network, credential or telemetry** call and
  emits no model-generated explanation — every block and sentence is assembled
  deterministically from the AST of one module only;
* it is **Qt-free** and imports only the standard library plus
  :mod:`hrca.scanner` (for identifier/source-range conventions) and
  :mod:`hrca.twin` (for fingerprints), so the desktop client can never import
  it (enforced by :mod:`tests.test_architecture`).

The unit of output is a **block**. Every block carries a stable ``block_id``
(independent of wording), a ``block_type``, a deterministic ``order``, a typed
``payload``, a deterministic ``display_text``, one or more ``source_anchors``
where grounded, a ``provenance``, a ``confidence`` (with a reason when low), an
``editability``, a ``state``, and the language version ``0.1``.

The 14 block types are: ``entity``, ``purpose``, ``input``, ``step``,
``decision``, ``loop``, ``call``, ``exception``, ``return``, ``side_effect``,
``dependency``, ``invariant``, ``limitation`` and ``note`` (draft-only).
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional, Tuple

from . import scanner, twin

CODEMAP_LANGUAGE_VERSION = "0.1"
GENERATOR = "hrca-codemap"

# -- block types ---------------------------------------------------------

BT_ENTITY = "entity"
BT_PURPOSE = "purpose"
BT_INPUT = "input"
BT_STEP = "step"
BT_DECISION = "decision"
BT_LOOP = "loop"
BT_CALL = "call"
BT_EXCEPTION = "exception"
BT_RETURN = "return"
BT_SIDE_EFFECT = "side_effect"
BT_DEPENDENCY = "dependency"
BT_INVARIANT = "invariant"
BT_LIMITATION = "limitation"
BT_NOTE = "note"

BLOCK_TYPES = frozenset(
    {
        BT_ENTITY,
        BT_PURPOSE,
        BT_INPUT,
        BT_STEP,
        BT_DECISION,
        BT_LOOP,
        BT_CALL,
        BT_EXCEPTION,
        BT_RETURN,
        BT_SIDE_EFFECT,
        BT_DEPENDENCY,
        BT_INVARIANT,
        BT_LIMITATION,
        BT_NOTE,
    }
)

# -- provenance / confidence / state / editability -----------------------

PROVENANCE_VERIFIED = twin.PROVENANCE_VERIFIED
PROVENANCE_SOURCE_AUTHORED = "source_authored"
PROVENANCE_USER_AUTHORED = twin.PROVENANCE_USER_AUTHORED
PROVENANCE_INFERRED = twin.PROVENANCE_INFERRED
PROVENANCE_UNRESOLVED = twin.PROVENANCE_UNRESOLVED

CONF_HIGH = twin.CONF_HIGH
CONF_LOW = twin.CONF_LOW

STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_CONFLICT = "conflict"
STATE_UNSUPPORTED = "unsupported"
STATE_BLOCKED = "blocked"

EDITABILITY_READ_ONLY = "read_only"
EDITABILITY_REPLACE_DESCRIPTION = "replace_description"
EDITABILITY_REPLACE_CONDITION_INTENT = "replace_condition_intent"
EDITABILITY_DRAFT_ONLY = "draft_only"

# Block types that appear in the "Procedure" flow (not deferred to a later
# section). ``note`` is draft-only but renders here too.
_PROCEDURE_TYPES = frozenset(
    {BT_STEP, BT_DECISION, BT_LOOP, BT_CALL, BT_EXCEPTION, BT_NOTE}
)

# Fixed reasons attached to limitation blocks, keyed by unsupported kind.
_UNSUPPORTED_REASONS = {
    "lambda": "lambda expressions are not modeled",
    "comprehension": "comprehensions are not modeled",
    "generator": "generator expressions are not modeled",
    "dynamic import": "dynamic imports are resolved at runtime and not modeled",
    "reflection": "reflection is resolved at runtime and not modeled",
    "decorator": "decorator semantics are recorded but not modeled",
    "metaclass": "metaclasses are not modeled",
    "pattern matching": "pattern matching is not modeled",
}

# Augmented-assignment operator -> honest rendering verb.
_AUG_WORD = {
    ast.Add: "incremented",
    ast.Sub: "decremented",
    ast.Mult: "multiplied",
    ast.Div: "divided",
    ast.FloorDiv: "floored by",
    ast.Mod: "moduloed",
    ast.Pow: "raised to the power of",
}

# Functions whose call is evidence of runtime reflection (never modeled).
_REFLECTION_FUNCS = frozenset(
    {"eval", "exec", "getattr", "setattr", "hasattr", "delattr"}
)


def dumps(obj: Any) -> str:
    """Serialize ``obj`` to a single-line, deterministic, ASCII-safe JSON string."""
    import json

    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def block_id_for(locator: str, block_type: str, ordinal: int) -> str:
    """Return a stable, wording-independent block identifier.

    The id is structural — ``locator`` + ``block_type`` + ``ordinal`` — never
    the rendered sentence, so a formatting/wording change never changes an id.
    """
    return f"codemap:{locator}:{block_type}:{ordinal}"


def fingerprint_block(block: Dict[str, Any]) -> str:
    """Return the SHA-256 fingerprint of a block's *source facts*.

    Only the source-grounded fields — ``block_type``, ``payload`` and
    ``source_anchors`` — are fingerprinted, so a wording-only change to the
    renderer never changes a block's identity/fingerprint (SCOPE I).
    """
    canon = dumps(
        {
            "block_type": block.get("block_type"),
            "payload": block.get("payload"),
            "source_anchors": block.get("source_anchors"),
        }
    )
    return twin.sha256_hex(canon.encode("utf-8"))


# -- parameter / signature rendering -------------------------------------


def _param_text(param: Dict[str, Any]) -> str:
    name = param.get("name", "?")
    prefix = {"vararg": "*", "kwarg": "**"}.get(param.get("param_kind"), "")
    text = prefix + name
    annotation = param.get("annotation")
    if annotation:
        text += f": {annotation}"
    default = param.get("default")
    if default is not None:
        text += f" = {default}"
    return text


def _kind_word(kind: str) -> str:
    return {
        "module": "Module",
        "class": "Class",
        "function": "Function",
        "method": "Method",
        "async_function": "Async function",
    }.get(kind, "Entity")


def _entity_signature(
    kind: str, name: str, params: List[Dict[str, Any]], ret: Optional[str]
) -> str:
    if kind == "module":
        return f"Module {name}"
    if kind == "class":
        return f"Class {name}"
    parts: List[str] = []
    seen_kwonly = False
    for param in params:
        if param.get("param_kind") == "keyword_only" and not seen_kwonly:
            parts.append("*")
            seen_kwonly = True
        parts.append(_param_text(param))
    sig = f"{name}({', '.join(parts)})"
    if ret:
        sig += f" -> {ret}"
    return f"{_kind_word(kind)} {sig}"


# -- the builder ---------------------------------------------------------


class _Builder:
    """Walks one parsed module and produces the ordered, deterministic blocks."""

    def __init__(
        self, rel_path: str, module_name: str, source: str, baseline_revision: Optional[str]
    ) -> None:
        self.rel_path = rel_path
        self.module_name = module_name
        self.source = source
        self.baseline_revision = baseline_revision
        self.blocks: List[Dict[str, Any]] = []
        self._ordinal = 0
        self._order: Dict[Optional[str], int] = {}
        self._locator_stack: List[str] = [module_name]
        self._kind_stack: List[str] = ["module"]
        self._entity_id_stack: List[str] = []

    # -- helpers --------------------------------------------------------

    def _locator(self) -> str:
        return self._locator_stack[-1]

    def _kind(self) -> str:
        return self._kind_stack[-1]

    def _qname(self, name: str) -> str:
        return f"{self._locator()}.{name}"

    def _entity_parent_id(self) -> Optional[str]:
        return self._entity_id_stack[-1] if self._entity_id_stack else None

    def _module_anchor(self, block_id: str) -> Dict[str, Any]:
        lines = self.source.splitlines() or [""]
        return {
            "file": self.rel_path,
            "source_id": block_id,
            "lineno": 1,
            "col_offset": 0,
            "end_lineno": len(lines),
            "end_col_offset": len(lines[-1]) if lines else 0,
        }

    def _anchor(self, node: Optional[ast.AST], block_id: str) -> Optional[Dict[str, Any]]:
        rng = scanner._src_range(node)
        if rng is None:
            return None
        return {
            "file": self.rel_path,
            "source_id": block_id,
            "lineno": rng["lineno"],
            "col_offset": rng["col_offset"],
            "end_lineno": rng["end_lineno"],
            "end_col_offset": rng["end_col_offset"],
        }

    def _emit(
        self,
        locator: str,
        parent_id: Optional[str],
        block_type: str,
        subject: str,
        payload: Dict[str, Any],
        display_text: str,
        node: Optional[ast.AST],
        provenance: str,
        confidence: str,
        state: str,
        editability: str,
        reason: Optional[str] = None,
        module_range: bool = False,
    ) -> str:
        block_id = block_id_for(locator, block_type, self._ordinal)
        if module_range:
            anchors = [self._module_anchor(block_id)]
        elif node is not None:
            anchor = self._anchor(node, block_id)
            anchors = [anchor] if anchor is not None else []
        else:
            anchors = []
        block = {
            "block_id": block_id,
            "block_type": block_type,
            "parent_id": parent_id,
            "order": self._order.get(parent_id, 0),
            "subject": subject,
            "payload": payload,
            "display_text": display_text,
            "source_anchors": anchors,
            "baseline_revision": self.baseline_revision,
            "source_fingerprint": "",
            "provenance": provenance,
            "confidence": confidence,
            "confidence_reason": reason,
            "editability": editability,
            "state": state,
            "language_version": CODEMAP_LANGUAGE_VERSION,
        }
        self._ordinal += 1
        self._order[parent_id] = block["order"] + 1
        self.blocks.append(block)
        return block_id

    # -- unsupported-construct detection --------------------------------

    @staticmethod
    def _is_dynamic_import(node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Attribute):
            return (
                isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
                and func.attr == "import_module"
            )
        if isinstance(func, ast.Name):
            return func.id == "__import__"
        return False

    def _scan_unsupported(self, node: ast.AST, out: List[Tuple[str, str]]) -> None:
        if isinstance(node, ast.Lambda):
            out.append(("lambda", "lambda"))
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            out.append(("comprehension", type(node).__name__))
        elif isinstance(node, ast.GeneratorExp):
            out.append(("generator", "generator expression"))
        elif isinstance(node, ast.Call) and self._is_dynamic_import(node):
            out.append(("dynamic import", scanner._render(node)))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _REFLECTION_FUNCS
        ):
            out.append(("reflection", node.func.id))
        for child in ast.iter_child_nodes(node):
            self._scan_unsupported(child, out)

    def _note_unsupported(self, node: Optional[ast.AST], container_id: str) -> None:
        if node is None:
            return
        found: List[Tuple[str, str]] = []
        self._scan_unsupported(node, found)
        seen = set()
        for kind, evidence in found:
            key = (kind, evidence)
            if key in seen:
                continue
            seen.add(key)
            reason = _UNSUPPORTED_REASONS.get(kind, "not modeled")
            self._emit(
                self._locator(),
                container_id,
                BT_LIMITATION,
                kind,
                {"kind": kind, "evidence": evidence, "reason": reason},
                f"{kind} is not modeled and is reported as unresolved.",
                node,
                PROVENANCE_UNRESOLVED,
                CONF_LOW,
                STATE_UNSUPPORTED,
                EDITABILITY_READ_ONLY,
                reason=reason,
            )

    # -- statement dispatch ---------------------------------------------

    def _visit_body(
        self, statements: List[ast.stmt], container_id: str, guard_condition: Optional[str] = None
    ) -> List[str]:
        ids: List[str] = []
        for stmt in statements:
            bid = self._visit_statement(stmt, container_id, guard_condition)
            if bid:
                ids.append(bid)
        return ids

    def _visit_statement(
        self, stmt: ast.stmt, container_id: str, guard_condition: Optional[str] = None
    ) -> Optional[str]:
        if isinstance(stmt, ast.ClassDef):
            return self._entity(stmt, "class", False)
        if isinstance(stmt, ast.FunctionDef):
            return self._entity(stmt, "function", False)
        if isinstance(stmt, ast.AsyncFunctionDef):
            return self._entity(stmt, "async_function", True)
        if isinstance(stmt, ast.Assign):
            return self._assign(stmt, container_id)
        if isinstance(stmt, ast.AnnAssign):
            return self._annassign(stmt, container_id)
        if isinstance(stmt, ast.AugAssign):
            return self._augassign(stmt, container_id)
        if isinstance(stmt, ast.Expr):
            return self._expr(stmt, container_id)
        if isinstance(stmt, ast.If):
            return self._if(stmt, container_id)
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            return self._loop(stmt, container_id)
        if isinstance(stmt, ast.Try):
            return self._try(stmt, container_id)
        if isinstance(stmt, ast.Raise):
            return self._raise(stmt, container_id, guard_condition)
        if isinstance(stmt, ast.Return):
            return self._return(stmt, container_id)
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            return self._import(stmt, container_id)
        if isinstance(stmt, ast.Assert):
            return self._assert(stmt, container_id)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            return self._with(stmt, container_id)
        if isinstance(stmt, (ast.Pass, ast.Break, ast.Continue)):
            return self._simple(stmt, container_id)
        if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
            return self._limitation(
                "pattern matching", "match statement", "pattern matching is not modeled",
                container_id, stmt,
            )
        # global / nonlocal / delete / other unmodeled statements: no block.
        return None

    # -- definitions ----------------------------------------------------

    def _entity(self, node: Any, kind: str, is_async: bool) -> str:
        name = node.name
        locator = self._qname(name)
        is_method = self._kind() == "class"
        effective_kind = "method" if is_method else kind
        params = self._params(node.args) if effective_kind in (
            "function", "method", "async_function"
        ) else []
        ret = (
            scanner._render(node.returns)
            if effective_kind in ("function", "method", "async_function") else None
        )
        signature = _entity_signature(effective_kind, name, params, ret)
        decorators = [scanner._render(d) for d in node.decorator_list] or None
        bases = [scanner._render(b) for b in node.bases] if kind == "class" else None
        payload = {
            "name": name,
            "kind": effective_kind,
            "locator": locator,
            "decorators": decorators,
            "bases": bases,
            "return_annotation": ret,
            "async": is_async or None,
            "is_method": is_method or None,
        }
        entity_id = self._emit(
            locator,
            self._entity_parent_id(),
            BT_ENTITY,
            signature,
            payload,
            signature,
            node,
            PROVENANCE_VERIFIED,
            CONF_HIGH,
            STATE_CURRENT,
            EDITABILITY_READ_ONLY,
        )

        self._locator_stack.append(locator)
        self._kind_stack.append(effective_kind)
        self._entity_id_stack.append(entity_id)

        doc = ast.get_docstring(node, clean=True)
        purpose_text = doc if doc else None
        purpose_display = doc or f"{_kind_word(effective_kind)} {name} has no documented purpose."
        self._emit(
            locator,
            entity_id,
            BT_PURPOSE,
            "Purpose",
            {"text": purpose_text},
            purpose_display,
            node,
            PROVENANCE_SOURCE_AUTHORED if doc else PROVENANCE_VERIFIED,
            CONF_HIGH,
            STATE_CURRENT,
            EDITABILITY_REPLACE_DESCRIPTION,
        )

        if params:
            param_text = ", ".join(_param_text(p) for p in params)
            self._emit(
                locator,
                entity_id,
                BT_INPUT,
                "Inputs",
                {"parameters": params},
                param_text,
                node,
                PROVENANCE_VERIFIED,
                CONF_HIGH,
                STATE_CURRENT,
                EDITABILITY_READ_ONLY,
            )

        if node.decorator_list:
            for dec in node.decorator_list:
                self._limitation(
                    "decorator", scanner._render(dec),
                    _UNSUPPORTED_REASONS["decorator"], entity_id, dec,
                )

        start = 0
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            start = 1
        for stmt in node.body[start:]:
            self._visit_statement(stmt, entity_id)

        self._entity_id_stack.pop()
        self._kind_stack.pop()
        self._locator_stack.pop()
        return entity_id

    def _params(self, args: ast.arguments) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        def one(arg: ast.arg, pkind: str, default: Optional[str]) -> Dict[str, Any]:
            return {
                "name": arg.arg,
                "annotation": scanner._render(arg.annotation),
                "default": default,
                "param_kind": pkind,
            }

        posonly = list(args.posonlyargs)
        pos = list(args.args)
        all_pos = posonly + pos
        defaults = list(args.defaults)
        pos_defaults = [None] * (len(all_pos) - len(defaults)) + [
            scanner._render(d) if d is not None else None for d in defaults
        ]
        for i, arg in enumerate(all_pos):
            pkind = "positional_only" if i < len(posonly) else "positional"
            out.append(one(arg, pkind, pos_defaults[i]))
        if args.vararg:
            out.append(one(args.vararg, "vararg", None))
        kwonly = list(args.kwonlyargs)
        kw_defaults = list(args.kw_defaults) if args.kw_defaults else []
        for i, arg in enumerate(kwonly):
            default = (
                scanner._render(kw_defaults[i])
                if i < len(kw_defaults) and kw_defaults[i] is not None
                else None
            )
            out.append(one(arg, "keyword_only", default))
        if args.kwarg:
            out.append(one(args.kwarg, "kwarg", None))
        return out

    def _limitation(
        self, kind: str, evidence: str, reason: str, container_id: str, node: ast.AST
    ) -> str:
        return self._emit(
            self._locator(),
            container_id,
            BT_LIMITATION,
            kind,
            {"kind": kind, "evidence": evidence, "reason": reason},
            f"{kind} is not modeled and is reported as unresolved.",
            node,
            PROVENANCE_UNRESOLVED,
            CONF_LOW,
            STATE_UNSUPPORTED,
            EDITABILITY_READ_ONLY,
            reason=reason,
        )

    # -- statements ------------------------------------------------------

    def _assign(self, node: ast.Assign, container_id: str) -> Optional[str]:
        locator = self._locator()
        targets = [scanner._render(t) for t in node.targets]
        self._note_unsupported(node.value, container_id)
        if any(isinstance(t, (ast.Attribute, ast.Subscript)) for t in node.targets):
            desc = "Mutates " + ", ".join(targets)
            evidence = [f"{t} = {scanner._render(node.value)}" for t in targets]
            self._emit(
                locator, container_id, BT_SIDE_EFFECT, "mutation",
                {"description": desc, "evidence": evidence},
                f"{desc}.", node, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT,
                EDITABILITY_READ_ONLY,
            )
            return None
        expr = scanner._render(node.value)
        target = ", ".join(targets)
        return self._emit(
            locator, container_id, BT_STEP, target,
            {"operation": "assign", "target": target, "expression": expr},
            f"{target} is assigned {expr}", node, PROVENANCE_VERIFIED, CONF_HIGH,
            STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    def _annassign(self, node: ast.AnnAssign, container_id: str) -> str:
        locator = self._locator()
        target = scanner._render(node.target)
        annotation = scanner._render(node.annotation)
        self._note_unsupported(node.value, container_id)
        if node.value is not None:
            value = scanner._render(node.value)
            return self._emit(
                locator, container_id, BT_STEP, target,
                {"operation": "annassign", "target": target, "expression": value},
                f"{target} is assigned {value}", node, PROVENANCE_VERIFIED, CONF_HIGH,
                STATE_CURRENT, EDITABILITY_READ_ONLY,
            )
        return self._emit(
            locator, container_id, BT_STEP, target,
            {"operation": "annassign", "target": target, "expression": annotation},
            f"{target} is declared as {annotation}", node, PROVENANCE_VERIFIED,
            CONF_HIGH, STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    def _augassign(self, node: ast.AugAssign, container_id: str) -> str:
        locator = self._locator()
        target = scanner._render(node.target)
        expr = scanner._render(node.value)
        self._note_unsupported(node.value, container_id)
        if isinstance(node.target, (ast.Attribute, ast.Subscript)):
            desc = f"Mutates {target}"
            self._emit(
                locator, container_id, BT_SIDE_EFFECT, "mutation",
                {"description": desc, "evidence": [f"{target} = {scanner._render(node)}"]},
                f"{desc}.", node, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT,
                EDITABILITY_READ_ONLY,
            )
            return target
        verb = _AUG_WORD.get(type(node.op), "updated")
        return self._emit(
            locator, container_id, BT_STEP, target,
            {"operation": "augassign", "target": target, "expression": expr},
            f"{target} is {verb} by {expr}", node, PROVENANCE_VERIFIED, CONF_HIGH,
            STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    def _expr(self, node: ast.Expr, container_id: str) -> Optional[str]:
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return None  # a stray string literal, not a call or step
        if isinstance(value, ast.Call):
            self._note_unsupported(value, container_id)
            return self._call(value, container_id)
        self._note_unsupported(value, container_id)
        rendered = scanner._render(value)
        return self._emit(
            self._locator(), container_id, BT_STEP, "expression",
            {"operation": "expr", "target": None, "expression": rendered},
            f"{rendered} is evaluated", node, PROVENANCE_VERIFIED, CONF_HIGH,
            STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    def _call(self, node: ast.Call, container_id: str) -> str:
        target = scanner._render(node.func)
        args = [scanner._render(a) for a in node.args] + [
            f"{kw.arg}={scanner._render(kw.value)}" for kw in node.keywords
        ]
        return self._emit(
            self._locator(), container_id, BT_CALL, target,
            {"target": target, "args": args},
            f"Calls {target}.", node, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT,
            EDITABILITY_READ_ONLY,
        )

    def _if(self, node: ast.If, container_id: str) -> str:
        locator = self._locator()
        test = scanner._render(node.test)
        self._note_unsupported(node.test, container_id)
        display = f"If {test} is true, the following runs:"
        payload: Dict[str, Any] = {"condition": test, "branches": []}
        decision_id = self._emit(
            locator, container_id, BT_DECISION, test, payload, display, node,
            PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT,
            EDITABILITY_REPLACE_CONDITION_INTENT,
        )

        branches: List[Dict[str, Any]] = []
        current = node
        guard_first = (
            len(current.body) == 1
            and isinstance(current.body[0], ast.Raise)
            and not current.orelse
        )
        body_ids = self._visit_body(
            current.body, decision_id, guard_condition=test if guard_first else None
        )
        branches.append({"label": "if", "condition": None, "block_ids": body_ids})
        else_part = current.orelse
        while else_part:
            if len(else_part) == 1 and isinstance(else_part[0], ast.If):
                elif_node = else_part[0]
                elif_test = scanner._render(elif_node.test)
                self._note_unsupported(elif_node.test, decision_id)
                elif_guard = (
                    len(elif_node.body) == 1
                    and isinstance(elif_node.body[0], ast.Raise)
                    and not elif_node.orelse
                )
                elif_ids = self._visit_body(
                    elif_node.body, decision_id,
                    guard_condition=elif_test if elif_guard else None,
                )
                branches.append(
                    {"label": "elif", "condition": elif_test, "block_ids": elif_ids}
                )
                else_part = elif_node.orelse
            else:
                else_ids = self._visit_body(else_part, decision_id)
                branches.append({"label": "else", "condition": None, "block_ids": else_ids})
                else_part = []
        payload["branches"] = branches
        return decision_id

    def _loop(self, node: ast.AST, container_id: str) -> str:
        locator = self._locator()
        if isinstance(node, ast.While):
            condition = scanner._render(node.test)
            self._note_unsupported(node.test, container_id)
            display = f"While {condition}:"
            payload: Dict[str, Any] = {
                "kind": "while", "target": None, "iterator": None,
                "condition": condition, "body_block_ids": [], "orelse_block_ids": [],
            }
            subject = condition
        else:
            is_async = isinstance(node, ast.AsyncFor)
            target = scanner._render(node.target)
            iterator = scanner._render(node.iter)
            self._note_unsupported(node.iter, container_id)
            display = f"For each {target} in {iterator}:"
            if is_async:
                display += " (asynchronous)"
            payload = {
                "kind": "async_for" if is_async else "for", "target": target,
                "iterator": iterator, "condition": None,
                "body_block_ids": [], "orelse_block_ids": [],
            }
            subject = target
        loop_id = self._emit(
            locator, container_id, BT_LOOP, subject, payload, display, node,
            PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT, EDITABILITY_READ_ONLY,
        )
        payload["body_block_ids"] = self._visit_body(node.body, loop_id)
        payload["orelse_block_ids"] = self._visit_body(node.orelse, loop_id)
        return loop_id

    def _try(self, node: ast.Try, container_id: str) -> str:
        locator = self._locator()
        payload: Dict[str, Any] = {
            "body_ids": [], "handlers": [], "else_ids": [], "finally_ids": [], "raised": [],
        }
        try_id = self._emit(
            locator, container_id, BT_EXCEPTION, "try", payload, "Handles exceptions.",
            node, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT, EDITABILITY_READ_ONLY,
        )
        payload["body_ids"] = self._visit_body(node.body, try_id)
        for handler in node.handlers:
            handler_type = scanner._render(handler.type) if handler.type is not None else None
            handler_ids = self._visit_body(handler.body, try_id)
            payload["handlers"].append({"type": handler_type, "block_ids": handler_ids})
        if node.orelse:
            payload["else_ids"] = self._visit_body(node.orelse, try_id)
        if node.finalbody:
            payload["finally_ids"] = self._visit_body(node.finalbody, try_id)
        return try_id

    def _raise(
        self, node: ast.Raise, container_id: str, guard_condition: Optional[str] = None
    ) -> str:
        locator = self._locator()
        self._note_unsupported(node.exc, container_id)
        exc = scanner._render(node.exc) if node.exc is not None else None
        type_str = exc if exc else "an exception"
        text = f"Raises {type_str}"
        if guard_condition:
            text += f" when {guard_condition}"
        text += "."
        return self._emit(
            locator, container_id, BT_EXCEPTION, type_str,
            {
                "body_ids": [], "handlers": [], "else_ids": [], "finally_ids": [],
                "raised": [{"type": exc, "condition": guard_condition}],
            },
            text, node, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT,
            EDITABILITY_READ_ONLY,
        )

    def _return(self, node: ast.Return, container_id: str) -> str:
        locator = self._locator()
        self._note_unsupported(node.value, container_id)
        value = scanner._render(node.value) if node.value is not None else None
        text = f"Returns {value}." if value is not None else "Returns nothing."
        return self._emit(
            locator, container_id, BT_RETURN, value or "None",
            {"value_expr": value}, text, node, PROVENANCE_VERIFIED, CONF_HIGH,
            STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    def _import(self, node: ast.AST, container_id: str) -> str:
        locator = self._locator()
        emitted: Optional[str] = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                emitted = self._emit(
                    locator, container_id, BT_DEPENDENCY, alias.name,
                    {"target": alias.name, "alias": alias.asname, "status": "resolved"},
                    f"Imports {alias.name}.", node, PROVENANCE_VERIFIED, CONF_HIGH,
                    STATE_CURRENT, EDITABILITY_READ_ONLY,
                )
        else:
            module = node.module
            for alias in node.names:
                if alias.name == "*":
                    target = f"{module}.*" if module else "*"
                else:
                    target = f"{module}.{alias.name}" if module else alias.name
                emitted = self._emit(
                    locator, container_id, BT_DEPENDENCY, target,
                    {"target": target, "alias": alias.asname, "status": "resolved"},
                    f"Imports {target}.", node, PROVENANCE_VERIFIED, CONF_HIGH,
                    STATE_CURRENT, EDITABILITY_READ_ONLY,
                )
        return emitted

    def _assert(self, node: ast.Assert, container_id: str) -> str:
        locator = self._locator()
        self._note_unsupported(node.test, container_id)
        assertion = scanner._render(node.test)
        return self._emit(
            locator, container_id, BT_INVARIANT, assertion,
            {"assertion": assertion, "documented": []},
            f"Asserts {assertion}.", node, PROVENANCE_VERIFIED, CONF_HIGH,
            STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    def _with(self, node: ast.AST, container_id: str) -> str:
        locator = self._locator()
        is_async = isinstance(node, ast.AsyncWith)
        contexts = [scanner._render(item.context_expr) for item in node.items]
        joined = ", ".join(contexts)
        for item in node.items:
            self._note_unsupported(item.context_expr, container_id)
        display = f"With {joined}:"
        if is_async:
            display += " (asynchronous)"
        payload: Dict[str, Any] = {"description": display, "evidence": contexts, "body_block_ids": []}
        with_id = self._emit(
            locator, container_id, BT_SIDE_EFFECT, joined, payload, display, node,
            PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT, EDITABILITY_READ_ONLY,
        )
        payload["body_block_ids"] = self._visit_body(node.body, with_id)
        return with_id

    def _simple(self, node: ast.AST, container_id: str) -> str:
        locator = self._locator()
        if isinstance(node, ast.Pass):
            return self._emit(
                locator, container_id, BT_STEP, "pass",
                {"operation": "pass", "target": None, "expression": None},
                "Passes.", node, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT,
                EDITABILITY_READ_ONLY,
            )
        if isinstance(node, ast.Break):
            return self._emit(
                locator, container_id, BT_STEP, "break",
                {"operation": "break", "target": None, "expression": None},
                "Breaks out of the loop.", node, PROVENANCE_VERIFIED, CONF_HIGH,
                STATE_CURRENT, EDITABILITY_READ_ONLY,
            )
        return self._emit(
            locator, container_id, BT_STEP, "continue",
            {"operation": "continue", "target": None, "expression": None},
            "Continues to the next iteration.", node, PROVENANCE_VERIFIED, CONF_HIGH,
            STATE_CURRENT, EDITABILITY_READ_ONLY,
        )

    # -- entry point ----------------------------------------------------

    def build(self, tree: ast.Module) -> List[Dict[str, Any]]:
        module_payload = {
            "name": self.module_name, "kind": "module", "locator": self.module_name,
            "decorators": None, "bases": None, "return_annotation": None,
            "async": None, "is_method": None,
        }
        signature = f"Module {self.module_name}"
        module_id = self._emit(
            self.module_name, None, BT_ENTITY, signature, module_payload, signature,
            None, PROVENANCE_VERIFIED, CONF_HIGH, STATE_CURRENT, EDITABILITY_READ_ONLY,
            module_range=True,
        )
        self._entity_id_stack.append(module_id)
        self._locator_stack.append(self.module_name)
        self._kind_stack.append("module")

        doc = ast.get_docstring(tree, clean=True)
        purpose_text = doc if doc else None
        purpose_display = doc or f"Module {self.module_name} has no documented purpose."
        self._emit(
            self.module_name, module_id, BT_PURPOSE, "Purpose", {"text": purpose_text},
            purpose_display, None,
            PROVENANCE_SOURCE_AUTHORED if doc else PROVENANCE_VERIFIED,
            CONF_HIGH, STATE_CURRENT, EDITABILITY_REPLACE_DESCRIPTION,
        )

        start = 0
        if (
            tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)
        ):
            start = 1
        for stmt in tree.body[start:]:
            self._visit_statement(stmt, module_id)

        self._entity_id_stack.pop()
        self._kind_stack.pop()
        self._locator_stack.pop()
        return self.blocks


# -- public API ----------------------------------------------------------


def build_codemap(
    source: str,
    rel_path: str,
    module_name: str,
    baseline_revision: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build the ordered procedural blocks for one module's ``source``.

    Returns a flat list of blocks in deterministic source order, each carrying
    a stable ``block_id`` and a ``source_fingerprint`` computed from its source
    facts (not its rendered wording).
    """
    tree = ast.parse(source, filename=rel_path)
    blocks = _Builder(rel_path, module_name, source, baseline_revision).build(tree)
    for block in blocks:
        block["source_fingerprint"] = fingerprint_block(block)
    return blocks


def _entity_locator(block: Dict[str, Any]) -> str:
    return (block.get("payload") or {}).get("locator", "")


def entity_list(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a compact, source-ordered descriptor for every entity block."""
    out: List[Dict[str, Any]] = []
    for block in blocks:
        if block.get("block_type") != BT_ENTITY:
            continue
        payload = block.get("payload") or {}
        out.append(
            {
                "block_id": block.get("block_id"),
                "locator": payload.get("locator"),
                "kind": payload.get("kind"),
                "name": payload.get("name"),
                "subject": block.get("display_text"),
                "parent_id": block.get("parent_id"),
                "order": block.get("order"),
            }
        )
    return out


def blocks_for_entity(blocks: List[Dict[str, Any]], entity_locator: str) -> List[Dict[str, Any]]:
    """Return the entity block plus its descendants (excluding nested entities'
    own subtrees) in deterministic source order, or ``[]`` when unknown."""
    entity = next(
        (
            b
            for b in blocks
            if b.get("block_type") == BT_ENTITY
            and _entity_locator(b) == entity_locator
        ),
        None,
    )
    if entity is None:
        return []
    index = {b.get("block_id"): b for b in blocks}

    seen = set()

    def collect(block_id: str) -> None:
        block = index.get(block_id)
        if block is None or block_id in seen:
            return
        seen.add(block_id)
        if block.get("block_type") == BT_ENTITY and block_id != entity.get("block_id"):
            return  # do not descend into a nested entity's own subtree
        for child in sorted(
            (b for b in blocks if b.get("parent_id") == block_id),
            key=lambda b: (b.get("order", 0), b.get("block_id", "")),
        ):
            collect(child.get("block_id"))

    collect(entity.get("block_id"))
    return [b for b in blocks if b.get("block_id") in seen]


def call_targets(blocks: List[Dict[str, Any]]) -> List[str]:
    """Return the static call targets evidenced by ``call`` blocks."""
    return [b["payload"]["target"] for b in blocks if b.get("block_type") == BT_CALL]


def dependency_targets(blocks: List[Dict[str, Any]]) -> List[str]:
    """Return the static dependency targets evidenced by ``dependency`` blocks."""
    return [b["payload"]["target"] for b in blocks if b.get("block_type") == BT_DEPENDENCY]


# -- renderer ------------------------------------------------------------


def _children_of(
    blocks: List[Dict[str, Any]], parent_id: Optional[str]
) -> List[Dict[str, Any]]:
    return sorted(
        (b for b in blocks if b.get("parent_id") == parent_id),
        key=lambda b: (b.get("order", 0), b.get("block_id", "")),
    )


def _render_block(
    block: Dict[str, Any],
    blocks: List[Dict[str, Any]],
    indent: str,
) -> List[str]:
    """Render one procedure-flow block (and its nested bodies) as indented lines."""
    block_type = block.get("block_type")
    lines: List[str] = [indent + str(block.get("display_text", ""))]
    deeper = indent + "  "
    payload = block.get("payload") or {}
    by_id = {b.get("block_id"): b for b in blocks}

    if block_type == BT_DECISION:
        for branch in payload.get("branches", []):
            label = branch.get("label")
            if label == "elif":
                lines.append(indent + f"Otherwise, if {branch.get('condition')}:")
            elif label == "else":
                lines.append(indent + "Otherwise:")
            for bid in branch.get("block_ids", []):
                child = by_id.get(bid)
                if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                    lines.extend(_render_block(child, blocks, deeper))
    elif block_type == BT_LOOP:
        for bid in payload.get("body_block_ids", []):
            child = by_id.get(bid)
            if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                lines.extend(_render_block(child, blocks, deeper))
        if payload.get("orelse_block_ids"):
            lines.append(indent + "Otherwise (the loop completed without break):")
            for bid in payload.get("orelse_block_ids", []):
                child = by_id.get(bid)
                if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                    lines.extend(_render_block(child, blocks, deeper))
    elif block_type == BT_EXCEPTION:
        # A ``raise`` renders as a single line (already the display_text); a
        # ``try`` renders its protected body then handlers/else/finally.
        if payload.get("body_ids") or payload.get("handlers") or payload.get("else_ids") \
                or payload.get("finally_ids"):
            for bid in payload.get("body_ids", []):
                child = by_id.get(bid)
                if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                    lines.extend(_render_block(child, blocks, deeper))
            for handler in payload.get("handlers", []):
                htype = handler.get("type") or "any exception"
                lines.append(indent + f"Handles {htype}:")
                for bid in handler.get("block_ids", []):
                    child = by_id.get(bid)
                    if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                        lines.extend(_render_block(child, blocks, deeper))
            if payload.get("else_ids"):
                lines.append(indent + "If no exception occurred:")
                for bid in payload.get("else_ids", []):
                    child = by_id.get(bid)
                    if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                        lines.extend(_render_block(child, blocks, deeper))
            if payload.get("finally_ids"):
                lines.append(indent + "Always after the try:")
                for bid in payload.get("finally_ids", []):
                    child = by_id.get(bid)
                    if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                        lines.extend(_render_block(child, blocks, deeper))
    elif block_type == BT_SIDE_EFFECT:
        for bid in payload.get("body_block_ids", []):
            child = by_id.get(bid)
            if child is not None and child.get("block_type") in _PROCEDURE_TYPES:
                lines.extend(_render_block(child, blocks, deeper))
    return lines


def _descendant_blocks(
    entity_id: str, blocks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Return all non-entity descendants of ``entity_id`` (stopping at nested entities)."""
    by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for b in blocks:
        by_parent.setdefault(b.get("parent_id"), []).append(b)
    out: List[Dict[str, Any]] = []
    stack = [entity_id]
    seen = set()
    while stack:
        bid = stack.pop()
        for child in sorted(
            by_parent.get(bid, []), key=lambda b: (b.get("order", 0), b.get("block_id", ""))
        ):
            if child.get("block_id") in seen:
                continue
            seen.add(child.get("block_id"))
            out.append(child)
            if child.get("block_type") != BT_ENTITY:
                stack.append(child.get("block_id"))
    out.sort(key=lambda b: (b.get("order", 0), b.get("block_id", "")))
    return out


def _render_entity(root: Dict[str, Any], blocks: List[Dict[str, Any]]) -> str:
    entity_id = root.get("block_id")
    lines: List[str] = [str(root.get("display_text", ""))]
    descendants = _descendant_blocks(entity_id, blocks)
    direct = [b for b in descendants if b.get("parent_id") == entity_id]

    def section(heading: str, items: List[Dict[str, Any]]) -> None:
        if not items:
            return
        lines.append("")
        lines.append(heading)
        for item in items:
            lines.append(f"  {item.get('display_text', '')}")

    section("Inputs:", [b for b in direct if b.get("block_type") == BT_INPUT])
    purposes = [b for b in direct if b.get("block_type") == BT_PURPOSE]
    if purposes:
        lines.append("")
        lines.append("Purpose:")
        for item in purposes:
            lines.append(f"  {item.get('display_text', '')}")

    procedure = [b for b in direct if b.get("block_type") in _PROCEDURE_TYPES]
    nested_entities = [b for b in direct if b.get("block_type") == BT_ENTITY]
    if procedure or nested_entities:
        lines.append("")
        lines.append("Procedure:")
        for item in procedure:
            lines.extend(_render_block(item, blocks, "  "))
        for item in nested_entities:
            lines.append(f"  declares {item.get('display_text', '')}")

    section("Return:", [b for b in descendants if b.get("block_type") == BT_RETURN])
    side = [b for b in descendants if b.get("block_type") == BT_SIDE_EFFECT]
    if side:
        lines.append("")
        lines.append("Side effects:")
        for item in side:
            lines.extend(_render_block(item, blocks, "  "))
    section("Dependencies:", [b for b in descendants if b.get("block_type") == BT_DEPENDENCY])
    section("Invariants:", [b for b in descendants if b.get("block_type") == BT_INVARIANT])
    section("Limitations:", [b for b in descendants if b.get("block_type") == BT_LIMITATION])
    return "\n".join(lines)


def render_blocks(blocks: List[Dict[str, Any]]) -> str:
    """Render ``blocks`` as the deterministic procedural document.

    Section order (SCOPE E): Entity -> Inputs -> Purpose -> Procedure -> Return
    -> Side Effects -> Dependencies -> Invariants -> Limitations.
    """
    if not blocks:
        return ""
    index = {b.get("block_id"): b for b in blocks}
    roots = [
        b
        for b in blocks
        if b.get("block_type") == BT_ENTITY
        and (b.get("parent_id") is None or b.get("parent_id") not in index)
    ]
    if not roots:
        roots = sorted(
            (b for b in blocks if b.get("block_type") == BT_ENTITY),
            key=lambda b: (b.get("order", 0), b.get("block_id", "")),
        )
    return "\n\n".join(_render_entity(root, blocks) for root in roots)


__all__ = [
    "CODEMAP_LANGUAGE_VERSION",
    "GENERATOR",
    "BLOCK_TYPES",
    "BT_ENTITY",
    "BT_PURPOSE",
    "BT_INPUT",
    "BT_STEP",
    "BT_DECISION",
    "BT_LOOP",
    "BT_CALL",
    "BT_EXCEPTION",
    "BT_RETURN",
    "BT_SIDE_EFFECT",
    "BT_DEPENDENCY",
    "BT_INVARIANT",
    "BT_LIMITATION",
    "BT_NOTE",
    "PROVENANCE_VERIFIED",
    "PROVENANCE_SOURCE_AUTHORED",
    "PROVENANCE_USER_AUTHORED",
    "PROVENANCE_INFERRED",
    "PROVENANCE_UNRESOLVED",
    "CONF_HIGH",
    "CONF_LOW",
    "STATE_CURRENT",
    "STATE_STALE",
    "STATE_CONFLICT",
    "STATE_UNSUPPORTED",
    "STATE_BLOCKED",
    "EDITABILITY_READ_ONLY",
    "EDITABILITY_REPLACE_DESCRIPTION",
    "EDITABILITY_REPLACE_CONDITION_INTENT",
    "EDITABILITY_DRAFT_ONLY",
    "dumps",
    "block_id_for",
    "fingerprint_block",
    "build_codemap",
    "entity_list",
    "blocks_for_entity",
    "call_targets",
    "dependency_targets",
    "render_blocks",
]
