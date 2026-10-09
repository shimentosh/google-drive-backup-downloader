"""Google Drive Backup Downloader - application entry point.

Run with::

    python app.py

Optional flags::

    --data-dir PATH   use a different database/settings/log directory
    --debug           verbose logging
    --version         print the version and exit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make ``python app.py`` work from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gdbd.constants import APP_NAME, APP_SHORT_NAME, APP_VERSION, ORG_NAME  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(prog="drive-backup", description=APP_NAME)
    parser.add_argument("--data-dir", default=None,
                        help="directory for the database, settings and logs")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {APP_VERSION}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Start the desktop application and run the Qt event loop."""
    args = parse_args(argv)

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from gdbd.config.settings import AppContext, asset_path
    from gdbd.services.controller import AppController
    from gdbd.ui.main_window import MainWindow
    from gdbd.ui.theme import apply_qt_palette, build_stylesheet, resolve_palette
    from gdbd.utils.logging import configure_logging, get_logger

    context = AppContext.create(Path(args.data_dir) if args.data_dir else None)
    log_path = configure_logging(
        context.paths.logs, debug=args.debug or context.settings.debug_logging
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    # No setApplicationDisplayName: Qt would append it to every window title,
    # which duplicates the name the main window already sets.
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName(APP_SHORT_NAME)

    icon_file = asset_path("app.ico")
    if icon_file is not None:
        app.setWindowIcon(QIcon(str(icon_file)))

    log = get_logger("gdbd.app")
    log.info("%s %s starting (data dir: %s)", APP_NAME, APP_VERSION, context.paths.data_dir)
    log.info("Application log: %s", log_path)

    palette = resolve_palette(context.settings.theme)
    apply_qt_palette(app, palette)
    app.setStyleSheet(build_stylesheet(palette))

    controller = AppController(context)
    window = MainWindow(controller, palette)
    window.show()

    # Reconnect silently if a previous session stored credentials.
    controller.restore_session()

    exit_code = app.exec()
    log.info("Application exited with code %s", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
