"""Reusable presentation widgets shared by every page."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..theme import Palette


class Card(QFrame):
    """A rounded surface panel with a consistent inner margin."""

    def __init__(self, parent: QWidget | None = None, padding: int = 18,
                 spacing: int = 12, sub: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("SubCard" if sub else "Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)

    def body(self) -> QVBoxLayout:
        """The card's vertical layout."""
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """Append a widget to the card and return it."""
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        """Append a nested layout to the card."""
        self._layout.addLayout(layout)

    def with_shadow(self, palette: Palette) -> Card:
        """Attach a soft drop shadow (used for elevated panels)."""
        effect = QGraphicsDropShadowEffect(self)
        effect.setBlurRadius(28)
        effect.setOffset(0, 6)
        effect.setColor(QColor(0, 0, 0, 60 if palette.name == "dark" else 26))
        self.setGraphicsEffect(effect)
        return self


class StatCard(Card):
    """Label / big value / optional caption, used across the dashboard."""

    def __init__(
        self,
        label: str,
        value: str = "-",
        caption: str = "",
        parent: QWidget | None = None,
        small: bool = False,
    ) -> None:
        super().__init__(parent, padding=16, spacing=4)
        self.label = QLabel(label.upper())
        self.label.setObjectName("CardLabel")
        self.value = QLabel(value)
        self.value.setObjectName("CardValueSmall" if small else "CardValue")
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.caption = QLabel(caption)
        self.caption.setObjectName("Faint")
        self.caption.setVisible(bool(caption))
        self.caption.setWordWrap(True)
        self.add(self.label)
        self.add(self.value)
        self.add(self.caption)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_value(self, value: str, caption: str | None = None) -> None:
        """Update the displayed value and optional caption."""
        self.value.setText(value)
        if caption is not None:
            self.caption.setText(caption)
            self.caption.setVisible(bool(caption))


class StatusPill(QLabel):
    """Small coloured status chip."""

    def __init__(self, text: str = "", tone: str = "neutral",
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._palette: Palette | None = None
        self._tone = tone
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(23)
        self.setContentsMargins(0, 0, 0, 0)

    def apply(self, palette: Palette) -> None:
        """Bind the pill to a palette so it can colour itself."""
        self._palette = palette
        self._restyle()

    def set_status(self, text: str, tone: str = "neutral") -> None:
        """Change the label and tone (``neutral|info|success|warning|danger``)."""
        self.setText(f"  {text}  ")
        self._tone = tone
        self._restyle()

    def _restyle(self) -> None:
        if self._palette is None:
            return
        p = self._palette
        colors = {
            "neutral": (p.text_muted, p.surface_alt),
            "info": (p.accent, p.accent_soft),
            "success": (p.success, _tint(p.success, p.name)),
            "warning": (p.warning, _tint(p.warning, p.name)),
            "danger": (p.danger, _tint(p.danger, p.name)),
        }
        fg, bg = colors.get(self._tone, colors["neutral"])
        self.setStyleSheet(
            f"background: {bg}; color: {fg}; border-radius: 11px;"
            f" font-size: 11px; font-weight: 700; padding: 0 4px;"
        )


def _tint(color: str, theme: str) -> str:
    """Low-opacity version of a colour, for pill backgrounds."""
    c = QColor(color)
    c.setAlpha(46 if theme == "dark" else 30)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {c.alpha() / 255:.2f})"


class Divider(QFrame):
    """A one-pixel horizontal rule."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


class PageHeader(QWidget):
    """Title, subtitle and a slot for page-level actions."""

    def __init__(self, title: str, subtitle: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setVisible(bool(subtitle))
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        row.addLayout(text)
        row.addStretch(1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)

    def set_subtitle(self, text: str) -> None:
        """Replace the subtitle line."""
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


class KeyValue(QWidget):
    """A left-aligned label with a right-aligned value."""

    def __init__(self, key: str, value: str = "-", parent: QWidget | None = None,
                 mono: bool = False) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 3, 0, 3)
        row.setSpacing(12)
        self.key = QLabel(key)
        self.key.setObjectName("Muted")
        self.value = QLabel(value)
        self.value.setObjectName("Mono" if mono else "")
        self.value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self.value.font()
        font.setWeight(QFont.Weight.DemiBold)
        self.value.setFont(font)
        row.addWidget(self.key)
        row.addStretch(1)
        row.addWidget(self.value)

    def set_value(self, text: str) -> None:
        """Update the right-hand value."""
        self.value.setText(text)


class Banner(QFrame):
    """Inline notice used for warnings, errors and confirmations."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SubCard")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 11, 14, 11)
        row.setSpacing(10)
        self.text = QLabel()
        self.text.setWordWrap(True)
        row.addWidget(self.text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)
        self.setVisible(False)
        self._palette: Palette | None = None

    def apply(self, palette: Palette) -> None:
        """Bind the banner to a palette."""
        self._palette = palette

    def show_message(self, message: str, tone: str = "info") -> None:
        """Display ``message`` with a tone-appropriate accent."""
        self.text.setText(message)
        self.setVisible(True)
        if self._palette is None:
            return
        p = self._palette
        color = {
            "info": p.accent, "success": p.success,
            "warning": p.warning, "danger": p.danger,
        }.get(tone, p.accent)
        self.setStyleSheet(
            f"QFrame#SubCard {{ border: 1px solid {color};"
            f" border-left: 3px solid {color}; background: {_tint(color, p.name)};"
            f" border-radius: 10px; }}"
        )

    def hide_message(self) -> None:
        """Hide the banner."""
        self.setVisible(False)


def hline(*widgets: QWidget, spacing: int = 10, margins: tuple = (0, 0, 0, 0)) -> QHBoxLayout:
    """Build a horizontal layout containing ``widgets``."""
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for widget in widgets:
        layout.addWidget(widget)
    return layout
