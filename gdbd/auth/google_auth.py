"""Google OAuth 2.0 for an installed desktop application.

Flow: the user clicks *Connect Google Drive*, the system browser opens Google's
consent screen, and Google redirects back to a loopback HTTP server this
process starts on an ephemeral port. The application never sees the password.

Only ``drive.readonly`` is requested - the narrowest scope that can enumerate
and download an entire Drive. (``drive.file`` cannot see pre-existing files, and
``drive.metadata.readonly`` cannot download content, so neither is sufficient.)
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..config.settings import AppPaths, client_config_from_env, credentials_search_paths
from ..constants import APP_NAME, DRIVE_SCOPES
from ..utils.logging import get_logger
from .token_manager import TokenManager

log = get_logger(__name__)


class AuthError(Exception):
    """Authentication failed in a way the user must act on."""


class CredentialsNotConfigured(AuthError):
    """No OAuth client is configured, so the flow cannot even start."""


@dataclass(frozen=True)
class AuthResult:
    """Outcome of a successful authentication."""

    credentials: Credentials
    email: str
    display_name: str | None = None


def find_client_config(paths: AppPaths, explicit: str = "") -> tuple[dict, str]:
    """Locate the OAuth client configuration.

    Returns ``(client_config, source_description)``. Raises
    :class:`CredentialsNotConfigured` when neither a ``credentials.json`` file
    nor the environment variables are present.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return _read_client_file(candidate), str(candidate)
        raise CredentialsNotConfigured(
            f"The configured OAuth client file was not found:\n{candidate}"
        )

    env_config = client_config_from_env()
    if env_config is not None:
        return env_config, "environment variables (GDBD_CLIENT_ID / GDBD_CLIENT_SECRET)"

    for candidate in credentials_search_paths(paths):
        if candidate.is_file():
            return _read_client_file(candidate), str(candidate)

    searched = "\n".join(f"  - {p}" for p in credentials_search_paths(paths))
    raise CredentialsNotConfigured(
        "No Google OAuth client was found.\n\n"
        "Create a Desktop-app OAuth client in the Google Cloud Console, download "
        "credentials.json and place it in one of these locations:\n"
        f"{searched}\n\n"
        "Alternatively set the GDBD_CLIENT_ID and GDBD_CLIENT_SECRET environment "
        "variables."
    )


def _read_client_file(path: Path) -> dict:
    """Parse and validate a downloaded ``credentials.json``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialsNotConfigured(f"Could not read {path}: {exc}") from exc
    if "installed" not in data and "web" not in data:
        raise CredentialsNotConfigured(
            f"{path} is not an OAuth client file. Download the JSON for a "
            '"Desktop app" OAuth client from the Google Cloud Console.'
        )
    if "web" in data and "installed" not in data:
        raise CredentialsNotConfigured(
            f"{path} contains a Web-application OAuth client. This app needs a "
            '"Desktop app" client, which supports the loopback redirect it uses.'
        )
    return data


def client_config_from_pair(client_id: str, client_secret: str) -> dict:
    """Build a Desktop-app client config from a pasted client ID and secret."""
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise CredentialsNotConfigured(
            "That does not look like an OAuth Client ID. It should end with "
            ".apps.googleusercontent.com"
        )
    if not client_secret:
        raise CredentialsNotConfigured("The Client secret is empty.")
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


def install_client_config(paths: AppPaths, source: Path | dict) -> Path:
    """Validate an OAuth client and save it as the app's ``credentials.json``.

    ``source`` is either a downloaded client file or an already-built config.
    The copy lives in the data directory, so it is found on every start no
    matter where the original file was.
    """
    data = _read_client_file(Path(source)) if not isinstance(source, dict) else source
    target = paths.default_credentials
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Saved OAuth client configuration to %s", target)
    return target


class GoogleAuthenticator:
    """Owns credential acquisition, refresh, persistence and revocation."""

    def __init__(self, paths: AppPaths, credentials_path: str = "") -> None:
        self.paths = paths
        self.credentials_path = credentials_path
        self.tokens = TokenManager(paths.tokens)
        self._lock = threading.RLock()
        self._credentials: Credentials | None = None

    # -- state -------------------------------------------------------------
    @property
    def credentials(self) -> Credentials | None:
        """The in-memory credentials, if the user is connected."""
        return self._credentials

    def is_connected(self) -> bool:
        """True when usable credentials are loaded."""
        creds = self._credentials
        return bool(creds and (creds.valid or creds.refresh_token))

    def has_stored_credentials(self) -> bool:
        """True when a previous session saved credentials."""
        return self.tokens.has_credentials()

    def client_configured(self) -> bool:
        """True when an OAuth client file or environment pair is available."""
        try:
            find_client_config(self.paths, self.credentials_path)
            return True
        except CredentialsNotConfigured:
            return False

    def client_id(self) -> str:
        """The configured OAuth Client ID, or ``""`` when none is configured."""
        try:
            config, _ = find_client_config(self.paths, self.credentials_path)
        except CredentialsNotConfigured:
            return ""
        return config.get("installed", {}).get("client_id", "")

    # -- acquisition -------------------------------------------------------
    def load_stored(self) -> Credentials | None:
        """Load and, if needed, refresh previously stored credentials.

        Returns ``None`` when nothing is stored or the stored grant is no longer
        valid (revoked, expired refresh token, changed scopes).
        """
        with self._lock:
            creds = self.tokens.load(DRIVE_SCOPES)
            if creds is None:
                return None
            if creds.valid:
                self._credentials = creds
                return creds
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError as exc:
                    log.warning("Stored credentials could not be refreshed: %s",
                                type(exc).__name__)
                    self.tokens.clear()
                    return None
                except Exception as exc:  # network problems are not fatal here
                    log.warning("Token refresh failed (%s); will retry on next use",
                                type(exc).__name__)
                    self._credentials = creds
                    return creds
                self.tokens.save(creds)
                self._credentials = creds
                return creds
            return None

    def authorize_interactive(self, timeout: int = 300) -> Credentials:
        """Run the browser consent flow and return fresh credentials.

        Blocks the calling thread until the user finishes or the local server
        times out, so call it from a worker thread, never the Qt main thread.
        """
        client_config, source = find_client_config(self.paths, self.credentials_path)
        log.info("Starting OAuth consent flow using client from %s", source)
        flow = InstalledAppFlow.from_client_config(client_config, DRIVE_SCOPES)
        try:
            creds = flow.run_local_server(
                port=0,
                open_browser=True,
                timeout_seconds=timeout,
                authorization_prompt_message="",
                success_message=(
                    f"{APP_NAME} is connected. You can close this tab and return "
                    "to the application."
                ),
                # Guarantees a refresh token even on repeat authorisations.
                access_type="offline",
                prompt="consent",
            )
        except Exception as exc:
            raise AuthError(_friendly_auth_error(exc)) from exc
        if creds is None:
            raise AuthError("Authorisation was not completed.")
        with self._lock:
            self._credentials = creds
            self.tokens.save(creds)
        log.info("OAuth consent completed successfully")
        return creds

    def ensure_valid(self) -> Credentials:
        """Return valid credentials, refreshing them if they have expired."""
        with self._lock:
            creds = self._credentials
            if creds is None:
                raise AuthError("Not connected to Google Drive.")
            if creds.valid:
                return creds
            if not creds.refresh_token:
                raise AuthError(
                    "The Google session expired and cannot be renewed. "
                    "Reconnect your account."
                )
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                self.tokens.clear()
                self._credentials = None
                raise AuthError(
                    "Google rejected the saved authorisation (it may have been "
                    "revoked). Reconnect your account."
                ) from exc
            self.tokens.save(creds)
            return creds

    # -- teardown ----------------------------------------------------------
    def disconnect(self, revoke: bool = True) -> None:
        """Forget the account locally and, when possible, revoke the grant."""
        with self._lock:
            creds = self._credentials
            self._credentials = None
        if revoke and creds is not None and creds.token:
            try:
                import requests

                requests.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": creds.token},
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    timeout=10,
                )
                log.info("Revoked Google authorisation")
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("Could not revoke token remotely: %s", type(exc).__name__)
        self.tokens.clear()
        log.info("Disconnected Google account")


def _friendly_auth_error(exc: Exception) -> str:
    """Translate an OAuth library exception into something actionable."""
    text = str(exc)
    lowered = text.lower()
    if "access_denied" in lowered:
        return (
            "Access was denied on the Google consent screen. If your OAuth "
            "consent screen is in Testing mode, add your Google account as a "
            "test user, then try again."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return "The sign-in window timed out before consent was granted."
    if "redirect_uri_mismatch" in lowered:
        return (
            "Google rejected the redirect URI. Make sure the OAuth client is of "
            'type "Desktop app".'
        )
    if "address already in use" in lowered:
        return "Could not start the local sign-in listener; another app is using the port."
    return f"Google sign-in failed: {text}"
