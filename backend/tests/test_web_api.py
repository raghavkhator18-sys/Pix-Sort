from pathlib import Path
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from pixsort.main import MAX_UPLOAD_BYTES, app, create_app
from pixsort.pipeline import AnalysisCancelled, AnalysisReport
from fixture_helpers import get_fixture_dir


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_module_app_is_fastapi_application() -> None:
    response = TestClient(app).get("/docs")
    assert response.status_code == 200
    assert "PixSort local API" in response.text


def test_analyze_upload_returns_report_and_thumbnails(client: TestClient) -> None:
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as stream:
        response = client.post(
            "/api/analyze",
            files=[("files", ("photo.jpeg", stream, "image/jpeg"))],
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["processed"] == 1
    assert payload["images"][0]["filename"] == "photo.jpeg"
    assert payload["images"][0]["thumbnail"].startswith("data:image/jpeg;base64,")
    assert "recommendations" in payload
    assert payload["groups"] == []
    assert payload["progress"] == {"percent": 100, "stage": "Preparing results"}


def test_analyze_upload_returns_stable_group_members(client: TestClient) -> None:
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as first, image.open("rb") as second:
        response = client.post(
            "/api/analyze",
            files=[
                ("files", ("first.jpeg", first, "image/jpeg")),
                ("files", ("second.jpeg", second, "image/jpeg")),
            ],
        )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["type"] == "exact_duplicate"
    assert len(group["member_photo_ids"]) == 2
    assert len({image["id"] for image in payload["images"]}) == 2
    assert set(group["member_paths"]) == {"first.jpeg", "second.jpeg"}
    assert all(image["preview"].startswith("data:image/jpeg;base64,") for image in payload["images"])
    assert {item["photo_id"] for item in payload["quality"]} == {
        image["id"] for image in payload["images"]
    }


def test_rejects_unsupported_upload(client: TestClient) -> None:
    response = client.post(
        "/api/analyze",
        files=[("files", ("notes.txt", b"not an image", "text/plain"))],
    )
    assert response.status_code == 415


def test_rejects_oversized_upload(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pixsort.main.MAX_UPLOAD_BYTES", 4)
    response = client.post(
        "/api/analyze",
        files=[("files", ("photo.jpeg", b"12345", "image/jpeg"))],
    )
    assert response.status_code == 413


def _wait_for_terminal(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    pytest.fail("analysis job did not reach a terminal state")


def test_analysis_job_can_be_created_and_polled(client: TestClient) -> None:
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as stream:
        response = client.post(
            "/api/analyze/jobs",
            files=[("files", ("photo.jpeg", stream, "image/jpeg"))],
        )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    completed = _wait_for_terminal(client, job_id)
    assert completed["status"] == "completed"
    assert completed["progress_percent"] == 100
    assert completed["processed_images"] == completed["total_images"] == 1
    assert completed["results"]["images"][0]["filename"] == "photo.jpeg"


def test_analysis_job_reports_detection_stages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages: list[str] = []

    def analyze(*args: object, **kwargs: object) -> object:
        callback = kwargs["progress_callback"]
        callback(1, 1, "Detecting duplicates")
        stages.append("Detecting duplicates")
        callback(1, 1, "Detecting near duplicates")
        stages.append("Detecting near duplicates")
        callback(1, 1, "Detecting visual similarity")
        stages.append("Detecting visual similarity")
        callback(1, 1, "Preparing results")
        return AnalysisReport(summary={"discovered": 0, "processed": 0, "errors": 0})

    monkeypatch.setattr("pixsort.main.analyze_directory", analyze)
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as stream:
        response = client.post(
            "/api/analyze/jobs",
            files=[("files", ("photo.jpeg", stream, "image/jpeg"))],
        )
    completed = _wait_for_terminal(client, response.json()["job_id"])
    assert completed["status"] == "completed"
    assert stages == [
        "Detecting duplicates",
        "Detecting near duplicates",
        "Detecting visual similarity",
    ]


def test_analysis_job_reports_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("test analysis failure")

    monkeypatch.setattr("pixsort.main.analyze_directory", fail)
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as stream:
        response = client.post(
            "/api/analyze/jobs",
            files=[("files", ("photo.jpeg", stream, "image/jpeg"))],
        )
    failed = _wait_for_terminal(client, response.json()["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "test analysis failure"
    assert failed["progress_percent"] < 100


def test_analysis_job_can_be_cancelled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def cancel(*args: object, **kwargs: object) -> object:
        cancel_event = kwargs["cancel_event"]
        while not cancel_event.is_set():
            time.sleep(0.005)
        raise AnalysisCancelled("Analysis cancelled by user.")

    monkeypatch.setattr("pixsort.main.analyze_directory", cancel)
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as stream:
        response = client.post(
            "/api/analyze/jobs",
            files=[("files", ("photo.jpeg", stream, "image/jpeg"))],
        )
    job_id = response.json()["job_id"]
    cancel_response = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 200
    cancelled = _wait_for_terminal(client, job_id)
    assert cancelled["status"] == "cancelled"
    assert cancelled["progress_percent"] < 100


def test_unknown_analysis_job_is_rejected(client: TestClient) -> None:
    response = client.get("/api/jobs/missing")
    assert response.status_code == 404


def test_legacy_analysis_failure_is_logged_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from fastapi.testclient import TestClient

    def fail(*args: object, **kwargs: object) -> object:
        raise RuntimeError("private internal failure")

    monkeypatch.setattr("pixsort.main.analyze_directory", fail)
    caplog.set_level("ERROR", logger="pixsort.api")
    image = get_fixture_dir() / "10.jpeg"
    with image.open("rb") as stream:
        with TestClient(create_app(), raise_server_exceptions=False) as failing_client:
            response = failing_client.post(
                "/api/analyze",
                files=[("files", ("photo.jpeg", stream, "image/jpeg"))],
            )
    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Photo analysis failed. Check the PixSort server log for details."
    )
    assert "private internal failure" in caplog.text

def test_medium_confidence_near_duplicates_require_review() -> None:
    from pixsort.main import _build_groups

    report = AnalysisReport(recommendations=[{
        "category": "near_duplicate_candidate", "paths": ["a", "b"],
        "preferred_path": "a", "confidence": "medium",
        "removal_candidate_paths": ["b"], "reasons": ["Similar structure"],
    }])
    group = _build_groups(report, {"a": "photo-a", "b": "photo-b"})[0]
    assert group["needs_review"] is True
    assert group["removal_candidate_ids"] == []
