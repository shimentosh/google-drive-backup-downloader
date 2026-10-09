"""Application-wide constants for Google Drive Backup Downloader."""

from __future__ import annotations

APP_NAME = "Google Drive Backup Downloader"
APP_SHORT_NAME = "GDriveBackup"
APP_SUBTITLE = "Complete Google Drive Backup"
APP_VERSION = "1.2.0"
ORG_NAME = "GDriveBackup"
AUTHOR_NAME = "Shimanto"

#: Public links shown in the About section and the setup guide.
REPO_URL = "https://github.com/shimentosh/google-drive-backup-downloader"
WEBSITE_URL = "https://shimentosh.github.io/google-drive-backup-downloader/"
AUTHOR_URL = "https://shimanto.xyz"
ISSUES_URL = f"{REPO_URL}/issues"
RELEASES_URL = f"{REPO_URL}/releases/latest"
SETUP_GUIDE_URL = f"{WEBSITE_URL}setup.html"

#: Suffix used for in-progress downloads. Renamed to the final name only after
#: the download has been verified.
PARTIAL_SUFFIX = ".gdbd.part"

#: Read-only access is all this application ever needs.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

#: Default streaming chunk size (8 MiB) - large enough for good throughput on
#: multi-gigabyte videos, small enough to keep memory flat.
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MIN_CHUNK_SIZE = 256 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024

DEFAULT_WORKERS = 4
MAX_WORKERS = 16

#: Google Drive folder MIME type.
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

#: Folder name used for items that are reachable in the account but not under
#: "My Drive" (for example items that live only in "Shared with me").
UNFILED_FOLDER = "_Unfiled"
SHARED_WITH_ME_FOLDER = "Shared with me"
MY_DRIVE_FOLDER = "My Drive"

#: Fields requested for every Drive item during a scan.
DRIVE_FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,sha256Checksum,modifiedTime,createdTime,"
    "parents,trashed,shortcutDetails,driveId,ownedByMe,capabilities/canDownload,"
    "exportLinks,webViewLink,fileExtension"
)

LIST_FIELDS = f"nextPageToken,files({DRIVE_FILE_FIELDS})"

#: Retry policy.
RETRY_INITIAL_DELAY = 2.0
RETRY_MAX_DELAY = 64.0
RETRY_MULTIPLIER = 2.0
DEFAULT_MAX_RETRIES = 5

#: HTTP status codes that are worth retrying.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

#: Safety margin kept free on the destination volume (1 GiB).
DISK_SPACE_MARGIN = 1024 * 1024 * 1024
