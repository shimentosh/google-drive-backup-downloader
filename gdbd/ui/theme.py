"""Application palette and stylesheet.

One palette dictionary drives both the Qt stylesheet and any custom painting,
so dark and light modes stay consistent without duplicated colour literals.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette


@dataclass(frozen=True)
class Palette:
    """Every colour the interface uses."""

    name: str
    bg: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    on_accent: str
    success: str
    warning: str
    danger: str
    track: str
    sidebar: str
    shadow: str


DARK = Palette(
    name="dark",
    bg="#0f1116",
    surface="#171a21",
    surface_alt="#1e222b",
    surface_hover="#252a35",
    border="#272c37",
    border_strong="#39404f",
    text="#e8ebf2",
    text_muted="#9aa3b7",
    text_faint="#6b7488",
    accent="#4b8bff",
    accent_hover="#5f99ff",
    accent_pressed="#3a77e6",
    accent_soft="#1b2740",
    on_accent="#ffffff",
    success="#2fbf7f",
    warning="#e8a33d",
    danger="#f0554d",
    track="#242935",
    sidebar="#12151b",
    shadow="rgba(0, 0, 0, 0.45)",
)

LIGHT = Palette(
    name="light",
    bg="#f2f4f8",
    surface="#ffffff",
    surface_alt="#f7f9fc",
    surface_hover="#eef1f7",
    border="#e0e5ee",
    border_strong="#c8d0de",
    text="#161a22",
    text_muted="#5c6579",
    text_faint="#8a92a4",
    accent="#2a6df5",
    accent_hover="#3d7cf7",
    accent_pressed="#1f5bd6",
    accent_soft="#e7efff",
    on_accent="#ffffff",
    success="#12a06a",
    warning="#c9820f",
    danger="#d93b32",
    track="#e6eaf2",
    sidebar="#ffffff",
    shadow="rgba(16, 24, 40, 0.10)",
)


def resolve_palette(preference: str) -> Palette:
    """Pick a palette for ``dark`` / ``light`` / ``system``."""
    if preference == "dark":
        return DARK
    if preference == "light":
        return LIGHT
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        try:
            return DARK if scheme() == Qt.ColorScheme.Dark else LIGHT
        except Exception:  # pragma: no cover - older Qt
            pass
    return DARK


def apply_qt_palette(app: QGuiApplication, palette: Palette) -> None:
    """Align Qt's own palette with the stylesheet (menus, tooltips, selections)."""
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, QColor(palette.bg))
    qp.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Base, QColor(palette.surface))
    qp.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.surface_alt))
    qp.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Button, QColor(palette.surface_alt))
    qp.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor(palette.on_accent))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.surface_alt))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette.text_faint))
    app.setPalette(qp)


_FALLBACK_CHECK = ":/qt-project.org/styles/commonstyle/images/standardbutton-apply-16.png"


def _check_mark(color: str) -> str:
    """A crisp tick in ``color`` for checked boxes, cached as a PNG file.

    Stylesheets can only load images from files or resources, so the vector
    icon is rendered once to the temp directory. Falls back to Qt's built-in
    tick when no GUI application exists yet (e.g. pure stylesheet tests).
    """
    if QGuiApplication.instance() is None:
        return _FALLBACK_CHECK
    try:
        import tempfile
        from pathlib import Path

        from .icons import icon

        target = Path(tempfile.gettempdir()) / f"gdbd-check-{color.lstrip('#')}.png"
        if not target.exists():
            icon("check", color, 28).pixmap(28, 28).save(str(target), "PNG")
        return target.as_posix()
    except Exception:  # pragma: no cover - never let decoration break startup
        return _FALLBACK_CHECK


def build_stylesheet(p: Palette) -> str:
    """Render the full application stylesheet for a palette."""
    check = _check_mark(p.on_accent)
    return f"""
* {{
    font-family: "Segoe UI", "Inter", "SF Pro Text", system-ui, sans-serif;
    font-size: 13px;
    color: {p.text};
}}
QWidget#Root, QMainWindow {{ background: {p.bg}; }}

/* ---------- Sidebar ---------- */
QWidget#Sidebar {{
    background: {p.sidebar};
    border-right: 1px solid {p.border};
}}
QLabel#BrandName {{
    font-size: 15px; font-weight: 700; color: {p.text}; letter-spacing: 0.2px;
}}
QLabel#BrandSub {{ font-size: 11px; color: {p.text_faint}; }}
QPushButton#NavButton {{
    background: transparent; border: none; border-radius: 8px;
    padding: 9px 12px; text-align: left; color: {p.text_muted};
    font-size: 13px; font-weight: 500;
}}
QPushButton#NavButton:hover {{ background: {p.surface_hover}; color: {p.text}; }}
QPushButton#NavButton:checked {{
    background: {p.accent_soft}; color: {p.accent}; font-weight: 600;
}}
QLabel#NavSection {{
    color: {p.text_faint}; font-size: 10px; font-weight: 700;
    letter-spacing: 1.1px; padding: 4px 12px;
}}

/* ---------- Cards & surfaces ---------- */
QFrame#Card, QWidget#Card {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QFrame#SubCard {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QFrame#Divider {{ background: {p.border}; border: none; max-height: 1px; }}

QLabel#PageTitle {{ font-size: 21px; font-weight: 700; }}
QLabel#PageSubtitle {{ font-size: 13px; color: {p.text_muted}; }}
QLabel#SectionTitle {{ font-size: 14px; font-weight: 650; }}
QLabel#CardLabel {{
    font-size: 11px; font-weight: 600; color: {p.text_faint}; letter-spacing: 0.6px;
}}
QLabel#CardValue {{ font-size: 24px; font-weight: 700; }}
QLabel#CardValueSmall {{ font-size: 17px; font-weight: 650; }}
QLabel#Muted {{ color: {p.text_muted}; }}
QLabel#Faint {{ color: {p.text_faint}; font-size: 12px; }}
QLabel#Mono {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px; }}
QLabel#Hero {{ font-size: 30px; font-weight: 750; }}
QLabel#HeroSub {{ font-size: 14px; color: {p.text_muted}; }}

/* ---------- Buttons ---------- */
QPushButton {{
    background: {p.surface_alt};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 600;
    color: {p.text};
}}
QPushButton:hover {{ background: {p.surface_hover}; }}
QPushButton:pressed {{ background: {p.border}; }}
QPushButton:disabled {{ color: {p.text_faint}; background: {p.surface}; border-color: {p.border}; }}

QPushButton#Primary {{
    background: {p.accent}; border: 1px solid {p.accent}; color: {p.on_accent};
    padding: 10px 22px; font-size: 13px; font-weight: 650;
}}
QPushButton#Primary:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#Primary:pressed {{ background: {p.accent_pressed}; }}
QPushButton#Primary:disabled {{ background: {p.border}; border-color: {p.border}; color: {p.text_faint}; }}

QPushButton#PrimaryLarge {{
    background: {p.accent}; border: 1px solid {p.accent}; color: {p.on_accent};
    padding: 14px 30px; font-size: 15px; font-weight: 700; border-radius: 10px;
}}
QPushButton#PrimaryLarge:hover {{ background: {p.accent_hover}; }}
QPushButton#PrimaryLarge:disabled {{ background: {p.border}; border-color: {p.border}; color: {p.text_faint}; }}

QPushButton#Danger {{ color: {p.danger}; border-color: {p.border_strong}; }}
QPushButton#Danger:hover {{ background: {p.danger}; color: #fff; border-color: {p.danger}; }}
QPushButton#Ghost {{ background: transparent; border: none; color: {p.accent}; padding: 6px 8px; }}
QPushButton#Ghost:hover {{ background: {p.surface_hover}; }}

/* ---------- Inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {p.surface_alt};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: {p.accent};
    selection-color: {p.on_accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {p.surface_alt}; border: 1px solid {p.border_strong};
    selection-background-color: {p.accent_soft}; selection-color: {p.text};
    outline: none; padding: 4px;
}}
QCheckBox, QRadioButton {{ spacing: 9px; padding: 3px 0; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px; }}
QCheckBox::indicator {{
    border: 1.5px solid {p.border_strong}; border-radius: 5px; background: {p.surface_alt};
}}
QCheckBox::indicator:checked {{
    background: {p.accent}; border-color: {p.accent};
    image: url({check});
}}
QRadioButton::indicator {{
    border: 1.5px solid {p.border_strong}; border-radius: 9px; background: {p.surface_alt};
}}
QRadioButton::indicator:checked {{ border: 5px solid {p.accent}; background: {p.surface}; }}

QSlider::groove:horizontal {{ height: 5px; background: {p.track}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {p.surface}; border: 2px solid {p.accent};
    width: 15px; height: 15px; margin: -6px 0; border-radius: 9px;
}}

/* ---------- Tables & lists ---------- */
QTableWidget, QTreeWidget, QListWidget {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
    gridline-color: {p.border}; outline: none;
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{
    padding: 7px 8px; border-bottom: 1px solid {p.border};
}}
QTableWidget::item:selected, QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {p.accent_soft}; color: {p.text};
}}
QHeaderView::section {{
    background: {p.surface_alt}; color: {p.text_faint};
    border: none; border-bottom: 1px solid {p.border};
    padding: 8px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
}}
QTableCornerButton::section {{ background: {p.surface_alt}; border: none; }}

/* ---------- Scrollbars ---------- */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {p.border_strong}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: {p.border_strong}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- Misc ---------- */
QToolTip {{
    background: {p.surface_alt}; color: {p.text};
    border: 1px solid {p.border_strong}; border-radius: 6px; padding: 6px 8px;
}}
QProgressBar {{
    background: {p.track}; border: none; border-radius: 4px;
    height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
QMessageBox, QDialog {{ background: {p.surface}; }}
QSplitter::handle {{ background: {p.border}; }}
"""
