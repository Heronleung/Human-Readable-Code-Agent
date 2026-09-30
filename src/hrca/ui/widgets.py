"""Low-level, reusable Qt widgets shared by the shell and the destinations.

These were extracted from :mod:`hrca.ui.client` so the destination modules can
reuse them without importing the window (which would be a cycle). They are
presentation primitives only: a read-only code view with a palette-driven
highlighter, an eliding label, a hairline splitter and the library/project
tree views. None of them touches the boundary.
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QStandardItem,
    QStandardItemModel,  # noqa: F401 - re-exported for callers
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QSplitterHandle,
    QTreeView,
    QWidget,
)

from . import style

_PY_KEYWORDS = (
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
)

_PY_KEYWORD_PATTERN = r"\b(?:" + "|".join(_PY_KEYWORDS) + r")\b"


class PythonHighlighter(QSyntaxHighlighter):
    """A minimal Python syntax highlighter whose colours come from the palette."""

    def __init__(self, document, palette: style.Palette) -> None:
        super().__init__(document)
        self._rules: List[tuple] = []

        keyword_fmt = QTextCharFormat()
        keyword_fmt.setForeground(QColor(palette.syntax_keyword))
        keyword_fmt.setFontWeight(QFont.Bold)

        string_fmt = QTextCharFormat()
        string_fmt.setForeground(QColor(palette.syntax_string))

        comment_fmt = QTextCharFormat()
        comment_fmt.setForeground(QColor(palette.syntax_comment))

        number_fmt = QTextCharFormat()
        number_fmt.setForeground(QColor(palette.syntax_number))

        self._rules = [
            (_PY_KEYWORD_PATTERN, keyword_fmt),
            (r"\".*?\"|'.*?'", string_fmt),
            (r"#[^\n]*", comment_fmt),
            (r"\b\d+(?:\.\d+)?\b", number_fmt),
        ]

    def highlightBlock(self, text: str) -> None:
        """Apply the palette's syntax formats to one block of text."""
        for pattern, fmt in self._rules:
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class CodeView(QPlainTextEdit):
    """A read-only, monospaced, syntax-highlighted code/JSON view."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        palette: Optional[style.Palette] = None,
    ) -> None:
        super().__init__(parent)
        self._palette = palette or style.palette_for()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFont(style.code_font())
        self._highlighter = PythonHighlighter(self.document(), self._palette)
        self._apply_line_height()

    def setPlainText(self, text: str) -> None:
        """Set the text and re-apply the proportional line height."""
        super().setPlainText(text)
        self._apply_line_height()

    def _apply_line_height(self) -> None:
        """Set about 1.45 proportional line spacing across the document."""
        fmt = QTextBlockFormat()
        fmt.setLineHeight(
            style.CODE_LINE_HEIGHT_PERCENT,
            QTextBlockFormat.ProportionalHeight.value,
        )
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.Document)
        cursor.mergeBlockFormat(fmt)


class ElidedLabel(QLabel):
    """A :class:`QLabel` that elides its full text to fit its width.

    ``text()`` returns the full text when the widget has no width yet (so
    offscreen tests read the un-elided value); once laid out, the text is
    elided rather than wrapping or growing. The complete text is preserved in
    ``fullText()`` and in the tooltip.
    """

    def __init__(
        self,
        text: str = "",
        elide_mode: Qt.TextElideMode = Qt.ElideMiddle,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self._elide_mode = elide_mode
        self.setToolTip(text)
        self._refresh()

    def setText(self, text: str) -> None:
        """Set the full text; the displayed form is elided to fit."""
        self._full_text = text
        self.setToolTip(text)
        self._refresh()

    def fullText(self) -> str:
        """Return the un-elided text."""
        return self._full_text

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        if self.width() <= 0:
            super().setText(self._full_text)
        else:
            super().setText(
                self.fontMetrics().elidedText(
                    self._full_text, self._elide_mode, self.width()
                )
            )


class _HairlineHandle(QSplitterHandle):
    """A 1 px hairline splitter handle inside a 6 px interactive hit area."""

    def __init__(
        self,
        orientation: Qt.Orientation,
        parent: QSplitter,
        palette: style.Palette,
    ) -> None:
        super().__init__(orientation, parent)
        self._palette = palette
        self._hovered = False
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.transparent)
        color = QColor(self._palette.accent if self._hovered else self._palette.border)
        painter.setPen(QPen(color, style.SPLITTER_HAIRLINE_WIDTH))
        if self.orientation() == Qt.Horizontal:
            x = self.width() // 2
            painter.drawLine(x, 0, x, self.height())
        else:
            y = self.height() // 2
            painter.drawLine(0, y, self.width(), y)
        painter.end()


class HairlineSplitter(QSplitter):
    """A :class:`QSplitter` whose handles are 1 px hairlines with a 6 px hit area."""

    def __init__(
        self,
        orientation: Qt.Orientation,
        palette: style.Palette,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._palette = palette
        self.setHandleWidth(style.SPLITTER_HANDLE_WIDTH)

    def createHandle(self) -> QSplitterHandle:
        return _HairlineHandle(self.orientation(), self, self._palette)


class _ProjectTreeView(QTreeView):
    """A :class:`QTreeView` that toggles a folder on the *first* click.

    Qt delivers a rapid second click as a ``MouseButtonDblClick``, which
    neither emits ``clicked`` for the branch indicator nor toggles it, so a
    folder appears not to close until the double-click interval elapses.
    Toggling on both press and double-click makes every click a single,
    immediate toggle.
    """

    node_kind = "dir"

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._toggle_dir_at(event):
            QAbstractItemView.mousePressEvent(self, event)
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._toggle_dir_at(event):
            QAbstractItemView.mouseDoubleClickEvent(self, event)
            return
        super().mouseDoubleClickEvent(event)

    def _toggle_dir_at(self, event) -> bool:
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            return False
        item = self.model().itemFromIndex(index)
        if item is None or item.data(Qt.UserRole + 1) != self.node_kind:
            return False
        if self.isExpanded(index):
            self.collapse(index)
        else:
            self.expand(index)
        return True


class _DocumentTreeView(_ProjectTreeView):
    """A document-library tree that toggles a folder on the *first* click."""

    node_kind = "folder"


def _json_text(value: Any) -> str:
    """Pretty-print ``value`` for display (non-ASCII rendered readably)."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


__all__ = [
    "PythonHighlighter",
    "CodeView",
    "ElidedLabel",
    "HairlineSplitter",
    "_ProjectTreeView",
    "_DocumentTreeView",
    "_json_text",
]
