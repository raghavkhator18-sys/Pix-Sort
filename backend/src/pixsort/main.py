"""FastAPI adapter around the local PixSort analysis engine."""

import base64
import hashlib
import html
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import tempfile
from threading import Event, Lock
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, ImageOps

import os
from pydantic import BaseModel

from .config import AnalysisConfig
from .google_drive import (
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveAuthRequired,
    GoogleDriveConfigError,
    GoogleDriveNotFoundError,
    GoogleDriveRateLimitError,
    GoogleOAuthManager,
    load_env,
    parse_google_drive_url,
)
from .pipeline import AnalysisCancelled, AnalysisReport, analyze_directory

# Ensure environment variables are loaded
load_env()

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_COUNT = 200
logger = logging.getLogger("pixsort.api")


class GoogleDriveImportRequest(BaseModel):
    url: str


@dataclass
class AnalysisJob:
    job_id: str
    directory: Path
    filenames: list[str]
    total_images: int
    cancel_event: Event = field(default_factory=Event)
    status: str = "queued"
    progress_percent: int = 0
    processed_images: int = 0
    current_stage: str = "Queued"
    results: dict[str, object] | None = None
    error: str | None = None


def _replace_paths(value: object, path_map: dict[str, str]) -> object:
    if isinstance(value, str):
        return path_map.get(value, value)
    if isinstance(value, list):
        return [_replace_paths(item, path_map) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, path_map) for key, item in value.items()}
    return value


def _thumbnail_data_url(path: Path, size: tuple[int, int] = (320, 240)) -> str | None:
    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail(size)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=82)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except (OSError, ValueError):
        return None


def _build_groups(report: AnalysisReport, image_ids: dict[str, str]) -> list[dict[str, object]]:
    report_dict = report.to_dict()
    recommendations = report_dict["recommendations"]
    groups: list[tuple[str, list[str], dict[str, object]]] = []
    used: set[str] = set()
    priority = ("exact_duplicate", "possible_burst", "near_duplicate_candidate", "similar_only")
    for category in priority:
        for recommendation in recommendations:
            if recommendation.get("category") != category:
                continue
            paths = sorted(str(path) for path in recommendation.get("paths", []))
            candidate = [path for path in paths if path not in used]
            if len(candidate) >= 2:
                groups.append((category, candidate, recommendation))
                used.update(candidate)

    result = []
    for category, paths, recommendation in groups:
        member_ids = [image_ids[path] for path in paths]
        digest = hashlib.sha256(
            f"{category}:{','.join(sorted(member_ids))}".encode()
        ).hexdigest()[:12]
        preferred_path = recommendation.get("preferred_path")
        confidence = str(recommendation.get("confidence", "low"))
        removal_paths = [
            str(path) for path in recommendation.get("removal_candidate_paths", [])
        ]
        # ONLY exact duplicates and verified high-confidence near-duplicates should be auto_safe.
        # Possible bursts and similar-only photos must ALWAYS require user review with no pre-selection.
        is_duplicate = category in {"exact_duplicate", "near_duplicate_candidate"}
        auto_safe = bool(
            is_duplicate
            and preferred_path
            and confidence in {"high", "medium"}
            and (category == "exact_duplicate" or confidence == "high")
            and removal_paths
            and category != "possible_burst"
            and category != "similar_only"
        )
        result.append({
            "id": f"group-{digest}",
            "type": category,
            "member_photo_ids": member_ids,
            "member_paths": paths,
            "similarity": "exact" if category == "exact_duplicate" else category,
            "keeper_photo_id": image_ids.get(str(preferred_path)) if preferred_path else None,
            "keeper_path": preferred_path,
            "confidence": confidence,
            "needs_review": not bool(auto_safe),
            "recommended_keeper_id": image_ids.get(str(preferred_path)) if preferred_path else None,
            "recommendation_confidence": confidence,
            "recommendation_reason": " ".join(
                str(reason) for reason in recommendation.get("reasons", [])
            ),
            "removal_candidate_ids": [
                image_ids[path] for path in removal_paths if path in image_ids
            ] if auto_safe else [],
        })
    return result


def _report_response(
    report: AnalysisReport, original_filenames: list[str]
) -> dict[str, object]:
    filename_counts: dict[str, int] = {}
    image_ids: dict[str, str] = {}
    for record, filename in zip(report.images, original_filenames):
        occurrence = filename_counts.get(filename, 0)
        filename_counts[filename] = occurrence + 1
        stable_key = f"{filename}:{occurrence}".encode()
        image_ids[record.path] = f"photo-{hashlib.sha256(stable_key).hexdigest()[:12]}"
    path_map = {
        record.path: original_filenames[index]
        for index, record in enumerate(report.images)
    }
    response = _replace_paths(report.to_dict(), path_map)
    response["groups"] = _build_groups(report, image_ids)
    for group in response["groups"]:
        group["member_paths"] = [
            path_map.get(path, path) for path in group["member_paths"]
        ]
        if group["keeper_path"]:
            group["keeper_path"] = path_map.get(group["keeper_path"], group["keeper_path"])
    response["progress"] = {"percent": 100, "stage": "Preparing results"}
    response["quality"] = [
        {**item, "photo_id": image_ids[quality.path]}
        for item, quality in zip(response["quality"], report.quality)
    ]
    grouped_ids = {
        photo_id for group in response["groups"] for photo_id in group["member_photo_ids"]
    }
    response["images"] = [
        {
            **image,
            "id": image_ids[report.images[index].path],
            "filename": original_filenames[index],
            "path": original_filenames[index],
            "thumbnail": report.images[index].thumbnail or _thumbnail_data_url(record_path),
            "preview": _thumbnail_data_url(record_path, (1280, 1280))
            if image_ids[report.images[index].path] in grouped_ids else None,
        }
        for index, (image, record_path) in enumerate(zip(
            response["images"],
            (Path(record.path) for record in report.images),
        ))
    ]
    return response


def _job_response(job: AnalysisJob) -> dict[str, object]:
    response: dict[str, object] = {
        "job_id": job.job_id,
        "status": job.status,
        "progress_percent": job.progress_percent,
        "processed_images": job.processed_images,
        "total_images": job.total_images,
        "current_stage": job.current_stage,
    }
    if job.results is not None:
        response["results"] = job.results
    if job.error is not None:
        response["error"] = job.error
    return response


def create_app(
    oauth_manager: GoogleOAuthManager | None = None,
    gdrive_client: GoogleDriveClient | None = None,
):
    from fastapi import Cookie, Header, FastAPI, File, HTTPException, Response, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse

    if oauth_manager is None:
        oauth_manager = GoogleOAuthManager()

    app = FastAPI(title="PixSort local API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    jobs: dict[str, AnalysisJob] = {}
    jobs_lock = Lock()
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pixsort-analysis")

    def update_gdrive_stage(job: AnalysisJob, stage_msg: str) -> None:
        with jobs_lock:
            job.current_stage = stage_msg

    def update_job_progress(
        job: AnalysisJob,
        done: int,
        total: int,
        stage: str,
        start_percent: int = 0,
        span_percent: int = 90,
    ) -> None:
        with jobs_lock:
            processed = min(job.total_images, max(0, done))
            fraction = (processed / total) if total > 0 else 0
            if stage.startswith("Detecting visual"):
                stage_name = "Comparing visually similar photos"
                percent = start_percent + int(span_percent * 0.94)
            elif stage.startswith("Detecting near"):
                stage_name = "Detecting exact and near duplicates"
                percent = start_percent + int(span_percent * 0.88)
            elif stage.startswith("Detecting duplicates"):
                stage_name = "Detecting exact and near duplicates"
                percent = start_percent + int(span_percent * 0.82)
            elif stage.startswith("Preparing"):
                stage_name = "Preparing results"
                percent = start_percent + int(span_percent * 0.98)
            elif stage.startswith("Analyzing"):
                stage_name = f"Analyzing image quality ({processed}/{total})"
                percent = start_percent + int(fraction * (span_percent * 0.80))
            elif stage.startswith("Loading"):
                stage_name = "Preparing images"
                percent = start_percent + int(fraction * 5)
            else:
                stage_name = stage
                percent = start_percent + int(fraction * span_percent)

            job.progress_percent = max(job.progress_percent, min(99, percent))
            job.current_stage = stage_name
            job.processed_images = max(job.processed_images, processed)

    def run_gdrive_job(
        job: AnalysisJob, client: GoogleDriveClient, resource_id: str, is_folder: bool
    ) -> None:
        with jobs_lock:
            job.status = "running"
            job.current_stage = "Scanning Google Drive folder"
        try:
            files_to_download, skipped_files = client.scan_recursive(
                resource_id,
                is_folder=is_folder,
                cancel_event=job.cancel_event,
                max_count=MAX_UPLOAD_COUNT,
                progress_callback=lambda stage_msg: update_gdrive_stage(job, stage_msg),
            )
            total_files = len(files_to_download)
            with jobs_lock:
                job.total_images = total_files
                job.current_stage = f"Downloading images (0/{total_files})"

            original_filenames: list[str] = []
            downloaded_paths: list[Path] = []
            supported_extensions = AnalysisConfig().supported_extensions
            download_lock = Lock()
            completed_downloads = 0

            # Bounded concurrent downloads (max 4 concurrent workers)
            def download_worker(index: int, item: Any) -> None:
                nonlocal completed_downloads
                if job.cancel_event.is_set():
                    raise AnalysisCancelled("Analysis cancelled by user.")
                suffix = Path(item.name).suffix.lower()
                if suffix not in supported_extensions:
                    suffix = ".jpg"
                dest = job.directory / f"{index:04d}-{uuid4().hex}{suffix}"
                try:
                    client.download_file(
                        item.id,
                        dest,
                        max_bytes=MAX_UPLOAD_BYTES,
                        cancel_event=job.cancel_event,
                    )
                    with Image.open(dest) as img:
                        img.verify()
                    with download_lock:
                        downloaded_paths.append(dest)
                        original_filenames.append(item.relative_path)
                        completed_downloads += 1
                        with jobs_lock:
                            job.current_stage = f"Downloading images ({completed_downloads}/{total_files})"
                            job.processed_images = completed_downloads
                            job.progress_percent = min(30, int(completed_downloads * 30 / max(1, total_files)))
                except AnalysisCancelled:
                    dest.unlink(missing_ok=True)
                    raise
                except Exception as exc:
                    dest.unlink(missing_ok=True)
                    logger.warning("Skipping invalid or failed download %s: %s", item.name, exc)
                    with download_lock:
                        skipped_files.append({"name": item.relative_path, "reason": f"download_failed: {exc}"})
                        completed_downloads += 1
                        with jobs_lock:
                            job.current_stage = f"Downloading images ({completed_downloads}/{total_files})"
                            job.processed_images = completed_downloads
                            job.progress_percent = min(30, int(completed_downloads * 30 / max(1, total_files)))

            with ThreadPoolExecutor(max_workers=min(4, max(1, total_files))) as dl_pool:
                futures = [
                    dl_pool.submit(download_worker, idx, item)
                    for idx, item in enumerate(files_to_download, 1)
                ]
                for future in futures:
                    future.result()

            if not downloaded_paths:
                raise RuntimeError("No valid images could be retrieved from Google Drive.")

            # Stable sort by filename
            paired = sorted(zip(downloaded_paths, original_filenames), key=lambda x: str(x[0]))
            downloaded_paths = [p for p, _ in paired]
            original_filenames = [f for _, f in paired]
            job.filenames = original_filenames

            with jobs_lock:
                job.total_images = len(downloaded_paths)
                job.processed_images = 0
                job.current_stage = "Preparing images"
                job.progress_percent = max(job.progress_percent, 30)

            report = analyze_directory(
                job.directory,
                cancel_event=job.cancel_event,
                progress_callback=lambda done, total, stage: update_job_progress(
                    job, done, total, stage, start_percent=30, span_percent=68
                ),
            )
            results = _report_response(report, original_filenames)
            if skipped_files:
                results["skipped_files"] = skipped_files

            with jobs_lock:
                job.results = results
                job.progress_percent = 100
                job.processed_images = job.total_images
                job.current_stage = "Analysis complete"
                job.status = "completed"
        except AnalysisCancelled as exc:
            with jobs_lock:
                job.status = "cancelled"
                job.error = str(exc)
        except Exception as exc:
            logger.exception("Google Drive analysis job %s failed", job.job_id)
            with jobs_lock:
                job.status = "failed"
                job.error = str(exc)
        finally:
            import shutil
            shutil.rmtree(job.directory, ignore_errors=True)

    def run_job(job: AnalysisJob) -> None:
        with jobs_lock:
            job.status = "running"
            job.current_stage = "Preparing images"
        try:
            report = analyze_directory(
                job.directory,
                cancel_event=job.cancel_event,
                progress_callback=lambda done, total, stage: update_job_progress(
                    job, done, total, stage, start_percent=0, span_percent=98
                ),
            )
            results = _report_response(report, job.filenames)
            with jobs_lock:
                job.results = results
                job.progress_percent = 100
                job.processed_images = job.total_images
                job.current_stage = "Analysis complete"
                job.status = "completed"
        except AnalysisCancelled as exc:
            with jobs_lock:
                job.status = "cancelled"
                job.error = str(exc)
        except Exception as exc:
            logger.exception("Analysis job %s failed", job.job_id)
            with jobs_lock:
                job.status = "failed"
                job.error = str(exc)
        finally:
            import shutil
            shutil.rmtree(job.directory, ignore_errors=True)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/analyze/jobs")
    async def start_analysis_job(
        files: list[UploadFile] = File(description="Local photo files"),
    ) -> dict[str, object]:
        if not files:
            raise HTTPException(status_code=400, detail="Select at least one image.")
        if len(files) > MAX_UPLOAD_COUNT:
            raise HTTPException(
                status_code=413,
                detail=f"Upload at most {MAX_UPLOAD_COUNT} images at a time.",
            )
        upload_dir = Path(tempfile.mkdtemp(prefix="pixsort-upload-"))
        original_filenames: list[str] = []
        try:
            supported_extensions = AnalysisConfig().supported_extensions
            for upload in files:
                original_filename = upload.filename or "unnamed file"
                suffix = Path(original_filename).suffix.lower()
                if suffix not in supported_extensions:
                    raise HTTPException(
                        status_code=415,
                        detail=f"Unsupported image type: {original_filename}",
                    )
                destination = upload_dir / f"{len(original_filenames):04d}-{uuid4().hex}{suffix}"
                original_filenames.append(original_filename)
                size = 0
                try:
                    with destination.open("wb") as output:
                        while chunk := await upload.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413,
                                    detail=f"{upload.filename} exceeds the 25 MB file limit.",
                                )
                            output.write(chunk)
                finally:
                    await upload.close()
        except Exception:
            import shutil
            shutil.rmtree(upload_dir, ignore_errors=True)
            raise
        job = AnalysisJob(
            job_id=uuid4().hex,
            directory=upload_dir,
            filenames=original_filenames,
            total_images=len(original_filenames),
        )
        with jobs_lock:
            jobs[job.job_id] = job
        executor.submit(run_job, job)
        return {"job_id": job.job_id, "status": job.status}

    @app.get("/api/jobs/{job_id}")
    def get_analysis_job(job_id: str) -> dict[str, object]:
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Analysis job not found.")
            return _job_response(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_analysis_job(job_id: str) -> dict[str, object]:
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Analysis job not found.")
            if job.status in {"queued", "running"}:
                job.cancel_event.set()
            return _job_response(job)

    @app.get("/api/gdrive/config")
    def gdrive_config(
        pixsort_session: str | None = Cookie(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:].strip()
        elif pixsort_session:
            token = oauth_manager.get_token(pixsort_session)
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        return {
            "configured": oauth_manager.is_configured() or bool(api_key),
            "oauth_available": oauth_manager.is_configured(),
            "api_key_available": bool(api_key),
            "authorized": bool(token),
        }

    @app.get("/api/auth/google/url")
    def google_auth_url(redirect_uri: str | None = None) -> dict[str, str]:
        if not oauth_manager.is_configured():
            raise HTTPException(
                status_code=503,
                detail="Google Drive API credentials are not configured on this server. Please configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the environment.",
            )
        url, state = oauth_manager.create_authorization_url(redirect_uri=redirect_uri)
        return {"url": url, "state": state}

    @app.get("/api/auth/google/callback", response_class=HTMLResponse)
    def google_auth_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        response: Response = None,
    ) -> str:
        if error:
            safe_error = json.dumps(str(error)).replace("<", "\\u003c")
            return (
                "<!DOCTYPE html><html><body>"
                "<script>"
                f"if (window.opener) {{ window.opener.postMessage({{ type: 'GOOGLE_AUTH_ERROR', error: {safe_error} }}, '*'); window.close(); }} else {{ window.location.href = '/'; }}"
                "</script>"
                f"<p>Authorization failed: {html.escape(error)}</p></body></html>"
            )
        if not code or not state or not oauth_manager.verify_state(state):
            return (
                "<!DOCTYPE html><html><body>"
                "<script>"
                "if (window.opener) { window.opener.postMessage({ type: 'GOOGLE_AUTH_ERROR', error: 'Invalid or expired state.' }, '*'); window.close(); } else { window.location.href = '/'; }"
                "</script>"
                "<p>Invalid authorization request.</p></body></html>"
            )
        try:
            tokens = oauth_manager.exchange_code(code)
            session_id = uuid4().hex
            oauth_manager.store_token(session_id, tokens)
            if response:
                response.set_cookie("pixsort_session", session_id, httponly=True, samesite="lax")
            return (
                "<!DOCTYPE html><html><body>"
                "<script>"
                "if (window.opener) { window.opener.postMessage({ type: 'GOOGLE_AUTH_SUCCESS' }, '*'); window.close(); } else { window.location.href = '/'; }"
                "</script>"
                "<p>Connected to Google Drive! You can close this window.</p></body></html>"
            )
        except Exception as exc:
            safe_exc = json.dumps(str(exc)).replace("<", "\\u003c")
            return (
                f"<!DOCTYPE html><html><body><script>if (window.opener) {{ window.opener.postMessage({{ type: 'GOOGLE_AUTH_ERROR', error: {safe_exc} }}, '*'); window.close(); }} else {{ window.location.href = '/'; }}</script><p>Authorization exchange failed.</p></body></html>"
            )

    @app.post("/api/auth/google/disconnect")
    def google_auth_disconnect(
        response: Response,
        pixsort_session: str | None = Cookie(default=None),
    ) -> dict[str, str]:
        if pixsort_session:
            oauth_manager.clear_token(pixsort_session)
        response.delete_cookie("pixsort_session")
        return {"status": "disconnected"}

    @app.get("/api/gdrive/folders")
    def list_gdrive_folders(
        pixsort_session: str | None = Cookie(default=None),
        authorization: str | None = Header(default=None),
    ) -> list[dict[str, Any]]:
        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:].strip()
        elif pixsort_session:
            token = oauth_manager.get_token(pixsort_session)

        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        client = gdrive_client
        if client is None:
            if not token and not api_key:
                raise HTTPException(
                    status_code=401,
                    detail="Please connect your Google Drive account to browse your folders.",
                )
            client = GoogleDriveClient(api_key=api_key, access_token=token)

        try:
            return client.list_user_folders()
        except GoogleDriveAuthRequired as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        except Exception as exc:
            logger.exception("Failed to list Google Drive folders")
            raise HTTPException(status_code=500, detail="Failed to retrieve folders from Google Drive.")

    @app.post("/api/gdrive/import")
    async def import_from_gdrive(
        payload: GoogleDriveImportRequest,
        pixsort_session: str | None = Cookie(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        try:
            resource_id, kind = parse_google_drive_url(payload.url)
        except GoogleDriveError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:].strip()
        elif pixsort_session:
            token = oauth_manager.get_token(pixsort_session)

        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        client = gdrive_client
        if client is None:
            if not token and not api_key and not oauth_manager.is_configured():
                raise HTTPException(
                    status_code=503,
                    detail="Google Drive API credentials are not configured on this server. Please configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (or GOOGLE_API_KEY) in the environment.",
                )
            client = GoogleDriveClient(api_key=api_key, access_token=token)

        try:
            meta = client.get_metadata(resource_id)
        except GoogleDriveAuthRequired as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        except GoogleDriveNotFoundError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        except GoogleDriveRateLimitError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        except GoogleDriveError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
        except Exception as exc:
            logger.exception("Failed to verify Google Drive resource")
            raise HTTPException(status_code=500, detail="Failed to access Google Drive.")

        mime = meta.get("mimeType", "")
        if mime == "application/vnd.google-apps.shortcut":
            details = meta.get("shortcutDetails", {})
            target_id = details.get("targetId")
            target_mime = details.get("targetMimeType", "")
            if target_id:
                resource_id = target_id
                mime = target_mime

        is_folder = (mime == "application/vnd.google-apps.folder") or (kind == "folder")

        upload_dir = Path(tempfile.mkdtemp(prefix="pixsort-gdrive-"))
        job = AnalysisJob(
            job_id=uuid4().hex,
            directory=upload_dir,
            filenames=[],
            total_images=0,
            current_stage="Scanning Google Drive folder",
        )
        with jobs_lock:
            jobs[job.job_id] = job

        executor.submit(run_gdrive_job, job, client, resource_id, is_folder)
        return {"job_id": job.job_id, "status": job.status}

    @app.post("/api/analyze")
    async def analyze(
        files: list[UploadFile] = File(description="Local photo files"),
    ) -> dict[str, object]:
        if not files:
            raise HTTPException(status_code=400, detail="Select at least one image.")
        if len(files) > MAX_UPLOAD_COUNT:
            raise HTTPException(
                status_code=413,
                detail=f"Upload at most {MAX_UPLOAD_COUNT} images at a time.",
            )

        supported_extensions = AnalysisConfig().supported_extensions
        with tempfile.TemporaryDirectory(prefix="pixsort-upload-") as temp_dir:
            upload_dir = Path(temp_dir)
            original_filenames: list[str] = []
            for upload in files:
                original_filename = upload.filename or "unnamed file"
                suffix = Path(original_filename).suffix.lower()
                if suffix not in supported_extensions:
                    await upload.close()
                    raise HTTPException(
                        status_code=415,
                        detail=f"Unsupported image type: {original_filename}",
                    )
                destination = upload_dir / f"{len(original_filenames):04d}-{uuid4().hex}{suffix}"
                original_filenames.append(original_filename)
                size = 0
                try:
                    with destination.open("wb") as output:
                        while chunk := await upload.read(1024 * 1024):
                            size += len(chunk)
                            if size > MAX_UPLOAD_BYTES:
                                raise HTTPException(
                                    status_code=413,
                                    detail=f"{upload.filename} exceeds the 25 MB file limit.",
                                )
                            output.write(chunk)
                finally:
                    await upload.close()

            try:
                report = analyze_directory(upload_dir)
                return _report_response(report, original_filenames)
            except Exception as exc:
                logger.exception("Legacy analysis request failed")
                raise HTTPException(
                    status_code=500,
                    detail="Photo analysis failed. Check the PixSort server log for details.",
                ) from exc

    return app


app = create_app()


__all__ = ["MAX_UPLOAD_BYTES", "MAX_UPLOAD_COUNT", "app", "create_app"]
