"""P3.2 visual design system for the IDE workspace shell.

This module is the *single owner* of every visual value in the desktop client:
colours, spacing, radii, typography, component geometry and the Qt style sheet.
No widget in :mod:`hrca.client` hard-codes an ad-hoc colour, radius or pixel
padding — every token is read from here, and the module itself contains no
business logic (it never imports the scanner, planner, report, provider, Git or
command-execution code, and it never decides whether an action is permitted).

It provides:

* a light and a dark :class:`Palette` — the same set of semantic tokens in both
  palettes, chosen at start-up from the Qt palette so the app follows the
  operating-system appearance;
* the 4 px spacing scale, the corner radii, the typography sizes and the
  component geometry constants;
* :func:`palette_for` / :func:`apply` — detect the colour scheme and install the
  matching style sheet;
* :func:`contrast_ratio` — a WCAG relative-luminance helper used by the tests to
  prove the accessibility contract.

Semantic tokens (identical names in both palettes): ``window`` background,
``surface`` (raised), ``sunken`` (inputs/code areas), ``border`` (hairline),
``text`` (primary), ``text_secondary``, ``text_disabled``, ``accent``,
``focus``, ``on_accent`` (text drawn over the accent), plus state colours
``info`` / ``success`` / ``warning`` / ``error`` / ``neutral`` and the four
syntax-highlight colours. State colours are always paired with a word, never
relied on alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QProxyStyle, QStyle

# ---------------------------------------------------------------------------
# Spacing — a 4 px base scale. Only these values may appear as margins/padding.
# ``SPACE_0`` is the flush (zero) inset/gap used at pane edges where a parent
# and its only child meet with no padding.
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
# Corner radii — 6 px containers/inputs, 4 px small chips. No shadows, no
# gradients, no bevels or 3D frames anywhere.
# ---------------------------------------------------------------------------
RADIUS_CONTAINER = 6
RADIUS_CHIP = 4

# ---------------------------------------------------------------------------
# Typography (px). Interface text uses the platform UI font at 13 px; status
# and panel headers are smaller; code and Twin text use the fixed-width font
# at 13 px with about 1.45 line spacing.
# ---------------------------------------------------------------------------
UI_FONT_SIZE = 13
STATUS_FONT_SIZE = 12
PANEL_HEADER_FONT_SIZE = 11
CODE_FONT_SIZE = 13
CODE_LINE_SPACING = 1.45
CODE_LINE_HEIGHT_PERCENT = 145  # int(CODE_LINE_SPACING * 100), Qt proportional mode

# ---------------------------------------------------------------------------
# Component geometry (px).
# ---------------------------------------------------------------------------
COMMAND_BAR_HEIGHT = 40
STATUS_BAR_HEIGHT = 24
PANEL_HEADER_HEIGHT = 28
CHAT_COMPOSER_HEIGHT = 56

# Bottom utility panel geometry. The panel is a single header (the flat tab bar
# plus the disclosure control) above one body. The header must be tall enough to
# fit the tab bar; the body floor keeps the selected surface usable at every
# supported window size while the header height is the only height the panel
# reserves when collapsed.
BOTTOM_PANEL_HEADER_HEIGHT = 32
BOTTOM_PANEL_BODY_MIN_HEIGHT = 200
BOTTOM_PANEL_MIN_HEIGHT = BOTTOM_PANEL_HEADER_HEIGHT + BOTTOM_PANEL_BODY_MIN_HEIGHT
BOTTOM_PANEL_MAX_HEIGHT = 10000
BOTTOM_PANEL_DEFAULT_HEIGHT = 240
BOTTOM_PANEL_STRETCH = 1

TREE_ROW_HEIGHT = 22
TREE_INDENT = 20
TAB_HEIGHT = 30

# Project Explorer disclosure indicators. Every tree depth reserves a fixed
# 20 px slot for the branch indicator; a 6 x 6 px vector chevron is painted
# dead-centre inside that slot and only its direction changes with state. The
# 20 px slot and 22 px row together give an at-least-20 x 20 interactive hit
# target. No disclosure glyph is ever embedded in a folder label.
TREE_DISCLOSURE_SLOT_WIDTH = 20
TREE_CHEVRON_SIZE = 6
TREE_CHEVRON_STROKE = 1.5
TREE_DISCLOSURE_HIT_SIZE = 20

# Code Map header pin control (P3.3). The lock is a monochrome vector padlock
# painted at LOCK_ICON_SIZE with a LOCK_STROKE outline; only its shape (closed
# vs open shackle) and colour change with the pin state.
LOCK_ICON_SIZE = 16
LOCK_STROKE = 1.75

SPLITTER_HANDLE_WIDTH = 6          # 6 px interactive hit area
SPLITTER_HAIRLINE_WIDTH = 1        # visually a 1 px hairline

EXPLORER_DEFAULT_WIDTH = 240
EXPLORER_MIN_WIDTH = 180
EXPLORER_MAX_WIDTH = 420
SOURCE_MIN_WIDTH = 360
TWIN_MIN_WIDTH = 300

# Initial primary-splitter pane widths (px), applied once at construction.
PRIMARY_SOURCE_INITIAL_WIDTH = 560
PRIMARY_TWIN_INITIAL_WIDTH = 360

# Primary-splitter stretch factors (unitless): Explorer is fixed (0); Source
# and Twin take extra width in a 3 : 2 ratio.
EXPLORER_STRETCH = 0
SOURCE_STRETCH = 3
TWIN_STRETCH = 2

TWIN_CONTENT_MAX_WIDTH = 720

# Initial vertical-splitter primary-workspace height (px) and the
# workspace : bottom-panel stretch ratio (3 : 1).
PRIMARY_WORKSPACE_INITIAL_HEIGHT = 560
PRIMARY_WORKSPACE_STRETCH = 3

# Persistent status-field maximum widths (px); the Root and File fields carry
# long paths and are capped so the six fields stay on one row.
STATUS_ROOT_MAX_WIDTH = 260
STATUS_FILE_MAX_WIDTH = 220

WINDOW_DEFAULT_WIDTH = 1360
WINDOW_DEFAULT_HEIGHT = 840
WINDOW_MIN_WIDTH = 1024
WINDOW_MIN_HEIGHT = 640

# Accessibility thresholds (WCAG).
CONTRAST_BODY = 4.5
CONTRAST_LARGE = 3.0


def _rgba(hex_color: str, alpha: int) -> str:
    """Return ``hex_color`` (``#rrggbb``) as an ``rgba(r, g, b, a)`` string."""
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


@dataclass(frozen=True)
class Palette:
    """One colour scheme: the same semantic tokens in light and dark variants."""

    name: str
    is_dark: bool
    window: str
    surface: str
    sunken: str
    border: str
    text: str
    text_secondary: str
    text_disabled: str
    accent: str
    accent_hover: str
    accent_pressed: str
    focus: str
    on_accent: str
    info: str
    success: str
    warning: str
    error: str
    neutral: str
    syntax_keyword: str
    syntax_string: str
    syntax_comment: str
    syntax_number: str
    # alpha (0-255) used to tint the surface for a state chip background.
    _chip_alpha: int = 30

    def color(self, value: str) -> QColor:
        """Return ``value`` as a :class:`QColor` (accepts hex or rgba)."""
        return QColor(value)

    def state_fg(self, token: str) -> str:
        """Return the foreground hex for a semantic state token."""
        return getattr(self, token)

    def state_bg(self, token: str) -> str:
        """Return a subtle surface tint for a state chip, from the state colour."""
        return _rgba(self.state_fg(token), self._chip_alpha)


# ---------------------------------------------------------------------------
# Palettes.
# ---------------------------------------------------------------------------
LIGHT_PALETTE = Palette(
    name="light",
    is_dark=False,
    window="#f3f4f6",
    surface="#ffffff",
    sunken="#eceef1",
    border="#d0d4da",
    text="#1f2328",
    text_secondary="#57606a",
    text_disabled="#8c959f",
    accent="#1f2328",
    accent_hover="#333a41",
    accent_pressed="#0e1013",
    focus="#1f2328",
    on_accent="#ffffff",
    info="#57606a",
    success="#1a7f37",
    warning="#9a6700",
    error="#cf222e",
    neutral="#57606a",
    syntax_keyword="#0550ae",
    syntax_string="#116329",
    syntax_comment="#57606a",
    syntax_number="#6633bb",
    _chip_alpha=10,
)

DARK_PALETTE = Palette(
    name="dark",
    is_dark=True,
    window="#1e1f22",
    surface="#26272b",
    sunken="#17181b",
    border="#3a3c41",
    text="#dcddde",
    text_secondary="#9aa0a6",
    text_disabled="#6e7681",
    accent="#ffffff",
    accent_hover="#e6e6e6",
    accent_pressed="#cccccc",
    focus="#e6e6e6",
    on_accent="#1f2328",
    info="#9aa0a6",
    success="#3fb950",
    warning="#d29922",
    error="#ff7b72",
    neutral="#9aa0a6",
    syntax_keyword="#c586c0",
    syntax_string="#ce9178",
    syntax_comment="#7aa668",
    syntax_number="#b5cea8",
    _chip_alpha=28,
)

PALETTES = {"light": LIGHT_PALETTE, "dark": DARK_PALETTE}

# ---------------------------------------------------------------------------
# State token names shared by the Twin chip and any state indicator.
# ---------------------------------------------------------------------------
STATE_INFO = "info"
STATE_SUCCESS = "success"
STATE_WARNING = "warning"
STATE_ERROR = "error"
STATE_NEUTRAL = "neutral"

# The six bounded Twin presentation states map onto the five semantic state
# colours. ``empty`` and ``unsupported`` both use the neutral token (they both
# mean "no Twin is available"); the chip always carries a distinct word, so
# colour is never the sole signal.
TWIN_STATE_TOKEN = {
    "empty": STATE_NEUTRAL,
    "loading": STATE_INFO,
    "available": STATE_SUCCESS,
    "stale": STATE_WARNING,
    "conflict": STATE_ERROR,
    "unsupported": STATE_NEUTRAL,
}

# Human-readable chip words for each Twin state (title case, never colour alone).
TWIN_STATE_WORD = {
    "empty": "Empty",
    "loading": "Loading",
    "available": "Available",
    "stale": "Stale",
    "conflict": "Conflict",
    "unsupported": "Unsupported",
}


# ---------------------------------------------------------------------------
# Fonts.
# ---------------------------------------------------------------------------
def ui_font(size: int = UI_FONT_SIZE) -> QFont:
    """Return the platform UI font at ``size`` px."""
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPixelSize(size)
    return font


def code_font(size: int = CODE_FONT_SIZE) -> QFont:
    """Return the fixed-width font at ``size`` px for code and Twin text."""
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPixelSize(size)
    return font


def panel_header_font() -> QFont:
    """Return the panel-header font (11 px, letter-spaced, uppercase applied by
    the caller so letter-spacing stays in one place)."""
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPixelSize(PANEL_HEADER_FONT_SIZE)
    font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
    return font


def tree_folder_font(base: Optional[QFont] = None) -> QFont:
    """Return a bold copy of ``base`` (default: the UI font) for folder rows."""
    font = QFont(base) if base is not None else ui_font()
    font.setBold(True)
    return font


def tree_chevron_vertices(open_state: bool) -> List[QPointF]:
    """Return the 3 vertices of a chevron centred at the origin.

    A right-pointing chevron (collapsed, ``open_state`` false) or a
    down-pointing chevron (expanded, ``open_state`` true), both spanning the
    full :data:`TREE_CHEVRON_SIZE` square. The only difference between the two
    states is the chevron direction.
    """
    half = TREE_CHEVRON_SIZE / 2.0
    if open_state:
        return [QPointF(-half, -half), QPointF(0.0, half), QPointF(half, -half)]
    return [QPointF(-half, -half), QPointF(half, 0.0), QPointF(-half, half)]


def lock_icon(palette: Palette, locked: bool, enabled: bool = True) -> QIcon:
    """Return a monochrome vector padlock icon for the Code Map pin control.

    The closed glyph (``locked`` true) has a full shackle and is drawn in the
    palette's ``on_accent`` colour (over the accent checked background); the
    open glyph has a gap on one side and is drawn in ``text_secondary`` (or
    ``text_disabled`` when the control is disabled). The icon is painted with
    :class:`QPainter` — no emoji, glyph, icon pack or image asset.
    """
    if locked:
        color = palette.on_accent
    elif enabled:
        color = palette.text_secondary
    else:
        color = palette.text_disabled

    size = LOCK_ICON_SIZE
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), LOCK_STROKE)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QColor(color))

    # Body: a filled rounded rectangle in the lower half.
    painter.drawRoundedRect(QRectF(4.0, 9.0, 8.0, 5.0), 1.5, 1.5)

    # Shackle: a stroked arc plus the leg(s) that descend into the body. The
    # open (unlocked) glyph leaves the left leg un-drawn so the gap is the
    # only shape difference between the two states.
    painter.setBrush(Qt.NoBrush)
    if locked:
        painter.drawArc(QRectF(5.0, 4.0, 6.0, 6.0), 0, 180 * 16)
        painter.drawLine(QPointF(5.0, 7.0), QPointF(5.0, 10.0))
    else:
        painter.drawArc(QRectF(5.0, 4.0, 6.0, 6.0), 0, 135 * 16)
    painter.drawLine(QPointF(11.0, 7.0), QPointF(11.0, 10.0))
    painter.end()
    return QIcon(pixmap)


class TreeBranchStyle(QProxyStyle):
    """Draw the tree branch indicator as a monochrome vector chevron.

    Replaces Qt's native branch arrow with a right-pointing (collapsed) or
    down-pointing (expanded) chevron painted inside a fixed
    :data:`TREE_CHEVRON_SIZE` square centred in the fixed
    :data:`TREE_DISCLOSURE_SLOT_WIDTH` branch slot. Toggling a folder flips only
    the chevron direction — the slot width, label position, child indentation
    and row geometry are all untouched. Leaf items (no ``State_Children``) draw
    no indicator while keeping their normal depth alignment.
    """

    def __init__(self, palette: Palette) -> None:
        super().__init__()
        self._palette = palette

    def set_palette(self, palette: Palette) -> None:
        """Point the chevron at a different palette (e.g. light -> dark)."""
        self._palette = palette

    def drawPrimitive(self, element, option, painter, widget=None) -> None:
        if element == QStyle.PE_IndicatorBranch and option is not None:
            self._draw_branch(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    def _draw_branch(self, option, painter) -> None:
        if not (option.state & QStyle.State_Children):
            return  # leaf: no indicator, normal depth alignment retained
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(
            self._palette.text
            if (option.state & QStyle.State_MouseOver)
            or (option.state & QStyle.State_Selected)
            else self._palette.text_secondary
        )
        pen = QPen(color, TREE_CHEVRON_STROKE)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        center = option.rect.center()
        points = QPolygonF(tree_chevron_vertices(bool(option.state & QStyle.State_Open)))
        points.translate(center.x(), center.y())
        painter.drawPolyline(points)
        painter.restore()


# ---------------------------------------------------------------------------
# Colour-scheme detection and style-sheet installation.
# ---------------------------------------------------------------------------
def detect_color_scheme(app: Optional[QGuiApplication] = None) -> str:
    """Return ``"dark"`` or ``"light"`` for the operating-system appearance.

    Prefers the Qt colour-scheme hint; falls back to the window lightness when
    the hint is unavailable or unknown.
    """
    if app is None:
        app = QGuiApplication.instance()
    if app is not None:
        style_hints = getattr(app, "styleHints", None)
        if style_hints is not None:
            # ``QGuiApplication.styleHints`` is a method; call it to obtain the
            # QStyleHints object, then read its colour-scheme hint. Older Qt
            # (< 6.5) has no ``colorScheme()``, so fall back to the palette.
            try:
                scheme = style_hints().colorScheme()
            except (AttributeError, TypeError):  # Qt < 6.5 or a non-callable hint
                scheme = Qt.ColorScheme.Unknown
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"
    # Fall back to the palette window lightness only when the hint is Unknown
    # or unavailable.
    if app is not None:
        if app.palette().window().color().lightness() < 128:
            return "dark"
    return "light"


def palette_for(app: Optional[QGuiApplication] = None) -> Palette:
    """Return the :class:`Palette` matching the current colour scheme."""
    return PALETTES[detect_color_scheme(app)]


def contrast_ratio(fg: str, bg: str) -> float:
    """Return the WCAG contrast ratio between two colours (``#rrggbb``)."""
    def luminance(value: str) -> float:
        c = QColor(value)
        channels = []
        for channel in (c.redF(), c.greenF(), c.blueF()):
            if channel <= 0.03928:
                channels.append(channel / 12.92)
            else:
                channels.append(((channel + 0.055) / 1.055) ** 2.4)
        r, g, b = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    l1 = luminance(fg)
    l2 = luminance(bg)
    lighter, darker = (l1, l2) if l1 > l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


def chip_rendered_background(palette: Palette, token: str) -> str:
    """Return the opaque ``#rrggbb`` a state chip paints behind its text.

    A chip background is the state colour tinted over ``surface`` at
    ``_chip_alpha``; compositing the two yields the solid colour the chip text
    is actually drawn over, which is what the WCAG contrast tests measure.
    """
    fg = QColor(palette.state_fg(token))
    bg = QColor(palette.surface)
    alpha = palette._chip_alpha / 255.0
    r = round(fg.red() * alpha + bg.red() * (1.0 - alpha))
    g = round(fg.green() * alpha + bg.green() * (1.0 - alpha))
    b = round(fg.blue() * alpha + bg.blue() * (1.0 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


def build_stylesheet(palette: Palette) -> str:
    """Return the Qt style sheet for ``palette``.

    Every colour and radius is taken from the palette or the module constants;
    no value is invented here. Placeholders are ``$token`` so the style sheet's
    own ``{}`` braces are left untouched.
    """
    tokens: Dict[str, str] = {
        "$window": palette.window,
        "$surface": palette.surface,
        "$sunken": palette.sunken,
        "$border": palette.border,
        "$text": palette.text,
        "$text_secondary": palette.text_secondary,
        "$text_disabled": palette.text_disabled,
        "$accent": palette.accent,
        "$accent_hover": palette.accent_hover,
        "$accent_pressed": palette.accent_pressed,
        "$focus": palette.focus,
        "$on_accent": palette.on_accent,
        "$radius": f"{RADIUS_CONTAINER}px",
        "$chip_radius": f"{RADIUS_CHIP}px",
        "$tree_row": f"{TREE_ROW_HEIGHT}px",
        "$selection": _rgba(palette.accent, palette._chip_alpha),
    }

    qss = r"""
QMainWindow, QWidget#root { background: $window; color: $text; }

/* ---- command bar ---- */
QWidget#commandBar { background: $window; border-bottom: 1px solid $border; }

/* ---- pane backgrounds ---- */
QWidget#explorerPanel, QWidget#sourcePanel, QWidget#twinPanel,
QWidget#chatPanel { background: $surface; }

/* ---- generic flat push button ---- */
QPushButton {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius;
    padding: 6px 12px;
}
QPushButton:hover { background: $sunken; }
QPushButton:pressed { background: $border; }
QPushButton:disabled {
    color: $text_disabled;
    background: $sunken;
    border-color: $border;
}
QPushButton:focus { border: 1px solid $focus; }

/* primary action — monochrome: near-black on light, white on dark */
QPushButton#primaryButton {
    background: $accent;
    color: $on_accent;
    border: 1px solid $accent;
}
QPushButton#primaryButton:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primaryButton:pressed { background: $accent_pressed; border-color: $accent_pressed; }
QPushButton#primaryButton:disabled {
    background: $sunken;
    color: $text_disabled;
    border-color: $border;
}

/* ---- compact, flat tool buttons (collapse / disclosure / close) ---- */
QToolButton {
    background: transparent;
    color: $text_secondary;
    border: none;
    border-radius: $chip_radius;
    padding: 2px 4px;
}
QToolButton:hover { color: $text; background: $sunken; }
QToolButton:focus { border: 1px solid $focus; }

/* ---- Code Map pin control (checkable, monochrome lock) ---- */
QToolButton#twinLockButton { padding: 2px; }
QToolButton#twinLockButton:checked { background: $accent; color: $on_accent; }
QToolButton#twinLockButton:checked:hover { background: $accent_hover; }

/* ---- Code Map behavior-node controls (flat, text-like, left-aligned) ---- */
QPushButton#behaviorNodeButton {
    background: transparent;
    color: $text;
    border: 1px solid transparent;
    border-radius: $chip_radius;
    padding: 4px 8px;
    text-align: left;
}
QPushButton#behaviorNodeButton:hover { background: $sunken; border-color: $border; }
QPushButton#behaviorNodeButton:pressed { background: $border; }
QPushButton#behaviorNodeButton:focus { border: 1px solid $focus; }

/* ---- panel header label ---- */
QLabel#panelHeader {
    color: $text_secondary;
    font-size: 11px;
    background: transparent;
    padding: 8px 12px 4px 12px;
}

/* ---- secondary / status / empty-state text ---- */
QLabel#secondary { color: $text_secondary; }
QLabel#projectRootLabel { color: $text_secondary; padding: 4px 12px 6px 12px; }
QLabel#emptyState { color: $text_secondary; padding: 16px; }
QLabel#statusField { color: $text_secondary; font-size: 12px; }

/* ---- Project Explorer tree ---- */
QTreeView#projectTree {
    background: $surface;
    border: none;
    outline: none;
    font-size: 13px;
}
QTreeView#projectTree::item { height: $tree_row; }
QTreeView#projectTree::item:selected {
    background: $selection;
    color: $text;
}
QTreeView#projectTree::item:hover { background: $sunken; }

/* ---- code and document views ---- */
QPlainTextEdit { background: $sunken; color: $text; border: none; }

/* ---- Twin body is a QLabel (not a text edit), so target the real class ---- */
QLabel#twinBody { background: $surface; border: none; padding: 0; }

/* ---- flat tabs (Source Code and bottom panel) ---- */
QTabWidget#sourceTabs::pane {
    border: none;
    border-top: 1px solid $border;
    background: $sunken;
}
QTabBar::tab {
    background: transparent;
    color: $text_secondary;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 4px 12px;
    min-height: 22px;
}
QTabBar::tab:selected {
    color: $text;
    font-weight: bold;
    border-bottom: 2px solid $accent;
}
QTabBar::tab:hover:!selected { color: $text; }
QTabBar::close-button { subcontrol-origin: padding; }

/* ---- bottom utility panel: header (tab bar + disclosure) and body ---- */
QWidget#bottomPanelHeader {
    background: $surface;
    border-bottom: 1px solid $border;
}
QTabBar#bottomPanelTabs { background: transparent; }

/* ---- Agent Chat composer ---- */
QTextEdit#chatComposer {
    background: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius;
    padding: 6px;
}
QTextEdit#chatComposer:disabled {
    background: $sunken;
    color: $text_disabled;
    border-color: $border;
}

/* ---- status bar ---- */
QWidget#statusBar { background: $surface; border-top: 1px solid $border; }

/* ---- splitter handles are painted by HairlineSplitterHandle ---- */
QSplitter::handle { background: transparent; }
"""

    # Replace longer placeholders first so a prefix token (``$text``) never
    # corrupts a longer one (``$text_secondary`` / ``$text_disabled``).
    for placeholder, value in sorted(tokens.items(), key=lambda kv: -len(kv[0])):
        qss = qss.replace(placeholder, value)
    return qss


# The single branch style installed on the application. It is created once and
# reused across :func:`apply` calls (which may fire more than once per process,
# e.g. when a ``MainWindow`` re-applies its palette), so the chevron style is
# never stacked on top of itself.
_tree_branch_style: Optional[TreeBranchStyle] = None


def apply(app: QGuiApplication, palette: Palette) -> None:
    """Install ``palette`` as the application style sheet and base font.

    The base interface font is set on the application so every widget inherits
    it; individual widgets that need a smaller or fixed-width face call
    :func:`panel_header_font` / :func:`code_font` and set it directly (a Qt
    style-sheet ``font-size`` would otherwise override any programmatic font).

    The :class:`TreeBranchStyle` is installed *before* the style sheet so Qt
    wraps it (rather than the reverse) and still delegates the branch indicator
    to it; everything else keeps the style-sheet styling.
    """
    global _tree_branch_style
    app.setFont(ui_font())
    if _tree_branch_style is None:
        _tree_branch_style = TreeBranchStyle(palette)
        app.setStyle(_tree_branch_style)
    else:
        _tree_branch_style.set_palette(palette)
    app.setStyleSheet(build_stylesheet(palette))


# ---------------------------------------------------------------------------
# Component style-sheet factories.
#
# These are the only place a widget-specific style-sheet *value* is composed.
# :mod:`hrca.client` calls a factory with the palette (and, for the chip, the
# state name) and never assembles a colour, radius or pixel string itself.
# ---------------------------------------------------------------------------
def secondary_text_style(palette: Palette) -> str:
    """Return the style sheet for secondary (muted) text."""
    return f"color: {palette.text_secondary};"


def status_label_style(palette: Palette) -> str:
    """Return the style sheet for the transient status-bar message."""
    return f"color: {palette.text}; font-size: {STATUS_FONT_SIZE}px;"


def status_field_style(palette: Palette) -> str:
    """Return the style sheet for a persistent status-bar field."""
    return f"color: {palette.text_secondary}; font-size: {STATUS_FONT_SIZE}px;"


def project_root_label_style(palette: Palette) -> str:
    """Return the style sheet for the explorer's project-root footer label."""
    return f"color: {palette.text_secondary}; padding: {GAP_TIGHT}px {INSET}px;"


def twin_chip_style(palette: Palette, state: str) -> str:
    """Return the style sheet for the Twin state chip in ``state``.

    The colour comes from the state's semantic token; the word is applied
    separately by the caller so colour is never the sole signal.
    """
    token = TWIN_STATE_TOKEN.get(state, STATE_NEUTRAL)
    fg = palette.state_fg(token)
    bg = palette.state_bg(token)
    return (
        f"color: {fg}; background: {bg}; border-radius: {RADIUS_CHIP}px; "
        f"padding: 1px {GAP_TIGHT}px; font-size: {STATUS_FONT_SIZE}px;"
    )


def preview_banner_style(palette: Palette) -> str:
    """Return the style sheet for the labelled read-only preview banner."""
    return (
        f"color: {palette.text_secondary}; font-size: {STATUS_FONT_SIZE}px; "
        f"padding: {GAP_TIGHT}px {INSET}px;"
    )


def unavailable_banner_style(palette: Palette) -> str:
    """Return the style sheet for the bounded unavailable-state banner."""
    return (
        f"color: {palette.warning}; font-size: {STATUS_FONT_SIZE}px; "
        f"padding: {GAP_TIGHT}px {INSET}px;"
    )


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
    "UI_FONT_SIZE",
    "STATUS_FONT_SIZE",
    "PANEL_HEADER_FONT_SIZE",
    "CODE_FONT_SIZE",
    "CODE_LINE_SPACING",
    "CODE_LINE_HEIGHT_PERCENT",
    "COMMAND_BAR_HEIGHT",
    "STATUS_BAR_HEIGHT",
    "PANEL_HEADER_HEIGHT",
    "CHAT_COMPOSER_HEIGHT",
    "BOTTOM_PANEL_HEADER_HEIGHT",
    "BOTTOM_PANEL_BODY_MIN_HEIGHT",
    "BOTTOM_PANEL_MIN_HEIGHT",
    "BOTTOM_PANEL_MAX_HEIGHT",
    "BOTTOM_PANEL_DEFAULT_HEIGHT",
    "BOTTOM_PANEL_STRETCH",
    "TREE_ROW_HEIGHT",
    "TREE_INDENT",
    "TREE_DISCLOSURE_SLOT_WIDTH",
    "TREE_CHEVRON_SIZE",
    "TREE_CHEVRON_STROKE",
    "TREE_DISCLOSURE_HIT_SIZE",
    "LOCK_ICON_SIZE",
    "LOCK_STROKE",
    "TAB_HEIGHT",
    "SPLITTER_HANDLE_WIDTH",
    "SPLITTER_HAIRLINE_WIDTH",
    "EXPLORER_DEFAULT_WIDTH",
    "EXPLORER_MIN_WIDTH",
    "EXPLORER_MAX_WIDTH",
    "SOURCE_MIN_WIDTH",
    "TWIN_MIN_WIDTH",
    "PRIMARY_SOURCE_INITIAL_WIDTH",
    "PRIMARY_TWIN_INITIAL_WIDTH",
    "EXPLORER_STRETCH",
    "SOURCE_STRETCH",
    "TWIN_STRETCH",
    "TWIN_CONTENT_MAX_WIDTH",
    "PRIMARY_WORKSPACE_INITIAL_HEIGHT",
    "PRIMARY_WORKSPACE_STRETCH",
    "STATUS_ROOT_MAX_WIDTH",
    "STATUS_FILE_MAX_WIDTH",
    "WINDOW_DEFAULT_WIDTH",
    "WINDOW_DEFAULT_HEIGHT",
    "WINDOW_MIN_WIDTH",
    "WINDOW_MIN_HEIGHT",
    "CONTRAST_BODY",
    "CONTRAST_LARGE",
    "STATE_INFO",
    "STATE_SUCCESS",
    "STATE_WARNING",
    "STATE_ERROR",
    "STATE_NEUTRAL",
    "TWIN_STATE_TOKEN",
    "TWIN_STATE_WORD",
    "Palette",
    "LIGHT_PALETTE",
    "DARK_PALETTE",
    "PALETTES",
    "ui_font",
    "code_font",
    "panel_header_font",
    "tree_folder_font",
    "tree_chevron_vertices",
    "lock_icon",
    "TreeBranchStyle",
    "detect_color_scheme",
    "palette_for",
    "contrast_ratio",
    "chip_rendered_background",
    "build_stylesheet",
    "apply",
    "secondary_text_style",
    "status_label_style",
    "status_field_style",
    "project_root_label_style",
    "twin_chip_style",
    "preview_banner_style",
    "unavailable_banner_style",
]
