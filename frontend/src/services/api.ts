import type { AnalysisJobStatus, Report } from "../types";

export async function analyzePhotos(files: FileList, signal?: AbortSignal): Promise<Report> {
  const body = new FormData();
  Array.from(files).forEach((file) => body.append("files", file));
  const response = await fetch("/api/analyze", { method: "POST", body, signal });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail ?? "Analysis failed.");
  return payload as Report;
}

async function readJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(
      typeof payload.detail === "string"
        ? payload.detail
        : `Request failed with HTTP ${response.status}.`,
    );
    (error as Error & { status?: number; code?: string }).status = response.status;
    (error as Error & { status?: number; code?: string }).code = payload.code;
    throw error;
  }
  return payload as T;
}

export async function startAnalysisJob(files: FileList, signal?: AbortSignal): Promise<{ job_id: string }> {
  const body = new FormData();
  Array.from(files).forEach((file) => body.append("files", file));
  const result = await readJson<Partial<{ job_id: string }>>(
    await fetch("/api/analyze/jobs", { method: "POST", body, signal }),
  );
  if (!result.job_id) throw new Error("The analysis server returned no job ID.");
  return { job_id: result.job_id };
}

export async function getAnalysisJob(jobId: string, signal?: AbortSignal): Promise<AnalysisJobStatus> {
  const result = await readJson<Partial<AnalysisJobStatus>>(
    await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { signal }),
  );
  if (
    !result.job_id ||
    !result.status ||
    typeof result.progress_percent !== "number" ||
    typeof result.processed_images !== "number" ||
    typeof result.total_images !== "number" ||
    typeof result.current_stage !== "string"
  ) {
    throw new Error("The analysis server returned an invalid job status.");
  }
  if (
    result.total_images < 0 ||
    result.processed_images < 0 ||
    result.processed_images > result.total_images ||
    result.progress_percent < 0 ||
    result.progress_percent > 100
  ) {
    throw new Error("The analysis server returned invalid progress values.");
  }
  return result as AnalysisJobStatus;
}

export async function cancelAnalysisJob(jobId: string): Promise<AnalysisJobStatus> {
  return readJson(await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }));
}

export async function getGoogleDriveConfig(): Promise<import("../types").GoogleDriveConfig> {
  return readJson(await fetch("/api/gdrive/config"));
}

export async function getGoogleAuthUrl(redirectUri?: string): Promise<{ url: string; state: string }> {
  const params = redirectUri ? `?redirect_uri=${encodeURIComponent(redirectUri)}` : "";
  return readJson(await fetch(`/api/auth/google/url${params}`));
}

export async function importFromGoogleDrive(url: string, signal?: AbortSignal): Promise<{ job_id: string }> {
  const result = await readJson<Partial<{ job_id: string }>>(
    await fetch("/api/gdrive/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
      signal,
    }),
  );
  if (!result.job_id) throw new Error("The server returned no job ID for the Google Drive import.");
  return { job_id: result.job_id };
}

export async function disconnectGoogleDrive(): Promise<void> {
  await readJson(await fetch("/api/auth/google/disconnect", { method: "POST" }));
}

export async function listGoogleDriveFolders(): Promise<Array<{ id: string; name: string; modifiedTime?: string }>> {
  return readJson(await fetch("/api/gdrive/folders"));
}
