"""Progress visualisation widgets."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ...utils.formatting import format_bytes, format_duration, format_speed
from ..theme import Palette


class ProgressBar(QWidget):
    """A rounded progress bar that can also render an indeterminate pulse."""

    def __init__(self, parent: QWidget | None = None, height: int = 9,
                 palette: Palette | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._indeterminate = False
        self._phase = 0.0
        self._palette = palette
        self._color: str | None = None
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def apply(self, palette: Palette) -> None:
        """Bind the bar to a palette."""
        self._palette = palette
        self.update()

    def set_color(self, color: str | None) -> None:
        """Override the fill colour (used for warning/error states)."""
        self._color = color
        self.update()

    def set_fraction(self, value: float) -> None:
        """Set progress in the range ``0.0`` to ``1.0``."""
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._value) < 0.0005 and not self._indeterminate:
            return
        self._value = value
        self._indeterminate = False
        self.update()

    def set_indeterminate(self, on: bool = True) -> None:
        """Switch to (or away from) the animated indeterminate style."""
        if self._indeterminate == on:
            return
        self._indeterminate = on
        self.update()

    def advance(self) -> None:
        """Step the indeterminate animation; call from a timer."""
        if self._indeterminate:
            self._phase = (self._phase + 0.022) % 1.0
            self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        p = self._palette
        track = QColor(p.track if p else "#2a2f3a")
        fill = QColor(self._color or (p.accent if p else "#4b8bff"))
        radius = self.height() / 2.0
        rect = QRectF(0, 0, self.width(), self.height())

        painter.setBrush(QBrush(track))
        painter.drawRoundedRect(rect, radius, radius)

        if self._indeterminate:
            span = max(60.0, self.width() * 0.28)
            travel = self.width() + span
            x = self._phase * travel - span
            bar = QRectF(max(0.0, x), 0,
                         min(span, self.width() - max(0.0, x)), self.height())
            if bar.width() > 1:
                painter.setBrush(QBrush(fill))
                painter.drawRoundedRect(bar, radius, radius)
        elif self._value > 0:
            width = max(self.height(), self.width() * self._value)
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(QRectF(0, 0, width, self.height()), radius, radius)
        painter.end()


class Ring(QWidget):
    """A circular percentage indicator for the backup headline."""

    def __init__(self, parent: QWidget | None = None, size: int = 132,
                 palette: Palette | None = None) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._palette = palette
        self._caption = ""
        self.setFixedSize(size, size)

    def apply(self, palette: Palette) -> None:
        """Bind the ring to a palette."""
        self._palette = palette
        self.update()

    def set_fraction(self, value: float, caption: str = "") -> None:
        """Set the filled fraction and the caption under the percentage."""
        self._value = max(0.0, min(1.0, float(value)))
        self._caption = caption
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802 - Qt override
        p = self._palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        thickness = 10
        rect = QRectF(thickness / 2, thickness / 2,
                      self.width() - thickness, self.height() - thickness)

        pen = QPen(QColor(p.track if p else "#2a2f3a"), thickness)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)

        if self._value > 0:
            pen.setColor(QColor(p.accent if p else "#4b8bff"))
            painter.setPen(pen)
            painter.drawArc(rect, 90 * 16, int(-360 * 16 * self._value))

        # The percentage sits just above centre so the caption below it has room;
        # both are positioned relative to the widget so any ring size works.
        caption_gap = 11 if self._caption else 0
        painter.setPen(QColor(p.text if p else "#e8ebf2"))
        font = painter.font()
        font.setPointSize(max(13, int(self.height() * 0.16)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(0, -caption_gap, 0, -caption_gap),
            Qt.AlignmentFlag.AlignCenter, f"{self._value * 100:.0f}%",
        )
        if self._caption:
            painter.setPen(QColor(p.text_faint if p else "#6b7488"))
            font.setPointSize(9)
            font.setWeight(QFont.Weight.Medium)
            painter.setFont(font)
            painter.drawText(
                self.rect().adjusted(8, self.height() // 2 + 6, -8, 0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                self._caption,
            )
        painter.end()


class TransferPanel(QWidget):
    """File name, bar and the bytes/speed/ETA line for one transfer."""

    def __init__(self, parent: QWidget | None = None, big: bool = False) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title = QLabel("-")
        self.title.setObjectName("SectionTitle" if big else "")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.percent = QLabel("0%")
        self.percent.setObjectName("Muted")
        top.addWidget(self.title, 1)
        top.addWidget(self.percent)
        layout.addLayout(top)

        self.bar = ProgressBar(height=10 if big else 7)
        layout.addWidget(self.bar)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        self.bytes_label = QLabel("-")
        self.bytes_label.setObjectName("Faint")
        self.rate_label = QLabel("")
        self.rate_label.setObjectName("Faint")
        self.rate_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        bottom.addWidget(self.bytes_label, 1)
        bottom.addWidget(self.rate_label)
        layout.addLayout(bottom)

    def apply(self, palette: Palette) -> None:
        """Bind the inner bar to a palette."""
        self.bar.apply(palette)

    def update_transfer(
        self,
        name: str,
        done: int,
        total: int,
        speed: float | None = None,
        eta: float | None = None,
        indeterminate: bool = False,
    ) -> None:
        """Refresh every element from one transfer's numbers."""
        self.title.setText(name or "-")
        if indeterminate or total <= 0:
            self.bar.set_indeterminate(True)
            self.percent.setText("")
            self.bytes_label.setText(format_bytes(done) if done else "-")
        else:
            fraction = done / total
            self.bar.set_indeterminate(False)
            self.bar.set_fraction(fraction)
            self.percent.setText(f"{fraction * 100:.0f}%")
            self.bytes_label.setText(f"{format_bytes(done)} / {format_bytes(total)}")

        parts = []
        if speed:
            parts.append(format_speed(speed))
        if eta:
            parts.append(f"ETA {format_duration(eta)}")
        self.rate_label.setText("   ".join(parts))

    def clear(self) -> None:
        """Reset to the idle state."""
        self.title.setText("-")
        self.percent.setText("")
        self.bytes_label.setText("-")
        self.rate_label.setText("")
        self.bar.set_indeterminate(False)
        self.bar.set_fraction(0)
