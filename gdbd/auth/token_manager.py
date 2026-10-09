"""Persistence for OAuth credentials.

The refresh token is the one long-lived secret this application holds, so it is
stored in the Windows Credential Manager (via ``keyring``) when that is
available, and in a permission-restricted file otherwise. Tokens are never
logged, never shown in the UI, and never leave the machine except in the
requests the Google client libraries make to Google's own token endpoint.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials

from ..constants import APP_SHORT_NAME
from ..utils.logging import get_logger

log = get_logger(__name__)

_KEYRING_SERVICE = f"{APP_SHORT_NAME}:oauth"
_KEYRING_USER = "default"

try:  # pragma: no cover - availability depends on the platform
    import keyring

    _KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover
    keyring = None  # type: ignore[assignment]
    _KEYRING_AVAILABLE = False


def _restrict_permissions(path: Path) -> None:
    """Limit a file to the current user only.

    On Windows this uses ``icacls`` because POSIX mode bits do not express ACLs;
    on other platforms it is a plain ``chmod 600``.
    """
    try:
        if sys.platform.startswith("win"):
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    check=False,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        else:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:  # pragma: no cover - best effort hardening
        log.debug("Could not restrict permissions on token store: %s", exc)


class TokenManager:
    """Loads, saves and clears the stored OAuth credentials."""

    def __init__(self, token_path: Path, use_keyring: bool = True) -> None:
        self.token_path = Path(token_path)
        self.use_keyring = use_keyring and _KEYRING_AVAILABLE

    # -- storage backends --------------------------------------------------
    def _keyring_load(self) -> str | None:
        if not self.use_keyring:
            return None
        try:
            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - backend dependent
            log.debug("Keyring read unavailable, falling back to file: %s", exc)
            return None

    def _keyring_save(self, payload: str) -> bool:
        if not self.use_keyring:
            return False
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, payload)  # type: ignore[union-attr]
            return True
        except Exception as exc:  # pragma: no cover - backend dependent
            log.debug("Keyring write unavailable, falling back to file: %s", exc)
            return False

    def _keyring_clear(self) -> None:
        if not self.use_keyring:
            return
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - nothing stored
            pass

    # -- public API --------------------------------------------------------
    def has_credentials(self) -> bool:
        """True when some stored credential exists (valid or not)."""
        return self._keyring_load() is not None or self.token_path.exists()

    def load(self, scopes: list[str]) -> Credentials | None:
        """Return stored credentials, or ``None`` when nothing usable is saved."""
        payload = self._keyring_load()
        if payload is None and self.token_path.exists():
            try:
                payload = self.token_path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("Could not read token store: %s", exc)
                return None
        if not payload:
            return None
        try:
            info = json.loads(payload)
            return Credentials.from_authorized_user_info(info, scopes)
        except (ValueError, KeyError) as exc:
            log.warning("Stored credentials are unreadable and will be discarded (%s)",
                        type(exc).__name__)
            return None

    def save(self, credentials: Credentials) -> None:
        """Persist credentials, preferring the OS credential store."""
        payload = credentials.to_json()
        if self._keyring_save(payload):
            # Remove any older file copy so the secret exists in one place only.
            self._remove_file()
            return
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.token_path.with_suffix(".tmp")
            temp.write_text(payload, encoding="utf-8")
            _restrict_permissions(temp)
            os.replace(temp, self.token_path)
            _restrict_permissions(self.token_path)
        except OSError as exc:
            log.error("Could not persist credentials: %s", exc)

    def clear(self) -> None:
        """Delete every stored copy of the credentials."""
        self._keyring_clear()
        self._remove_file()

    def _remove_file(self) -> None:
        try:
            if self.token_path.exists():
                self.token_path.unlink()
        except OSError as exc:  # pragma: no cover - defensive
            log.debug("Could not remove token file: %s", exc)

    @property
    def backend_name(self) -> str:
        """Human-readable description of where the token is kept."""
        return "Windows Credential Manager" if self.use_keyring else "restricted local file"
