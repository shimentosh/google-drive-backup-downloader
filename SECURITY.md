# Security policy

Google Drive Backup Downloader handles OAuth tokens for your Google account, so
security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://github.com/shimentosh/google-drive-backup-downloader/security/advisories/new)
instead, or contact the author via <https://shimanto.xyz>.

## What the app does to protect you

- Requests only the read-only `drive.readonly` scope, so it cannot change or delete anything in your Drive.
- Never sees your Google password; sign-in happens in your browser on Google's own site.
- Stores the refresh token in Windows Credential Manager (or a user-restricted file as a fallback).
- Redacts tokens and secrets from every log line.
- Talks to no server other than Google's API. No telemetry, no analytics.

Never post `credentials.json`, `token.json`, client secrets or log files containing them in an issue.
