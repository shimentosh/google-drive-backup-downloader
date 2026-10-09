# Contributing

Thanks for helping improve Google Drive Backup Downloader!

## Development setup

```powershell
git clone https://github.com/shimentosh/google-drive-backup-downloader.git
cd google-drive-backup-downloader
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python app.py --debug
```

## Before opening a pull request

- Run `pytest`. The suite needs no Google credentials; the Drive API is faked in-process.
- Keep the Qt main thread free of network or disk work; use the controller's background tasks.
- Match the existing style: type hints, short docstrings, no new runtime dependencies without discussion.
- If you change the UI, regenerate the screenshots with `python packaging/screenshots.py`.

## Releasing

Bump `APP_VERSION` in `gdbd/constants.py`, add an entry to `CHANGELOG.md`, then:

```powershell
git tag v1.2.0
git push origin v1.2.0
```

The **Release** workflow runs the tests, builds the portable `.exe` and the folder `.zip`, and publishes them.
