# Changelog

## 1.2.0 (2026-10-09)

### Added
- **Guided first-run setup.** The welcome screen walks through the one-time Google setup in 5 numbered steps, each with an **Open** button that goes straight to the right Google Cloud page and a **Show me** link to the illustrated guide.
- **Illustrated setup guide** on the website in English and বাংলা, with troubleshooting for every common Google error.
- While the browser is open for sign-in, the app explains what Google will show ("Google hasn't verified this app" → Continue, then tick the Drive permission).

### Changed
- Setup instructions follow Google's current console (Google Auth platform → Branding, Audience, Clients).
- Sign-in and scan errors are rewritten into plain fixes: not a test user (Error 403), wrong client type (Error 400 redirect_uri_mismatch), Drive API turned off, and the weekly sign-out caused by Google's 7-day limit for apps in Testing mode (with the "Publish app" fix).

## 1.1.0 (2026-10-09)

### Added
- **Keeps your PC awake** while a backup is downloading, so Windows sleep no longer interrupts long runs (Settings → *While a backup runs*). A paused backup lets the PC sleep normally.
- **Desktop notification** and taskbar flash when a backup finishes while the window is in the background.
- **Live progress on the taskbar**: the window title shows the percentage backed up.
- **About** section in Settings with the version, update check, website, source code and issue links.
- **Restart to apply** button after changing the theme.
- Setup help links straight to the online guide and the Google Cloud Console.
- Website, screenshots, MIT license, issue templates and automated Windows releases.

### Fixed
- The *Backup* page could clip its statistics and file names on shorter windows; it now scrolls like every other page.
- The *Backup* page header said "Idle" during a running backup; it now shows the destination folder.
- *Only files I own* stayed clickable during a backup and could trigger a rescan mid-run.
- Changing the destination from Settings skipped the rescan and was allowed during a running backup.
- *Reconnect* and *Disconnect* in Settings were available mid-backup.
- Checkbox ticks were hard to see; they are now crisp white checks.
- The disabled resume button read "Run again" during a running backup.
- Warnings in the status bar now stay on screen longer than info messages.

## 1.0.0

- First release: full-Drive backup with preserved folder structure, resumable streaming downloads, MD5 verification, incremental re-runs, Google Workspace export, shared drives, dark and light themes.
