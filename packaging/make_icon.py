"""Generate ``assets/app.ico`` from the application's built-in shield glyph.

Run once (or after changing the glyph); the result is committed so a packaging
build needs no extra tooling::

    python packaging/make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: Sizes Windows picks between for the taskbar, Explorer and Alt-Tab.
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png(size: int, color: str, background: str) -> bytes:
    """Render the shield glyph to a PNG at ``size`` pixels."""
    from PySide6.QtCore import QBuffer, QByteArray, Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    from gdbd.ui.icons import _PATHS, _STROKE

    body = _PATHS["shield"]
    # Stroke weight is scaled down at large sizes so the glyph stays elegant.
    stroke = max(1.2, 1.7 * (48 / size) ** 0.35)
    # Drop the default stroke width: a second stroke-width attribute makes the
    # SVG invalid, and Qt then renders an empty glyph.
    attrs = _STROKE.format(color=color).replace('stroke-width="1.7" ', "")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'width="{size}" height="{size}" {attrs} '
        f'stroke-width="{stroke:.2f}">{body}</svg>'
    )

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # Rounded background plate so the icon reads on any desktop wallpaper.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(background))
    radius = size * 0.22
    painter.drawRoundedRect(0, 0, size, size, radius, radius)

    inset = size * 0.16
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(
        painter, __import__("PySide6.QtCore", fromlist=["QRectF"]).QRectF(
            inset, inset, size - 2 * inset, size - 2 * inset
        )
    )
    painter.end()

    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


def build_ico(pngs: dict[int, bytes]) -> bytes:
    """Pack PNG images into a Windows .ico container."""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)          # reserved, type=icon, count
    entries = b""
    payload = b""
    offset = 6 + 16 * count
    for size in sorted(pngs):
        data = pngs[size]
        dimension = 0 if size >= 256 else size          # 0 means 256 in the ICO format
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset
        )
        payload += data
        offset += len(data)
    return header + entries + payload


def main() -> int:
    """Write ``assets/app.ico``."""
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    pngs = {size: render_png(size, "#ffffff", "#4b8bff") for size in SIZES}
    target = ROOT / "assets" / "app.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_ico(pngs))
    print(f"Wrote {target} ({target.stat().st_size:,} bytes, "
          f"{len(SIZES)} sizes)")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
