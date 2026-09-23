"""Shared, Qt-free visual-token contract (P4.2a v10).

The single source of truth for every application-owned visual value shared
between the Qt desktop (:mod:`hrca.style`) and the native Win32 credential
sheet (:mod:`hrca.credential_sheet_win`). Both adapters consume these semantic
tokens so the two surfaces share one component grammar instead of duplicated
literal palettes and metrics.

This module contains only presentation constants and pure scaling/colour
helpers. It imports no Qt, no Win32 handle, no credential/provider/boundary
code and no business logic, and it performs no side effects. Values follow the
restrained pixel-inspired contract: a 4 px base grid, compact 24/28/32 px
controls, one-pixel neutral borders and near-square 0-2 px corner radii.

Colours are held as ``#rrggbb`` hex strings. :func:`colorref` converts one to a
Win32 ``COLORREF`` (0x00BBGGRR) for the native adapter; the Qt adapter consumes
the hex values directly. :func:`scale` performs the single logical-to-physical
pixel scale the native sheet applies once, so no module mixes logical and
physical pixel constants.
"""

from __future__ import annotations

from typing import Dict

# ---------------------------------------------------------------------------
# Spacing — a 4 px base scale. ``SPACE_0`` is the flush (zero) inset/gap used
# where a parent and its only child meet with no padding.
# ---------------------------------------------------------------------------
SPACE_0 = 0
SPACE_4 = 4
SPACE_8 = 8
SPACE_12 = 12
SPACE_16 = 16
SPACE_24 = 24

# Standard insets: 12 px content inset, 8 px between related controls,
# 16 px between grouped sections.
INSET = SPACE_12
GAP_TIGHT = SPACE_8
GAP_GROUP = SPACE_16

# ---------------------------------------------------------------------------
# Corner radii — near-square 0-2 px for both containers/inputs and small chips.
# ---------------------------------------------------------------------------
RADIUS_CONTAINER = 2
RADIUS_CHIP = 2

# ---------------------------------------------------------------------------
# Border / stroke roles — crisp one-pixel neutral borders.
# ---------------------------------------------------------------------------
BORDER_WIDTH = 1
FOCUS_BORDER_WIDTH = 1

# ---------------------------------------------------------------------------
# Typography roles (px). Body copy uses the platform UI font; micro-headings
# and status/evidence text use the platform fixed-width font.
# ---------------------------------------------------------------------------
FONT_BODY = 13
FONT_STATUS = 12
FONT_MICRO_HEADING = 11
FONT_CODE = 13

# ---------------------------------------------------------------------------
# Control heights (px) — compact, on the 4 px grid.
# ---------------------------------------------------------------------------
CONTROL_HEIGHT_COMPACT = 28  # toolbar / compact peer buttons
CONTROL_HEIGHT_FIELD = 24    # single-line name / key / provider fields
CONTROL_HEIGHT_BUTTON = 30   # dialog Save / Cancel / Rename / Replace actions

# ---------------------------------------------------------------------------
# Monochrome light/dark palettes. The accent is near-black (light) / white
# (dark); success/warning/error are status-only exceptions; the syntax colours
# are code-highlight metadata, not a general accent.
# ---------------------------------------------------------------------------
LIGHT_COLORS: Dict[str, str] = {
    "window": "#f3f4f6",
    "surface": "#ffffff",
    "sunken": "#eceef1",
    "border": "#d0d4da",
    "text": "#1f2328",
    "text_secondary": "#57606a",
    "text_disabled": "#8c959f",
    "accent": "#1f2328",
    "accent_hover": "#333a41",
    "accent_pressed": "#0e1013",
    "focus": "#1f2328",
    "on_accent": "#ffffff",
    "info": "#57606a",
    "success": "#1a7f37",
    "warning": "#9a6700",
    "error": "#cf222e",
    "neutral": "#57606a",
    "syntax_keyword": "#0550ae",
    "syntax_string": "#116329",
    "syntax_comment": "#57606a",
    "syntax_number": "#6633bb",
}

DARK_COLORS: Dict[str, str] = {
    "window": "#1e1f22",
    "surface": "#26272b",
    "sunken": "#17181b",
    "border": "#3a3c41",
    "text": "#dcddde",
    "text_secondary": "#9aa0a6",
    "text_disabled": "#6e7681",
    "accent": "#ffffff",
    "accent_hover": "#e6e6e6",
    "accent_pressed": "#cccccc",
    "focus": "#e6e6e6",
    "on_accent": "#1f2328",
    "info": "#9aa0a6",
    "success": "#3fb950",
    "warning": "#d29922",
    "error": "#ff7b72",
    "neutral": "#9aa0a6",
    "syntax_keyword": "#c586c0",
    "syntax_string": "#ce9178",
    "syntax_comment": "#7aa668",
    "syntax_number": "#b5cea8",
}

# Chip background alpha (0-255) tinted over the surface for state chips.
CHIP_ALPHA_LIGHT = 10
CHIP_ALPHA_DARK = 28


def colors(scheme: str) -> Dict[str, str]:
    """Return the colour-token mapping for ``scheme`` (``"light"`` or ``"dark"``)."""
    return DARK_COLORS if scheme == "dark" else LIGHT_COLORS


def chip_alpha(scheme: str) -> int:
    """Return the chip tint alpha for ``scheme``."""
    return CHIP_ALPHA_DARK if scheme == "dark" else CHIP_ALPHA_LIGHT


def scale(value: int, dpi: int = 96) -> int:
    """Scale one logical pixel ``value`` to ``dpi`` (physical pixels) once.

    ``dpi <= 0`` is treated as 96. Zero stays zero; a non-zero value scales to
    at least one physical pixel.
    """
    if dpi <= 0:
        dpi = 96
    if value == 0:
        return 0
    return max(1, round(value * dpi / 96))


def colorref(hex_color: str) -> int:
    """Return a Win32 ``COLORREF`` (0x00BBGGRR) for a ``#rrggbb`` hex colour."""
    value = hex_color.lstrip("#")
    r = int(value[0:2], 16)
    g = int(value[2:4], 16)
    b = int(value[4:6], 16)
    return (b << 16) | (g << 8) | r


__all__ = [
    "SPACE_0",
    "SPACE_4",
    "SPACE_8",
    "SPACE_12",
    "SPACE_16",
    "SPACE_24",
    "INSET",
    "GAP_TIGHT",
    "GAP_GROUP",
    "RADIUS_CONTAINER",
    "RADIUS_CHIP",
    "BORDER_WIDTH",
    "FOCUS_BORDER_WIDTH",
    "FONT_BODY",
    "FONT_STATUS",
    "FONT_MICRO_HEADING",
    "FONT_CODE",
    "CONTROL_HEIGHT_COMPACT",
    "CONTROL_HEIGHT_FIELD",
    "CONTROL_HEIGHT_BUTTON",
    "LIGHT_COLORS",
    "DARK_COLORS",
    "CHIP_ALPHA_LIGHT",
    "CHIP_ALPHA_DARK",
    "colors",
    "chip_alpha",
    "scale",
    "colorref",
]
