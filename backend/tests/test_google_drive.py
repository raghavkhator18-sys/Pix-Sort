"""Comprehensive tests for Google Drive integration."""

from pathlib import Path
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from fixture_helpers import get_fixture_dir
from pixsort.google_drive import (
    DriveFileItem,
    GoogleDriveAuthRequired,
    GoogleDriveClient,
    GoogleDriveConfigError,
    GoogleDriveError,
    GoogleDriveNotFoundError,
    GoogleDriveRateLimitError,
    GoogleOAuthManager,
    load_env,
    parse_google_drive_url,
)
from pixsort.main import AnalysisJob, create_app


def _wait_for_terminal(client: TestClient, job_id: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.02)
    pytest.fail(f"Analysis job {job_id} did not reach terminal state within {timeout_seconds}s")


class MockGoogleDriveClient(GoogleDriveClient):
    """Mock client for testing without network requests."""

    def __init__(
        self,
        metadata_map: dict[str, dict[str, Any]] | None = None,
        files_map: dict[str, list[dict[str, Any]]] | None = None,
        file_bytes_map: dict[str, bytes] | None = None,
        auth_error_ids: set[str] | None = None,
        not_found_ids: set[str] | None = None,
    ):
        super().__init__()
        self.metadata_map = metadata_map or {}
        self.files_map = files_map or {}
        self.file_bytes_map = file_bytes_map or {}
        self.auth_error_ids = auth_error_ids or set()
        self.not_found_ids = not_found_ids or set()

    def get_metadata(self, file_id: str) -> dict[str, Any]:
        if file_id in self.auth_error_ids:
            raise GoogleDriveAuthRequired()
        if file_id in self.not_found_ids:
            raise GoogleDriveNotFoundError()
        if file_id in self.metadata_map:
            return self.metadata_map[file_id]
        return {"id": file_id, "name": "sample.jpg", "mimeType": "image/jpeg", "size": 1024}

    def scan_recursive(
        self,
        root_id: str,
        is_folder: bool = True,
        cancel_event: Any = None,
        max_count: int = 200,
        progress_callback: Any = None,
    ) -> tuple[list[DriveFileItem], list[dict[str, str]]]:
        if root_id in self.auth_error_ids:
            raise GoogleDriveAuthRequired()
        if root_id in self.not_found_ids:
            raise GoogleDriveNotFoundError()
        return super().scan_recursive(root_id, is_folder, cancel_event, max_count, progress_callback)

    def download_file(
        self,
        file_id: str,
        destination: Path,
        max_bytes: int = 25 * 1024 * 1024,
        cancel_event: Any = None,
    ) -> int:
        if file_id in self.auth_error_ids:
            raise GoogleDriveAuthRequired()
        if file_id in self.file_bytes_map:
            content = self.file_bytes_map[file_id]
            destination.write_bytes(content)
            return len(content)
        # Default: write a valid fixture JPEG
        fixture_path = get_fixture_dir() / "10.jpeg"
        content = fixture_path.read_bytes()
        destination.write_bytes(content)
        return len(content)


# --- 1. URL Parsing Tests ---

def test_parse_google_drive_folder_urls() -> None:
    folder_id = "1aBcDeFgHiJkLmNoPqRsTuVwXyZ_12345"
    urls = [
        f"https://drive.google.com/drive/folders/{folder_id}",
        f"https://drive.google.com/drive/u/0/folders/{folder_id}",
        f"https://drive.google.com/drive/u/1/folders/{folder_id}",
        f"https://drive.google.com/drive/mobile/folders/{folder_id}",
        f"https://drive.google.com/folderview?id={folder_id}",
        f"https://drive.google.com/open?id={folder_id}&usp=sharing",
    ]
    for url in urls:
        parsed_id, kind = parse_google_drive_url(url)
        assert parsed_id == folder_id
        assert kind in {"folder", "file"}


def test_parse_google_drive_file_urls() -> None:
    file_id = "1aBcDeFgHiJkLmNoPqRsTuVwXyZ_67890"
    urls = [
        f"https://drive.google.com/file/d/{file_id}/view?usp=sharing",
        f"https://drive.google.com/file/d/{file_id}/edit",
        f"https://docs.google.com/file/d/{file_id}",
    ]
    for url in urls:
        parsed_id, kind = parse_google_drive_url(url)
        assert parsed_id == file_id
        assert kind == "file"


def test_parse_invalid_urls_rejected() -> None:
    invalid_cases = [
        "",
        "   ",
        "https://example.com/drive/folders/123",
        "https://dropbox.com/s/12345/photos",
        "https://drive.google.com/drive/folders/short",  # too short
        "ftp://drive.google.com/file/d/123456789012",
        "https://drive.google.com/file/d/invalid!char@acters/view",
    ]
    for bad_url in invalid_cases:
        with pytest.raises(GoogleDriveError) as exc_info:
            parse_google_drive_url(bad_url)
        assert exc_info.value.status_code == 400


def test_parse_general_drive_homepage_links() -> None:
    general_links = [
        "https://drive.google.com/drive/my-drive",
        "https://drive.google.com/drive/u/0/my-drive",
        "https://drive.google.com/drive/home",
        "https://drive.google.com/drive/shared-with-me",
        "https://drive.google.com/drive/",
        "https://drive.google.com",
    ]
    for link in general_links:
        with pytest.raises(GoogleDriveError) as exc_info:
            parse_google_drive_url(link)
        assert exc_info.value.code == "GENERAL_DRIVE_LINK"
        assert "general Google Drive page" in exc_info.value.message


def test_parse_raw_id() -> None:
    folder_id = "1aBcDeFgHiJkLmNoPqRsTuVwXyZ_12345"
    parsed_id, kind = parse_google_drive_url(folder_id)
    assert parsed_id == folder_id
    assert kind == "folder"


# --- 2. Configuration & OAuth Endpoint Tests ---

def test_gdrive_config_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    app = create_app()
    client = TestClient(app)
    response = client.get("/api/gdrive/config")
    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is False
    assert data["oauth_available"] is False
    assert data["authorized"] is False


def test_gdrive_config_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")

    app = create_app()
    client = TestClient(app)
    response = client.get("/api/gdrive/config")
    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is True
    assert data["oauth_available"] is True
    assert data["api_key_available"] is True


def test_load_env_loads_environment_safely(tmp_path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        pytest.skip("python-dotenv is not installed in the global test runner environment")

    env_file = tmp_path / ".env"
    env_file.write_text("GOOGLE_CLIENT_ID=dummy-env-id\nGOOGLE_CLIENT_SECRET=dummy-env-secret\n", encoding="utf-8")
    
    load_dotenv(dotenv_path=env_file, override=True)
    mgr = GoogleOAuthManager()
    assert mgr.is_configured() is True
    assert mgr.client_id == "dummy-env-id"


def test_dynamic_oauth_manager_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    mgr = GoogleOAuthManager()
    assert mgr.is_configured() is False

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "dyn-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "dyn-sec")
    assert mgr.is_configured() is True
    assert mgr.client_id == "dyn-id"


def test_missing_credentials_endpoint_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    app = create_app()
    client = TestClient(app)

    # 1. /api/auth/google/url returns 503 with exact error message
    url_res = client.get("/api/auth/google/url")
    assert url_res.status_code == 503
    assert "Google Drive API credentials are not configured" in url_res.json()["detail"]

    # 2. /api/gdrive/import returns 503 with exact error message
    import_res = client.post("/api/gdrive/import", json={"url": "https://drive.google.com/drive/folders/12345678901234567890"})
    assert import_res.status_code == 503
    assert "Google Drive API credentials are not configured" in import_res.json()["detail"]


def test_google_auth_url_and_callback() -> None:
    oauth_manager = GoogleOAuthManager(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8000/api/auth/google/callback",
    )
    app = create_app(oauth_manager=oauth_manager)
    client = TestClient(app)

    # 1. Generate Auth URL
    res_url = client.get("/api/auth/google/url")
    assert res_url.status_code == 200
    auth_data = res_url.json()
    assert "https://accounts.google.com/o/oauth2/v2/auth" in auth_data["url"]
    assert "test-client-id" in auth_data["url"]
    assert "state=" in auth_data["url"]
    state = auth_data["state"]

    # 2. Mock exchange and simulate callback
    oauth_manager.exchange_code = MagicMock(return_value={"access_token": "mock-token-xyz"})

    res_cb = client.get(f"/api/auth/google/callback?code=mock_code&state={state}")
    assert res_cb.status_code == 200
    assert "GOOGLE_AUTH_SUCCESS" in res_cb.text

    # Verify session cookie was set
    assert "pixsort_session" in res_cb.cookies


def test_google_auth_callback_cancelled() -> None:
    oauth_manager = GoogleOAuthManager(client_id="id", client_secret="secret")
    app = create_app(oauth_manager=oauth_manager)
    client = TestClient(app)

    response = client.get("/api/auth/google/callback?error=access_denied")
    assert response.status_code == 200
    assert "GOOGLE_AUTH_ERROR" in response.text
    assert "access_denied" in response.text


# --- 3. Google Drive Import & Analysis Pipeline Integration ---

def test_successful_file_import() -> None:
    mock_drive = MockGoogleDriveClient(
        metadata_map={
            "file_1234567890": {
                "id": "file_1234567890",
                "name": "photo.jpg",
                "mimeType": "image/jpeg",
                "size": 5000,
            }
        }
    )
    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.post(
        "/api/gdrive/import",
        json={"url": "https://drive.google.com/file/d/file_1234567890/view"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] == "completed"
    assert terminal["progress_percent"] == 100
    assert terminal["results"]["summary"]["processed"] == 1
    assert terminal["results"]["images"][0]["filename"] == "photo.jpg"
    assert terminal["results"]["images"][0]["thumbnail"].startswith("data:image/jpeg;base64,")


def test_shared_folder_access() -> None:
    folder_id = "folder_root_12345"
    mock_drive = MockGoogleDriveClient(
        metadata_map={
            folder_id: {"id": folder_id, "name": "Shared Photos", "mimeType": "application/vnd.google-apps.folder"}
        }
    )
    # Mock scan_recursive directly on client
    mock_drive.scan_recursive = MagicMock(
        return_value=(
            [
                DriveFileItem(id="img1", name="pic1.jpeg", relative_path="pic1.jpeg", size=2048, mime_type="image/jpeg"),
                DriveFileItem(id="img2", name="pic2.jpeg", relative_path="pic2.jpeg", size=2048, mime_type="image/jpeg"),
            ],
            [],
        )
    )

    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.post(
        "/api/gdrive/import",
        json={"url": f"https://drive.google.com/drive/folders/{folder_id}"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] == "completed"
    assert terminal["results"]["summary"]["processed"] == 2
    filenames = {img["filename"] for img in terminal["results"]["images"]}
    assert filenames == {"pic1.jpeg", "pic2.jpeg"}


def test_private_folder_authorization_requirement() -> None:
    folder_id = "private_folder_12345"
    mock_drive = MockGoogleDriveClient(auth_error_ids={folder_id})
    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.post(
        "/api/gdrive/import",
        json={"url": f"https://drive.google.com/drive/folders/{folder_id}"},
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "PixSort couldn't access this Google Drive folder." in detail
    assert "Connect your Google Drive account or check your existing access permissions" in detail
    assert "You do not need to make your photos public." in detail


def test_recursive_nested_folder_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that nested subfolders (Vacation/Day 1, Vacation/Day 2/Family) are discovered with relative paths."""
    client = GoogleDriveClient(access_token="fake_token")

    # Mock HTTP client responses for folder tree:
    # Root -> Day 1 (folder), beach.jpg (file)
    # Day 1 -> sunset.jpg (file)
    def mock_get(url: str, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        q = params.get("q", "") if params else ""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "'root_12345'" in q:
            mock_resp.json.return_value = {
                "files": [
                    {"id": "folder_day1", "name": "Day 1", "mimeType": "application/vnd.google-apps.folder"},
                    {"id": "file_beach", "name": "beach.jpg", "mimeType": "image/jpeg", "size": 100},
                ]
            }
        elif "'folder_day1'" in q:
            mock_resp.json.return_value = {
                "files": [
                    {"id": "file_sunset", "name": "sunset.jpg", "mimeType": "image/jpeg", "size": 100},
                ]
            }
        else:
            mock_resp.json.return_value = {"files": []}
        return mock_resp

    mock_http = MagicMock()
    mock_http.get.side_effect = mock_get
    client._http = mock_http

    files, skipped = client.scan_recursive("root_12345", is_folder=True)
    assert len(files) == 2
    rel_paths = {f.relative_path for f in files}
    assert rel_paths == {"beach.jpg", "Day 1/sunset.jpg"}
    assert skipped == []


def test_api_pagination() -> None:
    """Verifies that nextPageToken is followed across pages."""
    client = GoogleDriveClient(access_token="fake_token")

    def mock_get(url: str, params: dict[str, Any] | None = None, headers: Any = None) -> Any:
        page_token = params.get("pageToken") if params else None
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if not page_token:
            mock_resp.json.return_value = {
                "nextPageToken": "token_page_2",
                "files": [
                    {"id": "img_p1", "name": "first.jpg", "mimeType": "image/jpeg", "size": 100},
                ],
            }
        else:
            mock_resp.json.return_value = {
                "files": [
                    {"id": "img_p2", "name": "second.jpg", "mimeType": "image/jpeg", "size": 100},
                ],
            }
        return mock_resp

    mock_http = MagicMock()
    mock_http.get.side_effect = mock_get
    client._http = mock_http

    files, skipped = client.scan_recursive("folder_pages", is_folder=True)
    assert len(files) == 2
    assert [f.name for f in files] == ["first.jpg", "second.jpg"]


def test_duplicate_drive_file_ids() -> None:
    """Verifies that multiple occurrences of the same Drive file ID are de-duplicated."""
    client = GoogleDriveClient(access_token="fake_token")

    mock_http = MagicMock()
    mock_http.get.return_value.status_code = 200
    mock_http.get.return_value.json.return_value = {
        "files": [
            {"id": "same_file_id", "name": "duplicate1.jpg", "mimeType": "image/jpeg", "size": 100},
            {"id": "same_file_id", "name": "duplicate2.jpg", "mimeType": "image/jpeg", "size": 100},
            {"id": "unique_file_id", "name": "unique.jpg", "mimeType": "image/jpeg", "size": 100},
        ]
    }
    client._http = mock_http

    files, skipped = client.scan_recursive("folder_dups", is_folder=True)
    assert len(files) == 2
    ids = [f.id for f in files]
    assert ids == ["same_file_id", "unique_file_id"]


def test_unsupported_files_skipped() -> None:
    """Verifies non-image files are safely skipped and reported."""
    client = GoogleDriveClient(access_token="fake_token")

    mock_http = MagicMock()
    mock_http.get.return_value.status_code = 200
    mock_http.get.return_value.json.return_value = {
        "files": [
            {"id": "pdf1", "name": "document.pdf", "mimeType": "application/pdf", "size": 100},
            {"id": "mp4_1", "name": "video.mp4", "mimeType": "video/mp4", "size": 100},
            {"id": "img1", "name": "real_photo.jpg", "mimeType": "image/jpeg", "size": 100},
        ]
    }
    client._http = mock_http

    files, skipped = client.scan_recursive("folder_mixed", is_folder=True)
    assert len(files) == 1
    assert files[0].name == "real_photo.jpg"
    assert len(skipped) == 2
    assert {s["name"] for s in skipped} == {"document.pdf", "video.mp4"}


def test_partial_import_failure() -> None:
    """Verifies that corrupt or failed image downloads are skipped while valid images succeed."""
    mock_drive = MockGoogleDriveClient(
        file_bytes_map={
            "corrupt_file": b"this is completely corrupted non-image binary data",
        }
    )
    mock_drive.scan_recursive = MagicMock(
        return_value=(
            [
                DriveFileItem(id="corrupt_file", name="bad.jpg", relative_path="bad.jpg", size=100, mime_type="image/jpeg"),
                DriveFileItem(id="good_file", name="good.jpg", relative_path="good.jpg", size=100, mime_type="image/jpeg"),
            ],
            [],
        )
    )

    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.post(
        "/api/gdrive/import",
        json={"url": "https://drive.google.com/drive/folders/mixed_folder_12345"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] == "completed"
    assert terminal["results"]["summary"]["processed"] == 1
    assert terminal["results"]["images"][0]["filename"] == "good.jpg"
    assert "skipped_files" in terminal["results"]
    assert any(s["name"] == "bad.jpg" for s in terminal["results"]["skipped_files"])


def test_empty_folder_reports_clear_error() -> None:
    mock_drive = MockGoogleDriveClient()
    mock_drive.scan_recursive = MagicMock(
        side_effect=GoogleDriveError(
            "No supported images were found in the specified Google Drive location.",
            code="EMPTY_FOLDER",
            status_code=400,
        )
    )

    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.post(
        "/api/gdrive/import",
        json={"url": "https://drive.google.com/drive/folders/empty_folder_12345"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] == "failed"
    assert "No supported images were found" in terminal["error"]


def test_cancellation_and_cleanup() -> None:
    mock_drive = MockGoogleDriveClient()

    def slow_scan(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.5)
        return ([], [])

    mock_drive.scan_recursive = slow_scan

    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.post(
        "/api/gdrive/import",
        json={"url": "https://drive.google.com/drive/folders/slow_folder_12345"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    # Cancel immediately
    cancel_res = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_res.status_code == 200

    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] in {"cancelled", "failed"}


def test_existing_local_upload_regression() -> None:
    """Verifies that existing local upload functionality remains 100% intact."""
    app = create_app()
    client = TestClient(app)

    image_path = get_fixture_dir() / "10.jpeg"
    with image_path.open("rb") as stream:
        response = client.post(
            "/api/analyze/jobs",
            files=[("files", ("local_test.jpeg", stream, "image/jpeg"))],
        )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    terminal = _wait_for_terminal(client, job_id)
    assert terminal["status"] == "completed"
    assert terminal["results"]["images"][0]["filename"] == "local_test.jpeg"


def test_list_user_folders_endpoint() -> None:
    mock_drive = MockGoogleDriveClient()
    mock_drive.list_user_folders = MagicMock(
        return_value=[
            {"id": "folder_1", "name": "Vacation 2026"},
            {"id": "folder_2", "name": "Family"},
        ]
    )
    app = create_app(gdrive_client=mock_drive)
    client = TestClient(app)

    response = client.get("/api/gdrive/folders")
    assert response.status_code == 200
    folders = response.json()
    assert len(folders) == 2
    assert folders[0]["name"] == "Vacation 2026"


@pytest.mark.parametrize("error", [
    "</script><script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '\\\'");alert(1);//',
    "line\nbreak",
])
def test_oauth_callback_escapes_untrusted_errors(error: str) -> None:
    import html
    import json

    client = TestClient(create_app())
    response = client.get("/api/auth/google/callback", params={"error": error})
    assert response.status_code == 200
    assert response.text.count("<script>") == 1
    assert response.text.count("</script>") == 1
    assert f"<p>Authorization failed: {html.escape(error)}</p>" in response.text
    safe = json.dumps(error).replace("<", "\\u003c")
    assert f"error: {safe} " in response.text


def test_oauth_callback_escapes_exchange_exception() -> None:
    oauth = GoogleOAuthManager(client_id="test", client_secret="test")
    oauth.verify_state = MagicMock(return_value=True)
    oauth.exchange_code = MagicMock(side_effect=RuntimeError("</script><script>alert(1)</script>"))
    response = TestClient(create_app(oauth_manager=oauth)).get(
        "/api/auth/google/callback", params={"code": "code", "state": "state"}
    )
    assert response.text.count("<script>") == 1
    assert response.text.count("</script>") == 1
    assert "\\u003c/script>" in response.text
