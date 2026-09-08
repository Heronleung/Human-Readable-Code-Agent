"""Document-driven app-package contract and validator (P4.3).

The first executable foundation for Specification 2.0: a versioned, bounded
**app-package** manifest that describes a hand-written business-rules package
without ever carrying executable code. A package is *data only* — it declares
the approved input **form** fields, the approved **result** fields, a fixed
**runtime identity** and a named **handler** that is resolved from a code-owned
allowlist (never from package text).

This module is pure in the same sense as :mod:`hrca.proposal` and
:mod:`hrca.advisory`:

* no filesystem, network, credential, command, Git, repository-write, provider
  or runner access;
* Qt-free, stdlib-only (``re``, ``json``, ``decimal``);
* every field is validated to a bounded shape; a malformed, oversized,
  unsupported, path-escaping, script-like or runtime-mismatched package is
  rejected with a bounded reason *before* any runner is started.

The package and any model-like text are **untrusted input**. The trusted side is
only the code-owned handler allowlist and the runner binary.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

APP_PACKAGE_SCHEMA_VERSION = "1.0.0"

# Fixed runtime identity: a package must name exactly this, so a package built
# for a different runner/image is rejected rather than silently mis-executed.
RUNNER_IDENTITY = "hrca-runner:v1"

# Bounded limits (code-owned; never from package input).
MAX_PACKAGE_BYTES = 16 * 1024  # 16 KiB manifest
MAX_TITLE_CHARS = 120
MAX_FORM_FIELDS = 32
MAX_RESULT_FIELDS = 32
MAX_FIELD_NAME_CHARS = 64
MAX_TEXT_CHARS = 256
MAX_CHOICES = 64
MAX_CHOICE_CHARS = 64
MAX_DECIMAL_VALUE = 10 ** 9
DECIMAL_QUANTUM = "0.01"

# Field types a form/result may declare. No ``expr``, ``eval``, ``code`` or
# ``html`` type exists, so a package can never smuggle executable text.
TYPE_DECIMAL = "decimal"
TYPE_INTEGER = "integer"
TYPE_BOOLEAN = "boolean"
TYPE_TEXT = "text"
TYPE_CHOICE = "choice"
FIELD_TYPES = frozenset(
    {TYPE_DECIMAL, TYPE_INTEGER, TYPE_BOOLEAN, TYPE_TEXT, TYPE_CHOICE}
)

# The exact top-level keys a package may carry. A ``script``, ``code``,
# ``html``, ``command``, ``mount``/``mounts``, ``env``/``environment``,
# ``dependencies``/``install``, ``dockerfile`` or ``image`` field is therefore
# rejected as an unknown key — a package can never request a shell, a mount, an
# environment variable or a dependency installation.
_ALLOWED_PACKAGE_KEYS = frozenset(
    {"schema_version", "package_id", "runtime", "handler", "title", "form", "result"}
)

# Field-definition keys. Anything else on a field is rejected, so a field can
# never carry an executable or expression payload.
_ALLOWED_FIELD_KEYS = frozenset(
    {"name", "type", "required", "min", "max", "max_chars", "options"}
)

# Code-owned allowlists. The handler is a *name* here, resolved by the runner
# to a trusted, pre-installed function — never read from the package.
ALLOWED_PACKAGE_IDS = frozenset({"quotation-rules"})
ALLOWED_HANDLERS = frozenset({"quotation_rules.evaluate"})

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_DECIMAL_STR_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")

# Bounded rejection reasons (never interpolate caller content).
REASON_NOT_MAPPING = "package is not a mapping"
REASON_TOO_LARGE = "package is too large"
REASON_UNKNOWN_KEYS = "package has unsupported fields"
REASON_UNSUPPORTED_SCHEMA = "unsupported schema_version"
REASON_RUNTIME_MISMATCH = "runtime identity mismatch"
REASON_UNSUPPORTED_PACKAGE = "unsupported package_id"
REASON_UNSUPPORTED_HANDLER = "unsupported handler"
REASON_INVALID_TITLE = "invalid title"
REASON_INVALID_FORM = "invalid form field"
REASON_INVALID_RESULT = "invalid result field"
REASON_NO_FORM = "package has no form fields"

# Normalized run-result states (produced by the broker, surfaced by the boundary).
STATE_OK = "ok"
STATE_INPUT_INVALID = "input_invalid"
STATE_PACKAGE_INVALID = "package_invalid"
STATE_RUNTIME_UNAVAILABLE = "runtime_unavailable"
STATE_RUNTIME_BLOCKED = "runtime_blocked"
STATE_TIMEOUT = "timeout"
STATE_OUTPUT_INVALID = "output_invalid"
STATE_RUNNER_FAILED = "runner_failed"
RUN_STATES = frozenset(
    {
        STATE_OK,
        STATE_INPUT_INVALID,
        STATE_PACKAGE_INVALID,
        STATE_RUNTIME_UNAVAILABLE,
        STATE_RUNTIME_BLOCKED,
        STATE_TIMEOUT,
        STATE_OUTPUT_INVALID,
        STATE_RUNNER_FAILED,
    }
)


def dumps(obj: Any) -> str:
    """Serialize to a single-line, deterministic, ASCII-safe JSON string."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _bounded_str(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _decimal_value(value: Any) -> Optional[Decimal]:
    """Coerce a bounded decimal input to a :class:`Decimal`, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str) and _DECIMAL_STR_RE.match(value):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _validate_field(field: Any, kind: str) -> Optional[str]:
    if not isinstance(field, dict):
        return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    if not set(field).issubset(_ALLOWED_FIELD_KEYS):
        return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    name = field.get("name")
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    ftype = field.get("type")
    if ftype not in FIELD_TYPES:
        return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    if kind == "form":
        required = field.get("required", True)
        if not isinstance(required, bool):
            return REASON_INVALID_FORM
    else:
        if "required" in field:
            return REASON_INVALID_RESULT
    if ftype in (TYPE_DECIMAL, TYPE_INTEGER):
        lo = field.get("min")
        hi = field.get("max")
        if lo is not None and (not _finite_number(lo) or abs(lo) > MAX_DECIMAL_VALUE):
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
        if hi is not None and (not _finite_number(hi) or abs(hi) > MAX_DECIMAL_VALUE):
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
        if lo is not None and hi is not None and lo > hi:
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    elif ftype == TYPE_TEXT:
        max_chars = field.get("max_chars", MAX_TEXT_CHARS)
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not (
            1 <= max_chars <= MAX_TEXT_CHARS
        ):
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    elif ftype == TYPE_CHOICE:
        options = field.get("options")
        if not isinstance(options, list) or not options or len(options) > MAX_CHOICES:
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
        if not all(_bounded_str(o, MAX_CHOICE_CHARS) for o in options):
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
        if len(set(options)) != len(options):
            return REASON_INVALID_FORM if kind == "form" else REASON_INVALID_RESULT
    return None


def validate_package(value: Any) -> Optional[str]:
    """Return a bounded reason when ``value`` is not a valid app package.

    Rejects a non-mapping, an oversized manifest, any key outside the fixed
    allowlist (which implicitly rejects script/HTML/command/mount/env/
    dependency fields), a wrong schema version, a runtime-identity mismatch, an
    unsupported package id or handler (which rejects path-escaping and script-
    like identifiers), or a malformed form/result field.
    """
    if not isinstance(value, dict):
        return REASON_NOT_MAPPING
    if len(dumps(value).encode("utf-8")) > MAX_PACKAGE_BYTES:
        return REASON_TOO_LARGE
    unknown = set(value) - _ALLOWED_PACKAGE_KEYS
    if unknown:
        return REASON_UNKNOWN_KEYS
    if value.get("schema_version") != APP_PACKAGE_SCHEMA_VERSION:
        return REASON_UNSUPPORTED_SCHEMA
    if value.get("runtime") != RUNNER_IDENTITY:
        return REASON_RUNTIME_MISMATCH
    package_id = value.get("package_id")
    if not isinstance(package_id, str) or package_id not in ALLOWED_PACKAGE_IDS:
        return REASON_UNSUPPORTED_PACKAGE
    handler = value.get("handler")
    if not isinstance(handler, str) or handler not in ALLOWED_HANDLERS:
        return REASON_UNSUPPORTED_HANDLER
    if not _bounded_str(value.get("title"), MAX_TITLE_CHARS):
        return REASON_INVALID_TITLE
    form = value.get("form")
    if not isinstance(form, list) or not form or len(form) > MAX_FORM_FIELDS:
        return REASON_NO_FORM
    for field in form:
        reason = _validate_field(field, "form")
        if reason is not None:
            return reason
    result = value.get("result")
    if not isinstance(result, list) or not result or len(result) > MAX_RESULT_FIELDS:
        return REASON_INVALID_RESULT
    for field in result:
        reason = _validate_field(field, "result")
        if reason is not None:
            return reason
    return None


def validate_form_input(package: Dict[str, Any], value: Any) -> Optional[str]:
    """Validate ``value`` against the package's form schema.

    Returns a bounded reason on failure. A required field must be present, an
    unknown key is rejected, and each value must match its declared type and
    bounds. Decimal values are coerced exactly (via ``Decimal``).
    """
    if not isinstance(value, dict):
        return "input is not a mapping"
    fields = {f["name"]: f for f in package.get("form", [])}
    unknown = set(value) - set(fields)
    if unknown:
        return "input has unknown fields"
    for field in package.get("form", []):
        name = field["name"]
        if name not in value:
            if field.get("required", True):
                return "missing required field"
            continue
        item = value[name]
        ftype = field["type"]
        if ftype == TYPE_DECIMAL:
            dec = _decimal_value(item)
            if dec is None:
                return "invalid decimal"
            if field.get("min") is not None and dec < Decimal(str(field["min"])):
                return "value below minimum"
            if field.get("max") is not None and dec > Decimal(str(field["max"])):
                return "value above maximum"
        elif ftype == TYPE_INTEGER:
            if isinstance(item, bool) or not isinstance(item, int):
                return "invalid integer"
            if field.get("min") is not None and item < field["min"]:
                return "value below minimum"
            if field.get("max") is not None and item > field["max"]:
                return "value above maximum"
        elif ftype == TYPE_BOOLEAN:
            if not isinstance(item, bool):
                return "invalid boolean"
        elif ftype == TYPE_TEXT:
            if not isinstance(item, str) or len(item) > field.get("max_chars", MAX_TEXT_CHARS):
                return "invalid text"
        elif ftype == TYPE_CHOICE:
            if item not in field.get("options", []):
                return "invalid choice"
    return None


def validate_result(package: Dict[str, Any], value: Any) -> Optional[str]:
    """Validate a runner output against the package's result schema."""
    if not isinstance(value, dict):
        return "result is not a mapping"
    fields = {f["name"]: f for f in package.get("result", [])}
    unknown = set(value) - set(fields)
    if unknown:
        return "result has unknown fields"
    for field in package.get("result", []):
        name = field["name"]
        if name not in value:
            return "missing result field"
        item = value[name]
        ftype = field["type"]
        if ftype == TYPE_DECIMAL:
            if _decimal_value(item) is None:
                return "invalid result decimal"
        elif ftype == TYPE_INTEGER:
            if isinstance(item, bool) or not isinstance(item, int):
                return "invalid result integer"
        elif ftype == TYPE_BOOLEAN:
            if not isinstance(item, bool):
                return "invalid result boolean"
        elif ftype == TYPE_TEXT:
            if not isinstance(item, str):
                return "invalid result text"
    return None


def quotation_reference_package() -> Dict[str, Any]:
    """Return the hand-written quotation-rules reference package (P4.3).

    This is the single deterministic fixture that proves the contract,
    renderer and runner integrate. It is authored here, not generated by a
    model. Rules: a 5% member discount, free shipping at or above a 100.00
    threshold, a per-region fee, non-negative subtotal, and half-up rounding
    to two decimal places.
    """
    return {
        "schema_version": APP_PACKAGE_SCHEMA_VERSION,
        "package_id": "quotation-rules",
        "runtime": RUNNER_IDENTITY,
        "handler": "quotation_rules.evaluate",
        "title": "Quotation rules",
        "form": [
            {"name": "subtotal", "type": TYPE_DECIMAL, "min": 0, "max": MAX_DECIMAL_VALUE, "required": True},
            {"name": "member", "type": TYPE_BOOLEAN, "required": True},
            {
                "name": "region",
                "type": TYPE_CHOICE,
                "options": ["west", "north", "south", "east"],
                "required": True,
            },
        ],
        "result": [
            {"name": "discount", "type": TYPE_DECIMAL},
            {"name": "shipping_fee", "type": TYPE_DECIMAL},
            {"name": "regional_fee", "type": TYPE_DECIMAL},
            {"name": "total", "type": TYPE_DECIMAL},
        ],
    }


__all__ = [
    "APP_PACKAGE_SCHEMA_VERSION",
    "RUNNER_IDENTITY",
    "MAX_PACKAGE_BYTES",
    "MAX_FORM_FIELDS",
    "MAX_RESULT_FIELDS",
    "TYPE_DECIMAL",
    "TYPE_INTEGER",
    "TYPE_BOOLEAN",
    "TYPE_TEXT",
    "TYPE_CHOICE",
    "FIELD_TYPES",
    "ALLOWED_PACKAGE_IDS",
    "ALLOWED_HANDLERS",
    "STATE_OK",
    "STATE_INPUT_INVALID",
    "STATE_PACKAGE_INVALID",
    "STATE_RUNTIME_UNAVAILABLE",
    "STATE_RUNTIME_BLOCKED",
    "STATE_TIMEOUT",
    "STATE_OUTPUT_INVALID",
    "STATE_RUNNER_FAILED",
    "RUN_STATES",
    "dumps",
    "validate_package",
    "validate_form_input",
    "validate_result",
    "quotation_reference_package",
]
