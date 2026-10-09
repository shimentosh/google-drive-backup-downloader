# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Google Drive Backup Downloader.

Build with ``packaging\\build_exe.ps1`` (recommended) or directly::

    pyinstaller --noconfirm --clean packaging/drive_backup.spec

Set ``GDBD_ONEFILE=1`` to produce a single executable instead of a folder.

No secrets are bundled: ``credentials.json`` is deliberately NOT collected, so
the executable can be shared without leaking an OAuth client.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821 - injected by PyInstaller
ONEFILE = os.environ.get("GDBD_ONEFILE", "0") == "1"

APP_NAME = "GoogleDriveBackupDownloader"

# googleapiclient loads its discovery documents and several transports lazily,
# so they have to be named explicitly.
hidden = [
    "googleapiclient.discovery",
    "googleapiclient.discovery_cache",
    "googleapiclient.discovery_cache.file_cache",
    "google.auth.transport.requests",
    "google_auth_httplib2",
    "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "keyring.backends.Windows",
    "keyring.backends.chainer",
    "keyring.backends.fail",
]
hidden += collect_submodules("gdbd")

# The static Drive API discovery document ships inside googleapiclient.
datas = []
try:
    import googleapiclient

    discovery = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
    if discovery.is_dir():
        datas.append((str(discovery), "googleapiclient/discovery_cache/documents"))
except Exception:  # pragma: no cover - handled by PyInstaller's own hooks
    pass

assets = ROOT / "assets"
if assets.is_dir():
    datas.append((str(assets), "assets"))

icon_path = assets / "app.ico"
icon = str(icon_path) if icon_path.exists() else None

a = Analysis(                                    # noqa: F821
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trimming Qt modules the app never loads keeps the bundle well under 200 MB.
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.Qt3DCore",
        "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSensors",
        "PySide6.QtSerialPort", "PySide6.QtDesigner", "PySide6.QtTest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)                                # noqa: F821

if ONEFILE:
    exe = EXE(                                   # noqa: F821
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,                           # windowed desktop app
        icon=icon,
    )
else:
    exe = EXE(                                   # noqa: F821
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        icon=icon,
    )
    coll = COLLECT(                              # noqa: F821
        exe, a.binaries, a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
