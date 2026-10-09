"""Vector icons rendered from inline SVG.

Keeping the icon set in-process means no image files to ship, no network
dependency, and icons that recolour themselves for the active theme.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap

try:  # pragma: no cover - QtSvg ships with PySide6-Addons
    from PySide6.QtSvg import QSvgRenderer

    _SVG_AVAILABLE = True
except Exception:  # pragma: no cover
    QSvgRenderer = None  # type: ignore[assignment]
    _SVG_AVAILABLE = False

_STROKE = (
    'fill="none" stroke="{color}" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round"'
)

#: 24x24 icon bodies. ``{color}`` is substituted at render time.
_PATHS: dict[str, str] = {
    "overview": '<rect x="3" y="3" width="7.5" height="7.5" rx="2"/>'
                '<rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/>'
                '<rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/>'
                '<rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/>',
    "drive": '<path d="M7.7 3.5h8.6l5 8.7-4.3 7.6H6.9l-4.3-7.6z"/>'
             '<path d="M7.7 3.5 3 11.9M16.3 3.5l4.3 8.4M6.9 19.8l4.4-7.9h9.3"/>',
    "backup": '<path d="M12 3.5v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/>'
              '<path d="M4 16.5v2.2a1.8 1.8 0 0 0 1.8 1.8h12.4a1.8 1.8 0 0 0 1.8-1.8v-2.2"/>',
    "history": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.2V12l3.2 2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/>'
                '<path d="M19.4 14.5a1.6 1.6 0 0 0 .32 1.77l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.6 1.6 0 0 0-1.77-.32 1.6 1.6 0 0 0-.97 1.47V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1.05-1.47 1.6 1.6 0 0 0-1.77.32l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.6 1.6 0 0 0 .32-1.77 1.6 1.6 0 0 0-1.47-.97H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.47-1.05 1.6 1.6 0 0 0-.32-1.77l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.6 1.6 0 0 0 1.77.32H9a1.6 1.6 0 0 0 .97-1.47V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 .97 1.47 1.6 1.6 0 0 0 1.77-.32l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.6 1.6 0 0 0-.32 1.77V9a1.6 1.6 0 0 0 1.47.97H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.47.97z"/>',
    "logs": '<path d="M5 4.5h14M5 9.5h14M5 14.5h9M5 19.5h9"/>',
    "check": '<path d="m4.5 12.5 5 5 10-11"/>',
    "cross": '<path d="M6 6l12 12M18 6 6 18"/>',
    "pause": '<rect x="6.5" y="4.5" width="4" height="15" rx="1.3"/>'
             '<rect x="13.5" y="4.5" width="4" height="15" rx="1.3"/>',
    "play": '<path d="M7 4.8 19 12 7 19.2z"/>',
    "stop": '<rect x="5.5" y="5.5" width="13" height="13" rx="2.5"/>',
    "folder": '<path d="M3.5 6.5a2 2 0 0 1 2-2h3.4l2 2.5h7.6a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-13.7-5.2L3.5 8.5"/><path d="M3.5 4v4.5H8"/>'
               '<path d="M4 13a8 8 0 0 0 13.7 5.2l2.8-2.7"/><path d="M20.5 20v-4.5H16"/>',
    "warning": '<path d="M12 3.8 21 19.5H3z"/><path d="M12 9.5v4.2M12 16.8v.1"/>',
    "cloud": '<path d="M7 18.5h10.2a4.3 4.3 0 0 0 .6-8.6 6 6 0 0 0-11.6-1A4.3 4.3 0 0 0 7 18.5z"/>',
    "shield": '<path d="M12 3.2 4.8 6v6.1c0 4.3 3 8.1 7.2 9.2 4.2-1.1 7.2-4.9 7.2-9.2V6z"/>'
              '<path d="m9.2 12.2 2 2 3.6-4"/>',
    "link": '<path d="M10 13.5a4 4 0 0 0 5.7 0l2.6-2.6a4 4 0 0 0-5.7-5.7l-1.5 1.5"/>'
            '<path d="M14 10.5a4 4 0 0 0-5.7 0l-2.6 2.6a4 4 0 0 0 5.7 5.7l1.5-1.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "external": '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5 11 13"/>'
                '<path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
}


@lru_cache(maxsize=256)
def _pixmap(name: str, color: str, size: int, ratio: int) -> QPixmap:
    body = _PATHS.get(name)
    # Painted at full device resolution first; the device-pixel ratio is applied
    # afterwards so QPainter does not scale the SVG a second time.
    px = QPixmap(size * ratio, size * ratio)
    px.fill(Qt.GlobalColor.transparent)
    if not body or not _SVG_AVAILABLE:
        px.setDevicePixelRatio(ratio)
        return px
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'width="{size * ratio}" height="{size * ratio}" '
        f'{_STROKE.format(color=color)}>{body}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    px.setDevicePixelRatio(ratio)
    return px


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """Return a themed :class:`QIcon` for one of the built-in glyphs."""
    return QIcon(_pixmap(name, color, size, 2))


def icon_size(size: int = 18) -> QSize:
    """Convenience :class:`QSize` matching :func:`icon`."""
    return QSize(size, size)


def available() -> list[str]:
    """Names of every built-in icon."""
    return sorted(_PATHS)
