"""UI smoke tests: every page builds, and state gating disables the right actions.

These run headless with the offscreen Qt platform and never touch the network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gdbd.config.settings import AppContext  # noqa: E402
from gdbd.downloader.manager import ActiveTransfer, BackupOutcome, BackupProgress  # noqa: E402
from gdbd.downloader.worker import TaskResult  # noqa: E402
from gdbd.database.schema import ItemStatus  # noqa: E402
from gdbd.drive.scanner import ScanProgress, ScanResult  # noqa: E402
from gdbd.services.event_bus import AppState  # noqa: E402
from gdbd.ui.theme import DARK, LIGHT, build_stylesheet, resolve_palette  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for the whole test session."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def awake_calls(monkeypatch):
    """Record sleep-policy requests instead of changing this machine's power state."""
    calls: list[bool] = []
    monkeypatch.setattr("gdbd.ui.main_window.keep_awake", calls.append)
    return calls


@pytest.fixture()
def window(qt_app, tmp_path: Path):
    """A fully built main window backed by a throwaway data directory."""
    from gdbd.services.controller import AppController
    from gdbd.ui.main_window import MainWindow

    context = AppContext.create(tmp_path / "data")
    controller = AppController(context)
    win = MainWindow(controller, DARK)
    yield win
    win._animation.stop()
    controller.db.close()


class TestTheme:
    """Both palettes produce a complete stylesheet."""

    @pytest.mark.parametrize("palette", [DARK, LIGHT])
    def test_stylesheet_renders(self, palette):
        css = build_stylesheet(palette)
        assert "QPushButton#Primary" in css
        assert palette.accent in css
        assert "{color}" not in css and "{p." not in css

    def test_resolve_palette_honours_an_explicit_choice(self, qt_app):
        assert resolve_palette("dark") is DARK
        assert resolve_palette("light") is LIGHT

    def test_every_palette_field_is_a_colour(self):
        for palette in (DARK, LIGHT):
            for name, value in vars(palette).items():
                if name == "name":
                    continue
                assert value.startswith(("#", "rgba")), f"{palette.name}.{name}"


class TestPagesBuild:
    """Every page constructs and can be shown."""

    def test_all_pages_exist(self, window):
        assert window.pages.count() == 7

    @pytest.mark.parametrize("index", range(7))
    def test_page_can_be_displayed(self, window, index):
        window.shell.setCurrentIndex(1)
        window._show_page(index)
        assert window.pages.currentIndex() == index

    def test_sidebar_selection_follows_the_page(self, window):
        window.shell.setCurrentIndex(1)
        from gdbd.ui.main_window import PAGE_RUNNING, PAGE_SETUP

        window._show_page(PAGE_RUNNING)
        assert window.nav_buttons[2].isChecked()
        window._show_page(PAGE_SETUP)
        assert window.nav_buttons[2].isChecked()

    def test_welcome_is_shown_while_disconnected(self, window):
        assert window.shell.currentIndex() == 0


class TestStateGating:
    """Invalid actions are disabled for each application state."""

    def test_backup_button_needs_a_scan(self, window):
        window._on_state(AppState.CONNECTED)
        assert not window.dashboard.backup_button.isEnabled()
        window._on_state(AppState.READY)
        assert window.dashboard.backup_button.isEnabled()

    def test_scanning_disables_rescan_and_backup(self, window):
        window._on_state(AppState.SCANNING)
        assert not window.dashboard.refresh_button.isEnabled()
        assert not window.dashboard.backup_button.isEnabled()
        assert not window.dashboard.scan_card.isHidden()

    def test_running_backup_locks_the_account_and_options(self, window):
        window._on_state(AppState.BACKUP_RUNNING)
        assert not window.disconnect_button.isEnabled()
        assert not window.setup_page.rescan_button.isEnabled()
        assert not window.setup_page.workers_slider.isEnabled()
        assert window.backup_page.pause_button.isEnabled()
        assert not window.backup_page.resume_button.isEnabled()

    def test_running_backup_locks_scope_and_settings(self, window):
        window._on_state(AppState.BACKUP_RUNNING)
        assert not window.setup_page.opt_owned_only.isEnabled()
        assert not window.settings_page.browse_button.isEnabled()
        assert not window.settings_page.disconnect_button.isEnabled()
        window._on_state(AppState.READY)
        assert window.settings_page.browse_button.isEnabled()

    def test_pc_is_kept_awake_only_while_downloading(self, window, awake_calls):
        window._on_state(AppState.BACKUP_RUNNING)
        assert awake_calls[-1] is True
        window._on_state(AppState.BACKUP_PAUSED)
        assert awake_calls[-1] is False
        window._on_state(AppState.BACKUP_RUNNING)
        window._on_state(AppState.BACKUP_COMPLETED)
        assert awake_calls[-1] is False

    def test_keep_awake_respects_the_setting(self, window, awake_calls):
        window.controller.settings.update(prevent_sleep=False)
        window._on_state(AppState.BACKUP_RUNNING)
        assert awake_calls[-1] is False

    def test_destination_cannot_change_mid_backup(self, window, monkeypatch):
        monkeypatch.setattr(window.controller, "_state", AppState.BACKUP_RUNNING)
        opened = []
        monkeypatch.setattr("gdbd.ui.main_window.QFileDialog.getExistingDirectory",
                            lambda *a, **k: opened.append(a) or "")
        window._choose_destination()
        assert opened == []

    def test_paused_backup_offers_resume_and_cancel(self, window):
        window._on_state(AppState.BACKUP_PAUSED)
        assert window.backup_page.resume_button.isEnabled()
        assert window.backup_page.cancel_button.isEnabled()
        assert not window.backup_page.pause_button.isEnabled()

    def test_completed_backup_allows_retry(self, window):
        window._on_state(AppState.BACKUP_COMPLETED_WITH_ERRORS)
        assert window.backup_page.retry_button.isEnabled()
        assert not window.backup_page.pause_button.isEnabled()

    def test_disconnected_returns_to_the_welcome_screen(self, window):
        window.shell.setCurrentIndex(1)
        window._on_state(AppState.DISCONNECTED)
        assert window.shell.currentIndex() == 0


class TestProgressRendering:
    """Progress snapshots render without errors and show real numbers."""

    def test_backup_progress_updates_the_page(self, window):
        progress = BackupProgress(
            total_files=12483, finished_files=9421, completed_files=9400,
            skipped_files=21, failed_files=3, unsupported_files=0,
            total_bytes=679_000_000_000, done_bytes=517_000_000_000,
            speed=77_800_000, eta_seconds=4080,
            active=[ActiveTransfer(1, "video_2026.mp4", "Videos/video_2026.mp4",
                                   7_800_000_000, 10_000_000_000, 0.0, 0)],
        )
        window.backup_page.set_progress(progress)
        assert "9,421" in window.backup_page.files_stat.value.text()
        assert "MB/s" in window.backup_page.speed_stat.value.text()
        assert window.backup_page.eta_stat.value.text() == "1h 08m"
        assert window.backup_page.failed_stat.value.text() == "3"
        assert not window.backup_page.panels[0].isHidden()
        assert "video_2026.mp4" in window.backup_page.panels[0].title.text()

    def test_progress_is_byte_based_not_file_based(self):
        progress = BackupProgress(total_files=100, finished_files=90,
                                  total_bytes=1000, done_bytes=250)
        assert progress.byte_fraction == pytest.approx(0.25)

    def test_scan_progress_updates_the_dashboard(self, window):
        window.dashboard.set_scan_progress(
            ScanProgress(files=12483, folders=1274, bytes_total=10**12,
                         current_path="Videos/YouTube/2026", stage="planning")
        )
        text = window.dashboard.scan_counts.text()
        assert "12,483" in text and "1,274" in text
        assert "Videos/YouTube/2026" in window.dashboard.scan_current.text()

    def test_scan_result_fills_the_statistics(self, window):
        result = ScanResult(
            files=12483, folders=1274, total_bytes=735_000_000_000,
            export_files=42, unsupported_files=3,
            category_bytes={"Videos": 442_000_000_000, "Images": 135_000_000_000},
            category_counts={"Videos": 900, "Images": 4000},
        )
        window.dashboard.set_scan_result(result)
        assert window.dashboard.files_card.value.text() == "12,483"
        assert window.dashboard.folders_card.value.text() == "1,274"
        assert "GB" in window.dashboard.size_card.value.text()
        assert not window.dashboard.banner.isHidden()

    def test_outcome_banner(self, window):
        window.backup_page.set_outcome(
            BackupOutcome(completed=12470, skipped=10, failed=3, unsupported=0,
                          bytes_downloaded=10**11, duration=8040),
            "D:/Backup",
        )
        assert not window.backup_page.banner.isHidden()
        assert "12,470" in window.backup_page.banner.text.text()


class TestQueueView:
    """The queue lists active and finished transfers."""

    def _record(self, item_id: int, name: str, status: str):
        from gdbd.database.models import DriveItemRecord

        item = DriveItemRecord(id=item_id, account_id=1, destination="d",
                               drive_file_id=f"f{item_id}", name=name,
                               mime_type="video/mp4", relative_path=name,
                               local_path=name, total_bytes=1000)
        return TaskResult(item, status, 1000)

    def test_active_then_finished(self, window):
        queue = window.backup_page.queue
        queue.reset_queue()
        queue.update_active([ActiveTransfer(1, "a.mp4", "a.mp4", 500, 1000, 0.0, 0)])
        assert queue.rowCount() == 1
        assert "Downloading" in queue.item(0, 0).text()

        queue.mark_finished(self._record(1, "a.mp4", ItemStatus.COMPLETED))
        assert "Completed" in queue.item(0, 0).text()

    def test_failed_rows_are_kept_when_clearing_completed(self, window):
        queue = window.backup_page.queue
        queue.reset_queue()
        queue.mark_finished(self._record(1, "ok.mp4", ItemStatus.COMPLETED))
        queue.mark_finished(self._record(2, "bad.zip", ItemStatus.FAILED))
        assert queue.rowCount() == 2
        queue.clear_completed()
        assert queue.rowCount() == 1
        assert "Failed" in queue.item(0, 0).text()

    def test_filter_hides_other_statuses(self, window):
        queue = window.backup_page.queue
        queue.reset_queue()
        queue.mark_finished(self._record(1, "ok.mp4", ItemStatus.COMPLETED))
        queue.mark_finished(self._record(2, "bad.zip", ItemStatus.FAILED))
        queue.set_filter(ItemStatus.FAILED)
        hidden = [queue.isRowHidden(row) for row in range(queue.rowCount())]
        assert hidden.count(False) == 1


class TestSettingsPage:
    """Settings controls read and write the persisted object."""

    def test_controls_reflect_the_settings(self, window):
        window.controller.settings.update(workers=7, retry_count=9)
        window.settings_page.load()
        assert window.settings_page.workers_spin.value() == 7
        assert window.settings_page.retry_spin.value() == 9

    def test_changing_a_control_persists(self, window):
        window.settings_page.workers_spin.setValue(11)
        assert window.controller.settings.workers == 11
        reloaded = type(window.controller.settings).load(
            window.controller.context.paths.settings_file
        )
        assert reloaded.workers == 11

    def test_export_choices_are_populated(self, window):
        for box in window.settings_page.export_boxes.values():
            assert box.count() >= 2
            assert box.currentData()


class TestWelcomePage:
    """The first-run screen guards against a missing OAuth client."""

    def test_missing_credentials_blocks_connect(self, window):
        window.welcome.set_credentials_missing(True)
        assert not window.welcome.connect_button.isEnabled()
        assert not window.welcome.banner.isHidden()

    def test_present_credentials_enable_connect(self, window):
        window.welcome.set_credentials_missing(False)
        assert window.welcome.connect_button.isEnabled()

    def test_account_summary_appears(self, window):
        window.welcome.show_account("user@example.com", 10**11, 10**12)
        assert not window.welcome.summary.isHidden()
        assert window.welcome.account_row.value.text() == "user@example.com"
