"""Google Drive integration for PixSort.

Provides secure URL parsing, recursive folder traversal, file downloads,
content validation, error handling, and OAuth management.
"""

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import random
import re
import secrets
import time
from threading import Event
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from PIL import Image

from .config import AnalysisConfig

logger = logging.getLogger("pixsort.gdrive")

_env_loaded = False


def load_env(force: bool = False) -> bool:
    """Load environment variables from .env in backend or repo root if present.

    Returns True if an .env file was located and loaded, False otherwise.
    """
    global _env_loaded
    if _env_loaded and not force:
        return True
    _env_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv is not installed; skipping .env loading")
        return False

    loaded_any = False
    # 1. backend/.env (parent of src/pixsort is backend)
    try:
        backend_env = Path(__file__).resolve().parents[2] / ".env"
        if backend_env.is_file():
            load_dotenv(dotenv_path=backend_env, override=False)
            loaded_any = True
    except Exception:
        pass

    # 2. repo root/.env (parent of backend is repo root)
    try:
        root_env = Path(__file__).resolve().parents[3] / ".env"
        if root_env.is_file():
            load_dotenv(dotenv_path=root_env, override=False)
            loaded_any = True
    except Exception:
        pass

    # 3. Fallback standard dotenv search
    if not loaded_any:
        try:
            loaded_any = load_dotenv(override=False)
        except Exception:
            pass

    return loaded_any


# Automatically load .env on module import
load_env()

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

# Regex patterns for supported Google Drive URLs
FOLDER_URL_PATTERNS = [
    re.compile(r"^https?://drive\.google\.com/drive/(?:u/\d+/)?folders/(?P<id>[a-zA-Z0-9_-]+)"),
    re.compile(r"^https?://drive\.google\.com/drive/mobile/folders/(?P<id>[a-zA-Z0-9_-]+)"),
    re.compile(r"^https?://drive\.google\.com/folderview\?.*id=(?P<id>[a-zA-Z0-9_-]+)"),
]

FILE_URL_PATTERNS = [
    re.compile(r"^https?://(?:drive|docs)\.google\.com/file/d/(?P<id>[a-zA-Z0-9_-]+)"),
    re.compile(r"^https?://drive\.google\.com/open\?.*id=(?P<id>[a-zA-Z0-9_-]+)"),
]

GENERAL_DRIVE_PATTERNS = [
    re.compile(r"^https?://drive\.google\.com/drive/(?:u/\d+/)?(?:my-drive|home|shared-with-me|recent|starred|trash|priority)(?:/|\?.*)?$"),
    re.compile(r"^https?://drive\.google\.com/?$"),
    re.compile(r"^https?://drive\.google\.com/drive/?$"),
]

ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


class GoogleDriveError(Exception):
    """Base error for Google Drive operations with a user-friendly message."""

    def __init__(self, message: str, code: str = "ERROR", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class GoogleDriveAuthRequired(GoogleDriveError):
    """Raised when access to a resource requires user authorization."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message or (
                "PixSort couldn't access this Google Drive folder. "
                "The folder may be private, or authorization may be required. "
                "Connect your Google Drive account or check your existing access permissions, "
                "then try again. You do not need to make your photos public."
            ),
            code="AUTH_REQUIRED",
            status_code=403,
        )


class GoogleDriveConfigError(GoogleDriveError):
    """Raised when Google Drive API credentials are not configured."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message or (
                "Google Drive API credentials are not configured on this server. "
                "Please configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (or GOOGLE_API_KEY) in the environment."
            ),
            code="CONFIG_MISSING",
            status_code=503,
        )


class GoogleDriveNotFoundError(GoogleDriveError):
    """Raised when the requested file or folder does not exist."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message or "The requested Google Drive file or folder could not be found.",
            code="NOT_FOUND",
            status_code=404,
        )


class GoogleDriveRateLimitError(GoogleDriveError):
    """Raised when Google Drive rate limits are encountered."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message or "Google Drive API rate limit reached or service temporarily unavailable. Please try again later.",
            code="RATE_LIMITED",
            status_code=429,
        )


@dataclass
class DriveFileItem:
    id: str
    name: str
    relative_path: str
    size: int
    mime_type: str


def parse_google_drive_url(url: str) -> tuple[str, str]:
    """Parse a Google Drive URL and return (resource_id, resource_kind).

    resource_kind is 'folder' or 'file'.
    Raises GoogleDriveError if the URL is invalid or unsupported.
    """
    if not url or not url.strip():
        raise GoogleDriveError("Please enter a valid Google Drive link.", code="EMPTY_URL", status_code=400)

    url = url.strip()

    # Direct resource ID pasted
    if ID_PATTERN.match(url):
        return url, "folder"

    # Reject non-http(s) schemes
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise GoogleDriveError("Unsupported Google Drive link format. Please provide a link to a file or folder.", code="INVALID_URL", status_code=400)

    # Restrict domains strictly to google drive/docs
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"drive.google.com", "docs.google.com"}:
        raise GoogleDriveError("Unsupported Google Drive link format. Please provide a link to a file or folder.", code="INVALID_URL", status_code=400)

    # Check for general drive homepage / my-drive links
    for pattern in GENERAL_DRIVE_PATTERNS:
        if pattern.match(url):
            raise GoogleDriveError(
                "This link points to your general Google Drive page rather than a specific folder or file. Please open the specific folder or photo in Google Drive and copy its link, or choose from your folders after authorization.",
                code="GENERAL_DRIVE_LINK",
                status_code=400,
            )

    # Check folder patterns
    for pattern in FOLDER_URL_PATTERNS:
        match = pattern.match(url)
        if match:
            resource_id = match.group("id")
            if ID_PATTERN.match(resource_id):
                return resource_id, "folder"

    # Check file patterns
    for pattern in FILE_URL_PATTERNS:
        match = pattern.match(url)
        if match:
            resource_id = match.group("id")
            if ID_PATTERN.match(resource_id):
                return resource_id, "file"

    # Check query param ?id=
    query_params = parse_qs(parsed.query)
    if "id" in query_params:
        potential_id = query_params["id"][0]
        if ID_PATTERN.match(potential_id):
            if "folders" in parsed.path or "folderview" in parsed.path:
                return potential_id, "folder"
            return potential_id, "file"

    raise GoogleDriveError(
        "Unsupported Google Drive link format. Please provide a link to a file or folder.",
        code="INVALID_URL",
        status_code=400,
    )


class GoogleOAuthManager:
    """Manages Google OAuth 2.0 configuration, authorization URL generation, and tokens."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._tokens: dict[str, dict[str, Any]] = {}
        self._states: dict[str, float] = {}

    @property
    def client_id(self) -> str:
        if self._client_id:
            return self._client_id
        return os.environ.get("GOOGLE_CLIENT_ID", "").strip()

    @client_id.setter
    def client_id(self, val: str | None) -> None:
        self._client_id = val

    @property
    def client_secret(self) -> str:
        if self._client_secret:
            return self._client_secret
        return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

    @client_secret.setter
    def client_secret(self, val: str | None) -> None:
        self._client_secret = val

    @property
    def redirect_uri(self) -> str:
        if self._redirect_uri:
            return self._redirect_uri
        return os.environ.get(
            "GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback"
        ).strip()

    @redirect_uri.setter
    def redirect_uri(self, val: str | None) -> None:
        self._redirect_uri = val

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def create_authorization_url(self, redirect_uri: str | None = None) -> tuple[str, str]:
        if not self.is_configured():
            raise GoogleDriveConfigError()

        state = secrets.token_urlsafe(32)
        # Store state with 10-minute expiry
        self._states[state] = time.time() + 600

        effective_redirect = redirect_uri or self.redirect_uri
        params = {
            "client_id": self.client_id,
            "redirect_uri": effective_redirect,
            "response_type": "code",
            "scope": DRIVE_READONLY_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{OAUTH_AUTH_URL}?{urlencode(params)}", state

    def verify_state(self, state: str) -> bool:
        expiry = self._states.pop(state, None)
        if expiry is None:
            return False
        return time.time() <= expiry

    def exchange_code(
        self, code: str, redirect_uri: str | None = None, client: httpx.Client | None = None
    ) -> dict[str, Any]:
        if not self.is_configured():
            raise GoogleDriveConfigError()

        data = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "grant_type": "authorization_code",
        }

        http = client or httpx.Client(timeout=15.0)
        try:
            response = http.post(OAUTH_TOKEN_URL, data=data)
            if response.status_code != 200:
                logger.error("OAuth token exchange failed with status %d: %s", response.status_code, response.text)
                raise GoogleDriveError(
                    "Google Drive authorization failed or was revoked. Please try connecting again.",
                    code="AUTH_FAILED",
                    status_code=400,
                )
            token_payload = response.json()
            return token_payload
        finally:
            if client is None:
                http.close()

    def store_token(self, session_id: str, token_data: dict[str, Any]) -> None:
        self._tokens[session_id] = token_data

    def get_token(self, session_id: str) -> str | None:
        token_data = self._tokens.get(session_id)
        if not token_data:
            return None
        return token_data.get("access_token")

    def clear_token(self, session_id: str) -> None:
        self._tokens.pop(session_id, None)


class GoogleDriveClient:
    """Client for Google Drive API operations."""

    def __init__(
        self,
        api_key: str | None = None,
        access_token: str | None = None,
        http_client: httpx.Client | None = None,
    ):
        self._api_key = api_key
        self.access_token = access_token
        self._http = http_client

    @property
    def api_key(self) -> str:
        if self._api_key:
            return self._api_key
        return os.environ.get("GOOGLE_API_KEY", "").strip()

    @api_key.setter
    def api_key(self, val: str | None) -> None:
        self._api_key = val

    def _get_http(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        return httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _auth_params(self) -> dict[str, str]:
        if not self.access_token and self.api_key:
            return {"key": self.api_key}
        return {}

    def get_metadata(self, file_id: str) -> dict[str, Any]:
        """Fetch file or folder metadata."""
        http = self._get_http()
        close_needed = self._http is None
        params = {
            "fields": "id,name,mimeType,size,parents,shortcutDetails",
            "supportsAllDrives": "true",
            **self._auth_params(),
        }
        url = f"{DRIVE_API_BASE}/files/{file_id}"
        try:
            response = http.get(url, params=params, headers=self._headers())
            if response.status_code in {401, 403}:
                raise GoogleDriveAuthRequired()
            if response.status_code == 404:
                # Private files can return 404 to unauthenticated users
                if not self.access_token:
                    raise GoogleDriveAuthRequired()
                raise GoogleDriveNotFoundError()
            if response.status_code in {429, 503}:
                raise GoogleDriveRateLimitError()
            if response.status_code != 200:
                raise GoogleDriveError(f"Failed to access Google Drive item: HTTP {response.status_code}")
            return response.json()
        finally:
            if close_needed:
                http.close()

    def scan_recursive(
        self,
        root_id: str,
        is_folder: bool = True,
        cancel_event: Event | None = None,
        max_count: int = 200,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[list[DriveFileItem], list[dict[str, str]]]:
        """Traverse subfolders recursively and discover supported image files.

        Returns (files_to_download, skipped_files).
        """
        supported_exts = AnalysisConfig().supported_extensions
        http = self._get_http()
        close_needed = self._http is None

        try:
            if not is_folder:
                meta = self.get_metadata(root_id)
                name = meta.get("name", "unnamed")
                ext = Path(name).suffix.lower()
                mime = meta.get("mimeType", "")
                if ext in supported_exts or mime.startswith("image/"):
                    return [
                        DriveFileItem(
                            id=root_id,
                            name=name,
                            relative_path=name,
                            size=int(meta.get("size", 0)),
                            mime_type=mime,
                        )
                    ], []
                raise GoogleDriveError("No supported images were found in the specified Google Drive location.", code="EMPTY_FOLDER", status_code=400)

            # Recursive folder scanning
            queue: list[tuple[str, str]] = [(root_id, "")]
            visited_folder_ids: set[str] = set()
            seen_file_ids: set[str] = set()
            files_to_download: list[DriveFileItem] = []
            skipped_files: list[dict[str, str]] = []

            while queue and len(files_to_download) < max_count:
                if cancel_event and cancel_event.is_set():
                    from .pipeline import AnalysisCancelled
                    raise AnalysisCancelled("Analysis cancelled by user.")

                folder_id, rel_dir = queue.pop(0)
                if folder_id in visited_folder_ids:
                    continue
                visited_folder_ids.add(folder_id)

                if progress_callback:
                    progress_callback(f"Scanning folder: {rel_dir or 'root'}")

                page_token: str | None = None
                while True:
                    if cancel_event and cancel_event.is_set():
                        from .pipeline import AnalysisCancelled
                        raise AnalysisCancelled("Analysis cancelled by user.")

                    params = {
                        "q": f"'{folder_id}' in parents and trashed = false",
                        "fields": "nextPageToken, files(id, name, mimeType, size, shortcutDetails)",
                        "pageSize": 100,
                        "supportsAllDrives": "true",
                        "includeItemsFromAllDrives": "true",
                        **self._auth_params(),
                    }
                    if page_token:
                        params["pageToken"] = page_token

                    response = http.get(
                        f"{DRIVE_API_BASE}/files",
                        params=params,
                        headers=self._headers(),
                    )

                    if response.status_code in {401, 403}:
                        if folder_id == root_id:
                            raise GoogleDriveAuthRequired()
                        skipped_files.append({"path": rel_dir or folder_id, "reason": "inaccessible_subfolder"})
                        break

                    if response.status_code == 404:
                        if folder_id == root_id:
                            raise GoogleDriveNotFoundError()
                        skipped_files.append({"path": rel_dir or folder_id, "reason": "subfolder_not_found"})
                        break

                    if response.status_code in {429, 503}:
                        raise GoogleDriveRateLimitError()

                    if response.status_code != 200:
                        if folder_id == root_id:
                            raise GoogleDriveError(f"Google Drive folder listing failed: HTTP {response.status_code}")
                        skipped_files.append({"path": rel_dir or folder_id, "reason": f"http_error_{response.status_code}"})
                        break

                    data = response.json()
                    for item in data.get("files", []):
                        if cancel_event and cancel_event.is_set():
                            from .pipeline import AnalysisCancelled
                            raise AnalysisCancelled("Analysis cancelled by user.")

                        item_id = item["id"]
                        mime = item.get("mimeType", "")
                        name = item.get("name", "unnamed")

                        # Folders
                        if mime == "application/vnd.google-apps.folder":
                            sub_rel = f"{rel_dir}/{name}" if rel_dir else name
                            if item_id not in visited_folder_ids:
                                queue.append((item_id, sub_rel))
                            continue

                        # Shortcuts
                        if mime == "application/vnd.google-apps.shortcut":
                            details = item.get("shortcutDetails", {})
                            target_id = details.get("targetId")
                            target_mime = details.get("targetMimeType", "")
                            if not target_id:
                                continue
                            if target_mime == "application/vnd.google-apps.folder":
                                sub_rel = f"{rel_dir}/{name}" if rel_dir else name
                                if target_id not in visited_folder_ids:
                                    queue.append((target_id, sub_rel))
                                continue
                            item_id = target_id
                            mime = target_mime

                        # De-duplicate file IDs
                        if item_id in seen_file_ids:
                            continue
                        seen_file_ids.add(item_id)

                        file_rel_path = f"{rel_dir}/{name}" if rel_dir else name
                        ext = Path(name).suffix.lower()
                        is_image = (ext in supported_exts) or mime.startswith("image/")

                        if is_image:
                            files_to_download.append(
                                DriveFileItem(
                                    id=item_id,
                                    name=name,
                                    relative_path=file_rel_path,
                                    size=int(item.get("size", 0)),
                                    mime_type=mime,
                                )
                            )
                            if len(files_to_download) >= max_count:
                                break
                        else:
                            skipped_files.append({"name": file_rel_path, "reason": "unsupported_type"})

                    page_token = data.get("nextPageToken")
                    if not page_token or len(files_to_download) >= max_count:
                        break

            if not files_to_download:
                raise GoogleDriveError(
                    "No supported images were found in the specified Google Drive location.",
                    code="EMPTY_FOLDER",
                    status_code=400,
                )

            return files_to_download, skipped_files
        finally:
            if close_needed:
                http.close()

    def download_file(
        self,
        file_id: str,
        destination: Path,
        max_bytes: int = 25 * 1024 * 1024,
        cancel_event: Event | None = None,
    ) -> int:
        """Download file content from Google Drive with validation and size limit enforcement."""
        http = self._get_http()
        close_needed = self._http is None
        params = {"alt": "media", "supportsAllDrives": "true", **self._auth_params()}
        url = f"{DRIVE_API_BASE}/files/{file_id}"

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            for attempt in range(3):
                if cancel_event and cancel_event.is_set():
                    from .pipeline import AnalysisCancelled
                    raise AnalysisCancelled("Analysis cancelled by user.")

                try:
                    with http.stream("GET", url, params=params, headers=self._headers()) as response:
                        if response.status_code in {429, 503} and attempt < 2:
                            backoff = min(8.0, 0.5 * (2 ** attempt) + random.uniform(0.1, 0.4))
                            time.sleep(backoff)
                            continue
                        if response.status_code in {401, 403}:
                            raise GoogleDriveAuthRequired()
                        if response.status_code == 404:
                            raise GoogleDriveNotFoundError()
                        if response.status_code != 200:
                            raise GoogleDriveError(f"Download failed: HTTP {response.status_code}")

                        written = 0
                        with destination.open("wb") as out:
                            for chunk in response.iter_bytes(chunk_size=128 * 1024):
                                if cancel_event and cancel_event.is_set():
                                    from .pipeline import AnalysisCancelled
                                    raise AnalysisCancelled("Analysis cancelled by user.")
                                written += len(chunk)
                                if written > max_bytes:
                                    destination.unlink(missing_ok=True)
                                    raise GoogleDriveError(f"File exceeds maximum allowed size ({max_bytes} bytes).", code="FILE_TOO_LARGE")
                                out.write(chunk)
                        return written
                except (GoogleDriveAuthRequired, GoogleDriveNotFoundError, GoogleDriveError):
                    destination.unlink(missing_ok=True)
                    raise
                except Exception:
                    destination.unlink(missing_ok=True)
                    if attempt >= 2:
                        raise
                    time.sleep(min(8.0, 0.5 * (2 ** attempt) + random.uniform(0.1, 0.4)))
            raise GoogleDriveRateLimitError()
        finally:
            if close_needed:
                http.close()

    def list_user_folders(self, max_results: int = 50) -> list[dict[str, Any]]:
        """List accessible folders in the user's Google Drive for direct selection."""
        http = self._get_http()
        close_needed = self._http is None
        params = {
            "q": "mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            "fields": "files(id, name, modifiedTime)",
            "pageSize": max_results,
            "orderBy": "folder,modifiedTime desc",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            **self._auth_params(),
        }
        try:
            response = http.get(f"{DRIVE_API_BASE}/files", params=params, headers=self._headers())
            if response.status_code in {401, 403}:
                raise GoogleDriveAuthRequired()
            if response.status_code != 200:
                raise GoogleDriveError(f"Failed to list Google Drive folders: HTTP {response.status_code}")
            data = response.json()
            return data.get("files", [])
        finally:
            if close_needed:
                http.close()
