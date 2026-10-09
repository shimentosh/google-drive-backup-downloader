"""Render README / website screenshots of the real UI with sample data.

Never contacts Google. On Windows it uses the native platform plugin (windows
flash briefly on screen) so controls render exactly as users see them::

    python packaging/screenshots.py            # writes docs/assets/screenshots/
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if sys.platform == "win32" else "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gdbd.config.settings import AppContext  # noqa: E402
from gdbd.database.models import Account, DriveItemRecord  # noqa: E402
from gdbd.database.schema import ItemStatus  # noqa: E402
from gdbd.downloader.manager import ActiveTransfer, BackupProgress  # noqa: E402
from gdbd.downloader.worker import TaskResult  # noqa: E402
from gdbd.drive.models import (  # noqa: E402
    CATEGORY_ARCHIVE, CATEGORY_AUDIO, CATEGORY_DOCUMENT, CATEGORY_IMAGE,
    CATEGORY_OTHER, CATEGORY_VIDEO,
)
from gdbd.drive.scanner import ScanResult  # noqa: E402
from gdbd.services.backup_service import BackupPlan  # noqa: E402
from gdbd.services.controller import AppController  # noqa: E402
from gdbd.services.event_bus import AppState  # noqa: E402
from gdbd.ui import main_window as mw  # noqa: E402
from gdbd.ui.theme import DARK, LIGHT, apply_qt_palette, build_stylesheet  # noqa: E402

GB = 1_000_000_000
OUT = ROOT / "docs" / "assets" / "screenshots"
DEST = r"D:\Backups\Google Drive"


def item(i: int, name: str, path: str, size: int) -> DriveItemRecord:
    return DriveItemRecord(id=i, account_id=1, destination=DEST, drive_file_id=f"f{i}",
                           name=name, mime_type="video/mp4", relative_path=path,
                           local_path=f"{DEST}\\{path}", size=size, total_bytes=size)


def build(app: QApplication, palette, data_dir: Path) -> mw.MainWindow:
    apply_qt_palette(app, palette)
    app.setStyleSheet(build_stylesheet(palette))
    mw.keep_awake = lambda _enabled: False
    context = AppContext.create(data_dir)
    context.settings.update(destination=DEST)
    controller = AppController(context)
    win = mw.MainWindow(controller, palette)
    win.resize(1280, 900)
    return win


def snap(app: QApplication, win, name: str) -> None:
    for _ in range(5):
        app.processEvents()
        time.sleep(0.02)
    OUT.mkdir(parents=True, exist_ok=True)
    win.grab().save(str(OUT / f"{name}.png"))
    print("wrote", name)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    account = Account(id=1, email="you@gmail.com", display_name="You",
                      storage_limit=2000 * GB, storage_used=735 * GB)
    scan = ScanResult(
        files=12483, folders=1274, total_bytes=735 * GB, downloadable_files=12438,
        downloadable_bytes=735 * GB, export_files=42, unsupported_files=3,
        category_bytes={CATEGORY_VIDEO: 442 * GB, CATEGORY_IMAGE: 135 * GB,
                        CATEGORY_DOCUMENT: 61 * GB, CATEGORY_AUDIO: 38 * GB,
                        CATEGORY_ARCHIVE: 44 * GB, CATEGORY_OTHER: 15 * GB},
        category_counts={CATEGORY_VIDEO: 912, CATEGORY_IMAGE: 8204,
                         CATEGORY_DOCUMENT: 2610, CATEGORY_AUDIO: 377,
                         CATEGORY_ARCHIVE: 96, CATEGORY_OTHER: 284},
    )

    for palette, suffix in ((DARK, ""), (LIGHT, "-light")):
        with tempfile.TemporaryDirectory() as tmp:
            win = build(app, palette, Path(tmp))
            win.show()

            # Welcome
            win.welcome.set_credentials_missing(False, "1234567890-demo")
            win._on_state(AppState.DISCONNECTED)
            snap(app, win, f"welcome{suffix}")

            # Overview
            win.shell.setCurrentIndex(1)
            win.dashboard.set_account(account)
            win.account_chip.setText(account.email)
            win._on_state(AppState.READY)
            win.dashboard.set_scan_result(scan)
            win.dashboard.set_destination(DEST, 1840 * GB)
            win._show_page(mw.PAGE_OVERVIEW)
            snap(app, win, f"overview{suffix}")

            # Backup setup
            win.pages.setCurrentIndex(mw.PAGE_SETUP)
            win.nav_buttons[2].setChecked(True)
            win.setup_page.set_plan(BackupPlan(
                destination=DEST, total_files=12483, total_folders=1274,
                total_bytes=735 * GB, pending_files=12483, pending_bytes=735 * GB,
                free_space=1840 * GB, export_files=42, unsupported_files=3,
            ))
            win.setup_page.set_state(AppState.READY)
            snap(app, win, f"setup{suffix}")

            # Running backup
            win.backup_page.reset()
            win.backup_page.set_destination(DEST)
            win._on_state(AppState.BACKUP_RUNNING)
            win.pages.setCurrentIndex(mw.PAGE_RUNNING)
            now = time.monotonic()
            active = [
                ActiveTransfer(1, "wedding_highlights_4k.mp4",
                               "Videos/2026/wedding_highlights_4k.mp4",
                               int(7.8 * GB), 10 * GB, now - 120, 0),
                ActiveTransfer(2, "Thesis_final_v3.pdf", "University/Thesis_final_v3.pdf",
                               31_000_000, 48_000_000, now - 2, 0),
                ActiveTransfer(3, "IMG_20260712_181102.jpg", "Photos/IMG_20260712_181102.jpg",
                               2_100_000, 6_400_000, now - 1, 0),
            ]
            for i, (name, path, size, status) in enumerate([
                ("vacation_drone.mov", "Videos/2026/vacation_drone.mov", 3 * GB,
                 ItemStatus.COMPLETED),
                ("Budget 2026.xlsx", "Work/Budget 2026.xlsx", 840_000, ItemStatus.COMPLETED),
                ("podcast_ep42.mp3", "Audio/podcast_ep42.mp3", 61_000_000,
                 ItemStatus.COMPLETED),
                ("Project archive.zip", "Work/Project archive.zip", int(2.4 * GB),
                 ItemStatus.SKIPPED),
                ("Client survey", "Work/Client survey", 0, ItemStatus.UNSUPPORTED),
            ], start=10):
                win.backup_page.add_result(TaskResult(item(i, name, path, size), status, size))
            progress = BackupProgress(
                total_files=12483, finished_files=9421, completed_files=9400,
                skipped_files=21, failed_files=0, unsupported_files=3,
                total_bytes=735 * GB, done_bytes=559 * GB, speed=77_800_000,
                eta_seconds=2280, active=active,
            )
            win.backup_page.set_progress(progress)
            win._on_backup_progress(progress)
            snap(app, win, f"backup{suffix}")

            # Settings
            win._on_state(AppState.READY)
            win._show_page(mw.PAGE_SETTINGS)
            win.settings_page.set_account(account)
            snap(app, win, f"settings{suffix}")

            win._animation.stop()
            win.controller.db.close()
            win.close()


def social_card() -> None:
    """1200x630 Open Graph / GitHub social preview image."""
    card = QPixmap(1200, 630)
    painter = QPainter(card)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    bg = QLinearGradient(0, 0, 1200, 630)
    bg.setColorAt(0, QColor("#0b0f19"))
    bg.setColorAt(1, QColor("#14213d"))
    painter.fillRect(card.rect(), bg)

    shot = QPixmap(str(OUT / "backup.png"))
    target = QRectF(560, 120, 760, 760 * shot.height() / shot.width())
    clip = QPainterPath()
    clip.addRoundedRect(target, 18, 18)
    painter.setClipPath(clip)
    painter.drawPixmap(target, shot, QRectF(shot.rect()))
    painter.setClipping(False)

    painter.setPen(QColor("#4b8bff"))
    painter.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
    painter.drawText(QRectF(64, 92, 480, 30), "FREE · OPEN SOURCE · WINDOWS")
    painter.setPen(QColor("#ffffff"))
    painter.setFont(QFont("Segoe UI", 44, QFont.Weight.Bold))
    painter.drawText(QRectF(64, 132, 480, 240), Qt.TextFlag.TextWordWrap,
                     "Back up your entire Google Drive")
    painter.setPen(QColor("#9aa3b7"))
    painter.setFont(QFont("Segoe UI", 17))
    painter.drawText(QRectF(64, 390, 470, 120), Qt.TextFlag.TextWordWrap,
                     "Folder structure preserved, resumable downloads, "
                     "read-only access. Nothing uploaded anywhere.")
    painter.setPen(QColor("#6b7488"))
    painter.setFont(QFont("Segoe UI", 14))
    painter.drawText(QRectF(64, 548, 480, 30), "by shimanto.xyz")
    painter.end()
    card.save(str(OUT.parent / "social-card.png"))
    print("wrote social-card")


if __name__ == "__main__":
    main()
    social_card()
